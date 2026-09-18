from __future__ import annotations

from typing import Callable, Dict, List, Optional

from ..errors import APIError
from ..models import CancelToken, ProviderResult, StageEvent, VoiceEntry


class TTSProvider:
    """TTS 供应商接口。新供应商继承本类并用 @register_provider 注册。"""

    name = "base"
    display_name = "基础供应商"

    def validate_config(self, config: dict) -> None:
        raise NotImplementedError

    def synthesize_batch(
        self,
        entries: List[VoiceEntry],
        config: dict,
        on_event: Callable[[StageEvent], None],
        cancel_token: CancelToken,
        on_entry: Optional[Callable[[VoiceEntry, bytes], None]] = None,
    ) -> ProviderResult:
        """批量合成，返回 {voice_id: mp3字节}。on_event 上报进度，cancel_token 用于取消。

        on_entry：可选，每条目合成成功拿到音频后立即回调（用于边转边落盘）。
        """
        raise NotImplementedError

    def synthesize_one(self, entry: VoiceEntry, config: dict,
                       cancel_token: CancelToken) -> bytes:
        """单条合成（「单条转换」链路的中间层接口），返回 mp3 字节。

        默认实现委托 synthesize_batch([entry])，任何供应商自动兼容单条转换；
        供应商可覆盖以走更优路径（如 MiniMax 覆盖为同步接口 /v1/t2a_v2，
        一次请求直接返回音频）。失败抛 APIError。
        """
        result = self.synthesize_batch([entry], config, lambda ev: None, cancel_token)
        data = result.audio_by_id.get(entry.voice_id)
        if not data:
            detail = result.warnings[0] if result.warnings else "供应商未返回音频"
            raise APIError(f"合成: {detail}", "api")
        return data
