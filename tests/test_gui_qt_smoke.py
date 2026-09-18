from __future__ import annotations

"""PySide6 视图层冒烟测试：offscreen 平台实例化主窗口，驱动目录加载与级联选择。

无显示服务器也可运行（QT_QPA_PLATFORM=offscreen）；未安装 PySide6 时自动跳过。
"""

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from t2s_tool.gui.app import App, SettingsDialog  # noqa: E402

HEADERS = ("语音ID", "中文", "英文")
ROWS = [("Q001", "你好", "hello"), ("Q002", "谢谢", "thanks"), ("Q003", None, "empty zh")]


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(tmp_path, qapp, make_xlsx, monkeypatch):
    """主窗口：配置/任务记录重定向到临时目录，避免读写项目真实文件。"""
    cfg = tmp_path / "cfg.json"
    tasks = tmp_path / "tasks.json"
    monkeypatch.setattr("t2s_tool.paths.default_config_path", lambda: str(cfg))
    monkeypatch.setattr("t2s_tool.paths.default_tasks_path", lambda: str(tasks))
    w = App()
    w.ctl.config_path = str(cfg)
    yield w
    w.close()


def test_three_tabs_and_shared_catalog(window, make_xlsx):
    assert window.tabs.count() == 3
    xlsx = make_xlsx(HEADERS, ROWS)
    window.input_edit.setText(xlsx)
    window._load_catalog()
    # 批量页签：复选列表默认全选
    assert window.lang_list.count() == 2
    assert window._selected_languages() == ["中文", "英文"]
    # 单条页签：语言下拉联动 ID 下拉与文本预览
    assert window.single_lang_combo.currentText() == "中文"
    ids = [window.single_id_combo.itemText(i) for i in range(window.single_id_combo.count())]
    assert ids == ["Q001", "Q002", "Q003"]
    assert window.single_text.toPlainText() == "你好"


def test_language_select_all_clear(window, make_xlsx):
    window.input_edit.setText(make_xlsx(HEADERS, ROWS))
    window._load_catalog()
    window._set_all_languages(Qt.Unchecked)
    assert window._selected_languages() == []  # 全不选 = 全部语言
    window._set_all_languages(Qt.Checked)
    assert len(window._selected_languages()) == 2


def test_single_empty_text_disables_convert(window, make_xlsx):
    window.input_edit.setText(make_xlsx(HEADERS, ROWS))
    window._load_catalog()
    window.single_id_combo.setCurrentText("Q003")  # 中文列空文本
    assert not window.single_btn.isEnabled()
    assert "空文本" in window.single_text.toPlainText()
    window.single_id_combo.setCurrentText("Q002")
    assert window.single_btn.isEnabled()


def test_single_id_filter(window, make_xlsx):
    window.input_edit.setText(make_xlsx(HEADERS, ROWS))
    window._load_catalog()
    window.single_filter.setText("Q001")
    assert window.single_id_combo.count() == 1
    window.single_filter.clear()
    assert window.single_id_combo.count() == 3


def test_settings_dialog_roundtrip(window):
    dlg = SettingsDialog(window, "MiniMax", window._block)
    dlg.voice_id.setText("test-voice")
    dlg._on_accept()
    assert dlg.result_block is not None
    assert dlg.result_block["voice_setting"]["voice_id"] == "test-voice"
    # 未提供的键回落保留原块内容
    assert "audio_setting" in dlg.result_block
