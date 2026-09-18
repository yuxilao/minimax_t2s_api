from __future__ import annotations

import os
import threading

import pytest

from t2s_tool.errors import CancelledError, ParseError
from t2s_tool.gui.controller import GuiController, GuiParams, GuiSingleParams
from t2s_tool.models import JobReport, SingleResult

HEADERS = ("语音ID", "中文", "英文")
ROWS = [
    ("Q001", "一", "one"),
    ("Q002", "二", "two"),
    ("Q003", None, "three"),
]


def _ctl(tmp_path):
    # 显式指向不存在的配置文件，避免读项目真实 config.json
    return GuiController(config_path=str(tmp_path / "gui-cfg.json"))


def _make_gui_xlsx(make_xlsx):
    return make_xlsx(HEADERS, ROWS, name="gui.xlsx")


def test_parser_choices(tmp_path):
    choices = _ctl(tmp_path).parser_choices()
    assert choices[0][0] == "auto"
    names = [n for n, _ in choices]
    assert "xlsx_9lang" in names


def test_provider_choices(tmp_path):
    names = [n for n, _ in _ctl(tmp_path).provider_choices()]
    assert "minimax" in names
    assert "fake" in names


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = _ctl(tmp_path).load_config()  # 不抛异常
    assert "minimax" in cfg["providers"]
    assert cfg["providers"]["minimax"]["api_key"] == ""
    assert cfg["provider"] == "minimax"


def test_save_config_roundtrip(tmp_path):
    ctl = _ctl(tmp_path)
    ctl.save_config("minimax", {"api_key": "round-key", "model": "speech-2.8-hd"})
    assert os.path.exists(ctl.config_path)
    cfg = ctl.load_config()
    assert cfg["provider"] == "minimax"
    assert cfg["providers"]["minimax"]["api_key"] == "round-key"
    # 未提供的键回落默认
    assert cfg["providers"]["minimax"]["voice_setting"]["voice_id"] == "male-qn-qingse"


def test_parse_preview_and_language_choices(tmp_path, make_xlsx):
    ctl = _ctl(tmp_path)
    xlsx = _make_gui_xlsx(make_xlsx)
    result = ctl.parse_preview(xlsx, "auto")
    assert len(result.languages) == 2
    choices = ctl.language_choices()
    assert choices == [("中文", 3), ("英文", 3)]


def test_parse_preview_missing_file_raises(tmp_path):
    ctl = _ctl(tmp_path)
    with pytest.raises(ParseError):
        ctl.parse_preview(str(tmp_path / "nope.xlsx"), "auto")


def test_language_choices_before_preview_is_empty(tmp_path):
    assert _ctl(tmp_path).language_choices() == []


def _wait(done, timeout=60):
    assert done.wait(timeout), "任务线程未在 %s 秒内完成" % timeout


def test_start_on_done_success(tmp_path, make_xlsx):
    ctl = _ctl(tmp_path)
    xlsx = _make_gui_xlsx(make_xlsx)
    out_dir = str(tmp_path / "gui-out")
    done = threading.Event()
    box = {}

    def on_done(ok, payload):
        box["ok"] = ok
        box["payload"] = payload
        done.set()

    params = GuiParams(
        input_path=xlsx,
        parser_name="auto",
        languages=["英文"],
        provider_name="fake",
        provider_config={},
        output_dir=out_dir,
    )
    ctl.start(params, on_event=lambda ev: None, on_done=on_done)
    _wait(done)
    assert box["ok"] is True, box.get("payload")
    report = box["payload"]
    assert isinstance(report, JobReport)
    assert len(report.language_reports) == 1
    rep = report.language_reports[0]
    assert rep.language == "英文"
    assert rep.success_ids == ["Q001", "Q002", "Q003"]
    assert os.path.isfile(os.path.join(out_dir, "英文.tar"))
    assert os.path.isfile(os.path.join(out_dir, "英文", "Q002.mp3"))


def test_cancel_after_start_no_exception(tmp_path, make_xlsx):
    ctl = _ctl(tmp_path)
    xlsx = _make_gui_xlsx(make_xlsx)
    done = threading.Event()
    box = {}

    def on_done(ok, payload):
        box["ok"] = ok
        box["payload"] = payload
        done.set()

    params = GuiParams(
        input_path=xlsx,
        parser_name="auto",
        languages=[],
        provider_name="fake",
        provider_config={},
        output_dir=str(tmp_path / "gui-cancel"),
    )
    ctl.start(params, on_event=lambda ev: None, on_done=on_done)
    ctl.cancel()  # 立即取消，不应抛出
    _wait(done)
    # 竞态允许两种结局：取消前已全部完成（ok=True），或 CancelledError（ok=False）
    if not box["ok"]:
        assert isinstance(box["payload"], CancelledError)


def test_entry_choices_before_preview_is_empty(tmp_path):
    assert _ctl(tmp_path).entry_choices("英文") == []


def test_entry_choices_sorted_and_text(tmp_path, make_xlsx):
    ctl = _ctl(tmp_path)
    ctl.parse_preview(_make_gui_xlsx(make_xlsx), "auto")
    assert ctl.entry_choices("英文") == [
        ("Q001", "one"), ("Q002", "two"), ("Q003", "three")]
    zh = dict(ctl.entry_choices("中文"))
    assert zh["Q003"] == ""  # 空文本条目 text 为空串


def test_entry_choices_unknown_language_raises(tmp_path, make_xlsx):
    ctl = _ctl(tmp_path)
    ctl.parse_preview(_make_gui_xlsx(make_xlsx), "auto")
    with pytest.raises(ParseError):
        ctl.entry_choices("日语")


def test_start_single_success(tmp_path, make_xlsx):
    ctl = _ctl(tmp_path)
    xlsx = _make_gui_xlsx(make_xlsx)
    out_dir = str(tmp_path / "gui-single-out")
    done = threading.Event()
    box = {}

    def on_done(ok, payload):
        box["ok"] = ok
        box["payload"] = payload
        done.set()

    params = GuiSingleParams(
        input_path=xlsx,
        parser_name="auto",
        language="英文",
        voice_id="Q002",
        provider_name="fake",
        provider_config={},
        output_dir=out_dir,
    )
    ctl.start_single(params, on_event=lambda ev: None, on_done=on_done)
    _wait(done)
    assert box["ok"] is True, box.get("payload")
    res = box["payload"]
    assert isinstance(res, SingleResult)
    with open(res.output_path, "rb") as f:
        assert f.read() == b"FAKE-MP3:Q002"


def test_start_single_unknown_language_fails(tmp_path, make_xlsx):
    ctl = _ctl(tmp_path)
    xlsx = _make_gui_xlsx(make_xlsx)
    done = threading.Event()
    box = {}

    def on_done(ok, payload):
        box["ok"] = ok
        box["payload"] = payload
        done.set()

    params = GuiSingleParams(
        input_path=xlsx,
        parser_name="auto",
        language="日语",
        voice_id="Q001",
        provider_name="fake",
        provider_config={},
        output_dir=str(tmp_path / "gui-single-fail"),
    )
    ctl.start_single(params, on_event=lambda ev: None, on_done=on_done)
    _wait(done)
    assert box["ok"] is False
    assert isinstance(box["payload"], ParseError)

# 注：主窗口冒烟已由 tests/test_gui_qt_smoke.py 覆盖（PySide6 offscreen 平台），
# 旧的 tkinter 版 test_gui_smoke 随视图层重写移除。
