"""登录态探测域（决策 C3：**请求构造用上游，判定保留**）。

背景：`/otn/login/checkUser`（本项目）与 `/otn/login/conf`（上游）都能表达
「当前会话是否登录」，但**请求形态与判活字段都不同**：

===============  =============================  ==========================
后端             请求                           判活字段
===============  =============================  ==========================
``self``         ``POST /otn/login/checkUser``  ``data.flag``（bool）
``pjialin``      ``GET  /otn/login/conf``       ``data.is_login`` == ``'Y'``
===============  =============================  ==========================

所以这条门面**只固定两件事**：

1. **怎么问** —— :class:`LoginProbeSpec` 的 ``path`` / ``method`` / ``data`` / ``referer``；
2. **从响应哪里读结论** —— ``alive_paths`` / ``alive_values``。

而「回 HTML 一律判失效」「拿不到结论一律判不可用」这些是**真机踩出来的硬约束**
（本项目曾把「判不出来」当「还活着」，结果是界面显示在线、之后每个请求都被拒），
两套后端都必须遵守 —— 它们留在接口层（`inter/api.py`），不在这条门面上。

这正是 C3 决策的原话：**请求用上游，判定保留**。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: 判活结果的三个取值。**唯一处定义** —— 别处不许再写字符串字面量。
#:
#: - :data:`ALIVE`：拿到了**明确的肯定**信号；
#: - :data:`DEAD`：拿到了**明确的否定**信号；
#: - :data:`UNKNOWN`：拿到了响应，但读不出结论（字段不在、结构变了）。
#:
#: ⚠️ 第三态不能并进 :data:`DEAD`：本项目在「人证核验」上就吃过这个亏 ——
#: 把「判不出来」当「未核验」，结果正常账号被永久挡住。
#: 登录态这边同理：把「读不出结论」当「已失效」会导致**反复重新登录**。
ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"

_VERDICTS = (ALIVE, DEAD, UNKNOWN)

#: 找不到字段时用的哨兵 —— 用来区分「键不存在」与「键存在但值是 ``None``」。
#: 这个区分是必要的：`self` 后端的现行行为是「``flag`` 键在就按它的真值算」，
#: 于是 ``{"flag": None}`` 判 False（而不是「读不出」）。
_MISSING = object()


@dataclass(frozen=True)
class LoginProbeSpec:
    """一次登录态探测的完整规格（**全是数据，没有逻辑**）。

    做成数据而不是「后端方法」，是因为按 C3 决策，两套实现**只有这些不同**；
    逻辑（怎么判、失败怎么办）必须是共享的一份，否则改一处漏一处。
    """

    #: 后端名（必须与 `kernel/registry.py` 的注册键一致，由契约测试核对）
    name: str
    #: 站内路径，如 ``/otn/login/checkUser``
    path: str
    #: ``"post"`` / ``"get"``
    method: str
    #: 请求体（POST）或查询参数（GET）
    data: Mapping[str, Any] = field(default_factory=dict)
    #: 同源 Referer 路径（空串 = 不带）。真机上「没逛过页面」的请求会被 WAF 拒，
    #: 但**上游那条本来就没带** —— 差异如实保留，由差分表显示出来。
    referer: str = ""
    #: 判活字段的候选点号路径（写法与上游一致），按顺序试第一个「存在」的。
    alive_paths: tuple[str, ...] = ()
    #: 明确表示「活着」的取值集合；``None`` = 按 Python 真值判断。
    #: 上游是 ``data.is_login == 'Y'``，所以它必须显式给 ``("Y",)`` ——
    #: 否则 ``is_login="N"`` 会被 Python 真值判成「活着」（非空字符串）。
    alive_values: tuple[str, ...] | None = None


@runtime_checkable
class LoginProbe(Protocol):
    """登录态探测后端。两个后端都必须满足它。"""

    #: 后端名（必须与注册表键一致）
    name: str

    def spec(self) -> LoginProbeSpec:
        """返回本次探测的规格。"""
        ...


def dig(payload: Any, path: str) -> Any:
    """按点号路径取值（``"data.is_login"``）；任一层缺失返回 :data:`_MISSING`。

    写法与上游一致（上游正是 ``result.get('data.is_login')``），
    这样「上游的字段路径」可以直接搬进 ``alive_paths``，不用翻译。
    """
    current = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def alive_of(payload: Any, spec: LoginProbeSpec) -> str:
    """从响应里判读登录态，返回 :data:`ALIVE` / :data:`DEAD` / :data:`UNKNOWN`。

    **这是全项目唯一一处「登录态怎么判」的实现。**
    两套后端共用它，所以不存在「切换后端顺手改了判定」的可能。
    """
    for path in spec.alive_paths:
        value = dig(payload, path)
        if value is _MISSING:
            # 「键不存在」≠「值为假」：前者是「读不出」，继续找下一个候选；
            # 后者是明确的信号，按下面的逻辑定论。
            continue
        if spec.alive_values is not None:
            allowed = {str(v).strip().upper() for v in spec.alive_values}
            return ALIVE if str(value).strip().upper() in allowed else DEAD
        # 未指定取值集合 → 按 Python 真值（与 `self` 后端的现行行为一致：
        # 它本来就是 `bool(data["flag"])`，而 `bool("false")` 是 True ——
        # 这个「怪但忠实」的语义必须保留，改掉就等于悄悄换了行为）。
        return ALIVE if bool(value) else DEAD
    return UNKNOWN


assert set(_VERDICTS) == {ALIVE, DEAD, UNKNOWN}
