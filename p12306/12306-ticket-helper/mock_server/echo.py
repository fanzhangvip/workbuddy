"""本地回声服务 —— 用来**证明「请求真的发出去了」**。

为什么需要它（F4 能力可用性证明）
---------------------------------
本项目吃过一次很典型的亏：云打码**看起来配好了**，实际请求根本没发对
（超级鹰的表单是 `user=&pass2=&softid=0`，账号空、代码里配的 key 压根没进请求、
图片**根本没上传**；云码缺 `type`）。而 `available()` 只看「有没有填 key」，
失败又被 `log.warning` 吞掉 —— 使用者只看到「识别失败」，
**永远不知道请求压根没发出去**。

这类「假实现」用常规测试抓不到（断言返回值 = None 时，代码和测试会一起说"没事"）。
唯一可靠的判据是：**看请求长什么样**。

所以本模块只做一件事：**把所有到达的请求原样记下来**，
让测试能断言「请求到达了 / 必填字段在 / 图片真的在 body 里」。

它**不依赖外网**，因此可以进 CI。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

#: 默认响应（各渠道按自己的协议覆盖）
DEFAULT_RESPONSE: tuple[int, bytes, str] = (200, b"{}", "application/json")


@dataclass
class EchoRecord:
    """一次到达的请求。**原样保存 raw body** —— 这是判据的基础。"""

    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    raw: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    @property
    def form(self) -> dict[str, str]:
        """``application/x-www-form-urlencoded`` 的解析结果。"""
        parsed = parse_qs(self.raw.decode("utf-8", "replace"), keep_blank_values=True)
        return {k: v[0] if v else "" for k, v in parsed.items()}

    @property
    def json(self) -> Any:
        try:
            return json.loads(self.raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None

    @property
    def text(self) -> str:
        return self.raw.decode("utf-8", "replace")


@dataclass
class EchoServer:
    """记录一切到达的请求；响应内容可配。

    用法（测试里）::

        with EchoServer() as echo:
            echo.respond({"code": 0})
            channel = ServerChanChannel(cfg(webhook=echo.url("/hook")))
            assert channel.send("t", "b")
            assert echo.records[-1].form["title"] == "t"
    """

    records: list[EchoRecord] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None
    _response: tuple[int, bytes, str] = DEFAULT_RESPONSE
    #: 每次请求的处理钩子（可选）：``hook(record) -> 覆盖响应 或 None``
    hook: Any = None

    # ------------------------------------------------------------------ #
    def respond(
        self,
        body: Any = None,
        *,
        status: int = 200,
        content_type: str = "application/json",
        raw: bytes | None = None,
    ) -> "EchoServer":
        """设置响应。``body`` 为 dict/list 时按 JSON 序列化。"""
        if raw is not None:
            payload = raw
        elif isinstance(body, (bytes, str)):
            payload = body.encode("utf-8") if isinstance(body, str) else body
        else:
            payload = json.dumps(body if body is not None else {}).encode("utf-8")
        self._response = (status, payload, content_type)
        return self

    @property
    def url_base(self) -> str:
        assert self._server is not None, "EchoServer 还没 start()"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def url(self, path: str = "/") -> str:
        return self.url_base + (path if path.startswith("/") else "/" + path)

    @property
    def empty(self) -> bool:
        return not self.records

    def clear(self) -> None:
        self.records.clear()

    # ------------------------------------------------------------------ #
    def start(self) -> "EchoServer":
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                parsed = urlparse(self.path)
                record = EchoRecord(
                    method=self.command,
                    path=parsed.path,
                    query=parse_qs(parsed.query, keep_blank_values=True),
                    headers={k.lower(): v for k, v in self.headers.items()},
                    raw=raw,
                )
                outer.records.append(record)

                status, payload, ctype = outer._response
                if outer.hook is not None:
                    override = outer.hook(record)
                    if override is not None:
                        o_status, o_body, o_ctype = override
                        status, payload, ctype = o_status, o_body, o_ctype

                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_DELETE = _handle

            def log_message(self, *args: Any) -> None:  # 关掉噪音
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def __enter__(self) -> "EchoServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()
