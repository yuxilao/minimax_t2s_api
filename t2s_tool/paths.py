from __future__ import annotations

import os
import sys


def app_dir() -> str:
    """应用根目录：PyInstaller 冻结时为 exe 所在目录，否则为项目根目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_config_path() -> str:
    return os.path.join(app_dir(), "config.json")


def default_parsers_dir() -> str:
    """用户自定义解析脚本目录（放 .py 即生效）。"""
    return os.path.join(app_dir(), "parsers_custom")


def default_output_dir() -> str:
    return os.path.join(app_dir(), "语音包")


def default_tasks_path() -> str:
    """任务记录存储文件：exe 旁（冻结）或项目根目录 tasks.json。"""
    return os.path.join(app_dir(), "tasks.json")
