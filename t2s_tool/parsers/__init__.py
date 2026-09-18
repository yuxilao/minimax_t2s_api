from __future__ import annotations

import importlib.util
import os
from typing import Dict, List

from ..errors import ParseError
from .base import ParserBase

_REGISTRY: Dict[str, type] = {}
_loaded_custom: set = set()


def register_parser(cls):
    """类装饰器：注册解析器。name 缺失/为 base/重复 -> ValueError。"""
    name = getattr(cls, "name", "")
    if not name or name == "base":
        raise ValueError(f"解析器缺少合法 name: {cls!r}")
    if name in _REGISTRY:
        raise ValueError(f"解析器名称重复: {name}")
    _REGISTRY[name] = cls
    return cls


def get_parser(name: str) -> ParserBase:
    if name not in _REGISTRY:
        raise ParseError(f"未知解析器: {name}，可用: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def list_parsers() -> List[ParserBase]:
    return [cls() for cls in _REGISTRY.values()]


def detect_best(path: str) -> ParserBase:
    """对全部已注册解析器跑 detect，返回得分最高者；全为 0 抛 ParseError。"""
    best, best_score = None, 0
    for p in list_parsers():
        try:
            score = int(p.detect(path) or 0)
        except Exception:
            score = 0
        if score > best_score:
            best, best_score = p, score
    if best is None:
        raise ParseError(f"没有解析器能识别该文件: {path}")
    return best


def _load_custom_module(path: str) -> None:
    mod_name = "t2s_custom_" + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def load_parsers(extra_dir: str = "parsers_custom") -> None:
    """加载内置解析器（延迟导入=按需），并扫描用户自定义目录（存在才扫）。

    新增解析脚本 = 把实现了 ParserBase 且带 @register_parser 的 .py 丢进 extra_dir。
    """
    from . import placeholder, xlsx_9lang  # noqa: F401

    if os.path.isdir(extra_dir):
        for fname in sorted(os.listdir(extra_dir)):
            if fname.endswith(".py") and not fname.startswith("_"):
                full = os.path.abspath(os.path.join(extra_dir, fname))
                if full not in _loaded_custom:
                    _loaded_custom.add(full)
                    _load_custom_module(full)
