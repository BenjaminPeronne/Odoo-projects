import subprocess
from pathlib import Path
from unittest import mock

from test_module_layout import ModuleLayoutTests, DummyJob
import odoo_manager_web as web

URL = 'ssh://git@gitlab.sudokeys.com:10022/team/addons.git'
COMMIT = 'a' * 40


class RepositoryModulesTests(ModuleLayoutTests):
    manifests = {
        'alpha': "{'name': 'Alpha', 'version': '18.0.1.0.1'}",
        'beta': "{'name': 'Beta', 'version': '18.0.1.0.0'}",
    }

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(web, 'project_odoo_version', return_value='18.0')
        patcher.start()
        self.addCleanup(patcher.stop)

    def clone(self, command, **kwargs):
        if 'rev-parse' in command:
            return subprocess.CompletedProcess(command, 0, stdout=COMMIT + '\n', stderr='')
        root = Path(command[-1])
        root.mkdir(parents=True)
        for name, manifest in self.manifests.items():
            (root / name).mkdir()
            (root / name / '__manifest__.py').write_text(manifest)
            (root / name / 'code.py').write_text('new')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    def run_import(self, names, commit=''):
        job = DummyJob()
        with mock.patch.object(web.subprocess, 'run', side_effect=self.clone):
            web.repository_modules_job(job, self.project, URL, '18.0', names, commit)
        return job

    def storage(self, name=''):
        return self.project_root / 'odoo/addons-store' / name

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
                web.validate_module_repository(url, '18.0', 'alpha')
        self.assertEqual(web.validate_module_repository(URL, '18.0', 'alpha')[0], URL)
        self.assertEqual(
            web.validate_module_repository('git@gitlab.sudokeys.com:team/repository.git', '18.0', 'alpha')[0],
            'git@gitlab.sudokeys.com:team/repository.git',
        )
        self.assertEqual(web.validate_module_repository(URL, '18.0', 'beta,alpha', COMMIT)[2:], (['alpha', 'beta'], COMMIT))
        with self.assertRaisesRegex(ValueError, 'au moins un module'):
            web.validate_module_repository(URL, '18.0', '')
        with self.assertRaisesRegex(ValueError, 'Commit'):
            web.validate_module_repository(URL, '18.0', 'alpha', 'HEAD; rm -rf /')

    def test_repository_clone_uses_non_interactive_ssh_without_credentials(self):
        def ssh_clone(command, **kwargs):
            rendered_command = ' '.join(command)
            self.assertIn('core.sshCommand=ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new', command)
            self.assertIn('protocol.ssh.allow=always', command)
            self.assertNotIn('credential.helper', rendered_command)
            return self.clone(command, **kwargs)

        with mock.patch('odoo_manager_core.project_creator.platform_id', return_value='linux'), \
                mock.patch.object(web.subprocess, 'run', side_effect=ssh_clone):
            web.repository_modules_job(DummyJob(), self.project, URL, '18.0', ['alpha'])

    def test_new_module_is_copied_and_linked(self):
        job = self.run_import(['alpha'])
        self.assertEqual(job.result, {'kind': 'repository_modules', 'modules': ['alpha'], 'added': ['alpha'], 'updated': []})
        self.assertTrue((self.project_root / 'odoo/addons/alpha').is_symlink())
        self.assertFalse((self.project_root / 'odoo/addons/beta').exists())

    def test_mixed_selection_adds_new_modules_and_updates_managed_ones(self):
        # Recette : 3 modules dont 2 déjà présents faisaient échouer tout l'import.
        self.run_import(['alpha'])
        (self.storage('alpha') / 'code.py').write_text('old')

        job = self.run_import(['beta', 'alpha'])

        self.assertEqual((job.result['added'], job.result['updated']), (['beta'], ['alpha']))
        self.assertEqual((self.storage('alpha') / 'code.py').read_text(), 'new')
        self.assertTrue((self.project_root / 'odoo/addons/beta').is_symlink())
        self.assertTrue(list((self.root / '.odoo_manager_backups/modules' / self.project).glob('*/code.py')))

    def test_failed_import_restores_updates_and_removes_only_what_it_created(self):
        self.run_import(['alpha'])
        (self.storage('alpha') / 'code.py').write_text('old')
        original = web.copy_module_to_storage

        def fail_beta(job, project, candidate, **kwargs):
            if candidate.name == 'beta':
                raise OSError('disk full')
            return original(job, project, candidate, **kwargs)

        with mock.patch.object(web, 'copy_module_to_storage', side_effect=fail_beta):
            with self.assertRaises(OSError):
                self.run_import(['alpha', 'beta'])
        self.assertEqual((self.storage('alpha') / 'code.py').read_text(), 'old')
        self.assertTrue((self.project_root / 'odoo/addons/alpha').is_symlink())
        self.assertFalse(self.storage('beta').exists())
        self.assertFalse((self.project_root / 'odoo/addons/beta').is_symlink())

    def test_missing_selection_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'absents du dépôt'):
            self.run_import(['missing'])

    def test_empty_selection_is_rejected_without_cloning(self):
        with mock.patch.object(web.subprocess, 'run', side_effect=AssertionError('clone')):
            with self.assertRaisesRegex(ValueError, 'au moins un module'):
                web.repository_modules_job(DummyJob(), self.project, URL, '18.0', [])

    def test_repository_clone_failure_leaves_project_unchanged(self):
        with mock.patch.object(web.subprocess, 'run', return_value=subprocess.CompletedProcess([], 128)):
            with self.assertRaises(RuntimeError):
                web.repository_modules_job(DummyJob(), self.project, URL, '18.0', ['alpha'])
        self.assertEqual(list(self.storage().iterdir()), [])

    def test_repository_clone_reports_missing_credentials(self):
        failure = subprocess.CompletedProcess([], 128, stderr='git@gitlab.sudokeys.com: Permission denied (publickey).')
        with mock.patch.object(web.subprocess, 'run', return_value=failure):
            with self.assertRaisesRegex(RuntimeError, 'clé SSH'):
                web.repository_modules_job(DummyJob(), self.project, URL, '18.0', ['alpha'])

    def test_symlink_blocks_only_the_module_that_contains_it(self):
        def clone_link(command, **kwargs):
            result = self.clone(command, **kwargs)
            if 'clone' in command:
                (Path(command[-1]) / 'beta/external').symlink_to(self.external)
            return result

        with mock.patch.object(web.subprocess, 'run', side_effect=clone_link):
            with self.assertRaisesRegex(ValueError, 'beta : Contient des liens symboliques'):
                web.repository_modules_job(DummyJob(), self.project, URL, '18.0', ['beta'])
            self.assertEqual(list(self.storage().iterdir()), [])
            web.repository_modules_job(DummyJob(), self.project, URL, '18.0', ['alpha'])
        self.assertTrue(self.storage('alpha').is_dir())

    def test_changed_branch_since_inspection_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'nouveaux commits'):
            self.run_import(['alpha'], commit='b' * 40)
        self.assertEqual(list(self.storage().iterdir()), [])
        self.assertEqual(self.run_import(['alpha'], commit=COMMIT).result['added'], ['alpha'])

    def test_module_for_another_odoo_series_is_blocked(self):
        self.manifests = {**self.manifests, 'alpha': "{'name': 'Alpha', 'version': '17.0.1.0.0'}"}
        with self.assertRaisesRegex(ValueError, 'Prévu pour Odoo 17.0'):
            self.run_import(['alpha', 'beta'])
        self.assertEqual(list(self.storage().iterdir()), [])

    def test_repository_add_refuses_to_shadow_a_module_already_provided_by_the_project(self):
        core = self.project_root / 'odoo/odoo/addons/beta'
        core.mkdir(parents=True)
        (core / '__manifest__.py').write_text("{'name': 'Core'}")
        web.clear_project_module_cache(self.project)

        with self.assertRaisesRegex(ValueError, 'avant toute modification[\\s\\S]*beta : Fourni par Odoo standard'):
            self.run_import(['alpha', 'beta'])
        self.assertFalse((self.project_root / 'odoo/addons/beta').exists())
        self.assertFalse(self.storage('alpha').exists())

    def test_module_linked_to_another_project_source_is_blocked(self):
        # Cas CARITEL : le module est fourni par la copie d'un autre dépôt sous addons-store.
        other = self.storage('caritel_v18/addons/alpha')
        other.mkdir(parents=True)
        (other / '__manifest__.py').write_text("{'name': 'Alpha'}")
        (self.project_root / 'odoo/addons/alpha').symlink_to('../addons-store/caritel_v18/addons/alpha')

        with self.assertRaisesRegex(ValueError, 'alpha : Déjà fourni par addons-store/caritel_v18/addons.'):
            self.run_import(['alpha'])
        self.assertEqual((self.project_root / 'odoo/addons/alpha').resolve(), other.resolve())

    def test_tree_listing_follows_import_discovery_rules(self):
        tree = "\n".join([
            "100644 blob a\tsale_x/__manifest__.py",
            "100644 blob b\tsale_x/tests/fixture/__manifest__.py",
            "100644 blob c\taddons/stock_y/__openerp__.py",
            "100644 blob d\tnode_modules/pkg/__manifest__.py",
            "120000 blob e\tstock_y_link",
            "100644 blob f\tREADME.md",
        ])
        modules, symlinks = web.repository_tree_modules(tree, "client-addons")
        self.assertEqual(
            {("sale_x", "sale_x", "__manifest__.py"), ("addons/stock_y", "stock_y", "__openerp__.py")},
            set(modules),
        )
        self.assertEqual(["stock_y_link"], symlinks)

        single, _ = web.repository_tree_modules("100644 blob a\t__manifest__.py\n100644 blob b\tsub/__manifest__.py", "my_module")
        self.assertEqual([("", "my_module", "__manifest__.py")], single)

    def test_inspection_reads_only_manifests_and_plans_each_module(self):
        self.run_import(['alpha'])
        (self.storage('alpha') / '__manifest__.py').write_text("{'name': 'Alpha', 'version': '18.0.1.0.0'}")
        commands = []

        def fake_git(command, **kwargs):
            commands.append(command)
            if 'clone' in command:
                Path(command[-1]).mkdir(parents=True)
                return subprocess.CompletedProcess(command, 0, stdout='', stderr='')
            if 'rev-parse' in command:
                return subprocess.CompletedProcess(command, 0, stdout=COMMIT, stderr='')
            if 'ls-tree' in command:
                tree = "\n".join([
                    "100644 blob a\talpha/__manifest__.py",
                    "100644 blob b\tbeta/__manifest__.py",
                    "100644 blob c\tbad name/__manifest__.py",
                    "100644 blob d\told/__manifest__.py",
                ])
                return subprocess.CompletedProcess(command, 0, stdout=tree, stderr='')
            self.assertIn('core.sparseCheckout=true', command)
            checkout = Path(command[command.index('-C') + 1])
            for manifest, content in zip(
                (line.lstrip('/') for line in (checkout / '.git/info/sparse-checkout').read_text().splitlines()),
                ("{'version': '18.0.1.0.1'}", "{'name': 'Beta'}", "{}", "{'version': '16.0.1.0.0'}"),
            ):
                (checkout / manifest).parent.mkdir(parents=True, exist_ok=True)
                (checkout / manifest).write_text(content)
            return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

        with mock.patch.object(web.subprocess, 'run', side_effect=fake_git), \
                mock.patch.object(web, 'module_dirs', side_effect=AssertionError('scan du projet')):
            result = web.inspect_repository_modules(self.project, URL, 'DEV')

        clone = next(command for command in commands if 'clone' in command)
        self.assertIn('--filter=blob:none', clone)
        self.assertIn('--no-checkout', clone)
        self.assertEqual(COMMIT, result['commit'])
        self.assertTrue(result['manifests_read'])
        by_name = {module['name']: module for module in result['modules']}
        self.assertEqual(('update', '18.0.1.0.0', '18.0.1.0.1'),
                         (by_name['alpha']['action'], by_name['alpha']['current_version'], by_name['alpha']['version']))
        self.assertEqual('add', by_name['beta']['action'])
        self.assertEqual('blocked', by_name['bad name']['action'])
        self.assertIn('Odoo 16.0', by_name['old']['reason'])
        self.assertEqual(list(self.storage().iterdir()), [self.storage('alpha')])

    def test_sparse_checkout_pattern_matches_only_the_exact_manifest(self):
        self.assertEqual('/addons/x\\*y/__manifest__.py\n', web.sparse_checkout_pattern('addons/x*y/__manifest__.py'))
        self.assertEqual('/#odd/__manifest__.py\n', web.sparse_checkout_pattern('#odd/__manifest__.py'))

    def test_downgrade_is_allowed_with_a_warning(self):
        self.run_import(['alpha'])
        (self.storage('alpha') / '__manifest__.py').write_text("{'name': 'Alpha', 'version': '18.0.2.0.0'}")
        plans = web.repository_module_plans(
            self.project, [{'name': 'alpha', 'path': 'alpha', 'manifest': {'version': '18.0.1.0.1'}}], {}, '18.0')
        self.assertEqual('update', plans[0]['action'])
        self.assertIn('plus ancienne', plans[0]['warning'])

    def test_inspection_rejects_untrusted_repository_url(self):
        with self.assertRaises(ValueError):
            web.inspect_repository_modules(self.project, 'https://example.org/repo.git', 'main')

    def test_repository_import_never_scans_the_whole_project(self):
        # Sous Windows, module_dirs() lance wsl.exe : un import ne doit jamais en dépendre.
        with mock.patch.object(web, 'module_dirs', side_effect=AssertionError('scan du projet')):
            job = self.run_import(['alpha'])
        self.assertEqual(job.result['modules'], ['alpha'])
