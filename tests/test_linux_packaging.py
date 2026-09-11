import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "odoo-manager-next"


class LinuxPackagingTests(unittest.TestCase):
    def test_debian_package_has_required_project_homepage(self):
        package = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))

        self.assertEqual(
            package.get("homepage"),
            "https://github.com/BenjaminPeronne/Odoo-projects",
        )

    def test_desktop_name_matches_app_id_and_is_synchronized(self):
        package = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
        builder_config = (FRONTEND / "electron-builder.yml").read_text(encoding="utf-8")

        self.assertEqual(package.get("desktopName"), "com.sudokeys.odoo-manager.desktop")
        self.assertIn("appId: com.sudokeys.odoo-manager\n", builder_config)
        self.assertIn("  syncDesktopName: true\n", builder_config)


if __name__ == "__main__":
    unittest.main()
