"""查询路径发现域 · 本项目写法（**默认后端**）：固定候选列表盲试。

⚠️ **本文件的输出必须与引入本域之前逐字节一致**（`list(candidates)`）。
它的存在有两个目的：① 让「切换」可被对照；② 提供**一键回退**的落点。

盲试的代价（写清楚，免得以后有人以为它没成本）
----------------------------------------------
候选列表里当前只有一条有效，其余每条都是一次**注定被拒**的请求。
被拒的表现是 HTML 错误页 —— 与「被风控」长得一样，所以这些无用请求
既浪费配额，又**污染风控判断**（本项目已有「被拒即停」的硬约束）。
不换默认值的原因不是「它更好」，而是「换默认值 = 换行为」，
要先有差分与证据（见 `docs/pjialin内核化总体架构设计方案v3.md` §10-17）。
"""

from __future__ import annotations

from typing import Sequence

from ..domains.query_route import dedupe

#: 与注册表键一致的唯一名字
NAME = "self"


class SelfQueryRoute:
    """原样返回调用方给的候选列表（现状）。"""

    name = NAME

    def routes(self, *, init_page: str, candidates: Sequence[str]) -> list[str]:
        # 刻意忽略 init_page：本项目此前不读它。
        # 若这里顺手用上它，就会**同时**改变 self 后端的行为，
        # 于是「默认行为零变化」这条底线就没了。
        return dedupe(candidates)
