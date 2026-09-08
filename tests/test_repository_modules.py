import subprocess
import shlex
from pathlib import Path
from unittest import mock

from test_module_layout import ModuleLayoutTests, DummyJob
import odoo_manager_web as web


class RepositoryModulesTests(ModuleLayoutTests):
    def clone(self, command, **kwargs):
        root = Path(command[-1])
        root.mkdir(parents=True)
        for name in ('alpha', 'beta'):
            (root / name).mkdir()
            (root / name / '__manifest__.py').write_text("{'name': 'Test'}")
            (root / name / 'code.py').write_text('new')
        return subprocess.CompletedProcess(command, 0)

    def run_import(self, mode='add', names=None):
        with mock.patch.object(web.subprocess, 'run', side_effect=self.clone):
            web.repository_modules_job(DummyJob(), self.project, 'https://example.com/addons.git', '18.0', mode, names or [])

    def test_repository_validation(self):
        invalid_urls = (
            'http://example.com/a',
            'https://token@example.com/a',
            'https://example.com/a?token=x',
            'file:///tmp/a',
            'https://gitlab.example/https://gitlab.example/team/repository.git',
            'https://gitlab.example/team/repository.gitteam/repository.git',
            'https://gitlab.example/team\\repository.git',
        )
        for url in invalid_urls:
            with self.assertRaises(ValueError):
                web.validate_module_repository(url, '18.0', 'add', '')
        valid_url = 'https://gitlab.example/team/repository.git'
        self.assertEqual(web.validate_module_repository(valid_url, '18.0', 'add', '')[0], valid_url)
        with self.assertRaises(ValueError):
            web.validate_module_repository('https://example.com/a', '18.0', 'update', '')

    def test_repository_credentials_validation(self):
        self.assertEqual(web.validate_repository_credentials('', ''), ('', ''))
        self.assertEqual(web.validate_repository_credentials('', 'secret'), ('oauth2', 'secret'))
        self.assertEqual(web.validate_repository_credentials('benjamin', 'secret'), ('benjamin', 'secret'))
        with self.assertRaises(ValueError):
            web.validate_repository_credentials('benjamin', '')

    def test_repository_credentials_are_temporary_and_absent_from_command(self):
        def authenticated_clone(command, **kwargs):
            # On Windows, ProjectCreator probes WSL before building the clone command.
            # Keep that probe separate from the Git command asserted below.
            if not any(str(argument).startswith('credential.helper=store --file=') for argument in command):
                return subprocess.CompletedProcess(command, 1)
            rendered_command = ' '.join(command)
            self.assertNotIn('secret-token', rendered_command)
            helper = next(argument for argument in command if argument.startswith('credential.helper=store --file='))
            helper_command = helper.removeprefix('credential.helper=')
            credentials_path = Path(shlex.split(helper_command)[1].removeprefix('--file='))
            self.assertEqual(credentials_path.stat().st_mode & 0o777, 0o600)
            self.assertIn('benjamin:secret-token@', credentials_path.read_text())
            return self.clone(command, **kwargs)

        with mock.patch.object(web.subprocess, 'run', side_effect=authenticated_clone):
            web.repository_modules_job(
                DummyJob(), self.project, 'https://example.com/addons.git', '18.0',
                'add', [], 'benjamin', 'secret-token')

    def test_repository_add_select_and_conflict(self):
        self.run_import(names=['alpha'])
        self.assertTrue((self.project_root / 'odoo/addons/alpha').is_symlink())
        self.assertFalse((self.project_root / 'odoo/addons/beta').exists())
        with self.assertRaises(ValueError):
            self.run_import(names=['beta', 'alpha'])
        self.assertFalse((self.project_root / 'odoo/addons/beta').exists())

    def test_repository_update_rollback(self):
        self.run_import()
        storage = self.project_root / 'odoo/addons-store'
        (storage / 'alpha/code.py').write_text('old')
        original = web.copy_module_to_storage
        def fail_second(job, project, candidate, **kwargs):
            if candidate.name == 'beta':
                raise OSError('disk full')
            return original(job, project, candidate, **kwargs)
        with mock.patch.object(web, 'copy_module_to_storage', side_effect=fail_second):
            with self.assertRaises(OSError):
                self.run_import('update', ['alpha', 'beta'])
        self.assertEqual((storage / 'alpha/code.py').read_text(), 'old')
        self.assertTrue((storage / 'beta/__manifest__.py').is_file())

    def test_repository_update_and_missing_selection(self):
        self.run_import(names=['alpha'])
        storage = self.project_root / 'odoo/addons-store'
        (storage / 'alpha/code.py').write_text('old')
        self.run_import('update', ['alpha'])
        self.assertEqual((storage / 'alpha/code.py').read_text(), 'new')
        self.assertTrue(list((self.root / '.odoo_manager_backups/modules' / self.project).glob('*/code.py')))
        with self.assertRaises(ValueError):
            self.run_import(names=['missing'])

    def test_repository_clone_failure_leaves_project_unchanged(self):
        with mock.patch.object(web.subprocess, 'run', return_value=subprocess.CompletedProcess([], 128)):
            with self.assertRaises(RuntimeError):
                web.repository_modules_job(DummyJob(), self.project, 'https://example.com/addons.git', '18.0', 'add', [])
        self.assertEqual(list((self.project_root / 'odoo/addons-store').iterdir()), [])

    def test_repository_clone_reports_missing_credentials(self):
        failure = subprocess.CompletedProcess([], 128, stderr='fatal: unable to get password from user')
        with mock.patch.object(web.subprocess, 'run', return_value=failure):
            with self.assertRaisesRegex(RuntimeError, 'authentification HTTPS'):
                web.repository_modules_job(
                    DummyJob(), self.project, 'https://example.com/addons.git', '18.0', 'add', [])

    def test_repository_symlink_rejected(self):
        def clone_link(command, **kwargs):
            result = self.clone(command, **kwargs)
            (Path(command[-1]) / 'alpha/external').symlink_to(self.external)
            return result
        with mock.patch.object(web.subprocess, 'run', side_effect=clone_link):
            with self.assertRaises(ValueError):
                web.repository_modules_job(DummyJob(), self.project, 'https://example.com/addons.git', '18.0', 'add', [])
        self.assertEqual(list((self.project_root / 'odoo/addons-store').iterdir()), [])
