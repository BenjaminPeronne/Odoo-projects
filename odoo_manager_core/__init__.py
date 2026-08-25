"""Shared core for the local Odoo manager."""

from .config import ManagerSettings, SettingsStore
from .project_service import ProjectService
from .project_creator import ProjectCreator
from .system import docker_status, open_terminal, start_docker

__all__ = ["ManagerSettings", "SettingsStore", "ProjectCreator", "ProjectService", "docker_status", "open_terminal", "start_docker"]
