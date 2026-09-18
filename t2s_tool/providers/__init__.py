from __future__ import annotations

from typing import Dict, List

from ..errors import ConfigError
from .base import TTSProvider

_REGISTRY: Dict[str, type] = {}


def register_provider(cls):
    name = getattr(cls, "name", "")
    if not name or name == "base":
        raise ValueError(f"供应商缺少合法 name: {cls!r}")
    if name in _REGISTRY:
        raise ValueError(f"供应商名称重复: {name}")
    _REGISTRY[name] = cls
    return cls


def get_provider(name: str) -> TTSProvider:
    if name not in _REGISTRY:
        raise ConfigError(f"未知供应商: {name}，可用: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def list_providers() -> List[TTSProvider]:
    return [cls() for cls in _REGISTRY.values()]


def load_providers() -> None:
    """加载内置供应商（延迟导入=按需）。"""
    from . import fake, minimax  # noqa: F401
