from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from .. import parsers, paths, providers
from ..config import PROVIDER_DEFAULTS, load_config, migrate_config, save_config
from ..errors import ParseError
from ..models import CancelToken, ParseResult, StageEvent
from ..pipeline import run_job, run_single


@dataclass
class GuiParams:
    input_path: str
    parser_name: str            # "auto" 或解析器 name
    languages: List[str]        # 空 = 全部语言
    provider_name: str
    provider_config: dict
    output_dir: str


@dataclass
class GuiSingleParams:
    input_path: str
    parser_name: str            # "auto" 或解析器 name
    language: str
    voice_id: str
    provider_name: str
    provider_config: dict
    output_dir: str


class GuiController:
    """GUI 的非 UI 决策层：解析预览、语言选择、配置读写、任务启停。"""

    def __init__(self, config_path: Optional[str] = None) -> None:
        self.config_path = config_path or paths.default_config_path()
        self.parse_result: Optional[ParseResult] = None
        self.cancel_token: Optional[CancelToken] = None
        self.worker: Optional[threading.Thread] = None
        self.single_cancel_token: Optional[CancelToken] = None
        self.single_worker: Optional[threading.Thread] = None
        parsers.load_parsers(paths.default_parsers_dir())
        providers.load_providers()

    def parser_choices(self) -> List[Tuple[str, str]]:
        """[(name, display_name)]，首项为自动检测。"""
        return [("auto", "自动检测")] + [(p.name, p.display_name) for p in parsers.list_parsers()]

    def provider_choices(self) -> List[Tuple[str, str]]:
        return [(p.name, p.display_name) for p in providers.list_providers()]

    def load_config(self) -> dict:
        """配置文件不存在时返回迁移后的默认配置（不抛异常），便于 GUI 首次启动。"""
        if not os.path.exists(self.config_path):
            return migrate_config({})
        return load_config(self.config_path)

    def save_config(self, provider_name: str, provider_config: dict) -> None:
        cfg = self.load_config()
        cfg["provider"] = provider_name
        cfg.setdefault("providers", {})[provider_name] = provider_config
        save_config(cfg, self.config_path)

    def parse_preview(self, path: str, parser_name: str) -> ParseResult:
        """解析语音目录并缓存结果，供 language_choices 使用。"""
        if not path or not os.path.exists(path):
            raise ParseError(f"语音目录文件不存在: {path}")
        if parser_name in (None, "auto"):
            parser = parsers.detect_best(path)
        else:
            parser = parsers.get_parser(parser_name)
        self.parse_result = parser.parse(path)
        return self.parse_result

    def language_choices(self) -> List[Tuple[str, int]]:
        """[(语言, 条目数)]；未解析返回空列表。"""
        if not self.parse_result:
            return []
        return [(p.language, len(p.entries)) for p in self.parse_result.languages]

    def entry_choices(self, language: str) -> List[Tuple[str, str]]:
        """某语言的 [(voice_id, text)]；未解析返回 []，未知语言抛 ParseError。"""
        if not self.parse_result:
            return []
        for p in self.parse_result.languages:
            if p.language == language:
                return [(e.voice_id, e.text) for e in p.entries]
        avail = sorted(p.language for p in self.parse_result.languages)
        raise ParseError(f"未找到语言: {language}，可用: {avail}")

    def start(
        self,
        params: GuiParams,
        on_event: Callable[[StageEvent], None],
        on_done: Callable[[bool, object], None],
        task_store=None,
    ) -> None:
        """在 daemon 线程中跑 run_job；on_done(ok, payload)，payload 为 JobReport 或异常。

        batch 模式（minimax + task_store 提供时）自动 submit_only：提交即返，
        后续生命周期由任务中心管理。
        """
        self.cancel_token = CancelToken()

        def _work() -> None:
            try:
                config = {"provider": params.provider_name,
                          "providers": {params.provider_name: params.provider_config}}
                mode = str(params.provider_config.get("tts_mode") or "batch").lower()
                submit_only = (mode == "batch" and params.provider_name == "minimax"
                               and task_store is not None)
                report = run_job(
                    input_path=params.input_path,
                    parser_name=params.parser_name,
                    languages=params.languages or None,
                    provider_name=params.provider_name,
                    config=config,
                    output_dir=params.output_dir,
                    on_event=on_event,
                    cancel_token=self.cancel_token,
                    parsers_dir=paths.default_parsers_dir(),
                    submit_only=submit_only,
                    task_store=task_store,
                )
                on_done(True, report)
            except Exception as e:
                on_done(False, e)

        self.worker = threading.Thread(target=_work, daemon=True)
        self.worker.start()

    def cancel(self) -> None:
        if self.cancel_token is not None:
            self.cancel_token.cancel()

    def start_single(
        self,
        params: GuiSingleParams,
        on_event: Callable[[StageEvent], None],
        on_done: Callable[[bool, object], None],
    ) -> None:
        """daemon 线程跑 run_single；on_done(ok, payload)，payload 为 SingleResult 或异常。"""
        self.single_cancel_token = CancelToken()

        def _work() -> None:
            try:
                config = {"provider": params.provider_name,
                          "providers": {params.provider_name: params.provider_config}}
                result = run_single(
                    input_path=params.input_path,
                    language=params.language,
                    voice_id=params.voice_id,
                    parser_name=params.parser_name,
                    provider_name=params.provider_name,
                    config=config,
                    output_dir=params.output_dir,
                    on_event=on_event,
                    cancel_token=self.single_cancel_token,
                    parsers_dir=paths.default_parsers_dir(),
                )
                on_done(True, result)
            except Exception as e:
                on_done(False, e)

        self.single_worker = threading.Thread(target=_work, daemon=True)
        self.single_worker.start()

    def cancel_single(self) -> None:
        if self.single_cancel_token is not None:
            self.single_cancel_token.cancel()

    @staticmethod
    def default_provider_config() -> dict:
        import copy
        return copy.deepcopy(PROVIDER_DEFAULTS)
