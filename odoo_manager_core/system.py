import json
import os
import subprocess

from .platform import (
    command_prefix,
    executable_available,
    executable_search_path,
    execution_path,
    open_terminal_script,
    platform_id,
    resolve_executable,
    start_docker_desktop,
)


def docker_command(settings, *arguments):
    return [*command_prefix(settings), resolve_executable(settings.docker_executable, settings), *arguments]


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


def docker_status_payload(settings, state, installed, running, message, **extra):
    system = platform_id()
    payload = {
        "state": state,
        "installed": installed,
        "running": running,
        "message": message,
        "platform": system,
        "execution_mode": settings.execution_mode,
        "can_start": system in {"macos", "windows"} and installed,
        "install_guide": docker_install_guide(system, settings.execution_mode),
    }
    payload.update(extra)
    return payload


def docker_status(settings, timeout=6):
    system = platform_id()
    if not executable_available(settings.docker_executable, settings):
        message = "Docker est introuvable. Installe Docker Desktop et vérifie les paramètres."
        if settings.execution_mode == "wsl":
            message = "WSL est introuvable ou indisponible. Vérifie le mode d'exécution Windows."
        return docker_status_payload(settings, "missing", False, False, message, can_start=False)

    command = docker_command(settings, "info", "--format", "{{json .ServerVersion}}")
    try:
        env = os.environ.copy()
        env["PATH"] = executable_search_path()
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False, env=env)
    except subprocess.TimeoutExpired:
        return docker_status_payload(
            settings,
            "starting",
            True,
            False,
            "Docker ne répond pas encore. Le moteur est peut-être en cours de démarrage.",
        )
    except OSError as exc:
        return docker_status_payload(settings, "stopped", True, False, f"Docker ne peut pas être exécuté: {exc}")

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Le moteur Docker est arrêté.").strip().splitlines()
        return docker_status_payload(
            settings,
            "stopped",
            True,
            False,
            detail[-1] if detail else "Le moteur Docker est arrêté.",
        )

    raw_version = result.stdout.strip()
    try:
        version = json.loads(raw_version) if raw_version else ""
    except json.JSONDecodeError:
        version = raw_version.strip('"')
    return docker_status_payload(
        settings,
        "ready",
        True,
        True,
        f"Docker est opérationnel{f' ({version})' if version else ''}.",
        version=version,
        can_start=False,
    )


def start_docker(settings):
    result = start_docker_desktop(settings)
    return {"ok": result.ok, "message": result.message}


def open_terminal(settings, script_path, cwd=None):
    result = open_terminal_script(settings, script_path, cwd=cwd)
    return {"ok": result.ok, "message": result.message}
