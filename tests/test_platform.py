import unittest
from pathlib import Path
from unittest import mock

from odoo_manager_core.config import ManagerSettings
from odoo_manager_core.platform import execution_path, executable_search_path, hidden_process_kwargs, open_terminal_script


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
        self.assertEqual(command[4], "--exec")
        self.assertEqual(command[-2:], ["sh", "/mnt/c/create_project.sh"])

    @mock.patch("odoo_manager_core.platform.Path")
    @mock.patch("odoo_manager_core.platform.subprocess.run")
    def test_wslpath_receives_path_without_default_shell_reparsing(self, run, path_class):
        run.return_value = mock.Mock(returncode=0, stdout="/mnt/c/Users/Demo/Odoo-projects\n", stderr="")
        path_class.return_value.expanduser.return_value.resolve.return_value = (
            r"C:\Users\Demo\Odoo-projects"
        )
        settings = ManagerSettings.from_dict(
            {"execution_mode": "wsl", "wsl_distribution": "Ubuntu"},
            "/tmp/workspace",
        )

        translated = execution_path("/tmp/workspace", settings)

        self.assertEqual(translated, "/mnt/c/Users/Demo/Odoo-projects")
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["wsl.exe", "-d", "Ubuntu", "--exec", "wslpath"])
        self.assertEqual(command[-1], "C:/Users/Demo/Odoo-projects")

    @mock.patch("odoo_manager_core.platform.shutil.which", return_value=None)
    @mock.patch("odoo_manager_core.platform.platform_id", return_value="linux")
    def test_linux_reports_missing_terminal(self, _platform, _which):
        settings = ManagerSettings.from_dict({}, "/tmp/workspace")
        result = open_terminal_script(settings, "/tmp/create_project.sh")
        self.assertFalse(result.ok)
        self.assertIn("terminal graphique", result.message)


class WindowsProcessTests(unittest.TestCase):
    @mock.patch("odoo_manager_core.platform.platform.system", return_value="Windows")
    def test_background_commands_never_create_a_console_window(self, _system):
        self.assertEqual(hidden_process_kwargs()["creationflags"], 0x08000000)

    @mock.patch.dict(
        "odoo_manager_core.platform.os.environ",
        {
            "PATH": "",
            "ProgramFiles": r"C:\Program Files",
            "LOCALAPPDATA": r"C:\Users\Demo\AppData\Local",
            "SystemRoot": r"C:\Windows",
        },
        clear=False,
    )
    @mock.patch("odoo_manager_core.platform.platform.system", return_value="Windows")
    def test_windows_search_path_includes_git_openssh_and_winget(self, _system):
        search_path = executable_search_path()

        self.assertIn(str(Path(r"C:\Program Files") / "Git" / "cmd"), search_path)
        self.assertIn(str(Path(r"C:\Program Files") / "Git" / "usr" / "bin"), search_path)
        self.assertIn(str(Path(r"C:\Windows") / "System32" / "OpenSSH"), search_path)
        self.assertIn(
            str(Path(r"C:\Users\Demo\AppData\Local") / "Microsoft" / "WindowsApps"),
            search_path,
        )


if __name__ == "__main__":
    unittest.main()
