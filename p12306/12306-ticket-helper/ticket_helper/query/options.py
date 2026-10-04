"""车次候选拉取（任务配置：白名单 / 黑名单不再手填）。

为什么单独抽一层
----------------
任务配置里的「车次白名单 / 车次黑名单」原本要用户自己敲车次号。可现实是：
用户**根本不知道这条线路当天有哪些车次**，只能先跑去 12306 网页上抄一遍，
抄错一个字符就变成"任务跑了半天一票没抢到"。

所以这里提供「从 12306 拉一张当天的车次表」，让配置从「手敲」变成「勾选」。

和 :class:`~ticket_helper.query.engine.QueryEngine` 的关键区别
--------------------------------------------------------------
``QueryEngine`` 会**按任务配置筛选**（白名单 / 席别 / 时间区间），
而这里要的恰恰是**未筛选的全量车次** —— 用户还没配白名单，拿什么筛？
因此不能复用 ``query_once``，必须直接走 :class:`~ticket_helper.inter.api.TicketApi`。

两条硬约束（都是从既有故障里总结出来的，改这里之前先读一遍）
------------------------------------------------------------
1. **组合数必须封顶**。用户在日期/站点框里随手写三四个逗号，
   组合数就是十几，一次性全打出去等于自找风控。超出部分不查，并明确告诉前端
   ``truncated=True``（而不是悄悄少查几个）。
2. **被风控立刻停手**。``RiskControlError`` 是会话级问题，继续把剩下的组合打完
   只会把「短暂限流」变成「持续限流」，所以直接 break，剩下的组合一个都不发。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from ticket_helper.errors import (
    InterError,
    LoginError,
    RiskControlError,
    TicketHelperError,
    ValidationError,
)
from ticket_helper.inter.api import TicketApi
from ticket_helper.query.engine import TRAIN_TYPE_PREFIX

log = logging.getLogger(__name__)

#: 单次拉取最多查询多少个「日期 × 起止站」组合。
#: 取值偏保守：这个动作是人手点的，正常情况下 1~4 个组合足够；
#: 真有一堆日期也是先配好一个再看，而不是一次全查（那也是风控的味道）。
MAX_COMBOS = 12

#: 结果缓存有效期（秒）。用户点完「拉取」后往往要改几次日期再点回来，
#: 没有缓存就是反复打同一批接口。
CACHE_TTL = 60.0

#: 「像余票的值」——余票只可能是数字 / 有 / 无 / 候补。
#: 为什么要卡这一道：真机 ``result`` 里有个位置放的是**席别编码串**
#: （形如 ``90M0O0W0``，每趟车都一样），解析层把它当成了「动卧余票」，
#: 直接展示就是界面上的「动卧 90M0O0W0」—— 既没意义，还让人以为解析坏了。
_REMAIN_PATTERN = re.compile(r"^\d+(候补)?$")
_REMAIN_WORDS = ("有", "无", "候补")


def _is_remain_text(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if _REMAIN_PATTERN.match(text):
        return True
    return text in _REMAIN_WORDS or text.endswith("候补")


@dataclass
class TrainOption:
    """一条候选车次（同车次跨日期会合并成一条）。"""

    train_code: str
    train_type: str = "其他"
    from_station: str = ""
    to_station: str = ""
    start_time: str = ""
    arrive_time: str = ""
    duration: str = ""
    #: 该车次在本次拉取范围内出现过的日期（保持传入顺序）
    dates: list[str] = field(default_factory=list)
    #: 任意一个日期可预订即为 True
    buyable: bool = False
    #: 所有日期都未到起售时间（抢预售时很有用）
    not_yet_on_sale: bool = False
    #: 席别 → 余票文本（``""`` 表示该日期没这个席别）
    seats: dict[str, str] = field(default_factory=dict)
    #: 有票的席别名（按 12306 返回的席别顺序无关，仅作展示）
    available_seats: list[str] = field(default_factory=list)
    #: 内部车次号（下单接口的 train_no，展示用不到，留给后续排查）
    train_no: str = ""

    @property
    def remain_text(self) -> str:
        """一目了然的余票摘要，例如 ``二等座 5 · 一等座 2``。"""
        if self.available_seats:
            return " · ".join(f"{name} {self.seats.get(name, '')}".strip() for name in self.available_seats)
        for name, value in self.seats.items():
            if value and "候" in value:
                return f"{name} 候补"
        if self.not_yet_on_sale:
            return "未到起售时间"
        return "无票"

    def to_dict(self) -> dict[str, Any]:
        return {
            "train_code": self.train_code,
            "train_type": self.train_type,
            "from_station": self.from_station,
            "to_station": self.to_station,
            "start_time": self.start_time,
            "arrive_time": self.arrive_time,
            "duration": self.duration,
            "dates": list(self.dates),
            "buyable": self.buyable,
            "not_yet_on_sale": self.not_yet_on_sale,
            "seats": dict(self.seats),
            "available_seats": list(self.available_seats),
            "remain_text": self.remain_text,
            "train_no": self.train_no,
        }


class TrainOptionsService:
    """按「日期 × 起止站」组合拉取候选车次。"""

    def __init__(
        self,
        station_map: Any = None,
        *,
        max_combos: int = MAX_COMBOS,
        cache_ttl: float = CACHE_TTL,
    ) -> None:
        self.station_map = station_map
        self.max_combos = max(1, int(max_combos))
        self.cache_ttl = max(0.0, float(cache_ttl))
        self._cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # 缓存
    # ------------------------------------------------------------------ #
    @staticmethod
    def _cache_key(dates: list[str], from_stations: list[str], to_stations: list[str]) -> tuple:
        return (tuple(dates), tuple(from_stations), tuple(to_stations))

    def _cache_get(self, key: tuple) -> dict[str, Any] | None:
        if self.cache_ttl <= 0:
            return None
        with self._lock:
            hit = self._cache.get(key)
        if hit is None:
            return None
        if time.time() - hit[0] > self.cache_ttl:
            return None
        return hit[1]

    def _cache_put(self, key: tuple, payload: dict[str, Any]) -> None:
        if self.cache_ttl <= 0:
            return
        with self._lock:
            self._cache[key] = (time.time(), payload)
            # 上限保护：这个缓存只服务于「反复点拉取」的场景，十几条够了
            if len(self._cache) > 32:
                oldest = min(self._cache.items(), key=lambda kv: kv[1][0])[0]
                self._cache.pop(oldest, None)

    def invalidate(self) -> None:
        """清缓存（配置里的账号/站点发生变化时调用）。"""
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------------ #
    # 拉取
    # ------------------------------------------------------------------ #
    def fetch(
        self,
        api: TicketApi,
        *,
        dates: Iterable[str],
        from_stations: Iterable[str],
        to_stations: Iterable[str],
        refresh: bool = False,
    ) -> dict[str, Any]:
        """拉取候选车次。

        返回值结构::

            {
              "trains":   [TrainOption...],
              "combos":   2,          # 需要查询的组合总数
              "queried":  2,          # 实际查成的组合数
              "truncated": False,     # 是否因为超过 max_combos 被截断
              "risk_limited": False,  # 是否被风控拦下（拦下后不再继续查）
              "errors":   ["2026-10-02 广州南→长沙南: ..."],
              "cached":   False,
            }

        **单个组合失败不算整体失败**：某一天查不到就只在那一天留一条 error，
        其余日期的车次照常返回 —— 否则「有一条线路名称写错」会让整张表变空白。
        """
        date_list = _clean(dates)
        from_list = _clean(from_stations)
        to_list = _clean(to_stations)
        if not date_list or not from_list or not to_list:
            raise ValidationError("需要至少一个乘车日期、一个出发站和一个到达站")

        key = self._cache_key(date_list, from_list, to_list)
        if not refresh:
            cached = self._cache_get(key)
            if cached is not None:
                return {**cached, "cached": True}

        pairs = [(f, t) for f in from_list for t in to_list]
        combos = [(f, t, d) for d in date_list for f, t in pairs]
        truncated = len(combos) > self.max_combos
        if truncated:
            log.info(
                "车次拉取组合数 %d 超过上限 %d，仅查询前 %d 个",
                len(combos),
                self.max_combos,
                self.max_combos,
            )
            combos = combos[: self.max_combos]

        errors: list[str] = []
        risk_limited = False
        queried = 0
        merged: dict[str, TrainOption] = {}

        for from_station, to_station, train_date in combos:
            try:
                from_code = self._code_of(from_station)
                to_code = self._code_of(to_station)
            except ValidationError as exc:
                errors.append(str(exc.message))
                log.warning("车次拉取站点解析失败: %s", exc.message)
                continue

            try:
                tickets = api.query_left_ticket(train_date, from_code, to_code)
            except RiskControlError as exc:
                # 会话级问题：再打下去只会把短暂限流变成持续限流，立刻停手
                risk_limited = True
                errors.append(f"{train_date} {from_station}→{to_station}: {exc.message}")
                log.warning("车次拉取被风控拦下，停止后续组合: %s", exc.message)
                break
            except LoginError as exc:
                # 会话失效同理：后面每个组合都会失败，没必要挨个撞一遍
                errors.append(f"{train_date} {from_station}→{to_station}: {exc.message}")
                log.warning("车次拉取时会话失效，停止后续组合: %s", exc.message)
                break
            except TicketHelperError as exc:
                errors.append(f"{train_date} {from_station}→{to_station}: {_message_of(exc)}")
                log.warning("车次拉取失败 %s %s→%s: %s", train_date, from_station, to_station, exc)
                continue

            queried += 1
            for ticket in tickets:
                _merge(merged, ticket, train_date)

        trains = sorted(merged.values(), key=_sort_key)
        payload: dict[str, Any] = {
            "trains": [t.to_dict() for t in trains],
            "combos": len(combos),
            "queried": queried,
            "truncated": truncated,
            "risk_limited": risk_limited,
            "errors": errors,
            "cached": False,
            "fetched_at": time.time(),
        }
        # 有错就不进缓存：否则用户改完站点名回来，拿到的还是上一次的失败结果
        if not errors:
            self._cache_put(key, payload)
        return payload

    # ------------------------------------------------------------------ #
    def _code_of(self, station: str) -> str:
        """站名 → 电报码；没有站点表时原样透传（mock / 单测场景）。"""
        if self.station_map is None:
            return station
        return self.station_map.code_of(station)


# --------------------------------------------------------------------------- #
def _clean(values: Iterable[str] | None) -> list[str]:
    """去空白、去重且**保持传入顺序**（顺序对用户理解结果有意义）。"""
    result: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _sort_key(option: TrainOption) -> tuple:
    """发车时间升序；发车时间拿不到的排最后（而不是排最前）。"""
    return (option.start_time or "99:99", option.train_code)


def _merge(merged: dict[str, TrainOption], ticket: Any, train_date: str) -> None:
    """把一条余票记录并入候选表（同一车次的多个日期合并成一条）。"""
    code = str(getattr(ticket, "train_code", "") or "").upper()
    if not code:
        return
    seats = {
        name: str(info.remain).strip()
        for name, info in (getattr(ticket, "seats", None) or {}).items()
        if _is_remain_text(getattr(info, "remain", ""))
    }
    available = [
        name
        for name, info in (getattr(ticket, "seats", None) or {}).items()
        if info.available and _is_remain_text(info.remain)
    ]

    option = merged.get(code)
    if option is None:
        merged[code] = TrainOption(
            train_code=code,
            train_type=TRAIN_TYPE_PREFIX.get(code[0], "其他"),
            from_station=str(getattr(ticket, "from_station", "") or ""),
            to_station=str(getattr(ticket, "to_station", "") or ""),
            start_time=str(getattr(ticket, "start_time", "") or ""),
            arrive_time=str(getattr(ticket, "arrive_time", "") or ""),
            duration=str(getattr(ticket, "duration", "") or ""),
            dates=[train_date],
            buyable=bool(getattr(ticket, "buyable", False)),
            not_yet_on_sale=bool(getattr(ticket, "not_yet_on_sale", False)),
            seats=dict(seats),
            available_seats=available,
            train_no=str(getattr(ticket, "train_no", "") or ""),
        )
        return

    if train_date not in option.dates:
        option.dates.append(train_date)
    option.buyable = option.buyable or bool(getattr(ticket, "buyable", False))
    # 只要有一天"未到起售"就不能算未开售，得所有日期都未开售才算
    option.not_yet_on_sale = option.not_yet_on_sale and bool(getattr(ticket, "not_yet_on_sale", False))
    for name, remain in seats.items():
        option.seats[name] = _better_remain(option.seats.get(name, ""), remain)
    for name in available:
        if name not in option.available_seats:
            option.available_seats.append(name)


def _better_remain(old: str, new: str) -> str:
    """同日期的同一席别取「更有信息量」的那个值。

    官方对同一席别会给出 ``5`` / ``有`` / ``候补`` / ``无``，
    直接覆盖会让「有一张票」被后一次的「无」冲掉，用户看到的就成了无票。
    规则：带数字的优先于没数字的，其余保持先到的。
    """
    if not old:
        return new
    if any(ch.isdigit() for ch in new) and not any(ch.isdigit() for ch in old):
        return new
    return old


def _message_of(exc: BaseException) -> str:
    if isinstance(exc, InterError):
        return exc.message
    return str(exc) or "未知错误"
