import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_desktop.py"
SPEC = importlib.util.spec_from_file_location("build_desktop", SCRIPT)
assert SPEC and SPEC.loader
build_desktop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_desktop)


class TauriBuildCommandTests(unittest.TestCase):
    def test_build_command_without_extra_config(self):
        self.assertEqual(
            build_desktop.tauri_build_command("nsis", {}),
            ["npm", "run", "tauri", "build", "--", "--bundles", "nsis"],
        )

    def test_build_command_with_extra_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "signing.json"
            config_path.write_text("{}", encoding="utf-8")
            self.assertEqual(
                build_desktop.tauri_build_command(
                    "nsis", {"TAURI_EXTRA_CONFIG": str(config_path)}
                ),
                [
                    "npm",
                    "run",
                    "tauri",
                    "build",
                    "--",
                    "--bundles",
                    "nsis",
                    "--config",
                    str(config_path),
                ],
            )

    def test_missing_extra_config_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "introuvable"):
            build_desktop.tauri_build_command(
                "nsis", {"TAURI_EXTRA_CONFIG": "/missing/signing.json"}
            )


if __name__ == "__main__":
    unittest.main()
