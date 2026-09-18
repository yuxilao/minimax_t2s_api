#!/usr/bin/env bash
set -euo pipefail
# 构建 Linux 单文件可执行程序，产物: dist/t2s-voice-tool
python3 -m pip install -r requirements.txt pyinstaller
pyinstaller --clean --noconfirm t2s_tool_gui.spec
echo "构建完成: dist/t2s-voice-tool"
