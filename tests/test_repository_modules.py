import subprocess
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
            web.repository_modules_job(DummyJob(), self.project, 'ssh://git@gitlab.sudokeys.com:10022/team/addons.git', '18.0', mode, names or [])

    def test_repository_validation(self):
        invalid_urls = (
            'http://example.com/a',
            'https://gitlab.sudokeys.com/team/repository.git',
            'file:///tmp/a',
            'ssh://git@gitlab.sudokeys.com/team/repository.git',
            'ssh://token@gitlab.sudokeys.com:10022/team/repository.git',
            'ssh://git@gitlab.example:10022/team/repository.git',
        )
        for url in invalid_urls:
            with self.assertRaises(ValueError):
                web.validate_module_repository(url, '18.0', 'add', '')
        valid_url = 'ssh://git@gitlab.sudokeys.com:10022/team/repository.git'
        self.assertEqual(web.validate_module_repository(valid_url, '18.0', 'add', '')[0], valid_url)
        self.assertEqual(
            web.validate_module_repository('git@gitlab.sudokeys.com:team/repository.git', '18.0', 'add', '')[0],
            'git@gitlab.sudokeys.com:team/repository.git',
        )
        with self.assertRaises(ValueError):
            web.validate_module_repository(valid_url, '18.0', 'update', '')

    def test_repository_clone_uses_non_interactive_ssh_without_credentials(self):
        def ssh_clone(command, **kwargs):
            rendered_command = ' '.join(command)
            self.assertIn('core.sshCommand=ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new', command)
            self.assertIn('protocol.ssh.allow=always', command)
            self.assertNotIn('credential.helper', rendered_command)
            return self.clone(command, **kwargs)

        with mock.patch('odoo_manager_core.project_creator.platform_id', return_value='linux'), \
                mock.patch.object(web.subprocess, 'run', side_effect=ssh_clone):
            web.repository_modules_job(
                DummyJob(), self.project, 'ssh://git@gitlab.sudokeys.com:10022/team/addons.git',
                '18.0', 'add', [])

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
                web.repository_modules_job(
                    DummyJob(), self.project,
                    'ssh://git@gitlab.sudokeys.com:10022/team/addons.git', '18.0', 'add', [])
        self.assertEqual(list((self.project_root / 'odoo/addons-store').iterdir()), [])

    def test_repository_clone_reports_missing_credentials(self):
        failure = subprocess.CompletedProcess([], 128, stderr='git@gitlab.sudokeys.com: Permission denied (publickey).')
        with mock.patch.object(web.subprocess, 'run', return_value=failure):
            with self.assertRaisesRegex(RuntimeError, 'clé SSH'):
                web.repository_modules_job(
                    DummyJob(), self.project, 'ssh://git@gitlab.sudokeys.com:10022/team/addons.git', '18.0', 'add', [])

    def test_repository_symlink_rejected(self):
        def clone_link(command, **kwargs):
            result = self.clone(command, **kwargs)
            (Path(command[-1]) / 'alpha/external').symlink_to(self.external)
            return result
        with mock.patch.object(web.subprocess, 'run', side_effect=clone_link):
            with self.assertRaises(ValueError):
                web.repository_modules_job(DummyJob(), self.project, 'ssh://git@gitlab.sudokeys.com:10022/team/addons.git', '18.0', 'add', [])
        self.assertEqual(list((self.project_root / 'odoo/addons-store').iterdir()), [])
