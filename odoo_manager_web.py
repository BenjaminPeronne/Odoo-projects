#!/usr/bin/env python3
import ast
import errno
import html
import http.client
import json
import ntpath
import os
import posixpath
import queue
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import tempfile
import time
import traceback
import urllib.parse
import urllib.request
import urllib.error
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath

from odoo_manager_runtime import initialize_runtime_streams


RUNTIME_LOG_PATH, _RUNTIME_STREAMS = initialize_runtime_streams()

from odoo_manager_core import ManagerSettings, ProjectCreator, SettingsStore, ProjectService, docker_status, start_docker
from odoo_manager_core.platform import (
    command_uses_wsl,
    command_prefix,
    executable_available,
    executable_search_path,
    execution_path,
    host_executable_available,
    hidden_process_kwargs,
    open_terminal_command,
    platform_id,
    resolve_executable,
    resolve_host_executable,
    workspace_command_prefix,
    workspace_execution_path,
    workspace_wsl_context,
    wsl_command_prefix,
    wsl_executable_available,
    wsl_command_with_cwd,
    find_wsl_executable_distribution,
    reset_wsl_executable_cache,
    wsl_execution_path,
    wsl_windows_path,
    wsl_path_context,
    wsl_unc_path,
)
from odoo_manager_core.project_creator import (
    SUPPORTED_ODOO_VERSIONS,
    abandoned_staging_entries,
    validate_git_ref,
    validate_gitlab_repository,
    validate_new_project_name,
    validate_odoo_version,
)
from odoo_manager_core.migration import (
    compare_projects,
    copy_project,
    copy_project_privileged,
    is_project_directory,
    list_tree,
    measure_project,
    migration_candidates,
    privileged_prefix,
    project_is_stopped,
)
from odoo_manager_core import jobs as job_control
from odoo_manager_core.project_service import terminate_active_processes as terminate_project_processes
from odoo_manager_core.traefik import url_with_port
from odoo_manager_core.odoo_log_display import OdooLogDisplay, compact_odoo_log_text
from odoo_manager_core.docker_api import EngineUnavailable
from odoo_manager_core.system import (
    active_engine_client,
    docker_command,
    reset_docker_backend_cache,
    shell_command,
)
from odoo_manager_core.windows_links import (
    MIGRATION_JOURNAL_NAME,
    contains_wsl_symlink,
    convert_wsl_symlinks,
    is_wsl_symlink,
    native_symlinks_supported,
    wsl_symlinks,
)


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()
workspace_candidates = (
    Path.home() / "Documents" / "Developer" / "Odoo-projects",
    Path.home() / "Documents" / "Odoo-projects",
    Path.home() / "Odoo-projects",
)
DEFAULT_WORKSPACE_FALLBACK = next(
    (path for path in workspace_candidates if path.is_dir()),
    workspace_candidates[-1] if getattr(sys, "frozen", False) else ROOT,
)
DEFAULT_WORKSPACE = Path(os.environ.get("ODOO_WORKSPACE", DEFAULT_WORKSPACE_FALLBACK)).resolve()
SETTINGS_STORE = SettingsStore(DEFAULT_WORKSPACE)
SETTINGS = SETTINGS_STORE.load()
WORKSPACE = Path(SETTINGS.workspace).resolve()
MANAGER = Path(os.environ.get("ODOO_MANAGER_SCRIPT", ROOT / "odoo_manager.sh")).resolve()
LOCAL_MODULE_OVERRIDES = SETTINGS_STORE.path.with_name("local_module_overrides.json")
DELETED_PROJECTS = WORKSPACE / ".odoo_manager_deleted"
DELETED_MODULES = WORKSPACE / ".odoo_manager_deleted_modules"
HOST = os.environ.get("ODOO_GUI_HOST", "127.0.0.1")
PORT = int(os.environ.get("ODOO_GUI_PORT", str(SETTINGS.api_port)))
TRAEFIK_REPO = "ssh://git@gitlab.sudokeys.com:10022/devops/docker-local-tools.git"

SAFE_PROJECT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_MODULE_RE = re.compile(r"^[A-Za-z0-9_,.-]+$")
SAFE_IMPORT_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_path_is_dir(path):
    """Return False when the host cannot currently inspect a directory.

    Windows UNC paths backed by WSL can raise ``WinError 1`` while the
    distribution is stopped or reconnecting. Filesystem availability is a
    runtime state, so UI snapshots must not fail entirely in that situation.
    """
    try:
        return Path(path).is_dir()
    except OSError:
        return False


def safe_path_exists(path):
    """Return False when a path is absent or temporarily inaccessible."""
    try:
        return Path(path).exists()
    except OSError:
        return False


MAX_DB_NAME_BYTES = 63
MAX_JSON_BODY_BYTES = 1024 * 1024
MAX_RETAINED_JOBS = 60
MAX_RUNNING_JOBS = 4
JOB_LINES_LIMIT = 700
JOB_OUTPUT_LIMIT = 120_000
MAX_ZIP_ENTRIES = 100_000
MAX_ZIP_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_DATABASE_BACKUP_BYTES = int(os.environ.get("ODOO_MANAGER_MAX_BACKUP_BYTES", 100 * 1024 * 1024 * 1024))
MAX_DATABASE_BACKUP_ENTRIES = 2_000_000

# Catalogue aligné sur https://www.odoo.com/fr_FR/page/all-apps : ordre et
# catégories de la page. Les modules absents d'une version sont signalés par l'UI.
SOCLE_SECTIONS = (
    ("website", "Site internet"),
    ("sales", "Ventes"),
    ("finance", "Finance"),
    ("inventory-manufacturing", "Inventaire & Fabrication"),
    ("human-resources", "Ressources humaines"),
    ("marketing", "Marketing"),
    ("services", "Services"),
    ("productivity", "Productivité"),
    ("customization", "Personnalisation"),
)

SOCLE_APPS = (
    ("website", "Site Web", "website", ("website",)),
    ("ecommerce", "eCommerce", "website", ("website_sale",)),
    ("blog", "Blog", "website", ("website_blog",)),
    ("forum", "Forum", "website", ("website_forum",)),
    ("elearning", "eLearning", "website", ("website_slides",)),
    ("live_chat", "Live Chat", "website", ("im_livechat",)),
    ("crm", "CRM", "sales", ("crm",)),
    ("sales", "Ventes", "sales", ("sale_management",)),
    ("point_of_sale", "Point de Vente", "sales", ("point_of_sale",)),
    ("subscriptions", "Abonnements", "sales", ("sale_subscription",)),
    ("rental", "Location", "sales", ("sale_renting",)),
    ("accounting_fr", "Comptabilité française", "finance", ("account_accountant", "l10n_fr")),
    ("invoicing", "Facturation", "finance", ("account",)),
    ("expenses", "Notes de frais", "finance", ("hr_expense",)),
    ("documents", "Documents", "finance", ("documents",)),
    ("spreadsheet", "Feuilles de calcul", "finance", ("documents_spreadsheet",)),
    ("sign", "Signature", "finance", ("sign",)),
    ("esg", "ESG", "finance", ("esg",)),
    ("inventory", "Inventaire", "inventory-manufacturing", ("stock",)),
    ("manufacturing", "Fabrication", "inventory-manufacturing", ("mrp",)),
    ("plm", "PLM", "inventory-manufacturing", ("mrp_plm",)),
    ("purchase", "Achats", "inventory-manufacturing", ("purchase",)),
    ("maintenance", "Maintenance", "inventory-manufacturing", ("maintenance",)),
    ("quality", "Qualité", "inventory-manufacturing", ("quality_control",)),
    ("employees", "Employés", "human-resources", ("hr",)),
    ("recruitment", "Recrutement", "human-resources", ("hr_recruitment",)),
    ("time_off", "Congés", "human-resources", ("hr_holidays",)),
    ("appraisals", "Évaluations", "human-resources", ("hr_appraisal",)),
    ("referrals", "Recommandation", "human-resources", ("hr_referral",)),
    ("fleet", "Parc automobile", "human-resources", ("fleet",)),
    ("marketing_automation", "Automatisation Marketing", "marketing", ("marketing_automation",)),
    ("email_marketing", "E-mail Marketing", "marketing", ("mass_mailing",)),
    ("sms_marketing", "Marketing par SMS", "marketing", ("mass_mailing_sms",)),
    ("social_marketing", "Social Marketing", "marketing", ("social",)),
    ("events", "Événements", "marketing", ("event",)),
    ("surveys", "Sondage", "marketing", ("survey",)),
    ("project", "Projet", "services", ("project",)),
    ("timesheets", "Feuilles de temps", "services", ("hr_timesheet",)),
    ("field_service", "Services sur site", "services", ("industry_fsm",)),
    ("helpdesk", "Assistance", "services", ("helpdesk",)),
    ("planning", "Planification", "services", ("planning",)),
    ("appointments", "Rendez-vous", "services", ("appointment",)),
    ("discuss", "Discussion", "productivity", ("mail",)),
    ("approvals", "Validations", "productivity", ("approvals",)),
    ("iot", "Internet des Objets", "productivity", ("iot",)),
    ("voip", "VoIP", "productivity", ("voip",)),
    ("knowledge", "Connaissances", "productivity", ("knowledge",)),
    ("ai", "IA", "productivity", ("ai_app",)),
    ("studio", "Studio", "customization", ("web_studio",)),
)

SOCLE_PRESETS = {app_id: (label, modules) for app_id, label, _section, modules in SOCLE_APPS}

# États pour lesquels Odoo considère une dépendance comme satisfaite.
INSTALLED_MODULE_STATES = frozenset(("installed", "to install", "to upgrade"))
MODULE_GRAPH_CACHE = {}
MODULE_GRAPH_TTL_SECONDS = 45

JOBS = {}
JOBS_LOCK = threading.Lock()
NEXT_JOB_ID = 1
ACTIVE_PROCESSES = set()
ACTIVE_PROCESSES_LOCK = threading.Lock()

EVENT_SUBSCRIBERS = set()
EVENT_SUBSCRIBERS_LOCK = threading.Lock()
EVENT_WATCH_INTERVAL_SECONDS = 2
# La liste des bases coûte un `docker exec psql` par projet démarré : inutile toutes les 2 s.
EVENT_DATABASES_MAX_AGE_SECONDS = 30
OVERVIEW_DATABASES_CACHE = {}
OVERVIEW_DATABASES_CACHE_LOCK = threading.Lock()
# Sondes `docker exec` simultanées (overview, diagnostic) ; au-delà elles attendent leur tour.
DOCKER_PROBE_WORKERS = 8
EVENT_DOCKER_REFRESH = threading.Event()
_EVENT_WATCH_THREAD_STARTED = False
_EVENT_WATCH_THREAD_LOCK = threading.Lock()
LOCAL_MODULE_OVERRIDES_LOCK = threading.Lock()
ERROR_LOG_LOCK = threading.Lock()
ERROR_LOG_PATH = SETTINGS_STORE.path.with_name("errors.jsonl")
MAX_ERROR_LOG_BYTES = 2 * 1024 * 1024
MAX_ERROR_ENTRIES_RETURNED = 250


def sanitize_error_text(value, limit=4000):
    text = str(value or "").strip()
    text = re.sub(r"(https?://[^:/\s]+:)[^@/\s]+@", r"\1***@", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)(password|passwd|token|secret|authorization)(\s*[:=]\s*)[^\s,;]+",
        r"\1\2***",
        text,
    )
    return text[:limit]


def record_manager_error(source, message, *, details="", project=""):
    entry = {
        "id": time.time_ns(),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": sanitize_error_text(source, 160) or "manager",
        "project": sanitize_error_text(project, 120),
        "message": sanitize_error_text(message, 1200) or "Erreur sans message.",
        "details": sanitize_error_text(details, 4000),
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    try:
        with ERROR_LOG_LOCK:
            ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if ERROR_LOG_PATH.exists() and ERROR_LOG_PATH.stat().st_size >= MAX_ERROR_LOG_BYTES:
                previous = ERROR_LOG_PATH.with_name("errors.previous.jsonl")
                previous.unlink(missing_ok=True)
                ERROR_LOG_PATH.replace(previous)
            with ERROR_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError:
        traceback.print_exc()
    return entry


def manager_errors_snapshot():
    entries = []
    with ERROR_LOG_LOCK:
        lines = []
        for path in (ERROR_LOG_PATH.with_name("errors.previous.jsonl"), ERROR_LOG_PATH):
            try:
                lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                continue
    for line in lines[-MAX_ERROR_ENTRIES_RETURNED:]:
        try:
            entry = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    entries.reverse()
    return {"entries": entries, "path": str(ERROR_LOG_PATH)}


def clear_manager_errors():
    with ERROR_LOG_LOCK:
        ERROR_LOG_PATH.unlink(missing_ok=True)
        ERROR_LOG_PATH.with_name("errors.previous.jsonl").unlink(missing_ok=True)


def truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "oui"}


def path_is_relative_to(path, parent):
    """Comparaison lexicale, comme PurePath.relative_to, sans en recopier les segments.

    relative_to reconstruit chaque parent en Python : 55 % du temps de la liste
    des modules (4 000 appels pour un projet Enterprise).
    """
    flavour = ntpath if isinstance(path, PureWindowsPath) else posixpath
    path_text = flavour.normcase(str(path))
    parent_text = flavour.normcase(str(parent))
    if path_text == parent_text:
        return True
    return path_text.startswith(parent_text.rstrip(flavour.sep) + flavour.sep)


def project_imports_root(project):
    return WORKSPACE / project / "odoo" / "addons-store" / ".odoo_manager_imports"


def project_staging_imports_root(project):
    return WORKSPACE / ".odoo_manager_imports" / project


def database_restore_staging_root(project):
    return WORKSPACE / ".odoo_manager_imports" / "database-restores" / project


def module_import_roots(project):
    roots = (project_imports_root(project), project_staging_imports_root(project))
    if active_workspace_wsl_context():
        return roots
    return tuple(root.resolve() for root in roots)


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
    env["ODOO_WORKSPACE"] = str(WORKSPACE)
    if SETTINGS.traefik_directory:
        env["TRAEFIK_DIR"] = str(Path(SETTINGS.traefik_directory).expanduser())
    env["ODOO_MANAGER_EXECUTION_MODE"] = SETTINGS.execution_mode
    env["ODOO_MANAGER_DOCKER"] = SETTINGS.docker_executable
    env["ODOO_MANAGER_BRAINKEYS"] = SETTINGS.brainkeys_executable
    if SETTINGS.wsl_distribution:
        env["ODOO_MANAGER_WSL_DISTRIBUTION"] = SETTINGS.wsl_distribution
    return env


def active_workspace_wsl_context():
    if platform_id() != "windows":
        return None
    return workspace_wsl_context(SETTINGS, WORKSPACE)


def workspace_tool_prefix():
    return workspace_command_prefix(SETTINGS, WORKSPACE)


def workspace_tool_cwd():
    return Path.home() if active_workspace_wsl_context() else WORKSPACE


def apply_settings(settings):
    global SETTINGS, WORKSPACE, DELETED_PROJECTS, DELETED_MODULES
    SETTINGS = settings
    WORKSPACE = Path(settings.workspace).resolve()
    DELETED_PROJECTS = WORKSPACE / ".odoo_manager_deleted"
    DELETED_MODULES = WORKSPACE / ".odoo_manager_deleted_modules"
    with MODULE_CACHE_LOCK:
        MODULE_CACHE.clear()
        WSL_MODULE_METADATA.clear()
    reset_docker_backend_cache()


def settings_snapshot():
    payload = SETTINGS.to_dict()
    payload.update(
        {
            "config_file": str(SETTINGS_STORE.path),
            "workspace_exists": safe_path_is_dir(WORKSPACE),
            "platform": platform_id(),
            "api_port_actual": PORT,
        }
    )
    return payload


BROWSER_ORIGINS = frozenset({
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://tauri.localhost",
    "tauri://localhost",
    "https://tauri.localhost",
    "app://sdk",
})
LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def allowed_browser_origins():
    return BROWSER_ORIGINS | {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}


def request_hostname(host_header):
    host = str(host_header or "").strip().lower()
    if host.startswith("["):
        return host[1:host.index("]")] if "]" in host else ""
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def untrusted_request_reason(headers):
    """Protège l'API locale, sans authentification, des pages web du navigateur.

    Un Host hors loopback trahit un DNS rebinding. Un Origin étranger trahit une
    requête CSRF : les requêtes « simples » (text/plain, multipart) partent sans
    preflight et la CORS n'empêche que la lecture de la réponse, pas l'action.
    Les clients locaux sans navigateur (Electron main, scripts) n'envoient pas d'Origin.
    """
    if request_hostname(headers.get("Host")) not in LOOPBACK_HOSTNAMES:
        return "Hôte non autorisé."
    origin = headers.get("Origin")
    if origin is not None and origin not in allowed_browser_origins():
        return "Origine non autorisée."
    return ""


def add_cors_headers(handler):
    origin = handler.headers.get("Origin", "")
    if origin in allowed_browser_origins():
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")


def json_response(handler, payload, status=200):
    if status >= 400 and isinstance(payload, dict) and payload.get("error"):
        record_manager_error(
            f"API {getattr(handler, 'command', '')} {getattr(handler, 'path', '')}",
            payload["error"],
            details=f"HTTP {status}",
        )
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(body)))
        add_cors_headers(handler)
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        return None


def empty_response(handler, status=204):
    try:
        handler.send_response(status)
        handler.send_header("Content-Length", "0")
        add_cors_headers(handler)
        handler.end_headers()
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
    requested_cwd = Path(cwd) if cwd is not None else None
    command_cwd = requested_cwd
    if command_cwd is None:
        command_cwd = next(
            (
                candidate
                for candidate in (WORKSPACE, WORKSPACE.parent, Path.home(), ROOT)
                if safe_path_is_dir(candidate)
            ),
            Path.cwd(),
        )
    command = [str(argument) for argument in args]
    if platform_id() == "windows" and command_uses_wsl(command):
        try:
            command = wsl_command_with_cwd(command, command_cwd, SETTINGS, WORKSPACE)
        except RuntimeError as exc:
            return 2, str(exc)
        command_cwd = Path.home()
    elif requested_cwd is not None and not safe_path_is_dir(requested_cwd):
        return 2, f"Dossier de travail introuvable: {requested_cwd}"
    try:
        result = job_control.run_process(
            command,
            timeout,
            cwd=str(command_cwd),
            env=command_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_process_kwargs(),
        )
        return result.returncode, result.stdout.strip()
    except OSError as exc:
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
    return str(default_traefik_directory())


def local_traefik_directory():
    if SETTINGS.traefik_directory:
        return Path(SETTINGS.traefik_directory).expanduser()
    return default_traefik_directory()


def project_service():
    return ProjectService(SETTINGS, WORKSPACE, traefik_dir=local_traefik_directory())


def traefik_compose_probe():
    directory = local_traefik_directory()
    if not directory or not directory.exists():
        return False, False, False
    has_compose = any((directory / name).exists() for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"))
    return has_compose, True, has_compose


TRAEFIK_DETECTION_MAX_AGE_SECONDS = 10
TRAEFIK_DETECTION_LOCK = threading.Lock()
TRAEFIK_DETECTION = {"key": None, "checked_at": 0.0, "value": None}


def invalidate_traefik_detection():
    with TRAEFIK_DETECTION_LOCK:
        TRAEFIK_DETECTION["checked_at"] = 0.0


def detected_traefik(docker_running=True):
    """Instance Traefik qui sert les projets et son port HTTP, mémorisés quelques secondes.

    L'aperçu est rafraîchi en continu : sans cache, chaque URL de projet relancerait `docker ps`.
    Docker arrêté, seul le compose de Traefik indique le port qui sera publié.
    """
    service = project_service()
    if not docker_running:
        return {"instance": None, "managed": False, "http_port": service.traefik_http_port_from_config()}
    key = (tuple(docker_command(SETTINGS)), str(service.traefik_dir or ""))
    with TRAEFIK_DETECTION_LOCK:
        fresh = time.monotonic() - TRAEFIK_DETECTION["checked_at"] < TRAEFIK_DETECTION_MAX_AGE_SECONDS
        if TRAEFIK_DETECTION["key"] == key and fresh:
            return TRAEFIK_DETECTION["value"]
    instance = service.traefik_instance(refresh=True)
    value = {
        "instance": instance,
        "managed": bool(instance and service.is_managed_traefik(instance)),
        "http_port": service.traefik_http_port(),
    }
    with TRAEFIK_DETECTION_LOCK:
        TRAEFIK_DETECTION.update(key=key, checked_at=time.monotonic(), value=value)
    return value


def running_in_wsl():
    if os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop"):
        return True
    return hasattr(os, "uname") and "microsoft" in os.uname().release.lower()


def local_port_listening(port, tables=("/proc/net/tcp", "/proc/net/tcp6")):
    """Vrai si un processus écoute déjà sur ce port TCP, lu sans droits dans /proc.

    Sous WSL, toutes les distributions partagent le réseau de la VM : le Traefik de Docker
    Desktop y occupe le port 80 et empêche celui de l'environnement Linux de démarrer.
    """
    wanted = f":{port:04X}"
    for table in tables:
        try:
            with open(table, encoding="ascii") as handle:
                next(handle, None)
                for line in handle:
                    fields = line.split()
                    # Colonne 4 : état, 0A = LISTEN.
                    if len(fields) > 3 and fields[1].endswith(wanted) and fields[3] == "0A":
                        return True
        except OSError:
            continue
    return False


def traefik_status(docker=None):
    docker = docker or docker_status(SETTINGS)
    has_compose, exists, valid = traefik_compose_probe()
    detection = detected_traefik(docker["running"])
    instance = detection["instance"]
    http_port = detection["http_port"]
    problems = instance.compatibility_problems() if instance and instance.running and not detection["managed"] else []
    running = bool(instance and instance.running and not problems)
    external = running and not detection["managed"]
    port_hint = "" if http_port == 80 else f" (port HTTP {http_port})"

    if running and external:
        state = "running"
        message = f"Instance Traefik existante utilisée : conteneur {instance.name}{port_hint or ' (port HTTP 80)'}."
    elif running:
        state = "running"
        message = f"Traefik est opérationnel{port_hint}."
    elif problems:
        state = "conflict"
        message = f"Une instance Traefik existante ({instance.describe()}) ne peut pas servir les projets : {'; '.join(problems)}."
    elif platform_id() == "linux" and local_port_listening(http_port):
        state = "port_busy"
        message = (
            f"Le port {http_port} est déjà occupé, probablement par le Traefik de Docker Desktop : "
            "arrête-le pour que les projets de l'environnement Linux soient accessibles."
            if running_in_wsl()
            else f"Le port {http_port} est déjà occupé par un autre service : libère-le pour démarrer Traefik."
        )
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
        "http_port": http_port,
        "container": instance.name if instance else "",
        "external": external,
        "repo": TRAEFIK_REPO,
        "requires_docker": not docker["running"],
        "can_install": docker["running"] and not valid,
        "can_start": docker["running"] and valid and not running,
    }


def abandoned_staging_snapshot():
    """Dossiers de créations interrompues, proposés au nettoyage par l'interface."""
    entries = abandoned_staging_entries(WORKSPACE)
    return {
        "count": len(entries),
        "names": [entry["name"] for entry in entries[:20]],
        "oldest_modified_at": min((entry["modified_at"] for entry in entries), default=0),
    }


def system_status_snapshot(docker=None):
    docker = docker or docker_status(SETTINGS)
    return {
        "docker": docker,
        "traefik": traefik_status(docker),
        "workspace": str(WORKSPACE),
        "workspace_exists": safe_path_is_dir(WORKSPACE),
        "abandoned_staging": abandoned_staging_snapshot(),
    }


def preferred_git_runtime():
    if platform_id() != "windows":
        available = executable_available("git", SETTINGS)
        return {
            "kind": "native",
            "label": "Système",
            "distribution": "",
            "available": available,
            "command": [resolve_executable("git", SETTINGS)],
            "native_available": available,
            "wsl_available": False,
        }

    context = active_workspace_wsl_context()
    distribution = context.distribution if context else SETTINGS.wsl_distribution
    native_available = host_executable_available("git")
    # Git pour Windows suffit hors workspace WSL : aucune sonde WSL dans ce cas.
    needs_wsl_probe = bool(context) or not native_available
    wsl_distribution = find_wsl_executable_distribution("git", distribution) if needs_wsl_probe else None
    wsl_available = wsl_distribution is not None
    use_wsl = (bool(context) and wsl_available) or (not native_available and wsl_available)
    if use_wsl:
        return {
            "kind": "wsl",
            "label": f"WSL ({wsl_distribution})" if wsl_distribution else "WSL",
            "distribution": wsl_distribution or "",
            "available": True,
            "command": [*wsl_command_prefix(wsl_distribution or ""), "git"],
            "native_available": native_available,
            "wsl_available": wsl_available,
        }
    return {
        "kind": "native",
        "label": "Windows",
        "distribution": "",
        "available": native_available,
        "command": [resolve_host_executable("git")],
        "native_available": native_available,
        "wsl_available": wsl_available,
    }


def ssh_runtime():
    git_runtime = preferred_git_runtime()
    if git_runtime["available"]:
        return git_runtime
    context = active_workspace_wsl_context()
    if context and host_executable_available("wsl.exe"):
        return {
            **git_runtime,
            "kind": "wsl",
            "label": f"WSL ({context.distribution})",
            "distribution": context.distribution,
        }
    return git_runtime


def project_creation_prerequisites():
    git_runtime = preferred_git_runtime()
    git_probe_cwd = Path.home() if git_runtime["kind"] == "wsl" else (WORKSPACE if WORKSPACE.exists() else ROOT)
    git_code, git_output = run_capture(
        [*git_runtime["command"], "--version"],
        cwd=git_probe_cwd,
        timeout=8,
    )
    selected_ssh_runtime = ssh_runtime()
    if selected_ssh_runtime["kind"] == "wsl":
        prefix = wsl_command_prefix(selected_ssh_runtime["distribution"])
        key_code, key_output = run_capture(
            [
                *prefix,
                "sh",
                "-lc",
                'find "$HOME/.ssh" -maxdepth 1 -type f -name "*.pub" -print 2>/dev/null',
            ],
            cwd=git_probe_cwd,
            timeout=8,
        )
        ssh_keys = [Path(line.strip()).name for line in key_output.splitlines() if line.strip()] if key_code == 0 else []
    else:
        ssh_dir = Path.home() / ".ssh"
        ssh_keys = sorted(path.name for path in ssh_dir.glob("*.pub") if path.is_file()) if ssh_dir.exists() else []

    if selected_ssh_runtime["kind"] == "wsl":
        prefix = wsl_command_prefix(selected_ssh_runtime["distribution"])
        keygen_code, _keygen_output = run_capture(
            [*prefix, "sh", "-lc", "command -v ssh-keygen >/dev/null 2>&1"],
            cwd=git_probe_cwd,
            timeout=6,
        )
        ssh_keygen_available = keygen_code == 0
    else:
        ssh_keygen_available = host_executable_available("ssh-keygen") if platform_id() == "windows" else executable_available("ssh-keygen", SETTINGS)

    git_install_supported = platform_id() == "windows" and (
        host_executable_available("wsl.exe") or host_executable_available("winget")
    )

    wsl_context = active_workspace_wsl_context()
    if wsl_context:
        workspace_prefix = wsl_command_prefix(wsl_context.distribution)
        workspace_linux = workspace_execution_path(WORKSPACE, SETTINGS, WORKSPACE)
        workspace_code, _workspace_output = run_capture(
            [*workspace_prefix, "test", "-d", workspace_linux],
            cwd=Path.home(),
            timeout=6,
        )
        writable_code, _writable_output = run_capture(
            [*workspace_prefix, "test", "-w", workspace_linux],
            cwd=Path.home(),
            timeout=6,
        )
        workspace_exists = workspace_code == 0
        workspace_ready = workspace_exists and writable_code == 0
    else:
        workspace_exists = WORKSPACE.exists() and WORKSPACE.is_dir()
        writable_parent = WORKSPACE.parent
        while not writable_parent.exists() and writable_parent != writable_parent.parent:
            writable_parent = writable_parent.parent
        workspace_ready = (
            os.access(WORKSPACE, os.W_OK)
            if workspace_exists
            else writable_parent.is_dir() and os.access(writable_parent, os.W_OK)
        )

    return {
        "workspace": str(WORKSPACE),
        "workspace_exists": workspace_exists,
        "workspace_ready": workspace_ready,
        "git_available": git_code == 0,
        "git_version": git_output.splitlines()[0] if git_code == 0 and git_output else "",
        "git_native_available": git_runtime["native_available"],
        "git_wsl_available": git_runtime["wsl_available"],
        "git_install_supported": git_install_supported,
        "git_install_message": (
            f"Git peut être installé automatiquement dans WSL ({wsl_context.distribution})."
            if wsl_context and git_install_supported
            else "Git peut être installé automatiquement avec Windows Package Manager."
            if git_install_supported
            else "Installe Git dans l'environnement du dossier de projets."
        ),
        "ssh_key_present": bool(ssh_keys),
        "ssh_keys": ssh_keys,
        "ssh_keygen_available": ssh_keygen_available,
        "tool_environment": git_runtime["label"],
        "gitlab_ssh_keys_url": "https://gitlab.sudokeys.com/-/user_settings/ssh_keys",
        "supported_versions": list(SUPPORTED_ODOO_VERSIONS),
    }


def ssh_public_keys_snapshot():
    runtime = ssh_runtime()
    if runtime["kind"] == "wsl":
        script = (
            'for key in "$HOME"/.ssh/*.pub; do '
            '[ -f "$key" ] || continue; '
            'printf "%s\\t" "${key##*/}"; tr -d "\\r\\n" < "$key"; printf "\\n"; '
            "done"
        )
        code, output = run_capture(
            [*wsl_command_prefix(runtime["distribution"]), "sh", "-lc", script],
            cwd=Path.home(),
            timeout=8,
        )
        if code != 0:
            raise RuntimeError("Impossible de lire les clés SSH dans WSL.")
        keys = []
        for line in output.splitlines():
            name, separator, public_key = line.partition("\t")
            if separator and public_key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-")):
                keys.append({"name": name, "public_key": public_key})
        return {"keys": keys}

    ssh_dir = Path.home() / ".ssh"
    keys = []
    if ssh_dir.is_dir():
        for path in sorted(ssh_dir.glob("*.pub")):
            try:
                public_key = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if public_key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-")):
                keys.append({"name": path.name, "public_key": public_key})
    return {"keys": keys}


def validate_ssh_comment(value):
    comment = str(value or "").strip()
    if len(comment) > 254 or any(ord(character) < 32 for character in comment):
        raise ValueError("Le commentaire de la clé SSH est invalide.")
    return comment


SSH_KEY_BACKUP_DIRNAME = "odoo-manager-backups"


def backup_native_ssh_key(ssh_dir):
    backup_dir = ssh_dir / SSH_KEY_BACKUP_DIRNAME / time.strftime("%Y%m%d_%H%M%S")
    suffix = 1
    while backup_dir.exists():
        backup_dir = backup_dir.with_name(f"{time.strftime('%Y%m%d_%H%M%S')}_{suffix}")
        suffix += 1
    backup_dir.mkdir(mode=0o700, parents=True)
    for name in ("id_ed25519", "id_ed25519.pub"):
        source = ssh_dir / name
        if source.exists() or source.is_symlink():
            shutil.move(str(source), str(backup_dir / name))
    return backup_dir


def generate_ssh_key(comment="", replace=False):
    comment = validate_ssh_comment(comment)
    existing = ssh_public_keys_snapshot()["keys"]
    default_existing = next((key for key in existing if key["name"] == "id_ed25519.pub"), None)
    if default_existing and not replace:
        return {**default_existing, "created": False, "message": "La clé Ed25519 existe déjà."}

    backup_location = ""
    runtime = ssh_runtime()
    if runtime["kind"] == "wsl":
        comment_argument = f" -C {shlex.quote(comment)}" if comment else ""
        if replace:
            # L'ancienne paire est déplacée, jamais supprimée : elle peut encore servir ailleurs que sur GitLab.
            existing_key_step = (
                'if [ -e "$HOME/.ssh/id_ed25519" ] || [ -e "$HOME/.ssh/id_ed25519.pub" ]; then '
                f'backup="$HOME/.ssh/{SSH_KEY_BACKUP_DIRNAME}/$(date +%Y%m%d_%H%M%S)_$$" && '
                'mkdir -p "$backup" && chmod 700 "$backup" && '
                'for name in id_ed25519 id_ed25519.pub; do '
                '[ -e "$HOME/.ssh/$name" ] && mv "$HOME/.ssh/$name" "$backup/$name"; done; '
                'echo "BACKUP:$backup"; fi; '
            )
        else:
            existing_key_step = (
                'if [ -e "$HOME/.ssh/id_ed25519" ]; then '
                'echo "Une clé privée id_ed25519 existe déjà sans clé publique." >&2; exit 3; fi; '
            )
        script = (
            'mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh" && '
            + existing_key_step
            + f'ssh-keygen -t ed25519 -f "$HOME/.ssh/id_ed25519" -N ""{comment_argument}'
        )
        code, output = run_capture(
            [*wsl_command_prefix(runtime["distribution"]), "sh", "-lc", script],
            cwd=Path.home(),
            timeout=30,
        )
        backup_line = next((line for line in output.splitlines() if line.startswith("BACKUP:")), "")
        backup_location = backup_line.removeprefix("BACKUP:").strip()
        if code != 0:
            raise RuntimeError(output or "Impossible de générer la clé SSH dans WSL.")
    else:
        if not executable_available("ssh-keygen", SETTINGS):
            raise RuntimeError("ssh-keygen est introuvable. Installe Git avant de générer la clé SSH.")
        ssh_dir = Path.home() / ".ssh"
        ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_key = ssh_dir / "id_ed25519"
        if replace and (private_key.exists() or private_key.with_suffix(".pub").exists()):
            backup_location = str(backup_native_ssh_key(ssh_dir))
        elif private_key.exists():
            raise RuntimeError("Une clé privée id_ed25519 existe déjà sans clé publique. Aucun fichier n'a été écrasé.")
        command = [resolve_executable("ssh-keygen", SETTINGS), "-t", "ed25519", "-f", str(private_key), "-N", ""]
        if comment:
            command.extend(["-C", comment])
        code, output = run_capture(command, cwd=Path.home(), timeout=30)
        if code != 0:
            raise RuntimeError(output or "Impossible de générer la clé SSH.")

    generated = ssh_public_keys_snapshot()["keys"]
    public_key = next((key for key in generated if key["name"] == "id_ed25519.pub"), None)
    if not public_key:
        raise RuntimeError("La clé a été générée mais sa partie publique reste introuvable.")
    if replace and backup_location:
        return {
            **public_key,
            "created": True,
            "backup": backup_location,
            "message": f"Nouvelle clé SSH Ed25519 générée. L'ancienne clé est conservée dans {backup_location}.",
        }
    return {**public_key, "created": True, "message": "Clé SSH Ed25519 générée."}


def install_git_job(job):
    try:
        install_git(job)
    finally:
        # Git vient peut-être d'apparaître : la détection mise en cache ne doit pas le masquer.
        reset_wsl_executable_cache()


def install_git(job):
    if platform_id() != "windows":
        raise RuntimeError("L'installation automatique de Git est disponible sous Windows.")

    wsl_context = active_workspace_wsl_context()
    if wsl_context:
        current_code, current_output = run_capture(
            [*workspace_tool_prefix(), "git", "--version"],
            cwd=workspace_tool_cwd(),
            timeout=8,
        )
        if current_code == 0:
            job.add(current_output or f"Git est déjà installé dans WSL ({wsl_context.distribution}).")
            return
        script = (
            "set -eu; "
            "if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get install -y git openssh-client; "
            "elif command -v dnf >/dev/null 2>&1; then dnf install -y git openssh-clients; "
            "elif command -v yum >/dev/null 2>&1; then yum install -y git openssh-clients; "
            "elif command -v apk >/dev/null 2>&1; then apk add git openssh-client; "
            "elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install git openssh; "
            "elif command -v pacman >/dev/null 2>&1; then pacman -Sy --noconfirm git openssh; "
            "else echo 'Gestionnaire de paquets WSL non pris en charge.' >&2; exit 2; fi"
        )
        job.add(f"Installation de Git dans WSL ({wsl_context.distribution})...")
        code = run_stream(
            job,
            [*wsl_command_prefix(wsl_context.distribution, user="root"), "sh", "-lc", script],
            cwd=workspace_tool_cwd(),
        )
        git_code, git_output = run_capture(
            [*workspace_tool_prefix(), "git", "--version"],
            cwd=workspace_tool_cwd(),
            timeout=12,
        )
        if code != 0 or git_code != 0:
            raise RuntimeError("Git n'a pas été détecté dans WSL après l'installation.")
        job.add(git_output or "Git installé dans WSL.")
        return

    if not executable_available("winget", SETTINGS):
        raise RuntimeError("Windows Package Manager (winget) est introuvable. Mets Windows à jour ou installe App Installer.")

    current_code, current_output = run_capture([resolve_executable("git", SETTINGS), "--version"], timeout=8)
    if current_code == 0:
        job.add(current_output or "Git est déjà installé.")
        return

    winget = resolve_executable("winget", SETTINGS)
    command = [
        winget,
        "install",
        "--id",
        "Git.Git",
        "--exact",
        "--source",
        "winget",
        "--silent",
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--disable-interactivity",
    ]
    job.add("Installation silencieuse de Git pour Windows avec winget...")
    code = run_stream(job, command, cwd=WORKSPACE.parent if WORKSPACE.parent.is_dir() else Path.home())
    git_code, git_output = run_capture([resolve_executable("git", SETTINGS), "--version"], timeout=12)
    if code != 0 or git_code != 0:
        raise RuntimeError("Git n'a pas été détecté après l'installation. Consulte les détails du job puis réessaie.")
    job.add(git_output or "Git installé.")


def container_status(name):
    detected = container_states_via_api()
    if detected is not None:
        return detected.get(name, "absent")
    code, output = run_capture(docker_command(SETTINGS, "inspect", "-f", "{{.State.Status}}", name), timeout=5)
    if code != 0 or not output:
        return "absent"
    return output.splitlines()[0].strip()


def container_states_via_api():
    """États des conteneurs par l'API du moteur, ou None pour repasser par la CLI."""
    client = active_engine_client(SETTINGS)
    if client is None:
        return None
    try:
        return client.container_states()
    except EngineUnavailable:
        return None


def container_statuses(names):
    names = tuple(names)
    if not names:
        return {}
    # La boucle d'événements relit ces états toutes les 2 s : 8 ms par l'API contre 150 ms par la CLI.
    detected = container_states_via_api()
    if detected is not None:
        return {name: detected.get(name, "absent") for name in names}
    code, output = run_capture(
        docker_command(SETTINGS, "ps", "-a", "--format", "{{.Names}}|{{.State}}"),
        timeout=8,
    )
    if code != 0:
        return {name: container_status(name) for name in names}

    detected = {}
    for line in output.splitlines():
        name, separator, state = line.partition("|")
        if separator and name in names:
            detected[name] = state.strip() or "absent"
    return {name: detected.get(name, "absent") for name in names}


def project_dirs():
    projects = []
    try:
        workspace_available = WORKSPACE.is_dir()
    except OSError:
        return projects
    if not workspace_available:
        return projects
    try:
        items = tuple(WORKSPACE.iterdir())
    except OSError:
        return projects
    for item in items:
        try:
            is_directory = item.is_dir()
        except OSError:
            continue
        if not is_directory:
            continue
        try:
            has_compose = any((item / name).exists() for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"))
        except OSError:
            continue
        if has_compose:
            projects.append(item.name)
    return sorted(projects)


def validate_project(project):
    if not project or not SAFE_PROJECT_RE.match(project):
        raise ValueError("Nom de projet invalide.")
    if project not in project_dirs():
        raise ValueError("Projet introuvable.")
    return project


def validate_db(db_name):
    if not isinstance(db_name, str) or not db_name:
        raise ValueError("Nom de base invalide.")
    if any(ord(character) < 32 or ord(character) == 127 for character in db_name):
        raise ValueError("Nom de base contenant un caractère de contrôle invalide.")
    if len(db_name.encode("utf-8")) > MAX_DB_NAME_BYTES:
        raise ValueError(f"Nom de base trop long ({MAX_DB_NAME_BYTES} octets maximum).")
    return db_name


def validate_new_db(db_name):
    db_name = validate_db(db_name)
    if db_name != db_name.strip() or db_name in {".", ".."} or "/" in db_name or "\\" in db_name:
        raise ValueError("Nom de base incompatible avec le stockage local.")
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


def validate_socle_presets(value):
    requested = list(dict.fromkeys(item.strip() for item in str(value or "").split(",") if item.strip()))
    if not requested:
        raise ValueError("Sélectionne au moins une application du socle.")
    unknown = sorted(set(requested) - set(SOCLE_PRESETS))
    if unknown:
        raise ValueError("Applications de socle inconnues : " + ", ".join(unknown))
    return requested


def module_name_list(modules):
    value = validate_modules(modules)
    names = sorted({name.strip() for name in value.split(",") if name.strip()})
    if not names:
        raise ValueError("Aucun module fourni.")
    return names


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
        if safe_path_exists(candidate):
            return candidate
    return None


def project_url(project, traefik_port=None):
    if traefik_port is None:
        traefik_port = detected_traefik()["http_port"]
    file = compose_file(project)
    if file:
        try:
            content = file.read_text(encoding="utf-8", errors="ignore")
            match = re.search(r"Host\(`([^`]+)`\)", content)
            if match:
                return url_with_port(f"http://{match.group(1)}/", traefik_port)
        except OSError:
            pass

    code, output = run_capture(docker_command(SETTINGS, "port", f"odoo-{project}", "8069/tcp"), timeout=1)
    if code == 0 and output:
        first = output.splitlines()[0].strip()
        port = first.rsplit(":", 1)[-1]
        if port.isdigit():
            return f"http://localhost:{port}/"
    return url_with_port(f"http://dev.{project}.localhost/", traefik_port)


def project_odoo_version(project):
    release_file = WORKSPACE / project / "odoo" / "odoo" / "odoo" / "release.py"
    if safe_path_exists(release_file):
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


def list_databases_for(project, check_container=True):
    if check_container and container_status(f"postgresql-{project}") != "running":
        return []
    query = "select datname from pg_database where datistemplate = false order by datname;"
    code, output = run_capture(
        docker_command(SETTINGS, "exec", f"postgresql-{project}", "psql", "-U", "postgres", "-Atc", query),
        timeout=12,
    )
    if code != 0:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def open_postgresql_console(project, db_name):
    project = validate_project(project)
    db_name = validate_odoo_db(db_name)
    container = f"postgresql-{project}"
    if container_status(container) != "running":
        raise RuntimeError("Le conteneur PostgreSQL du projet n'est pas démarré.")
    if db_name not in list_databases_for(project, check_container=False):
        raise ValueError("La base Odoo sélectionnée n'existe plus dans PostgreSQL.")

    command = docker_command(
        SETTINGS,
        "exec",
        "-it",
        container,
        "psql",
        "-U",
        "postgres",
        "-d",
        db_name,
    )
    result = open_terminal_command(
        SETTINGS,
        command,
        cwd=WORKSPACE,
        label=f"la console PostgreSQL de {db_name}",
    )
    if not result.ok:
        raise RuntimeError(result.message)
    return {"ok": True, "message": result.message, "database": db_name}


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


WSL_MODULE_METADATA = {}


def linux_path_is_relative_to(path, parent):
    path = posixpath.normpath(path)
    parent = posixpath.normpath(parent)
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def wsl_module_roots(project, distribution):
    # Traduits une fois par liste : les retraduire pour chacun des ~1 300 modules
    # multipliait les résolutions de chemins Windows.
    translate = lambda path: wsl_execution_path(path, distribution)
    return (
        translate(project_addons_link_parent(project)),
        translate(project_addons_storage_parent(project)),
        translate(project_legacy_addons_storage_parent(project)),
        [translate(root) for root in module_import_roots(project)],
    )


WSL_SHELL_AVAILABILITY = {}
WSL_SHELL_AVAILABILITY_TTL_SECONDS = 60


def wsl_shell_available(distribution):
    """`wsl.exe` coûte de 0,3 à plusieurs secondes : on ne le relance pas à chaque liste."""
    now = time.monotonic()
    cached = WSL_SHELL_AVAILABILITY.get(distribution)
    if cached and now - cached[0] < WSL_SHELL_AVAILABILITY_TTL_SECONDS:
        return cached[1]
    available = wsl_executable_available("sh", distribution)
    # Seul le succès est mémorisé : sans distribution, l'échec de wsl.exe est immédiat, et
    # un WSL installé entre-temps doit être pris en compte sans attendre l'expiration.
    if available:
        WSL_SHELL_AVAILABILITY[distribution] = (now, available)
    else:
        WSL_SHELL_AVAILABILITY.pop(distribution, None)
    return available


def wsl_module_metadata(
    project,
    linux_path,
    source_path,
    is_link,
    distribution,
    host_path="",
    host_source_path="",
    roots=None,
):
    link_parent, storage_parent, legacy_parent, imports_roots = roots or wsl_module_roots(project, distribution)
    parent = posixpath.dirname(linux_path)
    source_parent = posixpath.dirname(source_path)
    name = posixpath.basename(linux_path)
    link_path = linux_path if parent == link_parent else ""
    if not link_path:
        candidate_link = posixpath.join(link_parent, name)
        if candidate_link == linux_path:
            link_path = candidate_link

    direct_storage = source_parent == storage_parent
    direct_legacy = source_parent == legacy_parent
    imported = any(linux_path_is_relative_to(source_path, root) for root in imports_roots)
    in_storage = linux_path_is_relative_to(source_path, storage_parent)

    if parent == link_parent and is_link:
        if direct_storage:
            kind = "lien vers addons-store"
            removal_mode = "link_and_storage"
            removal_note = "Supprime le lien odoo/addons et le dossier dans odoo/addons-store."
            removable = True
        elif direct_legacy:
            kind = "lien vers ancien stockage"
            removal_mode = "link_and_legacy_storage"
            removal_note = "Supprime le lien odoo/addons et le dossier dans l'ancien odoo/odoo/addons."
            removable = True
        elif imported:
            kind = "lien vers import outil"
            removal_mode = "link_and_import"
            removal_note = "Supprime le lien odoo/addons et le dossier extrait géré par l'outil."
            removable = True
        elif in_storage:
            kind = "lien vers dépôt addons-store"
            removal_mode = "protected_store"
            removal_note = "Module fourni par un dépôt sous addons-store; suppression du lien seule le ferait réapparaître."
            removable = False
        else:
            kind = "lien vers source externe"
            removal_mode = "link_only"
            removal_note = "Supprime le lien dans odoo/addons. La source externe est conservée."
            removable = True
    elif parent == link_parent:
        kind = "dossier direct dans odoo/addons"
        removal_mode = "directory"
        removal_note = "Déplace le dossier du module hors de odoo/addons."
        removable = True
    elif parent == storage_parent:
        kind = "addons-store"
        removal_mode = "protected_source"
        removal_note = "Module dans odoo/addons-store sans lien géré dans odoo/addons."
        removable = False
    elif parent == legacy_parent:
        kind = "ancien stockage"
        removal_mode = "protected_legacy_source"
        removal_note = "Module dans l'ancien dossier odoo/odoo/addons sans lien géré dans odoo/addons."
        removable = False
    elif in_storage:
        kind = "addons-store"
        removal_mode = "protected"
        removal_note = "Module hors du dossier odoo/addons du projet."
        removable = False
    else:
        kind = "source externe"
        removal_mode = "protected"
        removal_note = "Module hors du dossier odoo/addons du projet."
        removable = False

    host_path = host_path or wsl_unc_path(distribution, linux_path)
    host_source_path = host_source_path or wsl_unc_path(distribution, source_path)
    return host_path, {
        "path": host_path,
        "link_path": host_path if link_path else "",
        "source_path": host_source_path,
        "path_kind": kind,
        "removable": removable,
        "removal_mode": removal_mode,
        "removal_note": removal_note,
    }


def module_parent_candidates(project):
    """Dossiers scannés pour les addons, dans l'ordre de priorité du premier nom trouvé."""
    base = project_odoo_root(project)
    return [
        project_addons_link_parent(project),
        project_addons_storage_parent(project),
        project_legacy_addons_storage_parent(project),
        base / "odoo" / "odoo" / "addons",
        base / "addons-store" / "odoo_entreprise",
        base / "addons-store" / "odoo_enterprise",
    ]


def active_wsl_distribution():
    context = active_workspace_wsl_context()
    return context.distribution if context else SETTINGS.wsl_distribution


def wsl_module_dirs(project):
    distribution = active_wsl_distribution()
    linux_candidates = [wsl_execution_path(path, distribution) for path in module_parent_candidates(project)]
    script = (
        'found_parent=0; for parent do [ -d "$parent" ] && found_parent=1; done; '
        '[ "$found_parent" -eq 1 ] || { echo "Aucun dossier addons lisible depuis WSL." >&2; exit 3; }; '
        'for parent do [ -d "$parent" ] || continue; '
        'find "$parent" -mindepth 1 -maxdepth 1 \\( -type d -o -type l \\) -print 2>/dev/null | '
        'while IFS= read -r child; do '
        '[ -f "$child/__manifest__.py" ] || [ -f "$child/__openerp__.py" ] || continue; '
        # Aucun sous-processus pour un dossier ordinaire : readlink seulement pour les liens,
        # les chemins Windows sont calculés côté Python (wsl_windows_path).
        'target="$child"; linked=0; '
        'if [ -L "$child" ]; then linked=1; target=$(readlink -f -- "$child" 2>/dev/null || printf "%s" "$child"); fi; '
        'printf "%s\\t%s\\t%s\\n" "$child" "$target" "$linked"; '
        'done; done'
    )
    code, output = run_capture(
        [*wsl_command_prefix(distribution), "sh", "-c", script, "odoo-manager", *linux_candidates],
        cwd=workspace_tool_cwd(),
        timeout=30,
    )
    if code != 0:
        detail = output.strip() or "La commande de détection des addons a échoué dans WSL."
        raise RuntimeError(f"Impossible de lire les modules du projet depuis WSL : {detail}")

    seen = set()
    paths = []
    roots = wsl_module_roots(project, distribution)
    for line in output.splitlines():
        parts = line.split("\t", 4)
        if len(parts) < 3:
            continue
        linux_path, source_path, linked = parts[:3]
        windows_path = wsl_windows_path(posixpath.normpath(linux_path), distribution)
        windows_source_path = wsl_windows_path(posixpath.normpath(source_path or linux_path), distribution)
        name = posixpath.basename(linux_path)
        if not SAFE_MODULE_RE.fullmatch(name) or name in seen:
            continue
        seen.add(name)
        host_path, metadata = wsl_module_metadata(
            project,
            posixpath.normpath(linux_path),
            posixpath.normpath(source_path or linux_path),
            linked == "1",
            distribution,
            windows_path,
            windows_source_path,
            roots=roots,
        )
        WSL_MODULE_METADATA[host_path.casefold()] = metadata
        paths.append(Path(host_path))
    return paths


def project_has_wsl_links(project):
    """Liens WSL hérités d'une ancienne version : Windows ne peut pas les lire."""
    if platform_id() != "windows":
        return False
    return any(contains_wsl_symlink(parent) for parent in module_parent_candidates(project))


def project_reads_modules_through_wsl(project):
    # Un scan natif prend 0,2 s contre 30 à 70 s via WSL : WSL seulement quand Windows ne peut pas lire les liens.
    return project_has_wsl_links(project) and wsl_shell_available(SETTINGS.wsl_distribution)


def module_dirs(project):
    if active_workspace_wsl_context():
        yield from wsl_module_dirs(project)
        return
    if project_reads_modules_through_wsl(project):
        try:
            yield from wsl_module_dirs(project)
            return
        except RuntimeError:
            # A native scan remains useful when WSL is temporarily unavailable.
            # Individual inaccessible WSL links are ignored below instead of
            # turning the whole modules endpoint into an HTTP 500 response.
            pass
    candidates = module_parent_candidates(project)
    seen = set()
    readable_parent = False
    access_errors = []
    for parent in candidates:
        try:
            exists = parent.exists()
        except OSError as exc:
            access_errors.append(f"{parent}: {exc}")
            continue
        if not exists:
            continue
        try:
            children = sorted(parent.iterdir(), key=lambda p: p.name.lower())
            readable_parent = True
        except OSError as exc:
            access_errors.append(f"{parent}: {exc}")
            continue
        for child in children:
            try:
                is_directory = child.is_dir()
                is_link = child.is_symlink()
            except OSError as exc:
                access_errors.append(f"{child}: {exc}")
                continue
            if not is_directory and not is_link:
                continue
            manifest = child / "__manifest__.py"
            openerp = child / "__openerp__.py"
            try:
                has_manifest = manifest.exists() or openerp.exists()
            except OSError as exc:
                access_errors.append(f"{child}: {exc}")
                continue
            if not has_manifest:
                continue
            key = child.name
            if key in seen:
                continue
            seen.add(key)
            yield child
    if not readable_parent and access_errors:
        raise RuntimeError(
            "Impossible de lire les dossiers addons du projet sous Windows : "
            + " ; ".join(access_errors[:3])
        )


MODULE_CACHE = {}
MODULE_CACHE_LOCK = threading.Lock()

# Odoo stores Studio customizations under this virtual module name. It has no
# addon directory and must not be reported as missing source code.
DATABASE_ONLY_MODULES = frozenset({"studio_customization"})
TRANSIENT_MODULE_STATES = frozenset({"to install", "to upgrade", "to remove"})
ACTIVE_MODULE_STATES = frozenset({"installed", *TRANSIENT_MODULE_STATES})


def load_local_module_overrides():
    try:
        payload = json.loads(LOCAL_MODULE_OVERRIDES.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    workspaces = payload.get("workspaces")
    if not isinstance(workspaces, dict):
        workspaces = {}
    return {"version": 1, "workspaces": workspaces}


def ignored_missing_modules(project, db_name):
    with LOCAL_MODULE_OVERRIDES_LOCK:
        payload = load_local_module_overrides()
    workspace = payload["workspaces"].get(str(WORKSPACE), {})
    project_config = workspace.get(project, {}) if isinstance(workspace, dict) else {}
    modules = project_config.get(db_name, []) if isinstance(project_config, dict) else []
    return {name for name in modules if isinstance(name, str) and SAFE_MODULE_RE.fullmatch(name)}


def remember_ignored_missing_modules(project, db_name, modules):
    with LOCAL_MODULE_OVERRIDES_LOCK:
        payload = load_local_module_overrides()
        workspaces = payload["workspaces"]
        workspace = workspaces.setdefault(str(WORKSPACE), {})
        project_config = workspace.setdefault(project, {})
        current = {
            name
            for name in project_config.get(db_name, [])
            if isinstance(name, str) and SAFE_MODULE_RE.fullmatch(name)
        }
        current.update(modules)
        project_config[db_name] = sorted(current)
        LOCAL_MODULE_OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
        temporary = LOCAL_MODULE_OVERRIDES.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(LOCAL_MODULE_OVERRIDES)


def forget_ignored_missing_modules(project, db_name, modules):
    with LOCAL_MODULE_OVERRIDES_LOCK:
        payload = load_local_module_overrides()
        workspaces = payload["workspaces"]
        workspace = workspaces.get(str(WORKSPACE), {})
        project_config = workspace.get(project, {}) if isinstance(workspace, dict) else {}
        current = set(project_config.get(db_name, [])) if isinstance(project_config, dict) else set()
        current.difference_update(modules)
        if isinstance(project_config, dict):
            project_config[db_name] = sorted(current)
        LOCAL_MODULE_OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
        temporary = LOCAL_MODULE_OVERRIDES.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(LOCAL_MODULE_OVERRIDES)


def local_ignore_plan(states, available_names, requested, dependencies, already_excluded=None):
    requested = set(requested)
    already_excluded = set(already_excluded or ())
    candidates = sorted(
        name
        for name in requested
        if name in states
        and states[name].get("state") in TRANSIENT_MODULE_STATES
        and name not in available_names
        and name not in DATABASE_ONLY_MODULES
    )
    invalid = sorted(requested - set(candidates))
    excluded = set(candidates) | already_excluded
    automatic = set()
    changed = True
    while changed:
        changed = False
        for dependent, dependency in dependencies:
            if dependency not in excluded or dependent in excluded:
                continue
            if states.get(dependent, {}).get("state") not in ACTIVE_MODULE_STATES:
                continue
            excluded.add(dependent)
            automatic.add(dependent)
            changed = True
    return candidates, invalid, sorted(automatic)


def modules_missing_from_code(states, available_names, accepted_states):
    return sorted(
        name
        for name, state in states.items()
        if state.get("state") in accepted_states
        and name not in available_names
        and name not in DATABASE_ONLY_MODULES
    )


def clear_project_module_cache(project):
    with MODULE_CACHE_LOCK:
        MODULE_CACHE.pop(project, None)
        MODULE_GRAPH_CACHE.pop(project, None)
        WSL_MODULE_METADATA.clear()
    WSL_SHELL_AVAILABILITY.clear()


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


def safe_resolve(path):
    """Resolve a path without failing on Windows links owned by WSL."""
    path = Path(path)
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def path_scope(parent):
    """Test « `path` est dans `parent` », même règle que path_is_relative_to, par chaînes.

    pathlib parcourt les parents du chemin à chaque test : ~2 000 tests par liste de modules
    coûtaient 20 ms, contre 0,6 ms ici. La règle reste lexicale et la casse suit normcase,
    comme pathlib (ignorée sous Windows, respectée ailleurs).
    """
    parent_text = os.path.normcase(os.fspath(parent))
    prefix = parent_text if parent_text.endswith(os.sep) else parent_text + os.sep

    def contains(path):
        text = os.path.normcase(os.fspath(path))
        return text == parent_text or text.startswith(prefix)

    return contains


@dataclass(frozen=True)
class ModuleLayout:
    """Dossiers résolus d'un projet, calculés une fois par liste de modules.

    Partagé au sein d'une seule liste : jamais réutilisé entre requêtes ni par une opération
    destructive, qui refait ses propres contrôles avec path_is_relative_to.
    """

    link_parent: Path
    storage_parent: Path
    legacy_storage_parent: Path
    imports_roots: tuple
    # Parent d'un module tel que module_dirs le donne (casse normalisée) -> le même, résolu.
    known_parents: dict
    in_storage: object
    in_imports: tuple

    def in_import_root(self, path):
        return any(contains(path) for contains in self.in_imports)


def module_layout_context(project):
    link_parent = safe_resolve(project_addons_link_parent(project))
    storage_parent = safe_resolve(project_addons_storage_parent(project))
    legacy_storage_parent = safe_resolve(project_legacy_addons_storage_parent(project))
    imports_roots = tuple(module_import_roots(project))
    return ModuleLayout(
        link_parent=link_parent,
        storage_parent=storage_parent,
        legacy_storage_parent=legacy_storage_parent,
        imports_roots=imports_roots,
        known_parents={
            os.path.normcase(os.fspath(project_addons_link_parent(project))): link_parent,
            os.path.normcase(os.fspath(project_addons_storage_parent(project))): storage_parent,
            os.path.normcase(os.fspath(project_legacy_addons_storage_parent(project))): legacy_storage_parent,
        },
        in_storage=path_scope(storage_parent),
        in_imports=tuple(path_scope(root) for root in imports_roots),
    )


def module_parent_in_layout(project, path, layout):
    """Parent résolu d'un module sans relancer la résolution des dossiers déjà résolus du contexte.

    Sous Windows, chaque résolution coûte un appel système (GetFinalPathNameByHandle) :
    la refaire pour le parent commun de ~1 400 modules doublait le temps de la liste.
    Un parent absent de la table est simplement résolu : la table n'évite qu'un coût.
    """
    parent = os.path.dirname(os.fspath(path))
    return layout.known_parents.get(os.path.normcase(parent)) or safe_resolve(parent)


def module_location_info(project, path, layout=None):
    metadata = WSL_MODULE_METADATA.get(str(path).casefold())
    if metadata:
        return {
            key: metadata[key]
            for key in ("path", "link_path", "source_path", "path_kind")
        }
    layout = layout or module_layout_context(project)
    link_parent = layout.link_parent
    storage_parent = layout.storage_parent
    legacy_storage_parent = layout.legacy_storage_parent
    parent = module_parent_in_layout(project, path, layout)
    source_path = safe_resolve(path) if path.is_symlink() else path

    link_path = ""
    if parent == link_parent:
        link_path = str(path)
    else:
        candidate_link = project_addons_link_parent(project) / path.name
        try:
            if candidate_link.exists() or candidate_link.is_symlink():
                link_path = str(candidate_link)
        except OSError:
            pass

    if parent == link_parent and path.is_symlink():
        # source_path et les dossiers du contexte sont déjà résolus.
        if source_path.parent == storage_parent:
            kind = "lien vers addons-store"
        elif source_path.parent == legacy_storage_parent:
            kind = "lien vers ancien stockage"
        elif layout.in_import_root(source_path):
            kind = "lien vers import outil"
        elif layout.in_storage(source_path):
            kind = "lien vers dépôt addons-store"
        else:
            kind = "lien vers source externe"
    elif parent == link_parent:
        kind = "dossier direct dans odoo/addons"
    elif parent == storage_parent:
        kind = "addons-store"
    elif parent == legacy_storage_parent:
        kind = "ancien stockage"
    elif layout.in_storage(safe_resolve(path)):
        kind = "addons-store"
    else:
        kind = "source externe"

    return {
        "path": str(path),
        "link_path": link_path,
        "source_path": str(source_path),
        "path_kind": kind,
    }


def module_origin(source_path):
    normalized_path = str(source_path).replace("\\", "/").casefold()
    if (
        "/addons-store/odoo_entreprise/" in normalized_path
        or "/addons-store/odoo_enterprise/" in normalized_path
    ):
        return "enterprise"
    return "other"


def basic_module(project, path, layout=None):
    location = module_location_info(project, path, layout)
    name = posixpath.basename(str(path).replace("\\", "/"))
    return {
        "name": name,
        "title": name,
        "summary": "",
        "version": "",
        "category": "",
        "installable": True,
        "origin": module_origin(location["source_path"]),
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


def module_removal_info(project, path, layout=None):
    metadata = WSL_MODULE_METADATA.get(str(path).casefold())
    if metadata:
        return {
            key: metadata[key]
            for key in ("removable", "removal_mode", "removal_note")
        }
    layout = layout or module_layout_context(project)
    link_parent = layout.link_parent
    storage_parent = layout.storage_parent
    legacy_storage_parent = layout.legacy_storage_parent
    parent = module_parent_in_layout(project, path, layout)

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
        # La cible est résolue à chaque lecture ; les dossiers du contexte le sont déjà.
        target = safe_resolve(path)
        if target.parent == storage_parent:
            return {
                "removable": True,
                "removal_mode": "link_and_storage",
                "removal_note": "Supprime le lien odoo/addons et le dossier dans odoo/addons-store.",
            }
        if target.parent == legacy_storage_parent:
            return {
                "removable": True,
                "removal_mode": "link_and_legacy_storage",
                "removal_note": "Supprime le lien odoo/addons et le dossier dans l'ancien odoo/odoo/addons.",
            }
        if layout.in_import_root(target):
            return {
                "removable": True,
                "removal_mode": "link_and_import",
                "removal_note": "Supprime le lien odoo/addons et le dossier extrait géré par l'outil.",
            }
        if layout.in_storage(target):
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


def installed_modules(project, db_name, check_container=True):
    if not db_name or (check_container and container_status(f"postgresql-{project}") != "running"):
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
    layout = module_layout_context(project)
    now = time.time()
    with MODULE_CACHE_LOCK:
        cached = MODULE_CACHE.get(cache_key)
        if cached and now - cached["created_at"] < 45:
            base_modules = [dict(item) for item in cached["modules"]]
        else:
            base_modules = None

    if base_modules is None:
        base_modules = [basic_module(project, path, layout) for path in module_dirs(project)]
        if base_modules:
            with MODULE_CACHE_LOCK:
                MODULE_CACHE[cache_key] = {"created_at": now, "modules": [dict(item) for item in base_modules]}

    states = installed_modules(project, db_name) if db_name else {}
    modules = []
    for module in base_modules:
        module = dict(module)
        state = states.get(module["name"], {})
        module["state"] = state.get("state", "disponible")
        module["installed_version"] = state.get("installed_version", "")
        module.update(module_removal_info(project, Path(module["path"]), layout))
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
    filestore_root = (WORKSPACE / project / "odoo_data" / "filestore").resolve(strict=False)
    filestore = (filestore_root / db_name).resolve(strict=False)
    files = set()
    if filestore == filestore_root or not path_is_relative_to(filestore, filestore_root):
        return files, filestore
    context = active_workspace_wsl_context()
    if context:
        linux_filestore = workspace_execution_path(filestore, SETTINGS, WORKSPACE)
        code, output = run_capture(
            [
                *wsl_command_prefix(context.distribution),
                "find",
                linux_filestore,
                "-mindepth",
                "2",
                "-maxdepth",
                "2",
                "-type",
                "f",
                "-printf",
                "%P\\n",
            ],
            cwd=WORKSPACE,
            timeout=30,
        )
        if code == 0:
            files.update(line.strip() for line in output.splitlines() if line.strip())
        return files, filestore
    if not filestore.exists():
        return files, filestore
    for path in filestore.glob("*/*"):
        if path.is_file():
            try:
                files.add(str(path.relative_to(filestore)))
            except ValueError:
                pass
    return files, filestore


def filestore_summary(stored, actual):
    referenced = set(stored)
    present = referenced & actual
    missing = referenced - actual
    return {
        "referenced": len(stored),
        "referenced_unique": len(referenced),
        "actual": len(present),
        "physical_total": len(actual),
        "missing": len(missing),
        "module_update_supported": True,
    }, sorted(missing)


def filestore_diagnostic_issue(db_name, filestore, missing):
    displayed = list(missing[:5])
    remaining = len(missing) - len(displayed)
    if remaining > 0:
        displayed.append(f"... {remaining} autre(s) fichier(s) manquant(s) non affiché(s)")
    return {
        "severity": "warning",
        "title": f"Filestore partiel pour {db_name} (non bloquant pour la MAJ des modules)",
        "details": (
            f"{len(missing)} fichier(s) référencé(s) par ir_attachment sont absents de {filestore}. "
            "Il n'est pas nécessaire de télécharger le filestore pour mettre à jour le code des modules. "
            "Utilisez le mode sans filestore : les références seront conservées et seuls les médias absents "
            "resteront indisponibles."
        ),
        "items": displayed,
    }


def database_diagnostics(project, db_name, available_paths):
    """Diagnostic d'une base : ses informations et, dans l'ordre, les problèmes à remonter au projet."""
    db_info = {
        "name": db_name,
        "issues": [],
        "pending_modules": [],
        "pending_missing_modules": [],
        "ignored_missing_modules": [],
        "local_excluded_modules": [],
    }
    issues = []

    def report(issue):
        issues.append(issue)
        db_info["issues"].append(issue)

    # PostgreSQL vient d'être vu démarré : pas de `docker inspect` à chaque base.
    states = installed_modules(project, db_name, check_container=False)
    if not states:
        issues.append(
            {
                "severity": "error",
                "title": f"Impossible de lire les modules de {db_name}",
                "details": "La table ir_module_module est inaccessible ou ne contient aucun module.",
                "items": [],
            }
        )
        return db_info, issues

    pending_missing = modules_missing_from_code(states, available_paths, TRANSIENT_MODULE_STATES)
    db_info["pending_missing_modules"] = pending_missing
    db_info["pending_modules"] = [
        {
            "name": name,
            "state": state.get("state", ""),
            "code_available": name in available_paths or name in DATABASE_ONLY_MODULES,
        }
        for name, state in sorted(states.items())
        if state.get("state") in TRANSIENT_MODULE_STATES and name not in DATABASE_ONLY_MODULES
    ]
    pending_missing_names = set(pending_missing)
    pending_available = sorted(
        f"{name} · {state.get('state', '')}"
        for name, state in states.items()
        if state.get("state") in TRANSIENT_MODULE_STATES
        and name not in pending_missing_names
        and name not in DATABASE_ONLY_MODULES
    )
    if pending_available:
        issue = {
            "severity": "warning",
            "title": f"Opération module en attente dans {db_name}",
            "details": "Le code de ces modules est disponible, mais Odoo doit encore terminer leur opération.",
            "items": pending_available[:40],
        }
        report(issue)

    configured_ignored = ignored_missing_modules(project, db_name)
    installed_missing_all = modules_missing_from_code(states, available_paths, {"installed"})
    ignored_installed_missing = sorted(set(installed_missing_all) & configured_ignored)
    installed_missing = sorted(set(installed_missing_all) - configured_ignored)
    db_info["ignored_missing_modules"] = ignored_installed_missing
    local_excluded = sorted(
        name for name in configured_ignored if states.get(name, {}).get("state") == "installed"
    )
    db_info["local_excluded_modules"] = local_excluded
    if installed_missing:
        issue = {
            "severity": "error",
            "title": f"Modules installés absents du code dans {db_name}",
            "details": "La base les considère installés, mais aucun dossier addon correspondant n'est présent dans les chemins montés.",
            "items": installed_missing[:60],
        }
        report(issue)

    if local_excluded:
        issue = {
            "severity": "warning",
            "title": f"Modules exclus des mises à jour sur la copie locale {db_name}",
            "details": (
                "Leur opération en attente a été annulée localement sans désinstallation ni suppression de données. "
                "Les prochaines mises à jour utiliseront une liste explicite et les laisseront inchangés."
            ),
            "items": local_excluded[:60],
        }
        report(issue)

    if pending_missing:
        issue = {
            "severity": "error",
            "title": f"Modules en attente absents du code dans {db_name}",
            "details": (
                "Odoo ne peut pas terminer leur opération tant que leurs dossiers addon et leurs dépendances "
                "ne sont pas restaurés dans les chemins montés."
            ),
            "items": pending_missing[:60],
        }
        report(issue)

    stored = db_query_lines(
        project,
        db_name,
        "select store_fname from ir_attachment where store_fname is not null and store_fname <> '' order by store_fname;",
        timeout=18,
    )
    actual, filestore = filestore_files(project, db_name)
    filestore_stats, missing = filestore_summary(stored, actual)
    db_info["filestore"] = {"path": str(filestore), **filestore_stats}
    if missing:
        issue = filestore_diagnostic_issue(db_name, filestore, missing)
        report(issue)
    return db_info, issues


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

    statuses = container_statuses((f"odoo-{project}", f"postgresql-{project}"))
    odoo_status = statuses[f"odoo-{project}"]
    pg_status = statuses[f"postgresql-{project}"]
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

    with ThreadPoolExecutor(max_workers=DOCKER_PROBE_WORKERS, thread_name_prefix="project-diagnostics") as executor:
        # Le parcours des addons (disque) avance pendant la lecture des bases (Docker).
        module_paths = executor.submit(lambda: {path.name: path for path in module_dirs(project)})
        databases = [
            db_name for db_name in list_databases_for(project, check_container=False) if db_name != "postgres"
        ]
        available_paths = module_paths.result()
        # Chaque base coûte deux `docker exec psql` et un parcours de son filestore, indépendants d'une
        # base à l'autre : en série, le diagnostic enchaînait 13 `docker exec` pour 3 bases (1,1 s).
        for db_info, issues in executor.map(lambda db_name: database_diagnostics(project, db_name, available_paths), databases):
            diagnostics["databases"].append(db_info)
            diagnostics["issues"].extend(issues)

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


def invalidate_overview_databases(project=None):
    with OVERVIEW_DATABASES_CACHE_LOCK:
        if project is None:
            OVERVIEW_DATABASES_CACHE.clear()
        else:
            OVERVIEW_DATABASES_CACHE.pop(project, None)


def cached_overview_databases(project, max_age):
    """Liste des bases encore valide pour `max_age`, ou None s'il faut interroger PostgreSQL."""
    if max_age is None:
        return None
    with OVERVIEW_DATABASES_CACHE_LOCK:
        cached = OVERVIEW_DATABASES_CACHE.get(project)
    if cached and time.monotonic() - cached[0] < max_age:
        return list(cached[1])
    return None


def probe_overview_databases(project, max_age=None):
    now = time.monotonic()
    databases = list_databases_for(project, check_container=False)
    # Une liste vide peut venir d'un PostgreSQL encore en démarrage : on ne la garde pas.
    if databases and max_age is not None:
        with OVERVIEW_DATABASES_CACHE_LOCK:
            OVERVIEW_DATABASES_CACHE[project] = (now, list(databases))
    return databases


def overview_databases(project, max_age=None):
    cached = cached_overview_databases(project, max_age)
    return cached if cached is not None else probe_overview_databases(project, max_age)


def overview_databases_by_project(projects, max_age=None):
    """Bases de chaque projet démarré ; les sondes que le cache ne sert pas partent en parallèle.

    Chaque sonde est un `docker exec psql` (~50 ms sous Docker Desktop). En série, l'overview
    grandissait avec le nombre de projets démarrés : 233 ms pour 5 contre 92 ms en parallèle.
    """
    results = {}
    pending = []
    for project in projects:
        cached = cached_overview_databases(project, max_age)
        if cached is None:
            pending.append(project)
        else:
            results[project] = cached
    if len(pending) == 1:
        results[pending[0]] = probe_overview_databases(pending[0], max_age)
    elif pending:
        workers = min(DOCKER_PROBE_WORKERS, len(pending))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="overview-databases") as executor:
            results.update(zip(pending, executor.map(lambda project: probe_overview_databases(project, max_age), pending)))
    return results


def docker_poll_seconds():
    try:
        return max(3, int(SETTINGS.docker_poll_interval))
    except (TypeError, ValueError):
        return 10


def overview(docker=None, databases_max_age=None):
    docker = docker or docker_status(SETTINGS)
    docker_ok = docker["running"]
    docker_message = docker["message"]
    project_names = project_dirs()
    container_names = [name for project in project_names for name in (f"odoo-{project}", f"postgresql-{project}")]
    statuses = container_statuses(container_names) if docker_ok else {}
    traefik_port = detected_traefik(docker_ok)["http_port"] if project_names else None
    running_postgres = [project for project in project_names if statuses.get(f"postgresql-{project}") == "running"]
    databases_by_project = overview_databases_by_project(running_postgres, databases_max_age)
    projects = []
    for project in project_names:
        odoo_status = statuses.get(f"odoo-{project}", "absent") if docker_ok else "docker off"
        pg_status = statuses.get(f"postgresql-{project}", "absent") if docker_ok else "docker off"
        if pg_status == "running":
            databases = databases_by_project[project]
        else:
            databases = []
            invalidate_overview_databases(project)
        url = project_url(project, traefik_port=traefik_port)
        projects.append(
            {
                "name": project,
                "odoo_version": project_odoo_version(project),
                "odoo_status": odoo_status,
                "postgres_status": pg_status,
                "url": url,
                "database_manager_url": urllib.parse.urljoin(url, "web/database/manager"),
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


def bootstrap_snapshot():
    """Return one coherent startup snapshot backed by a single Docker probe."""
    docker = docker_status(SETTINGS)
    return {
        "overview": overview(docker),
        "system_status": system_status_snapshot(docker),
        "settings": settings_snapshot(),
        "jobs": jobs_snapshot(compact=True),
    }


JOB_ACTIVE_STATUSES = frozenset({"running", "cancelling"})
JOB_UNFINISHED_STATUSES = frozenset({"queued", "running", "cancelling"})
MODULE_OPERATION_CANCEL_HINT = (
    "Le processus Odoo est arrêté, les modules que l'action a laissés en attente reprennent leur état précédent, "
    "puis Odoo redémarre. Les modules déjà traités par Odoo restent modifiés."
)
ODOO_SCRIPT_CANCEL_HINT = (
    "Le script Odoo est arrêté avant d'enregistrer ses modifications, puis Odoo redémarre."
)
MODULE_FILES_CANCEL_HINT = "Les modules déjà copiés par cette action sont retirés et les versions remplacées restaurées."
# (arrêt possible, ce que fait l'arrêt ou pourquoi il est impossible), par fonction d'action.
JOB_CANCEL_POLICIES = {
    "start_project_job": (True, "Les conteneurs et le serveur Odoo démarrés par cette action sont arrêtés ; ce qui tournait déjà reste démarré."),
    "stop_project_job": (True, "L'arrêt est interrompu : les conteneurs déjà arrêtés le restent."),
    "update_project_job": (True, "Le téléchargement en cours est interrompu. Un git pull commencé va d'abord à son terme."),
    "update_all_projects_job": (True, "La mise à jour s'arrête au projet en cours. Un git pull commencé va d'abord à son terme."),
    "module_command_job": (True, MODULE_OPERATION_CANCEL_HINT),
    "update_all_modules_job": (True, MODULE_OPERATION_CANCEL_HINT),
    "update_imported_modules_job": (True, MODULE_OPERATION_CANCEL_HINT),
    "reset_module_translations_job": (True, MODULE_OPERATION_CANCEL_HINT),
    "install_socle_job": (True, MODULE_OPERATION_CANCEL_HINT),
    "neutralize_database_job": (True, ODOO_SCRIPT_CANCEL_HINT),
    "regenerate_assets_job": (True, ODOO_SCRIPT_CANCEL_HINT),
    "reset_all_translations_job": (True, ODOO_SCRIPT_CANCEL_HINT),
    "reset_admin_password_job": (True, ODOO_SCRIPT_CANCEL_HINT),
    "restore_database_job": (True, "La restauration est interrompue et la base partiellement restaurée est supprimée avec son filestore."),
    "create_database_job": (True, "La création est interrompue et la base partiellement créée est supprimée avec son filestore."),
    "drop_database_job": (True, "Possible tant que la suppression n'a pas été envoyée à Odoo ; ensuite elle va à son terme."),
    "delete_project_job": (True, "Possible pendant l'arrêt des conteneurs : le projet est conservé. Le déplacement du dossier ne peut pas être interrompu."),
    "delete_module_code_job": (True, "Possible pendant la désinstallation Odoo (modules remis en état). La suppression des fichiers ne peut pas être interrompue."),
    "repository_modules_job": (True, MODULE_FILES_CANCEL_HINT),
    "import_zip_modules_job": (True, MODULE_FILES_CANCEL_HINT),
    "link_modules_job": (True, MODULE_FILES_CANCEL_HINT),
    "create_project_job": (True, "La création est interrompue : le projet partiellement créé et ses conteneurs sont retirés (dossier conservé dans la corbeille du gestionnaire)."),
    "install_traefik_job": (True, "Le téléchargement est interrompu ; aucun dossier partiel n'est conservé."),
    "convert_wsl_addon_links_job": (True, "La conversion s'arrête entre deux liens ; relance-la plus tard pour la terminer."),
    "install_git_job": (False, "l'installeur Windows ne peut pas être interrompu sans risque."),
    "repair_enterprise_links_job": (False, "opération courte sur les liens de modules."),
    "cancel_missing_module_operations_job": (False, "modification courte de la base, appliquée en une seule transaction."),
    "restore_module_update_exclusions_job": (False, "modification courte de la base, appliquée en une seule transaction."),
}
DEFAULT_JOB_CANCEL_POLICY = (True, "L'action est interrompue ; ce qu'elle a déjà modifié n'est pas annulé automatiquement.")


def jobs_conflict(first, second):
    """Deux actions qui ne doivent pas tourner en même temps : même ressource, ou action sur tous les projets."""
    if first.resources & second.resources:
        return True
    for everything, other in ((first, second), (second, first)):
        # `*` couvre tous les projets, pas Git ni Traefik.
        if "*" in everything.resources and any(item == "*" or item.startswith("project:") for item in other.resources):
            return True
    return False


def job_waiting_reason(job):
    """Pourquoi une action attend ; appelé sous JOBS_LOCK."""
    for other in JOBS.values():
        if other.id >= job.id:
            break
        if other.status in JOB_UNFINISHED_STATUSES and jobs_conflict(job, other):
            return f"Après « {other.title} »"
    return f"Limite de {MAX_RUNNING_JOBS} actions simultanées atteinte"


def schedule_jobs():
    """Démarre, dans l'ordre d'arrivée, les actions en attente qui ne croisent aucune action active."""
    to_start = []
    with JOBS_LOCK:
        active = [job for job in JOBS.values() if job.status in JOB_ACTIVE_STATUSES]
        waiting = []
        for job in JOBS.values():
            if job.status != "queued":
                continue
            # Une action arrivée plus tôt et encore bloquée garde la priorité sur son projet.
            if len(active) >= MAX_RUNNING_JOBS or any(jobs_conflict(job, other) for other in (*active, *waiting)):
                waiting.append(job)
                continue
            job.status = "running"
            job.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
            job.thread = threading.Thread(target=job.run, daemon=True)
            active.append(job)
            to_start.append(job)
    for job in to_start:
        job.thread.start()


def cancel_job(job_id):
    """Arrête une action en cours ou la retire de la file d'attente."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise ValueError("Action introuvable.")
        status = job.status
        if status in {"done", "error", "cancelled"}:
            raise ValueError("Cette action est déjà terminée.")
        if status == "running" and not job.cancellable:
            raise ValueError(f"« {job.title} » ne peut pas être arrêtée : {job.cancel_hint}")
        if status == "queued":
            job.status = "cancelled"
            job.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            job.error_message = "Retirée de la file d'attente avant son démarrage."
            job.target = None
            job.args = ()
    if status == "queued":
        job.add(job.error_message)
        job.publish_completion()
        schedule_jobs()
        return job
    if status == "cancelling":
        return job

    outcome = job.control.request_cancel()
    if outcome == "refused":
        raise ValueError(
            f"« {job.title} » ne peut plus être arrêtée : étape irréversible en cours ({job.control.irreversible_step})."
        )
    with JOBS_LOCK:
        if job.status == "running":
            job.status = "cancelling"
    if outcome == "deferred":
        job.add(f"Arrêt demandé : il sera effectif à la fin de l'étape en cours ({job.control.protected_step}).")
    elif outcome == "accepted":
        job.add("Arrêt demandé : interruption de l'action...")
    return job


class Job:
    def __init__(self, title, target, args=(), project=None, resources=None):
        global NEXT_JOB_ID
        self.title = title
        self.project = project
        self.status = "queued"
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.finished_at = None
        self.error_message = ""
        self.lines = []
        self.output = ""
        # Nombre cumulé de caractères écrits : permet à l'interface de ne demander que la suite.
        self.output_total = 0
        self.result = {}
        self.progress = None
        self.target = target
        self.args = args
        self.thread = None
        self.control = job_control.JobControl()
        self.cancellable, self.cancel_hint = JOB_CANCEL_POLICIES.get(
            getattr(target, "__name__", ""), DEFAULT_JOB_CANCEL_POLICY
        )
        if resources is None:
            resources = {f"project:{project}"} if project else ()
        self.resources = frozenset(resources)
        with JOBS_LOCK:
            finished_ids = [job_id for job_id, job in JOBS.items() if job.status not in JOB_UNFINISHED_STATUSES]
            excess = max(0, len(JOBS) - MAX_RETAINED_JOBS + 1)
            for job_id in finished_ids[:excess]:
                JOBS.pop(job_id, None)
            self.id = NEXT_JOB_ID
            NEXT_JOB_ID += 1
            JOBS[self.id] = self
        schedule_jobs()

    def add(self, line):
        with JOBS_LOCK:
            self.lines.append(line.rstrip("\n"))
            self._append_output(line.rstrip("\n") + "\n")
        # Chaque ligne écrite par l'action est un point d'arrêt : les longues sorties Odoo s'interrompent vite.
        if job_control.current_control() is self.control:
            self.control.checkpoint()

    def _trim_lines(self):
        # Troncature amortie : recopier 700 lignes et 120 Ko à chaque ligne coûtait
        # cher pendant les longues sorties Odoo, verrou global tenu.
        if len(self.lines) > JOB_LINES_LIMIT * 2:
            del self.lines[:-JOB_LINES_LIMIT]

    def _append_output(self, text):
        self.output += text
        self.output_total += len(text)
        if len(self.output) > JOB_OUTPUT_LIMIT * 2:
            self.output = self.output[-JOB_OUTPUT_LIMIT:]
        self._trim_lines()

    def add_text(self, text):
        with JOBS_LOCK:
            self._append_output(text)
            for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                if line:
                    self.lines.append(line)
            self._trim_lines()

    def set_progress(self, label, current=None, total=None):
        with JOBS_LOCK:
            self.progress = {
                "label": str(label),
                "current": current,
                "total": total,
            }

    def run(self):
        failure = None
        failure_trace = ""
        try:
            with job_control.bind_control(self.control):
                try:
                    self.target(self, *self.args)
                except BaseException as exc:
                    failure, failure_trace = exc, traceback.format_exc()
                cancelled = self.control.cancel_requested and (failure is not None or self.control.cleaning_up)
                if cancelled:
                    self.finish_cancelled(failure)
            if not cancelled and failure is None:
                with JOBS_LOCK:
                    if self.status in JOB_ACTIVE_STATUSES:
                        self.status = "done"
                if self.control.cancel_requested:
                    self.add("Arrêt demandé après la fin de l'action : rien n'a été annulé.")
            elif not cancelled:
                self.error_message = str(failure).strip() or "Une erreur inattendue est survenue."
                self.add(f"Erreur: {self.error_message}")
                with JOBS_LOCK:
                    self.status = "error"
                record_manager_error(
                    f"Job #{self.id} · {self.title}",
                    failure,
                    details=failure_trace,
                    project=self.project or "",
                )
        finally:
            dependents = []
            with JOBS_LOCK:
                self.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self.args = ()
                self.target = None
                self.thread = None
                if self.status in {"error", "cancelled"}:
                    outcome = "a été arrêtée" if self.status == "cancelled" else "a échoué"
                    for other in JOBS.values():
                        if other.status == "queued" and jobs_conflict(self, other):
                            other.status = "cancelled"
                            other.finished_at = self.finished_at
                            other.error_message = f"Annulée : l'action précédente « {self.title} » {outcome}."
                            other.target = None
                            other.args = ()
                            dependents.append(other)
            for other in dependents:
                other.add(other.error_message)
                other.publish_completion()
            invalidate_overview_databases()
            EVENT_DOCKER_REFRESH.set()
            self.publish_completion()
            schedule_jobs()

    def finish_cancelled(self, failure):
        self.control.begin_cleanup()
        if failure is not None and not isinstance(failure, job_control.JobCancelled):
            self.add(f"Interruption : {failure}")
        failures = self.control.run_reverts(self.add)
        message = "Action arrêtée par l'utilisateur."
        if failures:
            message += " Retour arrière incomplet : " + " ; ".join(failures)
            record_manager_error(f"Job #{self.id} · {self.title}", message, project=self.project or "")
        self.error_message = message
        self.add(message)
        with JOBS_LOCK:
            self.status = "cancelled"

    def publish_completion(self):
        publish_event(
            "job_completed",
            {
                "id": self.id,
                "status": self.status,
                "project": self.project,
                "finished_at": self.finished_at,
                "result": self.result,
            },
        )


def terminate_active_subprocesses(wait_seconds=0.5):
    with ACTIVE_PROCESSES_LOCK:
        processes = list(ACTIVE_PROCESSES)
    for process in processes:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
    deadline = time.monotonic() + wait_seconds
    for process in processes:
        remaining = max(0, deadline - time.monotonic())
        if process.poll() is None:
            try:
                process.wait(timeout=remaining)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    process.kill()
                except OSError:
                    pass


def run_stream(job, args, cwd=None):
    cwd = cwd or WORKSPACE
    command = [str(argument) for argument in args]
    process_cwd = Path(cwd)
    if platform_id() == "windows" and command_uses_wsl(command):
        command = wsl_command_with_cwd(command, process_cwd, SETTINGS, WORKSPACE)
        process_cwd = Path.home()
    elif not process_cwd.is_dir():
        raise RuntimeError(f"Dossier de travail introuvable: {process_cwd}")
    job_control.checkpoint()
    job.add("$ " + " ".join(command))
    process = subprocess.Popen(
        command,
        cwd=str(process_cwd),
        env=command_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **hidden_process_kwargs(),
    )
    with ACTIVE_PROCESSES_LOCK:
        ACTIVE_PROCESSES.add(process)
    job_control.track_process(process)
    try:
        assert process.stdout is not None
        for line in process.stdout:
            job.add(line)
        code = process.wait()
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        job_control.untrack_process(process)
        if process.stdout is not None:
            process.stdout.close()
        with ACTIVE_PROCESSES_LOCK:
            ACTIVE_PROCESSES.discard(process)
    job.add(f"Code retour: {code}")
    job_control.checkpoint()
    if code != 0 and job.status == "running":
        job.status = "error"
    return code


def manager_job(job, *args):
    if not MANAGER.exists():
        raise RuntimeError(f"Script introuvable: {MANAGER}")
    return run_stream(job, manager_command(*args))


def manager_command(*args):
    if platform_id() != "windows" or SETTINGS.execution_mode != "wsl":
        return shell_command(SETTINGS, MANAGER, *args)

    docker_host_path = resolve_host_executable(SETTINGS.docker_executable)
    variables = [
        f"ODOO_WORKSPACE={execution_path(WORKSPACE, SETTINGS)}",
        f"ODOO_MANAGER_DOCKER={execution_path(docker_host_path, SETTINGS)}",
        "ODOO_MANAGER_EXECUTION_MODE=wsl",
    ]
    traefik_dir = local_traefik_directory()
    if traefik_dir:
        variables.append(f"TRAEFIK_DIR={execution_path(traefik_dir, SETTINGS)}")
    return [
        *command_prefix(SETTINGS),
        "env",
        *variables,
        "sh",
        execution_path(MANAGER, SETTINGS),
        *args,
    ]


def update_all_modules_manager_args(project, db_name, allow_missing_filestore=False):
    command = "--update-all-modules-without-filestore" if allow_missing_filestore else "--update-all-modules"
    return command, project, db_name


def available_update_modules(project, db_name, states=None, available_names=None, excluded_names=None):
    states = states if states is not None else installed_modules(project, db_name)
    available_names = available_names if available_names is not None else {path.name for path in module_dirs(project)}
    excluded_names = set(excluded_names or ())
    return sorted(
        name
        for name, state in states.items()
        if name in available_names
        and name not in excluded_names
        and state.get("state") in {"installed", "to upgrade"}
    )


def active_local_module_exceptions(project, db_name, states=None, available_names=None):
    states = states if states is not None else installed_modules(project, db_name)
    configured = ignored_missing_modules(project, db_name)
    return sorted(
        name
        for name in configured
        if states.get(name, {}).get("state") == "installed"
    )


def cancel_missing_module_operations_job(job, project, db_name, modules):
    project = validate_project(project)
    db_name = validate_odoo_db(db_name)
    requested = module_name_list(modules)

    if container_status(f"postgresql-{project}") != "running":
        project_service().start_project(project, log=job.add)

    states = installed_modules(project, db_name)
    if not states:
        raise RuntimeError(f"Impossible de lire les modules de {db_name}.")

    available_names = {path.name for path in module_dirs(project)}
    dependency_lines = db_query_lines(
        project,
        db_name,
        "select m.name || '|' || d.name "
        "from ir_module_module_dependency d "
        "join ir_module_module m on m.id = d.module_id "
        "where m.state in ('installed','to install','to upgrade','to remove') "
        "order by m.name,d.name;",
    )
    dependencies = []
    for line in dependency_lines:
        dependent, separator, dependency = line.partition("|")
        if separator:
            dependencies.append((dependent, dependency))

    candidates, invalid, automatic_exclusions = local_ignore_plan(
        states,
        available_names,
        requested,
        dependencies,
        already_excluded=ignored_missing_modules(project, db_name),
    )
    if invalid:
        raise RuntimeError(
            "Ces modules ne sont pas des opérations en attente avec code absent: " + ", ".join(invalid)
        )
    reset_modules = sorted(set(candidates) | {
        name for name in automatic_exclusions
        if states.get(name, {}).get("state") in TRANSIENT_MODULE_STATES
    })
    quoted = ",".join(f"'{name}'" for name in reset_modules)
    query = (
        "begin; "
        "lock table ir_module_module in row exclusive mode; "
        "update ir_module_module "
        "set state = case when state = 'to install' then 'uninstalled' else 'installed' end "
        f"where name in ({quoted}) and state in ('to install','to upgrade','to remove') "
        "returning name; "
        "select name || '|' || state from ir_module_module "
        f"where name in ({quoted}) and state in ('installed','uninstalled') order by name; "
        "commit;"
    )
    changed_lines = db_query_lines(project, db_name, query)
    changed = {}
    for line in changed_lines:
        name, separator, state = line.partition("|")
        if separator and name in reset_modules:
            changed[name] = state
    if set(changed) != set(reset_modules):
        missing = sorted(set(reset_modules) - set(changed))
        raise RuntimeError("La base n'a pas confirmé la modification de: " + ", ".join(missing))

    retained = sorted(
        {name for name, state in changed.items() if state == "installed"}
        | {name for name in automatic_exclusions if states.get(name, {}).get("state") == "installed"}
    )
    if retained:
        remember_ignored_missing_modules(project, db_name, retained)

    job.add("Opérations annulées pour les modules dont le code est absent:")
    for name in candidates:
        job.add(f"- {name}: {states[name].get('state')} -> {changed[name]}")
    if automatic_exclusions:
        job.add("Dépendants exclus automatiquement de cette mise à jour locale:")
        for name in automatic_exclusions:
            previous = states.get(name, {}).get("state", "inconnu")
            current = changed.get(name, previous)
            job.add(f"- {name}: {previous} -> {current}")
    job.add("Aucune donnée métier, table ou pièce jointe n'a été supprimée.")
    job.add("Les prochaines mises à jour utiliseront uniquement les modules non exclus dont le code est disponible.")


def restore_module_update_exclusions_job(job, project, db_name, modules):
    project = validate_project(project)
    db_name = validate_odoo_db(db_name)
    requested = module_name_list(modules)
    configured = ignored_missing_modules(project, db_name)
    invalid = sorted(set(requested) - configured)
    if invalid:
        raise RuntimeError("Ces modules ne sont pas exclus localement: " + ", ".join(invalid))

    quoted = ",".join(f"'{name}'" for name in requested)
    query = (
        "begin; "
        "lock table ir_module_module in row exclusive mode; "
        "update ir_module_module set state = 'to upgrade' "
        f"where name in ({quoted}) and state = 'installed' "
        "returning name || '|' || state; "
        "commit;"
    )
    changed_lines = db_query_lines(project, db_name, query)
    changed = {
        name
        for line in changed_lines
        for name, separator, state in [line.partition("|")]
        if separator and state == "to upgrade" and name in requested
    }
    if changed != set(requested):
        missing = sorted(set(requested) - changed)
        raise RuntimeError("La base n'a pas confirmé la réactivation de: " + ", ".join(missing))

    forget_ignored_missing_modules(project, db_name, requested)
    job.add("Modules réactivés pour la prochaine mise à jour:")
    for name in sorted(changed):
        job.add(f"- {name}: installed -> to upgrade")
    job.add("Si leur code est absent, le diagnostic les signalera de nouveau avant la mise à jour.")


def legacy_workspace_path():
    """Ancien dossier de projets Windows, vu depuis la distribution.

    L'application le transmet au lancement (ODOO_MANAGER_LEGACY_WORKSPACE) ; un réglage
    explicite l'emporte.
    """
    configured = str(getattr(SETTINGS, "legacy_workspace", "") or "").strip()
    configured = configured or os.environ.get("ODOO_MANAGER_LEGACY_WORKSPACE", "").strip()
    return Path(configured) if configured else None


def migration_snapshot():
    """Projets restés sur le disque Windows, proposés à la migration."""
    source = legacy_workspace_path()
    if not source or not safe_path_is_dir(source):
        return {"available": False, "source": str(source or ""), "projects": []}
    return {
        "available": True,
        "source": str(source),
        "projects": migration_candidates(source, WORKSPACE, migration_privileges()),
    }


def migration_privileges():
    """sudo sous Linux : la base du projet appartient à l'uid PostgreSQL du conteneur."""
    return privileged_prefix() if platform_id() == "linux" else []


def migrate_project_job(job, project):
    """Copie un projet du disque Windows vers l'environnement Linux, sans toucher à l'original."""
    source_root = legacy_workspace_path()
    if not source_root:
        raise RuntimeError("Aucun ancien dossier de projets n'est connu.")
    source = source_root / project
    if not is_project_directory(source):
        raise ValueError(f"{project} n'est pas un projet Odoo dans {source_root}.")
    prefix = migration_privileges()
    if not project_is_stopped(source, prefix):
        raise RuntimeError(
            f"{project} tourne encore : arrête-le dans l'ancienne application avant de le migrer, "
            "sinon sa base serait copiée dans un état incohérent."
        )
    destination = WORKSPACE / project

    job.add(f"Mesure de {project}...")
    # Lue une seule fois : elle sert à la mesure puis au contrôle de la copie.
    source_listing = list_tree(source, prefix) if prefix else None
    measured = measure_project(source, prefix, listing=source_listing)
    job.add(f"{measured['files']} fichiers, {measured['bytes'] / (1024 ** 3):.1f} Go à copier.")
    free = shutil.disk_usage(WORKSPACE).free
    if free < measured["bytes"] * 1.1:
        raise RuntimeError("Espace disque insuffisant dans l'environnement Linux pour cette copie.")

    def report(copied, total):
        job.progress = {"current": copied, "total": total or measured["files"]}

    try:
        if prefix:
            copy_project_privileged(source, destination, prefix, log=job.add, progress=report, total_files=measured["files"])
        else:
            copy_project(source, destination, log=job.add, progress=report, total_files=measured["files"])
    except Exception:
        # Une copie interrompue ne doit pas laisser un demi-projet dans la liste.
        if prefix:
            subprocess.run([*prefix, "rm", "-rf", "--", str(destination)], check=False, timeout=600)
        else:
            shutil.rmtree(destination, ignore_errors=True)
        raise

    job.add("Contrôle de la copie...")
    comparison = compare_projects(source, destination, prefix, source_listing=source_listing)
    if not comparison["identical"]:
        job.add(
            f"{comparison['missing_count']} fichier(s) manquant(s), "
            f"{comparison['different_links_count']} lien(s) différent(s)."
        )
        raise RuntimeError("La copie ne correspond pas à l'original : le projet migré a été conservé pour inspection.")
    clear_project_module_cache(project)
    invalidate_overview_databases(project)
    job.add(f"{project} est migré. L'original reste dans {source_root} : supprime-le quand tu l'auras vérifié.")
    return {"project": project, "files": comparison["copied_files"], "source": str(source)}


def cleanup_staging_job(job):
    """Supprime les dossiers de créations interrompues, jamais un projet."""
    entries = abandoned_staging_entries(WORKSPACE)
    if not entries:
        job.add("Aucun dossier de préparation à nettoyer.")
        return {"removed": 0}

    creator = ProjectCreator(SETTINGS, WORKSPACE, project_service())
    free_before = shutil.disk_usage(WORKSPACE).free
    removed = 0
    for entry in entries:
        path = Path(entry["path"])
        job.add(f"Suppression de {path.name}...")
        creator.cleanup_staging_path(path, log=job.add)
        if path.exists():
            job.add(f"{path.name} n'a pas pu être supprimé.")
            continue
        removed += 1
    freed = max(0, shutil.disk_usage(WORKSPACE).free - free_before)
    job.add(f"{removed} dossier(s) supprimé(s), {freed / (1024 ** 3):.1f} Go libérés.")
    return {"removed": removed, "freed_bytes": freed}


def install_traefik_job(job):
    status = docker_status(SETTINGS)
    if not status["running"]:
        raise RuntimeError("Docker doit être installé et démarré avant l'installation de Traefik.")
    # Même résolution que ProjectService.git : sous Windows, Git peut n'exister que dans WSL.
    git_runtime = preferred_git_runtime()
    git_probe_cwd = Path.home() if git_runtime["kind"] == "wsl" else (WORKSPACE if WORKSPACE.exists() else ROOT)
    git_code, git_output = run_capture([*git_runtime["command"], "--version"], cwd=git_probe_cwd, timeout=8)
    if git_code != 0:
        raise RuntimeError(
            "Git est introuvable, côté Windows comme dans WSL. Utilise le bouton Installer dans l'étape Git."
            if platform_id() == "windows"
            else "Git doit être installé avant Traefik. Utilise le bouton Installer dans l'étape Git."
        )
    job.add(git_output.splitlines()[0] if git_output else "Git détecté.")
    job.add(f"Git utilisé : {git_runtime['label']}.")
    try:
        project_service().install_traefik(TRAEFIK_REPO, log=job.add)
    finally:
        invalidate_traefik_detection()


def start_project_job(job, project):
    project = validate_project(project)
    try:
        project_service().start_project(project, log=job.add)
    finally:
        invalidate_traefik_detection()


def update_project_job(job, project):
    project = validate_project(project)
    project_service().update_project(project, log=job.add)
    clear_project_module_cache(project)


def update_all_projects_job(job):
    projects = project_dirs()
    project_service().update_all_projects(log=job.add)
    for project in projects:
        clear_project_module_cache(project)


def neutralize_database_job(job, project, db_name):
    project = validate_project(project)
    db_name = validate_odoo_db(db_name)
    if db_name not in list_databases_for(project):
        raise ValueError("La base Odoo sélectionnée n'existe plus dans PostgreSQL.")
    project_service().run_odoo_neutralize_command(project, db_name, log=job.add)


def validate_admin_password(password):
    password = str(password or "")
    if not password.strip():
        raise ValueError("Saisis le nouveau mot de passe administrateur.")
    if len(password) > 128:
        raise ValueError("Le mot de passe administrateur ne doit pas dépasser 128 caractères.")
    if any(ord(char) < 32 for char in password):
        raise ValueError("Le mot de passe administrateur contient des caractères non autorisés.")
    return password


def existing_odoo_database(project, db_name):
    project = validate_project(project)
    db_name = validate_odoo_db(db_name)
    if db_name not in list_databases_for(project):
        raise ValueError("La base Odoo sélectionnée n'existe plus dans PostgreSQL.")
    return project, db_name


def reset_module_translations_job(job, project, db_name, modules):
    module_command_job(job, "--update-module", project, db_name, modules, overwrite_translations=True)


def regenerate_assets_job(job, project, db_name):
    project, db_name = existing_odoo_database(project, db_name)
    project_service().run_odoo_regenerate_assets(project, db_name, log=job.add)


ODOO_LANGUAGE_CODE_RE = re.compile(r"^[a-z]{2,3}(?:_[A-Z]{2})?(?:@[a-z]+)?$")


def validate_language_codes(value):
    codes = list(dict.fromkeys(code.strip() for code in str(value or "").split(",") if code.strip()))
    invalid = [code for code in codes if not ODOO_LANGUAGE_CODE_RE.fullmatch(code)]
    if invalid:
        raise ValueError("Code(s) de langue invalide(s) : " + ", ".join(invalid))
    return codes


def installed_languages(project, db_name):
    lines = db_query_lines(project, db_name, "select code, name from res_lang where active order by name;")
    return [{"code": code, "name": name} for code, _, name in (line.partition("|") for line in lines)]


def reset_all_translations_job(job, project, db_name, languages):
    project, db_name = existing_odoo_database(project, db_name)
    project_service().run_odoo_reset_all_translations(
        project, db_name, validate_language_codes(languages), log=job.add,
    )


def reset_admin_password_job(job, project, db_name, password):
    project, db_name = existing_odoo_database(project, db_name)
    project_service().run_odoo_reset_admin_password(project, db_name, validate_admin_password(password), log=job.add)


def local_odoo_connection(url, timeout):
    """Connexion HTTP vers une instance locale, servie par Traefik sur la boucle locale.

    Windows ne résout pas les sous-domaines de .localhost (getaddrinfo, erreur 11001),
    contrairement aux navigateurs, à macOS et à Linux : on se connecte à 127.0.0.1 en
    gardant le nom d'hôte dans l'en-tête Host, comme la sonde de disponibilité d'Odoo.
    Retourne (connexion, chemin, en-tête Host).
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not host:
        raise RuntimeError(f"URL Odoo invalide : {url}")
    connect_host = host if host in {"127.0.0.1", "localhost", "::1"} else "127.0.0.1"
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(connect_host, parsed.port, timeout=timeout)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    host_header = host if parsed.port in {None, 80, 443} else f"{host}:{parsed.port}"
    return connection, target, host_header


def post_form_no_redirect(url, data, timeout=240):
    body = urllib.parse.urlencode(data).encode("utf-8")
    connection, target, host_header = local_odoo_connection(url, timeout)
    with job_control.interruptible(connection.close):
        return send_form_no_redirect(connection, target, host_header, body)


def send_form_no_redirect(connection, target, host_header, body):
    try:
        connection.request(
            "POST",
            target,
            body=body,
            headers={"Host": host_header, "Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        if response.status in (301, 302, 303, 307, 308):
            return response.status, ""
        if response.status >= 400:
            content = response.read(4096).decode("utf-8", errors="replace")
            raise RuntimeError(f"Odoo a retourne HTTP {response.status}: {content[:600]}")
        return response.status, response.read(131072).decode("utf-8", errors="replace")
    except (OSError, http.client.HTTPException) as exc:
        raise RuntimeError(f"Odoo ne répond pas sur {host_header} : {exc}") from exc
    finally:
        connection.close()


def extract_odoo_page_error(content):
    if not content:
        return ""
    pattern = r'<div\b[^>]*class=["\'][^"\']*\balert-danger\b[^"\']*["\'][^>]*>(.*?)</div>'
    for match in re.finditer(pattern, content, flags=re.IGNORECASE | re.DOTALL):
        message = re.sub(r"<[^>]+>", " ", match.group(1))
        message = re.sub(r"\s+", " ", html.unescape(message)).strip()
        if message:
            return message
    return ""


def validate_odoo_backup_archive(backup_path):
    backup_path = Path(backup_path)
    if not zipfile.is_zipfile(backup_path):
        raise ValueError("La sauvegarde n'est pas une archive ZIP Odoo valide.")

    try:
        with zipfile.ZipFile(backup_path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DATABASE_BACKUP_ENTRIES:
                raise ValueError("La sauvegarde contient trop de fichiers.")
            names = {entry.filename.replace("\\", "/") for entry in entries}
            if "dump.sql" not in names:
                raise ValueError("Archive Odoo invalide: le fichier dump.sql est absent.")
            for entry in entries:
                normalized = entry.filename.replace("\\", "/")
                if normalized != "dump.sql" and not normalized.startswith("filestore/"):
                    continue
                parts = [part for part in normalized.split("/") if part]
                if normalized.startswith("/") or ".." in parts:
                    raise ValueError("Archive Odoo invalide: chemin de fichier dangereux.")
                if entry.flag_bits & 0x1:
                    raise ValueError("Les sauvegardes ZIP chiffrées ne sont pas prises en charge.")
            return {
                "entries": len(entries),
                "has_filestore": any(name.startswith("filestore/") for name in names),
                "has_manifest": "manifest.json" in names,
            }
    except zipfile.BadZipFile as exc:
        raise ValueError("La sauvegarde ZIP est illisible ou endommagée.") from exc


def save_request_body_to_file(stream, content_length, destination, chunk_size=1024 * 1024):
    destination = Path(destination)
    remaining = int(content_length)
    with destination.open("wb") as output:
        while remaining:
            chunk = stream.read(min(chunk_size, remaining))
            if not chunk:
                raise ValueError("Le téléversement de la sauvegarde a été interrompu.")
            output.write(chunk)
            remaining -= len(chunk)
    return destination


def multipart_field(boundary, name, value):
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode("utf-8")


def post_odoo_database_restore(job, url, backup_path, filename, db_name, master_pwd, copy, neutralize):
    try:
        connection, target, host_header = local_odoo_connection(url, timeout=2 * 60 * 60)
    except RuntimeError as exc:
        raise RuntimeError("URL Odoo invalide pour la restauration.") from exc

    boundary = f"----OdooManager{os.getpid()}{time.time_ns()}"
    fields = [
        ("master_pwd", master_pwd),
        ("name", db_name),
        ("copy", "true" if copy else "false"),
    ]
    if neutralize:
        fields.append(("neutralize_database", "on"))
    prefix = b"".join(multipart_field(boundary, name, value) for name, value in fields)
    safe_filename = SAFE_IMPORT_NAME_RE.sub("_", Path(filename).name) or "backup.zip"
    prefix += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="backup_file"; filename="{safe_filename}"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode("utf-8")
    suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
    backup_size = Path(backup_path).stat().st_size
    content_length = len(prefix) + backup_size + len(suffix)
    sent = 0
    next_progress = 10
    # Fermer la connexion interrompt l'envoi ou l'attente de la réponse quand l'action est arrêtée.
    with job_control.interruptible(connection.close):
        try:
            connection.putrequest("POST", target, skip_host=True)
            connection.putheader("Host", host_header)
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(content_length))
            connection.putheader("Connection", "close")
            connection.endheaders()
            connection.send(prefix)
            with Path(backup_path).open("rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
                    progress = int((sent * 100) / backup_size) if backup_size else 100
                    if progress >= next_progress:
                        job.add(f"Envoi de la sauvegarde vers Odoo... {min(progress, 100)} %")
                        next_progress = ((progress // 10) + 1) * 10
            connection.send(suffix)
            response = connection.getresponse()
            content = response.read(1024 * 1024).decode("utf-8", errors="replace")
            return response.status, content
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeError(f"La restauration n'a pas pu être transmise à Odoo: {exc}") from exc
        finally:
            connection.close()


def odoo_restore_error(content):
    if "Database restore error:" not in content:
        return ""
    plain = html.unescape(re.sub(r"<[^>]+>", " ", content))
    plain = re.sub(r"\s+", " ", plain).strip()
    marker = "Database restore error:"
    return plain[plain.find(marker):plain.find(marker) + 800]


def restore_database_job(job, project, backup_path, filename, db_name, master_pwd, copy=True, neutralize=True):
    project = validate_project(project)
    db_name = validate_new_db(db_name)
    master_pwd = validate_required_text(master_pwd, "Master password")
    backup_path = Path(backup_path)
    service = project_service()
    cron_safe_server_started = False
    try:
        details = validate_odoo_backup_archive(backup_path)
        size_mb = backup_path.stat().st_size / (1024 * 1024)
        job.add(f"Restauration de {db_name} dans {project}")
        job.add(f"Sauvegarde: {filename} ({size_mb:.1f} Mo)")
        job.add("Filestore inclus: " + ("oui" if details["has_filestore"] else "non"))
        job.add("Base déclarée comme copie: " + ("oui" if copy else "non"))
        job.add("Neutralisation: " + ("activée" if neutralize else "désactivée"))
        job.add("Démarrage du projet avant restauration...")
        service.start_project(project, log=job.add)

        if db_name in set(list_databases_for(project)):
            raise RuntimeError(f"La base existe déjà: {db_name}")
        job_control.on_cancel(
            f"suppression de la base partiellement restaurée {db_name}",
            lambda: drop_partial_database(job, project, db_name),
        )

        if neutralize:
            job.add("Passage temporaire d'Odoo en mode sans cron pendant la restauration...")
            service.stop_odoo_server(project, log=job.add)
            cron_safe_server_started = True
            service.start_odoo_server(project, log=job.add, disable_cron=True)
            service.wait_project_http(project, log=job.add)

        url = urllib.parse.urljoin(project_url(project), "web/database/restore")
        version = project_odoo_version(project)
        native_restore_neutralization = bool(neutralize and version != "15.0")
        if neutralize and not native_restore_neutralization:
            job.add("Odoo 15: neutralisation appliquée par la seconde passe après restauration.")
        job.add(f"Restauration via Odoo: {url}")
        if not neutralize:
            # Odoo poursuit la restauration dans son serveur même si la connexion est coupée.
            job_control.on_cancel(
                "redémarrage d'Odoo pour interrompre la restauration côté serveur",
                lambda: restart_odoo_server(job, service, project),
            )
        status, content = post_odoo_database_restore(
            job,
            url,
            backup_path,
            filename,
            db_name,
            master_pwd,
            bool(copy),
            native_restore_neutralization,
        )
        job.add(f"Réponse Odoo: HTTP {status}")
        restore_error = odoo_restore_error(content)
        if restore_error:
            raise RuntimeError(restore_error)
        if status not in {200, 201, 202, 301, 302, 303}:
            raise RuntimeError(f"Odoo a refusé la restauration avec le statut HTTP {status}.")

        for waited in range(0, 122, 2):
            if db_name in set(list_databases_for(project)):
                job.add(f"Base restaurée: {db_name}")
                if neutralize:
                    job.add("Seconde passe de neutralisation et contrôles de sécurité...")
                    # Cette méthode arrête le serveur sans cron et redémarre le serveur normal,
                    # y compris si la neutralisation échoue.
                    cron_safe_server_started = False
                    service.run_odoo_neutralize_command(project, db_name, log=job.add)
                clear_project_module_cache(project)
                return
            job.add(f"Attente apparition base... {waited}s/120s")
            job_control.sleep(2)
        raise RuntimeError("Odoo a accepté la sauvegarde, mais la base n'apparaît pas dans PostgreSQL.")
    finally:
        try:
            if cron_safe_server_started:
                job.add("Rétablissement du serveur Odoo normal après interruption de la restauration...")
                try:
                    service.stop_odoo_server(project, log=job.add)
                    service.start_odoo_server(project, log=job.add)
                    service.wait_project_http(project, log=job.add)
                except Exception as exc:
                    job.add(f"Erreur pendant le rétablissement du serveur Odoo: {exc}")
            backup_path.unlink(missing_ok=True)
            job.add("Fichier temporaire de restauration supprimé.")
        finally:
            parent = backup_path.parent
            try:
                parent.rmdir()
            except OSError:
                pass


def create_database_job(job, project, db_name, master_pwd, login, password, lang, country, demo):
    project = validate_project(project)
    db_name = validate_new_db(db_name)
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
    service = project_service()
    job_control.on_cancel(
        f"suppression de la base partiellement créée {db_name}",
        lambda: drop_partial_database(job, project, db_name),
    )
    job_control.on_cancel(
        "redémarrage d'Odoo pour interrompre la création côté serveur",
        lambda: restart_odoo_server(job, service, project),
    )

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
    odoo_error = extract_odoo_page_error(content) if status == 200 else ""
    if odoo_error:
        raise RuntimeError(f"Odoo a refusé la création de la base : {odoo_error}")
    if status == 200 and content:
        job.add("Odoo a renvoyé une page sans confirmation explicite ; vérification dans PostgreSQL...")

    max_wait = 120
    for waited in range(0, max_wait + 2, 2):
        job.set_progress("Initialisation de la base Odoo", min(waited, max_wait), max_wait)
        databases = set(list_databases_for(project))
        if db_name in databases:
            job.set_progress("Base Odoo prête", max_wait, max_wait)
            job.add(f"Base créée : {db_name}")
            clear_project_module_cache(project)
            job.result = {"kind": "database_creation", "database": db_name}
            return
        if waited == 0 or waited % 10 == 0:
            job.add(f"Initialisation de la base... {waited}s/{max_wait}s")
        job_control.sleep(2)

    raise RuntimeError(
        "La création a été envoyée, mais la base n'apparaît pas dans PostgreSQL après 120 secondes."
    )


def drop_database_job(job, project, db_name, master_pwd):
    project, db_name = existing_odoo_database(project, db_name)
    master_pwd = validate_required_text(master_pwd, "Master password")

    url = urllib.parse.urljoin(project_url(project), "web/database/drop")
    job.add(f"Suppression de la base {db_name} dans {project}")
    job.add(f"Appel Odoo: {url}")
    with job_control.protected(f"suppression de la base {db_name} par Odoo", irreversible=True):
        status, content = post_form_no_redirect(url, {"master_pwd": master_pwd, "name": db_name})
        job.add(f"Réponse Odoo: HTTP {status}")
        odoo_error = extract_odoo_page_error(content) if status == 200 else ""
        if odoo_error:
            raise RuntimeError(f"Odoo a refusé la suppression de la base : {odoo_error}")

        for waited in range(0, 32, 2):
            if db_name not in set(list_databases_for(project)):
                invalidate_overview_databases(project)
                clear_project_module_cache(project)
                job.add(f"Base supprimée (filestore inclus) : {db_name}")
                return
            job_control.sleep(2)
    raise RuntimeError("La suppression a été envoyée, mais la base est toujours présente dans PostgreSQL.")


def drop_partial_database(job, project, db_name):
    """Supprime une base laissée incomplète par une restauration ou une création interrompue."""
    if container_status(f"postgresql-{project}") != "running":
        job.add(f"PostgreSQL arrêté : rien à supprimer pour {db_name}.")
        return
    literal = db_name.replace("'", "''")
    identifier = db_name.replace('"', '""')
    for attempt in range(3):
        # Deux -c : DROP DATABASE refuse de s'exécuter dans le bloc de transaction d'un -c unique.
        code, output = run_capture(
            docker_command(
                SETTINGS, "exec", f"postgresql-{project}", "psql", "-X", "-v", "ON_ERROR_STOP=1",
                "-U", "postgres", "-d", "postgres",
                "-c", f"select pg_terminate_backend(pid) from pg_stat_activity where datname = '{literal}' and pid <> pg_backend_pid();",
                "-c", f'drop database if exists "{identifier}";',
            ),
            timeout=120,
        )
        if code == 0:
            break
        job.add(output[-600:] if output else f"Suppression de {db_name} refusée par PostgreSQL.")
        time.sleep(2)
    else:
        raise RuntimeError(f"la base {db_name} n'a pas pu être supprimée")
    if container_status(f"odoo-{project}") == "running":
        run_capture(
            docker_command(SETTINGS, "exec", f"odoo-{project}", "rm", "-rf", "--", f"/home/odoo/srv/data/filestore/{db_name}"),
            timeout=120,
        )
    invalidate_overview_databases(project)
    clear_project_module_cache(project)
    job.add(f"Base {db_name} et son filestore supprimés.")


def restart_odoo_server(job, service, project):
    service.stop_odoo_server(project, log=job.add)
    service.start_odoo_server(project, log=job.add)


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

    with job_control.protected(f"déplacement de {project} dans la corbeille", irreversible=True):
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

    if is_wsl_symlink(entry):
        # Windows ne peut ni lire ni vérifier la cible (WinError 1920) : rien n'est supprimé à l'aveugle.
        raise RuntimeError(
            f"Suppression refusée pour {module_name} : son lien a été créé par WSL et Windows ne peut pas le vérifier. "
            "Convertis d'abord les liens du projet depuis le bandeau affiché dans le projet."
        )
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


def addon_links_wsl_distribution(*directories, creating=False):
    """Distribution WSL qui porte les liens d'addons de ces dossiers, sinon None.

    Windows ne voit pas les liens créés par WSL : `exists()` et `is_symlink()`
    renvoient False, puis `symlink_to` échoue (WinError 183). Un lien Windows
    relatif est en revanche suivi par Docker Desktop : WSL ne sert que pour un
    workspace WSL, un dossier qui contient encore des liens WSL, ou une création
    que Windows refuse (mode développeur désactivé). Même règle que ProjectCreator.
    """
    if platform_id() != "windows":
        return None
    if active_workspace_wsl_context() or SETTINGS.execution_mode == "wsl":
        return active_wsl_distribution()
    needs_wsl = any(contains_wsl_symlink(directory) for directory in directories) or (
        creating and not all(native_symlinks_supported(directory) for directory in directories)
    )
    if not needs_wsl:
        return None
    return SETTINGS.wsl_distribution if wsl_shell_available(SETTINGS.wsl_distribution) else None


def wsl_entry_path(path, distribution):
    # Seul le parent est traduit : résoudre l'entrée suivrait le lien vers sa cible.
    path = Path(path)
    return wsl_execution_path(path.parent, distribution).rstrip("/") + "/" + path.name


def run_wsl_script(distribution, script, arguments, error):
    code, output = run_capture(
        [*wsl_command_prefix(distribution), "sh", "-c", script, "odoo-manager", *arguments],
        cwd=workspace_tool_cwd(),
        timeout=30,
    )
    if code != 0:
        raise RuntimeError(f"{error} : {output.strip()[:300] or f'code {code}'}")
    return output


ADDON_LINK_STATUS_CHUNK = 100  # Paires par appel WSL : reste sous la limite de ligne de commande Windows.


def addon_link_statuses(pairs):
    """États de plusieurs liens en un appel WSL par lot ; voir addon_link_status."""
    pairs = list(pairs)
    distribution = addon_links_wsl_distribution(*{Path(link).parent for link, _target in pairs})
    if distribution is None:
        return [addon_link_status_native(link, target) for link, target in pairs]
    script = (
        'while [ "$#" -gt 1 ]; do link=$1; target=$2; shift 2; '
        'if [ -L "$link" ]; then '
        'if [ "$(readlink -f -- "$link")" = "$(readlink -f -- "$target")" ]; then state=matching; else state=different; fi; '
        'printf "%s\\t%s\\n" "$state" "$(readlink -- "$link")"; '
        'elif [ -e "$link" ]; then printf "other\\t\\n"; else printf "missing\\t\\n"; fi; done'
    )
    statuses = []
    for index in range(0, len(pairs), ADDON_LINK_STATUS_CHUNK):
        chunk = pairs[index:index + ADDON_LINK_STATUS_CHUNK]
        arguments = [
            value
            for link, target in chunk
            for value in (wsl_entry_path(link, distribution), wsl_entry_path(target, distribution))
        ]
        output = run_wsl_script(distribution, script, arguments, "Lecture des liens d'addons impossible depuis WSL")
        lines = output.strip("\n").split("\n") if output.strip() else []
        if len(lines) != len(chunk):
            raise RuntimeError("Réponse incomplète de WSL pendant la lecture des liens d'addons.")
        for line in lines:
            state, _, value = line.partition("\t")
            if state not in {"missing", "matching", "different", "other"}:
                raise RuntimeError(f"Réponse inattendue de WSL pour les liens d'addons : {line[:200]}")
            statuses.append((state, value))
    return statuses


def addon_link_status(link_path, expected_target):
    """Retourne (état, valeur du lien) ; état : missing, matching, different ou other."""
    return addon_link_statuses([(link_path, expected_target)])[0]


def addon_link_status_native(link_path, expected_target):
    if link_path.is_symlink():
        matching = link_path.resolve(strict=False) == Path(expected_target).resolve(strict=False)
        return ("matching" if matching else "different"), os.readlink(link_path)
    return ("other" if link_path.exists() else "missing"), ""


def create_addon_link(link_path, link_value):
    distribution = addon_links_wsl_distribution(link_path.parent, creating=True)
    if distribution is None:
        link_path.symlink_to(link_value, target_is_directory=True)
        return
    # Lien Linux relatif : c'est Odoo, dans Docker, qui le suit.
    run_wsl_script(
        distribution,
        'ln -s -- "$1" "$2"',
        [str(link_value).replace("\\", "/"), wsl_entry_path(link_path, distribution)],
        f"Création du lien {link_path.name} impossible via WSL",
    )


def remove_module_entry(path):
    distribution = addon_links_wsl_distribution(path.parent)
    if distribution is not None:
        run_wsl_script(
            distribution, 'rm -rf -- "$1"', [wsl_entry_path(path, distribution)],
            f"Suppression de {path.name} impossible via WSL",
        )
    elif path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def move_module_entry(source, destination):
    distribution = addon_links_wsl_distribution(source.parent, destination.parent)
    if distribution is None:
        shutil.move(str(source), str(destination))
        return
    run_wsl_script(
        distribution,
        'mv -- "$1" "$2"',
        [wsl_entry_path(source, distribution), wsl_entry_path(destination, distribution)],
        f"Déplacement de {source.name} impossible via WSL",
    )


def managed_storage_link(project, module_name, storage_path):
    link_path = project_addons_link_parent(project) / module_name
    return addon_link_status(link_path, storage_path)[0] == "matching"


def managed_module_copy_ready(project, module_name, storage_path):
    expected_path = project_addons_storage_parent(project) / module_name
    if storage_path.absolute() != expected_path.absolute():
        return False
    if storage_path.is_symlink() or not storage_path.is_dir():
        return False
    return (storage_path / "__manifest__.py").is_file() or (storage_path / "__openerp__.py").is_file()


class ModuleChangeJournal:
    """Ce qu'un import de modules a créé ou remplacé, pour le défaire s'il est arrêté."""

    def __init__(self):
        self.created = []
        self.backups = []

    def rollback(self, job):
        for path in reversed(self.created):
            remove_module_entry(path)
        for target, backup in reversed(self.backups):
            move_module_entry(backup, target)
        job.add("Import arrêté : modules copiés retirés, versions précédentes restaurées.")


def copy_module_to_storage(job, project, module_path, replace_existing=False, journal=None):
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
        if addon_link_status(link_path, storage_path)[0] == "missing":
            raise RuntimeError(
                f"Remplacement refusé pour {module_name}: un dossier existe déjà dans odoo/addons-store sans lien géré."
            )
        backup = backup_existing_module(job, project, storage_path)
        if journal is not None:
            journal.backups.append((storage_path, backup))

    ignore = shutil.ignore_patterns(".git", "__pycache__", "node_modules", ".DS_Store")
    if journal is not None:
        journal.created.append(storage_path)
    shutil.copytree(source_path, storage_path, symlinks=True, ignore=ignore)
    job.add(f"Module copié dans addons-store: {source_path} -> {storage_path}")
    return storage_path


def ensure_relative_module_link(job, project, module_name, storage_path, replace_existing=False, journal=None):
    link_parent = project_addons_link_parent(project)
    link_parent.mkdir(parents=True, exist_ok=True)
    link_path = link_parent / module_name
    link_value = Path(os.path.relpath(storage_path, start=link_parent))

    link_state, current = addon_link_status(link_path, storage_path)
    if link_state != "missing":
        if link_state == "matching":
            if current.startswith("/") or Path(current).is_absolute():
                remove_module_entry(link_path)
                create_addon_link(link_path, link_value)
                job.add(f"Lien converti en relatif: {link_path} -> {link_value}")
                return True
            job.add(f"Déjà lié en relatif: {module_name}")
            return False
        info = module_removal_info(project, link_path)
        if not replace_existing:
            raise RuntimeError(f"Le module existe déjà dans le projet: {link_path}")
        if not info["removable"]:
            can_replace_store_link = (
                link_state == "different"
                and info.get("removal_mode") == "protected_store"
                and managed_module_copy_ready(project, module_name, storage_path)
            )
            if not can_replace_store_link:
                raise RuntimeError(f"Remplacement refusé pour {module_name}: {info['removal_note']}")
            job.add(
                f"Lien fourni par un dépôt remplacé par la copie gérée: {link_path} -> {link_value}. "
                "La source du dépôt est conservée."
            )
        backup = backup_existing_module(job, project, link_path)
        if journal is not None:
            journal.backups.append((link_path, backup))

    if journal is not None:
        journal.created.append(link_path)
    create_addon_link(link_path, link_value)
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
    journal = ModuleChangeJournal()
    try:
        for module_path in candidates:
            module_path = module_path.resolve()
            storage_path = copy_module_to_storage(
                job, project, module_path, replace_existing=replace_existing, journal=journal,
            )
            changed = ensure_relative_module_link(
                job, project, storage_path.name, storage_path, replace_existing=replace_existing, journal=journal,
            )
            if changed:
                linked += 1
            else:
                skipped += 1
    except job_control.JobCancelled:
        journal.rollback(job)
        clear_project_module_cache(project)
        raise

    clear_project_module_cache(project)
    job.add(f"Terminé. Modules préparés: {linked}. Déjà présents: {skipped}.")
    job.add("Installe ou mets à jour le module depuis l'interface.")


def link_module_candidates(job, project, candidates, replace_existing=False):
    install_module_candidates(job, project, candidates, replace_existing=replace_existing)


def wsl_addon_link_targets(project, module_names):
    """Resolve odoo/addons links from WSL, which Windows Python cannot read (WinError 1920)."""
    context = active_workspace_wsl_context()
    distribution = context.distribution if context else SETTINGS.wsl_distribution
    link_parent = wsl_execution_path(project_addons_link_parent(project), distribution).rstrip("/")
    script = (
        'parent=$1; shift; for name do child="$parent/$name"; '
        '[ -L "$child" ] || continue; '
        'target=$(readlink -f -- "$child" 2>/dev/null) || continue; '
        'printf "%s\\t%s\\n" "$name" "$(wslpath -w "$target" 2>/dev/null || printf "%s" "$target")"; '
        'done'
    )
    code, output = run_capture(
        [*wsl_command_prefix(distribution), "sh", "-c", script, "odoo-manager", link_parent, *module_names],
        cwd=workspace_tool_cwd(),
        timeout=30,
    )
    if code != 0:
        raise RuntimeError(output.strip() or "Lecture des liens d'addons impossible depuis WSL.")
    return {
        name: Path(target)
        for name, target in (line.split("\t", 1) for line in output.splitlines() if "\t" in line)
    }


def normalize_module_layout_for_action(job, project, module_names):
    link_parent = project_addons_link_parent(project)
    storage_parent = project_addons_storage_parent(project)
    legacy_storage_parent = project_legacy_addons_storage_parent(project)
    unreadable = []

    for module_name in module_names:
        link_path = link_parent / module_name
        storage_path = storage_parent / module_name
        try:
            present = link_path.exists() or link_path.is_symlink()
        except OSError:
            unreadable.append(module_name)
            continue
        if not present:
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

    if not unreadable:
        return
    # Links written by WSL work for Odoo in Docker; only their layout check needs
    # WSL. Never move or replace what Windows cannot inspect.
    targets = {}
    if platform_id() == "windows":
        try:
            targets = wsl_addon_link_targets(project, unreadable)
        except RuntimeError as exc:
            job.add(f"Liens WSL non vérifiés : {exc}")
    storage_prefix = os.path.normcase(str(storage_parent.resolve())) + os.sep
    for module_name in unreadable:
        target = targets.get(module_name)
        if target is not None and os.path.normcase(str(target)).startswith(storage_prefix):
            continue
        job.add(f"Layout non normalisé pour {module_name}: entrée illisible depuis Windows, laissée inchangée.")


def enterprise_addons_roots(project):
    storage = project_addons_storage_parent(project)
    return tuple(path for path in (storage / "odoo_entreprise", storage / "odoo_enterprise") if path.is_dir())


def ensure_enterprise_module_links(job, project):
    project = validate_project(project)
    roots = enterprise_addons_roots(project)
    if not roots:
        raise RuntimeError(
            "Aucun dossier Odoo Enterprise trouvé dans addons-store/odoo_entreprise ou addons-store/odoo_enterprise."
        )

    creator = ProjectCreator(SETTINGS, WORKSPACE, project_service())
    candidates = {}
    for root in roots:
        for module_path in creator.module_directories(root):
            previous = candidates.get(module_path.name)
            if previous is not None and previous.resolve() != module_path.resolve():
                raise RuntimeError(
                    f"Module Enterprise dupliqué dans plusieurs dossiers : {module_path.name} ({previous} et {module_path})."
                )
            candidates[module_path.name] = module_path
    if not candidates:
        raise RuntimeError("Le dossier Odoo Enterprise ne contient aucun module reconnaissable.")

    link_parent = project_addons_link_parent(project)
    link_parent.mkdir(parents=True, exist_ok=True)
    states = creator.module_link_states(candidates, link_parent)
    conflicts = [name for name, state in states.items() if state == "conflict"]
    provided = sorted(name for name, state in states.items() if state == "provided")
    missing_before = sum(state == "missing" for state in states.values())
    if conflicts:
        raise RuntimeError(
            "Liens Enterprise non modifiés car des dossiers ou liens cassés d'une autre source "
            f"occupent odoo/addons ({len(conflicts)}) : " + ", ".join(sorted(conflicts))
        )
    if provided:
        job.add(
            f"{len(provided)} module(s) Enterprise déjà fourni(s) par une autre source dans odoo/addons, conservé(s) : "
            + ", ".join(provided[:20]) + (" …" if len(provided) > 20 else "")
        )

    for root in roots:
        creator.link_modules(root, link_parent, log=job.add, replace=False)

    states = creator.module_link_states(candidates, link_parent)
    missing_after = [name for name, state in states.items() if state not in {"correct", "provided"}]
    if missing_after:
        raise RuntimeError("Création des liens Enterprise incomplète : " + ", ".join(sorted(missing_after)))
    clear_project_module_cache(project)
    job.add(
        f"Liens Enterprise vérifiés : {len(candidates)} module(s), {missing_before} lien(s) créé(s), aucun conflit."
    )
    return set(candidates)


def repair_enterprise_links_job(job, project):
    ensure_enterprise_module_links(job, project)
    job.add("Vérification des liens symboliques Enterprise terminée.")


def wsl_link_parents(project):
    """Dossiers d'addons qui contiennent des liens WSL ou une conversion interrompue."""
    return [
        parent
        for parent in module_parent_candidates(project)
        if contains_wsl_symlink(parent) or (parent / MIGRATION_JOURNAL_NAME).is_file()
    ]


def addon_links_snapshot(project):
    """État des liens d'addons hérités de WSL, pour proposer leur conversion."""
    project = validate_project(project)
    if platform_id() != "windows" or active_workspace_wsl_context():
        return {"supported": False, "wsl_links": 0, "interrupted": False, "native_symlinks": True}
    parents = wsl_link_parents(project)
    return {
        "supported": True,
        "wsl_links": sum(len(wsl_symlinks(parent)) for parent in parents),
        "interrupted": any((parent / MIGRATION_JOURNAL_NAME).is_file() for parent in parents),
        "native_symlinks": all(native_symlinks_supported(parent) for parent in parents),
    }


def convert_wsl_addon_links_job(job, project):
    project = validate_project(project)
    if platform_id() != "windows":
        raise RuntimeError("La conversion des liens WSL ne concerne que Windows.")
    if active_workspace_wsl_context():
        raise RuntimeError("Le dossier des projets est dans WSL : ses liens Linux sont attendus et restent inchangés.")
    parents = wsl_link_parents(project)
    if not parents:
        job.add("Aucun lien WSL à convertir : les modules sont déjà lus directement par Windows.")
        return
    if container_status(f"odoo-{project}") == "running":
        raise RuntimeError("Arrête le projet avant de convertir ses liens : Odoo lit ces dossiers pendant son exécution.")
    if not all(native_symlinks_supported(parent) for parent in parents):
        raise RuntimeError(
            "Windows refuse la création de liens symboliques. Active le mode développeur "
            "(Paramètres > Système > Espace développeurs), puis relance la conversion."
        )

    converted = 0
    problems = []
    for parent in parents:
        job.add(f"Conversion des liens WSL de {parent}...")
        result = convert_wsl_symlinks(parent, log=job.add)
        converted += result["converted"]
        problems.extend(f"{name} : {reason}" for name, reason in (*result["skipped"], *result["failures"]))
    clear_project_module_cache(project)
    job.add(f"{converted} lien(s) converti(s) en liens Windows relatifs, lisibles par Windows et par Docker.")
    if problems:
        raise RuntimeError(
            f"{len(problems)} lien(s) non converti(s), relance la conversion après vérification : "
            + " ; ".join(problems[:10]) + (" …" if len(problems) > 10 else "")
        )
    job.add("La liste des modules est désormais lue directement par Windows, sans WSL.")


def read_manifest_dict(path):
    # Odoo lit lui-même le manifeste avec ast.literal_eval : même règle ici.
    for filename in ("__manifest__.py", "__openerp__.py"):
        try:
            text = (Path(path) / filename).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        return parse_manifest_text(text)
    return {}


def parse_manifest_text(text):
    try:
        manifest = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def manifest_graph_entry(manifest):
    depends = sorted({name for name in manifest.get("depends") or () if isinstance(name, str)})
    auto_install = manifest.get("auto_install", False)
    if isinstance(auto_install, (list, tuple, set)):
        # Odoo 16+ : seules ces dépendances déclenchent l'installation automatique.
        triggers = sorted({name for name in auto_install if isinstance(name, str)})
        auto_install = True
    else:
        triggers = depends
        auto_install = bool(auto_install)
    return {
        "title": str(manifest.get("name") or ""),
        "depends": depends,
        "auto_install": auto_install,
        "auto_install_triggers": triggers,
        "installable": bool(manifest.get("installable", True)),
        "application": bool(manifest.get("application", False)),
        # L'installation auto dépend alors du pays des sociétés : non prévisible ici.
        "country_restricted": bool(manifest.get("countries")),
    }


def module_graph_from_paths(paths):
    graph = {}
    for path in paths:
        name = posixpath.basename(str(path).replace("\\", "/"))
        if name not in graph:
            graph[name] = manifest_graph_entry(read_manifest_dict(path))
    return graph


# Marqueur textuel : un séparateur de contrôle comme \x1e est retiré par str.strip().
MANIFEST_RECORD_MARKER = "@@odoo-manager-manifest@@ "


def wsl_module_graph(project):
    """Lit tous les manifestes depuis WSL en une commande.

    Windows ne traverse pas les liens symboliques créés par WSL dans odoo/addons
    (WinError 1920) : lus depuis Windows, ces manifestes semblaient vides et le
    graphe perdait toutes ses dépendances.
    """
    distribution = active_wsl_distribution()
    linux_candidates = [wsl_execution_path(path, distribution) for path in module_parent_candidates(project)]
    script = (
        'found_parent=0; for parent do [ -d "$parent" ] && found_parent=1; done; '
        '[ "$found_parent" -eq 1 ] || { echo "Aucun dossier addons lisible depuis WSL." >&2; exit 3; }; '
        'for parent do [ -d "$parent" ] || continue; '
        'find "$parent" -mindepth 1 -maxdepth 1 \\( -type d -o -type l \\) -print 2>/dev/null | '
        'while IFS= read -r child; do '
        'for manifest in "$child/__manifest__.py" "$child/__openerp__.py"; do '
        '[ -f "$manifest" ] || continue; '
        'printf "\\n%s%s\\n" "$marker" "${child##*/}"; cat -- "$manifest" 2>/dev/null; break; '
        'done; done; done'
    )
    code, output = run_capture(
        [*wsl_command_prefix(distribution), "sh", "-c", 'marker=$1; shift; ' + script, "odoo-manager",
         MANIFEST_RECORD_MARKER, *linux_candidates],
        cwd=workspace_tool_cwd(),
        timeout=60,
    )
    if code != 0:
        detail = "délai dépassé" if code == 124 else (output.strip()[:300] or f"code {code}")
        raise RuntimeError(f"Impossible de lire les manifestes des modules depuis WSL : {detail}")
    graph = {}
    for record in ("\n" + output).split("\n" + MANIFEST_RECORD_MARKER)[1:]:
        name, _, text = record.partition("\n")
        name = name.strip()
        if not SAFE_MODULE_RE.fullmatch(name) or name in graph:
            continue
        graph[name] = manifest_graph_entry(parse_manifest_text(text))
    return graph


def project_module_graph(project):
    if active_workspace_wsl_context():
        return wsl_module_graph(project)
    if project_reads_modules_through_wsl(project):
        try:
            return wsl_module_graph(project)
        except RuntimeError:
            pass  # Même repli que module_dirs lorsque WSL est momentanément indisponible.
    return module_graph_from_paths(module_dirs(project))


def module_dependency_graph(project):
    now = time.time()
    with MODULE_CACHE_LOCK:
        cached = MODULE_GRAPH_CACHE.get(project)
        if cached and now - cached["created_at"] < MODULE_GRAPH_TTL_SECONDS:
            return cached["graph"]
    graph = project_module_graph(project)
    with MODULE_CACHE_LOCK:
        MODULE_GRAPH_CACHE[project] = {"created_at": now, "graph": graph}
    return graph


def module_install_plan(graph, states, requested):
    """Reproduit la résolution d'Odoo pour `-i` : dépendances récursives puis
    modules auto_install dont un déclencheur passe à l'état « to install »."""
    installed = {name for name, info in states.items() if info.get("state") in INSTALLED_MODULE_STATES}
    installed.add("base")
    requested = list(dict.fromkeys(requested))
    to_install = {}
    missing = {}
    uninstallable = {}
    auto_installed = set()

    def add(name, required_by):
        stack = [(name, required_by)]
        while stack:
            current, parent = stack.pop()
            if current in installed or current in to_install:
                continue
            entry = graph.get(current)
            if entry is None:
                missing.setdefault(current, parent)
                continue
            if not entry["installable"]:
                uninstallable.setdefault(current, parent)
                continue
            to_install[current] = parent
            stack.extend((dependency, current) for dependency in entry["depends"])

    for name in requested:
        add(name, "")

    candidates = sorted(
        name
        for name, entry in graph.items()
        if entry["auto_install"] and entry["installable"] and entry["auto_install_triggers"]
        and not entry["country_restricted"]
    )
    changed = True
    while changed:
        changed = False
        for name in candidates:
            if name in installed or name in to_install:
                continue
            triggers = graph[name]["auto_install_triggers"]
            satisfied = all(trigger in installed or trigger in to_install for trigger in triggers)
            if satisfied and any(trigger in to_install for trigger in triggers):
                auto_installed.add(name)
                add(name, "")
                changed = True

    requested_set = set(requested)
    new_modules = [name for name in to_install if name not in requested_set]

    def described(names):
        return [{"name": name, "title": graph.get(name, {}).get("title") or name} for name in sorted(names)]

    return {
        "requested": [name for name in requested if name in to_install],
        "already_installed": [name for name in requested if name in installed],
        "dependencies": described(name for name in new_modules if name not in auto_installed),
        "auto_installed": described(name for name in new_modules if name in auto_installed),
        "applications": described(name for name in new_modules if graph[name]["application"]),
        "missing": [{"name": name, "required_by": parent} for name, parent in sorted(missing.items())],
        "uninstallable": [{"name": name, "required_by": parent} for name, parent in sorted(uninstallable.items())],
        "total": len(to_install),
    }


def socle_preset_modules(preset_ids):
    return list(dict.fromkeys(
        module_name
        for preset_id in preset_ids
        for module_name in SOCLE_PRESETS[preset_id][1]
    ))


def socle_catalog(project, db_name):
    graph = module_dependency_graph(project)
    states = installed_modules(project, db_name) if db_name else {}
    apps = []
    for app_id, label, section, modules in SOCLE_APPS:
        missing = [name for name in modules if name not in graph]
        installed = [name for name in modules if states.get(name, {}).get("state") == "installed"]
        extra = 0
        if not missing and len(installed) < len(modules):
            plan = module_install_plan(graph, states, modules)
            extra = len(plan["dependencies"]) + len(plan["auto_installed"])
        apps.append({
            "id": app_id,
            "label": label,
            "section": section,
            "modules": list(modules),
            "missing": missing,
            "installed_modules": installed,
            "extra_count": extra,
        })
    return {
        "sections": [{"id": section_id, "label": label} for section_id, label in SOCLE_SECTIONS],
        "apps": apps,
        "states_available": bool(states),
    }


def socle_install_plan(project, db_name, presets):
    graph = module_dependency_graph(project)
    states = installed_modules(project, db_name)
    return module_install_plan(graph, states, socle_preset_modules(validate_socle_presets(presets)))


def log_module_install_plan(job, plan):
    if plan["dependencies"]:
        job.add(f"Dépendances installées en plus ({len(plan['dependencies'])}) : "
                + ", ".join(item["name"] for item in plan["dependencies"]))
    if plan["auto_installed"]:
        job.add(f"Modules installés automatiquement par Odoo ({len(plan['auto_installed'])}) : "
                + ", ".join(item["name"] for item in plan["auto_installed"]))


def install_socle_job(job, project, db_name, presets):
    project = validate_project(project)
    db_name = validate_odoo_db(db_name)
    preset_ids = validate_socle_presets(presets)
    requested_modules = socle_preset_modules(preset_ids)

    job.add("Vérification et création des liens symboliques Odoo Enterprise...")
    ensure_enterprise_module_links(job, project)
    graph = project_module_graph(project)
    missing = [name for name in requested_modules if name not in graph]
    if missing:
        raise RuntimeError("Modules requis absents du projet : " + ", ".join(missing))

    states = installed_modules(project, db_name)
    pending = [name for name in requested_modules if states.get(name, {}).get("state") != "installed"]
    already_installed = [name for name in requested_modules if name not in pending]
    if already_installed:
        job.add("Modules déjà installés : " + ", ".join(already_installed))
    if not pending:
        job.add("Le socle sélectionné est déjà entièrement installé.")
        return
    plan = module_install_plan(graph, states, pending)
    blocking = plan["missing"] + plan["uninstallable"]
    if blocking:
        raise RuntimeError(
            "Dépendances introuvables ou non installables dans le projet : "
            + ", ".join(f"{item['name']} (requis par {item['required_by']})" for item in blocking)
        )
    job.add("Installation du socle: " + ", ".join(pending))
    log_module_install_plan(job, plan)
    module_command_job(job, "--install-module", project, db_name, ",".join(pending))


def module_command_job(job, flag, project, db_name, modules, overwrite_translations=False):
    project = validate_project(project)
    db_name = validate_odoo_db(db_name)
    module_names = [name.strip() for name in validate_modules(modules).split(",") if name.strip()]
    if not module_names:
        raise RuntimeError("Aucun module fourni.")

    if flag in ("--install-module", "--update-module"):
        normalize_module_layout_for_action(job, project, module_names)

    if flag == "--uninstall-module":
        project_service().run_odoo_uninstall_command(
            project,
            db_name,
            ",".join(module_names),
            log=job.add,
        )
        return
    if flag not in ("--install-module", "--update-module"):
        raise ValueError("Action module Odoo inconnue.")

    try:
        project_service().run_odoo_module_command(
            project,
            db_name,
            ",".join(module_names),
            option="-i" if flag == "--install-module" else "-u",
            log=job.add,
            overwrite_translations=overwrite_translations,
        )
    except RuntimeError as exc:
        hint = missing_code_failure_hint(project, db_name, str(exc))
        if hint:
            job.add(hint)
            raise RuntimeError(f"{exc} {hint}") from exc
        raise


# Erreurs Odoo typiques d'une base qui référence le code d'un module absent du projet.
MISSING_CODE_ERROR_RE = re.compile(
    r"n'existe pas|does not exist|non-existing model|External ID not found|No module named|KeyError",
    re.IGNORECASE,
)


def missing_code_failure_hint(project, db_name, message):
    """Désigne les modules installés sans code quand l'échec Odoo porte sur un champ ou modèle absent."""
    if not MISSING_CODE_ERROR_RE.search(message):
        return ""
    try:
        states = installed_modules(project, db_name)
        available = {path.name for path in module_dirs(project)}
    except (OSError, RuntimeError):
        return ""
    missing = sorted(
        set(modules_missing_from_code(states, available, ACTIVE_MODULE_STATES)) - ignored_missing_modules(project, db_name)
    )
    if not missing:
        return ""
    shown = ", ".join(missing[:8]) + (f" et {len(missing) - 8} autre(s)" if len(missing) > 8 else "")
    return (
        f"Cause probable : la base {db_name} référence des modules installés dont le code est absent du projet "
        f"({shown}). Restaure leur code dans le projet (liste complète dans l'onglet Diagnostic), puis relance."
    )


def update_imported_modules_job(job, project, db_name, modules):
    project = validate_project(project)
    db_name = validate_odoo_db(db_name)
    requested = module_name_list(modules)
    available = {path.name for path in module_dirs(project)}
    missing = sorted(set(requested) - available)
    if missing:
        raise RuntimeError("Modules importés introuvables dans le projet : " + ", ".join(missing))

    restored_exclusions = sorted(set(requested) & ignored_missing_modules(project, db_name))
    if restored_exclusions:
        forget_ignored_missing_modules(project, db_name, restored_exclusions)
        job.add("Code restauré, exclusions locales retirées : " + ", ".join(restored_exclusions))

    states = installed_modules(project, db_name)
    to_update = [name for name in requested if states.get(name, {}).get("state") in {"installed", "to upgrade"}]
    to_install = [name for name in requested if name not in to_update]
    if to_install:
        job.add("Nouveaux modules à installer : " + ", ".join(to_install))
        module_command_job(job, "--install-module", project, db_name, ",".join(to_install))
    if to_update:
        job.add("Modules existants à mettre à jour : " + ", ".join(to_update))
        module_command_job(job, "--update-module", project, db_name, ",".join(to_update))
    job.result = {"kind": "module_update", "scope": "imported", "modules": requested}


def update_all_modules_job(job, project, db_name, modules):
    module_command_job(job, "--update-module", project, db_name, modules)
    job.result = {"kind": "module_update", "scope": "all", "modules": []}


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
            module_command_job(job, "--uninstall-module", project, db_name, ",".join(installed))
        else:
            job.add(f"Aucun module sélectionné n'est installé dans {db_name}; suppression du code uniquement.")
    else:
        job.add("Désinstallation Odoo non demandée; suppression du code uniquement.")

    removed = 0
    with job_control.protected("suppression du code des modules", irreversible=True):
        for module_name in module_names:
            if delete_module_file_entry(job, project, module_name):
                removed += 1

    clear_project_module_cache(project)
    job.add(f"Suppression terminée. Entrées retirées de odoo/addons: {removed}.")
    job.add(f"Emplacement de récupération: {DELETED_MODULES / project}")


def create_project_job(
    job,
    name,
    version,
    source_type,
    repository_url,
    repository_branch,
    rika_instance,
    rika_login,
    rika_password,
    start_after_creation,
):
    creator = ProjectCreator(SETTINGS, WORKSPACE, project_service())
    job_control.on_cancel(f"retrait du projet {name} créé par cette action", lambda: discard_created_project(job, name))
    creator.create(
        name,
        version,
        source_type=source_type,
        repository_url=repository_url,
        repository_branch=repository_branch,
        rika_instance=rika_instance,
        rika_login=rika_login,
        rika_password=rika_password,
        log=job.add,
    )

    if not start_after_creation:
        job.add("Le projet est prêt. Tu peux le démarrer depuis le gestionnaire.")
        return

    docker = docker_status(SETTINGS)
    if not docker["running"]:
        job.add("Docker n'est pas disponible: le projet a été créé mais n'a pas été démarré.")
        return

    current_traefik = traefik_status(docker)
    if not current_traefik["installed"] and not current_traefik["running"]:
        job.add("Traefik est absent. Installation automatique avant le premier démarrage...")
        install_traefik_job(job)
    project_service().start_project(name, log=job.add)


def discard_created_project(job, name):
    path = WORKSPACE / name
    if not (path.exists() or path.is_symlink()):
        job.add(f"Aucun dossier {name} créé : rien à retirer.")
        return
    if any((path / compose).is_file() for compose in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")):
        docker_ok, _message = docker_available()
        if docker_ok:
            run_stream(job, docker_command(SETTINGS, "compose", "down"), cwd=path)
    DELETED_PROJECTS.mkdir(parents=True, exist_ok=True)
    destination = unique_child(DELETED_PROJECTS, f"{time.strftime('%Y%m%d_%H%M%S')}_{name}_creation_interrompue")
    shutil.move(str(path), str(destination))
    clear_project_module_cache(name)
    job.add(f"Projet partiellement créé déplacé dans : {destination}")


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
    move_module_entry(target, backup)
    job.add(f"Module existant sauvegardé: {target} -> {backup}")
    return backup


def link_modules_job(job, project, source):
    project = validate_project(project)
    source_path = Path(source).expanduser().resolve()
    if not source_path.exists() or not source_path.is_dir():
        raise RuntimeError(f"Dossier introuvable: {source_path}")
    link_module_candidates(job, project, find_module_candidates(source_path))


def validate_module_repository(url, branch, modules, commit=""):
    url = validate_gitlab_repository(url)
    branch = validate_git_ref(branch)
    names = module_name_list(modules) if modules else []
    if not names:
        raise ValueError("Sélectionne au moins un module à importer.")
    commit = str(commit or "").strip()
    if commit and not REPOSITORY_COMMIT_RE.fullmatch(commit):
        raise ValueError("Commit d’analyse invalide.")
    return url, branch, names, commit


REPOSITORY_SKIPPED_DIRS = frozenset({".git", "__pycache__", "node_modules"})
MANIFEST_FILENAMES = ("__manifest__.py", "__openerp__.py")


def repository_modules_from_tree(tree_output, repository_name):
    """Modules d'un `git ls-tree -r` selon la règle de find_module_candidates.

    Un manifeste à la racine fait du dépôt un module unique ; sinon chaque dossier portant
    un manifeste est un module, sans descendre dans ses sous-dossiers.
    """
    manifest_dirs = set()
    has_symlinks = False
    for line in str(tree_output or "").splitlines():
        meta, _, path = line.partition("\t")
        if not path:
            continue
        if meta.split(" ", 1)[0] == "120000":
            has_symlinks = True
        if posixpath.basename(path) in MANIFEST_FILENAMES:
            manifest_dirs.add(posixpath.dirname(path))
    modules = {}
    for directory in sorted(manifest_dirs, key=lambda item: (item.count("/") if item else -1, item)):
        parts = directory.split("/") if directory else []
        if any(part in REPOSITORY_SKIPPED_DIRS for part in parts):
            continue
        if "" in modules:
            break
        if any("/".join(parts[:index]) in modules for index in range(1, len(parts))):
            continue
        modules[directory] = parts[-1] if parts else repository_name
    return modules, has_symlinks


def module_provided_by_project(project, name):
    """Module standard, Enterprise ou ancien stockage portant ce nom, sans scanner le projet.

    module_dirs() lance la détection et le scan WSL sous Windows : trop coûteux pour
    vérifier quelques noms, et source de processus inattendus pendant un import.
    """
    base = project_odoo_root(project)
    parents = (
        project_legacy_addons_storage_parent(project),
        base / "odoo" / "odoo" / "addons",
        base / "addons-store" / "odoo_entreprise",
        base / "addons-store" / "odoo_enterprise",
    )
    for parent in parents:
        for manifest in MANIFEST_FILENAMES:
            try:
                if (parent / name / manifest).is_file():
                    return True
            except OSError:
                # Lien créé par WSL illisible depuis Windows : on le considère présent par prudence.
                return True
    return False


REPOSITORY_GIT_OPTIONS = (
    "-c", "core.sshCommand=ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
    "-c", "protocol.allow=never", "-c", "protocol.ssh.allow=always",
)
REPOSITORY_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_MANIFEST_BYTES = 512 * 1024
ODOO_SERIES_VERSION_RE = re.compile(r"^(\d+\.\d+)\.\d+\.\d+\.\d+$")


def manifest_version_key(version):
    parts = re.findall(r"\d+", str(version or ""))
    return tuple(int(part) for part in parts) if parts else None


def read_repository_manifest(module_path):
    for filename in MANIFEST_FILENAMES:
        manifest = Path(module_path) / filename
        try:
            if manifest.is_symlink() or not manifest.is_file():
                continue
            if manifest.stat().st_size > MAX_MANIFEST_BYTES:
                return {}
            return parse_manifest_text(manifest.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return {}
    return {}


def repository_module_plans(project, modules, states, odoo_version):
    """Décide, module par module, ce qu'un import ferait : add, update ou blocked.

    Même règle pour l'aperçu et pour l'import, qui la réapplique sur le commit
    réellement récupéré avant toute modification du projet.
    `modules` : dicts name, path, manifest, has_symlink, duplicate.
    """
    storage_parent = project_addons_storage_parent(project)
    link_parent = project_addons_link_parent(project)
    valid = [module for module in modules if SAFE_MODULE_RE.fullmatch(module["name"]) and "," not in module["name"]]
    link_states = dict(zip(
        (module["name"] for module in valid),
        addon_link_statuses((link_parent / module["name"], storage_parent / module["name"]) for module in valid),
    ))
    plans = []
    for module in modules:
        name = module["name"]
        manifest = module.get("manifest") or {}
        version = str(manifest.get("version") or "")
        state = states.get(name, {})
        plan = {
            "name": name,
            "path": module.get("path") or ".",
            "title": str(manifest.get("name") or ""),
            "version": version,
            "current_version": "",
            "installed_version": state.get("installed_version", ""),
            "state": state.get("state", ""),
            "action": "blocked",
            "reason": "",
            "warning": "",
        }
        plans.append(plan)
        if name not in link_states:
            plan["reason"] = "Nom de module invalide."
            continue
        if module.get("duplicate"):
            plan["reason"] = "Présent plusieurs fois dans le dépôt."
            continue
        if module.get("has_symlink"):
            plan["reason"] = "Contient des liens symboliques."
            continue
        if manifest and not manifest.get("installable", True):
            plan["reason"] = "Marqué non installable dans son manifeste."
            continue
        series = ODOO_SERIES_VERSION_RE.match(version)
        if series and odoo_version and series.group(1) != odoo_version:
            plan["reason"] = f"Prévu pour Odoo {series.group(1)}, le projet est en Odoo {odoo_version}."
            continue

        storage = storage_parent / name
        link_state, link_value = link_states[name]
        if link_state == "matching" and managed_module_copy_ready(project, name, storage):
            plan["action"] = "update"
            plan["current_version"] = str(read_manifest_dict(storage).get("version") or "")
            current_key, new_key = manifest_version_key(plan["current_version"]), manifest_version_key(version)
            if current_key and new_key and new_key < current_key:
                plan["warning"] = f"Version plus ancienne que celle du projet ({plan['current_version']})."
        elif link_state == "different":
            source = posixpath.dirname(posixpath.normpath(link_value.replace("\\", "/"))).lstrip("./")
            plan["reason"] = f"Déjà fourni par {source}." if source else "Déjà fourni par un autre dossier du projet."
        elif link_state == "other":
            plan["reason"] = "Un dossier non géré occupe odoo/addons."
        elif safe_path_exists(storage) or storage.is_symlink():
            plan["reason"] = "Présent dans addons-store sans lien géré par le manager."
        elif module_provided_by_project(project, name):
            plan["reason"] = "Fourni par Odoo standard ou Enterprise : une copie le masquerait."
        else:
            plan["action"] = "add"
            if plan["state"] in ACTIVE_MODULE_STATES:
                plan["warning"] = "Installé en base mais code absent : l’import le rétablit."
    return plans


def inspect_repository_modules(project, url, branch, db_name=""):
    project = validate_project(project)
    url = validate_gitlab_repository(url)
    branch = validate_git_ref(branch)
    creator = ProjectCreator(SETTINGS, WORKSPACE, project_service())
    staging = project_staging_imports_root(project)
    staging.mkdir(parents=True, exist_ok=True)
    repository_name = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
    with tempfile.TemporaryDirectory(prefix="repository-inspect-", dir=staging) as temporary:
        checkout = Path(temporary) / (repository_name or "repository")
        # Clone sans contenu : seuls les manifestes sont téléchargés ensuite.
        clone = creator.git(
            *REPOSITORY_GIT_OPTIONS,
            "clone", "--depth", "1", "--filter=blob:none", "--no-checkout",
            "--single-branch", "--branch", branch, "--", url, creator.command_path(checkout),
        )
        code, output = creator.project_service.capture(clone, cwd=creator.command_cwd, timeout=180)
        if code:
            raise repository_clone_error(output)
        git_dir = creator.command_path(checkout)
        code, commit = creator.project_service.capture(creator.git("-C", git_dir, "rev-parse", "HEAD"), cwd=creator.command_cwd, timeout=30)
        commit = commit.strip()
        if code or not REPOSITORY_COMMIT_RE.fullmatch(commit):
            raise RuntimeError("Lecture du commit de la branche impossible.")
        code, tree = creator.project_service.capture(
            creator.git("-C", git_dir, "ls-tree", "-r", "--full-tree", "HEAD"), cwd=creator.command_cwd, timeout=60,
        )
        if code:
            raise RuntimeError("Lecture de l’arborescence du dépôt impossible.")
        found, symlink_paths = repository_tree_modules(tree, repository_name)
        manifests = {}
        if found:
            # Checkout clairsemé limité aux manifestes : Git récupère ces fichiers en un seul
            # échange, là où `checkout -- chemins` les téléchargeait un par un (50 s pour 20 modules).
            sparse = checkout / ".git" / "info" / "sparse-checkout"
            sparse.parent.mkdir(parents=True, exist_ok=True)
            sparse.write_text(
                "".join(sparse_checkout_pattern(posixpath.join(path, filename) if path else filename)
                        for path, _name, filename in found),
                encoding="utf-8",
            )
            fetch = creator.git(*REPOSITORY_GIT_OPTIONS, "-c", "core.sparseCheckout=true", "-C", git_dir, "checkout", "-q", "HEAD")
            code, _ = creator.project_service.capture(fetch, cwd=creator.command_cwd, timeout=180)
            if code == 0:
                manifests = {path: read_repository_manifest(checkout / path if path else checkout) for path, _name, _file in found}
    names = [name for _path, name, _file in found]
    modules = [
        {
            "name": name,
            "path": path,
            "manifest": manifests.get(path, {}),
            "has_symlink": any(link == path or not path or link.startswith(path + "/") for link in symlink_paths),
            "duplicate": names.count(name) > 1,
        }
        for path, name, _file in sorted(found, key=lambda item: (item[1].lower(), item[0]))
    ]
    states = installed_modules(project, db_name) if db_name else {}
    odoo_version = project_odoo_version(project)
    return {
        "commit": commit,
        "odoo_version": odoo_version,
        "manifests_read": bool(manifests),
        "modules": repository_module_plans(project, modules, states, odoo_version),
    }


def sparse_checkout_pattern(path):
    """Motif sparse-checkout ne désignant que ce chemin exact, caractères spéciaux échappés."""
    escaped = "".join("\\" + char if char in "*?[\\" else char for char in path)
    return "/" + escaped + "\n"


def repository_tree_modules(tree_output, repository_name):
    """Liste (chemin, nom, manifeste) et les liens symboliques d'un `git ls-tree -r`."""
    found, has_symlinks = repository_modules_from_tree(tree_output, repository_name)
    manifest_files = {}
    symlinks = []
    for line in str(tree_output or "").splitlines():
        meta, _, path = line.partition("\t")
        if not path:
            continue
        if meta.split(" ", 1)[0] == "120000":
            symlinks.append(path)
        directory, filename = posixpath.dirname(path), posixpath.basename(path)
        if filename in MANIFEST_FILENAMES and directory in found:
            manifest_files.setdefault(directory, filename)
    return [(path, name, manifest_files.get(path, MANIFEST_FILENAMES[0])) for path, name in found.items()], symlinks


def repository_clone_error(stderr):
    details = str(stderr or "").casefold()
    if any(marker in details for marker in (
            "permission denied (publickey)", "no such identity", "sign_and_send_pubkey")):
        return RuntimeError(
            "GitLab refuse la clé SSH de cet ordinateur. Ouvre l’assistant Clé SSH du manager, "
            "puis vérifie que sa clé publique est autorisée dans GitLab."
        )
    if "host key verification failed" in details:
        return RuntimeError("L’identité du serveur GitLab n’a pas pu être vérifiée par SSH.")
    if any(marker in details for marker in (
            "remote branch", "couldn't find remote ref", "could not find remote branch",
            "not found in upstream origin")):
        return RuntimeError("La branche ou le tag demandé est introuvable dans ce dépôt.")
    if any(marker in details for marker in (
            "could not resolve hostname", "failed to connect", "connection timed out", "connection refused")):
        return RuntimeError("GitLab est inaccessible depuis cet ordinateur. Vérifie le réseau et le DNS.")
    return RuntimeError(
        "Récupération Git impossible. Vérifie l’URL SSH, la branche et l’autorisation de la clé dans GitLab."
    )


def repository_modules_job(job, project, url, branch, names, commit=""):
    """Import des modules choisis : ajout ou remplacement décidé par module, tout ou rien."""
    project = validate_project(project)
    if not names:
        raise ValueError("Sélectionne au moins un module à importer.")
    creator = ProjectCreator(SETTINGS, WORKSPACE, project_service())
    staging = project_staging_imports_root(project)
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="repository-", dir=staging) as temporary:
        checkout = Path(temporary) / url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
        job.add(f"Récupération du dépôt {url}, branche {branch}…")
        command = creator.git(
            *REPOSITORY_GIT_OPTIONS,
            "clone", "--depth", "1", "--single-branch", "--branch", branch, "--", url, creator.command_path(checkout),
        )
        code, output = creator.project_service.capture(command, cwd=creator.command_cwd, timeout=300)
        if code:
            raise repository_clone_error(output)
        if commit:
            code, head = creator.project_service.capture(
                creator.git("-C", creator.command_path(checkout), "rev-parse", "HEAD"), cwd=creator.command_cwd, timeout=30,
            )
            if code or head.strip() != commit:
                raise ValueError(
                    f"La branche {branch} a reçu de nouveaux commits depuis l’analyse. "
                    "Rouvre l’import pour vérifier les modules avant de les copier."
                )
            job.add(f"Commit vérifié : {commit[:12]}")

        by_name = {}
        for candidate in find_module_candidates(checkout):
            by_name.setdefault(candidate.name, []).append(candidate)
        names = list(dict.fromkeys(names))
        missing = sorted(set(names) - by_name.keys())
        if missing:
            raise ValueError("Modules absents du dépôt : " + ", ".join(missing))
        selected = [by_name[name][0] for name in names]
        plans = repository_module_plans(
            project,
            [
                {
                    "name": candidate.name,
                    "path": str(candidate.relative_to(checkout)),
                    "manifest": read_repository_manifest(candidate),
                    # Liens refusés dans les modules copiés : ils pourraient pointer hors du dépôt.
                    "has_symlink": candidate.is_symlink() or any(path.is_symlink() for path in candidate.rglob("*")),
                    "duplicate": len(by_name[candidate.name]) > 1,
                }
                for candidate in selected
            ],
            {},
            project_odoo_version(project),
        )
        blocked = [plan for plan in plans if plan["action"] == "blocked"]
        if blocked:
            raise ValueError(
                "Import annulé avant toute modification :\n"
                + "\n".join(f"• {plan['name']} : {plan['reason']}" for plan in blocked)
            )
        for plan in plans:
            if plan["warning"]:
                job.add(f"Attention, {plan['name']} : {plan['warning']}")

        storage, links = project_addons_storage_parent(project), project_addons_link_parent(project)
        added = [plan["name"] for plan in plans if plan["action"] == "add"]
        updated = [plan["name"] for plan in plans if plan["action"] == "update"]
        job.add(
            f"Modules à ajouter ({len(added)}) : {', '.join(added) or '-'} · "
            f"à mettre à jour ({len(updated)}) : {', '.join(updated) or '-'}"
        )
        backups, created = [], []
        try:
            for candidate, plan in zip(selected, plans):
                target, link = storage / candidate.name, links / candidate.name
                if plan["action"] == "update":
                    backups.append((target, backup_existing_module(job, project, target)))
                # Seul ce que l'import crée est retiré en cas d'échec, jamais un dossier déjà présent.
                if not (target.exists() or target.is_symlink()):
                    created.append(target)
                copy_module_to_storage(job, project, candidate)
                if plan["action"] == "add" and addon_link_status(link, target)[0] == "missing":
                    created.append(link)
                ensure_relative_module_link(job, project, candidate.name, target)
        except BaseException:
            for path in reversed(created):
                remove_module_entry(path)
            for target, backup in reversed(backups):
                move_module_entry(backup, target)
            job.add("Import annulé ; les versions précédentes ont été restaurées.")
            raise
        finally:
            clear_project_module_cache(project)
        job.add(f"Code préparé : {len(plans)} module(s), source {url}, branche {branch}.")
        job.add("Installe ou mets à jour ces modules dans la base Odoo depuis l’interface.")
        job.result = {"kind": "repository_modules", "modules": names, "added": added, "updated": updated}


def safe_import_name(filename):
    stem = Path(filename or "modules").stem or "modules"
    return SAFE_IMPORT_NAME_RE.sub("_", stem).strip("._") or "modules"


def safe_extract_zip(zip_path, destination):
    destination.mkdir(parents=True, exist_ok=True)
    base = destination.resolve()
    skipped_links = []
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise RuntimeError(f"ZIP trop volumineux: plus de {MAX_ZIP_ENTRIES} entrées.")
        if sum(info.file_size for info in infos) > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise RuntimeError("ZIP trop volumineux après décompression. Limite: 2 Go.")
        for info in infos:
            name = info.filename
            if not name or name.startswith(("/", "\\")):
                raise RuntimeError(f"Chemin ZIP invalide: {name}")
            if "\\" in name or "\x00" in name or re.match(r"^[A-Za-z]:", name):
                raise RuntimeError(f"Chemin ZIP invalide: {name}")
            parts = Path(name).parts
            if any(part == ".." for part in parts):
                raise RuntimeError(f"Chemin ZIP dangereux: {name}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                skipped_links.append(name)
                continue
            if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise RuntimeError(f"Type de fichier ZIP non pris en charge: {name}")
            target = (destination / name).resolve()
            if base != target and base not in target.parents:
                raise RuntimeError(f"Extraction hors dossier refusee: {name}")
        for info in infos:
            if info.filename in skipped_links:
                continue
            archive.extract(info, destination)
    return skipped_links


def extract_zip_module_candidates(project, filename, data):
    project = validate_project(project)
    if not filename.lower().endswith(".zip"):
        raise RuntimeError("Le fichier doit etre un ZIP.")
    if not data:
        raise RuntimeError("Fichier ZIP vide.")

    imports_root = project_staging_imports_root(project)
    imports_root.mkdir(parents=True, exist_ok=True)
    while True:
        import_dir = unique_child(imports_root, safe_import_name(filename))
        try:
            import_dir.mkdir()
            break
        except FileExistsError:
            continue
    zip_path = import_dir.with_suffix(".zip")

    try:
        zip_path.write_bytes(data)
        skipped_links = safe_extract_zip(zip_path, import_dir)
        candidates = find_module_candidates(import_dir)
        names = [candidate.name for candidate in candidates]
        seen = set()
        duplicates = set()
        for name in names:
            if name in seen:
                duplicates.add(name)
            seen.add(name)
        duplicates = sorted(duplicates)
        if duplicates:
            raise RuntimeError(
                "Modules en double dans le ZIP: " + ", ".join(duplicates)
            )
        return import_dir, candidates, skipped_links
    except Exception:
        shutil.rmtree(import_dir, ignore_errors=True)
        raise
    finally:
        try:
            zip_path.unlink()
        except OSError:
            pass


def inspect_zip_modules(project, filename, data):
    import_dir, candidates, skipped_links = extract_zip_module_candidates(project, filename, data)
    try:
        return {
            "modules": [candidate.name for candidate in candidates],
            "ignored_symlinks": len(skipped_links),
        }
    finally:
        shutil.rmtree(import_dir, ignore_errors=True)


def import_zip_modules_job(job, project, filename, data, replace_existing=False, selected_modules=None):
    project = validate_project(project)
    job.add(f"Import ZIP: {filename}")
    job.add(f"Projet cible: {project}")
    if replace_existing:
        job.add("Mode remplacement: actif. Les modules existants seront sauvegardés avant remplacement.")

    import_dir = None
    try:
        import_dir, candidates, skipped_links = extract_zip_module_candidates(project, filename, data)
        if skipped_links:
            job.add(
                f"Liens symboliques internes ignores pendant l'extraction securisee: {len(skipped_links)}"
            )
            for name in skipped_links[:10]:
                job.add(f" - {name}")
            if len(skipped_links) > 10:
                job.add(f" - ... {len(skipped_links) - 10} autre(s) lien(s)")

        job.add(f"Modules detectes dans le ZIP: {len(candidates)}")
        for candidate in candidates:
            job.add(f" - {candidate.name}")

        if selected_modules is not None:
            requested = module_name_list(selected_modules)
            by_name = {candidate.name: candidate for candidate in candidates}
            unknown = [name for name in requested if name not in by_name]
            if unknown:
                raise RuntimeError(
                    "Modules sélectionnés absents du ZIP: " + ", ".join(unknown)
                )
            candidates = [by_name[name] for name in requested]
            job.add(f"Modules sélectionnés pour l'import: {len(candidates)}")
            for candidate in candidates:
                job.add(f" - {candidate.name}")

        link_module_candidates(job, project, candidates, replace_existing=replace_existing)
    except Exception:
        if import_dir is None:
            job.add("Archive temporaire nettoyée après erreur d'analyse.")
        raise
    finally:
        if import_dir is not None:
            shutil.rmtree(import_dir, ignore_errors=True)
            job.add(f"Archive temporaire nettoyée: {import_dir}")


def job_output_payload(job, compact, detail_job_id, output_from):
    if compact and job.id != detail_job_id:
        return {"lines": [], "output": ""}
    output = job.output[-JOB_OUTPUT_LIMIT:]
    window_start = job.output_total - len(output)
    if compact and output_from and window_start <= output_from <= job.output_total:
        # Suite seulement : l'interface possède déjà les caractères précédents.
        return {"lines": [], "output": output[len(output) - (job.output_total - output_from):], "output_from": output_from}
    payload = {"lines": job.lines[-JOB_LINES_LIMIT:], "output": output}
    if compact:
        payload["output_from"] = 0
    return payload


def jobs_snapshot(detail_job_id=None, compact=False, output_from=None):
    with JOBS_LOCK:
        values = list(JOBS.values())[-30:]
        if compact and detail_job_id is None and values:
            detail_job_id = values[-1].id
        return [
            {
                "id": job.id,
                "title": job.title,
                "project": job.project,
                "status": job.status,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "error_message": job.error_message,
                "last_line": job.lines[-1] if job.lines else "",
                "output_total": job.output_total,
                **job_output_payload(job, compact, detail_job_id, output_from),
                "result": dict(job.result),
                "progress": dict(job.progress) if job.progress else None,
                **job_cancel_payload(job),
            }
            for job in reversed(values)
        ]


def job_creation_payload(job):
    with JOBS_LOCK:
        return {
            "id": job.id,
            "title": job.title,
            "project": job.project,
            "status": job.status,
            "started_at": job.started_at,
            "lines": job.lines[-JOB_LINES_LIMIT:],
            **job_cancel_payload(job),
        }


def job_cancel_payload(job):
    """Ce que l'interface peut proposer pour arrêter l'action ; appelé sous JOBS_LOCK."""
    irreversible = job.control.irreversible_step if job.status == "running" else ""
    return {
        "cancellable": job.status == "queued" or (job.status == "running" and job.cancellable and not irreversible),
        "cancel_hint": job.cancel_hint,
        "cancel_blocked_step": irreversible,
        "cancel_pending_step": job.control.protected_step if job.status == "cancelling" else "",
        "waiting_for": job_waiting_reason(job) if job.status == "queued" else "",
    }


def clear_jobs_history():
    with JOBS_LOCK:
        running = {job_id: job for job_id, job in JOBS.items() if job.status in JOB_UNFINISHED_STATUSES}
        JOBS.clear()
        JOBS.update(running)
        return len(running)


def delete_job_history(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise ValueError("Action introuvable.")
        if job.status in JOB_UNFINISHED_STATUSES:
            raise ValueError("Impossible de supprimer une action en cours ou en attente : arrête-la d'abord.")
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


def publish_event(event_type, payload):
    """Push a live update to every connected /api/stream subscriber."""
    message = f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    with EVENT_SUBSCRIBERS_LOCK:
        subscribers = list(EVENT_SUBSCRIBERS)
    for subscriber_queue in subscribers:
        try:
            subscriber_queue.put_nowait(message)
        except queue.Full:
            pass


def event_watch_loop():
    last_overview_json = None
    last_system_json = None
    docker = None
    docker_checked_at = 0.0
    while True:
        try:
            with EVENT_SUBSCRIBERS_LOCK:
                has_subscribers = bool(EVENT_SUBSCRIBERS)
            if has_subscribers:
                # `docker info` et l'état système suivent l'intervalle configuré ; seul
                # `docker ps`, peu coûteux, garde le rythme court des voyants ON/OFF.
                now = time.monotonic()
                refresh_docker = (
                    docker is None
                    or now - docker_checked_at >= docker_poll_seconds()
                    or EVENT_DOCKER_REFRESH.is_set()
                )
                if refresh_docker:
                    EVENT_DOCKER_REFRESH.clear()
                    docker = docker_status(SETTINGS)
                    docker_checked_at = now
                overview_payload = overview(docker, databases_max_age=EVENT_DATABASES_MAX_AGE_SECONDS)
                overview_json = json.dumps(overview_payload, sort_keys=True, ensure_ascii=False)
                if overview_json != last_overview_json:
                    last_overview_json = overview_json
                    publish_event("overview", overview_payload)

                if refresh_docker:
                    system_payload = system_status_snapshot(docker)
                    system_json = json.dumps(system_payload, sort_keys=True, ensure_ascii=False)
                    if system_json != last_system_json:
                        last_system_json = system_json
                        publish_event("system_status", system_payload)
            else:
                last_overview_json = None
                last_system_json = None
                docker = None
        except Exception:
            traceback.print_exc()
        time.sleep(EVENT_WATCH_INTERVAL_SECONDS)


def ensure_event_watch_thread_started():
    global _EVENT_WATCH_THREAD_STARTED
    with _EVENT_WATCH_THREAD_LOCK:
        if _EVENT_WATCH_THREAD_STARTED:
            return
        _EVENT_WATCH_THREAD_STARTED = True
        threading.Thread(target=event_watch_loop, daemon=True).start()


CONTAINER_LOG_FILE_CANDIDATES = (
    "/home/odoo/srv/data/odoo.log",
    "/var/log/odoo/odoo.log",
    "/tmp/odoo.log",
)


def discover_container_log_file(container):
    """Return the path of the first non-empty known Odoo log file inside the container, if any."""
    shell = "for f in " + " ".join(CONTAINER_LOG_FILE_CANDIDATES) + "; do if [ -s \"$f\" ]; then echo \"$f\"; exit 0; fi; done; exit 1"
    code, output = run_capture(docker_command(SETTINGS, "exec", container, "sh", "-lc", shell), timeout=10)
    if code == 0 and output.strip():
        return output.strip().splitlines()[0].strip()
    return None


def start_log_follow_process(project):
    """Start a subprocess following the Odoo container logs live, or None if unavailable."""
    container = f"odoo-{project}"
    status = container_status(container)
    if status in {"running", "restarting", "paused"}:
        log_file = discover_container_log_file(container)
        if log_file:
            # Odoo writes request/module traffic to its log file, not to the container's stdout.
            command = docker_command(SETTINGS, "exec", container, "sh", "-lc", f"tail -n 200 -f '{log_file}'")
        else:
            command = docker_command(SETTINGS, "logs", "-f", "--tail", "200", container)
        cwd = WORKSPACE
    else:
        service = compose_service_for(project)
        if not service:
            return None
        command = docker_command(SETTINGS, "compose", "logs", "-f", "--tail", "200", service)
        cwd = WORKSPACE / project
    command, process_cwd = project_service().prepare_command(command, cwd)
    return subprocess.Popen(
        command,
        cwd=str(process_cwd),
        env=command_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **hidden_process_kwargs(),
    )


def read_container_log_file(container):
    shell = "for f in " + " ".join(CONTAINER_LOG_FILE_CANDIDATES) + "; do if [ -s \"$f\" ]; then echo \"===== $f =====\"; tail -n 260 \"$f\"; exit 0; fi; done; exit 1"
    code, output = run_capture(docker_command(SETTINGS, "exec", container, "sh", "-lc", shell), timeout=10)
    if code == 0 and output.strip():
        return output.strip()
    return ""


def tail_logs(project, raw=False):
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
            sections.append(file_logs if raw else compact_odoo_log_text(file_logs))
            return "\n\n".join(sections)

    if status != "absent":
        code, output = run_capture(docker_command(SETTINGS, "logs", "--tail", "260", container), timeout=12)
        if code == 0 and output.strip():
            sections.append("===== docker logs =====")
            sections.append(output.strip() if raw else compact_odoo_log_text(output.strip()))
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

    def discard_request_body(self):
        """Lit le corps avant de refuser la requête.

        Fermer la connexion en laissant des octets non lus fait envoyer un RST par
        Windows : le client perd la réponse 403 et ne voit qu'une connexion coupée.
        Au-delà de la limite JSON, la connexion est coupée sans rien lire.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return
        if length <= 0 or length > MAX_JSON_BODY_BYTES:
            return
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                return
            remaining -= len(chunk)

    def reject_untrusted_request(self):
        reason = untrusted_request_reason(self.headers)
        if not reason:
            return False
        body = json.dumps({"error": reason}, ensure_ascii=False).encode("utf-8")
        try:
            self.discard_request_body()
            self.send_response(403)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        self.close_connection = True
        return True

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        if length > MAX_JSON_BODY_BYTES:
            raise ValueError("Requête JSON trop volumineuse.")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type application/json requis.")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_OPTIONS(self):
        if self.reject_untrusted_request():
            return
        self.send_response(204)
        add_cors_headers(self)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Odoo-Database-Name, X-Odoo-Master-Password, "
            "X-Odoo-Copy, X-Odoo-Neutralize, X-File-Name",
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):
        if self.reject_untrusted_request():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                return html_response(self, INDEX_HTML)
            if path == "/favicon.ico":
                # Demandé d'office par le navigateur sur la page de secours : ce n'est pas une erreur du service.
                return empty_response(self)
            if path == "/api/health":
                return json_response(
                    self,
                    {
                        "ok": True,
                        "pid": os.getpid(),
                        "instance_id": os.environ.get("ODOO_MANAGER_INSTANCE_ID", ""),
                        "port": PORT,
                        "log_file": str(RUNTIME_LOG_PATH),
                    },
                )
            if path == "/api/bootstrap":
                return json_response(self, bootstrap_snapshot())
            if path == "/api/overview":
                return json_response(self, overview())
            if path == "/api/settings":
                return json_response(self, {"settings": settings_snapshot()})
            if path == "/api/errors":
                return json_response(self, manager_errors_snapshot())
            if path == "/api/system/status":
                return json_response(self, system_status_snapshot())
            if path == "/api/system/project-creation-prerequisites":
                return json_response(self, project_creation_prerequisites())
            if path == "/api/system/migration":
                return json_response(self, migration_snapshot())
            if path == "/api/system/ssh-keys":
                return json_response(self, ssh_public_keys_snapshot())
            if path == "/api/jobs":
                params = urllib.parse.parse_qs(parsed.query)
                detail_value = params.get("detail", [""])[0]
                detail_job_id = int(detail_value) if detail_value.isdigit() else None
                output_value = params.get("output_from", [""])[0]
                output_from = int(output_value) if detail_job_id and output_value.isdigit() else None
                return json_response(
                    self,
                    {"jobs": jobs_snapshot(detail_job_id=detail_job_id, compact=True, output_from=output_from)},
                )
            if path == "/api/stream":
                return self.stream_events()

            match = re.match(r"^/api/projects/([^/]+)/logs/stream$", path)
            if match:
                project = validate_project(urllib.parse.unquote(match.group(1)))
                raw = truthy(urllib.parse.parse_qs(parsed.query).get("raw", [""])[0])
                return self.stream_project_logs(project, raw=raw)

            match = re.match(r"^/api/projects/([^/]+)/modules$", path)
            if match:
                project = validate_project(urllib.parse.unquote(match.group(1)))
                params = urllib.parse.parse_qs(parsed.query)
                db_name = params.get("db", [""])[0]
                if db_name:
                    validate_db(db_name)
                return json_response(self, {"modules": modules_for(project, db_name)})

            match = re.match(r"^/api/projects/([^/]+)/addon-links$", path)
            if match:
                project = urllib.parse.unquote(match.group(1))
                return json_response(self, addon_links_snapshot(project))

            match = re.match(r"^/api/projects/([^/]+)/languages$", path)
            if match:
                project = validate_project(urllib.parse.unquote(match.group(1)))
                db_name = validate_odoo_db(urllib.parse.parse_qs(parsed.query).get("db", [""])[0])
                return json_response(self, {"languages": installed_languages(project, db_name)})

            match = re.match(r"^/api/projects/([^/]+)/socle$", path)
            if match:
                project = validate_project(urllib.parse.unquote(match.group(1)))
                db_name = urllib.parse.parse_qs(parsed.query).get("db", [""])[0]
                if db_name:
                    validate_odoo_db(db_name)
                return json_response(self, socle_catalog(project, db_name))

            match = re.match(r"^/api/projects/([^/]+)/socle/plan$", path)
            if match:
                project = validate_project(urllib.parse.unquote(match.group(1)))
                params = urllib.parse.parse_qs(parsed.query)
                db_name = validate_odoo_db(params.get("db", [""])[0])
                return json_response(self, socle_install_plan(project, db_name, params.get("presets", [""])[0]))

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
                raw = truthy(urllib.parse.parse_qs(parsed.query).get("raw", [""])[0])
                return json_response(self, {"logs": tail_logs(project, raw=raw)})

            match = re.match(r"^/api/projects/([^/]+)/diagnostics$", path)
            if match:
                project = validate_project(urllib.parse.unquote(match.group(1)))
                return json_response(self, project_diagnostics(project))

            return json_response(self, {"error": "Route introuvable."}, status=404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None
        except ValueError as exc:
            return json_response(self, {"error": str(exc)}, status=400)
        except Exception as exc:
            traceback.print_exc()
            return json_response(self, {"error": str(exc)}, status=500)

    def write_sse(self, event_type, payload):
        message = f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        self.wfile.write(message.encode("utf-8"))
        self.wfile.flush()

    def stream_events(self):
        """Long-lived SSE connection pushing live overview/system_status updates."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            # no-transform : un proxy (next dev) ne doit ni compresser ni retenir le flux.
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            add_cors_headers(self)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None

        subscriber_queue = queue.Queue(maxsize=20)
        with EVENT_SUBSCRIBERS_LOCK:
            EVENT_SUBSCRIBERS.add(subscriber_queue)
        try:
            self.wfile.write(b"retry: 2000\n\n")
            docker = docker_status(SETTINGS)
            self.write_sse("overview", overview(docker))
            self.write_sse("system_status", system_status_snapshot(docker))
            while True:
                try:
                    message = subscriber_queue.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(message.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None
        except Exception:
            traceback.print_exc()
            return None
        finally:
            with EVENT_SUBSCRIBERS_LOCK:
                EVENT_SUBSCRIBERS.discard(subscriber_queue)

    def stream_project_logs(self, project, raw=False):
        """Long-lived SSE connection tailing the Odoo container logs live."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            # no-transform : un proxy (next dev) ne doit ni compresser ni retenir le flux.
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            add_cors_headers(self)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None

        process = None
        try:
            try:
                process = start_log_follow_process(project)
            except OSError as exc:
                self.write_sse("log", {"line": f"Impossible de suivre les logs: {exc}"})
                self.write_sse("log_end", {})
                return None
            if process is None:
                self.write_sse(
                    "log",
                    {"line": "Aucun conteneur Odoo actif pour ce projet. Démarre le projet puis réessaie."},
                )
                self.write_sse("log_end", {})
                return None
            with ACTIVE_PROCESSES_LOCK:
                ACTIVE_PROCESSES.add(process)
            self.write_sse("log", {"line": f"--- Suivi en direct des logs de {project} ---"})
            assert process.stdout is not None
            display = None if raw else OdooLogDisplay()
            for line in process.stdout:
                lines = [line.rstrip("\n")] if display is None else display.feed(line)
                for visible_line in lines:
                    self.write_sse("log", {"line": visible_line})
            if display is not None:
                for visible_line in display.finish():
                    self.write_sse("log", {"line": visible_line})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None
        except Exception:
            traceback.print_exc()
            return None
        finally:
            if process is not None:
                with ACTIVE_PROCESSES_LOCK:
                    ACTIVE_PROCESSES.discard(process)
                if process.poll() is None:
                    try:
                        process.terminate()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=3)
                except Exception:
                    try:
                        process.kill()
                    except OSError:
                        pass

    def do_POST(self):
        if self.reject_untrusted_request():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/system/shutdown":
            json_response(self, {"ok": True})

            def shutdown_server():
                terminate_active_subprocesses()
                terminate_project_processes()
                self.server.shutdown()

            threading.Thread(target=shutdown_server, daemon=True).start()
            return

        if parsed.path == "/api/errors/report":
            try:
                payload = self.read_json()
                record_manager_error(
                    "Interface",
                    payload.get("message", ""),
                    details=payload.get("details", ""),
                    project=payload.get("project", ""),
                )
                return json_response(self, {"ok": True}, status=201)
            except Exception as exc:
                return json_response(self, {"error": str(exc)}, status=400)

        if parsed.path == "/api/settings":
            try:
                payload = self.read_json()
                with JOBS_LOCK:
                    running = [job.title for job in JOBS.values() if job.status in JOB_UNFINISHED_STATUSES]
                # Closing onboarding changes no path used by a running job; the
                # first project creation sends it right after starting its job.
                interface_only = set(payload) <= {"onboarding_completed", "create_workspace"}
                if running and not interface_only:
                    raise ValueError(
                        "Traitement en cours : "
                        + ", ".join(running)
                        + ". Consulte le suivi des actions avant de modifier les paramètres."
                    )
                create_workspace = bool(payload.pop("create_workspace", False))
                settings = SETTINGS_STORE.update(payload, create_workspace=create_workspace)
                apply_settings(settings)
                return json_response(self, {"settings": settings_snapshot()})
            except Exception as exc:
                return json_response(self, {"error": str(exc)}, status=400)

        if parsed.path == "/api/system/docker/start":
            result = start_docker(SETTINGS)
            return json_response(self, result, status=200 if result.get("ok") else 400)

        if parsed.path == "/api/system/ssh-key/generate":
            try:
                payload = self.read_json()
                return json_response(
                    self,
                    generate_ssh_key(payload.get("comment", ""), replace=truthy(payload.get("replace", False))),
                    status=201,
                )
            except (ValueError, RuntimeError, OSError) as exc:
                return json_response(self, {"error": str(exc)}, status=400)

        postgresql_match = re.match(r"^/api/projects/([^/]+)/postgresql/open$", parsed.path)
        if postgresql_match:
            try:
                project = validate_project(urllib.parse.unquote(postgresql_match.group(1)))
                payload = self.read_json()
                return json_response(self, open_postgresql_console(project, payload.get("db", "")))
            except (ValueError, RuntimeError, OSError) as exc:
                return json_response(self, {"error": str(exc)}, status=400)

        restore_match = re.match(r"^/api/projects/([^/]+)/database-restore$", parsed.path)
        if restore_match:
            destination = None
            try:
                project = validate_project(urllib.parse.unquote(restore_match.group(1)))
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0:
                    raise ValueError("Fichier de sauvegarde ZIP manquant.")
                if content_length > MAX_DATABASE_BACKUP_BYTES:
                    max_gb = MAX_DATABASE_BACKUP_BYTES / (1024 * 1024 * 1024)
                    raise ValueError(f"Sauvegarde trop volumineuse. Limite configurée: {max_gb:.0f} Go.")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() not in {
                    "application/zip",
                    "application/octet-stream",
                }:
                    raise ValueError("Format de téléversement invalide. Sélectionne une sauvegarde ZIP Odoo.")

                db_name = validate_new_db(urllib.parse.unquote(self.headers.get("X-Odoo-Database-Name", "")))
                master_pwd = validate_required_text(
                    urllib.parse.unquote(self.headers.get("X-Odoo-Master-Password", "odoo")),
                    "Master password",
                )
                copy_database = truthy(self.headers.get("X-Odoo-Copy", "1"))
                neutralize = truthy(self.headers.get("X-Odoo-Neutralize", "1"))
                filename = urllib.parse.unquote(self.headers.get("X-File-Name", "backup.zip"))
                filename = SAFE_IMPORT_NAME_RE.sub("_", Path(filename).name) or "backup.zip"
                if not filename.lower().endswith(".zip"):
                    raise ValueError("La sauvegarde doit être un fichier ZIP.")

                staging_root = database_restore_staging_root(project)
                staging_root.mkdir(parents=True, exist_ok=True)
                free_space = shutil.disk_usage(staging_root).free
                if free_space < content_length + 512 * 1024 * 1024:
                    raise ValueError("Espace disque insuffisant pour préparer la restauration.")
                destination = staging_root / f"{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns()}_{filename}"
                save_request_body_to_file(self.rfile, content_length, destination)
                details = validate_odoo_backup_archive(destination)
                job = Job(
                    f"Restaurer {db_name} dans {project}",
                    restore_database_job,
                    (project, destination, filename, db_name, master_pwd, copy_database, neutralize),
                    project=project,
                )
                destination = None
                return json_response(
                    self,
                    {"job": job_creation_payload(job), "backup": details},
                    status=201,
                )
            except Exception as exc:
                if destination is not None:
                    destination.unlink(missing_ok=True)
                return json_response(self, {"error": str(exc)}, status=400)

        repository_inspect_match = re.match(r"^/api/projects/([^/]+)/repository/inspect$", parsed.path)
        if repository_inspect_match:
            try:
                payload = self.read_json()
                db_name = str(payload.get("db") or "")
                if db_name:
                    validate_odoo_db(db_name)
                result = inspect_repository_modules(
                    urllib.parse.unquote(repository_inspect_match.group(1)),
                    payload.get("url", ""),
                    payload.get("branch", ""),
                    db_name,
                )
                return json_response(self, result)
            except Exception as exc:
                return json_response(self, {"error": str(exc)}, status=400)

        zip_inspect_match = re.match(r"^/api/projects/([^/]+)/module-zip/inspect$", parsed.path)
        if zip_inspect_match:
            try:
                project = validate_project(urllib.parse.unquote(zip_inspect_match.group(1)))
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0:
                    raise ValueError("Fichier ZIP manquant.")
                if length > 250 * 1024 * 1024:
                    raise ValueError("ZIP trop volumineux. Limite: 250 Mo.")
                body = self.rfile.read(length)
                _, files = parse_multipart_form(self.headers.get("Content-Type", ""), body)
                upload = files.get("zip")
                if not upload:
                    raise ValueError("Champ fichier ZIP introuvable.")
                filename = upload.get("filename") or "modules.zip"
                result = inspect_zip_modules(project, filename, upload["data"])
                return json_response(self, result)
            except Exception as exc:
                return json_response(self, {"error": str(exc)}, status=400)

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
                selected_modules = fields.get("modules")
                if selected_modules is not None:
                    module_name_list(selected_modules)
                job = Job(
                    f"Importer ZIP {filename}",
                    import_zip_modules_job,
                    (project, filename, upload["data"], replace_existing, selected_modules),
                    project=project,
                )
                return json_response(
                    self,
                    {"job": job_creation_payload(job)},
                    status=201,
                )
            except Exception as exc:
                return json_response(self, {"error": str(exc)}, status=400)

        cancel_match = re.match(r"^/api/jobs/([0-9]+)/cancel$", parsed.path)
        if cancel_match:
            try:
                self.read_json()
                job = cancel_job(int(cancel_match.group(1)))
                return json_response(self, {"job": job_creation_payload(job)})
            except ValueError as exc:
                return json_response(self, {"error": str(exc)}, status=409)

        if parsed.path != "/api/jobs":
            return json_response(self, {"error": "Route introuvable."}, status=404)
        try:
            payload = self.read_json()
            action = payload.get("action")

            if action == "repository_modules":
                project = validate_project(payload.get("project", ""))
                url, branch, names, commit = validate_module_repository(
                    payload.get("url"), payload.get("branch"), payload.get("modules"), payload.get("commit"))
                job = Job(f"Importer des modules depuis Git · {project}",
                          repository_modules_job, (project, url, branch, names, commit), project=project)
            elif action == "start_project":
                project = validate_project(payload.get("project", ""))
                job = Job(f"Démarrer {project}", start_project_job, (project,), project=project)
            elif action == "stop_project":
                project = validate_project(payload.get("project", ""))
                job = Job(f"Arrêter {project}", stop_project_job, (project,), project=project)
            elif action == "update_project":
                project = validate_project(payload.get("project", ""))
                job = Job(f"MAJ projet {project}", update_project_job, (project,), project=project)
            elif action == "update_all":
                job = Job("MAJ tous les projets", update_all_projects_job, resources={"*"})
            elif action == "update_all_modules":
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                allow_missing_filestore = truthy(payload.get("allow_missing_filestore"))
                title_suffix = " sans filestore complet" if allow_missing_filestore else ""
                module_states = installed_modules(project, db_name)
                available_names = {path.name for path in module_dirs(project)}
                local_exceptions = active_local_module_exceptions(
                    project,
                    db_name,
                    states=module_states,
                    available_names=available_names,
                )
                if local_exceptions:
                    modules = available_update_modules(
                        project,
                        db_name,
                        states=module_states,
                        available_names=available_names,
                        excluded_names=local_exceptions,
                    )
                    if not modules:
                        raise ValueError("Aucun module installé avec code disponible à mettre à jour.")
                    job = Job(
                        f"Mettre à jour les modules disponibles sur {db_name}{title_suffix}",
                        update_all_modules_job,
                        (project, db_name, ",".join(modules)),
                        project=project,
                    )
                else:
                    job = Job(
                        f"Mettre à jour tous les modules sur {db_name}{title_suffix}",
                        update_all_modules_job,
                        (project, db_name, "all"),
                        project=project,
                    )
            elif action == "update_imported_modules":
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                modules = validate_modules(payload.get("modules", ""))
                job = Job(
                    f"Installer ou mettre à jour les modules importés sur {db_name}",
                    update_imported_modules_job,
                    (project, db_name, modules),
                    project=project,
                )
            elif action == "update_local_modules":
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                modules = available_update_modules(project, db_name)
                if not modules:
                    raise ValueError("Aucun addon projet installé à mettre à jour.")
                job = Job(
                    f"Mettre à jour les addons projet sur {db_name}",
                    module_command_job,
                    ("--update-module", project, db_name, ",".join(modules)),
                    project=project,
                )
            elif action == "ignore_missing_modules_locally":
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                modules = validate_modules(payload.get("modules", ""))
                job = Job(
                    f"Exclure localement {modules} sur {db_name}",
                    cancel_missing_module_operations_job,
                    (project, db_name, modules),
                    project=project,
                )
            elif action == "restore_module_update_exclusions":
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                modules = validate_modules(payload.get("modules", ""))
                job = Job(
                    f"Réactiver les mises à jour de {modules} sur {db_name}",
                    restore_module_update_exclusions_job,
                    (project, db_name, modules),
                    project=project,
                )
            elif action == "create_project":
                name = validate_new_project_name(payload.get("name", ""))
                source_type = str(payload.get("source_type", "standard") or "standard").strip()
                version = str(payload.get("version", "") or "").strip()
                repository_url = str(payload.get("repository_url", "") or "").strip()
                repository_branch = str(payload.get("repository_branch", "") or "").strip()
                rika_instance = str(payload.get("rika_instance", "") or "").strip()
                rika_login = str(payload.get("rika_login", "") or "").strip()
                rika_password = str(payload.get("rika_password", "") or "")
                if source_type not in {"standard", "gitlab", "rika"}:
                    raise ValueError("Type de source invalide.")
                if source_type != "rika":
                    version = validate_odoo_version(version)
                elif not rika_instance or not rika_login or not rika_password:
                    raise ValueError("L'instance et les identifiants RIKA sont requis.")
                if source_type == "gitlab":
                    repository_url = validate_gitlab_repository(repository_url)
                    repository_branch = validate_git_ref(repository_branch)
                if (WORKSPACE / name).exists() or (WORKSPACE / name).is_symlink():
                    raise ValueError(f"Un projet nommé {name} existe déjà dans le workspace.")
                start_after_creation = truthy(payload.get("start_after_creation", True))
                job = Job(
                    f"Créer le projet {name or 'Odoo'}",
                    create_project_job,
                    (
                        name,
                        version,
                        source_type,
                        repository_url,
                        repository_branch,
                        rika_instance,
                        rika_login,
                        rika_password,
                        start_after_creation,
                    ),
                    project=name,
                )
            elif action == "migrate_project":
                name = validate_new_project_name(payload.get("project", ""))
                job = Job(f"Migrer {name} vers l'environnement Linux", migrate_project_job, (name,), project=name)
            elif action == "cleanup_staging":
                job = Job("Nettoyer les créations interrompues", cleanup_staging_job)
            elif action == "install_traefik":
                job = Job("Installer Traefik", install_traefik_job, resources={"traefik"})
            elif action == "install_git":
                job = Job("Installer Git pour Windows", install_git_job, resources={"git"})
            elif action == "create_database":
                project = validate_project(payload.get("project", ""))
                db_name = validate_new_db(payload.get("db", ""))
                master_pwd = payload.get("master_pwd", "")
                login = payload.get("login", "")
                password = payload.get("password", "")
                lang = payload.get("lang", "fr_FR")
                country = payload.get("country", "")
                demo = bool(payload.get("demo", False))
                job = Job(f"Créer base {db_name}", create_database_job, (project, db_name, master_pwd, login, password, lang, country, demo), project=project)
            elif action == "drop_database":
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                job = Job(
                    f"Supprimer base {db_name}",
                    drop_database_job,
                    (project, db_name, payload.get("master_pwd", "")),
                    project=project,
                )
            elif action == "neutralize_database":
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                job = Job(
                    f"Neutraliser {db_name}",
                    neutralize_database_job,
                    (project, db_name),
                    project=project,
                )
            elif action == "reset_module_translations":
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                modules = validate_modules(payload.get("modules", ""))
                job = Job(
                    f"Réinitialiser les traductions de {modules} sur {db_name}",
                    reset_module_translations_job,
                    (project, db_name, modules),
                    project=project,
                )
            elif action == "reset_all_translations":
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                languages = validate_language_codes(payload.get("languages", ""))
                job = Job(
                    f"Réinitialiser toutes les traductions de {db_name}",
                    reset_all_translations_job,
                    (project, db_name, ",".join(languages)),
                    project=project,
                )
            elif action == "regenerate_assets":
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                job = Job(
                    f"Régénérer les assets de {db_name}",
                    regenerate_assets_job,
                    (project, db_name),
                    project=project,
                )
            elif action == "reset_admin_password":
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                password = validate_admin_password(payload.get("password"))
                job = Job(
                    f"Réinitialiser le mot de passe admin de {db_name}",
                    reset_admin_password_job,
                    (project, db_name, password),
                    project=project,
                )
            elif action == "delete_project":
                project = validate_project(payload.get("project", ""))
                job = Job(f"Supprimer {project}", delete_project_job, (project,), project=project)
            elif action == "delete_module_code":
                project = validate_project(payload.get("project", ""))
                modules = validate_modules(payload.get("modules", ""))
                db_name = str(payload.get("db", "") or "").strip()
                uninstall_first = bool(payload.get("uninstall_first", False))
                if uninstall_first:
                    db_name = validate_odoo_db(db_name)
                job = Job(f"Supprimer modules {modules} du projet", delete_module_code_job, (project, modules, db_name, uninstall_first), project=project)
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
                job = Job(f"{label} {modules} sur {db_name}", module_command_job, (flag, project, db_name, modules), project=project)
            elif action == "install_socle":
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                presets = ",".join(validate_socle_presets(payload.get("presets", "")))
                job = Job(f"Installer le socle sur {db_name}", install_socle_job, (project, db_name, presets), project=project)
            elif action == "repair_enterprise_links":
                project = validate_project(payload.get("project", ""))
                job = Job(
                    f"Vérifier les liens Enterprise de {project}",
                    repair_enterprise_links_job,
                    (project,),
                    project=project,
                )
            elif action == "convert_wsl_addon_links":
                project = validate_project(payload.get("project", ""))
                job = Job(
                    f"Convertir les liens WSL de {project}",
                    convert_wsl_addon_links_job,
                    (project,),
                    project=project,
                )
            elif action == "link_modules":
                project = validate_project(payload.get("project", ""))
                source = payload.get("source", "")
                if not source:
                    raise ValueError("Dossier de modules manquant.")
                job = Job(f"Lier modules dans {project}", link_modules_job, (project, source), project=project)
            else:
                return json_response(self, {"error": "Action inconnue."}, status=400)

            return json_response(
                self,
                {"job": job_creation_payload(job)},
                status=201,
            )
        except Exception as exc:
            return json_response(self, {"error": str(exc)}, status=400)

    def do_DELETE(self):
        if self.reject_untrusted_request():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/errors":
            clear_manager_errors()
            return json_response(self, {"ok": True})
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


class ManagerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    allow_reuse_address = True


def address_is_already_in_use(error):
    return error.errno in {errno.EADDRINUSE, 48, 98, 10048}


def main():
    if not MANAGER.exists():
        raise SystemExit(f"Script introuvable: {MANAGER}")
    url = f"http://{HOST}:{PORT}/"
    try:
        server = ManagerHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        if address_is_already_in_use(exc):
            print(f"Interface deja lancee ou port occupe: {url}")
            print("Utilise ./odoo_next_gui.sh --stop puis ./odoo_next_gui.sh --background pour recharger.")
            return
        raise
    print(f"Interface Odoo locale: {url}")
    print(f"Workspace: {WORKSPACE}")
    ensure_event_watch_thread_started()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        terminate_active_subprocesses()
        terminate_project_processes()
        server.server_close()


if __name__ == "__main__":
    main()
