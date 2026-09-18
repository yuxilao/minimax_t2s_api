from __future__ import annotations

from typing import Optional


class APIError(Exception):
    """API 调用错误，支持错误类型分类。"""

    def __init__(self, message: str, error_type: str = "unknown", status_code: Optional[int] = None):
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code


class ConfigError(Exception):
    """配置文件错误。"""


class FileError(Exception):
    """文件操作错误。"""


class ParseError(Exception):
    """语音目录解析错误。"""


class CancelledError(Exception):
    """任务被用户取消。"""
