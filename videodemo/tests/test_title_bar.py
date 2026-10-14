"""自绘标题栏（`src/ui/title_bar.py`）回归。

核心诉求：窗口标题里那句「…，https://www.zhipoai.com/」中的网址
必须能点开官网。原生标题栏只能放纯文本，所以改成了无边框窗口 +
自绘标题栏；这个文件盯三件事：

1. **命中判定**（compute_hit_test）——标题栏区域交给系统当标题
   （拖动/贴边分屏/双击最大化都靠它），边缘 8px 是缩放热区，
   链接和窗口按钮的矩形要让回给控件，否则点了只会拖窗口；
2. **标题栏控件本身**——链接真的带官网地址、开外部链接，
   最大化按钮随窗口状态换图形；
3. **与主窗口的协作**——全屏收起标题栏、退出恢复；
   窗口标题（任务栏/Alt+Tab 用）保持完整版权句。

命中判定是纯几何函数，可以脱离 Windows 消息直接单测；
控件部分全部离屏渲染（`QT_QPA_PLATFORM=offscreen`）。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.ui import branding  # noqa: E402
from src.ui.title_bar import (  # noqa: E402
    HTBOTTOM,
    HTBOTTOMLEFT,
    HTBOTTOMRIGHT,
    HTCAPTION,
    HTCLIENT,
    HTLEFT,
    HTRIGHT,
    HTTOP,
    HTTOPLEFT,
    HTTOPRIGHT,
    TitleBar,
    compute_hit_test,
)


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    from src.ui.theme import apply_theme

    apply_theme(app)
    return app


def _hit(pos, *, title_height=36, maximized=False, interactive=()) -> int:
    return compute_hit_test(
        pos, 1180, 700, title_height, interactive, maximized=maximized
    )


class ComputeHitTestTest(unittest.TestCase):
    """命中判定：哪里算标题、哪里算缩放边、哪里让给控件。"""

    def test_title_area_is_caption(self) -> None:
        # 标题栏中部（y < 36）→ 拖动交给系统
        self.assertEqual(_hit(QPoint(400, 18)), HTCAPTION)
        self.assertEqual(_hit(QPoint(400, 36)), HTCAPTION)

    def test_below_title_bar_is_client(self) -> None:
        self.assertEqual(_hit(QPoint(400, 200)), HTCLIENT)

    def test_edges_are_resize_borders(self) -> None:
        self.assertEqual(_hit(QPoint(2, 300)), HTLEFT)
        self.assertEqual(_hit(QPoint(1178, 300)), HTRIGHT)
        self.assertEqual(_hit(QPoint(400, 1)), HTTOP)
        self.assertEqual(_hit(QPoint(400, 698)), HTBOTTOM)

    def test_corners_take_priority(self) -> None:
        self.assertEqual(_hit(QPoint(2, 2)), HTTOPLEFT)
        self.assertEqual(_hit(QPoint(1178, 2)), HTTOPRIGHT)
        self.assertEqual(_hit(QPoint(2, 698)), HTBOTTOMLEFT)
        self.assertEqual(_hit(QPoint(1178, 698)), HTBOTTOMRIGHT)

    def test_interactive_rects_stay_client(self) -> None:
        # 链接和按钮所在的矩形必须返回 HTCLIENT，
        # 否则点击会被系统当成"拖标题栏"吞掉，链接就点不动了
        link = QRect(230, 4, 160, 28)
        self.assertEqual(
            _hit(QPoint(300, 18), interactive=(link,)), HTCLIENT
        )
        # 同一行、矩形外仍是标题栏
        self.assertEqual(_hit(QPoint(100, 18), interactive=(link,)), HTCAPTION)

    def test_maximized_has_no_resize_borders(self) -> None:
        # 最大化后边缘不该出现缩放光标；顶部热区落回标题栏拖动
        self.assertEqual(_hit(QPoint(2, 2), maximized=True), HTCAPTION)
        self.assertEqual(_hit(QPoint(1178, 698), maximized=True), HTCLIENT)
        self.assertEqual(_hit(QPoint(2, 300), maximized=True), HTCLIENT)

    def test_hidden_title_bar_has_no_caption(self) -> None:
        # 全屏时标题栏被收起（title_height=0），整屏都是客户区
        self.assertEqual(_hit(QPoint(400, 18), title_height=0), HTCLIENT)


class TitleBarWidgetTest(unittest.TestCase):
    """标题栏控件本身。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _app()

    def setUp(self) -> None:
        self.bar = TitleBar()

    def test_link_points_to_website_and_opens_externally(self) -> None:
        # 这是本次改造的核心：标题里的网址是真正的超链接
        self.assertIn(branding.WEBSITE, self.bar.label_link.text())
        self.assertTrue(self.bar.label_link.openExternalLinks())
        self.assertEqual(
            self.bar.label_link.textFormat().name, "RichText"
        )

    def test_shows_copyright_text(self) -> None:
        self.assertIn(branding.COPYRIGHT, self.bar.label_text.text())

    def test_max_button_toggles_glyph(self) -> None:
        self.bar.update_max_button(False)
        self.assertEqual(self.bar.btn_max.text(), "□")
        self.bar.update_max_button(True)
        self.assertEqual(self.bar.btn_max.text(), "▣")

    def test_interactive_rects_cover_link_and_buttons(self) -> None:
        rects = self.bar.interactive_rects()
        self.assertEqual(len(rects), 4)
        for rect in rects:
            self.assertTrue(rect.isValid() and not rect.isEmpty())

    def test_fixed_height(self) -> None:
        self.assertEqual(self.bar.height(), 36)


class MainWindowTitleBarTest(unittest.TestCase):
    """标题栏与主窗口的协作。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _app()

    def setUp(self) -> None:
        from src.ui.main_window import MainWindow

        self.window = MainWindow()
        self.window.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.close()

    def test_window_is_frameless_with_title_bar(self) -> None:
        self.assertTrue(
            bool(self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        )
        self.assertTrue(self.window.title_bar.isVisible())

    def test_native_title_string_keeps_full_sentence(self) -> None:
        # 任务栏 / Alt+Tab 用的标题仍是完整版权句
        self.assertEqual(self.window.windowTitle(), branding.WINDOW_TITLE)

    def test_fullscreen_hides_title_bar_and_restores(self) -> None:
        self.window.showFullScreen()
        self.app.processEvents()
        self.assertFalse(self.window.title_bar.isVisible())
        self.window.showNormal()
        self.app.processEvents()
        self.assertTrue(self.window.title_bar.isVisible())

    def test_maximize_button_follows_window_state(self) -> None:
        self.window.showMaximized()
        self.app.processEvents()
        self.assertEqual(self.window.title_bar.btn_max.text(), "▣")
        self.window.showNormal()
        self.app.processEvents()
        self.assertEqual(self.window.title_bar.btn_max.text(), "□")


if __name__ == "__main__":
    unittest.main()
