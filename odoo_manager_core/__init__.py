"""Shared core for the local Odoo manager."""

from .config import ManagerSettings, SettingsStore
from .system import docker_status, open_terminal, start_docker

__all__ = ["ManagerSettings", "SettingsStore", "docker_status", "open_terminal", "start_docker"]
