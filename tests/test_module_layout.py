import tempfile
import unittest
from pathlib import Path

import odoo_manager_web as web


class DummyJob:
    def __init__(self):
        self.lines = []

    def add(self, line):
        self.lines.append(line)


class ModuleLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = "TEST_PROJECT"
        self.project_root = self.root / self.project
        (self.project_root / "odoo" / "addons").mkdir(parents=True)
        (self.project_root / "odoo" / "odoo" / "addons").mkdir(parents=True)
        (self.project_root / "compose.yml").write_text("services: {}\n", encoding="utf-8")
        self.external = self.root / "external" / "custom_module"
        self.external.mkdir(parents=True)
        (self.external / "__manifest__.py").write_text("{'name': 'Custom'}\n", encoding="utf-8")

        self.previous_workspace = web.WORKSPACE
        self.previous_deleted_modules = web.DELETED_MODULES
        web.WORKSPACE = self.root
        web.DELETED_MODULES = self.root / ".deleted_modules"
        web.clear_project_module_cache(self.project)

    def tearDown(self):
        web.WORKSPACE = self.previous_workspace
        web.DELETED_MODULES = self.previous_deleted_modules
        web.clear_project_module_cache(self.project)
        self.temporary.cleanup()

    def test_link_module_copies_to_storage_and_creates_relative_link(self):
        job = DummyJob()

        web.link_module_candidates(job, self.project, [self.external])

        storage = self.project_root / "odoo" / "odoo" / "addons" / "custom_module"
        link = self.project_root / "odoo" / "addons" / "custom_module"

        self.assertTrue(storage.is_dir())
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path("../odoo/addons/custom_module"), Path(link.readlink()))

        modules = web.modules_for(self.project)
        module = next(item for item in modules if item["name"] == "custom_module")
        self.assertEqual(str(link), module["path"])
        self.assertEqual(str(link), module["link_path"])
        self.assertEqual(str(storage.resolve(strict=False)), module["source_path"])
        self.assertEqual("lien vers source projet", module["path_kind"])

    def test_delete_module_removes_link_and_storage_copy(self):
        job = DummyJob()
        web.link_module_candidates(job, self.project, [self.external])

        removed = web.delete_module_file_entry(job, self.project, "custom_module")

        storage = self.project_root / "odoo" / "odoo" / "addons" / "custom_module"
        link = self.project_root / "odoo" / "addons" / "custom_module"

        self.assertTrue(removed)
        self.assertFalse(link.exists() or link.is_symlink())
        self.assertFalse(storage.exists())
        self.assertTrue(any((web.DELETED_MODULES / self.project).iterdir()))


if __name__ == "__main__":
    unittest.main()
