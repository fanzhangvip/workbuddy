"""余票查询引擎（4.3 / QY-01 ~ QY-07）。

职责：
- 把任务配置展开为「多日期 × 多起止站」的查询矩阵（QY-01/QY-02）；
- 调用接口层拿到余票，做车次 / 席别 / 时间区间筛选（QY-03/04/05）；
- 结构化输出并落库（QY-07），同时采集查询延迟（LG-04）。

本模块**不含下单逻辑**，只回答「现在有哪些票可用」，
便于单元测试与复用（候补引擎也需要查询能力）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from ticket_helper.config.settings import TaskConfig
from ticket_helper.errors import (
    InterError,
    LoginError,
    RiskControlError,
    TrainDateUnavailableError,
    ValidationError,
)
from ticket_helper.inter.api import TicketApi
from ticket_helper.inter.parser import SEAT_CODE, SeatInfo, TrainTicket
from ticket_helper.logger import metrics
from ticket_helper import train_date as train_date_util

log = logging.getLogger(__name__)

#: 车次首字母 → 类型（QY-03 车次类型筛选）
TRAIN_TYPE_PREFIX: dict[str, str] = {
    "G": "高铁",
    "D": "动车",
    "C": "动车",
    "Z": "直达",
    "T": "特快",
    "K": "快速",
    "Y": "普快",
}


@dataclass
class QueryResult:
    """一次查询的汇总结果。"""

    tickets: list[TrainTicket] = field(default_factory=list)
    latency_ms: int = 0
    combos: int = 0
    errors: list[str] = field(default_factory=list)
    queried_at: float = field(default_factory=time.time)

    @property
    def has_ticket(self) -> bool:
        return any(t.has_any_ticket for t in self.tickets)

    def to_dict(self) -> dict[str, Any]:
        return {
            "latency_ms": self.latency_ms,
            "combos": self.combos,
            "errors": self.errors,
            "has_ticket": self.has_ticket,
            "queried_at": self.queried_at,
            "tickets": [t.to_dict() for t in self.tickets],
        }


@dataclass
class Candidate:
    """匹配到的候选（车次 + 席别），供抢票引擎消费。"""

    ticket: TrainTicket
    seat: SeatInfo
    train_rank: int
    seat_rank: int

    @property
    def sort_key(self) -> tuple[int, int, int]:
        # 车次优先级 → 席别优先级 → 历时（早到优先）
        return (self.train_rank, self.seat_rank, self.ticket.duration_minutes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "train_code": self.ticket.train_code,
            "train_date": self.ticket.train_date,
            "from_station": self.ticket.from_station,
            "to_station": self.ticket.to_station,
            "start_time": self.ticket.start_time,
            "arrive_time": self.ticket.arrive_time,
            "seat_type": self.seat.seat_type,
            "remain": self.seat.remain,
            "train_rank": self.train_rank,
            "seat_rank": self.seat_rank,
        }


class TrainFilter:
    """车次 / 席别 / 时间区间筛选器（纯函数，便于单测）。"""

    def __init__(self, task: TaskConfig) -> None:
        self.task = task
        self.whitelist = {t.strip().upper() for t in task.trains_whitelist if t.strip()}
        self.blacklist = {t.strip().upper() for t in task.trains_blacklist if t.strip()}
        self.train_types = set(task.train_types)
        self.depart_range = self._parse_time_range(task.depart_time_range)

    @staticmethod
    def _parse_time_range(value: list[str]) -> tuple[int, int] | None:
        if not value or len(value) != 2:
            return None
        try:
            start = _to_minutes(value[0])
            end = _to_minutes(value[1])
        except ValueError:
            return None
        if start > end:  # 跨天（如 22:00-06:00）
            return (start, end + 24 * 60)
        return (start, end)

    def train_type_of(self, train_code: str) -> str:
        if not train_code:
            return "其他"
        return TRAIN_TYPE_PREFIX.get(train_code[0].upper(), "其他")

    def accept_train(self, ticket: TrainTicket) -> bool:
        code = (ticket.train_code or "").upper()
        if self.blacklist and code in self.blacklist:
            return False
        if self.whitelist and code not in self.whitelist:
            return False
        if self.train_types:
            if self.train_type_of(code) not in self.train_types:
                return False
        if self.depart_range is not None:
            minutes = _to_minutes(ticket.start_time)
            start, end = self.depart_range
            if end > 24 * 60:
                # 跨天区间：命中 [start, 24h) ∪ [0, end-24h)
                if not (minutes >= start or minutes <= end - 24 * 60):
                    return False
            elif not (start <= minutes <= end):
                return False
        return True

    def filter(self, tickets: Iterable[TrainTicket]) -> list[TrainTicket]:
        return [t for t in tickets if self.accept_train(t)]


class QueryEngine:
    """余票查询引擎。"""

    def __init__(self, api: TicketApi, task: TaskConfig, *, db: Any | None = None) -> None:
        self.api = api
        self.task = task
        self.db = db
        self.filter = TrainFilter(task)
        self.query_count = 0
        self.error_count = 0
        #: 最近一次查询的完整结果。给看板用 —— 只有「命中的候选」是**答不出**
        #: 「为什么一个候选都没有」的：查询失败、日期不可查、车次被筛掉，
        #: 三种情况下候选都是空，但对使用者要做的事完全不同。
        self.last_result: QueryResult | None = None

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def feed_tickets(self, tickets: list[TrainTicket]) -> list[TrainTicket]:
        """直接注入余票数据（候补引擎 / Web 端手动刷新 / 测试复用）。"""
        filtered = self.filter.filter(tickets)
        self._persist(filtered, latency_ms=0)
        return filtered

    def query_once(self, *, with_price: bool = False, with_stops: bool = False) -> QueryResult:
        """执行一轮完整查询（覆盖全部日期 × 起止站组合）。

        **已过去的日期不会发请求**：它们是**确定**不可查的（12306 对这类日期回的是
        与风控完全相同的 HTML 错误页，见 :mod:`ticket_helper.train_date`），
        发了只会白挨一次拒绝，还有可能把会话真的打成限流。
        原因会写进 ``result.errors``，让界面能如实说明「为什么没有票」。

        「还没开售」的日期**照常请求**：那条规则随时间会变（明天可能就开售了），
        提前拦掉会变成「明明开售了却不查」——那是更糟的失效。
        """
        started = time.perf_counter()
        result = QueryResult()
        combos = self.task.combos()
        result.combos = len(combos)

        for from_station, to_station, train_date in combos:
            if train_date_util.classify(train_date) == train_date_util.DATE_PAST:
                problem = train_date_util.explain(train_date)
                result.errors.append(f"{train_date} {from_station}→{to_station}: {problem}")
                log.warning("任务[%s]跳过已过去的乘车日期: %s", self.task.name, problem)
                continue

            try:
                from_code = self.api.stations.code_of(from_station)
                to_code = self.api.stations.code_of(to_station)
            except ValidationError as exc:
                result.errors.append(str(exc))
                log.error("任务[%s]站点解析失败: %s", self.task.name, exc)
                continue

            try:
                tickets = self.api.query_left_ticket(train_date, from_code, to_code)
            except (LoginError, RiskControlError, TrainDateUnavailableError):
                # 这三类都不是「单个组合的局部问题」：
                #   - 会话失效 / 被风控 → 继续打其它组合只会加剧风控；
                #   - 日期不可查（还没开售）→ 上层要按「慢速等开售」处理，
                #     而不是按 query_interval 一秒一次猛冲（那会把会话真的打成限流）。
                # 所以必须抛给上层统一处理。其余错误则按组合吞掉，保住整体查询的可用性。
                self.error_count += 1
                metrics.incr("query.error")
                raise
            except InterError as exc:
                self.error_count += 1
                metrics.incr("query.error")
                result.errors.append(f"{train_date} {from_station}→{to_station}: {exc.message}")
                log.warning("任务[%s]查询失败 %s %s→%s: %s", self.task.name, train_date, from_station, to_station, exc)
                continue

            accepted = self.filter.filter(tickets)
            for ticket in accepted:
                if with_price and ticket.train_code:
                    ticket.price_map = self.api.query_ticket_price(
                        ticket.train_no or ticket.train_code,
                        ticket.from_station_no,
                        ticket.to_station_no,
                        [SEAT_CODE.get(s, "") for s in self.task.seat_types if s in SEAT_CODE],
                        ticket.train_date,
                    )
                if with_stops:
                    ticket.stop_stations = self.api.query_stop_stations(
                        ticket.train_no or ticket.train_code,
                        ticket.from_station_code,
                        ticket.to_station_code,
                        ticket.train_date,
                    )
            result.tickets.extend(accepted)

        latency_ms = int((time.perf_counter() - started) * 1000)
        result.latency_ms = latency_ms
        self.query_count += 1
        self.last_result = result
        metrics.incr("query.total")
        metrics.observe("query.latency", latency_ms)
        self._persist(result.tickets, latency_ms=latency_ms)
        return result

    def _persist(self, tickets: list[TrainTicket], *, latency_ms: int) -> None:
        """查询结果落库（QY-07 / LG-02）。"""
        if self.db is None or not tickets:
            return
        try:
            records: list[dict[str, Any]] = []
            for ticket in tickets:
                for seat in ticket.seats.values():
                    records.append(
                        {
                            "train_no": ticket.train_code,
                            "seat_type": seat.seat_type,
                            "remain": seat.remain,
                            "latency_ms": latency_ms,
                            "hit": int(seat.available),
                        }
                    )
            if records:
                self.db.save_query_records(self.task.id or 0, records)
        except Exception as exc:  # noqa: BLE001 - 落库失败不影响抢票
            log.error("查询结果落库失败: %s", exc)

    # ------------------------------------------------------------------ #
    # 候选筛选
    # ------------------------------------------------------------------ #
    def build_candidates(
        self,
        tickets: list[TrainTicket],
        *,
        seat_scope: str = "available",
    ) -> list[Candidate]:
        """按「车次优先级 → 席别优先级」生成有序候选列表（GR-03 / GR-04）。

        ``seat_scope``:
        - ``available``：只取有票席别（抢票用）
        - ``standby``：只取仅候补席别（候补用）
        - ``any``：两者都取
        """
        train_rank = {code.upper(): i for i, code in enumerate(self.task.trains_whitelist)}
        default_train_rank = len(train_rank)
        seat_rank = {name: i for i, name in enumerate(self.task.seat_types)}
        default_seat_rank = len(seat_rank)

        candidates: list[Candidate] = []
        for ticket in tickets:
            if not ticket.buyable and seat_scope == "available":
                # 未到起售时间 / 停售的车次不能进入下单流程
                continue
            if seat_scope == "standby":
                seats = ticket.standby_seats(self.task.seat_types)
            elif seat_scope == "available":
                seats = ticket.available_seats(self.task.seat_types)
            else:
                seats = list(ticket.available_seats(self.task.seat_types)) + list(
                    ticket.standby_seats(self.task.seat_types)
                )
            for seat in seats:
                candidates.append(
                    Candidate(
                        ticket=ticket,
                        seat=seat,
                        train_rank=train_rank.get(ticket.train_code.upper(), default_train_rank),
                        seat_rank=seat_rank.get(seat.seat_type, default_seat_rank),
                    )
                )
        candidates.sort(key=lambda c: c.sort_key)
        return candidates

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task.id,
            "task_name": self.task.name,
            "query_count": self.query_count,
            "error_count": self.error_count,
            "interval": self.task.query_interval,
        }

    def apply_config(self, task: TaskConfig) -> None:
        """配置热更新。"""
        self.task = task
        self.filter = TrainFilter(task)


# --------------------------------------------------------------------------- #
def _to_minutes(value: str) -> int:
    """``"08:30"`` → 510，非法值抛 ValueError。"""
    text = (value or "").strip()
    if not text:
        raise ValueError("时间为空")
    hours, _, minutes = text.partition(":")
    hours_i = int(hours)
    minutes_i = int(minutes or 0)
    if not (0 <= hours_i <= 23 and 0 <= minutes_i <= 59):
        raise ValueError(f"时间非法: {value}")
    return hours_i * 60 + minutes_i
