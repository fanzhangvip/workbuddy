"""自检里"实播测试前如何准备地址"的判定测试。

这段逻辑容易被改坏，而它的错误表现很隐蔽：自检照样退出码 0，只是悄悄
把"测网页地址"退化成"测能不能打开一个 HTML 页面"。所以固定住以下两条：

1. 已知视频站点（``WEB_PAGE``）→ **必须先解析**，再拿解析出的流去实测；
2. 看不出类型的 http 地址（``UNKNOWN_HTTP``）→ **不在这里解析**，
   留给实播测试做"先直连、失败再解析"的兜底（与界面策略保持一致）。

运行:
    ./.venv/Scripts/python.exe -m unittest tests.test_selfcheck_target -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import resolver  # noqa: E402
from src.utils import selfcheck  # noqa: E402


class _FakeSay:
    """收集 say() 的输出。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class _FakeResult:
    def __init__(self, ok: bool = True, url: str = "", error: str = "",
                 report: str = "") -> None:
        self.ok = ok
        self.url = url
        self.error = error
        self._report = report

    def report(self) -> str:
        return self._report


class TestPrepareProbeTarget(unittest.TestCase):
    def setUp(self) -> None:
        self.say = _FakeSay()
        self.problems: list[str] = []
        self.calls: list[str] = []
        self.next_result = _FakeResult(
            ok=True, url="https://cdn.example.com/real.mp4", report="结论: 单文件"
        )
        self._orig_resolve = resolver.resolve
        resolver.resolve = self._fake_resolve  # type: ignore[assignment]

    def tearDown(self) -> None:
        resolver.resolve = self._orig_resolve

    def _fake_resolve(self, url, **_kwargs):
        self.calls.append(url)
        return self.next_result

    # ---------------------------------------------------------------- 必须解析
    def test_web_page_is_resolved_first(self) -> None:
        """已知站点必须先解析——否则自检测的是"能不能打开 HTML"。"""
        page = "https://www.bilibili.com/video/BV1xx411c7mD"
        target = selfcheck._prepare_probe_target(page, self.say, self.problems)
        self.assertEqual(self.calls, [page])
        self.assertEqual(target, "https://cdn.example.com/real.mp4")

    # -------------------------------------------------- 不解析，留给实播兜底
    def test_unknown_http_is_left_for_the_probe_fallback(self) -> None:
        """看不出类型的 http 地址不在这里解析，交给实播测试兜底。"""
        for url in (
            "http://example.com/play.php?id=1",
            "https://example.com/download",
            "http://127.0.0.1:8821/.tools/webtest/page.html",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    selfcheck._prepare_probe_target(url, self.say, self.problems), url
                )
        self.assertEqual(self.calls, [], "UNKNOWN_HTTP 不该在这里被解析")

    def test_direct_media_and_live_streams_pass_through(self) -> None:
        for url in (
            "https://example.com/a.mp4",
            "https://example.com/live/index.m3u8",
            "rtsp://192.168.1.10:8554/test",
            "rtmp://example.com/live/stream",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    selfcheck._prepare_probe_target(url, self.say, self.problems), url
                )
        self.assertEqual(self.calls, [])

    # ------------------------------------------------------------------ 失败面
    def test_resolve_failure_is_recorded_and_returns_empty(self) -> None:
        self.next_result = _FakeResult(
            ok=False, error="该站点只提供分离的音视频流", report="结论: 分离流"
        )
        page = "https://www.bilibili.com/video/BV1xx411c7mD"
        self.assertEqual(
            selfcheck._prepare_probe_target(page, self.say, self.problems), ""
        )
        self.assertTrue(self.problems)
        self.assertIn("分离", self.problems[0])

    def test_resolve_exception_does_not_propagate(self) -> None:
        """解析抛异常时自检本身不能崩，要记成问题并返回空串。"""

        def boom(_url, **_kwargs):
            raise RuntimeError("yt-dlp 内部炸了")

        resolver.resolve = boom  # type: ignore[assignment]
        page = "https://www.bilibili.com/video/BV1xx411c7mD"
        self.assertEqual(
            selfcheck._prepare_probe_target(page, self.say, self.problems), ""
        )
        self.assertTrue(self.problems)
        self.assertIn("异常", self.problems[0])


class TestKindOf(unittest.TestCase):
    def test_returns_none_when_classify_explodes(self) -> None:
        """分类失败不该把自检本身搞崩——当作"不值得兜底解析"。"""
        import src.core.url_router as router

        original = router.classify
        router.classify = lambda _url: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertIsNone(selfcheck._kind_of("http://example.com/x"))
        finally:
            router.classify = original

    def test_normal_classification(self) -> None:
        from src.core.url_router import UrlKind

        self.assertIs(
            selfcheck._kind_of("http://example.com/x.html"), UrlKind.UNKNOWN_HTTP
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
