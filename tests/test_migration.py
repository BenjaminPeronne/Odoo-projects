import os
import tempfile
import unittest
from pathlib import Path

from odoo_manager_core.migration import (
    MIGRATION_MARKER,
    compare_projects,
    copy_project,
    is_project_directory,
    measure_project,
    migration_candidates,
    project_is_stopped,
)


def build_project(root, name, *, running=False, with_links=True):
    """Projet minimal, à l'image d'un projet réel : code, liens d'addons, base, filestore."""
    project = root / name
    (project / "odoo" / "addons-store" / "mon_module").mkdir(parents=True)
    (project / "odoo" / "addons").mkdir(parents=True)
    (project / "odoo" / "addons-store" / "mon_module" / "__manifest__.py").write_text("{'name': 'mon'}", encoding="utf-8")
    (project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (project / "odoo.conf").write_text("[options]\n", encoding="utf-8")
    data = project / "postgresql_data"
    data.mkdir()
    (data / "PG_VERSION").write_text("14\n", encoding="utf-8")
    if running:
        (data / "postmaster.pid").write_text("42\n", encoding="utf-8")
    (project / "odoo_data" / "filestore" / "test").mkdir(parents=True)
    (project / "odoo_data" / "filestore" / "test" / "a1").write_text("piece jointe", encoding="utf-8")
    if with_links:
        # Séparateur natif : Windows ne suit pas un lien dont la cible utilise des barres obliques.
        target = os.path.join("..", "addons-store", "mon_module")
        os.symlink(target, project / "odoo" / "addons" / "mon_module", target_is_directory=True)
    return project


@unittest.skipUnless(
    os.name != "nt" or os.environ.get("ODOO_MANAGER_SYMLINKS_OK") or hasattr(os, "symlink"),
    "Les liens symboliques doivent être autorisés.",
)
class ProjectMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.windows = Path(self.temporary.name) / "windows"
        self.linux = Path(self.temporary.name) / "linux"
        self.windows.mkdir()
        self.linux.mkdir()
        self.addCleanup(self.temporary.cleanup)

    def test_only_odoo_projects_are_proposed(self):
        build_project(self.windows, "CLIENT_A")
        (self.windows / "notes").mkdir()
        (self.windows / ".odoo_manager_staging").mkdir()

        candidates = migration_candidates(self.windows, self.linux)

        self.assertEqual(["CLIENT_A"], [candidate["name"] for candidate in candidates])
        self.assertFalse(candidates[0]["already_migrated"])
        self.assertTrue(candidates[0]["stopped"])

    def test_a_running_project_is_reported_as_such(self):
        # Copier postgresql_data pendant que PostgreSQL écrit donnerait une base incohérente.
        build_project(self.windows, "CLIENT_B", running=True)
        self.assertFalse(migration_candidates(self.windows, self.linux)[0]["stopped"])
        self.assertFalse(project_is_stopped(self.windows / "CLIENT_B"))

    def test_an_already_migrated_project_is_flagged(self):
        build_project(self.windows, "CLIENT_C")
        (self.linux / "CLIENT_C").mkdir()
        self.assertTrue(migration_candidates(self.windows, self.linux)[0]["already_migrated"])

    def test_copy_keeps_links_relative_and_leaves_the_original_intact(self):
        source = build_project(self.windows, "CLIENT_D")
        before = measure_project(source)

        copy_project(source, self.linux / "CLIENT_D")

        link = self.linux / "CLIENT_D" / "odoo" / "addons" / "mon_module"
        self.assertTrue(link.is_symlink())
        self.assertEqual("../addons-store/mon_module", os.readlink(link).replace(os.sep, "/"))
        # Le lien désigne bien la copie, pas l'original.
        self.assertTrue((link / "__manifest__.py").exists())
        self.assertEqual(before, measure_project(source))
        self.assertTrue((self.linux / "CLIENT_D" / MIGRATION_MARKER).exists())

    def test_copy_brings_the_database_and_the_filestore(self):
        source = build_project(self.windows, "CLIENT_E")
        copy_project(source, self.linux / "CLIENT_E")
        self.assertEqual("14\n", (self.linux / "CLIENT_E" / "postgresql_data" / "PG_VERSION").read_text(encoding="utf-8"))
        self.assertEqual(
            "piece jointe",
            (self.linux / "CLIENT_E" / "odoo_data" / "filestore" / "test" / "a1").read_text(encoding="utf-8"),
        )

    def test_comparison_confirms_an_identical_copy(self):
        source = build_project(self.windows, "CLIENT_F")
        copy_project(source, self.linux / "CLIENT_F")

        comparison = compare_projects(source, self.linux / "CLIENT_F")

        self.assertTrue(comparison["identical"], comparison)
        self.assertEqual(comparison["source_files"], comparison["copied_files"])

    def test_comparison_catches_a_missing_file_and_a_changed_link(self):
        source = build_project(self.windows, "CLIENT_G")
        destination = self.linux / "CLIENT_G"
        copy_project(source, destination)
        (destination / "odoo.conf").unlink()
        link = destination / "odoo" / "addons" / "mon_module"
        link.unlink()
        os.symlink(os.path.join(os.sep, "ailleurs", "mon_module"), link, target_is_directory=True)

        comparison = compare_projects(source, destination)

        self.assertFalse(comparison["identical"])
        self.assertEqual(["odoo.conf"], comparison["missing"])
        self.assertEqual(["odoo/addons/mon_module"], comparison["different_links"])

    def test_copying_over_an_existing_project_is_refused(self):
        source = build_project(self.windows, "CLIENT_H")
        (self.linux / "CLIENT_H").mkdir()
        with self.assertRaises(ValueError):
            copy_project(source, self.linux / "CLIENT_H")

    def test_progress_is_reported_during_a_long_copy(self):
        source = build_project(self.windows, "CLIENT_I")
        lines = []
        copy_project(source, self.linux / "CLIENT_I", log=lines.append)
        self.assertTrue(any("Copie terminée" in line for line in lines))

    def test_a_directory_without_compose_is_not_a_project(self):
        (self.windows / "vide").mkdir()
        self.assertFalse(is_project_directory(self.windows / "vide"))


if __name__ == "__main__":
    unittest.main()
