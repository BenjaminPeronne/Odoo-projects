import os
import platform
import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LaunchResult:
    ok: bool
    message: str


def platform_id():
    name = platform.system()
    if name == "Darwin":
        return "macos"
    if name == "Windows":
        return "windows"
    return "linux"


def command_prefix(settings):
    if settings.execution_mode != "wsl":
        return []
    command = ["wsl.exe"]
    if settings.wsl_distribution:
        command.extend(["-d", settings.wsl_distribution])
    command.append("--")
    return command


def execution_path(path, settings):
    path = str(Path(path).expanduser().resolve())
    if settings.execution_mode != "wsl":
        return path
    command = [*command_prefix(settings), "wslpath", "-a", "-u", path]
    result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
    translated = result.stdout.strip()
    if result.returncode != 0 or not translated:
        detail = (result.stderr or result.stdout or "wslpath a échoué").strip()
        raise RuntimeError(f"Impossible de traduire le chemin pour WSL: {detail}")
    return translated


def executable_available(executable, settings):
    if settings.execution_mode == "wsl":
        return shutil.which("wsl.exe") is not None
    path = Path(executable).expanduser()
    if path.is_absolute():
        return path.exists() and path.is_file()
    return shutil.which(executable) is not None


def start_docker_desktop(settings):
    current_platform = platform_id()
    try:
        if current_platform == "macos":
            subprocess.Popen(["open", "-a", "Docker"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return LaunchResult(True, "Docker Desktop est en cours d'ouverture.")
        if current_platform == "windows":
            roots = [
                os.environ.get("ProgramFiles", ""),
                os.environ.get("LOCALAPPDATA", ""),
            ]
            candidates = [
                Path(roots[0]) / "Docker" / "Docker" / "Docker Desktop.exe" if roots[0] else None,
                Path(roots[1]) / "Programs" / "Docker" / "Docker" / "Docker Desktop.exe" if roots[1] else None,
            ]
            executable = next((path for path in candidates if path and path.exists()), None)
            if not executable:
                return LaunchResult(False, "Docker Desktop est introuvable. Vérifie son installation.")
            subprocess.Popen([str(executable)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return LaunchResult(True, "Docker Desktop est en cours d'ouverture.")

        systemctl = shutil.which("systemctl")
        if systemctl:
            result = subprocess.run(
                [systemctl, "--user", "start", "docker-desktop"],
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
            if result.returncode == 0:
                return LaunchResult(True, "Docker Desktop est en cours de démarrage.")
        return LaunchResult(False, "Démarre Docker Desktop ou le service Docker depuis le système.")
    except (OSError, subprocess.SubprocessError) as exc:
        return LaunchResult(False, f"Impossible de démarrer Docker: {exc}")


def open_terminal_script(settings, script_path, cwd=None):
    current_platform = platform_id()
    script_path = Path(script_path).expanduser().resolve()
    cwd = Path(cwd or script_path.parent).expanduser().resolve()
    preferred = settings.terminal.strip().lower()

    try:
        if current_platform == "macos":
            terminal_command = f"sh {shlex.quote(str(script_path))}"
            application = "iTerm" if preferred in {"iterm", "iterm2"} else "Terminal"
            if application == "iTerm":
                source = (
                    'tell application "iTerm"\n'
                    "  activate\n"
                    "  if (count of windows) = 0 then create window with default profile\n"
                    f"  tell current session of current window to write text {json.dumps(terminal_command)}\n"
                    "end tell\n"
                )
            else:
                source = (
                    'tell application "Terminal"\n'
                    "  activate\n"
                    f"  do script {json.dumps(terminal_command)}\n"
                    "end tell\n"
                )
            process = subprocess.run(
                ["osascript", "-e", source],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if process.returncode != 0:
                detail = (process.stderr or process.stdout).strip()
                return LaunchResult(False, f"Impossible d’ouvrir {application}: {detail}")
            return LaunchResult(True, f"{application} ouvert.")

        if current_platform == "windows":
            if settings.execution_mode != "wsl":
                return LaunchResult(False, "Sous Windows, configure le mode WSL 2 pour exécuter Brainkeys.")
            translated_script = execution_path(script_path, settings)
            command = [*command_prefix(settings), "sh", translated_script]
            windows_terminal = shutil.which("wt.exe")
            if windows_terminal:
                subprocess.Popen([windows_terminal, *command], cwd=str(cwd))
                return LaunchResult(True, "Windows Terminal ouvert dans WSL 2.")
            cmd = shutil.which("cmd.exe")
            if cmd:
                subprocess.Popen([cmd, "/c", "start", "", *command], cwd=str(cwd))
                return LaunchResult(True, "Terminal WSL 2 ouvert.")
            return LaunchResult(False, "Windows Terminal et cmd.exe sont introuvables.")

        candidates = []
        if preferred not in {"", "auto"}:
            candidates.append(preferred)
        candidates.extend(["x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal", "xterm"])
        executable = next((shutil.which(name) for name in candidates if shutil.which(name)), None)
        if not executable:
            return LaunchResult(False, "Aucun terminal graphique compatible n’a été trouvé.")
        name = Path(executable).name
        if name == "gnome-terminal":
            command = [executable, "--", "sh", str(script_path)]
        else:
            command = [executable, "-e", "sh", str(script_path)]
        subprocess.Popen(command, cwd=str(cwd))
        return LaunchResult(True, f"Terminal ouvert avec {name}.")
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return LaunchResult(False, f"Impossible d’ouvrir le terminal: {exc}")
