"""12306 接口层：HTTP 基础设施、协议解析、站点表与接口门面。"""

from ticket_helper.inter.api import Captcha, LoginResult, QueueResult, TicketApi
from ticket_helper.inter.base import HttpClient, HttpResponse, RiskSignal, expect_json, is_login_required
from ticket_helper.inter.parser import (
    SEAT_CODE,
    SEAT_INDEX,
    SeatInfo,
    TrainTicket,
    build_old_passenger_str,
    build_passenger_ticket_str,
    parse_init_dc_html,
    parse_left_ticket_payload,
    parse_left_ticket_result,
)
from ticket_helper.inter.station import StationMap, get_station_map

__all__ = [
    "Captcha",
    "HttpClient",
    "HttpResponse",
    "LoginResult",
    "QueueResult",
    "RiskSignal",
    "SEAT_CODE",
    "SEAT_INDEX",
    "SeatInfo",
    "StationMap",
    "TicketApi",
    "TrainTicket",
    "build_old_passenger_str",
    "build_passenger_ticket_str",
    "expect_json",
    "get_station_map",
    "is_login_required",
    "parse_init_dc_html",
    "parse_left_ticket_payload",
    "parse_left_ticket_result",
]
