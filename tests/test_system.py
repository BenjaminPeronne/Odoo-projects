import unittest
from unittest import mock

from odoo_manager_core.config import ManagerSettings
from odoo_manager_core.system import docker_status, shell_command


class DockerStatusTests(unittest.TestCase):
    def setUp(self):
        self.settings = ManagerSettings.from_dict({}, "/tmp/workspace")

    @mock.patch("odoo_manager_core.system.executable_available", return_value=False)
    def test_missing_docker(self, _available):
        status = docker_status(self.settings)
        self.assertEqual(status["state"], "missing")
        self.assertFalse(status["running"])

    @mock.patch("odoo_manager_core.system.executable_available", return_value=True)
    @mock.patch("odoo_manager_core.system.subprocess.run")
    def test_ready_docker(self, run, _available):
        run.return_value = mock.Mock(returncode=0, stdout='"28.0.0"\n', stderr="")
        status = docker_status(self.settings)
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["version"], "28.0.0")

    @mock.patch("odoo_manager_core.system.executable_available", return_value=True)
    @mock.patch("odoo_manager_core.system.subprocess.run")
    def test_stopped_docker(self, run, _available):
        run.return_value = mock.Mock(returncode=1, stdout="", stderr="daemon unavailable")
        status = docker_status(self.settings)
        self.assertEqual(status["state"], "stopped")
        self.assertIn("daemon unavailable", status["message"])

    @mock.patch("odoo_manager_core.system.execution_path", return_value="/mnt/c/tools/odoo_manager.sh")
    def test_shell_command_uses_wsl_prefix(self, _execution_path):
        settings = ManagerSettings.from_dict(
            {"execution_mode": "wsl", "wsl_distribution": "Ubuntu"},
            "/tmp/workspace",
        )
        command = shell_command(settings, "C:/tools/odoo_manager.sh", "--list")
        self.assertEqual(
            command,
            ["wsl.exe", "-d", "Ubuntu", "--", "sh", "/mnt/c/tools/odoo_manager.sh", "--list"],
        )


if __name__ == "__main__":
    unittest.main()
