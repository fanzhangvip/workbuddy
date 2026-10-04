"""下载器测试。

用 `tests/httpfixture.py` 起的真实本地 HTTP 服务器，覆盖：

- 直链下载完成、进度上报、文件大小正确；
- **断点续传**（先放一个 `.part` 再下，服务端要真的返回 206）；
- **取消**（服务端慢速吐数据，下到一半取消，`.part` 应保留下来供续传）；
- 404 的友好报错；
- **网页地址走 yt-dlp**（本地 HTML 页面 + `<video src>`，generic 解析器能识破，
  这条链路完全不需要外网，却能真实验证"yt-dlp 下载 + 进度回调"是否接通）。

最后一条是刻意的：只用 mock 的话，"yt-dlp 的参数配错了"这种问题永远测不出来。
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402
from tests.httpfixture import LocalHTTPServer  # noqa: E402

from src.core.downloader import (  # noqa: E402
    DownloadManager,
    DownloadState,
    _friendly_download_error,
    _video_only,
    find_ffmpeg,
    format_seconds,
    format_size,
    guess_extension,
    safe_filename,
    unique_path,
)

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def _wait_for(predicate, timeout: float = 40.0) -> bool:
    """等到条件成立。下载跑在工作线程里，这里只看任务对象上的状态字段。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        if _app is not None:
            _app.processEvents()
        time.sleep(0.05)
    return False


def _settle(manager: DownloadManager, task_id: int, timeout: float = 40.0):
    task = manager.task(task_id)
    _wait_for(
        lambda: task is not None and task.state in DownloadState.FINISHED, timeout
    )
    return task


class HelperTest(unittest.TestCase):
    def test_format_size(self) -> None:
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(512), "512 B")
        self.assertEqual(format_size(1024), "1.0 KB")
        self.assertEqual(format_size(1536), "1.5 KB")
        self.assertEqual(format_size(5 * 1024 * 1024), "5.0 MB")

    def test_format_seconds(self) -> None:
        self.assertEqual(format_seconds(0), "00:00")
        self.assertEqual(format_seconds(75), "01:15")
        self.assertEqual(format_seconds(3725), "1:02:05")

    def test_safe_filename_strips_illegal_chars(self) -> None:
        self.assertEqual(safe_filename('a<b>c:d"e/f\\g|h?i*j'), "a_b_c_d_e_f_g_h_i_j")
        # Windows 不允许文件名以点或空格结尾
        self.assertEqual(safe_filename("trailing... "), "trailing")
        self.assertEqual(safe_filename(""), "download")

    def test_safe_filename_avoids_device_names(self) -> None:
        """CON/PRN 这类保留名在 Windows 上会直接创建失败。"""
        self.assertTrue(safe_filename("CON").startswith("_"))
        self.assertTrue(safe_filename("com1").startswith("_"))

    def test_safe_filename_truncates(self) -> None:
        self.assertLessEqual(len(safe_filename("x" * 500)), 120)

    def test_guess_extension_ignores_query(self) -> None:
        self.assertEqual(guess_extension("https://a.com/v.mp4?token=xyz"), ".mp4")
        self.assertEqual(guess_extension("https://a.com/v"), ".mp4")
        self.assertEqual(guess_extension("https://a.com/v.m3u8"), ".m3u8")

    def test_unique_path_avoids_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "a.mp4").write_bytes(b"x")
            self.assertEqual(unique_path(directory, "a.mp4").name, "a (2).mp4")

    def test_unique_path_part_awareness(self) -> None:
        """默认要把 .part 算作被占用；关掉之后才允许复用（续传用）。"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "a.mp4.part").write_bytes(b"x")
            self.assertEqual(unique_path(directory, "a.mp4").name, "a (2).mp4")
            self.assertEqual(
                unique_path(directory, "a.mp4", consider_part=False).name, "a.mp4"
            )

    def test_friendly_error_mapping(self) -> None:
        self.assertIn("网络连接失败", _friendly_download_error(OSError("Connection timed out")))
        self.assertIn("拒绝访问", _friendly_download_error(OSError("403 Forbidden")))
        self.assertIn("地址不存在", _friendly_download_error(OSError("404 Not Found")))
        # 认不出来时也要把原始信息带上，别吞掉线索
        self.assertIn("boom", _friendly_download_error(OSError("boom")))

    def test_find_ffmpeg_returns_tuple(self) -> None:
        found, path = find_ffmpeg()
        self.assertIsInstance(found, bool)
        self.assertIsInstance(path, str)
        if found:
            self.assertTrue(Path(path).is_file())

    def test_video_only_detection(self) -> None:
        self.assertTrue(_video_only({"acodec": "none"}))
        self.assertFalse(_video_only({"acodec": "mp4a.40.2"}))
        # 未知（None）不能当成"没有声音"
        self.assertFalse(_video_only({"acodec": None}))
        self.assertFalse(_video_only("not a dict"))


class DirectDownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.manager = DownloadManager()
        self.manager.set_download_dir(self.dir)

    def tearDown(self) -> None:
        self.manager.cancel_all()
        self._tmp.cleanup()

    def test_download_completes_with_correct_size(self) -> None:
        with LocalHTTPServer() as server:
            task = self.manager.start(server.url("/file.bin"), title="clip")
            self.assertIsNotNone(task)
            task = _settle(self.manager, task.task_id)

            self.assertEqual(task.state, DownloadState.DONE, task.error)
            self.assertTrue(task.dest.is_file())
            self.assertEqual(task.dest.stat().st_size, server.size)
            self.assertEqual(task.dest.read_bytes(), server.payload)
            self.assertEqual(task.percent, 100.0)
            # 完成后不该留下 .part
            self.assertFalse(task.part_path.exists())

    def test_filename_keeps_single_extension(self) -> None:
        """没给标题时文件名取自地址末段，扩展名不能被拼两遍。

        地址栏直接粘贴直链再点下载时，`_download_current` 就是不传标题的，
        所以这是用户最常走的那条路——早期版本会下出 `video.mp4.mp4`。
        """
        with LocalHTTPServer() as server:
            task = self.manager.start(server.url("/video.mp4"))
            self.assertIsNotNone(task)
            task = _settle(self.manager, task.task_id)

        self.assertEqual(task.state, DownloadState.DONE, task.error)
        self.assertEqual(task.dest.name, "video.mp4")

    def test_filename_gains_extension_from_url(self) -> None:
        """有标题时标题通常不带扩展名，这时才该从地址补上。"""
        with LocalHTTPServer() as server:
            task = self.manager.start(server.url("/video.mp4"), title="我的视频")
        self.assertIsNotNone(task)
        self.assertTrue(task.dest.name.startswith("我的视频"))
        self.assertEqual(task.dest.suffix, ".mp4")

    def test_progress_is_reported(self) -> None:
        with LocalHTTPServer() as server:
            seen: list[int] = []
            manager = self.manager
            manager.taskUpdated.connect(lambda t: seen.append(t.received))
            task = manager.start(server.url("/file.bin"), title="progress")
            _settle(manager, task.task_id)
            # 进度是跨线程排队投递的：任务状态已置为终态时，最后几次
            # progressReady 可能还排在事件队列里，必须再泵一会儿才算数。
            _wait_for(lambda: server.size in seen, timeout=5)

        self.assertIn(server.size, seen, "完成时的字节数应当上报过")
        self.assertGreater(len(seen), 2, "应当有多次进度更新，而不是只有首尾两次")

    def test_cancel_leaves_part_for_resume(self) -> None:
        with LocalHTTPServer() as server:
            server.set_slow(delay=0.05, chunk_size=4 * 1024)
            task = self.manager.start(server.url("/slow.bin"), title="slow")
            self.assertIsNotNone(task)

            # 等真正开始并且下了一部分，再取消
            _wait_for(lambda: task.received > 0, timeout=10)
            self.manager.cancel(task.task_id)
            task = _settle(self.manager, task.task_id, timeout=20)

            self.assertEqual(task.state, DownloadState.CANCELLED)
            self.assertFalse(task.dest.exists(), "取消后不该出现正式文件")
            self.assertTrue(task.part_path.exists(), "取消应保留 .part 以供续传")
            self.assertGreater(task.received, 0)
            self.assertLess(task.received, server.size)

    def test_resume_from_existing_part(self) -> None:
        """先写好一半的 .part，再下：服务端应收到 Range，最终文件必须完整。"""
        with LocalHTTPServer() as server:
            dest = self.dir / "resumed.bin"
            half = server.size // 2
            dest.with_name(dest.name + ".part").write_bytes(server.payload[:half])

            task = self.manager.start(server.url("/file.bin"), title="resumed", dest=dest)
            task = _settle(self.manager, task.task_id)

            self.assertEqual(task.state, DownloadState.DONE, task.error)
            self.assertEqual(dest.stat().st_size, server.size)
            self.assertEqual(dest.read_bytes(), server.payload)

            ranges = [
                entry["headers"].get("Range")
                for entry in server.requests_for("/file.bin")
            ]
            self.assertTrue(
                any(r == f"bytes={half}-" for r in ranges if r),
                f"续传必须发 Range 请求，实际收到: {ranges}",
            )

    def test_retry_reuses_dest_and_resumes(self) -> None:
        """重试必须复用原路径——否则会变成另存一份，续传形同虚设。"""
        with LocalHTTPServer() as server:
            first = self.manager.start(server.url("/file.bin"), title="retry")
            _settle(self.manager, first.task_id)
            first.dest.unlink()

            second = self.manager.start(
                server.url("/file.bin"), title="retry", dest=first.dest
            )
            self.assertEqual(second.dest, first.dest)
            second = _settle(self.manager, second.task_id)
            self.assertEqual(second.state, DownloadState.DONE)

    def test_404_fails_with_friendly_message(self) -> None:
        with LocalHTTPServer() as server:
            task = self.manager.start(server.url("/missing.bin"), title="missing")
            task = _settle(self.manager, task.task_id)

            self.assertEqual(task.state, DownloadState.FAILED)
            self.assertIn("404", task.error)
            self.assertIn("地址不存在", task.error)

    def test_empty_url_returns_none(self) -> None:
        self.assertIsNone(self.manager.start("   "))

    def test_headers_are_sent(self) -> None:
        """防盗链请求头必须真的发出去——这是本地代理之外的兜底路径。"""
        with LocalHTTPServer() as server:
            task = self.manager.start(
                server.url("/file.bin"),
                title="withheaders",
                headers={"Referer": "https://origin.example/", "X-Test": "1"},
            )
            _settle(self.manager, task.task_id)
            headers = server.last_headers("/file.bin")
        self.assertEqual(headers.get("Referer"), "https://origin.example/")
        self.assertEqual(headers.get("X-Test"), "1")

    def test_concurrency_limit_respected(self) -> None:
        self.manager.set_concurrency(1)
        with LocalHTTPServer() as server:
            server.set_slow(delay=0.05, chunk_size=8 * 1024)
            first = self.manager.start(server.url("/slow.bin"), title="a")
            second = self.manager.start(server.url("/slow.bin"), title="b")

            _wait_for(lambda: first.state == DownloadState.RUNNING, timeout=10)
            # 并发上限为 1 时，第二个必须还在排队
            self.assertEqual(second.state, DownloadState.QUEUED)
            self.manager.cancel_all()


class WebDownloadTest(unittest.TestCase):
    """网页地址走 yt-dlp 的那条链路（本地页面，不需要外网）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.manager = DownloadManager()
        self.manager.set_download_dir(self.dir)

    def tearDown(self) -> None:
        self.manager.cancel_all()
        self._tmp.cleanup()

    def test_yt_dlp_downloads_from_local_page(self) -> None:
        with LocalHTTPServer() as server:
            task = self.manager.start(
                server.url("/page.html"), title="page", is_web=True
            )
            self.assertIsNotNone(task)
            task = _settle(self.manager, task.task_id, timeout=60)

            self.assertEqual(task.state, DownloadState.DONE, task.error)
            self.assertTrue(task.dest.is_file(), f"没找到输出文件: {task.dest}")
            self.assertEqual(task.dest.stat().st_size, server.size)

    def test_yt_dlp_progress_hook_feeds_task(self) -> None:
        """进度回调必须真的把字节数写回任务对象——这是"进度条会动"的前提。"""
        with LocalHTTPServer() as server:
            task = self.manager.start(
                server.url("/page.html"), title="page2", is_web=True
            )
            _settle(self.manager, task.task_id, timeout=60)
        self.assertEqual(task.received, server.size)
        self.assertEqual(task.total, server.size)
        self.assertEqual(task.percent, 100.0)

    def test_cancel_web_download(self) -> None:
        """yt-dlp 那条路径的取消：靠 progress_hook 抛异常中断。

        必须让**媒体文件本身**慢下来（夹具里 set_slow 对所有端点生效），
        否则数据一瞬间就到齐，hook 还没来得及被调用下载就结束了。
        """
        with LocalHTTPServer(size=1024 * 1024) as server:
            server.set_slow(delay=0.05, chunk_size=4 * 1024)
            task = self.manager.start(
                server.url("/page.html"), title="page-slow", is_web=True
            )
            # 等它真的开始写数据（或退一步：给足 3 秒窗口再取消）
            _wait_for(lambda: task.received > 0, timeout=3)
            self.manager.cancel(task.task_id)
            task = _settle(self.manager, task.task_id, timeout=40)

            self.assertEqual(task.state, DownloadState.CANCELLED, task.error)
            self.assertLess(task.received, server.size)

    def test_ytdlp_hook_raises_cancelled(self) -> None:
        """取消的机制本身：置位后 hook 必须抛 _Cancelled 把下载拽停。"""
        import threading

        from src.core.downloader import DownloadTask, _Cancelled

        manager = DownloadManager()
        task = DownloadTask(task_id=1, url="http://x/y.mp4", dest=self.dir / "y.mp4")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(_Cancelled):
            manager._on_ytdlp_progress(  # noqa: SLF001 - 单测直接验证这一处逻辑
                task, {"status": "downloading", "downloaded_bytes": 1}, cancel
            )


if __name__ == "__main__":
    unittest.main()
