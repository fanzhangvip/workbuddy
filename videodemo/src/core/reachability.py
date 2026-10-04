"""解析前的网络可达性快速预检 + DNS 兜底（DoH）。

## 为什么需要它

用户粘贴一个网页视频地址，而所在网络把该站点整段拦掉时，现状是这样的：
yt-dlp 在里面反复重试，25 秒后我们报一句「解析超时」。用户只看到"慢"，
既不知道"为什么"，也不知道该做什么。

本仓库开发环境里 `youtube.com` / `vimeo.com` 正是这种情况，实测：

    www.youtube.com  系统解析 -> 157.240.7.20（Facebook 的网段！）
                     真实地址 -> 142.251.150.4（Google 的 142.251 段）
                     TCP 443  -> 一律超时（连真地址也超时）

注意 DNS **是"成功"的**——只是给了一个假地址。所以"能解析出 IP"绝不能当成
"能连上"。本模块用 3 秒左右的 DNS + TCP 探测先判一次，把结论说清楚，
并跳过注定失败的解析：省下的二十多秒是实打实的。

## DNS 兜底（DoH）

上面那种"只污染 DNS、不拦流量"的网络，换个能用的 DoH 就能拿到真地址，
而真地址往往是通的。所以：

    系统解析拿到地址却连不上  ->  用 DoH 再查一次
    DoH 的地址实测能连上     ->  注册改写，让这个域名解析到可用地址

**只有当 DoH 给的地址确实能连上，才注册改写。** 健康网络里绝不动它——
多此一举地把好好的解析改坏，比不做这件事还糟。

## 绝不误伤（三条硬规矩）

1. **配置了代理就跳过预检。** 走代理时"直连 origin 通不通"与能否播放无关，
   拿它去判失败会误杀。
2. **本机地址跳过。** 本机服务没有这类问题（判定复用 `url_router.is_loopback`，
   与本地反向代理的判定保持同一套标准）。
3. **结果按 (host, port) 缓存 60 秒。** 同一地址连播/重试不该每次都多花时间。

## 关于改写 `socket.getaddrinfo`

`yt-dlp` 最终经 `urllib3` 调 `socket.getaddrinfo` 建连，所以在这个函数上做定点
改写是有效的（等价于 curl 的 `--resolve`）。TLS 的 SNI 与证书校验用的是 URL 里的
主机名，不取自这里，所以证书校验照常。

代价：这是**进程级**改动，打上补丁后所有线程都受影响。因此只改写**明确登记过**
的主机名，其余一律原样交给系统解析；补丁只在第一次真的需要时才装，
且可以用设置项 `network/doh_fallback` 整体关掉。
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from src.core.url_router import is_loopback
from src.utils.logger import get_logger

_log = get_logger(__name__)

#: 单次 TCP 连接探测的超时（秒）。真能连上的服务器通常几十毫秒就返回了。
DEFAULT_TCP_TIMEOUT = 2.0

#: DoH 查询超时（秒）
DOH_TIMEOUT = 3.0

#: 最多同时试几个地址。
#:
#: 一个域名常常解析出 8 个以上地址，**串行**探"全都不通"最坏要
#: timeout × 地址数（实测 8 个 × 3 秒 = 24 秒，比不做预检还慢）。
#: 并发之后总耗时就是单个 timeout；只取前几个是因为同一 CDN 的地址
#: 通常要么都通要么都不通，多试无益。
_MAX_PROBE_ADDRESSES = 3

#: 可达性结果缓存时长（秒）
CACHE_TTL_S = 60.0

#: 内置 DoH 端点。`doh.pub`（腾讯）在实测环境里可达且能返回正确结果，
#: 放在第一位；`dns.alidns.com` 作备选。
DEFAULT_DOH_ENDPOINTS = (
    "https://doh.pub/dns-query",
    "https://dns.alidns.com/resolve",
)

#: 走这些协议才谈得上"网页可达性"
_HTTP_SCHEMES = ("http", "https")

_DEFAULT_PORTS = {"http": 80, "https": 443}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


# ------------------------------------------------------------------ 结果模型
@dataclass
class Reachability:
    """一次可达性预检的结论。"""

    url: str
    scheme: str = ""
    host: str = ""
    port: int = 0

    #: 系统 DNS 是否给出了地址
    dns_ok: bool = False
    #: 系统 DNS 给出的地址
    ips: tuple[str, ...] = ()
    #: TCP 是否连上了（真连上，不是"解析到就算"）
    tcp_ok: bool = False

    #: DoH 查到的地址（与系统解析不同才有值）
    doh_ips: tuple[str, ...] = ()
    #: DoH 的地址实测能连上，且已注册改写
    doh_rescued: bool = False
    #: 有证据表明系统 DNS 的结果不可信
    dns_suspect: bool = False

    elapsed_ms: int = 0
    error: str = ""

    #: 一行结论（面向用户）
    headline: str = ""
    #: 多行说明（面向用户）
    detail: str = ""
    #: 可尝试的做法
    advice: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocked(self) -> bool:
        """是否有"硬性"结论：网络层不通，继续解析没有意义。"""
        return not self.tcp_ok

    def report(self) -> str:
        """多行文本报告（自检 / 命令行 / 错误框用）。"""
        lines = [f"地址  : {self.url}"]
        if self.host:
            where = f"{self.host}:{self.port}"
            lines.append(f"目标  : {where}")
        lines.append(f"域名解析: {'成功' if self.dns_ok else '失败'}"
                     + (f" -> {', '.join(self.ips)}" if self.ips else ""))
        if self.doh_ips:
            lines.append(f"DoH 解析: {', '.join(self.doh_ips)}"
                         + ("（实测可用，已启用）" if self.doh_rescued else "（实测也不通）"))
        lines.append(f"TCP 连接: {'成功' if self.tcp_ok else '失败'}"
                     + (f"（{self.elapsed_ms} ms）" if self.elapsed_ms else ""))
        if self.headline:
            lines.append("")
            lines.append(f"结论  : {self.headline}")
        if self.detail:
            lines.append("")
            lines.append(self.detail)
        if self.advice:
            lines.append("")
            lines.append("可尝试：")
            lines.extend(f"· {item}" for item in self.advice)
        return "\n".join(lines)


# ------------------------------------------------------------------ 基础探测
def _tcp_ok(host: str, port: int, timeout: float) -> bool:
    """能否真的建立 TCP 连接。异常一律当"不通"。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _tcp_any(ips, port: int, timeout: float, *,
             limit: int = _MAX_PROBE_ADDRESSES) -> bool:
    """并发探测多个地址，任一能连上就算通。

    并发是这里的关键：串行在"全都不通"时要等 timeout × 地址数，
    那是预检里最坏的一条路径（也是唯一会被用户感知到的路径）。
    """
    targets = [ip for ip in dict.fromkeys(ips)][:limit]
    if not targets:
        return False
    if len(targets) == 1:
        return _tcp_ok(targets[0], port, timeout)

    found = threading.Event()

    def poke(ip: str) -> None:
        if found.is_set():
            return
        if _tcp_ok(ip, port, timeout):
            found.set()

    threads = [
        threading.Thread(target=poke, args=(ip,), daemon=True, name="reach-probe")
        for ip in targets
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout + 0.5)
    return found.is_set()


def _system_resolve(host: str, port: int) -> tuple[str, ...]:
    """系统 DNS 解析。失败返回空元组。

    刻意**不抛异常**：解析失败是预期情形之一，调用方只看结果。
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        _log.info("系统解析失败 %s: %s", host, exc)
        return ()
    seen: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.append(ip)
    return tuple(seen)


def host_port_of(url: str) -> tuple[str, int]:
    """取 (host, port)；取不到时返回 ("", 0)。"""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return "", 0
    host = (parsed.hostname or "").lower()
    if not host:
        return "", 0
    try:
        port = parsed.port or _DEFAULT_PORTS.get(parsed.scheme.lower(), 0)
    except ValueError:
        port = _DEFAULT_PORTS.get(parsed.scheme.lower(), 0)
    return host, int(port)


def is_probable(raw: str) -> bool:
    """这个地址值不值得做网页可达性预检。

    只对 http(s) 的非本机地址做；本机地址由调用方用 `url_router.is_loopback`
    另行排除（这里不 import，避免模块间绕圈依赖）。
    """
    try:
        parsed = urlparse((raw or "").strip())
    except ValueError:
        return False
    return parsed.scheme.lower() in _HTTP_SCHEMES and bool(parsed.hostname)


# ------------------------------------------------------------------ DoH
def _parse_doh_answers(payload: bytes) -> tuple[str, ...]:
    """从 DoH 的 JSON 应答里取出 A / AAAA 记录地址。

    两种常见格式都认：
      · RFC 8484 JSON（`dns.alidns.com`、Cloudflare）—— 顶层有 "Answer"
      · Google/腾讯的 JSON 变体 —— 结构相同，故一并处理
    取不到就返回空元组，绝不抛异常。
    """
    try:
        data = json.loads(payload.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return ()
    if not isinstance(data, dict):
        return ()
    answers = data.get("Answer")
    if not isinstance(answers, list):
        return ()
    out: list[str] = []
    for item in answers:
        if not isinstance(item, dict):
            continue
        if int(item.get("type") or 0) not in (1, 28):   # A / AAAA
            continue
        value = str(item.get("data") or "").strip()
        if value and value not in out:
            out.append(value)
    return tuple(out)


def _doh_query(endpoint: str, host: str, timeout: float) -> tuple[str, ...]:
    """向一个 DoH 端点查询一次，返回地址元组。"""
    import http.client

    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        return ()
    path = parsed.path or "/dns-query"
    path += f"?name={host}&type=A"

    conn = http.client.HTTPSConnection(
        parsed.hostname, parsed.port or 443, timeout=timeout
    )
    try:
        conn.request("GET", path, headers={
            "Accept": "application/dns-json",
            "User-Agent": _UA,
        })
        response = conn.getresponse()
        if response.status != 200:
            _log.info("DoH %s 返回 %s", endpoint, response.status)
            return ()
        return _parse_doh_answers(response.read(65536))
    except (OSError, ValueError, ssl.SSLError) as exc:
        _log.info("DoH %s 查询失败: %s", endpoint, exc)
        return ()
    finally:
        try:
            conn.close()
        except OSError:
            pass


def doh_lookup(host: str, *, timeout: float = DOH_TIMEOUT,
               endpoints: tuple[str, ...] = DEFAULT_DOH_ENDPOINTS) -> tuple[str, ...]:
    """依次问各个 DoH 端点，第一个有结果的即返回。

    全部失败返回空元组——DoH 只是"能救就救"，救不了不该影响主流程。
    """
    for endpoint in endpoints:
        ips = _doh_query(endpoint, host, timeout)
        if ips:
            _log.info("DoH 解析成功 %s via %s -> %s", host, endpoint, ips)
            return ips
    return ()


# ------------------------------------------------------------------ 解析改写
_overrides: dict[str, tuple[str, ...]] = {}
_override_lock = threading.Lock()
_patcher_installed = False


def registered_overrides() -> dict[str, tuple[str, ...]]:
    """当前生效的改写表（快照，供自检/日志展示）。"""
    with _override_lock:
        return dict(_overrides)


def register_override(host: str, ips: tuple[str, ...]) -> None:
    """把某个主机名固定解析到给定地址，并确保补丁已装上。"""
    host = (host or "").lower()
    if not host or not ips:
        return
    global _patcher_installed
    with _override_lock:
        _overrides[host] = tuple(ips)
        need_patch = not _patcher_installed
        if need_patch:
            _patcher_installed = True
    if need_patch:
        _install_patcher()
    _log.info("已注册 DNS 改写: %s -> %s", host, ", ".join(ips))


def clear_overrides() -> None:
    """清空改写表（测试用；也供"恢复默认设置"调用）。"""
    with _override_lock:
        _overrides.clear()


def _install_patcher() -> None:
    """把 `socket.getaddrinfo` 换成"查表优先"的版本。

    **只装一次**：重复装会把上一次的包装再包一层，越包越深。
    未登记的主机名原样走系统解析，行为与打补丁前完全一致。
    """
    original = socket.getaddrinfo

    def patched(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        with _override_lock:
            ips = _overrides.get(str(host).lower()) if isinstance(host, str) else None
        if not ips:
            return original(host, port, family, type, proto, flags)

        # 用**原始参数**去解析改写后的 IP：family/type/proto 该是什么就是什么，
        # 自己拼 sockaddr 会在 IPv6 与各种 flags 上出岔子。
        results: list = []
        for ip in ips:
            try:
                results.extend(original(ip, port, family, type, proto, flags))
            except OSError:
                continue
        if results:
            return results
        # 改写后的地址一个都解析不出来（例如只给了 v6 地址但要求 v4），
        # 退回系统解析，别把请求彻底堵死。
        return original(host, port, family, type, proto, flags)

    socket.getaddrinfo = patched  # type: ignore[assignment]


# ------------------------------------------------------------------ 缓存
_cache: dict[tuple[str, int], tuple[float, Reachability]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: tuple[str, int]) -> Reachability | None:
    with _cache_lock:
        hit = _cache.get(key)
    if hit is None:
        return None
    stamped, value = hit
    if time.monotonic() - stamped > CACHE_TTL_S:
        with _cache_lock:
            _cache.pop(key, None)
        return None
    return value


def _cache_put(key: tuple[str, int], value: Reachability) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ------------------------------------------------------------------ 结论文案
def _blocked_verdict(pre: Reachability) -> tuple[str, str, tuple[str, ...]]:
    """把"连不上"翻译成一句结论 + 多行说明 + 可尝试的做法。"""
    if not pre.dns_ok:
        headline = "域名解析失败：这台机器拿不到该网站的地址"
        detail = (
            f"系统 DNS 无法解析 {pre.host}。\n"
            "常见原因：DNS 服务器不可用、或该域名被本地 DNS 策略拦截。"
        )
        advice = (
            "换一个 DNS（如 223.5.5.5 / 119.29.29.29）后重试",
            "在设置里填写可用的网络代理",
        )
        return headline, detail, advice

    if pre.doh_rescued:      # 理论上不会走到这里（能连上就不算 blocked）
        return "网络可达", "", ()

    if pre.doh_ips:
        headline = f"连不上 {pre.host}：域名能解析，但网络层不通"
        detail = (
            f"系统把 {pre.host} 解析到 {', '.join(pre.ips)}，TCP 连不上；\n"
            f"用 DoH 查到的地址（{', '.join(pre.doh_ips)}）同样连不上。\n"
            "说明不只是 DNS 的问题——这个站点在当前网络下被整体拦截了。"
        )
    elif pre.dns_suspect:
        headline = f"连不上 {pre.host}：解析到的是无效地址"
        detail = (
            f"系统把 {pre.host} 解析到 {', '.join(pre.ips)}，但连不上，"
            "而且和 DoH 的结果对不上，\n"
            "看起来是 DNS 被污染（返回了假地址）。"
        )
    else:
        headline = f"连不上 {pre.host}：TCP 连接超时"
        detail = (
            f"域名解析正常（{', '.join(pre.ips)}），但 {pre.port} 端口连不上。\n"
            "常见原因：该站点在当前网络被拦截，或需要走代理才能访问。"
        )

    advice = (
        "在「设置 → 网络」里填写可用的 HTTP 代理，再重试",
        "换一个网络环境（如手机热点）验证是不是当前网络的问题",
        "如果手头有该视频的直接流地址，可以直接粘贴播放",
    )
    return headline, detail, advice


def _ok_verdict(pre: Reachability) -> tuple[str, str, tuple[str, ...]]:
    if pre.doh_rescued:
        return (
            f"网络可达（系统 DNS 不可信，已改用 DoH 找到可用地址）",
            f"{pre.host} 的系统解析结果 {', '.join(pre.ips)} 连不上，"
            f"DoH 给出的 {', '.join(pre.doh_ips)} 可用，已自动改用后者。",
            (),
        )
    return (f"网络可达（{pre.elapsed_ms} ms）", "", ())


def probe(
    url: str,
    *,
    timeout: float = DEFAULT_TCP_TIMEOUT,
    use_doh: bool = True,
    use_cache: bool = True,
    endpoints: tuple[str, ...] | None = None,
    skip_loopback: bool = True,
) -> Reachability | None:
    """探测一个 http(s) 地址的网络可达性。

    Args:
        timeout: 单次 TCP 连接的超时（秒）。
        use_doh: 系统解析连不上时，是否用 DoH 再查一次。
        use_cache: 是否读写 (host, port) 级缓存。
        endpoints: 自定义 DoH 端点；``None`` 用内置默认。
        skip_loopback: 本机地址直接返回 ``None``。默认开——"本机服务连不上"
            该按"服务没起来"处理，不能拿去判"这个站点被网络拦截了"。

    Returns:
        `Reachability`；地址不是 http(s)、取不到主机、或指向本机时返回
        ``None``——调用方据此知道"这条地址不适用本预检"，而不是"不可达"。

    流程：系统解析 → TCP 并发实测 →（连不上时才）DoH 兜底 → 再并发实测。
    """
    if not is_probable(url):
        return None
    if skip_loopback and is_loopback(url):
        return None

    host, port = host_port_of(url)
    if not host or not port:
        return None

    key = (host, port)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    started = time.monotonic()
    pre = Reachability(url=url.strip(), host=host, port=port)
    try:
        pre.scheme = urlparse(url.strip()).scheme.lower()
    except ValueError:
        pre.scheme = ""

    pre.ips = _system_resolve(host, port)
    pre.dns_ok = bool(pre.ips)
    if pre.dns_ok:
        pre.tcp_ok = _tcp_any(pre.ips, port, timeout)

    if not pre.tcp_ok and use_doh:
        doh_ips = doh_lookup(host, endpoints=endpoints or DEFAULT_DOH_ENDPOINTS)
        fresh = tuple(ip for ip in doh_ips if ip not in pre.ips)
        pre.doh_ips = doh_ips
        if fresh:
            pre.dns_suspect = True
            # 只有当 DoH 的地址**确实能连上**才认；否则只是留作诊断证据
            if _tcp_any(fresh, port, timeout):
                register_override(host, doh_ips)
                pre.tcp_ok = True
                pre.doh_rescued = True

    pre.elapsed_ms = int((time.monotonic() - started) * 1000)
    if pre.tcp_ok:
        pre.headline, pre.detail, pre.advice = _ok_verdict(pre)
    else:
        pre.error = "网络不可达"
        pre.headline, pre.detail, pre.advice = _blocked_verdict(pre)

    if pre.doh_rescued:
        _log.info("DoH 兜底成功 %s: %s -> %s", host, pre.ips, pre.doh_ips)
    elif not pre.tcp_ok:
        _log.warning("可达性预检未通过 %s:%s -> %s", host, port, pre.headline)

    if use_cache:
        _cache_put(key, pre)
    return pre


def reset() -> None:
    """清掉缓存与改写表（测试与"恢复默认设置"用）。"""
    clear_cache()
    clear_overrides()
