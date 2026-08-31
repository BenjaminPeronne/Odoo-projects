import tempfile
import unittest
from pathlib import Path
from unittest import mock

from odoo_manager_core.config import ManagerSettings
from odoo_manager_core.project_creator import (
    ProjectCreator,
    validate_git_ref,
    validate_gitlab_repository,
    validate_new_project_name,
)
from odoo_manager_core.project_service import ProjectService


class FakeRunner:
    def __init__(self, fail_repository="", wsl_path_exists=False):
        self.fail_repository = fail_repository
        self.wsl_path_exists = wsl_path_exists
        self.commands = []

    def capture(self, command, cwd=None, timeout=10):
        self.commands.append(command)
        if "test" in command:
            return (0, "") if self.wsl_path_exists else (1, "")
        return 0, "git version 2.50.0"

    def stream(self, command, cwd=None, log=None):
        self.commands.append(command)
        if "ln" in command or "rm" in command:
            return 0
        repository = next((item for item in command if isinstance(item, str) and item.endswith(".git")), "")
        if repository == self.fail_repository:
            return 1
        destination = Path(command[-1])
        destination.mkdir(parents=True)
        if repository.endswith("docker-odoo-local.git"):
            (destination / "docker-compose.yml").write_text("container_name: odoo-XXXXXX\n", encoding="utf-8")
            (destination / "odoo.conf").write_text("db_host = postgresql-XXXXXX\n", encoding="utf-8")
        elif repository.endswith("/odoo.git"):
            release = destination / "odoo" / "release.py"
            release.parent.mkdir(parents=True)
            release.write_text("version_info = (19, 0, 0)\n", encoding="utf-8")
        elif repository.endswith("odoo_entreprise.git"):
            module = destination / "web_enterprise"
            module.mkdir()
            (module / "__manifest__.py").write_text("{}\n", encoding="utf-8")
        else:
            module = destination / "custom_module"
            module.mkdir()
            (module / "__manifest__.py").write_text("{}\n", encoding="utf-8")
        return 0


class ProjectCreatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.settings = ManagerSettings.from_dict({}, self.workspace)

    def tearDown(self):
        self.temporary.cleanup()

    def creator(self, runner=None):
        service = ProjectService(self.settings, self.workspace, runner=runner or FakeRunner())
        return ProjectCreator(self.settings, self.workspace, service)

    def test_standard_project_is_created_atomically_with_relative_enterprise_links(self):
        runner = FakeRunner()
        target = self.creator(runner).create("DEMO_V19", "19.0")

        self.assertTrue((target / "docker-compose.yml").exists())
        self.assertIn("odoo-DEMO_V19", (target / "docker-compose.yml").read_text(encoding="utf-8"))
        link = target / "odoo" / "addons" / "web_enterprise"
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path("../addons-store/odoo_entreprise/web_enterprise"), link.readlink())
        self.assertFalse((self.workspace / ".odoo_manager_staging").exists())
        clone_commands = [command for command in runner.commands if "clone" in command]
        self.assertTrue(clone_commands)
        self.assertTrue(
            all(
                command[command.index("clone") - 2 : command.index("clone")]
                == ["-c", "core.longpaths=true"]
                and command[command.index("clone") + 1 : command.index("clone") + 3]
                == ["--config", "core.longpaths=true"]
                for command in clone_commands
            )
        )

    def test_gitlab_addons_are_cloned_to_store_and_linked(self):
        target = self.creator().create(
            "CLIENT_V19",
            "19.0",
            source_type="gitlab",
            repository_url="ssh://git@gitlab.sudokeys.com:10022/sudokeys/client-addons.git",
            repository_branch="19.0",
        )

        link = target / "odoo" / "addons" / "custom_module"
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path("../addons-store/client-addons/custom_module"), link.readlink())

    @mock.patch("odoo_manager_core.project_creator.wsl_execution_path")
    def test_wsl_mode_creates_relative_links_through_linux(self, wsl_execution_path):
        project = self.workspace / "DEMO"
        module = project / "odoo" / "addons-store" / "custom" / "custom_module"
        addons = project / "odoo" / "addons"
        module.mkdir(parents=True)
        addons.mkdir(parents=True)
        (module / "__manifest__.py").write_text("{}\n", encoding="utf-8")
        wsl_execution_path.return_value = "/mnt/c/Odoo/DEMO/odoo/addons/custom_module"
        runner = FakeRunner()
        settings = ManagerSettings.from_dict(
            {"execution_mode": "wsl", "wsl_distribution": "Ubuntu"},
            self.workspace,
        )
        creator = ProjectCreator(settings, self.workspace, ProjectService(settings, self.workspace, runner=runner))

        linked = creator.link_modules(module.parent, addons)

        self.assertEqual(linked, 1)
        self.assertEqual(
            runner.commands[-1],
            [
                "wsl.exe",
                "-d",
                "Ubuntu",
                "--exec",
                "ln",
                "-s",
                "../addons-store/custom/custom_module",
                "/mnt/c/Odoo/DEMO/odoo/addons/custom_module",
            ],
        )

    @mock.patch.object(Path, "symlink_to", side_effect=OSError("privilege missing"))
    @mock.patch("odoo_manager_core.project_creator.platform_id", return_value="windows")
    @mock.patch(
        "odoo_manager_core.project_creator.wsl_execution_path",
        return_value="/mnt/c/Odoo/DEMO/odoo/addons/custom_module",
    )
    def test_native_windows_falls_back_to_wsl_for_relative_links(
        self,
        _wsl_execution_path,
        _platform,
        _symlink,
    ):
        project = self.workspace / "DEMO"
        module = project / "odoo" / "addons-store" / "custom" / "custom_module"
        addons = project / "odoo" / "addons"
        module.mkdir(parents=True)
        addons.mkdir(parents=True)
        (module / "__manifest__.py").write_text("{}\n", encoding="utf-8")
        runner = FakeRunner()
        creator = self.creator(runner)

        linked = creator.link_modules(module.parent, addons)

        self.assertEqual(linked, 1)
        self.assertEqual(
            runner.commands[-1],
            [
                "wsl.exe",
                "--exec",
                "ln",
                "-s",
                "../addons-store/custom/custom_module",
                "/mnt/c/Odoo/DEMO/odoo/addons/custom_module",
            ],
        )

    @mock.patch.object(Path, "exists", side_effect=OSError(1920, "unreadable WSL symlink"))
    @mock.patch.object(Path, "is_symlink", side_effect=OSError(1920, "unreadable WSL symlink"))
    @mock.patch("odoo_manager_core.project_creator.platform_id", return_value="windows")
    @mock.patch(
        "odoo_manager_core.project_creator.wsl_execution_path",
        return_value="/mnt/c/Odoo/DEMO/odoo/addons/account_3way_match",
    )
    def test_windows_detects_wsl_link_when_pathlib_returns_winerror_1920(
        self,
        _wsl_execution_path,
        _platform,
        _is_symlink,
        _exists,
    ):
        runner = FakeRunner(wsl_path_exists=True)
        creator = self.creator(runner)

        exists = creator.path_entry_exists(self.workspace / "DEMO" / "odoo" / "addons" / "account_3way_match")

        self.assertTrue(exists)
        self.assertEqual(
            runner.commands[-1],
            [
                "wsl.exe",
                "--exec",
                "test",
                "-e",
                "/mnt/c/Odoo/DEMO/odoo/addons/account_3way_match",
            ],
        )

    @mock.patch.object(Path, "is_symlink", side_effect=OSError(1920, "unreadable WSL symlink"))
    @mock.patch("odoo_manager_core.project_creator.platform_id", return_value="windows")
    @mock.patch(
        "odoo_manager_core.project_creator.wsl_execution_path",
        return_value="/mnt/c/Odoo/DEMO/odoo/addons/account_3way_match",
    )
    def test_windows_removes_unreadable_wsl_link_via_wsl(
        self,
        _wsl_execution_path,
        _platform,
        _is_symlink,
    ):
        runner = FakeRunner()
        creator = self.creator(runner)

        creator.remove_path_entry(self.workspace / "DEMO" / "odoo" / "addons" / "account_3way_match")

        self.assertEqual(
            runner.commands[-1],
            [
                "wsl.exe",
                "--exec",
                "rm",
                "-rf",
                "--",
                "/mnt/c/Odoo/DEMO/odoo/addons/account_3way_match",
            ],
        )

    def test_failed_clone_leaves_no_partial_project(self):
        runner = FakeRunner(fail_repository="ssh://git@gitlab.sudokeys.com:10022/sudokeys/odoo.git")

        with self.assertRaisesRegex(RuntimeError, "clé SSH"):
            self.creator(runner).create("BROKEN", "19.0")

        self.assertFalse((self.workspace / "BROKEN").exists())
        self.assertFalse((self.workspace / ".odoo_manager_staging").exists())

    def test_inputs_are_strictly_validated(self):
        for invalid in ("", ".hidden", "name with spaces", "../demo"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_new_project_name(invalid)
        for invalid in ("master;rm", "../master", "feature..test"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_git_ref(invalid)
        with self.assertRaises(ValueError):
            validate_gitlab_repository("https://example.com/repository.git")


if __name__ == "__main__":
    unittest.main()
