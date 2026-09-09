import tempfile
import unittest
from pathlib import Path
from unittest import mock

from odoo_manager_core.config import DEFAULT_API_PORT, ManagerSettings, SettingsStore, default_config_dir, expand_home_reference


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
                    "api_port": 19876,
                    "show_technical_details": True,
                    "interface_icon": "local",
                    "bases_layout": "compact",
                    "modules_layout": "classic",
                    "onboarding_completed": True,
                },
                create_workspace=True,
            )
            loaded = store.load()
            self.assertEqual(settings, loaded)
            self.assertEqual(loaded.execution_mode, "wsl")
            self.assertEqual(loaded.docker_poll_interval, 3)
            self.assertEqual(loaded.api_port, 19876)
            self.assertFalse(loaded.start_project_before_open)
            self.assertTrue(loaded.show_technical_details)
            self.assertEqual(loaded.interface_icon, "local")
            self.assertEqual(loaded.bases_layout, "compact")
            self.assertEqual(loaded.modules_layout, "classic")
            self.assertTrue(loaded.onboarding_completed)

    def test_open_odoo_does_not_start_project_by_default(self):
        settings = ManagerSettings.from_dict({}, "/tmp/workspace")

        self.assertFalse(settings.start_project_before_open)
        self.assertFalse(settings.show_technical_details)

    def test_layout_defaults_and_invalid_values_use_classic(self):
        for payload in ({}, {"bases_layout": "unknown", "modules_layout": None}):
            settings = ManagerSettings.from_dict(payload, "/tmp/workspace")
            self.assertEqual(settings.bases_layout, "classic")
            self.assertEqual(settings.modules_layout, "classic")

    def test_interface_icon_defaults_to_manager_for_unknown_value(self):
        settings = ManagerSettings.from_dict({"interface_icon": "unknown"}, "/tmp/workspace")

        self.assertEqual(settings.interface_icon, "manager")

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

    def test_api_port_defaults_and_rejects_invalid_values(self):
        self.assertEqual(ManagerSettings.from_dict({}, "/tmp/workspace").api_port, DEFAULT_API_PORT)
        self.assertEqual(
            ManagerSettings.from_dict({"api_port": 80}, "/tmp/workspace").api_port,
            DEFAULT_API_PORT,
        )
        self.assertEqual(
            ManagerSettings.from_dict({"api_port": "invalid"}, "/tmp/workspace").api_port,
            DEFAULT_API_PORT,
        )

    def test_wsl_unc_workspace_is_preserved_in_canonical_form(self):
        settings = ManagerSettings.from_dict(
            {},
            r"\\wsl$\Ubuntu\home\demo\Odoo-projects",
        )

        self.assertEqual(
            settings.workspace,
            r"\\wsl.localhost\Ubuntu\home\demo\Odoo-projects",
        )

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
