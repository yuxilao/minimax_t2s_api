from __future__ import annotations

from ..errors import ParseError
from . import register_parser
from .base import ParserBase


@register_parser
class PlaceholderParser(ParserBase):
    """占位解析器：演示如何扩展。实现 ParserBase 并注册后即可被自动加载。"""

    name = "placeholder"
    display_name = "占位解析器(扩展示例)"
    extensions = ()

    def detect(self, path: str) -> int:
        return 0

    def parse(self, path: str):
        raise ParseError("占位解析器未实现：请参照 t2s_tool/parsers/xlsx_9lang.py 编写自定义解析脚本，放入 parsers_custom/ 目录")
