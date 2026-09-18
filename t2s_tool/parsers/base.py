from __future__ import annotations

from typing import Tuple

from ..models import ParseResult


class ParserBase:
    """语音目录解析器接口。新解析器继承本类并用 @register_parser 注册。"""

    name = "base"
    display_name = "基础解析器"
    extensions: Tuple[str, ...] = ()

    def detect(self, path: str) -> int:
        """返回 0-100 的匹配分，用于自动识别。无法识别必须返回 0。"""
        raise NotImplementedError

    def parse(self, path: str) -> ParseResult:
        raise NotImplementedError
