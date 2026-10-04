"""测试用的本地 HTTP 服务器（不是 test_ 开头，不会被 unittest 收集为用例）。

为什么要有一个真的 HTTP 服务器，而不是 mock 掉 requests：

- **Range 请求**只能靠真实响应验证（要真回 206 + `Content-Range`）。
  我们最怕的就是"能播但拖不动进度条"，而这个问题只有真跑一遍才暴露得出来。
- **请求头注入**（防盗链代理的核心）也必须看服务端**实际收到**了什么头，
  在客户端断言"我设了 Referer"没有意义——请求可能根本没发出去。
- **取消与续传**需要一个会慢慢吐数据的服务端才会走到。

所以这里的服务器会：记录每次请求收到的头、支持 Range、能按需限速、能返回 404。
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

#: 默认文件大小（字节）
DEFAULT_SIZE = 256 * 1024

#: 一个带 <video> 的最小 HTML 页面，供 yt-dlp 的 generic 解析器识破
VIDEO_PAGE = """<!DOCTYPE html>
<html><head><title>Test Clip</title></head>
<body>
  <h1>Test Clip</h1>
  <video src="{media}" controls preload="none"></video>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # 由服务器实例注入
    server: "_Server"

    def do_GET(self) -> None:  # noqa: N802
        self._respond("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._respond("HEAD")

    # ------------------------------------------------------------ 主体
    def _respond(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
        except ValueError:
            self._send(400, b"bad request")
            return

        self.server.requests.append(
            {"path": parsed.path, "query": parsed.query, "headers": dict(self.headers)}
        )

        if parsed.path in ("/file.bin", "/slow.bin"):
            self._serve_bytes(method, self.server.payload)
            return
        if parsed.path == "/video.mp4":
            self._serve_bytes(
                method, self.server.payload, content_type=self.server.media_content_type
            )
            return
        if parsed.path == "/page.html":
            body = VIDEO_PAGE.format(media="/video.mp4").encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8", method)
            return
        if parsed.path == "/status":
            self._send(200, b'{"ok": true}', "application/json")
            return
        self._send(404, b"not found")

    def _serve_bytes(
        self, method: str, payload: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        """支持 Range 的标准实现（单区间）。

        若服务器开了限速（`set_slow`），就按 `slow_chunk_size` + `chunk_delay`
        慢慢吐——**限速对所有端点生效**，因为 yt-dlp 要走的是 `/video.mp4`
        而不是我们手工指定的 `/slow.bin`，只限速后者的话 yt-dlp 会瞬间下完，
        "取消"就永远测不到。
        """
        total = len(payload)
        start, end = 0, total - 1
        partial = False

        header = self.headers.get("Range")
        if header and header.startswith("bytes="):
            spec = header[len("bytes="):].split(",")[0].strip()
            first, _, last = spec.partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else total - 1
                elif last:
                    # bytes=-N 取最后 N 字节
                    start = max(0, total - int(last))
            except ValueError:
                start, end = 0, total - 1
            if start >= total:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, total - 1)
            partial = True

        chunk = payload[start:end + 1]
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        if method == "HEAD":
            return

        if not self.server.slow:
            self.wfile.write(chunk)
            return

        size = self.server.slow_chunk_size
        try:
            for offset in range(0, len(chunk), size):
                if self.server.stop_slow:
                    return
                self.wfile.write(chunk[offset:offset + size])
                self.wfile.flush()
                time.sleep(self.server.chunk_delay)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send(
        self, code: int, body: bytes, content_type: str = "text/plain", method: str = "GET"
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address) -> None:
        super().__init__(address, _Handler)
        self.payload = bytes((i % 251) for i in range(DEFAULT_SIZE))
        self.media_content_type = "video/mp4"
        self.requests: list[dict] = []
        self.chunk_delay = 0.05
        self.slow_chunk_size = 8 * 1024
        self.slow = False
        self.stop_slow = False

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        """客户端中途断开（取消下载）是测试的**预期行为**，别打到 stderr 上。

        http.server 默认会把整条异常栈打出来，几十行噪音会把真正的
        测试结果淹没——而这类"连接被中止"在取消场景里必然发生。
        """
        import sys as _sys

        exc = _sys.exc_info()[1]
        if isinstance(
            exc,
            (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError),
        ):
            return
        super().handle_error(request, client_address)


class LocalHTTPServer:
    """上下文管理器：起一个本地 HTTP 服务器供测试使用。

    `payload_file` 给出时直接服务这个真实文件（`/video.mp4` 返回它的字节，
    并带服务端记录请求头的能力）。要验证"真能解码出画面"就必须这样——
    合成字节过不了解码器。
    """

    def __init__(self, size: int = DEFAULT_SIZE, payload_file: "Path | None" = None) -> None:
        self._server = _Server(("127.0.0.1", 0))
        if payload_file is not None:
            self._server.payload = Path(payload_file).read_bytes()
            suffix = Path(payload_file).suffix.lower()
            if suffix == ".mp4":
                self._server.media_content_type = "video/mp4"
            elif suffix in (".m4a", ".aac"):
                self._server.media_content_type = "audio/mp4"
            elif suffix == ".webm":
                self._server.media_content_type = "video/webm"
            else:
                self._server.media_content_type = "application/octet-stream"
        elif size != DEFAULT_SIZE:
            self._server.payload = bytes((i % 251) for i in range(size))
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    # ------------------------------------------------------------ 生命周期
    def __enter__(self) -> "LocalHTTPServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        self._server.stop_slow = True
        try:
            self._server.shutdown()
            self._server.server_close()
        except OSError:
            pass

    # ------------------------------------------------------------ 访问
    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def payload(self) -> bytes:
        return self._server.payload

    @property
    def size(self) -> int:
        return len(self._server.payload)

    def url(self, path: str) -> str:
        return f"{self.base}{path}"

    def requests_for(self, path: str) -> list[dict]:
        """某路径收到过的请求（含请求头），用于验证请求头是否真的发出去了。"""
        return [entry for entry in self._server.requests if entry["path"] == path]

    def last_headers(self, path: str) -> dict:
        hits = self.requests_for(path)
        return hits[-1]["headers"] if hits else {}

    def clear_requests(self) -> None:
        self._server.requests.clear()

    def set_slow(self, delay: float, chunk_size: int = 8 * 1024) -> None:
        """开启限速。对所有端点生效（含 yt-dlp 会去取的 /video.mp4）。"""
        self._server.slow = True
        self._server.chunk_delay = delay
        self._server.slow_chunk_size = chunk_size

    def query_of(self, path: str) -> dict:
        """某个路径最后一次请求的 query 参数（代理把目标放在 query 里）。"""
        hits = self.requests_for(path)
        return parse_qs(hits[-1]["query"]) if hits else {}
