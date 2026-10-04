"""VPN / 代理体检（`src/utils/proxy_check.py`）。

这一层的价值全在"结论要能指导操作"，所以测试重点不是"函数返回了什么"，
而是**每种失败组合给没给对方向**：

  · 有代理、直连不通、经代理通 → 代理是好的，保持现状；
  · 直连通、经代理不通       → 是代理配置错了（填错端口最典型）；
  · 两边都不通但本机有网     → 问题在代理链路；
  · 两边都不通且本机也没网   → 先修网络，别在代理上打转。

全部用桩，不联网。真实探测只由设置面板的「测试连通性」按钮和
`tools/vpn_check.py` 触发。
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils import net_proxy as NP  # noqa: E402
from src.utils import proxy_check as PC  # noqa: E402

SETTING_HIT = NP.ProxyHit(proxy="http://127.0.0.1:7897", source=NP.SOURCE_SETTING)


def _probe(status: int = 200, *, proxy: str = "", error: str = "", ms: int = 100) -> PC.Probe:
    return PC.Probe(
        target="https://www.youtube.com/",
        proxy=proxy,
        status=status,
        ms=ms,
        error=error,
    )


class Failing(Exception):
    """模拟网络异常。"""


# ---------------------------------------------------------------- 来源


class SourcesTest(unittest.TestCase):
    def test_three_sources_in_priority_order(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            NP, "system_proxy", return_value=""
        ):
            items = PC.sources("http://127.0.0.1:7897")
        self.assertEqual([i.key for i in items], ["settings", "env", "system"])
        self.assertTrue(items[0].configured, "设置里填了就该算已配置")
        self.assertFalse(items[1].configured)
        self.assertFalse(items[2].configured)

    def test_blank_setting_is_not_configured(self) -> None:
        """只有一个空格不能当成"配了代理"——那是用户清空输入框的常见残留。"""
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            NP, "system_proxy", return_value=""
        ):
            items = PC.sources("   ")
        self.assertFalse(items[0].configured)

    def test_env_and_system_are_read(self) -> None:
        with mock.patch.dict(
            os.environ, {"https_proxy": "http://127.0.0.1:8080"}, clear=True
        ), mock.patch.object(NP, "system_proxy", return_value="http://127.0.0.1:7897"):
            items = PC.sources("")
        self.assertEqual(items[1].value, "http://127.0.0.1:8080")
        self.assertEqual(items[2].value, "http://127.0.0.1:7897")
        self.assertIn("Clash", items[2].role, "界面文案要指出系统代理是谁写的")


# ---------------------------------------------------------------- 生效判定


class ActiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {}, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_settings_wins(self) -> None:
        with mock.patch.object(NP, "system_proxy", return_value="http://sys:1"):
            hit = PC.active_hit("http://app:2")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.proxy, "http://app:2")
        self.assertEqual(hit.source, NP.SOURCE_SETTING)

    def test_none_when_nothing_configured(self) -> None:
        with mock.patch.object(NP, "system_proxy", return_value=""):
            self.assertIsNone(PC.active_hit(""))

    def test_describe_without_proxy_points_at_tun_mode(self) -> None:
        """没代理时最可能的真相是"用了 TUN 模式"，文案必须把这条说出来。"""
        with mock.patch.object(NP, "system_proxy", return_value=""):
            text = PC.describe_active("")
        self.assertIn("没有检测到代理", text)
        self.assertIn("TUN", text)

    def test_describe_with_proxy_states_source(self) -> None:
        text = PC.describe_active("http://127.0.0.1:7897")
        self.assertIn("当前生效", text)
        self.assertIn("设置中指定的代理", text)
        self.assertIn("http://127.0.0.1:7897", text)

    def test_describe_warns_about_socks_without_pysocks(self) -> None:
        """SOCKS 少了 PySocks 会抛一句很难懂的异常，这里必须提前说。"""
        with mock.patch.object(PC, "socks_ready", return_value=False):
            text = PC.describe_active("socks5://127.0.0.1:7891")
        self.assertIn("SOCKS", text)
        self.assertIn("PySocks", text)

    def test_socks_without_pysocks_says_holistic_warning_in_report(self) -> None:
        with mock.patch.object(PC, "socks_ready", return_value=False), mock.patch.object(
            PC, "probe", side_effect=lambda *a, **kw: _probe()
        ):
            report = PC.check(app_proxy="socks5://127.0.0.1:7891")
        self.assertTrue(any("PySocks" in note for note in report.notes))


# ---------------------------------------------------------------- 单次探测


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status_code = status
        self.closed = False

    def close(self) -> None:
        self.closed = True


class ProbeTest(unittest.TestCase):
    def test_success(self) -> None:
        with mock.patch("requests.get", return_value=_FakeResponse(200)) as get:
            result = PC.probe("https://x/", proxy="http://p:1", timeout=3)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, 200)
        kwargs = get.call_args.kwargs
        self.assertEqual(kwargs["proxies"], {"http": "http://p:1", "https": "http://p:1"})
        self.assertEqual(kwargs["timeout"], 3)

    def test_direct_probe_suppresses_env_proxy(self) -> None:
        """直连探测必须显式压住环境变量里的代理，否则测出来的不是"直连"。"""
        with mock.patch("requests.get", return_value=_FakeResponse(200)) as get:
            PC.probe("https://x/")
        self.assertEqual(
            get.call_args.kwargs["proxies"], {"http": None, "https": None}
        )

    def test_body_is_closed_not_downloaded(self) -> None:
        """只读响应头就断开：这是体检不是下载，绝不能把大文件拖下来。"""
        response = _FakeResponse(200)
        with mock.patch("requests.get", return_value=response):
            PC.probe("https://x/")
        self.assertTrue(response.closed)
        self.assertIsNotNone(getattr(response, "closed", None))

    def test_stream_is_requested(self) -> None:
        with mock.patch("requests.get", return_value=_FakeResponse(200)) as get:
            PC.probe("https://x/")
        self.assertTrue(get.call_args.kwargs["stream"])

    def test_client_error_is_not_ok_but_says_answered(self) -> None:
        with mock.patch("requests.get", return_value=_FakeResponse(403)):
            result = PC.probe("https://x/")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 403)
        self.assertIn("有应答", result.line())

    def test_gateway_error_is_flagged(self) -> None:
        with mock.patch("requests.get", return_value=_FakeResponse(502)):
            result = PC.probe("https://x/", proxy="http://p:1")
        self.assertTrue(result.gateway_error)
        with mock.patch("requests.get", return_value=_FakeResponse(200)):
            self.assertFalse(PC.probe("https://x/").gateway_error)

    def test_exception_becomes_failed_probe(self) -> None:
        with mock.patch("requests.get", side_effect=TimeoutError("timed out")):
            result = PC.probe("https://x/", proxy="http://p:1")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 0)
        self.assertIn("无应答", result.error)

    def test_labels(self) -> None:
        self.assertEqual(_probe().label, "直连")
        self.assertEqual(_probe(proxy="http://p:1").label, "经代理 http://p:1")

    def test_explain_translates_common_failures(self) -> None:
        cases = [
            (TimeoutError("timed out"), "无应答"),
            (OSError("getaddrinfo failed"), "域名解析失败"),
            (Failing("Missing dependencies for SOCKS support"), "PySocks"),
        ]
        for exc, expected in cases:
            with self.subTest(exc=type(exc).__name__):
                self.assertIn(expected, PC._explain(exc, "http://p:1"))  # noqa: SLF001

    def test_explain_keeps_the_exception_name_for_troubleshooting(self) -> None:
        """人话在前、技术名在后：用户看得懂，排查时也还认得出是哪一类异常。"""
        text = PC._explain(TimeoutError("timed out"), "")
        self.assertIn("TimeoutError", text)

    def test_explain_truncates_unknown_errors(self) -> None:
        """requests 的原始异常动辄两三百字符，整段摆到界面上等于没说。"""
        exc = RuntimeError("x" * 400)
        text = PC._explain(exc, "")  # noqa: SLF001
        self.assertLess(len(text), 160)
        self.assertTrue(text.endswith("…"))

    def test_explain_proxy_error_points_at_the_port(self) -> None:
        text = PC._explain(Failing("ProxyError: cannot connect to proxy"), "http://p:9")  # noqa: SLF001
        self.assertIn("http://p:9", text)


# ---------------------------------------------------------------- 结论


class ConcludeTest(unittest.TestCase):
    """每种失败组合都要给出**方向正确**的下一步。

    这类诊断最容易犯的错是"笼统地说不通"，用户拿到的信息等于零。
    """

    def _run(self, *, hit, direct: PC.Probe, through: PC.Probe | None, fallback=None):
        with mock.patch.object(PC, "active_hit", return_value=hit), mock.patch.object(
            PC, "probe", side_effect=lambda target, **kw: (
                fallback if target == PC.FALLBACK_TARGET else (through if kw.get("proxy") else direct)
            )
        ):
            return PC.check(app_proxy="http://app:1" if hit else "")

    def test_direct_ok_without_proxy(self) -> None:
        report = self._run(hit=None, direct=_probe(), through=None)
        self.assertTrue(report.ok)
        self.assertIn("直连可用", report.verdict)
        self.assertIn("TUN", report.advice)

    def test_blocked_target_needs_proxy_but_none_configured(self) -> None:
        report = self._run(
            hit=None, direct=_probe(0, error="timed out"), through=None, fallback=_probe()
        )
        self.assertFalse(report.ok)
        self.assertIn("没有检测到任何代理", report.verdict)
        self.assertIn("系统代理", report.advice)

    def test_proxy_is_the_only_way_and_it_works(self) -> None:
        report = self._run(
            hit=SETTING_HIT,
            direct=_probe(0, error="timed out"),
            through=_probe(200, proxy=SETTING_HIT.proxy),
        )
        self.assertTrue(report.ok)
        self.assertIn("代理可用", report.verdict)

    def test_proxy_breaks_what_direct_could_do(self) -> None:
        """填错端口最典型的症状：本来是好的，填了代理反而不通。"""
        report = self._run(
            hit=SETTING_HIT, direct=_probe(), through=_probe(0, error="ProxyError")
        )
        self.assertIn("⚠", report.verdict)
        self.assertIn("端口", report.advice)
        self.assertIn("清空", report.advice)

    def test_gateway_error_blames_the_node_not_the_port(self) -> None:
        """502/503/504 是代理自己回的，方向应该指向"换节点"，不是"改端口"。"""
        report = self._run(
            hit=SETTING_HIT, direct=_probe(), through=_probe(502, proxy=SETTING_HIT.proxy)
        )
        self.assertIn("换", report.advice)
        self.assertIn("502", report.advice)

    def test_both_fail_but_machine_has_internet(self) -> None:
        report = self._run(
            hit=SETTING_HIT,
            direct=_probe(0, error="timed out"),
            through=_probe(0, error="timed out", proxy=SETTING_HIT.proxy),
            fallback=_probe(200),
        )
        self.assertIn("本机有网", report.verdict)

    def test_both_fail_and_machine_is_offline(self) -> None:
        report = self._run(
            hit=SETTING_HIT,
            direct=_probe(0, error="timed out"),
            through=_probe(0, error="timed out", proxy=SETTING_HIT.proxy),
            fallback=_probe(0, error="timed out"),
        )
        self.assertIn("都不通", report.verdict)

    def test_direct_fail_and_offline_without_proxy(self) -> None:
        report = self._run(
            hit=None, direct=_probe(0, error="timed out"), through=None, fallback=_probe(0)
        )
        self.assertIn("网络本身", report.verdict)

    def test_report_text_contains_every_probe(self) -> None:
        report = self._run(
            hit=SETTING_HIT,
            direct=_probe(0, error="timed out"),
            through=_probe(200, proxy=SETTING_HIT.proxy),
        )
        text = report.text()
        self.assertIn("直连", text)
        self.assertIn("经代理 http://127.0.0.1:7897", text)
        self.assertIn("结论", text)


# ---------------------------------------------------------------- 设置面板


class SettingsDialogVpnTest(unittest.TestCase):
    """设置里那一栏必须能自己说清状态，并能真的验一次。"""

    KEYS = ("network/proxy",)

    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from src.utils import settings as S

        self.S = S
        self._saved = S.settings().value(S.K_PROXY, None)
        self._env = mock.patch.dict(os.environ, {}, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)

    def tearDown(self) -> None:
        store = self.S.settings()
        if self._saved is None:
            store.remove(self.S.K_PROXY)
        else:
            store.setValue(self.S.K_PROXY, self._saved)

    def _dialog(self):
        from src.ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog()
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_detect_fills_env_proxy(self) -> None:
        with mock.patch.dict(
            os.environ, {"https_proxy": "http://127.0.0.1:7897"}, clear=True
        ), mock.patch.object(NP, "system_proxy", return_value=""):
            dialog = self._dialog()
            dialog._detect_system_proxy()  # noqa: SLF001
        self.assertEqual(dialog.edit_proxy.text(), "http://127.0.0.1:7897")
        self.assertIn("环境变量", dialog.label_net_status.text())

    def test_detect_falls_back_to_system_proxy(self) -> None:
        with mock.patch.object(NP, "system_proxy", return_value="http://127.0.0.1:7897"):
            dialog = self._dialog()
            dialog._detect_system_proxy()  # noqa: SLF001
        self.assertEqual(dialog.edit_proxy.text(), "http://127.0.0.1:7897")
        self.assertIn("系统代理", dialog.label_net_status.text())

    def test_detect_when_nothing_found_does_not_leave_field_empty_silently(self) -> None:
        """"没检测到"是正常结论（可能是 TUN 模式），必须说出来而不是静默。"""
        with mock.patch.object(NP, "system_proxy", return_value=""):
            dialog = self._dialog()
            dialog._detect_system_proxy()  # noqa: SLF001
        self.assertIn("TUN", dialog.label_net_status.text())

    def test_typing_address_refreshes_status_immediately(self) -> None:
        """填了地址就该立刻看到"当前生效"变了，不用等保存。"""
        dialog = self._dialog()
        dialog.edit_proxy.setText("http://127.0.0.1:7897")
        self.assertIn("当前生效", dialog.label_net_status.text())
        self.assertIn("7897", dialog.label_net_status.text())

    def test_clearing_address_returns_to_auto_detect_text(self) -> None:
        with mock.patch.object(NP, "system_proxy", return_value=""):
            dialog = self._dialog()
            dialog.edit_proxy.setText("http://127.0.0.1:7897")
            dialog.edit_proxy.setText("")
            self.assertIn("没有检测到代理", dialog.label_net_status.text())

    def test_test_button_runs_in_background_and_reports(self) -> None:
        """测完要恢复按钮、把结论写出来——不能永远停在"正在测试"。"""
        fake = PC.Report(
            target=PC.DEFAULT_TARGET,
            app_proxy="",
            hit=None,
            direct=_probe(),
            verdict="直连可用，当前没有代理在生效。",
            advice="保持现状。",
        )
        with mock.patch.object(PC, "check", return_value=fake), mock.patch.object(
            NP, "system_proxy", return_value=""
        ):
            dialog = self._dialog()
            dialog._test_network()  # noqa: SLF001
            self.assertFalse(dialog.btn_net_test.isEnabled())
            deadline = time.monotonic() + 5
            while not dialog._net_result and time.monotonic() < deadline:  # noqa: SLF001
                time.sleep(0.01)
            dialog._poll_net_test()  # noqa: SLF001

        self.assertTrue(dialog.btn_net_test.isEnabled(), "测完必须把按钮放回来")
        self.assertIn("直连可用", dialog.label_net_result.text())
        self.assertFalse(dialog._net_poll.isActive())  # noqa: SLF001

    def test_test_failure_does_not_crash_the_dialog(self) -> None:
        """体检抛异常（代理软件刚被关掉之类）不能让设置面板崩。"""
        with mock.patch.object(PC, "check", side_effect=RuntimeError("boom")), mock.patch.object(
            NP, "system_proxy", return_value=""
        ):
            dialog = self._dialog()
            dialog._test_network()  # noqa: SLF001
            deadline = time.monotonic() + 5
            while not dialog._net_result and time.monotonic() < deadline:  # noqa: SLF001
                time.sleep(0.01)
            dialog._poll_net_test()  # noqa: SLF001

        self.assertTrue(dialog.btn_net_test.isEnabled())
        self.assertIn("测试失败", dialog.label_net_result.text())

    def test_value_is_collected_for_saving(self) -> None:
        dialog = self._dialog()
        dialog.edit_proxy.setText(" socks5://127.0.0.1:7891 ")
        self.assertEqual(
            dialog._collect()[self.S.K_PROXY],  # noqa: SLF001
            "socks5://127.0.0.1:7891",
            "存盘前要去掉首尾空格，否则 requests 会拿到非法地址",
        )

    def test_repeated_test_clicks_do_not_stack_threads(self) -> None:
        dialog = self._dialog()
        dialog._net_poll.start()  # noqa: SLF001 - 假装上一次还在跑
        with mock.patch.object(PC, "check") as check:
            dialog._test_network()  # noqa: SLF001
        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
