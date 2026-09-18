# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：Windows/Linux 通用，产出单文件免安装可执行程序。

block_cipher = None

a = Analysis(
    ['run_gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['openpyxl', 'openpyxl.cell._writer'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2'],  # 只许 PySide6 一种 Qt 绑定进包：本机装过 PyQt5 时构建会被 PyInstaller 中止
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='t2s-voice-tool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
)
