"""本地反向代理：给播放内核补上它发不出去的请求头。

## 为什么需要它

Qt 的 QMediaPlayer **没有任何注入自定义请求头的接口**（见 qt_backend.set_source），
而"防盗链"恰恰就是靠请求头判断的：

- `Referer` 必须是站点自己的域名（否则返回 403）；
- 有些站点还要校验 `User-Agent`、`Cookie`。

于是做法是：在本机起一个 HTTP 代理，播放器去请求
`http://127.0.0.1:<port>/proxy?u=<目标地址>`，由代理带着正确的头去取真实内容，
再把响应**原样**（含状态码与 Range 相关响应头）转回给播放器。播放器全程以为
自己在跟一个普通的 HTTP 服务器打交道，于是拖动进度条、断点续传都照常工作。

## 必须原样透传的东西

拖动进度条时播放器发的是 `Range: bytes=...`，服务端回 `206 Partial Content`
和 `Content-Range`。这三样（请求头、状态码、响应头）少一个都会表现为
"能播但拖不动"或"拖动后从头开始"。这是本模块最容易写错的地方。

## 主动剔除的请求头

`Accept-Encoding` 一定要删掉。真实服务器看到它就会返回 gzip/br 压缩内容，
此时 `Content-Length` 是压缩后的长度，和实际写出的字节数对不上——
播放器按这个长度读，会一直等不到足够的字节而卡住。
媒体文件本来也不该被压缩。

## 已知局限（如实说明）

HLS 的 `.m3u8` 清单里若写的是**相对路径或指向源站的绝对地址**，播放器后续请求
分片时会绕过本代理（拿不到 Referer）。所以：
- 单文件直链、DASH/分片直链：完全可用；
- HLS 清单：只有清单里本身带了签名 token 才能用，否则仍需 mpv 后端或外部播放器。
`proxy_url()` 因此对 `.m3u8` 返回空串（让调用方别自欺欺人）。
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from src.core.playlist import default_referer_for
from src.utils.logger import get_logger

log = get_logger(__name__)

#: 目标地址参数名
PARAM_URL = "u"
#: Referer 覆盖参数名
PARAM_REFERER = "r"
#: User-Agent 覆盖参数名
PARAM_USER_AGENT = "ua"

#: 连接与读取超时（秒）
_TIMEOUT = (15, 60)

#: 需要禁用的默认浏览器 UA（有些站点会拒绝不认识的 UA）
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: 从客户端请求里透传给源站的请求头：(小写查找名, 发出的规范写法)。
#:
#: **必须用规范大小写发出去。** HTTP 规范说头名大小写不敏感，但现实里不少
#: CDN / 防盗链中间件是按字面比对的；把 `Range` 发成 `range` 就可能被当成
#: 不支持断点续传（表现为"能播但拖不动"）。这里刻意写成映射而不是裸字符串。
_PASS_REQUEST_HEADERS: tuple[tuple[str, str], ...] = (
    ("range", "Range"),
    ("if-range", "If-Range"),
    ("if-modified-since", "If-Modified-Since"),
    ("if-none-match", "If-None-Match"),
    ("accept", "Accept"),
)

#: 从源站响应里透传给客户端的响应头。
#:
#: `date` 不在其中：`BaseHTTPRequestHandler.send_response()` 自己就会发一个
#: Date，再透传一个就成了重复头（实测响应头里出现两行 Date）。
_PASS_RESPONSE_HEADERS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "last-modified",
    "etag",
    "cache-control",
    "expires",
    "content-disposition",
)

#: 这些请求头必须由本代理自己决定，不能被客户端带着走
_STRIP_REQUEST_HEADERS = ("host", "connection", "accept-encoding", "referer", "user-agent")

#: 是否强制"响应完就关连接"。
#:
#: 默认开启。原因：本代理是**串行**的——一个连接上一次只处理一个请求，
#: 把上游的整段内容写完才算完（写不进去就阻塞，等客户端来读）。
#: 而播放内核（FFmpeg）在读媒体头之后经常会在**同一条 keep-alive 连接上**
#: 立刻发第二个请求去定位（Range 到文件末尾找索引）。此时我们那条连接
#: 还卡在"写第一个响应"上，它读不到新请求、我们等不到它把旧响应读完，
#: 双方互等到超时才回退。声明 `Connection: close` 后，播放内核只能另开一条
#: 连接，由线程池并行处理，这条路径就通了。
_FORCE_CLOSE = True

#: 源站单次请求的最大字节数 —— **本模块最关键的一个常量**。
#:
#: YouTube 这类站点会**对"整段"请求限速**。实测同一个 googlevideo 地址
#: （音频流 itag=251，经代理）：
#:
#:     Range: bytes=0-                  → 15 KB/s   （等于整文件）
#:     不带 Range（普通 GET，等于整文件） →  8 KB/s
#:     Range: bytes=0-129203407（整文件）→  9 KB/s
#:     Range: bytes=0-1048575（1 MB）   → 1649 KB/s
#:     Range: bytes=0-4194304（4 MB）   → 1627 KB/s
#:
#: 差 100 倍以上。而播放器一上来要的恰恰是整段（`Range: bytes=0-`），
#: 于是它只能在十几 KB/s 的涓流里等——这就是"媒体要 20 秒才加载完"
#: （Qt 的网络超时正好也是 20 秒，会直接报错）、"播着播着卡住"的真凶。
#:
#: 对策：**本代理从不向源站要整段**。播放器要整段，我们就替它拆成一段段
#: 有界区间依次取回，再接成一条连续字节流发回去。对播放器完全透明：
#: 状态码、Content-Range、Content-Length 都按它原本请求的那样回答。
_SLICE_BYTES = 8 * 1024 * 1024

#: 转发给客户端时的读块大小。
#:
#: 不能太大：`iter_content(n)` 会等凑够 n 字节才返回，块越大"首字节"
#: 就越晚到达播放器（播放器只要 32 KB 就能开始解析）。64 KB 在
#: 1.6 MB/s 的链路上只等 40 ms，可以忽略。
_RELAY_CHUNK = 64 * 1024


def parse_range(value: str) -> tuple[int, int | None] | None:
    """解析 ``bytes=S-E`` / ``bytes=S-`` → ``(start, end_or_None)``。

    只认单区间形式；``bytes=-N``（末尾 N 字节）、多区间、语法错误一律返回
    ``None``——调用方据此"原样透传"，不去做自己没把握的改写。
    """
    text = (value or "").strip()
    if not text.lower().startswith("bytes="):
        return None
    spec = text[len("bytes=") :].split(",")[0].strip()
    left, sep, right = spec.partition("-")
    if not sep:
        return None
    left, right = left.strip(), right.strip()
    if not left:  # bytes=-N：只给结尾长度，算不出起点
        return None
    try:
        start = int(left)
    except ValueError:
        return None
    if not right:
        return start, None
    try:
        end = int(right)
    except ValueError:
        return None
    return (start, end) if end >= start else None


def total_from_content_range(value: str) -> int:
    """从 ``bytes 0-1023/4096`` 里取出总长度；取不到返回 -1。"""
    _, _, total = (value or "").partition("/")
    total = total.strip()
    # 以 `*` 表示总长未知（很少见），或空
    if not total or total == "*":
        return -1
    try:
        return int(total)
    except ValueError:
        return -1


class _ClientGone(Exception):
    """客户端把连接掐了（seek / 换源 / 关窗口）。

    这在播放器身上是**常态**，不是错误：换源或拖动进度条时它会直接丢掉
    当前连接。转发循环用它做"立刻收工"的信号，日志里按普通事件处理。
    """


def referer_for(target: str, explicit: str = "") -> str:
    """决定该给目标地址配什么 Referer。

    优先用显式指定的；否则由目标地址推导站点根地址（`https://a.com/v/x.mp4`
    → `https://a.com/`），这是通过防盗链校验最常见也最有效的一招。
    """
    return (explicit or "").strip() or default_referer_for(target)


def proxy_capable(url: str) -> bool:
    """这个地址走代理是否真的有意义。

    这里只判**机制上能不能代理**：HLS 清单不做（见模块文档里的说明）；
    非 http(s) 的协议（rtsp/rtmp 等）本来也不走 HTTP 代理。

    **"这次该不该代理"是调用方的业务判断，不在这里做。** 例如回环地址
    （`127.0.0.1`）机制上完全可以代理，但本机服务没有防盗链问题，绕一圈
    纯属多余——那个判断在 `player_controller._proxy_route` 里。
    """
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    path = (parsed.path or "").lower()
    return not path.endswith((".m3u8", ".m3u"))


def _upstream_proxies(target: str) -> dict | None:
    """转发到 ``target`` 时该用哪条上游代理。

    - **本机目标**（测试里的假服务器、局域网页面）必须显式绕开代理，
      否则会得到一个和真实原因毫不相干的 ProxyError（`{"http": None, ...}`
      的显式 None 能压住环境变量里的代理）；
    - **其余目标**：设置里填了代理就用它；没填返回 ``None``，让 requests 按
      环境变量 / 系统代理走。

    返回 ``None`` 这一条是**播放能不能走代理的关键**：播放内核（Qt 的 FFmpeg
    后端）不认系统代理，而 requests 认。所以"有代理在生效"时由这层替它转发。
    """
    from src.utils.urls import is_loopback_url

    if is_loopback_url(target):
        return {"http": None, "https": None}

    configured = ""
    try:
        from src.utils import settings as S

        configured = S.get_str(S.K_PROXY, "").strip()
    except Exception:  # noqa: BLE001 - 读配置失败就退回"按环境变量走"
        configured = ""

    if configured:
        return {"http": configured, "https": configured}
    return None


class _ProxyHandler(BaseHTTPRequestHandler):
    """把 /proxy?u=... 转发到目标地址。"""

    # HTTP/1.1 支持 keep-alive，播放器复用连接时体验更好（seek 更快）
    protocol_version = "HTTP/1.1"
    server_version = "UniversalURLPlayerProxy/1.0"

    # ------------------------------------------------------------ 入口
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 命名
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def do_OPTIONS(self) -> None:  # noqa: N802
        # 有些播放器会先发 OPTIONS 探路；直接放行即可
        self.send_response(200)
        self.send_header("Allow", "GET, HEAD, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------ 主体
    def _handle(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
        except ValueError:
            self._fail(400, "地址无法解析")
            return

        if parsed.path in ("/status", "/"):
            self._status()
            return
        if parsed.path != "/proxy":
            self._fail(404, "只支持 /proxy 与 /status")
            return

        params = parse_qs(parsed.query)
        target = unquote((params.get(PARAM_URL) or [""])[0]).strip()
        if not target:
            self._fail(400, "缺少参数 u（目标地址）")
            return

        referer = unquote((params.get(PARAM_REFERER) or [""])[0])
        user_agent = unquote((params.get(PARAM_USER_AGENT) or [""])[0])
        self._forward(method, target, referer, user_agent)

    def _status(self) -> None:
        """健康检查：用于自检与界面上的"代理是否在跑"。"""
        body = json.dumps(
            {"ok": True, "service": "universal-url-player-proxy"}, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(
        self, method: str, target: str, referer: str, user_agent: str = ""
    ) -> None:
        headers = self._outgoing_headers(target, referer, user_agent)
        proxies = _upstream_proxies(target)

        # Range 由我们自己决定发什么（见 _SLICE_BYTES），所以先从透传集合里摘出来
        client_range = headers.pop("Range", "")
        parsed = parse_range(client_range)

        # 要改写成有界区间的三种情形（见 _SLICE_BYTES）：
        #   · 客户端没带 Range —— 它要的是整个文件；
        #   · `bytes=S-` —— 从这里一直到结尾，同样"没有上界"；
        #   · `bytes=S-E` 但区间很大 —— 相当于把整个文件再要一遍。
        # 只有"明确的、不太大的区间"才原样透传，那种请求源站本来就不限速。
        start = parsed[0] if parsed else 0
        client_end = parsed[1] if parsed else None
        wants_whole = not client_range or client_end is None
        huge = client_end is not None and client_end - start + 1 >= _SLICE_BYTES * 2

        if method != "HEAD" and (wants_whole or huge):
            if self._forward_sliced(
                target, headers, proxies, start, client_end, partial=bool(client_range)
            ):
                return
            # 源站不按区间回应（拿不到总长度）：退回"原样转发"那条老路
        if client_range:
            # 没走切片（或切片不适用）时，客户端要哪一段就原样转给源站
            headers["Range"] = client_range

        log.debug(
            "代理请求 %s %s Range=%s",
            method,
            target[:120],
            headers.get("Range", "（无）"),
        )
        response = self._request(method, target, headers, proxies)
        if response is None:
            return
        started = time.monotonic()
        try:
            self._relay(response, method, started)
        finally:
            response.close()

    def _request(self, method: str, target: str, headers: dict, proxies: dict | None):
        """向源站发一次请求。失败时已经回过 502，返回 None。"""
        import requests

        started = time.monotonic()
        try:
            response = requests.request(
                method,
                target,
                headers=headers,
                stream=True,
                timeout=_TIMEOUT,
                allow_redirects=True,
                proxies=proxies,
            )
        except requests.RequestException as exc:
            log.warning("代理转发失败 %s: %s", target, exc)
            self._fail(502, f"转发失败：{exc}")
            return None
        log.debug(
            "代理响应 %s 状态=%s Content-Range=%s 连接耗时=%.2fs",
            target[:80],
            response.status_code,
            response.headers.get("content-range", "（无）"),
            time.monotonic() - started,
        )
        return response

    def _forward_sliced(
        self,
        target: str,
        headers: dict,
        proxies: dict | None,
        start: int,
        client_end: int | None = None,
        *,
        partial: bool = True,
    ) -> bool:
        """把"整段"请求拆成一段段有界区间，依次取回、拼成一条连续字节流。

        `client_end` 是客户端自己给的上界（``bytes=S-E`` 里的 E）；None 表示
        它要的是"S 一直到结尾"。`partial=False` 表示客户端压根没发 Range
        （它要整个文件），此时回 200、不带 Content-Range。

        返回 True  = 已经替客户端给出完整回应（无论成败），调用方别再插手；
        返回 False = **一个字节都还没发给客户端**，可以退回普通转发。

        "只回一次响应头、正文分片续写"是关键：播放器看到的就是一个普通的
        206 响应，Content-Range/Content-Length 与它请求的完全对得上，
        但它背后其实是一串 8 MB 的有界区间请求（源站只对整段限速）。
        """
        offset = start
        total = -1
        first = True
        started = time.monotonic()
        written_total = 0

        while True:
            slice_headers = dict(headers)
            slice_headers["Range"] = f"bytes={offset}-{offset + _SLICE_BYTES - 1}"
            response = self._request("GET", target, slice_headers, proxies)
            if response is None:
                # 首片就失败：502 已经发过了；后续片失败：连接只能就此打住
                return True
            try:
                if first:
                    if response.status_code != 206:
                        return False
                    total = total_from_content_range(
                        response.headers.get("content-range", "")
                    )
                    if total <= 0 or total <= start:
                        return False
                    first = False
                    stop_at = total if client_end is None else min(total, client_end + 1)
                    budget = stop_at - start
                    self._send_sliced_head(
                        response, start, stop_at, total, partial=partial, budget=budget
                    )
                else:
                    stop_at = total if client_end is None else min(total, client_end + 1)
                    budget = stop_at - start - written_total
                if budget <= 0:
                    return True
                written_total += self._pump(response, budget)
            except _ClientGone:
                log.debug("客户端提前断开（多半是 seek）: %s", self.path)
                return True
            finally:
                response.close()

            stop_at = total if client_end is None else min(total, client_end + 1)
            if written_total >= stop_at - start:
                elapsed = time.monotonic() - started
                log.debug(
                    "分片转发完成 共 %.2fMB 用时 %.2fs (%.0f KB/s)",
                    written_total / 1048576,
                    elapsed,
                    written_total / max(elapsed, 1e-6) / 1024,
                )
                return True
            if written_total == 0:
                # 这一段源站一个字节都没给，再要下去也是空转
                log.debug("分片转发：源站在偏移 %d 处没有数据，停止", offset)
                return True
            offset = start + written_total

    def _send_sliced_head(
        self, response, start: int, stop_at: int, total: int, *, partial: bool, budget: int
    ) -> None:
        """给客户端发响应头。这是它这次能收到的**唯一**一份响应头。"""
        self.send_response(206 if partial else 200)
        for name in _PASS_RESPONSE_HEADERS:
            # 长度、区间、以及 "支持断点续传" 都由我们自己声明（我们看到的只是第一片）
            if name in ("content-length", "content-range", "accept-ranges"):
                continue
            value = response.headers.get(name)
            if value is not None:
                self.send_header(name.title(), value)
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{stop_at - 1}/{total}")
        self.send_header("Content-Length", str(budget))
        if _FORCE_CLOSE:
            self.send_header("Connection", "close")
        self.end_headers()


    def _outgoing_headers(self, target: str, referer: str, user_agent: str = "") -> dict:
        from src.utils import settings as S

        headers: dict[str, str] = {}
        for lookup, canonical in _PASS_REQUEST_HEADERS:
            # self.headers 的 get 是大小写不敏感的，查找用小写名即可
            value = self.headers.get(lookup)
            if value:
                headers[canonical] = value

        # Referer 的优先级：调用方显式指定（解析器从页面拿到的，最准确）
        # > 设置里手填的 > 由目标地址推导的站点根。
        #
        # 早期版本漏掉了"调用方显式指定"这一档，一律用推导出来的站点根——
        # 对 B 站这类 CDN 域名与站点域名不同的地址，等于把正确的 Referer
        # 换成了错的，防盗链照样过不去。
        resolved = referer_for(target, referer or S.get_str(S.K_LOCAL_PROXY_REFERER, ""))
        if resolved:
            headers["Referer"] = resolved

        # User-Agent 同理：解析器给的是该站点认可的 UA，优先于通用设置
        agent = (
            user_agent.strip()
            or S.get_str(S.K_USER_AGENT, "").strip()
            or DEFAULT_USER_AGENT
        )
        headers["User-Agent"] = agent
        # 让源站不要压缩：压缩会破坏 Content-Length 与实际字节数的一致性
        headers["Accept-Encoding"] = "identity"
        return headers

    def _relay(self, response, method: str, started: float = 0.0) -> None:
        """把源站响应转回给客户端。状态码与 Range 响应头必须原样保留。"""
        self.send_response(response.status_code)

        forwarded_length = None
        for name in _PASS_RESPONSE_HEADERS:
            value = response.headers.get(name)
            if value is None:
                continue
            if name == "content-length":
                forwarded_length = value
                continue
            self.send_header(name.title(), value)
        # Content-Length 放最后发，且对 HEAD 请求必须照发（客户端靠它判断大小）
        if forwarded_length is not None:
            self.send_header("Content-Length", forwarded_length)
        elif method == "HEAD":
            self.send_header("Content-Length", "0")

        if _FORCE_CLOSE:
            # 见 _FORCE_CLOSE 的说明：不保持连接，避免"上一个响应还没写完、
            # 客户端又在同一条连接上发新请求"造成互等。
            self.send_header("Connection", "close")

        self.end_headers()

        if method == "HEAD":
            return

        try:
            written = self._pump(response, None, log_progress=started > 0, started=started)
        except _ClientGone:
            log.debug("客户端提前断开（多半是 seek）: %s", self.path)
            return
        if started:
            elapsed = time.monotonic() - started
            log.debug(
                "代理转发结束 已发送=%.2fMB 用时=%.2fs (%.0f KB/s)",
                written / 1048576,
                elapsed,
                written / max(elapsed, 1e-6) / 1024,
            )

    def _pump(self, response, budget: int | None, *, log_progress: bool = False,
              started: float = 0.0) -> int:
        """把一段响应正文写给客户端，返回写出的字节数。

        `budget` 是这次最多写多少字节（None = 直到这一段结束）。切片转发时
        每一片都要精确记账，多写一个字节客户端的 Content-Length 就对不上了。

        客户端断开（seek / 换源）抛 `_ClientGone`，由调用方决定怎么收场。
        """
        written = 0
        last_report = time.monotonic()
        try:
            for piece in response.iter_content(chunk_size=_RELAY_CHUNK):
                if not piece:
                    continue
                if budget is not None and written + len(piece) > budget:
                    piece = piece[: budget - written]
                self.wfile.write(piece)
                written += len(piece)
                if budget is not None and written >= budget:
                    break
                if log_progress:
                    now = time.monotonic()
                    if now - last_report >= 5.0:
                        last_report = now
                        log.debug("代理进度 已发送=%.2fMB", written / 1048576)
        except (BrokenPipeError, ConnectionResetError) as exc:
            # 播放器拖动进度条时会主动断开当前连接，这不是错误
            raise _ClientGone from exc
        except OSError as exc:
            raise _ClientGone from exc
        return written

    # ------------------------------------------------------------ 输出
    def _fail(self, code: int, message: str) -> None:
        body = message.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 - 基类签名
        # 默认实现会把每一行请求打到 stderr，日志会被刷爆，这里降到 debug
        log.debug("proxy: " + fmt, *args)


class _ProxyServer(ThreadingHTTPServer):
    """只做一件事：别把"客户端断开"当成异常报到日志里。

    播放器是**经常**会中途掐断连接的——换源、seek 之后就丢开旧连接、
    检测到不支持就放弃、关窗口——这些都会让写到 `wfile` 的那一步抛
    `ConnectionResetError`。基类的 `handle_error` 会打一整条异常栈到
    stderr，几十行噪音会把日志里真正有用的东西淹掉，而且会让用户以为
    程序出错了。这些情况在这里是**正常**的，静音即可。
    """

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        exc = sys.exc_info()[1]
        if isinstance(
            exc,
            (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError),
        ):
            log.debug("代理连接被客户端中断（正常）: %s", exc)
            return
        super().handle_error(request, client_address)


class LocalProxyServer:
    """在 127.0.0.1 上跑的极简反向代理。"""

    def __init__(self, port: int = 0) -> None:
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port = int(port)

    # ------------------------------------------------------------ 生命周期
    def start(self, port: int | None = None) -> int:
        """启动并返回实际监听的端口。已在运行则直接返回现有端口。"""
        if self._httpd is not None:
            return self.port

        wanted = self._port if port is None else int(port)
        # 只绑 127.0.0.1：这是本机给自己用的，绝不能暴露到局域网
        self._httpd = _ProxyServer(("127.0.0.1", wanted), _ProxyHandler)
        self._httpd.daemon_threads = True
        self._port = self._httpd.server_address[1]

        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="local-proxy",
            daemon=True,
        )
        self._thread.start()
        log.info("本地反向代理已启动: http://127.0.0.1:%d", self._port)
        return self._port

    def stop(self) -> None:
        if self._httpd is None:
            return
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except OSError as exc:
            log.debug("关闭代理时出错: %s", exc)
        finally:
            self._httpd = None
            self._thread = None
            log.info("本地反向代理已停止")

    # ------------------------------------------------------------ 查询
    @property
    def port(self) -> int:
        return self._port

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def status_url(self) -> str:
        return f"{self.base_url()}/status"

    def health_check(self, timeout: float = 2.0) -> bool:
        """真的连一次 /status，确认端口可用（只看对象状态不够）。"""
        if not self.running:
            return False
        import urllib.request

        try:
            with urllib.request.urlopen(self.status_url(), timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return bool(payload.get("ok"))
        except Exception as exc:  # noqa: BLE001
            log.debug("代理健康检查失败: %s", exc)
            return False

    def url_for(self, target: str, referer: str = "", user_agent: str = "") -> str:
        """构造让播放器去请求的本地地址。"""
        query = f"{PARAM_URL}={quote(target, safe='')}"
        if referer:
            query += f"&{PARAM_REFERER}={quote(referer, safe='')}"
        if user_agent:
            query += f"&{PARAM_USER_AGENT}={quote(user_agent, safe='')}"
        return f"{self.base_url()}/proxy?{query}"


# ------------------------------------------------------------------ 单例
_server: LocalProxyServer | None = None
_lock = threading.Lock()


def get_server() -> LocalProxyServer:
    """取全局代理实例（惰性创建，不会自动启动）。"""
    global _server
    with _lock:
        if _server is None:
            _server = LocalProxyServer()
        return _server


def ensure_started(port: int = 0) -> LocalProxyServer | None:
    """确保代理已启动。启动失败返回 None（不抛异常，播放不该因此中断）。"""
    server = get_server()
    if server.running:
        return server
    try:
        server.start(port)
    except OSError as exc:
        log.warning("本地反向代理启动失败: %s", exc)
        return None
    return server


def stop() -> None:
    """停止代理（程序退出时调用）。"""
    if _server is not None:
        _server.stop()


def is_running() -> bool:
    return _server is not None and _server.running


def port() -> int:
    return _server.port if _server is not None else 0


def describe() -> str:
    """一行摘要，供自检与设置面板展示。"""
    if _server is None or not _server.running:
        return "本地反向代理：未启动"
    url = _server.status_url()
    ok = _server.health_check()
    return f"本地反向代理：{'运行中' if ok else '已启动但健康检查失败'} · {url}"


def pick_header(headers: dict | None, name: str) -> str:
    """从请求头字典里按大小写不敏感的方式取一个值。

    yt-dlp 给回来的 `http_headers` 是原样透传的，键名大小写不保证
    （见过 `Referer`、`referer`、`User-Agent`、`user-agent` 都出现过）。
    """
    if not headers:
        return ""
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted and value:
            return str(value).strip()
    return ""


def proxy_url(target: str, headers: dict | None = None, *, required: bool = False) -> str:
    """需要时返回可用于播放的本地代理地址；不适用则返回空串。

    `headers` 是解析器给出的请求头（可含 Referer / User-Agent），会随地址一起
    编码进查询串，由代理在转发时补上。调用方约定：拿到空串就用原地址直接播。

    `required=True` 表示**这条不是"增强"，而是唯一能播的路子**，此时不看总
    开关。目前唯一的场景是"有代理在生效"：播放内核（Qt 的 FFmpeg 后端）不认
    系统代理，不走这一层转发就必然报"无法连接到服务器"。防盗链那两种用法仍
    尊重总开关——用户关掉开关，说明他不要这层增强。
    """
    from src.utils import settings as S

    if not required and not S.local_proxy_enabled():
        return ""
    if not proxy_capable(target):
        return ""

    server = ensure_started(S.get_int(S.K_LOCAL_PROXY_PORT, 0))
    if server is None:
        return ""
    return server.url_for(
        target,
        pick_header(headers, "Referer"),
        pick_header(headers, "User-Agent"),
    )


def find_free_port() -> int:
    """找一个空闲端口（给设置面板的"自动"选项做提示用）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
