"""12306 响应解析（QY-T6）。

余票接口返回 ``result`` 数组，每项是 ``|`` 分隔的定长字符串。字段位置是官方
协议的一部分，这里集中定义并配单元测试，避免各业务模块散落硬编码下标。
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from ticket_helper.errors import ResponseParseError

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 余票 result 字段下标（官方协议）
# --------------------------------------------------------------------------- #
IDX_SECRET_STR = 0
IDX_BUTTON_TEXT = 1
#: 内部车次号（如 24000G6102A0），下单接口的 train_no 参数用它，不是票面车次
IDX_TRAIN_NO = 2
IDX_TRAIN_CODE = 3          # 票面车次，如 G6102
IDX_START_STATION_CODE = 4
IDX_END_STATION_CODE = 5
IDX_FROM_STATION_CODE = 6
IDX_TO_STATION_CODE = 7
IDX_START_TIME = 8
IDX_ARRIVE_TIME = 9
IDX_DURATION = 10           # 历时，如 02:35
IDX_CAN_WEB_BUY = 11        # Y=可预订 / N=不可 / IS_TIME_NOT_BUY=未到起售时间
IDX_YP_INFO = 12
IDX_START_TRAIN_DATE = 13
IDX_TRAIN_SEAT_FEATURE = 14
IDX_SEAT_TYPES = 15
IDX_LOCATION_CODE = 16
IDX_FROM_STATION_NO = 17
IDX_TO_STATION_NO = 18
IDX_IS_SUPPORT_CARD = 19
IDX_CONTROLLED_TRAIN_FLAG = 20
IDX_GG_NUM = 21             # 高级动卧
IDX_GR_NUM = 22             # 高级软卧
IDX_QT_NUM = 23             # 其他
IDX_RW_NUM = 24             # 软卧
IDX_RZ_NUM = 25             # 软座
IDX_TZ_NUM = 26             # 特等座
IDX_WZ_NUM = 27             # 无座
IDX_YB_NUM = 28             # 硬卧包房
IDX_YW_NUM = 29             # 硬卧
IDX_YZ_NUM = 30             # 硬座
IDX_ZE_NUM = 31             # 二等座
IDX_ZY_NUM = 32             # 一等座
IDX_SWZ_NUM = 33            # 商务座
IDX_SRRB_NUM = 34           # 动卧
IDX_YP_EX = 35
IDX_SEAT_TYPES_ALT = 36

#: 席别中文名 → result 下标
SEAT_INDEX: dict[str, int] = {
    "商务座": IDX_SWZ_NUM,
    "特等座": IDX_TZ_NUM,
    "一等座": IDX_ZY_NUM,
    "二等座": IDX_ZE_NUM,
    "高级软卧": IDX_GR_NUM,
    "软卧": IDX_RW_NUM,
    "硬卧": IDX_YW_NUM,
    "软座": IDX_RZ_NUM,
    "硬座": IDX_YZ_NUM,
    "无座": IDX_WZ_NUM,
    "动卧": IDX_SRRB_NUM,
}

#: 席别中文名 → 12306 下单用的席别编码
SEAT_CODE: dict[str, str] = {
    "商务座": "9",
    "特等座": "P",
    "一等座": "M",
    "二等座": "O",
    "高级软卧": "6",
    "软卧": "4",
    "硬卧": "3",
    "软座": "2",
    "硬座": "1",
    "无座": "1",
    "动卧": "F",
}

#: 席别编码 → 中文名（反向）
SEAT_NAME_BY_CODE: dict[str, str] = {v: k for k, v in SEAT_CODE.items()}

#: 无票 / 停售的占位值
NO_TICKET_MARKERS = ("无", "", "--", "*")
#: 候补标记：有余票但标注「候补」时官方返回带「候」或数字+"候"
STANDBY_MARKERS = ("候补",)

MIN_RESULT_FIELDS = 36


@dataclass
class SeatInfo:
    """单个席别的余票信息。"""

    seat_type: str
    remain: str
    code: str = ""
    available: bool = False
    standby_only: bool = False

    @property
    def remain_count(self) -> int:
        """可解析出的数字余票；``有`` / 候补 等返回 0。"""
        digits = re.sub(r"\D", "", self.remain or "")
        return int(digits) if digits else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "seat_type": self.seat_type,
            "remain": self.remain,
            "code": self.code,
            "available": self.available,
            "standby_only": self.standby_only,
            "remain_count": self.remain_count,
        }


@dataclass
class TrainTicket:
    """一个车次的余票快照（QY-07 结构化）。"""

    train_code: str
    secret_str: str = ""
    #: 内部车次号（下单/排队接口需要），与票面车次 train_code 不同
    train_no: str = ""
    from_station_code: str = ""
    to_station_code: str = ""
    from_station: str = ""
    to_station: str = ""
    start_time: str = ""
    arrive_time: str = ""
    duration: str = ""
    train_date: str = ""
    can_web_buy: str = ""
    button_text: str = ""
    location_code: str = ""
    #: 余票信息明细（``yp_info``，余票行下标 12）。
    #:
    #: ★ 它就是 ``getQueueCount.leftTicket`` 与 ``confirmSingleForQueue.leftTicketStr``
    #: 要的值（上游示例值 ``CmDJZYrwUoJ1jFNonIgPzPFdMBvSSE8xfdUwvb2lq8CCWn%2Bzk1vM3roJaHk%3D``
    #: 正是这个形状）。与 ``seat_types_raw``（下标 15，席别列表如 ``O9M3``）**不是一回事**；
    #: 拿后者去发第 ⑤/⑧ 步，离线因为 mock 不校验而全绿，真机则必然被拒（回流 #61）。
    yp_info: str = ""
    from_station_no: str = ""
    to_station_no: str = ""
    seat_types_raw: str = ""
    seats: dict[str, SeatInfo] = field(default_factory=dict)
    price_map: dict[str, float] = field(default_factory=dict)
    stop_stations: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @property
    def buyable(self) -> bool:
        """是否处于「可预订」状态（未到起售时间 / 已停售都为 False）。"""
        return self.can_web_buy in ("Y", "y")

    @property
    def not_yet_on_sale(self) -> bool:
        return self.can_web_buy == "IS_TIME_NOT_BUY"

    @property
    def duration_minutes(self) -> int:
        try:
            hours, _, minutes = self.duration.partition(":")
            return int(hours) * 60 + int(minutes or 0)
        except (ValueError, AttributeError):
            return 9999

    def seat(self, seat_type: str) -> SeatInfo | None:
        return self.seats.get(seat_type)

    def available_seats(self, priority: list[str] | None = None) -> list[SeatInfo]:
        """返回**有票**的席别。

        ``priority`` 非空时**严格只返回列表内的席别** —— 这一点很重要：
        用户配置的 ``seat_types`` 表达的是「我愿意接受哪些席别」，
        如果自动把列表外的席别（比如无座）也拿去下单，就违背了用户的授权范围。
        ``priority`` 为空时返回全部有票席别（仅用于展示）。
        """
        if priority:
            return [
                self.seats[name]
                for name in priority
                if name in self.seats and self.seats[name].available
            ]
        return [info for info in self.seats.values() if info.available]

    def standby_seats(self, priority: list[str]) -> list[SeatInfo]:
        """按优先级返回**仅候补**的席别。"""
        return [
            self.seats[s]
            for s in priority
            if s in self.seats and self.seats[s].standby_only
        ]

    @property
    def has_any_ticket(self) -> bool:
        return any(seat.available for seat in self.seats.values())

    def to_dict(self, *, with_stops: bool = False) -> dict[str, Any]:
        payload = {
            "train_code": self.train_code,
            "train_no": self.train_no,
            "from_station": self.from_station,
            "to_station": self.to_station,
            "from_station_code": self.from_station_code,
            "to_station_code": self.to_station_code,
            "start_time": self.start_time,
            "arrive_time": self.arrive_time,
            "duration": self.duration,
            "duration_minutes": self.duration_minutes,
            "train_date": self.train_date,
            "buyable": self.buyable,
            "not_yet_on_sale": self.not_yet_on_sale,
            "button_text": self.button_text,
            "seats": {name: info.to_dict() for name, info in self.seats.items()},
            "price_map": self.price_map,
            "location_code": self.location_code,
            "yp_info": self.yp_info,
            "available_seats": [i.seat_type for i in self.available_seats(None)],
        }
        if with_stops:
            payload["stop_stations"] = self.stop_stations
        return payload


# --------------------------------------------------------------------------- #
# 解析函数
# --------------------------------------------------------------------------- #
def _normalize_date(value: str) -> str:
    """把官方常见的 ``20261001`` 归一化为 ``2026-10-01``。

    官方不同接口对日期的格式并不统一，若不做归一化，
    ``TrainTicket.train_date`` 会一会儿是 8 位、一会儿是带横杠的，
    下游按日期比较/去重时就会出现诡异的「同一张票被当成两张」。
    """
    text = (value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def parse_seat_value(raw: str) -> SeatInfo:
    """解析单个席别的余票值。"""
    value = (raw or "").strip()
    available = value not in NO_TICKET_MARKERS and "无" not in value
    standby_only = any(marker in value for marker in STANDBY_MARKERS)
    if standby_only and not re.search(r"\d", value):
        # 纯「候补」表示暂无余票，只能候补
        available = False
    return SeatInfo(seat_type="", remain=value, available=available, standby_only=standby_only)


def parse_left_ticket_result(item: str, *, station_map: Any = None, train_date: str = "") -> TrainTicket:
    """把一条 ``|`` 分隔的余票字符串解析为 :class:`TrainTicket`。"""
    parts = item.split("|")
    if len(parts) < MIN_RESULT_FIELDS:
        raise ResponseParseError(f"余票字段数异常({len(parts)}): {item[:80]}")

    from_code = parts[IDX_FROM_STATION_CODE]
    to_code = parts[IDX_TO_STATION_CODE]
    ticket = TrainTicket(
        train_code=parts[IDX_TRAIN_CODE],
        # ★ secretStr 在响应里是 **URL 编码**的，必须在**入口**解一次。
        #
        # 真机实测（2026-09-30，84/84 行都带 `%2F` / `%2B`）：
        # `tuq87mx%2FclTrefw%2BoWU...`（410 字符）解出来是 368 字符的原始串。
        # 不解码的后果**不是报错**，而是提交订单时官方回一句
        # 「提交失败，请重试...」—— 因为 `requests` 会把 `%` 再编码成 `%25`，
        # 官方解一次拿到的仍是字面的 `%2F`，等于给了一串不存在的 secretStr。
        #
        # 上游 `pjialin/order/order.py::submit_order_request` 那行
        # `urllib.parse.unquote(...)`（注释写着「解密」）就是干这件事的。
        #
        # ⚠️ 只在**这里**解一次。若某个消费方再解一次就是双重解码；
        # 若把「原始编码串」和「解码串」同时留下来，早晚会有人拿错那个。
        secret_str=urllib.parse.unquote(parts[IDX_SECRET_STR]),
        train_no=parts[IDX_TRAIN_NO],
        from_station_code=from_code,
        to_station_code=to_code,
        from_station=station_map.name_of(from_code) if station_map else from_code,
        to_station=station_map.name_of(to_code) if station_map else to_code,
        start_time=parts[IDX_START_TIME],
        arrive_time=parts[IDX_ARRIVE_TIME],
        duration=parts[IDX_DURATION],
        train_date=_normalize_date(parts[IDX_START_TRAIN_DATE]) or train_date,
        can_web_buy=parts[IDX_CAN_WEB_BUY],
        button_text=parts[IDX_BUTTON_TEXT],
        location_code=parts[IDX_LOCATION_CODE],
        yp_info=parts[IDX_YP_INFO],
        from_station_no=parts[IDX_FROM_STATION_NO],
        to_station_no=parts[IDX_TO_STATION_NO],
        seat_types_raw=parts[IDX_SEAT_TYPES],
    )
    for seat_name, index in SEAT_INDEX.items():
        if index >= len(parts):
            continue
        info = parse_seat_value(parts[index])
        info.seat_type = seat_name
        info.code = SEAT_CODE.get(seat_name, "")
        ticket.seats[seat_name] = info
    return ticket


def parse_left_ticket_payload(
    payload: Any,
    *,
    station_map: Any = None,
    train_date: str = "",
    tolerate_bad_rows: bool = True,
) -> list[TrainTicket]:
    """解析余票接口整体响应。"""
    if not isinstance(payload, dict):
        raise ResponseParseError("余票响应不是 JSON 对象")

    if payload.get("status") is False or payload.get("httpstatus") not in (200, "200", None):
        if payload.get("status") is False:
            message = payload.get("messages") or payload.get("c_url") or "余票查询失败"
            raise ResponseParseError(f"余票查询失败: {message}")

    data = payload.get("data")
    if isinstance(data, dict) and data.get("result") is not None:
        rows = data.get("result") or []
    elif isinstance(data, list):
        rows = data
    elif isinstance(payload.get("result"), list):
        rows = payload["result"]
    else:
        rows = []

    tickets: list[TrainTicket] = []
    for row in rows:
        if isinstance(row, dict):
            tickets.append(_parse_dict_row(row, station_map, train_date))
            continue
        try:
            tickets.append(parse_left_ticket_result(str(row), station_map=station_map, train_date=train_date))
        except ResponseParseError as exc:
            if not tolerate_bad_rows:
                raise
            log.debug("跳过无法解析的余票行: %s", exc)
    return tickets


def _parse_dict_row(row: dict[str, Any], station_map: Any, train_date: str) -> TrainTicket:
    """兼容部分接口返回对象数组的情况（mock / 灰度接口）。"""
    ticket = TrainTicket(
        train_code=row.get("station_train_code") or row.get("train_code") or "",
        secret_str=row.get("secretStr") or row.get("secret_str") or "",
        train_no=row.get("train_no") or row.get("trainNo") or "",
        from_station_code=row.get("from_station_telecode") or row.get("from_station_code") or "",
        to_station_code=row.get("to_station_telecode") or row.get("to_station_code") or "",
        start_time=row.get("start_time", ""),
        arrive_time=row.get("arrive_time", ""),
        duration=row.get("lishi") or row.get("duration") or "",
        train_date=_normalize_date(row.get("start_train_date") or "") or train_date,
        can_web_buy=row.get("canWebBuy") or row.get("can_web_buy") or "Y",
        button_text=row.get("buttonTextInfo") or "",
        location_code=row.get("location_code", ""),
        yp_info=row.get("yp_info", ""),
        from_station_no=str(row.get("from_station_no", "")),
        to_station_no=str(row.get("to_station_no", "")),
        seat_types_raw=row.get("seat_types", ""),
    )
    ticket.from_station = station_map.name_of(ticket.from_station_code) if station_map else ticket.from_station_code
    ticket.to_station = station_map.name_of(ticket.to_station_code) if station_map else ticket.to_station_code

    seat_fields = {
        "商务座": "swz_num",
        "特等座": "tz_num",
        "一等座": "zy_num",
        "二等座": "ze_num",
        "高级软卧": "gr_num",
        "软卧": "rw_num",
        "硬卧": "yw_num",
        "软座": "rz_num",
        "硬座": "yz_num",
        "无座": "wz_num",
        "动卧": "srrb_num",
    }
    for seat_name, key in seat_fields.items():
        if key not in row:
            continue
        info = parse_seat_value(str(row.get(key, "")))
        info.seat_type = seat_name
        info.code = SEAT_CODE.get(seat_name, "")
        ticket.seats[seat_name] = info
    return ticket


# --------------------------------------------------------------------------- #
# 下单页 HTML / 其他
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(r"globalRepeatSubmitToken\s*=\s*'([^']+)'")

#: ``ticketInfoForPassengerForm`` 的**多种已知写法**（回流 #49 追查）。
#:
#: 老实现只有一条 ``=\s*(\{.*?\});`` —— **硬要求结尾是分号**。官方只要换一种写法
#: （换行结尾、``JSON.parse('...')``、双引号包裹），就会**静默**匹配不到，
#: 而症状与「页面里真没有这个变量」**完全一样**（都是 ``ticket_info == {}``）。
#: 判据必须先穷尽写法，才有资格下「页面里没有」这个结论。
_TICKET_FORM_RES = (
    re.compile(r"ticketInfoForPassengerForm\s*=\s*(\{.*?\})\s*;", re.S),
    re.compile(r"ticketInfoForPassengerForm\s*=\s*(\{.*?\})\s*(?:</script>|$)", re.S),
    re.compile(r"ticketInfoForPassengerForm\s*[:=]\s*JSON[.]parse\(\s*'(\{.*?\})'\s*\)", re.S),
    re.compile(r'ticketInfoForPassengerForm\s*[:=]\s*JSON[.]parse\(\s*"(\{.*?\})"\s*\)', re.S),
)

#: 整段 form 都拿不到时的**抢救式单字段提取**。
#: 用途只有一个：把「这几个值到底拿没拿到」变成**可判读**，而不是悄悄退化成空串。
_LOOSE_VALUE_RES = {
    name: re.compile(r"['\"]" + name + r"['\"]\s*:\s*['\"]([^'\"]*)['\"]")
    for name in ("leftTicketStr", "key_check_isChange", "train_location", "purpose_codes")
}


def _salvage_ticket_info(html: str) -> dict[str, Any]:
    """退一步：从整页里逐个抠出下单要用的那 4 个值。

    ★ 为什么值得留着：这 4 个值**只从这一处来**（第 ⑤ 步的 ``leftTicket``、
    第 ⑧ 步的 ``leftTicketStr`` / ``key_check_isChange`` / ``train_location``）。
    抠到几个算几个，并由调用方按「抠到没抠到」说不同的话 —— 比一律返回空、
    让真机上第 ⑧ 步带着空值去建单要好得多（那一步会**真的**产生待支付订单）。
    """
    found: dict[str, Any] = {}
    for name, pattern in _LOOSE_VALUE_RES.items():
        match = pattern.search(html or "")
        if match:
            found[name] = match.group(1)
    return found


def parse_init_dc_html(html: str) -> dict[str, Any]:
    """从 initDc 返回的 HTML 中抽取提交订单所需的 token 与表单数据。"""
    token_match = _TOKEN_RE.search(html or "")
    if not token_match:
        raise ResponseParseError("initDc 未返回 REPEAT_SUBMIT_TOKEN（可能未登录或已被风控）")
    result: dict[str, Any] = {"repeat_submit_token": token_match.group(1), "ticket_info": {}}
    raw = ""
    for pattern in _TICKET_FORM_RES:
        form_match = pattern.search(html or "")
        if form_match:
            raw = form_match.group(1)
            break
    if not raw:
        salvaged = _salvage_ticket_info(html or "")
        if salvaged:
            result["ticket_info"] = salvaged
            log.warning(
                "initDc 页面里没找到完整的 ticketInfoForPassengerForm，已按单字段抢救出 %s；"
                "若缺 key_check_isChange / leftTicketStr，第 ⑥/⑧ 步会退到下单服务里的兜底值",
                sorted(salvaged),
            )
        else:
            log.warning(
                "initDc 页面里找不到 ticketInfoForPassengerForm（页面 %d 字符）："
                "第 ⑤/⑧ 步的 leftTicketStr / key_check_isChange 将退到兜底 ——"
                "leftTicketStr 用余票行的 yp_info、key_check_isChange 只能发空串。"
                "真机若仍被拒，请用 scripts/diagnose_order.py 的页面取证确认页面里到底有什么",
                len(html or ""),
            )
        return result
    import json

    try:
        result["ticket_info"] = json.loads(raw.replace("'", '"'))
    except ValueError:
        # ★ 以前这里是 log.debug —— 默认级别下看不见，等于静默。
        # 「页面里在、但解析不了」与「页面里没有」是**两种不同的处置**，必须分开。
        salvaged = _salvage_ticket_info(html or "")
        if salvaged:
            result["ticket_info"] = salvaged
        log.warning(
            "ticketInfoForPassengerForm 不是合法 JSON/JS 字面量（前 120 字符：%r）", raw[:120]
        )
    return result


#: 官方加密串在乘客字段里的名字。**真机确实下发**（``getPassengerDTOs`` →
#: ``data.normal_passengers[]``），而 ``get_passengers`` 是**原样透传**整个列表，
#: 所以它一直躺在数据里，只是构造下单参数时从没被用过。见回流 #51。
PASSENGER_ENC_KEYS = ("allEncStr", "all_enc_str")


def passenger_enc_str(passenger: dict[str, Any]) -> str:
    """取乘车人的官方加密串（``allEncStr``）。**唯一实现处**。"""
    for key in PASSENGER_ENC_KEYS:
        value = str(passenger.get(key) or "").strip()
        if value:
            return value
    return ""


def build_passenger_ticket_str(passengers: list[dict[str, Any]], seat_code: str) -> str:
    """构造 ``passengerTicketStr``（下单核心参数，**逐段对齐上游**）。

    格式：``席别编码,0,票种,姓名,证件类型,证件号,手机号,N,官方加密串``
    多乘客用 ``_`` 连接（上游是每条末尾补 ``_`` 再 ``rstrip('_')``，与本函数的
    ``"_".join`` 等价）。

    ⚠️ 第 9 段 ``allEncStr`` **不是可选装饰**（回流 #51）
    ----------------------------------------------------
    真机上 ``checkOrderInfo`` 回的是一句含糊的「余票不足！」——而根因是我们把
    加密串整段漏发了。三方独立证据：

    1. 上游直购 ``order.py:595`` 的模板就是 ``...,N,{enc_str}_``（**9 段**）；
    2. 上游候补与 ``testerSunshine`` 的 ``passengerInfo`` 也一律带 ``allEncStr``，
       且本项目候补链路（``kernel/self_builder.py``）早已写明「缺 allEncStr 官方必拒」；
    3. 数据源里有它 —— ``getPassengerDTOs`` 真机下发，我们只是没用。

    **缺它时仍然发第 9 段（空串）并留 WARNING**：不静默退回旧形状，
    否则真机上又会变成「说不清为什么被拒」。判据落在 mock 收到的表单上
    （``test_order_params_on_the_wire.py``），撤掉本实现即变红。
    """
    chunks: list[str] = []
    for passenger in passengers:
        enc_str = passenger_enc_str(passenger)
        if not enc_str:
            log.warning(
                "乘车人「%s」缺少官方加密串 allEncStr，passengerTicketStr 第 9 段为空；"
                "官方多半会以「余票不足！」这类业务消息拒绝（回流 #51）",
                passenger.get("passenger_name", ""),
            )
        chunks.append(
            ",".join(
                [
                    seat_code,
                    "0",
                    str(passenger.get("passenger_type", "1")),
                    str(passenger.get("passenger_name", "")),
                    str(passenger.get("passenger_id_type_code", "1")),
                    str(passenger.get("passenger_id_no", "")),
                    str(passenger.get("mobile_no", "") or ""),
                    "N",
                    enc_str,
                ]
            )
        )
    return "_".join(chunks)


def build_old_passenger_str(passengers: list[dict[str, Any]]) -> str:
    """构造 ``oldPassengerStr``。

    上游形状：每人 ``姓名,证件类型,证件号,票种_``，拼完**去掉末尾 ``_``，再补
    ``'__ _ _'``**。上游源码那一行自己写着注释「**不加后面请求会出错**」，
    据此逐字对齐（回流 #51）。
    """
    joined = "".join(
        f"{p.get('passenger_name','')},{p.get('passenger_id_type_code','1')},"
        f"{p.get('passenger_id_no','')},{p.get('passenger_type','1')}_"
        for p in passengers
    )
    return joined.rstrip("_") + "__ _ _"


# --------------------------------------------------------------------------- #
# 个人中心：账号信息 / 乘车人 / 我的订单
# --------------------------------------------------------------------------- #
def first_of(data: dict[str, Any], *keys: str, default: Any = "") -> Any:
    """按顺序取第一个「有值」的键。

    官方同一语义的字段名在不同接口/版本里并不统一（``order_no`` / ``sequence_no``、
    ``train_code`` / ``station_train_code``），逐个 if 取会写成一堆分支；
    这里统一成「候选键列表」的表达方式，也方便日后把真机新字段补进来。
    """
    for key in keys:
        if key in data:
            value = data.get(key)
            if value not in (None, "", [], {}):
                return value
    return default


def mask_id_no(value: str) -> str:
    """证件号打码，保留前 6 后 4（与 12306 官方个人中心的展示一致）。

    为什么要自己再打一次码：官方 ``/otn/passengers/query`` 已经打了码，
    但 ``getPassengerDTOs``（下单链路）返回的是**完整**证件号。
    个人中心是「看」的场景，没有任何展示完整证件号的必要。
    """
    text = str(value or "").strip()
    if len(text) <= 10:
        return text
    return f"{text[:6]}{'*' * (len(text) - 10)}{text[-4:]}"


def mask_phone(value: str) -> str:
    """手机号打码，保留前 3 后 4。"""
    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) < 7:
        return text
    return f"{digits[:3]}****{digits[-4:]}"


def mask_email(value: str) -> str:
    """邮箱打码，保留首字符与域名。"""
    text = str(value or "").strip()
    if "@" not in text:
        return text
    name, _, domain = text.partition("@")
    if not name:
        return text
    return f"{name[0]}{'*' * max(1, len(name) - 1)}@{domain}"


#: 人证核验的判定候选字段。**必须拿到明确字段才下结论**，
#: 拿不到就返回 ``None``（对应界面上的「无法判定」）——
#: 以前用「姓名 ≠ 账号名」猜，把扫码登录的已实名账号误判成「未核验」。
_PROFILE_VERIFY_KEYS = (
    "isUserVerified",
    "userVerified",
    "isChecked",
    "verifyStatus",
    "face_flag",
    "verified_flag",
)


def truthy_flag(value: Any) -> bool | None:
    """把官方的各种「是/否」表示归一成 ``True`` / ``False`` / ``None``。

    返回 ``None`` 表示「这个值判不出来」—— 调用方**不要**把它当成 False。
    官方字段缺省、给空串、给没见过的枚举值，都属于「判不出来」。
    """
    if value is None or value == "":
        return None
    text = str(value).strip().upper()
    if text in ("Y", "1", "TRUE", "YES", "2", "已核验", "通过"):
        return True
    if text in ("N", "0", "FALSE", "NO", "未核验", "不通过"):
        return False
    return None


#: 兼容旧名（模块内外都有引用）
_truthy_flag = truthy_flag


#: ``parse_user_profile`` 会消费掉的源字段名。
#: 用途：算出「还有哪些字段没见过」，真机排查时一眼能看出官方新增了什么。
#: **新增字段映射时记得同步这里**，否则新字段会一直被算作「未知字段」。
_PROFILE_CONSUMED_KEYS = frozenset(
    {
        "userDTO", "user",
        "user_name", "username", "login_name",
        "real_name", "name", "cn_name",
        "user_type_name", "userTypeName", "user_type",
        "sexCode", "sex_code", "sex", "gender",
        "birthday", "birth_date",
        "email", "emailAddress", "email_address",
        "mobile_no", "mobile", "phone_number", "phoneNumber", "telephone",
        "id_type_name", "idTypeName", "id_type",
        "id_no", "idNo", "id_card",
        "isCheckEmail", "isCheckMail", "emailChecked",
        "isCheckMobile", "isCheckPhone", "mobileChecked",
        "member_level", "memberLevel", "user_level_name",
        "points", "user_score", "credit",
        "register_time", "create_time", "reg_time",
        "is_student", "studentFlag",
    }
) | frozenset(_PROFILE_VERIFY_KEYS)


#: 登录态响应里表示「还活着」的字段候选（点号路径，与上游写法一致）。
#:
#: 顺序有意义：先看 ``data.flag``（``checkUser`` 的现行字段），
#: 再看顶层的 ``status``（历史版本用过）。
#: **唯一真源** —— `kernel/self/login_check.py` 与接口层的默认规格都引用它。
CHECK_USER_ALIVE_PATHS = ("data.flag", "status")


def unwrap_user_payload(payload: Any) -> dict[str, Any]:
    """把用户类响应剥到「真正装着字段的那一层」。

    官方这几个接口的嵌套层数**不固定**，实测/上游出处支持三种：

    ==================================  ==========================================
    ``data`` 平铺                        ``{"user_name": ...}``
    ``data.userDTO``                     ``{"userDTO": {"user_name": ...}}``
    ``data.userDTO.loginUserDTO``        上游 ``user/job.py::get_user_info`` 读的
                                         就是这条（见 ``vendor/pjialin/ticket_facts.py``
                                         的 ``USER_RESPONSE_FIELDS``，逐字切出）
    ==================================  ==========================================

    为什么剥层必须**集中在一处**：三种结构的字段名完全一样，所以「没剥够」
    不会报任何错 —— 它只会让所有字段解析成空串。症状是「账号信息页一片空白」，
    与「接口挂了」长得一模一样，极难联想到是少剥了一层。
    两处消费点各写一遍，就必然有一处会漏。

    剥不动时**原样返回**（不抛）：调用方各有自己的错误口径。
    """
    if not isinstance(payload, dict):
        return {}
    data: Any = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return {}
    for wrapper in ("userDTO", "user"):
        nested = data.get(wrapper)
        if isinstance(nested, dict):
            data = {**nested, **{k: v for k, v in data.items() if k != wrapper}}
    # 上游还多一层 loginUserDTO（`data.userDTO.loginUserDTO`）
    inner = data.get("loginUserDTO")
    if isinstance(inner, dict) and inner:
        data = {**inner, **{k: v for k, v in data.items() if k != "loginUserDTO"}}
    return data


def parse_user_profile(payload: Any) -> dict[str, Any]:
    """解析个人中心接口（``initMy12306Api``）的响应，输出统一字段。

    **这一层的价值在于「字段名归一」**：官方这几个接口里，
    同一个语义常有 2~3 个字段名，且随版本变化。上层（Web / 桌面端）只认这里的字段名，
    真机发现新字段名时只改本函数即可。

    账号信息里的证件号 / 手机 / 邮箱一律**打码后**返回：这是展示场景，
    没有任何理由把完整信息渲染到界面上。
    """
    if not isinstance(payload, dict):
        raise ResponseParseError("账号信息响应不是 JSON 对象")
    # 嵌套层数不固定（data / data.userDTO / data.userDTO.loginUserDTO），
    # 剥层集中在 unwrap_user_payload 一处 —— 见它的 docstring。
    data = unwrap_user_payload(payload)

    verify = None
    for key in _PROFILE_VERIFY_KEYS:
        verify = _truthy_flag(data.get(key))
        if verify is not None:
            break

    profile: dict[str, Any] = {
        "username": str(first_of(data, "user_name", "username", "login_name")),
        "real_name": str(first_of(data, "real_name", "name", "cn_name")),
        "user_type": str(first_of(data, "user_type_name", "userTypeName", "user_type")),
        "sex": _sex_text(first_of(data, "sexCode", "sex_code", "sex", "gender")),
        "birthday": str(first_of(data, "birthday", "birth_date")),
        "email": mask_email(first_of(data, "email", "emailAddress", "email_address")),
        "mobile": mask_phone(first_of(data, "mobile_no", "mobile", "phone_number", "phoneNumber", "telephone")),
        "id_type": str(first_of(data, "id_type_name", "idTypeName", "id_type")),
        "id_no": mask_id_no(first_of(data, "id_no", "idNo", "id_card")),
        "verified": verify,
        "is_email_checked": _truthy_flag(first_of(data, "isCheckEmail", "isCheckMail", "emailChecked", default=None)),
        "is_mobile_checked": _truthy_flag(
            first_of(data, "isCheckMobile", "isCheckPhone", "mobileChecked", default=None)
        ),
        "member_level": str(first_of(data, "member_level", "memberLevel", "user_level_name")),
        "points": str(first_of(data, "points", "user_score", "credit")),
        "register_time": str(first_of(data, "register_time", "create_time", "reg_time")),
        "is_student": _truthy_flag(first_of(data, "is_student", "studentFlag", default=None)),
    }
    # 保留「其余字段」的键名（只留键，不留值）：真机排查时知道接口到底给了什么，
    # 又不会把没脱敏的内容带出去。
    profile["extra_keys"] = sorted(k for k in data if k not in _PROFILE_CONSUMED_KEYS)
    return profile


def _sex_text(value: Any) -> str:
    """官方性别是 ``M`` / ``F``，也有直接给中文的版本。"""
    text = str(value or "").strip()
    return {"M": "男", "F": "女", "1": "男", "0": "女"}.get(text.upper() if len(text) == 1 else text, text)


def parse_passengers_payload(payload: Any) -> list[dict[str, Any]]:
    """解析乘车人响应，输出统一字段。

    兼容两条链路的响应：
    - ``/otn/passengers/query``（个人中心，分页）→ ``data.datas``；
    - ``getPassengerDTOs``（下单链路）→ ``data.normal_passengers``。

    证件号与手机号统一在这里打码 —— 无论上游给的是明文还是打码值，
    都重新打一遍，保证「展示层拿到的永远不可能是完整证件号」。
    """
    if not isinstance(payload, dict):
        raise ResponseParseError("乘车人响应不是 JSON 对象")
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    rows = first_of(data, "datas", "normal_passengers", "passengers", default=[])
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list):
        return []

    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(first_of(row, "passenger_name", "name"))
        if not name:
            # 没有姓名的行不是乘车人（官方偶尔会在同一数组里混入其它对象）
            continue
        result.append(
            {
                "passenger_name": name,
                "passenger_id_type_code": str(first_of(row, "passenger_id_type_code", "id_type", default="1")),
                "passenger_id_type_name": str(first_of(row, "passenger_id_type_name", "id_type_name")),
                "passenger_id_no": mask_id_no(first_of(row, "passenger_id_no", "id_no")),
                "passenger_type": str(first_of(row, "passenger_type", "type", default="1")),
                "passenger_type_name": str(first_of(row, "passenger_type_name", "type_name")),
                "mobile_no": mask_phone(first_of(row, "mobile_no", "mobile", "phone")),
                "sex_name": _sex_text(first_of(row, "sex_code", "sex", "sexCode", default="")),
                "birthday": str(first_of(row, "birthday", "birth_date")),
                "verify_status": str(first_of(row, "passenger_verify_status", "verify_status", "check_status")),
                "verify_status_name": str(first_of(row, "passenger_verify_status_name", "verify_status_name")),
                "is_self": bool(_truthy_flag(first_of(row, "isUserSelf", "is_self", default=None))),
                "total_times": str(first_of(row, "total_times", "totalTimes")),
                "index_id": str(first_of(row, "index_id", "idx", "passenger_id")),
            }
        )
    return result


#: 订单票种状态码 → 中文（官方文档给的状态对应表）
ORDER_TICKET_STATUS = {
    "a": "已支付", "b": "已出票", "c": "已退票", "d": "已改签", "e": "改签中",
    "f": "改签票", "g": "退票中", "h": "已变更到站", "i": "待支付", "j": "改签待支付",
    "m": "已出站", "n": "待核验", "p": "已变更到站", "q": "变更到站中", "r": "变更到站票",
    "s": "变更到站待支付",
}


def parse_order_history(payload: Any) -> dict[str, Any]:
    """解析「我的订单」分页响应，输出 ``{orders, page_index, page_count}``。

    字段名全部走 :func:`first_of` 的候选列表：官方在不同查询条件（未出行 / 历史 /
    改签 / 退票）下返回的键名并不完全一致，写死单个键名会导致某一类订单整片空白。
    """
    if not isinstance(payload, dict):
        raise ResponseParseError("订单响应不是 JSON 对象")
    if payload.get("status") is False:
        # 未登录 / 风控 / 日期范围非法时官方回的是这个信封：``data`` 可能是个空对象，
        # 直接往下走会得到「0 条订单」——把一次失败伪装成「你没有订单」，
        # 这是最容易被当成「程序没问题、是我真没买过票」的假象。
        raise ResponseParseError(
            str(first_of(payload, "messages", "message", default="订单查询失败")) or "订单查询失败"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        # 未登录 / 被风控时官方会回一个没有 data 的信封
        raise ResponseParseError(
            str(first_of(payload, "messages", "message", default="订单查询失败")) or "订单查询失败"
        )

    rows = first_of(data, "OrderDTODataList", "orderDTODataList", "orders", "datas", "orderList", "list", default=[])
    if not isinstance(rows, list):
        rows = []

    orders = [_parse_order_row(row) for row in rows if isinstance(row, dict)]
    # 官方的 ``pageIndex`` 是 **0-based**（与请求参数同语义），对外统一成 1-based ——
    # 否则界面上会出现「第 0 页」这种一眼就知道不对、却要查半天的东西。
    page_index = int(str(first_of(data, "pageIndex", "page_index", default=0) or 0).split(".")[0] or 0) + 1
    page_count = int(str(first_of(data, "pageCount", "page_count", "totalPage", default=1) or 1).split(".")[0] or 1)
    return {
        "orders": orders,
        "page_index": page_index,
        "page_count": max(page_count, 1),
        #: 官方称「名下共有多少单」。**这是 0 条时最关键的指标**：
        #: 0 → 官方认为没有订单（程序没问题）；>0 而列表为空 → 窗口/口径不对；
        #: >0 且列表非空却解析出 0 条 → 字段名变了（要改代码）。
        #: 没有这个数，界面只能显示「没有符合条件的订单」，三种情况混在一起没法分。
        "total_number": _to_int(first_of(data, "order_total_number", "orderTotalNumber", default=None)),
        "start_date": str(first_of(data, "train_start_date", "queryStartDate")),
        "end_date": str(first_of(data, "train_end_date", "queryEndDate")),
    }


def _to_int(value: Any) -> int | None:
    """官方有时把计数写成 ``"3"`` 或 ``3.0``；判不出来就返回 ``None`` 而不是 0。"""
    if value is None or value == "":
        return None
    try:
        return int(str(value).split(".")[0])
    except (TypeError, ValueError):
        return None


def _first_scalar(value: Any) -> Any:
    """官方有些字段是「单元素数组」（如 ``from_station_name_page: ["长沙南"]``）。

    直接 ``str(["长沙南"])`` 会得到 ``"['长沙南']"`` 这种东西 —— 界面上就会显示成
    ``['长沙南']``。统一取首元素。
    """
    if isinstance(value, (list, tuple)):
        return value[0] if value else ""
    return value


def _date_only(value: Any) -> str:
    """``"2026-09-30 23:42"`` → ``"2026-09-30"``；``20260930`` → ``2026-09-30``。"""
    text = _normalize_date(str(_first_scalar(value) or ""))
    matched = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    return matched.group(1) if matched else text


def _order_passenger(item: dict[str, Any]) -> dict[str, Any]:
    """把一张票/一位乘客归一化（字段名候选与真机对齐）。"""
    status_code = str(first_of(item, "ticket_status", "status"))
    return {
        "name": str(first_of(item, "passenger_name", "name")),
        "ticket_type_name": str(first_of(item, "ticket_type_name", "passenger_type_name", "ticketTypeName")),
        "seat_type_name": str(first_of(item, "seat_type_name", "seatTypeName", "seat_type")),
        "coach_no": str(first_of(item, "coach_no", "coachNo")),
        "seat_no": str(first_of(item, "seat_no", "seatNo")),
        "ticket_no": str(first_of(item, "ticket_no", "ticketNo")),
        "status": str(
            first_of(item, "ticket_status_name", "status_name", "ticketStatusName")
            or ORDER_TICKET_STATUS.get(status_code, "")
        ),
    }


def _parse_order_row(row: dict[str, Any]) -> dict[str, Any]:
    """把一条订单记录归一化。

    **真机字段名与直觉差得很远**（2026-09-29 用真实账号抓下来的原始记录）：

    ====================  ==========================================
    真机字段              含义
    ====================  ==========================================
    ``train_code_page``            车次（如 ``G4212``）
    ``from_station_name_page``     出发站，**是数组** ``["长沙南"]``
    ``to_station_name_page``       到达站，**是数组**
    ``start_train_date_page``      乘车日期 + 发车时间 ``"2026-09-30 23:42"``
    ``start_time_page``            发车时间
    ``arrive_time_page``           到达时间
    ``ticket_total_price_page``    金额（元）
    ``array_passser_name_page``    乘客姓名数组（官方拼写就是 ``passser``）
    ``recordCount``                本单共几张票
    ``tickets[]``                  票的明细，车次在
                                   ``tickets[].stationTrainDTO.trainDTO.train_code``
    ====================  ==========================================

    所以候选键一律把**真机字段放在最前面**，老字段名留在后面兼容 mock 与灰度版本。
    之前只按直觉猜（``train_code`` / ``from_station_name`` / ``passengerDTOs``），
    结果真机上订单**能取到但每一列都是空的**。
    """
    ticket_rows = row.get("tickets") if isinstance(row.get("tickets"), list) else []
    first_ticket = ticket_rows[0] if ticket_rows and isinstance(ticket_rows[0], dict) else {}
    station_train = first_ticket.get("stationTrainDTO")
    station_train = station_train if isinstance(station_train, dict) else {}
    train_dto = station_train.get("trainDTO")
    train_dto = train_dto if isinstance(train_dto, dict) else {}

    passengers: list[dict[str, Any]] = []
    # 1) 下单链路 / 部分版本给的是 passengerDTOs
    for item in row.get("passengerDTOs") or row.get("passenger_dtos") or []:
        if isinstance(item, dict):
            passengers.append(_order_passenger(item))
    # 2) 真机的票明细在 tickets[]，乘客挂在每张票上；
    #    席别 / 座位 / 票种在**票**这一层，姓名在 passengerDTO 里 —— 两层合并再归一化，
    #    不然「有订单但座位列全空」的现象会一直存在。
    if not passengers:
        for item in ticket_rows:
            if not isinstance(item, dict):
                continue
            nested = item.get("passengerDTO") if isinstance(item.get("passengerDTO"), dict) else {}
            if str(nested.get("passenger_name") or nested.get("name") or "").strip():
                passengers.append(_order_passenger({**item, **nested}))
    # 3) 最后兜底：订单级只有姓名数组
    if not passengers:
        for name in row.get("array_passser_name_page") or []:
            if str(name).strip():
                passengers.append(_order_passenger({"passenger_name": str(name)}))

    pay_flag = str(first_of(row, "pay_flag", "payFlag"))
    return {
        "order_no": str(first_of(row, "sequence_no", "order_no", "orderNo", "order_id")),
        "order_date": str(_first_scalar(first_of(row, "order_date", "orderDate", "order_time"))),
        "train_code": str(
            _first_scalar(first_of(row, "train_code_page", "train_code", "station_train_code", "stationTrainCode"))
        )
        or str(train_dto.get("train_code") or ""),
        "train_date": _date_only(
            first_of(row, "start_train_date_page", "train_date", "trainDate", "start_train_date")
        ),
        # 真机车站/时间/金额都带 ``_page`` 后缀，且车站是数组
        "from_station": str(_first_scalar(
            first_of(row, "from_station_name_page", "from_station_name", "fromStationName", "start_station_name")
        )),
        "to_station": str(_first_scalar(
            first_of(row, "to_station_name_page", "to_station_name", "toStationName", "end_station_name")
        )),
        "start_time": str(_first_scalar(first_of(row, "start_time_page", "start_time", "startTime"))),
        "arrive_time": str(_first_scalar(first_of(row, "arrive_time_page", "arrive_time", "arriveTime"))),
        "seat_type": str(_first_scalar(first_of(row, "seat_type_name", "seatTypeName", default="")))
        or str(first_ticket.get("seatTypeName") or first_ticket.get("seat_type_name") or "")
        or str(station_train.get("seatTypeName") or ""),
        "price": str(_first_scalar(
            first_of(row, "ticket_total_price_page", "ticket_total_price", "ticketTotalPrice", "total_price")
        )),
        "status": str(first_of(row, "order_status_name", "orderStatusName", "status_name", default=""))
        or str(first_ticket.get("ticket_status_name") or ""),
        "pay_flag": pay_flag,
        # 官方用 pay_flag=Y 表示「这笔还需要支付」；直接给调用方一个布尔量，
        # 免得前端各自去记 Y 到底代表「要付」还是「已付」。
        "need_pay": pay_flag.upper() == "Y",
        # 真机没有 can_refund/can_resign，而是 return_flag（能否退票）。
        # resign_flag 的取值语义（见到过 "2"）还没核实，所以**不猜**。
        "can_refund": bool(_truthy_flag(first_of(row, "can_refund", "refundable", "return_flag", default=None))),
        "can_resign": bool(_truthy_flag(first_of(row, "can_resign", "resignable", default=None))),
        "passengers": passengers,
        # 姓名**去重**（保序）：真机上同一张订单里同一个乘客会出现在多条票记录中，
        # 直接 join 会显示成「樊章,樊章」。明细仍保留在 passengers 里。
        "passenger_names": ",".join(dict.fromkeys(p["name"] for p in passengers if p["name"])),
    }



# --------------------------------------------------------------------------- #
# 扫码登录（AC-08）
# --------------------------------------------------------------------------- #
#: 官方 checkqr 的 result_code → 内部状态。
#: 用字符串常量而不是直接透传数字，避免各层到处出现裸魔数。
QR_CODE_WAITING = "0"      # 二维码已生成，等待用户扫描
QR_CODE_SCANNED = "1"      # 已扫码，等待用户在手机上确认
QR_CODE_CONFIRMED = "2"    # 已确认，登录成功（响应里带 uamtk）
QR_CODE_EXPIRED = "3"      # 二维码过期或被取消

#: 对内统一使用的状态名（Web / 桌面端都按这些值判断）
QR_STATE_WAITING = "waiting"
QR_STATE_SCANNED = "scanned"
QR_STATE_CONFIRMED = "confirmed"
QR_STATE_EXPIRED = "expired"
QR_STATE_FAILED = "failed"
#: 响应无法识别（字段缺失 / 未知状态码 / 错误信封）。
#: **这不是终态**：接口偶发返回异常信封时，二维码往往仍然有效，
#: 直接判失败会让用户「还没扫就被通知失败」。上层应保留会话并继续轮询。
QR_STATE_UNKNOWN = "unknown"

QR_STATE_MAP = {
    QR_CODE_WAITING: QR_STATE_WAITING,
    QR_CODE_SCANNED: QR_STATE_SCANNED,
    QR_CODE_CONFIRMED: QR_STATE_CONFIRMED,
    QR_CODE_EXPIRED: QR_STATE_EXPIRED,
}

QR_STATE_TEXT = {
    QR_STATE_WAITING: "请使用 12306 App 扫描二维码",
    QR_STATE_SCANNED: "已扫描，请在手机上确认登录",
    QR_STATE_CONFIRMED: "登录成功",
    QR_STATE_EXPIRED: "二维码已失效，请刷新重试",
    QR_STATE_FAILED: "扫码登录失败",
    QR_STATE_UNKNOWN: "暂时无法获取扫码状态，正在重试…",
}


def qr_state_of(payload: dict[str, Any] | None) -> str | None:
    """尝试从响应里解析出扫码状态；无法识别时返回 ``None``。

    供接口层判断「这个响应是否可用」——不可用时应当换一种请求方式重试
    （官方 checkqr 是 POST 表单，某些部署只认 GET），而不是把请求判死。
    """
    data = payload or {}
    for field in ("result_code", "status"):
        raw = str(data.get(field, "")).strip()
        if raw in QR_STATE_MAP:
            return QR_STATE_MAP[raw]
    return None


def _qr_error_message(data: dict[str, Any]) -> str:
    """从各种可能的错误信封里尽量取出一句人话。"""
    message = str(data.get("result_message") or "").strip()
    if message:
        return message
    messages = data.get("messages")
    if isinstance(messages, list) and messages:
        return str(messages[0]).strip()
    if isinstance(messages, str):
        return messages.strip()
    return ""


def parse_qr_create(payload: dict[str, Any] | None) -> tuple[str, str]:
    """解析生成二维码的响应，返回 ``(uuid, image_base64)``。

    官方返回形如::

        {"result_code": 0, "result_message": "OK", "uuid": "...", "image": "iVBORw0..."}

    注意 ``image`` 是**裸 base64**（不带 ``data:image/png;base64,`` 前缀），
    前端展示时需要自己拼前缀；这里原样返回，不替调用方做展示层决定。
    """
    data = payload or {}
    uuid = str(data.get("uuid") or "").strip()
    image = str(data.get("image") or "").strip()
    result_code = str(data.get("result_code", ""))
    if not uuid:
        message = _qr_error_message(data) or "生成登录二维码失败"
        raise ResponseParseError(f"{message}（result_code={result_code or '空'}）")
    return uuid, image


def parse_qr_check(payload: dict[str, Any] | None) -> tuple[str, str, str]:
    """解析扫码状态响应，返回 ``(state, uamtk, message)``。

    无法识别的响应返回 ``QR_STATE_UNKNOWN`` 而**不是** ``failed`` ——
    官方偶发返回错误信封（例如 ``{"result_message": "操作失败"}`` 而无 result_code），
    此时二维码通常仍然有效，把会话判死会造成「还没扫就提示失败」。
    """
    data = payload or {}
    state = qr_state_of(data)
    message = _qr_error_message(data)
    uamtk = str(data.get("uamtk") or data.get("newapptk") or "").strip()

    if state is None:
        detail = f"（服务端返回：{message}）" if message else "（响应中缺少状态字段）"
        log.warning(
            "无法识别 checkqr 响应%s，将继续轮询: %s",
            detail,
            {k: v for k, v in data.items() if k in ("result_code", "status", "result_message", "messages")},
        )
        return QR_STATE_UNKNOWN, uamtk, f"暂时无法获取扫码状态{detail}"

    if state == QR_STATE_CONFIRMED and not uamtk:
        # 确认成功但没给 uamtk：调用方需要回退到 uamtk 接口换取
        log.debug("扫码确认成功但响应未携带 uamtk，将回退到 uamtk 接口")
    return state, uamtk, message or QR_STATE_TEXT.get(state, "")
