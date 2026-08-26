import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import odoo_manager_web as web


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


class ContainerStatusBatchTests(unittest.TestCase):
    @patch("odoo_manager_web.run_capture")
    def test_reads_all_container_states_with_one_docker_call(self, run_capture):
        run_capture.return_value = (0, "odoo-DEMO|running\npostgresql-DEMO|exited\n")

        statuses = web.container_statuses(("odoo-DEMO", "postgresql-DEMO", "odoo-MISSING"))

        self.assertEqual(
            statuses,
            {"odoo-DEMO": "running", "postgresql-DEMO": "exited", "odoo-MISSING": "absent"},
        )
        self.assertEqual(run_capture.call_count, 1)


class BootstrapSnapshotTests(unittest.TestCase):
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


class ProjectCreationPrerequisitesTests(unittest.TestCase):
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

        candidates, invalid, blockers = web.local_ignore_plan(
            states,
            {"protexodoo"},
            {"auto_backup", "auto_backup_sh"},
            [("auto_backup_sh", "auto_backup")],
        )

        self.assertEqual(candidates, ["auto_backup", "auto_backup_sh"])
        self.assertEqual(invalid, [])
        self.assertEqual(blockers, {})

    def test_local_ignore_refuses_a_dependency_of_an_active_available_module(self):
        states = {
            "account_invoice_margin": {"state": "to upgrade"},
            "protexodoo": {"state": "to upgrade"},
        }

        candidates, invalid, blockers = web.local_ignore_plan(
            states,
            {"protexodoo"},
            {"account_invoice_margin"},
            [("protexodoo", "account_invoice_margin")],
        )

        self.assertEqual(candidates, ["account_invoice_margin"])
        self.assertEqual(invalid, [])
        self.assertEqual(blockers, {"account_invoice_margin": ["protexodoo"]})

    def test_local_ignore_accepts_available_dependent_modules_selected_together(self):
        states = {
            "protexodoo": {"state": "to upgrade"},
            "protex_studio": {"state": "to upgrade"},
        }

        candidates, invalid, blockers = web.local_ignore_plan(
            states,
            {"protexodoo", "protex_studio"},
            {"protexodoo", "protex_studio"},
            [("protex_studio", "protexodoo")],
        )

        self.assertEqual(candidates, ["protex_studio", "protexodoo"])
        self.assertEqual(invalid, [])
        self.assertEqual(blockers, {})

    def test_local_ignore_accepts_dependency_of_an_already_excluded_module(self):
        states = {
            "account_invoice_margin": {"state": "to upgrade"},
            "protexodoo": {"state": "installed"},
        }

        candidates, invalid, blockers = web.local_ignore_plan(
            states,
            {"protexodoo"},
            {"account_invoice_margin"},
            [("protexodoo", "account_invoice_margin")],
            already_excluded={"protexodoo"},
        )

        self.assertEqual(candidates, ["account_invoice_margin"])
        self.assertEqual(invalid, [])
        self.assertEqual(blockers, {})

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
        self.assertNotIn("delete", update_query.lower())
        remember_ignored.assert_called_once_with("DEMO", "demo", ["auto_backup", "auto_backup_sh"])
        self.assertIn("Aucune donnée métier", "\n".join(job.lines))


if __name__ == "__main__":
    unittest.main()
