import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "build-desktop.yml"


class ReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_build_step_does_not_inject_empty_apple_secrets(self):
        build_step = self.workflow.split(
            "- name: Build native sidecar and Tauri installer", 1
        )[1].split("- name: Verify signed macOS packages", 1)[0]

        self.assertNotIn("secrets.APPLE_ID", build_step)
        self.assertNotIn("secrets.APPLE_PASSWORD", build_step)
        self.assertNotIn("secrets.APPLE_TEAM_ID", build_step)

    def test_notarization_variables_are_exported_after_signing_setup(self):
        signing_step = self.workflow.split(
            "- name: Configure macOS signing and notarization", 1
        )[1].split("- name: Configure Windows Authenticode signing", 1)[0]

        self.assertIn('write_github_env APPLE_ID "$APPLE_ID"', signing_step)
        self.assertIn('write_github_env APPLE_PASSWORD "$APPLE_PASSWORD"', signing_step)
        self.assertIn('write_github_env APPLE_TEAM_ID "$APPLE_TEAM_ID"', signing_step)


if __name__ == "__main__":
    unittest.main()
