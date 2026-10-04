"""本机打码服务：ddddocr 识别 → 上游协议要的「格子编号」。

为什么需要它
------------
（决策 §10-1/2/3）打码必须用上游 `pjialin/py12306` 的代码，但：

- 上游 ``helpers/OCR.py`` 的源码里**没有识别能力**，只有「把图发给谁」的渠道；
- 三个渠道里 ``free`` 指向被禁的托管域、``ruokuai`` 是第三方付费平台；
- 只剩下 ``user`` 模式：把图 POST 到**我们自己**的地址，期望拿回**格子编号**。

所以「图 → 格子编号」这一段由本服务补齐，识别内核用 ddddocr（本地 ONNX）。
**「格子编号 → x,y 坐标」仍然完全由上游完成**（``get_image_position_by_offset``）：
本服务一个坐标都不算 —— 这正是修掉「假实现」的关键（老实现自己算坐标且算错）。

协议（与上游 ``OCR.get_image_by_free_site`` 完全一致，不是我们发明的）
--------------------------------------------------------------------
::

    POST <endpoint>
      form: img=<base64 图片>

    → 200 {"msg": "success", "result": [1, 4, 8]}   成功（编号 1..8）
    → 200 {"msg": "fail"}                            识别不出来

格子编号的排布来自上游
----------------------
上游公式（``OCR.get_image_position_by_offset``，``width = height = 75``）::

    x = 75 * ((o - 1) % 4 + 1) - 37.5   + 抖动(-5..5)
    y = 75 * ceil(o / 4)         - 37.5 + 抖动(-5..5)

即 **4 列 × 2 行、每格 75px**，编号 ``o = (行-1)*4 + 列``。
本模块只做它的**逆运算**，并且由
``tests/test_kernel_captcha.py::test_grid_math_is_the_inverse_of_upstream``
拿上游公式（把抖动置零）反向核对 —— 两边哪天不一致，测试当场变红。

用法::

    python -m ticket_helper.verify.captcha_server                 # 127.0.0.1:8899/check
    python -m ticket_helper.verify.captcha_server --port 9000 --path /code
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

log = logging.getLogger(__name__)

#: 上游格子边长（px）—— 与 ``OCR.get_image_position_by_offset`` 的 width/height 一致
CELL = 75
#: 上游的列数与行数（编号 1..8 为 4 列 × 2 行）
COLUMNS = 4
ROWS = 2
MAX_GRID = COLUMNS * ROWS

#: 协议里成功/失败的标记（与上游 `result.get('msg') == 'success'` 对齐）
MSG_OK = "success"
MSG_FAIL = "fail"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8899
DEFAULT_PATH = "/check"


# --------------------------------------------------------------------------- #
# 格子编号 ⇄ 坐标（逆运算；正向由上游负责）
# --------------------------------------------------------------------------- #
def grid_of_point(x: float, y: float) -> int | None:
    """一个点落在第几格（1..8）。落在格网之外返回 ``None``。

    逆运算推导：格 ``(col, row)`` 的中心是 ``(75*col - 37.5, 75*row - 37.5)``，
    所以 ``col = floor(x / 75) + 1``、``row = floor(y / 75) + 1``。
    """
    col = int(x // CELL) + 1
    row = int(y // CELL) + 1
    if not (1 <= col <= COLUMNS) or not (1 <= row <= ROWS):
        return None
    return (row - 1) * COLUMNS + col


def grids_from_rects(rects: list[tuple[int, int, int, int]]) -> list[int]:
    """检测框 → 格子编号（按中心点落在哪一格；去重并升序）。

    按**中心点**而不是左上角：检测框通常紧贴字符，左上角容易被格线切开。
    """
    found: set[int] = set()
    for x1, y1, x2, y2 in rects:
        grid = grid_of_point((x1 + x2) / 2.0, (y1 + y2) / 2.0)
        if grid is not None:
            found.add(grid)
    return sorted(found)


def recognize_grids(image: bytes) -> list[int] | None:
    """图 → 格子编号。识别不出返回 ``None``（**不抛**）。

    识别内核是 ddddocr（可选依赖）。没装就走 ``None`` ——
    服务照常启动，只是每次都回 ``{"msg":"fail"}``，
    绝不返回「猜的编号」：错误的编号会被上游换算成**错误的点击坐标**，
    而那种错误在界面上看起来和「识别不准」一模一样。
    """
    from ticket_helper.verify.service import DdddocrSolver  # noqa: PLC0415

    solver = DdddocrSolver()
    rects = solver.detect_rects(image)
    if not rects:
        log.debug("本机打码：未检出候选框（%s）", solver.last_note or "无说明")
        return None
    grids = grids_from_rects(rects)
    if not grids:
        log.debug("本机打码：候选框全在格网之外，丢弃")
        return None
    return grids


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    #: 由 :func:`create_server` 注入（避免把配置塞进类属性导致两个实例互相污染）
    expected_path = DEFAULT_PATH
    protocol_version = "HTTP/1.1"

    # 默认实现会把每个请求打到 stderr，污染日志且带时间戳噪音
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("本机打码服务: " + fmt, *args)

    def _reply(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802  （BaseHTTPRequestHandler 的约定名）
        """给使用者一个「服务活着吗」的探针。"""
        if self.path.split("?")[0] == "/healthz":
            self._reply({"msg": MSG_OK, "result": [], "service": "ticket-helper-captcha"})
            return
        self._reply({"msg": MSG_FAIL, "error": "只接受 POST"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != self.expected_path:
            self._reply({"msg": MSG_FAIL, "error": f"路径应为 {self.expected_path}"}, status=404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._reply({"msg": MSG_FAIL, "error": "空请求体"})
            return
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        fields = parse_qs(raw)
        # ⚠️ 字段名必须是上游的 `img`（`OCR.get_image_by_free_site` 就是这么发的）。
        # 老实现发的是 `image`，服务端就算实现了也收不到 —— 那正是「静默失效」。
        values = fields.get("img") or []
        if not values:
            self._reply({"msg": MSG_FAIL, "error": "缺少 img 字段"})
            return
        try:
            image = base64.b64decode(values[0])
        except Exception as exc:  # noqa: BLE001
            self._reply({"msg": MSG_FAIL, "error": f"img 不是合法 base64: {exc}"})
            return

        grids = recognize_grids(image)
        if not grids:
            self._reply({"msg": MSG_FAIL})
            return
        self._reply({"msg": MSG_OK, "result": grids})


def create_server(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, path: str = DEFAULT_PATH
) -> ThreadingHTTPServer:
    """建服务（不启动）。``port=0`` 时由系统分配，便于测试。"""
    handler = type("_BoundHandler", (_Handler,), {"expected_path": path})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, path: str = DEFAULT_PATH) -> None:
    server = create_server(host, port, path)
    bound = server.server_address
    log.info("本机打码服务已启动：http://%s:%s%s", bound[0], bound[1], path)
    log.info(
        "在上游内核配置里填这个地址（codec.user_endpoint），"
        "或设 TICKET_HELPER_CAPTCHA_ENDPOINT=%s",
        f"http://{bound[0]}:{bound[1]}{path}",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机打码服务（ddddocr → 上游协议的格子编号）")
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口")
    parser.add_argument("--path", default=DEFAULT_PATH, help="接口路径")
    args = parser.parse_args(argv)
    serve(args.host, args.port, args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# 供测试与外部调用：确认服务线程能干净地停下来
def serve_in_background(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, path: str = DEFAULT_PATH
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = create_server(host, port, path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
