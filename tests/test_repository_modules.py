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
        job = DummyJob()
        with mock.patch.object(web.subprocess, 'run', side_effect=self.clone):
            web.repository_modules_job(job, self.project, 'ssh://git@gitlab.sudokeys.com:10022/team/addons.git', '18.0', mode, names or [])
        return job

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
        self.assertEqual(web.validate_module_repository(valid_url, '18.0', 'update', '')[3], [])
        with self.assertRaises(ValueError):
            web.validate_module_repository(valid_url, '18.0', 'replace', '')

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
        job = self.run_import(names=['alpha'])
        self.assertEqual(job.result, {'kind': 'repository_modules', 'mode': 'add', 'modules': ['alpha']})
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

    def test_repository_update_without_names_replaces_every_managed_module_only(self):
        self.run_import(names=['alpha'])
        storage = self.project_root / 'odoo/addons-store'
        (storage / 'alpha/code.py').write_text('old')

        job = self.run_import('update')

        self.assertEqual(job.result['modules'], ['alpha'])
        self.assertEqual((storage / 'alpha/code.py').read_text(), 'new')
        self.assertFalse((storage / 'beta').exists())
        self.assertTrue(any('1 module(s) du dépôt ignoré(s)' in line for line in job.lines))

    def test_repository_update_without_names_requires_a_managed_module(self):
        with self.assertRaisesRegex(ValueError, 'Aucun module du dépôt'):
            self.run_import('update')

    def test_tree_listing_follows_import_discovery_rules(self):
        tree = "\n".join([
            "100644 blob a\tsale_x/__manifest__.py",
            "100644 blob b\tsale_x/tests/fixture/__manifest__.py",
            "100644 blob c\taddons/stock_y/__openerp__.py",
            "100644 blob d\tnode_modules/pkg/__manifest__.py",
            "120000 blob e\tstock_y_link",
            "100644 blob f\tREADME.md",
        ])
        modules, has_symlinks = web.repository_modules_from_tree(tree, "client-addons")
        self.assertEqual({"sale_x": "sale_x", "addons/stock_y": "stock_y"}, modules)
        self.assertTrue(has_symlinks)

        single, _ = web.repository_modules_from_tree("100644 blob a\t__manifest__.py\n100644 blob b\tsub/__manifest__.py", "my_module")
        self.assertEqual({"": "my_module"}, single)

    def test_inspection_lists_tree_without_checkout_and_reports_project_status(self):
        self.run_import(names=['alpha'])
        commands = []

        def fake_git(command, **kwargs):
            commands.append(command)
            if 'clone' in command:
                Path(command[-1]).mkdir(parents=True)
                return subprocess.CompletedProcess(command, 0, stdout='', stderr='')
            tree = "100644 blob a\talpha/__manifest__.py\n100644 blob b\tbeta/__manifest__.py\n100644 blob c\tbad name/__manifest__.py"
            return subprocess.CompletedProcess(command, 0, stdout=tree, stderr='')

        with mock.patch.object(web.subprocess, 'run', side_effect=fake_git), \
                mock.patch.object(web, 'module_dirs', side_effect=AssertionError('scan du projet')):
            result = web.inspect_repository_modules(
                self.project, 'ssh://git@gitlab.sudokeys.com:10022/team/addons.git', 'DEV')

        clone = next(command for command in commands if 'clone' in command)
        self.assertIn('--filter=blob:none', clone)
        self.assertIn('--no-checkout', clone)
        self.assertTrue(any('ls-tree' in command for command in commands))
        by_name = {module['name']: module for module in result['modules']}
        self.assertTrue(by_name['alpha']['present'] and by_name['alpha']['updatable'])
        self.assertFalse(by_name['beta']['present'])
        self.assertFalse(by_name['bad name']['valid'])
        self.assertEqual(list((self.project_root / 'odoo/addons-store').iterdir()), [self.project_root / 'odoo/addons-store/alpha'])

    def test_inspection_rejects_untrusted_repository_url(self):
        with self.assertRaises(ValueError):
            web.inspect_repository_modules(self.project, 'https://example.org/repo.git', 'main')

    def test_repository_add_refuses_to_shadow_a_module_already_provided_by_the_project(self):
        core = self.project_root / 'odoo/odoo/addons/beta'
        core.mkdir(parents=True)
        (core / '__manifest__.py').write_text("{'name': 'Core'}")
        web.clear_project_module_cache(self.project)

        with self.assertRaisesRegex(ValueError, 'déjà fourni par le projet'):
            self.run_import(names=['beta'])
        self.assertFalse((self.project_root / 'odoo/addons/beta').exists())
        self.assertFalse((self.project_root / 'odoo/addons-store/beta').exists())

    def test_repository_import_never_scans_the_whole_project(self):
        # Sous Windows, module_dirs() lance wsl.exe : un import ne doit jamais en dépendre.
        with mock.patch.object(web, 'module_dirs', side_effect=AssertionError('scan du projet')):
            job = self.run_import(names=['alpha'])
        self.assertEqual(job.result['modules'], ['alpha'])
