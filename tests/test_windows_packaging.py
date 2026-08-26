import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TAURI_ROOT = ROOT / "odoo-manager-next" / "src-tauri"


class WindowsPackagingTests(unittest.TestCase):
    def test_nsis_upgrade_hook_stops_locked_application_processes(self):
        config = json.loads((TAURI_ROOT / "tauri.windows.conf.json").read_text())
        relative_hook = config["bundle"]["windows"]["nsis"]["installerHooks"]
        hook_path = TAURI_ROOT / relative_hook

        self.assertTrue(hook_path.is_file())
        hook = hook_path.read_text()
        self.assertIn("NSIS_HOOK_PREINSTALL", hook)
        self.assertIn("NSIS_HOOK_PREUNINSTALL", hook)
        self.assertIn("odoo-manager-backend.exe", hook)
        self.assertIn("Odoo Manager.exe", hook)


if __name__ == "__main__":
    unittest.main()
