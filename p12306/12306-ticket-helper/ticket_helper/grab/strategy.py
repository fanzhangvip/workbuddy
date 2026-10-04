"""车次 / 席别优先级选择策略（GR-03 / GR-04）。

优先级由配置驱动，本模块只负责「从候选里挑下一个该试的」，
是纯逻辑，便于单元测试（GR-T4）。

排序规则：
1. 车次优先级：白名单顺序即优先级（用户把最想要的车次放最前）；
   白名单外的车次排在白名单之后；
2. 席别优先级：按 ``seat_types`` 配置顺序（如 二等座 → 一等座 → 无座）；
3. 同优先级按历时升序（先到先得，符合购票直觉）。
"""

from __future__ import annotations

from typing import Iterable

from ticket_helper.config.settings import TaskConfig
from ticket_helper.query.engine import Candidate, QueryEngine
from ticket_helper.inter.parser import TrainTicket

#: 席别「舒适度」参考序（当用户未配置 seat_types 时兜底）
DEFAULT_SEAT_PREFERENCE = ["二等座", "一等座", "硬卧", "软卧", "硬座", "无座"]


class PickStrategy:
    """候选选择策略。"""

    def __init__(self, task: TaskConfig) -> None:
        self.task = task
        self.train_priority = [t.strip().upper() for t in task.trains_whitelist if t.strip()]
        self.seat_priority = list(task.seat_types) or list(DEFAULT_SEAT_PREFERENCE)

    # ------------------------------------------------------------------ #
    def train_rank(self, train_code: str) -> int:
        code = (train_code or "").upper()
        try:
            return self.train_priority.index(code)
        except ValueError:
            return len(self.train_priority)

    def seat_rank(self, seat_type: str) -> int:
        try:
            return self.seat_priority.index(seat_type)
        except ValueError:
            return len(self.seat_priority)

    def rank(self, ticket: TrainTicket, seat_type: str) -> tuple[int, int, int]:
        return (self.train_rank(ticket.train_code), self.seat_rank(seat_type), ticket.duration_minutes)

    def sort(self, candidates: Iterable[Candidate]) -> list[Candidate]:
        return sorted(candidates, key=lambda c: c.sort_key)

    def next_candidate(self, candidates: Iterable[Candidate], *, exclude: set[str] | None = None) -> Candidate | None:
        """返回下一个候选；``exclude`` 里是已尝试失败的键（车次|日期|席别）。"""
        skip = exclude or set()
        for candidate in self.sort(candidates):
            key = self.key_of(candidate)
            if key in skip:
                continue
            return candidate
        return None

    @staticmethod
    def key_of(candidate: Candidate) -> str:
        ticket = candidate.ticket
        return f"{ticket.train_code}|{ticket.train_date}|{ticket.from_station_code}|{ticket.to_station_code}|{candidate.seat.seat_type}"

    def describe(self) -> dict[str, object]:
        return {
            "train_priority": self.train_priority,
            "seat_priority": self.seat_priority,
            "whitelist_mode": bool(self.train_priority),
        }

    def apply_config(self, task: TaskConfig) -> None:
        self.task = task
        self.train_priority = [t.strip().upper() for t in task.trains_whitelist if t.strip()]
        self.seat_priority = list(task.seat_types) or list(DEFAULT_SEAT_PREFERENCE)
