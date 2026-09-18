# -*- coding: utf-8 -*-
"""PySide6 视图层：语音包工具主窗口（Windows / Linux 跨平台）。

设计原则（UI 评审后重构）：
- 目录加载全局共享：顶部「语音目录」区加载一次，批量/单条两个页签共用解析结果；
- 参数收纳进「设置」对话框：主界面只留供应商选择 + 音色摘要，12 个合成参数不常驻；
- 输出目录全局唯一：底部栏一处修改，两个页签共用；
- 语言选择为复选列表（默认全选），提供全选/清空，不再依赖 Ctrl 点选隐式语义；
- 反馈人话化：进度条 + 中文状态行 + 状态栏临时消息；技术细节进可折叠的「详细日志」；
  错误才弹窗，成功只走状态栏；
- 单条转换不提供「取消」按钮（同步请求发出后不可中断，不给虚假期待）。

控件命名约定（供 scripts/gui_e2e_check.py 自动化驱动）：
  input_edit / parser_combo / load_btn / catalog_info
  lang_list(QListWidget 复选) / sel_all_btn / sel_none_btn / lang_count
  start_btn / cancel_btn / progress / batch_status / log_view / clear_log_btn
  single_lang_combo / single_filter / single_id_combo / single_text
  single_btn / single_status / single_open_btn
  tasks_tree(QTreeWidget) / refresh_btn / finalize_btn / retry_btn / delete_btn
  auto_refresh_chk / tasks_status
  output_edit / output_browse_btn / open_output_btn / provider_combo / voice_label / settings_btn
"""
from __future__ import annotations

import copy
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QTabWidget, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .. import config as config_mod, paths, tasks as tasks_mod
from ..config import PROVIDER_DEFAULTS
from ..errors import ParseError
from ..tasks import STATUS_LABELS, expiry_risk
from .controller import GuiController, GuiParams, GuiSingleParams
from .task_controller import TaskController

# ------------------------------------------------------------------ 样式
ACCENT = "#3d6fe0"
OK_COLOR = "#0a7a2f"
ERR_COLOR = "#b02a2a"
BUSY_COLOR = "#0a58ca"

QSS = f"""
QMainWindow, QDialog {{ background: #f5f6fa; }}
QGroupBox {{
    font-weight: 600; border: 1px solid #d9dce3; border-radius: 8px;
    margin-top: 12px; padding-top: 10px; background: #ffffff;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #333333; }}
QPushButton {{
    border: 1px solid #c9cdd6; border-radius: 6px; padding: 6px 14px; background: #ffffff;
}}
QPushButton:hover {{ background: #eef1f7; }}
QPushButton:disabled {{ color: #9aa0ab; background: #f0f1f4; border-color: #e0e2e8; }}
QPushButton[primary="true"] {{
    background: {ACCENT}; color: #ffffff; border: none; font-weight: 600; padding: 7px 20px;
}}
QPushButton[primary="true"]:hover {{ background: #335fc4; }}
QPushButton[primary="true"]:disabled {{ background: #a9bce8; color: #f4f6fc; }}
QLineEdit, QComboBox, QPlainTextEdit, QListWidget, QTreeWidget, QSpinBox, QDoubleSpinBox {{
    border: 1px solid #d2d6de; border-radius: 6px; padding: 4px 6px; background: #ffffff;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QTabWidget::pane {{ border: 1px solid #d9dce3; border-radius: 8px; background: #ffffff; }}
QTabBar::tab {{
    padding: 8px 20px; border-top-left-radius: 8px; border-top-right-radius: 8px;
    background: #e9ebf1; margin-right: 4px; color: #444444;
}}
QTabBar::tab:selected {{ background: #ffffff; color: #222222; font-weight: 600; }}
QProgressBar {{
    border: 1px solid #d2d6de; border-radius: 7px; height: 14px;
    text-align: center; background: #f0f1f4; color: #555555;
}}
QProgressBar::chunk {{ border-radius: 6px; background: {ACCENT}; }}
QStatusBar {{ background: #eef0f4; }}
QToolButton {{ border: none; color: {ACCENT}; padding: 2px 6px; }}
"""

# 后台 stage -> 人话状态
STAGE_LABELS = {
    "parse": "解析目录", "zip": "打包文本", "upload": "上传",
    "create": "创建/合成", "poll": "转换中", "download": "下载结果",
    "package": "整理语音包", "done": "完成",
}

# 设置对话框下拉的可选值（可编辑，不限制新值）
MODEL_CHOICES = ["speech-2.8-turbo", "speech-2.8-hd", "speech-02-turbo", "speech-02-hd",
                 "speech-01-turbo", "speech-01-hd"]
SAMPLE_RATES = ["8000", "16000", "22050", "24000", "32000", "44100"]
BITRATES = ["32000", "64000", "96000", "128000", "160000", "192000", "256000"]
FORMATS = ["mp3", "pcm", "flac"]
CHANNELS = ["1", "2"]


def _editable_combo(items: List[str], current: str) -> QComboBox:
    cb = QComboBox()
    cb.setEditable(True)
    cb.addItems(items)
    if current and current not in items:
        cb.addItem(current)
    cb.setCurrentText(current or "")
    return cb


class SettingsDialog(QDialog):
    """供应商与合成参数设置（参数不常驻主界面，在这里「设一次」）。

    编辑的是当前供应商的配置块；保存时整体写回 config.json。
    """

    def __init__(self, parent: QWidget, display_name: str, block: Dict[str, Any]) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"设置 — {display_name}")
        self.setMinimumWidth(520)
        self._block = copy.deepcopy(block)

        vs = block.get("voice_setting") or {}
        audio = block.get("audio_setting") or {}

        root = QVBoxLayout(self)

        basic = QGroupBox("账号与音色")
        form = QFormLayout(basic)
        key_row = QHBoxLayout()
        self.api_key = QLineEdit(str(block.get("api_key") or ""))
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("粘贴 API Key")
        eye = QToolButton()
        eye.setText("👁")
        eye.setCheckable(True)
        eye.toggled.connect(lambda on: self.api_key.setEchoMode(
            QLineEdit.Normal if on else QLineEdit.Password))
        key_row.addWidget(self.api_key, 1)
        key_row.addWidget(eye)
        form.addRow("API Key", key_row)
        self.voice_id = QLineEdit(str(vs.get("voice_id") or ""))
        self.voice_id.setPlaceholderText("如 English_radiant_girl")
        form.addRow("音色 ID", self.voice_id)
        root.addWidget(basic)

        adv = QGroupBox("高级参数（一般保持默认）")
        form = QFormLayout(adv)
        self.base_url = QLineEdit(str(block.get("base_url") or ""))
        form.addRow("base_url", self.base_url)
        self.model = _editable_combo(MODEL_CHOICES, str(block.get("model") or ""))
        form.addRow("模型", self.model)
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.5, 2.0)
        self.speed.setSingleStep(0.1)
        self.speed.setValue(float(vs.get("speed") or 1.0))
        form.addRow("语速", self.speed)
        self.vol = QDoubleSpinBox()
        self.vol.setRange(0.1, 10.0)
        self.vol.setSingleStep(0.1)
        self.vol.setValue(float(vs.get("vol") or 1.0))
        form.addRow("音量", self.vol)
        self.pitch = QSpinBox()
        self.pitch.setRange(-12, 12)
        self.pitch.setValue(int(vs.get("pitch") or 0))
        form.addRow("音调", self.pitch)
        self.sample_rate = _editable_combo(SAMPLE_RATES, str(audio.get("audio_sample_rate") or ""))
        form.addRow("采样率", self.sample_rate)
        self.bitrate = _editable_combo(BITRATES, str(audio.get("bitrate") or ""))
        form.addRow("比特率", self.bitrate)
        self.audio_format = _editable_combo(FORMATS, str(audio.get("format") or ""))
        form.addRow("格式", self.audio_format)
        self.channel = _editable_combo(CHANNELS, str(audio.get("channel") or ""))
        form.addRow("声道", self.channel)
        root.addWidget(adv)

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Save).setText("保存")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

        self.result_block: Optional[Dict[str, Any]] = None

    def _on_accept(self) -> None:
        try:
            sample_rate = int(self.sample_rate.currentText().strip())
            bitrate = int(self.bitrate.currentText().strip())
            channel = int(self.channel.currentText().strip())
        except ValueError:
            QMessageBox.warning(self, "参数错误", "采样率 / 比特率 / 声道需要整数")
            return
        block = copy.deepcopy(self._block)
        block["api_key"] = self.api_key.text().strip()
        block["base_url"] = self.base_url.text().strip()
        block["model"] = self.model.currentText().strip()
        voice = dict(block.get("voice_setting") or {})
        voice.update({"voice_id": self.voice_id.text().strip(),
                      "speed": self.speed.value(), "vol": self.vol.value(),
                      "pitch": self.pitch.value()})
        block["voice_setting"] = voice
        audio = dict(block.get("audio_setting") or {})
        audio.update({"audio_sample_rate": sample_rate, "bitrate": bitrate,
                      "format": self.audio_format.currentText().strip(), "channel": channel})
        block["audio_setting"] = audio
        self.result_block = block
        self.accept()


class _Bridge(QObject):
    """工作线程 -> GUI 线程 的信号桥（Qt 自动排队到主线程）。"""

    job_event = Signal(object)          # StageEvent（批量）
    job_done = Signal(object)           # (ok, payload)
    single_event = Signal(object)       # StageEvent（单条）
    single_done = Signal(object)        # (ok, payload)
    task_msg = Signal(str, object)      # TaskController 回调


class App(QMainWindow):
    """主窗口：顶部共享目录区 + 三页签（批量转换/单条转换/任务中心）+ 底部输出与供应商栏。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("语音包工具 (MiniMax)")
        self.resize(1000, 760)
        self.setMinimumSize(880, 620)

        self.ctl = GuiController()
        self.bridge = _Bridge()
        self.bridge.job_event.connect(self._on_job_event)
        self.bridge.job_done.connect(self._on_job_done)
        self.bridge.single_event.connect(self._on_single_event)
        self.bridge.single_done.connect(self._on_single_done)
        self.bridge.task_msg.connect(self._on_task_msg)

        # 供应商 name <-> display 映射
        self._provider_pairs: List[Tuple[str, str]] = self.ctl.provider_choices()
        self._provider_by_display: Dict[str, str] = {d: n for n, d in self._provider_pairs}
        self._provider_display: Dict[str, str] = {n: d for n, d in self._provider_pairs}
        self._parser_pairs: List[Tuple[str, str]] = self.ctl.parser_choices()
        self._parser_by_display: Dict[str, str] = {d: n for n, d in self._parser_pairs}

        self._cfg: dict = {"provider": "", "providers": {}}
        self._block: dict = copy.deepcopy(PROVIDER_DEFAULTS)
        self._single_text_by_id: Dict[str, str] = {}
        self._single_ids_all: List[str] = []
        self._single_last_path: Optional[str] = None
        self._expiry_warned: set = set()
        self._log_was_poll = False

        self.task_ctl = TaskController(
            paths.default_tasks_path(),
            lambda: config_mod.get_provider_config(self._cfg, "minimax"),
            lambda kind, payload: self.bridge.task_msg.emit(kind, payload),
        )

        self._build_ui()
        self._initial_load_config()
        self._reload_tasks()

        # 任务中心自动刷新（勾选后每 60 秒）
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(60000)
        self._auto_timer.timeout.connect(self._auto_refresh_tick)

    # ================================================================ 布局
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)
        self.setCentralWidget(central)

        root.addWidget(self._build_catalog_group())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_batch_tab(), "批量转换")
        self.tabs.addTab(self._build_single_tab(), "单条转换")
        self.tabs.addTab(self._build_tasks_tab(), "任务中心")
        root.addWidget(self.tabs, 1)
        root.addWidget(self._build_bottom_bar())
        self.statusBar().showMessage("就绪", 3000)

    def _build_catalog_group(self) -> QGroupBox:
        box = QGroupBox("语音目录（批量与单条转换共用）")
        lay = QVBoxLayout(box)
        row = QHBoxLayout()
        row.addWidget(QLabel("目录文件"))
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("选择语音目录文件（.xlsx），加载后两个页签共用")
        row.addWidget(self.input_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_input)
        row.addWidget(browse)
        row.addWidget(QLabel("解析器"))
        self.parser_combo = QComboBox()
        self.parser_combo.addItems([d for _, d in self._parser_pairs])
        row.addWidget(self.parser_combo)
        self.load_btn = QPushButton("加载目录")
        self.load_btn.setProperty("primary", True)
        self.load_btn.clicked.connect(self._load_catalog)
        row.addWidget(self.load_btn)
        lay.addLayout(row)
        self.catalog_info = QLabel("尚未加载目录")
        self.catalog_info.setStyleSheet("color:#777777;")
        lay.addWidget(self.catalog_info)
        return box

    def _build_batch_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)

        lang_box = QGroupBox("选择语言")
        lang_lay = QVBoxLayout(lang_box)
        btns = QHBoxLayout()
        self.sel_all_btn = QPushButton("全选")
        self.sel_all_btn.clicked.connect(lambda: self._set_all_languages(Qt.Checked))
        self.sel_none_btn = QPushButton("清空")
        self.sel_none_btn.clicked.connect(lambda: self._set_all_languages(Qt.Unchecked))
        btns.addWidget(self.sel_all_btn)
        btns.addWidget(self.sel_none_btn)
        self.lang_count = QLabel("请先加载目录")
        self.lang_count.setStyleSheet("color:#777777;")
        btns.addWidget(self.lang_count)
        btns.addStretch(1)
        hint = QLabel("全部不勾选 = 转换全部语言")
        hint.setStyleSheet("color:#999999;")
        btns.addWidget(hint)
        lang_lay.addLayout(btns)
        self.lang_list = QListWidget()
        self.lang_list.setMaximumHeight(180)
        self.lang_list.itemChanged.connect(lambda _item: self._update_lang_count())
        lang_lay.addWidget(self.lang_list)
        lay.addWidget(lang_box)

        run_row = QHBoxLayout()
        self.start_btn = QPushButton("开始转换")
        self.start_btn.setProperty("primary", True)
        self.start_btn.clicked.connect(self._start_batch)
        run_row.addWidget(self.start_btn)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_batch)
        run_row.addWidget(self.cancel_btn)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumWidth(220)
        run_row.addWidget(self.progress)
        self.batch_status = QLabel("就绪")
        run_row.addWidget(self.batch_status)
        run_row.addStretch(1)
        lay.addLayout(run_row)

        log_head = QHBoxLayout()
        self.log_toggle = QToolButton()
        self.log_toggle.setText("收起详细日志 ▾")
        self.log_toggle.setCheckable(True)
        self.log_toggle.setChecked(True)
        self.log_toggle.clicked.connect(self._toggle_log)
        log_head.addWidget(self.log_toggle)
        log_head.addStretch(1)
        self.clear_log_btn = QToolButton()
        self.clear_log_btn.setText("清空")
        self.clear_log_btn.clicked.connect(lambda: self.log_view.clear())
        log_head.addWidget(self.clear_log_btn)
        lay.addLayout(log_head)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        lay.addWidget(self.log_view, 1)
        return page

    def _build_single_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)

        box = QGroupBox("单条语音转换（同步接口：一次请求直接返回音频）")
        form = QFormLayout(box)

        lang_row = QHBoxLayout()
        self.single_lang_combo = QComboBox()
        self.single_lang_combo.setMinimumWidth(220)
        self.single_lang_combo.currentTextChanged.connect(self._on_single_language)
        lang_row.addWidget(self.single_lang_combo, 1)
        form.addRow("语言", lang_row)

        id_row = QHBoxLayout()
        self.single_id_combo = QComboBox()
        self.single_id_combo.setMinimumWidth(220)
        self.single_id_combo.currentTextChanged.connect(self._on_single_id)
        id_row.addWidget(self.single_id_combo, 1)
        self.single_filter = QLineEdit()
        self.single_filter.setPlaceholderText("输入关键字过滤 ID")
        self.single_filter.setClearButtonEnabled(True)
        self.single_filter.setMaximumWidth(180)
        self.single_filter.textChanged.connect(self._apply_id_filter)
        id_row.addWidget(self.single_filter)
        form.addRow("语音ID", id_row)

        self.single_text = QPlainTextEdit()
        self.single_text.setReadOnly(True)
        self.single_text.setPlaceholderText("选择语音ID后显示对应文本")
        self.single_text.setMaximumHeight(120)
        form.addRow("文本预览", self.single_text)
        lay.addWidget(box)

        run_row = QHBoxLayout()
        self.single_btn = QPushButton("转换")
        self.single_btn.setProperty("primary", True)
        self.single_btn.setEnabled(False)
        self.single_btn.clicked.connect(self._start_single)
        run_row.addWidget(self.single_btn)
        self.single_open_btn = QPushButton("打开所在目录")
        self.single_open_btn.clicked.connect(self._single_open_output)
        run_row.addWidget(self.single_open_btn)
        self.single_status = QLabel("请先加载目录并选择语言与语音ID")
        self.single_status.setStyleSheet("color:#777777;")
        run_row.addWidget(self.single_status)
        run_row.addStretch(1)
        lay.addLayout(run_row)
        lay.addStretch(1)
        return page

    def _build_tasks_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        btns = QHBoxLayout()
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self._tasks_refresh)
        btns.addWidget(self.refresh_btn)
        self.finalize_btn = QPushButton("下载并打包")
        self.finalize_btn.clicked.connect(self._tasks_finalize)
        btns.addWidget(self.finalize_btn)
        self.retry_btn = QPushButton("重试")
        self.retry_btn.clicked.connect(self._tasks_retry)
        btns.addWidget(self.retry_btn)
        self.delete_btn = QPushButton("删除记录")
        self.delete_btn.clicked.connect(self._tasks_delete)
        btns.addWidget(self.delete_btn)
        self.auto_refresh_chk = QCheckBox("自动刷新（每 60 秒）")
        self.auto_refresh_chk.toggled.connect(self._on_auto_refresh_toggled)
        btns.addWidget(self.auto_refresh_chk)
        self.tasks_status = QLabel("（无任务）")
        self.tasks_status.setStyleSheet("color:#777777;")
        btns.addWidget(self.tasks_status)
        btns.addStretch(1)
        lay.addLayout(btns)

        self.tasks_tree = QTreeWidget()
        self.tasks_tree.setColumnCount(7)
        self.tasks_tree.setHeaderLabels(
            ["任务ID", "语言", "条数", "状态", "已提交", "结果file_id", "最近错误/缺失"])
        self.tasks_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tasks_tree.setRootIsDecorated(False)
        self.tasks_tree.setUniformRowHeights(True)
        for i, w in enumerate((150, 170, 50, 80, 110, 110, 240)):
            self.tasks_tree.setColumnWidth(i, w)
        lay.addWidget(self.tasks_tree, 1)

        note = QLabel("批量任务的结果文件约 9 小时后过期，请及时「下载并打包」。")
        note.setStyleSheet("color:#999999;")
        lay.addWidget(note)
        return page

    def _build_bottom_bar(self) -> QFrame:
        bar = QFrame()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("输出目录"))
        self.output_edit = QLineEdit(paths.default_output_dir())
        lay.addWidget(self.output_edit, 1)
        self.output_browse_btn = QPushButton("浏览…")
        self.output_browse_btn.clicked.connect(self._browse_output)
        lay.addWidget(self.output_browse_btn)
        self.open_output_btn = QPushButton("打开")
        self.open_output_btn.clicked.connect(self._open_output)
        lay.addWidget(self.open_output_btn)

        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setStyleSheet("color:#d9dce3;")
        lay.addWidget(line)

        lay.addWidget(QLabel("供应商"))
        self.provider_combo = QComboBox()
        self.provider_combo.addItems([d for _, d in self._provider_pairs])
        self.provider_combo.currentTextChanged.connect(self._on_provider_selected)
        lay.addWidget(self.provider_combo)
        self.voice_label = QLabel("")
        self.voice_label.setStyleSheet("color:#777777;")
        lay.addWidget(self.voice_label)
        self.settings_btn = QPushButton("⚙ 设置")
        self.settings_btn.clicked.connect(self._open_settings)
        lay.addWidget(self.settings_btn)
        return bar

    # ================================================================ 通用
    def _parser_name(self) -> str:
        return self._parser_by_display.get(self.parser_combo.currentText(), "auto")

    def _provider_name(self) -> str:
        display = self.provider_combo.currentText()
        if display in self._provider_by_display:
            return self._provider_by_display[display]
        return self._provider_pairs[0][0] if self._provider_pairs else "minimax"

    def _browse_input(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择语音目录文件", "", "Excel 语音目录 (*.xlsx);;所有文件 (*)")
        if path:
            self.input_edit.setText(path)

    def _browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if path:
            self.output_edit.setText(path)

    def _open_path(self, path: str) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            QMessageBox.warning(self, "打开失败", f"{path}\n{e}")

    def _open_output(self) -> None:
        path = os.path.abspath(self.output_edit.text().strip() or paths.default_output_dir())
        if not os.path.isdir(path):
            self.statusBar().showMessage(f"输出目录不存在: {path}", 5000)
            return
        self._open_path(path)

    @staticmethod
    def _ago(ts: float) -> str:
        secs = max(0, int(time.time() - ts))
        if secs < 60:
            return f"{secs}秒前"
        if secs < 3600:
            return f"{secs // 60}分钟前"
        if secs < 86400:
            return f"{secs // 3600}小时{secs % 3600 // 60}分前"
        return f"{secs // 86400}天前"

    def _log(self, line: str, _poll: bool = False) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{stamp}  {str(line).replace(chr(10), ' ')}")
        self._log_was_poll = _poll

    def _toggle_log(self, checked: bool) -> None:
        self.log_view.setVisible(checked)
        self.log_toggle.setText("收起详细日志 ▾" if checked else "展开详细日志 ▸")

    def _status(self, label: QLabel, text: str, kind: str = "info") -> None:
        color = {"info": "#555555", "ok": OK_COLOR, "err": ERR_COLOR, "busy": BUSY_COLOR}[kind]
        label.setStyleSheet(f"color:{color};")
        label.setText(text)

    # ================================================================ 配置
    def _initial_load_config(self) -> None:
        try:
            self._cfg = self.ctl.load_config()
        except Exception as e:  # 配置损坏不阻塞窗口启动
            self._cfg = {"provider": "", "providers": {}}
            self._log(f"读取配置失败: {e}")
        name = self._cfg.get("provider") or (self._provider_pairs[0][0] if self._provider_pairs else "")
        self._apply_provider(name)

    def _apply_provider(self, name: str) -> None:
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentText(self._provider_display.get(name, name))
        self.provider_combo.blockSignals(False)
        block = (self._cfg.get("providers") or {}).get(name)
        self._block = copy.deepcopy(block) if isinstance(block, dict) else copy.deepcopy(PROVIDER_DEFAULTS)
        voice = (self._block.get("voice_setting") or {}).get("voice_id") or ""
        self.voice_label.setText(f"音色: {voice}")

    def _on_provider_selected(self, _display: str) -> None:
        self._apply_provider(self._provider_name())

    def _open_settings(self) -> None:
        name = self._provider_name()
        dlg = SettingsDialog(self, self._provider_display.get(name, name), self._block)
        if dlg.exec() != QDialog.Accepted or dlg.result_block is None:
            return
        try:
            self.ctl.save_config(name, dlg.result_block)
        except Exception as e:
            QMessageBox.critical(self, "保存配置失败", str(e))
            self._log(f"保存配置失败: {e}")
            return
        self._cfg.setdefault("providers", {})[name] = copy.deepcopy(dlg.result_block)
        self._block = dlg.result_block
        voice = (self._block.get("voice_setting") or {}).get("voice_id") or ""
        self.voice_label.setText(f"音色: {voice}")
        self.statusBar().showMessage(f"设置已保存（{self._provider_display.get(name, name)}）", 5000)
        self._log(f"已保存 {name} 配置")

    def _require_api_key(self) -> bool:
        """minimax 缺 API Key 时引导去设置对话框，返回 False。"""
        if self._provider_name() == "minimax" and not (self._block.get("api_key") or "").strip():
            ret = QMessageBox.warning(
                self, "缺少 API Key", "当前供应商（MiniMax）还没有配置 API Key。\n是否现在打开「设置」填写？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if ret == QMessageBox.Yes:
                self._open_settings()
            return False
        return True

    # ================================================================ 目录加载（共享）
    def _load_catalog(self) -> None:
        path = self.input_edit.text().strip()
        if not path or not os.path.isfile(path):
            self.statusBar().showMessage("请先选择存在的语音目录文件", 5000)
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = self.ctl.parse_preview(path, self._parser_name())
        except Exception as e:
            QMessageBox.critical(self, "解析失败", str(e))
            self._log(f"解析失败: {e}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        choices = self.ctl.language_choices()
        # 批量页签：复选列表（默认全选）
        self.lang_list.blockSignals(True)
        self.lang_list.clear()
        total_entries = 0
        for lang, count in choices:
            item = QListWidgetItem(f"{lang}（{count} 条）")
            item.setData(Qt.UserRole, lang)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.lang_list.addItem(item)
            total_entries += count
        self.lang_list.blockSignals(False)
        self._update_lang_count()
        # 单条页签：语言下拉
        self.single_lang_combo.blockSignals(True)
        self.single_lang_combo.clear()
        self.single_lang_combo.addItems([lang for lang, _ in choices])
        self.single_lang_combo.blockSignals(False)
        self._single_text_by_id = {}
        self._single_ids_all = []
        self.single_id_combo.clear()
        self.single_text.clear()
        if choices:
            self.single_lang_combo.setCurrentIndex(0)  # 自动选第一种语言
            self._on_single_language(choices[0][0])
        self.catalog_info.setText(
            f"已加载：{result.source_path}（解析器 {result.parser_name}，"
            f"共 {len(choices)} 种语言 / {total_entries} 条）")
        self.catalog_info.setStyleSheet(f"color:{OK_COLOR};")
        for w in result.warnings:
            self._log(f"解析警告: {w}")
        self.statusBar().showMessage(f"目录加载完成：{len(choices)} 种语言", 5000)
        self._log(f"目录加载完成: {result.source_path}，{len(choices)} 种语言 / {total_entries} 条")

    # ================================================================ 批量转换
    def _set_all_languages(self, state: Qt.CheckState) -> None:
        self.lang_list.blockSignals(True)
        for i in range(self.lang_list.count()):
            self.lang_list.item(i).setCheckState(state)
        self.lang_list.blockSignals(False)
        self._update_lang_count()

    def _update_lang_count(self) -> None:
        total = self.lang_list.count()
        checked = sum(1 for i in range(total)
                      if self.lang_list.item(i).checkState() == Qt.Checked)
        self.lang_count.setText(
            f"已选 {checked}/{total} 种语言" if total else "请先加载目录")

    def _selected_languages(self) -> List[str]:
        return [self.lang_list.item(i).data(Qt.UserRole)
                for i in range(self.lang_list.count())
                if self.lang_list.item(i).checkState() == Qt.Checked]

    def _start_batch(self) -> None:
        path = self.input_edit.text().strip()
        if not path or not os.path.isfile(path):
            self.statusBar().showMessage("请先选择存在的语音目录文件", 5000)
            return
        out_dir = self.output_edit.text().strip()
        if not out_dir:
            self.statusBar().showMessage("请填写输出目录", 5000)
            return
        if not self._require_api_key():
            return
        params = GuiParams(
            input_path=path,
            parser_name=self._parser_name(),
            languages=self._selected_languages(),
            provider_name=self._provider_name(),
            provider_config=copy.deepcopy(self._block),
            output_dir=out_dir,
        )
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setRange(0, 0)  # 忙碌指示
        self._status(self.batch_status, "任务进行中…", "busy")
        langs = "、".join(params.languages) or "全部"
        self._log(f"开始批量转换: {params.input_path}（语言: {langs}）")
        self.ctl.start(
            params,
            on_event=self.bridge.job_event.emit,
            on_done=lambda ok, payload: self.bridge.job_done.emit((ok, payload)),
            task_store=self.task_ctl.store,
        )

    def _cancel_batch(self) -> None:
        self.ctl.cancel()
        self._log("已请求取消批量转换…")

    def _on_job_event(self, ev) -> None:
        label = STAGE_LABELS.get(ev.stage, ev.stage)
        self._status(self.batch_status, f"{label}：{ev.message}", "busy")
        if ev.stage == "poll":
            self._log_poll(f"{label}：{ev.message}")
        else:
            self._log(f"{label}：{ev.message}")

    def _log_poll(self, line: str) -> None:
        """轮询进度：替换日志最后一行，不刷屏。"""
        doc = self.log_view.document()
        idx = doc.blockCount() - 1
        last = doc.findBlockByNumber(idx).text() if idx >= 0 else ""
        if self._log_was_poll and "转换中：" in last:
            cursor = QTextCursor(doc.findBlockByNumber(idx))
            cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            cursor.insertText(f"{time.strftime('%H:%M:%S')}  {line}")
            bar = self.log_view.verticalScrollBar()
            bar.setValue(bar.maximum())
            return
        self._log(line, _poll=True)

    def _on_job_done(self, data: Tuple[bool, Any]) -> None:
        ok, payload = data
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        if ok and getattr(payload, "submitted_tasks", None):
            tids = ", ".join(str(t) for t in payload.submitted_tasks)
            self._status(self.batch_status, "任务已提交，请到「任务中心」下载结果", "ok")
            self._log(f"任务已提交: {tids}")
            self.tabs.setCurrentIndex(2)
            self._reload_tasks()
            QMessageBox.information(
                self, "任务已提交",
                f"已提交 {len(payload.submitted_tasks)} 个批量任务：\n{tids}\n\n"
                "请到「任务中心」刷新状态；任务完成后点「下载并打包」落盘。\n"
                "注意：结果文件约 9 小时后过期。")
            return
        if ok:
            lines = []
            for rep in getattr(payload, "language_reports", []) or []:
                succ, miss = len(rep.success_ids), len(rep.missing_ids)
                lines.append(f"{rep.language}: 成功 {succ}，缺失 {miss}")
                self._log(f"{rep.language}: 成功 {succ}，缺失 {miss} -> {rep.tar_path or '未生成语音包'}")
                if rep.missing_ids:
                    self._log(f"  {rep.language} 缺失ID: {', '.join(rep.missing_ids)}")
            for w in (getattr(payload, "parse_warnings", None) or [])[:10]:
                self._log(f"解析警告: {w}")
            self._status(self.batch_status, "转换完成", "ok")
            self.statusBar().showMessage("批量转换完成", 5000)
            QMessageBox.information(
                self, "转换完成",
                "\n".join(lines) + f"\n\n输出目录: {self.output_edit.text().strip()}")
        else:
            self._status(self.batch_status, "任务失败", "err")
            self._log(f"任务失败: {payload}")
            QMessageBox.critical(self, "任务失败", str(payload))

    # ================================================================ 单条转换
    def _on_single_language(self, lang: str) -> None:
        if not lang:
            return
        try:
            entries = self.ctl.entry_choices(lang)
        except ParseError as e:
            QMessageBox.warning(self, "解析失败", str(e))
            return
        self._single_text_by_id = dict(entries)
        self._single_ids_all = [vid for vid, _ in entries]
        self.single_filter.clear()
        self._apply_id_filter()
        if self._single_ids_all:
            self.single_id_combo.setCurrentIndex(0)
            self._on_single_id(self.single_id_combo.currentText())

    def _apply_id_filter(self) -> None:
        keyword = self.single_filter.text().strip()
        current = self.single_id_combo.currentText()
        ids = [v for v in self._single_ids_all if keyword in v] if keyword else self._single_ids_all
        self.single_id_combo.blockSignals(True)
        self.single_id_combo.clear()
        self.single_id_combo.addItems(ids)
        if current in ids:
            self.single_id_combo.setCurrentText(current)
        self.single_id_combo.blockSignals(False)
        self._on_single_id(self.single_id_combo.currentText())

    def _on_single_id(self, vid: str) -> None:
        if not vid:
            self.single_btn.setEnabled(False)
            return
        text = self._single_text_by_id.get(vid, "")
        if text:
            self.single_text.setPlainText(text)
            self.single_btn.setEnabled(True)
            self._status(self.single_status, "就绪", "info")
        else:
            self.single_text.setPlainText("（⚠ 该条目为空文本，无法合成）")
            self.single_btn.setEnabled(False)
            self._status(self.single_status, "空文本无法合成", "err")

    def _start_single(self) -> None:
        path = self.input_edit.text().strip()
        if not path or not os.path.isfile(path):
            self.statusBar().showMessage("请先选择存在的语音目录文件", 5000)
            return
        lang = self.single_lang_combo.currentText().strip()
        vid = self.single_id_combo.currentText().strip()
        if not lang or not vid:
            self.statusBar().showMessage("请先加载目录并选择语言与语音ID", 5000)
            return
        out_dir = self.output_edit.text().strip()
        if not out_dir:
            self.statusBar().showMessage("请填写输出目录", 5000)
            return
        if not self._require_api_key():
            return
        params = GuiSingleParams(
            input_path=path,
            parser_name=self._parser_name(),
            language=lang,
            voice_id=vid,
            provider_name=self._provider_name(),
            provider_config=copy.deepcopy(self._block),
            output_dir=out_dir,
        )
        self.single_btn.setEnabled(False)
        self._status(self.single_status, "转换中…", "busy")
        self._log(f"[单条] 开始转换: {lang}/{vid}")
        self.ctl.start_single(
            params,
            on_event=self.bridge.single_event.emit,
            on_done=lambda ok, payload: self.bridge.single_done.emit((ok, payload)),
        )

    def _on_single_event(self, ev) -> None:
        label = STAGE_LABELS.get(ev.stage, ev.stage)
        self._status(self.single_status, f"{label}：{ev.message}", "busy")
        self._log(f"[单条] {label}：{ev.message}")

    def _on_single_done(self, data: Tuple[bool, Any]) -> None:
        ok, payload = data
        self.single_btn.setEnabled(True)
        if ok:
            self._single_last_path = payload.output_path
            self._status(self.single_status, f"已生成 {payload.output_path}", "ok")
            self.statusBar().showMessage(
                f"{payload.language}/{payload.voice_id} 转换完成", 5000)
            self._log(f"[单条] 完成: {payload.language}/{payload.voice_id} -> "
                      f"{payload.output_path}（{payload.size} 字节）")
        else:
            self._status(self.single_status, "转换失败", "err")
            self._log(f"[单条] 失败: {payload}")
            QMessageBox.critical(self, "单条转换失败", str(payload))

    def _single_open_output(self) -> None:
        if self._single_last_path:
            target = os.path.dirname(self._single_last_path)
        else:
            target = self.output_edit.text().strip()
        target = os.path.abspath(target or paths.default_output_dir())
        if not os.path.isdir(target):
            self.statusBar().showMessage(f"目录不存在: {target}", 5000)
            return
        self._open_path(target)

    # ================================================================ 任务中心
    def _reload_tasks(self) -> None:
        selected_ids = set()
        for item in self.tasks_tree.selectedItems():
            selected_ids.add(str(item.text(0)))
        self.tasks_tree.clear()
        records = self.task_ctl.list_records()
        color_map = {tasks_mod.PROCESSING: BUSY_COLOR, tasks_mod.SUCCESS: OK_COLOR,
                     tasks_mod.DOWNLOADING: "#8a6d00", tasks_mod.PACKAGED: "#666666",
                     tasks_mod.FAILED: ERR_COLOR, tasks_mod.EXPIRED: ERR_COLOR,
                     tasks_mod.LOST: ERR_COLOR}
        for rec in records:
            langs = "、".join(rec.languages)
            err = rec.last_error or ""
            if rec.status == tasks_mod.PACKAGED and rec.language_results:
                miss = sum(len(v.get("missing") or []) for v in rec.language_results.values())
                err = err or (f"缺失 {miss} 条" if miss else "全部成功")
            item = QTreeWidgetItem([
                str(rec.task_id), langs, str(rec.entry_count),
                STATUS_LABELS.get(rec.status, rec.status),
                self._ago(rec.created_at),
                str(rec.result_file_id or (rec.upload_file_id or "")),
                err[:80]])
            color = color_map.get(rec.status)
            if color:
                item.setForeground(3, QColor(color))
            if str(rec.task_id) in selected_ids:
                item.setSelected(True)
            self.tasks_tree.addTopLevelItem(item)
        self._status(self.tasks_status, f"共 {len(records)} 条任务", "info")
        for rec in records:
            if expiry_risk(rec) and rec.task_id not in self._expiry_warned:
                self._expiry_warned.add(rec.task_id)
                self._log(f"⚠ 任务 {rec.task_id} 已提交超 8 小时仍未下载，"
                          "结果文件约 9 小时过期，请尽快「下载并打包」")
                self.statusBar().showMessage(
                    f"任务 {rec.task_id} 结果文件临近过期，请尽快下载", 8000)

    def _selected_task_ids(self) -> List[str]:
        return [str(item.text(0)) for item in self.tasks_tree.selectedItems()]

    def _tasks_refresh(self) -> None:
        ids = self._selected_task_ids()
        self._status(self.tasks_status, "刷新中…", "busy")
        self.task_ctl.request_refresh(ids or None)

    def _tasks_finalize(self) -> None:
        ids = self._selected_task_ids()
        if not ids:
            self.statusBar().showMessage("请先选择「待下载」状态的任务", 5000)
            return
        self._status(self.tasks_status, "下载打包中…", "busy")
        for tid in ids:
            self.task_ctl.request_finalize(tid)

    def _tasks_retry(self) -> None:
        ids = self._selected_task_ids()
        if not ids:
            self.statusBar().showMessage("请先选择失败/过期/丢失的任务", 5000)
            return
        self._status(self.tasks_status, "重新提交中…", "busy")
        for tid in ids:
            self.task_ctl.request_retry(tid)

    def _tasks_delete(self) -> None:
        ids = self._selected_task_ids()
        if not ids:
            self.statusBar().showMessage("请先选择要删除的任务记录", 5000)
            return
        ret = QMessageBox.question(
            self, "确认", f"删除 {len(ids)} 条任务记录？（不影响已生成的文件）")
        if ret != QMessageBox.Yes:
            return
        for tid in ids:
            self.task_ctl.request_delete(tid)

    def _on_auto_refresh_toggled(self, on: bool) -> None:
        if on:
            self._auto_timer.start()
            self._log("任务中心自动刷新已开启（每 60 秒）")
            self.task_ctl.request_refresh(None)
        else:
            self._auto_timer.stop()

    def _auto_refresh_tick(self) -> None:
        self.task_ctl.request_refresh(None)

    def _on_task_msg(self, kind: str, payload: object) -> None:
        if kind == "tasks_changed":
            self._reload_tasks()
        elif kind == "task_op":
            ok, message = payload  # type: ignore[misc]
            if ok:
                self._status(self.tasks_status, str(message), "ok")
                self.statusBar().showMessage(str(message), 5000)
            else:
                self._status(self.tasks_status, "操作失败", "err")
                QMessageBox.critical(self, "任务中心操作失败", str(message))
            self._log(f"[任务中心] {message}")
        elif kind == "event":
            ev = payload
            label = STAGE_LABELS.get(getattr(ev, "stage", ""), getattr(ev, "stage", ""))
            self._status(self.tasks_status, f"{label}：{getattr(ev, 'message', '')}", "busy")


def _preflight_linux_display() -> Optional[str]:
    """Linux 启动前检查显示环境与 Qt xcb 插件依赖，返回 None 或用户可读的错误说明。

    Qt 6.5+ 的 xcb 平台插件运行时 dlopen libxcb-cursor；缺失时 Qt 直接 abort，
    报错信息对普通用户不友好——这里提前拦截，给出可操作的修复命令。
    """
    if not sys.platform.startswith("linux"):
        return None
    if os.environ.get("QT_QPA_PLATFORM"):
        return None  # 用户显式指定平台（offscreen/wayland…），不干预
    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        return None  # 纯 Wayland 会话不加载 xcb 插件
    if not os.environ.get("DISPLAY"):
        return ("未检测到图形显示环境（DISPLAY 未设置）。\n"
                "请在图形桌面中启动；SSH/无头环境仅可用 QT_QPA_PLATFORM=offscreen 运行自检脚本。")
    import ctypes
    import ctypes.util
    try:
        ctypes.CDLL("libxcb-cursor.so.0")  # 走 LD_LIBRARY_PATH + ldconfig 缓存
        return None
    except OSError:
        if ctypes.util.find_library("xcb-cursor") is not None:
            return None
    return ("缺少系统库 libxcb-cursor0（Qt 6.5+ 的 xcb 平台插件依赖，缺失会导致启动即崩溃）。\n"
            "安装后重试：\n"
            "  Ubuntu/Debian: sudo apt install libxcb-cursor0\n"
            "  Fedora:        sudo dnf install xcb-util-cursor\n"
            "  Arch:          sudo pacman -S libxcb-cursor")


def main() -> None:
    problem = _preflight_linux_display()
    if problem:
        # 此时 QApplication 都可能起不来，弹窗不可靠，直接给终端可读信息
        sys.stderr.write(f"无法启动图形界面：\n{problem}\n")
        sys.exit(2)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    w = App()
    w.show()
    sys.exit(app.exec())
