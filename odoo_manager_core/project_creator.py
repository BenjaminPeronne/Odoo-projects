import os
import re
import shlex
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.cookiejar import CookieJar
from pathlib import Path

from .platform import (
    execution_path,
    host_executable_available,
    platform_id,
    resolve_executable,
    workspace_execution_path,
    workspace_wsl_context,
    wsl_command_prefix,
    find_wsl_executable_distribution,
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
RIKA_INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
RIKA_BASE_URL = "https://rika.sudokeys.com/"
MAX_RIKA_ARCHIVE_BYTES = 100 * 1024 * 1024 * 1024
MAX_RIKA_ARCHIVE_ENTRIES = 2_000_000


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


def validate_rika_instance(instance):
    instance = str(instance or "").strip()
    if not RIKA_INSTANCE_RE.fullmatch(instance) or instance in {".", ".."}:
        raise ValueError("Instance RIKA invalide. Utilise uniquement son nom, par exemple prod01.")
    return instance


def detected_odoo_version(project_root):
    release = Path(project_root) / "odoo" / "odoo" / "release.py"
    try:
        content = release.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        raise RuntimeError("La copie RIKA ne contient pas le fichier de version Odoo attendu.") from exc
    match = re.search(r"version_info\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*,", content)
    version = f"{match.group(1)}.{match.group(2)}" if match else ""
    if version not in SUPPORTED_ODOO_VERSIONS:
        raise RuntimeError("La version Odoo de cette instance RIKA n'est pas prise en charge.")
    return version


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
        distribution = detected_wsl.distribution if detected_wsl else settings.wsl_distribution
        native_git = host_executable_available("git")
        wsl_distribution = find_wsl_executable_distribution("git", distribution) if platform_id() == "windows" else None
        wsl_git = wsl_distribution is not None
        self.git_wsl_distribution = wsl_distribution if ((detected_wsl and wsl_git) or (not native_git and wsl_git)) else None

    @property
    def command_cwd(self):
        return Path.home() if self.git_wsl_distribution is not None else self.workspace

    def log(self, callback, message):
        if callback:
            callback(message)

    def command_path(self, path):
        if self.git_wsl_distribution is not None:
            return wsl_execution_path(path, self.git_wsl_distribution)
        if self.settings.execution_mode == "wsl":
            return execution_path(path, self.settings)
        return str(Path(path).resolve())

    def git(self, *arguments):
        prefix = wsl_command_prefix(self.git_wsl_distribution) if self.git_wsl_distribution is not None else []
        executable = "git" if self.git_wsl_distribution is not None else resolve_executable("git", self.settings)
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
        if self.git_wsl_distribution is not None:
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

    def module_link_states(self, candidates, addons_dir):
        """Inspect links in the filesystem that creates and consumes them."""
        if platform_id() == "windows" and (self.wsl_context or self.settings.execution_mode == "wsl"):
            distribution = self.wsl_context.distribution if self.wsl_context else self.settings.wsl_distribution
            lines = ["set -eu"]
            for name, source in candidates.items():
                link = shlex.quote(wsl_execution_path(addons_dir / name, distribution))
                target = shlex.quote(wsl_execution_path(source, distribution))
                lines.extend([
                    f"if [ -L {link} ] && [ -d {link} ] && [ \"$(readlink -f -- {link})\" = \"$(readlink -f -- {target})\" ]; then",
                    f"printf '%s\\t%s\\n' {shlex.quote(name)} correct",
                    f"elif [ -e {link} ] || [ -L {link} ]; then",
                    f"printf '%s\\t%s\\n' {shlex.quote(name)} conflict",
                    "else",
                    f"printf '%s\\t%s\\n' {shlex.quote(name)} missing",
                    "fi",
                ])
            # Enterprise contains hundreds of modules: do not exceed Windows'
            # command-line limit by passing the generated script with sh -c.
            script_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline="\n", suffix=".sh",
                    prefix=".odoo_manager_check_", dir=addons_dir, delete=False,
                ) as script:
                    script_path = Path(script.name)
                    script.write("\n".join(lines) + "\n")
                code, output = self.project_service.capture(
                    [*wsl_command_prefix(distribution), "sh", wsl_execution_path(script_path, distribution)],
                    cwd=self.command_cwd, timeout=60,
                )
            finally:
                if script_path is not None:
                    script_path.unlink(missing_ok=True)
            states = dict(line.split("\t", 1) for line in output.splitlines() if "\t" in line)
            if code != 0 or set(states) != set(candidates) or any(
                state not in {"correct", "conflict", "missing"} for state in states.values()
            ):
                raise RuntimeError("Impossible de vérifier les liens Enterprise depuis WSL. Vérifie l'accès au workspace et réessaie.")
            return states
        states = {}
        for name, source in candidates.items():
            link = addons_dir / name
            if not self.path_entry_exists(link):
                states[name] = "missing"
            elif link.is_symlink() and link.is_dir() and link.resolve() == source.resolve():
                states[name] = "correct"
            else:
                states[name] = "conflict"
        return states

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

    @staticmethod
    def extract_rika_archive(archive, destination):
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        destination_root = destination.resolve()
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            if len(entries) > MAX_RIKA_ARCHIVE_ENTRIES:
                raise RuntimeError("La copie RIKA contient trop de fichiers.")
            total_size = sum(max(0, entry.file_size) for entry in entries)
            if total_size > MAX_RIKA_ARCHIVE_BYTES:
                raise RuntimeError("La copie RIKA dépasse la taille maximale autorisée.")
            for entry in entries:
                normalized = entry.filename.replace("\\", "/")
                parts = Path(normalized).parts
                if (
                    not normalized
                    or normalized.startswith("/")
                    or Path(normalized).is_absolute()
                    or (parts and parts[0].endswith(":"))
                    or any(part in {"", ".", ".."} for part in parts)
                    or ((entry.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise RuntimeError("La copie RIKA contient un chemin non sécurisé.")
                target = (destination / Path(*parts)).resolve()
                try:
                    target.relative_to(destination_root)
                except ValueError as exc:
                    raise RuntimeError("La copie RIKA tente d'écrire hors du projet temporaire.") from exc
            bundle.extractall(destination)

    def download_rika_project(self, instance, login, password, temporary, log=None):
        instance = validate_rika_instance(instance)
        login = str(login or "").strip()
        password = str(password or "")
        if not login or not password:
            raise ValueError("L'identifiant et le mot de passe RIKA sont requis.")

        cookies = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
        auth_payload = urllib.parse.urlencode({
            "name": login,
            "password": password,
            "submit": "Connexion",
        }).encode("utf-8")
        try:
            self.log(log, "Connexion sécurisée à RIKA...")
            with opener.open(
                urllib.request.Request(
                    urllib.parse.urljoin(RIKA_BASE_URL, "login"),
                    data=auth_payload,
                    method="POST",
                ),
                timeout=30,
            ):
                pass
            if not any(cookie.name == "sessionId" and cookie.value for cookie in cookies):
                raise RuntimeError("RIKA a refusé l'authentification. Vérifie tes identifiants.")

            encoded_instance = urllib.parse.quote(instance, safe="")
            self.log(log, f"Génération de la copie RIKA de {instance}...")
            with opener.open(
                urllib.parse.urljoin(RIKA_BASE_URL, f"{encoded_instance}?action=zip"),
                timeout=300,
            ):
                pass

            archive = Path(temporary) / f"{instance}.zip"
            self.log(log, f"Téléchargement de la copie RIKA de {instance}...")
            with opener.open(
                urllib.parse.urljoin(RIKA_BASE_URL, f"{encoded_instance}.zip"),
                timeout=300,
            ) as response, archive.open("wb") as output:
                content_length = int(response.headers.get("Content-Length") or 0)
                if content_length > MAX_RIKA_ARCHIVE_BYTES:
                    raise RuntimeError("La copie RIKA dépasse la taille maximale autorisée.")
                downloaded = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_RIKA_ARCHIVE_BYTES:
                        raise RuntimeError("La copie RIKA dépasse la taille maximale autorisée.")
                    output.write(chunk)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise RuntimeError("RIKA a refusé l'authentification ou l'accès à cette instance.") from exc
            if exc.code == 404:
                raise RuntimeError(f"L'instance RIKA {instance} est introuvable.") from exc
            raise RuntimeError(f"RIKA a retourné une erreur HTTP {exc.code}.") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("RIKA est inaccessible. Vérifie la connexion réseau puis réessaie.") from exc

        extracted = Path(temporary) / "rika"
        self.log(log, "Décompression et contrôle de la copie RIKA...")
        try:
            self.extract_rika_archive(archive, extracted)
        except (OSError, zipfile.BadZipFile) as exc:
            raise RuntimeError("Le fichier reçu depuis RIKA n'est pas une sauvegarde ZIP exploitable.") from exc
        source_root = extracted / instance
        version = detected_odoo_version(source_root)
        self.log(log, f"Version détectée dans RIKA : Odoo {version}")
        return source_root, version

    def create(
        self,
        name,
        version,
        source_type="standard",
        repository_url="",
        repository_branch="",
        rika_instance="",
        rika_login="",
        rika_password="",
        log=None,
    ):
        name = validate_new_project_name(name)
        source_type = str(source_type or "standard").strip().lower()
        if source_type not in {"standard", "gitlab", "rika"}:
            raise ValueError("Type de source invalide.")
        version = validate_odoo_version(version) if source_type != "rika" else ""

        repository_url = str(repository_url or "").strip()
        repository_branch = str(repository_branch or "").strip()
        if source_type == "gitlab":
            repository_url = validate_gitlab_repository(repository_url)
            repository_branch = validate_git_ref(repository_branch)
        if source_type == "rika":
            rika_instance = validate_rika_instance(rika_instance)

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
            rika_source = None
            if source_type == "rika":
                rika_source, version = self.download_rika_project(
                    rika_instance,
                    rika_login,
                    rika_password,
                    temporary,
                    log=log,
                )
            self.log(log, f"Création du projet {name} en Odoo {version}")
            self.clone(LOCAL_TEMPLATE_REPOSITORY, version, staged_project, log=log)

            odoo_root = staged_project / "odoo"
            if rika_source is not None:
                if odoo_root.exists():
                    if any(odoo_root.iterdir()):
                        raise RuntimeError("Le modèle Docker contient déjà un dossier Odoo non vide.")
                    odoo_root.rmdir()
                rika_source.replace(odoo_root)
                self.configure_template(staged_project, name)
                staged_project.replace(target)
                self.log(log, f"Projet RIKA copié: {target}")
                self.log(log, f"URL locale: http://dev.{name}.localhost/")
                return target

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
