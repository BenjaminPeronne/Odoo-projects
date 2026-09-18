import unittest
from unittest import mock

from odoo_manager_core.config import ManagerSettings
from odoo_manager_core.docker_api import DockerEngineClient, EngineEndpoint, EngineUnavailable
from odoo_manager_core.system import docker_command, docker_status, reset_docker_backend_cache, shell_command


class DockerStatusTests(unittest.TestCase):
    def setUp(self):
        reset_docker_backend_cache()
        self.settings = ManagerSettings.from_dict({}, "/tmp/workspace")
        # Ces scénarios simulent la CLI : l'API du moteur ne doit pas viser le Docker du poste.
        patcher = mock.patch("odoo_manager_core.system.engine_endpoint", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    @mock.patch("odoo_manager_core.system.platform_id", return_value="linux")
    @mock.patch("odoo_manager_core.system.executable_available", return_value=False)
    def test_missing_docker(self, _available, _platform):
        status = docker_status(self.settings)
        self.assertEqual(status["state"], "missing")
        self.assertFalse(status["running"])
        self.assertFalse(status["can_start"])
        self.assertIn("install_guide", status)
        self.assertIn("Docker", status["install_guide"]["title"])
        self.assertTrue(status["install_guide"]["download_url"].startswith("https://www.docker.com/"))
        self.assertTrue(status["install_guide"]["install_url"].startswith("https://docs.docker.com/"))
        self.assertGreaterEqual(len(status["install_guide"]["steps"]), 2)

    @mock.patch("odoo_manager_core.system.platform_id", return_value="linux")
    @mock.patch("odoo_manager_core.system.executable_available", return_value=True)
    @mock.patch("odoo_manager_core.system.subprocess.run")
    def test_ready_docker(self, run, _available, _platform):
        run.return_value = mock.Mock(returncode=0, stdout='"28.0.0"\n', stderr="")
        status = docker_status(self.settings)
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["version"], "28.0.0")
        self.assertFalse(status["can_start"])

    @mock.patch("odoo_manager_core.platform.platform.system", return_value="Windows")
    @mock.patch("odoo_manager_core.system.platform_id", return_value="windows")
    @mock.patch("odoo_manager_core.system.host_executable_available", return_value=True)
    @mock.patch("odoo_manager_core.system.subprocess.run")
    def test_windows_docker_probe_is_hidden(self, run, _available, _platform, _system):
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

    @mock.patch("odoo_manager_core.system.platform_id", return_value="linux")
    @mock.patch("odoo_manager_core.system.executable_available", return_value=True)
    @mock.patch("odoo_manager_core.system.subprocess.run")
    def test_stopped_docker(self, run, _available, _platform):
        run.return_value = mock.Mock(returncode=1, stdout="", stderr="daemon unavailable")
        status = docker_status(self.settings)
        self.assertEqual(status["state"], "stopped")
        self.assertIn("daemon unavailable", status["message"])

    @mock.patch("odoo_manager_core.system.platform_id", return_value="linux")
    @mock.patch("odoo_manager_core.system.executable_available", return_value=True)
    @mock.patch("odoo_manager_core.system.subprocess.run")
    def test_slow_docker_that_answered_recently_stays_ready(self, run, _available, _platform):
        import subprocess

        run.return_value = mock.Mock(returncode=0, stdout='"29.8.0"\n', stderr="")
        self.assertEqual("ready", docker_status(self.settings)["state"])

        run.side_effect = subprocess.TimeoutExpired(["docker", "version"], 6)
        slow = docker_status(self.settings)

        self.assertEqual("ready", slow["state"])
        self.assertTrue(slow["running"])
        self.assertTrue(slow["slow"])
        self.assertEqual("29.8.0", slow["version"])
        self.assertIn("répond lentement", slow["message"])

        with mock.patch("odoo_manager_core.system.time.monotonic", return_value=10**9):
            stopped = docker_status(self.settings)
        self.assertEqual("stopped", stopped["state"])
        self.assertEqual("Docker ne répond pas dans le délai de 6 s.", stopped["message"])

    @mock.patch("odoo_manager_core.system.platform_id", return_value="linux")
    @mock.patch("odoo_manager_core.system.executable_available", return_value=True)
    @mock.patch("odoo_manager_core.system.subprocess.run")
    def test_docker_that_never_answered_is_not_reported_ready_on_timeout(self, run, _available, _platform):
        import subprocess

        run.side_effect = subprocess.TimeoutExpired(["docker", "version"], 6)

        status = docker_status(self.settings)

        self.assertEqual("stopped", status["state"])
        self.assertFalse(status["running"])

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
    def test_windows_docker_status_does_not_wake_wsl_when_native_docker_runs(self, run, _platform, _available, _resolve):
        # Chaque sonde WSL démarrait la VM, toutes les 10 s avec l'overview.
        run.return_value = mock.Mock(returncode=0, stdout='"28.0.0"\n', stderr="")
        settings = ManagerSettings.from_dict({}, "/tmp/workspace")

        status = docker_status(settings)

        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["backend"], "native")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([[r"C:\Docker\docker.exe", "version", "--format", "{{json .Server.Version}}"]], commands)

    @mock.patch("odoo_manager_core.system.resolve_host_executable", return_value=r"C:\Docker\docker.exe")
    @mock.patch("odoo_manager_core.system.host_executable_available", return_value=True)
    @mock.patch("odoo_manager_core.system.platform_id", return_value="windows")
    @mock.patch("odoo_manager_core.system.subprocess.run")
    def test_windows_falls_back_to_running_wsl_docker(self, run, _platform, _available, _resolve):
        def result(command, **_kwargs):
            if command[0] == r"C:\Docker\docker.exe":
                return mock.Mock(returncode=1, stdout="", stderr="Docker Desktop stopped")
            if "--version" in command:
                return mock.Mock(returncode=0, stdout="Docker version 28.0.0", stderr="")
            return mock.Mock(returncode=0, stdout='"28.0.0"\n', stderr="")

        run.side_effect = result

        status = docker_status(self.settings)

        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["backend"], "wsl")
        self.assertEqual(docker_command(self.settings, "ps")[:3], ["wsl.exe", "--exec", "docker"])


class DockerEngineApiTests(unittest.TestCase):
    """L'état lu par l'API évite 150 à 560 ms et deux processus par appel de la CLI."""

    def setUp(self):
        reset_docker_backend_cache()
        self.settings = ManagerSettings.from_dict({}, "/tmp/workspace")

    @mock.patch("odoo_manager_core.system.host_executable_available", return_value=True)
    @mock.patch("odoo_manager_core.system.platform_id", return_value="windows")
    @mock.patch("odoo_manager_core.system.resolve_host_executable", return_value=r"C:\Docker\docker.exe")
    @mock.patch("odoo_manager_core.system.subprocess.run")
    @mock.patch("odoo_manager_core.system.engine_endpoint", return_value=EngineEndpoint("npipe", r"\.\pipe\docker_engine"))
    def test_status_comes_from_the_engine_api_without_running_docker(self, _endpoint, run, _resolve, _platform, _available):
        with mock.patch.object(DockerEngineClient, "server_version", return_value="29.7.2"):
            status = docker_status(self.settings)

        self.assertEqual(("ready", True, "29.7.2"), (status["state"], status["running"], status["version"]))
        run.assert_not_called()

    @mock.patch("odoo_manager_core.system.host_executable_available", return_value=True)
    @mock.patch("odoo_manager_core.system.platform_id", return_value="windows")
    @mock.patch("odoo_manager_core.system.resolve_host_executable", return_value=r"C:\Docker\docker.exe")
    @mock.patch("odoo_manager_core.system.subprocess.run")
    @mock.patch("odoo_manager_core.system.engine_endpoint", return_value=EngineEndpoint("npipe", r"\.\pipe\docker_engine"))
    def test_unreachable_api_falls_back_to_the_cli(self, _endpoint, run, _resolve, _platform, _available):
        run.return_value = mock.Mock(returncode=0, stdout='"29.7.2"\n', stderr="")

        with mock.patch.object(DockerEngineClient, "server_version", side_effect=EngineUnavailable("pipe absent")):
            status = docker_status(self.settings)

        self.assertEqual(("ready", "29.7.2"), (status["state"], status["version"]))
        self.assertEqual(
            [[r"C:\Docker\docker.exe", "version", "--format", "{{json .Server.Version}}"]],
            [call.args[0] for call in run.call_args_list],
        )


if __name__ == "__main__":
    unittest.main()
