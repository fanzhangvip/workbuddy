"""查询路径策略差分表：**两个后端各自会发出哪些请求**。

为什么这张表值得单独存在
------------------------
`scripts/kernel_diff.py` 比的是「请求体长什么样」。这一层比的是
**「要发几次请求才打到对的那条」** —— 而它正是本域的存在理由：

- ``self``：候选列表盲试。候选里只有一条当前有效，其余每条都是
  **注定被拒**的请求，而拒的表现（HTML 错误页）与「被风控」长得一样。
- ``pjialin``：先读余票页给出的路径。**不多发任何请求**
  （那张页面本来就要加载，见 `TicketApi.ensure_session_warm`）。

用法::

    python scripts/query_route_diff.py

输出是给人看的，但它断言的东西在
``tests/test_kernel_query.py::test_the_strategy_decides_which_path_is_requested``
里被钉住（判据是 mock 记下的**真实请求路径**）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from ticket_helper.inter import urls as urls_mod  # noqa: E402
from ticket_helper.kernel import registry  # noqa: E402

DOMAIN = "query_route"


def _routes_with(backend: str, page: str) -> list[str]:
    """指定后端跑一次，返回它给出的路径顺序。"""
    previous = os.environ.get(registry.ENV_VAR)
    os.environ[registry.ENV_VAR] = f"{DOMAIN}={backend}"
    registry.reset_cache()
    try:
        domain = registry.get(DOMAIN)
        return list(
            domain.routes(init_page=page, candidates=urls_mod.LEFT_TICKET_QUERY_CANDIDATES)
        )
    finally:
        if previous is None:
            os.environ.pop(registry.ENV_VAR, None)
        else:
            os.environ[registry.ENV_VAR] = previous
        registry.reset_cache()


def _requests_to_reach(routes: list[str], truth: str) -> int | None:
    """按顺序尝试，打到 ``truth`` 需要几次请求（打不到返回 None）。"""
    for index, path in enumerate(routes, start=1):
        if path == truth:
            return index
    return None


SCENARIOS = [
    (
        "余票页宣称 leftTicket/queryA（= 官方当前路径）",
        "<html><script>var CLeftTicketUrl = 'leftTicket/queryA';</script></html>",
        "/otn/leftTicket/queryA",
    ),
    (
        "余票页宣称 leftTicket/queryZ（与本项目候选第一条相同）",
        "<html><script>var CLeftTicketUrl = 'leftTicket/queryZ';</script></html>",
        "/otn/leftTicket/queryZ",
    ),
    (
        "页面里没有该字段（结构变了 / 预热失败）",
        "<html><body>nothing here</body></html>",
        urls_mod.LEFT_TICKET_QUERY_CANDIDATES[0],
    ),
]


def main() -> int:
    self_name, up_name = registry.available_backends()[DOMAIN]
    print("=" * 78)
    print("查询路径策略差异表（同一个会话、同一张余票页）")
    print("=" * 78)
    print(f"候选列表（两家共用的兜底）：{list(urls_mod.LEFT_TICKET_QUERY_CANDIDATES)}")

    problems: list[str] = []
    for title, page, truth in SCENARIOS:
        mine = _routes_with(self_name, page)
        theirs = _routes_with(up_name, page)
        print(f"\n[{title}]")
        print(f"    {self_name:<14}: {' → '.join(mine)}")
        print(f"    {up_name:<14}: {' → '.join(theirs)}")
        cost_self = _requests_to_reach(mine, truth)
        cost_up = _requests_to_reach(theirs, truth)
        print(f"    打到「对的那条」需要几次请求：{self_name}={cost_self}  {up_name}={cost_up}")
        if truth not in mine or truth not in theirs:
            problems.append(f"{title}: 有一条路径打不到 {truth}（兜底丢了？）")
        if cost_up is not None and cost_self is not None and cost_up > cost_self:
            problems.append(f"{title}: 上游策略比本项目多花请求（与文档结论相反）")

    print("\n" + "-" * 78)
    if problems:
        print("✘ 与预期不符：")
        for item in problems:
            print(f"    - {item}")
        return 1
    print("✔ 两家都不会漏掉「对的那条」；本项目盲试在前两种场景下会多花请求。")
    print("")
    print("⚠️  这张表**只说明差在哪、差多少请求**，不说明该不该切默认：")
    print("   切默认 = 换行为，要先有真机证据（v3 §10-17）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
