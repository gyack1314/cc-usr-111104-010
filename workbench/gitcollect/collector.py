"""固定提交递归收集器：以入口解析出的 commit 树为唯一事实来源。"""
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import CollectionError
from .gitcmd import GitRunner, build_env
from .gitobj import object_id, is_lfs_pointer, parse_commit
from .identity import (
    normalize_identity, resolve_submodule_url, safe_join, safe_submodule_path,
)
from ..raw_store import save_bytes

MANIFEST_NAME = 'manifest.json'
SUCCESS_MARKER = 'COMPLETED'
MANIFEST_VERSION = 1
TREE_MODE = '040000'
GITLINK_MODE = '160000'
SYMLINK_MODE = '120000'


@dataclass
class Config:
    whitelist: list
    max_depth: int = 8
    max_paths: int = 10000
    max_bytes: int = 100 * 1024 * 1024
    work_dir: str | None = None

    def normalized_whitelist(self):
        return [normalize_identity(item) for item in self.whitelist]


def _atomic_write(path, data, mode='wb'):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.part-', dir=str(path.parent))
    try:
        with os.fdopen(fd, mode) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class _ObjectStore:
    """sha256 内容存储；复用对象时由 raw_store 重新核对，不信任任何半成品。"""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def store(self, data):
        return save_bytes(self.root, data)


def collect(location, ref, config, output_dir):
    """执行一次采集。成功返回 manifest dict；任何异常都不留下成功标记。"""
    if not isinstance(config, Config):
        raise CollectionError('config 必须是 Config 实例')
    if config.max_depth < 0 or config.max_paths <= 0 or config.max_bytes < 0:
        raise CollectionError('深度/路径/字节预算配置不合法')
    whitelist = config.normalized_whitelist()
    root_identity = normalize_identity(location)
    if root_identity not in whitelist:
        raise CollectionError(f'入口仓库不在白名单内: {root_identity}')

    output = Path(output_dir).resolve()
    work = Path(config.work_dir).resolve() if config.work_dir else output.parent / (output.name + '.work')
    if (output / 'package' / SUCCESS_MARKER).exists():
        raise CollectionError(f'输出目录已包含成功资料包: {output / "package"}')
    # 输出目录不得位于任何白名单来源仓库内，避免污染来源
    for identity in whitelist:
        source_path = identity[7:] if identity.startswith('file://') else None
        if source_path and str(output).startswith(source_path.rstrip('/') + '/'):
            raise CollectionError('输出目录不得位于来源仓库内部')
    output.mkdir(parents=True, exist_ok=True)
    staging = work / 'staging'
    # 上次中止可能留下未完成的 staging（无成功标记）；对象缓存保留复用，
    # 但 staging 必须清空重写，绝不能信任其中的半成品。
    if staging.exists():
        shutil.rmtree(staging)
    cache_root = work / 'git-cache'
    isolated_home = work / 'home'
    isolated_home.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)
    runner = GitRunner(build_env(isolated_home))

    obj_store = _ObjectStore(staging / 'objects')
    # 已抓取并验证过的 (identity, commit)，仅用于复用对象获取；路径实例不合并
    fetched_commits = {}
    # 每个身份的缓存裸库
    cache_dirs = {}
    path_count = 0
    byte_count = 0
    entries = []
    lock_nodes = {}

    def cache_for(identity):
        if identity not in cache_dirs:
            digest = object_id('blob', identity.encode('utf-8'))
            target = cache_root / digest
            runner.init_cache(target)
            cache_dirs[identity] = target
        return cache_dirs[identity]

    def materialize_commit(identity, commit):
        """确保 commit 及其树对象已在缓存中，并返回 (cache_dir, tree_id, commit_bytes)。"""
        key = (identity, commit)
        if key in fetched_commits:
            return fetched_commits[key]
        cache = cache_for(identity)
        runner.fetch_commit(cache, identity, commit)
        if runner.object_type(cache, commit) != 'commit':
            raise CollectionError(f'{commit} 在 {identity} 中不是 commit 对象')
        commit_bytes = runner.cat_object(cache, commit)
        if object_id('commit', commit_bytes) != commit:
            raise CollectionError(f'提交对象哈希与记录的 gitlink 不一致: {commit}')
        info = parse_commit(commit_bytes)
        tree_id = info.get('tree')
        if not tree_id or runner.object_type(cache, tree_id) != 'tree':
            raise CollectionError(f'提交 {commit} 缺少可读取的树对象')
        fetched_commits[key] = (cache, tree_id, commit_bytes)
        return cache, tree_id, commit_bytes

    def charge_path():
        nonlocal path_count
        path_count += 1
        if path_count > config.max_paths:
            raise CollectionError(f'超出总路径数预算 {config.max_paths}')

    root_commit = runner.resolve_ref(root_identity, ref)
    stack = [{
        'identity': root_identity, 'commit': root_commit, 'prefix': '',
        'depth': 0, 'parent_path': None,
    }]
    root_tree_id = None

    while stack:
        frame = stack.pop()
        identity = frame['identity']
        commit = frame['commit']
        prefix = frame['prefix']
        depth = frame['depth']
        cache, tree_id, _commit_bytes = materialize_commit(identity, commit)
        if depth == 0:
            root_tree_id = tree_id

        rows = runner.list_tree(cache, tree_id)
        # 收集该树内的 .gitmodules 声明（可能位于子目录，按 Git 语义其 path 相对仓库根）
        declarations = []
        declared_paths = set()
        for mode, kind, oid, row_path in rows:
            if row_path == '.gitmodules' or row_path.endswith('/.gitmodules'):
                if kind != 'blob' or mode not in ('100644', '100755'):
                    raise CollectionError(f'{identity}:{commit} 的 .gitmodules 不是普通文件')
                order, records = runner.read_gitmodules(cache, oid)
                for name in order:
                    rec = records[name]
                    if 'path' not in rec or 'url' not in rec:
                        raise CollectionError(f'.gitmodules 子模块 {name} 缺少 path 或 url 声明')
                    norm_path = safe_submodule_path(rec['path'])
                    if norm_path in declared_paths:
                        raise CollectionError(f'.gitmodules 重复声明子模块路径: {norm_path}')
                    declared_paths.add(norm_path)
                    declarations.append({
                        'name': name, 'path': norm_path, 'url': rec['url'].strip(),
                        'gitmodules_path': row_path,
                    })
        declaration_by_path = {d['path']: d for d in declarations}

        # 该帧内真实子树（040000）登记路径预算并保留 Git 树对象 ID；
        # gitlink 前缀目录属于导出结构而非真实 Git 树，不伪造树条目，
        # 核对器重建 Git 树时把 gitlink 视为叶子、不穿透其前缀。
        subtree_oids = {p: o for mode, kind, o, p in rows if mode == TREE_MODE}
        for sp in sorted(subtree_oids):
            charge_path()
            entries.append({
                'path': safe_join(prefix, sp), 'mode': TREE_MODE, 'type': 'tree',
                'oid': subtree_oids[sp],
            })

        for mode, kind, oid, row_path in rows:
            export_path = safe_join(prefix, row_path)
            if mode == TREE_MODE:
                continue
            if mode == GITLINK_MODE:
                if kind != 'commit':
                    raise CollectionError(f'{export_path} 标记为 gitlink 但对象类型为 {kind}')
                charge_path()
                decl = declaration_by_path.get(row_path)
                if decl is None:
                    raise CollectionError(f'gitlink {row_path} 缺少 .gitmodules 路径声明')
                child_identity = resolve_submodule_url(identity, decl['url'])
                if child_identity not in whitelist:
                    raise CollectionError(f'子模块解析后不在白名单内: {child_identity}（声明于 {row_path}）')
                if depth + 1 > config.max_depth:
                    raise CollectionError(f'子模块嵌套深度超过上限 {config.max_depth}: {export_path}')
                # 精确取得记录的 commit，绝不跟随子模块默认分支
                materialize_commit(child_identity, oid)
                child_cache = cache_dirs[child_identity]
                child_commit_bytes = runner.cat_object(child_cache, oid)
                child_info = parse_commit(child_commit_bytes)
                entries.append({
                    'path': export_path, 'mode': GITLINK_MODE, 'type': 'gitlink',
                    'oid': oid, 'source': child_identity,
                })
                lock_nodes[export_path] = {
                    'path': export_path, 'parent_repo': identity,
                    'declared_url': decl['url'], 'resolved_repo': child_identity,
                    'commit': oid, 'tree': child_info['tree'],
                    'depth': depth + 1, 'parent_path': frame['parent_path'],
                }
                stack.append({
                    'identity': child_identity, 'commit': oid, 'prefix': export_path,
                    'depth': depth + 1, 'parent_path': export_path,
                })
                continue

            if kind != 'blob':
                raise CollectionError(f'{export_path} 的对象类型 {kind} 不受支持')
            if mode not in ('100644', '100755', SYMLINK_MODE):
                raise CollectionError(f'{export_path} 的文件模式 {mode} 不受支持')
            charge_path()
            data = runner.cat_object(cache, oid)
            actual_oid = object_id('blob', data)
            if actual_oid != oid:
                raise CollectionError(f'{export_path} 重算对象 ID 与树记录不一致: {actual_oid} != {oid}')
            byte_count += len(data)
            if byte_count > config.max_bytes:
                raise CollectionError(f'超出内容字节预算 {config.max_bytes}')
            store_key = obj_store.store(data)
            entry = {
                'path': export_path, 'mode': mode, 'type': 'blob', 'oid': oid,
                'size': len(data), 'sha256': store_key, 'store_key': store_key,
            }
            if mode == SYMLINK_MODE:
                target = data.decode('utf-8', 'surrogateescape')
                if '\x00' in target:
                    raise CollectionError(f'符号链接目标含 NUL: {export_path}')
                entry['symlink_target'] = target
            if is_lfs_pointer(data):
                entry['lfs_pointer'] = True
            entries.append(entry)

        # 未被树使用的声明即声明不匹配（路径上不是 gitlink）
        linked = {p for m, k, o, p in rows if m == GITLINK_MODE}
        for decl in declarations:
            if decl['path'] not in linked:
                raise CollectionError(
                    f'.gitmodules 声明了 {decl["path"]} 但树中该路径不是 gitlink（声明不匹配/重复声明）')

    if root_tree_id is None:
        raise CollectionError('未能取得根提交树')

    # 锁图中的父子连接（同一 commit 出现在多路径时各自保留节点）
    lock_graph = sorted(lock_nodes.values(), key=lambda n: n['path'])

    # 保存根 commit 及全部接触过的 commit 原始对象，供离线核对
    stored_commits = []
    for (identity, commit), (_cache, _tree, commit_bytes) in sorted(fetched_commits.items()):
        key = obj_store.store(commit_bytes)
        stored_commits.append({'repo': identity, 'commit': commit, 'store_key': key})

    manifest = {
        'manifest_version': MANIFEST_VERSION,
        'root': {
            'source': root_identity, 'requested_ref': ref,
            'commit': root_commit, 'tree': root_tree_id,
        },
        'limits': {
            'max_depth': config.max_depth,
            'max_paths': config.max_paths,
            'max_bytes': config.max_bytes,
        },
        'counts': {
            'export_paths': path_count,
            'content_bytes': byte_count,
            'entries': len(entries),
        },
        'whitelist': whitelist,
        'entries': sorted(entries, key=lambda e: e['path']),
        'submodule_lock': lock_graph,
        'commits': stored_commits,
    }

    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
    _atomic_write(staging / MANIFEST_NAME, manifest_bytes)
    # 标记最后写入：此前任何中止都不会产生正式成功包
    _atomic_write(staging / SUCCESS_MARKER, b'ok\n')
    os.replace(staging, output / 'package')
    return manifest
