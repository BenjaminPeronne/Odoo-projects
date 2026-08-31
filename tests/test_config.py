import tempfile
import unittest
from pathlib import Path
from unittest import mock

from odoo_manager_core.config import ManagerSettings, SettingsStore, default_config_dir, expand_home_reference


class SettingsTests(unittest.TestCase):
    def test_home_reference_is_expanded_for_legacy_traefik_setting(self):
        self.assertEqual(
            expand_home_reference(r"$HOME\docker-local-tools\traefik", home="C:/Users/Demo"),
            str(Path("C:/Users/Demo") / "docker-local-tools" / "traefik"),
        )

    def test_platform_config_directories(self):
        home = Path("/home/test")
        self.assertEqual(
            default_config_dir("Darwin", {}, home),
            home / "Library" / "Application Support" / "Odoo Manager",
        )
        self.assertEqual(
            default_config_dir("Linux", {"XDG_CONFIG_HOME": "/config"}, home),
            Path("/config/odoo-manager"),
        )
        self.assertEqual(
            default_config_dir("Windows", {"APPDATA": "C:/Users/test/AppData/Roaming"}, home),
            Path("C:/Users/test/AppData/Roaming/Odoo Manager"),
        )

    @mock.patch("odoo_manager_core.config.platform.system", return_value="Linux")
    def test_store_round_trip_and_validation(self, _system):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            config_file = root / "config.json"
            store = SettingsStore(workspace, config_file)
            settings = store.update(
                {
                    "workspace": str(workspace),
                    "execution_mode": "wsl",
                    "wsl_distribution": "Ubuntu",
                    "docker_poll_interval": 1,
                    "onboarding_completed": True,
                },
                create_workspace=True,
            )
            loaded = store.load()
            self.assertEqual(settings, loaded)
            self.assertEqual(loaded.execution_mode, "wsl")
            self.assertEqual(loaded.docker_poll_interval, 3)
            self.assertFalse(loaded.start_project_before_open)
            self.assertTrue(loaded.onboarding_completed)

    def test_open_odoo_does_not_start_project_by_default(self):
        settings = ManagerSettings.from_dict({}, "/tmp/workspace")

        self.assertFalse(settings.start_project_before_open)

    def test_open_odoo_start_preference_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            store = SettingsStore(workspace, root / "config.json")

            store.update(
                {"start_project_before_open": True},
                create_workspace=True,
            )

            self.assertTrue(store.load().start_project_before_open)

    def test_invalid_mode_uses_native(self):
        settings = ManagerSettings.from_dict({"execution_mode": "dos"}, "/tmp/workspace")
        self.assertEqual(settings.execution_mode, "native")

    @mock.patch("odoo_manager_core.config.platform.system", return_value="Windows")
    def test_windows_legacy_wsl_mode_is_migrated_to_automatic_native(self, _system):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = SettingsStore(root / "workspace", root / "config.json").update(
                {"execution_mode": "wsl", "wsl_distribution": "Ubuntu"},
                create_workspace=True,
            )

        self.assertEqual(settings.execution_mode, "native")
        self.assertEqual(settings.wsl_distribution, "")


if __name__ == "__main__":
    unittest.main()
