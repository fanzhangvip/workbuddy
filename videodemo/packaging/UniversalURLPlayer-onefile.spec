# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：单文件版（onefile）。

产出 `dist/UniversalURLPlayer-单文件.exe`，一个文件拷走就能用。

代价：每次启动都要把自己解压到临时目录，冷启动明显更慢
（本产物约 100 MB 量级，首次启动可能有数秒等待）。
追求启动速度请用目录版。

构建:
    ./.venv/Scripts/python.exe packaging/build.py --onefile
"""

import os
import sys

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

ONEFILE_NAME = f"{APP_NAME}-single"

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
    a.binaries,
    a.datas,
    [],
    name=ONEFILE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path(),
)
