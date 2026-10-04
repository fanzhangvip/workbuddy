"""剪贴板识别逻辑测试。

这个功能的成败全在"会不会误报"：剪贴板变化极其频繁，只要有一次误提示，
用户就会直接关掉它。所以测试重点放在**不该提示的情形**上。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from src.utils.clipboard_watcher import (  # noqa: E402
    MAX_LENGTH,
    ClipboardWatcher,
    looks_like_playable,
)

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class LooksLikePlayableTest(unittest.TestCase):
    def test_accepts_media_links(self) -> None:
        for text in (
            "https://example.com/video.mp4",
            "https://example.com/live/index.m3u8?token=abc",
            "rtsp://192.168.1.10:554/stream1",
            "rtmp://live.example.com/app/stream",
            "http://example.com/a.mkv",
        ):
            self.assertTrue(looks_like_playable(text), text)

    def test_accepts_known_video_site_pages(self) -> None:
        for text in (
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://v.qq.com/x/cover/abc.html",
        ):
            self.assertTrue(looks_like_playable(text), text)

    def test_strips_surrounding_whitespace(self) -> None:
        self.assertTrue(looks_like_playable("  https://example.com/a.mp4  \n"))

    # ------------------------------------------------------------ 不该提示
    def test_rejects_plain_text(self) -> None:
        for text in (
            "",
            "   ",
            "这是一段普通的中文",
            "hello world",
            "import os",
            "C:\\Users\\me\\notes.txt",
        ):
            self.assertFalse(looks_like_playable(text), text)

    def test_rejects_multi_line_text(self) -> None:
        """复制一整篇文章时里面总有链接，但那不代表"想播放这一条"。"""
        self.assertFalse(
            looks_like_playable("看这个 https://example.com/a.mp4\n还有别的")
        )

    def test_rejects_relative_paths_without_scheme(self) -> None:
        self.assertFalse(looks_like_playable("videos/a.mp4"))
        self.assertFalse(looks_like_playable("example.com/a.mp4"))

    def test_rejects_overlong_text(self) -> None:
        long_url = "https://example.com/" + "x" * MAX_LENGTH
        self.assertFalse(looks_like_playable(long_url))

    def test_rejects_nonexistent_local_path(self) -> None:
        """本地路径必须真的存在才提示，否则随便一句带斜杠的话都会误报。"""
        self.assertFalse(looks_like_playable("C:\\nope\\not-here.mp4"))

    def test_accepts_existing_local_file(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
            path = Path(handle.name)
        try:
            self.assertTrue(looks_like_playable(str(path)))
        finally:
            path.unlink(missing_ok=True)


class WatcherBehaviourTest(unittest.TestCase):
    def setUp(self) -> None:
        self.watcher = ClipboardWatcher()
        self.detected: list[str] = []
        self.watcher.urlDetected.connect(self.detected.append)
        self._saved = None
        board = QApplication.clipboard()
        self._saved = board.text()

    def tearDown(self) -> None:
        self.watcher.set_enabled(False)
        if self._saved is not None:
            QApplication.clipboard().setText(self._saved)

    def _set_clipboard(self, text: str) -> None:
        QApplication.clipboard().setText(text)
        _app.processEvents()  # type: ignore[union-attr]

    def test_emits_once_per_unique_url(self) -> None:
        self.watcher.set_enabled(True)
        self.detected.clear()

        self._set_clipboard("https://example.com/a.mp4")
        self.assertEqual(len(self.detected), 1)

        # 同一条再复制一次不该重复提示
        self.watcher.check_now()
        self.assertEqual(len(self.detected), 1)

        self._set_clipboard("https://example.com/b.mp4")
        self.assertEqual(len(self.detected), 2)
        self.assertEqual(self.detected[-1], "https://example.com/b.mp4")

    def test_does_not_emit_when_disabled(self) -> None:
        self.watcher.set_enabled(False)
        self.detected.clear()
        self._set_clipboard("https://example.com/c.mp4")
        self.assertEqual(self.detected, [])
        self.assertFalse(self.watcher.check_now())

    def test_ignores_plain_text(self) -> None:
        self.watcher.set_enabled(True)
        self.detected.clear()
        self._set_clipboard("just some words")
        self.assertEqual(self.detected, [])

    def test_forget_allows_repeat(self) -> None:
        self.watcher.set_enabled(True)
        self.detected.clear()
        self._set_clipboard("https://example.com/d.mp4")
        self.assertEqual(len(self.detected), 1)
        self.watcher.forget()
        self.assertTrue(self.watcher.check_now())
        self.assertEqual(len(self.detected), 2)

    def test_enable_twice_is_safe(self) -> None:
        self.watcher.set_enabled(True)
        self.watcher.set_enabled(True)
        self.assertTrue(self.watcher.enabled)
        self.watcher.set_enabled(False)
        self.watcher.set_enabled(False)
        self.assertFalse(self.watcher.enabled)


if __name__ == "__main__":
    unittest.main()
