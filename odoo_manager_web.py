#!/usr/bin/env python3
import errno
import html
import http.client
import json
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from odoo_manager_runtime import initialize_runtime_streams


RUNTIME_LOG_PATH, _RUNTIME_STREAMS = initialize_runtime_streams()

from odoo_manager_core import ManagerSettings, ProjectCreator, SettingsStore, ProjectService, docker_status, start_docker
from odoo_manager_core.platform import (
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
    wsl_path_context,
    wsl_unc_path,
)
from odoo_manager_core.project_creator import (
    SUPPORTED_ODOO_VERSIONS,
    validate_git_ref,
    validate_gitlab_repository,
    validate_new_project_name,
    validate_odoo_version,
)
from odoo_manager_core.project_service import terminate_active_processes as terminate_project_processes
from odoo_manager_core.system import docker_command, reset_docker_backend_cache, shell_command


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
PORT = int(os.environ.get("ODOO_GUI_PORT", "8765"))
TRAEFIK_REPO = "ssh://git@gitlab.sudokeys.com:10022/devops/docker-local-tools.git"

SAFE_PROJECT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_MODULE_RE = re.compile(r"^[A-Za-z0-9_,.-]+$")
SAFE_IMPORT_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

MAX_DB_NAME_BYTES = 63
MAX_JSON_BODY_BYTES = 1024 * 1024
MAX_RETAINED_JOBS = 60
MAX_RUNNING_JOBS = 4
MAX_ZIP_ENTRIES = 100_000
MAX_ZIP_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_DATABASE_BACKUP_BYTES = int(os.environ.get("ODOO_MANAGER_MAX_BACKUP_BYTES", 100 * 1024 * 1024 * 1024))
MAX_DATABASE_BACKUP_ENTRIES = 2_000_000

SOCLE_PRESETS = {
    "sales": ("Ventes", ("sale_management",)),
    "crm": ("CRM", ("crm",)),
    "purchase": ("Achats", ("purchase",)),
    "inventory": ("Inventaire", ("stock",)),
    "accounting_fr": ("Comptabilité française", ("account_accountant", "l10n_fr")),
    "manufacturing": ("Fabrication", ("mrp",)),
    "project": ("Projet", ("project",)),
    "timesheets": ("Feuilles de temps", ("hr_timesheet",)),
    "employees": ("Employés", ("hr",)),
    "time_off": ("Congés", ("hr_holidays",)),
    "expenses": ("Notes de frais", ("hr_expense",)),
    "helpdesk": ("Assistance", ("helpdesk",)),
    "field_service": ("Services sur site", ("industry_fsm",)),
    "planning": ("Planification", ("planning",)),
    "documents": ("Documents", ("documents",)),
    "sign": ("Signature", ("sign",)),
    "subscriptions": ("Abonnements", ("sale_subscription",)),
    "point_of_sale": ("Point de Vente", ("point_of_sale",)),
    "ecommerce": ("eCommerce", ("website_sale",)),
}

JOBS = {}
JOBS_LOCK = threading.Lock()
NEXT_JOB_ID = 1
ACTIVE_PROCESSES = set()
ACTIVE_PROCESSES_LOCK = threading.Lock()

EVENT_SUBSCRIBERS = set()
EVENT_SUBSCRIBERS_LOCK = threading.Lock()
EVENT_WATCH_INTERVAL_SECONDS = 2
_EVENT_WATCH_THREAD_STARTED = False
_EVENT_WATCH_THREAD_LOCK = threading.Lock()
LOCAL_MODULE_OVERRIDES_LOCK = threading.Lock()


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
            "workspace_exists": WORKSPACE.exists() and WORKSPACE.is_dir(),
            "platform": platform_id(),
        }
    )
    return payload


def add_cors_headers(handler):
    origin = handler.headers.get("Origin", "")
    allowed = {
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://tauri.localhost",
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
        handler.send_header("Cache-Control", "no-store")
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
    requested_cwd = Path(cwd) if cwd is not None else None
    if requested_cwd is not None and not requested_cwd.is_dir():
        return 2, f"Dossier de travail introuvable: {requested_cwd}"
    command_cwd = requested_cwd
    if command_cwd is None:
        command_cwd = next(
            (
                candidate
                for candidate in (WORKSPACE, WORKSPACE.parent, Path.home(), ROOT)
                if candidate.is_dir()
            ),
            Path.cwd(),
        )
    try:
        result = subprocess.run(
            args,
            cwd=str(command_cwd),
            env=command_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
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


def traefik_status(docker=None):
    docker = docker or docker_status(SETTINGS)
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


def system_status_snapshot(docker=None):
    docker = docker or docker_status(SETTINGS)
    return {
        "docker": docker,
        "traefik": traefik_status(docker),
        "workspace": str(WORKSPACE),
        "workspace_exists": WORKSPACE.exists() and WORKSPACE.is_dir(),
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
    wsl_available = wsl_executable_available("git", distribution)
    use_wsl = (bool(context) and wsl_available) or (not native_available and wsl_available)
    if use_wsl:
        return {
            "kind": "wsl",
            "label": f"WSL ({distribution})" if distribution else "WSL",
            "distribution": distribution,
            "available": True,
            "command": [*wsl_command_prefix(distribution), "git"],
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


def generate_ssh_key(comment=""):
    comment = validate_ssh_comment(comment)
    existing = ssh_public_keys_snapshot()["keys"]
    default_existing = next((key for key in existing if key["name"] == "id_ed25519.pub"), None)
    if default_existing:
        return {**default_existing, "created": False, "message": "La clé Ed25519 existe déjà."}

    runtime = ssh_runtime()
    if runtime["kind"] == "wsl":
        comment_argument = f" -C {shlex.quote(comment)}" if comment else ""
        script = (
            'mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh" && '
            'if [ -e "$HOME/.ssh/id_ed25519" ]; then '
            'echo "Une clé privée id_ed25519 existe déjà sans clé publique." >&2; exit 3; fi; '
            f'ssh-keygen -t ed25519 -f "$HOME/.ssh/id_ed25519" -N ""{comment_argument}'
        )
        code, output = run_capture(
            [*wsl_command_prefix(runtime["distribution"]), "sh", "-lc", script],
            cwd=Path.home(),
            timeout=30,
        )
        if code != 0:
            raise RuntimeError(output or "Impossible de générer la clé SSH dans WSL.")
    else:
        if not executable_available("ssh-keygen", SETTINGS):
            raise RuntimeError("ssh-keygen est introuvable. Installe Git avant de générer la clé SSH.")
        ssh_dir = Path.home() / ".ssh"
        ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_key = ssh_dir / "id_ed25519"
        if private_key.exists():
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
    return {**public_key, "created": True, "message": "Clé SSH Ed25519 générée."}


def install_git_job(job):
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
    code, output = run_capture(docker_command(SETTINGS, "inspect", "-f", "{{.State.Status}}", name), timeout=5)
    if code != 0 or not output:
        return "absent"
    return output.splitlines()[0].strip()


def container_statuses(names):
    names = tuple(names)
    if not names:
        return {}
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


def wsl_module_metadata(project, linux_path, source_path, is_link, distribution):
    link_parent = workspace_execution_path(project_addons_link_parent(project), SETTINGS, WORKSPACE)
    storage_parent = workspace_execution_path(project_addons_storage_parent(project), SETTINGS, WORKSPACE)
    legacy_parent = workspace_execution_path(project_legacy_addons_storage_parent(project), SETTINGS, WORKSPACE)
    imports_roots = [workspace_execution_path(root, SETTINGS, WORKSPACE) for root in module_import_roots(project)]
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

    host_path = wsl_unc_path(distribution, linux_path)
    return host_path, {
        "path": host_path,
        "link_path": wsl_unc_path(distribution, link_path) if link_path else "",
        "source_path": wsl_unc_path(distribution, source_path),
        "path_kind": kind,
        "removable": removable,
        "removal_mode": removal_mode,
        "removal_note": removal_note,
    }


def wsl_module_dirs(project):
    context = active_workspace_wsl_context()
    if not context:
        return []
    candidates = [
        project_addons_link_parent(project),
        project_addons_storage_parent(project),
        project_legacy_addons_storage_parent(project),
        project_odoo_root(project) / "odoo" / "odoo" / "addons",
        project_odoo_root(project) / "addons-store" / "odoo_entreprise",
        project_odoo_root(project) / "addons-store" / "odoo_enterprise",
    ]
    linux_candidates = [workspace_execution_path(path, SETTINGS, WORKSPACE) for path in candidates]
    script = (
        'for parent do [ -d "$parent" ] || continue; '
        'find "$parent" -mindepth 1 -maxdepth 1 \\( -type d -o -type l \\) -print 2>/dev/null | '
        'while IFS= read -r child; do '
        '[ -f "$child/__manifest__.py" ] || [ -f "$child/__openerp__.py" ] || continue; '
        'target=$(readlink -f -- "$child" 2>/dev/null || printf "%s" "$child"); '
        'if [ -L "$child" ]; then linked=1; else linked=0; fi; '
        'printf "%s\\t%s\\t%s\\n" "$child" "$target" "$linked"; '
        'done; done'
    )
    code, output = run_capture(
        [*workspace_tool_prefix(), "sh", "-c", script, "odoo-manager", *linux_candidates],
        cwd=workspace_tool_cwd(),
        timeout=30,
    )
    if code != 0:
        return []

    seen = set()
    paths = []
    for line in output.splitlines():
        linux_path, separator, remainder = line.partition("\t")
        source_path, second_separator, linked = remainder.partition("\t")
        if not separator or not second_separator:
            continue
        name = posixpath.basename(linux_path)
        if not SAFE_MODULE_RE.fullmatch(name) or name in seen:
            continue
        seen.add(name)
        host_path, metadata = wsl_module_metadata(
            project,
            posixpath.normpath(linux_path),
            posixpath.normpath(source_path or linux_path),
            linked == "1",
            context.distribution,
        )
        WSL_MODULE_METADATA[host_path.casefold()] = metadata
        paths.append(Path(host_path))
    return paths


def module_dirs(project):
    if active_workspace_wsl_context():
        yield from wsl_module_dirs(project)
        return
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
        if active_workspace_wsl_context():
            WSL_MODULE_METADATA.clear()


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
    metadata = WSL_MODULE_METADATA.get(str(path).casefold())
    if metadata:
        return {
            key: metadata[key]
            for key in ("path", "link_path", "source_path", "path_kind")
        }
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


def module_origin(source_path):
    normalized_path = str(source_path).replace("\\", "/").casefold()
    if (
        "/addons-store/odoo_entreprise/" in normalized_path
        or "/addons-store/odoo_enterprise/" in normalized_path
    ):
        return "enterprise"
    return "other"


def basic_module(project, path):
    location = module_location_info(project, path)
    name = posixpath.basename(str(path).replace("\\", "/")) if wsl_path_context(path) else path.name
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


def module_removal_info(project, path):
    metadata = WSL_MODULE_METADATA.get(str(path).casefold())
    if metadata:
        return {
            key: metadata[key]
            for key in ("removable", "removal_mode", "removal_note")
        }
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
    filestore_root = (WORKSPACE / project / "odoo_data" / "filestore").resolve(strict=False)
    filestore = (filestore_root / db_name).resolve(strict=False)
    files = set()
    if filestore == filestore_root or not path_is_relative_to(filestore, filestore_root):
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
        db_info = {
            "name": db_name,
            "issues": [],
            "pending_modules": [],
            "pending_missing_modules": [],
            "ignored_missing_modules": [],
            "local_excluded_modules": [],
        }
        diagnostics["databases"].append(db_info)

        states = installed_modules(project, db_name)
        if not states:
            diagnostics["issues"].append(
                {
                    "severity": "error",
                    "title": f"Impossible de lire les modules de {db_name}",
                    "details": "La table ir_module_module est inaccessible ou ne contient aucun module.",
                    "items": [],
                }
            )
            continue

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
            diagnostics["issues"].append(issue)
            db_info["issues"].append(issue)

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
            diagnostics["issues"].append(issue)
            db_info["issues"].append(issue)

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
            diagnostics["issues"].append(issue)
            db_info["issues"].append(issue)

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
            diagnostics["issues"].append(issue)
            db_info["issues"].append(issue)

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


def overview(docker=None):
    docker = docker or docker_status(SETTINGS)
    docker_ok = docker["running"]
    docker_message = docker["message"]
    project_names = project_dirs()
    container_names = [name for project in project_names for name in (f"odoo-{project}", f"postgresql-{project}")]
    statuses = container_statuses(container_names) if docker_ok else {}
    projects = []
    for project in project_names:
        odoo_status = statuses.get(f"odoo-{project}", "absent") if docker_ok else "docker off"
        pg_status = statuses.get(f"postgresql-{project}", "absent") if docker_ok else "docker off"
        databases = list_databases_for(project, check_container=False) if pg_status == "running" else []
        url = project_url(project)
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


class Job:
    def __init__(self, title, target, args=(), project=None):
        global NEXT_JOB_ID
        self.title = title
        self.project = project
        self.status = "running"
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.finished_at = None
        self.lines = []
        self.output = ""
        self.target = target
        self.args = args
        self.thread = None
        with JOBS_LOCK:
            for active in JOBS.values():
                if active.status == "running" and project and active.project == project:
                    if (getattr(target, "__name__", "") == "repository_modules_job"
                            or getattr(active.target, "__name__", "") == "repository_modules_job"):
                        raise ValueError("Une action est déjà en cours sur ce projet. Attends sa fin avant l’import ou la mise à jour.")
            running_count = sum(job.status == "running" for job in JOBS.values())
            if running_count >= MAX_RUNNING_JOBS:
                raise ValueError(
                    f"Trop d'actions sont déjà en cours ({MAX_RUNNING_JOBS} maximum). Attends la fin d'une action."
                )
            completed_ids = [job_id for job_id, job in JOBS.items() if job.status != "running"]
            excess = max(0, len(JOBS) - MAX_RETAINED_JOBS + 1)
            for job_id in completed_ids[:excess]:
                JOBS.pop(job_id, None)
            self.id = NEXT_JOB_ID
            NEXT_JOB_ID += 1
            JOBS[self.id] = self
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
            with JOBS_LOCK:
                self.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self.args = ()
                self.target = None
                self.thread = None


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
    job.add("$ " + " ".join(str(arg) for arg in args))
    process = subprocess.Popen(
        args,
        cwd=str(cwd),
        env=command_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **hidden_process_kwargs(),
    )
    with ACTIVE_PROCESSES_LOCK:
        ACTIVE_PROCESSES.add(process)
    try:
        assert process.stdout is not None
        for line in process.stdout:
            job.add(line)
        code = process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()
        with ACTIVE_PROCESSES_LOCK:
            ACTIVE_PROCESSES.discard(process)
    job.add(f"Code retour: {code}")
    if code != 0:
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
        "returning name || '|' || state; "
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


def install_traefik_job(job):
    status = docker_status(SETTINGS)
    if not status["running"]:
        raise RuntimeError("Docker doit être installé et démarré avant l'installation de Traefik.")
    git = (
        resolve_host_executable("git")
        if platform_id() == "windows" and SETTINGS.execution_mode == "wsl"
        else resolve_executable("git", SETTINGS)
    )
    git_code, git_output = run_capture([git, "--version"], timeout=8)
    if git_code != 0:
        raise RuntimeError("Git doit être installé avant Traefik. Utilise le bouton Installer dans l'étape Git.")
    job.add(git_output.splitlines()[0] if git_output else "Git détecté.")
    project_service().install_traefik(TRAEFIK_REPO, log=job.add)


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


def neutralize_database_job(job, project, db_name):
    project = validate_project(project)
    db_name = validate_odoo_db(db_name)
    if db_name not in list_databases_for(project):
        raise ValueError("La base Odoo sélectionnée n'existe plus dans PostgreSQL.")
    project_service().run_odoo_neutralize_command(project, db_name, log=job.add)


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
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("URL Odoo invalide pour la restauration.")

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
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query

    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=2 * 60 * 60)
    sent = 0
    next_progress = 10
    try:
        connection.putrequest("POST", target)
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
            time.sleep(2)
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
    conflicts = []
    missing_before = 0
    for name, source in candidates.items():
        link = link_parent / name
        if not creator.path_entry_exists(link):
            missing_before += 1
            continue
        try:
            correct = link.is_symlink() and link.resolve(strict=False) == source.resolve(strict=False)
        except OSError:
            correct = False
        if not correct:
            conflicts.append(name)
    if conflicts:
        raise RuntimeError(
            "Liens Enterprise non modifiés car des modules existent déjà avec une autre source dans odoo/addons : "
            + ", ".join(sorted(conflicts))
        )

    for root in roots:
        creator.link_modules(root, link_parent, log=job.add, replace=False)

    missing_after = [name for name in candidates if not creator.path_entry_exists(link_parent / name)]
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


def install_socle_job(job, project, db_name, presets):
    project = validate_project(project)
    db_name = validate_odoo_db(db_name)
    preset_ids = validate_socle_presets(presets)
    requested_modules = list(dict.fromkeys(
        module_name
        for preset_id in preset_ids
        for module_name in SOCLE_PRESETS[preset_id][1]
    ))

    job.add("Vérification et création des liens symboliques Odoo Enterprise...")
    ensure_enterprise_module_links(job, project)
    available = {path.name for path in module_dirs(project)}
    missing = [name for name in requested_modules if name not in available]
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
    job.add("Installation du socle: " + ", ".join(pending))
    module_command_job(job, "--install-module", project, db_name, ",".join(pending))


def module_command_job(job, flag, project, db_name, modules):
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

    project_service().run_odoo_module_command(
        project,
        db_name,
        ",".join(module_names),
        option="-i" if flag == "--install-module" else "-u",
        log=job.add,
    )


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
    for module_name in module_names:
        if delete_module_file_entry(job, project, module_name):
            removed += 1

    clear_project_module_cache(project)
    job.add(f"Suppression terminée. Entrées retirées de odoo/addons: {removed}.")
    job.add(f"Emplacement de récupération: {DELETED_MODULES / project}")


def create_project_job(job, name, version, source_type, repository_url, repository_branch, start_after_creation):
    creator = ProjectCreator(SETTINGS, WORKSPACE, project_service())
    creator.create(
        name,
        version,
        source_type=source_type,
        repository_url=repository_url,
        repository_branch=repository_branch,
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
    if not current_traefik["installed"]:
        job.add("Traefik est absent. Installation automatique avant le premier démarrage...")
        install_traefik_job(job)
    project_service().start_project(name, log=job.add)


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


def validate_module_repository(url, branch, mode, modules):
    url = validate_gitlab_repository(url)
    branch = validate_git_ref(branch)
    if mode not in {"add", "update"}:
        raise ValueError("Mode d’import invalide.")
    names = module_name_list(modules) if modules else []
    if mode == "update" and not names:
        raise ValueError("Indique les noms techniques des modules à remplacer.")
    return url, branch, mode, names


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


def repository_modules_job(job, project, url, branch, mode, names):
    """Import an isolated SSH snapshot; roll back every changed module on failure."""
    project = validate_project(project)
    creator = ProjectCreator(SETTINGS, WORKSPACE, project_service())
    staging = project_staging_imports_root(project)
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="repository-", dir=staging) as temporary:
        checkout = Path(temporary) / url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        job.add(f"Récupération du dépôt {url}, branche {branch}…")
        command = creator.git(
            "-c", "core.sshCommand=ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
            "-c", "protocol.allow=never", "-c", "protocol.ssh.allow=always",
            "clone", "--depth", "1",
            "--single-branch", "--branch", branch, "--", url, creator.command_path(checkout),
        )
        result = subprocess.run(command, cwd=creator.command_cwd, capture_output=True, text=True,
                                timeout=300, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                                **hidden_process_kwargs())
        if result.returncode:
            raise repository_clone_error(result.stderr)
        # Reject symlinks before discovery/copy, including links outside the checkout.
        if any(path.is_symlink() for path in checkout.rglob("*") if ".git" not in path.relative_to(checkout).parts):
            raise ValueError("Ce dépôt contient des liens symboliques : import refusé.")
        candidates = find_module_candidates(checkout)
        by_name = {}
        for candidate in candidates:
            validate_modules(candidate.name)
            if candidate.name in by_name:
                raise ValueError(f"Nom de module ambigu dans le dépôt : {candidate.name}")
            by_name[candidate.name] = candidate
        if names:
            missing = sorted(set(names) - by_name.keys())
            if missing:
                raise ValueError("Modules absents du dépôt : " + ", ".join(missing))
            candidates = [by_name[name] for name in dict.fromkeys(names)]
        if not candidates:
            raise ValueError("Aucun module Odoo trouvé dans le dépôt.")
        storage = project_addons_storage_parent(project)
        links = project_addons_link_parent(project)
        for candidate in candidates:
            target, link = storage / candidate.name, links / candidate.name
            exists = target.exists() or target.is_symlink() or link.exists() or link.is_symlink()
            if mode == "add" and exists:
                raise ValueError(f"Module déjà présent : {candidate.name}. Utilise la mise à jour.")
            if mode == "update" and not (managed_module_copy_ready(project, candidate.name, target)
                                          and managed_storage_link(project, candidate.name, target)):
                raise ValueError(f"Mise à jour refusée : {candidate.name} doit être une copie gérée dans addons-store.")
        job.add("Modules sélectionnés : " + ", ".join(c.name for c in candidates))
        backups, created = [], []
        try:
            for candidate in candidates:
                target, link = storage / candidate.name, links / candidate.name
                if mode == "update":
                    backups.append((target, backup_existing_module(job, project, target)))
                created.append(target)
                copy_module_to_storage(job, project, candidate)
                if mode == "add":
                    created.append(link)
                ensure_relative_module_link(job, project, candidate.name, target)
        except Exception:
            for path in reversed(created):
                if path.is_symlink():
                    path.unlink()
                elif path.exists():
                    shutil.rmtree(path)
            for target, backup in reversed(backups):
                shutil.move(str(backup), str(target))
            job.add("Import annulé ; les versions précédentes ont été restaurées.")
            raise
        finally:
            clear_project_module_cache(project)
        job.add(f"Code préparé : {len(candidates)} module(s), source {url}, branche {branch}.")
        job.add("Installe ou mets à jour ces modules dans la base Odoo depuis l’interface.")


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


def jobs_snapshot(detail_job_id=None, compact=False):
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
                "lines": list(job.lines) if not compact or job.id == detail_job_id else [],
                "output": job.output if not compact or job.id == detail_job_id else "",
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
    while True:
        try:
            with EVENT_SUBSCRIBERS_LOCK:
                has_subscribers = bool(EVENT_SUBSCRIBERS)
            if has_subscribers:
                docker = docker_status(SETTINGS)
                overview_payload = overview(docker)
                overview_json = json.dumps(overview_payload, sort_keys=True, ensure_ascii=False)
                if overview_json != last_overview_json:
                    last_overview_json = overview_json
                    publish_event("overview", overview_payload)

                system_payload = system_status_snapshot(docker)
                system_json = json.dumps(system_payload, sort_keys=True, ensure_ascii=False)
                if system_json != last_system_json:
                    last_system_json = system_json
                    publish_event("system_status", system_payload)
            else:
                last_overview_json = None
                last_system_json = None
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
    return subprocess.Popen(
        command,
        cwd=str(cwd),
        env=command_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **hidden_process_kwargs(),
    )


def read_container_log_file(container):
    shell = "for f in " + " ".join(CONTAINER_LOG_FILE_CANDIDATES) + "; do if [ -s \"$f\" ]; then echo \"===== $f =====\"; tail -n 260 \"$f\"; exit 0; fi; done; exit 1"
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
        if length > MAX_JSON_BODY_BYTES:
            raise ValueError("Requête JSON trop volumineuse.")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_OPTIONS(self):
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
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                return html_response(self, INDEX_HTML)
            if path == "/api/health":
                return json_response(
                    self,
                    {
                        "ok": True,
                        "pid": os.getpid(),
                        "log_file": str(RUNTIME_LOG_PATH),
                    },
                )
            if path == "/api/bootstrap":
                return json_response(self, bootstrap_snapshot())
            if path == "/api/overview":
                return json_response(self, overview())
            if path == "/api/settings":
                return json_response(self, {"settings": settings_snapshot()})
            if path == "/api/system/status":
                return json_response(self, system_status_snapshot())
            if path == "/api/system/project-creation-prerequisites":
                return json_response(self, project_creation_prerequisites())
            if path == "/api/system/ssh-keys":
                return json_response(self, ssh_public_keys_snapshot())
            if path == "/api/jobs":
                params = urllib.parse.parse_qs(parsed.query)
                detail_value = params.get("detail", [""])[0]
                detail_job_id = int(detail_value) if detail_value.isdigit() else None
                return json_response(self, {"jobs": jobs_snapshot(detail_job_id=detail_job_id, compact=True)})
            if path == "/api/stream":
                return self.stream_events()

            match = re.match(r"^/api/projects/([^/]+)/logs/stream$", path)
            if match:
                project = validate_project(urllib.parse.unquote(match.group(1)))
                return self.stream_project_logs(project)

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
            self.send_header("Cache-Control", "no-cache")
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

    def stream_project_logs(self, project):
        """Long-lived SSE connection tailing the Odoo container logs live."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
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
            for line in process.stdout:
                self.write_sse("log", {"line": line.rstrip("\n")})
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
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/system/shutdown":
            json_response(self, {"ok": True})

            def shutdown_server():
                terminate_active_subprocesses()
                terminate_project_processes()
                self.server.shutdown()

            threading.Thread(target=shutdown_server, daemon=True).start()
            return

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

        if parsed.path == "/api/system/ssh-key/generate":
            try:
                payload = self.read_json()
                return json_response(self, generate_ssh_key(payload.get("comment", "")), status=201)
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
                    {
                        "job": {
                            "id": job.id,
                            "title": job.title,
                            "status": job.status,
                            "started_at": job.started_at,
                            "lines": job.lines,
                        },
                        "backup": details,
                    },
                    status=201,
                )
            except Exception as exc:
                if destination is not None:
                    destination.unlink(missing_ok=True)
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

            if action == "repository_modules":
                project = validate_project(payload.get("project", ""))
                url, branch, mode, names = validate_module_repository(
                    payload.get("url"), payload.get("branch"), payload.get("mode"), payload.get("modules"))
                job = Job(f"{'Ajouter' if mode == 'add' else 'Actualiser'} des modules depuis Git · {project}",
                          repository_modules_job, (project, url, branch, mode, names), project=project)
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
                job = Job("MAJ tous les projets", update_all_projects_job)
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
                        module_command_job,
                        ("--update-module", project, db_name, ",".join(modules)),
                        project=project,
                    )
                else:
                    job = Job(
                        f"Mettre à jour tous les modules sur {db_name}{title_suffix}",
                        module_command_job,
                        ("--update-module", project, db_name, "all"),
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
                version = validate_odoo_version(payload.get("version", ""))
                source_type = str(payload.get("source_type", "standard") or "standard").strip()
                repository_url = str(payload.get("repository_url", "") or "").strip()
                repository_branch = str(payload.get("repository_branch", "") or "").strip()
                if source_type not in {"standard", "gitlab"}:
                    raise ValueError("Type de source invalide.")
                if source_type == "gitlab":
                    repository_url = validate_gitlab_repository(repository_url)
                    repository_branch = validate_git_ref(repository_branch)
                if (WORKSPACE / name).exists() or (WORKSPACE / name).is_symlink():
                    raise ValueError(f"Un projet nommé {name} existe déjà dans le workspace.")
                start_after_creation = truthy(payload.get("start_after_creation", True))
                job = Job(
                    f"Créer le projet {name or 'Odoo'}",
                    create_project_job,
                    (name, version, source_type, repository_url, repository_branch, start_after_creation),
                    project=name,
                )
            elif action == "install_traefik":
                job = Job("Installer Traefik", install_traefik_job)
            elif action == "install_git":
                job = Job("Installer Git pour Windows", install_git_job)
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
            elif action == "neutralize_database":
                project = validate_project(payload.get("project", ""))
                db_name = validate_odoo_db(payload.get("db", ""))
                job = Job(
                    f"Neutraliser {db_name}",
                    neutralize_database_job,
                    (project, db_name),
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
                {
                    "job": {
                        "id": job.id,
                        "title": job.title,
                        "project": job.project,
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
