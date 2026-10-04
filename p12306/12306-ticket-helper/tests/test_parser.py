"""站点表与余票解析测试（QY-T1 / QY-T6）。"""

from __future__ import annotations

import pytest

from ticket_helper.errors import ResponseParseError, ValidationError
from ticket_helper.inter.parser import (
    SEAT_CODE,
    build_old_passenger_str,
    build_passenger_ticket_str,
    parse_init_dc_html,
    parse_left_ticket_payload,
    parse_left_ticket_result,
)
from ticket_helper.inter.station import StationMap
from mock_server.server import build_ticket_row


# --------------------------------------------------------------------------- #
# QY-T1 站名 → 编码
# --------------------------------------------------------------------------- #
def test_station_code_lookup(station_map: StationMap):
    assert station_map.code_of("北京") == "BJP"
    assert station_map.code_of("广州南") == "IZQ"
    assert station_map.code_of("长沙南") == "CWQ"
    assert len(station_map) > 300


def test_station_lookup_tolerates_suffix_and_code(station_map: StationMap):
    assert station_map.code_of("北京站") == "BJP"          # 容错「站」后缀
    assert station_map.code_of("BJP") == "BJP"             # 直接给电报码
    assert station_map.name_of("BJP") == "北京"
    assert station_map.has("上海虹桥") is True
    assert station_map.has("不存在的火星站") is False


@pytest.mark.parametrize("bad", ["", "   ", "不存在的火星站"])
def test_station_unknown_raises(station_map: StationMap, bad: str):
    with pytest.raises(ValidationError):
        station_map.code_of(bad)


def test_station_bulk_conversion(station_map: StationMap):
    assert station_map.codes_of(["广州", "长沙"]) == ["GZQ", "CSQ"]
    assert station_map.names_of(["GZQ", "CSQ"]) == ["广州", "长沙"]


def test_station_map_offline_still_usable(app_paths):
    """即使不允许联网，包内兜底表也必须可用（离线可启动）。"""
    local = StationMap(app_paths).load(allow_remote=False)
    assert len(local) > 100
    assert local.code_of("广州") == "GZQ"


# --------------------------------------------------------------------------- #
# QY-T6 余票解析
# --------------------------------------------------------------------------- #
def _row(**seats) -> str:
    return build_ticket_row(
        train_code="G6102",
        secret_seed="ss",
        from_code="GZQ",
        to_code="CWQ",
        start_time="08:12",
        arrive_time="10:47",
        duration="02:35",
        train_date="2026-10-01",
        seats=seats,
    )


def test_parse_ticket_row(station_map: StationMap):
    ticket = parse_left_ticket_result(_row(二等座="5", 一等座="无", 商务座="有"), station_map=station_map)
    assert ticket.train_code == "G6102"
    assert ticket.from_station == "广州"
    assert ticket.to_station == "长沙南"
    assert ticket.train_no == "24000G6102A0"
    assert ticket.start_time == "08:12"
    assert ticket.duration == "02:35"
    assert ticket.duration_minutes == 155
    assert ticket.buyable is True
    assert ticket.has_any_ticket is True

    assert ticket.seats["二等座"].available is True
    assert ticket.seats["二等座"].remain_count == 5
    assert ticket.seats["一等座"].available is False
    assert ticket.seats["商务座"].available is True


def test_parse_ticket_no_seat_sold_out(station_map: StationMap):
    ticket = parse_left_ticket_result(_row(二等座="无", 一等座="", 商务座="--"), station_map=station_map)
    assert ticket.has_any_ticket is False
    assert ticket.seats["二等座"].remain_count == 0


def test_parse_standby_marker(station_map: StationMap):
    ticket = parse_left_ticket_result(_row(二等座="候补", 一等座="候补"), station_map=station_map)
    assert ticket.seats["二等座"].standby_only is True
    assert ticket.seats["二等座"].available is False
    assert ticket.standby_seats(["二等座"]) != []


def test_parse_not_yet_on_sale(station_map: StationMap):
    row = build_ticket_row(
        train_code="G1", secret_seed="s", from_code="GZQ", to_code="CWQ",
        start_time="08:00", arrive_time="10:00", duration="02:00",
        train_date="2026-10-01", seats={"二等座": "5"}, can_web_buy="IS_TIME_NOT_BUY",
    )
    ticket = parse_left_ticket_result(row, station_map=station_map)
    assert ticket.buyable is False
    assert ticket.not_yet_on_sale is True


def test_parse_short_row_raises():
    with pytest.raises(ResponseParseError):
        parse_left_ticket_result("a|b|c")


def test_parse_full_payload(station_map: StationMap):
    payload = {
        "httpstatus": 200,
        "status": True,
        "data": {"result": [_row(二等座="5"), _row(二等座="无")], "map": {}, "flag": "1"},
    }
    tickets = parse_left_ticket_payload(payload, station_map=station_map, train_date="2026-10-01")
    assert len(tickets) == 2


def test_parse_payload_bad_row_is_tolerated(station_map: StationMap):
    payload = {"httpstatus": 200, "status": True, "data": {"result": ["garbage", _row(二等座="3")]}}
    tickets = parse_left_ticket_payload(payload, station_map=station_map)
    assert len(tickets) == 1


def test_parse_payload_status_false_raises():
    with pytest.raises(ResponseParseError):
        parse_left_ticket_payload({"status": False, "messages": ["查询失败"]})


def test_parse_payload_not_json():
    with pytest.raises(ResponseParseError):
        parse_left_ticket_payload("oops")  # type: ignore[arg-type]


def test_parse_dict_row_payload(station_map: StationMap):
    """兼容对象数组形式的余票响应。"""
    payload = {
        "status": True,
        "data": {
            "result": [
                {
                    "station_train_code": "D1234",
                    "from_station_telecode": "GZQ",
                    "to_station_telecode": "CWQ",
                    "start_time": "09:00",
                    "arrive_time": "13:00",
                    "lishi": "04:00",
                    "ze_num": "12",
                    "zy_num": "无",
                }
            ]
        },
    }
    tickets = parse_left_ticket_payload(payload, station_map=station_map)
    assert tickets[0].train_code == "D1234"
    assert tickets[0].seats["二等座"].remain_count == 12
    assert tickets[0].seats["一等座"].available is False


# --------------------------------------------------------------------------- #
# 下单参数构造
# --------------------------------------------------------------------------- #
def test_build_passenger_payloads():
    # allEncStr 是真机 getPassengerDTOs 也下发的字段（缺它 checkOrderInfo 必被拒，回流 #51），
    # 所以测试数据必须带上它 —— 否则测的就不是真机形状。
    passengers = [
        {"passenger_name": "张三", "passenger_id_type_code": "1", "passenger_id_no": "110101199001011234",
         "passenger_type": "1", "mobile_no": "13800000000", "allEncStr": "enc-zhangsan"},
        {"passenger_name": "李四", "passenger_id_type_code": "1", "passenger_id_no": "110101199202022345",
         "passenger_type": "1", "allEncStr": "enc-lisi"},
    ]
    ticket_str = build_passenger_ticket_str(passengers, SEAT_CODE["二等座"])
    # 9 段，第 9 段是官方加密串 —— 与上游 make_passenger_ticket_str 逐段对齐（回流 #51）
    assert ticket_str.startswith("O,0,1,张三,1,110101199001011234,13800000000,N,enc-zhangsan")
    assert "_" in ticket_str
    assert ticket_str.count("_") == 1  # 两个乘客用 _ 连接
    assert all(len(c.split(",")) == 9 for c in ticket_str.split("_"))

    old_str = build_old_passenger_str(passengers)
    # 上游：拼完 rstrip('_') 再补 '__ _ _'（源码注释：不加后面请求会出错）
    assert old_str == "张三,1,110101199001011234,1_李四,1,110101199202022345,1__ _ _"
    assert old_str.endswith("__ _ _")


def test_parse_init_dc_html():
    html = (
        "<html><script>"
        "var globalRepeatSubmitToken = 'TOKEN-1';"
        'var ticketInfoForPassengerForm = {"leftTicketStr":"abc","key_check_isChange":"KEY"};'
        "</script></html>"
    )
    parsed = parse_init_dc_html(html)
    assert parsed["repeat_submit_token"] == "TOKEN-1"
    assert parsed["ticket_info"]["key_check_isChange"] == "KEY"


def test_parse_init_dc_html_without_token_raises():
    with pytest.raises(ResponseParseError):
        parse_init_dc_html("<html>登录页</html>")


def test_order_history_reports_official_total_number():
    """0 条时必须能区分「真没订单」与「有单但没取到」。

    真机现象：使用者看到「没有符合条件的订单」，看不出是账号确实没订单、
    还是窗口/口径不对、还是解析全落空。官方 ``order_total_number`` 就是那把尺子。
    """
    from ticket_helper.inter.parser import parse_order_history

    empty = parse_order_history({"status": True, "data": {"OrderDTODataList": [], "order_total_number": 0}})
    assert empty["orders"] == []
    assert empty["total_number"] == 0, "官方说 0 单 → 程序没问题，别再当成 bug 查"

    some = parse_order_history({"status": True, "data": {"OrderDTODataList": [], "order_total_number": "3"}})
    assert some["total_number"] == 3, "官方说有单却没取到 → 这才需要继续查"

    missing = parse_order_history({"status": True, "data": {"OrderDTODataList": []}})
    assert missing["total_number"] is None, "拿不到就如实说「不知道」，不能退化成 0"


def test_order_history_reports_official_total_number():
    """0 条时必须能区分「真没订单」与「有单但没取到」。

    真机现象：使用者看到「没有符合条件的订单」，看不出是账号确实没订单、
    还是窗口/口径不对、还是解析全落空。官方 ``order_total_number`` 就是那把尺子。
    """
    from ticket_helper.inter.parser import parse_order_history

    empty = parse_order_history({"status": True, "data": {"OrderDTODataList": [], "order_total_number": 0}})
    assert empty["orders"] == []
    assert empty["total_number"] == 0, "官方说 0 单 → 程序没问题，别再当成 bug 查"

    some = parse_order_history({"status": True, "data": {"OrderDTODataList": [], "order_total_number": "3"}})
    assert some["total_number"] == 3, "官方说有单却没取到 → 这才需要继续查"

    missing = parse_order_history({"status": True, "data": {"OrderDTODataList": []}})
    assert missing["total_number"] is None, "拿不到就如实说「不知道」，不能退化成 0"
