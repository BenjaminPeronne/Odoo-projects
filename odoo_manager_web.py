#!/usr/bin/env python3
import html
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from odoo_manager_core import ManagerSettings, SettingsStore, ProjectService, docker_status, open_terminal, start_docker
from odoo_manager_core.platform import command_prefix, executable_search_path, execution_path
from odoo_manager_core.system import docker_command, shell_command


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()
if getattr(sys, "frozen", False):
    workspace_candidates = (
        Path.home() / "Documents" / "Developer" / "Odoo-projects",
        Path.home() / "Documents" / "Odoo-projects",
        Path.home() / "Odoo-projects",
    )
    DEFAULT_WORKSPACE_FALLBACK = next((path for path in workspace_candidates if path.is_dir()), workspace_candidates[-1])
else:
    DEFAULT_WORKSPACE_FALLBACK = ROOT
DEFAULT_WORKSPACE = Path(os.environ.get("ODOO_WORKSPACE", DEFAULT_WORKSPACE_FALLBACK)).resolve()
SETTINGS_STORE = SettingsStore(DEFAULT_WORKSPACE)
SETTINGS = SETTINGS_STORE.load()
WORKSPACE = Path(SETTINGS.workspace).resolve()
MANAGER = Path(os.environ.get("ODOO_MANAGER_SCRIPT", ROOT / "odoo_manager.sh")).resolve()
DELETED_PROJECTS = WORKSPACE / ".odoo_manager_deleted"
DELETED_MODULES = WORKSPACE / ".odoo_manager_deleted_modules"
HOST = os.environ.get("ODOO_GUI_HOST", "127.0.0.1")
PORT = int(os.environ.get("ODOO_GUI_PORT", "8765"))
TRAEFIK_REPO = "ssh://git@gitlab.sudokeys.com:10022/devops/docker-local-tools.git"

SAFE_PROJECT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_DB_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_MODULE_RE = re.compile(r"^[A-Za-z0-9_,.-]+$")
SAFE_IMPORT_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

JOBS = {}
JOBS_LOCK = threading.Lock()
NEXT_JOB_ID = 1


def truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "oui"}


def path_is_relative_to(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def project_imports_root(project):
    return WORKSPACE / project / "odoo" / "addons-store" / ".odoo_manager_imports"


def project_staging_imports_root(project):
    return WORKSPACE / ".odoo_manager_imports" / project


def module_import_roots(project):
    return (
        project_imports_root(project).resolve(),
        project_staging_imports_root(project).resolve(),
    )


def project_odoo_root(project):
    return WORKSPACE / project / "odoo"


def project_addons_link_parent(project):
    return project_odoo_root(project) / "addons"


def project_addons_storage_parent(project):
    return project_odoo_root(project) / "addons-store"


def project_legacy_addons_storage_parent(project):
    return project_odoo_root(project) / "odoo" / "addons"


def path_is_direct_child_of(path, parent):
    return path.resolve(strict=False).parent == parent.resolve(strict=False)


def unique_child(parent, name):
    candidate = parent / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}"
    suffix = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = parent / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}_{suffix}"
        suffix += 1
    return candidate


def command_env():
    env = os.environ.copy()
    env["PATH"] = executable_search_path()
    env["PYTHONUNBUFFERED"] = "1"
    env["ODOO_WORKSPACE"] = execution_path(WORKSPACE, SETTINGS)
    if SETTINGS.traefik_directory:
        env["TRAEFIK_DIR"] = execution_path(SETTINGS.traefik_directory, SETTINGS)
    env["ODOO_MANAGER_EXECUTION_MODE"] = SETTINGS.execution_mode
    env["ODOO_MANAGER_DOCKER"] = SETTINGS.docker_executable
    env["ODOO_MANAGER_BRAINKEYS"] = SETTINGS.brainkeys_executable
    if SETTINGS.wsl_distribution:
        env["ODOO_MANAGER_WSL_DISTRIBUTION"] = SETTINGS.wsl_distribution
    return env


def apply_settings(settings):
    global SETTINGS, WORKSPACE, DELETED_PROJECTS, DELETED_MODULES
    SETTINGS = settings
    WORKSPACE = Path(settings.workspace).resolve()
    DELETED_PROJECTS = WORKSPACE / ".odoo_manager_deleted"
    DELETED_MODULES = WORKSPACE / ".odoo_manager_deleted_modules"
    with MODULE_CACHE_LOCK:
        MODULE_CACHE.clear()


def settings_snapshot():
    payload = SETTINGS.to_dict()
    payload.update(
        {
            "config_file": str(SETTINGS_STORE.path),
            "workspace_exists": WORKSPACE.exists() and WORKSPACE.is_dir(),
            "platform": docker_status(SETTINGS).get("platform", ""),
        }
    )
    return payload


def add_cors_headers(handler):
    origin = handler.headers.get("Origin", "")
    allowed = {
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "tauri://localhost",
        "https://tauri.localhost",
    }
    if origin in allowed:
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")


def json_response(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        add_cors_headers(handler)
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        return None


def html_response(handler, body, status=200):
    data = body.encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        add_cors_headers(handler)
        handler.end_headers()
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        return None


def parse_multipart_form(content_type, body):
    match = re.search(r"boundary=([^;]+)", content_type or "")
    if not match:
        raise ValueError("Boundary multipart manquante.")
    boundary = match.group(1).strip().strip('"').encode("utf-8")
    fields = {}
    files = {}

    for part in body.split(b"--" + boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].strip(b"\r\n")
        header_blob, separator, payload = part.partition(b"\r\n\r\n")
        if not separator:
            continue
        headers = {}
        for line in header_blob.decode("utf-8", errors="replace").split("\r\n"):
            key, sep, value = line.partition(":")
            if sep:
                headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name_match = re.search(r'name="([^"]+)"', disposition)
        if not name_match:
            continue
        name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        payload = payload.rstrip(b"\r\n")
        if filename_match:
            files[name] = {
                "filename": Path(filename_match.group(1)).name,
                "data": payload,
            }
        else:
            fields[name] = payload.decode("utf-8", errors="replace")

    return fields, files


def run_capture(args, cwd=None, timeout=12):
    cwd = cwd or WORKSPACE
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd),
            env=command_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip()
    except FileNotFoundError as exc:
        return 127, str(exc)
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or "").strip()


def docker_available():
    status = docker_status(SETTINGS)
    return status["running"], status["message"]


def default_traefik_directory():
    return Path.home() / "docker-local-tools" / "traefik"


def traefik_directory_label():
    if SETTINGS.traefik_directory:
        return str(Path(SETTINGS.traefik_directory).expanduser())
    if SETTINGS.execution_mode == "wsl":
        return "$HOME/docker-local-tools/traefik"
    return str(default_traefik_directory())


def local_traefik_directory():
    if SETTINGS.traefik_directory:
        return Path(SETTINGS.traefik_directory).expanduser()
    if SETTINGS.execution_mode == "wsl":
        return None
    return default_traefik_directory()


def project_service():
    return ProjectService(SETTINGS, WORKSPACE, traefik_dir=local_traefik_directory())


def traefik_compose_probe():
    if SETTINGS.execution_mode == "wsl" and not SETTINGS.traefik_directory:
        script = (
            'dir="$HOME/docker-local-tools/traefik"; '
            'if [ ! -d "$dir" ]; then echo missing; exit 2; fi; '
            'if [ -f "$dir/docker-compose.yml" ] || [ -f "$dir/docker-compose.yaml" ] || '
            '[ -f "$dir/compose.yml" ] || [ -f "$dir/compose.yaml" ]; then echo ready; exit 0; fi; '
            'echo invalid; exit 3'
        )
        code, output = run_capture([*command_prefix(SETTINGS), "sh", "-lc", script], timeout=6)
        state = (output.splitlines()[-1] if output else "").strip()
        return code == 0, state != "missing", state == "ready"

    directory = local_traefik_directory()
    if not directory or not directory.exists():
        return False, False, False
    has_compose = any((directory / name).exists() for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"))
    return has_compose, True, has_compose


def traefik_status():
    docker = docker_status(SETTINGS)
    has_compose, exists, valid = traefik_compose_probe()
    running = False
    if docker["running"]:
        running = container_status("traefik") == "running"

    if running:
        state = "running"
        message = "Traefik est opérationnel."
    elif not exists:
        state = "missing"
        message = "Traefik n'est pas installé dans le dossier attendu."
    elif not valid:
        state = "invalid"
        message = "Le dossier Traefik existe mais aucun fichier compose n'a été trouvé."
    else:
        state = "stopped"
        message = "Traefik est installé mais pas démarré."

    return {
        "state": state,
        "path": traefik_directory_label(),
        "installed": has_compose,
        "running": running,
        "message": message,
        "repo": TRAEFIK_REPO,
        "requires_docker": not docker["running"],
        "can_install": docker["running"] and not valid,
        "can_start": docker["running"] and valid and not running,
    }


def container_status(name):
    code, output = run_capture(docker_command(SETTINGS, "inspect", "-f", "{{.State.Status}}", name), timeout=5)
    if code != 0 or not output:
        return "absent"
    return output.splitlines()[0].strip()


def project_dirs():
    projects = []
    if not WORKSPACE.exists():
        return projects
    for item in WORKSPACE.iterdir():
        if not item.is_dir():
            continue
        if any((item / name).exists() for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")):
            projects.append(item.name)
    return sorted(projects)


def validate_project(project):
    if not project or not SAFE_PROJECT_RE.match(project):
        raise ValueError("Nom de projet invalide.")
    if project not in project_dirs():
        raise ValueError("Projet introuvable.")
    return project


def validate_db(db_name):
    if not db_name or not SAFE_DB_RE.match(db_name):
        raise ValueError("Nom de base invalide.")
    return db_name


def validate_odoo_db(db_name):
    db_name = validate_db(db_name)
    if db_name == "postgres":
        raise ValueError("Sélectionne une base Odoo, pas la base système postgres.")
    return db_name


def validate_modules(modules):
    if not modules or not SAFE_MODULE_RE.match(modules):
        raise ValueError("Nom de module invalide.")
    return modules


def validate_required_text(value, label, max_len=160):
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} manquant.")
    if len(text) > max_len:
        raise ValueError(f"{label} trop long.")
    return text


def validate_lang(lang):
    lang = str(lang or "fr_FR").strip()
    if not re.match(r"^[a-z]{2}_[A-Z]{2}$", lang):
        raise ValueError("Langue invalide.")
    return lang


def validate_country(country):
    country = str(country or "").strip().upper()
    if country and not re.match(r"^[A-Z]{2}$", country):
        raise ValueError("Pays invalide. Utilise un code ISO sur 2 lettres, par exemple FR.")
    return country


def compose_file(project):
    path = WORKSPACE / project
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        candidate = path / name
        if candidate.exists():
            return candidate
    return None


def project_url(project):
    file = compose_file(project)
    if file:
        try:
            content = file.read_text(encoding="utf-8", errors="ignore")
            match = re.search(r"Host\(`([^`]+)`\)", content)
            if match:
                return f"http://{match.group(1)}/"
        except OSError:
            pass

    code, output = run_capture(docker_command(SETTINGS, "port", f"odoo-{project}", "8069/tcp"), timeout=1)
    if code == 0 and output:
        first = output.splitlines()[0].strip()
        port = first.rsplit(":", 1)[-1]
        if port.isdigit():
            return f"http://localhost:{port}/"
    return f"http://dev.{project}.localhost/"


def project_odoo_version(project):
    release_file = WORKSPACE / project / "odoo" / "odoo" / "odoo" / "release.py"
    if release_file.exists():
        try:
            text = release_file.read_text(encoding="utf-8", errors="ignore")
            match = re.search(r"version_info\s*=\s*\((\d+),\s*(\d+)", text)
            if match:
                return f"{match.group(1)}.{match.group(2)}"
        except OSError:
            pass

    compose = compose_file(project)
    if compose:
        try:
            text = compose.read_text(encoding="utf-8", errors="ignore")
            match = re.search(r"docker-odoo-local:(\d+\.\d+)", text)
            if match:
                return match.group(1)
        except OSError:
            pass
    return ""


def list_databases_for(project):
    if container_status(f"postgresql-{project}") != "running":
        return []
    query = "select datname from pg_database where datistemplate = false order by datname;"
    code, output = run_capture(
        docker_command(SETTINGS, "exec", f"postgresql-{project}", "psql", "-U", "postgres", "-Atc", query),
        timeout=12,
    )
    if code != 0:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def database_base_versions(project, databases):
    versions = {}
    if container_status(f"postgresql-{project}") != "running":
        return versions
    for db_name in databases:
        if db_name == "postgres":
            continue
        query = "select latest_version from ir_module_module where name='base' limit 1;"
        code, output = run_capture(
            docker_command(SETTINGS, "exec", f"postgresql-{project}", "psql", "-U", "postgres", "-d", db_name, "-Atc", query),
            timeout=8,
        )
        if code == 0 and output.strip():
            versions[db_name] = output.strip().splitlines()[0]
    return versions


def module_dirs(project):
    base = project_odoo_root(project)
    candidates = [
        project_addons_link_parent(project),
        project_addons_storage_parent(project),
        project_legacy_addons_storage_parent(project),
        base / "odoo" / "odoo" / "addons",
        base / "addons-store" / "odoo_entreprise",
        base / "addons-store" / "odoo_enterprise",
    ]
    seen = set()
    for parent in candidates:
        if not parent.exists():
            continue
        try:
            children = sorted(parent.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() and not child.is_symlink():
                continue
            manifest = child / "__manifest__.py"
            openerp = child / "__openerp__.py"
            if not manifest.exists() and not openerp.exists():
                continue
            key = child.name
            if key in seen:
                continue
            seen.add(key)
            yield child


MODULE_CACHE = {}
MODULE_CACHE_LOCK = threading.Lock()


def clear_project_module_cache(project):
    with MODULE_CACHE_LOCK:
        MODULE_CACHE.pop(project, None)


def manifest_value(text, key):
    quoted_key_1 = "'" + key + "'"
    quoted_key_2 = '"' + key + '"'
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith((quoted_key_1, quoted_key_2)):
            continue
        _, _, raw_value = stripped.partition(":")
        raw_value = raw_value.strip().rstrip(",")
        if raw_value in ("True", "False"):
            return raw_value == "True"
        if len(raw_value) >= 2 and raw_value[0] in ("'", '"'):
            quote = raw_value[0]
            end = raw_value.find(quote, 1)
            if end > 0:
                return raw_value[1:end]
        if raw_value.startswith("True"):
            return True
        if raw_value.startswith("False"):
            return False
    return None


def parse_manifest(path):
    manifest = path / "__manifest__.py"
    if not manifest.exists():
        manifest = path / "__openerp__.py"
    text = ""
    try:
        with manifest.open("r", encoding="utf-8", errors="ignore") as handle:
            text = handle.read(65536)
    except OSError:
        pass
    installable = manifest_value(text, "installable")
    return {
        "name": path.name,
        "title": str(manifest_value(text, "name") or path.name),
        "summary": str(manifest_value(text, "summary") or "")[:220],
        "version": str(manifest_value(text, "version") or ""),
        "category": str(manifest_value(text, "category") or ""),
        "installable": True if installable is None else bool(installable),
        "path": str(path),
    }


def module_location_info(project, path):
    link_parent = project_addons_link_parent(project).resolve(strict=False)
    storage_parent = project_addons_storage_parent(project).resolve(strict=False)
    legacy_storage_parent = project_legacy_addons_storage_parent(project).resolve(strict=False)
    imports_roots = module_import_roots(project)
    parent = path.parent.resolve(strict=False)
    source_path = path.resolve(strict=False) if path.is_symlink() else path

    link_path = ""
    if parent == link_parent:
        link_path = str(path)
    else:
        candidate_link = project_addons_link_parent(project) / path.name
        if candidate_link.exists() or candidate_link.is_symlink():
            link_path = str(candidate_link)

    if parent == link_parent and path.is_symlink():
        if path_is_direct_child_of(source_path, storage_parent):
            kind = "lien vers addons-store"
        elif path_is_direct_child_of(source_path, legacy_storage_parent):
            kind = "lien vers ancien stockage"
        elif any(path_is_relative_to(source_path, root) for root in imports_roots):
            kind = "lien vers import outil"
        elif path_is_relative_to(source_path, storage_parent):
            kind = "lien vers dépôt addons-store"
        else:
            kind = "lien vers source externe"
    elif parent == link_parent:
        kind = "dossier direct dans odoo/addons"
    elif parent == storage_parent:
        kind = "addons-store"
    elif parent == legacy_storage_parent:
        kind = "ancien stockage"
    elif path_is_relative_to(path.resolve(strict=False), storage_parent):
        kind = "addons-store"
    else:
        kind = "source externe"

    return {
        "path": str(path),
        "link_path": link_path,
        "source_path": str(source_path),
        "path_kind": kind,
    }


def basic_module(project, path):
    location = module_location_info(project, path)
    return {
        "name": path.name,
        "title": path.name,
        "summary": "",
        "version": "",
        "category": "",
        "installable": True,
        **location,
    }


def should_parse_manifest(path):
    text_path = str(path)
    if "/addons-store/" in text_path:
        return False
    if "/odoo/odoo/" in text_path:
        return False
    if path.is_symlink():
        return False
    return True


def module_removal_info(project, path):
    link_parent = project_addons_link_parent(project).resolve()
    storage_parent = project_addons_storage_parent(project).resolve()
    legacy_storage_parent = project_legacy_addons_storage_parent(project).resolve()
    imports_roots = module_import_roots(project)
    parent = path.parent.resolve()

    if parent != link_parent:
        if parent == storage_parent:
            return {
                "removable": False,
                "removal_mode": "protected_source",
                "removal_note": "Module dans odoo/addons-store sans lien géré dans odoo/addons.",
            }
        if parent == legacy_storage_parent:
            return {
                "removable": False,
                "removal_mode": "protected_legacy_source",
                "removal_note": "Module dans l'ancien dossier odoo/odoo/addons sans lien géré dans odoo/addons.",
            }
        return {
            "removable": False,
            "removal_mode": "protected",
            "removal_note": "Module hors du dossier odoo/addons du projet.",
        }

    if path.is_symlink():
        target = path.resolve(strict=False)
        if path_is_direct_child_of(target, storage_parent):
            return {
                "removable": True,
                "removal_mode": "link_and_storage",
                "removal_note": "Supprime le lien odoo/addons et le dossier dans odoo/addons-store.",
            }
        if path_is_direct_child_of(target, legacy_storage_parent):
            return {
                "removable": True,
                "removal_mode": "link_and_legacy_storage",
                "removal_note": "Supprime le lien odoo/addons et le dossier dans l'ancien odoo/odoo/addons.",
            }
        if any(path_is_relative_to(target, root) for root in imports_roots):
            return {
                "removable": True,
                "removal_mode": "link_and_import",
                "removal_note": "Supprime le lien odoo/addons et le dossier extrait géré par l'outil.",
            }
        if path_is_relative_to(target, storage_parent):
            return {
                "removable": False,
                "removal_mode": "protected_store",
                "removal_note": "Module fourni par un dépôt sous addons-store; suppression du lien seule le ferait réapparaître.",
            }
        return {
            "removable": True,
            "removal_mode": "link_only",
            "removal_note": "Supprime le lien dans odoo/addons. La source externe est conservée.",
        }

    return {
        "removable": True,
        "removal_mode": "directory",
        "removal_note": "Déplace le dossier du module hors de odoo/addons.",
    }


def installed_modules(project, db_name):
    if not db_name or container_status(f"postgresql-{project}") != "running":
        return {}
    query = "select name,state,coalesce(latest_version,'') from ir_module_module order by name;"
    code, output = run_capture(
        docker_command(SETTINGS, "exec", f"postgresql-{project}", "psql", "-U", "postgres", "-d", db_name, "-Atc", query),
        timeout=18,
    )
    states = {}
    if code != 0:
        return states
    for line in output.splitlines():
        parts = line.split("|")
        if len(parts) >= 2:
            states[parts[0]] = {"state": parts[1], "installed_version": parts[2] if len(parts) > 2 else ""}
    return states


def modules_for(project, db_name=None):
    cache_key = project
    now = time.time()
    with MODULE_CACHE_LOCK:
        cached = MODULE_CACHE.get(cache_key)
        if cached and now - cached["created_at"] < 45:
            base_modules = [dict(item) for item in cached["modules"]]
        else:
            base_modules = None

    if base_modules is None:
        base_modules = [basic_module(project, path) for path in module_dirs(project)]
        with MODULE_CACHE_LOCK:
            MODULE_CACHE[cache_key] = {"created_at": now, "modules": [dict(item) for item in base_modules]}

    states = installed_modules(project, db_name) if db_name else {}
    modules = []
    for module in base_modules:
        module = dict(module)
        state = states.get(module["name"], {})
        module["state"] = state.get("state", "disponible")
        module["installed_version"] = state.get("installed_version", "")
        module.update(module_removal_info(project, Path(module["path"])))
        modules.append(module)
    return modules


def db_query_lines(project, db_name, query, timeout=18):
    code, output = run_capture(
        docker_command(SETTINGS, "exec", f"postgresql-{project}", "psql", "-U", "postgres", "-d", db_name, "-Atc", query),
        timeout=timeout,
    )
    if code != 0:
        raise RuntimeError(output or "Requête PostgreSQL impossible.")
    return [line.strip() for line in output.splitlines() if line.strip()]


def filestore_files(project, db_name):
    filestore = WORKSPACE / project / "odoo_data" / "filestore" / db_name
    files = set()
    if not filestore.exists():
        return files, filestore
    for path in filestore.glob("*/*"):
        if path.is_file():
            try:
                files.add(str(path.relative_to(filestore)))
            except ValueError:
                pass
    return files, filestore


def project_diagnostics(project):
    project = validate_project(project)
    docker_ok, docker_message = docker_available()
    diagnostics = {
        "project": project,
        "docker_ok": docker_ok,
        "issues": [],
        "databases": [],
    }
    if not docker_ok:
        diagnostics["issues"].append(
            {
                "severity": "error",
                "title": "Docker indisponible",
                "details": docker_message,
                "items": [],
            }
        )
        return diagnostics

    odoo_status = container_status(f"odoo-{project}")
    pg_status = container_status(f"postgresql-{project}")
    diagnostics["odoo_status"] = odoo_status
    diagnostics["postgres_status"] = pg_status
    if pg_status != "running":
        diagnostics["issues"].append(
            {
                "severity": "error",
                "title": "PostgreSQL n'est pas démarré",
                "details": f"Conteneur postgresql-{project}: {pg_status}",
                "items": [],
            }
        )
        return diagnostics

    available_paths = {path.name: path for path in module_dirs(project)}
    databases = [db_name for db_name in list_databases_for(project) if db_name != "postgres"]

    for db_name in databases:
        db_info = {"name": db_name, "issues": []}
        diagnostics["databases"].append(db_info)

        try:
            pending = db_query_lines(
                project,
                db_name,
                "select name || '|' || state from ir_module_module where state in ('to install','to upgrade','to remove') order by name;",
                timeout=12,
            )
        except Exception as exc:
            diagnostics["issues"].append(
                {
                    "severity": "error",
                    "title": f"Impossible de lire les modules de {db_name}",
                    "details": str(exc),
                    "items": [],
                }
            )
            continue

        if pending:
            items = [line.replace("|", " · ") for line in pending[:40]]
            issue = {
                "severity": "warning",
                "title": f"Opération module en attente dans {db_name}",
                "details": "Odoo peut refuser une installation tant que ces modules restent dans un état transitoire.",
                "items": items,
            }
            diagnostics["issues"].append(issue)
            db_info["issues"].append(issue)

        states = installed_modules(project, db_name)
        installed_missing = sorted(name for name, state in states.items() if state.get("state") == "installed" and name not in available_paths)
        if installed_missing:
            issue = {
                "severity": "error",
                "title": f"Modules installés absents du code dans {db_name}",
                "details": "La base les considère installés, mais aucun dossier addon correspondant n'est présent dans les chemins montés.",
                "items": installed_missing[:60],
            }
            diagnostics["issues"].append(issue)
            db_info["issues"].append(issue)

        stored = db_query_lines(
            project,
            db_name,
            "select store_fname from ir_attachment where store_fname is not null and store_fname <> '' order by store_fname;",
            timeout=18,
        )
        referenced = set(stored)
        actual, filestore = filestore_files(project, db_name)
        missing = sorted(referenced - actual)
        db_info["filestore"] = {
            "path": str(filestore),
            "referenced": len(stored),
            "referenced_unique": len(referenced),
            "actual": len(actual),
            "missing": len(missing),
        }
        if missing:
            issue = {
                "severity": "error",
                "title": f"Filestore incomplet pour {db_name}",
                "details": (
                    f"{len(missing)} fichier(s) référencé(s) par ir_attachment sont absents de {filestore}. "
                    "Il faut récupérer le filestore source ou recréer les pièces jointes concernées."
                ),
                "items": missing[:40],
            }
            diagnostics["issues"].append(issue)
            db_info["issues"].append(issue)

    if not diagnostics["issues"]:
        diagnostics["issues"].append(
            {
                "severity": "success",
                "title": "Aucun problème structurel détecté",
                "details": "Conteneurs, modules installés et filestore semblent cohérents.",
                "items": [],
            }
        )

    return diagnostics


def overview():
    docker_ok, docker_message = docker_available()
    projects = []
    for project in project_dirs():
        odoo_status = container_status(f"odoo-{project}") if docker_ok else "docker off"
        pg_status = container_status(f"postgresql-{project}") if docker_ok else "docker off"
        databases = list_databases_for(project) if docker_ok else []
        projects.append(
            {
                "name": project,
                "odoo_version": project_odoo_version(project),
                "odoo_status": odoo_status,
                "postgres_status": pg_status,
                "url": project_url(project),
                "database_manager_url": urllib.parse.urljoin(project_url(project), "web/database/manager"),
                "databases": databases,
                "database_versions": {},
            }
        )
    return {
        "workspace": str(WORKSPACE),
        "docker_ok": docker_ok,
        "docker_message": docker_message,
        "projects": projects,
    }


class Job:
    def __init__(self, title, target, args=()):
        global NEXT_JOB_ID
        with JOBS_LOCK:
            self.id = NEXT_JOB_ID
            NEXT_JOB_ID += 1
            JOBS[self.id] = self
        self.title = title
        self.status = "running"
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.finished_at = None
        self.lines = []
        self.output = ""
        self.target = target
        self.args = args
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def add(self, line):
        with JOBS_LOCK:
            self.lines.append(line.rstrip("\n"))
            self.lines = self.lines[-700:]
            self.output += line.rstrip("\n") + "\n"
            self.output = self.output[-120000:]

    def add_text(self, text):
        with JOBS_LOCK:
            self.output += text
            self.output = self.output[-120000:]
            for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                if line:
                    self.lines.append(line)
            self.lines = self.lines[-700:]

    def run(self):
        try:
            self.target(self, *self.args)
            if self.status == "running":
                self.status = "done"
        except Exception as exc:
            self.add(f"Erreur: {exc}")
            self.status = "error"
        finally:
            self.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")


def run_stream(job, args, cwd=None):
    cwd = cwd or WORKSPACE
    job.add("$ " + " ".join(str(arg) for arg in args))
    process = subprocess.Popen(
        args,
        cwd=str(cwd),
        env=command_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        job.add(line)
    code = process.wait()
    job.add(f"Code retour: {code}")
    if code != 0:
        job.status = "error"
    return code


def manager_job(job, *args):
    if not MANAGER.exists():
        raise RuntimeError(f"Script introuvable: {MANAGER}")
    run_stream(job, shell_command(SETTINGS, MANAGER, *args))


def install_traefik_job(job):
    status = docker_status(SETTINGS)
    if not status["running"]:
        raise RuntimeError("Docker doit être installé et démarré avant l'installation de Traefik.")
    manager_job(job, "--install-traefik")


def start_project_job(job, project):
    project = validate_project(project)
    project_service().start_project(project, log=job.add)


def update_project_job(job, project):
    project = validate_project(project)
    project_service().update_project(project, log=job.add)
    clear_project_module_cache(project)


def update_all_projects_job(job):
    projects = project_dirs()
    project_service().update_all_projects(log=job.add)
    for project in projects:
        clear_project_module_cache(project)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_form_no_redirect(url, data, timeout=240):
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    opener = urllib.request.build_opener(NoRedirectHandler)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(4096).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return exc.code, ""
        content = exc.read(4096).decode("utf-8", errors="replace")
        raise RuntimeError(f"Odoo a retourne HTTP {exc.code}: {content[:600]}")


def create_database_job(job, project, db_name, master_pwd, login, password, lang, country, demo):
    project = validate_project(project)
    db_name = validate_db(db_name)
    master_pwd = validate_required_text(master_pwd, "Master password")
    login = validate_required_text(login, "Login administrateur")
    password = validate_required_text(password, "Mot de passe administrateur")
    lang = validate_lang(lang)
    country = validate_country(country)
    demo = bool(demo)

    job.add(f"Creation de la base {db_name} dans {project}")
    job.add("Demarrage du projet avant creation de base...")
    project_service().start_project(project, log=job.add)

    existing = set(list_databases_for(project))
    if db_name in existing:
        raise RuntimeError(f"La base existe deja: {db_name}")

    url = urllib.parse.urljoin(project_url(project), "web/database/create")
    form = {
        "master_pwd": master_pwd,
        "name": db_name,
        "login": login,
        "password": password,
        "lang": lang,
        "phone": "",
        "demo": "on" if demo else "",
    }
    if country:
        form["country_code"] = country

    job.add(f"Appel Odoo: {url}")
    job.add(f"Langue: {lang}" + (f" · Pays: {country}" if country else ""))
    job.add("Donnees de demonstration: " + ("oui" if demo else "non"))
    status, content = post_form_no_redirect(url, form)
    job.add(f"Reponse Odoo: HTTP {status}")
    if status == 200 and content:
        preview = re.sub(r"\s+", " ", content).strip()[:500]
        if preview:
            job.add(f"Apercu reponse: {preview}")

    for waited in range(0, 62, 2):
        databases = set(list_databases_for(project))
        if db_name in databases:
            job.add(f"Base creee: {db_name}")
            clear_project_module_cache(project)
            return
        job.add(f"Attente apparition base... {waited}s/60s")
        time.sleep(2)

    raise RuntimeError("La creation a ete envoyee, mais la base n'apparait pas dans PostgreSQL.")


def delete_project_job(job, project):
    project = validate_project(project)
    path = (WORKSPACE / project).resolve()
    if path.parent != WORKSPACE:
        raise RuntimeError("Chemin projet refuse.")

    job.add(f"Suppression du projet {project}")
    docker_ok, docker_message = docker_available()
    if docker_ok:
        job.add("Arret des conteneurs Docker Compose...")
        code = run_stream(job, docker_command(SETTINGS, "compose", "down"), cwd=path)
        if code != 0:
            raise RuntimeError("Impossible d'arreter Docker Compose proprement.")
    else:
        job.add("Docker ne repond pas, deplacement du dossier sans arret Compose.")
        if docker_message:
            job.add(docker_message[:800])

    DELETED_PROJECTS.mkdir(parents=True, exist_ok=True)
    base_name = f"{time.strftime('%Y%m%d_%H%M%S')}_{project}"
    destination = DELETED_PROJECTS / base_name
    suffix = 1
    while destination.exists():
        suffix += 1
        destination = DELETED_PROJECTS / f"{base_name}_{suffix}"

    shutil.move(str(path), str(destination))
    clear_project_module_cache(project)
    job.add(f"Projet deplace dans: {destination}")
    job.add("Suppression terminee. Le dossier reste recuperable a cet emplacement.")


def prune_empty_dirs(path, stop_at):
    current = path
    while current != stop_at and path_is_relative_to(current, stop_at):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def move_deleted_module_path(job, project, module_name, path, label):
    destination_root = DELETED_MODULES / project
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = unique_child(destination_root, module_name)
    shutil.move(str(path), str(destination))
    job.add(f"{label} déplacé: {path} -> {destination}")
    return destination


def delete_module_file_entry(job, project, module_name):
    primary_addons = project_addons_link_parent(project).resolve()
    storage_parent = project_addons_storage_parent(project).resolve()
    legacy_storage_parent = project_legacy_addons_storage_parent(project).resolve()
    imports_roots = module_import_roots(project)
    entry = primary_addons / module_name

    if not entry.exists() and not entry.is_symlink():
        job.add(f"Module introuvable dans odoo/addons: {module_name}")
        return False

    info = module_removal_info(project, entry)
    if not info["removable"]:
        raise RuntimeError(f"Suppression refusée pour {module_name}: {info['removal_note']}")

    if entry.is_symlink():
        target = entry.resolve(strict=False)
        entry.unlink()
        job.add(f"Lien supprimé: {entry}")
        if path_is_direct_child_of(target, storage_parent) or path_is_direct_child_of(target, legacy_storage_parent):
            if target.exists() or target.is_symlink():
                move_deleted_module_path(job, project, module_name, target, "Dossier addons-store")
            else:
                job.add(f"Dossier addons-store déjà absent: {target}")
        else:
            imports_root = next((root for root in imports_roots if path_is_relative_to(target, root)), None)
            if imports_root is not None:
                if target.exists() or target.is_symlink():
                    move_deleted_module_path(job, project, module_name, target, "Dossier importé")
                    prune_empty_dirs(target.parent, imports_root)
                else:
                    job.add(f"Cible importée déjà absente: {target}")
            else:
                job.add(f"Source externe conservée: {target}")
    elif entry.is_dir():
        move_deleted_module_path(job, project, module_name, entry, "Dossier addon")
    else:
        move_deleted_module_path(job, project, module_name, entry, "Fichier addon")

    return True


def stop_project_job(job, project):
    project = validate_project(project)
    project_service().stop_project(project, log=job.add)


def managed_storage_link(project, module_name, storage_path):
    link_path = project_addons_link_parent(project) / module_name
    return link_path.is_symlink() and link_path.resolve(strict=False) == storage_path.resolve(strict=False)


def managed_module_copy_ready(project, module_name, storage_path):
    expected_path = project_addons_storage_parent(project) / module_name
    if storage_path.absolute() != expected_path.absolute():
        return False
    if storage_path.is_symlink() or not storage_path.is_dir():
        return False
    return (storage_path / "__manifest__.py").is_file() or (storage_path / "__openerp__.py").is_file()


def copy_module_to_storage(job, project, module_path, replace_existing=False):
    module_name = module_path.name
    validate_modules(module_name)
    storage_parent = project_addons_storage_parent(project)
    link_path = project_addons_link_parent(project) / module_name
    storage_path = storage_parent / module_name
    storage_parent.mkdir(parents=True, exist_ok=True)

    source_path = module_path.resolve()
    if storage_path.exists() or storage_path.is_symlink():
        if storage_path.resolve(strict=False) == source_path:
            job.add(f"Module déjà dans addons-store: {storage_path}")
            return storage_path
        if not replace_existing:
            raise RuntimeError(f"Le module existe déjà dans addons-store du projet: {storage_path}")
        if not managed_storage_link(project, module_name, storage_path) and not link_path.exists() and not link_path.is_symlink():
            raise RuntimeError(
                f"Remplacement refusé pour {module_name}: un dossier existe déjà dans odoo/addons-store sans lien géré."
            )
        backup_existing_module(job, project, storage_path)

    ignore = shutil.ignore_patterns(".git", "__pycache__", "node_modules", ".DS_Store")
    shutil.copytree(source_path, storage_path, symlinks=True, ignore=ignore)
    job.add(f"Module copié dans addons-store: {source_path} -> {storage_path}")
    return storage_path


def ensure_relative_module_link(job, project, module_name, storage_path, replace_existing=False):
    link_parent = project_addons_link_parent(project)
    link_parent.mkdir(parents=True, exist_ok=True)
    link_path = link_parent / module_name
    link_value = Path(os.path.relpath(storage_path, start=link_parent))

    if link_path.exists() or link_path.is_symlink():
        if link_path.is_symlink() and link_path.resolve(strict=False) == storage_path.resolve(strict=False):
            current = os.readlink(link_path)
            if Path(current).is_absolute():
                link_path.unlink()
                link_path.symlink_to(link_value, target_is_directory=True)
                job.add(f"Lien converti en relatif: {link_path} -> {link_value}")
                return True
            job.add(f"Déjà lié en relatif: {module_name}")
            return False
        info = module_removal_info(project, link_path)
        if not replace_existing:
            raise RuntimeError(f"Le module existe déjà dans le projet: {link_path}")
        if not info["removable"]:
            can_replace_store_link = (
                link_path.is_symlink()
                and info.get("removal_mode") == "protected_store"
                and managed_module_copy_ready(project, module_name, storage_path)
            )
            if not can_replace_store_link:
                raise RuntimeError(f"Remplacement refusé pour {module_name}: {info['removal_note']}")
            job.add(
                f"Lien fourni par un dépôt remplacé par la copie gérée: {link_path} -> {link_value}. "
                "La source du dépôt est conservée."
            )
        backup_existing_module(job, project, link_path)

    link_path.symlink_to(link_value, target_is_directory=True)
    job.add(f"Lien relatif créé: {link_path} -> {link_value}")
    return True


def install_module_candidates(job, project, candidates, replace_existing=False):
    storage_parent = project_addons_storage_parent(project)
    link_parent = project_addons_link_parent(project)

    if not candidates:
        raise RuntimeError("Aucun module Odoo trouve dans ce dossier.")

    job.add(f"Dossier addons-store modules: {storage_parent}")
    job.add(f"Dossier liens Odoo: {link_parent}")

    linked = 0
    skipped = 0
    for module_path in candidates:
        module_path = module_path.resolve()
        storage_path = copy_module_to_storage(job, project, module_path, replace_existing=replace_existing)
        changed = ensure_relative_module_link(job, project, storage_path.name, storage_path, replace_existing=replace_existing)
        if changed:
            linked += 1
        else:
            skipped += 1

    clear_project_module_cache(project)
    job.add(f"Terminé. Modules préparés: {linked}. Déjà présents: {skipped}.")
    job.add("Installe ou mets à jour le module depuis l'interface.")


def link_module_candidates(job, project, candidates, replace_existing=False):
    install_module_candidates(job, project, candidates, replace_existing=replace_existing)


def normalize_module_layout_for_action(job, project, module_names):
    link_parent = project_addons_link_parent(project)
    storage_parent = project_addons_storage_parent(project)
    legacy_storage_parent = project_legacy_addons_storage_parent(project)

    for module_name in module_names:
        link_path = link_parent / module_name
        storage_path = storage_parent / module_name
        if not link_path.exists() and not link_path.is_symlink():
            continue

        if link_path.is_symlink():
            target = link_path.resolve(strict=False)
            if path_is_relative_to(target, storage_parent.resolve()):
                continue
            if path_is_direct_child_of(target, legacy_storage_parent):
                copied = copy_module_to_storage(job, project, target, replace_existing=False)
                ensure_relative_module_link(job, project, module_name, copied, replace_existing=True)
                job.add(f"Layout module migré vers addons-store avant action Odoo: {module_name}")
                continue
            if not target.exists():
                job.add(f"Layout non normalisé pour {module_name}: cible absente {target}")
                continue
            if storage_path.exists() or storage_path.is_symlink():
                if not (storage_path / "__manifest__.py").exists() and not (storage_path / "__openerp__.py").exists():
                    job.add(f"Layout non normalisé pour {module_name}: dossier addons-store existant sans manifest {storage_path}")
                    continue
                ensure_relative_module_link(job, project, module_name, storage_path, replace_existing=True)
                job.add(f"Lien migré vers le dossier addons-store existant: {module_name}")
                continue
            copied = copy_module_to_storage(job, project, target, replace_existing=False)
            ensure_relative_module_link(job, project, module_name, copied, replace_existing=True)
            job.add(f"Layout module normalisé avant action Odoo: {module_name}")
            continue

        if link_path.is_dir():
            if storage_path.exists() or storage_path.is_symlink():
                job.add(f"Layout non normalisé pour {module_name}: dossier addons-store déjà présent {storage_path}")
                continue
            storage_parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(link_path), str(storage_path))
            job.add(f"Dossier addon déplacé vers addons-store: {link_path} -> {storage_path}")
            ensure_relative_module_link(job, project, module_name, storage_path, replace_existing=True)


def module_command_job(job, flag, project, db_name, modules):
    project = validate_project(project)
    db_name = validate_odoo_db(db_name)
    module_names = [name.strip() for name in validate_modules(modules).split(",") if name.strip()]
    if not module_names:
        raise RuntimeError("Aucun module fourni.")

    if flag in ("--install-module", "--update-module"):
        normalize_module_layout_for_action(job, project, module_names)

    manager_job(job, flag, project, db_name, ",".join(module_names))


def delete_module_code_job(job, project, modules, db_name="", uninstall_first=False):
    project = validate_project(project)
    module_names = [name.strip() for name in validate_modules(modules).split(",") if name.strip()]
    module_names = list(dict.fromkeys(module_names))
    if not module_names:
        raise RuntimeError("Aucun module fourni.")

    db_name = str(db_name or "").strip()
    uninstall_first = bool(uninstall_first and db_name)

    job.add(f"Suppression réelle de modules dans {project}")
    job.add("Modules: " + ", ".join(module_names))

    if uninstall_first:
        db_name = validate_odoo_db(db_name)
        states = installed_modules(project, db_name)
        installed = [name for name in module_names if states.get(name, {}).get("state") == "installed"]
        if installed:
            job.add(f"Désinstallation Odoo avant suppression: {', '.join(installed)}")
            code = run_stream(job, shell_command(SETTINGS, MANAGER, "--uninstall-module", project, db_name, ",".join(installed)))
            if code != 0:
                raise RuntimeError("La désinstallation Odoo a échoué; suppression du code annulée.")
        else:
            job.add(f"Aucun module sélectionné n'est installé dans {db_name}; suppression du code uniquement.")
    else:
        job.add("Désinstallation Odoo non demandée; suppression du code uniquement.")

    removed = 0
    for module_name in module_names:
        if delete_module_file_entry(job, project, module_name):
            removed += 1

    clear_project_module_cache(project)
    job.add(f"Suppression terminée. Entrées retirées de odoo/addons: {removed}.")
    job.add(f"Emplacement de récupération: {DELETED_MODULES / project}")


def create_project_terminal_job(job):
    if not MANAGER.exists():
        raise RuntimeError(f"Script introuvable: {MANAGER}")

    launcher_dir = WORKSPACE / ".odoo_manager_terminal"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    launcher = launcher_dir / "create_project.sh"
    shell_workspace = execution_path(WORKSPACE, SETTINGS)
    shell_manager = execution_path(MANAGER, SETTINGS)
    shell_traefik = execution_path(SETTINGS.traefik_directory, SETTINGS) if SETTINGS.traefik_directory else ""
    exports = [
        f"export ODOO_WORKSPACE={shlex.quote(shell_workspace)}",
        f"export ODOO_MANAGER_DOCKER={shlex.quote(SETTINGS.docker_executable)}",
        f"export ODOO_MANAGER_BRAINKEYS={shlex.quote(SETTINGS.brainkeys_executable)}",
    ]
    if shell_traefik:
        exports.append(f"export TRAEFIK_DIR={shlex.quote(shell_traefik)}")
    launcher.write_text(
        "\n".join(
            [
                "#!/usr/bin/env sh",
                "set -eu",
                f"cd {shlex.quote(shell_workspace)}",
                *exports,
                'echo "Creation d un nouveau projet Odoo local via Brainkeys"',
                'echo "Workspace: $(pwd)"',
                'echo ""',
                f"sh {shlex.quote(shell_manager)} --create-project",
                'echo ""',
                'echo "Process termine. Appuyez sur Entree pour fermer ce terminal."',
                "read _",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    result = open_terminal(SETTINGS, launcher, cwd=WORKSPACE)
    job.add(result["message"])
    if not result["ok"]:
        raise RuntimeError(result["message"])
    job.add("Terminal local ouvert. Termine le processus Brainkeys dans cette fenêtre.")
    job.add("Quand Brainkeys est terminé, clique sur Actualiser dans le gestionnaire.")


def find_module_candidates(source_path):
    candidates = []
    if (source_path / "__manifest__.py").exists() or (source_path / "__openerp__.py").exists():
        candidates.append(source_path)
    else:
        for root, dirs, files in os.walk(source_path):
            root_path = Path(root)
            if "__manifest__.py" in files or "__openerp__.py" in files:
                candidates.append(root_path)
                dirs[:] = []
                continue
            if root_path != source_path and root_path.name in {".git", "__pycache__", "node_modules"}:
                dirs[:] = []
    return sorted(candidates, key=lambda p: p.name.lower())


def backup_existing_module(job, project, target):
    backup_root = WORKSPACE / ".odoo_manager_backups" / "modules" / project
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / f"{time.strftime('%Y%m%d_%H%M%S')}_{target.name}"
    suffix = 1
    while backup.exists() or backup.is_symlink():
        backup = backup_root / f"{time.strftime('%Y%m%d_%H%M%S')}_{target.name}_{suffix}"
        suffix += 1
    shutil.move(str(target), str(backup))
    job.add(f"Module existant sauvegardé: {target} -> {backup}")
    return backup


def link_modules_job(job, project, source):
    project = validate_project(project)
    source_path = Path(source).expanduser().resolve()
    if not source_path.exists() or not source_path.is_dir():
        raise RuntimeError(f"Dossier introuvable: {source_path}")
    link_module_candidates(job, project, find_module_candidates(source_path))


def safe_import_name(filename):
    stem = Path(filename or "modules").stem or "modules"
    return SAFE_IMPORT_NAME_RE.sub("_", stem).strip("._") or "modules"


def safe_extract_zip(zip_path, destination):
    destination.mkdir(parents=True, exist_ok=True)
    base = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename
            if not name or name.startswith(("/", "\\")):
                raise RuntimeError(f"Chemin ZIP invalide: {name}")
            parts = Path(name).parts
            if any(part == ".." for part in parts):
                raise RuntimeError(f"Chemin ZIP dangereux: {name}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise RuntimeError(f"Lien symbolique refuse dans le ZIP: {name}")
            target = (destination / name).resolve()
            if base != target and base not in target.parents:
                raise RuntimeError(f"Extraction hors dossier refusee: {name}")
        archive.extractall(destination)


def import_zip_modules_job(job, project, filename, data, replace_existing=False):
    project = validate_project(project)
    if not filename.lower().endswith(".zip"):
        raise RuntimeError("Le fichier doit etre un ZIP.")
    if not data:
        raise RuntimeError("Fichier ZIP vide.")

    imports_root = project_staging_imports_root(project)
    import_dir = unique_child(imports_root, safe_import_name(filename))
    zip_path = import_dir.with_suffix(".zip")
    imports_root.mkdir(parents=True, exist_ok=True)

    job.add(f"Import ZIP: {filename}")
    job.add(f"Projet cible: {project}")
    if replace_existing:
        job.add("Mode remplacement: actif. Les modules existants seront sauvegardés avant remplacement.")
    zip_path.write_bytes(data)

    try:
        safe_extract_zip(zip_path, import_dir)
    finally:
        try:
            zip_path.unlink()
        except OSError:
            pass

    candidates = find_module_candidates(import_dir)
    job.add(f"Modules detectes dans le ZIP: {len(candidates)}")
    for candidate in candidates:
        job.add(f" - {candidate.name}")
    try:
        link_module_candidates(job, project, candidates, replace_existing=replace_existing)
    finally:
        shutil.rmtree(import_dir, ignore_errors=True)
        job.add(f"Archive temporaire nettoyée: {import_dir}")


def jobs_snapshot():
    with JOBS_LOCK:
        values = list(JOBS.values())[-30:]
        return [
            {
                "id": job.id,
                "title": job.title,
                "status": job.status,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "lines": job.lines,
                "output": job.output,
            }
            for job in reversed(values)
        ]


def clear_jobs_history():
    with JOBS_LOCK:
        running = {job_id: job for job_id, job in JOBS.items() if job.status == "running"}
        JOBS.clear()
        JOBS.update(running)
        return len(running)


def delete_job_history(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise ValueError("Action introuvable.")
        if job.status == "running":
            raise ValueError("Impossible de supprimer une action en cours.")
        del JOBS[job_id]


def compose_service_for(project, pattern="odoo"):
    path = WORKSPACE / project
    code, output = run_capture(docker_command(SETTINGS, "compose", "config", "--services"), cwd=path, timeout=8)
    if code != 0:
        return ""
    for line in output.splitlines():
        service = line.strip()
        if pattern in service.lower():
            return service
    return ""


def read_container_log_file(container):
    paths = [
        "/home/odoo/srv/data/odoo.log",
        "/var/log/odoo/odoo.log",
        "/tmp/odoo.log",
    ]
    shell = "for f in " + " ".join(paths) + "; do if [ -s \"$f\" ]; then echo \"===== $f =====\"; tail -n 260 \"$f\"; exit 0; fi; done; exit 1"
    code, output = run_capture(docker_command(SETTINGS, "exec", container, "sh", "-lc", shell), timeout=10)
    if code == 0 and output.strip():
        return output.strip()
    return ""


def tail_logs(project):
    validate_project(project)
    docker_ok, docker_message = docker_available()
    if not docker_ok:
        return "Docker ne répond pas.\n\n" + docker_message

    container = f"odoo-{project}"
    status = container_status(container)
    sections = [f"Projet: {project}", f"Conteneur: {container} ({status})"]

    if status == "running":
        file_logs = read_container_log_file(container)
        if file_logs:
            sections.append(file_logs)
            return "\n\n".join(sections)

    if status != "absent":
        code, output = run_capture(docker_command(SETTINGS, "logs", "--tail", "260", container), timeout=12)
        if code == 0 and output.strip():
            sections.append("===== docker logs =====")
            sections.append(output.strip())
            return "\n\n".join(sections)
        if output.strip():
            sections.append("docker logs a retourné une erreur:")
            sections.append(output.strip())

    service = compose_service_for(project)
    if service:
        code, output = run_capture(docker_command(SETTINGS, "compose", "logs", "--tail", "260", service), cwd=WORKSPACE / project, timeout=14)
        if code == 0 and output.strip():
            sections.append(f"===== docker compose logs {service} =====")
            sections.append(output.strip())
            return "\n\n".join(sections)
        if output.strip():
            sections.append("docker compose logs a retourné une erreur:")
            sections.append(output.strip())

    sections.append("Aucun log Odoo trouvé. Démarre le projet puis réessaie.")
    return "\n\n".join(sections)


INDEX_HTML = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Odoo Manager API</title>
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #f6f7f9;
      color: #111827;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main {
      width: min(680px, calc(100vw - 32px));
      border: 1px solid #d8dee8;
      border-radius: 12px;
      background: white;
      box-shadow: 0 18px 50px rgba(15, 23, 42, .08);
      padding: 28px;
    }
    h1 { margin: 0 0 8px; font-size: 24px; }
    p { margin: 0 0 14px; color: #4b5563; line-height: 1.5; }
    code { border-radius: 6px; background: #eef2f7; padding: 2px 6px; }
    a { color: #1d4ed8; font-weight: 600; }
  </style>
</head>
<body>
  <main>
    <h1>Odoo Manager API</h1>
    <p>La vue Bootstrap historique a été archivée et n'est plus exposée par le backend.</p>
    <p>L'interface active est maintenant l'application Next/Tauri. En développement, lance <code>./odoo_next_gui.sh</code>.</p>
    <p>Archive locale : <code>archive/bootstrap/odoo_manager_bootstrap_legacy.html</code></p>
  </main>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        add_cors_headers(self)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                return html_response(self, INDEX_HTML)
            if path == "/api/overview":
                return json_response(self, overview())
            if path == "/api/settings":
                return json_response(self, {"settings": settings_snapshot()})
            if path == "/api/system/status":
                return json_response(
                    self,
                    {
                        "docker": docker_status(SETTINGS),
                        "traefik": traefik_status(),
                        "workspace": str(WORKSPACE),
                        "workspace_exists": WORKSPACE.exists() and WORKSPACE.is_dir(),
                    },
                )
            if path == "/api/jobs":
                return json_response(self, {"jobs": jobs_snapshot()})

            match = re.match(r"^/api/projects/([^/]+)/modules$", path)
            if match:
                project = validate_project(urllib.parse.unquote(match.group(1)))
                params = urllib.parse.parse_qs(parsed.query)
                db_name = params.get("db", [""])[0]
                if db_name:
                    validate_db(db_name)
                return json_response(self, {"modules": modules_for(project, db_name)})

            match = re.match(r"^/api/projects/([^/]+)/databases$", path)
            if match:
                project = validate_project(urllib.parse.unquote(match.group(1)))
                return json_response(self, {"databases": list_databases_for(project)})

            match = re.match(r"^/api/projects/([^/]+)/database-versions$", path)
            if match:
                project = validate_project(urllib.parse.unquote(match.group(1)))
                databases = list_databases_for(project)
                return json_response(self, {"versions": database_base_versions(project, databases)})

            match = re.match(r"^/api/projects/([^/]+)/logs$", path)
            if match:
                project = validate_project(urllib.parse.unquote(match.group(1)))
                return json_response(self, {"logs": tail_logs(project)})

            match = re.match(r"^/api/projects/([^/]+)/diagnostics$", path)
            if match:
                project = validate_project(urllib.parse.unquote(match.group(1)))
                return json_response(self, project_diagnostics(project))

            return json_response(self, {"error": "Route introuvable."}, status=404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None
        except Exception as exc:
            return json_response(self, {"error": str(exc)}, status=500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/settings":
            try:
                payload = self.read_json()
                with JOBS_LOCK:
                    running = [job.title for job in JOBS.values() if job.status == "running"]
                if running:
                    raise ValueError("Un traitement est en cours. Attends sa fin avant de changer le workspace.")
                create_workspace = bool(payload.pop("create_workspace", False))
                settings = SETTINGS_STORE.update(payload, create_workspace=create_workspace)
                apply_settings(settings)
                return json_response(self, {"settings": settings_snapshot()})
            except Exception as exc:
                return json_response(self, {"error": str(exc)}, status=400)

        if parsed.path == "/api/system/docker/start":
            result = start_docker(SETTINGS)
            return json_response(self, result, status=200 if result.get("ok") else 400)

        zip_match = re.match(r"^/api/projects/([^/]+)/module-zip$", parsed.path)
        if zip_match:
            try:
                project = validate_project(urllib.parse.unquote(zip_match.group(1)))
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0:
                    raise ValueError("Fichier ZIP manquant.")
                if length > 250 * 1024 * 1024:
                    raise ValueError("ZIP trop volumineux. Limite: 250 Mo.")
                body = self.rfile.read(length)
                fields, files = parse_multipart_form(self.headers.get("Content-Type", ""), body)
                upload = files.get("zip")
                if not upload:
                    raise ValueError("Champ fichier ZIP introuvable.")
                filename = upload.get("filename") or "modules.zip"
                replace_existing = truthy(fields.get("replace_existing"))
                job = Job(f"Importer ZIP {filename}", import_zip_modules_job, (project, filename, upload["data"], replace_existing))
                return json_response(
                    self,
                    {
                        "job": {
                            "id": job.id,
                            "title": job.title,
                            "status": job.status,
                            "started_at": job.started_at,
                            "lines": job.lines,
                        }
                    },
                    status=201,
                )
            except Exception as exc:
                return json_response(self, {"error": str(exc)}, status=400)

        if parsed.path != "/api/jobs":
            return json_response(self, {"error": "Route introuvable."}, status=404)
        try:
            payload = self.read_json()
            action = payload.get("action")

            if action == "start_project":
                project = validate_project(payload.get("project", ""))
                job = Job(f"Démarrer {project}", start_project_job, (project,))
            elif action == "stop_project":
                project = validate_project(payload.get("project", ""))
                job = Job(f"Arrêter {project}", stop_project_job, (project,))
            elif action == "update_project":
                project = validate_project(payload.get("project", ""))
                job = Job(f"MAJ projet {project}", update_project_job, (project,))
            elif action == "update_all":
                job = Job("MAJ tous les projets", update_all_projects_job)
            elif action == "update_all_modules":
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                job = Job(f"Mettre à jour tous les modules sur {db_name}", manager_job, ("--update-all-modules", project, db_name))
            elif action == "update_local_modules":
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                job = Job(f"Mettre à jour les addons projet sur {db_name}", manager_job, ("--update-local-modules", project, db_name))
            elif action == "create_project_terminal":
                job = Job("Ouvrir Terminal pour créer un projet", create_project_terminal_job)
            elif action == "install_traefik":
                job = Job("Installer Traefik", install_traefik_job)
            elif action == "create_database":
                project = validate_project(payload.get("project", ""))
                db_name = validate_db(payload.get("db", ""))
                master_pwd = payload.get("master_pwd", "")
                login = payload.get("login", "")
                password = payload.get("password", "")
                lang = payload.get("lang", "fr_FR")
                country = payload.get("country", "")
                demo = bool(payload.get("demo", False))
                job = Job(f"Créer base {db_name}", create_database_job, (project, db_name, master_pwd, login, password, lang, country, demo))
            elif action == "delete_project":
                project = validate_project(payload.get("project", ""))
                job = Job(f"Supprimer {project}", delete_project_job, (project,))
            elif action == "delete_module_code":
                project = validate_project(payload.get("project", ""))
                modules = validate_modules(payload.get("modules", ""))
                db_name = str(payload.get("db", "") or "").strip()
                uninstall_first = bool(payload.get("uninstall_first", False))
                if uninstall_first:
                    db_name = validate_odoo_db(db_name)
                job = Job(f"Supprimer modules {modules} du projet", delete_module_code_job, (project, modules, db_name, uninstall_first))
            elif action in ("install_module", "update_module", "uninstall_module"):
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                modules = validate_modules(payload.get("modules", ""))
                if action == "install_module":
                    flag = "--install-module"
                    label = "Installer"
                elif action == "uninstall_module":
                    flag = "--uninstall-module"
                    label = "Désinstaller"
                else:
                    flag = "--update-module"
                    label = "Mettre à jour"
                if action == "uninstall_module":
                    job = Job(f"{label} {modules} sur {db_name}", manager_job, (flag, project, db_name, modules))
                else:
                    job = Job(f"{label} {modules} sur {db_name}", module_command_job, (flag, project, db_name, modules))
            elif action == "link_modules":
                project = validate_project(payload.get("project", ""))
                source = payload.get("source", "")
                if not source:
                    raise ValueError("Dossier de modules manquant.")
                job = Job(f"Lier modules dans {project}", link_modules_job, (project, source))
            else:
                return json_response(self, {"error": "Action inconnue."}, status=400)

            return json_response(
                self,
                {
                    "job": {
                        "id": job.id,
                        "title": job.title,
                        "status": job.status,
                        "started_at": job.started_at,
                        "lines": job.lines,
                    }
                },
                status=201,
            )
        except Exception as exc:
            return json_response(self, {"error": str(exc)}, status=400)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        match = re.match(r"^/api/jobs/([0-9]+)$", parsed.path)
        if match:
            try:
                delete_job_history(int(match.group(1)))
                return json_response(self, {"ok": True})
            except Exception as exc:
                return json_response(self, {"error": str(exc)}, status=400)

        if parsed.path != "/api/jobs":
            return json_response(self, {"error": "Route introuvable."}, status=404)
        try:
            running = clear_jobs_history()
            return json_response(self, {"ok": True, "running": running})
        except Exception as exc:
            return json_response(self, {"error": str(exc)}, status=400)


def main():
    if not MANAGER.exists():
        raise SystemExit(f"Script introuvable: {MANAGER}")
    url = f"http://{HOST}:{PORT}/"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        if exc.errno == 48:
            print(f"Interface deja lancee ou port occupe: {url}")
            print("Utilise ./odoo_next_gui.sh --stop puis ./odoo_next_gui.sh --background pour recharger.")
            return
        raise
    print(f"Interface Odoo locale: {url}")
    print(f"Workspace: {WORKSPACE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
