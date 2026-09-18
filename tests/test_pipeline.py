from __future__ import annotations

import io
import os
import zipfile

import pytest

from t2s_tool import tasks as tasks_mod
from t2s_tool.errors import CancelledError, ConfigError, ParseError
from t2s_tool.models import CancelToken, JobReport, StageEvent
from t2s_tool.pipeline import run_job

from tests.conftest import FakeResponse

HEADERS = ("语音ID", "中文", "英文")
ROWS = [
    ("Q001", "一", "one"),
    ("Q002", "二", "two"),
    ("Q003", None, "three"),  # 中文列为空文本
]


def _collect(events):
    def on_event(ev):
        assert isinstance(ev, StageEvent)
        events.append(ev)

    return on_event


def _run(tmp_path, make_xlsx, xlsx=None, **kw):
    """跑一次 run_job；xlsx=None 时用默认 2 语言 3 行样本。返回 (report, events, out)。"""
    xlsx = xlsx or make_xlsx(HEADERS, ROWS)
    out = kw.pop("output_dir", str(tmp_path / "语音包"))
    events = kw.pop("events", [])
    return run_job(input_path=xlsx, output_dir=out,
                   on_event=_collect(events), **kw), events, out


def test_end_to_end_fake_provider(tmp_path, make_xlsx):
    report, events, out = _run(tmp_path, make_xlsx, provider_name="fake")
    assert isinstance(report, JobReport)
    assert len(report.language_reports) == 2

    zh, en = report.language_reports
    assert zh.language == "中文"
    assert zh.success_ids == ["Q001", "Q002"]
    assert zh.missing_ids == ["Q003"]  # 空文本条目缺失
    assert any("为空文本" in w for w in report.parse_warnings)

    assert en.success_ids == ["Q001", "Q002", "Q003"]
    assert en.missing_ids == []

    # 输出目录：2 语言目录 + 2 tar
    assert os.path.isdir(os.path.join(out, "中文"))
    assert os.path.isdir(os.path.join(out, "英文"))
    assert os.path.isfile(os.path.join(out, "中文.tar"))
    assert os.path.isfile(os.path.join(out, "英文.tar"))
    with open(os.path.join(out, "中文", "Q001.mp3"), "rb") as f:
        assert f.read() == b"FAKE-MP3:Q001"

    stages = [e.stage for e in events]
    assert "parse" in stages and "package" in stages and "done" in stages
    assert stages[-1] == "done"


def test_languages_filter(tmp_path, make_xlsx):
    report, _, _ = _run(tmp_path, make_xlsx, provider_name="fake",
                        languages=["英文"])
    assert len(report.language_reports) == 1
    rep = report.language_reports[0]
    assert rep.language == "英文"
    assert rep.success_ids == ["Q001", "Q002", "Q003"]


def test_unknown_language_raises(tmp_path, make_xlsx):
    with pytest.raises(ParseError):
        _run(tmp_path, make_xlsx, provider_name="fake", languages=["德语"])


def test_unknown_provider_raises(tmp_path, make_xlsx):
    with pytest.raises(ConfigError):
        _run(tmp_path, make_xlsx, provider_name="no-such-vendor")


def test_pre_cancelled_token_raises_and_no_output(tmp_path, make_xlsx):
    token = CancelToken()
    token.cancel()
    with pytest.raises(CancelledError):
        _run(tmp_path, make_xlsx, provider_name="fake", cancel_token=token)
    out = str(tmp_path / "语音包")
    if os.path.isdir(out):
        leftovers = []
        for root, dirs, files in os.walk(out):
            leftovers.extend(files)
        assert leftovers == []


def test_parser_name_explicit_equals_auto(tmp_path, make_xlsx):
    auto_report, _, _ = _run(tmp_path, make_xlsx, provider_name="fake",
                             output_dir=str(tmp_path / "out_auto"))
    fixed_report, _, _ = _run(tmp_path, make_xlsx, provider_name="fake",
                              parser_name="xlsx_9lang",
                              output_dir=str(tmp_path / "out_fixed"))

    def shape(rep):
        return [(r.language, r.success_ids, r.missing_ids, r.unknown_ids)
                for r in rep.language_reports]

    assert shape(auto_report) == shape(fixed_report)


def test_output_dir_created_when_missing(tmp_path, make_xlsx):
    deep = str(tmp_path / "a" / "b" / "新输出")
    _run(tmp_path, make_xlsx, provider_name="fake", output_dir=deep)
    assert os.path.isdir(deep)
    assert os.path.isfile(os.path.join(deep, "中文.tar"))


# ============================================================ batch 单任务编排
# tts_mode="batch" + minimax：全部选中语言合并 1 个 zip、1 个 API 任务；
# submit_only=True 入任务中心后立即返回，False 则阻塞到下载落盘。

BATCH_HEADERS = ("语音ID", "中文", "英文")
BATCH_ROWS = [
    ("Q001", "一", "one"),
    ("Q002", "二", "two"),
    ("Q003", None, None),      # 两语言均为空文本 → 不入 zip，计入缺失
]


def _batch_cfg(mini_cfg, **over):
    """run_job 的顶层 config：minimax 块切到 batch 模式（可再覆盖其他键）。"""
    block = dict(mini_cfg)
    block["tts_mode"] = "batch"
    block.update(over)
    return {"provider": "minimax", "providers": {"minimax": block}}


def _register_submit(fake, task_id=456, upload_file_id=123, usage=42):
    fake.add_post("files/upload", {"file": {"file_id": upload_file_id},
                                   "base_resp": {"status_code": 0}})
    fake.add_post("/v1/t2a_async_v2", {"task_id": task_id,
                                       "usage_characters": usage,
                                       "base_resp": {"status_code": 0}})


def _zip_texts(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return {n: zf.read(n).decode("utf-8") for n in zf.namelist()}


def test_batch_submit_only_stores_single_task(tmp_path, make_xlsx, mock_http,
                                              mini_cfg):
    fake = mock_http()
    _register_submit(fake)
    xlsx = make_xlsx(BATCH_HEADERS, BATCH_ROWS, name="b1.xlsx")
    out = str(tmp_path / "语音包")
    store = tasks_mod.TaskStore(str(tmp_path / "tasks.json"))
    events = []

    report, _, _ = _run(tmp_path, make_xlsx, xlsx=xlsx, config=_batch_cfg(mini_cfg),
                        output_dir=out, events=events, submit_only=True,
                        task_store=store)

    # 只提交，不轮询/不下载：upload → create 两个请求
    assert [(m, u) for m, u, _ in fake.calls] == [
        ("POST", "https://api.test/v1/files/upload"),
        ("POST", "https://api.test/v1/t2a_async_v2"),
    ]
    assert not [c for c in fake.calls if c[0] == "GET"]

    # 任务中心：跨语言合并为恰好 1 条记录
    records = store.list()
    assert len(records) == 1
    rec = records[0]
    assert rec.task_id == "456"
    assert rec.provider == "minimax"
    assert rec.languages == ["中文", "英文"]
    assert rec.entry_map == {"d0001": "中文", "d0002": "英文"}
    assert rec.entry_count == 4 and len(rec.entries) == 4       # 空文本不入快照
    assert all(e["text"].strip() for e in rec.entries)
    assert rec.status == tasks_mod.PROCESSING
    assert rec.output_dir == out
    assert rec.upload_file_id == 123 and rec.usage_characters == 42
    assert rec.attempts == 1 and rec.result_file_id is None

    assert report.submitted_tasks == ["456"]
    assert [r.language for r in report.language_reports] == ["中文", "英文"]
    for rep in report.language_reports:
        assert rep.success_ids == []
        assert rep.missing_ids == ["Q003"]                      # 空文本即缺失
        assert any("任务已提交 task_id=456" in w for w in rep.warnings)
        assert rep.output_dir is None and rep.tar_path is None
    assert [e.stage for e in events] == [
        "parse", "zip", "create", "package", "package", "done"]
    assert "单任务" in [e for e in events if e.stage == "zip"][0].message
    # submit_only 不创建任何语言子目录 / 语音包
    assert os.path.isdir(out)
    assert os.listdir(out) == []


def test_batch_submit_only_without_task_store_raises(tmp_path, make_xlsx, mock_http,
                                                     mini_cfg):
    fake = mock_http()
    _register_submit(fake)

    # 注：现实现是「先提交、后校验 task_store」，抛错时任务已在服务端创建（见汇报）
    with pytest.raises(ConfigError) as ei:
        _run(tmp_path, make_xlsx, xlsx=make_xlsx(BATCH_HEADERS, BATCH_ROWS,
                                                 name="b1b.xlsx"),
             config=_batch_cfg(mini_cfg), output_dir=str(tmp_path / "out"),
             submit_only=True)

    assert "task_store" in str(ei.value)
    assert len(fake.calls) == 2


def test_batch_blocking_path_writes_all_languages(tmp_path, make_xlsx, mock_http,
                                                  mini_cfg, make_tar):
    fake = mock_http()
    _register_submit(fake)
    fake.add_get("t2a_async_query_v2", [
        {"status": "Processing", "base_resp": {"status_code": 0}},
        {"status": "Success", "file_id": 901, "base_resp": {"status_code": 0}},
    ])
    fake.add_get("retrieve_content", FakeResponse(content=make_tar({
        "d0001Q001": b"zh-1", "d0001Q002": b"zh-2",
        "d0002Q001": b"en-1", "d0002Q002": b"en-2"})))
    xlsx = make_xlsx(BATCH_HEADERS, BATCH_ROWS, name="b2.xlsx")
    out = str(tmp_path / "语音包")
    events = []

    report, _, _ = _run(tmp_path, make_xlsx, xlsx=xlsx, config=_batch_cfg(mini_cfg),
                        output_dir=out, events=events)

    assert report.submitted_tasks == []
    assert [(m, u) for m, u, _ in fake.calls] == [
        ("POST", "https://api.test/v1/files/upload"),
        ("POST", "https://api.test/v1/t2a_async_v2"),
        ("GET", "https://api.test/v1/query/t2a_async_query_v2?task_id=456"),
        ("GET", "https://api.test/v1/query/t2a_async_query_v2?task_id=456"),
        ("GET", "https://api.test/v1/files/retrieve_content?file_id=901"),
    ]

    def read_bytes(*parts):
        with open(os.path.join(out, *parts), "rb") as f:
            return f.read()

    assert read_bytes("中文", "Q001.mp3") == b"zh-1"
    assert read_bytes("中文", "Q002.mp3") == b"zh-2"
    assert read_bytes("英文", "Q001.mp3") == b"en-1"
    assert read_bytes("英文", "Q002.mp3") == b"en-2"
    assert set(os.listdir(out)) == {"中文", "英文", "中文.tar", "英文.tar"}
    assert not os.path.exists(os.path.join(out, "中文", "Q003.mp3"))

    zh, en = report.language_reports
    assert (zh.success_ids, zh.missing_ids) == (["Q001", "Q002"], ["Q003"])
    assert (en.success_ids, en.missing_ids) == (["Q001", "Q002"], ["Q003"])
    assert zh.tar_path == os.path.join(out, "中文.tar")

    stages = [e.stage for e in events]
    assert {"zip", "create", "poll", "download", "package", "done"} <= set(stages)
    # 已知缺口：submit_batch 不上报事件，zip 与 create 之间没有 upload 进度（见汇报）
    assert "upload" not in stages
    assert stages[0] == "parse" and stages[-1] == "done"
    assert [e.message for e in events if e.stage == "package"] == [
        "中文: 成功 2，缺失 1，缺失ID: Q003",
        "英文: 成功 2，缺失 1，缺失ID: Q003",
    ]


def test_batch_zip_entry_naming_and_language_codes(tmp_path, make_xlsx,
                                                   mock_http_upload, mini_cfg):
    fake = mock_http_upload()
    _register_submit(fake)
    xlsx = make_xlsx(BATCH_HEADERS, BATCH_ROWS, name="b3.xlsx")
    store = tasks_mod.TaskStore(str(tmp_path / "tasks.json"))

    _run(tmp_path, make_xlsx, xlsx=xlsx, config=_batch_cfg(mini_cfg),
         output_dir=str(tmp_path / "out"), submit_only=True, task_store=store)

    assert len(fake.uploads) == 1
    texts = _zip_texts(fake.uploads[0][2])
    assert sorted(texts) == ["d0001Q001.txt", "d0001Q002.txt",
                             "d0002Q001.txt", "d0002Q002.txt"]
    # 目录码 -> 语言列 的映射与 entry_map 一致，文本原样入 zip
    assert texts == {"d0001Q001.txt": "一", "d0001Q002.txt": "二",
                     "d0002Q001.txt": "one", "d0002Q002.txt": "two"}
    rec = store.list()[0]
    assert rec.entry_map == {"d0001": "中文", "d0002": "英文"}
    assert [(e["dir"], e["voice_id"]) for e in rec.entries] == [
        ("d0001", "Q001"), ("d0001", "Q002"),
        ("d0002", "Q001"), ("d0002", "Q002")]


def test_batch_all_empty_text_submits_nothing(tmp_path, make_xlsx, mock_http,
                                              mini_cfg):
    fake = mock_http()
    fake.add_post("files/upload", {"file": {"file_id": 1},
                                   "base_resp": {"status_code": 0}})
    rows = [("Q001", None, "  "), ("Q002", "", None)]
    xlsx = make_xlsx(BATCH_HEADERS, rows, name="b4.xlsx")
    out = str(tmp_path / "语音包")
    store = tasks_mod.TaskStore(str(tmp_path / "tasks.json"))
    events = []

    report, _, _ = _run(tmp_path, make_xlsx, xlsx=xlsx, config=_batch_cfg(mini_cfg),
                        output_dir=out, events=events, submit_only=True,
                        task_store=store)

    assert fake.calls == []                                     # 完全不提交
    assert store.list() == []
    assert report.submitted_tasks == []
    assert [e.stage for e in events] == ["parse", "done"]
    assert "未提交任务" in events[-1].message
    for rep in report.language_reports:
        assert rep.success_ids == []
        assert rep.missing_ids == ["Q001", "Q002"]
        assert rep.warnings == ["全部条目为空文本"]
        assert rep.tar_path is None
    assert os.listdir(out) == []


def test_batch_submit_only_respects_language_filter(tmp_path, make_xlsx, mock_http,
                                                    mini_cfg):
    fake = mock_http()
    _register_submit(fake)
    xlsx = make_xlsx(BATCH_HEADERS, BATCH_ROWS, name="b5.xlsx")
    store = tasks_mod.TaskStore(str(tmp_path / "tasks.json"))

    report, _, _ = _run(tmp_path, make_xlsx, xlsx=xlsx, config=_batch_cfg(mini_cfg),
                        output_dir=str(tmp_path / "out"), languages=["英文"],
                        submit_only=True, task_store=store)

    rec = store.list()[0]
    assert rec.languages == ["英文"] and rec.entry_map == {"d0001": "英文"}
    assert [e["dir"] for e in rec.entries] == ["d0001", "d0001"]
    assert [e["voice_id"] for e in rec.entries] == ["Q001", "Q002"]
    assert report.submitted_tasks == ["456"]
    assert len(report.language_reports) == 1
    assert report.language_reports[0].missing_ids == ["Q003"]


def test_batch_missing_api_key_raises_before_submit(tmp_path, make_xlsx, mock_http,
                                                    mini_cfg):
    fake = mock_http()
    cfg = _batch_cfg(mini_cfg)
    cfg["providers"]["minimax"].pop("api_key")

    with pytest.raises(ConfigError):
        _run(tmp_path, make_xlsx, xlsx=make_xlsx(BATCH_HEADERS, BATCH_ROWS,
                                                 name="b6.xlsx"),
             config=cfg, output_dir=str(tmp_path / "out"), submit_only=True)

    assert fake.calls == []


# ---------------------------------------------------------- 逐条模式回归护栏

def test_sync_mode_still_loops_per_language_without_zip(tmp_path, make_xlsx,
                                                        mock_http, mini_cfg):
    """tts_mode=sync：仍走 provider 逐条链路（无 zip 上传），产物结构不变。"""
    fake = mock_http()
    mp3 = b"\xff\xfb\x90\x00sync"
    fake.add_post("/v1/t2a_v2",
                  {"data": {"audio": mp3.hex()}, "base_resp": {"status_code": 0}})
    xlsx = make_xlsx(BATCH_HEADERS, BATCH_ROWS, name="sync.xlsx")
    out = str(tmp_path / "语音包")
    events = []
    cfg = {"provider": "minimax",
           "providers": {"minimax": dict(mini_cfg, tts_mode="sync")}}

    report, _, _ = _run(tmp_path, make_xlsx, xlsx=xlsx, config=cfg, output_dir=out,
                        events=events)

    assert not any("files/upload" in u for _, u, _ in fake.calls)
    assert sum(1 for _, u, _ in fake.calls if u.endswith("/v1/t2a_v2")) == 4
    zh, en = report.language_reports
    assert (zh.success_ids, zh.missing_ids) == (["Q001", "Q002"], ["Q003"])
    assert (en.success_ids, en.missing_ids) == (["Q001", "Q002"], ["Q003"])
    assert set(os.listdir(out)) == {"中文", "英文", "中文.tar", "英文.tar"}
    with open(os.path.join(out, "中文", "Q001.mp3"), "rb") as f:
        assert f.read() == mp3
    stages = [e.stage for e in events]
    assert "create" in stages and "download" in stages
    assert "upload" not in stages and "zip" not in stages
