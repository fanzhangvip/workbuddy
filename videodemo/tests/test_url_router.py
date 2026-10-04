"""URL 路由与数据模型测试。

不依赖 Qt，可直接运行:
    ./.venv/Scripts/python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.media_item import MediaItem  # noqa: E402
from src.core.url_router import (  # noqa: E402
    UrlKind,
    classify,
    host_matches,
    is_loopback,
)
from src.utils.urls import is_loopback_url  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertIs(classify(""), UrlKind.UNKNOWN)
        self.assertIs(classify("   "), UrlKind.UNKNOWN)

    def test_local_existing_path(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
            path = fh.name
        try:
            self.assertIs(classify(path), UrlKind.LOCAL)
        finally:
            os.unlink(path)

    def test_file_uri(self) -> None:
        self.assertIs(classify("file:///D:/video/a.mp4"), UrlKind.LOCAL)

    def test_direct_media(self) -> None:
        for url in (
            "https://example.com/a.mp4",
            "http://example.com/dir/b.mkv",
            "https://cdn.example.com/c.FLV",
            "https://example.com/sub/d.webm",
        ):
            with self.subTest(url=url):
                self.assertIs(classify(url), UrlKind.DIRECT_MEDIA)

    def test_hls_with_query_is_still_hls(self) -> None:
        """带 token 的 m3u8 是最常见的误判场景。"""
        url = "https://cdn.example.com/live/stream.m3u8?token=abc123&expire=999"
        self.assertIs(classify(url), UrlKind.HLS)

    def test_live_streams(self) -> None:
        for url in (
            "rtsp://192.168.1.10:8554/test",
            "rtsps://example.com/live",
            "rtmp://example.com/live/stream",
            "rtmps://example.com/live",
            "udp://239.0.0.1:1234",
            "srt://example.com:9000",
        ):
            with self.subTest(url=url):
                self.assertIs(classify(url), UrlKind.LIVE_STREAM)

    def test_web_page(self) -> None:
        for url in (
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://youtu.be/aqz-KE-bpKQ",
            "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
            "https://v.douyin.com/xxxxx/",
            "https://www.bilibili.com/video/BV1xx411c7mD?p=2",
        ):
            with self.subTest(url=url):
                self.assertIs(classify(url), UrlKind.WEB_PAGE)

    def test_unknown_http(self) -> None:
        for url in (
            "http://example.com/",
            "http://example.com/play.php?id=1",
            "https://example.com/download",
        ):
            with self.subTest(url=url):
                self.assertIs(classify(url), UrlKind.UNKNOWN_HTTP)

    def test_unsupported_scheme(self) -> None:
        self.assertIs(classify("ftp://example.com/a.mp4"), UrlKind.UNKNOWN)
        self.assertIs(classify("just some text"), UrlKind.UNKNOWN)

    def test_scheme_省略的站点地址(self) -> None:
        self.assertIs(classify("www.bilibili.com/video/BV1xx411c7mD"), UrlKind.WEB_PAGE)


class TestHostMatches(unittest.TestCase):
    def test_subdomain(self) -> None:
        self.assertTrue(host_matches("www.bilibili.com"))
        self.assertTrue(host_matches("bilibili.com"))
        self.assertTrue(host_matches("b23.tv"))
        self.assertTrue(host_matches("www.youtube.com:443"))

    def test_not_fooled_by_suffix_trick(self) -> None:
        """notbilibili.com 不应命中 bilibili.com。"""
        self.assertFalse(host_matches("notbilibili.com"))
        self.assertFalse(host_matches("example.com"))


class TestMediaItem(unittest.TestCase):
    def test_title_from_url(self) -> None:
        item = MediaItem.from_url("https://example.com/dir/%E8%A7%86%E9%A2%91.mp4")
        self.assertEqual(item.title, "视频.mp4")

    def test_roundtrip(self) -> None:
        item = MediaItem.from_url("https://example.com/a.mp4")
        item.duration_ms = 1234
        restored = MediaItem.from_dict(item.to_dict())
        self.assertEqual(restored.url, item.url)
        self.assertEqual(restored.title, item.title)
        self.assertEqual(restored.duration_ms, 1234)
        self.assertIs(restored.kind, UrlKind.DIRECT_MEDIA)

    def test_play_url_prefers_resolved(self) -> None:
        item = MediaItem.from_url("https://example.com/page")
        self.assertEqual(item.play_url, "https://example.com/page")
        item.resolved_url = "https://cdn.example.com/real.mp4"
        self.assertEqual(item.play_url, "https://cdn.example.com/real.mp4")

    def test_effective_kind_follows_resolved_stream(self) -> None:
        """网页地址解析成功后，超时与直播判定必须看解析出来的流。"""
        item = MediaItem.from_url("https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertIs(item.kind, UrlKind.WEB_PAGE)
        self.assertIs(item.effective_kind, UrlKind.WEB_PAGE)
        item.resolved_url = "https://cdn.example.com/live/index.m3u8"
        item.resolved_kind = UrlKind.HLS
        self.assertIs(item.effective_kind, UrlKind.HLS)

    def test_external_url_returns_page_after_resolving(self) -> None:
        """外部打开要用原页面地址：解析出的直链是带签名的临时地址。"""
        page = "https://www.bilibili.com/video/BV1xx411c7mD"
        item = MediaItem.from_url(page)
        self.assertEqual(item.external_url, page)
        item.resolved_url = "https://cdn.example.com/real.m4s?sign=xxx"
        self.assertEqual(item.external_url, page)

    def test_resolved_url_is_not_persisted(self) -> None:
        """带签名的直链有有效期，不能写进配置，否则下次启动必然失效。"""
        item = MediaItem.from_url("https://www.bilibili.com/video/BV1xx411c7mD")
        item.resolved_url = "https://cdn.example.com/real.m4s?sign=xxx"
        self.assertNotIn("resolved_url", item.to_dict())


class LoopbackTest(unittest.TestCase):
    """回环判定：`core.url_router.is_loopback` 与 `utils.urls.is_loopback_url`。

    项目里有两处回环判定，问的其实是两个不同的问题：
      * `url_router.is_loopback`：这条地址该不该走**我们的本地反向代理**
        （本机服务没有防盗链问题，套代理只是白绕一圈）；
      * `urls.is_loopback_url`：这条地址该不该绕开**环境里的 HTTP_PROXY**
        （否则 requests 连 127.0.0.1 都会丢给环境代理，得到 ProxyError）。

    它们对"是不是本机"的判断必须一致，否则同一条地址会被一个判为"本机直连"、
    另一个判为"要走代理"。这组用例就是钉住这一点。
    """

    LOOPBACK = (
        "http://127.0.0.1:8000/a.mp4",
        "http://localhost:8000/a.mp4",
        "http://127.1:8000/a.mp4",
        "http://127.0.0.2/a.mp4",
        "http://127.255.255.254/a.mp4",
        "http://[::1]:8000/a.mp4",
        "http://a.localhost/a.mp4",
        "http://0.0.0.0:8000/a.mp4",
    )
    REMOTE = (
        "http://example.com/a.mp4",
        "https://cdn.example.com/video.m4s?sign=x",
        "http://192.168.1.10:8000/a.mp4",
        "http://10.0.0.5/a.mp4",
        "http://128.0.0.1/a.mp4",
        "http://notlocalhost.example/a.mp4",
        "",
    )

    def test_loopback_addresses_are_recognised(self) -> None:
        for url in self.LOOPBACK:
            with self.subTest(url=url):
                self.assertTrue(is_loopback(url), f"路由层漏判: {url}")
                self.assertTrue(is_loopback_url(url), f"工具层漏判: {url}")

    def test_remote_addresses_are_not_loopback(self) -> None:
        for url in self.REMOTE:
            with self.subTest(url=url):
                self.assertFalse(is_loopback(url), f"路由层误判: {url}")
                self.assertFalse(is_loopback_url(url), f"工具层误判: {url}")

    def test_the_two_helpers_agree(self) -> None:
        """不许漂移：两处的结论必须一致（HLS/局域网等其余分类都不受影响）。"""
        for url in self.LOOPBACK + self.REMOTE:
            with self.subTest(url=url):
                self.assertEqual(
                    is_loopback(url),
                    is_loopback_url(url),
                    f"两处回环判定不一致: {url}",
                )

    def test_any_scheme_counts_for_the_router(self) -> None:
        """路由层问的是"地址在不在本机"，与协议无关。"""
        self.assertTrue(is_loopback("rtsp://127.0.0.1:554/live"))
        self.assertTrue(is_loopback("rtmp://localhost/live/test"))

    def test_no_host_is_not_loopback(self) -> None:
        for url in ("", "   ", "file:///C:/a.mp4", "not a url"):
            with self.subTest(url=url):
                self.assertFalse(is_loopback(url))


if __name__ == "__main__":
    unittest.main(verbosity=2)
