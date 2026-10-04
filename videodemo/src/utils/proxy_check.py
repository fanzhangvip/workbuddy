"""VPN / 代理体检：现在走的是哪条路，那条路到底通不通。

## 为什么需要单独一层

程序里有三处要出网，但它们**读配置的方式不同**，这正是"设置里明明填了代理、
播放还是连不上"的根源：

  · **解析**（yt-dlp）与**下载**（requests）自己会读环境变量和 Windows 系统
    代理，不用程序额外做什么；
  · **播放**（Qt 的 FFmpeg 后端）**不认任何代理配置**，只能靠本地反向代理
    替它转发——而本地反向代理用的是 requests，于是又回到了上一条。
    这条链路由 `player_controller._proxy_route()` 在"检测到有代理生效"时
    自动接上，不需要用户去开什么开关（`local_proxy.proxy_url(required=True)`）。

所以"在设置里填代理"和"播放走了代理"中间隔着本地反向代理这一层，
本模块负责把这件事讲清楚。

## VPN 的两种模式，决定了要不要配置

  · **TUN / 全局模式**（Clash 的 TUN、系统级 VPN 客户端）：虚拟网卡接管路由，
    程序的裸 TCP 已经在隧道里了，**什么都不用配**——直连即走 VPN；
  · **系统代理模式**（Clash Verge 等的"系统代理"开关）：只改注册表里的
    ``ProxyServer``，**裸 TCP 完全不受影响**。程序会自动识别它，
    播放也会因此自动改走本地反向代理。

两种都能用，且都不需要用户手工干预。真正需要手填的只有一种情形：
代理软件既没开系统代理、也没开 TUN（或程序要在另一台机器上跑），
此时把它的监听地址填进设置即可。

## 本模块做什么

只做两件只读的事，不写任何配置、不改任何环境：

  1. 说清**当前生效的是哪一条**（设置 > 环境变量 > 系统代理）；
  2. **真的发一次请求**去验证——同一个目标，直连一次、经代理一次，
     用对照结果判断问题在"网络"还是在"代理配置"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from src.utils import net_proxy

#: 体检用的默认目标。选 YouTube 是因为它同时覆盖两件事：**被墙**、
#: 以及用户真正想访问的站——它若通，多半没有别的站不通。
DEFAULT_TARGET = "https://www.youtube.com/"

#: 兜底对照目标（国内可达、极轻量）。只在"直连和经代理都不通"时才用，
#: 用来把"本机根本没网"与"代理配置错了"区分开。
FALLBACK_TARGET = "https://www.baidu.com/"

#: 体检请求用的 UA。不带它有些站点会直接 403，那会把"网络通"误判成"不通"。
_PROBE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _scheme_of(proxy: str) -> str:
    return (urlparse(proxy if "://" in proxy else f"//{proxy}").scheme or "").lower()


def socks_ready() -> bool:
    """``socks5://`` 这类代理需要 PySocks；装了 requests 也不一定有。

    打包产物里若缺了它，SOCKS 代理会在运行时抛一句很难懂的
    ``Missing dependencies for SOCKS support``，所以这里提前探一次。
    """
    try:
        import socks  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


# --------------------------------------------------------------------- 来源


@dataclass(frozen=True)
class SourceInfo:
    """一个代理来源。

    `role` 说明它在程序里的分量，直接用于界面文案——三个来源并不是
    "都配了就叠加"的关系，而是**按优先级取第一个**。
    """

    key: str
    label: str
    value: str
    role: str

    @property
    def configured(self) -> bool:
        return bool(self.value.strip())


def sources(app_proxy: str = "") -> list[SourceInfo]:
    """三个代理来源的当前取值，按优先级排列。"""
    return [
        SourceInfo(
            "settings",
            "设置中填写",
            (app_proxy or "").strip(),
            "优先级最高，填了就用它",
        ),
        SourceInfo(
            "env",
            "环境变量",
            net_proxy.env_proxy(),
            "HTTPS_PROXY / HTTP_PROXY / ALL_PROXY",
        ),
        SourceInfo(
            "system",
            "Windows 系统代理",
            net_proxy.system_proxy(),
            "Clash 等工具的「系统代理」开关写在这里",
        ),
    ]


def active_hit(app_proxy: str = "", target: str = "") -> net_proxy.ProxyHit | None:
    """当前真正生效的那一条代理（没有则为 None）。"""
    return net_proxy.proxy_for(target or DEFAULT_TARGET, app_proxy=app_proxy)


def describe_active(app_proxy: str = "") -> str:
    """一句话：现在生效的是哪条代理，以及播放那边会发生什么。

    界面与自检都用它，所以文案要说人话、要能指导操作，而不是把配置原样回显。
    """
    hit = active_hit(app_proxy)
    if hit is None:
        return (
            "当前没有检测到代理：解析、下载、播放都走直连。\n"
            "如果你用的是 VPN 的 TUN／全局模式，这是正常现象——那种模式接管了"
            "整机路由，程序不需要、也不应该再配代理。"
        )

    scheme = _scheme_of(hit.proxy)
    note = ""
    if scheme.startswith("socks") and not socks_ready():
        note = "\n⚠ 这是 SOCKS 代理，但当前环境缺少 PySocks，实际会连不上。"

    return (
        f"当前生效：{hit.source} · {hit.proxy}{note}\n"
        "播放无需额外设置：检测到代理时，播放会自动改走本地反向代理转发"
        "（播放内核自己不认代理配置）。"
    )


# ------------------------------------------------------------------- 实测


@dataclass(frozen=True)
class Probe:
    """一次连通性实测的结果。

    ``ok`` 不单独存字段，而是**从状态码算出来**：多存一份就有两份可能对不上
    的真相，而"HTTP 200 却标着失败"这种自相矛盾的结果最难排查。
    """

    target: str
    proxy: str        # 空串 = 直连
    status: int
    ms: int
    error: str = ""

    @property
    def label(self) -> str:
        return f"经代理 {self.proxy}" if self.proxy else "直连"

    @property
    def ok(self) -> bool:
        """真的访问到了（2xx/3xx）。"""
        return 200 <= self.status < 400

    @property
    def gateway_error(self) -> bool:
        """502/503/504 —— 代理链路的典型症状：应答来自代理网关而不是目标站。

        区分它有实际意义：这两种失败的处理方向完全不同。"无应答"要去查
        节点／端口，"网关报错"则是代理软件自己或节点已经连不上游了。
        """
        return self.status in (502, 503, 504)

    def line(self) -> str:
        if self.ok:
            return f"{self.label}：HTTP {self.status}（{self.ms} ms）"
        if self.status:
            return (
                f"{self.label}：HTTP {self.status}（{self.ms} ms）"
                "—— 有应答，但服务端拒绝了这个请求"
            )
        return f"{self.label}：失败（{self.ms} ms）—— {self.error}"


def probe(
    target: str,
    *,
    proxy: str = "",
    timeout: float = 6.0,
) -> Probe:
    """对 ``target`` 发一次 GET，看它通不通。

    Args:
        proxy: 空串表示**显式直连**。

    ``proxy`` 为空时会显式传 ``{"http": None, "https": None}``：requests 见到
    这两个键就不再合并环境变量里的代理，这样才能测到"真实的直连结论"。
    这与 `local_proxy._upstream_proxies` 里的做法一致。

    只读 GET，读到响应头就断开（``stream=True`` + ``close()``），不会把
    大文件拖下来。
    """
    import requests

    started = time.monotonic()
    proxies = {"http": proxy, "https": proxy} if proxy else {"http": None, "https": None}
    try:
        response = requests.get(
            target,
            proxies=proxies,
            timeout=timeout,
            stream=True,
            allow_redirects=True,
            headers={"User-Agent": _PROBE_UA, "Accept-Encoding": "identity"},
        )
        status = int(response.status_code)
        response.close()
        return Probe(
            target=target,
            proxy=proxy,
            status=status,
            ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 - 任何网络异常都只当作"不通"
        return Probe(
            target=target,
            proxy=proxy,
            status=0,
            ms=int((time.monotonic() - started) * 1000),
            error=_explain(exc, proxy),
        )


#: 把底层异常翻译成"原因 + 技术名"。左边一组是判定用的关键词（全小写，
#: 同时匹配异常类名与消息），右边是给用户看的原因。
_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("connecttimeout", "connect timeout"), "连接超时：对方无应答"),
    (("readtimeout", "read timeout"), "读取超时：连上了但数据不来"),
    (("proxyerror", "cannot connect to proxy"), "连不上代理进程"),
    (("sslerror", "certificate"), "TLS／证书问题"),
    (("nameresolution", "getaddrinfo", "name or service"), "域名解析失败"),
    (("connectionreset", "connection aborted", "connectionrefused"), "连接被重置／拒绝"),
    (("timed out", "timeout"), "超时：对方无应答"),
    (("connectionerror", "connection refused"), "连接失败"),
)


def _explain(exc: Exception, proxy: str) -> str:
    """把底层异常翻译成用户能照着做的说明。

    原始的 requests 异常动辄两三百字符（还夹着连接对象的内存地址），
    直接摆到界面上等于没说。这里只保留"原因 + 异常类名"，排查时够用。
    """
    name = type(exc).__name__
    raw = str(exc).strip()
    lowered = f"{name} {raw}".lower()

    if "socks" in lowered and "dependencies" in lowered:
        # requests 缺 SOCKS 支持时抛的就是这一句，措辞本身已经说明了原因
        return "缺少 SOCKS 支持（requests 需要 PySocks）"
    if "socks" in lowered and not socks_ready():
        return "缺少 SOCKS 支持（未安装 PySocks）"

    for keys, reason in _HINTS:
        if any(key in lowered for key in keys):
            tail = reason
            if proxy and reason.startswith("连不上代理进程"):
                tail = f"{reason}（{proxy} 没在监听？）"
            return f"{tail}（{name}）"

    # 认不出来就退回原文，但一定要截断——别把整段堆栈摆到界面上
    snippet = raw[:120] + ("…" if len(raw) > 120 else "")
    return f"{name}: {snippet}" if snippet else name


@dataclass
class Report:
    """一次完整体检：来源 + 直连对照 + 经代理实测 + 结论。"""

    target: str
    app_proxy: str
    hit: net_proxy.ProxyHit | None
    direct: Probe
    through: Probe | None = None
    fallback: Probe | None = None
    verdict: str = ""
    advice: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """最终能不能访问到目标站。"""
        if self.through is not None:
            return self.through.ok
        return self.direct.ok

    def text(self) -> str:
        lines = [f"目标：{self.target}"]
        lines.append(self.direct.line())
        if self.through is not None:
            lines.append(self.through.line())
        if self.fallback is not None:
            lines.append(f"对照（{FALLBACK_TARGET}）{self.fallback.line()}")
        lines.append("")
        lines.append(f"结论：{self.verdict}")
        if self.advice:
            lines.append(self.advice)
        lines.extend(self.notes)
        return "\n".join(lines)


def check(
    target: str = "",
    app_proxy: str = "",
    *,
    timeout: float = 6.0,
) -> Report:
    """体检：同一个目标，直连一次、经代理一次，给出可执行的结论。"""
    target = (target or "").strip() or DEFAULT_TARGET
    hit = active_hit(app_proxy, target)

    direct = probe(target, timeout=timeout)
    through = probe(target, proxy=hit.proxy, timeout=timeout) if hit else None

    report = Report(
        target=target,
        app_proxy=app_proxy,
        hit=hit,
        direct=direct,
        through=through,
    )

    # 两边都不通时，再拿一个国内可达的站探一次，把"本机没网"与"代理配错"分开。
    if not direct.ok and (through is None or not through.ok):
        report.fallback = probe(FALLBACK_TARGET, timeout=timeout)

    _conclude(report)
    return report


def _conclude(report: Report) -> None:
    """把三次探测的结果翻译成结论与下一步动作。"""
    hit = report.hit
    target = report.target

    if hit is None:
        if report.direct.ok:
            report.verdict = "直连可用，当前没有代理在生效。"
            report.advice = (
                "如果你本来就在用 VPN，说明它是 TUN／全局模式（或根本没在跑），"
                "两种情况都不用在这里填代理。"
            )
        elif report.fallback is not None and not report.fallback.ok:
            report.verdict = "本机连国内站点也不通，是网络本身的问题。"
            report.advice = "先确认本机网络／VPN 客户端状态，再回来测。"
        else:
            report.verdict = f"直连不通，且没有检测到任何代理——{target} 在当前网络不可达。"
            report.advice = (
                "用 VPN 的话，两种做法任选：\n"
                "  · 打开 VPN 客户端的「系统代理」开关（本程序会自动识别，无需填地址）；\n"
                "  · 或把它的监听地址填到上面的「代理地址」里，例如 http://127.0.0.1:7897。"
            )
        report.notes.append(
            "提示：解析／下载／播放读的是同一份代理配置，所以这里的结论对三者都成立。"
        )
        return

    if report.through is None:  # 理论上不会发生，防御一下
        report.verdict = "未取得代理探测结果。"
        return

    direct_ok, proxy_ok = report.direct.ok, report.through.ok

    if proxy_ok and not direct_ok:
        report.verdict = f"代理可用：{target} 必须经代理，而经代理能访问。"
        report.advice = "保持现状即可。播放会自动经本地反向代理转发。"
    elif proxy_ok and direct_ok:
        report.verdict = "代理可用，且这条地址直连也通。"
        report.advice = "没问题；走不走代理都能看。"
    elif direct_ok and not proxy_ok:
        report.verdict = f"⚠ 走代理反而不通（{hit.source}：{hit.proxy}），但直连正常。"
        if report.through.gateway_error:
            report.advice = (
                "代理返回了 502/503/504——这是代理软件自己回的错误，"
                "说明它连不上所选节点。\n通常是节点已失效，或代理软件刚启动还没连上。"
                "换个节点再测一次。"
            )
        else:
            report.advice = (
                "说明这条代理本身有问题——地址／端口写错、代理软件没在跑、"
                "或节点不可用。\n先核对代理软件里显示的监听端口，再填到这里；"
                "填错了反而会让本来能播的地址播不了，可以直接清空此栏。"
            )
    else:
        if report.fallback is not None and report.fallback.ok:
            report.verdict = f"本机有网（能直连 {FALLBACK_TARGET}），但这个代理通路连不上 {target}。"
            if report.through.gateway_error:
                report.advice = (
                    "代理返回 502/503/504，是它自己回的错误，说明它连不上所选节点——"
                    "换个节点再测。"
                )
            else:
                report.advice = (
                    "问题在代理这一侧：多半是端口写错、代理软件没开、"
                    "或当前节点被墙／不可用。\n可以换个节点再测一次。"
                )
        else:
            report.verdict = "直连与经代理都不通。"
            report.advice = "先确认 VPN 客户端已开启并选好节点，再回来测。"

    if _scheme_of(hit.proxy).startswith("socks") and not socks_ready():
        report.notes.append(
            "⚠ 探测到 SOCKS 代理但缺少 PySocks——这种地址实际上用不了，"
            "建议改填代理软件的 HTTP 混合端口。"
        )
