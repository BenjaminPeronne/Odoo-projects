"""Liens d'addons hérités de WSL sous Windows : détection, décision et conversion."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from odoo_manager_core import windows_links
from odoo_manager_core.windows_links import (
    IO_REPARSE_TAG_LX_SYMLINK,
    MIGRATION_JOURNAL_NAME,
    convert_wsl_symlinks,
    native_link_value,
    parse_lx_symlink_reparse_data,
    read_migration_journal,
)
import test_module_layout
from test_module_layout import DummyJob
import odoo_manager_web as web


def lx_reparse_buffer(target, version=2, tag=IO_REPARSE_TAG_LX_SYMLINK):
    data = version.to_bytes(4, "little") + target.encode("utf-8")
    return tag.to_bytes(4, "little") + len(data).to_bytes(2, "little") + b"\0\0" + data


def can_create_symlinks():
    with tempfile.TemporaryDirectory() as directory:
        try:
            os.symlink(".", Path(directory) / "probe", target_is_directory=True)
        except OSError:
            return False
    return True


class ReparseDataTests(unittest.TestCase):
    def test_reads_the_linux_target_of_a_wsl_link(self):
        raw = lx_reparse_buffer("../addons-store/odoo_entreprise/account_accountant")
        self.assertEqual("../addons-store/odoo_entreprise/account_accountant", parse_lx_symlink_reparse_data(raw))

    def test_rejects_other_reparse_points_and_truncated_data(self):
        with self.assertRaisesRegex(ValueError, "inattendu"):
            parse_lx_symlink_reparse_data(lx_reparse_buffer("../x", tag=0xA000000C))
        with self.assertRaisesRegex(ValueError, "tronquées"):
            parse_lx_symlink_reparse_data(lx_reparse_buffer("../x")[:-1])
        with self.assertRaisesRegex(ValueError, "Version"):
            parse_lx_symlink_reparse_data(lx_reparse_buffer("../x", version=1))

    def test_only_relative_targets_become_windows_links(self):
        self.assertEqual(str(Path("..", "addons-store", "sale")), native_link_value("../addons-store/./sale"))
        for target in ("/mnt/c/Odoo/sale", "", "..\\sale", "C:/Odoo/sale", "a\0b"):
            self.assertIsNone(native_link_value(target), target)


@unittest.skipUnless(can_create_symlinks(), "creating symlinks requires Developer Mode on Windows")
class ConversionTests(unittest.TestCase):
    """Les liens WSL sont simulés par des fichiers ordinaires dont on fournit la cible."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addons = self.root / "addons"
        self.addons.mkdir()
        self.targets = {}
        for name in ("account", "sale", "stock"):
            (self.root / "addons-store" / name).mkdir(parents=True)
            (self.root / "addons-store" / name / "__manifest__.py").write_text("{}\n", encoding="utf-8")
            self.add_wsl_link(name, f"../addons-store/{name}")

    def add_wsl_link(self, name, target):
        (self.addons / name).write_text("lien WSL simulé", encoding="utf-8")
        self.targets[name] = target

    def simulated_wsl(self):
        def remaining_wsl_links(directory):
            return sorted(path for path in Path(directory).iterdir() if path.is_file() and path.name in self.targets)

        def tag(path):
            path = Path(path)
            if path.is_symlink():
                return windows_links.IO_REPARSE_TAG_SYMLINK
            return IO_REPARSE_TAG_LX_SYMLINK if path.is_file() and path.name in self.targets else 0

        return (
            mock.patch.object(windows_links, "wsl_symlinks", side_effect=remaining_wsl_links),
            mock.patch.object(windows_links, "read_wsl_symlink", side_effect=lambda path: self.targets[Path(path).name]),
            mock.patch.object(windows_links, "reparse_tag", side_effect=tag),
        )

    def test_links_become_relative_links_readable_by_windows(self):
        lines = []
        wsl_links, targets, tags = self.simulated_wsl()
        with wsl_links, targets, tags:
            result = convert_wsl_symlinks(self.addons, log=lines.append)

        self.assertEqual({"converted": 3, "skipped": [], "failures": []}, result)
        for name in self.targets:
            link = self.addons / name
            self.assertTrue(link.is_symlink())
            self.assertEqual(Path("..", "addons-store", name), Path(os.readlink(link)))
            self.assertTrue((link / "__manifest__.py").is_file())
        self.assertFalse((self.addons / MIGRATION_JOURNAL_NAME).exists())
        self.assertEqual(["Conversion des liens : 3/3"], lines)

    def test_interrupted_conversion_resumes_from_its_journal(self):
        real_symlink = os.symlink

        def symlink_failing_for_sale(value, path, target_is_directory=False):
            if Path(path).name == "sale":
                raise OSError("interruption simulée")
            return real_symlink(value, path, target_is_directory=target_is_directory)

        wsl_links, targets, tags = self.simulated_wsl()
        with wsl_links, targets, tags, mock.patch.object(windows_links.os, "symlink", side_effect=symlink_failing_for_sale):
            first = convert_wsl_symlinks(self.addons)
        self.assertEqual(2, first["converted"])
        self.assertEqual("sale", first["failures"][0][0])
        # Le lien WSL a déjà été retiré : seule sa cible notée dans le journal permet de le recréer.
        self.assertFalse(os.path.lexists(self.addons / "sale"))
        self.assertEqual("../addons-store/sale".replace("/", os.sep), read_migration_journal(self.addons)["sale"])

        wsl_links, targets, tags = self.simulated_wsl()
        with wsl_links, targets, tags:
            second = convert_wsl_symlinks(self.addons)
        self.assertEqual({"converted": 3, "skipped": [], "failures": []}, second)
        self.assertTrue((self.addons / "sale" / "__manifest__.py").is_file())
        self.assertFalse((self.addons / MIGRATION_JOURNAL_NAME).exists())

    def test_absolute_wsl_targets_are_left_untouched(self):
        self.add_wsl_link("external", "/home/demo/addons/external")
        wsl_links, targets, tags = self.simulated_wsl()
        with wsl_links, targets, tags:
            result = convert_wsl_symlinks(self.addons)
        self.assertEqual(3, result["converted"])
        self.assertEqual("external", result["skipped"][0][0])
        self.assertTrue((self.addons / "external").is_file())


@unittest.skipUnless(os.name == "nt", "WSL links only exist on Windows")
class RealWslLinkTests(unittest.TestCase):
    def test_reads_and_converts_a_link_created_by_wsl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "addons").mkdir()
            try:
                linux_root = subprocess.run(
                    ["wsl.exe", "--exec", "wslpath", "-a", "-u", str(root).replace("\\", "/")],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=True,
                ).stdout.strip()
                # Une cible absente au moment de `ln -s` force un lien WSL même avec le mode développeur.
                subprocess.run(
                    ["wsl.exe", "--cd", f"{linux_root}/addons", "--exec", "sh", "-c",
                     "ln -s ../store/mod_a mod_a && mkdir -p ../store/mod_a && touch ../store/mod_a/__manifest__.py"],
                    capture_output=True, timeout=30, check=True,
                )
            except (OSError, subprocess.SubprocessError):
                self.skipTest("aucune distribution WSL utilisable")
            link = root / "addons" / "mod_a"
            if not windows_links.is_wsl_symlink(link):
                self.skipTest("WSL a créé un lien natif")

            self.assertEqual("../store/mod_a", windows_links.read_wsl_symlink(link))
            if not windows_links.native_symlinks_supported(root / "addons"):
                self.skipTest("mode développeur désactivé")
            result = convert_wsl_symlinks(root / "addons")
            self.assertEqual(1, result["converted"])
            self.assertTrue((link / "__manifest__.py").is_file())


class BackendDecisionTests(unittest.TestCase):
    def setUp(self):
        test_module_layout.ModuleLayoutTests.setUp(self)
        self.addCleanup(test_module_layout.ModuleLayoutTests.tearDown, self)
        self.addons = self.project_root / "odoo" / "addons"

    def test_windows_lists_native_links_without_wsl(self):
        (self.project_root / "odoo" / "addons-store" / "sale").mkdir()
        (self.project_root / "odoo" / "addons-store" / "sale" / "__manifest__.py").write_text("{}\n", encoding="utf-8")
        with mock.patch.object(web, "platform_id", return_value="windows"), \
                mock.patch.object(web, "contains_wsl_symlink", return_value=False), \
                mock.patch.object(web, "wsl_shell_available", side_effect=AssertionError("sonde WSL")), \
                mock.patch.object(web, "wsl_module_dirs", side_effect=AssertionError("scan WSL")), \
                mock.patch.object(web, "wsl_module_graph", side_effect=AssertionError("graphe WSL")):
            self.assertEqual(["sale"], [path.name for path in web.module_dirs(self.project)])
            self.assertIn("sale", web.project_module_graph(self.project))

    def test_wsl_is_used_only_for_folders_windows_cannot_handle(self):
        with mock.patch.object(web, "platform_id", return_value="windows"), \
                mock.patch.object(web, "wsl_shell_available", return_value=True), \
                mock.patch.object(web, "native_symlinks_supported", return_value=True), \
                mock.patch.object(web, "contains_wsl_symlink", return_value=False):
            self.assertIsNone(web.addon_links_wsl_distribution(self.addons))
            self.assertIsNone(web.addon_links_wsl_distribution(self.addons, creating=True))
        with mock.patch.object(web, "platform_id", return_value="windows"), \
                mock.patch.object(web, "wsl_shell_available", return_value=True), \
                mock.patch.object(web, "contains_wsl_symlink", return_value=True):
            self.assertEqual("", web.addon_links_wsl_distribution(self.addons))
        with mock.patch.object(web, "platform_id", return_value="windows"), \
                mock.patch.object(web, "wsl_shell_available", return_value=True), \
                mock.patch.object(web, "native_symlinks_supported", return_value=False), \
                mock.patch.object(web, "contains_wsl_symlink", return_value=False):
            self.assertIsNone(web.addon_links_wsl_distribution(self.addons))
            self.assertEqual("", web.addon_links_wsl_distribution(self.addons, creating=True))

    def windows_with_legacy_links(self, **overrides):
        patches = {
            "platform_id": "windows",
            "contains_wsl_symlink": True,
            "native_symlinks_supported": True,
            "container_status": "exited",
        }
        patches.update(overrides)
        return [mock.patch.object(web, name, return_value=value) for name, value in patches.items()]

    def test_snapshot_counts_legacy_links_for_the_interface(self):
        patches = self.windows_with_legacy_links()
        patches.append(mock.patch.object(web, "wsl_symlinks", side_effect=lambda parent: [parent / "a", parent / "b"] if parent == self.addons else []))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        snapshot = web.addon_links_snapshot(self.project)
        self.assertTrue(snapshot["supported"])
        self.assertTrue(snapshot["native_symlinks"])
        self.assertFalse(snapshot["interrupted"])
        self.assertGreaterEqual(snapshot["wsl_links"], 2)

    def test_snapshot_is_not_offered_outside_windows(self):
        with mock.patch.object(web, "platform_id", return_value="macos"):
            snapshot = web.addon_links_snapshot(self.project)
        self.assertEqual({"supported": False, "wsl_links": 0, "interrupted": False, "native_symlinks": True}, snapshot)

    def run_conversion(self, **overrides):
        patches = self.windows_with_legacy_links(**overrides)
        for patcher in patches:
            patcher.start()
        try:
            job = DummyJob()
            with mock.patch.object(web, "convert_wsl_symlinks", return_value={"converted": 2, "skipped": [], "failures": []}) as convert:
                web.convert_wsl_addon_links_job(job, self.project)
            return job, convert
        finally:
            for patcher in patches:
                patcher.stop()

    def test_conversion_requires_a_stopped_project(self):
        with self.assertRaisesRegex(RuntimeError, "Arrête le projet"):
            self.run_conversion(container_status="running")

    def test_conversion_explains_developer_mode(self):
        with self.assertRaisesRegex(RuntimeError, "mode développeur"):
            self.run_conversion(native_symlinks_supported=False)

    def test_conversion_converts_every_legacy_folder_and_refreshes_modules(self):
        with mock.patch.object(web, "clear_project_module_cache") as clear_cache:
            job, convert = self.run_conversion()
        self.assertTrue(convert.called)
        clear_cache.assert_called_once_with(self.project)
        self.assertTrue(any("lue directement par Windows" in line for line in job.lines))


if __name__ == "__main__":
    unittest.main()
