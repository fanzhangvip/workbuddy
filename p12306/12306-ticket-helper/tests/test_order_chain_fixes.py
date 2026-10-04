"""下单链路四处硬缺陷的回归（回流 #60 / #61）。

判据全部落在**线缆上**（mock 记下的请求），而不是函数返回值 ——
因为这几处缺陷的共同点就是「离线怎么测都是绿的」：

| 缺陷 | 为什么离线看不见 |
|---|---|
| 下单验证码打了一条**不存在的路径** + `rand` 写错 | mock 以前**照着我们自己的错路径**实现，返回 PNG |
| `leftTicketStr` / `leftTicket` 取了余票行下标 15（席别列表） | mock 的这两步**只记录不校验** |
| `ticketInfoForPassengerForm` 解析要求结尾分号、失败还静默 | 没有一条用例覆盖「无分号」与「页面里没有」两种形态 |
| `waitTime=-2/-3`（失败/订单已撤销）被判成「排队超时，请自查」 | mock 只有 `queryOrderWaitTimeStatus=false` 一种失败形状 |

每一条**撤掉实现都会变红**：错路径由 mock 如实回 HTML 错误页、
关键参数由 mock 校验、负 waitTime 由新场景 `queue_cancelled` 复现。
"""

from __future__ import annotations

import pytest

from mock_server.server import MOCK_YP_INFO, build_ticket_row
from ticket_helper.errors import CaptchaUnavailableError, QueueError
from ticket_helper.inter import urls as inter_urls
from ticket_helper.inter.api import TicketApi
from ticket_helper.inter.base import HttpClient
from ticket_helper.inter.parser import SEAT_CODE, parse_init_dc_html, parse_left_ticket_result
from ticket_helper.order.service import ORDER_FAILED, OrderService

PASSENGER: dict[str, str] = {
    "passenger_name": "张三",
    "passenger_id_type_code": "1",
    "passenger_id_no": "110101199001011234",
    "passenger_type": "1",
    "mobile_no": "13800000000",
    "allEncStr": "enc-zhangsan-abc",
}


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _api(handle, app_context=None) -> TicketApi:
    station_map = app_context.station_map if app_context is not None else None
    return TicketApi(HttpClient(base_url=handle.base_url, retries=0), station_map)


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
    for item in reversed(handle.state.requests):
        if item["path"] == path:
            return dict(item["payload"] or {})
    return {}


class _FixedVerify:
    """固定答案的识别器替身（让下单链路能在 mock 上走完）。"""

    def recognize(self, image: bytes, *, hint: str = "", kind: str = "login") -> str:
        # 判据要具体：只看「非空」的话，`#59` 那种「把 HTML 错误页当图片传下来」
        # 的缺陷依旧能通过（HTML 当然也是「非空」）。
        assert image[:4] == b"\x89PNG", f"识别器必须收到真图片，实际前 4 字节 {image[:4]!r}"
        assert kind == "order", f"下单验证码必须带 kind=order，实际 {kind!r}"
        return "112,45"


class _FakeUser:
    def __init__(self, api: TicketApi) -> None:
        self.api = api
        self.verify = _FixedVerify()
        self.username = "test_user"


# --------------------------------------------------------------------------- #
# ① 下单验证码：路径与 rand（回流 #60）
# --------------------------------------------------------------------------- #
def test_order_captcha_uses_the_upstream_path_and_rand(mock_server) -> None:
    """★ 必须打 ``/otn/passcodeNew/getPassCodeNew``，且 ``rand=randp``。

    出处：``reference/upstream/testerSunshine_urlConf.py:334`` 的 ``codeImgByOrder``
    = ``/otn/passcodeNew/getPassCodeNew?module=passenger&rand=randp``。
    原实现写成 ``/otn/confirmPassenger/getPassCodeNew`` + ``rand=sjrand`` ——
    真机只回 HTML 错误页，图片**永远**拿不到（#59 那个「图片无法显示」的上游成因）。
    """
    assert inter_urls.GET_PASS_CODE_NEW == "/otn/passcodeNew/getPassCodeNew"
    assert inter_urls.ORDER_PASS_CODE_RAND == "randp"

    mock_server.state.requests.clear()
    captcha = _api(mock_server).get_order_captcha()

    assert captcha.image_bytes.startswith(b"\x89PNG"), "必须真的拿到图片字节"
    form = _recorded(mock_server, inter_urls.GET_PASS_CODE_NEW)
    assert form == {"module": "passenger", "rand": "randp"}, form


def test_a_wrong_rand_gets_no_image(mock_server) -> None:
    """反向判据：``rand`` 写错时官方不给图（真机形状 = HTTP 200 + HTML 错误页）。

    这条同时守住「mock 的 rand 校验真的在跑」——校验一旦被删，本用例会以
    「拿到了图片」失败，而不是静默通过。
    """
    with pytest.raises(CaptchaUnavailableError) as excinfo:
        _api(mock_server).get_order_captcha(rand="sjrand")
    assert "HTML" in str(excinfo.value.message), excinfo.value.message


# --------------------------------------------------------------------------- #
# ② leftTicket 的取值来源（回流 #61）
# --------------------------------------------------------------------------- #
def test_yp_info_is_parsed_and_differs_from_seat_types(app_context) -> None:
    """前提判据：两个字段必须**可分辨**，否则「取错字段」根本抓不住。"""
    ticket = _ticket(app_context)
    assert ticket.yp_info == MOCK_YP_INFO
    assert ticket.seat_types_raw == "O9M3"
    assert ticket.yp_info != ticket.seat_types_raw


def test_get_queue_count_falls_back_to_yp_info(mock_server, app_context) -> None:
    """★ 页面没下发 ticketInfoForPassengerForm 时，``leftTicket`` 兜底 ``yp_info``。

    真机 2026-10-03 复核：默认页面**会**下发（mock 默认也按真机形状发），
    所以「没有」只是一种防御分支（场景 ``no_initdc_form``）——
    但兜底本身必须仍然可用，且 mock 在该场景下接受 ``yp_info``。
    """
    mock_server.state.requests.clear()
    mock_server.set_scenario("no_initdc_form")
    # 场景要真的切过去（`set_scenario` 不返回值，判据落在状态上）
    assert mock_server.state.scenario == "no_initdc_form"
    try:
        api = _api(mock_server, app_context)
        ticket = _ticket(app_context)
        api.submit_order_request(ticket, from_name="广州南", to_name="长沙南")
        token = api.init_dc().get("repeat_submit_token", "")
        assert len(token) >= 8, f"mock 必须下发 token，实际 {token!r}"
        assert api.ticket_info == {}, f"该场景不该下发 legacy form：{api.ticket_info}"

        api.get_queue_count(ticket, SEAT_CODE["二等座"], token=token)
    finally:
        mock_server.set_scenario("has_ticket")

    form = _recorded(mock_server, inter_urls.GET_QUEUE_COUNT)
    assert form.get("leftTicket") == MOCK_YP_INFO, form
    assert form.get("train_location") == "H1", form


def test_mock_rejects_a_wrong_left_ticket(mock_server, app_context) -> None:
    """反向判据：把 ``leftTicket`` 换成席别列表，第 ⑤ 步必须被拒（判据真的在跑）。"""
    api = _api(mock_server, app_context)
    ticket = _ticket(app_context)
    api.submit_order_request(ticket, from_name="广州南", to_name="长沙南")
    token = api.init_dc().get("repeat_submit_token", "")

    with pytest.raises(QueueError):
        api.get_queue_count(ticket, SEAT_CODE["二等座"], left_ticket="O9M3", token=token)


# --------------------------------------------------------------------------- #
# ③ initDc 解析：多种写法都要认出来，且不再静默
# --------------------------------------------------------------------------- #
def _page(form_js: str) -> str:
    return "<script>var globalRepeatSubmitToken = 'TK';" + form_js + "</script>"


@pytest.mark.parametrize(
    ("label", "form_js", "expected"),
    [
        (
            "带分号（老写法）",
            "var ticketInfoForPassengerForm = {'leftTicketStr':'AAA','key_check_isChange':'KEY'};",
            {"leftTicketStr": "AAA", "key_check_isChange": "KEY"},
        ),
        (
            "无分号、换行结尾",
            'var ticketInfoForPassengerForm = {"leftTicketStr":"BBB","key_check_isChange":"K2"}',
            {"leftTicketStr": "BBB", "key_check_isChange": "K2"},
        ),
        (
            "JSON.parse 包裹",
            'ticketInfoForPassengerForm=JSON.parse(\'{"leftTicketStr":"CCC",'
            '"key_check_isChange":"K3"}\');',
            {"leftTicketStr": "CCC", "key_check_isChange": "K3"},
        ),
    ],
)
def test_init_dc_parser_accepts_known_shapes(label: str, form_js: str, expected: dict) -> None:
    parsed = parse_init_dc_html(_page(form_js))
    assert parsed["ticket_info"] == expected, f"{label}: {parsed['ticket_info']}"


def test_init_dc_without_the_form_warns_instead_of_silently_returning_empty(caplog) -> None:
    """★ 取不到必须**说出来**：它决定第 ⑧ 步会不会带空值去建单。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="ticket_helper.inter.parser"):
        parsed = parse_init_dc_html(_page("var other = 1;"))
    assert parsed["ticket_info"] == {}
    assert "ticketInfoForPassengerForm" in caplog.text
    assert "key_check_isChange" in caplog.text, "要说清后果，而不是只说没找到"


def test_init_dc_salvages_loose_values_when_the_wrapper_is_unparsable() -> None:
    """整段 form 拿不到时，至少把那几个值抠出来（判据要可判读，不要退化成空串）。"""
    parsed = parse_init_dc_html(_page("var other={'leftTicketStr':'DDD','key_check_isChange':'K5'};"))
    assert parsed["ticket_info"] == {"leftTicketStr": "DDD", "key_check_isChange": "K5"}


# --------------------------------------------------------------------------- #
# ④ 排队终态：waitTime=-3（订单已撤销）是**失败**，不是「结果未知」
# --------------------------------------------------------------------------- #
def test_cancelled_queue_is_reported_as_failure(mock_server, app_context) -> None:
    """★ 否则「已撤销」会被说成「排队超时，请到未完成订单查看」—— 与
    「把锁票成功当成已建单」是同一类错误：把确定的失败说成可能成功。"""
    mock_server.set_scenario("queue_cancelled")
    # 场景是真的切过去了（`set_scenario` 对未登记的场景会**静默返回 False**，
    # 不确认的话后面失败的会是「排队失败」这种与主题无关的断言）。
    assert mock_server.state.scenario == "queue_cancelled"

    api = _api(mock_server, app_context)
    ticket = _ticket(app_context)
    service = OrderService()

    result = service.submit(
        _FakeUser(api), ticket, ticket.seats["二等座"], [PASSENGER], max_retry=2
    )

    assert result.ok is False
    assert result.status == ORDER_FAILED, result.message
    assert "排队失败" in result.message, result.message
    assert "撤销" in result.message, result.message
    assert result.retryable is False, "已撤销的订单不该让上层重试"
