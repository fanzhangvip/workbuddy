"""
界面验证 + 截图。

用法:
    python tools/capture_ui.py [视频地址] [--offscreen]
    python tools/capture_ui.py --docs --offscreen   # 产出 README 用的四张预览图

说明:
- 默认用真实平台插件打开窗口。但在无桌面会话的环境里（如远程/CI），
  真实窗口会被系统直接终止且没有任何日志，此时请加 --offscreen。
- 离屏模式下 Qt 会把整个控件树渲染进像素图，界面布局、控件状态都是真实的；
  只有视频画面本身因为需要原生窗口表面而呈现为黑色，这属预期现象。
- `--docs` 会依次抓「启动态 / 播放态 / 解析中 / 分离流提示」四张图，
  直接写入 `docs/screenshots/`，文件名与 README 的引用一一对应。
  它靠本地测试视频驱动，不联网，因此可重复。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在导入 PySide6 之前决定平台插件
_argv = [a for a in sys.argv[1:] if not a.startswith("--")]
_offscreen = "--offscreen" in sys.argv
if _offscreen:
    import os

    os.environ["QT_QPA_PLATFORM"] = "offscreen"
else:
    import os

    os.environ.pop("QT_QPA_PLATFORM", None)

from src.utils.env_setup import setup_environment  # noqa: E402

setup_environment()

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

# 无桌面环境时错误弹窗会阻塞，这里让它立即返回
QMessageBox.exec = lambda self: 0  # type: ignore[method-assign]

from src.ui.main_window import MainWindow  # noqa: E402

OUT_DIR = ROOT / ".tools" / "captures"

#: README 引用的界面预览图输出到这里（`--docs` 模式）
DOCS_DIR = ROOT / "docs" / "screenshots"

#: 演示用的"站点只提供分离流"结论（照 B 站真机跑出来的形态写）。
#: 画面区只显示 `· ` 开头的行，所以建议都写成这个格式。
#: 另外画面区的文字列宽有限，每行控制在 28 个字以内，免得折行折得难看。
_SEPARATE_DETAIL = """· 点「同时播放画面与声音」音画都有
· 播放器会同时打开两条流并保持同步
· 或点「仅播放画面」只看画面（省流量）"""


def _demo_resolution():
    """构造一份"B 站只有分离流"的解析结果，用于渲出降级提示界面。"""
    from src.core.resolver import ResolveResult
    from src.core.url_router import UrlKind

    page = "https://www.bilibili.com/video/BV1xx411c7mD"
    signed = "https://upos-sz-mirrorcos.bilivideo.com/upgcxcode/x/x/x.m4s?deadline=1&sign=xxx"
    return ResolveResult(
        ok=False,
        page_url=page,
        title="演示视频：只有分离流",
        kind=UrlKind.WEB_PAGE,
        extractor="BiliBili",
        error="该站点只提供「分离的音视频流」，内置内核无法同时播放",
        detail=_SEPARATE_DETAIL,
        note="分离流",
        video_url=signed,
        video_note="1080P · avc1 · 212261 ms",
        video_kind=UrlKind.DIRECT_MEDIA,
        audio_url=signed,
        audio_note="M4A · 132k",
    )


def capture_docs(app: QApplication, window: MainWindow, source: str) -> None:
    """依次渲染并保存 README 里列出的四张界面预览图。"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    page = "https://www.bilibili.com/video/BV1xx411c7mD"

    def shot(name: str, target: MainWindow) -> None:
        pixmap = target.grab()
        if pixmap.isNull():
            print(f"抓图失败: {name}")
            return
        path = DOCS_DIR / f"{name}.png"
        pixmap.save(str(path), "PNG")
        print(f"已保存 {path}  ({pixmap.width()}x{pixmap.height()})")

    def new_window() -> MainWindow:
        win = MainWindow()
        win.resize(1200, 760)
        win.show()
        return win

    def start_playback() -> None:
        window.address.setText(source)
        window._play_from_address()  # noqa: SLF001

    # 解析相关的两张图用一个**全新窗口**来抓：状态栏与进度条才是干净的初始态，
    # 也正是"刚打开程序、粘贴一个网页地址"时的真实样子（复用同一个窗口会带上
    # 上一段的播放进度，截出来容易让人误解）。
    resolve_window: list[MainWindow] = []

    def show_resolving() -> None:
        win = new_window()
        resolve_window.append(win)
        win.address.setText(page)
        # 走真实的解析开始入口，保证文案与真实运行时一致
        win._on_resolve_started(page)  # noqa: SLF001

    def show_separate_streams() -> None:
        win = resolve_window[0]
        ctrl = win.controller
        # 让控制器"确实拿到过一份分离流结论"：这样错误框里才会出现
        # 「同时播放画面与声音」按钮，截出来的就是真实界面。
        # 当前条目也要设成那个页面地址——合流/降级入口只由**当前条目对应**的
        # 解析结果提供（player_controller._current_resolution），否则按钮不出现。
        from src.core.media_item import MediaItem  # noqa: PLC0415

        ctrl._resolution = _demo_resolution()  # noqa: SLF001
        ctrl._current = MediaItem.from_url(page)  # noqa: SLF001
        ctrl.errorRaised.emit(
            "网页地址解析失败",
            f"页面：{page}\n\n{_SEPARATE_DETAIL}",
        )

    def finish() -> None:
        for win in [window, *resolve_window]:
            win.close()
        app.quit()

    QTimer.singleShot(500, lambda: shot("01-idle", window))
    QTimer.singleShot(900, start_playback)
    QTimer.singleShot(3200, lambda: shot("02-playing", window))
    QTimer.singleShot(3600, show_resolving)
    QTimer.singleShot(4100, lambda: shot("03-resolving", resolve_window[0]))
    QTimer.singleShot(4600, show_separate_streams)
    QTimer.singleShot(5100, lambda: shot("04-separate-streams", resolve_window[0]))
    QTimer.singleShot(5600, finish)


def main() -> int:
    parser = argparse.ArgumentParser(description="界面验证与截图")
    parser.add_argument("source", nargs="?", help="要播放的地址")
    parser.add_argument("--offscreen", action="store_true", help="使用离屏渲染")
    parser.add_argument("--docs", action="store_true",
                        help=f"产出 README 用的四张界面预览图到 {DOCS_DIR}")
    args = parser.parse_args()

    source = args.source or str(ROOT / ".tools" / "testmedia" / "local_test.mp4")

    app = QApplication(_argv[:1])

    window = MainWindow()
    window.resize(1200, 760)
    window.show()

    print(f"平台插件: {'offscreen' if _offscreen else '真实窗口'}")
    print(f"播放地址: {source}")

    if args.docs:
        capture_docs(app, window, source)
        app.exec()
        print("完成")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    def start_playback() -> None:
        window.address.setText(source)
        window._play_from_address()  # noqa: SLF001

    def capture(kind: str) -> None:
        path = OUT_DIR / f"ui_{kind}.png"
        pixmap = window.grab()
        if pixmap.isNull():
            print(f"抓图失败: {kind}")
            return
        pixmap.save(str(path), "PNG")
        print(f"已保存 {path}  ({pixmap.width()}x{pixmap.height()})")

    def report_state() -> None:
        ctrl = window.controller
        print(
            f"状态: 已加载={ctrl._media_loaded} 直播={ctrl.is_live} "  # noqa: SLF001
            f"进度条最大值={window.control.slider.maximum()} "
            f"时间标签={window.control.label_time.text()!r}"
        )

    def go_fullscreen() -> None:
        window.toggle_fullscreen()
        print(f"已切换全屏: {window.isFullScreen()}")

    def finish() -> None:
        window.close()
        app.quit()

    QTimer.singleShot(500, lambda: capture("idle"))
    QTimer.singleShot(900, start_playback)
    QTimer.singleShot(4500, lambda: capture("playing"))
    QTimer.singleShot(4700, report_state)
    QTimer.singleShot(5500, go_fullscreen)
    QTimer.singleShot(6300, lambda: capture("fullscreen"))
    QTimer.singleShot(7500, finish)

    app.exec()
    print("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
