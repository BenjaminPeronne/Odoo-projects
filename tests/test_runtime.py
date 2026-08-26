import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from odoo_manager_runtime import initialize_runtime_streams, runtime_log_directory


class RuntimeLogTests(unittest.TestCase):
    def test_windows_default_log_directory_uses_local_app_data(self):
        path = runtime_log_directory(
            {"LOCALAPPDATA": r"C:\Users\Test\AppData\Local"},
            system_name="Windows",
            home=Path(r"C:\Users\Test"),
        )

        self.assertEqual(path, Path(r"C:\Users\Test\AppData\Local") / "Odoo Manager" / "logs")

    def test_missing_standard_streams_are_restored_to_backend_log(self):
        original_streams = (sys.stdin, sys.stdout, sys.stderr)
        opened = ()
        with tempfile.TemporaryDirectory() as temporary:
            try:
                with patch.dict(os.environ, {"ODOO_MANAGER_LOG_DIR": temporary}):
                    sys.stdin = None
                    sys.stdout = None
                    sys.stderr = None
                    log_path, opened = initialize_runtime_streams()
                    print("backend ready", flush=True)
                self.assertEqual(log_path, Path(temporary) / "backend.log")
            finally:
                sys.stdin, sys.stdout, sys.stderr = original_streams
                for stream in opened:
                    stream.close()

            content = log_path.read_text(encoding="utf-8")
            self.assertIn("Démarrage du backend Odoo Manager", content)
            self.assertIn("backend ready", content)


if __name__ == "__main__":
    unittest.main()
