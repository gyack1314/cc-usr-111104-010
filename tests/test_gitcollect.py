"""固定提交递归收集工具的单元测试（全部基于本地合成裸库）。"""
import json
import tempfile
import unittest
from pathlib import Path

from workbench.gitcollect import CollectionError, Config, collect, verify_package
from workbench.gitcollect.demo import build_demo_repos
from workbench.gitcollect.gitobj import build_tree, object_id, parse_commit
from workbench.gitcollect.identity import (
    normalize_identity, resolve_submodule_url, safe_submodule_path,
)


class IdentityTests(unittest.TestCase):
    def test_normalize_file_and_http(self):
        self.assertEqual(normalize_identity('/tmp/a/../b.git'),
                         'file://' + str(Path('/tmp/b.git')))
        self.assertEqual(normalize_identity('http://Host.EXAMPLE:80/repo.git/'),
                         'http://host.example/repo.git/')
        self.assertEqual(normalize_identity('https://h:8443/a/./b.git/'),
                         'https://h:8443/a/b.git/')

    def test_reject_credentials_and_ext_protocol(self):
        for bad in ('http://u:p@h/repo.git', 'ext::ssh %h', 'ssh://h/repo.git',
                    'file://otherhost/tmp/x.git', 'http://h/a?x=1'):
            with self.assertRaises(CollectionError):
                normalize_identity(bad)

    def test_relative_url_git_semantics(self):
        parent = normalize_identity('/srv/repos/root.git')
        self.assertEqual(resolve_submodule_url(parent, '../middle.git'),
                         normalize_identity('/srv/repos/middle.git'))
        parent2 = normalize_identity('/srv/repos/group/deep.git')
        self.assertEqual(resolve_submodule_url(parent2, '../../leaf.git'),
                         normalize_identity('/srv/repos/leaf.git'))
        http_parent = 'http://127.0.0.1:9/repos/root.git'
        self.assertEqual(resolve_submodule_url(http_parent, '../leaf.git'),
                         'http://127.0.0.1:9/repos/leaf.git')
        with self.assertRaises(CollectionError):
            resolve_submodule_url(parent, 'ext::evil %h')

    def test_safe_submodule_path(self):
        self.assertEqual(safe_submodule_path('a/b'), 'a/b')
        self.assertEqual(safe_submodule_path('a/./b'), 'a/b')
        for bad in ('/abs', '../x', 'a/../../b', 'a\\b', ''):
            with self.assertRaises(CollectionError):
                safe_submodule_path(bad)


class GitObjectTests(unittest.TestCase):
    def test_tree_mode_wire_format(self):
        data = b'hello\n'
        blob = object_id('blob', data)
        tree = build_tree([('040000', blob, 'd'), ('100644', blob, 'f')])
        self.assertIn(b'40000 d\x00', tree)
        self.assertIn(b'100644 f\x00', tree)
        # 树排序：目录按 name/ 参与比较
        names = [line for line in tree.split(b'\x00') if b' ' in line]
        self.assertEqual(names[0], b'40000 d')

    def test_commit_parse(self):
        content = b'tree ' + b'a' * 40 + b'\nparent ' + b'b' * 40 + b'\n\nbody\n'
        info = parse_commit(content)
        self.assertEqual(info['tree'], 'a' * 40)
        self.assertEqual(info['parents'], ['b' * 40])


class CollectDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls._tmp.name)
        cls.catalog = build_demo_repos(cls.base / 'repos')
        cls.repos = cls.base / 'repos'
        names = ['root.git', 'middle.git', 'inner.git', 'group/deep.git', 'leaf.git',
                 'broken.git', 'decl-mismatch.git', 'decl-duplicate.git', 'budget.git']
        cls.whitelist = [normalize_identity(str(cls.repos / n)) for n in names]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _collect(self, repo_name, ref='main', **kw):
        out = tempfile.mkdtemp(dir=self.base)
        kw.setdefault('whitelist', self.whitelist)
        cfg = Config(**kw)
        return collect(str(self.repos / f'{repo_name}.git'), ref, cfg, out), Path(out)

    def test_ref_pinned_then_branch_moves(self):
        manifest, out = self._collect('root')
        self.assertEqual(manifest['root']['commit'], self.catalog['root_initial'])
        import subprocess
        subprocess.run(['git', '-C', str(self.repos / 'root.git'), 'update-ref',
                        'refs/heads/main', self.catalog['root_moved']], check=True)
        try:
            manifest2, _ = self._collect('root')
            self.assertEqual(manifest2['root']['commit'], self.catalog['root_moved'])
        finally:
            subprocess.run(['git', '-C', str(self.repos / 'root.git'), 'update-ref',
                            'refs/heads/main', self.catalog['root_initial']], check=True)
        # 旧资料包仍可离线核对，且内容不随后来的分支移动改变
        report = verify_package(out / 'package')
        self.assertTrue(report['ok'])
        self.assertEqual(manifest['root']['commit'], self.catalog['root_initial'])

    def test_shared_commit_keeps_every_path_instance(self):
        manifest, _ = self._collect('root')
        leaf_links = [e for e in manifest['entries']
                      if e['mode'] == '160000' and e['oid'] == self.catalog['leaf_commit']]
        self.assertEqual(len(leaf_links), 5)
        paths = {e['path'] for e in leaf_links}
        self.assertEqual(paths, {
            'a/sensor', 'b', 'shared/leaf-copy',
            'a/deps/inner/vendor/sensor', 'a/deps/deep/vendor/leaf'})
        # 复用对象不影响字节计数：同一 blob 仍按路径实例计入
        self.assertEqual(manifest['counts']['export_paths'], len(manifest['entries']))

    def test_offline_verify_rebuilds_all_trees(self):
        manifest, out = self._collect('root')
        report = verify_package(out / 'package')
        self.assertTrue(report['ok'])
        self.assertEqual(report['root_tree'], manifest['root']['tree'])
        self.assertGreater(report['trees_checked'], 0)
        # 核对不依赖来源：把来源目录临时改名后仍可核对
        hidden = self.repos.rename(self.repos.with_suffix('.hidden'))
        try:
            report2 = verify_package(out / 'package')
            self.assertTrue(report2['ok'])
        finally:
            hidden.rename(self.repos)

    def test_missing_gitlink_commit_fails_without_marker(self):
        out = Path(tempfile.mkdtemp(dir=self.base))
        with self.assertRaises(CollectionError):
            collect(str(self.repos / 'broken.git'), 'main',
                    Config(whitelist=self.whitelist), out)
        self.assertFalse((out / 'package' / 'COMPLETED').exists())

    def test_declaration_mismatch_and_duplicate_fail(self):
        for name in ('decl-mismatch', 'decl-duplicate'):
            with self.assertRaises(CollectionError):
                self._collect(name)

    def test_budgets_enforced(self):
        with self.assertRaises(CollectionError):
            self._collect('budget', max_bytes=256)
        with self.assertRaises(CollectionError):
            self._collect('root', max_paths=10)
        with self.assertRaises(CollectionError):
            self._collect('root', max_depth=2)

    def test_whitelist_enforced_after_relative_resolution(self):
        with self.assertRaises(CollectionError):
            self._collect('root', whitelist=[self.whitelist[0]])

    def test_symlink_lfs_and_executable_recorded_not_materialized(self):
        _manifest, out = self._collect('root')
        pkg = out / 'package'
        self.assertFalse(any(p.is_symlink() for p in pkg.rglob('*')))
        manifest = json.loads((pkg / 'manifest.json').read_text(encoding='utf-8'))
        link = next(e for e in manifest['entries'] if e['path'] == 'reference')
        self.assertEqual(link['symlink_target'], '../../outside-target')
        lfs = next(e for e in manifest['entries']
                   if e['path'] == 'a/sensor/data/map.bin')
        self.assertTrue(lfs['lfs_pointer'])
        run = next(e for e in manifest['entries'] if e['path'] == 'run.py')
        self.assertEqual(run['mode'], '100755')

    def test_resume_reuses_verified_objects(self):
        out = Path(tempfile.mkdtemp(dir=self.base))
        with self.assertRaises(CollectionError):
            collect(str(self.repos / 'budget.git'), 'main',
                    Config(whitelist=self.whitelist, max_bytes=256), out)
        self.assertFalse((out / 'package' / 'COMPLETED').exists())
        collect(str(self.repos / 'budget.git'), 'main',
                Config(whitelist=self.whitelist, max_bytes=4096), out)
        report = verify_package(out / 'package')
        self.assertTrue(report['ok'])

    def test_tampered_package_is_detected(self):
        _manifest, out = self._collect('leaf')
        pkg = out / 'package'
        manifest_path = pkg / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['root']['tree'] = 'f' * 40
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(CollectionError):
            verify_package(pkg)

    def test_tampered_object_content_is_detected(self):
        _manifest, out = self._collect('leaf')
        pkg = out / 'package'
        blob = next(p for p in (pkg / 'objects').iterdir())
        blob.write_bytes(b'tampered content that breaks sha256')
        with self.assertRaises((CollectionError, ValueError)):
            verify_package(pkg)


if __name__ == '__main__':
    unittest.main()
