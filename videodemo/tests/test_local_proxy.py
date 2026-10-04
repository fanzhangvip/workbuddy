"""本地反向代理测试（防盗链）。

代理的价值全在"服务端**实际收到了什么**"，所以这里的断言都基于
`tests/httpfixture.py` 记录下来的请求头，而不是"我们在客户端设了什么"。

重点覆盖：
- 转发基本可用（状态码、内容、Content-Length 原样带回来）；
- **Range 透传**（要回 206 且带 `Content-Range`）——这是"能不能拖动进度条"的关键；
- **Referer 注入**（防盗链的核心）；
- HLS 清单不被代理（如实说明局限，而不是假装能行）；
- 开关关闭时 `proxy_url` 必须返回空串。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from tests.httpfixture import LocalHTTPServer  # noqa: E402

from src.core import local_proxy  # noqa: E402
from src.utils import settings as S  # noqa: E402


class ProxyCapabilityTest(unittest.TestCase):
    def test_mp4_is_proxy_capable(self) -> None:
        self.assertTrue(local_proxy.proxy_capable("https://a.com/v.mp4?token=x"))

    def test_hls_manifest_is_not_proxy_capable(self) -> None:
        """HLS 清单里的分片会绕过代理，所以如实拒绝，不假装能代理。"""
        self.assertFalse(local_proxy.proxy_capable("https://a.com/v.m3u8"))
        self.assertFalse(local_proxy.proxy_capable("https://a.com/v.m3u"))

    def test_non_http_scheme_not_capable(self) -> None:
        self.assertFalse(local_proxy.proxy_capable("rtsp://a.com/v"))
        self.assertFalse(local_proxy.proxy_capable("rtmp://a.com/v"))
        self.assertFalse(local_proxy.proxy_capable(""))

    def test_referer_derived_from_target(self) -> None:
        self.assertEqual(
            local_proxy.referer_for("https://www.a.com/x/y.mp4"), "https://www.a.com/"
        )
        # 显式指定优先
        self.assertEqual(
            local_proxy.referer_for("https://a.com/y.mp4", "https://b.com/"),
            "https://b.com/",
        )


class UpstreamProxyTest(unittest.TestCase):
    """转发时用哪条上游代理。

    这是"播放也能走代理"的关键：播放内核不认系统代理，requests 认，
    所以由这一层替它转发。
    """

    KEY = "network/proxy"

    def setUp(self) -> None:
        from src.utils import settings as S

        self.S = S
        store = S.settings()
        self._saved = store.value(self.KEY)
        store.setValue(self.KEY, "")

    def tearDown(self) -> None:
        store = self.S.settings()
        if self._saved is None:
            store.remove(self.KEY)
        else:
            store.setValue(self.KEY, self._saved)

    def test_loopback_must_bypass_the_proxy(self) -> None:
        """本机目标必须显式绕开，否则得到一个与真实原因无关的 ProxyError。"""
        self.assertEqual(
            local_proxy._upstream_proxies("http://127.0.0.1:9/x.bin"),  # noqa: SLF001
            {"http": None, "https": None},
        )

    def test_configured_proxy_is_used(self) -> None:
        self.S.settings().setValue(self.KEY, "http://127.0.0.1:7897")
        self.assertEqual(
            local_proxy._upstream_proxies("https://cdn.example.com/v.mp4"),  # noqa: SLF001
            {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"},
        )

    def test_unset_falls_back_to_environment(self) -> None:
        """没填就返回 None，让 requests 按环境变量 / 系统代理走。"""
        self.assertIsNone(
            local_proxy._upstream_proxies("https://cdn.example.com/v.mp4")  # noqa: SLF001
        )


class RequiredModeTest(unittest.TestCase):
    """`required=True`：这条不是"增强"，而是唯一能播的路子，不看总开关。"""

    KEY = "proxy/enabled"

    def setUp(self) -> None:
        from src.utils import settings as S

        self.S = S
        store = S.settings()
        self._saved = store.value(self.KEY)
        store.setValue(self.KEY, False)

    def tearDown(self) -> None:
        local_proxy.stop()
        store = self.S.settings()
        if self._saved is None:
            store.remove(self.KEY)
        else:
            store.setValue(self.KEY, self._saved)

    TARGET = "https://cdn.example.com/v.mp4"

    def test_switch_off_blocks_the_enhancement(self) -> None:
        self.assertEqual(local_proxy.proxy_url(self.TARGET), "")

    def test_required_ignores_the_switch(self) -> None:
        self.assertTrue(local_proxy.proxy_url(self.TARGET, required=True))

    def test_required_still_respects_capability(self) -> None:
        """强制也不能突破机制限制：HLS 清单代理不了就是代理不了。"""
        self.assertEqual(
            local_proxy.proxy_url("https://cdn.example.com/hls/index.m3u8",
                                  required=True),
            "",
        )


class ProxyServerTest(unittest.TestCase):
    def setUp(self) -> None:
        # 这几条用例断言的是"Referer 从目标地址推导"。如果开发机上恰好
        # 在设置里填过一个全局 Referer，它们就会莫名其妙地失败——
        # 测试不能依赖运行环境里的用户配置，所以先把它按到空值。
        from src.utils import settings as S

        self._referer_key = S.K_LOCAL_PROXY_REFERER
        self._saved_referer = S.settings().value(self._referer_key)
        S.settings().setValue(self._referer_key, "")

        self.server = local_proxy.LocalProxyServer()
        self.server.start(0)
        self.assertTrue(self.server.running)
        self.proxy_base = self.server.base_url()

    def tearDown(self) -> None:
        from src.utils import settings as S

        self.server.stop()
        if self._saved_referer is None:
            S.settings().remove(self._referer_key)
        else:
            S.settings().setValue(self._referer_key, self._saved_referer)

    # ------------------------------------------------------------ 基础
    def test_status_endpoint(self) -> None:
        response = requests.get(self.server.status_url(), timeout=5)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("ok"))

    def test_health_check(self) -> None:
        self.assertTrue(self.server.health_check())

    def test_unknown_path_is_404(self) -> None:
        response = requests.get(f"{self.proxy_base}/nope", timeout=5)
        self.assertEqual(response.status_code, 404)

    def test_missing_target_is_400(self) -> None:
        response = requests.get(f"{self.proxy_base}/proxy", timeout=5)
        self.assertEqual(response.status_code, 400)

    # ------------------------------------------------------------ 转发
    def test_forwards_content(self) -> None:
        with LocalHTTPServer() as origin:
            url = self.server.url_for(origin.url("/file.bin"))
            response = requests.get(url, timeout=10)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, origin.payload)
            # Content-Length 必须原样带回来，播放器靠它判断总大小
            self.assertEqual(
                response.headers.get("Content-Length"), str(origin.size)
            )
            self.assertEqual(response.headers.get("Accept-Ranges"), "bytes")

    def test_range_is_passed_through(self) -> None:
        """拖动进度条靠这个：请求头要转发，206 与 Content-Range 要带回来。"""
        with LocalHTTPServer() as origin:
            url = self.server.url_for(origin.url("/file.bin"))
            response = requests.get(url, headers={"Range": "bytes=100-199"}, timeout=10)

            self.assertEqual(response.status_code, 206)
            self.assertEqual(len(response.content), 100)
            self.assertEqual(response.content, origin.payload[100:200])
            self.assertEqual(
                response.headers.get("Content-Range"), f"bytes 100-199/{origin.size}"
            )
            # 源站确实收到了 Range
            sent = origin.last_headers("/file.bin").get("Range")
            self.assertEqual(sent, "bytes=100-199")

    def test_head_is_supported(self) -> None:
        with LocalHTTPServer() as origin:
            url = self.server.url_for(origin.url("/file.bin"))
            response = requests.head(url, timeout=10)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("Content-Length"), str(origin.size))
            self.assertEqual(response.content, b"")

    def test_referer_is_injected(self) -> None:
        """防盗链的核心：源站必须**真的**收到 Referer。"""
        with LocalHTTPServer() as origin:
            url = self.server.url_for(origin.url("/file.bin"))
            requests.get(url, timeout=10)
            headers = origin.last_headers("/file.bin")

        self.assertIn("Referer", headers)
        self.assertTrue(headers["Referer"].startswith("http://127.0.0.1:"))
        self.assertTrue(headers["User-Agent"])

    def test_explicit_referer_wins(self) -> None:
        with LocalHTTPServer() as origin:
            url = self.server.url_for(origin.url("/file.bin"), "https://origin.example/")
            requests.get(url, timeout=10)
            headers = origin.last_headers("/file.bin")
        self.assertEqual(headers.get("Referer"), "https://origin.example/")

    def test_resolver_user_agent_is_forwarded(self) -> None:
        """解析器给的 User-Agent 必须能送到源站（有些站点按 UA 放行）。"""
        with LocalHTTPServer() as origin:
            url = self.server.url_for(
                origin.url("/file.bin"), "", "BiliBili/1.0 (custom)"
            )
            requests.get(url, timeout=10)
            headers = origin.last_headers("/file.bin")
        self.assertEqual(headers.get("User-Agent"), "BiliBili/1.0 (custom)")

    def test_derived_referer_is_used_when_nothing_explicit(self) -> None:
        """没有显式 Referer 时，退回"由目标地址推导的站点根"。"""
        with LocalHTTPServer() as origin:
            url = self.server.url_for(origin.url("/file.bin"))
            requests.get(url, timeout=10)
            headers = origin.last_headers("/file.bin")
        self.assertTrue(headers.get("Referer", "").startswith("http://127.0.0.1:"))
        self.assertTrue(headers["Referer"].endswith("/"))

    def test_accept_encoding_disabled(self) -> None:
        """必须让源站不要压缩：压缩会让 Content-Length 与实际字节数对不上。"""
        with LocalHTTPServer() as origin:
            url = self.server.url_for(origin.url("/file.bin"))
            requests.get(
                url, headers={"Accept-Encoding": "gzip, deflate, br"}, timeout=10
            )
            headers = origin.last_headers("/file.bin")
        self.assertEqual(headers.get("Accept-Encoding"), "identity")

    def test_target_not_found_relays_404(self) -> None:
        with LocalHTTPServer() as origin:
            url = self.server.url_for(origin.url("/missing.bin"))
            response = requests.get(url, timeout=10)
        self.assertEqual(response.status_code, 404)

    def test_unreachable_target_returns_502(self) -> None:
        # 127.0.0.1 上一个几乎肯定没人监听的端口
        url = self.server.url_for("http://127.0.0.1:9/nothing.bin")
        response = requests.get(url, timeout=15)
        self.assertEqual(response.status_code, 502)

    def test_url_for_encodes_target(self) -> None:
        target = "https://a.com/v.mp4?token=abc&x=1"
        url = self.server.url_for(target)
        self.assertIn(quote(target, safe=""), url)
        self.assertNotIn("token=abc", url.split("u=")[1][: len(target)])


class RangeParsingTest(unittest.TestCase):
    """区间解析只认"能算清楚"的形式，认不出来就交给上层原样透传。"""

    def test_simple_forms(self) -> None:
        self.assertEqual(local_proxy.parse_range("bytes=0-"), (0, None))
        self.assertEqual(local_proxy.parse_range("bytes=100-199"), (100, 199))
        self.assertEqual(local_proxy.parse_range("bytes=100-"), (100, None))

    def test_unsupported_forms_are_none(self) -> None:
        for value in ("", "bytes=-500", "items=0-1", "bytes=abc-def", "bytes=200-100"):
            self.assertIsNone(local_proxy.parse_range(value), value)

    def test_multi_range_takes_the_first(self) -> None:
        self.assertEqual(local_proxy.parse_range("bytes=0-1,5-6"), (0, 1))

    def test_total_from_content_range(self) -> None:
        self.assertEqual(
            local_proxy.total_from_content_range("bytes 0-1023/4096"), 4096
        )
        self.assertEqual(local_proxy.total_from_content_range("bytes 0-1023/*"), -1)
        self.assertEqual(local_proxy.total_from_content_range(""), -1)


class UpstreamSlicingTest(unittest.TestCase):
    """源站对"整段"请求限速 100 倍，代理必须替播放器把它拆成有界区间。

    这是"只有画面没有声音"的根因所在：播放器一上来要的就是整段
    （`Range: bytes=0-`），源站只肯给它十几 KB/s，于是声音那一路要 20 秒
    才加载完（Qt 的网络超时正好也是 20 秒，会直接报错），画面却早就播起来了。
    音画因此拉开十几秒，靠 seek 去追又追不回来。

    这里的断言全部基于**源站实际收到了什么**——只看"客户端拿到了内容"
    是发现不了这个问题的。
    """

    def setUp(self) -> None:
        self.server = local_proxy.LocalProxyServer()
        self.server.start(0)
        # 把分片阈值调小，用几百 KB 的载荷就能验证"多片拼接"
        self._saved_slice = local_proxy._SLICE_BYTES
        local_proxy._SLICE_BYTES = 4096

    def tearDown(self) -> None:
        local_proxy._SLICE_BYTES = self._saved_slice
        self.server.stop()

    def _origin_ranges(self, origin) -> list[str]:
        return [item["headers"].get("Range") for item in origin.requests_for("/file.bin")]

    def test_whole_file_request_is_sliced_upstream(self) -> None:
        with LocalHTTPServer(size=20000) as origin:
            url = self.server.url_for(origin.url("/file.bin"))
            response = requests.get(url, headers={"Range": "bytes=0-"}, timeout=10)
            sent = self._origin_ranges(origin)

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers.get("Content-Range"), "bytes 0-19999/20000")
        self.assertEqual(response.headers.get("Content-Length"), "20000")
        self.assertEqual(response.content, origin.payload)

        # 关键：源站收到的每一条都必须是"有上界"的区间
        self.assertGreaterEqual(len(sent), 2, f"应当拆成多片，实际 {sent}")
        for value in sent:
            self.assertIsNotNone(value)
            parsed = local_proxy.parse_range(value)
            self.assertIsNotNone(parsed, value)
            self.assertIsNotNone(parsed[1], f"不该出现没有上界的请求: {value}")
        # 而且是从头开始、首尾相接、不重不漏地取的（内容断言已证明覆盖到末尾）
        self.assertEqual(local_proxy.parse_range(sent[0])[0], 0)
        for previous, current in zip(sent, sent[1:]):
            self.assertEqual(
                local_proxy.parse_range(current)[0],
                local_proxy.parse_range(previous)[1] + 1,
                f"分片必须首尾相接: {sent}",
            )

    def test_request_without_range_gets_a_plain_200(self) -> None:
        with LocalHTTPServer(size=20000) as origin:
            url = self.server.url_for(origin.url("/file.bin"))
            response = requests.get(url, timeout=10)
            sent = self._origin_ranges(origin)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("Content-Range"))
        self.assertEqual(response.headers.get("Content-Length"), "20000")
        self.assertEqual(response.content, origin.payload)
        for value in sent:
            self.assertIsNotNone(local_proxy.parse_range(value)[1])

    def test_open_ended_range_starts_midway(self) -> None:
        with LocalHTTPServer(size=20000) as origin:
            url = self.server.url_for(origin.url("/file.bin"))
            response = requests.get(url, headers={"Range": "bytes=5000-"}, timeout=10)
            sent = self._origin_ranges(origin)

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers.get("Content-Range"), "bytes 5000-19999/20000")
        self.assertEqual(response.headers.get("Content-Length"), "15000")
        self.assertEqual(response.content, origin.payload[5000:])
        self.assertEqual(local_proxy.parse_range(sent[0])[0], 5000)

    def test_huge_bounded_range_is_sliced_too(self) -> None:
        """`bytes=0-19999`（正好整个文件）和 `bytes=0-` 一样会被源站限速。"""
        with LocalHTTPServer(size=20000) as origin:
            url = self.server.url_for(origin.url("/file.bin"))
            response = requests.get(
                url, headers={"Range": "bytes=0-19999"}, timeout=10
            )
            sent = self._origin_ranges(origin)

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, origin.payload)
        self.assertGreaterEqual(len(sent), 2)

    def test_small_bounded_range_is_not_sliced(self) -> None:
        """明确的、不大的区间源站不限速，原样透传（少一层改写少一分风险）。"""
        with LocalHTTPServer(size=20000) as origin:
            url = self.server.url_for(origin.url("/file.bin"))
            response = requests.get(url, headers={"Range": "bytes=100-199"}, timeout=10)
            sent = self._origin_ranges(origin)

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, origin.payload[100:200])
        self.assertEqual(sent, ["bytes=100-199"])


class ProxyToggleTest(unittest.TestCase):
    """`proxy_url()` 是控制器的唯一入口，它的开关语义必须准确。"""

    def setUp(self) -> None:
        self._previous = S.get_bool(S.K_LOCAL_PROXY_ENABLED, False)

    def tearDown(self) -> None:
        S.settings().setValue(S.K_LOCAL_PROXY_ENABLED, self._previous)
        local_proxy.stop()

    def test_returns_empty_when_disabled(self) -> None:
        S.settings().setValue(S.K_LOCAL_PROXY_ENABLED, False)
        self.assertEqual(local_proxy.proxy_url("https://a.com/v.mp4"), "")

    def test_returns_url_when_enabled(self) -> None:
        S.settings().setValue(S.K_LOCAL_PROXY_ENABLED, True)
        url = local_proxy.proxy_url("https://a.com/v.mp4")
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertIn("/proxy?u=", url)

    def test_empty_for_hls_even_when_enabled(self) -> None:
        S.settings().setValue(S.K_LOCAL_PROXY_ENABLED, True)
        self.assertEqual(local_proxy.proxy_url("https://a.com/v.m3u8"), "")

    def test_describe_does_not_crash(self) -> None:
        local_proxy.stop()
        self.assertIn("未启动", local_proxy.describe())
        local_proxy.ensure_started(0)
        self.assertIn("代理", local_proxy.describe())


class PickHeaderTest(unittest.TestCase):
    """请求头字典的键名大小写不保证，取值必须大小写不敏感。"""

    def test_is_case_insensitive(self) -> None:
        for key in ("Referer", "referer", "REFERER", "rEfErEr"):
            with self.subTest(key=key):
                self.assertEqual(
                    local_proxy.pick_header({key: "https://a.com/"}, "Referer"),
                    "https://a.com/",
                )

    def test_missing_and_empty_values(self) -> None:
        self.assertEqual(local_proxy.pick_header(None, "Referer"), "")
        self.assertEqual(local_proxy.pick_header({}, "Referer"), "")
        self.assertEqual(local_proxy.pick_header({"Referer": ""}, "Referer"), "")
        self.assertEqual(local_proxy.pick_header({"Other": "x"}, "Referer"), "")

    def test_values_are_stripped(self) -> None:
        self.assertEqual(
            local_proxy.pick_header({"User-Agent": "  UA/1.0  "}, "User-Agent"), "UA/1.0"
        )


class ProxyUrlHeaderTest(unittest.TestCase):
    """`proxy_url(target, headers)` —— 解析器给的请求头要真的进到代理地址里。

    回归点：早期版本 `url_for` 只会带上"由目标地址推导的站点根"当 Referer，
    解析器从页面拿到的正确 Referer 被整个丢掉。对 B 站这类 CDN 域名与站点
    域名不同的地址，等于发了个错的 Referer，防盗链照样过不去。
    """

    def setUp(self) -> None:
        self._enabled = S.get_bool(S.K_LOCAL_PROXY_ENABLED, False)
        S.settings().setValue(S.K_LOCAL_PROXY_ENABLED, True)

    def tearDown(self) -> None:
        S.settings().setValue(S.K_LOCAL_PROXY_ENABLED, self._enabled)
        local_proxy.stop()

    def test_resolver_referer_reaches_the_origin(self) -> None:
        referer = "https://www.bilibili.com/video/BV1xx411c7mD"
        with LocalHTTPServer() as origin:
            url = local_proxy.proxy_url(origin.url("/file.bin"), {"Referer": referer})
            self.assertTrue(url)
            requests.get(url, timeout=10)
            headers = origin.last_headers("/file.bin")
        self.assertEqual(headers.get("Referer"), referer)

    def test_resolver_headers_are_case_insensitive(self) -> None:
        with LocalHTTPServer() as origin:
            url = local_proxy.proxy_url(
                origin.url("/file.bin"),
                {"referer": "https://lower.example/", "user-agent": "UA-Lower/2.0"},
            )
            requests.get(url, timeout=10)
            headers = origin.last_headers("/file.bin")
        self.assertEqual(headers.get("Referer"), "https://lower.example/")
        self.assertEqual(headers.get("User-Agent"), "UA-Lower/2.0")

    def test_no_headers_still_works(self) -> None:
        """直链用户没开解析，也没填 Referer —— 仍然要能走代理（开关语义）。"""
        with LocalHTTPServer() as origin:
            url = local_proxy.proxy_url(origin.url("/file.bin"))
            self.assertTrue(url)
            self.assertEqual(requests.get(url, timeout=10).status_code, 200)


if __name__ == "__main__":
    unittest.main()
