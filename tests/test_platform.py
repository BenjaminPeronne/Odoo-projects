import unittest
from unittest import mock

from odoo_manager_core.config import ManagerSettings
from odoo_manager_core.platform import open_terminal_script


class TerminalLaunchTests(unittest.TestCase):
    @mock.patch("odoo_manager_core.platform.platform_id", return_value="windows")
    def test_windows_native_requires_wsl(self, _platform):
        settings = ManagerSettings.from_dict({"execution_mode": "native"}, "/tmp/workspace")
        result = open_terminal_script(settings, "/tmp/create_project.sh")
        self.assertFalse(result.ok)
        self.assertIn("WSL 2", result.message)

    @mock.patch("odoo_manager_core.platform.subprocess.Popen")
    @mock.patch("odoo_manager_core.platform.execution_path", return_value="/mnt/c/create_project.sh")
    @mock.patch("odoo_manager_core.platform.shutil.which")
    @mock.patch("odoo_manager_core.platform.platform_id", return_value="windows")
    def test_windows_wsl_uses_windows_terminal(self, _platform, which, _execution_path, popen):
        which.side_effect = lambda name: "C:/Windows/wt.exe" if name == "wt.exe" else None
        settings = ManagerSettings.from_dict(
            {"execution_mode": "wsl", "wsl_distribution": "Ubuntu"},
            "/tmp/workspace",
        )
        result = open_terminal_script(settings, "/tmp/create_project.sh")
        self.assertTrue(result.ok)
        command = popen.call_args.args[0]
        self.assertEqual(command[:4], ["C:/Windows/wt.exe", "wsl.exe", "-d", "Ubuntu"])
        self.assertEqual(command[-2:], ["sh", "/mnt/c/create_project.sh"])

    @mock.patch("odoo_manager_core.platform.shutil.which", return_value=None)
    @mock.patch("odoo_manager_core.platform.platform_id", return_value="linux")
    def test_linux_reports_missing_terminal(self, _platform, _which):
        settings = ManagerSettings.from_dict({}, "/tmp/workspace")
        result = open_terminal_script(settings, "/tmp/create_project.sh")
        self.assertFalse(result.ok)
        self.assertIn("terminal graphique", result.message)


if __name__ == "__main__":
    unittest.main()
