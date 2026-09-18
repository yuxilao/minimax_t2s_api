from __future__ import annotations

from typing import List

from ..errors import APIError
from ..models import CancelToken, ProviderResult, StageEvent, VoiceEntry
from . import register_provider
from .base import TTSProvider


@register_provider
class FakeProvider(TTSProvider):
    """测试/演示用供应商：不联网，直接为每个非空条目生成伪 mp3 字节。"""

    name = "fake"
    display_name = "Fake(测试/演示)"

    def validate_config(self, config: dict) -> None:
        pass

    def synthesize_one(self, entry: VoiceEntry, config: dict, cancel_token: CancelToken) -> bytes:
        """覆盖中间层单条接口：不联网直接生成伪 mp3（保持静默，不上报批量事件）。"""
        cancel_token.throw_if_cancelled()
        if entry.is_empty:
            raise APIError("合成: 文本为空", "empty")
        return b"FAKE-MP3:" + entry.voice_id.encode("utf-8")

    def synthesize_batch(self, entries, config, on_event, cancel_token, on_entry=None) -> ProviderResult:
        audio = {}
        for e in entries:
            cancel_token.throw_if_cancelled()
            if e.is_empty:
                continue
            data = b"FAKE-MP3:" + e.voice_id.encode("utf-8")
            audio[e.voice_id] = data
            if on_entry is not None:
                on_entry(e, data)
        on_event(StageEvent("download", f"fake 生成 {len(audio)} 条音频", len(audio), len(audio)))
        return ProviderResult(audio, [])
