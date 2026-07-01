import io
import tempfile
import unittest
import zipfile
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
        (self.project_root / "odoo" / "addons-store").mkdir(parents=True)
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

        storage = self.project_root / "odoo" / "addons-store" / "custom_module"
        link = self.project_root / "odoo" / "addons" / "custom_module"

        self.assertTrue(storage.is_dir())
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path("../addons-store/custom_module"), Path(link.readlink()))

        modules = web.modules_for(self.project)
        module = next(item for item in modules if item["name"] == "custom_module")
        self.assertEqual(str(link), module["path"])
        self.assertEqual(str(link), module["link_path"])
        self.assertEqual(str(storage.resolve(strict=False)), module["source_path"])
        self.assertEqual("lien vers addons-store", module["path_kind"])

    def test_delete_module_removes_link_and_storage_copy(self):
        job = DummyJob()
        web.link_module_candidates(job, self.project, [self.external])

        removed = web.delete_module_file_entry(job, self.project, "custom_module")

        storage = self.project_root / "odoo" / "addons-store" / "custom_module"
        link = self.project_root / "odoo" / "addons" / "custom_module"

        self.assertTrue(removed)
        self.assertFalse(link.exists() or link.is_symlink())
        self.assertFalse(storage.exists())
        self.assertTrue(any((web.DELETED_MODULES / self.project).iterdir()))

    def test_normalize_legacy_source_link_migrates_to_addons_store(self):
        job = DummyJob()
        legacy = self.project_root / "odoo" / "odoo" / "addons" / "legacy_module"
        legacy.mkdir(parents=True)
        (legacy / "__manifest__.py").write_text("{'name': 'Legacy'}\n", encoding="utf-8")
        link = self.project_root / "odoo" / "addons" / "legacy_module"
        link.symlink_to(Path("../odoo/addons/legacy_module"), target_is_directory=True)

        web.normalize_module_layout_for_action(job, self.project, ["legacy_module"])

        storage = self.project_root / "odoo" / "addons-store" / "legacy_module"
        self.assertTrue(storage.is_dir())
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path("../addons-store/legacy_module"), Path(link.readlink()))

    def test_nested_addons_store_repository_link_is_protected_from_delete(self):
        job = DummyJob()
        repository_module = self.project_root / "odoo" / "addons-store" / "sodial-addons" / "repo_module"
        repository_module.mkdir(parents=True)
        (repository_module / "__manifest__.py").write_text("{'name': 'Repo'}\n", encoding="utf-8")
        link = self.project_root / "odoo" / "addons" / "repo_module"
        link.symlink_to(Path("../addons-store/sodial-addons/repo_module"), target_is_directory=True)

        with self.assertRaises(RuntimeError):
            web.delete_module_file_entry(job, self.project, "repo_module")

    def test_replace_nested_addons_store_link_points_to_managed_copy(self):
        job = DummyJob()
        repository_module = self.project_root / "odoo" / "addons-store" / "sodial-addons" / "repo_module"
        repository_module.mkdir(parents=True)
        (repository_module / "__manifest__.py").write_text("{'name': 'Repo'}\n", encoding="utf-8")
        link = self.project_root / "odoo" / "addons" / "repo_module"
        link.symlink_to(Path("../addons-store/sodial-addons/repo_module"), target_is_directory=True)

        replacement = self.root / "replacement" / "repo_module"
        replacement.mkdir(parents=True)
        (replacement / "__manifest__.py").write_text("{'name': 'Replacement'}\n", encoding="utf-8")

        web.link_module_candidates(job, self.project, [replacement], replace_existing=True)

        storage = self.project_root / "odoo" / "addons-store" / "repo_module"
        self.assertTrue(repository_module.is_dir())
        self.assertTrue(storage.is_dir())
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path("../addons-store/repo_module"), Path(link.readlink()))
        self.assertTrue(any("La source du dépôt est conservée" in line for line in job.lines))

    def test_replace_existing_managed_copy_when_link_still_targets_repository(self):
        job = DummyJob()
        repository_module = self.project_root / "odoo" / "addons-store" / "browseinfo" / "repo_module"
        repository_module.mkdir(parents=True)
        (repository_module / "__manifest__.py").write_text("{'name': 'Repository'}\n", encoding="utf-8")

        storage = self.project_root / "odoo" / "addons-store" / "repo_module"
        storage.mkdir()
        (storage / "__manifest__.py").write_text("{'name': 'Old managed copy'}\n", encoding="utf-8")

        link = self.project_root / "odoo" / "addons" / "repo_module"
        link.symlink_to(Path("../addons-store/browseinfo/repo_module"), target_is_directory=True)

        replacement = self.root / "replacement" / "repo_module"
        replacement.mkdir(parents=True)
        (replacement / "__manifest__.py").write_text("{'name': 'Fresh ZIP copy'}\n", encoding="utf-8")

        web.link_module_candidates(job, self.project, [replacement], replace_existing=True)

        self.assertTrue(repository_module.is_dir())
        self.assertIn("Fresh ZIP copy", (storage / "__manifest__.py").read_text(encoding="utf-8"))
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path("../addons-store/repo_module"), Path(link.readlink()))

    def test_failed_zip_import_cleans_staging_directory(self):
        job = DummyJob()
        web.link_module_candidates(job, self.project, [self.external])

        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w") as archive:
            archive.writestr("custom_module/__manifest__.py", "{'name': 'Replacement'}\n")

        with self.assertRaises(RuntimeError):
            web.import_zip_modules_job(
                job,
                self.project,
                "custom_modules.zip",
                archive_buffer.getvalue(),
                replace_existing=False,
            )

        staging_root = web.project_staging_imports_root(self.project)
        self.assertFalse(any(staging_root.iterdir()))
        self.assertTrue(any("Archive temporaire nettoyée" in line for line in job.lines))


if __name__ == "__main__":
    unittest.main()
