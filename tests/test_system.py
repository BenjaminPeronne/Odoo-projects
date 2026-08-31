import unittest
from unittest import mock

from odoo_manager_core.config import ManagerSettings
from odoo_manager_core.system import docker_command, docker_status, shell_command


class DockerStatusTests(unittest.TestCase):
    def setUp(self):
        self.settings = ManagerSettings.from_dict({}, "/tmp/workspace")

    @mock.patch("odoo_manager_core.system.executable_available", return_value=False)
    def test_missing_docker(self, _available):
        status = docker_status(self.settings)
        self.assertEqual(status["state"], "missing")
        self.assertFalse(status["running"])
        self.assertFalse(status["can_start"])
        self.assertIn("install_guide", status)
        self.assertIn("Docker", status["install_guide"]["title"])
        self.assertTrue(status["install_guide"]["download_url"].startswith("https://www.docker.com/"))
        self.assertTrue(status["install_guide"]["install_url"].startswith("https://docs.docker.com/"))
        self.assertGreaterEqual(len(status["install_guide"]["steps"]), 2)

    @mock.patch("odoo_manager_core.system.executable_available", return_value=True)
    @mock.patch("odoo_manager_core.system.subprocess.run")
    def test_ready_docker(self, run, _available):
        run.return_value = mock.Mock(returncode=0, stdout='"28.0.0"\n', stderr="")
        status = docker_status(self.settings)
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["version"], "28.0.0")
        self.assertFalse(status["can_start"])

    @mock.patch("odoo_manager_core.platform.platform.system", return_value="Windows")
    @mock.patch("odoo_manager_core.system.executable_available", return_value=True)
    @mock.patch("odoo_manager_core.system.subprocess.run")
    def test_windows_docker_probe_is_hidden(self, run, _available, _system):
        run.return_value = mock.Mock(returncode=0, stdout='"28.0.0"\n', stderr="")

        docker_status(self.settings)

        self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)

    @mock.patch("odoo_manager_core.system.subprocess.run")
    @mock.patch("odoo_manager_core.platform.shutil.which")
    @mock.patch("odoo_manager_core.platform.platform.system", return_value="Darwin")
    def test_ready_docker_resolves_common_app_path(self, _system, which, run):
        def fake_which(name, path=None):
            if name == "docker" and path and "/usr/local/bin" in path:
                return "/usr/local/bin/docker"
            return None

        which.side_effect = fake_which
        run.return_value = mock.Mock(returncode=0, stdout='"29.5.3"\n', stderr="")

        status = docker_status(self.settings)

        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["version"], "29.5.3")
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/local/bin/docker")
        self.assertIn("/usr/local/bin", run.call_args.kwargs["env"]["PATH"])

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
            ["wsl.exe", "-d", "Ubuntu", "--exec", "sh", "/mnt/c/tools/odoo_manager.sh", "--list"],
        )

    @mock.patch("odoo_manager_core.system.resolve_host_executable", return_value=r"C:\Docker\docker.exe")
    @mock.patch("odoo_manager_core.system.platform_id", return_value="windows")
    def test_windows_wsl_mode_uses_docker_desktop_cli(self, _platform, _resolve):
        settings = ManagerSettings.from_dict(
            {"execution_mode": "wsl", "wsl_distribution": "Ubuntu"},
            "/tmp/workspace",
        )

        command = docker_command(settings, "info")

        self.assertEqual(command, [r"C:\Docker\docker.exe", "info"])

    @mock.patch("odoo_manager_core.system.resolve_host_executable", return_value=r"C:\Docker\docker.exe")
    @mock.patch("odoo_manager_core.system.host_executable_available", return_value=True)
    @mock.patch("odoo_manager_core.system.platform_id", return_value="windows")
    @mock.patch("odoo_manager_core.system.subprocess.run")
    def test_windows_wsl_docker_status_probes_native_cli(self, run, _platform, _available, _resolve):
        run.return_value = mock.Mock(returncode=0, stdout='"28.0.0"\n', stderr="")
        settings = ManagerSettings.from_dict(
            {"execution_mode": "wsl", "wsl_distribution": "Ubuntu"},
            "/tmp/workspace",
        )

        status = docker_status(settings)

        self.assertEqual(status["state"], "ready")
        self.assertEqual(run.call_args.args[0][0], r"C:\Docker\docker.exe")


if __name__ == "__main__":
    unittest.main()
