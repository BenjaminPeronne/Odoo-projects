import http.client
import os
import platform
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from .system import docker_command
from .platform import (
    command_uses_wsl,
    executable_search_path,
    hidden_process_kwargs,
    host_executable_available,
    resolve_host_executable,
    workspace_execution_path,
    workspace_wsl_context,
    wsl_command_prefix,
    wsl_command_with_cwd,
    wsl_executable_available,
)


COMPOSE_FILENAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
ODOO_STARTUP_LOG = "/home/odoo/srv/data/odoo-manager-startup.log"
ODOO_STARTUP_STATUS = "/home/odoo/srv/data/odoo-manager-startup.status"
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
    def __init__(self, settings, workspace, traefik_dir=None, runner=None, http_probe=None):
        self.settings = settings
        self.workspace = Path(workspace)
        self.traefik_dir = Path(traefik_dir).expanduser() if traefik_dir else None
        self.runner = runner
        self.http_probe = http_probe

    def env(self):
        env = os.environ.copy()
        env["PATH"] = executable_search_path()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env.setdefault(
            "GIT_SSH_COMMAND",
            "ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new",
        )
        return env

    def log(self, callback, message):
        if callback:
            callback(message)

    def stream(self, command, cwd=None, log=None):
        cwd = Path(cwd or self.workspace)
        command, process_cwd = self.prepare_command(command, cwd)
        if self.runner:
            return self.runner.stream(command, cwd=process_cwd, log=log)

        self.log(log, "$ " + " ".join(str(arg) for arg in command))
        process = subprocess.Popen(
            command,
            cwd=str(process_cwd),
            env=self.env(),
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
        cwd = Path(cwd or self.workspace)
        command, process_cwd = self.prepare_command(command, cwd)
        if self.runner:
            return self.runner.capture(command, cwd=process_cwd, timeout=timeout)

        try:
            result = subprocess.run(
                command,
                cwd=str(process_cwd),
                env=self.env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                **hidden_process_kwargs(),
            )
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            if result.returncode == 0:
                return result.returncode, stdout or stderr
            return result.returncode, "\n".join(part for part in (stdout, stderr) if part)
        except OSError as exc:
            return 127, str(exc)
        except subprocess.TimeoutExpired as exc:
            return 124, (exc.stdout or "").strip()

    def docker(self, *arguments):
        return docker_command(self.settings, *arguments)

    def prepare_command(self, command, cwd):
        command = [str(argument) for argument in command]
        if platform.system() != "Windows" or not command_uses_wsl(command):
            return command, cwd
        prepared = wsl_command_with_cwd(command, cwd, self.settings, self.workspace)
        return prepared, Path.home()

    def git(self, *arguments):
        context = workspace_wsl_context(self.settings, self.workspace) if platform.system() == "Windows" else None
        distribution = context.distribution if context else self.settings.wsl_distribution
        native_available = host_executable_available("git")
        wsl_available = wsl_executable_available("git", distribution)
        if (context and wsl_available) or (not native_available and wsl_available):
            return [*wsl_command_prefix(distribution), "git", *arguments]
        return [resolve_host_executable("git"), *arguments]

    def command_path(self, command, path):
        if command_uses_wsl(command):
            return workspace_execution_path(path, self.settings, self.workspace)
        return str(path)

    def project_path(self, project):
        return self.workspace / project

    def compose_file(self, project):
        path = self.project_path(project)
        for name in COMPOSE_FILENAMES:
            candidate = path / name
            try:
                exists = candidate.exists()
            except OSError:
                return None
            if exists:
                return candidate
        return None

    def list_projects(self):
        try:
            workspace_available = self.workspace.is_dir()
        except OSError:
            return []
        if not workspace_available:
            return []
        projects = []
        try:
            items = tuple(self.workspace.iterdir())
        except OSError:
            return []
        for item in items:
            try:
                is_directory = item.is_dir()
            except OSError:
                continue
            if not is_directory:
                continue
            try:
                has_compose = any((item / name).exists() for name in COMPOSE_FILENAMES)
            except OSError:
                continue
            if has_compose:
                projects.append(item.name)
        return sorted(projects)

    def container_status(self, container):
        code, output = self.capture(self.docker("inspect", "-f", "{{.State.Status}}", container), timeout=5)
        if code != 0 or not output:
            return "absent"
        return output.splitlines()[0].strip()

    def is_running(self, container):
        return self.container_status(container) == "running"

    def container_health(self, container):
        health_format = "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
        code, output = self.capture(
            self.docker("inspect", "-f", health_format, container),
            timeout=5,
        )
        if code != 0 or not output:
            return "absent"
        return output.splitlines()[0].strip().lower()

    def container_startup_diagnostics(self, container, log=None):
        health_format = "{{if .State.Health}}{{json .State.Health}}{{else}}null{{end}}"
        health_code, health = self.capture(
            self.docker("inspect", "-f", health_format, container),
            timeout=8,
        )
        logs_code, logs = self.capture(
            self.docker("logs", "--tail", "80", container),
            timeout=12,
        )
        if health_code == 0 and health and health != "null":
            self.log(log, "Dernier état du contrôle de santé PostgreSQL:")
            self.log(log, health)
        if logs_code == 0 and logs:
            self.log(log, "Derniers logs PostgreSQL:")
            self.log(log, logs)

    def wait_for_postgres(self, project, max_wait=180, log=None, sleep=None):
        sleep = sleep or time.sleep
        container = f"postgresql-{project}"
        waited = 0
        last_state = None
        while waited <= max_wait:
            status = self.container_status(container)
            health = self.container_health(container) if status == "running" else "none"
            state = (status, health)
            if state != last_state or waited % 10 == 0:
                self.log(
                    log,
                    f"Attente PostgreSQL... {waited}s/{max_wait}s "
                    f"(conteneur: {status}, santé: {health})",
                )
                last_state = state
            if status == "running" and health in {"healthy", "none"}:
                return
            if status in {"dead", "exited", "paused"}:
                self.container_startup_diagnostics(container, log=log)
                raise RuntimeError(f"PostgreSQL s'est arrêté pendant son démarrage ({status}).")
            sleep(2)
            waited += 2

        self.container_startup_diagnostics(container, log=log)
        raise RuntimeError(
            f"PostgreSQL n'est pas devenu sain après {max_wait}s. "
            "Consulte les contrôles de santé et les logs affichés ci-dessus."
        )

    def recover_postgres_dependency(self, project, path, log=None):
        container = f"postgresql-{project}"
        status = self.container_status(container)
        health = self.container_health(container) if status == "running" else "none"
        if status != "running" or health not in {"starting", "unhealthy", "healthy"}:
            return None

        self.log(log, "")
        self.log(
            log,
            "PostgreSQL est encore en phase de démarrage. "
            "Le gestionnaire attend sa disponibilité sans recréer le volume de données.",
        )
        self.wait_for_postgres(project, log=log)
        self.log(log, "PostgreSQL est prêt. Reprise du démarrage Odoo...")
        return self.stream(
            self.docker("compose", "up", "-d", "--no-recreate"),
            cwd=path,
            log=log,
        )

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

    def install_traefik(self, repository, log=None):
        if not self.traefik_dir:
            raise RuntimeError("Le dossier Traefik n'est pas configuré.")
        if self.traefik_dir.name != "traefik":
            raise RuntimeError("Le dossier Traefik doit se terminer par docker-local-tools/traefik.")

        tools_dir = self.traefik_dir.parent
        parent_dir = tools_dir.parent
        compose_ready = any((self.traefik_dir / name).is_file() for name in COMPOSE_FILENAMES)
        git = self.git

        if compose_ready and not (tools_dir / ".git").is_dir():
            self.log(log, f"Traefik déjà présent: {self.traefik_dir}")
            self.start_traefik(log=log)
            return

        if tools_dir.exists():
            if not (tools_dir / ".git").is_dir():
                raise RuntimeError(f"Le dossier {tools_dir} existe mais n'est pas un dépôt Git exploitable.")
            self.log(log, "Mise à jour de docker-local-tools...")
            code = self.stream(git("pull", "--ff-only"), cwd=tools_dir, log=log)
            if code != 0:
                raise RuntimeError("Impossible de mettre à jour docker-local-tools.")
        else:
            parent_dir.mkdir(parents=True, exist_ok=True)
            temporary = parent_dir / f".{tools_dir.name}.odoo-manager-{uuid.uuid4().hex}"
            self.log(log, "Installation de docker-local-tools...")
            try:
                clone_prefix = git("clone", repository)
                clone_destination = self.command_path(clone_prefix, temporary)
                code = self.stream([*clone_prefix, clone_destination], cwd=parent_dir, log=log)
                if code != 0:
                    raise RuntimeError("Impossible de cloner docker-local-tools. Vérifie Git et ta clé SSH GitLab.")
                temporary_traefik = temporary / "traefik"
                if not any((temporary_traefik / name).is_file() for name in COMPOSE_FILENAMES):
                    raise RuntimeError("Le dépôt docker-local-tools ne contient pas de configuration Traefik valide.")
                temporary.replace(tools_dir)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)

        if not any((self.traefik_dir / name).is_file() for name in COMPOSE_FILENAMES):
            raise RuntimeError(f"Configuration Traefik introuvable après installation: {self.traefik_dir}")
        self.start_traefik(log=log)

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
            else:
                recovered_code = self.recover_postgres_dependency(project, path, log=log)
                if recovered_code is not None:
                    code = recovered_code
            if code != 0 and self.is_running(f"odoo-{project}"):
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

    def wait_for_odoo_container_initialization(
        self,
        container,
        max_wait=600,
        log=None,
        sleep=None,
    ):
        """Wait until the image entrypoint has installed project dependencies."""
        sleep = sleep or time.sleep
        waited = 0
        init_command = "tr '\\000' ' ' </proc/1/cmdline 2>/dev/null || true"
        announced = False

        while waited <= max_wait:
            status = self.container_status(container)
            if status != "running":
                self.odoo_startup_diagnostics(container, log=log)
                raise RuntimeError(
                    f"Le conteneur {container} s'est arrêté pendant sa préparation ({status})."
                )

            code, command = self.capture(
                self.docker("exec", container, "sh", "-lc", init_command),
                timeout=8,
            )
            initializing = code != 0 or "/init.sh" in command
            if not initializing:
                if announced:
                    self.log(log, "Préparation du conteneur Odoo terminée.")
                return

            if not announced:
                self.log(
                    log,
                    "Préparation du conteneur Odoo: installation des dépendances système et Python...",
                )
                announced = True
            elif waited % 10 == 0:
                self.log(log, f"Préparation du conteneur Odoo... {waited}s/{max_wait}s")

            sleep(2)
            waited += 2

        self.odoo_startup_diagnostics(container, log=log)
        raise RuntimeError(
            f"La préparation du conteneur Odoo dépasse {max_wait}s. "
            "Consulte les logs affichés ci-dessus."
        )

    def odoo_server_running(self, container):
        process_command = (
            "ps -eo args | "
            "grep -E '([/][o]doo-bin|[/][o]doo)( |$)' >/dev/null 2>&1"
        )
        code, _ = self.capture(
            self.docker("exec", container, "sh", "-lc", process_command),
            timeout=8,
        )
        return code == 0

    def odoo_startup_diagnostics(self, container, log=None):
        commands = (
            (
                "Résultat du dernier lancement Odoo:",
                self.docker(
                    "exec",
                    container,
                    "sh",
                    "-lc",
                    f"test -f {ODOO_STARTUP_STATUS} && cat {ODOO_STARTUP_STATUS} || true",
                ),
                8,
            ),
            (
                "Sortie du dernier lancement Odoo:",
                self.docker(
                    "exec",
                    container,
                    "sh",
                    "-lc",
                    f"tail -n 160 {ODOO_STARTUP_LOG} 2>/dev/null || true",
                ),
                12,
            ),
            (
                "Processus dans le conteneur Odoo:",
                self.docker(
                    "exec",
                    container,
                    "sh",
                    "-lc",
                    "ps -eo pid,args | grep -E '[o]doo|[p]ython' | tail -n 30 || true",
                ),
                8,
            ),
            (
                "Derniers logs Odoo:",
                self.docker(
                    "exec",
                    container,
                    "sh",
                    "-lc",
                    "tail -n 120 /home/odoo/srv/data/odoo.log 2>/dev/null || true",
                ),
                12,
            ),
            (
                "Derniers logs du conteneur Odoo:",
                self.docker("logs", "--tail", "80", container),
                12,
            ),
        )
        for title, command, timeout in commands:
            code, output = self.capture(command, timeout=timeout)
            if code == 0 and output:
                self.log(log, title)
                self.log(log, output)

    def wait_odoo_port(self, container, max_wait=300, log=None, sleep=None):
        sleep = sleep or time.sleep
        waited = 0
        command = (
            "import socket; "
            "s=socket.create_connection(('127.0.0.1', 8069), 2); "
            "s.close()"
        )
        while waited <= max_wait:
            if waited == 0 or waited % 10 == 0:
                self.log(log, f"Attente serveur Odoo... {waited}s/{max_wait}s")
            code, _ = self.capture(self.docker("exec", container, "python3", "-c", command), timeout=5)
            if code == 0:
                return
            if waited >= 4 and not self.odoo_server_running(container):
                self.odoo_startup_diagnostics(container, log=log)
                raise RuntimeError(
                    "Le processus Odoo s'est arrêté avant d'ouvrir le port 8069. "
                    "Consulte les logs Odoo affichés ci-dessus."
                )
            sleep(2)
            waited += 2
        self.odoo_startup_diagnostics(container, log=log)
        raise RuntimeError(
            f"Odoo fonctionne mais ne répond pas sur le port 8069 après {max_wait}s. "
            "Consulte les logs Odoo affichés ci-dessus."
        )

    @staticmethod
    def http_status(url, timeout=3):
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        if not host:
            return 0
        connect_host = host if host in {"127.0.0.1", "localhost", "::1"} else "127.0.0.1"
        connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_class(connect_host, parsed.port, timeout=timeout)
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            host_header = host if parsed.port in {None, 80, 443} else f"{host}:{parsed.port}"
            connection.request(
                "GET",
                path,
                headers={"Host": host_header, "User-Agent": "Odoo-Manager/readiness"},
            )
            response = connection.getresponse()
            return response.status
        except (OSError, http.client.HTTPException):
            return 0
        finally:
            connection.close()

    def wait_project_http(self, project, max_wait=30, log=None, sleep=None):
        sleep = sleep or time.sleep
        url = urllib.parse.urljoin(self.project_url(project), "web/login")
        probe = self.http_probe or self.http_status
        if self.runner is not None and self.http_probe is None:
            return

        waited = 0
        last_status = None
        while waited <= max_wait:
            status = probe(url)
            if status != last_status or waited % 10 == 0:
                displayed = status or "indisponible"
                self.log(log, f"Vérification de l'accès Odoo... {waited}s/{max_wait}s (HTTP {displayed})")
                last_status = status
            if 200 <= status < 500 and status != 404:
                return
            sleep(2)
            waited += 2
        raise RuntimeError(
            "Odoo répond dans son conteneur, mais Traefik ne fournit pas encore l'URL locale. "
            "Le navigateur n'a pas été ouvert afin d'éviter une page Bad Gateway."
        )

    def start_odoo_server(self, project, log=None):
        container = f"odoo-{project}"
        if self.odoo_server_running(container):
            self.log(log, f"Serveur Odoo déjà démarré dans {container}")
        else:
            self.log(log, f"Démarrage du serveur Odoo dans {container}...")
            launch_command = (
                f"rm -f {ODOO_STARTUP_STATUS}; "
                f": > {ODOO_STARTUP_LOG}; "
                "if [ -x /home/_venv/bin/python ] && "
                "[ -f /home/odoo/srv/server/odoo/odoo-bin ]; then "
                "/home/_venv/bin/python /home/odoo/srv/server/odoo/odoo-bin "
                "-c /home/odoo/srv/conf/odoo.conf "
                "--logfile=/home/odoo/srv/data/odoo.log; "
                "else "
                "odoo -c /home/odoo/srv/conf/odoo.conf "
                "--logfile=/home/odoo/srv/data/odoo.log; "
                "fi "
                f">> {ODOO_STARTUP_LOG} 2>&1; "
                "code=$?; "
                f"printf '%s\\n' \"$code\" > {ODOO_STARTUP_STATUS}; "
                "exit \"$code\""
            )
            code = self.stream(
                self.docker(
                    "exec",
                    "-e",
                    "LOG_ATTACHMENTS=False",
                    "-d",
                    container,
                    "sh",
                    "-lc",
                    launch_command,
                ),
                log=log,
            )
            if code != 0:
                raise RuntimeError("Impossible de démarrer le serveur Odoo dans le conteneur.")
        self.wait_odoo_port(container, log=log)

    def stop_odoo_server(self, project, log=None, max_wait=30, sleep=None):
        sleep = sleep or time.sleep
        container = f"odoo-{project}"
        if not self.odoo_server_running(container):
            return
        self.log(log, f"Arrêt du serveur Odoo dans {container}...")
        code = self.stream(
            self.docker(
                "exec",
                container,
                "sh",
                "-lc",
                "pkill -TERM -f '[o]doo-bin' || pkill -TERM -f '[ /]odoo ' || true",
            ),
            log=log,
        )
        if code != 0:
            raise RuntimeError("Impossible d'arrêter le serveur Odoo avant l'opération module.")
        waited = 0
        while waited <= max_wait:
            if not self.odoo_server_running(container):
                return
            sleep(1)
            waited += 1
        raise RuntimeError("Le serveur Odoo ne s'est pas arrêté dans le délai prévu.")

    def install_project_pip_requirements(self, project, log=None):
        requirements = self.project_path(project) / "init" / "requirements_pip.txt"
        try:
            has_requirements = any(
                line.strip() and not line.lstrip().startswith("#")
                for line in requirements.read_text(encoding="utf-8", errors="ignore").splitlines()
            )
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RuntimeError(f"Impossible de lire {requirements}: {exc}") from exc
        if not has_requirements:
            return
        self.log(log, "Vérification des dépendances Python du projet...")
        code = self.stream(
            self.docker(
                "exec",
                f"odoo-{project}",
                "/home/_venv/bin/python",
                "-m",
                "pip",
                "install",
                "-r",
                "/conf/requirements_pip.txt",
            ),
            log=log,
        )
        if code != 0:
            raise RuntimeError("L'installation des dépendances Python du projet a échoué.")

    def run_odoo_module_command(self, project, db_name, modules, option="-u", log=None):
        if option not in {"-i", "-u"}:
            raise ValueError("Option module Odoo invalide.")
        container = f"odoo-{project}"
        postgres = f"postgresql-{project}"
        if not self.is_running(container) or not self.is_running(postgres):
            self.start_project(project, log=log)
        else:
            self.wait_for_odoo_container_initialization(container, log=log)
        self.install_project_pip_requirements(project, log=log)

        action = "installation" if option == "-i" else "mise à jour"
        self.log(log, "")
        self.log(log, f"Commande Odoo ({action})")
        self.log(log, f"Projet: {project}")
        self.log(log, f"Base: {db_name}")
        self.log(log, f"Module(s): {modules}")
        self.log(log, f"Équivalent: odoo -d {db_name} {option} {modules} --stop-after-init")

        self.stop_odoo_server(project, log=log)
        command = self.docker(
            "exec",
            "-e",
            "LOG_ATTACHMENTS=False",
            container,
            "odoo",
            "-c",
            "/home/odoo/srv/conf/odoo.conf",
            "-d",
            db_name,
            option,
            modules,
            "--stop-after-init",
        )
        code = None
        try:
            code = self.stream(command, log=log)
            if code != 0:
                self.odoo_startup_diagnostics(container, log=log)
                raise RuntimeError(f"La commande Odoo a échoué avec le code {code}.")
        finally:
            self.log(log, "Redémarrage du serveur Odoo...")
            self.start_odoo_server(project, log=log)
        self.wait_project_http(project, log=log)
        self.log(log, "Opération module terminée.")
        self.log(log, f"URL Odoo: {self.project_url(project)}")

    def run_odoo_uninstall_command(self, project, db_name, modules, log=None):
        container = f"odoo-{project}"
        postgres = f"postgresql-{project}"
        if not self.is_running(container) or not self.is_running(postgres):
            self.start_project(project, log=log)
        else:
            self.wait_for_odoo_container_initialization(container, log=log)

        self.log(log, "")
        self.log(log, "Commande Odoo (désinstallation)")
        self.log(log, f"Projet: {project}")
        self.log(log, f"Base: {db_name}")
        self.log(log, f"Module(s): {modules}")

        uninstall_script = """import os

module_names = [name.strip() for name in os.environ.get("MODULE_NAMES", "").split(",") if name.strip()]
if not module_names:
    raise SystemExit("Aucun module fourni.")

modules = env["ir.module.module"].search([("name", "in", module_names)])
found = set(modules.mapped("name"))
missing = sorted(set(module_names) - found)
if missing:
    print("Module(s) introuvable(s): " + ", ".join(missing))

installed = modules.filtered(lambda module: module.state == "installed")
skipped = modules - installed
if skipped:
    print("Module(s) ignoré(s) car non installé(s): " + ", ".join(skipped.mapped("name")))

if not installed:
    raise SystemExit("Aucun module installé à désinstaller.")

print("Désinstallation: " + ", ".join(installed.mapped("name")))
installed.button_immediate_uninstall()
env.cr.commit()
print("Désinstallation terminée.")
"""
        shell_command = (
            "odoo shell -c /home/odoo/srv/conf/odoo.conf "
            "-d \"$ODOO_DB_NAME\" --no-http <<'ODOO_MANAGER_PY'\n"
            f"{uninstall_script}ODOO_MANAGER_PY"
        )

        self.stop_odoo_server(project, log=log)
        command = self.docker(
            "exec",
            "-e",
            "LOG_ATTACHMENTS=False",
            "-e",
            f"ODOO_DB_NAME={db_name}",
            "-e",
            f"MODULE_NAMES={modules}",
            container,
            "sh",
            "-lc",
            shell_command,
        )
        try:
            code = self.stream(command, log=log)
            if code != 0:
                self.odoo_startup_diagnostics(container, log=log)
                raise RuntimeError(f"La désinstallation Odoo a échoué avec le code {code}.")
        finally:
            self.log(log, "Redémarrage du serveur Odoo...")
            self.start_odoo_server(project, log=log)
        self.wait_project_http(project, log=log)
        self.log(log, "Désinstallation terminée.")
        self.log(log, f"URL Odoo: {self.project_url(project)}")

    def start_project(self, project, log=None):
        path = self.project_path(project)
        compose = self.compose_file(project)
        if not compose:
            raise RuntimeError(f"Projet introuvable ou sans fichier compose: {project}")
        self.fix_macos_localtime_mount(compose, log=log)
        self.start_traefik(log=log)
        self.log(log, f"Démarrage du projet {project}...")
        self.compose_up_project(project, path, log=log)
        container = f"odoo-{project}"
        self.wait_for_container(container, log=log)
        self.wait_for_odoo_container_initialization(container, log=log)
        self.start_odoo_server(project, log=log)
        self.wait_project_http(project, log=log)
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
            code = self.stream(self.git("pull", "--ff-only"), cwd=path, log=log)
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
