"""品牌与版权呈现的回归测试。

版权信息有四处呈现（窗口标题 / 状态栏 / 画面区 / 设置-关于页），
不锁住的话下次改品牌信息又会漏——这是改过一次名之后的教训（2026-10-07）。
"""

from __future__ import annotations

import sys
import unittest

from PySide6.QtCore import QSize
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QApplication, QLabel

from src.ui import branding

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([sys.argv[0]])


class BrandingConstantsTest(unittest.TestCase):
    def test_window_title_is_full_copyright_line(self) -> None:
        """窗口标题 = 「版权 + 许可 + 官网」整句，这是用户指定的呈现形式。"""
        self.assertEqual(
            branding.WINDOW_TITLE,
            "智泊AI版权所有，仅限智泊AI学员使用，https://www.zhipoai.com/",
        )

    def test_notice_line_carries_website(self) -> None:
        self.assertIn(branding.WEBSITE, branding.notice_line())


class MainWindowBrandingTest(unittest.TestCase):
    def setUp(self) -> None:
        from src.ui.main_window import MainWindow

        self.window = MainWindow()

    def tearDown(self) -> None:
        self.window.close()

    def test_title_uses_branding_constant(self) -> None:
        self.assertEqual(self.window.windowTitle(), branding.WINDOW_TITLE)

    def test_status_bar_brand_is_permanent_child(self) -> None:
        """版权挂在状态栏常驻区：showMessage 的临时消息不会把它顶掉。"""
        bar = self.window.statusBar()
        self.assertIn(self.window.brand_label, bar.children())

    def test_video_brand_visibility_follows_playback(self) -> None:
        from PySide6.QtWidgets import QWidget

        video = self.window.video
        video.set_video_widget(QWidget())
        video.show_video()
        self.assertFalse(video._brand.isVisibleTo(video))
        video.show_placeholder()
        self.assertTrue(video._brand.isVisibleTo(video))


class SettingsAboutTabTest(unittest.TestCase):
    def setUp(self) -> None:
        from src.ui.settings_dialog import SettingsDialog

        self.dialog = SettingsDialog()

    def tearDown(self) -> None:
        self.dialog.close()

    def test_about_tab_exists(self) -> None:
        labels = [
            self.dialog.tabs.tabText(i) for i in range(self.dialog.tabs.count())
        ]
        self.assertIn("关于", labels)

    def test_about_page_carries_copyright_and_website(self) -> None:
        area = self.dialog.tabs.widget(self.dialog.tabs.count() - 1)
        joined = "\n".join(
            label.text() for label in area.widget().findChildren(QLabel)
        )
        self.assertIn(branding.COPYRIGHT, joined)
        self.assertIn(branding.WEBSITE, joined)


class PlaylistPanelCompactTest(unittest.TestCase):
    """面板一窄要收起自己的「加入列表」——4 个字压不进 50px（会截断）。

    用 sendEvent 直接派发 QResizeEvent：面板在 splitter 里，手动 resize()
    会被布局立刻纠正回分配宽度，事件与断言之间隔着一次重排，不稳定。
    """

    def setUp(self) -> None:
        from src.ui.main_window import MainWindow

        self.window = MainWindow()

    def tearDown(self) -> None:
        self.window.close()

    @staticmethod
    def _send_resize(panel, width: int) -> None:
        event = QResizeEvent(QSize(width, panel.height()), panel.size())
        QApplication.sendEvent(panel, event)

    def test_narrow_panel_hides_add_button(self) -> None:
        panel = self.window.panel
        self._send_resize(panel, 280)
        self.assertFalse(panel.btn_add.isVisibleTo(panel))

    def test_wide_panel_shows_add_button(self) -> None:
        panel = self.window.panel
        self._send_resize(panel, 500)
        self.assertTrue(panel.btn_add.isVisibleTo(panel))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
