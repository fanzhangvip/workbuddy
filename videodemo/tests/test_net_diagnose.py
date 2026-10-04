"""net_diagnose 的单元测试。

全部使用本地资源（临时文件 + 本地假服务器），不依赖外网，可在离线环境运行。
"""

from __future__ import annotations

import contextlib
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.net_diagnose import diagnose  # noqa: E402


@contextlib.contextmanager
def _no_proxy():
    """强制走直连分支。

    开发/CI 环境里常带着 HTTP_PROXY 或系统代理，会让这些"直连行为"的用例
    落到代理分支上去，测的东西就不是它本来想测的了。
    """
    with mock.patch("src.utils.net_proxy.proxy_for", return_value=None):
        yield


@contextlib.contextmanager
def _with_proxy(proxy: str = "http://127.0.0.1:7897", source: str = "系统代理"):
    """假装有一条代理对该地址生效。"""
    hit = SimpleNamespace(proxy=proxy, source=source)
    with mock.patch("src.utils.net_proxy.proxy_for", return_value=hit):
        yield hit


def _free_port() -> int:
    """拿一个当前没有监听者的端口。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _FakeServer:
    """只对第一条请求回一段固定内容的 TCP 服务器。"""

    def __init__(self, response: bytes, *, read_bytes: bool = True) -> None:
        self.response = response
        self.read_bytes = read_bytes
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(4)
        self._sock.settimeout(0.3)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> _FakeServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop = True
        self._thread.join(timeout=2)
        self._sock.close()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            try:
                if self.read_bytes:
                    conn.settimeout(1.0)
                    try:
                        conn.recv(4096)
                    except OSError:
                        pass
                if self.response:
                    conn.sendall(self.response)
            except OSError:
                pass
            finally:
                conn.close()


class TestLocalFiles(unittest.TestCase):
    def test_existing_local_file(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
            fh.write(b"\x00" * 1024)
            path = fh.name
        try:
            result = diagnose(path)
            self.assertTrue(result.is_local)
            self.assertTrue(result.reachable)
            self.assertIn("本地文件", result.stages[0].detail)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_missing_local_file_is_reported_as_missing_not_bad_host(self) -> None:
        """不存在的相对路径必须报"文件不存在"，而不是"解析不出主机名"。"""
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.mp4"
            result = diagnose(str(missing))
            self.assertTrue(result.is_local)
            self.assertFalse(result.reachable)
            self.assertIn("不存在", result.stages[0].detail)
            self.assertIn("不存在", result.headline)

    def test_file_url_is_understood(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
            path = Path(fh.name)
        try:
            result = diagnose(path.as_uri())
            self.assertTrue(result.is_local)
            self.assertTrue(result.reachable)
        finally:
            path.unlink(missing_ok=True)


class TestNetworkFailures(unittest.TestCase):
    def test_dns_failure(self) -> None:
        # .invalid 是 RFC 2606 保留的顶级域，保证解析失败
        with _no_proxy():
            result = diagnose("http://no-such-host.invalid/a.mp4", timeout=2.0)
        self.assertFalse(result.reachable)
        self.assertEqual(result.stages[1].name, "DNS 解析")
        self.assertFalse(result.stages[1].ok)
        self.assertIn("无法解析", result.headline)

    def test_connection_refused(self) -> None:
        port = _free_port()
        result = diagnose(f"http://127.0.0.1:{port}/a.mp4", timeout=3.0)
        self.assertFalse(result.reachable)
        self.assertIn("拒绝", result.headline)
        self.assertIn("端口", " ".join(result.advice))

    def test_unroutable_address_fails(self) -> None:
        # 203.0.113.0/24 是保留的测试网段，不会有真实主机应答
        with _no_proxy():
            result = diagnose("http://203.0.113.1:80/a.mp4", timeout=1.0)
        self.assertFalse(result.reachable)

    def test_first_failure_points_at_network_stage(self) -> None:
        port = _free_port()
        result = diagnose(f"rtsp://127.0.0.1:{port}/x", timeout=3.0)
        failure = result.first_failure
        self.assertIsNotNone(failure)
        self.assertEqual(failure.name, "TCP 连接")


class TestProxyAwareness(unittest.TestCase):
    """有代理在生效时，不能拿裸 TCP 的结论下判断。

    真实踩过的坑：媒体地址已经改走本地反向代理、画面声音都在正常播放，
    诊断却仍去做裸 TCP 探测，于是把一条好地址报成"服务器已下线、地址可能已失效"。
    """

    TARGET = "https://rr1---sn-x.googlevideo.com/videoplayback?expire=1"

    def test_proxy_branch_skips_direct_stages(self) -> None:
        seen: dict = {}

        class _Resp:
            status_code = 206

            def close(self) -> None:  # pragma: no cover - 只是接口
                pass

        def fake_get(url, **kwargs):
            seen["url"] = url
            seen["proxies"] = kwargs.get("proxies")
            return _Resp()

        with _with_proxy(), mock.patch("requests.get", side_effect=fake_get):
            result = diagnose(self.TARGET, timeout=2.0)

        names = [s.name for s in result.stages]
        self.assertNotIn("TCP 连接", names)      # 不该再做直连探测
        self.assertNotIn("DNS 解析", names)
        self.assertIn("经代理探测", names)
        self.assertTrue(result.reachable)
        self.assertTrue(result.protocol_ok)
        self.assertEqual(seen["url"], self.TARGET)
        self.assertEqual(seen["proxies"]["https"], "http://127.0.0.1:7897")
        self.assertEqual(result.proxy, "http://127.0.0.1:7897")

    def test_proxy_502_is_reported_as_proxy_problem(self) -> None:
        """502 是代理自己返回的，不能甩锅给地址。"""

        class _Resp:
            status_code = 502

            def close(self) -> None:
                pass

        with _with_proxy(), mock.patch("requests.get", return_value=_Resp()):
            result = diagnose(self.TARGET, timeout=2.0)

        self.assertFalse(result.reachable)
        self.assertIn("代理", result.headline)
        self.assertNotIn("已失效", " ".join(result.advice))
        self.assertTrue(any("节点" in item for item in result.advice))

    def test_proxy_connection_error_points_at_proxy(self) -> None:
        with _with_proxy(), mock.patch("requests.get",
                                       side_effect=ConnectionError("boom")):
            result = diagnose(self.TARGET, timeout=2.0)

        self.assertFalse(result.reachable)
        self.assertIn("代理", result.headline)

    def test_loopback_is_never_sent_through_proxy(self) -> None:
        """本机地址即使配置了代理也直连——代理回环等于白绕一圈。"""
        port = _free_port()
        with _with_proxy(), mock.patch("requests.get") as fake_get:
            result = diagnose(f"http://127.0.0.1:{port}/a.mp4", timeout=2.0)
        fake_get.assert_not_called()
        self.assertFalse(result.reachable)
        # 关键是"做了直连探测"，具体报拒绝还是超时随环境而定
        self.assertIn("TCP 连接", [s.name for s in result.stages])

    def test_rtsp_still_probes_directly(self) -> None:
        """HTTP 代理管不到 rtsp，不能因为"有代理"就跳过直连探测。"""
        port = _free_port()
        with _with_proxy():
            result = diagnose(f"rtsp://127.0.0.1:{port}/x", timeout=2.0)
        self.assertIn("TCP 连接", [s.name for s in result.stages])


class TestProtocolProbe(unittest.TestCase):
    def test_rtsp_ok(self) -> None:
        server = _FakeServer(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n").start()
        try:
            result = diagnose(f"rtsp://127.0.0.1:{server.port}/test", timeout=3.0)
            self.assertTrue(result.reachable)
            self.assertTrue(result.protocol_ok)
            self.assertIn("200", result.stages[-1].detail)
        finally:
            server.stop()

    def test_rtsp_404_marks_protocol_not_ok(self) -> None:
        server = _FakeServer(b"RTSP/1.0 404 Not Found\r\nCSeq: 1\r\n\r\n").start()
        try:
            result = diagnose(f"rtsp://127.0.0.1:{server.port}/gone", timeout=3.0)
            self.assertTrue(result.reachable)          # 网络是通的
            self.assertFalse(result.protocol_ok)       # 但路径不对
            self.assertIn("路径", result.headline)
        finally:
            server.stop()

    def test_rtsp_401_still_counts_as_online(self) -> None:
        """要求鉴权说明服务是活的，不应被判成故障。"""
        server = _FakeServer(b"RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n").start()
        try:
            result = diagnose(f"rtsp://127.0.0.1:{server.port}/x", timeout=3.0)
            self.assertTrue(result.protocol_ok)
        finally:
            server.stop()

    def test_rtmp_handshake_ok(self) -> None:
        server = _FakeServer(bytes([3]) + bytes(1536)).start()
        try:
            result = diagnose(f"rtmp://127.0.0.1:{server.port}/live/x", timeout=3.0)
            self.assertTrue(result.reachable)
            self.assertTrue(result.protocol_ok)
            self.assertIn("RTMP", result.stages[-1].detail)
        finally:
            server.stop()

    def test_server_that_closes_without_answering(self) -> None:
        server = _FakeServer(b"", read_bytes=True).start()
        try:
            result = diagnose(f"rtsp://127.0.0.1:{server.port}/x", timeout=3.0)
            self.assertTrue(result.reachable)
            self.assertFalse(result.protocol_ok)
        finally:
            server.stop()

    def test_http_head_probe(self) -> None:
        body = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: video/mp4\r\n"
            b"Content-Length: 1234\r\n\r\n"
        )
        server = _FakeServer(body).start()
        try:
            result = diagnose(f"http://127.0.0.1:{server.port}/a.mp4", timeout=3.0)
            self.assertTrue(result.protocol_ok)
            detail = result.stages[-1].detail
            self.assertIn("200", detail)
            self.assertIn("video/mp4", detail)
        finally:
            server.stop()

    def test_probe_can_be_skipped(self) -> None:
        server = _FakeServer(b"").start()
        try:
            result = diagnose(
                f"rtsp://127.0.0.1:{server.port}/x", timeout=3.0, probe_protocol=False
            )
            self.assertTrue(result.reachable)
            self.assertTrue(result.stages[-1].skipped)
        finally:
            server.stop()


class TestReport(unittest.TestCase):
    def test_report_contains_stages_and_conclusion(self) -> None:
        server = _FakeServer(b"RTSP/1.0 200 OK\r\n\r\n").start()
        try:
            text = diagnose(f"rtsp://127.0.0.1:{server.port}/test", timeout=3.0).report()
        finally:
            server.stop()
        self.assertIn("诊断地址", text)
        self.assertIn("DNS 解析", text)
        self.assertIn("TCP 连接", text)
        self.assertIn("结论：", text)

    def test_rtsp_ok_advice_mentions_udp_limitation(self) -> None:
        """RTSP 可用时也必须如实提醒 UDP 传输这一限制。"""
        server = _FakeServer(b"RTSP/1.0 200 OK\r\n\r\n").start()
        try:
            result = diagnose(f"rtsp://127.0.0.1:{server.port}/test", timeout=3.0)
        finally:
            server.stop()
        self.assertTrue(any("UDP" in item for item in result.advice))


if __name__ == "__main__":
    unittest.main()
