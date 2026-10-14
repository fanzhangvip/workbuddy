"""自绘标题栏 —— 让标题里的官网链接可以真的点击。

Windows 的原生标题栏由系统绘制，上面只能放纯文本，窗口标题里那句
「智泊AI版权所有，仅限智泊AI学员使用，https://www.zhipoai.com/」
中的网址永远点不动。这里把窗口改成无边框 + 应用自绘标题栏：
外观由主题统一，网址是真正的超链接，点一下就用系统浏览器打开官网。

原生窗口行为不丢：拖动、贴边分屏（Aero Snap）、双击最大化、
右键系统菜单、边缘缩放，全部通过 WM_NCHITTEST 交还给系统处理
（见 main_window.nativeEvent）——这是 Windows 上自绘标题栏的标准做法，
不是自己模拟拖动。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from src.ui import branding
from src.ui.theme import TOKENS

# --- WM_NCHITTEST 的返回值（winuser.h）---
HTCLIENT = 1
HTCAPTION = 2
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

#: 窗口边缘的缩放热区宽度（逻辑像素）
RESIZE_MARGIN = 8

#: Windows 消息结构体。ctypes.wintypes 只能在 Windows 上导入，
#: 其他平台占位为 None（nativeEvent 侧会跳过）。
if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as _wt

    _MSG = _wt.MSG

    class _DwmMargins(ctypes.Structure):
        _fields_ = [
            ("cxLeftWidth", ctypes.c_int),
            ("cxRightWidth", ctypes.c_int),
            ("cyTopHeight", ctypes.c_int),
            ("cyBottomHeight", ctypes.c_int),
        ]

else:  # 非 Windows（含测试用的 offscreen）：不参与原生命中判定
    ctypes = None
    _MSG = None


def compute_hit_test(
    pos: QPoint,
    width: int,
    height: int,
    title_height: int,
    interactive_rects: tuple = (),
    *,
    maximized: bool = False,
) -> int:
    """纯几何的标题栏/边缘命中判定，nativeEvent 只负责把坐标换算进来。

    单独拆成普通函数，是因为离屏测试拿不到 Windows 消息，但这套
    「哪里算标题、哪里算缩放边、哪里让给控件」的逻辑才是核心，
    必须能直接单测。

    - 四角与四边（8px 热区）→ HT*缩放边：光标与缩放行为由系统给；
    - 标题栏区域 → HTCAPTION：拖动/贴边分屏/双击最大化/右键系统菜单
      全部由系统原生接管；
    - 标题栏里的链接和窗口按钮是真正要处理点击的控件，它们的矩形
      返回 HTCLIENT，让点击正常落到控件上。
    """
    x, y = pos.x(), pos.y()
    m = RESIZE_MARGIN

    # 1) 缩放边框。最大化/全屏时窗口不可缩放，边框码会让光标错误地
    #    变成缩放样式，必须整体跳过。
    if not maximized:
        if y <= m:
            if x <= m:
                return HTTOPLEFT
            if x >= width - m:
                return HTTOPRIGHT
        if y >= height - m:
            if x <= m:
                return HTBOTTOMLEFT
            if x >= width - m:
                return HTBOTTOMRIGHT
        if x <= m:
            return HTLEFT
        if x >= width - m:
            return HTRIGHT
        if y <= m:
            return HTTOP
        if y >= height - m:
            return HTBOTTOM

    # 2) 标题栏区域
    if 0 <= y <= title_height:
        for rect in interactive_rects:
            if rect.contains(pos):
                return HTCLIENT
        return HTCAPTION

    return HTCLIENT


def polish_native_window(hwnd: int) -> None:
    """给无边框窗口找回 Win11 的圆角与投影（尽力而为，失败静默跳过）。

    无边框窗口默认是直角、无影子的，贴着"黑色钻石玻璃"的主题会很生硬。
    DWM 的圆角偏好 + 1px 边框延伸（借回投影）能把这层系统质感补回来。
    """
    if sys.platform != "win32" or ctypes is None:
        return
    try:
        # DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2
        preference = ctypes.c_int(2)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(preference), ctypes.sizeof(preference)
        )
    except Exception:
        pass
    try:
        margins = _DwmMargins(1, 1, 1, 1)
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
    except Exception:
        pass


class TitleBar(QWidget):
    """应用自绘的标题栏：版权句 + 可点官网 + 窗口控制按钮。"""

    minimize_requested = Signal()
    toggle_maximize_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("titleBar")
        self.setFixedHeight(36)

        # 版权句是纯展示文字，不消费鼠标事件——点它等于点标题栏空白处，
        # 即拖动窗口（Windows 上由 HTCAPTION 接管，这里只影响其他平台）。
        self.label_text = QLabel(branding.COPYRIGHT + "，")
        self.label_text.setObjectName("titleLabel")
        self.label_text.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )

        # 官网是真正的超链接：这是本次改造的核心诉求
        self.label_link = QLabel(
            f'<a href="{branding.WEBSITE}" '
            f'style="color:{TOKENS["accent"]}; text-decoration:none">'
            f"{branding.WEBSITE}</a>"
        )
        self.label_link.setObjectName("titleLink")
        self.label_link.setTextFormat(Qt.TextFormat.RichText)
        # 点了交给系统浏览器打开，不用自己接 clicked
        self.label_link.setOpenExternalLinks(True)
        self.label_link.setToolTip(f"打开官网：{branding.WEBSITE}")

        self.btn_min = self._glyph_button("─", "最小化")
        self.btn_max = self._glyph_button("□", "最大化")
        self.btn_close = self._glyph_button("×", "关闭")
        self.btn_close.setObjectName("titleClose")

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 0, 6, 0)
        row.setSpacing(4)
        row.addWidget(self.label_text)
        row.addWidget(self.label_link)
        row.addStretch(1)
        row.addWidget(self.btn_min)
        row.addWidget(self.btn_max)
        row.addWidget(self.btn_close)

        self.btn_min.clicked.connect(self.minimize_requested)
        self.btn_max.clicked.connect(self.toggle_maximize_requested)
        self.btn_close.clicked.connect(self.close_requested)

    # ------------------------------------------------------------ 对外
    def interactive_rects(self) -> tuple:
        """标题栏内自己处理点击（不交给系统当标题栏）的矩形，窗口坐标。"""
        return (
            self.label_link.geometry(),
            self.btn_min.geometry(),
            self.btn_max.geometry(),
            self.btn_close.geometry(),
        )

    def update_max_button(self, maximized: bool) -> None:
        """按窗口状态切换最大化/还原图标。"""
        self.btn_max.setText("▣" if maximized else "□")
        self.btn_max.setToolTip("还原" if maximized else "最大化")

    # ------------------------------------------------------------ 内部
    def _glyph_button(self, glyph: str, tip: str) -> QPushButton:
        button = QPushButton(glyph)
        button.setFixedSize(44, 28)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setToolTip(tip)
        return button

    def mousePressEvent(self, event) -> bool:  # noqa: N802 - Qt 命名
        # Windows 上点标题栏走 WM_NCHITTEST(HTCAPTION)，到不了这里；
        # 这是给离屏环境/其他平台兜底的拖动入口。
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return True
        return super().mousePressEvent(event)
