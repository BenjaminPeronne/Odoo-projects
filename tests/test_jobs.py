import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from odoo_manager_core import jobs
from odoo_manager_core.config import ManagerSettings
from odoo_manager_core.system import docker_command


class JobControlTests(unittest.TestCase):
    def test_cancellation_is_raised_once_so_cleanup_code_can_run(self):
        control = jobs.JobControl()
        with jobs.bind_control(control):
            jobs.checkpoint()
            self.assertEqual("accepted", control.request_cancel())
            with self.assertRaises(jobs.JobCancelled):
                jobs.checkpoint()
            jobs.checkpoint()
            jobs.sleep(0)
        self.assertEqual("already", control.request_cancel())

    def test_protected_step_defers_and_irreversible_step_refuses(self):
        control = jobs.JobControl()
        with jobs.bind_control(control):
            with self.assertRaises(jobs.JobCancelled):
                with jobs.protected("git pull"):
                    self.assertEqual("deferred", control.request_cancel())
                    jobs.checkpoint()
                    self.assertEqual("git pull", control.protected_step)

        other = jobs.JobControl()
        with jobs.bind_control(other):
            with jobs.protected("suppression de la base", irreversible=True):
                self.assertEqual("refused", other.request_cancel())
                self.assertEqual("suppression de la base", other.irreversible_step)
            jobs.checkpoint()
        self.assertFalse(other.cancel_requested)

    def test_sleep_wakes_up_as_soon_as_cancellation_is_requested(self):
        control = jobs.JobControl()
        threading.Timer(0.05, control.request_cancel).start()
        started = time.monotonic()
        with jobs.bind_control(control), self.assertRaises(jobs.JobCancelled):
            jobs.sleep(10)
        self.assertLess(time.monotonic() - started, 2)

    def test_running_subprocess_is_terminated_and_reported_as_cancellation(self):
        control = jobs.JobControl()
        threading.Timer(0.2, control.request_cancel).start()
        started = time.monotonic()
        with jobs.bind_control(control), self.assertRaises(jobs.JobCancelled):
            jobs.run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                60,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.assertLess(time.monotonic() - started, 10)

    def test_interrupted_connection_error_becomes_a_cancellation(self):
        control = jobs.JobControl()
        closed = []
        with jobs.bind_control(control), self.assertRaises(jobs.JobCancelled):
            with jobs.interruptible(lambda: closed.append(True)):
                control.request_cancel()
                raise RuntimeError("connexion fermée")
        self.assertEqual([True], closed)

    def test_reverts_run_newest_first_and_continue_after_a_failure(self):
        control = jobs.JobControl()
        calls = []
        with jobs.bind_control(control):
            jobs.on_cancel("premier", lambda: calls.append("premier"))
            jobs.on_cancel("second", lambda: (_ for _ in ()).throw(RuntimeError("échec")))
            jobs.on_cancel("troisième", lambda: calls.append("troisième"))
            logs = []
            failures = control.run_reverts(logs.append)
        self.assertEqual(["troisième", "premier"], calls)
        self.assertEqual(["second (échec)"], failures)

    def test_docker_exec_commands_are_tagged_with_the_current_job(self):
        settings = ManagerSettings.from_dict({}, "/tmp/workspace")
        self.assertEqual(["exec", "odoo-demo", "ls"], docker_command(settings, "exec", "odoo-demo", "ls")[-3:])

        control = jobs.JobControl()
        with jobs.bind_control(control):
            tagged = docker_command(settings, "exec", "-e", "A=1", "odoo-demo", "odoo", "-u", "sale")
            detached = docker_command(settings, "exec", "-e", "A=1", "-d", "odoo-demo", "sh", "-lc", "odoo")
            token = control.token
            control.begin_cleanup()
            cleanup = docker_command(settings, "exec", "postgresql-demo", "psql")

        self.assertEqual(["exec", "-e", f"ODOO_MANAGER_JOB={token}", "-e", "A=1", "odoo-demo"], tagged[-9:-3])
        self.assertNotIn(f"ODOO_MANAGER_JOB={token}", detached)
        self.assertNotIn(f"ODOO_MANAGER_JOB={token}", cleanup)
        self.assertIn("odoo-demo", control._containers)

    @unittest.skipUnless(Path("/proc/self/environ").exists(), "nécessite /proc (Linux, conteneurs)")
    def test_kill_script_targets_only_processes_of_the_job(self):
        token = "test-token"
        marked = subprocess.Popen(["sleep", "30"], env={**os.environ, jobs.JOB_ENV_VARIABLE: token})
        other = subprocess.Popen(["sleep", "30"])
        try:
            subprocess.run(["sh", "-c", jobs.container_kill_script(token, "TERM")], check=False, timeout=10)
            marked.wait(timeout=5)
            self.assertIsNone(other.poll())
        finally:
            for process in (marked, other):
                if process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    unittest.main()
