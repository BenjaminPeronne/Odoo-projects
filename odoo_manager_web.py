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

from odoo_manager_core import ManagerSettings, SettingsStore, docker_status, open_terminal, start_docker
from odoo_manager_core.platform import execution_path
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

EXTRA_PATHS = [
    "/Applications/Docker.app/Contents/Resources/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
]

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


def module_import_roots(project):
    return (
        project_imports_root(project).resolve(),
        (WORKSPACE / ".odoo_manager_imports" / project).resolve(),
    )


def unique_child(parent, name):
    candidate = parent / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}"
    suffix = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = parent / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}_{suffix}"
        suffix += 1
    return candidate


def command_env():
    env = os.environ.copy()
    current = env.get("PATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    for path in EXTRA_PATHS:
        if path not in parts:
            parts.append(path)
    env["PATH"] = os.pathsep.join(parts)
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
    base = WORKSPACE / project / "odoo"
    candidates = [
        base / "addons",
        base / "odoo" / "odoo" / "addons",
        base / "odoo" / "addons",
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


def basic_module(path):
    return {
        "name": path.name,
        "title": path.name,
        "summary": "",
        "version": "",
        "category": "",
        "installable": True,
        "path": str(path),
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
    primary_addons = (WORKSPACE / project / "odoo" / "addons").resolve()
    imports_roots = module_import_roots(project)
    project_store = (WORKSPACE / project / "odoo" / "addons-store").resolve()
    parent = path.parent.resolve()

    if parent != primary_addons:
        return {
            "removable": False,
            "removal_mode": "protected",
            "removal_note": "Module hors du dossier odoo/addons du projet.",
        }

    if path.is_symlink():
        target = path.resolve(strict=False)
        if any(path_is_relative_to(target, root) for root in imports_roots):
            return {
                "removable": True,
                "removal_mode": "link_and_import",
                "removal_note": "Supprime le lien odoo/addons et le dossier extrait géré par l'outil.",
            }
        if path_is_relative_to(target, project_store):
            return {
                "removable": False,
                "removal_mode": "protected_store",
                "removal_note": "Module fourni par addons-store; suppression du lien seule le ferait réapparaître.",
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
        base_modules = [basic_module(path) for path in module_dirs(project)]
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
    code = run_stream(job, shell_command(SETTINGS, MANAGER, "--start", project))
    if code != 0:
        raise RuntimeError("Le projet n'a pas demarre correctement.")

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
    primary_addons = (WORKSPACE / project / "odoo" / "addons").resolve()
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


def link_module_candidates(job, project, candidates, replace_existing=False):
    target_parent = WORKSPACE / project / "odoo" / "addons"
    target_parent.mkdir(parents=True, exist_ok=True)
    project_odoo_root = (WORKSPACE / project / "odoo").resolve()

    if not candidates:
        raise RuntimeError("Aucun module Odoo trouve dans ce dossier.")

    linked = 0
    skipped = 0
    for module_path in candidates:
        module_path = module_path.resolve()
        target = target_parent / module_path.name
        if target.exists() or target.is_symlink():
            if target.is_symlink() and target.resolve(strict=False) == module_path:
                job.add(f"Déjà lié: {module_path.name}")
                skipped += 1
                continue
            if replace_existing:
                backup_existing_module(job, project, target)
            else:
                raise RuntimeError(f"Le module existe deja dans le projet: {target}")
        if path_is_relative_to(module_path, project_odoo_root):
            link_value = Path(os.path.relpath(module_path, start=target_parent))
        else:
            link_value = module_path
            job.add(
                f"Attention: {module_path.name} est hors du projet; "
                "ce lien absolu doit aussi être accessible dans le conteneur Docker."
            )
        target.symlink_to(link_value, target_is_directory=True)
        job.add(f"Module lié: {module_path.name} -> {target} ({link_value})")
        linked += 1

    clear_project_module_cache(project)
    job.add(f"Terminé. Modules liés: {linked}. Déjà présents: {skipped}.")
    job.add("Installe ou mets à jour le module depuis l'interface.")


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

    imports_root = project_imports_root(project)
    import_name = f"{time.strftime('%Y%m%d_%H%M%S')}_{safe_import_name(filename)}"
    import_dir = imports_root / import_name
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
    link_module_candidates(job, project, candidates, replace_existing=replace_existing)
    job.add(f"Archive extraite dans: {import_dir}")


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
  <title>Gestionnaire Odoo local</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
  <style>
    :root {
      --page-bg: #f6f7f9;
      --panel-border: #d9dee6;
      --panel-border-strong: #c8d0dc;
      --ink-soft: #667085;
      --row-hover: #f8fbff;
      --row-active: #eef6ff;
    }
    body { background: var(--page-bg); color: #1f2937; }
    .app-shell { max-width: 1540px; }
    .topbar { border-bottom: 1px solid var(--panel-border); background: #fff; }
    .topbar-inner { gap: 1rem; }
    .min-w-0 { min-width: 0; }
    .topbar-inner > .min-w-0 { flex: 1 1 18rem; max-width: 100%; }
    #workspace, #selectedProjectTitle, #selectedProjectUrl { overflow-wrap: anywhere; }
    .topbar-actions, .selected-actions, .panel-actions { display: flex; flex-wrap: wrap; gap: .5rem; }
    .topbar-actions .btn, .selected-actions .btn { white-space: nowrap; }
    .panel { background: #fff; border: 1px solid var(--panel-border); border-radius: 8px; box-shadow: 0 1px 2px rgba(16, 24, 40, .04); }
    .panel-header { border-bottom: 1px solid var(--panel-border); padding: .85rem 1rem; }
    .panel-body { padding: 1rem; }
    .project-row { cursor: pointer; transition: background-color .14s ease, box-shadow .14s ease; }
    .project-row:hover { background: var(--row-hover); }
    .project-row.active { background: var(--row-active); box-shadow: inset 4px 0 0 #2563eb; }
    .project-row:focus-visible { outline: 3px solid rgba(37, 99, 235, .24); outline-offset: -3px; }
    .project-actions { display: inline-flex; gap: .35rem; }
    .status-cell { min-width: 6.25rem; }
    .status-dot { display: inline-block; width: .55rem; height: .55rem; border-radius: 999px; margin-right: .35rem; }
    .dot-running { background: #16a34a; }
    .dot-exited, .dot-created { background: #f59e0b; }
    .dot-absent, .dot-off { background: #94a3b8; }
    .log-box { height: 330px; overflow: auto; background: #101828; color: #d1fadf; border-radius: 8px; padding: .75rem; font-size: .82rem; white-space: pre-wrap; overflow-wrap: anywhere; }
    .module-table { max-height: 470px; overflow: auto; }
    .module-table table, .projects-table { min-width: 760px; }
    .path-cell { max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .job-button {
      position: relative;
      display: block;
      width: 100%;
      border: 1px solid var(--panel-border-strong);
      background: #fff;
      color: #1f2937;
      cursor: pointer;
      font: inherit;
      padding: .85rem 3.15rem .85rem 1rem;
      border-radius: 8px;
      transition: background-color .14s ease, border-color .14s ease, box-shadow .14s ease;
    }
    .job-button:hover,
    .job-button:focus {
      color: #1f2937;
      border-color: #2563eb;
      background: #f8fbff;
    }
    .job-button.is-selected {
      color: #1f2937;
      border-color: #2563eb;
      background: #eff6ff;
      box-shadow: inset 4px 0 0 #2563eb;
    }
    .job-button.is-selected:hover,
    .job-button.is-selected:focus {
      color: #1f2937;
      background: #eaf3ff;
    }
    .job-button:focus-visible { outline: 3px solid rgba(37, 99, 235, .22); outline-offset: 2px; }
    .job-summary { display: flex; align-items: flex-start; justify-content: space-between; gap: .75rem; min-width: 0; }
    .job-title { min-width: 0; overflow-wrap: anywhere; line-height: 1.3; }
    .job-time { color: var(--ink-soft); margin-top: .25rem; }
    .job-status {
      flex: 0 0 auto;
      min-width: 4.4rem;
      max-width: 7rem;
      justify-content: center;
      white-space: nowrap;
      line-height: 1;
      padding: .42rem .58rem;
      border-radius: 7px;
      text-transform: lowercase;
    }
    .job-delete-btn {
      position: absolute;
      right: .55rem;
      top: 50%;
      transform: translateY(-50%);
      opacity: 0;
      pointer-events: none;
      background: #fff;
      transition: opacity .14s ease, background-color .14s ease, color .14s ease, border-color .14s ease;
    }
    .job-button:hover .job-delete-btn,
    .job-button:focus-within .job-delete-btn,
    .job-delete-btn:focus {
      opacity: 1;
      pointer-events: auto;
    }
    .job-delete-btn:hover { color: #fff; background: #dc3545; border-color: #dc3545; }
    .clickable-hint { color: var(--ink-soft); font-size: .78rem; }
    .small-muted { color: var(--ink-soft); font-size: .86rem; }
    .btn-icon { width: 2.35rem; padding-left: 0; padding-right: 0; }
    .btn:disabled, .btn.disabled { cursor: not-allowed; opacity: .58; }
    .btn:focus-visible, .form-control:focus, .form-select:focus { box-shadow: 0 0 0 .2rem rgba(37, 99, 235, .16); }
    .form-label { font-weight: 600; }
    .docker-alert-content { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; }
    .docker-alert-message { min-width: 0; overflow-wrap: anywhere; }
    .docker-alert-actions { display: flex; flex: 0 0 auto; flex-wrap: wrap; gap: .5rem; }
    table { font-size: .92rem; }
    @media (max-width: 1199.98px) {
      .topbar-inner { align-items: flex-start !important; }
      .topbar-actions { width: 100%; }
      .topbar-actions .btn { flex: 1 1 12rem; }
      .selected-actions { width: 100%; }
      .selected-actions .btn:not(.btn-icon) { flex: 1 1 12rem; }
      .log-box { height: 300px; }
    }
    @media (max-width: 767.98px) {
      .app-shell { padding-left: .75rem !important; padding-right: .75rem !important; }
      .topbar h1 { font-size: 1.25rem; }
      .panel-header { align-items: stretch !important; }
      .panel-actions { width: 100%; }
      .panel-actions .btn { flex: 1 1 9rem; }
      .module-table { max-height: none; }
      .log-box { height: 260px; font-size: .78rem; }
      .input-group { flex-wrap: wrap; }
      .input-group > .form-control, .input-group > .form-select { flex: 1 1 100%; width: 100%; }
      .input-group > .btn { flex: 1 1 auto; border-radius: .375rem !important; margin-top: .35rem; }
      .selected-actions .btn, .topbar-actions .btn { flex: 1 1 100%; }
      .job-button { padding: .8rem .85rem; }
      .job-summary { flex-direction: column; gap: .5rem; }
      .job-status { align-self: flex-start; min-width: 0; max-width: 100%; }
      .job-delete-btn {
        position: static;
        transform: none;
        opacity: 1;
        pointer-events: auto;
        margin-top: .65rem;
      }
      .docker-alert-content { flex-direction: column; }
      .docker-alert-actions, .docker-alert-actions .btn { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="app-shell topbar-inner mx-auto px-3 py-3 d-flex flex-wrap align-items-center justify-content-between">
      <div class="min-w-0">
        <h1 class="h4 mb-0">Gestionnaire Odoo local</h1>
        <div class="small-muted" id="workspace"></div>
      </div>
      <div class="topbar-actions">
        <button class="btn btn-outline-secondary" id="refreshBtn"><i class="bi bi-arrow-clockwise"></i> Actualiser</button>
        <button class="btn btn-outline-primary" id="createProjectBtn" title="Ouvre Terminal.app pour lancer Brainkeys"><i class="bi bi-folder-plus"></i> Nouveau projet</button>
        <button class="btn btn-outline-secondary" id="settingsBtn"><i class="bi bi-gear"></i> Paramètres</button>
        <button class="btn btn-primary" id="updateAllBtn" title="Mettre à jour le code et les images Docker de tous les projets"><i class="bi bi-cloud-download"></i> MAJ tous les projets</button>
      </div>
    </div>
  </div>

  <main class="app-shell mx-auto px-3 py-3">
    <div class="alert alert-warning d-none" id="dockerAlert"></div>

    <div class="row g-3">
      <section class="col-12 col-xl-5">
        <div class="panel">
          <div class="panel-header d-flex justify-content-between align-items-center">
            <div>
              <h2 class="h6 mb-0">Projets et bases</h2>
              <div class="clickable-hint">Cliquer sur une ligne pour sélectionner</div>
            </div>
            <span class="badge text-bg-light" id="projectCount">0</span>
          </div>
          <div class="table-responsive">
            <table class="table table-hover align-middle mb-0 projects-table">
              <thead class="table-light">
                <tr>
                  <th>Projet</th>
                  <th>Odoo</th>
                  <th>PostgreSQL</th>
                  <th>Bases</th>
                  <th class="text-end">Actions</th>
                </tr>
              </thead>
              <tbody id="projectsBody"></tbody>
            </table>
          </div>
        </div>
      </section>

      <section class="col-12 col-xl-7">
        <div class="panel mb-3">
          <div class="panel-header d-flex flex-wrap gap-2 align-items-center justify-content-between">
            <div>
              <h2 class="h6 mb-0" id="selectedProjectTitle">Aucun projet sélectionné</h2>
              <div class="small-muted" id="selectedProjectUrl"></div>
            </div>
            <div class="selected-actions">
              <a class="btn btn-outline-secondary disabled" id="openOdooBtn" target="_blank" title="Ouvrir l'instance Odoo"><i class="bi bi-box-arrow-up-right"></i> Odoo</a>
              <button class="btn btn-outline-secondary" id="createDatabaseBtn" disabled title="Créer une nouvelle base Odoo"><i class="bi bi-database-add"></i> Créer base</button>
              <a class="btn btn-outline-secondary disabled btn-icon" id="dbManagerBtn" target="_blank" title="Gestionnaire base Odoo"><i class="bi bi-database-gear"></i></a>
              <button class="btn btn-success" id="startBtn" disabled title="Démarrer le projet"><i class="bi bi-play-fill"></i> Démarrer</button>
              <button class="btn btn-outline-primary" id="updateProjectBtn" disabled title="Met à jour les modules installés présents dans le dossier odoo/addons du projet"><i class="bi bi-arrow-repeat"></i> MAJ addons projet</button>
              <button class="btn btn-outline-primary" id="updateAllModulesBtn" disabled title="Lance odoo -d BASE -u all --stop-after-init sur la base sélectionnée"><i class="bi bi-arrow-clockwise"></i> MAJ complète Odoo (-u all)</button>
              <button class="btn btn-outline-danger" id="deleteProjectBtn" disabled title="Déplacer le projet dans .odoo_manager_deleted"><i class="bi bi-trash"></i> Supprimer</button>
            </div>
          </div>
          <div class="panel-body">
            <div class="alert alert-danger d-none" id="versionAlert"></div>
            <div class="row g-3 align-items-end">
              <div class="col-12 col-md-5">
                <label class="form-label" for="databaseSelect">Base</label>
                <select class="form-select" id="databaseSelect"></select>
              </div>
              <div class="col-12 col-md-7">
                <label class="form-label" for="moduleNames">Modules</label>
                <div class="input-group">
	                  <input class="form-control" id="moduleNames" placeholder="sale,stock ou module_custom">
		                  <button class="btn btn-outline-primary" id="installBtn"><i class="bi bi-plus-circle"></i> Installer</button>
		                  <button class="btn btn-primary" id="upgradeBtn"><i class="bi bi-arrow-repeat"></i> Mettre à jour</button>
		                  <button class="btn btn-outline-danger" id="uninstallBtn"><i class="bi bi-trash"></i> Désinstaller</button>
		                  <button class="btn btn-danger" id="deleteModuleCodeBtn"><i class="bi bi-folder-x"></i> Supprimer code</button>
		                </div>
              </div>
              <div class="col-12">
                <label class="form-label">Ajouter des modules locaux</label>
                <div class="row g-2">
                  <div class="col-12 col-lg-6">
                    <div class="input-group">
                      <input class="form-control" id="sourcePath" placeholder="/Users/.../addons ou /Users/.../mon_module">
                      <button class="btn btn-outline-secondary" id="linkModuleBtn"><i class="bi bi-link-45deg"></i> Lier</button>
                    </div>
                  </div>
	                  <div class="col-12 col-lg-6">
	                    <div class="input-group">
	                      <input class="form-control" type="file" id="zipModuleFile" accept=".zip,application/zip,application/x-zip-compressed">
	                      <button class="btn btn-outline-secondary" id="importZipBtn"><i class="bi bi-file-zip"></i> Importer ZIP</button>
	                    </div>
	                    <div class="form-check mt-2">
	                      <input class="form-check-input" type="checkbox" id="replaceZipModules" checked>
	                      <label class="form-check-label small" for="replaceZipModules">Remplacer les modules existants avec sauvegarde automatique</label>
	                    </div>
	                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-header d-flex flex-wrap gap-2 align-items-center justify-content-between">
            <h2 class="h6 mb-0">Modules</h2>
            <input class="form-control form-control-sm" id="moduleSearch" style="max-width: 320px" placeholder="Filtrer par nom">
          </div>
          <div class="module-table">
            <table class="table table-hover align-middle mb-0">
              <thead class="table-light sticky-top">
                <tr>
                  <th>Module</th>
                  <th>État</th>
                  <th>Version</th>
                  <th>Chemin</th>
                  <th class="text-end">Actions</th>
                </tr>
              </thead>
              <tbody id="modulesBody">
                <tr><td colspan="5" class="text-center text-secondary py-4">Sélectionner un projet.</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section class="col-12">
        <div class="panel">
          <div class="panel-header d-flex flex-wrap gap-2 align-items-center justify-content-between">
            <h2 class="h6 mb-0">Exécutions et logs</h2>
            <div class="panel-actions">
              <button class="btn btn-outline-secondary btn-sm" id="logsBtn" disabled><i class="bi bi-file-text"></i> Logs Odoo</button>
              <button class="btn btn-outline-secondary btn-sm" id="jobsBtn"><i class="bi bi-arrow-clockwise"></i> Rafraîchir jobs</button>
              <button class="btn btn-outline-danger btn-sm" id="clearJobsBtn"><i class="bi bi-trash3"></i> Effacer historique</button>
            </div>
          </div>
          <div class="panel-body">
            <div class="row g-3">
              <div class="col-12 col-lg-4">
                <div id="jobsList" class="vstack gap-2"></div>
              </div>
              <div class="col-12 col-lg-8">
                <pre class="log-box mb-0" id="outputBox">Aucune exécution.</pre>
              </div>
            </div>
          </div>
        </div>
      </section>
    </div>
  </main>

  <div class="modal fade" id="databaseModal" tabindex="-1" aria-labelledby="databaseModalTitle" aria-hidden="true">
    <div class="modal-dialog modal-lg">
      <div class="modal-content">
        <div class="modal-header">
          <div>
            <h2 class="h5 mb-0" id="databaseModalTitle">Créer une base Odoo</h2>
            <div class="small-muted" id="databaseProjectLabel"></div>
          </div>
          <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Fermer"></button>
        </div>
        <div class="modal-body">
          <div class="row g-3">
            <div class="col-12 col-md-6">
              <label class="form-label" for="createDbName">Nom de la base</label>
              <input class="form-control" id="createDbName" placeholder="client_maquette">
            </div>
            <div class="col-12 col-md-6">
              <label class="form-label" for="createDbMasterPwd">Master password</label>
              <input class="form-control" id="createDbMasterPwd" type="password" value="odoo">
            </div>
            <div class="col-12 col-md-6">
              <label class="form-label" for="createDbLogin">Login administrateur</label>
              <input class="form-control" id="createDbLogin" value="admin">
            </div>
            <div class="col-12 col-md-6">
              <label class="form-label" for="createDbPassword">Mot de passe administrateur</label>
              <input class="form-control" id="createDbPassword" type="password" value="admin">
            </div>
            <div class="col-12 col-md-6">
              <label class="form-label" for="createDbLang">Langue</label>
              <select class="form-select" id="createDbLang">
                <option value="fr_FR" selected>Français</option>
                <option value="en_US">English</option>
              </select>
            </div>
            <div class="col-12 col-md-6">
              <label class="form-label" for="createDbCountry">Pays</label>
              <input class="form-control" id="createDbCountry" value="FR" maxlength="2">
            </div>
            <div class="col-12">
              <div class="form-check form-switch">
                <input class="form-check-input" type="checkbox" role="switch" id="createDbDemo">
                <label class="form-check-label" for="createDbDemo">Charger les données de démonstration</label>
              </div>
            </div>
          </div>
        </div>
        <div class="modal-footer justify-content-between">
          <div class="small-muted">Le gestionnaire démarre le projet, crée la base, puis recharge la liste.</div>
          <button type="button" class="btn btn-primary" id="createDbSubmitBtn"><i class="bi bi-database-add"></i> Créer la base</button>
        </div>
      </div>
    </div>
  </div>

  <div class="modal fade" id="deleteProjectModal" tabindex="-1" aria-labelledby="deleteProjectModalTitle" aria-hidden="true">
    <div class="modal-dialog">
      <div class="modal-content">
        <div class="modal-header">
          <div>
            <h2 class="h5 mb-0" id="deleteProjectModalTitle">Supprimer le projet</h2>
            <div class="small-muted" id="deleteProjectLabel"></div>
          </div>
          <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Fermer"></button>
        </div>
        <div class="modal-body">
          <div class="alert alert-warning mb-3">
            Les conteneurs seront arrêtés et le dossier sera déplacé dans <code>.odoo_manager_deleted</code>.
          </div>
          <label class="form-label" for="deleteConfirmName">Confirmer avec le nom du projet</label>
          <input class="form-control" id="deleteConfirmName" autocomplete="off">
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Annuler</button>
          <button type="button" class="btn btn-danger" id="deleteProjectSubmitBtn" disabled><i class="bi bi-trash"></i> Supprimer</button>
        </div>
      </div>
    </div>
  </div>

  <div class="modal fade" id="settingsModal" tabindex="-1" aria-labelledby="settingsModalTitle" aria-hidden="true">
    <div class="modal-dialog modal-lg modal-dialog-scrollable">
      <div class="modal-content">
        <div class="modal-header">
          <div>
            <h2 class="h5 mb-0" id="settingsModalTitle">Paramètres du gestionnaire</h2>
            <div class="small-muted">Le workspace sert à lire les projets et à créer les prochains environnements.</div>
          </div>
          <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Fermer"></button>
        </div>
        <div class="modal-body">
          <div class="row g-3">
            <div class="col-12">
              <label class="form-label" for="settingsWorkspace">Dossier des projets</label>
              <input class="form-control" id="settingsWorkspace" placeholder="/chemin/vers/Odoo-projects">
              <div class="form-text">Le dossier est créé s’il n’existe pas.</div>
            </div>
            <div class="col-12 col-md-6">
              <label class="form-label" for="settingsExecutionMode">Mode d’exécution</label>
              <select class="form-select" id="settingsExecutionMode">
                <option value="native">Natif</option>
                <option value="wsl">WSL 2 (Windows)</option>
              </select>
            </div>
            <div class="col-12 col-md-6">
              <label class="form-label" for="settingsWslDistribution">Distribution WSL</label>
              <input class="form-control" id="settingsWslDistribution" placeholder="Ubuntu">
            </div>
            <div class="col-12 col-md-6">
              <label class="form-label" for="settingsDockerExecutable">Commande Docker</label>
              <input class="form-control" id="settingsDockerExecutable" placeholder="docker">
            </div>
            <div class="col-12 col-md-6">
              <label class="form-label" for="settingsBrainkeysExecutable">Commande Brainkeys</label>
              <input class="form-control" id="settingsBrainkeysExecutable" placeholder="brainkeys">
            </div>
            <div class="col-12">
              <label class="form-label" for="settingsTraefikDirectory">Dossier Traefik</label>
              <input class="form-control" id="settingsTraefikDirectory" placeholder="Détection automatique si vide">
            </div>
            <div class="col-12 col-md-6">
              <label class="form-label" for="settingsTerminal">Terminal</label>
              <input class="form-control" id="settingsTerminal" placeholder="auto">
            </div>
            <div class="col-12 col-md-6">
              <label class="form-label" for="settingsDockerPoll">Vérification Docker (secondes)</label>
              <input class="form-control" id="settingsDockerPoll" type="number" min="3" max="60">
            </div>
            <div class="col-12">
              <div class="rounded border bg-light p-3 small-muted">
                <div id="settingsPlatform"></div>
                <div class="text-break" id="settingsConfigFile"></div>
              </div>
            </div>
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Annuler</button>
          <button type="button" class="btn btn-primary" id="settingsSaveBtn"><i class="bi bi-check2"></i> Enregistrer</button>
        </div>
      </div>
    </div>
  </div>

  <div class="toast-container position-fixed bottom-0 end-0 p-3" id="toastContainer"></div>

  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
  <script>
    let state = { overview: null, systemStatus: null, settings: null, lastDockerState: null, selectedProject: null, modules: [], selectedJobId: null, outputMode: 'job', databaseModal: null, deleteModal: null, settingsModal: null, dockerTimer: null };

    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const api = async (url, options = {}) => {
      const response = await fetch(url, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || response.statusText);
      return payload;
    };
    const notify = (message, kind = 'info') => {
      const container = document.getElementById('toastContainer');
      const element = document.createElement('div');
      const palette = kind === 'error' ? 'text-bg-danger' : kind === 'success' ? 'text-bg-success' : kind === 'warning' ? 'text-bg-warning' : 'text-bg-primary';
      element.className = `toast align-items-center border-0 ${palette}`;
      element.setAttribute('role', 'status');
      element.setAttribute('aria-live', 'polite');
      element.innerHTML = `<div class="d-flex"><div class="toast-body text-break">${esc(message)}</div><button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Fermer"></button></div>`;
      container.appendChild(element);
      const toast = new bootstrap.Toast(element, { delay: 4500 });
      element.addEventListener('hidden.bs.toast', () => element.remove());
      toast.show();
    };
    const badgeClass = (status) => {
      if (status === 'running') return 'text-bg-success';
      if (status === 'exited' || status === 'created') return 'text-bg-warning';
      if (status === 'docker off') return 'text-bg-secondary';
      return 'text-bg-light';
    };
    const dotClass = (status) => {
      if (status === 'running') return 'dot-running';
      if (status === 'exited' || status === 'created') return 'dot-exited';
      if (status === 'docker off') return 'dot-off';
      return 'dot-absent';
    };
    const selectedProject = () => (state.overview?.projects || []).find(p => p.name === state.selectedProject);
    const selectedDatabase = () => document.getElementById('databaseSelect').value;
    const selectedOdooDatabase = () => {
      const db = selectedDatabase();
      return db && db !== 'postgres' ? db : '';
    };
    const major = (version) => String(version || '').split('.')[0] || '';
    const selectedDatabaseVersion = () => {
      const project = selectedProject();
      const db = selectedDatabase();
      return project?.database_versions?.[db] || '';
    };
    const selectedDatabaseCompatible = () => {
      const project = selectedProject();
      const dbVersion = selectedDatabaseVersion();
      if (!project || !dbVersion || !project.odoo_version) return true;
      return major(project.odoo_version) === major(dbVersion);
    };

    function updateWslField() {
      const enabled = document.getElementById('settingsExecutionMode').value === 'wsl';
      document.getElementById('settingsWslDistribution').disabled = !enabled;
    }

    async function refreshSystemStatus() {
      try {
        state.systemStatus = await api('/api/system/status');
        const docker = state.systemStatus.docker;
        const alert = document.getElementById('dockerAlert');
        if (!docker.running) {
          alert.className = `alert ${docker.state === 'missing' ? 'alert-danger' : 'alert-warning'}`;
          alert.innerHTML = `
            <div class="docker-alert-content">
              <div class="docker-alert-message">
                <div class="fw-semibold"><i class="bi bi-exclamation-triangle me-1"></i> Docker n’est pas disponible</div>
                <div class="small mt-1">${esc(docker.message)}</div>
              </div>
              <div class="docker-alert-actions">
                ${docker.can_start ? '<button class="btn btn-sm btn-primary" id="startDockerBtn"><i class="bi bi-play-fill"></i> Ouvrir Docker</button>' : ''}
                <button class="btn btn-sm btn-outline-secondary" id="dockerSettingsBtn"><i class="bi bi-gear"></i> Paramètres</button>
              </div>
            </div>`;
          alert.classList.remove('d-none');
          document.getElementById('startDockerBtn')?.addEventListener('click', startDocker);
          document.getElementById('dockerSettingsBtn').addEventListener('click', openSettingsModal);
        } else {
          alert.classList.add('d-none');
          alert.innerHTML = '';
        }
        if (state.lastDockerState !== docker.state) {
          if (docker.running) notify('Docker est maintenant disponible.', 'success');
          else if (state.lastDockerState !== null) notify(docker.message || 'Docker n’est plus disponible.', 'error');
          else notify('Docker n’est pas démarré. Les actions Odoo sont indisponibles.', 'warning');
          state.lastDockerState = docker.state;
        }
      } catch (error) {
        if (state.lastDockerState !== 'api-error') notify(`État système indisponible : ${error.message}`, 'error');
        state.lastDockerState = 'api-error';
      }
    }

    async function startDocker() {
      try {
        const result = await api('/api/system/docker/start', { method: 'POST' });
        notify(result.message || 'Démarrage de Docker demandé.', 'info');
        window.setTimeout(refreshSystemStatus, 1500);
        window.setTimeout(refreshSystemStatus, 5000);
      } catch (error) {
        notify(error.message, 'error');
      }
    }

    async function loadSettings() {
      const payload = await api('/api/settings');
      state.settings = payload.settings;
      const settings = state.settings;
      document.getElementById('settingsWorkspace').value = settings.workspace || '';
      document.getElementById('settingsExecutionMode').value = settings.execution_mode || 'native';
      document.getElementById('settingsWslDistribution').value = settings.wsl_distribution || '';
      document.getElementById('settingsDockerExecutable').value = settings.docker_executable || 'docker';
      document.getElementById('settingsBrainkeysExecutable').value = settings.brainkeys_executable || 'brainkeys';
      document.getElementById('settingsTraefikDirectory').value = settings.traefik_directory || '';
      document.getElementById('settingsTerminal').value = settings.terminal || 'auto';
      document.getElementById('settingsDockerPoll').value = settings.docker_poll_interval || 10;
      document.getElementById('settingsPlatform').textContent = `Plateforme : ${settings.platform || '-'}`;
      document.getElementById('settingsConfigFile').textContent = `Configuration : ${settings.config_file || '-'}`;
      updateWslField();
      restartDockerTimer();
      return settings;
    }

    async function openSettingsModal() {
      try {
        await loadSettings();
        if (!state.settingsModal) state.settingsModal = new bootstrap.Modal(document.getElementById('settingsModal'));
        state.settingsModal.show();
      } catch (error) {
        notify(error.message, 'error');
      }
    }

    async function saveSettings() {
      const payload = {
        workspace: document.getElementById('settingsWorkspace').value.trim(),
        execution_mode: document.getElementById('settingsExecutionMode').value,
        wsl_distribution: document.getElementById('settingsWslDistribution').value.trim(),
        docker_executable: document.getElementById('settingsDockerExecutable').value.trim(),
        brainkeys_executable: document.getElementById('settingsBrainkeysExecutable').value.trim(),
        traefik_directory: document.getElementById('settingsTraefikDirectory').value.trim(),
        terminal: document.getElementById('settingsTerminal').value.trim(),
        docker_poll_interval: Number(document.getElementById('settingsDockerPoll').value),
        create_workspace: true,
      };
      try {
        const result = await api('/api/settings', { method: 'POST', body: JSON.stringify(payload) });
        state.settings = result.settings;
        state.selectedProject = null;
        state.settingsModal?.hide();
        notify('Paramètres enregistrés.', 'success');
        await Promise.all([refreshOverview(), refreshSystemStatus()]);
        restartDockerTimer();
      } catch (error) {
        notify(error.message, 'error');
      }
    }

    function restartDockerTimer() {
      if (state.dockerTimer) window.clearInterval(state.dockerTimer);
      const seconds = Math.max(3, Number(state.settings?.docker_poll_interval || 10));
      state.dockerTimer = window.setInterval(refreshSystemStatus, seconds * 1000);
    }

    async function refreshOverview() {
      state.overview = await api('/api/overview');
      document.getElementById('workspace').textContent = state.overview.workspace;
      document.getElementById('projectCount').textContent = state.overview.projects.length;
      if (state.selectedProject && !state.overview.projects.some(project => project.name === state.selectedProject)) {
        state.selectedProject = state.overview.projects[0]?.name || null;
      }
      if (!state.selectedProject && state.overview.projects.length) state.selectedProject = state.overview.projects[0].name;
      renderProjects();
      renderSelectedProject();
      if (state.selectedProject) {
        await refreshDatabaseVersions();
        await refreshModules();
      }
    }

    function renderProjects() {
      const body = document.getElementById('projectsBody');
      body.innerHTML = state.overview.projects.map(project => `
        <tr class="project-row ${project.name === state.selectedProject ? 'active' : ''}" data-project="${esc(project.name)}" tabindex="0" role="button" aria-label="Sélectionner ${esc(project.name)}">
          <td><strong>${esc(project.name)}</strong></td>
          <td class="status-cell"><span class="status-dot ${dotClass(project.odoo_status)}"></span><span class="badge ${badgeClass(project.odoo_status)}">${esc(project.odoo_status)}</span></td>
          <td class="status-cell"><span class="status-dot ${dotClass(project.postgres_status)}"></span><span class="badge ${badgeClass(project.postgres_status)}">${esc(project.postgres_status)}</span></td>
          <td>${project.databases.length ? project.databases.map(db => `<span class="badge text-bg-light me-1">${esc(db)}</span>`).join('') : '<span class="text-secondary">-</span>'}</td>
          <td class="text-end">
            <span class="project-actions">
              <button class="btn btn-outline-success btn-sm btn-icon" data-action="start" data-project="${esc(project.name)}" title="Démarrer ${esc(project.name)}" aria-label="Démarrer ${esc(project.name)}"><i class="bi bi-play-fill"></i></button>
              <a class="btn btn-outline-secondary btn-sm btn-icon" href="${esc(project.url)}" target="_blank" title="Ouvrir Odoo ${esc(project.name)}" aria-label="Ouvrir Odoo ${esc(project.name)}"><i class="bi bi-box-arrow-up-right"></i></a>
            </span>
          </td>
        </tr>
      `).join('');
      body.querySelectorAll('.project-row').forEach(row => {
        const selectRow = async (event) => {
          if (event.target.closest('button,a')) return;
          state.selectedProject = row.dataset.project;
          renderProjects();
          renderSelectedProject();
          await refreshDatabaseVersions();
          await refreshModules();
        };
        row.addEventListener('click', selectRow);
        row.addEventListener('keydown', async (event) => {
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          await selectRow(event);
        });
      });
      body.querySelectorAll('[data-action="start"]').forEach(btn => {
        btn.addEventListener('click', () => createJob('start_project', { project: btn.dataset.project }));
      });
    }

    function renderSelectedProject() {
      const project = selectedProject();
      const title = document.getElementById('selectedProjectTitle');
      const url = document.getElementById('selectedProjectUrl');
      const openBtn = document.getElementById('openOdooBtn');
      const dbBtn = document.getElementById('dbManagerBtn');
      const createDbBtn = document.getElementById('createDatabaseBtn');
      const startBtn = document.getElementById('startBtn');
      const updateBtn = document.getElementById('updateProjectBtn');
      const updateAllModulesBtn = document.getElementById('updateAllModulesBtn');
      const deleteBtn = document.getElementById('deleteProjectBtn');
      const logsBtn = document.getElementById('logsBtn');
      const versionAlert = document.getElementById('versionAlert');
      const select = document.getElementById('databaseSelect');
      if (!project) {
        title.textContent = 'Aucun projet sélectionné';
        url.textContent = '';
        select.innerHTML = '';
        versionAlert.classList.add('d-none');
        versionAlert.textContent = '';
        [openBtn, dbBtn].forEach(btn => btn.classList.add('disabled'));
        [createDbBtn, startBtn, updateBtn, updateAllModulesBtn, deleteBtn, logsBtn].forEach(btn => btn.disabled = true);
        return;
      }
      title.textContent = project.name;
      url.textContent = `${project.url}${project.odoo_version ? ` · Odoo ${project.odoo_version}` : ''}`;
      openBtn.href = project.url;
      dbBtn.href = project.database_manager_url;
      [openBtn, dbBtn].forEach(btn => btn.classList.remove('disabled'));
      [createDbBtn, startBtn, updateBtn, updateAllModulesBtn, deleteBtn, logsBtn].forEach(btn => btn.disabled = false);
      select.innerHTML = project.databases.length
        ? project.databases.map(db => {
            const version = project.database_versions?.[db] || '';
            const label = version ? `${db} · base ${version}` : db;
            return `<option value="${esc(db)}" ${db === 'postgres' ? '' : 'selected'}>${esc(label)}</option>`;
          }).join('')
        : '<option value="">Aucune base chargée</option>';
      renderVersionWarning();
    }

    function renderVersionWarning() {
      const project = selectedProject();
      const alert = document.getElementById('versionAlert');
      if (!project) return;
      const db = selectedDatabase();
      const dbVersion = selectedDatabaseVersion();
      if (db && dbVersion && project.odoo_version && major(project.odoo_version) !== major(dbVersion)) {
        alert.textContent = `Base incompatible: le projet local est en Odoo ${project.odoo_version}, mais la base ${db} est en Odoo ${dbVersion}. Recrée ou récupère ce projet dans la même version que la base avant d'installer des modules.`;
        alert.classList.remove('d-none');
      } else {
        alert.classList.add('d-none');
        alert.textContent = '';
      }
      renderModuleActionState();
    }

    async function refreshDatabaseVersions() {
      const project = selectedProject();
      if (!project) return;
      try {
        const payload = await api(`/api/projects/${encodeURIComponent(project.name)}/database-versions`);
        project.database_versions = payload.versions || {};
        renderSelectedProject();
      } catch (error) {
        project.database_versions = {};
      }
    }

    function renderModuleActionState() {
      const compatible = selectedDatabaseCompatible();
      const usableDb = Boolean(selectedOdooDatabase());
	      ['installBtn', 'upgradeBtn', 'uninstallBtn', 'deleteModuleCodeBtn', 'updateProjectBtn', 'updateAllModulesBtn'].forEach(id => {
	        const button = document.getElementById(id);
	        if (button) button.disabled = !compatible || !usableDb;
	      });
    }

    async function refreshModules() {
      const project = selectedProject();
      if (!project) return;
      const db = document.getElementById('databaseSelect').value;
      const query = db ? `?db=${encodeURIComponent(db)}` : '';
      state.modules = (await api(`/api/projects/${encodeURIComponent(project.name)}/modules${query}`)).modules;
      renderModules();
    }

    function renderModules() {
      const search = document.getElementById('moduleSearch').value.trim().toLowerCase();
      const db = document.getElementById('databaseSelect').value;
      const filtered = state.modules.filter(module => {
        return !search || module.name.toLowerCase().includes(search);
      }).slice(0, 250);
      const body = document.getElementById('modulesBody');
      if (!filtered.length) {
        body.innerHTML = '<tr><td colspan="5" class="text-center text-secondary py-4">Aucun module.</td></tr>';
        return;
      }
      body.innerHTML = filtered.map(module => `
        <tr>
          <td>
            <strong>${esc(module.name)}</strong>
            <div class="small-muted">${esc(module.title)}</div>
          </td>
          <td><span class="badge ${module.state === 'installed' ? 'text-bg-success' : 'text-bg-light'}">${esc(module.state)}</span></td>
          <td>${esc(module.installed_version || module.version || '-')}</td>
          <td class="path-cell" title="${esc(module.path)}"><code class="small">${esc(module.path.replace(state.overview.workspace + '/', ''))}</code></td>
	          <td class="text-end">
	            <button class="btn btn-outline-primary btn-sm btn-icon" data-module="${esc(module.name)}" data-action="install" title="Installer ${esc(module.name)}" aria-label="Installer ${esc(module.name)}" ${selectedOdooDatabase() && selectedDatabaseCompatible() ? '' : 'disabled'}><i class="bi bi-plus-circle"></i></button>
	            <button class="btn btn-primary btn-sm btn-icon" data-module="${esc(module.name)}" data-action="upgrade" title="Mettre à jour ${esc(module.name)}" aria-label="Mettre à jour ${esc(module.name)}" ${selectedOdooDatabase() && selectedDatabaseCompatible() ? '' : 'disabled'}><i class="bi bi-arrow-repeat"></i></button>
	            <button class="btn btn-outline-danger btn-sm btn-icon" data-module="${esc(module.name)}" data-action="uninstall" title="Désinstaller ${esc(module.name)}" aria-label="Désinstaller ${esc(module.name)}" ${selectedOdooDatabase() && selectedDatabaseCompatible() && module.state === 'installed' ? '' : 'disabled'}><i class="bi bi-trash"></i></button>
	            <button class="btn btn-danger btn-sm btn-icon" data-module="${esc(module.name)}" data-action="delete-code" title="${esc(module.removal_note || 'Supprimer le code addon')}" aria-label="Supprimer le code ${esc(module.name)}" ${module.removable ? '' : 'disabled'}><i class="bi bi-folder-x"></i></button>
	          </td>
        </tr>
      `).join('');
	      body.querySelectorAll('button[data-module]').forEach(btn => {
	        btn.addEventListener('click', () => {
	          const actionMap = {
	            install: 'install_module',
	            upgrade: 'update_module',
	            uninstall: 'uninstall_module',
	            'delete-code': 'delete_module_code',
	          };
	          if (btn.dataset.action === 'uninstall' && !confirm(`Désinstaller ${btn.dataset.module} de la base ${db} ? Les dossiers addons ne seront pas supprimés.`)) return;
	          if (btn.dataset.action === 'delete-code' && !confirm(`Supprimer réellement ${btn.dataset.module} du dossier addons du projet ? Le module sera désinstallé de la base si nécessaire.`)) return;
	          const action = actionMap[btn.dataset.action] || 'update_module';
	          createJob(action, { project: state.selectedProject, db, modules: btn.dataset.module, uninstall_first: btn.dataset.action === 'delete-code' && Boolean(db) });
	        });
	      });
    }

    async function createJob(action, payload = {}) {
      try {
        const result = await api('/api/jobs', {
          method: 'POST',
          body: JSON.stringify({ action, ...payload }),
        });
        state.selectedJobId = result.job.id;
        state.outputMode = 'job';
        await refreshJobs();
        return result.job;
      } catch (error) {
        document.getElementById('outputBox').textContent = `Erreur: ${error.message}`;
      }
    }

    async function refreshJobs() {
      const jobs = (await api('/api/jobs')).jobs;
      const list = document.getElementById('jobsList');
      list.innerHTML = jobs.map(job => `
        <button class="job-button text-start ${state.outputMode === 'job' && job.id === state.selectedJobId ? 'is-selected' : ''}" data-job="${job.id}">
          <div class="job-summary">
            <strong class="job-title">${esc(job.title)}</strong>
            <span class="badge job-status ${job.status === 'done' ? 'text-bg-success' : job.status === 'error' ? 'text-bg-danger' : 'text-bg-warning'}">${esc(job.status)}</span>
          </div>
          <div class="small job-time">${esc(job.started_at)}</div>
          <span class="btn btn-sm btn-outline-danger btn-icon job-delete-btn" data-delete-job="${job.id}" title="Supprimer cette entrée" aria-label="Supprimer ${esc(job.title)}"><i class="bi bi-trash3"></i></span>
        </button>
      `).join('') || '<div class="text-secondary">Aucune exécution.</div>';
      list.querySelectorAll('[data-job]').forEach(btn => {
        btn.addEventListener('click', async () => {
          state.selectedJobId = Number(btn.dataset.job);
          state.outputMode = 'job';
          await refreshJobs();
        });
      });
      list.querySelectorAll('[data-delete-job]').forEach(btn => {
        btn.addEventListener('click', async (event) => {
          event.preventDefault();
          event.stopPropagation();
          await deleteJobHistory(Number(btn.dataset.deleteJob));
        });
      });
      const selected = jobs.find(job => job.id === state.selectedJobId) || jobs[0];
      const output = document.getElementById('outputBox');
      if (state.outputMode === 'job') {
        if (selected) {
          output.textContent = selected.output || selected.lines.join('\\n') || 'En attente de sortie...';
          output.scrollTop = output.scrollHeight;
        } else {
          output.textContent = 'Aucune exécution.';
        }
      }
      if (jobs.some(job => job.status === 'running')) {
        window.setTimeout(refreshJobs, 1800);
      } else {
        window.setTimeout(refreshOverview, 700);
      }
    }

    async function importZipModules() {
      const project = selectedProject();
      const input = document.getElementById('zipModuleFile');
      const file = input.files?.[0];
      if (!project) {
        document.getElementById('outputBox').textContent = 'Erreur: aucun projet sélectionné.';
        return;
      }
      if (!file) {
        document.getElementById('outputBox').textContent = 'Erreur: sélectionne un fichier ZIP.';
        return;
      }
	      const form = new FormData();
	      form.append('zip', file);
	      form.append('replace_existing', document.getElementById('replaceZipModules').checked ? '1' : '0');
	      const response = await fetch(`/api/projects/${encodeURIComponent(project.name)}/module-zip`, {
        method: 'POST',
        body: form,
      });
      const payload = await response.json();
      if (!response.ok) {
        document.getElementById('outputBox').textContent = `Erreur: ${payload.error || response.statusText}`;
        return;
      }
      input.value = '';
      state.selectedJobId = payload.job.id;
      state.outputMode = 'job';
      await refreshJobs();
      await refreshModules();
      window.setTimeout(refreshModules, 1500);
      window.setTimeout(refreshModules, 3500);
    }

    async function showLogs() {
      const project = selectedProject();
      if (!project) return;
      const payload = await api(`/api/projects/${encodeURIComponent(project.name)}/logs`);
      state.outputMode = 'external';
      document.getElementById('outputBox').textContent = payload.logs || 'Aucun log.';
    }

    async function clearJobsHistory() {
      try {
        const response = await fetch('/api/jobs', { method: 'DELETE' });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || response.statusText);
        state.selectedJobId = null;
        document.getElementById('outputBox').textContent = payload.running
          ? `Historique effacé. ${payload.running} exécution(s) encore en cours.`
          : 'Historique effacé.';
        await refreshJobs();
      } catch (error) {
        document.getElementById('outputBox').textContent = `Erreur: ${error.message}`;
      }
    }

    async function deleteJobHistory(jobId) {
      try {
        const response = await fetch(`/api/jobs/${jobId}`, { method: 'DELETE' });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || response.statusText);
        if (state.selectedJobId === jobId) state.selectedJobId = null;
        await refreshJobs();
      } catch (error) {
        document.getElementById('outputBox').textContent = `Erreur: ${error.message}`;
      }
    }

    function openCreateDatabaseModal() {
      const project = selectedProject();
      if (!project) return;
      if (!state.databaseModal) {
        state.databaseModal = new bootstrap.Modal(document.getElementById('databaseModal'));
      }
      document.getElementById('databaseProjectLabel').textContent = project.name;
      document.getElementById('createDbName').value = '';
      document.getElementById('createDbMasterPwd').value = 'odoo';
      document.getElementById('createDbLogin').value = 'admin';
      document.getElementById('createDbPassword').value = 'admin';
      document.getElementById('createDbLang').value = 'fr_FR';
      document.getElementById('createDbCountry').value = 'FR';
      document.getElementById('createDbDemo').checked = false;
      state.databaseModal.show();
      window.setTimeout(() => document.getElementById('createDbName').focus(), 150);
    }

    async function submitCreateDatabase() {
      const project = selectedProject();
      if (!project) return;
      const payload = {
        project: project.name,
        db: document.getElementById('createDbName').value.trim(),
        master_pwd: document.getElementById('createDbMasterPwd').value,
        login: document.getElementById('createDbLogin').value.trim(),
        password: document.getElementById('createDbPassword').value,
        lang: document.getElementById('createDbLang').value,
        country: document.getElementById('createDbCountry').value.trim().toUpperCase(),
        demo: document.getElementById('createDbDemo').checked,
      };
      await createJob('create_database', payload);
      state.databaseModal?.hide();
    }

    function openDeleteProjectModal() {
      const project = selectedProject();
      if (!project) return;
      if (!state.deleteModal) {
        state.deleteModal = new bootstrap.Modal(document.getElementById('deleteProjectModal'));
      }
      document.getElementById('deleteProjectLabel').textContent = project.name;
      const input = document.getElementById('deleteConfirmName');
      input.value = '';
      document.getElementById('deleteProjectSubmitBtn').disabled = true;
      state.deleteModal.show();
      window.setTimeout(() => input.focus(), 150);
    }

    function updateDeleteConfirmation() {
      const project = selectedProject();
      const value = document.getElementById('deleteConfirmName').value.trim();
      document.getElementById('deleteProjectSubmitBtn').disabled = !project || value !== project.name;
    }

    async function submitDeleteProject() {
      const project = selectedProject();
      if (!project) return;
      await createJob('delete_project', { project: project.name });
      state.deleteModal?.hide();
    }

    document.getElementById('refreshBtn').addEventListener('click', refreshOverview);
    document.getElementById('jobsBtn').addEventListener('click', refreshJobs);
    document.getElementById('clearJobsBtn').addEventListener('click', clearJobsHistory);
    document.getElementById('logsBtn').addEventListener('click', showLogs);
    document.getElementById('createProjectBtn').addEventListener('click', () => createJob('create_project_terminal'));
    document.getElementById('settingsBtn').addEventListener('click', openSettingsModal);
    document.getElementById('settingsSaveBtn').addEventListener('click', saveSettings);
    document.getElementById('settingsExecutionMode').addEventListener('change', updateWslField);
    document.getElementById('startBtn').addEventListener('click', () => createJob('start_project', { project: state.selectedProject }));
    document.getElementById('updateProjectBtn').addEventListener('click', () => {
      createJob('update_local_modules', {
        project: state.selectedProject,
        db: selectedOdooDatabase(),
      }).then(() => window.setTimeout(refreshModules, 2500));
    });
    document.getElementById('updateAllModulesBtn').addEventListener('click', () => {
      const db = selectedOdooDatabase();
      if (!db) return;
      if (!confirm(`Lancer une mise à jour complète Odoo sur la base ${db} ?\\n\\nEquivalent: odoo -d ${db} -u all --stop-after-init`)) return;
      createJob('update_all_modules', {
        project: state.selectedProject,
        db,
      }).then(() => window.setTimeout(refreshModules, 2500));
    });
    document.getElementById('createDatabaseBtn').addEventListener('click', openCreateDatabaseModal);
    document.getElementById('createDbSubmitBtn').addEventListener('click', submitCreateDatabase);
    document.getElementById('deleteProjectBtn').addEventListener('click', openDeleteProjectModal);
    document.getElementById('deleteConfirmName').addEventListener('input', updateDeleteConfirmation);
    document.getElementById('deleteProjectSubmitBtn').addEventListener('click', submitDeleteProject);
    document.getElementById('updateAllBtn').addEventListener('click', () => createJob('update_all'));
    document.getElementById('databaseSelect').addEventListener('change', refreshModules);
    document.getElementById('databaseSelect').addEventListener('change', renderVersionWarning);
    document.getElementById('moduleSearch').addEventListener('input', renderModules);
    document.getElementById('installBtn').addEventListener('click', () => {
      createJob('install_module', {
        project: state.selectedProject,
        db: document.getElementById('databaseSelect').value,
        modules: document.getElementById('moduleNames').value.trim(),
      });
    });
	    document.getElementById('upgradeBtn').addEventListener('click', () => {
	      createJob('update_module', {
	        project: state.selectedProject,
	        db: document.getElementById('databaseSelect').value,
	        modules: document.getElementById('moduleNames').value.trim(),
	      });
	    });
		    document.getElementById('uninstallBtn').addEventListener('click', () => {
		      const db = document.getElementById('databaseSelect').value;
		      const modules = document.getElementById('moduleNames').value.trim();
		      if (!modules) return;
		      if (!confirm(`Désinstaller ${modules} de la base ${db} ? Les dossiers addons ne seront pas supprimés.`)) return;
	      createJob('uninstall_module', {
	        project: state.selectedProject,
	        db,
		        modules,
		      });
		    });
		    document.getElementById('deleteModuleCodeBtn').addEventListener('click', () => {
		      const db = document.getElementById('databaseSelect').value;
		      const modules = document.getElementById('moduleNames').value.trim();
		      if (!modules) return;
		      if (!confirm(`Supprimer réellement ${modules} du dossier addons du projet ? Les modules installés seront d'abord désinstallés de la base.`)) return;
		      createJob('delete_module_code', {
		        project: state.selectedProject,
		        db,
		        modules,
		        uninstall_first: true,
		      });
		    });
		    document.getElementById('importZipBtn').addEventListener('click', importZipModules);
    document.getElementById('linkModuleBtn').addEventListener('click', () => {
      createJob('link_modules', {
        project: state.selectedProject,
        source: document.getElementById('sourcePath').value.trim(),
      }).then(() => window.setTimeout(refreshModules, 1200));
    });

    Promise.all([refreshOverview(), refreshJobs(), loadSettings(), refreshSystemStatus()]).catch(error => notify(error.message, 'error'));
  </script>
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
                job = Job(f"Démarrer {project}", manager_job, ("--start", project))
            elif action == "update_project":
                project = validate_project(payload.get("project", ""))
                job = Job(f"MAJ projet {project}", manager_job, ("--update", project))
            elif action == "update_all":
                job = Job("MAJ tous les projets", manager_job, ("--update-all",))
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
                job = Job(f"{label} {modules} sur {db_name}", manager_job, (flag, project, db_name, modules))
            elif action == "link_modules":
                project = validate_project(payload.get("project", ""))
                source = payload.get("source", "")
                if not source:
                    raise ValueError("Dossier source manquant.")
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
            print("Utilise ./odoo_gui.sh --restart pour recharger la derniere version.")
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
