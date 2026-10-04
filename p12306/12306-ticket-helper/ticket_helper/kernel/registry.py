"""功能域 → 后端 的注册表 —— **全项目唯一的切换点**。

设计依据：``docs/pjialin内核化总体架构设计方案v3.md`` §4.1（铁律 R3/R4/R6）。

规则
----
1. 业务代码**只**通过 :func:`get` 取实现，不许 import `kernel/self` 或 `kernel/pjialin`；
2. 业务代码里**不许**出现 ``if backend == "pjialin"`` 之类的判断；
3. 每个功能域的开关**独立**（``TICKET_HELPER_KERNEL_BACKENDS=station=pjialin``），
   可逐个切、可一键回退；
4. 后端名**大小写敏感**（它是标识符，不是给人读的文案）；
5. 非法值**快速失败**（`ConfigError`），**不静默回退** ——
   静默回退会让「我明明切了 X」变成一句无法查证的猜测。

为什么默认仍是 `self`
--------------------
切默认值 = 换行为。P1 的目标是「**让它可切**」并**把差异摆出来**，
而不是替使用者决定。差异由 `scripts/kernel_diff.py` 与
`tests/test_kernel_station.py` 打印/断言，看清楚再切。
"""

from __future__ import annotations

import os
from typing import Any, Callable

from ticket_helper.errors import ConfigError

from .domains import (  # noqa: F401  （仅用于类型提示与契约核对）
    CaptchaSolver,
    LeftTicketRoute,
    HeartbeatProvider,
    LoginProbe,
    OrderSubmitter,
    PacingProvider,
    QrCheckBuilder,
    SlideSolver,
    StationTable,
)
from .pjialin import (
    PjialinCaptchaSolver,
    PjialinHeartbeat,
    PjialinLoginProbe,
    PjialinOrderSubmitter,
    PjialinPacing,
    PjialinQrCheck,
    PjialinQueryRoute,
    PjialinSlideSolver,
    PjialinStationTable,
)
from .self import (
    SelfCaptchaSolver,
    SelfHeartbeat,
    SelfLoginProbe,
    SelfOrderSubmitter,
    SelfPacing,
    SelfQrCheck,
    SelfQueryRoute,
    SelfSlideSolver,
    SelfStationTable,
)

#: 开关的环境变量名（唯一处定义）。格式：``域名=后端名[,域名=后端名…]``
ENV_VAR = "TICKET_HELPER_KERNEL_BACKENDS"

#: 各功能域的**默认**后端。**唯一处定义**。
#:
#: - ``station`` 仍是 `self`：差分发现上游表有 **11 个错电报码**（见
#:   `tests/test_kernel_station.py`），切过去会让「查不到车」，故不动默认。
#: - ``captcha`` 默认就是 `pjialin`：需求方明确要求打码必须用上游实现
#:   （决策 §10-1/2/3），且它未配置服务地址时会 ``available()=False``，
#:   自动让识别链继续往下走 —— 不会把功能打挂。
#: - ``query_route``（余票查询路径的取法）仍是 `self`：上游那套（读余票页
#:   给出的路径）**更省请求**，但换默认值 = 换行为，要先出差分与证据（v3 §10-17）。
#: - ``login_check``（登录态检测）仍是 `self`：两套的**端点与判活字段都不同**
#:   （``POST /otn/login/checkUser`` + ``data.flag`` vs
#:   ``GET /otn/login/conf`` + ``data.is_login=='Y'``），且上游那条**不带 Referer** ——
#:   真机上很可能被 WAF 拒。差异由 `scripts/login_check_diff.py` 显示。
DEFAULT_BACKENDS: dict[str, str] = {
    "station": "self",
    "captcha": "pjialin",
    "query_route": "self",
    "login_check": "self",
    #: ``qr_check``（扫码轮询的请求体）仍是 `self`：上游多带两个设备指纹 Cookie，
    #: 而本项目不带也真机跑通了 —— 差异留档（`tests/test_qr_baseline.py`），
    #: 换默认值要真机证据。
    "qr_check": "self",
    #: ``order_submit``（下单六步的请求规格）仍是 `self`：
    #: 上游那套有**两处取值不同**（``back_train_date`` 用出发日、``random`` 用随机小数串）、
    #: 且**六步都不带 Referer**（真机上很可能被 WAF 拒）、`check_order_info` 还多一套
    #: 滑块三件套（本项目此前无滑块实现）。差异由 `scripts/order_diff.py` 打印、
    #: `tests/test_order_baseline.py` 逐项断言。**换默认值要真机证据。**
    "order_submit": "self",
    #: ``slide``（下单滑块风控）默认就是上游 `pjialin`：
    #: 本项目**根本没有程序化解法**（`self` 是不可用的回退后端），
    #: 而上游有一套真浏览器拖拽实现 —— 决策「上游有的禁止自己实现」正是这一项。
    #: 它 `available()` 为假时**不报错**、只留一条 WARNING 照旧往下走，
    #: 所以切成默认不会打挂任何东西。
    "slide": "pjialin",
    #: ``pacing``（查询节奏参数）仍是 `self`：上游那套是 **0.5~1.0 秒一次**，
    #: 比本项目（1~8 秒 + 自适应）激进得多 —— 换默认值不是「更接近上游」，
    #: 而是**更容易被限流**。差异由 `scripts/pacing_diff.py` 打印。
    "pacing": "self",
    #: ``heartbeat`` 仍是 `self`：上游**没有地板**（配 1 秒就 1 秒一次），
    #: 而本项目有 30 秒地板。切过去 = 去掉防护，要真机证据。
    "heartbeat": "self",
}

#: 功能域 → {后端名: 构造器}
BUILDERS: dict[str, dict[str, Callable[..., Any]]] = {
    "station": {
        "self": SelfStationTable,
        "pjialin": PjialinStationTable,
    },
    "captcha": {
        "self": SelfCaptchaSolver,
        "pjialin": PjialinCaptchaSolver,
    },
    "query_route": {
        "self": SelfQueryRoute,
        "pjialin": PjialinQueryRoute,
    },
    "login_check": {
        "self": SelfLoginProbe,
        "pjialin": PjialinLoginProbe,
    },
    "qr_check": {
        "self": SelfQrCheck,
        "pjialin": PjialinQrCheck,
    },
    "order_submit": {
        "self": SelfOrderSubmitter,
        "pjialin": PjialinOrderSubmitter,
    },
    "slide": {
        "self": SelfSlideSolver,
        "pjialin": PjialinSlideSolver,
    },
    "pacing": {
        "self": SelfPacing,
        "pjialin": PjialinPacing,
    },
    "heartbeat": {
        "self": SelfHeartbeat,
        "pjialin": PjialinHeartbeat,
    },
}

#: 各功能域可用的后端名（供界面/脚本，避免各处硬写字符串）
BACKEND_ALIASES: dict[str, tuple[str, ...]] = {
    domain: tuple(impls) for domain, impls in BUILDERS.items()
}

_cache: dict[tuple, Any] = {}


def available_backends() -> dict[str, tuple[str, ...]]:
    """``{域名: (后端名…)}``。"""
    return dict(BACKEND_ALIASES)


def _parse_env() -> dict[str, str]:
    raw = (os.environ.get(ENV_VAR) or "").strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        if "=" not in item:
            raise ConfigError(
                f"{ENV_VAR} 格式应为「域名=后端名」，收到 {item!r}；"
                f"例如 {ENV_VAR}=station=pjialin"
            )
        domain, _, backend = item.partition("=")
        domain = domain.strip()
        # **域名写错必须报错**：否则 `staton=pjialin`（少一个 i）会静默无效果，
        # 使用者以为切了，实际跑的还是默认后端 —— 这正是「静默回退」的另一种形态。
        if domain not in BUILDERS:
            raise ConfigError(
                f"{ENV_VAR} 里的功能域名非法：{domain!r}；"
                f"已注册 {', '.join(sorted(BUILDERS))}"
            )
        out[domain] = backend.strip()
    return out


def backend_name(domain: str) -> str:
    """取某功能域当前生效的后端名。非法配置**快速失败**。"""
    if domain not in BUILDERS:
        raise ConfigError(
            f"未知的功能域 {domain!r}；已注册：{', '.join(sorted(BUILDERS))}"
        )
    name = _parse_env().get(domain) or DEFAULT_BACKENDS.get(domain, "self")
    if name not in BUILDERS[domain]:
        raise ConfigError(
            f"{ENV_VAR} 里 {domain} 的后端名非法：{name!r}；"
            f"可选 {', '.join(BUILDERS[domain])}"
        )
    return name


def get(domain: str, **kwargs: Any) -> Any:
    """取某功能域当前生效的实现（**按 (域, 后端, 构造参数) 缓存**）。

    缓存键包含后端名与构造参数 —— 所以**切换开关后拿到的是新实例**，
    而不是上一个后端留下的旧对象（那会让「切了没生效」变成一个很难查的假象）。

    为什么允许构造参数
    ------------------
    打码后端必须知道**服务地址**才能工作，而地址来自使用者配置，
    不可能写死在构造器里。参数进缓存键，因此「换了地址 = 换了实例」，
    不会有「改了配置但还在跑旧地址」的幽灵。

    参数必须可哈希（字符串 / 数字 / 布尔都行）；
    传了不可哈希的值就直接构造、不缓存 —— 与其静默给出陈旧实例，不如多构造一次。
    """
    name = backend_name(domain)
    try:
        key = (domain, name, tuple(sorted(kwargs.items())))
        hash(key)
    except TypeError:
        return BUILDERS[domain][name](**kwargs)
    if key not in _cache:
        _cache[key] = BUILDERS[domain][name](**kwargs)
    return _cache[key]


def reset_cache() -> None:
    """清缓存（测试用：切换开关后强制重建）。"""
    _cache.clear()
