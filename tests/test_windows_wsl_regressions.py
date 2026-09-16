"""Non-régressions de la recette Windows + WSL (septembre 2026).

Sous Windows, le workspace vit dans \\\\wsl.localhost et les liens de odoo/addons
sont créés par WSL. Windows ne les traverse pas (WinError 1920) et ne les voit
même pas (`exists()` et `is_symlink()` renvoient False). Ces tests simulent cet
aveuglement sur un système POSIX et exécutent les scripts WSL avec `sh`.
"""
import ast
import os
import subprocess
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from odoo_manager_core.config import ManagerSettings
from odoo_manager_core.project_service import ProjectService
import test_module_layout
from test_module_layout import DummyJob
import odoo_manager_web as web

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_SOURCES = (
    ROOT / "odoo_manager_web.py",
    *sorted((ROOT / "odoo_manager_core").glob("*.py")),
)
# Octet 0x9d : indéfini en cp1252, c'est lui qui cassait l'installation de module.
NON_CP1252_OUTPUT = "import sys; sys.stdout.buffer.write(b'avant \\x9d\\xff caf\\xc3\\xa9 \\xe2\\x80\\x9d\\n')"


class SubprocessDecodingTests(unittest.TestCase):
    def test_every_text_subprocess_decodes_utf8_without_failing(self):
        # Sans encoding explicite, Windows décode en cp1252 et lève UnicodeDecodeError
        # au premier caractère Odoo non représentable.
        offenders = []
        for source in PRODUCT_SOURCES:
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
                text = keywords.get("text") or keywords.get("universal_newlines")
                if not (isinstance(text, ast.Constant) and text.value is True):
                    continue
                encoding = keywords.get("encoding")
                errors = keywords.get("errors")
                if not (isinstance(encoding, ast.Constant) and encoding.value == "utf-8"
                        and isinstance(errors, ast.Constant) and errors.value == "replace"):
                    offenders.append(f"{source.name}:{node.lineno}")
        self.assertEqual([], offenders)

    def service(self):
        return ProjectService(ManagerSettings(workspace=str(ROOT)), ROOT)

    def test_stream_survives_bytes_undefined_in_cp1252_and_invalid_utf8(self):
        lines = []
        code = self.service().stream([sys.executable, "-c", NON_CP1252_OUTPUT], cwd=ROOT, log=lines.append)
        self.assertEqual(0, code)
        output = "".join(lines)
        self.assertIn("café", output)
        self.assertIn("”", output)
        self.assertIn("�", output)

    def test_capture_survives_undecodable_output(self):
        code, output = self.service().capture([sys.executable, "-c", NON_CP1252_OUTPUT], cwd=ROOT)
        self.assertEqual(0, code)
        self.assertIn("café", output)

    def test_web_run_capture_and_run_stream_survive_undecodable_output(self):
        code, output = web.run_capture([sys.executable, "-c", NON_CP1252_OUTPUT], cwd=ROOT)
        self.assertEqual(0, code)
        self.assertIn("café", output)
        job = DummyJob()
        job.status = "running"
        self.assertEqual(0, web.run_stream(job, [sys.executable, "-c", NON_CP1252_OUTPUT], cwd=ROOT))
        self.assertTrue(any("café" in line for line in job.lines))

    def test_stream_kills_the_command_when_reading_its_output_fails(self):
        started = time.monotonic()
        script = "import time; print('first', flush=True); time.sleep(30)"

        def failing_log(line):
            if line.startswith("first"):
                raise UnicodeDecodeError("charmap", b"\x9d", 0, 1, "character maps to <undefined>")

        with self.assertRaises(UnicodeDecodeError):
            self.service().stream([sys.executable, "-c", script], cwd=ROOT, log=failing_log)
        self.assertLess(time.monotonic() - started, 10)


class ModuleCommandFailureTests(unittest.TestCase):
    def test_restart_failure_does_not_hide_the_module_command_error(self):
        # Recette : l'UnicodeDecodeError était remplacée par « Le processus Odoo s'est
        # arrêté avant d'ouvrir le port 8069 » levé par le redémarrage dans le finally.
        service = ProjectService(ManagerSettings(workspace=str(ROOT)), ROOT)
        logs = []
        original = UnicodeDecodeError("charmap", b"\x9d", 0, 1, "character maps to <undefined>")
        with mock.patch.object(service, "ensure_odoo_containers_ready"), \
                mock.patch.object(service, "install_project_pip_requirements"), \
                mock.patch.object(service, "database_is_neutralized", return_value=False), \
                mock.patch.object(service, "stop_odoo_server"), \
                mock.patch.object(service, "stream", side_effect=original), \
                mock.patch.object(service, "start_odoo_server", side_effect=RuntimeError("port 8069")) as start, \
                mock.patch.object(service, "wait_project_http") as wait_http:
            with self.assertRaises(UnicodeDecodeError):
                service.run_odoo_module_command("DEMO", "demo", "dromcom_mrp", option="-i", log=logs.append)
        start.assert_called_once()
        wait_http.assert_not_called()
        self.assertTrue(any("Redémarrage du serveur Odoo impossible : port 8069" in line for line in logs))

    def test_successful_module_command_still_restarts_and_waits_for_odoo(self):
        service = ProjectService(ManagerSettings(workspace=str(ROOT)), ROOT)
        with mock.patch.object(service, "ensure_odoo_containers_ready"), \
                mock.patch.object(service, "install_project_pip_requirements"), \
                mock.patch.object(service, "database_is_neutralized", return_value=False), \
                mock.patch.object(service, "stop_odoo_server"), \
                mock.patch.object(service, "stream", return_value=0), \
                mock.patch.object(service, "start_odoo_server") as start, \
                mock.patch.object(service, "wait_project_http") as wait_http:
            service.run_odoo_module_command("DEMO", "demo", "dromcom_mrp", option="-i", log=lambda _line: None)
        start.assert_called_once()
        wait_http.assert_called_once()


@unittest.skipIf(os.name == "nt", "requires POSIX sh and symlink semantics provided by WSL")
class WindowsWslLayoutTests(unittest.TestCase):
    """Windows simulé : les scripts WSL tournent avec sh, les chemins sont identiques."""

    def setUp(self):
        # Réutilise le projet temporaire sans hériter (et relancer) les tests de ModuleLayoutTests.
        test_module_layout.ModuleLayoutTests.setUp(self)
        self.addCleanup(test_module_layout.ModuleLayoutTests.tearDown, self)
        context = mock.Mock(distribution="Ubuntu")
        patches = (
            mock.patch.object(web, "platform_id", return_value="windows"),
            mock.patch.object(web, "active_workspace_wsl_context", return_value=context),
            mock.patch.object(web, "wsl_execution_path", side_effect=lambda path, distribution: str(path)),
            mock.patch.object(web, "wsl_windows_path", side_effect=lambda path, distribution="": str(path)),
            mock.patch.object(web, "wsl_command_prefix", return_value=[]),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        web.clear_project_module_cache(self.project)

    def add_module(self, relative, manifest):
        module = self.project_root / "odoo" / relative
        module.mkdir(parents=True, exist_ok=True)
        (module / "__manifest__.py").write_bytes(manifest.encode("utf-8"))
        return module

    def windows_blind_to_wsl_links(self):
        """Ce que voit Python sous Windows face à un lien créé par WSL."""
        def unreadable(*args, **kwargs):
            raise AssertionError("lecture Windows d'un lien WSL")
        return (
            mock.patch.object(Path, "is_symlink", return_value=False),
            mock.patch.object(Path, "symlink_to", side_effect=FileExistsError(183, "WinError 183")),
            mock.patch.object(web, "read_manifest_dict", side_effect=unreadable),
        )

    def test_socle_dependencies_are_read_through_wsl_links(self):
        # Manifeste Enterprise lié depuis odoo/addons, avec un guillemet typographique
        # (octets e2 80 9d) : dépendances vides dans la recette Windows.
        self.add_module("addons-store/odoo_entreprise/sale_subscription", (
            "{'name': 'Abonnements “pro”', 'depends': ['sale_management', 'payment'], "
            "'application': True}\n"
        ))
        self.add_module("addons-store/sale_management", "{'name': 'Sales', 'depends': ['sale']}\n")
        self.add_module("addons-store/sale", "{'name': 'Sale', 'depends': ['mail']}\n")
        self.add_module("addons-store/payment", "{'name': 'Payment'}\n")
        self.add_module("addons-store/mail", "{'name': 'Mail'}\n")
        self.add_module("addons-store/sale_payment_bridge", (
            "{'name': 'Bridge', 'depends': ['sale', 'payment'], 'auto_install': True}\n"
        ))
        link = self.project_root / "odoo" / "addons" / "sale_subscription"
        link.symlink_to("../addons-store/odoo_entreprise/sale_subscription")

        blind_links, blind_create, blind_manifests = self.windows_blind_to_wsl_links()
        with blind_links, blind_create, blind_manifests:
            graph = web.project_module_graph(self.project)
            plan = web.module_install_plan(graph, {}, ["sale_subscription"])

        self.assertEqual(["payment", "sale_management"], graph["sale_subscription"]["depends"])
        self.assertIn("“pro”", graph["sale_subscription"]["title"])
        self.assertEqual(
            ["mail", "payment", "sale", "sale_management"],
            [item["name"] for item in plan["dependencies"]],
        )
        self.assertEqual(["sale_payment_bridge"], [item["name"] for item in plan["auto_installed"]])
        self.assertEqual([], plan["missing"])

    def test_socle_catalog_counts_extra_modules_on_windows(self):
        self.add_module("addons-store/crm", "{'name': 'CRM', 'depends': ['mail', 'calendar']}\n")
        self.add_module("addons-store/mail", "{'name': 'Mail'}\n")
        self.add_module("addons-store/calendar", "{'name': 'Calendar', 'depends': ['mail']}\n")
        (self.project_root / "odoo" / "addons" / "crm").symlink_to("../addons-store/crm")

        blind_links, blind_create, blind_manifests = self.windows_blind_to_wsl_links()
        with blind_links, blind_create, blind_manifests, \
                mock.patch.object(web, "installed_modules", return_value={}):
            catalog = web.socle_catalog(self.project, "demo")

        entry = next(app for app in catalog["apps"] if app["id"] == "crm")
        self.assertEqual([], entry["missing"])
        self.assertEqual(2, entry["extra_count"])

    def test_first_manifest_wins_like_the_module_listing(self):
        self.add_module("addons/dup", "{'name': 'Linked', 'depends': ['first']}\n")
        self.add_module("addons-store/dup", "{'name': 'Stored', 'depends': ['second']}\n")
        graph = web.wsl_module_graph(self.project)
        self.assertEqual(["first"], graph["dup"]["depends"])

    def test_wsl_manifest_read_failure_is_reported(self):
        with mock.patch.object(web, "run_capture", return_value=(124, "partial")):
            with self.assertRaisesRegex(RuntimeError, "délai dépassé"):
                web.project_module_graph(self.project)

    def test_repository_link_is_created_by_wsl_and_found_again(self):
        # Recette : WinError 183 sur chaque import Git, le lien existant étant invisible.
        storage = self.add_module("addons-store/dromcom_mrp", "{'name': 'Dromcom MRP'}\n")
        link = self.project_root / "odoo" / "addons" / "dromcom_mrp"

        blind_links, blind_create, blind_manifests = self.windows_blind_to_wsl_links()
        with blind_links, blind_create, blind_manifests:
            self.assertTrue(web.ensure_relative_module_link(DummyJob(), self.project, "dromcom_mrp", storage))
            job = DummyJob()
            self.assertFalse(web.ensure_relative_module_link(job, self.project, "dromcom_mrp", storage))
            self.assertTrue(web.managed_storage_link(self.project, "dromcom_mrp", storage))

        self.assertEqual("../addons-store/dromcom_mrp", os.readlink(link))
        self.assertTrue(any("Déjà lié en relatif" in line for line in job.lines))

    def test_absolute_wsl_link_is_converted_to_relative(self):
        storage = self.add_module("addons-store/dromcom_mrp", "{'name': 'Dromcom MRP'}\n")
        link = self.project_root / "odoo" / "addons" / "dromcom_mrp"
        link.symlink_to(storage)
        blind_links, blind_create, blind_manifests = self.windows_blind_to_wsl_links()
        with blind_links, blind_create, blind_manifests:
            self.assertTrue(web.ensure_relative_module_link(DummyJob(), self.project, "dromcom_mrp", storage))
        self.assertEqual("../addons-store/dromcom_mrp", os.readlink(link))

    def test_invisible_foreign_link_is_reported_instead_of_winerror_183(self):
        storage = self.add_module("addons-store/dromcom_transitaire", "{'name': 'Transitaire'}\n")
        other = self.add_module("addons-store/elsewhere/dromcom_transitaire", "{'name': 'Other'}\n")
        (self.project_root / "odoo" / "addons" / "dromcom_transitaire").symlink_to(other)
        blind_links, blind_create, blind_manifests = self.windows_blind_to_wsl_links()
        with blind_links, blind_create, blind_manifests:
            with self.assertRaisesRegex(RuntimeError, "existe déjà dans le projet"):
                web.ensure_relative_module_link(DummyJob(), self.project, "dromcom_transitaire", storage)

    def test_repository_add_refuses_an_invisible_existing_link(self):
        link = self.project_root / "odoo" / "addons" / "dromcom_mrp"
        link.symlink_to(self.external)
        with mock.patch.object(Path, "is_symlink", return_value=False):
            self.assertEqual("different", web.addon_link_status(link, self.project_root / "odoo" / "addons-store" / "dromcom_mrp")[0])

    def test_wsl_removal_deletes_the_link_but_never_its_target(self):
        storage = self.add_module("addons-store/dromcom_mrp", "{'name': 'Dromcom MRP'}\n")
        link = self.project_root / "odoo" / "addons" / "dromcom_mrp"
        link.symlink_to("../addons-store/dromcom_mrp")
        web.remove_module_entry(link)
        self.assertFalse(os.path.lexists(link))
        self.assertTrue((storage / "__manifest__.py").is_file())

    def test_wsl_move_keeps_the_link_itself(self):
        storage = self.add_module("addons-store/dromcom_mrp", "{'name': 'Dromcom MRP'}\n")
        link = self.project_root / "odoo" / "addons" / "dromcom_mrp"
        link.symlink_to(storage)
        backup = self.root / "backup_dromcom_mrp"
        web.move_module_entry(link, backup)
        self.assertTrue(backup.is_symlink())
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(storage.is_dir())


class EnterpriseProvidedByRepositoryTests(unittest.TestCase):
    def setUp(self):
        test_module_layout.ModuleLayoutTests.setUp(self)
        self.addCleanup(test_module_layout.ModuleLayoutTests.tearDown, self)

    def enterprise(self, relative, name):
        module = self.project_root / "odoo" / "addons-store" / relative / name
        module.mkdir(parents=True, exist_ok=True)
        (module / "__manifest__.py").write_text("{'name': 'Enterprise'}\n", encoding="utf-8")
        return module

    def test_links_to_a_repository_enterprise_copy_do_not_block_the_socle(self):
        # Recette CARITEL_18 : odoo/addons/account_accountant pointe vers
        # addons-store/caritel_v18/addons-store/odoo_entreprise, pas vers addons-store/odoo_entreprise.
        self.enterprise("odoo_entreprise", "account_accountant")
        self.enterprise("odoo_entreprise", "helpdesk")
        repository_copy = self.enterprise("caritel_v18/addons-store/odoo_entreprise", "account_accountant")
        link = self.project_root / "odoo" / "addons" / "account_accountant"
        link.symlink_to(os.path.relpath(repository_copy, link.parent))
        job = DummyJob()

        available = web.ensure_enterprise_module_links(job, self.project)

        self.assertEqual({"account_accountant", "helpdesk"}, available)
        self.assertEqual(repository_copy.resolve(), link.resolve())
        self.assertTrue((self.project_root / "odoo" / "addons" / "helpdesk").is_symlink())
        self.assertTrue(any("déjà fourni" in line and "account_accountant" in line for line in job.lines))

    def test_broken_link_still_blocks_enterprise_links(self):
        self.enterprise("odoo_entreprise", "account_accountant")
        (self.project_root / "odoo" / "addons" / "account_accountant").symlink_to("../missing")
        with self.assertRaisesRegex(RuntimeError, "liens cassés"):
            web.ensure_enterprise_module_links(DummyJob(), self.project)


class RikaErrorMessageTests(unittest.TestCase):
    def download(self, failing_url_part):
        from odoo_manager_core.project_creator import ProjectCreator

        class Opener:
            def open(self, request, timeout=None):
                url = getattr(request, "full_url", request)
                if failing_url_part in url:
                    raise urllib.error.HTTPError(url, 404, "File not found", {}, None)
                return mock.MagicMock()

        class SessionCookie:
            name = "sessionId"
            value = "token"

        # Sous Windows, le constructeur cherche git dans WSL avec les réglages : un Mock n'y a pas sa place.
        with mock.patch("odoo_manager_core.project_creator.platform_id", return_value="linux"):
            creator = ProjectCreator(mock.Mock(), ROOT, mock.Mock())
        with mock.patch("odoo_manager_core.project_creator.CookieJar", return_value=[SessionCookie()]), \
                mock.patch("urllib.request.build_opener", return_value=Opener()):
            return creator.download_rika_project("dev06", "login", "secret", ROOT)

    def test_unknown_instance_explains_the_expected_name(self):
        with self.assertRaisesRegex(RuntimeError, "introuvable.*nom exact"):
            self.download("?action=zip")

    def test_missing_archive_after_generation_is_not_reported_as_unknown_instance(self):
        with self.assertRaisesRegex(RuntimeError, "archive dev06.zip est introuvable"):
            self.download("dev06.zip")


if __name__ == "__main__":
    unittest.main()
