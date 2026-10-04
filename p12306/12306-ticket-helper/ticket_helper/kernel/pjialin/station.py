"""上游后端 · 站点表域。

数据全部来自上游 `py12306/helpers/station.py` + `data/stations.txt`（逐字 vendor，见
`vendor/pjialin/PROVENANCE.json`）。

**已知分歧（会出现在差分表里，必须写下来）**
--------------------------------------------
1. **空关键字的「常用站」**：本项目有 ``HOT_STATIONS`` 精选常用站；
   上游**没有这个概念**（它是配置驱动的应用，站名由使用者写死）。
   所以本后端在空关键字时按**官方序号**取前 N —— 与 ``self`` 后端的结果**必然不同**。
2. **模糊检索**：上游没有检索功能，两个后端共用 :func:`rank_candidates`
   （本项目的排序逻辑）—— 这处是「用我们的」，已在
   `docs/pjialin内核化总体架构设计方案v3.md` §2 表 C 登记。
"""

from __future__ import annotations

from typing import Any

from ..domains.station import rank_candidates, resolve_code
from .shim import upstream_module


class PjialinStationTable:
    """用上游的站点表数据，实现本项目的 `StationTable` 契约。"""

    name = "pjialin"

    def __init__(self) -> None:
        station_mod = upstream_module("py12306.helpers.station")
        # 上游 `Station` 是 @singleton：多次构造返回同一个实例
        self._station: Any = station_mod.Station()

        # 上游的数据形状：stations=[{key,name,pinyin,id}]，station_kvs={站名: 电报码}
        self._pairs: list[tuple[str, str]] = [
            (str(s.get("name", "")), str(s.get("key", "")))
            for s in self._station.stations
            if s.get("name") and s.get("key")
        ]
        self._code_by_name: dict[str, str] = dict(self._station.station_kvs)
        self._name_by_code: dict[str, str] = {}
        for name, code in self._pairs:
            self._name_by_code.setdefault(code, name)

    # ------------------------------------------------------------------ #
    def search(self, keyword: str = "", *, limit: int = 60) -> list[tuple[str, str]]:
        kw = (keyword or "").strip()
        limit = max(1, int(limit))
        if not kw:
            # 分歧 1：上游没有「常用站」，按官方序号取前 N（见模块 docstring）
            return self._pairs[:limit]
        return rank_candidates(self._pairs, kw, limit)

    def code_of(self, station_name: str) -> str:
        """站名 / 电报码 → 电报码；未知抛 ``ValidationError``（与 self 后端一致）。

        解析规则用**共享**的 :func:`resolve_code`，不是在这里再写一遍 ——
        否则两个后端的「容错规则」会悄悄分叉（「北京南站」在一个后端能用、另一个不能用）。
        """
        return resolve_code(
            station_name,
            lookup=lambda n: self._code_by_name.get(n, ""),
            code_exists=lambda c: c in self._name_by_code,
        )

    def name_of(self, code: str) -> str:
        """电报码 → 站名；查不到时**返回传入的码本身**（与 self 后端一致）。"""
        text = str(code or "")
        return self._name_by_code.get(text.strip().upper(), text)

    def __len__(self) -> int:
        return len(self._pairs)
