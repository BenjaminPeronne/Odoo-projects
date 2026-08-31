import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from odoo_manager_core.config import ManagerSettings
from odoo_manager_core.project_service import ProjectService


def has_command_tail(commands, tail):
    return any(command[-len(tail):] == tail for command in commands)


class FakeRunner:
    def __init__(self):
        self.streams = []
        self.captures = []
        self.statuses = {}
        self.health_statuses = {}
        self.odoo_server_running = True
        self.odoo_port_ready = True
        self.odoo_port_states = []
        self.stream_codes = []
        self.compose_container_ids = []
        self.localtime_mounts = {}
        self.container_networks = {}
        self.missing_networks = set()
        self.missing_network_outputs = {}
        self.networks_missing_after_stream = set()

    def stream(self, command, cwd=None, log=None):
        self.streams.append((list(command), Path(cwd) if cwd else None))
        if log:
            log("$ " + " ".join(command))
        code = self.stream_codes.pop(0) if self.stream_codes else 0
        self.missing_networks.update(self.networks_missing_after_stream)
        self.networks_missing_after_stream.clear()
        return code

    def capture(self, command, cwd=None, timeout=10):
        self.captures.append((list(command), Path(cwd) if cwd else None, timeout))
        command = list(command)
        if command[-3:] == ["compose", "ps", "-aq"]:
            output = "\n".join(self.compose_container_ids)
            return (0, output) if output else (0, "")
        if len(command) >= 5 and command[1:3] == ["inspect", "-f"] and "/etc/localtime" in command[3]:
            return 0, self.localtime_mounts.get(command[4], "")
        if len(command) >= 5 and command[1:3] == ["inspect", "-f"] and "NetworkSettings.Networks" in command[3]:
            networks = self.container_networks.get(command[4], {})
            output = "".join(f"{name}|{network_id};" for name, network_id in networks.items())
            return 0, output
        if len(command) >= 3 and command[-3:-1] == ["network", "inspect"]:
            network_id = command[-1]
            if network_id in self.missing_networks:
                return 1, self.missing_network_outputs.get(
                    network_id,
                    f"Error response from daemon: network {network_id} not found",
                )
            return 0, "[]"
        if len(command) >= 5 and command[1:4] == ["inspect", "-f", "{{.State.Status}}"]:
            status = self.statuses.get(command[4], "absent")
            return (0, status) if status != "absent" else (1, "")
        if len(command) >= 5 and command[1:3] == ["inspect", "-f"] and ".State.Health.Status" in command[3]:
            statuses = self.health_statuses.get(command[4], "none")
            if isinstance(statuses, list):
                status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
            else:
                status = statuses
            return (0, status) if status != "absent" else (1, "")
        if len(command) >= 5 and command[1:3] == ["inspect", "-f"] and "json .State.Health" in command[3]:
            return 0, '{"Status":"unhealthy"}'
        if len(command) >= 4 and command[1:3] == ["logs", "--tail"]:
            if command[-1].startswith("odoo-"):
                return 0, "odoo container startup log"
            return 0, "database system is starting up"
        if len(command) >= 5 and command[1:3] == ["exec", "odoo-DEMO"] and command[3:5] == ["sh", "-lc"]:
            if "tail -n 120" in command[-1]:
                return 0, "odoo startup traceback"
            if "tail -n 30" in command[-1]:
                return 0, "42 python3 /home/odoo/srv/server/odoo/odoo-bin"
            return (0, "") if self.odoo_server_running else (1, "")
        if len(command) >= 5 and command[1:3] == ["exec", "odoo-DEMO"] and command[3:5] == ["python3", "-c"]:
            if self.odoo_port_states:
                ready = self.odoo_port_states.pop(0)
            else:
                ready = self.odoo_port_ready
            return (0, "") if ready else (1, "")
        if len(command) >= 3 and command[1:3] == ["port", "odoo-DEMO"]:
            return 1, ""
        return 0, ""


class ProjectServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project_path = self.root / "DEMO"
        self.project_path.mkdir()
        (self.project_path / "compose.yml").write_text("services: {}\n", encoding="utf-8")
        self.settings = ManagerSettings.from_dict({}, str(self.root))
        self.runner = FakeRunner()
        self.service = ProjectService(self.settings, self.root, runner=self.runner)

    def tearDown(self):
        self.temporary.cleanup()

    def test_start_project_reuses_existing_containers_without_recreate(self):
        self.runner.statuses = {
            "odoo-DEMO": "running",
            "postgresql-DEMO": "running",
        }

        self.service.start_project("DEMO", log=lambda _line: None)

        commands = [command for command, _cwd in self.runner.streams]
        self.assertTrue(has_command_tail(commands, ["compose", "up", "-d", "--no-recreate"]))
        self.assertFalse(has_command_tail(commands, ["compose", "up", "--pull", "always", "-d"]))

    def test_start_project_creates_missing_containers_without_forced_pull(self):
        self.runner.statuses = {
            "odoo-DEMO": "absent",
            "postgresql-DEMO": "absent",
        }

        self.service.compose_up_project("DEMO", self.project_path, log=lambda _line: None)

        commands = [command for command, _cwd in self.runner.streams]
        self.assertTrue(has_command_tail(commands, ["compose", "up", "-d", "--no-recreate"]))
        self.assertFalse(any("--pull" in command for command in commands))

    @patch("odoo_manager_core.project_service.time.sleep", return_value=None)
    def test_start_project_waits_for_slow_postgres_then_resumes_compose(self, _sleep):
        self.runner.stream_codes = [1, 0]
        self.runner.statuses = {
            "postgresql-DEMO": "running",
            "odoo-DEMO": "running",
        }
        self.runner.health_statuses = {
            "postgresql-DEMO": ["unhealthy", "starting", "starting", "healthy"],
        }
        logs = []

        self.service.start_project("DEMO", log=logs.append)

        commands = [command for command, _cwd in self.runner.streams]
        compose_starts = [command for command in commands if command[-4:] == ["compose", "up", "-d", "--no-recreate"]]
        self.assertEqual(len(compose_starts), 2)
        self.assertTrue(any("encore en phase de démarrage" in line for line in logs))
        self.assertTrue(any("Reprise du démarrage Odoo" in line for line in logs))

    def test_start_project_waits_until_traefik_stops_returning_bad_gateway(self):
        statuses = iter((502, 502, 303))
        service = ProjectService(
            self.settings,
            self.root,
            runner=self.runner,
            http_probe=lambda _url: next(statuses),
        )
        logs = []

        service.wait_project_http("DEMO", log=logs.append, sleep=lambda _seconds: None)

        self.assertTrue(any("HTTP 502" in line for line in logs))
        self.assertTrue(any("HTTP 303" in line for line in logs))

    def test_wait_odoo_port_allows_slow_running_process(self):
        self.runner.odoo_port_states = [False, False, False, True]
        logs = []

        self.service.wait_odoo_port(
            "odoo-DEMO",
            max_wait=20,
            log=logs.append,
            sleep=lambda _seconds: None,
        )

        self.assertFalse(any("Derniers logs Odoo" in line for line in logs))

    def test_wait_odoo_port_stops_early_and_reports_logs_when_process_exits(self):
        self.runner.odoo_port_ready = False
        self.runner.odoo_server_running = False
        logs = []

        with self.assertRaisesRegex(RuntimeError, "processus Odoo s'est arrêté"):
            self.service.wait_odoo_port(
                "odoo-DEMO",
                max_wait=300,
                log=logs.append,
                sleep=lambda _seconds: None,
            )

        self.assertIn("odoo startup traceback", logs)
        self.assertIn("odoo container startup log", logs)

    @patch("odoo_manager_core.project_service.http.client.HTTPConnection")
    def test_http_probe_uses_loopback_with_traefik_host_header(self, connection_class):
        connection = connection_class.return_value
        connection.getresponse.return_value.status = 303

        status = ProjectService.http_status("http://dev.Caritel_v18.localhost/web/login")

        self.assertEqual(status, 303)
        connection_class.assert_called_once_with("127.0.0.1", None, timeout=3)
        connection.request.assert_called_once_with(
            "GET",
            "/web/login",
            headers={
                "Host": "dev.caritel_v18.localhost",
                "User-Agent": "Odoo-Manager/readiness",
            },
        )

    @patch("odoo_manager_core.project_service.platform.system", return_value="Darwin")
    def test_start_project_recovers_stale_macos_localtime_mount(self, _system):
        self.runner.stream_codes = [0]
        self.runner.compose_container_ids = ["postgres-id", "odoo-id"]
        self.runner.localtime_mounts = {
            "postgres-id": "/etc/localtime",
            "odoo-id": "/etc/localtime",
        }

        self.service.compose_up_project("DEMO", self.project_path, log=lambda _line: None)

        commands = [command for command, _cwd in self.runner.streams]
        self.assertFalse(has_command_tail(commands, ["compose", "up", "-d", "--no-recreate"]))
        self.assertTrue(has_command_tail(commands, ["compose", "up", "-d", "--force-recreate"]))

    @patch("odoo_manager_core.project_service.platform.system", return_value="Linux")
    def test_start_project_does_not_recreate_after_unrelated_compose_failure(self, _system):
        self.runner.stream_codes = [1]

        with self.assertRaisesRegex(RuntimeError, "données ont été conservées"):
            self.service.compose_up_project("DEMO", self.project_path, log=lambda _line: None)

        commands = [command for command, _cwd in self.runner.streams]
        self.assertFalse(any("--force-recreate" in command for command in commands))

    @patch("odoo_manager_core.project_service.platform.system", return_value="Linux")
    def test_start_project_recovers_containers_attached_to_deleted_network(self, _system):
        stale_network_id = "7b0c5d442968cc9b1ad34b9442c0f1c0c0b0d61dffbc0c0808261b30aa394c14"
        self.runner.stream_codes = [0]
        self.runner.compose_container_ids = ["postgres-id", "odoo-id"]
        self.runner.container_networks = {
            "postgres-id": {"traefik-local": stale_network_id},
            "odoo-id": {"traefik-local": stale_network_id},
        }
        self.runner.missing_networks = {stale_network_id}

        logs = []
        self.service.compose_up_project("DEMO", self.project_path, log=logs.append)

        commands = [command for command, _cwd in self.runner.streams]
        self.assertFalse(has_command_tail(commands, ["compose", "up", "-d", "--no-recreate"]))
        self.assertTrue(has_command_tail(commands, ["compose", "up", "-d", "--force-recreate"]))
        self.assertTrue(any("Ancien réseau Docker supprimé" in line for line in logs))

    @patch("odoo_manager_core.project_service.platform.system", return_value="Linux")
    def test_deleted_network_empty_json_output_is_treated_as_missing(self, _system):
        stale_network_id = "7b0c5d442968cc9b1ad34b9442c0f1c0c0b0d61dffbc0c0808261b30aa394c14"
        self.runner.compose_container_ids = ["postgres-id"]
        self.runner.container_networks = {
            "postgres-id": {"traefik-local": stale_network_id},
        }
        self.runner.missing_networks = {stale_network_id}
        self.runner.missing_network_outputs = {stale_network_id: "[]"}

        self.assertEqual(
            self.service.stale_container_networks(self.project_path),
            [("postgres-id", "traefik-local", stale_network_id)],
        )

    @patch("odoo_manager_core.project_service.platform.system", return_value="Linux")
    def test_start_project_recovers_network_deleted_during_compose_up(self, _system):
        stale_network_id = "7b0c5d442968cc9b1ad34b9442c0f1c0c0b0d61dffbc0c0808261b30aa394c14"
        self.runner.stream_codes = [1, 0]
        self.runner.compose_container_ids = ["postgres-id", "odoo-id"]
        self.runner.container_networks = {
            "postgres-id": {"traefik-local": stale_network_id},
            "odoo-id": {"traefik-local": stale_network_id},
        }
        self.runner.networks_missing_after_stream = {stale_network_id}

        self.service.compose_up_project("DEMO", self.project_path, log=lambda _line: None)

        commands = [command for command, _cwd in self.runner.streams]
        self.assertTrue(has_command_tail(commands, ["compose", "up", "-d", "--no-recreate"]))
        self.assertTrue(has_command_tail(commands, ["compose", "up", "-d", "--force-recreate"]))

    @patch("odoo_manager_core.project_service.platform.system", return_value="Linux")
    def test_deleted_network_recovery_is_not_skipped_when_odoo_is_still_running(self, _system):
        stale_network_id = "7b0c5d442968cc9b1ad34b9442c0f1c0c0b0d61dffbc0c0808261b30aa394c14"
        self.runner.stream_codes = [0]
        self.runner.statuses = {"odoo-DEMO": "running"}
        self.runner.compose_container_ids = ["postgres-id", "odoo-id"]
        self.runner.container_networks = {
            "postgres-id": {"traefik-local": stale_network_id},
            "odoo-id": {"traefik-local": stale_network_id},
        }
        self.runner.missing_networks = {stale_network_id}

        self.service.compose_up_project("DEMO", self.project_path, log=lambda _line: None)

        commands = [command for command, _cwd in self.runner.streams]
        self.assertTrue(has_command_tail(commands, ["compose", "up", "-d", "--force-recreate"]))

    @patch("odoo_manager_core.project_service.platform.system", return_value="Linux")
    def test_start_project_does_not_recreate_when_container_network_still_exists(self, _system):
        network_id = "6654d1b678b2d875eba39291d607c4709910ad8731452594a54829650fb05fcc"
        self.runner.stream_codes = [1]
        self.runner.compose_container_ids = ["postgres-id", "odoo-id"]
        self.runner.container_networks = {
            "postgres-id": {"traefik-local": network_id},
            "odoo-id": {"traefik-local": network_id},
        }

        with self.assertRaisesRegex(RuntimeError, "données ont été conservées"):
            self.service.compose_up_project("DEMO", self.project_path, log=lambda _line: None)

        commands = [command for command, _cwd in self.runner.streams]
        self.assertFalse(any("--force-recreate" in command for command in commands))

    def test_update_project_pulls_git_and_compose(self):
        (self.project_path / ".git").mkdir()

        self.service.update_project("DEMO", log=lambda _line: None)

        commands = [command for command, _cwd in self.runner.streams]
        self.assertTrue(any(Path(command[0]).stem.lower() == "git" and command[1:] == ["pull", "--ff-only"] for command in commands))
        self.assertTrue(has_command_tail(commands, ["compose", "pull"]))
        self.assertTrue(has_command_tail(commands, ["compose", "up", "-d", "--no-recreate"]))

    def test_update_all_projects_uses_workspace_projects(self):
        other = self.root / "OTHER"
        other.mkdir()
        (other / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

        self.service.update_all_projects(log=lambda _line: None)

        compose_cwds = [cwd.name for command, cwd in self.runner.streams if command[-2:] == ["compose", "pull"]]
        self.assertEqual(compose_cwds, ["DEMO", "OTHER"])

    def test_install_traefik_updates_repository_and_starts_compose_without_shell(self):
        tools = self.root / "docker-local-tools"
        traefik = tools / "traefik"
        (tools / ".git").mkdir(parents=True)
        traefik.mkdir()
        (traefik / "compose.yml").write_text("services: {}\n", encoding="utf-8")
        service = ProjectService(self.settings, self.root, traefik_dir=traefik, runner=self.runner)

        service.install_traefik("ssh://git@example.invalid/tools.git", log=lambda _line: None)

        commands = [command for command, _cwd in self.runner.streams]
        self.assertTrue(any(Path(command[0]).stem.lower() == "git" and command[1:] == ["pull", "--ff-only"] for command in commands))
        self.assertTrue(has_command_tail(commands, ["compose", "up", "-d"]))
        self.assertFalse(any(command[0] == "sh" for command in commands))

    def test_install_traefik_clones_missing_repository_atomically(self):
        class CloneRunner(FakeRunner):
            def stream(self, command, cwd=None, log=None):
                code = super().stream(command, cwd=cwd, log=log)
                if len(command) >= 4 and command[1] == "clone":
                    destination = Path(command[-1])
                    (destination / ".git").mkdir(parents=True)
                    (destination / "traefik").mkdir()
                    (destination / "traefik" / "compose.yml").write_text("services: {}\n", encoding="utf-8")
                return code

        runner = CloneRunner()
        traefik = self.root / "docker-local-tools" / "traefik"
        service = ProjectService(self.settings, self.root, traefik_dir=traefik, runner=runner)

        service.install_traefik("ssh://git@example.invalid/tools.git", log=lambda _line: None)

        self.assertTrue((traefik / "compose.yml").is_file())
        commands = [command for command, _cwd in runner.streams]
        self.assertTrue(any(len(command) >= 4 and command[1] == "clone" for command in commands))
        self.assertTrue(has_command_tail(commands, ["compose", "up", "-d"]))


if __name__ == "__main__":
    unittest.main()
