"""查询路径发现域：决定「先试哪条余票查询路径」。

为什么这是**实现选择**（该做可切换的域）
----------------------------------------
12306 的余票查询路径是**灰度轮换**的（``leftTicket/queryZ`` / ``queryA`` /
``query`` / ``queryO``），当前哪条有效是**外部状态**。两套取法都能工作：

===========  ==================================================================
``self``     按固定候选列表**依次盲试**：打错一条 = 一个注定失败的请求
``pjialin``  先读余票页 HTML 里上游用的那个字段拿路径，取不到再退回候选列表
===========  ==================================================================

换的是**取路径的策略**，不是换一个事实 —— 所以它适合做域。

> 对比：余票**字段下标**属协议事实，**不该**做域（那等于允许两套事实）。
> 它的正确做法是拿上游当外部锚点校验，见 `tests/test_query_baseline.py`。

两种策略的共同前提
------------------
**余票页必须先加载**（本项目 :meth:`TicketApi.ensure_session_warm`）。
那条约束原本的理由是「否则被 WAF 拒」，而它顺带把这页 HTML 拿到了手上 ——
所以 ``pjialin`` 策略**不需要任何额外请求**。

契约
----
1. 返回值是**按优先级排列**的路径列表，调用方依序尝试；
2. 返回空列表 = 「没有可用的路径」，调用方按自身逻辑报错（本域不抛）；
3. **必须包含兜底**：任何取不到路径的情况都要退回传入的 ``candidates`` ——
   否则一次页面结构变化就会让查询彻底不可用。
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable


@runtime_checkable
class LeftTicketRoute(Protocol):
    """余票查询路径的**排序**策略。两个后端都必须满足它。"""

    #: 后端名（必须与 `kernel/registry.py` 的注册键一致，由契约测试核对）
    name: str

    def routes(self, *, init_page: str, candidates: Sequence[str]) -> list[str]:
        """给出要按顺序尝试的查询路径。

        ``init_page`` 是余票页 HTML（可能为空串：预热失败时调用方拿不到正文）；
        ``candidates`` 是调用方的兜底候选列表。
        """
        ...


def dedupe(paths: Sequence[str]) -> list[str]:
    """去重并保持顺序（**唯一实现处**）。

    两边各写一遍去重，迟早在「保序 / 去空 / 大小写」上分叉，
    而症状是「某条路径被跳过」—— 看起来像官方把那条下线了。
    """
    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        text = (path or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out
