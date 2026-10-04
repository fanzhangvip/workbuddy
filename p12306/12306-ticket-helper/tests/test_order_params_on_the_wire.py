"""下单核心参数必须与上游逐段一致（回流 #51）。

真实故障（2026-09-30 真机）
-------------------------
余票充足、席别也选对了，``checkOrderInfo`` 却被官方回一句含糊的
**「余票不足！」**。逐字对照上游才发现两处形状差异：

- ``passengerTicketStr`` 少了**第 9 段** —— 官方加密串 ``allEncStr``；
- ``oldPassengerStr`` 没按上游补 ``'__ _ _'``（上游自己那行注释写着
  「**不加后面请求会出错**」）。

为什么以前一直全绿
------------------
mock 的 ``checkOrderInfo`` 原先**完全不校验参数**，于是「真机必被拒的形状」
在离线永远通过。这与 #48（余票 ``secretStr`` 没解码，而 mock 造的是纯文本、
不含 ``%``）是**同一个机制**：mock 按我们自己的形状出卷子，
只能证明「我和我自己一致」。

所以本文件的判据不是「函数返回什么」，而是
**mock 记录下来的表单长什么样** —— 把请求发出去、再看线缆上是什么。

变异（撤掉实现必须变红）
------------------------
去掉 ``build_passenger_ticket_str`` 的第 9 段，或去掉
``build_old_passenger_str`` 的 ``'__ _ _'``，本文件立即变红。
"""

from __future__ import annotations

import pytest

from ticket_helper.errors import InterError
from ticket_helper.inter import urls as inter_urls
from ticket_helper.inter.api import TicketApi
from ticket_helper.inter.base import HttpClient
from ticket_helper.inter.parser import (
    SEAT_CODE,
    build_old_passenger_str,
    build_passenger_ticket_str,
    parse_left_ticket_result,
)
from mock_server.server import (
    MOCK_LEFT_TICKET_STR,
    MOCK_YP_INFO,
    build_ticket_row,
)

#: 与真机 ``getPassengerDTOs`` 同形状的乘车人（**含官方加密串**）
PASSENGER: dict[str, str] = {
    "passenger_name": "张三",
    "passenger_id_type_code": "1",
    "passenger_id_no": "110101199001011234",
    "passenger_type": "1",
    "mobile_no": "13800000000",
    "allEncStr": "enc-zhangsan-abc",
}


# --------------------------------------------------------------------------- #
# 纯函数层（快、判据直白）：形状逐段对齐上游模板
# --------------------------------------------------------------------------- #
def test_passenger_ticket_str_has_nine_fields_per_passenger() -> None:
    """上游模板 ``...,N,{enc_str}_`` → 每人 **9 段**，第 9 段是加密串。"""
    built = build_passenger_ticket_str([PASSENGER], SEAT_CODE["二等座"])
    fields = built.split(",")
    assert len(fields) == 9, built
    assert fields[7] == "N"
    assert fields[8] == "enc-zhangsan-abc", "第 9 段必须是 allEncStr"


def test_old_passenger_str_has_the_upstream_suffix() -> None:
    """上游：拼完 ``rstrip('_')`` 再补 ``'__ _ _'``。"""
    built = build_old_passenger_str([PASSENGER])
    assert built == "张三,1,110101199001011234,1__ _ _", built
    assert built.endswith("__ _ _")


def test_missing_enc_str_still_emits_the_ninth_field(caplog) -> None:
    """缺加密串时**仍然发第 9 段（空串）**，但必须留下 WARNING。

    为什么不静默退回 8 段：那样真机上又变成「说不清的拒绝」。
    宁可形状固定、把可观测性交给日志。
    """
    import logging

    bare = {k: v for k, v in PASSENGER.items() if k != "allEncStr"}
    with caplog.at_level(logging.WARNING, logger="ticket_helper.inter.parser"):
        built = build_passenger_ticket_str([bare], SEAT_CODE["二等座"])
    assert built.endswith(",N,"), built
    assert "allEncStr" in caplog.text, "缺加密串必须留下可观测的痕迹"


# --------------------------------------------------------------------------- #
# 线缆层：mock 记下的表单才是判据
# --------------------------------------------------------------------------- #
def _api(handle, app_context) -> TicketApi:
    return TicketApi(HttpClient(base_url=handle.base_url, retries=0), app_context.station_map)


def _ticket(app_context):
    row = build_ticket_row(
        train_code="G6102",
        secret_seed="mock-secret-1",
        from_code="GZQ",
        to_code="CWQ",
        start_time="08:12",
        arrive_time="10:47",
        duration="02:35",
        train_date="2026-10-01",
        seats={"二等座": "5"},
    )
    return parse_left_ticket_result(row, station_map=app_context.station_map)


def _recorded(handle, path: str) -> dict:
    """mock 记下的**某一步的完整表单**。"""
    for item in reversed(handle.state.requests):
        if item["path"] == path:
            return dict(item["payload"] or {})
    return {}


def _open_order(handle, app_context, passengers):
    """走到 ``checkOrderInfo``：锁票 → initDc 拿 token。"""
    api = _api(handle, app_context)
    ticket = _ticket(app_context)
    api.submit_order_request(ticket, from_name="广州南", to_name="长沙南")
    token = api.init_dc().get("repeat_submit_token", "")
    assert len(token) >= 8, "mock 必须下发 token，实际 %r" % token
    return api, ticket, token, passengers


def test_check_order_info_sends_the_enc_str_on_the_wire(mock_server, app_context) -> None:
    """★ 核心判据：发出去的 ``passengerTicketStr`` 第 9 段就是官方加密串。"""
    mock_server.state.requests.clear()
    api, _ticket_obj, token, _ = _open_order(mock_server, app_context, [PASSENGER])

    assert api.check_order_info([PASSENGER], SEAT_CODE["二等座"], token=token, rand_code="") is True

    form = _recorded(mock_server, inter_urls.CHECK_ORDER_INFO)
    assert "passengerTicketStr" in form, "mock 必须记下这一步的表单：%r" % form
    fields = form["passengerTicketStr"].split(",")
    assert len(fields) == 9, f"线上必须 9 段，实际 {len(fields)}：{form['passengerTicketStr']!r}"
    assert fields[8] == PASSENGER["allEncStr"], "第 9 段必须是官方加密串"
    assert form["oldPassengerStr"].endswith("__ _ _"), form["oldPassengerStr"]


def test_confirm_single_for_queue_sends_the_same_shape(mock_server, app_context) -> None:
    """★ 第 6 步（真正建单前的那一步）用的是**同一份参数**，也必须同形状。"""
    mock_server.state.requests.clear()
    api, ticket, token, _ = _open_order(mock_server, app_context, [PASSENGER])
    api.check_order_info([PASSENGER], SEAT_CODE["二等座"], token=token, rand_code="")

    # 故意**不传** left_ticket：走 api 层的取值链（initDc 的 leftTicketStr 优先）。
    api.confirm_single_for_queue(
        [PASSENGER],
        SEAT_CODE["二等座"],
        ticket,
        key_check_is_change="",
        token=token,
        rand_code="",
    )

    form = _recorded(mock_server, inter_urls.CONFIRM_SINGLE_FOR_QUEUE)
    assert "passengerTicketStr" in form, "mock 必须记下这一步的表单：%r" % form
    assert len(form["passengerTicketStr"].split(",")) == 9, form["passengerTicketStr"]
    assert form["oldPassengerStr"].endswith("__ _ _"), form["oldPassengerStr"]
    # ★ 回流 #61 → #64 复核：`leftTicketStr` 的**权威来源是 initDc 页面下发的
    # `leftTicketStr`**（加密长串）—— 真机 2026-10-03 证实它与余票行的
    # `yp_info` **不是一个值**（#61 当时「= yp_info」的判断是半对：字段下标
    # 对了，但真机真正认的是 initDc 那份）。mock 里两者故意不同。
    assert form["leftTicketStr"] == MOCK_LEFT_TICKET_STR, form["leftTicketStr"]


def test_left_ticket_comes_from_init_dc_not_the_query_row(mock_server, app_context) -> None:
    """★ 第 ⑤ 步（getQueueCount）的 `leftTicket` 权威来源是 initDc 页面。

    真机 2026-10-03 存盘页面证实：initDc 下发的 ``leftTicketStr`` 是一长串
    URL 编码的加密串，**与余票行的 ``yp_info`` 不是一个值**；上游
    ``order/order.py:388`` 也从 ``ticketInfoForPassengerForm`` 取。
    同时 ``train_date`` 必须是官方浏览器同款 JS 日期串（发 ``2026-10-01``
    真机被回「系统忙」，回流 #64）。
    """
    mock_server.state.requests.clear()
    api, ticket, token, _ = _open_order(mock_server, app_context, [PASSENGER])

    assert ticket.yp_info, "解析余票时必须留下 yp_info（下标 12）"
    assert ticket.yp_info != ticket.seat_types_raw, (
        "两个字段必须可分辨，否则这条判据分辨不出「取错字段」"
    )
    # initDc 之后 ticket_info 必须带上页面下发的取值（mock 按真机形状下发）
    assert api.ticket_info.get("leftTicketStr") == MOCK_LEFT_TICKET_STR, api.ticket_info

    api.get_queue_count(ticket, SEAT_CODE["二等座"], token=token)

    form = _recorded(mock_server, inter_urls.GET_QUEUE_COUNT)
    assert form.get("leftTicket") == MOCK_LEFT_TICKET_STR, form
    assert form.get("train_location"), "train_location 不能为空"
    assert form.get("train_date") == (
        "Thu Oct 01 2026 00:00:00 GMT+0800 (China Standard Time)"
    ), form.get("train_date")


def test_missing_enc_str_is_rejected_not_silently_accepted(mock_server, app_context) -> None:
    """★ 反向判据：缺加密串时 mock **必须照真机拒绝**，不许静默放过。

    这条同时守住「mock 的校验真的在跑」——如果哪天有人把 mock 的校验删了，
    本用例会失败（因为它期望被拒却拿到了 True）。
    """
    mock_server.state.requests.clear()
    bare = {k: v for k, v in PASSENGER.items() if k != "allEncStr"}
    api, _t, token, _ = _open_order(mock_server, app_context, [bare])

    with pytest.raises(InterError) as excinfo:
        api.check_order_info([bare], SEAT_CODE["二等座"], token=token, rand_code="")
    assert "余票不足" in str(excinfo.value), "真机就是回这句含糊话术，mock 要复现它"


def test_old_passenger_str_suffix_is_checked_on_the_wire(mock_server, app_context, monkeypatch) -> None:
    """★ 反向判据：把 ``'__ _ _'`` 拿掉，mock 必须拒（这条就是 #51 的后半段）。"""
    monkeypatch.setattr(
        "ticket_helper.inter.api.build_old_passenger_str",
        lambda passengers: "张三,1,110101199001011234,1_",
    )
    mock_server.state.requests.clear()
    api, _t, token, _ = _open_order(mock_server, app_context, [PASSENGER])

    with pytest.raises(InterError) as excinfo:
        api.check_order_info([PASSENGER], SEAT_CODE["二等座"], token=token, rand_code="")
    assert "余票不足" in str(excinfo.value)
