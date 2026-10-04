"""播放后端：工厂选择、libmpv 可用性、地址工具。

mpv 是**可选**依赖（本环境通常没装），所以这里的测试不能假设它可用——
重点是验证"没装的时候行为也正确"：工厂必须回退到 Qt 后端，
并且所有描述/探测函数都不能抛异常。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from src.core.backends import factory  # noqa: E402
from src.core.backends.base import PlayerBackend  # noqa: E402
from src.core.backends.mpv_backend import (  # noqa: E402
    INSTALL_HINT,
    MPV_DLL_ENV,
    RTSP_TRANSPORTS,
    describe,
    find_mpv_dll,
    mpv_available,
    probe_dll,
)
from src.core.backends.qt_backend import QtMediaBackend  # noqa: E402
from src.core.url_router import UrlKind  # noqa: E402
from src.utils import settings as S  # noqa: E402
from src.utils.urls import is_loopback_url, to_playable_url  # noqa: E402

_app: QApplication | None = None

#: 本地测试素材（`.tools/` 已 gitignore，缺失时相关用例自动跳过）
_LOCAL_MEDIA = ROOT / ".tools" / "testmedia" / "local_test.mp4"


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    """跑 Qt 事件循环直到条件成立或超时。返回条件是否成立。"""
    from PySide6.QtCore import QEventLoop, QTimer

    if predicate():
        return True
    loop = QEventLoop()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    deadline.start(int(timeout * 1000))

    ticker = QTimer()
    ticker.timeout.connect(lambda: loop.quit() if predicate() else None)
    ticker.start(50)
    loop.exec()
    ticker.stop()
    deadline.stop()
    return bool(predicate())


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class LoopbackTest(unittest.TestCase):
    """环境变量里配了代理时，本机地址必须能被识别出来并绕开代理。"""

    def test_detects_loopback(self) -> None:
        for text in (
            "http://127.0.0.1:8080/v.mp4",
            "https://127.0.0.1/x",
            "http://localhost:5000/a.m3u8",
            "127.0.0.1:9000/v.mp4",
            "http://127.5.5.5/y.mp4",
        ):
            self.assertTrue(is_loopback_url(text), text)

    def test_rejects_remote(self) -> None:
        for text in (
            "https://example.com/v.mp4",
            "http://192.168.1.5/v.mp4",
            "rtsp://127.0.0.1/stream",     # 不是 http(s)，本工具不负责
            "",
        ):
            self.assertFalse(is_loopback_url(text), text)


class MpvAvailabilityTest(unittest.TestCase):
    def test_available_returns_bool(self) -> None:
        self.assertIsInstance(mpv_available(), bool)

    def test_describe_never_raises(self) -> None:
        text = describe()
        self.assertTrue(text)
        self.assertIn("libmpv", text)

    def test_find_dll_returns_string(self) -> None:
        self.assertIsInstance(find_mpv_dll(), str)

    def test_probe_returns_tuple(self) -> None:
        ok, message = probe_dll()
        self.assertIsInstance(ok, bool)
        self.assertTrue(message)
        if not mpv_available():
            self.assertIn("python-mpv", message)

    def test_install_hint_mentions_both_parts(self) -> None:
        """提示必须同时讲清 Python 绑定和动态库——只装一个是用不了的。"""
        self.assertIn("python-mpv", INSTALL_HINT)
        self.assertIn("mpv-2.dll", INSTALL_HINT)
        self.assertIn(MPV_DLL_ENV, INSTALL_HINT)

    def test_rtsp_transport_values(self) -> None:
        self.assertIn("tcp", RTSP_TRANSPORTS)
        self.assertIn("udp", RTSP_TRANSPORTS)


class FactoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._backend = S.get_str(S.K_BACKEND, "qt")
        self._prefer = S.get_bool(S.K_PREFER_MPV_FOR_LIVE, False)

    def tearDown(self) -> None:
        S.settings().setValue(S.K_BACKEND, self._backend)
        S.settings().setValue(S.K_PREFER_MPV_FOR_LIVE, self._prefer)

    def test_default_is_qt(self) -> None:
        S.settings().setValue(S.K_BACKEND, "qt")
        backend = factory.create_backend(UrlKind.DIRECT_MEDIA)
        self.assertIsInstance(backend, QtMediaBackend)
        self.assertIsInstance(backend, PlayerBackend)
        backend.release()

    def test_falls_back_when_mpv_missing(self) -> None:
        """选了 mpv 但没装：必须**静默回退**到 Qt，而不是抛异常。"""
        S.settings().setValue(S.K_BACKEND, "mpv")
        backend = factory.create_backend(UrlKind.DIRECT_MEDIA)
        if mpv_available():
            # 装了的话可能真的拿到 mpv 后端；那也是正确行为
            self.assertIsInstance(backend, PlayerBackend)
        else:
            self.assertIsInstance(backend, QtMediaBackend)
        backend.release()

    def test_live_preference_still_safe(self) -> None:
        S.settings().setValue(S.K_BACKEND, "qt")
        S.settings().setValue(S.K_PREFER_MPV_FOR_LIVE, True)
        backend = factory.create_backend(UrlKind.LIVE_STREAM)
        self.assertIsInstance(backend, PlayerBackend)
        backend.release()

    def test_describe_backends_mentions_both(self) -> None:
        text = factory.describe_backends()
        self.assertIn("QtMultimedia", text)
        self.assertIn("libmpv", text)

    def test_qt_backend_capabilities(self) -> None:
        backend = factory.create_backend(UrlKind.DIRECT_MEDIA)
        caps = backend.capabilities()
        self.assertIn("http", caps)
        self.assertIn("hls", caps)
        backend.release()


class QtExtraAudioTest(unittest.TestCase):
    """附加音轨（DASH 分离流）在 Qt 后端上的接口契约。

    `extra_audio_active()` 的语义是"音轨**已经加载**"，不是"设置过 URL"——
    它是给 `tools/page_play_check.py` 当硬证据用的（判断"声音真的在走"），
    所以 `setSource()` 之后必须先等到 `LoadedMedia` 才允许为真。
    下面两条分别验证这两个阶段。
    """

    def test_supports_extra_audio(self) -> None:
        backend = QtMediaBackend()
        self.assertTrue(backend.supports_extra_audio())
        self.assertFalse(backend.extra_audio_active())
        backend.release()

    def test_active_is_false_before_media_is_loaded(self) -> None:
        """设了 URL 但还没加载完成时，不能声称音轨在播。"""
        backend = QtMediaBackend()
        # 127.0.0.1:1 上不会有服务，永远加载不完
        backend.set_audio_source("http://127.0.0.1:1/audio.m4a")
        self.assertFalse(backend.extra_audio_active())
        backend.clear_audio_source()
        self.assertFalse(backend.extra_audio_active())
        backend.release()

    @unittest.skipUnless(
        _LOCAL_MEDIA.exists(), f"缺少本地测试素材 {_LOCAL_MEDIA}"
    )
    def test_real_audio_track_loads(self) -> None:
        """真的挂一路音轨上去，等它加载完成——验证整条链路通。"""
        backend = QtMediaBackend()
        backend.set_audio_source(to_playable_url(str(_LOCAL_MEDIA)))
        _wait_until(lambda: backend.extra_audio_active(), timeout=15)
        self.assertTrue(
            backend.extra_audio_active(),
            "本地音轨在 15s 内没有加载完成，附加音轨链路有问题",
        )
        backend.clear_audio_source()
        self.assertFalse(backend.extra_audio_active())
        backend.release()

    def test_empty_url_is_a_noop(self) -> None:
        backend = QtMediaBackend()
        backend.set_audio_source("")
        self.assertFalse(backend.extra_audio_active())
        backend.release()


if __name__ == "__main__":
    unittest.main()
