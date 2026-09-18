#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 全流程真实 API 自动化测试（PySide6 offscreen，驱动真实 App 控件，不绕过任何视图逻辑）。

流程：batch 提交即返 → 任务中心刷新到「待下载」→ 下载打包落盘 → async 模式取消链路。
用法: python3 scripts/gui_e2e_check.py   （真实接口，手动运行，不在 pytest 内）
消耗极少量 MiniMax 配额（2 语言 × 3 条短文本 = 6 条 + 取消阶段的少量提交）。

Qt 版驱动方式：
- QT_QPA_PLATFORM=offscreen + QApplication([])：无显示服务器（CI / SSH 会话）也可运行；
- 任务记录重定向：在 App() 创建前 monkeypatch app_mod.paths.default_tasks_path
  （TaskController 在 __init__ 里取值）；配置重定向：创建后 w.ctl.config_path = 副本
  再调 w._initial_load_config()（新 UI 没有「加载配置」按钮，启动自动加载）；
- 弹窗替换：QMessageBox.information/critical/warning/question 换成记录函数，模态框不阻塞；
- 泵事件循环：app.processEvents() 派发后台线程经信号桥（排队连接）发回主线程的回调。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# 无显示环境守卫：必须在 import PySide6 之前设置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 环境守卫：CA bundle 环境变量指向不存在文件时（如沙箱 /tmp 隔离）回退 certifi 默认
for _var in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
    _p = os.environ.get(_var)
    if _p and not os.path.exists(_p):
        os.environ.pop(_var)

WORK = "/tmp/gui_e2e"
XLSX = os.path.join(WORK, "list.xlsx")
OUT = os.path.join(WORK, "out")
CFG_COPY = os.path.join(WORK, "config.json")
TASKS = os.path.join(WORK, "tasks.json")

checks = []


def check(name, ok, detail=""):
    checks.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


# ---------- 1. 造小表（3 条短文本 × 2 语言列） ----------
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK)
import openpyxl

wb = openpyxl.Workbook()
ws = wb.active
ws.append(("AP音效名称", "场景详细描述", "中文语音内容", "英文"))
ws.append(("Q001", "开机", "设备已启动", "Device started"))
ws.append(("Q003", "联网", "联网成功", "Network connected"))
ws.append(("Q004", "开始清扫", "开始清扫", "Cleaning started"))
wb.save(XLSX)
check("构造测试语音目录", os.path.exists(XLSX), XLSX)

# ---------- 2. 复制配置并重定向，避免写坏用户 config.json / tasks.json ----------
shutil.copy(os.path.join(ROOT, "config.json"), CFG_COPY)

from PySide6.QtWidgets import QApplication

app = QApplication([])

from PySide6.QtCore import Qt
from t2s_tool.gui import app as app_mod

# 任务记录重定向：TaskController 在 App.__init__ 里取 paths.default_tasks_path()，
# 必须在创建窗口前打补丁
app_mod.paths.default_tasks_path = lambda: TASKS

# 弹窗改为记录（自动化无法点模态框）；question 模拟用户点了「是」
dialogs = []


def _record_box(kind):
    def _f(*a, **k):
        dialogs.append((kind, a[-2] if len(a) >= 3 else "", a[-1]))
    return _f


app_mod.QMessageBox.information = staticmethod(_record_box("info"))
app_mod.QMessageBox.critical = staticmethod(_record_box("critical"))
app_mod.QMessageBox.warning = staticmethod(_record_box("warn"))
app_mod.QMessageBox.question = staticmethod(lambda *a, **k: app_mod.QMessageBox.Yes)


def pump(deadline, cond):
    """泵 Qt 事件循环直到 cond() 或超时，返回是否满足（后台线程回调靠 processEvents 派发）。"""
    while time.time() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.1)
    return False


def task_rows():
    """任务中心表格全部行（每行的文本列列表）。"""
    return [[w.tasks_tree.topLevelItem(i).text(c)
             for c in range(w.tasks_tree.columnCount())]
            for i in range(w.tasks_tree.topLevelItemCount())]


t_start0 = time.time()
w = app_mod.App()
# 配置重定向：App 启动时已自动加载项目根 config.json（只读）；切到临时副本后重新执行
# 启动加载（新 UI 无「加载配置」按钮，_initial_load_config 即等价入口）
w.ctl.config_path = CFG_COPY
w._initial_load_config()

# ---------- 3. 加载配置（启动自动加载）+ 保存配置（走「⚙ 设置」对话框，校验写回） ----------
w.provider_combo.setCurrentText(w._provider_display.get("minimax", "MiniMax"))
key = str(w._block.get("api_key") or "")
check("启动自动加载配置(API Key 已填入)", len(key) > 20, f"key长度={len(key)}")

# 保存配置的方式：构造真实 SettingsDialog（不 exec 模态框），保持当前值触发「保存」
# （_on_accept 产出 result_block），再按 _open_settings 的落盘路径 ctl.save_config 写回
# 临时副本——比直接调 ctl.save_config(config) 多覆盖一层「对话框取值」逻辑，更接近真实交互。
dlg = app_mod.SettingsDialog(w, "MiniMax", w._block)
dlg._on_accept()
check("设置对话框保存产出配置块",
      dlg.result_block is not None and dlg.result_block.get("api_key") == key)
w.ctl.save_config("minimax", dlg.result_block)
w._cfg.setdefault("providers", {})["minimax"] = dlg.result_block
w._initial_load_config()  # 从临时副本回读（等价旧版「加载配置」按钮）

from t2s_tool.config import load_config, get_provider_config

saved = get_provider_config(load_config(CFG_COPY), "minimax")
check("保存配置写入临时副本", saved.get("api_key") == key and saved.get("tts_mode") == "batch",
      f"tts_mode={saved.get('tts_mode')}")

# ---------- 4. 加载目录、全选两种语言（验证单任务多语言合并） ----------
w.input_edit.setText(XLSX)
w.load_btn.click()
app.processEvents()
langs = [w.lang_list.item(i).data(Qt.UserRole) for i in range(w.lang_list.count())]
check("加载目录填充语言复选列表", langs == ["中文语音内容", "英文"], f"langs={langs}")
check("语言复选列表默认全选", all(w.lang_list.item(i).checkState() == Qt.Checked
                                for i in range(w.lang_list.count())))
w._set_all_languages(Qt.Checked)  # 显式全选（与点「全选」按钮同一路径）
w.output_edit.setText(OUT)

# ---------- 5. 开始转换（batch 提交即返） ----------
w.tabs.setCurrentIndex(0)
w.start_btn.click()
ok_submit = pump(time.time() + 120,
                 lambda: any(d[0] == "info" and d[1] == "任务已提交" for d in dialogs))
submit_elapsed = time.time() - t_start0
check("提交即返弹窗出现", ok_submit, f"耗时 {submit_elapsed:.0f}s")
check("提交后按钮状态恢复", w.start_btn.isEnabled() and not w.cancel_btn.isEnabled())

# ---------- 6. 任务中心出现记录（自动切页签） ----------
app.processEvents()
check("自动切换到任务中心页签", w.tabs.currentIndex() == 2)
rows_t = task_rows()
check("任务列表出现 1 条记录",
      len(rows_t) == 1 and rows_t[0][2] == "6" and "中文语音内容" in rows_t[0][1]
      and "英文" in rows_t[0][1],
      f"rows={rows_t}")
check("初始状态为处理中", bool(rows_t) and rows_t[0][3] == "处理中", str(rows_t[:1]))
check("提交阶段未落盘语言目录", not os.path.exists(os.path.join(OUT, "中文语音内容"))
      and not os.path.exists(os.path.join(OUT, "英文")))
_sz = os.path.getsize(TASKS) if os.path.exists(TASKS) else -1
check("任务记录已持久化", os.path.exists(TASKS), f"store={_sz}B")
log_text = w.log_view.toPlainText()
check("日志含解析/打包/创建/提交阶段",
      all(k in log_text for k in ("解析目录", "打包文本", "创建/合成", "任务已提交")))

# ---------- 7. 刷新全部 → 等待服务端完成（处理中 → 待下载；每 10 秒重刷模拟自动刷新） ----------
def refresh_until_ready(deadline):
    last_click = 0.0
    while time.time() < deadline:
        app.processEvents()
        rows_now = task_rows()
        if rows_now and rows_now[0][3] == "待下载":
            return True
        if time.time() - last_click >= 10:
            w.tasks_tree.clearSelection()  # 无选中 = 刷新全部非终态记录
            w.refresh_btn.click()
            last_click = time.time()
        time.sleep(0.2)
    return False


ok_success = refresh_until_ready(time.time() + 600)
check("刷新后状态变为待下载", ok_success,
      f"rows={task_rows()[:1]} 耗时{time.time() - t_start0:.0f}s")
if not ok_success:
    w.close()
    print("\n===== 汇总 =====")
    failed = [c for c in checks if not c[1]]
    print(f"{'全部通过' if not failed else '存在失败'}: {len(checks) - len(failed)}/{len(checks)}")
    sys.exit(1)

# ---------- 8. 下载并打包（选中 → 一键落盘） ----------
w.tasks_tree.topLevelItem(0).setSelected(True)
w.finalize_btn.click()
ok_packaged = pump(time.time() + 180,
                   lambda: bool(task_rows()) and task_rows()[0][3] == "已完成")
check("下载打包后状态为已完成", ok_packaged, f"rows={task_rows()[:1]}")

# ---------- 9. 产物校验（两种语言各 3 个 mp3 + 扁平 tar） ----------
all_ok, detail = True, []
for lang in ("中文语音内容", "英文"):
    lang_dir = os.path.join(OUT, lang)
    mp3s = sorted(os.listdir(lang_dir)) if os.path.isdir(lang_dir) else []
    expect = ["Q001.mp3", "Q003.mp3", "Q004.mp3"]
    if mp3s != expect:
        all_ok = False
        detail.append(f"{lang}:目录={mp3s}")
        continue
    for f in mp3s:
        data = open(os.path.join(lang_dir, f), "rb").read()
        magic = data[:3] == b"ID3" or (len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)
        if not (magic and len(data) > 1000):
            all_ok = False
            detail.append(f"{lang}/{f}={len(data)}B魔数={magic}")
    tar_members = sorted(tarfile.open(os.path.join(OUT, f"{lang}.tar")).getnames()) \
        if os.path.exists(os.path.join(OUT, f"{lang}.tar")) else []
    if tar_members != expect:
        all_ok = False
        detail.append(f"{lang}:tar={tar_members}")
check("两语言 mp3/tar 产物完整有效", all_ok, " ".join(detail))

store_data = json.load(open(TASKS, encoding="utf-8"))
rec = store_data["tasks"][0]
lr = rec.get("language_results") or {}
res_ok = (rec["status"] == "packaged" and set(lr) == {"中文语音内容", "英文"}
          and all(len(v["success"]) == 3 and not v["missing"] for v in lr.values()))
check("任务记录 language_results 完整", res_ok, json.dumps(lr, ensure_ascii=False)[:200])

# ---------- 10. 取消链路（async 逐条模式：有可取消窗口） ----------
cfg = json.load(open(CFG_COPY, encoding="utf-8"))
cfg["providers"]["minimax"]["tts_mode"] = "async"
json.dump(cfg, open(CFG_COPY, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
dialogs.clear()
w._initial_load_config()  # 重新加载 async 配置（新 UI 无「加载配置」按钮）
w.tabs.setCurrentIndex(0)
w._set_all_languages(Qt.Checked)
base_create = w.log_view.toPlainText().count("创建/合成：")
base_poll = w.log_view.toPlainText().count("转换中：")
w.start_btn.click()
pump(time.time() + 10,
     lambda: (w.log_view.toPlainText().count("创建/合成：") > base_create
              or w.log_view.toPlainText().count("转换中：") > base_poll))
w.cancel_btn.click()
ok_cancel = pump(time.time() + 90,
                 lambda: any(d[0] == "critical" and "取消" in d[2] for d in dialogs))
check("取消链路生效", ok_cancel, str([d for d in dialogs if d[0] == "critical"])[:150])

w.close()
print("\n===== 汇总 =====")
failed = [c for c in checks if not c[1]]
print(f"{'全部通过' if not failed else '存在失败'}: {len(checks) - len(failed)}/{len(checks)}")
sys.exit(1 if failed else 0)
