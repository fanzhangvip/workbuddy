"""网络可达性预检与 DNS 兜底（src/core/reachability.py）测试。

这里**不访问外网**：DoH 的应答用固定的 JSON 字符串验证解析逻辑，
真实连通性用本机的 `LocalHTTPServer` 与一个确定关着的端口验证。

重点覆盖三件容易出错的事：
  1. `socket.getaddrinfo` 改写只影响登记过的主机名，其余原样放行；
  2. `probe()` 对"不适用"的地址返回 None，而不是返回"不可达"；
  3. "DNS 有应答" != "连得上"——这正是本模块存在的理由。

运行:
    ./.venv/Scripts/python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import reachability as RC  # noqa: E402
from tests.httpfixture import LocalHTTPServer  # noqa: E402

#: 一个确认没有服务在监听的端口（关着的端口上 TCP 会立刻被拒，不用等超时）
CLOSED_PORT = 9


class TestDohParsing(unittest.TestCase):
    """DoH 应答解析：只认 A / AAAA，坏数据一律返回空元组。"""

    def test_parses_a_records(self) -> None:
        payload = (
            b'{"Status":0,"Answer":['
            b'{"name":"www.youtube.com.","type":1,"data":"142.251.150.4"},'
            b'{"name":"www.youtube.com.","type":1,"data":"142.251.151.4"}]}'
        )
        self.assertEqual(
            RC._parse_doh_answers(payload),
            ("142.251.150.4", "142.251.151.4"),
        )

    def test_accepts_aaaa_and_dedupes(self) -> None:
        payload = (
            b'{"Answer":['
            b'{"type":28,"data":"2606:4700::1"},'
            b'{"type":1,"data":"1.2.3.4"},'
            b'{"type":1,"data":"1.2.3.4"}]}'
        )
        self.assertEqual(RC._parse_doh_answers(payload), ("2606:4700::1", "1.2.3.4"))

    def test_ignores_non_address_records(self) -> None:
        """CNAME（type=5）不是地址，混进来会让 TCP 探测解析出一个域名。"""
        payload = b'{"Answer":[{"type":5,"data":"example.cdn.net."}]}'
        self.assertEqual(RC._parse_doh_answers(payload), ())

    def test_bad_payloads_are_empty(self) -> None:
        for payload in (b"", b"not json", b"[]", b'{"Status":0}', b'{"Answer":"x"}'):
            with self.subTest(payload=payload):
                self.assertEqual(RC._parse_doh_answers(payload), ())


class TestUrlHelpers(unittest.TestCase):
    def test_host_port_of(self) -> None:
        cases = {
            "https://a.com/x": ("a.com", 443),
            "http://a.com/x": ("a.com", 80),
            "http://a.com:8080/x": ("a.com", 8080),
            "https://A.COM": ("a.com", 443),
            "rtsp://a.com/stream": ("a.com", 0),   # 不在默认端口表里
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(RC.host_port_of(url), expected)

    def test_host_port_of_garbage(self) -> None:
        self.assertEqual(RC.host_port_of(""), ("", 0))
        self.assertEqual(RC.host_port_of("http://"), ("", 0))
        # 端口写坏了就退回该协议的默认端口（80），别把整条地址判成不可用
        self.assertEqual(RC.host_port_of("http://a.com:notaport/x"), ("a.com", 80))

    def test_is_probable_only_for_http(self) -> None:
        self.assertTrue(RC.is_probable("https://a.com/x"))
        self.assertFalse(RC.is_probable("rtsp://a.com/x"))
        self.assertFalse(RC.is_probable("file:///a.mp4"))
        self.assertFalse(RC.is_probable("a.com/x"))


class TestProbeApplicability(unittest.TestCase):
    """不适用时必须返回 None——返回"不可达"会让调用方跳过本该做的解析。"""

    def test_non_http_is_none(self) -> None:
        self.assertIsNone(RC.probe("rtsp://1.2.3.4:554/live"))
        self.assertIsNone(RC.probe("file:///tmp/a.mp4"))
        self.assertIsNone(RC.probe(""))

    def test_loopback_is_none_by_default(self) -> None:
        for url in ("http://127.0.0.1:9/x", "http://localhost:9/x",
                    "http://127.0.0.2:9/x", "http://[::1]:9/x"):
            with self.subTest(url=url):
                self.assertIsNone(RC.probe(url, use_doh=False))

    def test_loopback_can_be_forced(self) -> None:
        result = RC.probe(f"http://127.0.0.1:{CLOSED_PORT}/x",
                          use_doh=False, skip_loopback=False, use_cache=False)
        self.assertIsNotNone(result)
        self.assertFalse(result.tcp_ok)
        self.assertTrue(result.blocked)


class TestProbeAgainstLocalServer(unittest.TestCase):
    """真连一次本机服务：证明"通"与"不通"都判得出来。"""

    def test_reachable(self) -> None:
        with LocalHTTPServer() as server:
            result = RC.probe(server.url("/status"), use_doh=False,
                              skip_loopback=False, use_cache=False)
            self.assertIsNotNone(result)
            self.assertTrue(result.tcp_ok)
            self.assertFalse(result.blocked)
            self.assertIn("网络可达", result.headline)

    def test_closed_port_is_blocked(self) -> None:
        url = f"http://127.0.0.1:{CLOSED_PORT}/x"
        result = RC.probe(url, use_doh=False, skip_loopback=False, use_cache=False)
        self.assertIsNotNone(result)
        self.assertFalse(result.tcp_ok)
        self.assertTrue(result.blocked)
        # 结论必须能让用户照着做，不能只说"失败"
        self.assertTrue(result.advice)
        self.assertIn("结论", result.report())

    def test_result_is_cached_by_host_port(self) -> None:
        with LocalHTTPServer() as server:
            url = server.url("/status")
            first = RC.probe(url, use_doh=False, skip_loopback=False)
            second = RC.probe(url, use_doh=False, skip_loopback=False)
            self.assertIs(first, second)   # 命中缓存，连对象都是同一个
            RC.clear_cache()
            third = RC.probe(url, use_doh=False, skip_loopback=False)
            self.assertIsNot(first, third)


class TestDnsOverride(unittest.TestCase):
    """`socket.getaddrinfo` 定点改写：只动登记过的主机名。"""

    #: 用 .invalid 顶级域（RFC 2606 保留，保证不可能真的解析出来）
    FAKE_HOST = "unit-test-host.invalid"
    OTHER_HOST = "unit-test-other.invalid"

    def setUp(self) -> None:
        RC.clear_overrides()

    def tearDown(self) -> None:
        RC.clear_overrides()

    @staticmethod
    def _addresses(host: str, port: int = 80) -> list[str]:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        return sorted({info[4][0] for info in infos})

    def test_unregistered_host_still_fails(self) -> None:
        """没登记过的假域名应当照常解析失败——改写不该变成"万能解析"。"""
        with self.assertRaises(OSError):
            socket.getaddrinfo(self.FAKE_HOST, 80)

    def test_registered_host_resolves_to_override(self) -> None:
        RC.register_override(self.FAKE_HOST, ("127.0.0.1",))
        self.assertEqual(self._addresses(self.FAKE_HOST), ["127.0.0.1"])

    def test_registration_is_case_insensitive(self) -> None:
        RC.register_override(self.FAKE_HOST.upper(), ("127.0.0.1",))
        self.assertEqual(self._addresses(self.FAKE_HOST), ["127.0.0.1"])

    def test_other_hosts_are_untouched(self) -> None:
        """只改一个域名时，别的域名必须没有任何变化。"""
        RC.register_override(self.FAKE_HOST, ("127.0.0.1",))
        with self.assertRaises(OSError):
            socket.getaddrinfo(self.OTHER_HOST, 80)

    def test_clearing_restores_system_resolution(self) -> None:
        RC.register_override(self.FAKE_HOST, ("127.0.0.1",))
        self.assertEqual(self._addresses(self.FAKE_HOST), ["127.0.0.1"])
        RC.clear_overrides()
        with self.assertRaises(OSError):
            socket.getaddrinfo(self.FAKE_HOST, 80)

    def test_repeated_registration_does_not_stack_patches(self) -> None:
        """补丁必须只装一次：重复装会把上一次的包装再包一层，越包越深。"""
        for _ in range(5):
            RC.register_override(self.FAKE_HOST, ("127.0.0.1",))
        self.assertEqual(self._addresses(self.FAKE_HOST), ["127.0.0.1"])
        RC.register_override(self.OTHER_HOST, ("127.0.0.2",))
        self.assertEqual(self._addresses(self.OTHER_HOST), ["127.0.0.2"])

    def test_replacing_override(self) -> None:
        RC.register_override(self.FAKE_HOST, ("127.0.0.1",))
        RC.register_override(self.FAKE_HOST, ("127.0.0.2",))
        self.assertEqual(self._addresses(self.FAKE_HOST), ["127.0.0.2"])

    def test_empty_registration_is_ignored(self) -> None:
        RC.register_override(self.FAKE_HOST, ())
        self.assertEqual(RC.registered_overrides(), {})

    def test_registered_overrides_snapshot(self) -> None:
        RC.register_override(self.FAKE_HOST, ("127.0.0.1",))
        self.assertEqual(RC.registered_overrides(), {self.FAKE_HOST: ("127.0.0.1",)})


class TestTcpAny(unittest.TestCase):
    """并发探测：任一通即算通，全不通要如实返回 False。"""

    def test_first_hit_wins(self) -> None:
        with LocalHTTPServer() as server:
            self.assertTrue(RC._tcp_any(
                ["192.0.2.1", "127.0.0.1"], server.port, 1.0, limit=2
            ))

    def test_all_dead(self) -> None:
        self.assertFalse(RC._tcp_any(["127.0.0.2", "127.0.0.3"], CLOSED_PORT, 0.5))

    def test_empty(self) -> None:
        self.assertFalse(RC._tcp_any([], 443, 0.5))

    def test_dedupes_addresses(self) -> None:
        """同一个地址传来传去不该被当成多个目标重复探。"""
        with LocalHTTPServer() as server:
            self.assertTrue(RC._tcp_any(
                ["127.0.0.1", "127.0.0.1", "127.0.0.1"], server.port, 1.0
            ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
