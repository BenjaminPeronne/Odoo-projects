import json
import subprocess

from .platform import command_prefix, executable_available, execution_path, open_terminal_script, platform_id, start_docker_desktop


def docker_command(settings, *arguments):
    return [*command_prefix(settings), settings.docker_executable, *arguments]


def shell_command(settings, script_path, *arguments):
    script = execution_path(script_path, settings)
    return [*command_prefix(settings), "sh", script, *arguments]


def docker_status(settings, timeout=6):
    system = platform_id()
    if not executable_available(settings.docker_executable, settings):
        message = "Docker est introuvable. Installe Docker Desktop et vérifie les paramètres."
        if settings.execution_mode == "wsl":
            message = "WSL est introuvable ou indisponible. Vérifie le mode d'exécution Windows."
        return {
            "state": "missing",
            "installed": False,
            "running": False,
            "message": message,
            "platform": system,
            "execution_mode": settings.execution_mode,
            "can_start": system in {"macos", "windows"},
        }

    command = docker_command(settings, "info", "--format", "{{json .ServerVersion}}")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {
            "state": "starting",
            "installed": True,
            "running": False,
            "message": "Docker ne répond pas encore. Le moteur est peut-être en cours de démarrage.",
            "platform": system,
            "execution_mode": settings.execution_mode,
            "can_start": system in {"macos", "windows"},
        }
    except OSError as exc:
        return {
            "state": "stopped",
            "installed": True,
            "running": False,
            "message": f"Docker ne peut pas être exécuté: {exc}",
            "platform": system,
            "execution_mode": settings.execution_mode,
            "can_start": system in {"macos", "windows"},
        }

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Le moteur Docker est arrêté.").strip().splitlines()
        return {
            "state": "stopped",
            "installed": True,
            "running": False,
            "message": detail[-1] if detail else "Le moteur Docker est arrêté.",
            "platform": system,
            "execution_mode": settings.execution_mode,
            "can_start": system in {"macos", "windows"},
        }

    raw_version = result.stdout.strip()
    try:
        version = json.loads(raw_version) if raw_version else ""
    except json.JSONDecodeError:
        version = raw_version.strip('"')
    return {
        "state": "ready",
        "installed": True,
        "running": True,
        "message": f"Docker est opérationnel{f' ({version})' if version else ''}.",
        "version": version,
        "platform": system,
        "execution_mode": settings.execution_mode,
        "can_start": False,
    }


def start_docker(settings):
    result = start_docker_desktop(settings)
    return {"ok": result.ok, "message": result.message}


def open_terminal(settings, script_path, cwd=None):
    result = open_terminal_script(settings, script_path, cwd=cwd)
    return {"ok": result.ok, "message": result.message}
