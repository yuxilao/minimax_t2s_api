@echo off
chcp 65001 >nul
rem 构建 Windows 单文件免安装 exe，产物: dist\t2s-voice-tool.exe
python -m pip install -r requirements.txt pyinstaller
pyinstaller --clean --noconfirm t2s_tool_gui.spec
echo.
echo 构建完成: dist\t2s-voice-tool.exe
pause
