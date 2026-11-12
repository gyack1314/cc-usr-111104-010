"""离线核对：不调用 git、不访问来源，只用资料包内容重算 Git 对象身份。"""
import json
from pathlib import Path

from .errors import CollectionError
from .gitobj import object_id, build_tree
from .collector import MANIFEST_NAME, SUCCESS_MARKER, TREE_MODE, GITLINK_MODE, SYMLINK_MODE
from .gitobj import parse_commit, is_lfs_pointer


def verify_package(package_dir):
    """核对资料包，返回报告 dict；任何不一致抛出 CollectionError。"""
    package = Path(package_dir)
    if not package.is_dir():
        raise CollectionError(f'资料包目录不存在: {package}')
    if not (package / SUCCESS_MARKER).exists():
        raise CollectionError('缺少成功标记，资料包不是正式完成状态（可能为中止的半成品）')
    manifest = json.loads((package / MANIFEST_NAME).read_text(encoding='utf-8'))
    objects = package / 'objects'

    report = {'ok': False, 'problems': [], 'blobs_checked': 0, 'trees_checked': 0, 'commits_checked': 0}

    def fail(message):
        report['problems'].append(message)
        raise CollectionError(message)

    entries = manifest.get('entries', [])
    paths = [e['path'] for e in entries]
    if len(paths) != len(set(paths)):
        fail('清单中存在重复导出路径')

    # ---- 1) 逐 blob 重算 Git 对象 ID，校验模式、链接目标与 LFS 标注 ----
    blob_by_oid = {}
    for entry in entries:
        if entry['type'] != 'blob':
            continue
        data = (objects / entry['store_key']).read_bytes()
        if object_id('blob', data) != entry['oid']:
            fail(f'{entry["path"]}: 重算 blob 对象 ID 与清单不一致')
        if len(data) != entry['size']:
            fail(f'{entry["path"]}: 字节数与清单不一致')
        previous = blob_by_oid.setdefault(entry['oid'], data)
        if previous != data:
            fail(f'{entry["path"]}: 同一对象 ID 的内容在不同路径不一致')
        if entry['mode'] == SYMLINK_MODE:
            target = data.decode('utf-8', 'surrogateescape')
            if target != entry.get('symlink_target'):
                fail(f'{entry["path"]}: 符号链接目标与清单记录不一致')
        if is_lfs_pointer(data) != bool(entry.get('lfs_pointer')):
            fail(f'{entry["path"]}: LFS pointer 标注与实际内容不一致')
        report['blobs_checked'] += 1

    # ---- 2) 按导出前缀（= gitlink 路径，根仓为 ''）分组，重建每个真实 Git 树 ----
    lock = manifest.get('submodule_lock', [])
    lock_by_path = {n['path']: n for n in lock}
    lock_paths = sorted(lock_by_path, key=lambda p: p.count('/'))
    prefixes = [''] + lock_paths

    def owning_prefix(path):
        """返回路径所属的最近 gitlink 前缀（根仓为 ''）。

        gitlink 条目本身归其父组（沿路径前缀匹配，排除与自身完全相等的锁路径）。
        """
        owner = ''
        for lp in lock_paths:
            if path.startswith(lp + '/') and len(lp) > len(owner):
                owner = lp
        return owner

    def group_entries(prefix):
        base = '' if prefix == '' else prefix + '/'
        return [(p[len(base):], e) for p, e in ((x['path'], x) for x in entries)
                if p.startswith(base) and owning_prefix(p) == prefix]

    tree_oid_by_prefix = {}
    for prefix in prefixes:
        group = group_entries(prefix)
        own_paths = {rest for rest, _ in group}
        # 该组内每个条目的父目录都应存在真实子树条目；
        # gitlink 在本组内视为叶子（其展开内容属于更深前缀的组）。
        parent_dirs = set()
        for rest, entry in group:
            if entry['mode'] == GITLINK_MODE:
                leaf = rest
            else:
                leaf = rest
            parts = leaf.split('/')
            for i in range(1, len(parts)):
                parent_dirs.add('/'.join(parts[:i]))
        if not parent_dirs.issubset(own_paths):
            fail(f'前缀 {prefix or "/"} 缺少子树条目: {sorted(parent_dirs - own_paths)}')
        extra_trees = {rest for rest, e in group if e['mode'] == TREE_MODE} - parent_dirs
        if extra_trees:
            fail(f'前缀 {prefix or "/"} 存在多余子树条目: {sorted(extra_trees)}')

        # 自底向上重建
        all_dirs = parent_dirs | {''}
        rebuilt = {}
        for dir_path in sorted(all_dirs, key=lambda p: p.count('/'), reverse=True):
            dprefix = '' if dir_path == '' else dir_path + '/'
            children = {}
            for rest, entry in group:
                if not rest.startswith(dprefix):
                    continue
                tail = rest[len(dprefix):]
                if '/' in tail:
                    name = tail.split('/', 1)[0]
                    if name in children:
                        continue
                    child_full = dprefix + name
                    children[name] = (TREE_MODE, rebuilt[child_full])
                else:
                    children[tail] = (entry['mode'], entry['oid'])
            tree_bytes = build_tree([(m, o, n) for n, (m, o) in children.items()])
            rebuilt[dir_path] = object_id('tree', tree_bytes)
            report['trees_checked'] += 1
            if dir_path != '':
                declared = next(e for rest, e in group if rest == dir_path)
                if declared['oid'] != rebuilt[dir_path]:
                    fail(f'子树 {prefix + "/" + dir_path if prefix else dir_path} 重算对象 ID 不一致')
        tree_oid_by_prefix[prefix] = rebuilt['']

    # ---- 3) commit 对象：重算 ID，并校验 tree 指向与锁图 ----
    commit_by_id = {}
    for item in manifest.get('commits', []):
        data = (objects / item['store_key']).read_bytes()
        if object_id('commit', data) != item['commit']:
            fail(f'提交 {item["commit"]} 重算对象 ID 不一致')
        info = parse_commit(data)
        commit_by_id[item['commit']] = (item['repo'], info)
        report['commits_checked'] += 1

    root_commit = manifest['root']['commit']
    if root_commit not in commit_by_id:
        fail('根 commit 原始对象未保存在资料包中')
    root_repo, root_info = commit_by_id[root_commit]
    if root_info.get('tree') != manifest['root']['tree']:
        fail('根 commit 记录的 tree 与清单不一致')
    if tree_oid_by_prefix[''] != manifest['root']['tree']:
        fail('根树重算对象 ID 与清单记录的根 tree 不一致')
    report['root_tree'] = tree_oid_by_prefix['']

    # ---- 4) 子模块锁定图：gitlink 与锁节点一一对应，commit/tree 可离线核对 ----
    gitlinks = {e['path']: e for e in entries if e['mode'] == GITLINK_MODE}
    if set(gitlinks) != set(lock_by_path):
        fail(f'gitlink 与锁定图路径集合不一致: 缺少 {sorted(set(gitlinks) - set(lock_by_path))} '
             f'多余 {sorted(set(lock_by_path) - set(gitlinks))}')
    for path, node in lock_by_path.items():
        link = gitlinks[path]
        if link['oid'] != node['commit']:
            fail(f'{path}: 锁图 commit 与 gitlink 对象 ID 不一致')
        if link.get('source') != node['resolved_repo']:
            fail(f'{path}: 锁图来源仓库与清单不一致')
        if node['commit'] not in commit_by_id:
            fail(f'{path}: 锁定的子模块 commit 原始对象缺失，无法离线核对')
            continue
        repo, info = commit_by_id[node['commit']]
        if repo != node['resolved_repo']:
            fail(f'{path}: commit 保存的来源身份与锁图不一致')
        if info.get('tree') != node['tree']:
            fail(f'{path}: 锁图 tree 与 commit 对象内 tree 不一致')
        if tree_oid_by_prefix[path] != node['tree']:
            fail(f'{path}: 子模块树重算对象 ID 与锁定 tree 不一致')

    # ---- 5) 锁图层级：根级锁节点 depth=1 且无父路径，其余父路径必须是 gitlink ----
    for node in lock:
        if node['depth'] == 1:
            if node['parent_path'] is not None:
                fail(f'{node["path"]}: 根级锁节点不应有父路径')
        else:
            if node['parent_path'] not in gitlinks:
                fail(f'{node["path"]}: 父路径 {node["parent_path"]} 不是 gitlink')

    # ---- 6) 预算计数复核（共享对象按路径实例计数，不因复用漏算）----
    total_paths = len(entries)
    total_bytes = sum(e.get('size', 0) for e in entries if e['type'] == 'blob')
    if total_paths != manifest['counts']['export_paths']:
        fail('导出路径计数复核不一致')
    if total_bytes != manifest['counts']['content_bytes']:
        fail('内容字节计数复核不一致')
    limits = manifest['limits']
    if total_paths > limits['max_paths'] or total_bytes > limits['max_bytes']:
        fail('资料包超出其记录的预算')

    report['ok'] = True
    return report
