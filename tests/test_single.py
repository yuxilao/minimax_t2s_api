from __future__ import annotations

import os

import pytest

from t2s_tool.errors import APIError, ParseError
from t2s_tool.models import CancelToken, ProviderResult, SingleResult, VoiceEntry
from t2s_tool.pipeline import run_single
from t2s_tool.providers.base import TTSProvider
from t2s_tool.providers.minimax import MinimaxProvider

HEADERS = ("语音ID", "中文", "英文")
ROWS = [
    ("Q001", "一", "one"),
    ("Q002", "二", "two"),
    ("Q003", None, "three"),  # 中文列为空文本
]


def _xlsx(make_xlsx):
    return make_xlsx(HEADERS, ROWS, name="single.xlsx")


def test_run_single_fake_success(tmp_path, make_xlsx):
    out = str(tmp_path / "out")
    res = run_single(_xlsx(make_xlsx), "英文", "Q002",
                     provider_name="fake",
                     config={"provider": "fake", "providers": {"fake": {}}},
                     output_dir=out)
    assert isinstance(res, SingleResult)
    assert res.language == "英文"
    assert res.voice_id == "Q002"
    assert res.text == "two"
    assert res.output_path.endswith(os.path.join("out", "英文", "Q002.mp3"))
    assert res.size == len(b"FAKE-MP3:Q002")
    with open(res.output_path, "rb") as f:
        assert f.read() == b"FAKE-MP3:Q002"


def test_run_single_minimax_sync_success(tmp_path, make_xlsx, mock_http, mini_cfg):
    fake = mock_http()
    MP3 = b"\x00\x01FAKEMP3"
    fake.add_post("/v1/t2a_v2",
                  {"data": {"audio": MP3.hex()}, "base_resp": {"status_code": 0}})
    res = run_single(_xlsx(make_xlsx), "英文", "Q001",
                     provider_name="minimax",
                     config={"provider": "minimax", "providers": {"minimax": mini_cfg}},
                     output_dir=str(tmp_path / "out"))
    with open(res.output_path, "rb") as f:
        assert f.read() == MP3
    # 仅 1 次调用，且走同步接口 /v1/t2a_v2
    assert len(fake.calls) == 1
    method, url, kw = fake.calls[0]
    assert method == "POST"
    assert "/v1/t2a_v2" in url
    assert kw["json"]["text"] == "one"  # 英文/Q001 的文本
    for key in ("model", "voice_setting", "audio_setting"):
        assert key in kw["json"]
    # 不应出现任何异步接口调用
    assert all("t2a_async_v2" not in c[1] for c in fake.calls)


def test_run_single_unknown_language(tmp_path, make_xlsx):
    with pytest.raises(ParseError):
        run_single(_xlsx(make_xlsx), "日语", "Q001",
                   provider_name="fake",
                   config={"provider": "fake", "providers": {"fake": {}}},
                   output_dir=str(tmp_path / "out"))


def test_run_single_unknown_voice_id(tmp_path, make_xlsx):
    with pytest.raises(ParseError):
        run_single(_xlsx(make_xlsx), "英文", "Q999",
                   provider_name="fake",
                   config={"provider": "fake", "providers": {"fake": {}}},
                   output_dir=str(tmp_path / "out"))


def test_run_single_empty_text_no_http(tmp_path, make_xlsx, mock_http, mini_cfg):
    fake = mock_http()  # 不注册任何响应：任何 HTTP 调用都会立即失败
    with pytest.raises(ParseError):
        run_single(_xlsx(make_xlsx), "中文", "Q003",
                   provider_name="minimax",
                   config={"provider": "minimax", "providers": {"minimax": mini_cfg}},
                   output_dir=str(tmp_path / "out"))
    assert fake.calls == []  # 空文本未发起任何 HTTP


# ------------------------------------------------------------------ 中间层 synthesize_one

def test_minimax_synthesize_one_uses_sync(mock_http, mini_cfg):
    """MinimaxProvider.synthesize_one 覆盖中间层：固定走 /v1/t2a_v2 同步接口。"""
    fake = mock_http()
    MP3 = b"\x10\x20SYNCONLY"
    fake.add_post("/v1/t2a_v2",
                  {"data": {"audio": MP3.hex()}, "base_resp": {"status_code": 0}})
    data = MinimaxProvider().synthesize_one(VoiceEntry("Q001", "你好"), mini_cfg, CancelToken())
    assert data == MP3
    assert len(fake.calls) == 1
    method, url, kw = fake.calls[0]
    assert method == "POST" and "/v1/t2a_v2" in url
    assert kw["json"]["text"] == "你好"


class _DummyProvider(TTSProvider):
    """只实现批量接口的供应商：验证基类默认 synthesize_one 自动兼容单条链路。"""

    name = "dummy"
    display_name = "Dummy"

    def __init__(self, payload=b"DUMMY-MP3"):
        self._payload = payload
        self.batch_calls = []

    def validate_config(self, config):
        pass

    def synthesize_batch(self, entries, config, on_event, cancel_token, on_entry=None):
        self.batch_calls.append(list(entries))
        audio = {e.voice_id: self._payload for e in entries} if self._payload else {}
        return ProviderResult(audio, [] if self._payload else ["dummy 未返回音频"])


def test_base_synthesize_one_default_delegates_to_batch():
    """未覆盖 synthesize_one 的供应商：基类默认实现委托 synthesize_batch([entry])。"""
    p = _DummyProvider()
    data = p.synthesize_one(VoiceEntry("Q007", "x"), {}, CancelToken())
    assert data == b"DUMMY-MP3"
    assert [[e.voice_id for e in call] for call in p.batch_calls] == [["Q007"]]


def test_base_synthesize_one_default_raises_when_no_audio():
    with pytest.raises(APIError):
        _DummyProvider(payload=b"").synthesize_one(VoiceEntry("Q007", "x"), {}, CancelToken())
