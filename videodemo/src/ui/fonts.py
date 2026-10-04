"""
界面字体保障。

纯中文界面如果运行环境缺少中文字体，所有文字都会渲染成方框（豆腐块）。
这里按"先挑系统已装的中文字体，挑不到就手工加载字体文件"的顺序兜底，
保证文字始终可读。
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

from src.utils.logger import get_logger

log = get_logger(__name__)

# 常见中文字体，按优先级排列（Windows / macOS / Linux 各覆盖一些）
_PREFERRED_FAMILIES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "微软雅黑",
    "PingFang SC",
    "Hiragino Sans GB",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Micro Hei",
    "SimHei",
    "SimSun",
)

# 字体文件兜底路径（Windows 常见位置）
_FONT_FILE_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)

_APPLIED = False


def ensure_app_font(base_size: int = 10) -> str:
    """确保应用有一个能显示中文的字体。返回实际使用的字体名（失败返回空串）。

    幂等：重复调用只生效一次。
    """
    global _APPLIED
    if _APPLIED:
        return ""

    _APPLIED = True
    available = set(QFontDatabase.families())

    # 1) 系统已装的中文字体
    for family in _PREFERRED_FAMILIES:
        if family in available:
            _set_application_font(QFont(family, base_size))
            log.info("界面字体: %s（系统已装）", family)
            return family

    # 2) 手工加载字体文件
    for path in _FONT_FILE_CANDIDATES:
        if not os.path.isfile(path):
            continue
        font_id = QFontDatabase.addApplicationFont(path)
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        if families:
            family = families[0]
            _set_application_font(QFont(family, base_size))
            log.info("界面字体: %s（从 %s 加载）", family, Path(path).name)
            return family

    log.warning("未找到可用的中文字体，界面文字可能显示为方框")
    return ""


def _set_application_font(font: QFont) -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        app.setFont(font)
