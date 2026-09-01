import os
import platform
import json
import posixpath
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


COMMON_EXECUTABLE_PATHS = {
    "Darwin": [
        "/Applications/Docker.app/Contents/Resources/bin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ],
    "Linux": [
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/snap/bin",
    ],
}


def windows_executable_paths():
    if platform.system() != "Windows":
        return []
    program_files = os.environ.get("ProgramFiles", "")
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    windows_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidates = [
        Path(windows_root) / "System32",
        Path(windows_root) / "System32" / "OpenSSH",
    ]
    if program_files:
        candidates.extend(
            [
                Path(program_files) / "Git" / "cmd",
                Path(program_files) / "Git" / "bin",
                Path(program_files) / "Git" / "usr" / "bin",
                Path(program_files) / "Docker" / "Docker" / "resources" / "bin",
            ]
        )
    if local_app_data:
        candidates.extend(
            [
                Path(local_app_data) / "Microsoft" / "WindowsApps",
                Path(local_app_data) / "Programs" / "Git" / "cmd",
                Path(local_app_data) / "Programs" / "Git" / "bin",
                Path(local_app_data) / "Programs" / "Git" / "usr" / "bin",
                Path(local_app_data) / "Programs" / "Docker" / "Docker" / "resources" / "bin",
            ]
        )
    return [str(path) for path in candidates]


def hidden_process_kwargs():
    if platform.system() != "Windows":
        return {}
    creation_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return {"creationflags": creation_flag}


@dataclass(frozen=True)
class LaunchResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class WslPathContext:
    distribution: str
    linux_path: str

    @property
    def windows_path(self):
        suffix = self.linux_path.lstrip("/").replace("/", "\\")
        root = rf"\\wsl.localhost\{self.distribution}"
        return f"{root}\\{suffix}" if suffix else root


def wsl_path_context(path):
    """Return the WSL distribution and Linux path represented by a UNC path."""
    raw = os.fspath(path).strip()
    normalized = raw.replace("\\", "/")
    match = re.match(
        r"^//(?:wsl\.localhost|wsl\$)/([^/]+)(?:/(.*))?$",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    distribution = match.group(1).strip()
    if not distribution or distribution in {".", ".."}:
        return None
    relative = match.group(2) or ""
    linux_path = posixpath.normpath("/" + relative)
    return WslPathContext(distribution=distribution, linux_path=linux_path)


def wsl_unc_path(distribution, linux_path):
    linux_path = posixpath.normpath(str(linux_path or "/").replace("\\", "/"))
    if not linux_path.startswith("/"):
        linux_path = "/" + linux_path
    return WslPathContext(distribution=str(distribution), linux_path=linux_path).windows_path


def workspace_wsl_context(settings, workspace=None):
    context = wsl_path_context(workspace or settings.workspace)
    if context:
        return context
    if platform_id() == "windows" and settings.execution_mode == "wsl":
        return WslPathContext(settings.wsl_distribution, "")
    return None


def workspace_command_prefix(settings, workspace=None):
    context = workspace_wsl_context(settings, workspace)
    if not context:
        return command_prefix(settings)
    return wsl_command_prefix(context.distribution)


def workspace_execution_path(path, settings, workspace=None):
    context = workspace_wsl_context(settings, workspace)
    path_context = wsl_path_context(path)
    if path_context:
        if context and context.distribution.casefold() != path_context.distribution.casefold():
            raise RuntimeError(
                "Le chemin appartient à une autre distribution WSL "
                f"({path_context.distribution} au lieu de {context.distribution})."
            )
        return path_context.linux_path
    if context:
        return wsl_execution_path(path, context.distribution)
    return execution_path(path, settings)


def platform_id():
    name = platform.system()
    if name == "Darwin":
        return "macos"
    if name == "Windows":
        return "windows"
    return "linux"


def wsl_command_prefix(distribution="", user=""):
    command = ["wsl.exe"]
    if distribution:
        command.extend(["-d", distribution])
    if user:
        command.extend(["-u", user])
    # --exec bypasses the default Linux shell, which would otherwise consume
    # backslashes from Windows paths before wslpath receives them.
    command.append("--exec")
    return command


def command_prefix(settings):
    if settings.execution_mode != "wsl":
        return []
    return wsl_command_prefix(settings.wsl_distribution)


def executable_search_path(extra_paths=None):
    paths = [path for path in os.environ.get("PATH", "").split(os.pathsep) if path]
    for path in COMMON_EXECUTABLE_PATHS.get(platform.system(), []):
        if path not in paths:
            paths.append(path)
    for path in windows_executable_paths():
        if path not in paths:
            paths.append(path)
    for path in extra_paths or []:
        if path and path not in paths:
            paths.append(path)
    return os.pathsep.join(paths)


def resolve_executable(executable, settings):
    if settings.execution_mode == "wsl":
        return executable
    return resolve_host_executable(executable)


def resolve_host_executable(executable):
    path = Path(executable).expanduser()
    if path.is_absolute():
        return str(path)
    return shutil.which(executable, path=executable_search_path()) or executable


def host_executable_available(executable):
    path = Path(executable).expanduser()
    if path.is_absolute():
        return path.exists() and path.is_file()
    return shutil.which(executable, path=executable_search_path()) is not None


def wsl_executable_available(executable, distribution="", timeout=6):
    """Check an executable inside WSL without opening a console window."""
    if platform.system() != "Windows" or not host_executable_available("wsl.exe"):
        return False
    command = [
        *wsl_command_prefix(distribution),
        "sh",
        "-lc",
        'command -v -- "$1" >/dev/null 2>&1',
        "odoo-manager",
        str(executable),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **hidden_process_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def command_uses_wsl(command):
    if not command:
        return False
    return Path(str(command[0])).name.casefold() in {"wsl", "wsl.exe"} and "--exec" in command


def wsl_command_distribution(command):
    command = [str(argument) for argument in command]
    for option in ("-d", "--distribution"):
        if option in command:
            index = command.index(option)
            if index + 1 < len(command):
                return command[index + 1]
    return ""


def wsl_command_with_cwd(command, cwd, settings, workspace=None):
    """Attach an explicit Linux cwd to an existing wsl.exe command."""
    command = [str(argument) for argument in command]
    if not cwd or not command_uses_wsl(command):
        return command
    path_context = wsl_path_context(cwd)
    distribution = wsl_command_distribution(command)
    if path_context:
        if distribution and path_context.distribution.casefold() != distribution.casefold():
            raise RuntimeError(
                "Le répertoire de travail appartient à une autre distribution WSL "
                f"({path_context.distribution} au lieu de {distribution})."
            )
        linux_cwd = path_context.linux_path
    elif platform.system() == "Windows":
        linux_cwd = wsl_execution_path(cwd, distribution)
    else:
        linux_cwd = workspace_execution_path(cwd, settings, workspace)
    exec_index = command.index("--exec")
    return [*command[:exec_index], "--cd", linux_cwd, *command[exec_index:]]


def wsl_execution_path(path, distribution=""):
    context = wsl_path_context(path)
    if context:
        if distribution and context.distribution.casefold() != distribution.casefold():
            raise RuntimeError(
                "Le chemin appartient à une autre distribution WSL "
                f"({context.distribution} au lieu de {distribution})."
            )
        return context.linux_path
    path = str(Path(path).expanduser().resolve())
    path_for_wsl = path.replace("\\", "/")
    command = [*wsl_command_prefix(distribution), "wslpath", "-a", "-u", path_for_wsl]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            **hidden_process_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Impossible d’exécuter WSL: {exc}") from exc
    translated = result.stdout.strip()
    if result.returncode != 0 or not translated:
        detail = (result.stderr or result.stdout or "wslpath a échoué").strip()
        raise RuntimeError(f"Impossible de traduire le chemin pour WSL: {detail}")
    return translated


def execution_path(path, settings):
    resolved = str(Path(path).expanduser().resolve())
    if settings.execution_mode != "wsl":
        return resolved
    return wsl_execution_path(resolved, settings.wsl_distribution)


def executable_available(executable, settings):
    if settings.execution_mode == "wsl":
        return shutil.which("wsl.exe") is not None
    path = Path(executable).expanduser()
    if path.is_absolute():
        return path.exists() and path.is_file()
    return shutil.which(executable, path=executable_search_path()) is not None


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
            subprocess.Popen(
                [str(executable)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **hidden_process_kwargs(),
            )
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


def open_terminal_command(settings, command, cwd=None, label="la commande"):
    current_platform = platform_id()
    command = [str(argument) for argument in command]
    if not command:
        return LaunchResult(False, "Commande de terminal manquante.")
    cwd = Path(cwd or Path.home()).expanduser().resolve()
    preferred = settings.terminal.strip().lower()

    try:
        if current_platform == "macos":
            terminal_command = f"cd {shlex.quote(str(cwd))} && {shlex.join(command)}"
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
            return LaunchResult(True, f"{application} ouvert pour {label}.")

        if current_platform == "windows":
            windows_terminal = shutil.which("wt.exe")
            if windows_terminal:
                subprocess.Popen([windows_terminal, *command], cwd=str(cwd))
                return LaunchResult(True, f"Windows Terminal ouvert pour {label}.")
            cmd = shutil.which("cmd.exe")
            if cmd:
                subprocess.Popen([cmd, "/c", "start", "", *command], cwd=str(cwd))
                return LaunchResult(True, f"Terminal ouvert pour {label}.")
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
            terminal_arguments = [executable, "--", *command]
        else:
            terminal_arguments = [executable, "-e", *command]
        subprocess.Popen(terminal_arguments, cwd=str(cwd))
        return LaunchResult(True, f"Terminal ouvert avec {name} pour {label}.")
    except (OSError, subprocess.SubprocessError) as exc:
        return LaunchResult(False, f"Impossible d’ouvrir le terminal: {exc}")


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
            translated_script = wsl_execution_path(script_path, settings.wsl_distribution)
            command = [*wsl_command_prefix(settings.wsl_distribution), "sh", translated_script]
            windows_terminal = shutil.which("wt.exe")
            if windows_terminal:
                subprocess.Popen([windows_terminal, *command], cwd=str(cwd))
                return LaunchResult(True, "Windows Terminal ouvert pour la création du projet.")
            cmd = shutil.which("cmd.exe")
            if cmd:
                subprocess.Popen([cmd, "/c", "start", "", *command], cwd=str(cwd))
                return LaunchResult(True, "Terminal ouvert pour la création du projet.")
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
