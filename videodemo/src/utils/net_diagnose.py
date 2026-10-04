"""播放地址的分层连通性诊断。

## 为什么需要它

QMediaPlayer 遇到连不上的地址时，通常**不发任何错误**，只会长时间停在
LoadingMedia，最后被超时兜底判为"加载超时"。用户拿到的信息只有一句
"失败了"，完全无法判断是：

- 地址写错了 / 域名解析不了
- 服务器已下线 / 端口被运营商或公司防火墙屏蔽
- 能连上服务器，但路径/应用名不对（服务端 404）
- 服务器一切正常，问题在解码或传输方式（RTSP 走 UDP 被丢包）

这四种情况的处理方式完全不同，所以这里把诊断拆成四层，逐层给出结论：

    地址解析 → DNS 解析 → TCP 连接 → 协议握手

任何一层失败就停在那里，并把该层的结论翻译成可操作的中文建议。

## 代理会改变结论

`http/https` 地址如果当前有代理在生效（设置 / 环境变量 / Windows 系统代理），
**裸 TCP 探测的结论不能用来判断能否播放**：代理模式下数据包是发往代理本机的，
播放器根本不直连源站。拿裸 TCP 的结果下结论，会把一条**正在正常播放**的地址
报成"服务器已下线、地址可能已失效"——这在实际使用中真的发生过。

所以这类地址改走「经代理探测」分支：让 `requests` 带着同一条代理发一次
`Range: bytes=0-0` 的只读请求，拿到任何 HTTP 应答就说明这条链路是通的。
`rtsp/rtmp` 等协议仍按直连探测——HTTP 代理管不到它们。

## 设计约束

- 非代理分支只用标准库；代理分支复用程序已有的 `requests` 依赖
- 全程只做**只读探测**：RTSP 发 OPTIONS、RTMP 只做握手、HTTP 取 1 字节，
  都不会真正开始拉流，不会给服务器造成负担
- 可安全地在工作线程中调用；不做任何 Qt 依赖
"""

from __future__ import annotations

import os
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.request import url2pathname

# 看起来像文件系统路径的开头：C:\ / C:/ / \\server / ./ / ../
_PATHISH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|\.{1,2}[\\/])")

# 媒体扩展名（用于把 "dir/a.mp4" 这类相对路径也认成文件）
_MEDIA_EXT_RE = re.compile(
    r"\.(?:mp4|m4v|mkv|webm|flv|f4v|mov|avi|ts|m2ts|mts|mpg|mpeg|wmv|3gp|ogv"
    r"|mp3|m4a|aac|flac|wav|opus|mpd|m3u8|m3u)$",
    re.IGNORECASE,
)

# 各协议的默认端口
DEFAULT_PORTS: dict[str, int] = {
    "rtsp": 554,
    "rtsps": 322,
    "rtmp": 1935,
    "rtmps": 443,
    "rtmpt": 80,
    "rtmpts": 443,
    "http": 80,
    "https": 443,
    "srt": 9000,
}

# 这些协议靠 UDP 通信，无法用 TCP 探测，只能连到 TCP 控制端口后作罢
UDP_ONLY_SCHEMES = frozenset({"rtp", "udp", "srt"})

# 需要 TLS 包裹的协议
TLS_SCHEMES = frozenset({"rtsps", "rtmps", "rtmpts", "https"})

_UA = "UniversalURLPlayer/1.0"


@dataclass
class Stage:
    """一个诊断阶段的结果。"""

    name: str
    ok: bool
    detail: str
    elapsed_ms: int = 0
    skipped: bool = False


@dataclass
class Diagnosis:
    """一次完整诊断的结论。"""

    url: str
    scheme: str = ""
    host: str = ""
    port: int = 0
    stages: list[Stage] = field(default_factory=list)

    #: 网络层是否可用（DNS + TCP 通过）。转发握手失败不影响这个值
    reachable: bool = False
    #: 协议层是否可用（握手得到服务器正确应答）
    protocol_ok: bool = False
    #: 是否本地文件（不涉及网络）
    is_local: bool = False
    #: 本次诊断实际走的代理（空串 = 直连）
    proxy: str = ""

    headline: str = ""
    advice: list[str] = field(default_factory=list)
    file_path: str = ""

    @property
    def first_failure(self) -> Stage | None:
        for stage in self.stages:
            if not stage.ok and not stage.skipped:
                return stage
        return None

    @property
    def elapsed_ms(self) -> int:
        return sum(s.elapsed_ms for s in self.stages)

    def report(self) -> str:
        """生成给人看的分层报告。"""
        lines = [f"诊断地址：{self.url}", ""]
        for stage in self.stages:
            if stage.skipped:
                mark = "跳过"
            else:
                mark = "通过" if stage.ok else "失败"
            cost = f"（{stage.elapsed_ms} ms）" if stage.elapsed_ms else ""
            lines.append(f"[{mark}] {stage.name}{cost}")
            for chunk in str(stage.detail).splitlines():
                lines.append(f"        {chunk}")

        lines.append("")
        lines.append(f"结论：{self.headline}")
        if self.advice:
            lines.append("")
            lines.append("可尝试：")
            lines.extend(f"· {item}" for item in self.advice)
        return "\n".join(lines)


# ------------------------------------------------------------------ 辅助
def _local_path_of(url: str) -> str | None:
    """若地址指向本地文件，返回其文件系统路径。"""
    if url.lower().startswith("file://"):
        parsed = urlparse(url)
        path = url2pathname(parsed.path)
        # Windows 上 file:///C:/x 会得到 /C:/x，需要去掉前导斜杠
        if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path
    if "://" in url:
        return None
    # 形如 C:\a\b.mp4、\\server\share\a.mp4、./a.mp4
    if _PATHISH_RE.match(url) or os.path.isabs(url):
        return url
    # 即使文件不存在也要认出来（否则会被误报成"主机名解析失败"），
    # 例如 someone/nope.mp4、missing.m3u8
    if "/" in url or "\\" in url:
        if _MEDIA_EXT_RE.search(url):
            return url
    elif _MEDIA_EXT_RE.search(url):
        return url
    try:
        if os.path.exists(url):
            return url
    except (OSError, ValueError):
        pass
    return None


def _split_host_port(url: str, scheme: str) -> tuple[str, int]:
    """从 URL 中取主机与端口（RTMP 常见的 rtmp://host/app/name 也适用）。"""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or DEFAULT_PORTS.get(scheme, 0)
    return host, int(port)


def _tls_wrap(sock: socket.socket, host: str, timeout: float) -> socket.socket:
    """给已连接的 socket 套上 TLS（不校验证书）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sock.settimeout(timeout)
    return ctx.wrap_socket(sock, server_hostname=host or None)


def _recv_some(sock: socket.socket, limit: int = 4096, timeout: float = 4.0) -> bytes:
    """尽力读一段数据，读满或超时即返回。"""
    sock.settimeout(timeout)
    buf = b""
    try:
        while len(buf) < limit:
            chunk = sock.recv(limit - len(buf))
            if not chunk:
                break
            buf += chunk
            # 拿到一行/一段就够判断了，不必等满
            if b"\r\n" in buf or len(buf) >= 16:
                break
    except (TimeoutError, socket.timeout, OSError):
        pass
    return buf


# ------------------------------------------------------------------ 协议探测
def _probe_rtsp(raw: socket.socket, url: str) -> tuple[bool, str]:
    """发一条 RTSP OPTIONS，判断服务是否应答。"""
    request = (
        f"OPTIONS {url} RTSP/1.0\r\n"
        f"CSeq: 1\r\n"
        f"User-Agent: {_UA}\r\n"
        f"Accept: application/sdp\r\n"
        f"\r\n"
    ).encode("utf-8")
    raw.sendall(request)
    data = _recv_some(raw, 2048)
    if not data:
        return False, "已连上服务器，但服务端没有回应 RTSP 请求（不像 RTSP 服务）"
    text = data.decode("utf-8", "replace")
    first = text.splitlines()[0].strip() if text.splitlines() else text[:80]
    if "RTSP/1.0" not in text:
        return False, f"服务端返回的内容不是 RTSP 应答：{first[:100]}"
    code = ""
    parts = first.split()
    if len(parts) >= 2:
        code = parts[1]
    if code == "200":
        return True, f"服务端正常应答：{first[:100]}"
    if code in ("401", "403"):
        return True, f"服务端要求鉴权（{code}），说明服务在线：{first[:100]}"
    if code == "404":
        return False, f"服务端找不到该路径（404）：{first[:100]}"
    return True, f"服务端有应答（{first[:100]}）"


def _probe_rtmp(raw: socket.socket) -> tuple[bool, str]:
    """发送 RTMP C0+C1 握手，验证是否收到 S0+S1。"""
    payload = bytes([3]) + bytes(4) + bytes(4) + os.urandom(1528)
    raw.sendall(payload)
    raw.settimeout(4.0)
    buf = b""
    try:
        while len(buf) < 1537:
            chunk = raw.recv(1537 - len(buf))
            if not chunk:
                break
            buf += chunk
    except (TimeoutError, socket.timeout, OSError):
        pass
    if not buf:
        return False, "已连上服务器，但没有收到 RTMP 握手应答（端口上可能不是 RTMP 服务）"
    if buf[0] != 3:
        return False, f"RTMP 握手版本异常（收到 0x{buf[0]:02x}，期望 0x03）"
    if len(buf) < 1537:
        return False, f"RTMP 握手不完整（收到 {len(buf)} 字节，期望 1537）"
    return True, f"RTMP 握手成功（收到 {len(buf)} 字节 S0+S1）"


def _probe_http(raw: socket.socket, url: str, host: str, port: int) -> tuple[bool, str]:
    """发一条 HEAD，确认 HTTP 服务有正常响应。"""
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    host_header = host if port in (80, 443) else f"{host}:{port}"
    request = (
        f"HEAD {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        f"User-Agent: {_UA}\r\n"
        f"Accept: */*\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("utf-8")
    raw.sendall(request)
    data = _recv_some(raw, 2048)
    if not data:
        return False, "已连上服务器，但没有收到 HTTP 响应"
    text = data.decode("utf-8", "replace")
    first = text.splitlines()[0].strip() if text.splitlines() else ""
    if not first.upper().startswith("HTTP/"):
        return False, f"返回内容不是 HTTP 响应：{first[:100]}"
    code = first.split()[1] if len(first.split()) >= 2 else "?"
    extra = ""
    for line in text.splitlines()[1:]:
        low = line.lower()
        if low.startswith("content-type:") or low.startswith("content-length:"):
            extra += "  " + line.strip()
    if code.startswith("2"):
        return True, f"服务端正常应答：{first[:80]}{extra}"
    if code.startswith("4"):
        return False, f"服务端返回 {code}（地址可能已失效或需要鉴权）：{first[:80]}"
    return True, f"服务端有应答：{first[:80]}{extra}"


_PROBE_BY_SCHEME = {
    "rtsp": _probe_rtsp,
    "rtsps": _probe_rtsp,
}


# ------------------------------------------------------------------ 主流程
def _proxy_hit(url: str):
    """这条地址当前有没有代理在生效；有则返回 ProxyHit，否则 None。"""
    try:
        from src.utils import net_proxy
        from src.utils import settings as S

        app_proxy = S.get_str(S.K_PROXY, "")
    except Exception:  # noqa: BLE001 - 诊断不该被配置读取拖垮
        return None
    try:
        return net_proxy.proxy_for(url, app_proxy=app_proxy)
    except Exception:  # noqa: BLE001
        return None


def _probe_via_proxy(result: Diagnosis, url: str, proxy: str, timeout: float) -> None:
    """走代理发一次只读请求，判断这条链路通不通。

    只取 1 个字节就断开，不会真的把视频拉下来。
    """
    t0 = time.perf_counter()
    try:
        import requests

        resp = requests.get(
            url,
            headers={"User-Agent": _UA, "Range": "bytes=0-0"},
            proxies={"http": proxy, "https": proxy},
            timeout=max(timeout * 2, 8),
            stream=True,
        )
        status = resp.status_code
        resp.close()
    except Exception as exc:  # noqa: BLE001 - 各种网络异常都归为"不通"
        result.stages.append(
            Stage("经代理探测", False, f"经代理 {proxy} 请求失败："
                                     f"{type(exc).__name__}: {str(exc)[:80]}",
                  int((time.perf_counter() - t0) * 1000))
        )
        result.reachable = False
        result.headline = (
            f"该地址需要经代理访问，而经代理 {proxy} 也拿不到应答——问题在代理这条链路上。"
        )
        result.advice = [
            "代理软件（Clash / v2ray 等）是否在运行、节点是否可用；",
            "换一个代理节点再试：同一份订阅里不同节点差别很大，"
            "有的站点只认某些节点的出口地址；",
            "若刚才是从浏览器或其它程序复制的地址，注意它可能有时效（如带 expire 参数）。",
        ]
        return result

    ms = int((time.perf_counter() - t0) * 1000)

    # 502/503/504 大多不是源站返回的，而是**代理自己**给出的（节点不允许访问、
    # 上游挂了、TUN 没起来）。这类要如实说成"代理链路的问题"，别甩锅给地址。
    if status in (502, 503, 504):
        result.reachable = False
        result.stages.append(
            Stage("经代理探测", False,
                  f"经代理 {proxy} 得到 HTTP {status}（{ms} ms）——多为代理自身返回的错误",
                  ms)
        )
        result.headline = (
            f"经代理 {proxy} 访问该地址返回 HTTP {status}，多半是代理节点不让访问这个站。"
        )
        result.advice = [
            "换一个代理节点再试：同一份订阅里不同节点差别很大；",
            "确认代理软件（Clash / v2ray 等）的分流规则没有把该站点规则到 DIRECT 或 REJECT；",
            "这条地址若来自网页解析，可能已过期（如带 expire 参数），重新解析一次。",
        ]
        return result

    result.reachable = True
    result.protocol_ok = True
    result.stages.append(
        Stage("经代理探测", True, f"经代理 {proxy} 得到 HTTP {status}（{ms} ms）", ms)
    )
    result.headline = (
        "地址可达（经代理）——若仍无法播放，问题在解码或传输环节，不在网络。"
    )
    result.advice = []


def diagnose(
    url: str,
    *,
    timeout: float = 4.0,
    probe_protocol: bool = True,
) -> Diagnosis:
    """对地址做分层诊断。

    :param timeout: 每个网络阶段的超时（秒）
    :param probe_protocol: 是否在 TCP 连通后继续做协议握手（会更慢但更准）
    """
    url = (url or "").strip()
    result = Diagnosis(url=url)

    # ---- 第 0 层：地址解析
    t0 = time.perf_counter()
    local = _local_path_of(url)
    if local is not None:
        exists = os.path.exists(local)
        size = ""
        if exists:
            try:
                size = f"，大小 {os.path.getsize(local) / 1048576:.1f} MB"
            except OSError:
                pass
        result.is_local = True
        result.reachable = exists
        result.protocol_ok = exists
        result.file_path = local
        result.stages.append(
            Stage(
                "地址解析",
                exists,
                f"本地文件：{local}" + ("（存在" + size + "）" if exists else "（不存在）"),
                int((time.perf_counter() - t0) * 1000),
            )
        )
        if exists:
            result.headline = "本地文件存在，网络无关。「无法播放」通常是编码格式问题。"
            result.advice = ["若画面/声音异常，多为该文件编码不被内置解码器支持，可用外部播放器打开。"]
        else:
            result.headline = "本地文件不存在，路径写错了或文件已被移动/删除。"
            result.advice = ["确认路径拼写，或改用「打开文件」按钮选择。"]
        return result

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host, port = _split_host_port(url, scheme)
    result.scheme = scheme
    result.host = host
    result.port = port

    if not host:
        result.stages.append(
            Stage("地址解析", False, f"无法从地址中解析出主机名：{url}",
                  int((time.perf_counter() - t0) * 1000))
        )
        result.headline = "地址格式不正确，解析不出主机名。"
        result.advice = ["检查地址是否完整，例如 rtsp://主机或IP:端口/路径。"]
        return result

    result.stages.append(
        Stage("地址解析", True, f"协议 {scheme or '(无)'}，主机 {host}，端口 {port or '(默认)'}",
              int((time.perf_counter() - t0) * 1000))
    )

    # ---- 代理分支：http/https 有代理在生效时，直连探测的结论会误导人
    #      （代理模式下包是发往代理本机的，播放器并不直连源站）
    if scheme in ("http", "https"):
        from src.utils.urls import is_loopback_url

        if not is_loopback_url(url):
            hit = _proxy_hit(url)
            if hit is not None:
                result.stages.append(
                    Stage("代理检测", True,
                          f"检测到{hit.source} {hit.proxy}，改走代理探测（直连结论不适用）",
                      0, skipped=True)
                )
                result.proxy = hit.proxy
                _probe_via_proxy(result, url, hit.proxy, timeout)
                return result

    # ---- 第 1 层：DNS
    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, port or None, type=socket.SOCK_STREAM)
        ips = sorted({info[4][0] for info in infos})
        result.stages.append(
            Stage("DNS 解析", True, "解析到 " + ", ".join(ips[:4]),
                  int((time.perf_counter() - t0) * 1000))
        )
    except socket.gaierror as exc:
        result.stages.append(
            Stage("DNS 解析", False, f"无法解析主机名 {host}（{exc.strerror or exc}）",
                  int((time.perf_counter() - t0) * 1000))
        )
        result.headline = "域名无法解析——地址拼写有误，或当前网络/DNS 受限。"
        result.advice = [
            "核对主机名拼写；",
            "换用 IP 地址试试；",
            "若在公司网络，可能需要配置代理或 DNS。",
        ]
        return result

    if not port:
        result.stages.append(
            Stage("TCP 连接", False, f"无法确定 {scheme} 的端口，请显式写出端口",
                  skipped=True)
        )
        result.headline = f"不知道该连哪个端口（协议 {scheme}）。"
        result.advice = ["在地址里补上端口，例如 rtmp://主机:1935/应用/流名。"]
        return result

    # ---- 第 2 层：TCP 连接
    t0 = time.perf_counter()
    raw: socket.socket | None = None
    last_error = ""
    for info in infos:
        family, socktype, proto, _canon, addr = info
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(addr)
            raw = sock
            connect_ms = int((time.perf_counter() - t0) * 1000)
            result.reachable = True
            result.stages.append(
                Stage("TCP 连接", True, f"已连接到 {addr[0]}:{addr[1]}（{connect_ms} ms）",
                      connect_ms)
            )
            break
        except (TimeoutError, socket.timeout):
            last_error = f"连接 {addr[0]}:{addr[1]} 超时（{timeout:.0f} 秒无响应）"
        except ConnectionRefusedError:
            last_error = f"{addr[0]}:{addr[1]} 拒绝连接（端口未开放）"
        except OSError as exc:
            last_error = f"连接 {addr[0]}:{addr[1]} 失败：{exc}"
        finally:
            if raw is None:
                sock.close()

    if raw is None:
        result.stages.append(
            Stage("TCP 连接", False, last_error or "无法建立连接",
                  int((time.perf_counter() - t0) * 1000))
        )
        if "超时" in last_error:
            result.headline = (
                f"连不上服务器 {host}:{port}（超时）——服务器不可达。"
            )
            result.advice = [
                "这条地址很可能已经失效、或服务器已下线（网上流传的公开直播源失效速度很快）；",
                f"也可能是你的网络或防火墙不允许访问 {port} 端口——"
                "RTSP(554)/RTMP(1935) 在运营商网络与公司内网中常被屏蔽；",
                "换一台设备/换一个网络（比如手机热点）再试，可快速区分是地址问题还是网络问题；",
                "点「外部打开」用系统上的 VLC/mpv 试一下，可交叉验证。",
            ]
        elif "拒绝连接" in last_error:
            result.headline = f"{host}:{port} 明确拒绝了连接——端口上没有服务在监听。"
            result.advice = [
                "服务器的这个端口没有开放，地址或端口号可能写错了；",
                "若地址来自网络分享，可能已经过期。",
            ]
        else:
            result.headline = f"无法连接到 {host}:{port}。"
            result.advice = ["检查地址、端口与网络环境。"]
        return result

    # ---- 第 3 层：协议握手
    t0 = time.perf_counter()
    conn = raw
    if scheme in TLS_SCHEMES:
        try:
            conn = _tls_wrap(raw, host, timeout)
        except ssl.SSLError as exc:
            raw.close()
            result.stages.append(
                Stage("TLS 握手", False, f"TLS 握手失败：{exc}",
                      int((time.perf_counter() - t0) * 1000))
            )
            result.headline = "TCP 能连上，但 TLS 握手失败——端口上可能不是加密服务。"
            result.advice = ["确认协议是否正确（比如 rtsps 与 rtsp 的区别）。"]
            return result

    if not probe_protocol:
        conn.close()
        result.stages.append(Stage("协议握手", True, "已跳过（未启用）", 0, skipped=True))
        result.protocol_ok = True
        result.headline = "网络连通正常。"
        return result

    if scheme in UDP_ONLY_SCHEMES:
        conn.close()
        result.protocol_ok = True
        result.stages.append(
            Stage("协议握手", True,
                  f"{scheme} 走 UDP，TCP 探测无从判断", 0, skipped=True)
        )
        result.headline = "控制端口可达，但该协议基于 UDP，无法用本次探测判断。"
        result.advice = ["这类地址需要靠实际播放或外部播放器验证。"]
        return result

    try:
        if scheme in ("rtsp", "rtsps"):
            ok, detail = _probe_rtsp(conn, url)
        elif scheme in ("rtmp", "rtmps", "rtmpt", "rtmpts"):
            ok, detail = _probe_rtmp(conn)
        elif scheme in ("http", "https"):
            ok, detail = _probe_http(conn, url, host, port)
        else:
            ok, detail = True, f"未针对 {scheme} 实现协议探测"
    except (OSError, ValueError) as exc:
        ok, detail = False, f"协议探测出错：{exc}"
    finally:
        try:
            conn.close()
        except OSError:
            pass

    result.protocol_ok = ok
    result.stages.append(
        Stage("协议握手", ok, detail, int((time.perf_counter() - t0) * 1000))
    )

    if ok:
        result.headline = "地址可达，服务器应答正常——若仍无法播放，问题在解码或传输环节。"
        if scheme in ("rtsp", "rtsps"):
            result.advice = [
                "内置内核播放 RTSP 时使用 UDP 传输，且无法改为 TCP；"
                "若你的网络丢弃 UDP 包，会表现为一直加载却不出画面。"
                "这种情况请点「外部打开」，用 VLC / mpv 播放（它们支持强制 TCP）。",
                "确认服务端此刻确实在推流（没人推流时服务器会应答但拿不到画面）。",
            ]
        elif scheme in ("rtmp", "rtmps", "rtmpt", "rtmpts"):
            result.advice = [
                "服务器在线，请核对「应用名/流名」是否正确；",
                "若服务端要求鉴权，地址中需包含用户名密码。",
            ]
        else:
            result.advice = [
                "服务器在线。若仍失败，看日志里的 FFmpeg 报错以定位是编码还是请求头问题。"
            ]
    else:
        result.headline = "能连上服务器，但协议握手不通过——路径或应用名很可能不对。"
        result.advice = [
            "核对地址里的路径 / 应用名 / 流名（大小写敏感）；",
            "确认服务器此刻确实在推送该路径；",
            "若返回 404/Nonexistent，多半是路径不存在或流已停止。",
        ]

    return result
