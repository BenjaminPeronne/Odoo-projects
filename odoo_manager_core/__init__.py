"""Shared core for the local Odoo manager."""

from .config import ManagerSettings, SettingsStore
from .project_service import ProjectService
from .system import docker_status, open_terminal, start_docker

__all__ = ["ManagerSettings", "SettingsStore", "ProjectService", "docker_status", "open_terminal", "start_docker"]
