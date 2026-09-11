import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import smoke_test_windows_installer as smoke_test


ROOT = Path(__file__).resolve().parents[1]
class WindowsPackagingTests(unittest.TestCase):
    def test_resolves_electron_executable_and_adjacent_backend_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "SDK Local Manager.exe"
            executable.touch()
            runtime = root / "resources" / "backend" / "odoo-manager-backend-runtime"
            runtime.mkdir(parents=True)
            self.assertEqual(smoke_test.installed_paths(root), (executable, runtime))
            executable.unlink()
            self.assertIsNone(smoke_test.installed_paths(root)[0])

    @patch.object(smoke_test.time, "sleep")
    @patch.object(smoke_test, "stop_packaged_processes")
    @patch.object(smoke_test.shutil, "rmtree")
    def test_temporary_install_cleanup_retries_windows_locks(
        self,
        remove_tree,
        stop_packaged_processes,
        sleep,
    ):
        path = Mock()
        path.exists.return_value = True
        remove_tree.side_effect = [PermissionError(32, "locked"), None]

        smoke_test.remove_tree_with_retry(path)

        self.assertEqual(remove_tree.call_count, 2)
        stop_packaged_processes.assert_called_once_with()
        sleep.assert_called_once_with(0.5)

    @patch.object(smoke_test.time, "sleep")
    @patch.object(smoke_test, "request")
    def test_backend_shutdown_waits_until_health_endpoint_closes(self, request, sleep):
        request.side_effect = [(b"{}", {}), OSError("closed")]

        smoke_test.wait_for_backend_shutdown()

        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(0.2)


if __name__ == "__main__":
    unittest.main()
