"""Détection des instances Traefik présentes sur la machine et de leurs ports réels.

Les projets Odoo sont routés par Traefik via l'entrypoint `web`, le réseau `traefik-local`
et les middlewares `odoo-*@docker` définis par docker-local-tools. Une instance déjà
installée par l'utilisateur peut publier un autre port que 80, porter un autre nom de
conteneur ou ne pas remplir ces conditions : le gestionnaire doit le savoir avant
d'attendre une route qui n'arrivera jamais.
"""

import json
import re
import shlex
import threading
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath, PureWindowsPath


TRAEFIK_DEFAULT_HTTP_PORT = 80
TRAEFIK_NETWORK = "traefik-local"
TRAEFIK_PROJECT_ENTRYPOINT = "web"
TRAEFIK_PROJECT_MIDDLEWARES = ("odoo-forward", "odoo-compress", "odoo-headers")
TRAEFIK_CONFIG_FILENAMES = ("traefik.yml", "traefik.yaml", "traefik.toml")
TRAEFIK_CONFIG_PATHS = tuple(f"/etc/traefik/{name}" for name in TRAEFIK_CONFIG_FILENAMES)
DOCKER_PS_FIELDS = (
    "{{.ID}}", "{{.Names}}", "{{.Image}}", "{{.State}}", "{{.Ports}}", "{{.Networks}}",
    '{{.Label "com.docker.compose.project.working_dir"}}', "{{.Labels}}",
)
DOCKER_PORT_PATTERN = re.compile(
    r"^(?:(?P<ip>\[[0-9a-fA-F:]*\]|[0-9a-fA-F.:]*):)?(?P<host>\d+)(?:-(?P<host_end>\d+))?"
    r"->(?P<container>\d+)(?:-(?P<container_end>\d+))?/(?P<protocol>\w+)$"
)
IMAGE_PATTERN = re.compile(r"(?:^|/)traefik(?::|@|$)", re.IGNORECASE)
ENTRYPOINT_CACHE = {}
ENTRYPOINT_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class PublishedPort:
    host_ip: str
    host_port: int
    container_port: int
    protocol: str = "tcp"


@dataclass(frozen=True)
class TraefikInstance:
    container_id: str
    name: str
    image: str
    state: str
    published: tuple = ()
    networks: tuple = ()
    working_dir: str = ""
    labels: str = ""
    # None : configuration illisible, les entrypoints par défaut de Traefik sont supposés.
    entrypoints: dict = field(default=None, hash=False, compare=False)

    @property
    def running(self):
        return self.state == "running"

    @property
    def host_network(self):
        return "host" in self.networks

    @property
    def has_project_entrypoint(self):
        return self.entrypoints is None or TRAEFIK_PROJECT_ENTRYPOINT in self.entrypoints

    @property
    def http_entrypoint(self):
        """(nom, port conteneur) de l'entrypoint qui sert les projets."""
        entrypoints = self.entrypoints if self.entrypoints is not None else {TRAEFIK_PROJECT_ENTRYPOINT: 80}
        for name in (TRAEFIK_PROJECT_ENTRYPOINT, "http"):
            if name in entrypoints:
                return name, entrypoints[name]
        for name, port in entrypoints.items():
            if port == 80:
                return name, port
        candidates = [(name, port) for name, port in entrypoints.items() if name != "traefik"]
        return candidates[0] if candidates else (None, None)

    @property
    def http_port(self):
        """Port de la machine qui atteint l'entrypoint des projets, None s'il n'est pas publié."""
        _name, container_port = self.http_entrypoint
        if container_port is None:
            return None
        if self.host_network:
            return container_port
        candidates = [port for port in self.published if port.protocol == "tcp" and port.container_port == container_port]
        # Une publication IPv4 ou toutes interfaces est joignable par 127.0.0.1, contrairement à [::1].
        candidates.sort(key=lambda port: port.host_ip.startswith("[") or ":" in port.host_ip)
        return candidates[0].host_port if candidates else None

    @property
    def on_project_network(self):
        return self.host_network or TRAEFIK_NETWORK in self.networks

    @property
    def missing_middlewares(self):
        labels = self.labels.lower()
        return tuple(
            name for name in TRAEFIK_PROJECT_MIDDLEWARES
            if f"traefik.http.middlewares.{name}.".lower() not in labels
        )

    def compatibility_problems(self):
        """Raisons pour lesquelles les routes des projets ne peuvent pas fonctionner."""
        problems = []
        if not self.has_project_entrypoint:
            names = ", ".join(sorted(self.entrypoints)) or "aucun"
            problems.append(f"aucun entrypoint « {TRAEFIK_PROJECT_ENTRYPOINT} » (entrypoints : {names})")
        if self.http_port is None:
            problems.append("l'entrypoint HTTP n'est publié sur aucun port de cette machine")
        if not self.on_project_network:
            networks = ", ".join(self.networks) or "aucun"
            problems.append(f"conteneur absent du réseau {TRAEFIK_NETWORK} (réseaux : {networks})")
        return problems

    def describe(self):
        if not self.running:
            return f"{self.name} ({self.image}, {self.state})"
        port = self.http_port
        port_label = f"port HTTP {port}" if port else "aucun port HTTP publié"
        return f"{self.name} ({self.image}, {self.state}, {port_label})"


def parse_docker_ports(value):
    """Lit la colonne Ports de `docker ps` : `127.0.0.1:8080->80/tcp, :::8080->80/tcp, 443/tcp`."""
    ports = []
    for item in (value or "").split(","):
        match = DOCKER_PORT_PATTERN.match(item.strip())
        if not match:
            continue
        host = int(match.group("host"))
        container = int(match.group("container"))
        count = int(match.group("host_end") or host) - host + 1
        if match.group("container_end") and int(match.group("container_end")) - container + 1 != count:
            continue
        ports.extend(
            PublishedPort(match.group("ip") or "", host + offset, container + offset, match.group("protocol"))
            for offset in range(count)
        )
    return tuple(ports)


def address_port(address):
    match = re.search(r":(\d+)(?:/(tcp|udp))?\s*$", str(address or "").strip().strip("\"'"))
    if not match or match.group(2) == "udp":
        return None
    return int(match.group(1))


def entrypoints_from_arguments(arguments):
    """Entrypoints déclarés en ligne de commande ou par variables TRAEFIK_ENTRYPOINTS_*."""
    entrypoints = {}
    for argument in arguments or ():
        match = re.match(r"^--entry[pP]oints\.([^.=]+)\.address[=\s](.+)$", str(argument).strip())
        if match and address_port(match.group(2)) is not None:
            entrypoints[match.group(1)] = address_port(match.group(2))
    return entrypoints


def entrypoints_from_environment(variables):
    entrypoints = {}
    for variable in variables or ():
        match = re.match(r"^TRAEFIK_ENTRYPOINTS_([^=]+)_ADDRESS=(.*)$", str(variable), re.IGNORECASE)
        if match and address_port(match.group(2)) is not None:
            entrypoints[match.group(1).lower()] = address_port(match.group(2))
    return entrypoints


def entrypoints_from_config(text):
    """Entrypoints d'un fichier statique traefik.yml ou traefik.toml ; None s'il n'en déclare pas."""
    text = text or ""
    entrypoints = {}
    toml_sections = re.finditer(r"(?ms)^\s*\[entry[pP]oints\.([^\]\s.]+)\]\s*$(.*?)(?=^\s*\[|\Z)", text)
    for match in toml_sections:
        address = re.search(r"(?m)^\s*address\s*=\s*[\"']([^\"']+)[\"']", match.group(2))
        if address and address_port(address.group(1)) is not None:
            entrypoints[match.group(1)] = address_port(address.group(1))
    if entrypoints:
        return entrypoints

    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not re.match(r"^entry[pP]oints:\s*(#.*)?$", line):
            continue
        current = None
        child_indent = None
        for child in lines[index + 1:]:
            if not child.strip() or child.lstrip().startswith("#"):
                continue
            indent = len(child) - len(child.lstrip())
            if indent == 0:
                break
            if child_indent is None:
                child_indent = indent
            if indent == child_indent:
                name = re.match(r"^\s*[\"']?([^\"':\s]+)[\"']?:\s*$", child)
                current = name.group(1) if name else None
                continue
            address = re.match(r"^\s*address:\s*[\"']?([^\"'#]+?)[\"']?\s*(#.*)?$", child)
            if current and address and address_port(address.group(1)) is not None:
                entrypoints[current] = address_port(address.group(1))
        break
    return entrypoints or None


def compose_service_block(text, service):
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        if not re.match(rf"^(\s+){re.escape(service)}:\s*(#.*)?$", line):
            continue
        indent = len(line) - len(line.lstrip())
        block = []
        for child in lines[index + 1:]:
            if child.strip() and not child.lstrip().startswith("#") and len(child) - len(child.lstrip()) <= indent:
                break
            block.append(child)
        return block
    return None


def compose_service_container_name(text, service="traefik"):
    block = compose_service_block(text, service)
    for line in block or ():
        match = re.match(r"^\s*container_name:\s*[\"']?([^\"'#\s]+)", line)
        if match:
            return match.group(1)
    return None


def compose_service_ports(text, service="traefik"):
    """Ports courts d'un service compose, en conservant les ports de la machine choisis par l'utilisateur.

    Retourne None si la liste utilise une syntaxe non gérée (variables, syntaxe longue) :
    le compose doit alors rester tel quel plutôt que d'être réécrit avec des ports faux.
    """
    block = compose_service_block(text, service)
    if block is None:
        return None
    ports = []
    in_ports = False
    ports_indent = None
    for line in block:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if re.match(r"^\s*ports:\s*(!override\s*)?(#.*)?$", line):
            in_ports, ports_indent = True, indent
            continue
        if not in_ports:
            continue
        if indent <= ports_indent and not line.lstrip().startswith("-"):
            break
        item = re.match(r"^\s*-\s*[\"']?([^\"'#]+?)[\"']?\s*(#.*)?$", line)
        if not item or "$" in item.group(1):
            return None
        spec, _slash, protocol = item.group(1).partition("/")
        parts = spec.rsplit(":", 2)
        if not all(part.isdigit() for part in parts[-2:]) or len(parts) < 2:
            return None
        host_ip = parts[0] if len(parts) == 3 else ""
        ports.append(PublishedPort(host_ip, int(parts[-2]), int(parts[-1]), protocol or "tcp"))
    return ports


def same_directory(label, directory, execution_directory=None):
    """Compare le working_dir compose d'un conteneur au dossier Traefik (chemins Windows ou WSL)."""
    if not label or not directory:
        return False

    def normalize(value):
        value = str(value).strip()
        if re.match(r"^[A-Za-z]:[\\/]", value) or "\\" in value:
            return PureWindowsPath(value).as_posix().rstrip("/").lower()
        return PurePosixPath(value).as_posix().rstrip("/")

    candidates = {normalize(directory)}
    if execution_directory:
        candidates.add(normalize(execution_directory))
    return normalize(label) in candidates


def parse_instances(output):
    instances = []
    for line in (output or "").splitlines():
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        container_id, name, image, state, ports, networks, working_dir = fields[:7]
        labels = "\t".join(fields[7:])
        if not IMAGE_PATTERN.search(image.strip()) and name.strip() != "traefik":
            continue
        instances.append(
            TraefikInstance(
                container_id=container_id.strip(),
                name=name.strip(),
                image=image.strip(),
                state=state.strip().lower(),
                published=parse_docker_ports(ports),
                networks=tuple(item.strip() for item in networks.split(",") if item.strip()),
                working_dir=working_dir.strip(),
                labels=labels,
            )
        )
    return instances


def read_entrypoints(capture, docker_prefix, instance):
    """Lit les entrypoints d'un conteneur démarré : arguments, variables puis fichier statique."""
    code, output = capture(
        [*docker_prefix, "inspect", "-f", '{{json .Args}}{{"\\t"}}{{json .Config.Env}}', instance.container_id],
        timeout=5,
    )
    arguments, variables = [], []
    if code == 0 and output:
        raw_arguments, _tab, raw_variables = output.splitlines()[0].partition("\t")
        try:
            arguments = json.loads(raw_arguments) or []
            variables = json.loads(raw_variables) or []
        except ValueError:
            arguments, variables = [], []
    entrypoints = entrypoints_from_arguments(arguments)
    entrypoints.update(entrypoints_from_environment(variables))
    if entrypoints:
        return entrypoints
    if not instance.running:
        return None

    config_paths = list(TRAEFIK_CONFIG_PATHS)
    for argument in arguments:
        match = re.match(r"^--config[fF]ile[=\s](.+)$", str(argument))
        if match:
            config_paths.insert(0, match.group(1).strip())
    script = "for f in " + " ".join(shlex.quote(path) for path in config_paths) + '; do [ -f "$f" ] && cat "$f" && exit 0; done; exit 3'
    code, output = capture([*docker_prefix, "exec", instance.container_id, "sh", "-c", script], timeout=8)
    if code == 3:
        # Sans fichier ni argument, Traefik crée un unique entrypoint `http` sur :80.
        return {"http": 80}
    if code != 0:
        return None
    return entrypoints_from_config(output) or {"http": 80}


def reset_traefik_entrypoint_cache():
    with ENTRYPOINT_CACHE_LOCK:
        ENTRYPOINT_CACHE.clear()


def detect_traefik_instances(capture, docker_prefix):
    """Liste les conteneurs Traefik (démarrés ou non) ; None si Docker ne répond pas.

    Les entrypoints d'un conteneur démarré ne changent pas sans le recréer ou le redémarrer :
    ils sont mémorisés par identifiant, état et ports pour ne relancer ni inspect ni exec
    à chaque rafraîchissement de l'interface.
    """
    code, output = capture(
        [*docker_prefix, "ps", "-a", "--no-trunc", "--format", "\\t".join(DOCKER_PS_FIELDS)],
        timeout=8,
    )
    if code != 0:
        return None
    instances = []
    for instance in parse_instances(output):
        cache_key = (instance.container_id, instance.state, instance.published)
        with ENTRYPOINT_CACHE_LOCK:
            cached = ENTRYPOINT_CACHE.get(cache_key, False)
        if cached is not False:
            entrypoints = cached
        else:
            entrypoints = read_entrypoints(capture, docker_prefix, instance)
            if instance.running and entrypoints is not None:
                with ENTRYPOINT_CACHE_LOCK:
                    if len(ENTRYPOINT_CACHE) > 64:
                        ENTRYPOINT_CACHE.clear()
                    ENTRYPOINT_CACHE[cache_key] = entrypoints
        instances.append(replace(instance, entrypoints=entrypoints))
    return instances


def select_traefik_instance(instances, is_managed):
    """Instance qui servira les projets : démarrée d'abord, celle du dossier configuré ensuite."""
    if not instances:
        return None
    return sorted(
        instances,
        key=lambda instance: (
            not instance.running,
            not (instance.running and instance.http_port and not instance.compatibility_problems()),
            not is_managed(instance),
            instance.name != "traefik",
        ),
    )[0]


def url_with_port(url, port):
    """Ajoute le port Traefik à une URL http://hôte/ quand il diffère de 80."""
    if not port or int(port) == TRAEFIK_DEFAULT_HTTP_PORT:
        return url
    match = re.match(r"^(http://)([^/:]+)(/.*)?$", url)
    if not match:
        return url
    return f"{match.group(1)}{match.group(2)}:{int(port)}{match.group(3) or '/'}"
