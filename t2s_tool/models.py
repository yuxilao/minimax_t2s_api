from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .errors import CancelledError


@dataclass
class VoiceEntry:
    voice_id: str
    text: str

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass
class LanguagePack:
    language: str
    entries: List[VoiceEntry]


@dataclass
class ParseResult:
    source_path: str
    parser_name: str
    languages: List[LanguagePack]
    warnings: List[str] = field(default_factory=list)


@dataclass
class StageEvent:
    """统一进度事件。stage 取值: parse/zip/upload/create/poll/download/package/done。"""
    stage: str
    message: str
    current: int = 0
    total: int = 0


@dataclass
class ProviderResult:
    audio_by_id: Dict[str, bytes]  # voice_id -> mp3 字节
    warnings: List[str] = field(default_factory=list)


@dataclass
class LanguageReport:
    language: str
    success_ids: List[str]
    missing_ids: List[str]
    unknown_ids: List[str]
    warnings: List[str]
    output_dir: Optional[str]   # 全部缺失时为 None（不创建空目录）
    tar_path: Optional[str]     # 全部缺失时为 None（不打空 tar）


@dataclass
class JobReport:
    parse_warnings: List[str]
    language_reports: List[LanguageReport]
    submitted_tasks: List[str] = field(default_factory=list)  # submit_only 模式提交的任务ID


@dataclass
class SingleResult:
    """单条语音转换结果（同步接口链路）。"""
    language: str
    voice_id: str
    text: str
    output_path: str
    size: int  # 落盘音频字节数


class CancelToken:
    def __init__(self) -> None:
        self._flag = False

    def cancel(self) -> None:
        self._flag = True

    @property
    def cancelled(self) -> bool:
        return self._flag

    def throw_if_cancelled(self) -> None:
        if self._flag:
            raise CancelledError("任务已被用户取消")
