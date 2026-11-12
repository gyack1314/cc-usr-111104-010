"""仓库身份规范化、白名单校验、相对子模块地址解析与路径安全。

规则与 docs/source-format.md 对齐：HTTP/HTTPS 去默认端口并清理点路径，
本地路径取绝对规范路径；白名单比较规范化身份而非字符串前缀；
相对子模块地址按 Git 仓库相对语义解析后重新校验。
"""
import os
import posixpath
from urllib.parse import urlsplit, urlunsplit, unquote

from .errors import CollectionError

_ALLOWED_SCHEMES = ('file', 'http', 'https')
_DEFAULT_PORTS = {'http': 80, 'https': 443}


def _clean_http_path(path):
    if not path.startswith('/'):
        path = '/' + path
    cleaned = posixpath.normpath(path)
    if cleaned == '.':
        cleaned = '/'
    if path.endswith('/') and not cleaned.endswith('/'):
        cleaned += '/'
    return cleaned


def normalize_identity(location):
    """把仓库位置规范化为可比较的身份字符串，禁止凭据与未知协议。"""
    if location is None or not isinstance(location, str) or not location:
        raise CollectionError('仓库位置为空或类型不合法')
    if '\x00' in location or '\\' in location:
        raise CollectionError(f'仓库位置包含非法字符: {location!r}')
    if '::' in location:
        raise CollectionError(f'不允许的外部 helper 或扩展协议: {location!r}')
    if '://' not in location and not location.startswith('file:'):
        # 视为本地文件系统路径
        if location.startswith('/') or os.path.sep in location or location.startswith('.'):
            return 'file://' + os.path.normpath(os.path.abspath(location))
        raise CollectionError(f'无法识别的仓库位置: {location!r}')
    parts = urlsplit(location)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise CollectionError(f'不允许的协议 {scheme!r}，仅支持 file/http/https')
    if parts.username is not None or parts.password is not None:
        raise CollectionError('仓库地址不得携带用户名或密码等凭据')
    if parts.fragment or parts.query:
        raise CollectionError('仓库地址不得携带查询串或片段')
    if scheme == 'file':
        if parts.hostname not in (None, '', 'localhost'):
            raise CollectionError('file 地址不得指向远程主机')
        raw = unquote(parts.path)
        return 'file://' + os.path.normpath(os.path.abspath(raw))
    host = parts.hostname
    if not host:
        raise CollectionError('HTTP 仓库地址缺少主机名')
    port = parts.port
    netloc = host.lower()
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f'{netloc}:{port}'
    path = _clean_http_path(parts.path)
    return urlunsplit((scheme, netloc, path, '', ''))


def identity_to_location(identity):
    """规范化身份转成可交给 git 的位置。"""
    parts = urlsplit(identity)
    if parts.scheme == 'file':
        return parts.path
    return identity


def resolve_submodule_url(parent_identity, submodule_url):
    """按 Git 相对地址语义解析子模块 URL，并返回重新规范化后的身份。

    仅接受 http/https/file 系内的解析结果；未知协议（含 ext:: 等外部
    helper）在规范化阶段直接失败。
    """
    if not isinstance(submodule_url, str) or not submodule_url.strip():
        raise CollectionError('子模块 URL 为空')
    url = submodule_url.strip()
    if '\x00' in url or '\\' in url:
        raise CollectionError(f'子模块 URL 包含非法字符: {url!r}')
    # 任何含 "::" 的写法都是外部 remote helper / 扩展协议，一律拒绝
    if '::' in url:
        raise CollectionError(f'不允许的外部 helper 或扩展协议: {url!r}')
    # 显式绝对地址：直接重新规范化（协议白名单在其中执行）
    if '://' in url:
        return normalize_identity(url)
    parent = urlsplit(parent_identity)
    if url.startswith('/'):
        if parent.scheme == 'file':
            base = 'file://'
            resolved = posixpath.normpath(url)
        else:
            base = f'{parent.scheme}://{parent.netloc}'
            resolved = posixpath.normpath(url)
        return normalize_identity(base + resolved)
    # 相对地址（./ ../ 或无协议裸名），按 Git 语义处理：
    # 每个开头的 ../ 从父 URL 完整路径（含仓库名这一层）剥离一个组件，
    # 其余部分直接拼接；./ 仅作当前层前缀，不剥离组件。
    resolved = posixpath.normpath(posixpath.join(parent.path, url))
    if parent.scheme == 'file':
        return normalize_identity('file://' + resolved)
    return normalize_identity(urlunsplit((parent.scheme, parent.netloc, resolved, '', '')))


def safe_submodule_path(raw_path):
    """校验 .gitmodules 中声明的 path：禁止绝对路径、回溯、反斜杠、NUL。"""
    if not isinstance(raw_path, str) or not raw_path:
        raise CollectionError('子模块 path 为空或类型不合法')
    if '\x00' in raw_path or '\\' in raw_path or raw_path.startswith('/'):
        raise CollectionError(f'不安全的子模块路径: {raw_path!r}')
    normalized = posixpath.normpath(raw_path)
    if normalized in ('.', '..') or normalized.startswith('../') or '/../' in normalized:
        raise CollectionError(f'子模块路径越界: {raw_path!r}')
    if any(part in ('', '.', '..') for part in normalized.split('/')):
        raise CollectionError(f'子模块路径包含非法片段: {raw_path!r}')
    return normalized


def safe_join(base, leaf):
    """在导出前缀下拼接路径，任何越界结果都判失败。"""
    joined = posixpath.normpath(posixpath.join(base, leaf))
    if joined == '..' or joined.startswith('../') or joined.startswith('/'):
        raise CollectionError(f'路径拼接越界: {base!r} + {leaf!r}')
    return joined
