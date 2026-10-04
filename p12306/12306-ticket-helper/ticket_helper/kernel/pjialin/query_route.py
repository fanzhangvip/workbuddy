"""查询路径发现域 · 上游写法（``pjialin/py12306``）。

上游怎么做
----------
``query/query.py::get_query_api_type``::

    response = self.session.get(API_QUERY_INIT_PAGE)      # 余票页
    res = re.search(r'var CLeftTicketUrl = \'(.*)\';', response.text)
    self.api_type = res.group(1)                          # 例如 leftTicket/queryZ

也就是说：**余票页自己会告诉我们当前该用哪条查询路径**。
本项目早就加载了这张页面（`ensure_session_warm`），只是一直没读这个字段。

本文件不手抄那条正则 —— 它来自 ``vendor/pjialin/ticket_facts.py``，
由 `scripts/build_vendor_pjialin.py` 从上游原件**逐字切出**并可 `--check` 复验。

与上游的**两处刻意不同**（写清楚，否则会以为是抄漏了）
------------------------------------------------------
1. 上游把结果缓存成实例属性 ``api_type`` 并在失败时**递归重试 + sleep**
   （``return cls.get_query_api_type()``）—— 无限递归，且会卡住调用线程。
   本后端**不递归**：取不到就返回兜底候选。理由：递归重试等于对一个
   「页面结构变了」的确定性失败做无限重试，而调用方（余票查询）本来就有重试。
2. 上游取到路径后只打那一条；本后端把它**放在最前**并保留候选兜底 ——
   页面给出的路径万一失效，仍有退路。
"""

from __future__ import annotations

import re
from typing import Sequence

from ..domains.query_route import dedupe
from .shim import install

#: 与注册表键一致的唯一名字
NAME = "pjialin"


class PjialinQueryRoute:
    """用上游正则从余票页 HTML 读查询路径，取不到则退回候选列表。"""

    name = NAME

    def routes(self, *, init_page: str, candidates: Sequence[str]) -> list[str]:
        fallback = dedupe(candidates)
        path = self._path_from_page(init_page)
        if not path:
            return fallback
        return dedupe([path, *fallback])

    # ------------------------------------------------------------------ #
    @staticmethod
    def _path_from_page(init_page: str) -> str:
        """从余票页取出查询路径（**本项目用的形态**：以 ``/`` 开头的站内路径）。

        形态差异是这里必须处理的第一件事：页面里给的是 **URL 模板里的 ``{type}`` 片段**
        （``leftTicket/queryO``），而本项目的候选列表是**完整站内路径**
        （``/otn/leftTicket/queryZ``）。两个后端返回不同形态，调用方就没法统一使用。

        转换方式**不是我们发明的拼接**：用上游自己的 URL 模板
        （``vendor/pjialin/ticket_facts.py::ROUTE_URL_TEMPLATE``，从
        ``helpers/api.py`` 逐字切出）把 ``type`` 还原成上游会请求的 URL，
        再取路径部分。这样「片段 → 路径」的规则有出处、可复验。

        失败一律返回空串（**不抛**）：调用方是余票查询，它有自己的重试与报错口径；
        这里再抛一次只会把「页面结构变了」伪装成「查询失败」。
        """
        if not init_page:
            return ""
        try:
            pattern, group = _route_facts("ROUTE_PATTERN", "ROUTE_GROUP")
            template = _route_facts("ROUTE_URL_TEMPLATE")[0]
            match = re.search(pattern, init_page)
            if match is None:
                return ""
            segment = str(match.group(group)).strip()
            if not segment:
                return ""
            url = template.format(
                type=segment, left_date="", left_station="", arrive_station=""
            )
            return url.split("?", 1)[0]
        except Exception:  # noqa: BLE001 - 见 docstring
            return ""


def _route_facts(*names: str) -> tuple:
    """按名字取生成物里的上游协议事实（带 sha256 出处）。"""
    install()
    import ticket_facts  # noqa: PLC0415  （生成物：位于 sys.path 上的 vendor/pjialin）

    return tuple(getattr(ticket_facts, name) for name in names)
