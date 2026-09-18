from __future__ import annotations

# MiniMax 供应商测试：逐条合成模式（tts_mode = sync / async）+ 任务中心批量原语。
#
# 全部离线：requests 被 conftest.FakeRequests / CapturingRequests 替换，不触网。
# 逐条链路（sync/async）用例统一用 mini_cfg（tts_mode=async、concurrency=1），
# 保证请求/事件序列可断言；批量原语（submit_batch/query_task/extract_named_mp3s）
# 是单线程顺序调用，天然确定；batch 编排已上移到 pipeline（见 test_pipeline.py）。

import inspect
import io
import os
import tarfile
import tempfile
import zipfile

import pytest
import requests

from t2s_tool.errors import APIError, CancelledError, ConfigError
from t2s_tool.models import CancelToken, ProviderResult, StageEvent, VoiceEntry
from t2s_tool.providers import minimax
from t2s_tool.providers.minimax import DEFAULT_BASE_URL, MinimaxProvider

from tests.conftest import FakeRequests, FakeResponse


# ------------------------------------------------------------------ 小工具

def _events_sink(events):
    def on_event(ev):
        assert isinstance(ev, StageEvent)
        events.append(ev)

    return on_event


def _entries(*specs):
    return [VoiceEntry(vid, text) for vid, text in specs]


def _run(cfg, entries, token=None, events=None):
    """跑一次 synthesize_batch，返回 ProviderResult。"""
    if events is None:
        events = []
    provider = MinimaxProvider()
    return provider.synthesize_batch(
        entries, cfg, _events_sink(events), token or CancelToken())


def _run_with_entry(cfg, entries, token=None, events=None, on_entry=None):
    """带 on_entry 逐条目回调的 synthesize_batch（batch/sync/async 三模式统一签名）。"""
    if events is None:
        events = []
    provider = MinimaxProvider()
    return provider.synthesize_batch(
        entries, cfg, _events_sink(events), token or CancelToken(),
        on_entry=on_entry)


def _stages(events):
    return [(e.stage, e.current, e.total) for e in events]


def _ok_create(task_id=456):
    return {"task_id": task_id, "task_token": "tok", "usage_characters": 10,
            "base_resp": {"status_code": 0}}


def _ok_query(file_id=901):
    return {"status": "Success", "file_id": file_id, "base_resp": {"status_code": 0}}


def _member_tar(specs):
    """specs: [(member_name, bytes)]，手工构造任意成员名的 tar。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in specs:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _FakeTime:
    """替换 minimax 模块内的 time：可控时钟 + 记录 sleep 调用。"""

    def __init__(self, step=0.0):
        self.now = 0.0
        self.step = step  # 每次 time() 递进的秒数
        self.sleeps = []

    def time(self):
        value = self.now
        self.now += self.step
        return value

    def sleep(self, seconds):
        self.sleeps.append(seconds)


class _RaisingRequests(FakeRequests):
    """post/get 直接抛指定异常，用于覆盖 _post/_get 的异常映射分支。"""

    def __init__(self, exc):
        FakeRequests.__init__(self)
        self._exc = exc

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        raise self._exc

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        raise self._exc


# --------------------------------------------------------------- _check_http

@pytest.mark.parametrize("code,expected", [
    (401, "auth"),
    (403, "auth"),
    (429, "quota"),
    (500, "server"),
    (503, "server"),
    (418, "http"),
])
def test_check_http_classification(code, expected):
    with pytest.raises(APIError) as ei:
        minimax._check_http(FakeResponse(status_code=code), "测试")
    assert ei.value.error_type == expected
    assert ei.value.status_code == code
    assert "测试" in str(ei.value)


def test_check_http_200_ok():
    minimax._check_http(FakeResponse(status_code=200), "测试")  # 不抛


def test_check_http_404_is_http_type():
    with pytest.raises(APIError) as ei:
        minimax._check_http(FakeResponse(status_code=404), "下载")
    assert ei.value.error_type == "http"
    assert "404" in str(ei.value)


# ----------------------------------------------------------- _check_base_resp

@pytest.mark.parametrize("msg,expected", [
    ("今日额度已用尽", "quota"),
    ("daily quota exceeded", "quota"),
    ("rate limit reached", "quota"),
    ("invalid api key", "auth"),
    ("refresh token expired", "auth"),
    ("内部服务错误", "api"),
])
def test_check_base_resp_classification(msg, expected):
    result = {"base_resp": {"status_code": 1004, "status_msg": msg}}
    with pytest.raises(APIError) as ei:
        minimax._check_base_resp(result, "测试")
    assert ei.value.error_type == expected
    assert msg in str(ei.value)


def test_check_base_resp_zero_ok():
    minimax._check_base_resp({"base_resp": {"status_code": 0}}, "测试")
    minimax._check_base_resp({}, "测试")            # 无 base_resp 视为 OK
    minimax._check_base_resp({"base_resp": None}, "测试")  # null 亦视为 OK


# ------------------------------------------------------------------ _base

def test_base_uses_default_when_missing(mini_cfg):
    cfg = dict(mini_cfg)
    cfg.pop("base_url")
    assert minimax._base(cfg) == DEFAULT_BASE_URL


def test_base_strips_trailing_slash():
    assert minimax._base({"base_url": "https://api.test///"}) == "https://api.test"


# --------------------------------------------------------------- _create_task

def test_create_task_url_payload_headers(mock_http, mini_cfg):
    fake = mock_http()
    fake.add_post("/v1/t2a_async_v2", _ok_create(task_id=777))

    tid = minimax._create_task("你好，世界", mini_cfg)

    assert tid == "777"                      # 一律转成 str
    assert len(fake.calls) == 1
    method, url, kw = fake.calls[0]
    assert method == "POST"
    assert url == "https://api.test/v1/t2a_async_v2"
    assert kw["timeout"] == mini_cfg["request_timeout"]
    assert kw["headers"]["Authorization"] == "Bearer test-key"
    assert kw["headers"]["Content-Type"] == "application/json"
    payload = kw["json"]
    assert payload["text"] == "你好，世界"
    assert payload["model"] == "speech-2.8-hd"
    assert payload["voice_setting"] == {"voice_id": "v1"}
    assert payload["audio_setting"] == {"format": "mp3"}
    assert set(payload.keys()) == {"model", "text", "voice_setting", "audio_setting"}


def test_create_task_string_task_id_kept(mock_http, mini_cfg):
    fake = mock_http()
    fake.add_post("/v1/t2a_async_v2", _ok_create(task_id="abc-123"))
    assert minimax._create_task("x", mini_cfg) == "abc-123"


def test_create_task_default_base_url_used(mock_http, mini_cfg):
    fake = mock_http()
    fake.add_post("/v1/t2a_async_v2", _ok_create())
    cfg = dict(mini_cfg)
    cfg.pop("base_url")
    minimax._create_task("x", cfg)
    assert fake.calls[0][1] == DEFAULT_BASE_URL + "/v1/t2a_async_v2"


@pytest.mark.parametrize("resp", [
    _ok_create(task_id=0),        # falsy task_id
    _ok_create(task_id=None),
    {"task_token": "tok", "base_resp": {"status_code": 0}},
])
def test_create_task_without_task_id_raises_api(mock_http, mini_cfg, resp):
    fake = mock_http()
    fake.add_post("/v1/t2a_async_v2", resp)
    with pytest.raises(APIError) as ei:
        minimax._create_task("x", mini_cfg)
    assert ei.value.error_type == "api"
    assert "task_id" in str(ei.value)


def test_create_task_http_401_maps_auth(mock_http, mini_cfg):
    fake = mock_http()
    fake.add_post("/v1/t2a_async_v2", FakeResponse(status_code=401))
    with pytest.raises(APIError) as ei:
        minimax._create_task("x", mini_cfg)
    assert ei.value.error_type == "auth"
    assert ei.value.status_code == 401
    assert "创建任务" in str(ei.value)


@pytest.mark.parametrize("msg,expected", [
    ("quota exceeded", "quota"),
    ("bad api key", "auth"),
    ("text too long", "api"),
])
def test_create_task_base_resp_subdivision(mock_http, mini_cfg, msg, expected):
    fake = mock_http()
    fake.add_post("/v1/t2a_async_v2",
                  {"base_resp": {"status_code": 2049, "status_msg": msg}})
    with pytest.raises(APIError) as ei:
        minimax._create_task("x", mini_cfg)
    assert ei.value.error_type == expected


def test_create_task_bad_json_maps_parse(mock_http, mini_cfg):
    fake = mock_http()
    fake.add_post("/v1/t2a_async_v2",
                  FakeResponse(status_code=200, json_data=ValueError("bad json")))
    with pytest.raises(APIError) as ei:
        minimax._create_task("x", mini_cfg)
    assert ei.value.error_type == "parse"


@pytest.mark.parametrize("exc,expected", [
    (requests.exceptions.ReadTimeout("t"), "timeout"),
    (requests.exceptions.ConnectionError("net"), "network"),
    (requests.exceptions.TooManyRedirects("r"), "request"),
])
def test_post_exception_mapping(monkeypatch, mini_cfg, exc, expected):
    monkeypatch.setattr(minimax, "requests", _RaisingRequests(exc))
    with pytest.raises(APIError) as ei:
        minimax._create_task("x", mini_cfg)
    assert ei.value.error_type == expected


@pytest.mark.parametrize("exc,expected", [
    (requests.exceptions.ConnectTimeout("t"), "timeout"),
    (requests.exceptions.ConnectionError("net"), "network"),
])
def test_get_exception_mapping(monkeypatch, mini_cfg, exc, expected):
    monkeypatch.setattr(minimax, "requests", _RaisingRequests(exc))
    with pytest.raises(APIError) as ei:
        minimax._poll("1", mini_cfg, CancelToken())
    assert ei.value.error_type == expected


# --------------------------------------------------------------------- _poll

def test_poll_processing_then_success(mock_http, mini_cfg, monkeypatch):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", [
        {"status": "Processing"},
        {"status": "Processing"},
        _ok_query(file_id=901),
    ])
    mini_cfg["poll_interval"] = 0.5           # 注入可控间隔（假时钟不真等待）
    clock = _FakeTime()
    monkeypatch.setattr(minimax, "time", clock)

    fid = minimax._poll("777", mini_cfg, CancelToken())

    assert fid == "901"                       # 一律转成 str
    urls = [u for _, u, _ in fake.calls]
    assert urls == ["https://api.test/v1/query/t2a_async_query_v2?task_id=777"] * 3
    assert clock.sleeps == [0.5, 0.5]         # 前两次非 success 各 sleep 一次


@pytest.mark.parametrize("status", ["Success", "SUCCESS", "success"])
def test_poll_status_case_insensitive(mock_http, mini_cfg, status):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", {"status": status, "file_id": 55})
    assert minimax._poll("1", mini_cfg, CancelToken()) == "55"


def test_poll_success_without_file_id_raises_api(mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", {"status": "Success"})
    with pytest.raises(APIError) as ei:
        minimax._poll("1", mini_cfg, CancelToken())
    assert ei.value.error_type == "api"
    assert "file_id" in str(ei.value)


@pytest.mark.parametrize("status,expected", [
    ("Failed", "api"),
    ("FAILED", "api"),
    ("Expired", "expired"),
    ("expired", "expired"),
])
def test_poll_terminal_failure_states(mock_http, mini_cfg, status, expected):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", {"status": status})
    with pytest.raises(APIError) as ei:
        minimax._poll("1", mini_cfg, CancelToken())
    assert ei.value.error_type == expected


def test_poll_http_503_maps_server(mock_http, mini_cfg):
    """_poll 内部改调 query_task，HTTP 错误的 stage 文案也随之变为「查询」。"""
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", FakeResponse(status_code=503))
    with pytest.raises(APIError) as ei:
        minimax._poll("1", mini_cfg, CancelToken())
    assert ei.value.error_type == "server"
    assert "查询" in str(ei.value)


@pytest.mark.parametrize("poll_timeout,expected_calls", [(15, 1), (0, 0)])
def test_poll_timeout_raises_timeout(mock_http, mini_cfg, monkeypatch,
                                     poll_timeout, expected_calls):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", {"status": "Processing"})
    mini_cfg["poll_timeout"] = poll_timeout
    clock = _FakeTime(step=10.0)             # start=0, 其后每次 +10 秒
    monkeypatch.setattr(minimax, "time", clock)

    with pytest.raises(APIError) as ei:
        minimax._poll("1", mini_cfg, CancelToken())

    assert ei.value.error_type == "timeout"
    assert sum(1 for _, u, _ in fake.calls if "t2a_async_query_v2" in u) == expected_calls
    assert len(clock.sleeps) == expected_calls


def test_poll_pre_cancelled_token_raises_without_request(mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", _ok_query())
    token = CancelToken()
    token.cancel()
    with pytest.raises(CancelledError):
        minimax._poll("1", mini_cfg, token)
    assert fake.calls == []                   # 一次请求都不发


def test_poll_bad_json_maps_parse(mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2",
                 FakeResponse(status_code=200, json_data=ValueError("x")))
    with pytest.raises(APIError) as ei:
        minimax._poll("1", mini_cfg, CancelToken())
    assert ei.value.error_type == "parse"


def test_poll_base_resp_error_raises_immediately(mock_http, mini_cfg):
    """查询返回 base_resp 错误（如无此 task_id）应立即报错，而不是轮询到超时。"""
    fake = mock_http()
    fake.add_get("t2a_async_query_v2",
                 FakeResponse(status_code=200, json_data={
                     "base_resp": {"status_code": 2013, "status_msg": "task not found"}}))
    with pytest.raises(APIError) as ei:
        minimax._poll("999", mini_cfg, CancelToken())
    assert ei.value.error_type == "api"
    assert "task not found" in str(ei.value)
    assert len(fake.calls) == 1                # 一次请求即失败，不进入轮询循环


# --------------------------------------------------------- _extract_first_mp3

def test_extract_first_mp3_from_tar(make_tar):
    blob = make_tar({"Q001": b"payload-1"})
    assert minimax._extract_first_mp3(blob) == b"payload-1"


def test_extract_first_mp3_returns_first_member():
    blob = _member_tar([("a/content-1.mp3", b"first"), ("b/content-2.mp3", b"second")])
    assert minimax._extract_first_mp3(blob) == b"first"


def test_extract_first_mp3_extension_case_insensitive():
    blob = _member_tar([("CONTENT-1.MP3", b"upper")])
    assert minimax._extract_first_mp3(blob) == b"upper"


def test_extract_first_mp3_tar_without_mp3_raises_api():
    blob = _member_tar([("readme.txt", b"hello"), ("notes.json", b"{}")])
    with pytest.raises(APIError) as ei:
        minimax._extract_first_mp3(blob)
    assert ei.value.error_type == "api"
    assert "mp3" in str(ei.value)


def test_extract_first_mp3_raw_id3_returned_as_is():
    blob = b"ID3\x03\x00\x00\x00\x00\x00\x0a" + b"tag-payload"
    assert minimax._extract_first_mp3(blob) == blob


def test_extract_first_mp3_raw_frame_sync_returned_as_is():
    blob = b"\xff\xfb\x90\x64" + b"\x00" * 40
    assert minimax._extract_first_mp3(blob) == blob


def test_extract_first_mp3_garbage_raises_parse():
    with pytest.raises(APIError) as ei:
        minimax._extract_first_mp3(b"this is neither a tar nor an mp3 stream")
    assert ei.value.error_type == "parse"
    assert "tar" in str(ei.value)


# ------------------------------------------------------------ _download_audio

def test_download_audio_url_and_bytes(mock_http, mini_cfg, make_tar):
    fake = mock_http()
    fake.add_get("retrieve_content", FakeResponse(content=make_tar({"Q001": b"mp3-1"})))
    assert minimax._download_audio("901", mini_cfg) == b"mp3-1"
    method, url, kw = fake.calls[0]
    assert method == "GET"
    assert url == "https://api.test/v1/files/retrieve_content?file_id=901"
    assert kw["timeout"] == mini_cfg["download_timeout"]
    assert kw["headers"]["Authorization"] == "Bearer test-key"


def test_download_audio_http_500_maps_server(mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("retrieve_content", FakeResponse(status_code=500))
    with pytest.raises(APIError) as ei:
        minimax._download_audio("901", mini_cfg)
    assert ei.value.error_type == "server"
    assert "下载" in str(ei.value)


def test_download_audio_empty_content_maps_empty(mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("retrieve_content", FakeResponse(status_code=200, content=b""))
    with pytest.raises(APIError) as ei:
        minimax._download_audio("901", mini_cfg)
    assert ei.value.error_type == "empty"


def test_download_audio_non_tar_body_maps_parse(mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("retrieve_content", FakeResponse(content=b"<html>error</html>"))
    with pytest.raises(APIError) as ei:
        minimax._download_audio("901", mini_cfg)
    assert ei.value.error_type == "parse"


# --------------------------------------------------------- synthesize_batch

def test_synthesize_batch_happy_path(mock_http, register_happy_path, make_tar, mini_cfg):
    fake = mock_http()
    tar_bytes = make_tar({"Q001": b"mp3-bytes-1"})
    register_happy_path(fake, tar_bytes)
    events = []

    result = _run(mini_cfg, _entries(("Q001", "你好"), ("Q002", "")), events=events)

    assert isinstance(result, ProviderResult)
    assert result.audio_by_id == {"Q001": b"mp3-bytes-1"}
    assert "空文本跳过合成: Q002" in result.warnings

    # 请求顺序：create → query → retrieve_content
    assert [(m, u) for m, u, _ in fake.calls] == [
        ("POST", "https://api.test/v1/t2a_async_v2"),
        ("GET", "https://api.test/v1/query/t2a_async_query_v2?task_id=456"),
        ("GET", "https://api.test/v1/files/retrieve_content?file_id=901"),
    ]
    assert fake.calls[0][2]["json"]["text"] == "你好"
    assert fake.calls[0][2]["headers"]["Authorization"] == "Bearer test-key"

    # 事件：起始 create(0/1) + 每条完成 download(1/1)
    assert _stages(events) == [("create", 0, 1), ("download", 1, 1)]
    assert "开始合成 1 条" in events[0].message
    assert events[1].message == "已完成 1/1"


def test_synthesize_batch_maps_each_entry_to_its_own_audio(
        mock_http, register_happy_path, make_tar, mini_cfg):
    fake = mock_http()
    for fid, payload in ((901, b"mp3-1"), (902, b"mp3-2"), (903, b"mp3-3")):
        fake.add_get("retrieve_content?file_id=%d" % fid,
                     FakeResponse(content=make_tar({"ignored": payload})))
    register_happy_path(
        fake, b"", task_states=[_ok_query(901), _ok_query(902), _ok_query(903)])
    events = []

    result = _run(mini_cfg, _entries(("Q001", "a"), ("Q002", "b"), ("Q003", "c")),
                  events=events)

    assert result.audio_by_id == {"Q001": b"mp3-1", "Q002": b"mp3-2", "Q003": b"mp3-3"}
    assert result.warnings == []
    assert _stages(events) == [("create", 0, 3),
                               ("download", 1, 3), ("download", 2, 3), ("download", 3, 3)]


def test_synthesize_batch_whitespace_text_is_skipped(mock_http, mini_cfg):
    fake = mock_http()
    result = _run(mini_cfg, _entries(("Q001", "   "), ("Q002", "\t\n")))
    assert result.audio_by_id == {}
    assert "空文本跳过合成: Q001" in result.warnings
    assert "空文本跳过合成: Q002" in result.warnings
    assert fake.calls == []


def test_synthesize_batch_single_item_failure_degrades(mock_http, mini_cfg, make_tar):
    fake = mock_http()
    fake.add_post("/v1/t2a_async_v2", [_ok_create(), FakeResponse(status_code=500)])
    fake.add_get("t2a_async_query_v2", _ok_query())
    fake.add_get("retrieve_content", FakeResponse(content=make_tar({"Q001": b"good"})))

    events = []
    result = _run(mini_cfg, _entries(("Q001", "a"), ("Q002", "b")), events=events)

    assert result.audio_by_id == {"Q001": b"good"}
    assert len(result.warnings) == 1
    assert "Q002" in result.warnings[0]
    assert "服务器错误" in result.warnings[0]
    assert _stages(events) == [("create", 0, 2), ("download", 1, 2), ("download", 2, 2)]


def test_synthesize_batch_poll_failure_degrades(mock_http, register_happy_path, mini_cfg):
    fake = mock_http()
    # 两条都用同一 query 响应；第一条成功，第二条 create 成功但 query failed
    fake.add_post("/v1/t2a_async_v2", [_ok_create(11), _ok_create(22)])
    fake.add_get("t2a_async_query_v2",
                 [_ok_query(), {"status": "Failed", "base_resp": {"status_code": 0}}])
    fake.add_get("retrieve_content",
                 FakeResponse(content=_member_tar([("x.mp3", b"m")])))

    result = _run(mini_cfg, _entries(("Q001", "a"), ("Q002", "b")))
    assert result.audio_by_id == {"Q001": b"m"}
    assert any("Q002" in w and "failed" in w for w in result.warnings)


def test_synthesize_batch_unknown_error_degrades(mock_http, mini_cfg, monkeypatch):
    fake = mock_http()

    def boom(text, cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(minimax, "_create_task", boom)
    result = _run(mini_cfg, _entries(("Q001", "a")))
    assert result.audio_by_id == {}
    assert any("Q001" in w and "未知错误" in w for w in result.warnings)
    assert fake.calls == []


def test_synthesize_batch_fatal_auth_fast_fail(mock_http, mini_cfg):
    fake = mock_http()
    fake.add_post("/v1/t2a_async_v2", FakeResponse(status_code=401))
    fake.add_get("t2a_async_query_v2", _ok_query())
    fake.add_get("retrieve_content", FakeResponse(content=b""))
    token = CancelToken()

    with pytest.raises(APIError) as ei:
        _run(mini_cfg, _entries(("Q001", "a"), ("Q002", "b")), token=token)

    assert ei.value.error_type == "auth"
    assert token.cancelled
    # 快速失败：绝不进入轮询/下载阶段
    assert [u for m, u, _ in fake.calls if m == "GET"] == []


def test_synthesize_batch_fatal_quota_fast_fail(mock_http, mini_cfg):
    fake = mock_http()
    fake.add_post("/v1/t2a_async_v2",
                  {"base_resp": {"status_code": 2049, "status_msg": "insufficient quota"}})
    fake.add_get("t2a_async_query_v2", _ok_query())
    token = CancelToken()

    with pytest.raises(APIError) as ei:
        _run(mini_cfg, _entries(("Q001", "a"), ("Q002", "b")), token=token)

    assert ei.value.error_type == "quota"
    assert token.cancelled
    assert [u for m, u, _ in fake.calls if m == "GET"] == []


def test_fatal_error_types_constant():
    assert tuple(minimax.FATAL_ERROR_TYPES) == ("auth", "quota")


def test_synthesize_batch_pre_cancelled_no_request(mock_http, register_happy_path, mini_cfg):
    fake = mock_http()
    register_happy_path(fake, b"")
    token = CancelToken()
    token.cancel()

    with pytest.raises(CancelledError):
        _run(mini_cfg, _entries(("Q001", "a")), token=token)
    assert fake.calls == []


def test_synthesize_batch_cancel_after_first_item(mock_http, register_happy_path,
                                                  make_tar, mini_cfg):
    fake = mock_http()
    register_happy_path(fake, make_tar({"Q001": b"x"}))
    token = CancelToken()
    events = []

    def on_event(ev):
        events.append(ev)
        if ev.stage == "download" and ev.current == 1:
            token.cancel()

    with pytest.raises(CancelledError):
        MinimaxProvider().synthesize_batch(
            _entries(("Q001", "a"), ("Q002", "b")), mini_cfg, on_event, token)

    assert _stages(events) == [("create", 0, 2), ("download", 1, 2)]
    assert token.cancelled


def test_synthesize_batch_all_empty_returns_warnings_only(mock_http, register_happy_path,
                                                          mini_cfg):
    fake = mock_http()
    register_happy_path(fake, b"")
    events = []

    result = _run(mini_cfg, _entries(("Q001", ""), ("Q002", "  ")), events=events)

    assert result.audio_by_id == {}
    assert result.warnings == ["空文本跳过合成: Q001", "空文本跳过合成: Q002"]
    assert fake.calls == []
    assert events == []                       # 无待合成条目时不发进度事件


def test_synthesize_batch_missing_api_key_raises_config_error(mock_http, mini_cfg):
    mini_cfg.pop("api_key")
    with pytest.raises(ConfigError):
        _run(mini_cfg, _entries(("Q001", "a")))


def test_concurrency_passed_to_thread_pool(mock_http, register_happy_path, make_tar,
                                           mini_cfg, monkeypatch):
    seen = []
    real_executor = minimax.ThreadPoolExecutor

    class SpyExecutor(real_executor):
        def __init__(self, *args, **kwargs):
            seen.append(kwargs.get("max_workers", args[0] if args else None))
            real_executor.__init__(self, *args, **kwargs)

    fake = mock_http()
    register_happy_path(fake, make_tar({"Q001": b"z"}))
    monkeypatch.setattr(minimax, "ThreadPoolExecutor", SpyExecutor)

    _run(mini_cfg, _entries(("Q001", "a")))
    assert seen == [1]                        # mini_cfg 的 concurrency=1


def test_concurrency_default_and_floor(mock_http, register_happy_path, make_tar,
                                       mini_cfg, monkeypatch):
    seen = []
    real_executor = minimax.ThreadPoolExecutor

    class SpyExecutor(real_executor):
        def __init__(self, *args, **kwargs):
            seen.append(kwargs.get("max_workers", args[0] if args else None))
            real_executor.__init__(self, *args, **kwargs)

    monkeypatch.setattr(minimax, "ThreadPoolExecutor", SpyExecutor)

    cfg = dict(mini_cfg)
    cfg.pop("concurrency")
    fake1 = mock_http()                        # 每次跑一条，独立 fake
    register_happy_path(fake1, make_tar({"Q001": b"z"}))
    _run(cfg, _entries(("Q001", "a")))
    assert seen == [5]                        # 默认并发 5

    cfg2 = dict(mini_cfg)
    cfg2["concurrency"] = 0
    fake2 = mock_http()
    register_happy_path(fake2, make_tar({"Q001": b"z"}))
    _run(cfg2, _entries(("Q001", "a")))
    assert seen == [5, 1]                     # 下限 1


def test_synthesize_batch_concurrent_workers_complete(mock_http, mini_cfg, make_tar):
    """并发 > 1 冒烟：7 条目 3 worker 全部完成（响应均为可重复单值，无出队竞态）。"""
    fake = mock_http()
    fake.add_post("/v1/t2a_async_v2", _ok_create())
    fake.add_get("t2a_async_query_v2", _ok_query())
    fake.add_get("retrieve_content", FakeResponse(content=make_tar({"Q001": b"z"})))
    cfg = dict(mini_cfg)
    cfg["concurrency"] = 3
    events = []

    result = _run(cfg, _entries(*[("Q%03d" % i, "文本%d" % i) for i in range(1, 8)]),
                  events=events)

    assert len(result.audio_by_id) == 7
    assert set(result.audio_by_id) == {"Q%03d" % i for i in range(1, 8)}
    assert result.warnings == []
    # 进度事件的 current 由完成计数器驱动，必然 1..7 递增
    assert _stages(events) == [("create", 0, 7)] + \
        [("download", i, 7) for i in range(1, 8)]
    assert sum(1 for m, _, _ in fake.calls if m == "POST") == 7
    assert sum(1 for _, u, _ in fake.calls if "t2a_async_query_v2" in u) == 7
    assert sum(1 for _, u, _ in fake.calls if "retrieve_content" in u) == 7


# ============================================ 任务中心新原语（zip 批量单任务链路）
# 「任务中心 + 单任务全量转换」重构后，批量链路由无状态原语组成：
#   submit_batch —— _build_zip_named → _upload(purpose=t2a_async_input)
#                    → _create_batch_task(text_file_id 整型)，不轮询，返回三元组；
#   query_task   —— 单次状态查询，base_resp 错误（如 task not found）抛 APIError；
#   _download_tar / extract_named_mp3s —— 结果 tar 按 "d<目录码><语音ID>" 解包。
# synthesize_batch 不再执行 batch 模式（改由 pipeline 编排），直接调用即 ConfigError。

_TAR_CODES = "2038465880412660024_202604152300_387938953298334"  # 结果 tar 目录前缀样本


def _zip_texts(blob):
    """zip 字节 -> {成员名: 文本}（utf-8）。"""
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return {n: zf.read(n).decode("utf-8") for n in zf.namelist()}


def _named_tar(pairs):
    """[(条目编码, bytes)] -> 与 MiniMax 结果一致的 tar 字节。

    成员名 "<前缀>_<编码>/content-<前缀>_<编码>.mp3"（目录段与文件名段都含编码）。
    """
    return _member_tar([
        ("%s_%s/content-%s_%s.mp3" % (_TAR_CODES, code, _TAR_CODES, code), data)
        for code, data in pairs
    ])


def _t2s_tmp_dirs():
    """当前系统临时目录下本项目遗留的 t2s_* 工作目录集合。"""
    tmp_root = tempfile.gettempdir()
    return {n for n in os.listdir(tmp_root) if n.startswith("t2s_")}


def _spy_zip_builder(monkeypatch, sink):
    """包装 _build_zip_named：把产物路径记录到 sink，便于断言临时文件清理。"""
    real = minimax._build_zip_named

    def spy(pairs):
        path = real(pairs)
        sink.append(path)
        return path

    monkeypatch.setattr(minimax, "_build_zip_named", spy)
    return sink


# ---------------------------------------------------------------- submit_batch

class TestSubmitBatch:
    """submit_batch(zip_entries, cfg) -> (task_id, upload_file_id, usage_characters)。"""

    def test_submit_batch_returns_triple(self, mock_http_upload, mini_cfg):
        fake = mock_http_upload()
        fake.add_post("files/upload",
                      {"file": {"file_id": 123}, "base_resp": {"status_code": 0}})
        fake.add_post("/v1/t2a_async_v2", _ok_create(456))

        tid, upload_fid, usage = minimax.submit_batch(
            [("d0001Q001.txt", "你好"), ("d0001Q002.txt", "世界")], mini_cfg)

        assert (tid, upload_fid, usage) == ("456", 123, 10)
        assert isinstance(tid, str) and isinstance(upload_fid, int)
        assert isinstance(usage, int)
        # 只发两次请求：upload → create（不轮询）
        assert [(m, u) for m, u, _ in fake.calls] == [
            ("POST", "https://api.test/v1/files/upload"),
            ("POST", "https://api.test/v1/t2a_async_v2"),
        ]
        assert not [c for c in fake.calls if c[0] == "GET"]

    def test_submit_batch_upload_multipart_shape(self, mock_http_upload, mini_cfg):
        fake = mock_http_upload()
        fake.add_post("files/upload",
                      {"file": {"file_id": 123}, "base_resp": {"status_code": 0}})
        fake.add_post("/v1/t2a_async_v2", _ok_create(456))

        minimax.submit_batch([("d0002Q007.txt", "hello")], mini_cfg)

        method, url, kw = fake.calls[0]
        assert method == "POST" and url == "https://api.test/v1/files/upload"
        assert kw["data"] == {"purpose": "t2a_async_input"}      # 固定 purpose
        assert kw["headers"]["Authorization"] == "Bearer test-key"
        assert kw["timeout"] == mini_cfg["upload_timeout"]
        # multipart 字段：文件对象在请求内被读取，字节即 zip 内容
        assert len(fake.uploads) == 1
        up_url, member_name, blob, ctype = fake.uploads[0]
        assert up_url == url
        assert member_name == "batch.zip" and ctype == "application/zip"
        assert blob[:2] == b"PK"
        assert _zip_texts(blob) == {"d0002Q007.txt": "hello"}

    def test_submit_batch_zip_members_and_texts(self, mock_http_upload, mini_cfg):
        """zip 内每个 (文件名, 文本) 一个成员，utf-8 编码，顺序一致。"""
        fake = mock_http_upload()
        fake.add_post("files/upload",
                      {"file": {"file_id": 123}, "base_resp": {"status_code": 0}})
        fake.add_post("/v1/t2a_async_v2", _ok_create(456))
        pairs = [("d0001Q001.txt", "你好"), ("d0001Q003.txt", "  空白  "),
                 ("d0002Q001.txt", "world")]

        minimax.submit_batch(pairs, mini_cfg)

        texts = _zip_texts(fake.uploads[0][2])
        assert list(texts) == [p[0] for p in pairs]
        assert texts == {"d0001Q001.txt": "你好", "d0001Q003.txt": "  空白  ",
                         "d0002Q001.txt": "world"}
        assert "你好" in texts["d0001Q001.txt"]                   # 中文按 utf-8 保存

    def test_submit_batch_create_payload_uses_int_file_id(self, mock_http_upload,
                                                          mini_cfg):
        fake = mock_http_upload()
        fake.add_post("files/upload",
                      {"file": {"file_id": "888"}, "base_resp": {"status_code": 0}})
        fake.add_post("/v1/t2a_async_v2", _ok_create(456))

        tid, upload_fid, _usage = minimax.submit_batch(
            [("d0001Q001.txt", "a")], mini_cfg)

        assert upload_fid == 888                                  # 字符串转整型
        payload = fake.calls[1][2]["json"]
        assert isinstance(payload["text_file_id"], int)           # 必须整型
        assert payload["text_file_id"] == 888
        assert "text" not in payload
        assert set(payload) == {"model", "text_file_id", "voice_setting",
                                "audio_setting"}
        assert fake.calls[1][2]["headers"]["Content-Type"] == "application/json"

    def test_submit_batch_usage_characters_defaults_zero(self, mock_http_upload,
                                                         mini_cfg):
        fake = mock_http_upload()
        fake.add_post("files/upload",
                      {"file": {"file_id": 1}, "base_resp": {"status_code": 0}})
        fake.add_post("/v1/t2a_async_v2", {"task_id": "t-1",
                                           "base_resp": {"status_code": 0}})

        tid, upload_fid, usage = minimax.submit_batch(
            [("d0001Q001.txt", "a")], mini_cfg)

        assert (tid, upload_fid, usage) == ("t-1", 1, 0)

    def test_submit_batch_cleans_temp_zip(self, mock_http_upload, mini_cfg,
                                          monkeypatch):
        fake = mock_http_upload()
        fake.add_post("files/upload",
                      {"file": {"file_id": 123}, "base_resp": {"status_code": 0}})
        fake.add_post("/v1/t2a_async_v2", _ok_create(456))
        built = _spy_zip_builder(monkeypatch, [])
        before = _t2s_tmp_dirs()

        minimax.submit_batch([("d0001Q001.txt", "a")], mini_cfg)

        assert len(built) == 1
        assert not os.path.exists(built[0])                       # zip 已删
        assert not os.path.isdir(os.path.dirname(built[0]))       # 空工作目录一并移除
        assert _t2s_tmp_dirs() == before                          # 临时目录无残留

    def test_submit_batch_upload_auth_error_raises_and_cleans(self, mock_http_upload,
                                                              mini_cfg, monkeypatch):
        fake = mock_http_upload()
        fake.add_post("files/upload", FakeResponse(status_code=401))
        built = _spy_zip_builder(monkeypatch, [])
        before = _t2s_tmp_dirs()

        with pytest.raises(APIError) as ei:
            minimax.submit_batch([("d0001Q001.txt", "a")], mini_cfg)

        assert ei.value.error_type == "auth"
        assert ei.value.status_code == 401
        assert "上传" in str(ei.value)
        assert len(fake.calls) == 1                               # 未到 create 阶段
        assert built and not os.path.exists(built[0])             # finally 仍清理
        assert _t2s_tmp_dirs() == before

    def test_submit_batch_create_without_task_id_raises(self, mock_http_upload,
                                                        mini_cfg, monkeypatch):
        fake = mock_http_upload()
        fake.add_post("files/upload",
                      {"file": {"file_id": 123}, "base_resp": {"status_code": 0}})
        fake.add_post("/v1/t2a_async_v2", {"base_resp": {"status_code": 0}})
        built = _spy_zip_builder(monkeypatch, [])

        with pytest.raises(APIError) as ei:
            minimax.submit_batch([("d0001Q001.txt", "a")], mini_cfg)

        assert ei.value.error_type == "api"
        assert "task_id" in str(ei.value)
        assert built and not os.path.exists(built[0])

    def test_submit_batch_create_quota_error_propagates(self, mock_http_upload,
                                                        mini_cfg):
        fake = mock_http_upload()
        fake.add_post("files/upload",
                      {"file": {"file_id": 123}, "base_resp": {"status_code": 0}})
        fake.add_post("/v1/t2a_async_v2",
                      {"base_resp": {"status_code": 2049,
                                     "status_msg": "insufficient quota"}})

        with pytest.raises(APIError) as ei:
            minimax.submit_batch([("d0001Q001.txt", "a")], mini_cfg)

        assert ei.value.error_type == "quota"
        assert [u for m, u, _ in fake.calls] == [
            "https://api.test/v1/files/upload",
            "https://api.test/v1/t2a_async_v2",
        ]


# ------------------------------------------------- zip / upload / create 辅助原语

class TestBatchHelperPrimitives:
    """_build_zip_named / _upload / _create_batch_task / _download_tar / _cleanup。"""

    def test_build_zip_named_writes_one_txt_per_pair(self):
        zip_path = minimax._build_zip_named(
            [("d0001Q001.txt", "你好"), ("d0002Q003.txt", "world")])
        try:
            assert os.path.basename(zip_path) == "batch.zip"
            with open(zip_path, "rb") as f:
                blob = f.read()
            assert _zip_texts(blob) == {"d0001Q001.txt": "你好",
                                        "d0002Q003.txt": "world"}
        finally:
            minimax._cleanup(zip_path)
        assert not os.path.exists(zip_path)
        assert not os.path.isdir(os.path.dirname(zip_path))

    def test_upload_returns_int_file_id_and_defaults(self, mock_http, mini_cfg):
        fake = mock_http()
        fake.add_post("files/upload",
                      {"file": {"file_id": "888"}, "base_resp": {"status_code": 0}})
        fd, path = tempfile.mkstemp(suffix=".zip")
        try:
            os.write(fd, b"PK\x03\x04 fake zip")
            os.close(fd)
            fid = minimax._upload(path, mini_cfg)
        finally:
            os.unlink(path)
        assert fid == 888
        assert isinstance(fid, int)
        method, url, kw = fake.calls[0]
        assert (method, url) == ("POST", "https://api.test/v1/files/upload")
        assert kw["data"] == {"purpose": minimax.DEFAULT_UPLOAD_PURPOSE}
        assert "file" in kw["files"]
        assert kw["timeout"] == mini_cfg["upload_timeout"]

    def test_upload_purpose_override(self, mock_http, mini_cfg):
        fake = mock_http()
        fake.add_post("files/upload",
                      {"file": {"file_id": 1}, "base_resp": {"status_code": 0}})
        fd, path = tempfile.mkstemp(suffix=".zip")
        try:
            minimax._upload(path, dict(mini_cfg, upload_purpose="custom_type"))
        finally:
            os.unlink(path)
        assert fake.calls[0][2]["data"] == {"purpose": "custom_type"}

    @pytest.mark.parametrize("body", [
        {"base_resp": {"status_code": 0}},                          # 无 file 对象
        {"file": {}, "base_resp": {"status_code": 0}},              # file 内无 file_id
        {"file": {"file_id": 0}, "base_resp": {"status_code": 0}},  # 0 视为缺失
    ])
    def test_upload_missing_file_id_raises_api(self, mock_http, mini_cfg, body):
        fake = mock_http()
        fake.add_post("files/upload", body)
        fd, path = tempfile.mkstemp(suffix=".zip")
        try:
            with pytest.raises(APIError) as ei:
                minimax._upload(path, mini_cfg)
        finally:
            os.unlink(path)
        assert ei.value.error_type == "api"
        assert "file_id" in str(ei.value)

    def test_upload_legacy_top_level_file_id(self, mock_http, mini_cfg):
        """兼容旧版响应：file_id 位于顶层。"""
        fake = mock_http()
        fake.add_post("files/upload", {"file_id": 55,
                                       "base_resp": {"status_code": 0}})
        fd, path = tempfile.mkstemp(suffix=".zip")
        try:
            assert minimax._upload(path, mini_cfg) == 55
        finally:
            os.unlink(path)

    def test_create_batch_task_returns_task_id_and_usage(self, mock_http, mini_cfg):
        fake = mock_http()
        fake.add_post("/v1/t2a_async_v2", _ok_create(456))

        tid, usage = minimax._create_batch_task("123", mini_cfg)   # 入参兼容 str

        assert (tid, usage) == ("456", 10)
        assert isinstance(tid, str) and isinstance(usage, int)
        method, url, kw = fake.calls[0]
        assert (method, url) == ("POST", "https://api.test/v1/t2a_async_v2")
        payload = kw["json"]
        assert isinstance(payload["text_file_id"], int)            # payload 内整型
        assert payload["text_file_id"] == 123
        assert "text" not in payload

    def test_create_batch_task_without_task_id_raises_api(self, mock_http, mini_cfg):
        fake = mock_http()
        fake.add_post("/v1/t2a_async_v2", {"base_resp": {"status_code": 0}})
        with pytest.raises(APIError) as ei:
            minimax._create_batch_task(123, mini_cfg)
        assert ei.value.error_type == "api"
        assert "task_id" in str(ei.value)

    def test_download_tar_returns_raw_bytes(self, mock_http, mini_cfg):
        fake = mock_http()
        blob = _named_tar([("d0001Q001", b"not-parsed-here")])
        fake.add_get("retrieve_content", FakeResponse(content=blob))

        assert minimax._download_tar("901", mini_cfg) == blob      # 不解包，原样返回
        method, url, kw = fake.calls[0]
        assert method == "GET"
        assert url == "https://api.test/v1/files/retrieve_content?file_id=901"
        assert kw["timeout"] == mini_cfg["download_timeout"]
        assert kw["headers"]["Authorization"] == "Bearer test-key"

    def test_download_tar_empty_and_5xx_map(self, mock_http, mini_cfg):
        fake = mock_http()
        fake.add_get("retrieve_content", [
            FakeResponse(status_code=200, content=b""),
            FakeResponse(status_code=500),
        ])
        with pytest.raises(APIError) as ei:
            minimax._download_tar("901", mini_cfg)
        assert ei.value.error_type == "empty"
        with pytest.raises(APIError) as ei2:
            minimax._download_tar("901", mini_cfg)
        assert ei2.value.error_type == "server"
        assert "下载" in str(ei2.value)

    def test_cleanup_tolerates_missing_path_and_keeps_nonempty_dir(self, tmp_path):
        gone = str(tmp_path / "nope" / "batch.zip")
        minimax._cleanup(gone)                                    # 不抛
        work = tmp_path / "t2s_work"
        work.mkdir()
        target = work / "batch.zip"
        sibling = work / "keep.txt"
        for p in (sibling, target):
            p.write_text("x", encoding="utf-8")
        minimax._cleanup(str(target))
        assert not target.exists()
        assert work.is_dir()                                      # 非空目录保留
        minimax._cleanup(str(sibling))
        assert not work.exists()                                  # 变空后一并移除


# ---------------------------------------------------------------------- query_task

class TestQueryTask:
    """query_task(task_id, cfg) -> (status 小写, file_id 或 None)。"""

    def test_query_task_processing_returns_no_file_id(self, mock_http, mini_cfg):
        fake = mock_http()
        fake.add_get("t2a_async_query_v2",
                     {"status": "Processing", "base_resp": {"status_code": 0}})

        assert minimax.query_task("456", mini_cfg) == ("processing", None)
        method, url, kw = fake.calls[0]
        assert method == "GET"
        assert url == "https://api.test/v1/query/t2a_async_query_v2?task_id=456"
        assert kw["timeout"] == mini_cfg["request_timeout"]
        assert kw["headers"]["Authorization"] == "Bearer test-key"

    def test_query_task_success_stringifies_file_id(self, mock_http, mini_cfg):
        fake = mock_http()
        fake.add_get("t2a_async_query_v2", _ok_query(file_id=901))

        status, fid = minimax.query_task("456", mini_cfg)

        assert status == "success" and fid == "901"
        assert isinstance(fid, str)

    @pytest.mark.parametrize("raw,expected", [
        ("Processing", "processing"),
        ("PROCESSING", "processing"),
        ("Awaiting", "awaiting"),
        ("Failed", "failed"),
        ("FAILED", "failed"),
        ("Expired", "expired"),
        ("", ""),
    ])
    def test_query_task_status_lowercased_passthrough(self, mock_http, mini_cfg,
                                                      raw, expected):
        fake = mock_http()
        fake.add_get("t2a_async_query_v2", {"status": raw})

        assert minimax.query_task("1", mini_cfg) == (expected, None)
        assert len(fake.calls) == 1                               # 单次查询，不轮询

    def test_query_task_zero_file_id_is_none(self, mock_http, mini_cfg):
        fake = mock_http()
        fake.add_get("t2a_async_query_v2", {"status": "Success", "file_id": 0,
                                            "base_resp": {"status_code": 0}})

        assert minimax.query_task("1", mini_cfg) == ("success", None)

    def test_query_task_base_resp_task_not_found_raises(self, mock_http, mini_cfg):
        fake = mock_http()
        fake.add_get("t2a_async_query_v2", FakeResponse(status_code=200, json_data={
            "base_resp": {"status_code": 2013, "status_msg": "task not found"}}))

        with pytest.raises(APIError) as ei:
            minimax.query_task("999", mini_cfg)

        assert ei.value.error_type == "api"
        assert "task not found" in str(ei.value)
        assert "查询" in str(ei.value)
        assert len(fake.calls) == 1

    def test_query_task_base_resp_not_found_chinese(self, mock_http, mini_cfg):
        fake = mock_http()
        fake.add_get("t2a_async_query_v2", {
            "base_resp": {"status_code": 2013, "status_msg": "任务不存在"}})

        with pytest.raises(APIError) as ei:
            minimax.query_task("999", mini_cfg)

        assert "任务不存在" in str(ei.value)

    @pytest.mark.parametrize("code,expected", [
        (401, "auth"),
        (429, "quota"),
        (503, "server"),
    ])
    def test_query_task_http_status_maps(self, mock_http, mini_cfg, code, expected):
        fake = mock_http()
        fake.add_get("t2a_async_query_v2", FakeResponse(status_code=code))
        with pytest.raises(APIError) as ei:
            minimax.query_task("1", mini_cfg)
        assert ei.value.error_type == expected
        assert ei.value.status_code == code

    def test_query_task_bad_json_maps_parse(self, mock_http, mini_cfg):
        fake = mock_http()
        fake.add_get("t2a_async_query_v2",
                     FakeResponse(status_code=200, json_data=ValueError("bad")))
        with pytest.raises(APIError) as ei:
            minimax.query_task("1", mini_cfg)
        assert ei.value.error_type == "parse"

    def test_poll_delegates_to_query_task(self, mock_http, mini_cfg, monkeypatch):
        """_poll 现在内部调 query_task（复用同一套错误映射）。"""
        fake = mock_http()
        seen = []

        def spy(task_id, cfg):
            seen.append(task_id)
            if len(seen) == 1:
                return "processing", None
            return "success", "777"

        monkeypatch.setattr(minimax, "query_task", spy)
        monkeypatch.setattr(minimax, "time", _FakeTime())

        assert minimax._poll("42", mini_cfg, CancelToken()) == "777"
        assert seen == ["42", "42"]
        assert fake.calls == []                                   # 未走真实 http

    def test_poll_forwards_progress_events(self, mock_http, mini_cfg):
        fake = mock_http()
        fake.add_get("t2a_async_query_v2",
                     [{"status": "Processing"}, {"status": "Processing"},
                      _ok_query()])
        events = []

        fid = minimax._poll("1", mini_cfg, CancelToken(),
                            on_event=_events_sink(events))

        assert fid == "901"
        polls = [e for e in events if e.stage == "poll"]
        assert [p.current for p in polls] == [1, 2, 3]
        for p in polls:
            assert "已等待" in p.message and "秒" in p.message
            assert "processing" not in p.message                  # 只显示累计秒数


# ------------------------------------------------------------ extract_named_mp3s

class TestExtractNamedMp3s:
    """extract_named_mp3s(blob) -> ({d码+语音ID: mp3字节}, warnings)。"""

    def test_extract_returns_code_keys_for_four_members(self):
        blob = _named_tar([("d0001Q001", b"zh-1"), ("d0001Q003", b"zh-3"),
                           ("d0002Q001", b"en-1"), ("d0002Q003", b"en-3")])

        audio, warnings = minimax.extract_named_mp3s(blob)

        assert set(audio) == {"d0001Q001", "d0001Q003", "d0002Q001", "d0002Q003"}
        assert audio["d0001Q003"] == b"zh-3" and audio["d0002Q001"] == b"en-1"
        assert warnings == []

    def test_extract_from_make_tar_layout(self, make_tar):
        """conftest.make_tar 的目录布局（含 DIRTYPE 成员）也能按编码解出。"""
        blob = make_tar({"d0001Q001": b"a", "d0002Q002": b"bb"})

        audio, warnings = minimax.extract_named_mp3s(blob)

        assert audio == {"d0001Q001": b"a", "d0002Q002": b"bb"}
        assert warnings == []

    def test_extract_ignores_non_mp3_members(self):
        blob = _member_tar([("readme.txt", b"hi"), ("notes.json", b"{}")])

        audio, warnings = minimax.extract_named_mp3s(blob)

        assert audio == {}
        assert warnings == []                                     # 非 mp3 静默跳过

    def test_extract_accepts_uppercase_extension(self):
        blob = _member_tar([("pack_d0001Q001/content-d0001Q001.MP3", b"up")])

        audio, warnings = minimax.extract_named_mp3s(blob)

        assert audio == {"d0001Q001": b"up"}
        assert warnings == []

    def test_extract_unknown_member_warns(self):
        blob = _member_tar([("misc/content-plain.mp3", b"junk")])

        audio, warnings = minimax.extract_named_mp3s(blob)

        assert audio == {}
        assert len(warnings) == 1
        assert "未知成员" in warnings[0]
        assert "content-plain.mp3" in warnings[0]

    def test_extract_duplicate_code_overrides_with_warning(self):
        blob = _member_tar([("a_d0001Q001/content-x_d0001Q001.mp3", b"first"),
                            ("b_d0001Q001/content-y_d0001Q001.mp3", b"second")])

        audio, warnings = minimax.extract_named_mp3s(blob)

        assert audio == {"d0001Q001": b"second"}                  # 后者覆盖
        assert len(warnings) == 1
        assert "重复" in warnings[0] and "d0001Q001" in warnings[0]

    def test_extract_empty_mp3_warns_and_skips(self):
        blob = _named_tar([("d0001Q001", b""), ("d0001Q002", b"ok")])

        audio, warnings = minimax.extract_named_mp3s(blob)

        assert audio == {"d0001Q002": b"ok"}                      # 空数据不入 dict
        assert len(warnings) == 1
        assert "数据为空" in warnings[0] and "d0001Q001" in warnings[0]

    def test_extract_non_tar_raises_parse(self):
        with pytest.raises(APIError) as ei:
            minimax.extract_named_mp3s(b"this is definitely not a tar file")
        assert ei.value.error_type == "parse"
        assert "tar" in str(ei.value)

    def test_extract_prefers_basename_code(self):
        """目录段与文件名段都含编码时，basename 优先。"""
        blob = _member_tar([("pack_d0001Q001/content-x_d0002Q003.mp3", b"data")])

        audio, warnings = minimax.extract_named_mp3s(blob)

        assert audio == {"d0002Q003": b"data"}
        assert warnings == []

    def test_extract_falls_back_to_full_name_when_basename_has_no_code(self):
        """basename 无编码时回退整名匹配（真实结果包目录段带编码）。"""
        blob = _member_tar([("outer_d0003Q009/content-plain.mp3", b"data")])

        audio, warnings = minimax.extract_named_mp3s(blob)

        assert audio == {"d0003Q009": b"data"}
        assert warnings == []

    def test_extract_empty_tar_returns_empty_dict(self):
        audio, warnings = minimax.extract_named_mp3s(_member_tar([]))
        assert audio == {}
        assert warnings == []


# --------------------------------------------- provider 不再直接执行 batch 模式

class TestBatchModeRejectedByProvider:
    """tts_mode=batch 的编排已上移到 pipeline；provider 层直接调用必须拒绝。"""

    def test_synthesize_batch_batch_mode_raises_config_error(self, mock_http,
                                                             mini_cfg):
        fake = mock_http()
        fake.add_post("files/upload", {"file": {"file_id": 1},
                                       "base_resp": {"status_code": 0}})
        events = []

        with pytest.raises(ConfigError) as ei:
            _run(dict(mini_cfg, tts_mode="batch"), _entries(("Q001", "a")),
                 events=events)

        assert "batch" in str(ei.value)
        assert fake.calls == []                                   # 一次请求都不发
        assert events == []

    def test_synthesize_batch_default_mode_raises_config_error(self, mock_http,
                                                               mini_cfg):
        """缺省 tts_mode 即 batch（PROVIDER_DEFAULTS），provider 层同样拒绝。"""
        fake = mock_http()
        cfg = dict(mini_cfg)
        cfg.pop("tts_mode")

        with pytest.raises(ConfigError):
            _run(cfg, _entries(("Q001", "a")))

        assert fake.calls == []

    def test_synthesize_batch_sync_mode_still_works(self, mock_http, mini_cfg):
        """回归：sync 逐条链路不受 batch 上移影响。"""
        fake = mock_http()
        mp3 = b"\xff\xfb\x90\x00body"
        fake.add_post("/v1/t2a_v2",
                      {"data": {"audio": mp3.hex()}, "base_resp": {"status_code": 0}})

        result = _run(dict(mini_cfg, tts_mode="sync"), _entries(("Q001", "a")))

        assert result.audio_by_id == {"Q001": mp3}
        assert not any("files/upload" in u for _, u, _ in fake.calls)


# ================================================================ sync 模式
# tts_mode="sync"：POST /v1/t2a_v2 一次请求返回 hex 音频；超过 sync_max_chars
# 回退异步逐条链路。

class TestSyncMode:
    """同步接口链路与模式选择（sync/async 分界、回退）。"""

    def test_sync_happy_path(self, mock_http, mini_cfg):
        fake = mock_http()
        mp3 = b"\xff\xfb\x90\x00payload-bytes"
        fake.add_post("/v1/t2a_v2",
                      {"data": {"audio": mp3.hex()}, "base_resp": {"status_code": 0}})
        cfg = dict(mini_cfg, tts_mode="sync")
        events = []

        result = _run(cfg, _entries(("Q001", "你好")), events=events)

        assert result.audio_by_id == {"Q001": mp3}              # hex → 原始字节
        assert result.warnings == []
        assert [(m, u) for m, u, _ in fake.calls] == [
            ("POST", "https://api.test/v1/t2a_v2")]
        method, url, kw = fake.calls[0]
        assert kw["headers"]["Content-Type"] == "application/json"
        payload = kw["json"]
        assert payload["text"] == "你好"
        assert payload["model"] == "speech-2.8-hd"
        assert payload["voice_setting"] == {"voice_id": "v1"}
        assert payload["audio_setting"] == {"format": "mp3"}
        assert set(payload.keys()) == {"model", "text", "voice_setting",
                                       "audio_setting"}
        assert _stages(events) == [("create", 0, 1), ("download", 1, 1)]

    def test_sync_fatal_quota_raises_whole_batch(self, mock_http, mini_cfg):
        fake = mock_http()
        fake.add_post("/v1/t2a_v2",
                      {"base_resp": {"status_code": 2049,
                                     "status_msg": "insufficient quota"}})
        cfg = dict(mini_cfg, tts_mode="sync")
        token = CancelToken()

        with pytest.raises(APIError) as ei:                     # quota ∈ FATAL
            _run(cfg, _entries(("Q001", "a"), ("Q002", "b")), token=token)

        assert ei.value.error_type == "quota"
        assert token.cancelled

    def test_sync_missing_audio_raises_api(self, mock_http, mini_cfg):
        fake = mock_http()
        fake.add_post("/v1/t2a_v2", {"data": {}, "base_resp": {"status_code": 0}})
        with pytest.raises(APIError) as ei:
            minimax._synthesize_sync("x", dict(mini_cfg, tts_mode="sync"))
        assert ei.value.error_type == "api"
        assert "音频" in str(ei.value)

    def test_sync_bad_hex_raises_parse(self, mock_http, mini_cfg):
        fake = mock_http()
        fake.add_post("/v1/t2a_v2",
                      {"data": {"audio": "zz"}, "base_resp": {"status_code": 0}})
        with pytest.raises(APIError) as ei:
            minimax._synthesize_sync("x", dict(mini_cfg, tts_mode="sync"))
        assert ei.value.error_type == "parse"
        assert "hex" in str(ei.value)

    def test_sync_long_text_falls_back_to_async(self, mock_http, register_happy_path,
                                                make_tar, mini_cfg):
        fake = mock_http()
        register_happy_path(fake, make_tar({"Q001": b"async-audio"}))
        cfg = dict(mini_cfg, tts_mode="sync", sync_max_chars=5)

        result = _run(cfg, _entries(("Q001", "0123456789")))     # 10 字符 > 5

        assert result.audio_by_id == {"Q001": b"async-audio"}
        assert not any("/v1/t2a_v2" in u for _, u, _ in fake.calls)
        create = [c for c in fake.calls if c[0] == "POST"][0]
        assert create[1] == "https://api.test/v1/t2a_async_v2"   # 走逐条异步链路
        assert create[2]["json"]["text"] == "0123456789"

    def test_async_mode_never_calls_sync_endpoint(self, mock_http,
                                                  register_happy_path, make_tar,
                                                  mini_cfg):
        fake = mock_http()
        register_happy_path(fake, make_tar({"Q001": b"x"}),
                            task_states=[_ok_query(), _ok_query()])  # 每条一次查询
        result = _run(mini_cfg, _entries(("Q001", "短"), ("Q002", "也短")))  # 默认 async
        assert result.audio_by_id == {"Q001": b"x", "Q002": b"x"}
        assert not any("/v1/t2a_v2" in u for _, u, _ in fake.calls)

    def test_sync_on_entry_callback(self, mock_http, mini_cfg):
        fake = mock_http()
        mp3 = b"audio-bytes"
        fake.add_post("/v1/t2a_v2",
                      {"data": {"audio": mp3.hex()}, "base_resp": {"status_code": 0}})
        cfg = dict(mini_cfg, tts_mode="sync")
        seen = []

        result = _run_with_entry(
            cfg, _entries(("Q001", "a"), ("Q002", "b")),
            on_entry=lambda e, d: seen.append((e.voice_id, d)))

        assert result.audio_by_id == {"Q001": mp3, "Q002": mp3}
        assert set(seen) == {("Q001", mp3), ("Q002", mp3)}

    def test_sync_server_error_degrades(self, mock_http, mini_cfg):
        fake = mock_http()
        mp3 = b"ok"
        fake.add_post("/v1/t2a_v2", [
            FakeResponse(status_code=500),
            {"data": {"audio": mp3.hex()}, "base_resp": {"status_code": 0}},
        ])
        cfg = dict(mini_cfg, tts_mode="sync")                   # concurrency=1 顺序确定

        result = _run(cfg, _entries(("Q001", "a"), ("Q002", "b")))

        assert result.audio_by_id == {"Q002": mp3}
        assert len(result.warnings) == 1
        assert "Q001" in result.warnings[0] and "服务器错误" in result.warnings[0]


# ------------------------------------------------------------- validate_config

def test_validate_config_missing_key():
    with pytest.raises(ConfigError):
        MinimaxProvider().validate_config({"model": "m"})
    MinimaxProvider().validate_config({"api_key": "k"})  # 不应抛


def test_provider_registered():
    from t2s_tool.providers import get_provider

    provider = get_provider("minimax")
    assert provider.name == "minimax"
    assert provider.display_name == "MiniMax"


# --------------------------------------------------------------- 一致性守卫

def test_batch_mode_helpers_exist():
    """任务中心新原语必须存在且可调用；旧 batch 私有实现必须已删除。"""
    import t2s_tool.providers.minimax as m

    for fn in ("submit_batch", "query_task", "extract_named_mp3s",
               "_build_zip_named", "_upload", "_create_batch_task",
               "_download_tar", "_cleanup", "_poll"):
        assert callable(getattr(m, fn)), fn
    assert m.DEFAULT_UPLOAD_PURPOSE == "t2a_async_input"
    # 已废弃：单批打包/逐语言批量合成/旧解包/旧 ID 正则
    for gone in ("_build_zip", "_chunks", "_synthesize_zip_batch",
                 "_extract_mp3s_from_tar", "ID_TAIL"):
        assert not hasattr(m, gone), gone
    assert m.CODE_TAIL.pattern == r"(d\d{4}Q\d+)"
    assert m.DIR_CODE.pattern == r"d(\d{4})"


def test_synthesize_batch_signature_has_on_entry():
    """统一入口签名：synthesize_batch(entries, cfg, on_event, cancel_token, on_entry=None)。"""
    sig = inspect.signature(MinimaxProvider.synthesize_batch)
    params = sig.parameters
    for name in ("entries", "config", "on_event", "cancel_token", "on_entry"):
        assert name in params, name
    assert params["on_entry"].default is None


def test_batch_primitives_signatures():
    """新原语签名：submit_batch(zip_entries, cfg) / query_task(task_id, cfg)。"""
    for fn, expected in (
        (minimax.submit_batch, ["zip_entries", "cfg"]),
        (minimax.query_task, ["task_id", "cfg"]),
        (minimax.extract_named_mp3s, ["blob"]),
        (minimax._create_batch_task, ["file_id", "cfg"]),
        (minimax._build_zip_named, ["pairs"]),
    ):
        assert list(inspect.signature(fn).parameters) == expected, fn


def test_num_handles_none_and_missing_values():
    """_num：键缺失或显式 None 回退默认值，但保留合法的 0。"""
    assert minimax._num({}, "k", 7) == 7
    assert minimax._num({"k": None}, "k", 7) == 7
    assert minimax._num({"k": 0}, "k", 7) == 0
    assert minimax._num({"k": 3}, "k", 7) == 3
