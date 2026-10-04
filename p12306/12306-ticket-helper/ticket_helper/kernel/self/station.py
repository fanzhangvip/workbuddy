"""本项目后端 · 站点表域。

适配 `ticket_helper/inter/station.py::StationMap` —— **零行为变化**：
本类刻意不重写任何逻辑，只做「形状对齐」。
任何「顺手优化」都会让差分表出现噪声，把真差异淹掉。
"""

from __future__ import annotations

from typing import Any

from ticket_helper.inter.station import StationMap, get_station_map


class SelfStationTable:
    """转发给既有的 `StationMap`。"""

    name = "self"

    def __init__(self, table: StationMap | None = None) -> None:
        self._table: Any = table if table is not None else get_station_map()

    def search(self, keyword: str = "", *, limit: int = 60) -> list[tuple[str, str]]:
        return self._table.search(keyword, limit=limit)

    def code_of(self, station_name: str) -> str:
        return self._table.code_of(station_name)

    def name_of(self, code: str) -> str:
        return self._table.name_of(code)

    def __len__(self) -> int:
        return len(self._table)
