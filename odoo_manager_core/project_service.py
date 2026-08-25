import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .system import docker_command
from .platform import executable_search_path


COMPOSE_FILENAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
ACTIVE_PROCESSES = set()
ACTIVE_PROCESSES_LOCK = threading.Lock()


def terminate_active_processes(wait_seconds=0.5):
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


class ProjectService:
    def __init__(self, settings, workspace, traefik_dir=None, runner=None):
        self.settings = settings
        self.workspace = Path(workspace)
        self.traefik_dir = Path(traefik_dir).expanduser() if traefik_dir else None
        self.runner = runner

    def env(self):
        env = os.environ.copy()
        env["PATH"] = executable_search_path()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=10")
        return env

    def log(self, callback, message):
        if callback:
            callback(message)

    def stream(self, command, cwd=None, log=None):
        if self.runner:
            return self.runner.stream(command, cwd=cwd, log=log)

        cwd = cwd or self.workspace
        self.log(log, "$ " + " ".join(str(arg) for arg in command))
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with ACTIVE_PROCESSES_LOCK:
            ACTIVE_PROCESSES.add(process)
        try:
            assert process.stdout is not None
            for line in process.stdout:
                self.log(log, line)
            code = process.wait()
        finally:
            if process.stdout is not None:
                process.stdout.close()
            with ACTIVE_PROCESSES_LOCK:
                ACTIVE_PROCESSES.discard(process)
        self.log(log, f"Code retour: {code}")
        return code

    def capture(self, command, cwd=None, timeout=10):
        if self.runner:
            return self.runner.capture(command, cwd=cwd, timeout=timeout)

        try:
            result = subprocess.run(
                command,
                cwd=str(cwd or self.workspace),
                env=self.env(),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            if result.returncode == 0:
                return result.returncode, stdout or stderr
            return result.returncode, "\n".join(part for part in (stdout, stderr) if part)
        except FileNotFoundError as exc:
            return 127, str(exc)
        except subprocess.TimeoutExpired as exc:
            return 124, (exc.stdout or "").strip()

    def docker(self, *arguments):
        return docker_command(self.settings, *arguments)

    def project_path(self, project):
        return self.workspace / project

    def compose_file(self, project):
        path = self.project_path(project)
        for name in COMPOSE_FILENAMES:
            candidate = path / name
            if candidate.exists():
                return candidate
        return None

    def list_projects(self):
        if not self.workspace.exists():
            return []
        projects = []
        for item in self.workspace.iterdir():
            if not item.is_dir():
                continue
            if any((item / name).exists() for name in COMPOSE_FILENAMES):
                projects.append(item.name)
        return sorted(projects)

    def container_status(self, container):
        code, output = self.capture(self.docker("inspect", "-f", "{{.State.Status}}", container), timeout=5)
        if code != 0 or not output:
            return "absent"
        return output.splitlines()[0].strip()

    def is_running(self, container):
        return self.container_status(container) == "running"

    def fix_macos_localtime_mount(self, compose_file, log=None):
        if platform.system() != "Darwin":
            return
        try:
            content = compose_file.read_text(encoding="utf-8")
        except OSError:
            return
        if "/etc/localtime:/etc/localtime:ro" not in content:
            return

        backup = compose_file.with_name(f"{compose_file.name}.localtime.bak.{time.strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(compose_file, backup)
        filtered = "\n".join(
            line for line in content.splitlines() if "/etc/localtime:/etc/localtime:ro" not in line
        )
        compose_file.write_text(filtered + "\n", encoding="utf-8")
        self.log(log, f"Mount /etc/localtime supprimé du compose macOS: {compose_file}")
        self.log(log, f"Sauvegarde: {backup}")

    def project_url(self, project):
        compose = self.compose_file(project)
        if compose:
            try:
                content = compose.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                content = ""
            marker = "Host(`"
            if marker in content:
                host = content.split(marker, 1)[1].split("`)", 1)[0]
                if host:
                    return f"http://{host}/"

        code, output = self.capture(self.docker("port", f"odoo-{project}", "8069/tcp"), timeout=1)
        if code == 0 and output:
            first = output.splitlines()[0].strip()
            port = first.rsplit(":", 1)[-1]
            if port.isdigit():
                return f"http://localhost:{port}/"
        return f"http://dev.{project}.localhost/"

    def start_traefik(self, log=None):
        if not self.traefik_dir or not self.traefik_dir.exists():
            self.log(log, f"Traefik introuvable: {self.traefik_dir or ''}".rstrip())
            return
        if not any((self.traefik_dir / name).exists() for name in COMPOSE_FILENAMES):
            self.log(log, f"Dossier Traefik sans compose: {self.traefik_dir}")
            return

        self.log(log, "Démarrage de Traefik...")
        code = self.stream(self.docker("compose", "up", "-d"), cwd=self.traefik_dir, log=log)
        if code != 0:
            raise RuntimeError("Impossible de démarrer Traefik.")

    def compose_container_ids(self, path):
        code, output = self.capture(self.docker("compose", "ps", "-aq"), cwd=path, timeout=10)
        if code != 0 or not output:
            return []
        return [line.strip() for line in output.splitlines() if line.strip()]

    def stale_macos_localtime_mounts(self, path):
        if platform.system() != "Darwin":
            return []

        mount_format = (
            '{{range .Mounts}}{{if eq .Destination "/etc/localtime"}}'
            "{{.Source}}{{end}}{{end}}"
        )
        stale = []
        for container_id in self.compose_container_ids(path):
            code, source = self.capture(
                self.docker("inspect", "-f", mount_format, container_id),
                timeout=5,
            )
            if code == 0 and source.strip():
                stale.append((container_id, source.strip()))
        return stale

    def stale_container_networks(self, path):
        network_format = (
            "{{range $name, $network := .NetworkSettings.Networks}}"
            "{{$name}}|{{$network.NetworkID}};{{end}}"
        )
        stale = []
        checked = set()
        for container_id in self.compose_container_ids(path):
            code, output = self.capture(
                self.docker("inspect", "-f", network_format, container_id),
                timeout=5,
            )
            if code != 0:
                continue
            for item in output.split(";"):
                if "|" not in item:
                    continue
                network_name, network_id = (part.strip() for part in item.split("|", 1))
                if not network_id or network_id in checked:
                    continue
                checked.add(network_id)
                inspect_code, inspect_output = self.capture(
                    self.docker("network", "inspect", network_id),
                    timeout=5,
                )
                error = inspect_output.lower()
                missing_network = (
                    "not found" in error
                    or "no such network" in error
                    or inspect_output.strip() in {"[]", "null"}
                )
                if inspect_code != 0 and missing_network:
                    stale.append((container_id, network_name, network_id))
        return stale

    def recreate_stale_containers(self, path, stale_mounts, stale_networks, log=None):
        if stale_mounts:
            self.log(log, "Ancien montage macOS /etc/localtime détecté dans les conteneurs.")
            for container_id, source in stale_mounts:
                self.log(log, f" - {container_id[:12]}: {source} -> /etc/localtime")
        if stale_networks:
            self.log(log, "Ancien réseau Docker supprimé détecté dans les conteneurs.")
            for container_id, network_name, network_id in stale_networks:
                self.log(
                    log,
                    f" - {container_id[:12]}: {network_name} ({network_id[:12]})",
                )
        self.log(log, "Recréation contrôlée des conteneurs; les volumes et dossiers de données sont conservés.")
        return self.stream(
            self.docker("compose", "up", "-d", "--force-recreate"),
            cwd=path,
            log=log,
        )

    def compose_up_project(self, project, path, log=None):
        stale_mounts = self.stale_macos_localtime_mounts(path)
        stale_networks = self.stale_container_networks(path)
        if stale_mounts or stale_networks:
            self.log(log, "Anomalie Docker détectée avant démarrage.")
            code = self.recreate_stale_containers(path, stale_mounts, stale_networks, log=log)
        else:
            self.log(log, "Démarrage des conteneurs existants sans recréation...")
            code = self.stream(self.docker("compose", "up", "-d", "--no-recreate"), cwd=path, log=log)

        if code != 0:
            stale_mounts = self.stale_macos_localtime_mounts(path)
            stale_networks = self.stale_container_networks(path)
            if stale_mounts or stale_networks:
                self.log(log, "")
                self.log(log, "Anomalie Docker apparue pendant le démarrage.")
                code = self.recreate_stale_containers(path, stale_mounts, stale_networks, log=log)
            elif self.is_running(f"odoo-{project}"):
                self.log(log, f"Docker Compose a retourné une erreur, mais odoo-{project} est déjà running.")
                self.log(log, "Le gestionnaire continue avec le conteneur existant.")
                return
        if code != 0:
            status_code, status = self.capture(
                self.docker("compose", "ps", "-a"),
                cwd=path,
                timeout=10,
            )
            if status_code == 0 and status:
                self.log(log, "")
                self.log(log, "État des conteneurs du projet:")
                self.log(log, status)
            raise RuntimeError(
                "Docker Compose n'a pas démarré correctement. "
                "Les conteneurs existants et les données ont été conservées."
            )

    def wait_for_container(self, container, max_wait=60, log=None, sleep=time.sleep):
        waited = 0
        while waited <= max_wait:
            status = self.container_status(container)
            self.log(log, f"Attente {container}... {waited}s/{max_wait}s ({status})")
            if status == "running":
                return
            if status not in {"absent", "created", "restarting"}:
                raise RuntimeError(f"Le conteneur {container} est en état {status}.")
            sleep(2)
            waited += 2
        raise RuntimeError(f"Le conteneur {container} n'est pas running après {max_wait}s.")

    def odoo_server_running(self, container):
        code, _ = self.capture(self.docker("exec", container, "sh", "-lc", "ps aux | grep -E '[o]doo-bin' >/dev/null 2>&1"), timeout=8)
        return code == 0

    def wait_odoo_port(self, container, max_wait=90, log=None, sleep=time.sleep):
        waited = 0
        command = (
            "import socket; "
            "s=socket.create_connection(('127.0.0.1', 8069), 2); "
            "s.close()"
        )
        while waited <= max_wait:
            self.log(log, f"Attente serveur Odoo... {waited}s/{max_wait}s")
            code, _ = self.capture(self.docker("exec", container, "python3", "-c", command), timeout=5)
            if code == 0:
                return
            sleep(2)
            waited += 2
        raise RuntimeError("Odoo ne répond pas sur le port 8069.")

    def start_odoo_server(self, project, log=None):
        container = f"odoo-{project}"
        if self.odoo_server_running(container):
            self.log(log, f"Serveur Odoo déjà démarré dans {container}")
        else:
            self.log(log, f"Démarrage du serveur Odoo dans {container}...")
            code = self.stream(
                self.docker(
                    "exec",
                    "-e",
                    "LOG_ATTACHMENTS=False",
                    "-d",
                    container,
                    "odoo",
                    "-c",
                    "/home/odoo/srv/conf/odoo.conf",
                    "--logfile=/home/odoo/srv/data/odoo.log",
                ),
                log=log,
            )
            if code != 0:
                raise RuntimeError("Impossible de démarrer le serveur Odoo dans le conteneur.")
        self.wait_odoo_port(container, log=log)

    def start_project(self, project, log=None):
        path = self.project_path(project)
        compose = self.compose_file(project)
        if not compose:
            raise RuntimeError(f"Projet introuvable ou sans fichier compose: {project}")
        self.fix_macos_localtime_mount(compose, log=log)
        self.start_traefik(log=log)
        self.log(log, f"Démarrage du projet {project}...")
        self.compose_up_project(project, path, log=log)
        self.wait_for_container(f"odoo-{project}", log=log)
        self.start_odoo_server(project, log=log)
        self.log(log, "")
        self.log(log, f"Projet démarré: {project}")
        self.log(log, f"URL Odoo: {self.project_url(project)}")

    def stop_project(self, project, log=None):
        path = self.project_path(project)
        if not self.compose_file(project):
            raise RuntimeError(f"Projet introuvable ou sans fichier compose: {project}")
        self.log(log, f"Arrêt du projet {project}")
        code = self.stream(self.docker("compose", "stop"), cwd=path, log=log)
        if code != 0:
            raise RuntimeError("Impossible d'arrêter Docker Compose proprement.")
        self.log(log, f"Projet arrêté: {project}")

    def update_project(self, project, log=None):
        path = self.project_path(project)
        compose = self.compose_file(project)
        if not compose:
            raise RuntimeError(f"Projet introuvable ou sans fichier compose: {project}")
        self.fix_macos_localtime_mount(compose, log=log)
        self.log(log, "")
        self.log(log, f"Mise à jour du projet {project}")
        if (path / ".git").exists():
            self.log(log, "Git pull...")
            code = self.stream(["git", "pull", "--ff-only"], cwd=path, log=log)
            if code != 0:
                raise RuntimeError(f"Git pull impossible pour {project}.")
        else:
            self.log(log, "Pas de dépôt Git dans ce projet.")

        self.log(log, "Docker pull...")
        code = self.stream(self.docker("compose", "pull"), cwd=path, log=log)
        if code != 0:
            raise RuntimeError(f"Docker pull impossible pour {project}.")

        self.log(log, "Redémarrage compose...")
        self.compose_up_project(project, path, log=log)
        self.log(log, f"Mise à jour terminée: {project}")

    def update_all_projects(self, log=None):
        for project in self.list_projects():
            self.update_project(project, log=log)
