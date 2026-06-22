import tempfile
import unittest
from pathlib import Path

from odoo_manager_core.config import ManagerSettings, SettingsStore, default_config_dir


class SettingsTests(unittest.TestCase):
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

    def test_store_round_trip_and_validation(self):
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
                },
                create_workspace=True,
            )
            loaded = store.load()
            self.assertEqual(settings, loaded)
            self.assertEqual(loaded.execution_mode, "wsl")
            self.assertEqual(loaded.docker_poll_interval, 3)

    def test_invalid_mode_uses_native(self):
        settings = ManagerSettings.from_dict({"execution_mode": "dos"}, "/tmp/workspace")
        self.assertEqual(settings.execution_mode, "native")


if __name__ == "__main__":
    unittest.main()
