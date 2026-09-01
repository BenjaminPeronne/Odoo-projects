import json
import os
import subprocess
import threading
from dataclasses import dataclass

from .platform import (
    command_prefix,
    executable_available,
    executable_search_path,
    execution_path,
    host_executable_available,
    hidden_process_kwargs,
    open_terminal_script,
    platform_id,
    resolve_executable,
    resolve_host_executable,
    start_docker_desktop,
    workspace_wsl_context,
    wsl_command_prefix,
)


@dataclass(frozen=True)
class DockerBackend:
    kind: str
    command: tuple
    label: str
    distribution: str = ""


_DOCKER_BACKENDS = {}
_DOCKER_BACKENDS_LOCK = threading.Lock()


def _docker_cache_key(settings):
    return (
        platform_id(),
        str(getattr(settings, "workspace", "")),
        str(getattr(settings, "docker_executable", "docker")),
        str(getattr(settings, "wsl_distribution", "")),
    )


def reset_docker_backend_cache():
    with _DOCKER_BACKENDS_LOCK:
        _DOCKER_BACKENDS.clear()


def _cache_docker_backend(settings, backend):
    with _DOCKER_BACKENDS_LOCK:
        _DOCKER_BACKENDS[_docker_cache_key(settings)] = backend


def _cached_docker_backend(settings):
    with _DOCKER_BACKENDS_LOCK:
        return _DOCKER_BACKENDS.get(_docker_cache_key(settings))


def _windows_docker_backends(settings):
    native = DockerBackend(
        "native",
        (resolve_host_executable(settings.docker_executable),),
        "Windows",
    )
    context = workspace_wsl_context(settings, settings.workspace)
    distribution = context.distribution if context else settings.wsl_distribution
    wsl = DockerBackend(
        "wsl",
        (*wsl_command_prefix(distribution), "docker"),
        f"WSL ({distribution})" if distribution else "WSL",
        distribution,
    )
    return [wsl, native] if context else [native, wsl]


def _default_docker_backend(settings):
    cached = _cached_docker_backend(settings)
    if cached:
        return cached
    if platform_id() != "windows":
        return DockerBackend(
            settings.execution_mode,
            (*command_prefix(settings), resolve_executable(settings.docker_executable, settings)),
            "Système",
            settings.wsl_distribution if settings.execution_mode == "wsl" else "",
        )
    backends = _windows_docker_backends(settings)
    native = next(backend for backend in backends if backend.kind == "native")
    if host_executable_available(settings.docker_executable):
        return native
    return backends[0]


def docker_command(settings, *arguments):
    backend = _default_docker_backend(settings)
    return [*backend.command, *arguments]


def shell_command(settings, script_path, *arguments):
    script = execution_path(script_path, settings)
    return [*command_prefix(settings), "sh", script, *arguments]


def docker_install_guide(system, execution_mode="native"):
    guides = {
        "macos": {
            "title": "Installer Docker Desktop pour Mac",
            "download_url": "https://www.docker.com/products/docker-desktop/",
            "install_url": "https://docs.docker.com/desktop/setup/install/mac-install/",
            "steps": [
                "Télécharge Docker Desktop pour Mac depuis le site officiel Docker.",
                "Ouvre le fichier .dmg, place Docker dans Applications, puis lance Docker Desktop.",
                "Accepte les conditions, attends que Docker soit démarré, puis clique sur Actualiser.",
            ],
        },
        "windows": {
            "title": "Installer Docker Desktop pour Windows",
            "download_url": "https://www.docker.com/products/docker-desktop/",
            "install_url": "https://docs.docker.com/desktop/setup/install/windows-install/",
            "steps": [
                "Télécharge Docker Desktop pour Windows depuis le site officiel Docker.",
                "Installe Docker Desktop en gardant l’intégration WSL 2 activée.",
                "Redémarre Windows si demandé, lance Docker Desktop, puis clique sur Actualiser.",
            ],
        },
        "linux": {
            "title": "Installer Docker sur Linux",
            "download_url": "https://www.docker.com/products/docker-desktop/",
            "install_url": "https://docs.docker.com/desktop/setup/install/linux/",
            "steps": [
                "Installe Docker Desktop ou Docker Engine selon ta distribution Linux.",
                "Lance Docker et vérifie que la commande docker info répond.",
                "Reviens dans le gestionnaire puis clique sur Actualiser.",
            ],
        },
    }
    guide = guides.get(system, guides["linux"]).copy()
    if execution_mode == "wsl":
        guide = guide.copy()
        guide["title"] = "Installer Docker Desktop avec WSL 2"
        guide["install_url"] = "https://docs.docker.com/desktop/setup/install/windows-install/"
        guide["steps"] = [
            "Installe Docker Desktop pour Windows avec le backend WSL 2.",
            "Vérifie qu’une distribution WSL 2 comme Ubuntu est installée et démarrable.",
            "Dans Docker Desktop, active l’intégration WSL pour cette distribution, puis clique sur Actualiser.",
        ]
    return guide


def docker_status_payload(settings, state, installed, running, message, backend=None, **extra):
    system = platform_id()
    payload = {
        "state": state,
        "installed": installed,
        "running": running,
        "message": message,
        "platform": system,
        "execution_mode": backend.kind if backend else settings.execution_mode,
        "backend": backend.kind if backend else settings.execution_mode,
        "backend_label": backend.label if backend else "Système",
        "wsl_distribution": backend.distribution if backend else "",
        "can_start": system in {"macos", "windows"} and installed,
        "install_guide": docker_install_guide(system, backend.kind if backend else settings.execution_mode),
    }
    payload.update(extra)
    return payload


def docker_status(settings, timeout=6):
    system = platform_id()
    if system != "windows":
        docker_installed = executable_available(settings.docker_executable, settings)
        if not docker_installed:
            message = "Docker est introuvable. Installe Docker Desktop et vérifie les paramètres."
            return docker_status_payload(settings, "missing", False, False, message, can_start=False)
        backend = _default_docker_backend(settings)
        probes = [(backend, True)]
    else:
        probes = []
        for backend in _windows_docker_backends(settings):
            if backend.kind == "native":
                installed = host_executable_available(settings.docker_executable)
            elif not host_executable_available("wsl.exe"):
                installed = False
            else:
                try:
                    version_probe = subprocess.run(
                        [*backend.command, "--version"],
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        check=False,
                        **hidden_process_kwargs(),
                    )
                    installed = version_probe.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    installed = False
            probes.append((backend, installed))

    environments = []
    selected = None
    selected_result = None
    first_installed = None
    try:
        for backend, installed in probes:
            if not installed:
                environments.append({"backend": backend.kind, "label": backend.label, "installed": False, "running": False})
                continue
            if first_installed is None:
                first_installed = backend
            env = os.environ.copy()
            env["PATH"] = executable_search_path()
            try:
                result = subprocess.run(
                    [*backend.command, "info", "--format", "{{json .ServerVersion}}"],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    env=env,
                    **hidden_process_kwargs(),
                )
            except subprocess.TimeoutExpired:
                environments.append({"backend": backend.kind, "label": backend.label, "installed": True, "running": False, "message": "Délai dépassé"})
                continue
            except OSError as exc:
                environments.append({"backend": backend.kind, "label": backend.label, "installed": True, "running": False, "message": str(exc)})
                continue
            detail = (result.stderr or result.stdout or "").strip()
            environments.append(
                {
                    "backend": backend.kind,
                    "label": backend.label,
                    "installed": True,
                    "running": result.returncode == 0,
                    "message": detail,
                }
            )
            if result.returncode == 0 and selected is None:
                selected = backend
                selected_result = result
    except Exception:
        reset_docker_backend_cache()
        raise

    if selected is None:
        if first_installed is None:
            message = "Docker est introuvable sous Windows et dans WSL. Installe Docker Desktop ou Docker Engine dans WSL."
            return docker_status_payload(
                settings,
                "missing",
                False,
                False,
                message,
                can_start=False,
                environments=environments,
            )
        _cache_docker_backend(settings, first_installed)
        detail = next(
            (
                item.get("message", "")
                for item in environments
                if item["backend"] == first_installed.kind and item.get("message")
            ),
            "Le moteur Docker est arrêté.",
        )
        detail_lines = detail.strip().splitlines()
        return docker_status_payload(
            settings,
            "stopped",
            True,
            False,
            detail_lines[-1] if detail_lines else "Le moteur Docker est arrêté.",
            backend=first_installed,
            environments=environments,
        )

    _cache_docker_backend(settings, selected)

    raw_version = selected_result.stdout.strip()
    try:
        version = json.loads(raw_version) if raw_version else ""
    except json.JSONDecodeError:
        version = raw_version.strip('"')
    return docker_status_payload(
        settings,
        "ready",
        True,
        True,
        f"Docker est opérationnel via {selected.label}{f' ({version})' if version else ''}.",
        backend=selected,
        version=version,
        can_start=False,
        environments=environments,
    )


def start_docker(settings):
    result = start_docker_desktop(settings)
    return {"ok": result.ok, "message": result.message}


def open_terminal(settings, script_path, cwd=None):
    result = open_terminal_script(settings, script_path, cwd=cwd)
    return {"ok": result.ok, "message": result.message}
