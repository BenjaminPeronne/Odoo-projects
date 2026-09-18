import os
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from odoo_manager_core.migration import (
    MIGRATION_MARKER,
    compare_projects,
    copy_project,
    copy_project_privileged,
    list_tree,
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


SUDO = ["/usr/bin/sudo", "-n"]


def completed(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, "")


class PrivilegedMigrationTests(unittest.TestCase):
    """Relevé réel sous WSL : postgresql_data appartient à l'uid 999 en 0700, illisible pour `sdk`."""

    def test_an_unreadable_lock_is_checked_through_sudo(self):
        calls = []

        def run(command, **_kwargs):
            calls.append(command)
            return completed("running\n")

        with mock.patch.object(Path, "exists", side_effect=PermissionError(13, "Permission denied")):
            self.assertFalse(project_is_stopped("/mnt/c/p/SIMPAC", SUDO, run))
        self.assertEqual(SUDO, calls[0][:2])
        self.assertEqual(str(Path("/mnt/c/p/SIMPAC") / "postgresql_data" / "postmaster.pid"), calls[0][-1])

    def test_a_stopped_project_is_recognised_through_sudo(self):
        with mock.patch.object(Path, "exists", side_effect=PermissionError(13, "Permission denied")):
            self.assertTrue(project_is_stopped("/mnt/c/p/DEMO", SUDO, lambda *_a, **_k: completed("stopped\n")))

    def test_an_unreadable_lock_without_sudo_blocks_the_migration(self):
        with mock.patch.object(Path, "exists", side_effect=PermissionError(13, "Permission denied")):
            self.assertFalse(project_is_stopped("/mnt/c/p/DEMO", [], lambda *_a, **_k: completed("stopped\n")))

    def test_a_failing_sudo_never_reads_as_stopped(self):
        with mock.patch.object(Path, "exists", side_effect=PermissionError(13, "Permission denied")):
            self.assertFalse(project_is_stopped("/mnt/c/p/DEMO", SUDO, lambda *_a, **_k: completed("", returncode=1)))

    def listing(self):
        return completed(
            "docker-compose.yml\tf\t120\t\n"
            "postgresql_data\td\t4096\t\n"
            "postgresql_data/PG_VERSION\tf\t3\t\n"
            "odoo/addons/account\tl\t30\t../addons-store/odoo/addons/account\n"
        )

    def test_tree_listing_keeps_types_sizes_and_link_targets(self):
        entries = list_tree("/mnt/c/p/DEMO", SUDO, lambda *_a, **_k: self.listing())
        self.assertEqual(("l", 30, "../addons-store/odoo/addons/account"), entries["odoo/addons/account"])
        self.assertEqual(("d", 4096, ""), entries["postgresql_data"])

    def test_measure_counts_files_and_links_but_not_directories(self):
        measured = measure_project("/mnt/c/p/DEMO", SUDO, lambda *_a, **_k: self.listing())
        self.assertEqual({"files": 3, "bytes": 123}, measured)

    def test_comparison_through_sudo_sees_the_database_files(self):
        identical = compare_projects("/src", "/dst", SUDO, lambda *_a, **_k: self.listing())
        self.assertTrue(identical["identical"])

        def run(command, **_kwargs):
            return self.listing() if "/src" in command else completed("docker-compose.yml\tf\t120\t\n")

        truncated = compare_projects("/src", "/dst", SUDO, run)
        self.assertFalse(truncated["identical"])
        self.assertIn("postgresql_data/PG_VERSION", truncated["missing"])

    def test_the_source_is_listed_once_for_measure_and_comparison(self):
        # À travers /mnt/c, lister un projet Enterprise prend près de deux minutes.
        listed = []

        def run(command, **_kwargs):
            listed.append(command[3])
            return self.listing()

        listing = list_tree("/src", SUDO, run)
        measure_project("/src", SUDO, run, listing=listing)
        compare_projects("/src", "/dst", SUDO, run, source_listing=listing)
        self.assertEqual(["/src", "/dst"], listed)

    def test_privileged_copy_preserves_owners_and_marks_the_copy(self):
        calls = []

        class Process:
            returncode = 0

            def communicate(self, timeout=None):
                return "", ""

        def popen(command, **_kwargs):
            calls.append(command)
            return Process()

        def run(command, **_kwargs):
            calls.append(command)
            return completed()

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "DEMO"
            copy_project_privileged("/mnt/c/p/DEMO", destination, SUDO, popen=popen, run=run)

        self.assertEqual([*SUDO, "cp", "-a", "--", str(Path("/mnt/c/p/DEMO")), str(destination)], calls[0])
        self.assertTrue(calls[1][-1].endswith(MIGRATION_MARKER))

    def test_a_failed_privileged_copy_is_reported(self):
        class Process:
            returncode = 1

            def communicate(self, timeout=None):
                return "", "cp: cannot stat: No space left on device"

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "No space left"):
                copy_project_privileged("/src", Path(temporary) / "DEMO", SUDO,
                                        popen=lambda *_a, **_k: Process(), run=lambda *_a, **_k: completed())


if __name__ == "__main__":
    unittest.main()
