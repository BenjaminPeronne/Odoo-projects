import json
import os
import platform
from dataclasses import asdict, dataclass
from pathlib import Path


CONFIG_VERSION = 1


def default_config_dir(system_name=None, environ=None, home=None):
    system_name = system_name or platform.system()
    environ = environ or os.environ
    home = Path(home or Path.home())

    override = environ.get("ODOO_MANAGER_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if system_name == "Windows":
        base = environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return Path(base) / "Odoo Manager"
    if system_name == "Darwin":
        return home / "Library" / "Application Support" / "Odoo Manager"
    base = environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return Path(base) / "odoo-manager"


@dataclass(frozen=True)
class ManagerSettings:
    version: int = CONFIG_VERSION
    workspace: str = ""
    execution_mode: str = "native"
    wsl_distribution: str = ""
    docker_executable: str = "docker"
    brainkeys_executable: str = "brainkeys"
    traefik_directory: str = ""
    terminal: str = "auto"
    docker_poll_interval: int = 10

    @classmethod
    def from_dict(cls, payload, default_workspace):
        payload = payload if isinstance(payload, dict) else {}
        mode = str(payload.get("execution_mode", "native")).strip().lower()
        if mode not in {"native", "wsl"}:
            mode = "native"
        try:
            poll_interval = int(payload.get("docker_poll_interval", 10))
        except (TypeError, ValueError):
            poll_interval = 10
        poll_interval = min(60, max(3, poll_interval))

        workspace = str(payload.get("workspace") or default_workspace).strip()
        return cls(
            version=CONFIG_VERSION,
            workspace=str(Path(workspace).expanduser().resolve()),
            execution_mode=mode,
            wsl_distribution=str(payload.get("wsl_distribution", "")).strip(),
            docker_executable=str(payload.get("docker_executable", "docker")).strip() or "docker",
            brainkeys_executable=str(payload.get("brainkeys_executable", "brainkeys")).strip() or "brainkeys",
            traefik_directory=str(payload.get("traefik_directory", "")).strip(),
            terminal=str(payload.get("terminal", "auto")).strip() or "auto",
            docker_poll_interval=poll_interval,
        )

    def to_dict(self):
        return asdict(self)


class SettingsStore:
    def __init__(self, default_workspace, config_file=None):
        self.default_workspace = str(Path(default_workspace).expanduser().resolve())
        configured_file = os.environ.get("ODOO_MANAGER_CONFIG", "").strip()
        self.path = Path(config_file or configured_file or default_config_dir() / "config.json").expanduser()

    def load(self):
        payload = {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            payload = {}
        return ManagerSettings.from_dict(payload, self.default_workspace)

    def save(self, settings):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)
        return settings

    def update(self, payload, create_workspace=False):
        current = self.load().to_dict()
        current.update(payload if isinstance(payload, dict) else {})
        settings = ManagerSettings.from_dict(current, self.default_workspace)
        workspace = Path(settings.workspace)
        if create_workspace:
            workspace.mkdir(parents=True, exist_ok=True)
        if not workspace.exists() or not workspace.is_dir():
            raise ValueError(f"Dossier workspace introuvable: {workspace}")
        return self.save(settings)
