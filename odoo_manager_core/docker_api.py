"""Lecture de l'état de Docker par l'API du moteur, sans lancer la CLI.

Chaque appel de `docker.exe` coûte 150 ms et deux processus sous Windows, analysés
par l'antivirus ; `docker info` en lançait quatorze de plus pour ses plugins. Les
mêmes réponses arrivent en 3 à 12 ms par le named pipe du moteur.

Le client ne sert qu'aux lectures d'état (version, conteneurs). Les actions passent
toujours par la CLI, et l'API n'est utilisée que lorsque la CLI parlerait au même
moteur : contexte Docker par défaut, pas de `DOCKER_HOST` distant, pas d'exécution
dans WSL et pas d'exécutable Docker personnalisé. Toute erreur rend la main à la CLI.
"""

import json
import os
import socket
import threading
from pathlib import Path
from typing import NamedTuple

from .platform import platform_id


DEFAULT_TIMEOUT_SECONDS = 4
# Contextes dont le point de terminaison est celui des chemins par défaut ci-dessous.
LOCAL_CONTEXTS = {"", "default", "desktop-linux"}
WINDOWS_PIPE = r"\\.\pipe\docker_engine"
UNIX_SOCKETS = ("/var/run/docker.sock",)


class EngineUnavailable(RuntimeError):
    """Le moteur n'est pas joignable par l'API : la CLI reprend la main."""


class EngineEndpoint(NamedTuple):
    kind: str  # "npipe" ou "unix"
    address: str


def parse_docker_host(value):
    """Point de terminaison local d'un DOCKER_HOST, ou None s'il est distant ou illisible."""
    value = str(value or "").strip()
    if not value:
        return None
    if value.startswith("npipe://"):
        return EngineEndpoint("npipe", "\\\\" + value[len("npipe://"):].lstrip("/").replace("/", "\\"))
    if value.startswith("unix://"):
        return EngineEndpoint("unix", value[len("unix://"):] or "/var/run/docker.sock")
    return None


def current_docker_context(home=None):
    try:
        payload = json.loads((Path(home or Path.home()) / ".docker" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    context = payload.get("currentContext") if isinstance(payload, dict) else ""
    return str(context or "").strip()


def local_endpoint(platform_name=None, home=None, exists=None):
    platform_name = platform_name or platform_id()
    exists = exists or os.path.exists
    if platform_name == "windows":
        return EngineEndpoint("npipe", WINDOWS_PIPE)
    candidates = [*UNIX_SOCKETS, str(Path(home or Path.home()) / ".docker" / "run" / "docker.sock")]
    for candidate in candidates:
        if exists(candidate):
            return EngineEndpoint("unix", candidate)
    return None


def engine_endpoint(settings, environ=None, platform_name=None, home=None, exists=None, uses_wsl=False):
    """Point de terminaison utilisable, ou None s'il faut s'en tenir à la CLI."""
    environ = os.environ if environ is None else environ
    if uses_wsl:
        return None
    if str(getattr(settings, "docker_executable", "docker")).strip() not in {"", "docker"}:
        return None
    docker_host = environ.get("DOCKER_HOST", "")
    if docker_host:
        return parse_docker_host(docker_host)
    if current_docker_context(home) not in LOCAL_CONTEXTS:
        return None
    return local_endpoint(platform_name, home, exists)


def parse_http_response(raw):
    """Code et corps d'une réponse HTTP/1.1, y compris en découpage par morceaux."""
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise ValueError("Réponse HTTP tronquée.")
    lines = head.split(b"\r\n")
    parts = lines[0].split(b" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError("Statut HTTP illisible.")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.strip().lower()] = value.strip()
    if headers.get(b"transfer-encoding", b"").lower() == b"chunked":
        body = decode_chunked(body)
    return int(parts[1]), body


def decode_chunked(body):
    decoded = b""
    while True:
        size_line, separator, remainder = body.partition(b"\r\n")
        if not separator:
            raise ValueError("Corps découpé tronqué.")
        try:
            size = int(size_line.split(b";", 1)[0], 16)
        except ValueError as exc:
            raise ValueError("Taille de morceau illisible.") from exc
        if size == 0:
            return decoded
        decoded += remainder[:size]
        body = remainder[size + 2:]


def read_endpoint(endpoint, request):
    """Envoie une requête et lit toute la réponse, sans dépendance externe."""
    if endpoint.kind == "npipe":
        # Le named pipe se lit comme un fichier : aucune API Windows supplémentaire.
        with open(endpoint.address, "r+b", buffering=0) as pipe:
            pipe.write(request)
            chunks = []
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(DEFAULT_TIMEOUT_SECONDS)
        client.connect(endpoint.address)
        client.sendall(request)
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


class DockerEngineClient:
    """Lectures d'état du moteur Docker. Toute erreur lève EngineUnavailable."""

    def __init__(self, endpoint, reader=read_endpoint, timeout=DEFAULT_TIMEOUT_SECONDS):
        self.endpoint = endpoint
        self.reader = reader
        self.timeout = timeout

    def get(self, path):
        request = f"GET {path} HTTP/1.1\r\nHost: docker\r\nAccept: application/json\r\nConnection: close\r\n\r\n".encode()
        # Le named pipe n'expose pas de délai : la lecture est surveillée depuis ce thread.
        result = {}

        def read():
            try:
                result["raw"] = self.reader(self.endpoint, request)
            except Exception as exc:  # noqa: BLE001 - toute panne renvoie vers la CLI
                result["error"] = exc

        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        worker.join(self.timeout)
        if worker.is_alive():
            raise EngineUnavailable("Le moteur Docker ne répond pas.")
        if "error" in result:
            raise EngineUnavailable(str(result["error"]))
        try:
            status, body = parse_http_response(result["raw"])
        except ValueError as exc:
            raise EngineUnavailable(str(exc)) from exc
        if status >= 400:
            raise EngineUnavailable(f"Le moteur Docker a répondu {status}.")
        try:
            return json.loads(body.decode("utf-8", "replace") or "null")
        except ValueError as exc:
            raise EngineUnavailable("Réponse JSON illisible.") from exc

    def server_version(self):
        payload = self.get("/version")
        version = payload.get("Version") if isinstance(payload, dict) else ""
        if not version:
            raise EngineUnavailable("Version du moteur absente.")
        return str(version)

    def container_states(self):
        """État de chaque conteneur, comme `docker ps -a --format '{{.Names}}|{{.State}}'`."""
        payload = self.get("/containers/json?all=1")
        if not isinstance(payload, list):
            raise EngineUnavailable("Liste de conteneurs illisible.")
        states = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            state = str(item.get("State") or "").strip() or "absent"
            for name in item.get("Names") or []:
                states[str(name).lstrip("/")] = state
        return states

