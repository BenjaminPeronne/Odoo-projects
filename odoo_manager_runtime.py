"""Runtime helpers used before the Odoo Manager backend imports its core."""

from __future__ import annotations

import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Mapping


def runtime_log_directory(
    environ: Mapping[str, str] | None = None,
    *,
    system_name: str | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("ODOO_MANAGER_LOG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()

    system = system_name or platform.system()
    user_home = home or Path.home()
    if system == "Windows":
        local_app_data = env.get("LOCALAPPDATA") or env.get("APPDATA")
        if local_app_data:
            return Path(local_app_data) / "Odoo Manager" / "logs"
        return user_home / "AppData" / "Local" / "Odoo Manager" / "logs"
    if system == "Darwin":
        return user_home / "Library" / "Logs" / "Odoo Manager"

    state_home = env.get("XDG_STATE_HOME")
    return (Path(state_home) if state_home else user_home / ".local" / "state") / "odoo-manager"


def initialize_runtime_streams() -> tuple[Path, tuple[IO[str], ...]]:
    """Restore streams removed by PyInstaller's Windows no-console mode."""

    log_path = runtime_log_directory() / "backend.log"
    opened: list[IO[str]] = []
    if sys.stdout is None or sys.stderr is None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_stream = log_path.open("a", encoding="utf-8", buffering=1)
        opened.append(log_stream)
        if sys.stdout is None:
            sys.stdout = log_stream
        if sys.stderr is None:
            sys.stderr = log_stream
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"\n[{timestamp}] Démarrage du backend Odoo Manager", flush=True)

    if sys.stdin is None:
        input_stream = open(os.devnull, "r", encoding="utf-8")
        opened.append(input_stream)
        sys.stdin = input_stream

    return log_path, tuple(opened)
