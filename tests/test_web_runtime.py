import time
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import odoo_manager_web as web


class CorsTests(unittest.TestCase):
    def test_allows_windows_tauri_webview_origin(self):
        handler = Mock()
        handler.headers = {"Origin": "http://tauri.localhost"}

        web.add_cors_headers(handler)

        handler.send_header.assert_any_call(
            "Access-Control-Allow-Origin", "http://tauri.localhost"
        )

    def test_rejects_unknown_origin(self):
        handler = Mock()
        handler.headers = {"Origin": "https://example.invalid"}

        web.add_cors_headers(handler)

        handler.send_header.assert_not_called()


class ServerRuntimeTests(unittest.TestCase):
    def test_recognizes_address_in_use_on_supported_platforms(self):
        for error_number in (48, 98, 10048):
            with self.subTest(error_number=error_number):
                self.assertTrue(web.address_is_already_in_use(OSError(error_number, "port occupé")))

        self.assertFalse(web.address_is_already_in_use(OSError(2, "fichier absent")))


class DatabaseNameValidationTests(unittest.TestCase):
    def test_accepts_existing_odoo_database_name_with_hash(self):
        self.assertEqual(web.validate_db("sodial_recette#1"), "sodial_recette#1")
        self.assertEqual(web.validate_odoo_db("sodial_recette#1"), "sodial_recette#1")

    def test_accepts_unicode_spaces_and_punctuation_supported_by_postgresql(self):
        self.assertEqual(web.validate_db("recette été #1: test?"), "recette été #1: test?")

    def test_rejects_empty_name_and_control_characters(self):
        for name in ("", "base\nname", "base\tname", "base\x00name"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    web.validate_db(name)

    def test_new_database_rejects_unsafe_filestore_components(self):
        for name in (" base", "base ", ".", "..", "../base", "base\\test"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    web.validate_new_db(name)

    def test_rejects_names_over_postgresql_identifier_limit(self):
        with self.assertRaisesRegex(ValueError, "63 octets"):
            web.validate_db("é" * 32)

    @patch("odoo_manager_web.run_capture")
    @patch("odoo_manager_web.container_status", return_value="running")
    def test_database_name_is_passed_as_one_psql_argument(self, _status, run_capture):
        run_capture.return_value = (0, "base|installed|19.0")

        web.installed_modules("sodial_v19", "sodial_recette#1")

        command = run_capture.call_args.args[0]
        database_option = command.index("-d")
        self.assertEqual(command[database_option + 1], "sodial_recette#1")


class DatabaseRestoreTests(unittest.TestCase):
    def make_backup(self, root, include_dump=True):
        path = Path(root) / "backup.zip"
        with zipfile.ZipFile(path, "w") as archive:
            if include_dump:
                archive.writestr("dump.sql", "CREATE TABLE test(id integer);\n")
            archive.writestr("manifest.json", "{}")
            archive.writestr("filestore/ab/abcdef", b"attachment")
        return path

    def test_accepts_odoo_zip_backup_with_dump_and_filestore(self):
        with tempfile.TemporaryDirectory() as temporary:
            details = web.validate_odoo_backup_archive(self.make_backup(temporary))

        self.assertTrue(details["has_filestore"])
        self.assertTrue(details["has_manifest"])
        self.assertEqual(details["entries"], 3)

    def test_rejects_zip_without_database_dump(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.make_backup(temporary, include_dump=False)
            with self.assertRaisesRegex(ValueError, "dump.sql"):
                web.validate_odoo_backup_archive(path)

    @patch("odoo_manager_web.http.client.HTTPConnection")
    def test_streams_restore_with_official_odoo_form_fields(self, connection_type):
        connection = Mock()
        response = Mock(status=303)
        response.read.return_value = b""
        connection.getresponse.return_value = response
        connection_type.return_value = connection
        job = Mock()

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "backup.zip"
            path.write_bytes(b"backup-content")
            status, content = web.post_odoo_database_restore(
                job,
                "http://dev.demo.localhost/web/database/restore",
                path,
                "backup.zip",
                "demo_restore",
                "odoo",
                True,
                True,
            )

        self.assertEqual((status, content), (303, ""))
        connection.putrequest.assert_called_once_with("POST", "/web/database/restore")
        transmitted = b"".join(call.args[0] for call in connection.send.call_args_list)
        self.assertIn(b'name="master_pwd"\r\n\r\nodoo', transmitted)
        self.assertIn(b'name="name"\r\n\r\ndemo_restore', transmitted)
        self.assertIn(b'name="copy"\r\n\r\ntrue', transmitted)
        self.assertIn(b'name="neutralize_database"\r\n\r\non', transmitted)
        self.assertIn(b'name="backup_file"; filename="backup.zip"', transmitted)

    @patch("odoo_manager_web.project_odoo_version", return_value="15.0")
    @patch("odoo_manager_web.project_url", return_value="http://dev.demo.localhost/")
    @patch("odoo_manager_web.post_odoo_database_restore", return_value=(303, ""))
    @patch("odoo_manager_web.list_databases_for", side_effect=[[], ["demo_restore"]])
    @patch("odoo_manager_web.project_service")
    @patch("odoo_manager_web.validate_project", return_value="DEMO")
    def test_restore_job_removes_temporary_backup(
        self,
        _validate_project,
        project_service,
        _list_databases,
        _post_restore,
        _project_url,
        _project_version,
    ):
        job = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            path = self.make_backup(temporary)
            web.restore_database_job(
                job,
                "DEMO",
                path,
                "backup.zip",
                "demo_restore",
                "odoo",
                True,
                True,
            )
            self.assertFalse(path.exists())

        project_service.return_value.start_project.assert_called_once()
        project_service.return_value.stop_odoo_server.assert_called_once_with("DEMO", log=job.add)
        project_service.return_value.start_odoo_server.assert_called_once_with(
            "DEMO",
            log=job.add,
            disable_cron=True,
        )
        self.assertFalse(_post_restore.call_args.args[-1])
        project_service.return_value.run_odoo_neutralize_command.assert_called_once_with(
            "DEMO",
            "demo_restore",
            log=job.add,
        )

    @patch("odoo_manager_web.list_databases_for", return_value=["postgres", "demo_restore"])
    @patch("odoo_manager_web.project_service")
    @patch("odoo_manager_web.validate_project", return_value="DEMO")
    def test_existing_database_can_be_neutralized(
        self,
        _validate_project,
        project_service,
        _list_databases,
    ):
        job = Mock()

        web.neutralize_database_job(job, "DEMO", "demo_restore")

        project_service.return_value.run_odoo_neutralize_command.assert_called_once_with(
            "DEMO",
            "demo_restore",
            log=job.add,
        )


class PostgreSqlConsoleTests(unittest.TestCase):
    @patch("odoo_manager_web.open_terminal_command")
    @patch("odoo_manager_web.docker_command")
    @patch("odoo_manager_web.list_databases_for", return_value=["postgres", "sodial_recette#1"])
    @patch("odoo_manager_web.container_status", return_value="running")
    @patch("odoo_manager_web.validate_project", return_value="sodial_v19")
    def test_opens_psql_for_selected_odoo_database(
        self,
        _validate_project,
        _container_status,
        _list_databases,
        docker_command,
        open_terminal,
    ):
        docker_command.return_value = ["docker", "exec", "-it", "postgresql-sodial_v19", "psql"]
        open_terminal.return_value = Mock(ok=True, message="Terminal ouvert.")

        result = web.open_postgresql_console("sodial_v19", "sodial_recette#1")

        self.assertTrue(result["ok"])
        docker_command.assert_called_once_with(
            web.SETTINGS,
            "exec",
            "-it",
            "postgresql-sodial_v19",
            "psql",
            "-U",
            "postgres",
            "-d",
            "sodial_recette#1",
        )
        open_terminal.assert_called_once()

    @patch("odoo_manager_web.validate_project", return_value="sodial_v19")
    def test_rejects_postgres_system_database(self, _validate_project):
        with self.assertRaisesRegex(ValueError, "base système postgres"):
            web.open_postgresql_console("sodial_v19", "postgres")

    @patch("odoo_manager_web.list_databases_for", return_value=["postgres", "other_database"])
    @patch("odoo_manager_web.container_status", return_value="running")
    @patch("odoo_manager_web.validate_project", return_value="sodial_v19")
    def test_rejects_unknown_odoo_database(self, _validate_project, _container_status, _list_databases):
        with self.assertRaisesRegex(ValueError, "n'existe plus"):
            web.open_postgresql_console("sodial_v19", "missing_database")


class JobResourceTests(unittest.TestCase):
    def setUp(self):
        with web.JOBS_LOCK:
            self.previous_jobs = web.JOBS.copy()
            self.previous_next_job_id = web.NEXT_JOB_ID
            web.JOBS.clear()
            web.NEXT_JOB_ID = 1

    def tearDown(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with web.JOBS_LOCK:
                if not any(job.status == "running" for job in web.JOBS.values()):
                    break
            time.sleep(0.01)
        with web.JOBS_LOCK:
            web.JOBS.clear()
            web.JOBS.update(self.previous_jobs)
            web.NEXT_JOB_ID = self.previous_next_job_id

    def wait_for(self, job):
        deadline = time.monotonic() + 2
        while job.status == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertNotEqual(job.status, "running")

    def test_completed_job_releases_target_arguments_and_thread(self):
        payload = b"zip-content" * 1000
        job = web.Job("Import test", lambda _job, _payload: None, (payload,))

        self.wait_for(job)

        self.assertEqual(job.args, ())
        self.assertIsNone(job.target)
        self.assertIsNone(job.thread)

    def test_completed_job_history_is_bounded(self):
        for index in range(web.MAX_RETAINED_JOBS + 5):
            job = web.Job(f"Job {index}", lambda _job: None)
            self.wait_for(job)

        with web.JOBS_LOCK:
            self.assertLessEqual(len(web.JOBS), web.MAX_RETAINED_JOBS)

    def test_compact_job_snapshot_only_includes_selected_output(self):
        first = web.Job("First", lambda job: job.add("first output"))
        second = web.Job("Second", lambda job: job.add("second output"), project="DEMO")
        self.wait_for(first)
        self.wait_for(second)

        snapshot = web.jobs_snapshot(detail_job_id=second.id, compact=True)
        by_id = {job["id"]: job for job in snapshot}

        self.assertEqual(by_id[first.id]["lines"], [])
        self.assertEqual(by_id[first.id]["output"], "")
        self.assertEqual(by_id[second.id]["project"], "DEMO")
        self.assertEqual(by_id[second.id]["lines"], ["second output"])
        self.assertIn("second output", by_id[second.id]["output"])


class ContainerStatusBatchTests(unittest.TestCase):
    @patch("odoo_manager_web.run_capture")
    def test_skips_docker_probe_when_there_are_no_projects(self, run_capture):
        self.assertEqual(web.container_statuses(()), {})
        run_capture.assert_not_called()

    @patch("odoo_manager_web.run_capture")
    def test_reads_all_container_states_with_one_docker_call(self, run_capture):
        run_capture.return_value = (0, "odoo-DEMO|running\npostgresql-DEMO|exited\n")

        statuses = web.container_statuses(("odoo-DEMO", "postgresql-DEMO", "odoo-MISSING"))

        self.assertEqual(
            statuses,
            {"odoo-DEMO": "running", "postgresql-DEMO": "exited", "odoo-MISSING": "absent"},
        )
        self.assertEqual(run_capture.call_count, 1)


class ProjectDiscoveryTests(unittest.TestCase):
    def test_project_dirs_ignores_inaccessible_compose_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "DEMO"
            project.mkdir()
            (project / "compose.yml").write_text("services: {}\n", encoding="utf-8")
            inaccessible = workspace / "restricted"
            inaccessible.mkdir()
            protected_compose = inaccessible / "docker-compose.yml"
            original_exists = Path.exists

            def exists(path):
                if path == protected_compose:
                    raise PermissionError(5, "Access is denied", str(path))
                return original_exists(path)

            previous_workspace = web.WORKSPACE
            try:
                web.WORKSPACE = workspace
                with patch.object(Path, "exists", autospec=True, side_effect=exists):
                    projects = web.project_dirs()
            finally:
                web.WORKSPACE = previous_workspace

        self.assertEqual(projects, ["DEMO"])


class CommandWorkingDirectoryTests(unittest.TestCase):
    @patch("odoo_manager_web.subprocess.run")
    def test_missing_default_workspace_uses_existing_parent(self, run):
        run.return_value = Mock(returncode=0, stdout="ok")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous_workspace = web.WORKSPACE
            try:
                web.WORKSPACE = root / "not-created-yet"
                code, output = web.run_capture(["docker", "ps"])
            finally:
                web.WORKSPACE = previous_workspace

        self.assertEqual((code, output), (0, "ok"))
        self.assertEqual(Path(run.call_args.kwargs["cwd"]), root)

    @patch("odoo_manager_web.subprocess.run")
    def test_missing_explicit_working_directory_is_reported_without_execution(self, run):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing-project"
            code, output = web.run_capture(["docker", "compose", "ps"], cwd=missing)

        self.assertEqual(code, 2)
        self.assertIn("Dossier de travail introuvable", output)
        run.assert_not_called()

    @patch("odoo_manager_web.platform_id", return_value="windows")
    @patch("odoo_manager_web.wsl_command_with_cwd")
    @patch("odoo_manager_web.subprocess.run")
    def test_wsl_command_translates_windows_cwd_before_execution(self, run, prepare_cwd, _platform):
        windows_cwd = Path(r"C:\Users\Demo\Odoo-projects\DEMO")
        prepared = ["wsl.exe", "--cd", "/mnt/c/Users/Demo/Odoo-projects/DEMO", "--exec", "docker", "compose", "ps"]
        prepare_cwd.return_value = prepared
        run.return_value = Mock(returncode=0, stdout="ok")

        code, output = web.run_capture(
            ["wsl.exe", "--exec", "docker", "compose", "ps"],
            cwd=windows_cwd,
        )

        self.assertEqual((code, output), (0, "ok"))
        prepare_cwd.assert_called_once()
        self.assertEqual(run.call_args.args[0], prepared)
        self.assertEqual(Path(run.call_args.kwargs["cwd"]), Path.home())


class WslManagerCommandTests(unittest.TestCase):
    @patch("odoo_manager_web.execution_path")
    def test_generic_command_environment_keeps_host_paths(self, execution_path):
        previous_settings = web.SETTINGS
        previous_workspace = web.WORKSPACE
        try:
            web.SETTINGS = web.ManagerSettings.from_dict(
                {
                    "execution_mode": "wsl",
                    "wsl_distribution": "Ubuntu",
                    "traefik_directory": r"C:\Users\Demo\docker-local-tools\traefik",
                },
                r"C:\Users\Demo\Odoo-projects",
            )
            web.WORKSPACE = Path(r"C:\Users\Demo\Odoo-projects")

            environment = web.command_env()
        finally:
            web.SETTINGS = previous_settings
            web.WORKSPACE = previous_workspace

        execution_path.assert_not_called()
        self.assertEqual(environment["ODOO_WORKSPACE"], r"C:\Users\Demo\Odoo-projects")
        self.assertEqual(
            environment["TRAEFIK_DIR"],
            r"C:\Users\Demo\docker-local-tools\traefik",
        )

    @patch("odoo_manager_web.local_traefik_directory", return_value=Path(r"C:\Users\Demo\docker-local-tools\traefik"))
    @patch("odoo_manager_web.resolve_host_executable", return_value=r"C:\Docker\docker.exe")
    @patch("odoo_manager_web.execution_path")
    @patch("odoo_manager_web.platform_id", return_value="windows")
    def test_shell_compatibility_uses_host_docker_from_wsl(
        self,
        _platform,
        execution_path,
        _resolve_docker,
        _traefik,
    ):
        execution_path.side_effect = lambda path, _settings: {
            r"C:\Users\Demo\Odoo-projects": "/mnt/c/Users/Demo/Odoo-projects",
            r"C:\Docker\docker.exe": "/mnt/c/Docker/docker.exe",
            r"C:\Users\Demo\docker-local-tools\traefik": "/mnt/c/Users/Demo/docker-local-tools/traefik",
            r"C:\Odoo Manager\odoo_manager.sh": "/mnt/c/Odoo Manager/odoo_manager.sh",
        }[str(path)]
        previous_settings = web.SETTINGS
        previous_workspace = web.WORKSPACE
        previous_manager = web.MANAGER
        try:
            web.SETTINGS = web.ManagerSettings.from_dict(
                {"execution_mode": "wsl", "wsl_distribution": "Ubuntu"},
                r"C:\Users\Demo\Odoo-projects",
            )
            web.WORKSPACE = Path(r"C:\Users\Demo\Odoo-projects")
            web.MANAGER = Path(r"C:\Odoo Manager\odoo_manager.sh")

            command = web.manager_command("--update-module", "DEMO", "demo", "sale")
        finally:
            web.SETTINGS = previous_settings
            web.WORKSPACE = previous_workspace
            web.MANAGER = previous_manager

        self.assertEqual(command[:6], ["wsl.exe", "-d", "Ubuntu", "--exec", "env", "ODOO_WORKSPACE=/mnt/c/Users/Demo/Odoo-projects"])
        self.assertIn("ODOO_MANAGER_DOCKER=/mnt/c/Docker/docker.exe", command)
        self.assertIn("TRAEFIK_DIR=/mnt/c/Users/Demo/docker-local-tools/traefik", command)
        self.assertEqual(command[-4:], ["--update-module", "DEMO", "demo", "sale"])


class BootstrapSnapshotTests(unittest.TestCase):
    @patch("odoo_manager_web.jobs_snapshot", return_value=[])
    @patch("odoo_manager_web.container_status", return_value="absent")
    @patch("odoo_manager_web.docker_status")
    def test_first_start_succeeds_before_default_workspace_exists(
        self,
        docker_status,
        _container_status,
        _jobs_snapshot,
    ):
        docker_status.return_value = {
            "state": "ready",
            "installed": True,
            "running": True,
            "message": "Docker est opérationnel.",
            "platform": "windows",
            "execution_mode": "native",
            "can_start": False,
        }
        with tempfile.TemporaryDirectory() as temporary:
            previous_workspace = web.WORKSPACE
            try:
                web.WORKSPACE = Path(temporary) / "Odoo-projects"
                payload = web.bootstrap_snapshot()
            finally:
                web.WORKSPACE = previous_workspace

        self.assertEqual(payload["overview"]["projects"], [])
        self.assertFalse(payload["system_status"]["workspace_exists"])
        self.assertFalse(payload["settings"]["workspace_exists"])

    @patch("odoo_manager_web.jobs_snapshot", return_value=[])
    @patch("odoo_manager_web.project_dirs", return_value=[])
    @patch("odoo_manager_web.traefik_status")
    @patch("odoo_manager_web.docker_status")
    def test_bootstrap_uses_one_coherent_docker_probe(
        self,
        docker_status,
        traefik_status,
        _project_dirs,
        _jobs_snapshot,
    ):
        docker = {
            "state": "ready",
            "installed": True,
            "running": True,
            "message": "Docker est opérationnel.",
            "platform": "macos",
            "execution_mode": "native",
            "can_start": False,
        }
        docker_status.return_value = docker
        traefik_status.return_value = {"state": "running", "running": True}

        payload = web.bootstrap_snapshot()

        docker_status.assert_called_once_with(web.SETTINGS)
        traefik_status.assert_called_once_with(docker)
        self.assertIs(payload["system_status"]["docker"], docker)
        self.assertTrue(payload["overview"]["docker_ok"])
        self.assertEqual(payload["overview"]["docker_message"], docker["message"])
        self.assertEqual(payload["settings"]["platform"], web.platform_id())

    @patch("odoo_manager_web.docker_status")
    def test_settings_snapshot_does_not_probe_docker(self, docker_status):
        payload = web.settings_snapshot()

        docker_status.assert_not_called()
        self.assertIn(payload["platform"], {"macos", "windows", "linux"})


class TraefikPathTests(unittest.TestCase):
    @patch("odoo_manager_web.Path.home")
    def test_wsl_mode_uses_concrete_host_traefik_path(self, home):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home.return_value = root
            previous_settings = web.SETTINGS
            try:
                web.SETTINGS = web.ManagerSettings.from_dict(
                    {"execution_mode": "wsl", "wsl_distribution": "Ubuntu"},
                    root / "Odoo-projects",
                )
                expected = root / "docker-local-tools" / "traefik"
                self.assertEqual(web.local_traefik_directory(), expected)
                self.assertEqual(web.traefik_directory_label(), str(expected))
            finally:
                web.SETTINGS = previous_settings


class ProjectCreationPrerequisitesTests(unittest.TestCase):
    @patch("odoo_manager_web.wsl_executable_available", return_value=True)
    @patch("odoo_manager_web.host_executable_available", return_value=True)
    @patch("odoo_manager_web.platform_id", return_value="windows")
    @patch("odoo_manager_web.run_capture")
    def test_wsl_workspace_uses_git_and_ssh_from_detected_distribution(
        self,
        run_capture,
        _platform,
        _host_available,
        _wsl_available,
    ):
        def capture(command, **_kwargs):
            if "git" in command and "--version" in command:
                return 0, "git version 2.50.1"
            if "find" in " ".join(command):
                return 0, "/home/demo/.ssh/id_ed25519.pub"
            return 0, ""

        run_capture.side_effect = capture
        previous_workspace = web.WORKSPACE
        previous_settings = web.SETTINGS
        try:
            workspace = r"\\wsl.localhost\Ubuntu-24.04\home\demo\Odoo-projects"
            web.SETTINGS = web.ManagerSettings.from_dict({"execution_mode": "native"}, workspace)
            web.WORKSPACE = Path(workspace)

            payload = web.project_creation_prerequisites()
        finally:
            web.WORKSPACE = previous_workspace
            web.SETTINGS = previous_settings

        self.assertTrue(payload["git_available"])
        self.assertEqual(payload["ssh_keys"], ["id_ed25519.pub"])
        self.assertEqual(payload["tool_environment"], "WSL (Ubuntu-24.04)")
        git_command = next(command for command in (call.args[0] for call in run_capture.call_args_list) if "git" in command)
        self.assertEqual(git_command[:5], ["wsl.exe", "-d", "Ubuntu-24.04", "--exec", "git"])

    @patch("odoo_manager_web.run_capture", return_value=(0, "git version 2.50.0"))
    @patch("odoo_manager_web.Path.home")
    def test_reports_git_workspace_and_public_keys_without_reading_private_key(self, home, _run_capture):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ssh = root / ".ssh"
            ssh.mkdir()
            (ssh / "id_ed25519.pub").write_text("ssh-ed25519 public", encoding="utf-8")
            (ssh / "id_ed25519").write_text("private", encoding="utf-8")
            home.return_value = root
            previous_workspace = web.WORKSPACE
            try:
                web.WORKSPACE = root
                payload = web.project_creation_prerequisites()
            finally:
                web.WORKSPACE = previous_workspace

        self.assertTrue(payload["git_available"])
        self.assertTrue(payload["ssh_key_present"])
        self.assertEqual(payload["ssh_keys"], ["id_ed25519.pub"])

    @patch("odoo_manager_web.run_capture")
    @patch("odoo_manager_web.resolve_executable", return_value="ssh-keygen")
    @patch("odoo_manager_web.executable_available", return_value=True)
    @patch("odoo_manager_web.Path.home")
    def test_generates_ed25519_key_and_returns_only_public_material(
        self,
        home,
        _available,
        _resolve,
        run_capture,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home.return_value = root

            def generate(command, **_kwargs):
                key_path = Path(command[command.index("-f") + 1])
                key_path.write_text("PRIVATE", encoding="utf-8")
                key_path.with_suffix(".pub").write_text(
                    "ssh-ed25519 AAAATEST chef.projet@sudokeys.com\n",
                    encoding="utf-8",
                )
                return 0, "generated"

            run_capture.side_effect = generate
            payload = web.generate_ssh_key("chef.projet@sudokeys.com")

        self.assertTrue(payload["created"])
        self.assertEqual(payload["name"], "id_ed25519.pub")
        self.assertTrue(payload["public_key"].startswith("ssh-ed25519 "))
        self.assertNotIn("PRIVATE", str(payload))

    @patch("odoo_manager_web.run_capture")
    @patch("odoo_manager_web.Path.home")
    def test_existing_private_key_is_never_overwritten(self, home, run_capture):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ssh = root / ".ssh"
            ssh.mkdir()
            (ssh / "id_ed25519").write_text("PRIVATE", encoding="utf-8")
            home.return_value = root

            with self.assertRaisesRegex(RuntimeError, "Aucun fichier n'a été écrasé"):
                web.generate_ssh_key()

        run_capture.assert_not_called()


class GitInstallationTests(unittest.TestCase):
    class LogJob:
        def __init__(self):
            self.lines = []

        def add(self, line):
            self.lines.append(line)

    @patch("odoo_manager_web.run_stream", return_value=0)
    @patch("odoo_manager_web.run_capture")
    @patch("odoo_manager_web.resolve_executable")
    @patch("odoo_manager_web.executable_available", return_value=True)
    @patch("odoo_manager_web.platform_id", return_value="windows")
    def test_installs_git_with_noninteractive_winget_command(
        self,
        _platform,
        _available,
        resolve,
        capture,
        stream,
    ):
        resolve.side_effect = lambda executable, _settings: executable
        capture.side_effect = [(127, "missing"), (0, "git version 2.51.0")]
        job = self.LogJob()

        web.install_git_job(job)

        command = stream.call_args.args[1]
        self.assertEqual(command[:5], ["winget", "install", "--id", "Git.Git", "--exact"])
        self.assertIn("--silent", command)
        self.assertIn("--disable-interactivity", command)
        self.assertIn("git version 2.51.0", job.lines)

    @patch("odoo_manager_web.run_stream", return_value=0)
    @patch("odoo_manager_web.run_capture")
    @patch("odoo_manager_web.platform_id", return_value="windows")
    def test_installs_git_inside_distribution_selected_by_workspace(self, _platform, capture, stream):
        capture.side_effect = [(127, "missing"), (0, "git version 2.51.0")]
        previous_workspace = web.WORKSPACE
        previous_settings = web.SETTINGS
        try:
            workspace = r"\\wsl.localhost\Debian\home\demo\Odoo-projects"
            web.SETTINGS = web.ManagerSettings.from_dict({}, workspace)
            web.WORKSPACE = Path(workspace)
            job = self.LogJob()

            web.install_git_job(job)
        finally:
            web.WORKSPACE = previous_workspace
            web.SETTINGS = previous_settings

        command = stream.call_args.args[1]
        self.assertEqual(command[:7], ["wsl.exe", "-d", "Debian", "-u", "root", "--exec", "sh"])
        self.assertIn("apt-get install -y git openssh-client", command[-1])


class DiagnosticModuleTests(unittest.TestCase):
    class LogJob:
        def __init__(self):
            self.lines = []

        def add(self, line):
            self.lines.append(line)

    def test_studio_customization_is_not_reported_as_missing_code(self):
        states = {"studio_customization": {"state": "installed"}}

        missing = web.modules_missing_from_code(states, set(), {"installed"})

        self.assertEqual(missing, [])

    def test_distinguishes_missing_pending_modules_from_available_modules(self):
        states = {
            "missing_dependency": {"state": "to upgrade"},
            "available_custom": {"state": "to upgrade"},
            "installed_missing": {"state": "installed"},
        }

        missing = web.modules_missing_from_code(
            states,
            {"available_custom"},
            web.TRANSIENT_MODULE_STATES,
        )

        self.assertEqual(missing, ["missing_dependency"])

    def test_filestore_count_only_includes_referenced_files_that_are_present(self):
        stats, missing = web.filestore_summary(
            ["aa/referenced", "bb/missing", "aa/referenced"],
            {"aa/referenced", "cc/orphan"},
        )

        self.assertEqual(stats["referenced"], 3)
        self.assertEqual(stats["referenced_unique"], 2)
        self.assertEqual(stats["actual"], 1)
        self.assertEqual(stats["physical_total"], 2)
        self.assertEqual(stats["missing"], 1)
        self.assertEqual(missing, ["bb/missing"])

    def test_update_all_modules_uses_explicit_no_filestore_command(self):
        self.assertEqual(
            web.update_all_modules_manager_args("DEMO", "demo", True),
            ("--update-all-modules-without-filestore", "DEMO", "demo"),
        )
        self.assertEqual(
            web.update_all_modules_manager_args("DEMO", "demo", False),
            ("--update-all-modules", "DEMO", "demo"),
        )

    def test_available_update_list_excludes_missing_and_uninstalled_modules(self):
        states = {
            "base": {"state": "installed"},
            "protexodoo": {"state": "to upgrade"},
            "auto_backup": {"state": "installed"},
            "not_installed": {"state": "uninstalled"},
        }

        modules = web.available_update_modules(
            "DEMO",
            "demo",
            states=states,
            available_names={"base", "protexodoo", "not_installed"},
            excluded_names={"protexodoo"},
        )

        self.assertEqual(modules, ["base"])

    def test_incomplete_filestore_is_a_bounded_nonblocking_warning(self):
        missing = [f"00/file-{index}" for index in range(12)]

        issue = web.filestore_diagnostic_issue("demo", "/workspace/demo/filestore", missing)

        self.assertEqual(issue["severity"], "warning")
        self.assertIn("non bloquant", issue["title"])
        self.assertIn("pas nécessaire de télécharger", issue["details"])
        self.assertEqual(issue["items"][:5], missing[:5])
        self.assertEqual(issue["items"][-1], "... 7 autre(s) fichier(s) manquant(s) non affiché(s)")

    def test_local_ignore_allows_an_absent_dependency_chain_selected_together(self):
        states = {
            "auto_backup": {"state": "to upgrade"},
            "auto_backup_sh": {"state": "to upgrade"},
            "protexodoo": {"state": "to upgrade"},
        }

        candidates, invalid, automatic = web.local_ignore_plan(
            states,
            {"protexodoo"},
            {"auto_backup", "auto_backup_sh"},
            [("auto_backup_sh", "auto_backup")],
        )

        self.assertEqual(candidates, ["auto_backup", "auto_backup_sh"])
        self.assertEqual(invalid, [])
        self.assertEqual(automatic, [])

    def test_local_ignore_automatically_excludes_an_active_dependent_module(self):
        states = {
            "account_invoice_margin": {"state": "to upgrade"},
            "protexodoo": {"state": "to upgrade"},
        }

        candidates, invalid, automatic = web.local_ignore_plan(
            states,
            {"protexodoo"},
            {"account_invoice_margin"},
            [("protexodoo", "account_invoice_margin")],
        )

        self.assertEqual(candidates, ["account_invoice_margin"])
        self.assertEqual(invalid, [])
        self.assertEqual(automatic, ["protexodoo"])

    def test_local_ignore_only_accepts_requested_modules_with_missing_code(self):
        states = {
            "protexodoo": {"state": "to upgrade"},
            "protex_studio": {"state": "to upgrade"},
        }

        candidates, invalid, automatic = web.local_ignore_plan(
            states,
            {"protexodoo", "protex_studio"},
            {"protexodoo", "protex_studio"},
            [("protex_studio", "protexodoo")],
        )

        self.assertEqual(candidates, [])
        self.assertEqual(invalid, ["protex_studio", "protexodoo"])
        self.assertEqual(automatic, [])

    def test_local_ignore_cascades_through_multiple_active_dependents(self):
        states = {
            "product_sequence": {"state": "to upgrade"},
            "emph_base": {"state": "to upgrade"},
            "emph_sale": {"state": "installed"},
        }

        candidates, invalid, automatic = web.local_ignore_plan(
            states,
            {"emph_base", "emph_sale"},
            {"product_sequence"},
            [("emph_base", "product_sequence"), ("emph_sale", "emph_base")],
        )

        self.assertEqual(candidates, ["product_sequence"])
        self.assertEqual(invalid, [])
        self.assertEqual(automatic, ["emph_base", "emph_sale"])

    def test_local_ignore_accepts_dependency_of_an_already_excluded_module(self):
        states = {
            "account_invoice_margin": {"state": "to upgrade"},
            "protexodoo": {"state": "installed"},
        }

        candidates, invalid, automatic = web.local_ignore_plan(
            states,
            {"protexodoo"},
            {"account_invoice_margin"},
            [("protexodoo", "account_invoice_margin")],
            already_excluded={"protexodoo"},
        )

        self.assertEqual(candidates, ["account_invoice_margin"])
        self.assertEqual(invalid, [])
        self.assertEqual(automatic, [])

    def test_local_ignored_modules_are_persisted_per_workspace_project_and_database(self):
        with tempfile.TemporaryDirectory() as directory:
            overrides = Path(directory) / "local_module_overrides.json"
            with patch.object(web, "LOCAL_MODULE_OVERRIDES", overrides), patch.object(web, "WORKSPACE", Path("/workspace-a")):
                web.remember_ignored_missing_modules("DEMO", "demo", ["auto_backup"])
                web.remember_ignored_missing_modules("DEMO", "demo", ["auto_backup_sh"])

                self.assertEqual(
                    web.ignored_missing_modules("DEMO", "demo"),
                    {"auto_backup", "auto_backup_sh"},
                )
                self.assertEqual(web.ignored_missing_modules("DEMO", "other"), set())

    @patch("odoo_manager_web.remember_ignored_missing_modules")
    @patch("odoo_manager_web.db_query_lines")
    @patch("odoo_manager_web.module_dirs")
    @patch("odoo_manager_web.installed_modules")
    @patch("odoo_manager_web.container_status", return_value="running")
    @patch("odoo_manager_web.validate_project", return_value="DEMO")
    def test_cancel_missing_operations_updates_only_transient_rows(
        self,
        _validate_project,
        _container_status,
        installed_modules,
        module_dirs,
        db_query_lines,
        remember_ignored,
    ):
        installed_modules.return_value = {
            "auto_backup": {"state": "to upgrade"},
            "auto_backup_sh": {"state": "to upgrade"},
            "protexodoo": {"state": "to upgrade"},
        }
        module_dirs.return_value = [Path("/addons/protexodoo")]
        db_query_lines.side_effect = [
            ["auto_backup_sh|auto_backup"],
            ["auto_backup|installed", "auto_backup_sh|installed"],
        ]
        job = self.LogJob()

        web.cancel_missing_module_operations_job(job, "DEMO", "demo", "auto_backup,auto_backup_sh")

        update_query = db_query_lines.call_args_list[1].args[2]
        self.assertIn("state in ('to install','to upgrade','to remove')", update_query)
        self.assertIn("state in ('installed','uninstalled')", update_query)
        self.assertNotIn("delete", update_query.lower())
        remember_ignored.assert_called_once_with("DEMO", "demo", ["auto_backup", "auto_backup_sh"])
        self.assertIn("Aucune donnée métier", "\n".join(job.lines))

    @patch("odoo_manager_web.remember_ignored_missing_modules")
    @patch("odoo_manager_web.db_query_lines")
    @patch("odoo_manager_web.module_dirs")
    @patch("odoo_manager_web.installed_modules")
    @patch("odoo_manager_web.container_status", return_value="running")
    @patch("odoo_manager_web.validate_project", return_value="DEMO")
    def test_cancel_missing_operations_resets_and_excludes_dependents_automatically(
        self,
        _validate_project,
        _container_status,
        installed_modules,
        module_dirs,
        db_query_lines,
        remember_ignored,
    ):
        installed_modules.return_value = {
            "product_sequence": {"state": "to upgrade"},
            "emph_base": {"state": "to upgrade"},
        }
        module_dirs.return_value = [Path("/addons/emph_base")]
        db_query_lines.side_effect = [
            ["emph_base|product_sequence"],
            ["emph_base|installed", "product_sequence|installed"],
        ]
        job = self.LogJob()

        web.cancel_missing_module_operations_job(job, "DEMO", "demo", "product_sequence")

        update_query = db_query_lines.call_args_list[1].args[2]
        self.assertIn("'emph_base'", update_query)
        self.assertIn("'product_sequence'", update_query)
        remember_ignored.assert_called_once_with("DEMO", "demo", ["emph_base", "product_sequence"])
        self.assertIn("Dépendants exclus automatiquement", "\n".join(job.lines))


class DatabaseNeutralizationTests(unittest.TestCase):
    class LogJob:
        def __init__(self):
            self.lines = []

        def add(self, line):
            self.lines.append(line)

    @patch("odoo_manager_web.list_databases_for", return_value=["postgres", "demo"])
    @patch("odoo_manager_web.project_service")
    @patch("odoo_manager_web.validate_project", return_value="DEMO")
    def test_neutralize_delegates_to_the_odoo_engine(
        self,
        _validate_project,
        project_service,
        _list_databases,
    ):
        job = self.LogJob()

        web.neutralize_database_job(job, "DEMO", "demo")

        project_service.return_value.run_odoo_neutralize_command.assert_called_once_with(
            "DEMO",
            "demo",
            log=job.add,
        )

    @patch("odoo_manager_web.list_databases_for", return_value=["postgres"])
    @patch("odoo_manager_web.validate_project", return_value="DEMO")
    def test_neutralize_rejects_an_unknown_database(self, _validate_project, _list_databases):
        job = self.LogJob()

        with self.assertRaisesRegex(ValueError, "n'existe plus"):
            web.neutralize_database_job(job, "DEMO", "demo")


if __name__ == "__main__":
    unittest.main()
