# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：目录版（onedir）。

产出 `dist/UniversalURLPlayer/` 目录，里面有 `UniversalURLPlayer.exe`。
启动快（不需要每次解压），推荐日常使用与分发。

构建:
    ./.venv/Scripts/python.exe packaging/build.py --onedir
"""

import os
import sys

# 让 spec 能 import 同目录的 pack_config
_SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))  # noqa: F821 - SPEC 由 PyInstaller 注入
if _SPEC_DIR not in sys.path:
    sys.path.insert(0, _SPEC_DIR)

from pack_config import (  # noqa: E402
    APP_NAME,
    ENTRY_SCRIPT,
    HIDDENIMPORTS,
    PROJECT_ROOT,
    build_excludes,
    icon_path,
    optional_binaries,
    optional_hiddenimports,
)

a = Analysis(  # noqa: F821
    [str(ENTRY_SCRIPT)],
    pathex=[str(PROJECT_ROOT)],
    binaries=optional_binaries(),
    datas=[],
    # 可选依赖（python-mpv / imageio-ffmpeg）装上了才带上，
    # 没装就不塞进去，免得构建日志里全是"找不到模块"的假警告
    hiddenimports=HIDDENIMPORTS + optional_hiddenimports(),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=build_excludes(),
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 窗口程序，不弹黑色控制台（自检输出用 AttachConsole 接回终端）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path(),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
