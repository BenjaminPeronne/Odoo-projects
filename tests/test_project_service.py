import tempfile
import unittest
from pathlib import Path

from odoo_manager_core.config import ManagerSettings
from odoo_manager_core.project_service import ProjectService


def has_command_tail(commands, tail):
    return any(command[-len(tail):] == tail for command in commands)


class FakeRunner:
    def __init__(self):
        self.streams = []
        self.captures = []
        self.statuses = {}
        self.odoo_server_running = True
        self.odoo_port_ready = True

    def stream(self, command, cwd=None, log=None):
        self.streams.append((list(command), Path(cwd) if cwd else None))
        if log:
            log("$ " + " ".join(command))
        return 0

    def capture(self, command, cwd=None, timeout=10):
        self.captures.append((list(command), Path(cwd) if cwd else None, timeout))
        command = list(command)
        if len(command) >= 5 and command[1:4] == ["inspect", "-f", "{{.State.Status}}"]:
            status = self.statuses.get(command[4], "absent")
            return (0, status) if status != "absent" else (1, "")
        if len(command) >= 5 and command[1:3] == ["exec", "odoo-DEMO"] and command[3:5] == ["sh", "-lc"]:
            return (0, "") if self.odoo_server_running else (1, "")
        if len(command) >= 5 and command[1:3] == ["exec", "odoo-DEMO"] and command[3:5] == ["python3", "-c"]:
            return (0, "") if self.odoo_port_ready else (1, "")
        if len(command) >= 3 and command[1:3] == ["port", "odoo-DEMO"]:
            return 1, ""
        return 0, ""


class ProjectServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project_path = self.root / "DEMO"
        self.project_path.mkdir()
        (self.project_path / "compose.yml").write_text("services: {}\n", encoding="utf-8")
        self.settings = ManagerSettings.from_dict({}, str(self.root))
        self.runner = FakeRunner()
        self.service = ProjectService(self.settings, self.root, runner=self.runner)

    def tearDown(self):
        self.temporary.cleanup()

    def test_start_project_reuses_existing_containers_without_recreate(self):
        self.runner.statuses = {
            "odoo-DEMO": "running",
            "postgresql-DEMO": "running",
        }

        self.service.start_project("DEMO", log=lambda _line: None)

        commands = [command for command, _cwd in self.runner.streams]
        self.assertTrue(has_command_tail(commands, ["compose", "up", "-d", "--no-recreate"]))
        self.assertFalse(has_command_tail(commands, ["compose", "up", "--pull", "always", "-d"]))

    def test_start_project_pulls_when_containers_are_absent(self):
        self.runner.statuses = {
            "odoo-DEMO": "absent",
            "postgresql-DEMO": "absent",
        }

        self.service.compose_up_project("DEMO", self.project_path, log=lambda _line: None)

        commands = [command for command, _cwd in self.runner.streams]
        self.assertTrue(has_command_tail(commands, ["compose", "up", "--pull", "always", "-d"]))

    def test_update_project_pulls_git_and_compose(self):
        (self.project_path / ".git").mkdir()

        self.service.update_project("DEMO", log=lambda _line: None)

        commands = [command for command, _cwd in self.runner.streams]
        self.assertIn(["git", "pull", "--ff-only"], commands)
        self.assertTrue(has_command_tail(commands, ["compose", "pull"]))
        self.assertTrue(has_command_tail(commands, ["compose", "up", "-d"]))

    def test_update_all_projects_uses_workspace_projects(self):
        other = self.root / "OTHER"
        other.mkdir()
        (other / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

        self.service.update_all_projects(log=lambda _line: None)

        compose_cwds = [cwd.name for command, cwd in self.runner.streams if command[-2:] == ["compose", "pull"]]
        self.assertEqual(compose_cwds, ["DEMO", "OTHER"])


if __name__ == "__main__":
    unittest.main()
