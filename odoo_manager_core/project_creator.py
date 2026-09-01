import os
import re
import shlex
import shutil
import tempfile
import time
from pathlib import Path

from .platform import (
    command_prefix,
    execution_path,
    host_executable_available,
    platform_id,
    resolve_executable,
    workspace_command_prefix,
    workspace_execution_path,
    workspace_wsl_context,
    wsl_command_prefix,
    wsl_execution_path,
)


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
        detected_wsl = workspace_wsl_context(settings, workspace) if platform_id() == "windows" else None
        self.workspace = Path(detected_wsl.windows_path if detected_wsl else Path(workspace).expanduser().resolve())
        self.project_service = project_service
        self.wsl_context = detected_wsl

    @property
    def command_cwd(self):
        return Path.home() if self.wsl_context else self.workspace

    def log(self, callback, message):
        if callback:
            callback(message)

    def command_path(self, path):
        if self.wsl_context:
            return workspace_execution_path(path, self.settings, self.workspace)
        if self.settings.execution_mode == "wsl":
            return execution_path(path, self.settings)
        return str(Path(path).resolve())

    def git(self, *arguments):
        prefix = workspace_command_prefix(self.settings, self.workspace)
        executable = "git" if self.wsl_context else resolve_executable("git", self.settings)
        return [
            *prefix,
            executable,
            "-c",
            "core.longpaths=true",
            "-c",
            "core.fscache=true",
            "-c",
            "core.preloadindex=true",
            "-c",
            "core.autocrlf=false",
            "-c",
            "gc.auto=0",
            *arguments,
        ]

    def require_git(self):
        code, output = self.project_service.capture(
            self.git("--version"),
            cwd=self.command_cwd,
            timeout=8,
        )
        if code != 0:
            raise RuntimeError(
                "Git est requis pour créer un projet. Installe Git puis relance la vérification."
                + (f" Détail: {output}" if output else "")
            )

    def reference_repository(self, repository):
        if self.wsl_context:
            return None
        slug = repository_slug(repository)
        for project in sorted(self.workspace.iterdir(), key=lambda path: path.name.lower()):
            if not project.is_dir() or project.name.startswith(".odoo_manager"):
                continue
            candidates = [
                project,
                project / "odoo" / "odoo",
                project / "odoo" / "addons-store" / slug,
            ]
            for candidate in candidates:
                config = candidate / ".git" / "config"
                try:
                    content = config.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if repository in content:
                    return candidate
        return None

    def clone(self, repository, branch, destination, log=None):
        destination = Path(destination)
        self.log(log, f"Récupération de {repository.rsplit('/', 1)[-1]} ({branch})...")
        clone_arguments = [
            "clone",
            "--config",
            "core.longpaths=true",
            "--depth",
            "1",
            "--no-tags",
            "--branch",
            branch,
            "--single-branch",
        ]
        reference = self.reference_repository(repository)
        if reference is not None:
            self.log(log, f"Réutilisation des objets Git locaux: {reference}")
            clone_arguments.extend(
                ["--reference-if-able", self.command_path(reference), "--dissociate"]
            )
        clone_arguments.extend([repository, self.command_path(destination)])
        command = self.git(*clone_arguments)
        started_at = time.monotonic()
        code = self.project_service.stream(command, cwd=self.command_cwd, log=log)
        if code != 0:
            raise RuntimeError(
                "Le dépôt GitLab n'a pas pu être récupéré. Vérifie ta clé SSH, l'accès au dépôt et la branche. "
                "Sous Windows, place aussi le dossier des projets dans un chemin court, par exemple C:\\Odoo."
            )
        self.log(log, f"Dépôt récupéré en {time.monotonic() - started_at:.1f} s.")

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

    def link_module_via_wsl(self, relative, link, log=None):
        distribution = self.wsl_context.distribution if self.wsl_context else self.settings.wsl_distribution
        relative_target = str(relative).replace("\\", "/")
        try:
            destination = wsl_execution_path(link, distribution)
            command = [
                *wsl_command_prefix(distribution),
                "ln",
                "-s",
                relative_target,
                destination,
            ]
            code = self.project_service.stream(command, cwd=self.command_cwd, log=log)
        except (OSError, RuntimeError):
            return False
        if code == 0:
            self.log(log, f"Lien créé via WSL 2: {link.name}")
            return True
        return False

    def path_entry_exists_via_wsl(self, path):
        distribution = self.wsl_context.distribution if self.wsl_context else self.settings.wsl_distribution
        try:
            destination = wsl_execution_path(path, distribution)
        except (OSError, RuntimeError):
            return None
        for predicate in ("-e", "-L"):
            code, _output = self.project_service.capture(
                [*wsl_command_prefix(distribution), "test", predicate, destination],
                cwd=self.command_cwd,
                timeout=8,
            )
            if code == 0:
                return True
            if code != 1:
                return None
        return False

    def path_entry_exists(self, path):
        try:
            return path.exists() or path.is_symlink()
        except OSError:
            if platform_id() == "windows":
                detected = self.path_entry_exists_via_wsl(path)
                if detected is not None:
                    return detected
            raise

    def remove_path_via_wsl(self, path, log=None):
        distribution = self.wsl_context.distribution if self.wsl_context else self.settings.wsl_distribution
        try:
            destination = wsl_execution_path(path, distribution)
            code = self.project_service.stream(
                [*wsl_command_prefix(distribution), "rm", "-rf", "--", destination],
                cwd=self.command_cwd,
                log=log,
            )
        except (OSError, RuntimeError):
            return False
        return code == 0

    def remove_path_entry(self, path, log=None):
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
            return
        except OSError as exc:
            if platform_id() == "windows" and self.remove_path_via_wsl(path, log=log):
                self.log(log, f"Ancien lien supprimé via WSL 2: {path.name}")
                return
            raise RuntimeError(f"Impossible de remplacer le module existant: {path}") from exc

    def cleanup_staging_path(self, path, log=None):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return
        except OSError:
            if platform_id() == "windows" and self.remove_path_via_wsl(path, log=log):
                return
            self.log(log, f"Nettoyage différé requis pour le dossier temporaire: {path}")

    def link_modules_batch_via_wsl(self, modules, addons_dir, log=None, replace=False):
        if platform_id() != "windows" or not host_executable_available("wsl.exe"):
            return None

        distribution = self.wsl_context.distribution if self.wsl_context else self.settings.wsl_distribution
        try:
            addons_wsl = wsl_execution_path(addons_dir, distribution).rstrip("/")
        except (OSError, RuntimeError):
            return None

        script_path = addons_dir / ".odoo_manager_links.sh"
        script_wsl = f"{addons_wsl}/{script_path.name}"
        lines = [
            "#!/bin/sh",
            "set -eu",
            "linked=0",
            "skipped=0",
        ]
        total = len(modules)
        for index, module in enumerate(modules, start=1):
            link = f"{addons_wsl}/{module.name}"
            relative = str(Path(os.path.relpath(module, addons_dir))).replace("\\", "/")
            quoted_link = shlex.quote(link)
            quoted_relative = shlex.quote(relative)
            lines.append(f"if [ -e {quoted_link} ] || [ -L {quoted_link} ]; then")
            if replace:
                lines.append(f"  rm -rf -- {quoted_link}")
            else:
                lines.extend(
                    [
                        "  skipped=$((skipped + 1))",
                        "else",
                        f"  ln -s -- {quoted_relative} {quoted_link}",
                        "  linked=$((linked + 1))",
                    ]
                )
            if replace:
                lines.extend(
                    [
                        "fi",
                        f"ln -s -- {quoted_relative} {quoted_link}",
                        "linked=$((linked + 1))",
                    ]
                )
            else:
                lines.append("fi")
            if index % 100 == 0 or index == total:
                lines.append(f"printf '%s\\n' 'Préparation des liens: {index}/{total}'")
        lines.append("printf 'Liens terminés: %s créé(s), %s conservé(s).\\n' \"$linked\" \"$skipped\"")

        try:
            with script_path.open("w", encoding="utf-8", newline="\n") as script:
                script.write("\n".join(lines) + "\n")
            self.log(log, f"Préparation groupée de {total} lien(s) via WSL 2...")
            code = self.project_service.stream(
                [*wsl_command_prefix(distribution), "sh", script_wsl],
                cwd=self.command_cwd,
                log=log,
            )
        finally:
            try:
                script_path.unlink()
            except FileNotFoundError:
                pass
        if code != 0:
            raise RuntimeError(
                "Impossible de préparer les liens d'addons en une seule opération WSL 2. "
                "Vérifie que le workspace est accessible depuis WSL."
            )
        return total

    def link_modules(self, source_root, addons_dir, log=None, replace=False):
        modules = self.module_directories(source_root)
        if not modules:
            self.log(log, "Aucun module à lier dans ce dépôt.")
            return 0

        batch_count = self.link_modules_batch_via_wsl(
            modules,
            addons_dir,
            log=log,
            replace=replace,
        )
        if batch_count is not None:
            self.log(log, f"{batch_count} module(s) préparé(s) dans odoo/addons.")
            return batch_count

        linked = 0
        for module in modules:
            link = addons_dir / module.name
            if self.path_entry_exists(link):
                if not replace:
                    continue
                self.remove_path_entry(link, log=log)
            relative = Path(os.path.relpath(module, addons_dir))
            if self.wsl_context or self.settings.execution_mode == "wsl":
                if not self.link_module_via_wsl(relative, link, log=log):
                    raise RuntimeError(
                        "Impossible de créer les liens symboliques des addons via WSL 2. "
                        "Vérifie que le dossier des projets est accessible depuis WSL."
                    )
                linked += 1
                continue
            try:
                link.symlink_to(relative, target_is_directory=True)
            except OSError as exc:
                if platform_id() == "windows" and self.link_module_via_wsl(relative, link, log=log):
                    linked += 1
                    continue
                raise RuntimeError(
                    "Impossible de créer les liens symboliques des addons. "
                    "Sous Windows, installe WSL 2 ou active le mode développeur."
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
            self.cleanup_staging_path(temporary, log=log)
            try:
                staging_root.rmdir()
            except OSError:
                pass
