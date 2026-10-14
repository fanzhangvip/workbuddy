"""黑色钻石玻璃主题（`src/ui/theme.py`）与"防挤占"布局回归。

这个文件盯两件事：

1. **主题真的装上了**——`QApplication` 拿到深色调色板与全局 QSS，
   自绘控件从调色板取到的颜色与 QSS 里写的一致（两边不同源就会
   出现"样式表说浅字、调色板给深字"的割裂）；
2. **窄窗口不挤占**——主窗口与控制条在收窄时按设计收起按钮，
   地址栏/进度条拿到的是让出后的完整宽度，而不是被压到不可用。

全部离屏渲染（`QT_QPA_PLATFORM=offscreen`），布局计算是真实的，
只有字形渲染与真实运行不同。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    from src.ui.theme import apply_theme

    apply_theme(app)
    return app


class ThemeTest(unittest.TestCase):
    """主题装上之后，调色板与样式表必须是同一套颜色。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _app()

    def test_palette_is_dark(self) -> None:
        """窗口底色必须是深色——浅色底上玻璃面板会直接看穿。"""
        pal = self.app.palette()
        window = pal.color(QPalette.ColorRole.Window)
        self.assertLess(window.lightness(), 40, f"Window 底色过亮: {window.name()}")

    def test_highlight_matches_accent(self) -> None:
        """选中态颜色要与主题强调色一致（自绘 delegate 靠它取色）。"""
        from src.ui.theme import TOKENS

        pal = self.app.palette()
        highlight = pal.color(QPalette.ColorRole.Highlight).name().lower()
        self.assertEqual(
            highlight,
            TOKENS["accent"].lower(),
            "调色板 Highlight 与主题强调色不一致，列表选中态会跟样式表对不上",
        )

    def test_stylesheet_installed(self) -> None:
        """全局 QSS 必须真的挂上（空样式表 = 回到系统浅色外观）。"""
        self.assertTrue(self.app.styleSheet().strip(), "QApplication 上没有样式表")

    def test_ink_unknown_name_falls_back(self) -> None:
        """`ink()` 拼错名字时返回兜底色而不是抛错——自绘代码一年改一次，
        为一个拼写错误让整个界面画不出来不值得。"""
        from src.ui.theme import ink

        self.assertTrue(ink("not_a_color").isValid())
        self.assertTrue(ink("not_a_color", "#123456").isValid())

    def test_ink_parses_alpha_colors(self) -> None:
        """`#AARRGGBB` 形态的玻璃色要能被 QColor 解析出来。"""
        from src.ui.theme import ink

        glass = ink("glass")
        self.assertTrue(glass.isValid(), "glass 色解析失败")
        self.assertLess(glass.alpha(), 255, "glass 应该是半透明的")

    def test_apply_theme_is_idempotent(self) -> None:
        """重复调用不能出错（MainWindow 与 main.py 都会调）。"""
        from src.ui.theme import apply_theme

        self.assertTrue(apply_theme(self.app))


class MainWindowCompactTest(unittest.TestCase):
    """窄窗口时按钮按优先级收起，地址栏不被压扁。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _app()

    def setUp(self) -> None:
        from src.ui.main_window import MainWindow

        self.window = MainWindow()
        self.addCleanup(self.window.close)
        self.addCleanup(self.window.deleteLater)

    def _show_at(self, width: int, height: int = 640) -> None:
        self.window.resize(width, height)
        self.window.show()
        self.app.processEvents()

    def test_wide_window_shows_all_buttons(self) -> None:
        self._show_at(1280)
        texts = [b.text() for b in self.window._nav_buttons if b.isVisible()]  # noqa: SLF001
        self.assertEqual(len(texts), 6, f"宽窗口应收起 0 个按钮，实际可见 {texts}")

    def test_narrow_window_hides_secondary_buttons(self) -> None:
        self._show_at(760)
        visible = [
            b.text() for b in self.window._nav_buttons if b.isVisible()  # noqa: SLF001
        ]
        self.assertEqual(
            visible,
            ["播放", "粘贴", "打开文件"],
            f"最窄档应只剩 3 个按钮，实际 {visible}",
        )
        # 配置类按钮也要收起
        self.assertFalse(self.window.btn_settings.isVisible())
        self.assertFalse(self.window.btn_panel.isVisible())

    def test_play_button_never_hides(self) -> None:
        """「播放」是唯一不能少的功能——任何宽度下都必须可见。"""
        for width in (1280, 1024, 880, 760, 720):
            self._show_at(width)
            self.assertTrue(
                self.window.btn_play_url.isVisible(),
                f"宽度 {width} 时「播放」按钮不可见",
            )

    def test_address_bar_keeps_usable_width(self) -> None:
        """地址栏在窄窗口下仍要能用——这是这次改造的核心指标。

        改造前 760px 窗口里地址栏只剩 168px（8 个按钮把宽度吃光了）。
        """
        self._show_at(760)
        self.assertGreaterEqual(
            self.window.address.width(), 400,
            f"760px 窗口下地址栏只有 {self.window.address.width()}px，仍被挤占",
        )

    def test_fullscreen_hides_and_restores_top_area(self) -> None:
        """进全屏时顶部工具区收起，退出后恢复。"""
        self._show_at(1180)
        self.window.showFullScreen()
        self.app.processEvents()
        self.assertFalse(self.window.address.isVisible(), "全屏时地址栏应隐藏")

        self.window.showNormal()
        self.app.processEvents()
        self.assertTrue(self.window.address.isVisible(), "退出全屏后地址栏应恢复")


class ControlBarCompactTest(unittest.TestCase):
    """控制条两行布局：进度条独占一行且不会被压扁。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _app()

    def setUp(self) -> None:
        from src.ui.control_bar import ControlBar

        self.bar = ControlBar()
        self.bar.resize(900, 90)
        self.bar.show()
        self.addCleanup(self.bar.close)
        self.addCleanup(self.bar.deleteLater)
        self.app.processEvents()

    def test_slider_gets_the_wide_row(self) -> None:
        """进度条要拿到整行里最大的一份宽度。"""
        slider_w = self.bar.slider.width()
        for name in ("btn_prev", "btn_play", "btn_stop", "btn_next"):
            button = getattr(self.bar, name)
            self.assertGreater(
                slider_w, button.width() * 2,
                f"进度条({slider_w}px)应明显宽于按钮 {name}({button.width()}px)",
            )

    def test_narrow_bar_keeps_fullscreen(self) -> None:
        """窄条下依次收起辅助控件，但全屏永远在。"""
        self.bar.resize(600, 90)
        self.app.processEvents()
        self.assertFalse(self.bar.combo_rate.isVisible(), "600px 时倍速应收起")
        self.assertTrue(self.bar.btn_fullscreen.isVisible(), "全屏任何宽度都要在")

    def test_wide_bar_shows_all(self) -> None:
        self.bar.resize(900, 90)
        self.app.processEvents()
        for name in ("combo_rate", "btn_snapshot", "btn_external", "btn_fullscreen"):
            widget = getattr(self.bar, name)
            self.assertTrue(widget.isVisible(), f"900px 时 {name} 不应被收起")

    def test_time_label_fixed_width(self) -> None:
        """时间标签宽度固定——否则时间跳动时整行控件会横向抖。"""
        self.bar.set_position(65_000)  # 01:05
        self.bar.set_duration(3_600_000)  # 1:00:00
        width_before = self.bar.label_time.width()
        self.bar.set_position(3_540_000)
        self.assertEqual(
            self.bar.label_time.width(), width_before,
            "时间文字变长后标签宽度变了，整行会抖动",
        )


class PlaylistEmptyHintTest(unittest.TestCase):
    """空提示只描述当前标签页，且不再把另一个标签页的视图拽出来。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _app()

    def test_other_tab_view_not_force_shown(self) -> None:
        """播放列表为空时，历史记录的视图不能被强制 setVisible(True)。

        旧实现会把它拽成可见——可它所在标签页没被选中，Qt 不会给它做布局，
        尺寸冻结在初始 640x480，渲染时越界压到别的控件上。
        """
        from src.ui.main_window import MainWindow

        window = MainWindow()
        self.addCleanup(window.close)
        self.addCleanup(window.deleteLater)
        window.show()
        self.app.processEvents()

        panel = window.panel
        if panel.playlist.rowCount() == 0:
            # 播放列表为空（本机真实状态）：历史视图必须保持隐藏
            self.assertFalse(
                panel.view_history.isVisible(),
                "历史记录视图被强制可见，会以冻结尺寸越界渲染",
            )
            self.assertTrue(panel.label_empty.isVisible())
            self.assertIn("列表", panel.label_empty.text())

        # 切到历史页再切回来，提示文字要跟上
        panel.tabs.setCurrentIndex(1)
        self.app.processEvents()
        text_on_history = panel.label_empty.text()
        panel.tabs.setCurrentIndex(0)
        self.app.processEvents()
        # 两边都为空时文字都含"为空"，但不应崩、不应错位
        self.assertIn("为空", text_on_history)


if __name__ == "__main__":
    unittest.main()
