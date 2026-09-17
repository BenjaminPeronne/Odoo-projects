import hashlib
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WSL_DIRECTORY = ROOT / "wsl"
SCRIPT = ROOT / "scripts" / "build_wsl_image.py"
SPEC = importlib.util.spec_from_file_location("build_wsl_image", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class ImageNamingTests(unittest.TestCase):
    def test_names_follow_the_application_version(self):
        self.assertEqual("sdk-manager-rootfs:0.5.0", MODULE.image_tag("0.5.0"))
        self.assertEqual("sdk-manager-0.5.0.wsl", MODULE.image_file_name("0.5.0"))
        self.assertEqual("sdk-manager-0.5.0.wsl.sha256", MODULE.checksum_file_name("0.5.0"))

    def test_version_comes_from_the_application_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            frontend = Path(temporary)
            (frontend / "package.json").write_text('{"version": "1.2.3"}', encoding="utf-8")
            self.assertEqual("1.2.3", MODULE.application_version(frontend))

    def test_checksum_matches_the_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "image.wsl"
            archive.write_bytes(b"rootfs" * 1000)
            self.assertEqual(hashlib.sha256(b"rootfs" * 1000).hexdigest(), MODULE.sha256_of(archive))

    def test_missing_sources_are_reported_before_building(self):
        self.assertEqual([], MODULE.missing_sources())
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(list(MODULE.REQUIRED_FILES), MODULE.missing_sources(temporary))


class DistributionContentTests(unittest.TestCase):
    """L'image porte la promesse « un clic » : ce qu'elle contient ne doit pas dériver en silence."""

    def setUp(self):
        self.dockerfile = (WSL_DIRECTORY / "Dockerfile").read_text(encoding="utf-8")
        self.wsl_conf = (WSL_DIRECTORY / "wsl.conf").read_text(encoding="utf-8")

    def test_installs_docker_engine_and_compose(self):
        for package in ("docker-ce", "docker-compose-plugin", "containerd.io"):
            self.assertIn(package, self.dockerfile)

    def test_installs_the_tools_the_manager_drives(self):
        for package in ("git", "openssh-client", "systemd"):
            self.assertRegex(self.dockerfile, re.compile(rf"^\s+{re.escape(package)}\s*\\?$", re.MULTILINE))

    def test_pins_the_base_image(self):
        self.assertRegex(self.dockerfile, re.compile(r"^FROM debian:12-slim$", re.MULTILINE))

    def test_systemd_starts_docker_in_the_distribution(self):
        self.assertIn("systemd = true", self.wsl_conf)
        self.assertIn("systemctl enable docker", self.dockerfile)

    def test_windows_path_is_not_appended(self):
        # Sinon `docker` et `git` se résolvent vers les exécutables Windows, lents.
        self.assertIn("appendWindowsPath = false", self.wsl_conf)

    def test_windows_drives_stay_mounted_for_migration(self):
        self.assertRegex(self.wsl_conf, r"\[automount\][\s\S]*enabled = true")

    def test_default_user_matches_the_provisioned_account(self):
        self.assertIn("default = sdk", self.wsl_conf)
        self.assertIn("--uid 1000 sdk", self.dockerfile)

    def test_provisioning_is_shipped_and_executable_by_the_image(self):
        self.assertIn("COPY provision.sh /opt/sdk-manager/provision.sh", self.dockerfile)
        self.assertIn("chmod 0755 /opt/sdk-manager/provision.sh", self.dockerfile)

    def test_provisioning_never_touches_projects(self):
        provisioning = (WSL_DIRECTORY / "provision.sh").read_text(encoding="utf-8")
        self.assertNotIn("rm -rf", provisioning)
        self.assertIn("set -eu", provisioning)
        # Crée le dossier des projets sans écraser son contenu.
        self.assertIn('install -d -o "$SDK_USER"', provisioning)

    def test_container_logs_are_rotated(self):
        daemon = json.loads((WSL_DIRECTORY / "daemon.json").read_text(encoding="utf-8"))
        self.assertEqual("json-file", daemon["log-driver"])
        self.assertIn("max-size", daemon["log-opts"])


if __name__ == "__main__":
    unittest.main()
