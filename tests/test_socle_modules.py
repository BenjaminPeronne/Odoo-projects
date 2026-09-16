import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_module_layout import DummyJob, ModuleLayoutTests
import odoo_manager_web as web

class SocleModulesTests(ModuleLayoutTests):
    @unittest.skipIf(os.name == "nt", "requires POSIX symlink semantics provided by WSL")
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
                mock.patch(
                    "odoo_manager_core.project_creator.wsl_execution_path",
                    side_effect=lambda path, distribution: str(path),
                ):
            states = creator.module_link_states(sources, addons)
        self.assertEqual({"valid": "correct", "absent": "missing", "broken": "conflict", "other": "provided"}, states)
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

    def test_native_windows_checks_links_created_by_wsl_from_wsl(self):
        # Windows forces the native mode, yet link_modules creates the links with
        # WSL: Windows cannot read them (WinError 1920) and reported false conflicts.
        source = self.create_enterprise_module("account_accountant")
        creator = web.ProjectCreator(web.SETTINGS, web.WORKSPACE, mock.Mock())
        creator.settings = mock.Mock(execution_mode="native", wsl_distribution="")
        creator.project_service.capture.return_value = (0, "account_accountant\tcorrect\n")
        with mock.patch("odoo_manager_core.project_creator.platform_id", return_value="windows"), \
                mock.patch("odoo_manager_core.project_creator.host_executable_available", return_value=True), \
                mock.patch("odoo_manager_core.project_creator.wsl_execution_path", side_effect=lambda path, distribution: str(path)), \
                mock.patch.object(creator, "path_entry_exists", side_effect=AssertionError("Windows check")):
            states = creator.module_link_states({source.name: source}, source.parent)
        self.assertEqual({source.name: "correct"}, states)
        self.assertEqual(1, creator.project_service.capture.call_count)

    def test_windows_without_wsl_distribution_checks_native_links_natively(self):
        # wsl.exe can exist without any distribution (GitHub Windows runners):
        # link_modules then creates native links, which must be checked natively.
        source = self.create_enterprise_module("account_accountant")
        creator = web.ProjectCreator(web.SETTINGS, web.WORKSPACE, mock.Mock())
        creator.settings = mock.Mock(execution_mode="native", wsl_distribution="")
        with mock.patch("odoo_manager_core.project_creator.platform_id", return_value="windows"), \
                mock.patch("odoo_manager_core.project_creator.host_executable_available", return_value=True), \
                mock.patch(
                    "odoo_manager_core.project_creator.wsl_execution_path",
                    side_effect=RuntimeError("Windows Subsystem for Linux has no installed distributions."),
                ), \
                mock.patch.object(creator, "path_entry_exists", return_value=False) as path_entry_exists:
            states = creator.module_link_states({source.name: source}, source.parent)
        self.assertEqual({source.name: "missing"}, states)
        path_entry_exists.assert_called_once()
        creator.project_service.capture.assert_not_called()

    def test_wsl_mode_without_path_translation_is_an_error(self):
        source = self.create_enterprise_module("account_accountant")
        creator = web.ProjectCreator(web.SETTINGS, web.WORKSPACE, mock.Mock())
        creator.settings = mock.Mock(execution_mode="wsl", wsl_distribution="Ubuntu")
        with mock.patch("odoo_manager_core.project_creator.platform_id", return_value="windows"), \
                mock.patch("odoo_manager_core.project_creator.wsl_execution_path", side_effect=RuntimeError("no distribution")):
            with self.assertRaisesRegex(RuntimeError, "no distribution"):
                creator.module_link_states({source.name: source}, source.parent)

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

    def test_socle_job_logs_dependencies_and_rejects_missing_ones(self):
        mrp = self.create_enterprise_module("mrp")
        (mrp / "__manifest__.py").write_text("{'name': 'MRP', 'depends': ['stock']}\n", encoding="utf-8")
        stock = self.create_enterprise_module("stock")
        (stock / "__manifest__.py").write_text("{'name': 'Inventory', 'depends': ['product'], 'application': True}\n", encoding="utf-8")
        product = self.create_enterprise_module("product")
        calls = []

        with mock.patch.object(web, "ensure_enterprise_module_links", return_value=set()), \
                mock.patch.object(web, "module_dirs", return_value=iter([mrp, stock, product])), \
                mock.patch.object(web, "installed_modules", return_value={}), \
                mock.patch.object(web, "module_command_job", side_effect=lambda *args: calls.append(args[1:])):
            job = DummyJob()
            web.install_socle_job(job, self.project, "test_db", "manufacturing")

        self.assertEqual([("--install-module", self.project, "test_db", "mrp")], calls)
        self.assertTrue(any("Dépendances installées en plus (2) : product, stock" in line for line in job.lines))

        with mock.patch.object(web, "ensure_enterprise_module_links", return_value=set()), \
                mock.patch.object(web, "module_dirs", return_value=iter([mrp])), \
                mock.patch.object(web, "installed_modules", return_value={}), \
                mock.patch.object(web, "module_command_job") as command:
            with self.assertRaisesRegex(RuntimeError, r"stock \(requis par mrp\)"):
                web.install_socle_job(DummyJob(), self.project, "test_db", "manufacturing")
        command.assert_not_called()


def graph_entry(depends=(), auto_install=False, **extra):
    manifest = {"name": extra.pop("title", ""), "depends": list(depends), "auto_install": auto_install, **extra}
    return web.manifest_graph_entry(manifest)


class ModuleInstallPlanTests(unittest.TestCase):
    def test_plan_resolves_recursive_dependencies_and_skips_installed_modules(self):
        graph = {
            "mrp": graph_entry(["stock", "resource"]),
            "stock": graph_entry(["product"], application=True, title="Inventaire"),
            "product": graph_entry(["mail"]),
            "resource": graph_entry(["base"]),
            "mail": graph_entry(["base"]),
        }

        plan = web.module_install_plan(graph, {"mail": {"state": "installed"}}, ["mrp"])

        self.assertEqual(["mrp"], plan["requested"])
        self.assertEqual(["product", "resource", "stock"], [item["name"] for item in plan["dependencies"]])
        self.assertEqual([{"name": "stock", "title": "Inventaire"}], plan["applications"])
        self.assertEqual(4, plan["total"])
        self.assertEqual([], plan["missing"])

    def test_plan_follows_odoo_auto_install_rules(self):
        graph = {
            "sale": graph_entry(),
            "stock": graph_entry(),
            "account": graph_entry(),
            # Toutes les dépendances requises, dont une nouvelle : installé.
            "sale_stock": graph_entry(["sale", "stock"], auto_install=True),
            # Liste explicite de déclencheurs : les autres dépendances suivent.
            "sale_pdf": graph_entry(["sale", "account"], auto_install=["sale"]),
            # Déclencheurs déjà tous installés, aucun nouveau : ignoré, comme dans Odoo.
            "account_extra": graph_entry(["account"], auto_install=True),
            # Dépend du pays des sociétés : non prévisible, donc ignoré.
            "l10n_extra": graph_entry(["sale"], auto_install=True, countries=["fr"]),
            "stock_only": graph_entry(["stock", "purchase"], auto_install=True),
        }

        plan = web.module_install_plan(graph, {"account": {"state": "installed"}, "stock": {"state": "installed"}}, ["sale"])

        self.assertEqual(["sale_pdf", "sale_stock"], [item["name"] for item in plan["auto_installed"]])
        self.assertEqual([], plan["dependencies"])
        self.assertEqual(3, plan["total"])

    def test_plan_reports_missing_and_uninstallable_dependencies(self):
        graph = {
            "helpdesk": graph_entry(["mail", "legacy"]),
            "legacy": graph_entry(installable=False),
        }

        plan = web.module_install_plan(graph, {}, ["helpdesk", "base"])

        self.assertEqual([{"name": "mail", "required_by": "helpdesk"}], plan["missing"])
        self.assertEqual([{"name": "legacy", "required_by": "helpdesk"}], plan["uninstallable"])
        self.assertEqual(["base"], plan["already_installed"])

    def test_manifest_is_read_like_odoo_and_ignores_invalid_files(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "demo"
            module.mkdir()
            (module / "__manifest__.py").write_text(
                "# -*- coding: utf-8 -*-\n{\n    'name': 'Demo',\n    'depends': ['sale'],  # comment\n}\n",
                encoding="utf-8",
            )
            broken = Path(directory) / "broken"
            broken.mkdir()
            (broken / "__manifest__.py").write_text("{'name': open('x')}\n", encoding="utf-8")

            graph = web.module_graph_from_paths([module, broken])

        self.assertEqual(["sale"], graph["demo"]["depends"])
        self.assertEqual("Demo", graph["demo"]["title"])
        self.assertEqual([], graph["broken"]["depends"])

    def test_socle_catalog_matches_odoo_app_page_and_ships_icons(self):
        icons = Path(web.__file__).resolve().parent / "odoo-manager-next" / "public" / "odoo-apps"
        section_ids = {section_id for section_id, _label in web.SOCLE_SECTIONS}
        app_ids = [app_id for app_id, _label, _section, _modules in web.SOCLE_APPS]

        self.assertEqual(49, len(app_ids))
        self.assertEqual(len(app_ids), len(set(app_ids)))
        for app_id, _label, section, modules in web.SOCLE_APPS:
            self.assertIn(section, section_ids)
            self.assertTrue(modules)
            self.assertTrue((icons / f"{app_id}.svg").is_file(), app_id)
