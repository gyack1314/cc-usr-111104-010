"""固定提交递归Git源码收集工具（仅使用标准库）。"""
from .errors import CollectionError
from .identity import normalize_identity, resolve_submodule_url, safe_join, safe_submodule_path
from .collector import collect, Config
from .verify import verify_package

__all__ = [
    'CollectionError', 'Config', 'collect', 'verify_package',
    'normalize_identity', 'resolve_submodule_url', 'safe_join', 'safe_submodule_path',
]
