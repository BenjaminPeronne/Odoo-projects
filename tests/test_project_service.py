import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

from odoo_manager_core.config import ManagerSettings
from odoo_manager_core.project_service import (
    ODOO_STARTUP_LOG,
    ODOO_STARTUP_STATUS,
    ODOO_STATE_MARKER,
    ProjectService,
    add_postgres_healthcheck_start_period,
)
from odoo_manager_core.traefik import reset_traefik_entrypoint_cache


TRAEFIK_MIDDLEWARE_LABELS = (
    "traefik.http.middlewares.odoo-forward.headers.customrequestheaders.X-Forwarded-Proto=http,"
    "traefik.http.middlewares.odoo-compress.compress=true,"
    "traefik.http.middlewares.odoo-headers.headers.hostsproxyheaders=websocket,Upgrade"
)


def has_command_tail(commands, tail):
    return any(command[-len(tail):] == tail for command in commands)


def traefik_container(name="traefik", state="running", ports="127.0.0.1:80->80/tcp", networks="traefik-local",
                      working_dir="", labels=TRAEFIK_MIDDLEWARE_LABELS, container_id=None):
    """Ligne `docker ps` d'un conteneur Traefik, telle que la lit la détection."""
    return "\t".join((container_id or f"id-{name}", name, "traefik:3.6", state, ports, networks, working_dir, labels))


class FakeRunner:
    def __init__(self):
        self.streams = []
        self.captures = []
        self.statuses = {}
        self.health_statuses = {}
        self.odoo_server_running = True
        # États successifs renvoyés par la sonde du serveur Odoo, puis `odoo_server_running`.
        self.odoo_states = []
        self.odoo_port_ready = True
        self.odoo_port_states = []
        self.odoo_init_commands = []
        self.stream_codes = []
        self.stream_output = []
        self.compose_container_ids = []
        self.localtime_mounts = {}
        self.container_networks = {}
        self.missing_networks = set()
        self.missing_network_outputs = {}
        self.networks_missing_after_stream = set()
        self.database_query_outputs = {}
        self.traefik_containers = []
        self.traefik_containers_after_up = None
        self.traefik_arguments = {}
        self.published_port_owners = {}

    def stream(self, command, cwd=None, log=None):
        self.streams.append((list(command), Path(cwd) if cwd else None))
        if self.traefik_containers_after_up is not None and command[-2:] == ["up", "-d"]:
            self.traefik_containers = self.traefik_containers_after_up
        if log:
            log("$ " + " ".join(command))
            if self.stream_output and any("--stop-after-init" in argument for argument in command):
                for line in self.stream_output:
                    log(line)
        code = self.stream_codes.pop(0) if self.stream_codes else 0
        self.missing_networks.update(self.networks_missing_after_stream)
        self.networks_missing_after_stream.clear()
        return code

    def capture(self, command, cwd=None, timeout=10):
        self.captures.append((list(command), Path(cwd) if cwd else None, timeout))
        command = list(command)
        if "psql" in command and "-Atc" in command:
            query = command[command.index("-Atc") + 1]
            for fragment, result in self.database_query_outputs.items():
                if fragment in query:
                    return 0, result
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
        if len(command) >= 5 and command[1] == "exec" and ODOO_STATE_MARKER in command[-1]:
            if self.odoo_states:
                state = self.odoo_states.pop(0)
            else:
                state = "running" if self.odoo_server_running else "exited:1"
            return (124, "") if state == "unknown" else (0, ODOO_STATE_MARKER + state)
        if len(command) >= 5 and command[1:3] == ["exec", "odoo-DEMO"] and command[3:5] == ["sh", "-lc"]:
            if "/proc/1/cmdline" in command[-1]:
                init_command = self.odoo_init_commands.pop(0) if self.odoo_init_commands else "/bin/bash"
                return 0, init_command
            if ODOO_STARTUP_STATUS in command[-1]:
                return 0, "1"
            if ODOO_STARTUP_LOG in command[-1]:
                return 0, "ModuleNotFoundError: No module named 'missing_dependency'"
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
        if command[1:5] == ["ps", "-a", "--no-trunc", "--format"]:
            return 0, "\n".join(self.traefik_containers)
        if command[1:5] == ["ps", "--no-trunc", "--format", "{{.Labels}}"]:
            return 0, "\n".join(line.split("\t", 7)[-1] for line in self.traefik_containers if "\trunning\t" in line)
        if command[1:3] == ["ps", "--filter"]:
            return 0, self.published_port_owners.get(command[3].split("=", 1)[-1], "")
        if len(command) >= 5 and command[1:3] == ["inspect", "-f"] and "json .Args" in command[3]:
            return 0, self.traefik_arguments.get(command[4], '["--entrypoints.web.address=:80"]\t[]')
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
        reset_traefik_entrypoint_cache()

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

    @patch("odoo_manager_core.project_service.platform.system", return_value="Darwin")
    def test_macos_postgres_first_bootstrap_is_retried_and_restores_odoo_role(self, _platform):
        (self.project_path / "odoo.conf").write_text("db_user = odoo\ndb_password = odoo\n", encoding="utf-8")
        self.runner.statuses["postgresql-DEMO"] = "exited"
        self.runner.health_statuses["postgresql-DEMO"] = "healthy"
        self.runner.captures = []
        original_capture = self.runner.capture

        def capture(command, cwd=None, timeout=10):
            if command[-1] == "postgresql-DEMO" and "logs" in command:
                return 0, 'FATAL: data directory "/var/lib/postgresql/data" has wrong ownership'
            return original_capture(command, cwd, timeout)

        self.runner.capture = capture
        with patch.object(self.service, "container_status", return_value="exited"), patch.object(
            self.service, "wait_for_postgres"
        ) as wait_for_postgres:
            result = self.service.recover_macos_postgres_bootstrap("DEMO", self.project_path, log=lambda _line: None)

        self.assertEqual(result, 0)
        wait_for_postgres.assert_called_once_with("DEMO", log=ANY)
        commands = [command for command, _cwd in self.runner.streams]
        self.assertTrue(has_command_tail(commands, ["compose", "up", "-d", "--no-recreate"]))
        captures = [command for command, _cwd, _timeout in self.runner.captures]
        self.assertTrue(any("CREATE ROLE \"odoo\"" in command[-1] for command in captures if command and "psql" in command))
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

    @patch("odoo_manager_core.project_service.http.client.HTTPConnection")
    def test_http_probe_distinguishes_refused_port_from_slow_response(self, connection_class):
        connection_class.return_value.request.side_effect = ConnectionRefusedError()
        self.assertEqual((0, "refused"), ProjectService.http_probe_result("http://dev.demo.localhost/web/login"))

        connection_class.return_value.request.side_effect = TimeoutError()
        self.assertEqual((0, "timeout"), ProjectService.http_probe_result("http://dev.demo.localhost/web/login"))

    def test_traefik_failures_name_the_real_cause(self):
        self.runner.traefik_containers = [traefik_container()]
        cases = (
            ((0, "refused"), ["port 80", "Odoo, lui, fonctionne"]),
            ((404, ""), ["ne connaît pas la route", "réseau traefik-local"]),
            ((502, ""), ["n'arrive pas à joindre odoo-DEMO:8069"]),
            ((0, "timeout"), ["dépassent le délai"]),
        )
        for probe_result, expected in cases:
            with self.subTest(probe_result=probe_result):
                service = ProjectService(self.settings, self.root, runner=self.runner, http_probe=lambda _url, result=probe_result: result)
                with self.assertRaises(RuntimeError) as raised:
                    service.wait_project_http("DEMO", max_wait=4, log=lambda _line: None, sleep=lambda _seconds: None)
                for fragment in expected:
                    self.assertIn(fragment, str(raised.exception))
                self.assertNotIn("Traefik ne fournit pas encore", str(raised.exception))

    def test_postgres_healthcheck_gets_start_period_once_with_backup(self):
        compose = self.project_path / "compose.yml"
        compose.write_text(
            "services:\n"
            "  postgresql-DEMO:\n"
            "    healthcheck:\n"
            "      test: [\"CMD-SHELL\", \"pg_isready -U postgres\"]\n"
            "      interval: 3s\n"
            "      retries: 5\n"
            "    environment:\n"
            "      - POSTGRES_PASSWORD=postgres\n"
            "  odoo-DEMO:\n"
            "    healthcheck:\n"
            "      test: [\"CMD\", \"curl\", \"-f\", \"http://localhost:8069\"]\n",
            encoding="utf-8",
        )
        logs = []

        self.service.fix_postgres_healthcheck_start_period(compose, log=logs.append)
        self.service.fix_postgres_healthcheck_start_period(compose, log=logs.append)

        content = compose.read_text(encoding="utf-8")
        self.assertEqual(1, content.count("start_period: 120s"))
        self.assertIn("      retries: 5\n      start_period: 120s\n    environment:", content)
        self.assertEqual(1, len(list(self.project_path.glob("compose.yml.healthcheck.bak.*"))))
        self.assertEqual(1, sum("start_period" in line for line in logs))

    def test_postgres_healthcheck_patch_keeps_existing_start_period_and_crlf(self):
        existing = "  db:\n    healthcheck:\n      test: pg_isready\n      start_period: 30s\n"
        self.assertEqual((existing, False), add_postgres_healthcheck_start_period(existing))

        windows = "  db:\r\n    healthcheck:\r\n      test: pg_isready\r\n      retries: 5\r\n"
        updated, changed = add_postgres_healthcheck_start_period(windows)
        self.assertTrue(changed)
        self.assertTrue(updated.endswith("      retries: 5\r\n      start_period: 120s\r\n"))

    def test_broken_docker_port_forwarding_is_repaired_by_restarting_traefik(self):
        self.runner.statuses = {"traefik": "running"}
        results = iter([(0, "reset"), (0, "reset"), (0, "reset"), (0, "reset"), (404, ""), (303, "")])
        service = ProjectService(self.settings, self.root, runner=self.runner, http_probe=lambda _url: next(results))
        logs = []

        service.wait_project_http("DEMO", log=logs.append, sleep=lambda _seconds: None)

        commands = [command for command, _cwd in self.runner.streams]
        self.assertEqual(1, sum(command[-2:] == ["restart", "traefik"] for command in commands))
        self.assertTrue(any("Redirection du port 80 rétablie" in line for line in logs))

    def test_traefik_is_not_restarted_when_port_answers(self):
        self.runner.statuses = {"traefik": "running"}
        service = ProjectService(self.settings, self.root, runner=self.runner, http_probe=lambda _url: (404, ""))

        self.assertTrue(service.ensure_traefik_port_reachable(log=lambda _line: None, sleep=lambda _seconds: None))
        self.assertFalse(any("restart" in command for command, _cwd in self.runner.streams))

    def test_odoo_http_readiness_waits_for_first_page_instead_of_open_port(self):
        answers = iter([(2, "sans réponse : TimeoutError"), (1, "HTTP 500"), (0, "HTTP 303")])
        calls = []
        original_capture = self.runner.capture

        def capture(command, cwd=None, timeout=10):
            if "python3" in command and "/web/login" in command[-2]:
                calls.append(timeout)
                return next(answers)
            return original_capture(command, cwd, timeout)

        self.runner.capture = capture
        logs = []
        self.service.wait_odoo_http("odoo-DEMO", log=logs.append, sleep=lambda _seconds: None)

        self.assertEqual([60, 60, 60], calls)
        self.assertTrue(any("TimeoutError" in line for line in logs))
        self.assertTrue(any("Odoo répond dans son conteneur" in line for line in logs))

    def test_odoo_http_readiness_stops_when_process_dies(self):
        self.runner.odoo_server_running = False
        original_capture = self.runner.capture
        self.runner.capture = lambda command, cwd=None, timeout=10: (
            (2, "sans réponse : ConnectionRefusedError") if "python3" in command and "/web/login" in command[-2]
            else original_capture(command, cwd, timeout)
        )

        with self.assertRaisesRegex(RuntimeError, r"arrêté \(code 1\) pendant son chargement\. .*missing_dependency"):
            self.service.wait_odoo_http("odoo-DEMO", log=lambda _line: None, sleep=lambda _seconds: None)

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

    def test_waits_for_image_entrypoint_before_starting_odoo(self):
        self.runner.statuses = {"odoo-DEMO": "running"}
        self.runner.odoo_init_commands = [
            "/bin/bash /init.sh /bin/bash",
            "/bin/bash /init.sh /bin/bash",
            "/bin/bash",
        ]
        logs = []

        self.service.wait_for_odoo_container_initialization(
            "odoo-DEMO",
            max_wait=20,
            log=logs.append,
            sleep=lambda _seconds: None,
        )

        self.assertTrue(any("installation des dépendances" in line for line in logs))
        self.assertIn("Préparation du conteneur Odoo terminée.", logs)

    def test_start_project_checks_image_entrypoint_before_odoo_process(self):
        self.runner.statuses = {
            "odoo-DEMO": "running",
            "postgresql-DEMO": "running",
        }
        self.runner.odoo_init_commands = ["/bin/bash /init.sh /bin/bash", "/bin/bash"]

        self.service.start_project("DEMO", log=lambda _line: None)

        init_probe_index = next(
            index
            for index, (command, _cwd, _timeout) in enumerate(self.runner.captures)
            if "/proc/1/cmdline" in command[-1]
        )
        process_probe_index = next(
            index
            for index, (command, _cwd, _timeout) in enumerate(self.runner.captures)
            if ODOO_STATE_MARKER in command[-1]
        )
        self.assertLess(init_probe_index, process_probe_index)

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
        self.assertIn("1", logs)
        self.assertIn("ModuleNotFoundError: No module named 'missing_dependency'", logs)

    def odoo_launches(self):
        return [
            command for command, _cwd in self.runner.streams
            if command[1:3] == ["exec", "-e"] and "LOG_ATTACHMENTS=False" in command
        ]

    def test_server_wrongly_seen_as_running_is_launched_instead_of_reported_stopped(self):
        # PROTEX_V17 : « Serveur Odoo déjà démarré », puis « arrêté avant d'ouvrir le port 8069 »
        # alors qu'aucun serveur n'avait jamais été lancé.
        self.runner.odoo_states = ["running", "exited:1", "running"]
        self.runner.odoo_port_states = [False, False, False, False, True]
        logs = []

        self.service.start_odoo_server("DEMO", log=logs.append, sleep=lambda _seconds: None)

        self.assertIn("Serveur Odoo déjà démarré dans odoo-DEMO", logs)
        self.assertIn("Aucun serveur Odoo actif dans odoo-DEMO : lancement du serveur...", logs)
        self.assertEqual(1, len(self.odoo_launches()))

    def test_slow_docker_does_not_turn_a_starting_server_into_a_stopped_one(self):
        self.runner.odoo_states = ["exited:0", "unknown", "unknown", "unknown", "running"]
        self.runner.odoo_port_states = [False, False, False, False, False, True]

        self.service.start_odoo_server("DEMO", log=lambda _line: None, sleep=lambda _seconds: None)

        self.assertEqual(1, len(self.odoo_launches()))

    def test_unknown_state_is_retried_before_launching_a_server(self):
        self.runner.odoo_states = ["unknown", "unknown", "running"]

        self.service.start_odoo_server("DEMO", log=lambda _line: None, sleep=lambda _seconds: None)

        self.assertEqual([], self.odoo_launches())

    def test_launched_server_without_exit_code_gets_a_grace_period(self):
        self.runner.odoo_port_ready = False
        self.runner.odoo_states = ["absent"] * 20
        port_checks = []
        original_capture = self.runner.capture

        def capture(command, cwd=None, timeout=10):
            if command[3:5] == ["python3", "-c"]:
                port_checks.append(command)
            return original_capture(command, cwd, timeout)

        self.runner.capture = capture

        with self.assertRaisesRegex(RuntimeError, "s'est arrêté avant d'ouvrir le port 8069"):
            self.service.wait_odoo_port("odoo-DEMO", log=lambda _line: None, sleep=lambda _seconds: None)

        # Vérification à 4 s, puis 10 s de grâce par pas de 2 s.
        self.assertEqual(8, len(port_checks))

    def test_stopped_server_reports_the_error_of_the_current_run_only(self):
        self.runner.odoo_port_ready = False
        self.runner.odoo_states = ["exited:255"]
        odoo_log = "\n".join((
            "2026-09-15 19:41:21,556 379 ERROR PROTEX odoo.http: Exception during request handling.",
            "FileNotFoundError: [Errno 2] No such file or directory: '/home/odoo/srv/data/filestore/old'",
            "2026-09-17 14:17:31,000 90 INFO ? odoo: Odoo version 17.0",
            "2026-09-17 14:17:33,000 90 CRITICAL PROTEX odoo.service.server: Failed to initialize database `PROTEX`.",
            "psycopg2.OperationalError: connection to server at \"postgresql-DEMO\" failed",
        ))
        original_capture = self.runner.capture
        self.runner.capture = lambda command, cwd=None, timeout=10: (
            (0, odoo_log) if "tail -n 120" in command[-1] else original_capture(command, cwd, timeout)
        )

        with self.assertRaises(RuntimeError) as raised:
            self.service.wait_odoo_port("odoo-DEMO", log=lambda _line: None, sleep=lambda _seconds: None)

        message = str(raised.exception)
        self.assertIn("(code 255)", message)
        self.assertIn("psycopg2.OperationalError", message)
        self.assertNotIn("FileNotFoundError", message)

    def test_module_command_stops_server_even_when_docker_is_slow_to_answer(self):
        self.runner.odoo_states = ["unknown", "exited:0"]

        self.service.stop_odoo_server("DEMO", log=lambda _line: None, sleep=lambda _seconds: None)

        self.assertTrue(any("pkill" in command[-1] for command, _cwd in self.runner.streams))

    def test_view_parse_error_reason_includes_odoo_explanation(self):
        lines = [
            "2026-09-17 14:10:52,608 402 ERROR sudokeys_17092016 odoo.registry: Failed to load registry",
            "Traceback (most recent call last):",
            '  File "/home/odoo/srv/server/odoo/odoo/tools/convert.py", line 700, in _tag_root',
            "odoo.tools.convert.ParseError: while parsing /home/odoo/srv/server/addons/demo/views/res_config_settings_views.xml:3",
            "Erreur lors de la validation de la vue :",
            '<form string="Settings" class="oe_form_configuration" js_class="base_settings">',
            "Le champ `payslip_generate_and_send_trigger` n'existe pas",
            "View error context:",
            "{'name': 'res.config.settings.view.form.tracking'}",
        ]

        reason = ProjectService.odoo_command_failure_reason(lines)

        self.assertTrue(reason.endswith("res_config_settings_views.xml:3 — Le champ `payslip_generate_and_send_trigger` n'existe pas"))

    def test_cancelled_start_stops_only_containers_that_were_stopped_before(self):
        from odoo_manager_core import jobs

        for statuses, expected_stop in (({"odoo-DEMO": "exited", "postgresql-DEMO": "running"}, True),
                                        ({"odoo-DEMO": "running", "postgresql-DEMO": "running"}, False)):
            with self.subTest(statuses=statuses):
                self.runner.streams.clear()
                self.runner.statuses = dict(statuses)
                control = jobs.JobControl()
                with jobs.bind_control(control):
                    self.service.register_start_revert("DEMO", self.project_path, log=lambda _line: None)
                    control.run_reverts(lambda _line: None)
                commands = [command for command, _cwd in self.runner.streams]
                self.assertEqual(expected_stop, has_command_tail(commands, ["compose", "stop"]))

    def test_cancelled_module_operation_resets_only_the_pending_states_it_created(self):
        from odoo_manager_core import jobs

        self.runner.statuses = {"odoo-DEMO": "running", "postgresql-DEMO": "running"}
        self.runner.database_query_outputs = {
            "select name from ir_module_module where state in": "old_pending",
            "update ir_module_module": "sale -> installed",
        }
        queries = []
        original_capture = self.runner.capture

        def capture(command, cwd=None, timeout=10):
            if "psql" in command and "-Atc" in command:
                queries.append(command[command.index("-Atc") + 1])
            return original_capture(command, cwd, timeout)

        self.runner.capture = capture
        logs = []
        control = jobs.JobControl()
        with jobs.bind_control(control):
            self.service.register_module_operations_revert("DEMO", "demo", log=logs.append)
            control.run_reverts(logs.append)

        reset = next(query for query in queries if query.startswith("update ir_module_module"))
        self.assertIn("name not in ('old_pending')", reset)
        self.assertIn("when state = 'to install' then 'uninstalled' else 'installed'", reset)
        self.assertIn("Modules remis dans leur état précédent : sale -> installed", logs)

    def test_odoo_launch_records_early_output_and_exit_status(self):
        self.runner.odoo_server_running = False
        self.runner.odoo_port_ready = True

        self.service.start_odoo_server("DEMO", log=lambda _line: None)

        launch = next(
            command
            for command, _cwd in self.runner.streams
            if command[1:3] == ["exec", "-e"] and "LOG_ATTACHMENTS=False" in command
        )
        self.assertEqual(launch[-3:-1], ["sh", "-lc"])
        self.assertIn(f": > {ODOO_STARTUP_LOG}", launch[-1])
        self.assertIn(f"> {ODOO_STARTUP_STATUS}", launch[-1])
        self.assertIn("/home/_venv/bin/python /home/odoo/srv/server/odoo/odoo-bin", launch[-1])
        self.assertIn("--logfile=/home/odoo/srv/data/odoo.log", launch[-1])

    def test_odoo_launch_can_disable_cron_workers_during_safe_restore(self):
        self.runner.odoo_server_running = False
        self.runner.odoo_port_ready = True

        self.service.start_odoo_server("DEMO", log=lambda _line: None, disable_cron=True)

        launch = next(
            command
            for command, _cwd in self.runner.streams
            if command[1:3] == ["exec", "-e"] and "LOG_ATTACHMENTS=False" in command
        )
        self.assertEqual(launch[-1].count("--max-cron-threads=0"), 2)

    def make_traefik_service(self, compose_text):
        traefik = self.root / "docker-local-tools" / "traefik"
        traefik.mkdir(parents=True)
        (traefik / "docker-compose.yml").write_text(compose_text, encoding="utf-8")
        return ProjectService(self.settings, self.root, traefik_dir=traefik, runner=self.runner), traefik

    def test_traefik_is_started_bound_to_loopback_without_touching_its_repository(self):
        service, traefik = self.make_traefik_service("services:\n  traefik:\n    ports:\n      - 80:80\n")

        with patch.object(service, "compose_version", return_value=(2, 29, 1)):
            service.start_traefik(log=lambda _line: None)

        command = next(command for command, _cwd in self.runner.streams if "up" in command)
        override = self.root / ".odoo_manager_runtime" / "traefik-loopback.compose.yml"
        self.assertEqual(command[-2:], ["up", "-d"])
        self.assertIn(str(override), command)
        self.assertIn(str(traefik / "docker-compose.yml"), command)
        self.assertIn('ports: !override\n      - "127.0.0.1:80:80"', override.read_text(encoding="utf-8"))
        self.assertEqual(["docker-compose.yml"], sorted(path.name for path in traefik.iterdir()))

    def test_traefik_ports_are_left_unchanged_when_override_is_unsupported(self):
        for version, compose_text in (((2, 20, 0), "services:\n  traefik:\n    image: traefik\n"),
                                      ((2, 29, 1), "services:\n  proxy:\n    image: traefik\n")):
            with self.subTest(version=version):
                self.runner.streams.clear()
                service, traefik = self.make_traefik_service(compose_text)
                logs = []
                with patch.object(service, "compose_version", return_value=version):
                    service.start_traefik(log=logs.append)
                command = next(command for command, _cwd in self.runner.streams if "up" in command)
                self.assertNotIn("-f", command)
                self.assertTrue(any("laissés tels quels" in line or "trop ancien" in line for line in logs))
                (traefik / "docker-compose.yml").unlink()
                traefik.rmdir()
                traefik.parent.rmdir()

    def test_traefik_custom_host_port_is_kept_and_used_for_project_urls(self):
        service, traefik = self.make_traefik_service('services:\n  traefik:\n    container_name: traefik\n    ports:\n      - "8080:80"\n')
        (self.project_path / "compose.yml").write_text("labels:\n  - rule=Host(`dev.DEMO.localhost`)\n", encoding="utf-8")
        managed = str(traefik)
        self.runner.traefik_containers = [traefik_container(state="exited", ports="", working_dir=managed)]
        self.runner.traefik_containers_after_up = [traefik_container(ports="127.0.0.1:8080->80/tcp", working_dir=managed)]
        logs = []

        self.assertEqual("http://dev.DEMO.localhost:8080/", service.project_url("DEMO"))
        with patch.object(service, "compose_version", return_value=(2, 29, 1)):
            service.start_traefik(log=logs.append)

        override = self.root / ".odoo_manager_runtime" / "traefik-loopback.compose.yml"
        self.assertIn('ports: !override\n      - "127.0.0.1:8080:80"', override.read_text(encoding="utf-8"))
        self.assertNotIn("127.0.0.1:80:80", override.read_text(encoding="utf-8"))
        self.assertTrue(any("port HTTP 8080 (port personnalisé)" in line for line in logs))
        self.assertEqual("http://dev.DEMO.localhost:8080/", service.project_url("DEMO"))

    def test_existing_compatible_traefik_is_reused_instead_of_starting_a_second_one(self):
        service, _traefik = self.make_traefik_service("services:\n  traefik:\n    container_name: traefik\n    ports:\n      - 80:80\n")
        self.runner.traefik_containers = [
            traefik_container(name="proxy", ports="0.0.0.0:8000->80/tcp, :::8000->80/tcp", working_dir="/home/demo/proxy"),
        ]
        logs = []

        service.start_traefik(log=logs.append)

        self.assertFalse(any("up" in command for command, _cwd in self.runner.streams))
        self.assertTrue(any("Instance Traefik existante détectée : proxy" in line for line in logs))
        self.assertEqual("http://dev.DEMO.localhost:8000/", service.project_url("DEMO"))

    def test_incompatible_traefik_holding_the_port_stops_startup_immediately(self):
        service, _traefik = self.make_traefik_service("services:\n  traefik:\n    container_name: traefik\n    ports:\n      - 80:80\n")
        self.runner.traefik_containers = [
            traefik_container(name="edge", ports="0.0.0.0:80->80/tcp", networks="proxy", labels=""),
        ]
        self.runner.traefik_arguments = {"id-edge": '["--entrypoints.http.address=:80"]\t[]'}
        logs = []

        with self.assertRaises(RuntimeError) as raised:
            service.start_traefik(log=logs.append)

        message = str(raised.exception)
        self.assertIn("Le port 80 de cette machine est déjà utilisé par le conteneur Traefik edge", message)
        self.assertIn('"8080:80"', message)
        self.assertTrue(any("aucun entrypoint « web »" in line for line in logs))
        self.assertFalse(any("up" in command for command, _cwd in self.runner.streams))

    def test_port_used_outside_docker_names_its_owner_without_running_compose(self):
        traefik = self.root / "docker-local-tools" / "traefik"
        traefik.mkdir(parents=True)
        (traefik / "docker-compose.yml").write_text("services:\n  traefik:\n    ports:\n      - 80:80\n", encoding="utf-8")
        service = ProjectService(self.settings, self.root, traefik_dir=traefik, runner=self.runner, port_in_use=lambda port: port == 80)
        self.runner.published_port_owners = {"80": "nginx-legacy"}

        with self.assertRaises(RuntimeError) as raised:
            service.start_traefik(log=lambda _line: None)

        self.assertIn("déjà utilisé par le conteneur Docker nginx-legacy", str(raised.exception))
        self.assertFalse(self.runner.streams)

    def test_foreign_stopped_container_with_traefik_name_is_reported_before_compose(self):
        service, traefik = self.make_traefik_service("services:\n  traefik:\n    container_name: traefik\n    ports:\n      - 80:80\n")
        self.runner.traefik_containers = [traefik_container(state="exited", ports="", working_dir="/old/docker-local-tools/traefik")]

        with self.assertRaises(RuntimeError) as raised:
            service.start_traefik(log=lambda _line: None)

        self.assertIn("Un conteneur « traefik » existe déjà", str(raised.exception))
        self.assertIn("docker rm -f traefik", str(raised.exception))
        self.assertFalse(self.runner.streams)

    def test_project_check_follows_traefik_to_its_real_port(self):
        self.runner.traefik_containers = [traefik_container(ports="127.0.0.1:8081->80/tcp")]
        urls = []

        def probe(url):
            urls.append(url)
            return (303, "") if ":8081/" in url else (0, "refused")

        service = ProjectService(self.settings, self.root, runner=self.runner, http_probe=probe)
        service.use_traefik_instance(None)

        service.wait_project_http("DEMO", log=lambda _line: None, sleep=lambda _seconds: None)

        self.assertEqual("http://dev.DEMO.localhost:8081/web/login", urls[-1])
        self.assertLess(len(urls), 10)

    def test_project_check_stops_when_nothing_can_serve_the_route(self):
        cases = (
            ((0, "refused"), [], "Rien n'écoute sur le port 80", 4),
            ((404, ""), [], "aucun conteneur Traefik n'est démarré", 20),
            ((404, ""), [traefik_container(networks="proxy")], "absent du réseau traefik-local", 20),
            ((404, ""), [traefik_container(labels="")], "odoo-forward@docker", 20),
        )
        for probe_result, containers, expected, stop_after in cases:
            with self.subTest(expected=expected):
                self.runner.traefik_containers = containers
                probes = []
                service = ProjectService(
                    self.settings, self.root, runner=self.runner,
                    http_probe=lambda url, result=probe_result: probes.append(url) or result,
                )

                with self.assertRaises(RuntimeError) as raised:
                    service.wait_project_http("DEMO", log=lambda _line: None, sleep=lambda _seconds: None)

                self.assertIn(expected, str(raised.exception))
                # Une probe toutes les 2 s jusqu'au diagnostic, plus la sonde de l'API Traefik éventuelle.
                self.assertLessEqual(len(probes), stop_after // 2 + 2)

    def test_running_traefik_with_lost_port_forwarding_keeps_waiting_after_restart(self):
        self.runner.statuses = {"traefik": "running"}
        self.runner.traefik_containers = [traefik_container()]
        probes = []
        service = ProjectService(
            self.settings, self.root, runner=self.runner,
            http_probe=lambda url: probes.append(url) or (0, "reset"),
        )

        with self.assertRaises(RuntimeError) as raised:
            service.wait_project_http("DEMO", max_wait=30, log=lambda _line: None, sleep=lambda _seconds: None)

        self.assertIn("Rien n'écoute sur le port 80", str(raised.exception))
        self.assertEqual(1, sum(command[-2:] == ["restart", "traefik"] for command, _cwd in self.runner.streams))
        # 16 vérifications de la route sur 30 s, plus 1 sonde initiale et 15 sondes après le redémarrage.
        self.assertEqual(32, len(probes))

    def test_module_update_runs_explicit_odoo_command_and_restarts_server(self):
        self.runner.statuses = {
            "odoo-DEMO": "running",
            "postgresql-DEMO": "running",
        }
        self.runner.odoo_server_running = False
        self.runner.odoo_port_ready = True
        logs = []

        self.service.run_odoo_module_command(
            "DEMO",
            "PROTEX_20812",
            "sale_custom",
            option="-u",
            log=logs.append,
        )

        commands = [command for command, _cwd in self.runner.streams]
        update = next(command for command in commands if any("--stop-after-init" in argument for argument in command))
        self.assertEqual(update[-3:-1], ["bash", "-lc"])
        self.assertIn("odoo -c /home/odoo/srv/conf/odoo.conf -d PROTEX_20812 -u sale_custom --stop-after-init", update[-1])
        self.assertIn("| tee /home/odoo/srv/data/odoo-manager-module-", update[-1])
        self.assertIn('exit "${PIPESTATUS[0]}"', update[-1])
        self.assertTrue(any("Équivalent: odoo -d PROTEX_20812 -u sale_custom --stop-after-init" in line for line in logs))
        self.assertTrue(any("Redémarrage du serveur Odoo" in line for line in logs))

    def test_module_update_failure_reports_odoo_error_after_restart(self):
        self.runner.statuses = {"odoo-DEMO": "running", "postgresql-DEMO": "running"}
        self.runner.odoo_server_running = False
        self.runner.odoo_port_ready = True
        self.runner.stream_codes = [255]
        self.runner.stream_output = [
            "2026-09-15 ERROR demo odoo.modules: unable to load module",
            "Traceback (most recent call last):",
            "ModuleNotFoundError: No module named 'missing_dependency'",
        ]
        logs = []

        with self.assertRaisesRegex(RuntimeError, "ModuleNotFoundError.*missing_dependency"):
            self.service.run_odoo_module_command(
                "DEMO", "demo", "all", option="-u", log=logs.append,
            )

        self.assertTrue(any("Redémarrage du serveur Odoo" in line for line in logs))
        self.assertTrue(any("Échec de la commande Odoo (code 255)" in line for line in logs))
        self.assertTrue(any("Journal complet de cette commande" in line for line in logs))
        self.assertFalse(any("Sortie du dernier lancement Odoo" in line for line in logs))

    def test_module_error_survives_long_shutdown_output(self):
        self.runner.statuses = {"odoo-DEMO": "running", "postgresql-DEMO": "running"}
        self.runner.odoo_server_running = False
        self.runner.stream_codes = [255]
        self.runner.stream_output = [
            "2026-09-15 ERROR demo odoo.modules: Failed to initialize database",
            "ImportError: missing Odoo dependency",
            *[f"2026-09-15 INFO demo shutdown line {index}" for index in range(250)],
        ]
        with self.assertRaisesRegex(RuntimeError, "ImportError: missing Odoo dependency"):
            self.service.run_odoo_module_command("DEMO", "demo", "all", log=lambda _line: None)

    def test_info_filestore_traceback_is_not_reported_as_module_failure(self):
        reason = self.service.odoo_command_failure_reason([
            "2026-09-15 13:29:13 INFO demo ir_attachment: _file_gc could not unlink",
            "Traceback (most recent call last):",
            "FileNotFoundError: missing filestore item",
        ])
        self.assertIn("Cause non présente", reason)

    def test_module_update_failure_without_odoo_output_points_to_job_logs(self):
        self.runner.statuses = {"odoo-DEMO": "running", "postgresql-DEMO": "running"}
        self.runner.odoo_server_running = False
        self.runner.odoo_port_ready = True
        self.runner.stream_codes = [255]

        with self.assertRaisesRegex(RuntimeError, "Cause non présente.*Logs de cette tâche"):
            self.service.run_odoo_module_command(
                "DEMO", "demo", "all", option="-u", log=lambda _line: None,
            )

    def test_module_update_reneutralizes_an_already_neutralized_database_before_restart(self):
        self.runner.statuses = {
            "odoo-DEMO": "running",
            "postgresql-DEMO": "running",
        }
        self.runner.odoo_server_running = False
        self.runner.odoo_port_ready = True
        self.runner.database_query_outputs = {
            "(SELECT count(*) FROM ir_cron": "true|0|0",
            "database.is_neutralized": "true",
            "to_regclass('public.fetchmail_server')": "absent",
        }
        logs = []

        self.service.run_odoo_module_command(
            "DEMO",
            "PROTEX_20812",
            "sale_custom",
            option="-u",
            log=logs.append,
        )

        commands = [command for command, _cwd in self.runner.streams]
        update_index = next(index for index, command in enumerate(commands) if any("--stop-after-init" in argument for argument in command))
        neutralize_index = next(
            index for index, command in enumerate(commands) if "ODOO_MANAGER_NEUTRALIZATION_DONE" in command[-1]
        )
        restart_index = next(
            index for index, command in enumerate(commands) if ODOO_STARTUP_LOG in command[-1]
        )
        self.assertLess(update_index, neutralize_index)
        self.assertLess(neutralize_index, restart_index)
        self.assertTrue(any("nouvelle passe après l'opération module" in line for line in logs))

    def test_module_uninstall_runs_odoo_shell_and_restarts_server(self):
        self.runner.statuses = {
            "odoo-DEMO": "running",
            "postgresql-DEMO": "running",
        }
        self.runner.odoo_server_running = False
        self.runner.odoo_port_ready = True
        logs = []

        self.service.run_odoo_uninstall_command(
            "DEMO",
            "PROTEX_20812",
            "sale_custom",
            log=logs.append,
        )

        commands = [command for command, _cwd in self.runner.streams]
        uninstall = next(command for command in commands if "odoo shell" in command[-1])
        self.assertIn("ODOO_DB_NAME=PROTEX_20812", uninstall)
        self.assertIn("MODULE_NAMES=sale_custom", uninstall)
        self.assertIn('odoo shell -c /home/odoo/srv/conf/odoo.conf -d "$ODOO_DB_NAME" --no-http', uninstall[-1])
        self.assertIn("installed.button_immediate_uninstall()", uninstall[-1])
        self.assertTrue(any("Redémarrage du serveur Odoo" in line for line in logs))

    def test_module_translation_reset_adds_i18n_overwrite_before_stop_after_init(self):
        self.runner.statuses = {"odoo-DEMO": "running", "postgresql-DEMO": "running"}
        self.runner.odoo_server_running = False
        self.runner.odoo_port_ready = True
        logs = []

        self.service.run_odoo_module_command(
            "DEMO", "PROTEX_20812", "sale_custom", option="-u", log=logs.append, overwrite_translations=True,
        )

        commands = [command for command, _cwd in self.runner.streams]
        update = next(command for command in commands if any("--stop-after-init" in argument for argument in command))
        self.assertIn("-u sale_custom --i18n-overwrite --stop-after-init", update[-1])
        self.assertTrue(any(
            "Équivalent: odoo -d PROTEX_20812 -u sale_custom --i18n-overwrite --stop-after-init" in line
            for line in logs
        ))

    def test_asset_regeneration_unlinks_web_asset_attachments(self):
        self.runner.statuses = {"odoo-DEMO": "running", "postgresql-DEMO": "running"}
        self.runner.odoo_server_running = False
        self.runner.odoo_port_ready = True

        self.service.run_odoo_regenerate_assets("DEMO", "PROTEX_20812", log=lambda _line: None)

        commands = [command for command, _cwd in self.runner.streams]
        shell = next(command for command in commands if "odoo shell" in command[-1])
        self.assertIn('("url", "=like", "/web/assets/%")', shell[-1])
        self.assertIn("attachments.unlink()", shell[-1])

    def test_admin_password_reset_passes_secret_by_env_and_masks_it_in_logs(self):
        self.runner.statuses = {"odoo-DEMO": "running", "postgresql-DEMO": "running"}
        self.runner.odoo_server_running = False
        self.runner.odoo_port_ready = True
        logs = []

        self.service.run_odoo_reset_admin_password("DEMO", "PROTEX_20812", "S3cret-Local", log=logs.append)

        commands = [command for command, _cwd in self.runner.streams]
        shell = next(command for command in commands if "odoo shell" in command[-1])
        self.assertIn("ODOO_ADMIN_PASSWORD=S3cret-Local", shell)
        self.assertNotIn("S3cret-Local", shell[-1])
        self.assertIn('env.ref("base.user_admin"', shell[-1])
        self.assertFalse(any("S3cret-Local" in line for line in logs))
        self.assertTrue(any("ODOO_ADMIN_PASSWORD=********" in line for line in logs))

    def test_all_translations_reset_reloads_terms_of_installed_modules_without_update(self):
        self.runner.statuses = {"odoo-DEMO": "running", "postgresql-DEMO": "running"}
        self.runner.odoo_server_running = False
        self.runner.odoo_port_ready = True
        logs = []

        self.service.run_odoo_reset_all_translations("DEMO", "PROTEX_20812", ["fr_FR"], log=logs.append)

        commands = [command for command, _cwd in self.runner.streams]
        shell = next(command for command in commands if "odoo shell" in command[-1])
        self.assertIn("ODOO_LANGUAGES=fr_FR", shell)
        self.assertIn('search([("state", "=", "installed")])', shell[-1])
        self.assertIn("modules._update_translations(languages, overwrite=True)", shell[-1])
        self.assertFalse(any("--stop-after-init" in command for command in commands))
        self.assertTrue(any("Langue(s): fr_FR" in line for line in logs))

    def test_admin_password_reset_rejects_empty_password(self):
        with self.assertRaises(ValueError):
            self.service.run_odoo_reset_admin_password("DEMO", "PROTEX_20812", "", log=lambda _line: None)

    def test_neutralization_uses_odoo_engine_verifies_guards_and_restarts_server(self):
        self.runner.statuses = {
            "odoo-DEMO": "running",
            "postgresql-DEMO": "running",
        }
        self.runner.odoo_server_running = False
        self.runner.odoo_port_ready = True
        self.runner.database_query_outputs = {
            "database.is_neutralized": "true|0|0",
            "to_regclass('public.fetchmail_server')": "present",
            "FROM fetchmail_server": "0",
        }
        logs = []

        self.service.run_odoo_neutralize_command(
            "DEMO",
            "PROTEX_20812",
            log=logs.append,
        )

        commands = [command for command, _cwd in self.runner.streams]
        neutralize = next(command for command in commands if "ODOO_MANAGER_NEUTRALIZATION_DONE" in command[-1])
        self.assertIn("ODOO_DB_NAME=PROTEX_20812", neutralize)
        self.assertIn("from odoo.modules.neutralize import neutralize_database", neutralize[-1])
        self.assertIn('env["ir.cron"].search([])', neutralize[-1])
        self.assertIn("dummies[1:].unlink()", neutralize[-1])
        self.assertTrue(any("0 cron métier actif" in line for line in logs))
        self.assertTrue(any("Neutralisation terminée et contrôlée." in line for line in logs))
        self.assertTrue(any("Redémarrage du serveur Odoo" in line for line in logs))

    def test_failed_neutralization_check_still_restarts_odoo(self):
        self.runner.statuses = {
            "odoo-DEMO": "running",
            "postgresql-DEMO": "running",
        }
        self.runner.odoo_server_running = False
        self.runner.odoo_port_ready = True
        self.runner.database_query_outputs = {
            "database.is_neutralized": "true|1|0",
        }
        logs = []

        with self.assertRaisesRegex(RuntimeError, "cron"):
            self.service.run_odoo_neutralize_command(
                "DEMO",
                "PROTEX_20812",
                log=logs.append,
            )

        self.assertTrue(any("Redémarrage du serveur Odoo" in line for line in logs))
        self.assertTrue(
            any(ODOO_STARTUP_LOG in command[-1] for command, _cwd in self.runner.streams)
        )

    @patch("odoo_manager_core.platform.wsl_execution_path", return_value="/home/demo/Odoo-projects")
    @patch("odoo_manager_core.project_service.platform.system", return_value="Windows")
    def test_wsl_commands_receive_explicit_linux_working_directory(self, _system, _execution_path):
        command = ["wsl.exe", "-d", "Ubuntu", "--exec", "docker", "compose", "ps"]

        prepared, process_cwd = self.service.prepare_command(command, self.root)

        self.assertEqual(
            prepared[:6],
            ["wsl.exe", "-d", "Ubuntu", "--cd", "/home/demo/Odoo-projects", "--exec"],
        )
        self.assertEqual(process_cwd, Path.home())

    @patch("odoo_manager_core.project_service.http.client.HTTPConnection")
    def test_http_probe_uses_loopback_with_traefik_host_header(self, connection_class):
        connection = connection_class.return_value
        connection.getresponse.return_value.status = 303

        status = ProjectService.http_status("http://dev.Caritel_v18.localhost/web/login")

        self.assertEqual(status, 303)
        connection_class.assert_called_once_with("127.0.0.1", None, timeout=10)
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

    def test_list_projects_ignores_inaccessible_compose_files(self):
        inaccessible = self.root / "restricted"
        inaccessible.mkdir()
        protected_compose = inaccessible / "docker-compose.yml"
        original_exists = Path.exists

        def exists(path):
            if path == protected_compose:
                raise PermissionError(5, "Access is denied", str(path))
            return original_exists(path)

        with patch.object(Path, "exists", autospec=True, side_effect=exists):
            projects = self.service.list_projects()

        self.assertEqual(projects, ["DEMO"])

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

    @patch("odoo_manager_core.project_service.find_wsl_executable_distribution", return_value="Ubuntu-24.04")
    @patch("odoo_manager_core.project_service.host_executable_available", return_value=False)
    @patch("odoo_manager_core.project_service.platform.system", return_value="Windows")
    @patch("odoo_manager_core.platform.wsl_execution_path", return_value="/mnt/c/docker-local-tools")
    def test_traefik_install_uses_git_from_detected_wsl_distribution(self, _path, _system, _native, _wsl):
        tools = self.root / "docker-local-tools"
        traefik = tools / "traefik"
        (tools / ".git").mkdir(parents=True)
        traefik.mkdir()
        (traefik / "compose.yml").write_text("services: {}\n", encoding="utf-8")
        service = ProjectService(self.settings, self.root, traefik_dir=traefik, runner=self.runner)

        service.install_traefik("ssh://git@example.invalid/tools.git", log=lambda _line: None)

        git_command = next(command for command, _cwd in self.runner.streams if "git" in command)
        self.assertEqual(git_command[:3], ["wsl.exe", "-d", "Ubuntu-24.04"])
        self.assertEqual(git_command[-4:], ["--exec", "git", "pull", "--ff-only"])


if __name__ == "__main__":
    unittest.main()
