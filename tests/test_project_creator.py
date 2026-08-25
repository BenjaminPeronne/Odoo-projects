import tempfile
import unittest
from pathlib import Path

from odoo_manager_core.config import ManagerSettings
from odoo_manager_core.project_creator import (
    ProjectCreator,
    validate_git_ref,
    validate_gitlab_repository,
    validate_new_project_name,
)
from odoo_manager_core.project_service import ProjectService


class FakeRunner:
    def __init__(self, fail_repository=""):
        self.fail_repository = fail_repository
        self.commands = []

    def capture(self, command, cwd=None, timeout=10):
        return 0, "git version 2.50.0"

    def stream(self, command, cwd=None, log=None):
        self.commands.append(command)
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
        target = self.creator().create("DEMO_V19", "19.0")

        self.assertTrue((target / "docker-compose.yml").exists())
        self.assertIn("odoo-DEMO_V19", (target / "docker-compose.yml").read_text(encoding="utf-8"))
        link = target / "odoo" / "addons" / "web_enterprise"
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path("../addons-store/odoo_entreprise/web_enterprise"), link.readlink())
        self.assertFalse((self.workspace / ".odoo_manager_staging").exists())

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
