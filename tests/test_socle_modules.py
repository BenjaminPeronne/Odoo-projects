import subprocess
from unittest import mock

from test_module_layout import DummyJob, ModuleLayoutTests
import odoo_manager_web as web


class SocleModulesTests(ModuleLayoutTests):
    def test_generated_wsl_script_checks_targets_and_cleans_up(self):
        sources = {name: self.create_enterprise_module(name) for name in ("valid", "absent", "broken", "other")}
        addons = self.project_root / "odoo" / "addons"
        (addons / "valid").symlink_to(sources["valid"])
        (addons / "broken").symlink_to("../missing")
        (addons / "other").symlink_to(sources["valid"])
        service = mock.Mock()
        def execute(command, **kwargs):
            result = subprocess.run(["sh", command[-1]], capture_output=True, text=True, timeout=10)
            return result.returncode, result.stdout
        service.capture.side_effect = execute
        creator = web.ProjectCreator(web.SETTINGS, web.WORKSPACE, service)
        creator.settings = mock.Mock(execution_mode="wsl", wsl_distribution="Ubuntu")
        with mock.patch("odoo_manager_core.project_creator.platform_id", return_value="windows"), \
                mock.patch("odoo_manager_core.project_creator.wsl_execution_path", side_effect=lambda path, distribution: str(path)):
            states = creator.module_link_states(sources, addons)
        self.assertEqual({"valid": "correct", "absent": "missing", "broken": "conflict", "other": "conflict"}, states)
        self.assertEqual([], list(addons.glob(".odoo_manager_check_*")))

    def test_wsl_validation_does_not_use_windows_link_visibility(self):
        source = self.create_enterprise_module("account_accountant")
        creator = web.ProjectCreator(web.SETTINGS, web.WORKSPACE, mock.Mock())
        creator.settings = mock.Mock(execution_mode="wsl", wsl_distribution="Ubuntu")
        creator.project_service.capture.return_value = (0, "account_accountant\tcorrect\n")
        with mock.patch("odoo_manager_core.project_creator.platform_id", return_value="windows"), \
                mock.patch("odoo_manager_core.project_creator.wsl_execution_path", side_effect=lambda path, distribution: str(path)), \
                mock.patch.object(creator, "path_entry_exists", side_effect=AssertionError("Windows check")):
            states = creator.module_link_states({source.name: source}, source.parent)
        self.assertEqual({source.name: "correct"}, states)
        self.assertEqual(1, creator.project_service.capture.call_count)

    def test_wsl_incomplete_validation_is_an_error(self):
        source = self.create_enterprise_module("account_accountant")
        creator = web.ProjectCreator(web.SETTINGS, web.WORKSPACE, mock.Mock())
        creator.settings = mock.Mock(execution_mode="wsl", wsl_distribution="Ubuntu")
        creator.project_service.capture.return_value = (0, "")
        with mock.patch("odoo_manager_core.project_creator.platform_id", return_value="windows"), \
                mock.patch("odoo_manager_core.project_creator.wsl_execution_path", side_effect=lambda path, distribution: str(path)):
            with self.assertRaisesRegex(RuntimeError, "depuis WSL"):
                creator.module_link_states({source.name: source}, source.parent)

    def test_existing_enterprise_links_can_be_checked_again(self):
        self.create_enterprise_module("account_accountant")
        web.ensure_enterprise_module_links(DummyJob(), self.project)
        job = DummyJob()
        web.ensure_enterprise_module_links(job, self.project)
        self.assertTrue(any("0 lien(s) créé(s)" in line for line in job.lines))

    def test_broken_link_is_not_accepted_as_success(self):
        self.create_enterprise_module("account_accountant")
        link = self.project_root / "odoo" / "addons" / "account_accountant"
        def create_broken(*args, **kwargs):
            link.symlink_to("../missing")
        with mock.patch.object(web.ProjectCreator, "link_modules", side_effect=create_broken):
            with self.assertRaisesRegex(RuntimeError, "incomplète"):
                web.ensure_enterprise_module_links(DummyJob(), self.project)

    def create_enterprise_module(self, name):
        module = self.project_root / "odoo" / "addons-store" / "odoo_enterprise" / name
        module.mkdir(parents=True, exist_ok=True)
        (module / "__manifest__.py").write_text("{'name': 'Test'}\n", encoding="utf-8")
        return module

    def test_enterprise_links_are_created_and_verified(self):
        source = self.create_enterprise_module("sale_management")
        job = DummyJob()

        available = web.ensure_enterprise_module_links(job, self.project)

        link = self.project_root / "odoo" / "addons" / "sale_management"
        self.assertEqual({"sale_management"}, available)
        self.assertTrue(link.is_symlink())
        self.assertEqual(source.resolve(), link.resolve())
        self.assertTrue(any("1 lien(s) créé(s)" in line for line in job.lines))

    def test_enterprise_link_conflict_is_not_overwritten(self):
        self.create_enterprise_module("sale_management")
        conflict = self.project_root / "odoo" / "addons" / "sale_management"
        conflict.mkdir()
        (conflict / "__manifest__.py").write_text("{'name': 'Other'}\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "autre source"):
            web.ensure_enterprise_module_links(DummyJob(), self.project)

        self.assertFalse(conflict.is_symlink())

    def test_french_accounting_socle_does_not_request_other_localizations(self):
        requested = []

        def record_install(job, flag, project, db_name, modules):
            requested.extend(modules.split(","))

        with mock.patch.object(web, "ensure_enterprise_module_links", return_value={"account_accountant", "l10n_fr"}), \
                mock.patch.object(web, "module_dirs", return_value=iter([
                    self.create_enterprise_module("account_accountant"),
                    self.create_enterprise_module("l10n_fr"),
                ])), \
                mock.patch.object(web, "installed_modules", return_value={}), \
                mock.patch.object(web, "module_command_job", side_effect=record_install):
            web.install_socle_job(DummyJob(), self.project, "test_db", "accounting_fr")

        self.assertEqual(["account_accountant", "l10n_fr"], requested)
        self.assertFalse(any(name.startswith("l10n_") and name != "l10n_fr" for name in requested))

    def test_unknown_socle_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "inconnues"):
            web.validate_socle_presets("sales,unknown")

    def test_already_installed_socle_modules_are_not_reinstalled(self):
        sale = self.create_enterprise_module("sale_management")
        calls = []

        with mock.patch.object(web, "ensure_enterprise_module_links", return_value={"sale_management"}), \
                mock.patch.object(web, "module_dirs", return_value=iter([sale])), \
                mock.patch.object(web, "installed_modules", return_value={
                    "sale_management": {"state": "installed"},
                }), \
                mock.patch.object(web, "module_command_job", side_effect=lambda *args: calls.append(args)):
            job = DummyJob()
            web.install_socle_job(job, self.project, "test_db", "sales")

        self.assertEqual([], calls)
        self.assertTrue(any("déjà entièrement installé" in line for line in job.lines))

    def test_imported_modules_are_installed_or_updated_according_to_database_state(self):
        alpha = self.create_enterprise_module("alpha")
        beta = self.create_enterprise_module("beta")
        calls = []

        def record_command(job, flag, project, db_name, modules):
            calls.append((flag, modules))

        job = DummyJob()
        with mock.patch.object(web, "module_dirs", return_value=iter([alpha, beta])), \
                mock.patch.object(web, "installed_modules", return_value={
                    "alpha": {"state": "installed"},
                    "beta": {"state": "uninstalled"},
                }), \
                mock.patch.object(web, "ignored_missing_modules", return_value=set()), \
                mock.patch.object(web, "module_command_job", side_effect=record_command):
            web.update_imported_modules_job(job, self.project, "test_db", "alpha,beta")

        self.assertEqual(calls, [("--install-module", "beta"), ("--update-module", "alpha")])
        self.assertEqual(job.result, {"kind": "module_update", "scope": "imported", "modules": ["alpha", "beta"]})
