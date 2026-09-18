from __future__ import annotations

# 任务中心（t2s_tool.tasks）测试：TaskRecord 快照 / TaskStore 持久化 /
# 生命周期操作（refresh / finalize / retry）/ TaskController（后台线程串行 + post 回调）。
#
# 全部离线：requests 由 conftest.mock_http / mock_http_upload 替换（FakeRequests 系），
# 不触网；落盘只写 tmp_path；系统临时目录不参与（除 provider 的 zip，由 provider 自清理）。

import glob
import io
import json
import os
import tarfile
import threading
import time
import zipfile

import pytest
import requests

from t2s_tool import tasks
from t2s_tool.errors import APIError, ConfigError
from t2s_tool.gui.task_controller import TaskController

from tests.conftest import FakeRequests, FakeResponse

TAR_CODES = "2038465880412660024_202604152300_387938953298334"


# ------------------------------------------------------------------ 小工具

def _entries():
    """词条快照：中文 2 条 + 英文 3 条（Q003 只有英文有文本）。"""
    return [
        {"dir": "d0001", "voice_id": "Q001", "text": "一"},
        {"dir": "d0001", "voice_id": "Q002", "text": "二"},
        {"dir": "d0002", "voice_id": "Q001", "text": "one"},
        {"dir": "d0002", "voice_id": "Q002", "text": "two"},
        {"dir": "d0002", "voice_id": "Q003", "text": "three"},
    ]


def _rec(**overrides):
    """构造 TaskRecord：默认一条 5 词条 / 2 语言的 processing 记录。"""
    data = dict(
        task_id="456",
        provider="minimax",
        languages=["中文", "英文"],
        entry_map={"d0001": "中文", "d0002": "英文"},
        entries=_entries(),
        entry_count=5,
        created_at=1000.0,
        updated_at=1000.0,
        output_dir="out",
        status=tasks.PROCESSING,
    )
    data.update(overrides)
    return tasks.TaskRecord(**data)


def _ok_query(file_id=901, status="Success"):
    return {"status": status, "file_id": file_id, "base_resp": {"status_code": 0}}


def _not_found_body(msg="task not found"):
    return {"base_resp": {"status_code": 2013, "status_msg": msg}}


def _zip_names(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return sorted(zf.namelist())


def _named_tar_with_junk(pairs, junk_name=b"junk"):
    """在标准结果包基础上追加一个无编码的 mp3 成员（触发「未知成员」warning）。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for code, data in pairs:
            name = "%s_%s/content-%s_%s.mp3" % (TAR_CODES, code, TAR_CODES, code)
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        junk = tarfile.TarInfo("extra/readme-plain.mp3")
        junk.size = len(junk_name)
        tf.addfile(junk, io.BytesIO(junk_name))
    return buf.getvalue()


def _store(tmp_path, name="tasks.json"):
    return tasks.TaskStore(str(tmp_path / name))


def _read_json(tmp_path, name="tasks.json"):
    with open(str(tmp_path / name), "r", encoding="utf-8") as f:
        return json.load(f)


class _RaisingRequests(FakeRequests):
    """GET 直接抛异常，模拟网络层错误（超时/断连）。"""

    def __init__(self, exc):
        FakeRequests.__init__(self)
        self._exc = exc

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        raise self._exc


def _wait_for(posted, kind, count=1, timeout=5.0):
    """轮询收集器直到出现 count 个指定 kind 的回调，返回这些 payload。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = [p for k, p in list(posted) if k == kind]
        if len(got) >= count:
            return got
        time.sleep(0.02)
    raise AssertionError(
        "等待第 %d 个 %r 超时（%.1fs），已收到: %r" % (count, kind, timeout, list(posted)))


def _count_of(posted, kind):
    return sum(1 for k, _ in list(posted) if k == kind)


# ------------------------------------------------------------- TaskRecord 快照

def test_record_json_roundtrip_preserves_all_fields():
    rec = _rec(status=tasks.SUCCESS, result_file_id="901", upload_file_id=123,
               usage_characters=42, attempts=2, last_error="x",
               language_results={"中文": {"success": ["Q001"], "missing": [],
                                          "tar_path": "out/中文.tar"}})
    payload = json.loads(json.dumps(rec.to_dict(), ensure_ascii=False))
    assert payload["task_id"] == "456"
    assert payload["entry_map"] == {"d0001": "中文", "d0002": "英文"}

    restored = tasks.TaskRecord.from_dict(payload)
    assert restored == rec
    assert restored.language_results["中文"]["tar_path"] == "out/中文.tar"


def test_record_from_dict_ignores_unknown_fields():
    """前向兼容：新版本写入的未知字段被忽略，不影响加载。"""
    data = _rec().to_dict()
    data["future_field"] = "whatever"
    data["another_new_one"] = {"nested": True}

    rec = tasks.TaskRecord.from_dict(data)

    assert rec.task_id == "456"
    assert not hasattr(rec, "future_field")
    assert rec.attempts == 1


def test_record_defaults():
    rec = _rec()
    assert rec.status == tasks.PROCESSING
    assert rec.result_file_id is None and rec.upload_file_id is None
    assert rec.usage_characters == 0 and rec.attempts == 1
    assert rec.last_error is None
    assert rec.language_results == {}
    # 可变默认值走 field(default_factory)，实例间不共享
    assert _rec().language_results is not _rec().language_results


def test_status_constants_and_labels():
    assert tasks.TERMINAL_STATUSES == (tasks.PACKAGED, tasks.FAILED, tasks.EXPIRED)
    assert set(tasks.ALL_STATUSES) == {
        tasks.PROCESSING, tasks.SUCCESS, tasks.DOWNLOADING, tasks.PACKAGED,
        tasks.FAILED, tasks.EXPIRED, tasks.LOST}
    assert set(tasks.STATUS_LABELS) == set(tasks.ALL_STATUSES)
    assert tasks.STATUS_LABELS[tasks.PACKAGED] == "已完成"
    assert tasks.STATUS_LABELS[tasks.SUCCESS] == "待下载"
    assert tasks.STATUS_LABELS[tasks.LOST] == "记录丢失"
    for value in tasks.STATUS_LABELS.values():
        assert value and isinstance(value, str)


# ------------------------------------------------------------------- TaskStore

def test_store_upsert_get_and_bumps_updated_at(tmp_path):
    store = _store(tmp_path)
    rec = _rec(updated_at=0.0)
    before = time.time()

    store.upsert(rec)

    assert store.get("456") is rec
    assert rec.updated_at >= before               # upsert 会刷新 updated_at
    assert store.get("not-exist") is None


def test_store_get_accepts_non_string_task_id(tmp_path):
    store = _store(tmp_path)
    store.upsert(_rec(task_id="456"))
    assert store.get(456) is not None             # 内部统一 str 化


def test_store_list_newest_first(tmp_path):
    store = _store(tmp_path)
    store.upsert(_rec(task_id="old", created_at=1.0))
    store.upsert(_rec(task_id="mid", created_at=2.0))
    store.upsert(_rec(task_id="new", created_at=3.0))

    assert [r.task_id for r in store.list()] == ["new", "mid", "old"]


def test_store_upsert_replaces_same_task_id(tmp_path):
    store = _store(tmp_path)
    store.upsert(_rec(status=tasks.PROCESSING))
    store.upsert(_rec(status=tasks.SUCCESS, result_file_id="901"))

    assert len(store.list()) == 1
    assert store.get("456").status == tasks.SUCCESS
    assert store.get("456").result_file_id == "901"


def test_store_remove(tmp_path):
    store = _store(tmp_path)
    store.upsert(_rec())
    assert store.remove("456") is True
    assert store.list() == []
    assert store.remove("456") is False           # 二次删除返回 False
    assert _read_json(tmp_path)["tasks"] == []    # 删除已落盘


def test_store_persist_reload_roundtrip(tmp_path):
    store = _store(tmp_path)
    store.upsert(_rec(created_at=1.0, status=tasks.PACKAGED,
                      language_results={"中文": {"missing": []}}))
    store.upsert(_rec(task_id="777", status=tasks.FAILED, created_at=2.0))

    reloaded = _store(tmp_path)
    assert [r.task_id for r in reloaded.list()] == ["777", "456"]   # 新的在前
    assert reloaded.get("456").status == tasks.PACKAGED
    assert reloaded.get("456").language_results == {"中文": {"missing": []}}
    assert reloaded.get("456").entries == _entries()
    assert reloaded.get("777").attempts == 1


def test_store_save_is_atomic_and_leaves_no_tmp(tmp_path):
    store = _store(tmp_path)
    store.upsert(_rec())
    store.upsert(_rec(task_id="777"))

    assert os.path.exists(store.path)
    assert glob.glob(store.path + ".tmp") == []   # 临时文件已 os.replace 掉


def test_store_written_json_shape(tmp_path):
    store = _store(tmp_path)
    store.upsert(_rec(entry_count=5))
    payload = _read_json(tmp_path)
    assert payload["version"] == 1
    assert payload["tasks"][0]["entry_count"] == 5
    assert "一" in json.dumps(payload, ensure_ascii=False)   # 中文不转义


def test_store_corrupt_json_is_backed_up_and_rebuilt_empty(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text("{ this is not json !!", encoding="utf-8")

    store = tasks.TaskStore(str(path))

    assert store.list() == []
    assert not path.exists()                      # 损坏文件被移走
    backups = glob.glob(str(tmp_path / "tasks.json.corrupt-*.bak"))
    assert len(backups) == 1
    with open(backups[0], "r", encoding="utf-8") as f:
        assert "not json" in f.read()


def test_store_concurrent_upsert_keeps_all_records(tmp_path):
    store = _store(tmp_path)
    errors = []

    def worker(idx):
        try:
            for n in range(5):
                store.upsert(_rec(task_id="t%d-%d" % (idx, n), created_at=float(idx)))
        except Exception as e:                    # pragma: no cover - 断言用
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    ids = {r.task_id for r in store.list()}
    assert len(ids) == 10
    assert {i for i in ids if i.startswith("t1-")} and \
        {i for i in ids if i.startswith("t2-")}
    # 落盘内容一致：新文件可被重新加载
    assert {r.task_id for r in tasks.TaskStore(store.path).list()} == ids


# ----------------------------------------------------- build_zip_entries / 判定

def test_build_zip_entries_uses_dir_code_and_voice_id():
    pairs = tasks.build_zip_entries(_entries())
    assert pairs == [
        ("d0001Q001.txt", "一"),
        ("d0001Q002.txt", "二"),
        ("d0002Q001.txt", "one"),
        ("d0002Q002.txt", "two"),
        ("d0002Q003.txt", "three"),
    ]


def test_build_zip_entries_empty_snapshot():
    assert tasks.build_zip_entries([]) == []


@pytest.mark.parametrize("status,expected", [
    (tasks.PROCESSING, True),
    (tasks.SUCCESS, True),
    (tasks.LOST, True),
    (tasks.DOWNLOADING, False),
    (tasks.PACKAGED, False),
    (tasks.FAILED, False),
    (tasks.EXPIRED, False),
])
def test_needs_refresh_by_status(status, expected):
    assert tasks.needs_refresh(_rec(status=status)) is expected


def test_expiry_risk_uses_eight_hour_boundary():
    rec = _rec(status=tasks.SUCCESS, created_at=1000.0)
    exactly = 1000.0 + tasks.EXPIRY_WARN_SECONDS
    assert tasks.EXPIRY_WARN_SECONDS == 8 * 3600
    assert tasks.expiry_risk(rec, now=exactly - 1) is False
    assert tasks.expiry_risk(rec, now=exactly) is False        # 边界：不严格大于
    assert tasks.expiry_risk(rec, now=exactly + 1) is True


@pytest.mark.parametrize("status", [
    tasks.PROCESSING, tasks.DOWNLOADING, tasks.PACKAGED,
    tasks.FAILED, tasks.EXPIRED, tasks.LOST,
])
def test_expiry_risk_false_for_non_success(status):
    rec = _rec(status=status, created_at=0.0)
    assert tasks.expiry_risk(rec, now=time.time() + 10 ** 9) is False


# --------------------------------------------------------------- refresh_record

def test_refresh_processing_to_success_records_file_id(tmp_path, mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", _ok_query(file_id=901))
    store = _store(tmp_path)
    rec = _rec(status=tasks.PROCESSING, last_error="旧的错误")
    store.upsert(rec)

    changed = tasks.refresh_record(store, rec, mini_cfg)

    assert changed is True
    assert rec.status == tasks.SUCCESS
    assert rec.result_file_id == "901"
    assert rec.last_error is None
    assert store.get("456").status == tasks.SUCCESS            # 已持久化
    method, url, kw = fake.calls[0]
    assert method == "GET"
    assert url == "https://api.test/v1/query/t2a_async_query_v2?task_id=456"
    assert kw["headers"]["Authorization"] == "Bearer test-key"


def test_refresh_success_again_without_file_id_keeps_old_one(tmp_path, mock_http,
                                                             mini_cfg):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", {"status": "Success"})
    store = _store(tmp_path)
    rec = _rec(status=tasks.SUCCESS, result_file_id="901")

    changed = tasks.refresh_record(store, rec, mini_cfg)

    assert changed is False                                    # 状态未变
    assert rec.result_file_id == "901"                         # 保留既有 file_id


@pytest.mark.parametrize("api_status,expected,expected_error", [
    ("Failed", tasks.FAILED, "处理失败"),
    ("Expired", tasks.EXPIRED, "已过期"),
])
def test_refresh_maps_terminal_failures(tmp_path, mock_http, mini_cfg, api_status,
                                        expected, expected_error):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", {"status": api_status})
    store = _store(tmp_path)
    rec = _rec(status=tasks.PROCESSING)

    changed = tasks.refresh_record(store, rec, mini_cfg)

    assert changed is True
    assert rec.status == expected
    assert expected_error in rec.last_error
    assert store.get("456").status == expected


def test_refresh_still_processing_returns_false(tmp_path, mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", {"status": "Processing"})
    store = _store(tmp_path)
    rec = _rec(status=tasks.PROCESSING)

    assert tasks.refresh_record(store, rec, mini_cfg) is False
    assert rec.status == tasks.PROCESSING
    assert rec.result_file_id is None
    assert len(fake.calls) == 1                                # 单次查询，不自旋轮询


def test_refresh_task_not_found_marks_lost(tmp_path, mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", _not_found_body("task not found"))
    store = _store(tmp_path)
    rec = _rec(status=tasks.PROCESSING)

    changed = tasks.refresh_record(store, rec, mini_cfg)

    assert changed is True
    assert rec.status == tasks.LOST
    assert "task not found" in rec.last_error
    assert store.get("456").status == tasks.LOST


def test_refresh_chinese_not_found_marks_lost(tmp_path, mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", _not_found_body("任务不存在"))
    store = _store(tmp_path)
    rec = _rec(status=tasks.PROCESSING)

    assert tasks.refresh_record(store, rec, mini_cfg) is True
    assert rec.status == tasks.LOST
    assert "任务不存在" in rec.last_error


def test_refresh_timeout_keeps_status_and_records_error(tmp_path, mini_cfg,
                                                        monkeypatch):
    monkeypatch.setattr("t2s_tool.providers.minimax.requests",
                        _RaisingRequests(requests.exceptions.Timeout("t")))
    store = _store(tmp_path)
    rec = _rec(status=tasks.PROCESSING)

    changed = tasks.refresh_record(store, rec, mini_cfg)

    assert changed is False
    assert rec.status == tasks.PROCESSING                      # 状态不变
    assert "超时" in rec.last_error
    assert store.get("456").last_error == rec.last_error       # 仍落盘


def test_refresh_server_error_keeps_status(tmp_path, mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", FakeResponse(status_code=503))
    store = _store(tmp_path)
    rec = _rec(status=tasks.SUCCESS, result_file_id="901")

    assert tasks.refresh_record(store, rec, mini_cfg) is False
    assert rec.status == tasks.SUCCESS                         # 不被网络抖动改写
    assert "服务器错误" in rec.last_error
    assert rec.result_file_id == "901"


def test_refresh_rejects_unknown_provider(tmp_path, mini_cfg):
    store = _store(tmp_path)
    rec = _rec(provider="silk")
    with pytest.raises(ConfigError):
        tasks.refresh_record(store, rec, mini_cfg)


# ------------------------------------------------------------- refresh_records

def test_refresh_records_skips_explicit_terminal_records(tmp_path, mock_http, mini_cfg):
    fake = mock_http()                                          # 什么都不注册
    store = _store(tmp_path)
    packaged = _rec(task_id="p1", status=tasks.PACKAGED)
    failed = _rec(task_id="f1", status=tasks.FAILED)
    expired = _rec(task_id="e1", status=tasks.EXPIRED)

    changed = tasks.refresh_records(store, mini_cfg,
                                    records=[packaged, failed, expired])

    assert changed == 0
    assert fake.calls == []                                    # 一个查询都不发


def test_refresh_records_selects_non_terminal_by_default(tmp_path, mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", [
        _ok_query(), {"status": "Processing", "base_resp": {"status_code": 0}}])
    store = _store(tmp_path)
    store.upsert(_rec(task_id="done", status=tasks.PACKAGED, created_at=3.0))
    store.upsert(_rec(task_id="wait", status=tasks.PROCESSING, created_at=2.0))
    store.upsert(_rec(task_id="lost", status=tasks.LOST, created_at=1.0))
    events = []

    changed = tasks.refresh_records(store, mini_cfg, on_event=events.append)

    assert changed == 2
    queried = [u for _, u, _ in fake.calls]
    assert len(queried) == 2
    assert sorted(u.split("task_id=")[1] for u in queried) == ["lost", "wait"]
    assert store.get("done").status == tasks.PACKAGED          # 终态不被回退
    assert [e.stage for e in events] == ["refresh", "refresh"]
    assert "已完成" not in "".join(e.message for e in events)


def test_refresh_records_counts_only_changes(tmp_path, mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", {"status": "Processing",
                                        "base_resp": {"status_code": 0}})
    store = _store(tmp_path)
    store.upsert(_rec(task_id="a", status=tasks.PROCESSING, created_at=2.0))
    store.upsert(_rec(task_id="b", status=tasks.PROCESSING, created_at=1.0))

    assert tasks.refresh_records(store, mini_cfg) == 0         # 状态都没变


# ------------------------------------------------------------- finalize_record

def test_finalize_downloads_extracts_and_packs_per_language(tmp_path, mock_http,
                                                            mini_cfg, make_tar):
    fake = mock_http()
    blob = make_tar({"d0001Q001": b"zh-1", "d0001Q002": b"zh-2",
                     "d0002Q001": b"en-1", "d0002Q002": b"en-2"})   # 缺 d0002Q003
    fake.add_get("retrieve_content", FakeResponse(content=blob))
    store = _store(tmp_path)
    out = str(tmp_path / "语音包")
    rec = _rec(status=tasks.SUCCESS, result_file_id="901", output_dir=out)
    store.upsert(rec)
    events = []

    returned = tasks.finalize_record(store, rec, mini_cfg, on_event=events.append)

    assert returned is rec
    assert rec.status == tasks.PACKAGED
    assert store.get("456").status == tasks.PACKAGED           # 终态持久化
    assert fake.calls[0][1] == \
        "https://api.test/v1/files/retrieve_content?file_id=901"

    # 逐条落盘：语音包/<语言>/<语音ID>.mp3，字节与结果包一致
    def read_bytes(*parts):
        with open(os.path.join(out, *parts), "rb") as f:
            return f.read()

    assert read_bytes("中文", "Q001.mp3") == b"zh-1"
    assert read_bytes("中文", "Q002.mp3") == b"zh-2"
    assert read_bytes("英文", "Q001.mp3") == b"en-1"
    assert read_bytes("英文", "Q002.mp3") == b"en-2"
    assert not os.path.exists(os.path.join(out, "英文", "Q003.mp3"))

    # 每语言语音包 tar（成员扁平 <语音ID>.mp3）
    for lang in ("中文", "英文"):
        assert os.path.isfile(os.path.join(out, lang + ".tar"))
    with tarfile.open(os.path.join(out, "英文.tar")) as tf:
        assert sorted(m.name for m in tf.getmembers()) == ["Q001.mp3", "Q002.mp3"]

    # language_results 汇总
    assert set(rec.language_results) == {"中文", "英文"}
    zh = rec.language_results["中文"]
    assert zh["success"] == ["Q001", "Q002"] and zh["missing"] == []
    assert zh["tar_path"] == os.path.join(out, "中文.tar")
    en = rec.language_results["英文"]
    assert en["success"] == ["Q001", "Q002"]
    assert en["missing"] == ["Q003"]                           # 结果包缺第 5 条
    assert rec.last_error is None

    stages = [e.stage for e in events]
    assert stages.count("download") == 2
    assert stages.count("package") == 2
    pkg = [e for e in events if e.stage == "package"]
    assert "缺失ID: Q003" in pkg[1].message
    assert "英文" in pkg[1].message


@pytest.mark.parametrize("status", [
    tasks.PROCESSING, tasks.FAILED, tasks.EXPIRED, tasks.LOST,
])
def test_finalize_requires_success_status(tmp_path, mock_http, mini_cfg, status):
    fake = mock_http()
    store = _store(tmp_path)
    rec = _rec(status=status, result_file_id="901")

    with pytest.raises(APIError) as ei:
        tasks.finalize_record(store, rec, mini_cfg)

    assert "不可下载打包" in str(ei.value)
    assert rec.status == status                                # 状态不被改写
    assert fake.calls == []


def test_finalize_packaged_reentry_raises(tmp_path, mock_http, mini_cfg):
    fake = mock_http()
    store = _store(tmp_path)
    rec = _rec(status=tasks.PACKAGED, result_file_id="901")

    with pytest.raises(APIError) as ei:
        tasks.finalize_record(store, rec, mini_cfg)

    assert "重复" in str(ei.value)
    assert rec.status == tasks.PACKAGED
    assert fake.calls == []


def test_finalize_downloading_reentry_raises(tmp_path, mock_http, mini_cfg):
    fake = mock_http()
    store = _store(tmp_path)
    rec = _rec(status=tasks.DOWNLOADING, result_file_id="901")

    with pytest.raises(APIError) as ei:
        tasks.finalize_record(store, rec, mini_cfg)

    assert "正在下载" in str(ei.value)
    assert fake.calls == []


def test_finalize_without_result_file_id_raises(tmp_path, mock_http, mini_cfg):
    fake = mock_http()
    store = _store(tmp_path)
    rec = _rec(status=tasks.SUCCESS, result_file_id=None)

    with pytest.raises(APIError) as ei:
        tasks.finalize_record(store, rec, mini_cfg)

    assert "file_id" in str(ei.value)
    assert rec.status == tasks.SUCCESS
    assert fake.calls == []


def test_finalize_download_failure_rolls_back_to_success(tmp_path, mock_http, mini_cfg):
    """下载 500：状态回滚 SUCCESS（允许再次下载打包）并记 last_error。"""
    fake = mock_http()
    fake.add_get("retrieve_content", FakeResponse(status_code=500))
    store = _store(tmp_path)
    rec = _rec(status=tasks.SUCCESS, result_file_id="901",
               output_dir=str(tmp_path / "语音包"))

    with pytest.raises(APIError):
        tasks.finalize_record(store, rec, mini_cfg)

    assert rec.status == tasks.SUCCESS
    assert "服务器错误" in rec.last_error
    assert store.get("456").status == tasks.SUCCESS            # 回滚已落盘
    assert store.get("456").last_error == rec.last_error
    assert not os.path.exists(os.path.join(rec.output_dir, "中文"))


def test_finalize_non_tar_body_rolls_back(tmp_path, mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("retrieve_content", FakeResponse(content=b"<html>boom</html>"))
    store = _store(tmp_path)
    rec = _rec(status=tasks.SUCCESS, result_file_id="901",
               output_dir=str(tmp_path / "out2"))

    with pytest.raises(APIError) as ei:
        tasks.finalize_record(store, rec, mini_cfg)

    assert ei.value.error_type == "parse"
    assert rec.status == tasks.SUCCESS
    assert "tar" in rec.last_error


def test_finalize_records_extract_warnings_but_stays_packaged(tmp_path, mock_http,
                                                              mini_cfg, make_tar):
    """结果包混入无法识别的成员：仍完成打包，warning 进 last_error。"""
    fake = mock_http()
    blob = _named_tar_with_junk([("d0001Q001", b"zh-1"), ("d0001Q002", b"zh-2"),
                                 ("d0002Q001", b"en-1"), ("d0002Q002", b"en-2")])
    fake.add_get("retrieve_content", FakeResponse(content=blob))
    store = _store(tmp_path)
    rec = _rec(status=tasks.SUCCESS, result_file_id="901",
               output_dir=str(tmp_path / "out3"))

    tasks.finalize_record(store, rec, mini_cfg)

    assert rec.status == tasks.PACKAGED
    assert "未知成员" in rec.last_error
    assert rec.language_results["中文"]["success"] == ["Q001", "Q002"]


def test_finalize_skips_entries_with_unmapped_dir_code(tmp_path, mock_http,
                                                       mini_cfg, make_tar):
    """entry_map 缺某个目录码时，该目录词条不进任何语言包（不计成功也不计缺失）。"""
    fake = mock_http()
    fake.add_get("retrieve_content", FakeResponse(
        content=make_tar({"d0001Q001": b"zh-1", "d0001Q002": b"zh-2"})))
    store = _store(tmp_path)
    rec = _rec(status=tasks.SUCCESS, result_file_id="901", languages=["中文"],
               entry_map={"d0001": "中文"}, output_dir=str(tmp_path / "out4"))

    tasks.finalize_record(store, rec, mini_cfg)

    assert rec.status == tasks.PACKAGED
    assert rec.language_results["中文"]["success"] == ["Q001", "Q002"]
    assert rec.language_results["中文"]["missing"] == []
    assert not os.path.exists(os.path.join(rec.output_dir, "英文"))
    assert not os.path.exists(os.path.join(rec.output_dir, "英文.tar"))


# ----------------------------------------------------------------- retry_record

def test_retry_resubmits_snapshot_with_new_task_id(tmp_path, mock_http_upload,
                                                   mini_cfg):
    fake = mock_http_upload()
    fake.add_post("files/upload", {"file": {"file_id": 123},
                                   "base_resp": {"status_code": 0}})
    fake.add_post("/v1/t2a_async_v2", {"task_id": 777, "usage_characters": 25,
                                       "base_resp": {"status_code": 0}})
    store = _store(tmp_path)
    old_created = 1000.0
    rec = _rec(status=tasks.FAILED, attempts=1, last_error="服务端任务处理失败",
               created_at=old_created, result_file_id=None)
    store.upsert(rec)
    events = []

    out = tasks.retry_record(store, rec, mini_cfg, on_event=events.append)

    assert out is rec
    assert rec.task_id == "777" and rec.task_id != "456"
    assert rec.attempts == 2
    assert rec.status == tasks.PROCESSING
    assert rec.upload_file_id == 123 and rec.usage_characters == 25
    assert rec.last_error is None and rec.result_file_id is None
    assert rec.created_at > old_created                        # 过期风险重新计时
    assert store.get("777") is rec
    assert store.get("777").status == tasks.PROCESSING
    # 重新提交的 zip 与快照一一对应
    assert _zip_names(fake.uploads[0][2]) == [
        "d0001Q001.txt", "d0001Q002.txt",
        "d0002Q001.txt", "d0002Q002.txt", "d0002Q003.txt",
    ]
    payload = fake.calls[1][2]["json"]
    assert payload["text_file_id"] == 123 and isinstance(payload["text_file_id"], int)
    assert [e.stage for e in events] == ["create", "create"]
    assert "第 2 次尝试" in events[0].message and "5 条" in events[0].message
    assert "task_id=777" in events[1].message


@pytest.mark.parametrize("status", [tasks.SUCCESS, tasks.PROCESSING,
                                   tasks.DOWNLOADING, tasks.PACKAGED])
def test_retry_only_allowed_for_failed_expired_lost(tmp_path, mock_http, mini_cfg,
                                                    status):
    fake = mock_http()
    store = _store(tmp_path)
    rec = _rec(status=status)

    with pytest.raises(APIError) as ei:
        tasks.retry_record(store, rec, mini_cfg)

    assert "仅失败/过期/丢失的任务可重试" in str(ei.value)
    assert fake.calls == []


@pytest.mark.parametrize("status", [tasks.FAILED, tasks.EXPIRED, tasks.LOST])
def test_retry_allowed_for_terminal_and_lost(tmp_path, mock_http_upload, mini_cfg,
                                             status):
    fake = mock_http_upload()
    fake.add_post("files/upload", {"file": {"file_id": 1},
                                   "base_resp": {"status_code": 0}})
    fake.add_post("/v1/t2a_async_v2", {"task_id": "new-" + status,
                                       "base_resp": {"status_code": 0}})
    store = _store(tmp_path)
    rec = _rec(task_id="old-" + status, status=status, attempts=3)

    tasks.retry_record(store, rec, mini_cfg)

    assert rec.task_id == "new-" + status
    assert rec.attempts == 4
    assert rec.status == tasks.PROCESSING


def test_retry_replaces_old_task_id_key(tmp_path, mock_http_upload,
                                        mini_cfg):
    """retry 换 id 后 store 不应残留旧 id 键（已修复：retry 先 remove 旧主键再 upsert）。"""
    fake = mock_http_upload()
    fake.add_post("files/upload", {"file": {"file_id": 1},
                                   "base_resp": {"status_code": 0}})
    fake.add_post("/v1/t2a_async_v2", {"task_id": "999",
                                       "base_resp": {"status_code": 0}})
    store = _store(tmp_path)
    store.upsert(_rec(task_id="456", status=tasks.FAILED))

    tasks.retry_record(store, rec=store.get("456"), pcfg=mini_cfg)

    assert [r.task_id for r in store.list()] == ["999"]             # 只剩新 id，无别名
    assert store.get("456") is None                                 # 旧键已移除
    assert store.remove("999") is True
    assert len(store.list()) == 0                                   # 删除即彻底删除
    # 重载后一致，无幽灵行
    assert [r.task_id for r in tasks.TaskStore(store.path).list()] == []


def test_retry_without_snapshot_raises(tmp_path, mock_http, mini_cfg):
    fake = mock_http()
    store = _store(tmp_path)
    rec = _rec(status=tasks.FAILED, entries=[], entry_count=0)

    with pytest.raises(APIError) as ei:
        tasks.retry_record(store, rec, mini_cfg)

    assert "词条快照" in str(ei.value)
    assert fake.calls == []
    assert rec.status == tasks.FAILED                          # 失败不改状态


def test_retry_rejects_unknown_provider(tmp_path, mini_cfg):
    store = _store(tmp_path)
    with pytest.raises(ConfigError):
        tasks.retry_record(store, _rec(status=tasks.FAILED, provider="silk"),
                           {"api_key": "k"})


# --------------------------------------------------------------- TaskController

def _controller(tmp_path, mini_cfg, posted):
    """建控制器：post 回调收集到 posted 列表（worker 线程写入）。"""
    def post(kind, payload):
        posted.append((kind, payload))

    ctl = TaskController(str(tmp_path / "tasks.json"), lambda: mini_cfg, post)
    return ctl


def test_controller_refresh_updates_record_and_posts(tmp_path, mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", _ok_query(file_id=901))
    posted = []
    ctl = _controller(tmp_path, mini_cfg, posted)
    ctl.store.upsert(_rec(status=tasks.PROCESSING))

    ctl.request_refresh(["456"])

    ops = _wait_for(posted, "task_op", 1)
    assert ops[0][0] is True
    assert "1 条状态更新" in ops[0][1]
    assert _wait_for(posted, "tasks_changed", 1) == [None]
    assert ctl.get("456").status == tasks.SUCCESS
    assert ctl.get("456").result_file_id == "901"
    assert [e.stage for k, e in posted if k == "event"] == ["refresh"]
    assert "待下载" in [e.message for k, e in posted if k == "event"][0]
    assert os.path.exists(str(tmp_path / "tasks.json"))
    # 刷新后仍需人工/界面触发下载：状态未越级到 packaged
    assert ctl.get("456").status == tasks.SUCCESS


def test_controller_refresh_all_non_terminal_by_default(tmp_path, mock_http, mini_cfg):
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", _ok_query())
    posted = []
    ctl = _controller(tmp_path, mini_cfg, posted)
    ctl.store.upsert(_rec(task_id="a", status=tasks.PROCESSING, created_at=2.0))
    ctl.store.upsert(_rec(task_id="b", status=tasks.PACKAGED, created_at=1.0))

    ctl.request_refresh(None)

    ops = _wait_for(posted, "task_op", 1)
    assert ops[0][0] is True
    assert len(fake.calls) == 1                                # 只有 a 被查询
    assert ctl.get("a").status == tasks.SUCCESS
    assert ctl.get("b").status == tasks.PACKAGED


def test_controller_finalize_full_chain(tmp_path, mock_http, mini_cfg, make_tar):
    fake = mock_http()
    fake.add_get("retrieve_content", FakeResponse(
        content=make_tar({"d0001Q001": b"zh-1", "d0001Q002": b"zh-2",
                          "d0002Q001": b"en-1", "d0002Q002": b"en-2"})))
    posted = []
    ctl = _controller(tmp_path, mini_cfg, posted)
    out = str(tmp_path / "语音包")
    ctl.store.upsert(_rec(status=tasks.SUCCESS, result_file_id="901", output_dir=out))

    ctl.request_finalize("456")

    ops = _wait_for(posted, "task_op", 1)
    assert ops[0][0] is True
    assert "下载打包完成" in ops[0][1]
    assert _wait_for(posted, "tasks_changed", 1) == [None]
    assert ctl.get("456").status == tasks.PACKAGED
    assert os.path.isfile(os.path.join(out, "中文", "Q001.mp3"))
    assert os.path.isfile(os.path.join(out, "英文.tar"))
    assert [e.stage for k, e in posted if k == "event"] == \
        ["download", "download", "package", "package"]


def test_controller_finalize_missing_record_posts_failure(tmp_path, mock_http,
                                                          mini_cfg):
    fake = mock_http()
    posted = []
    ctl = _controller(tmp_path, mini_cfg, posted)

    ctl.request_finalize("nope")

    ops = _wait_for(posted, "task_op", 1)
    assert ops[0][0] is False
    assert "任务记录不存在" in ops[0][1]
    assert fake.calls == []
    _wait_for(posted, "tasks_changed", 1)                       # finally 仍通知刷新


def test_controller_finalize_error_rolls_back_and_reports(tmp_path, mock_http,
                                                          mini_cfg):
    fake = mock_http()
    fake.add_get("retrieve_content", FakeResponse(status_code=500))
    posted = []
    ctl = _controller(tmp_path, mini_cfg, posted)
    ctl.store.upsert(_rec(status=tasks.SUCCESS, result_file_id="901",
                          output_dir=str(tmp_path / "o")))

    ctl.request_finalize("456")

    ops = _wait_for(posted, "task_op", 1)
    assert ops[0][0] is False
    assert "服务器错误" in ops[0][1]
    assert ctl.get("456").status == tasks.SUCCESS              # 回滚可重试
    assert ops[0][1] == ctl.get("456").last_error


def test_controller_retry_resubmits_and_renames_task_id(tmp_path, mock_http_upload,
                                                        mini_cfg):
    fake = mock_http_upload()
    fake.add_post("files/upload", {"file": {"file_id": 12},
                                   "base_resp": {"status_code": 0}})
    fake.add_post("/v1/t2a_async_v2", {"task_id": 99,
                                       "base_resp": {"status_code": 0}})
    posted = []
    ctl = _controller(tmp_path, mini_cfg, posted)
    ctl.store.upsert(_rec(task_id="456", status=tasks.EXPIRED))

    ctl.request_retry("456")

    ops = _wait_for(posted, "task_op", 1)
    assert ops[0][0] is True
    assert "task_id=99" in ops[0][1]
    assert ctl.get("99").attempts == 2
    assert ctl.get("99").status == tasks.PROCESSING
    # 旧 id 键已被移除（retry 换主键不残留别名）
    assert ctl.get("456") is None
    assert _zip_names(fake.uploads[0][2])[0] == "d0001Q001.txt"


def test_controller_delete_removes_record(tmp_path, mock_http, mini_cfg):
    posted = []
    ctl = _controller(tmp_path, mini_cfg, posted)
    ctl.store.upsert(_rec())
    assert [r.task_id for r in ctl.list_records()] == ["456"]

    ctl.request_delete("456")

    ops = _wait_for(posted, "task_op", 1)
    assert ops[0] == (True, "记录已删除")
    assert _wait_for(posted, "tasks_changed", 1) == [None]
    assert ctl.list_records() == []
    assert _read_json(tmp_path)["tasks"] == []


def test_controller_ops_run_serially(tmp_path, mock_http, mini_cfg):
    """后台单线程串行：两条记录各一次刷新，两次操作结果按序回报。"""
    fake = mock_http()
    fake.add_get("t2a_async_query_v2", [_ok_query(), _ok_query()])
    posted = []
    ctl = _controller(tmp_path, mini_cfg, posted)
    ctl.store.upsert(_rec(task_id="a", status=tasks.PROCESSING, created_at=2.0))
    ctl.store.upsert(_rec(task_id="b", status=tasks.PROCESSING, created_at=1.0))
    first_ops = _count_of(posted, "task_op")

    ctl.request_refresh(["a"])
    ctl.request_refresh(["b"])

    ops = _wait_for(posted, "task_op", first_ops + 2)
    assert ops[first_ops][0] is True and ops[first_ops + 1][0] is True
    _wait_for(posted, "tasks_changed", first_ops + 2)
    assert len(fake.calls) == 2


def test_controller_unknown_record_for_retry(tmp_path, mock_http_upload, mini_cfg):
    fake = mock_http_upload()
    posted = []
    ctl = _controller(tmp_path, mini_cfg, posted)

    ctl.request_retry("ghost")

    ops = _wait_for(posted, "task_op", 1)
    assert ops[0][0] is False
    assert "ghost" in ops[0][1]
    assert fake.calls == []


# ------------------------------------------------------------ 无副作用守卫（收尾）

def _scan_project_root_paths():
    import t2s_tool

    root = os.path.dirname(os.path.abspath(t2s_tool.__file__))   # <项目根>/t2s_tool
    root = os.path.dirname(root)
    names = ("tasks.json", "tasks.json.tmp", "out", "语音包/未命名")
    paths = [os.path.join(root, name) for name in names]
    return root, {p for p in paths if os.path.exists(p)}


# 模块导入（收集阶段，先于任何用例执行）记录会话开始前已存在的路径：
# 项目根可能有用户真实 GUI 运行数据（如 tasks.json），守卫只对测试期间新增的路径负责。
_ROOT, _PREEXISTING_PATHS = _scan_project_root_paths()


def test_lifecycle_ops_leave_no_files_in_project_root():
    """全链路只写 tmp_path：项目根不得出现测试新增的 tasks.json / 相对 out/ 等残留。

    TaskRecord 默认 output_dir 是相对路径 "out"，一旦有用例漏传 tmp_path，
    本用例会立刻失败（而不是把垃圾文件写进仓库）。会话开始前已存在的运行时
    文件（用户真实数据）不算残留，不做断言。
    """
    for path in ("tasks.json", "tasks.json.tmp", "out", "语音包/未命名"):
        full = os.path.join(_ROOT, path)
        assert full in _PREEXISTING_PATHS or not os.path.exists(full), path
