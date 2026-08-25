import os
import re
import shutil
import tempfile
from pathlib import Path

from .platform import command_prefix, execution_path, resolve_executable


SUPPORTED_ODOO_VERSIONS = ("15.0", "16.0", "17.0", "18.0", "19.0")
ODOO_REPOSITORY = "ssh://git@gitlab.sudokeys.com:10022/sudokeys/odoo.git"
ENTERPRISE_REPOSITORY = "ssh://git@gitlab.sudokeys.com:10022/sudokeys/odoo_entreprise.git"
LOCAL_TEMPLATE_REPOSITORY = "ssh://git@gitlab.sudokeys.com:10022/devops/docker-odoo-local.git"

PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
SUDOKEYS_GITLAB_RE = re.compile(
    r"^(?:ssh://git@gitlab\.sudokeys\.com:10022/|git@gitlab\.sudokeys\.com:)"
    r"[A-Za-z0-9._/-]+\.git$"
)


def validate_new_project_name(name):
    name = str(name or "").strip()
    if not PROJECT_NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise ValueError(
            "Nom de projet invalide. Utilise des lettres, chiffres, points, tirets ou underscores."
        )
    if name.startswith(".odoo_manager"):
        raise ValueError("Ce nom de projet est réservé au gestionnaire.")
    return name


def validate_odoo_version(version):
    version = str(version or "").strip()
    if version not in SUPPORTED_ODOO_VERSIONS:
        raise ValueError("Version Odoo non prise en charge.")
    return version


def validate_git_ref(branch):
    branch = str(branch or "").strip()
    if (
        not GIT_REF_RE.fullmatch(branch)
        or ".." in branch
        or branch.endswith("/")
        or branch.endswith(".")
        or branch.endswith(".lock")
    ):
        raise ValueError("Nom de branche Git invalide.")
    return branch


def validate_gitlab_repository(url):
    url = str(url or "").strip()
    if not SUDOKEYS_GITLAB_RE.fullmatch(url):
        raise ValueError(
            "URL GitLab SSH invalide. Utilise une URL du GitLab Sudokeys terminée par .git."
        )
    return url


def repository_slug(url):
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


class ProjectCreator:
    """Create local projects without delegating business logic to Brainkeys."""

    def __init__(self, settings, workspace, project_service):
        self.settings = settings
        self.workspace = Path(workspace).expanduser().resolve()
        self.project_service = project_service

    def log(self, callback, message):
        if callback:
            callback(message)

    def command_path(self, path):
        if self.settings.execution_mode == "wsl":
            return execution_path(path, self.settings)
        return str(Path(path).resolve())

    def git(self, *arguments):
        return [
            *command_prefix(self.settings),
            resolve_executable("git", self.settings),
            *arguments,
        ]

    def require_git(self):
        code, output = self.project_service.capture(self.git("--version"), timeout=8)
        if code != 0:
            raise RuntimeError(
                "Git est requis pour créer un projet. Installe Git puis relance la vérification."
                + (f" Détail: {output}" if output else "")
            )

    def clone(self, repository, branch, destination, log=None):
        destination = Path(destination)
        self.log(log, f"Récupération de {repository.rsplit('/', 1)[-1]} ({branch})...")
        command = self.git(
            "clone",
            "--depth",
            "1",
            "--branch",
            branch,
            "--single-branch",
            repository,
            self.command_path(destination),
        )
        code = self.project_service.stream(command, cwd=self.workspace, log=log)
        if code != 0:
            raise RuntimeError(
                "Le dépôt GitLab n'a pas pu être récupéré. Vérifie ta clé SSH, l'accès au dépôt et la branche."
            )

    @staticmethod
    def module_directories(root):
        root = Path(root)
        modules = []
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            dirs[:] = sorted(
                directory
                for directory in dirs
                if directory not in {".git", ".github", "__pycache__", "node_modules", "setup"}
            )
            if "__manifest__.py" in files or "__openerp__.py" in files:
                modules.append(current_path)
                dirs[:] = []
        return sorted(modules, key=lambda path: path.name.lower())

    def link_modules(self, source_root, addons_dir, log=None, replace=False):
        linked = 0
        for module in self.module_directories(source_root):
            link = addons_dir / module.name
            if link.exists() or link.is_symlink():
                if not replace:
                    continue
                if link.is_symlink() or link.is_file():
                    link.unlink()
                else:
                    shutil.rmtree(link)
            relative = Path(os.path.relpath(module, addons_dir))
            try:
                link.symlink_to(relative, target_is_directory=True)
            except OSError as exc:
                raise RuntimeError(
                    "Impossible de créer les liens symboliques des addons. "
                    "Sous Windows, active le mode développeur ou utilise le mode WSL 2."
                ) from exc
            linked += 1
        self.log(log, f"{linked} module(s) lié(s) dans odoo/addons.")
        return linked

    @staticmethod
    def configure_template(project_path, project_name):
        candidates = [
            project_path / "docker-compose.yml",
            project_path / "docker-compose.yaml",
            project_path / "compose.yml",
            project_path / "compose.yaml",
            project_path / "odoo.conf",
        ]
        for candidate in candidates:
            if not candidate.exists():
                continue
            content = candidate.read_text(encoding="utf-8")
            candidate.write_text(content.replace("XXXXXX", project_name), encoding="utf-8")

    def create(
        self,
        name,
        version,
        source_type="standard",
        repository_url="",
        repository_branch="",
        log=None,
    ):
        name = validate_new_project_name(name)
        version = validate_odoo_version(version)
        source_type = str(source_type or "standard").strip().lower()
        if source_type not in {"standard", "gitlab"}:
            raise ValueError("Type de source invalide.")

        repository_url = str(repository_url or "").strip()
        repository_branch = str(repository_branch or "").strip()
        if source_type == "gitlab":
            repository_url = validate_gitlab_repository(repository_url)
            repository_branch = validate_git_ref(repository_branch)

        self.workspace.mkdir(parents=True, exist_ok=True)
        target = self.workspace / name
        if target.exists() or target.is_symlink():
            raise ValueError(f"Un projet nommé {name} existe déjà dans le workspace.")

        self.require_git()
        staging_root = self.workspace / ".odoo_manager_staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=staging_root))
        staged_project = temporary / "project"

        try:
            self.log(log, f"Création du projet {name} en Odoo {version}")
            self.clone(LOCAL_TEMPLATE_REPOSITORY, version, staged_project, log=log)

            odoo_root = staged_project / "odoo"
            addons_dir = odoo_root / "addons"
            store_dir = odoo_root / "addons-store"
            addons_dir.mkdir(parents=True, exist_ok=True)
            store_dir.mkdir(parents=True, exist_ok=True)

            self.clone(ODOO_REPOSITORY, version, odoo_root / "odoo", log=log)
            enterprise_dir = store_dir / "odoo_entreprise"
            self.clone(ENTERPRISE_REPOSITORY, version, enterprise_dir, log=log)
            self.link_modules(enterprise_dir, addons_dir, log=log)

            if source_type == "gitlab":
                custom_dir = store_dir / repository_slug(repository_url)
                self.clone(repository_url, repository_branch, custom_dir, log=log)
                custom_count = self.link_modules(custom_dir, addons_dir, log=log, replace=True)
                if custom_count == 0:
                    raise RuntimeError(
                        "Aucun module Odoo (__manifest__.py) n'a été trouvé dans le dépôt d'addons."
                    )

            self.configure_template(staged_project, name)
            staged_project.replace(target)
            self.log(log, f"Projet créé: {target}")
            self.log(log, f"URL locale: http://dev.{name}.localhost/")
            return target
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
            try:
                staging_root.rmdir()
            except OSError:
                pass
