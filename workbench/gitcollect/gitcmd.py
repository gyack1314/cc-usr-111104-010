"""受限的 Git plumbing 调用层。

安全约束：
- 隔离用户/系统/全局配置，禁用 URL 改写、外部 remote helper 与扩展协议；
- 重定向 hooksPath 到不存在的目录，仓库自带 hook 不会执行；
- 只通过 plumbing 命令读取对象，不检出工作树，不改动来源仓库的 ref；
- 抓取缓存为收集工具自建裸库，源仓库始终只读访问。
"""
import os
import subprocess

from .errors import CollectionError
from .identity import identity_to_location

_SAFE_ENV_VARS = (
    'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TMPDIR', 'SYSTEMROOT',
    'HOME',  # HOME 指向隔离目录，避免读取 ~/.gitconfig
)


def build_env(isolated_home):
    """构造最小化环境，丢弃所有 GIT_* 与凭据相关变量。"""
    env = {key: os.environ[key] for key in _SAFE_ENV_VARS if key in os.environ}
    env['HOME'] = str(isolated_home)
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GIT_CONFIG_NOSYSTEM'] = '1'
    env['GIT_CONFIG_GLOBAL'] = str(isolated_home / 'nonexistent-gitconfig')
    return env


# 抓取与解析阶段始终携带的安全配置
_SAFE_CONFIG = (
    '-c', 'core.hooksPath=/nonexistent/gitcollect-hooks',
    '-c', 'protocol.file.allow=user',
    '-c', 'protocol.http.allow=always',
    '-c', 'protocol.https.allow=always',
    '-c', 'protocol.ext.allow=never',
    '-c', 'protocol.allow=never',
    '-c', 'gc.auto=0',
    '-c', 'credential.helper=',
    '-c', 'credential.interactive=never',
    '-c', 'filter.lfs.smudge=',
    '-c', 'filter.lfs.clean=',
    '-c', 'filter.lfs.process=',
)


class GitRunner:
    def __init__(self, env):
        self._env = env

    def _run(self, args, input_bytes=None, check=True):
        proc = subprocess.run(
            ['git', *_SAFE_CONFIG, *args],
            input=input_bytes, capture_output=True, env=self._env,
        )
        if check and proc.returncode != 0:
            detail = proc.stderr.decode('utf-8', 'replace').strip()
            raise CollectionError(f'git 命令失败 {args[:2]}: {detail or "无错误输出"}')
        return proc

    def run_in(self, repo, args, input_bytes=None, check=True):
        return self._run(['-C', str(repo), *args], input_bytes, check)

    # ---- 来源仓库只读查询 ----
    def resolve_ref(self, identity, ref):
        """解析 ref 为完整 40 位 commit；本地库用 rev-parse，远程用 ls-remote。"""
        location = identity_to_location(identity)
        if identity.startswith('file://'):
            proc = self._run(
                ['-C', location, 'rev-parse', '--verify', '--quiet',
                 '--end-of-options', ref + '^{commit}'],
                check=False,
            )
            if proc.returncode != 0:
                raise CollectionError(f'无法在 {identity} 解析 ref {ref!r}')
            sha = proc.stdout.decode('ascii').strip()
        else:
            # 完整 SHA 直接采用并在后续抓取阶段验证；命名 ref 经 ls-remote 解析，
            # 不使用本地远程跟踪分支，确保看到的是来源当前公布的值。
            if len(ref) == 40 and all(c in '0123456789abcdef' for c in ref):
                sha = ref
            else:
                proc = self._run(['ls-remote', '--exit-code', location, ref], check=False)
                if proc.returncode != 0 or not proc.stdout:
                    raise CollectionError(f'无法在 {identity} 解析 ref {ref!r}')
                sha = proc.stdout.decode('ascii').split('\t', 1)[0].strip()
        if len(sha) != 40 or any(c not in '0123456789abcdef' for c in sha):
            raise CollectionError(f'解析结果不是完整40位commit: {sha!r}')
        return sha

    def batch_query(self, repo, oid):
        """通过 cat-file --batch 返回 (type, size, content)；对象缺失时返回 None。"""
        proc = self.run_in(repo, ['cat-file', '--batch'], input_bytes=(oid + '\n').encode('ascii'),
                           check=False)
        if proc.returncode != 0:
            raise CollectionError(f'cat-file --batch 失败: '
                                  f'{proc.stderr.decode("utf-8", "replace").strip()}')
        header, _, content = proc.stdout.partition(b'\n')
        parts = header.decode('ascii').split(' ')
        if parts[-1] == 'missing':
            return None
        _oid, type_name, size = parts
        content = content[:int(size)]
        return type_name, int(size), content

    def cat_object(self, repo, oid):
        query = self.batch_query(repo, oid)
        if query is None:
            raise CollectionError(f'对象 {oid} 缺失')
        return query[2]

    def object_type(self, repo, oid):
        query = self.batch_query(repo, oid)
        return None if query is None else query[0]

    def object_size(self, repo, oid):
        query = self.batch_query(repo, oid)
        if query is None:
            raise CollectionError(f'对象 {oid} 缺失')
        return query[1]

    def list_tree(self, repo, tree_id):
        """递归列出树条目：[(mode, kind, oid, path)]，path 为相对该树的 POSIX 路径。"""
        proc = self.run_in(repo, ['ls-tree', '-z', '-r', '-t', tree_id], check=False)
        if proc.returncode != 0:
            raise CollectionError(f'树对象 {tree_id} 不存在或无法读取')
        rows = []
        records = proc.stdout.split(b'\x00')
        for record in records:
            if not record:
                continue
            meta, path = record.split(b'\t', 1)
            mode, kind, oid = meta.decode('ascii').split(' ')
            rows.append((mode, kind, oid, path.decode('utf-8', 'surrogateescape')))
        return rows

    def read_gitmodules(self, repo, blob_id):
        """以 blob 为输入解析 .gitmodules，只接受 submodule.<name>.path/url。"""
        # 先列出全部键：submodule 段内任何非 path/url 属性都显式拒绝，
        # 防止 update=!<cmd> 等可执行配置被静默带入。
        all_proc = self.run_in(
            repo, ['config', '-z', '--blob', blob_id, '--get-regexp', r'^.+\..+$'],
            check=False,
        )
        if all_proc.returncode not in (0, 1):
            raise CollectionError('.gitmodules 解析失败')
        for record in all_proc.stdout.decode('utf-8', 'surrogateescape').split('\x00'):
            if not record:
                continue
            key = record.partition('\n')[0]
            parts = key.split('.')
            if len(parts) < 3 or parts[0] != 'submodule' or parts[-1] not in ('path', 'url'):
                raise CollectionError(f'.gitmodules 含不允许的属性: {key}')
        proc = self.run_in(
            repo,
            ['config', '-z', '--blob', blob_id, '--get-regexp', r'^submodule\..*\.(path|url)$'],
            check=False,
        )
        if proc.returncode not in (0, 1):
            raise CollectionError('.gitmodules 解析失败')
        text = proc.stdout.decode('utf-8', 'surrogateescape')
        records = {}
        order = []
        # git config -z --get-regexp 输出：每条记录为 "key\nvalue\0"
        for record in text.split('\x00'):
            if not record:
                continue
            key, sep, value = record.partition('\n')
            if not sep:
                raise CollectionError('.gitmodules 条目格式不合法')
            parts = key.split('.')
            # submodule.<name...>.path|url：name 本身可能含点
            if len(parts) < 3 or parts[0] != 'submodule' or parts[-1] not in ('path', 'url'):
                raise CollectionError(f'.gitmodules 含不允许的属性: {key}')
            name = '.'.join(parts[1:-1])
            if name not in records:
                records[name] = {}
                order.append(name)
            if parts[-1] in records[name]:
                raise CollectionError(f'.gitmodules 子模块 {name} 的 {parts[-1]} 重复声明')
            records[name][parts[-1]] = value
        return order, records

    # ---- 自建抓取缓存 ----
    def init_cache(self, cache_dir):
        cache_dir.mkdir(parents=True, exist_ok=True)
        if not (cache_dir / 'HEAD').exists():
            self._run(['init', '--bare', '-q', str(cache_dir)])
        self.run_in(cache_dir, ['config', 'gc.auto', '0'])
        return cache_dir

    def fetch_commit(self, cache_dir, identity, commit):
        """把指定 commit 所需对象抓取进缓存裸库；失败即缺对象。"""
        location = identity_to_location(identity)
        # 先抓全部 refs（浅历史仓库的 gitlink 提交往往只能经此获得）
        proc = self.run_in(
            cache_dir,
            ['fetch', '--no-tags', '--force', location,
             '+refs/heads/*:refs/remotes/collected/heads/*',
             '+refs/tags/*:refs/remotes/collected/tags/*'],
            check=False,
        )
        if self.object_type(cache_dir, commit) is not None:
            return
        # 再尝试直接按 SHA 抓取（允许 uploadpack 任意 SHA 的服务端）
        proc2 = self.run_in(
            cache_dir, ['fetch', '--no-tags', location, f'{commit}:refs/remotes/collected/sha/{commit}'],
            check=False,
        )
        if self.object_type(cache_dir, commit) is None:
            detail = (proc.stderr or proc2.stderr).decode('utf-8', 'replace').strip()
            raise CollectionError(f'缺少 gitlink 提交 {commit}（来源 {identity}）：{detail or "服务端未提供该对象"}')
