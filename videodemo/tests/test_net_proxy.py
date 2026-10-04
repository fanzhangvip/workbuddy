"""代理探测：预检该不该跑，取决于有没有代理在生效。

这一层的价值全在"别误判"：只要有一条代理在生效，裸 TCP 的直连结论就不能
拿去判"这个站点不可达"。历史上漏认 Windows 系统代理，导致开着 Clash 时
YouTube 被误杀成"连不上"。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils import net_proxy as NP  # noqa: E402


class EnvProxyTest(unittest.TestCase):
    def test_no_proxy_at_all(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(NP.env_proxy(), "")

    def test_https_wins_over_http_and_all(self) -> None:
        env = {
            "all_proxy": "socks5://127.0.0.1:1080",
            "http_proxy": "http://127.0.0.1:8080",
            "https_proxy": "http://127.0.0.1:7897",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(NP.env_proxy(), "http://127.0.0.1:7897")

    def test_http_used_when_https_absent(self) -> None:
        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:8080"}, clear=True):
            self.assertEqual(NP.env_proxy(), "http://127.0.0.1:8080")

    def test_uppercase_is_recognised(self) -> None:
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:8080"}, clear=True):
            self.assertEqual(NP.env_proxy(), "http://127.0.0.1:8080")

    def test_empty_value_counts_as_absent(self) -> None:
        """显式设成空串是"关掉代理"的常见写法，不能当成有代理。"""
        with mock.patch.dict(os.environ, {"https_proxy": "   "}, clear=True):
            self.assertEqual(NP.env_proxy(), "")


class NormalizeTest(unittest.TestCase):
    def test_bare_host_port_gets_scheme(self) -> None:
        self.assertEqual(NP.normalize("127.0.0.1:7897"), "http://127.0.0.1:7897")

    def test_scheme_is_kept(self) -> None:
        self.assertEqual(NP.normalize("http://127.0.0.1:7897"), "http://127.0.0.1:7897")

    def test_per_protocol_form_prefers_https(self) -> None:
        raw = "http=127.0.0.1:8080;https=127.0.0.1:7897"
        self.assertEqual(NP.normalize(raw), "http://127.0.0.1:7897")

    def test_per_protocol_form_falls_back_to_http(self) -> None:
        self.assertEqual(NP.normalize("http=127.0.0.1:8080"), "http://127.0.0.1:8080")

    def test_empty(self) -> None:
        self.assertEqual(NP.normalize(""), "")
        self.assertEqual(NP.normalize("   "), "")


class HostOfTest(unittest.TestCase):
    def test_with_scheme(self) -> None:
        self.assertEqual(NP.host_of("https://youtu.be/x"), "youtu.be")

    def test_without_scheme(self) -> None:
        self.assertEqual(NP.host_of("127.0.0.1:9/nothing.mp4"), "127.0.0.1")

    def test_host_is_lowercased(self) -> None:
        self.assertEqual(NP.host_of("https://WwW.YouTube.COM/x"), "www.youtube.com")

    def test_empty(self) -> None:
        self.assertEqual(NP.host_of(""), "")


class ProxyForTest(unittest.TestCase):
    """优先级：手填 > 环境变量 > 系统代理。"""

    def setUp(self) -> None:
        # 默认把三个来源都清干净，单个用例只打开自己要测的那个
        patcher = mock.patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("system_proxy", "env_proxy"):
            p = mock.patch.object(NP, name, return_value="")
            p.start()
            self.addCleanup(p.stop)

    def test_nothing_in_effect(self) -> None:
        self.assertIsNone(NP.proxy_for("https://youtu.be/x"))

    def test_app_setting_wins_over_env_and_system(self) -> None:
        with mock.patch.object(NP, "env_proxy", return_value="http://127.0.0.1:1111"), \
             mock.patch.object(NP, "system_proxy", return_value="http://127.0.0.1:2222"):
            hit = NP.proxy_for("https://youtu.be/x", app_proxy="http://127.0.0.1:3333")
        assert hit is not None
        self.assertEqual(hit.proxy, "http://127.0.0.1:3333")
        self.assertEqual(hit.source, NP.SOURCE_SETTING)

    def test_env_proxy_is_reported_as_env(self) -> None:
        with mock.patch.object(NP, "env_proxy", return_value="http://127.0.0.1:1111"):
            hit = NP.proxy_for("https://youtu.be/x")
        assert hit is not None
        self.assertEqual(hit.proxy, "http://127.0.0.1:1111")
        self.assertEqual(hit.source, NP.SOURCE_ENV)

    def test_system_proxy_is_reported_as_system(self) -> None:
        """Clash 的"系统代理"开关走的就是这条路，漏了它就会误杀 YouTube。"""
        with mock.patch.object(NP, "system_proxy", return_value="http://127.0.0.1:7897"):
            hit = NP.proxy_for("https://youtu.be/x")
        assert hit is not None
        self.assertEqual(hit.proxy, "http://127.0.0.1:7897")
        self.assertEqual(hit.source, NP.SOURCE_SYSTEM)

    def test_env_beats_system(self) -> None:
        with mock.patch.object(NP, "env_proxy", return_value="http://127.0.0.1:1111"), \
             mock.patch.object(NP, "system_proxy", return_value="http://127.0.0.1:2222"):
            hit = NP.proxy_for("https://youtu.be/x")
        assert hit is not None
        self.assertEqual(hit.source, NP.SOURCE_ENV)

    def test_no_proxy_host_still_gets_probed(self) -> None:
        """no_proxy 里的主机确实不走代理，预检要照跑。"""
        with mock.patch.object(NP, "env_proxy", return_value="http://127.0.0.1:1111"), \
             mock.patch.object(NP, "_bypassed", return_value=True):
            self.assertIsNone(NP.proxy_for("https://youtu.be/x"))

    def test_address_without_host_is_not_a_hit(self) -> None:
        with mock.patch.object(NP, "system_proxy", return_value="http://127.0.0.1:7897"):
            self.assertIsNone(NP.proxy_for(""))


class SystemProxyTest(unittest.TestCase):
    def test_non_windows_has_no_system_proxy(self) -> None:
        with mock.patch.object(NP.sys, "platform", "linux"):
            self.assertEqual(NP.system_proxy(), "")


if __name__ == "__main__":
    unittest.main()
