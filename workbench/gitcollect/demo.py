"""本地合成 Git 仓库生成器与全场景演示（全部为虚构教学数据）。

覆盖：分支移动、共享子模块（同 commit 多路径）、嵌套相对地址、
缺失 gitlink 提交、符号链接/可执行位/LFS pointer、超预算分支、
重复与不匹配的 .gitmodules 声明、深度上限。
"""
import json
import os
import subprocess
from pathlib import Path

from .errors import CollectionError
from .identity import normalize_identity
from .collector import Config, collect
from .verify import verify_package

_COMMIT_ENV = {
    'GIT_AUTHOR_DATE': '2026-01-01T00:00:00+00:00',
    'GIT_COMMITTER_DATE': '2026-01-01T00:00:00+00:00',
}


class RepoBuilder:
    def __init__(self, path):
        self.path = Path(path)
        subprocess.run(['git', 'init', '--bare', '-q', str(self.path)], check=True)
        self.env = dict(os.environ)
        self.env.update(_COMMIT_ENV)

    def git(self, *args, input_bytes=None, check=True):
        proc = subprocess.run(['git', '-C', str(self.path), *args],
                              input=input_bytes, capture_output=True, env=self.env)
        if check and proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode('utf-8', 'replace'))
        return proc

    def blob(self, data):
        return self.git('hash-object', '-w', '--stdin', input_bytes=data).stdout.decode().strip()

    def tree(self, entries):
        """entries 为 (mode, kind, oid, name)，name 可含斜杠，自动构造嵌套子树。"""
        root = {}

        def place(parts, mode, kind, oid):
            node = root
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = (mode, kind, oid)

        for mode, kind, oid, name in entries:
            place(name.split('/'), mode, kind, oid)

        def build(node):
            flat = []
            for name, value in node.items():
                if isinstance(value, dict):
                    flat.append(('040000', 'tree', build(value), name))
                else:
                    mode, kind, oid = value
                    flat.append((mode, kind, oid, name))
            lines = ''.join(f'{mode} {kind} {oid}\t{name}\n'
                            for mode, kind, oid, name in
                            sorted(flat, key=lambda x: x[3] + ('/' if x[0] == '040000' else '')))
            return self.git('mktree', input_bytes=lines.encode()).stdout.decode().strip()

        return build(root)

    def subtree(self, entries):
        return ('040000', 'tree', self.tree(entries))

    def commit(self, tree_id, message, parents=()):
        args = ['-c', 'user.name=课程资料', '-c', 'user.email=course@local.invalid',
                'commit-tree', tree_id]
        for parent in parents:
            args += ['-p', parent]
        return self.git(*args, input_bytes=message.encode()).stdout.decode().strip()

    def ref(self, name, commit):
        self.git('update-ref', f'refs/heads/{name}', commit)


def _gitmodules(rows):
    out = []
    for name, path, url in rows:
        out.append(f'[submodule "{name}"]\n\tpath = {path}\n\turl = {url}\n')
    return ''.join(out).encode()


def build_demo_repos(root):
    root = Path(root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('生成目录必须为空')
    root.mkdir(parents=True)

    # leaf：含普通文件、可执行文件、符号链接、LFS pointer
    leaf = RepoBuilder(root / 'leaf.git')
    lfs = (b'version https://git-lfs.github.com/spec/v1\n'
           b'oid sha256:' + b'0' * 64 + b'\nsize 42\n')
    leaf_tree = leaf.tree([
        ('100644', 'blob', leaf.blob('合成传感器说明\n'.encode()), 'sensor.txt'),
        ('100755', 'blob', leaf.blob(b'#!/bin/sh\necho synthetic\n'), 'probe.sh'),
        ('120000', 'blob', leaf.blob(b'sensor.txt'), 'docs/link-to-sensor'),
        ('100644', 'blob', leaf.blob(lfs), 'data/map.bin'),
    ])
    leaf_commit = leaf.commit(leaf_tree, '叶资料')
    leaf.ref('main', leaf_commit)
    leaf_v2 = leaf.commit(
        leaf.tree([('100644', 'blob', leaf.blob(b'leaf second revision'), 'sensor.txt')]),
        '叶资料第二版', parents=(leaf_commit,))
    leaf.ref('future', leaf_v2)

    # inner：嵌套相对地址 ../leaf.git（与 leaf 同目录）
    inner = RepoBuilder(root / 'inner.git')
    inner_gm = _gitmodules([('sensor', 'vendor/sensor', '../leaf.git')])
    inner_tree = inner.tree([
        ('100644', 'blob', inner.blob(inner_gm), '.gitmodules'),
        ('160000', 'commit', leaf_commit, 'vendor/sensor'),
        ('100644', 'blob', inner.blob(b'inner service\n'), 'service.py'),
    ])
    inner_commit = inner.commit(inner_tree, '内层资料')
    inner.ref('main', inner_commit)

    # group/deep：位于子目录，用 ../../leaf.git 演示多层相对地址
    deep = RepoBuilder(root / 'group' / 'deep.git')
    deep_gm = _gitmodules([('sensor', 'vendor/leaf', '../../leaf.git')])
    deep_tree = deep.tree([
        ('100644', 'blob', deep.blob(deep_gm), '.gitmodules'),
        ('160000', 'commit', leaf_commit, 'vendor/leaf'),
        ('100644', 'blob', deep.blob(b'deep service\n'), 'deep.py'),
    ])
    deep_commit = deep.commit(deep_tree, '子目录内层资料')
    deep.ref('main', deep_commit)

    # middle：相对地址 ../inner.git、../group/deep.git 与 ../leaf.git，形成嵌套与共享
    middle = RepoBuilder(root / 'middle.git')
    middle_gm = _gitmodules([
        ('inner', 'deps/inner', '../inner.git'),
        ('deep', 'deps/deep', '../group/deep.git'),
        ('sensor', 'sensor', '../leaf.git'),
    ])
    middle_tree = middle.tree([
        ('100644', 'blob', middle.blob(middle_gm), '.gitmodules'),
        ('160000', 'commit', inner_commit, 'deps/inner'),
        ('160000', 'commit', deep_commit, 'deps/deep'),
        ('160000', 'commit', leaf_commit, 'sensor'),
        ('100644', 'blob', middle.blob(b'middle glue code\n'), 'glue.py'),
    ])
    middle_commit = middle.commit(middle_tree, '中间资料')
    middle.ref('main', middle_commit)

    # root：两个路径复用同一 leaf commit（合法共享，路径不合并且各自计数）
    top = RepoBuilder(root / 'root.git')
    top_gm = _gitmodules([
        ('a', 'a', '../middle.git'),
        ('b', 'b', '../leaf.git'),
        ('c', 'shared/leaf-copy', '../leaf.git'),
    ])
    initial_tree = top.tree([
        ('100644', 'blob', top.blob(top_gm), '.gitmodules'),
        ('160000', 'commit', middle_commit, 'a'),
        ('160000', 'commit', leaf_commit, 'b'),
        ('160000', 'commit', leaf_commit, 'shared/leaf-copy'),
        ('120000', 'blob', top.blob(b'../../outside-target'), 'reference'),
        ('100755', 'blob', top.blob(b'print("synthetic")\n'), 'run.py'),
        ('100644', 'blob', top.blob(b'robot project notes\n'), 'README.md'),
    ])
    initial = top.commit(initial_tree, '根资料')
    moved = top.commit(
        top.tree([('100644', 'blob', top.blob(b'new revision after branch move\n'), 'new.txt')]),
        '分支移动后的资料', parents=(initial,))
    top.ref('main', initial)
    top.ref('advanced', moved)

    # broken：gitlink 指向任何来源都不存在的 commit
    missing_sha = '0123456789abcdef0123456789abcdef01234567'
    broken = RepoBuilder(root / 'broken.git')
    broken_gm = _gitmodules([('gone', 'gone', '../leaf.git')])
    broken_tree = broken.tree([
        ('100644', 'blob', broken.blob(broken_gm), '.gitmodules'),
        ('160000', 'commit', missing_sha, 'gone'),
    ])
    broken_commit = broken.commit(broken_tree, '缺失 gitlink 提交的资料')
    broken.ref('main', broken_commit)

    # decl-mismatch：.gitmodules 声明的路径在树里不是 gitlink
    bad1 = RepoBuilder(root / 'decl-mismatch.git')
    bad1_gm = _gitmodules([('ghost', 'ghost', '../leaf.git')])
    bad1_tree = bad1.tree([
        ('100644', 'blob', bad1.blob(bad1_gm), '.gitmodules'),
        ('100644', 'blob', bad1.blob(b'ordinary file instead of gitlink\n'), 'ghost'),
    ])
    bad1_commit = bad1.commit(bad1_tree, '声明不匹配')
    bad1.ref('main', bad1_commit)

    # duplicate：同一 path 重复声明
    bad2 = RepoBuilder(root / 'decl-duplicate.git')
    bad2_gm = (_gitmodules([('one', 'same', '../leaf.git')])
               + _gitmodules([('two', 'same', '../leaf.git')]))
    bad2_tree = bad2.tree([
        ('100644', 'blob', bad2.blob(bad2_gm), '.gitmodules'),
        ('160000', 'commit', leaf_commit, 'same'),
    ])
    bad2_commit = bad2.commit(bad2_tree, '重复声明')
    bad2.ref('main', bad2_commit)

    # budget：含超过小字节预算的分支
    budget = RepoBuilder(root / 'budget.git')
    big = b'x' * 400
    budget_tree = budget.tree([
        ('100644', 'blob', budget.blob(big), 'payload.bin'),
        ('100644', 'blob', budget.blob(b'small\n'), 'note.txt'),
    ])
    budget_commit = budget.commit(budget_tree, '超预算资料')
    budget.ref('main', budget_commit)

    catalog = {
        'root': str(top.path), 'root_initial': initial, 'root_moved': moved,
        'middle': str(middle.path), 'middle_commit': middle_commit,
        'inner': str(inner.path), 'inner_commit': inner_commit,
        'deep': str(deep.path), 'deep_commit': deep_commit,
        'leaf': str(leaf.path), 'leaf_commit': leaf_commit, 'leaf_v2': leaf_v2,
        'broken': str(broken.path), 'broken_commit': broken_commit, 'missing_sha': missing_sha,
        'decl_mismatch': str(bad1.path), 'decl_mismatch_commit': bad1_commit,
        'decl_duplicate': str(bad2.path), 'decl_duplicate_commit': bad2_commit,
        'budget': str(budget.path), 'budget_commit': budget_commit,
    }
    (root / 'catalog.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2),
                                        encoding='utf-8')
    return catalog


def _whitelist(root, base_url=None):
    names = ['root.git', 'middle.git', 'inner.git', 'group/deep.git', 'leaf.git',
             'broken.git', 'decl-mismatch.git', 'decl-duplicate.git', 'budget.git']
    if base_url:
        return [f'{base_url.rstrip("/")}/{n}' for n in names]
    return [normalize_identity(str(root / n)) for n in names]


def run_demo(repos_dir, output_dir, base_url=None):
    repos = Path(repos_dir).resolve()
    catalog = json.loads((repos / 'catalog.json').read_text(encoding='utf-8'))
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    whitelist = _whitelist(repos, base_url)

    def loc(name):
        if base_url:
            return f'{base_url.rstrip("/")}/{name}.git'
        return str(repos / f'{name}.git')

    results = {'scenarios': []}

    def record(name, ok, detail):
        results['scenarios'].append({'scenario': name, 'ok': ok, 'detail': detail})

    def write_entries(fname, rows):
        p = output / fname
        p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        return p

    def write_config(fname, **kw):
        data = {'whitelist': whitelist, **kw}
        p = output / fname
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        return p

    # 1) 分支移动：先固定 initial，再推进 main，资料包内容不随 ref 改变
    cfg = write_config('cfg-standard.json')
    entries = write_entries('entries-root.json', [{'location': loc('root'), 'ref': 'main'}])
    pkg1 = output / 'pkg-before-move'
    manifest1 = collect(str(repos / 'root.git') if not base_url else loc('root'),
                        'main', Config(whitelist=whitelist), pkg1)
    subprocess.run(['git', '-C', str(repos / 'root.git'), 'update-ref',
                    'refs/heads/main', catalog['root_moved']], check=True)
    try:
        pkg2 = output / 'pkg-after-move'
        manifest2 = collect(str(repos / 'root.git') if not base_url else loc('root'),
                            'main', Config(whitelist=whitelist), pkg2)
    finally:
        # 恢复来源 ref，演示本身不长期改动来源状态
        subprocess.run(['git', '-C', str(repos / 'root.git'), 'update-ref',
                        'refs/heads/main', catalog['root_initial']], check=True)
    moved_independence = (
        manifest1['root']['commit'] == catalog['root_initial']
        and manifest2['root']['commit'] == catalog['root_moved']
    )
    # pkg1 离线核对仍指向 initial，与来源分支现状无关
    report1 = verify_package(pkg1 / 'package')
    record('branch-move', moved_independence and report1['ok'],
           {'fixed_commit': manifest1['root']['commit'],
            'later_main': manifest2['root']['commit'],
            'offline_verify': report1['ok']})

    # 2) 共享子模块 + 嵌套相对地址：leaf commit 出现在 5 个路径实例
    shared_paths = [e['path'] for e in manifest1['entries']
                    if e['mode'] == '160000' and e['oid'] == catalog['leaf_commit']]
    nested = next((n for n in manifest1['submodule_lock']
                   if n['path'] == 'a/deps/inner/vendor/sensor'), None)
    deep_nested = next((n for n in manifest1['submodule_lock']
                        if n['path'] == 'a/deps/deep/vendor/leaf'), None)
    record('shared-and-nested',
           len(shared_paths) == 5 and nested is not None
           and nested['resolved_repo'].endswith('leaf.git')
           and nested['commit'] == catalog['leaf_commit']
           and deep_nested is not None
           and deep_nested['resolved_repo'].endswith('leaf.git'),
           {'leaf_gitlink_paths': sorted(shared_paths),
            'nested_relative_resolution': nested,
            'multilevel_relative_resolution': deep_nested})

    # 3) 链接/可执行/LFS 仅落清单与内容，不创建软链接
    link_entry = next(e for e in manifest1['entries'] if e['path'].endswith('link-to-sensor'))
    lfs_entry = next(e for e in manifest1['entries'] if e['path'].endswith('map.bin'))
    exec_entry = next(e for e in manifest1['entries'] if e['path'].endswith('probe.sh'))
    no_real_symlink = not any(p.is_symlink() for p in (pkg1 / 'package').rglob('*'))
    record('links-modes-lfs',
           link_entry['mode'] == '120000' and link_entry['symlink_target'] == 'sensor.txt'
           and lfs_entry.get('lfs_pointer') is True and exec_entry['mode'] == '100755'
           and no_real_symlink,
           {'symlink': link_entry['path'], 'lfs': lfs_entry['path'],
            'executable': exec_entry['path']})

    # 4) 缺失 gitlink 提交：失败且无成功标记
    fail_out = output / 'pkg-broken'
    try:
        collect(loc('broken'), 'main', Config(whitelist=whitelist), fail_out)
        record('missing-gitlink', False, {'error': '应当失败却成功'})
    except CollectionError as exc:
        no_marker = not (fail_out / 'package' / 'COMPLETED').exists()
        record('missing-gitlink', no_marker, {'error': str(exc), 'no_success_marker': no_marker})

    # 5) 超预算分支：字节预算失败
    budget_out = output / 'pkg-budget'
    try:
        collect(loc('budget'), 'main', Config(whitelist=whitelist, max_bytes=256), budget_out)
        record('over-budget', False, {'error': '应当失败却成功'})
    except CollectionError as exc:
        record('over-budget', '字节预算' in str(exc), {'error': str(exc)})

    # 6) 声明不匹配与重复声明
    for label, repo_name in (('decl-mismatch', 'decl-mismatch'), ('decl-duplicate', 'decl-duplicate')):
        out = output / f'pkg-{label}'
        try:
            collect(loc(repo_name), 'main', Config(whitelist=whitelist), out)
            record(label, False, {'error': '应当失败却成功'})
        except CollectionError as exc:
            record(label, True, {'error': str(exc)})

    # 7) 深度上限：root->middle->inner->leaf 需要深度 3
    shallow_out = output / 'pkg-depth'
    try:
        collect(loc('root'), 'main', Config(whitelist=whitelist, max_depth=2), shallow_out)
        record('depth-limit', False, {'error': '应当失败却成功'})
    except CollectionError as exc:
        record('depth-limit', '深度' in str(exc), {'error': str(exc)})

    # 8) 白名单外的子模块来源必须失败
    outside = output / 'pkg-not-whitelisted'
    try:
        collect(loc('root'), 'main', Config(whitelist=whitelist[:1]), outside)
        record('whitelist', False, {'error': '应当失败却成功'})
    except CollectionError as exc:
        record('whitelist', '白名单' in str(exc), {'error': str(exc)})

    # 9) 半成品重开：失败目录再次采集可复用已验证对象，成功后离线核对通过
    retry_out = output / 'pkg-retry'
    try:
        collect(loc('budget'), 'main', Config(whitelist=whitelist, max_bytes=256), retry_out)
    except CollectionError:
        pass
    manifest_retry = collect(loc('budget'), 'main', Config(whitelist=whitelist, max_bytes=2048), retry_out)
    report_retry = verify_package(retry_out / 'package')
    record('resume-after-abort', report_retry['ok'],
           {'reused_work_dir': True, 'verify': report_retry['ok'],
            'content_bytes': manifest_retry['counts']['content_bytes']})

    results['ok'] = all(s['ok'] for s in results['scenarios'])
    results['package_under_test'] = str(pkg1 / 'package')
    (output / 'demo-result.json').write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    return results
