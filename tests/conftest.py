from __future__ import annotations

import io
import os
import sys
import tarfile

import openpyxl
import pytest
import requests

# 保证项目根在 sys.path 上（离线，不触网）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

TAR_PREFIX = "2038465880412660024_202604152300_387938953298334"


class FakeResponse:
    """requests.Response 的最小替身。json_data 为 Exception 实例时 .json() 抛出它。"""

    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self.json_data = json_data
        self.content = content

    def json(self):
        if isinstance(self.json_data, Exception):
            raise self.json_data
        return self.json_data


class FakeRequests:
    """按 URL 子串匹配的假 requests 模块（仅 post/get + exceptions）。

    - add_post/add_get(url_part, response_or_list)：list 表示按调用顺序依次弹出，
      耗尽后再调用抛 AssertionError；单个值则每次调用重复返回。
    - dict / 其他非 FakeResponse 值会自动包成 FakeResponse(json_data=值)。
    - 同一次调用优先匹配最长（最具体）的 url_part。

    线程安全说明：calls.append 与 list.pop(0) 各自受 GIL 保护不会崩，但
    "响应序列按调用顺序弹出" 在多线程下不保证与提交顺序一致。并发逐条链路
    （synthesize_batch）的用例统一使用 concurrency=1，保证请求序列可预测断言。
    """

    def __init__(self):
        self.calls = []  # [(method, url, kwargs), ...]
        self._table = {"POST": [], "GET": []}
        self.exceptions = requests.exceptions  # 供 minimax 的 except 分支取属性

    def add_post(self, url_part, response):
        self._table["POST"].append((url_part, response))

    def add_get(self, url_part, response_or_list):
        self._table["GET"].append((url_part, response_or_list))

    def _resolve(self, method, url):
        entries = [(p, r) for p, r in self._table[method] if p in url]
        if not entries:
            raise AssertionError("未注册的 %s URL: %s" % (method, url))
        part, resp = max(entries, key=lambda pr: len(pr[0]))
        if isinstance(resp, list):
            if not resp:
                raise AssertionError(
                    "%s [%s] 的响应序列已耗尽（url=%s）" % (method, part, url))
            item = resp.pop(0)
        else:
            item = resp
        if isinstance(item, FakeResponse):
            return item
        return FakeResponse(json_data=item)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self._resolve("POST", url)

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._resolve("GET", url)


class CapturingRequests(FakeRequests):
    """FakeRequests + multipart 上传字节捕获（校验 zip 成员用）。

    minimax._upload 传入的 files 字段是 (文件名, 打开的文件对象, content_type)，
    该文件对象在 _upload 返回后立即被 with 关闭，所以必须在 post 调用当下把字节
    读出来存进 self.uploads，事后无法再取。
    """

    def __init__(self):
        FakeRequests.__init__(self)
        self.uploads = []  # [(url, 成员文件名, 字节, content_type), ...]

    def post(self, url, **kw):
        field = (kw.get("files") or {}).get("file")
        if field is not None:
            name = field[0]
            handle = field[1]
            ctype = field[2] if len(field) > 2 else None
            data = handle.read() if hasattr(handle, "read") else handle
            self.uploads.append((url, name, data, ctype))
        return FakeRequests.post(self, url, **kw)


@pytest.fixture
def make_xlsx(tmp_path):
    """工厂：f(headers, rows, name="list.xlsx") -> str，用 openpyxl 写入并返回路径。"""

    def f(headers, rows, name="list.xlsx"):
        path = tmp_path / name
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(list(headers))
        for r in rows:
            ws.append(list(r))
        wb.save(str(path))
        wb.close()
        return str(path)

    return f


@pytest.fixture
def make_tar():
    """工厂：f(items={vid: mp3bytes}, merged=False) -> tar 字节。

    成员名形态（两种形态共用，与 MiniMax 返回一致）：
    <TAR_PREFIX>_<vid>/content-<TAR_PREFIX>_<vid>.mp3
    merged=False 语义上对应"单语音 tar"（items 通常只有 1 个 vid）；
    merged=True  对应"同一 tar 内多个嵌套成员"。本实现同一布局均可覆盖两种场景。
    """

    def f(items, merged=False):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for vid, data in items.items():
                dirname = "%s_%s" % (TAR_PREFIX, vid)
                dinfo = tarfile.TarInfo(dirname)
                dinfo.type = tarfile.DIRTYPE
                dinfo.mode = 0o755
                tf.addfile(dinfo)
                fname = "%s/content-%s_%s.mp3" % (dirname, TAR_PREFIX, vid)
                finfo = tarfile.TarInfo(fname)
                finfo.size = len(data)
                tf.addfile(finfo, io.BytesIO(data))
        return buf.getvalue()

    return f


@pytest.fixture
def mock_http(monkeypatch):
    """工厂：返回 FakeRequests 并替换 t2s_tool.providers.minimax.requests。"""

    def f():
        fake = FakeRequests()
        monkeypatch.setattr("t2s_tool.providers.minimax.requests", fake)
        return fake

    return f


@pytest.fixture
def mini_cfg():
    """测试用 minimax 配置：poll_interval=0，超时收紧，避免任何真实等待。

    tts_mode="async"：既有逐条测试继续命中「创建任务 → 轮询 → 下载」异步链路
    （被测代码的默认模式是 batch，批量用例在派生 cfg 上显式覆盖 tts_mode）。
    concurrency=1：逐条串行执行，保证 fake.calls 与事件序列可预测断言。
    """
    return {
        "api_key": "test-key",
        "base_url": "https://api.test",
        "model": "speech-2.8-hd",
        "voice_setting": {"voice_id": "v1"},
        "audio_setting": {"format": "mp3"},
        "poll_interval": 0,
        "poll_timeout": 180,
        "request_timeout": 5,
        "upload_timeout": 5,
        "download_timeout": 5,
        "concurrency": 1,
        "tts_mode": "async",
    }


@pytest.fixture
def mock_http_upload(monkeypatch):
    """工厂：返回 CapturingRequests 并替换 t2s_tool.providers.minimax.requests。

    用法与 mock_http 一致，额外提供 fake.uploads（zip 上传的原始字节）供成员级断言。
    """

    def f():
        fake = CapturingRequests()
        monkeypatch.setattr("t2s_tool.providers.minimax.requests", fake)
        return fake

    return f


@pytest.fixture
def register_happy_path():
    """为 fake 注册「创建任务 → 轮询查询 → 下载结果」的标准成功链路。

    task_states 为查询状态序列（list，按调用顺序弹出）；默认一次即 Success。
    """

    def f(fake, tar_bytes, task_states=None):
        fake.add_post("/v1/t2a_async_v2",
                      {"task_id": 456, "task_token": "tok", "usage_characters": 10,
                       "base_resp": {"status_code": 0}})
        if task_states is None:
            task_states = [{"status": "Success", "file_id": 901,
                            "base_resp": {"status_code": 0}}]
        fake.add_get("t2a_async_query_v2", list(task_states))
        fake.add_get("retrieve_content", FakeResponse(content=tar_bytes))

    return f


@pytest.fixture
def register_batch_happy_path():
    """为 fake 注册 zip 批量模式（tts_mode="batch"）的标准成功链路：
    上传 zip → 创建批量任务（text_file_id 整型）→ 轮询 → 下载结果 tar。

    task_states 为查询状态序列（list，按调用顺序弹出）；默认一次即 Success。
    单批场景直接用本 fixture；拆批场景在用例内自行注册逐请求弹出的 list 响应。
    """

    def f(fake, tar_bytes, task_states=None):
        fake.add_post("files/upload",
                      {"file": {"file_id": 123}, "base_resp": {"status_code": 0}})
        fake.add_post("/v1/t2a_async_v2",
                      {"task_id": 456, "task_token": "tok", "usage_characters": 10,
                       "base_resp": {"status_code": 0}})
        if task_states is None:
            task_states = [{"status": "Success", "file_id": 901,
                            "base_resp": {"status_code": 0}}]
        fake.add_get("t2a_async_query_v2", list(task_states))
        fake.add_get("retrieve_content", FakeResponse(content=tar_bytes))

    return f
