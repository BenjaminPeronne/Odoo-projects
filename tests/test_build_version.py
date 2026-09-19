import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "set_app_version.py"
SPEC = importlib.util.spec_from_file_location("set_app_version", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class AppVersionTests(unittest.TestCase):
    def test_synchronizes_all_application_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frontend = root / "odoo-manager-next"
            tauri = frontend / "src-tauri"
            tauri.mkdir(parents=True)
            (frontend / "package.json").write_text('{"version":"0.1.1"}\n', encoding="utf-8")
            (frontend / "package-lock.json").write_text(
                '{"version":"0.1.1","packages":{"":{"version":"0.1.1"}}}\n',
                encoding="utf-8",
            )
            (tauri / "tauri.conf.json").write_text('{"version":"0.1.1"}\n', encoding="utf-8")
            (tauri / "Cargo.toml").write_text(
                '[package]\nname = "odoo-manager"\nversion = "0.1.1"\n',
                encoding="utf-8",
            )
            (tauri / "Cargo.lock").write_text(
                '[[package]]\nname = "odoo-manager"\nversion = "0.1.1"\n',
                encoding="utf-8",
            )

            MODULE.set_app_version(root, "0.1.2")

            self.assertEqual(json.loads((frontend / "package.json").read_text())["version"], "0.1.2")
            package_lock = json.loads((frontend / "package-lock.json").read_text())
            self.assertEqual(package_lock["version"], "0.1.2")
            self.assertEqual(package_lock["packages"][""]["version"], "0.1.2")
            self.assertIn('version = "0.1.2"', (tauri / "Cargo.toml").read_text())
            self.assertIn('version = "0.1.2"', (tauri / "Cargo.lock").read_text())

    def test_rejects_non_semantic_versions(self):
        with self.assertRaises(ValueError):
            MODULE.set_app_version(Path("/tmp/not-used"), "0.1.2-build1")


if __name__ == "__main__":
    unittest.main()


BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_desktop.py"
BUILD_SPEC = importlib.util.spec_from_file_location("build_desktop", BUILD_SCRIPT)
BUILD_MODULE = importlib.util.module_from_spec(BUILD_SPEC)
assert BUILD_SPEC.loader
BUILD_SPEC.loader.exec_module(BUILD_MODULE)


class InstallerSmokeTestGuardTests(unittest.TestCase):
    """Le test installe le même appId et ferme l'application : il casserait un poste de développement."""

    def test_runs_on_a_windows_ci_runner(self):
        self.assertTrue(BUILD_MODULE.installer_smoke_test_allowed("Windows", {"GITHUB_ACTIONS": "true"}))

    def test_is_skipped_on_a_developer_machine(self):
        self.assertFalse(BUILD_MODULE.installer_smoke_test_allowed("Windows", {}))

    def test_can_be_forced_on_a_disposable_machine(self):
        self.assertTrue(BUILD_MODULE.installer_smoke_test_allowed("Windows", {}, forced=True))

    def test_never_runs_outside_windows(self):
        self.assertFalse(BUILD_MODULE.installer_smoke_test_allowed("Linux", {"GITHUB_ACTIONS": "true"}, forced=True))
