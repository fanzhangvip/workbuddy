"""个人中心（账号信息 / 乘车人 / 我的订单）测试。

覆盖三类东西：
1. **解析层**：官方字段名不统一、打码口径、订单状态码 → 中文；
2. **协议层**：请求方法（POST→GET 回退）、分页、HTML 错误页、历史订单日期上限；
3. **聚合层**：一块失败不影响其它块（这是这套接口最容易出的体验问题）。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from ticket_helper.account.profile import AccountProfileService
from ticket_helper.errors import InterError, ResponseParseError, ValidationError
from ticket_helper.inter.api import TicketApi
from ticket_helper.inter.base import HttpClient
from ticket_helper.inter.parser import (
    mask_email,
    mask_id_no,
    mask_phone,
    parse_order_history,
    parse_passengers_payload,
    parse_user_profile,
    truthy_flag,
)


# --------------------------------------------------------------------------- #
# 解析层
# --------------------------------------------------------------------------- #
def test_mask_helpers_keep_edges():
    assert mask_id_no("110101199001011234") == "110101********1234"
    assert mask_phone("13800008888") == "138****8888"
    assert mask_email("zhangsan@example.com") == "z*******@example.com"
    # 太短的值原样返回：硬打码会把一个手机号打成 "138****" 这种没信息量的东西
    assert mask_id_no("") == ""
    assert mask_phone("123") == "123"


@pytest.mark.parametrize(
    "raw,expected",
    [("Y", True), ("1", True), ("N", False), ("0", False), ("", None), (None, None), ("?", None)],
)
def test_truthy_flag_is_tri_state(raw, expected):
    """判不出来必须是 ``None``，不能退化成 False。

    退化成 False 会让「接口没给字段」被显示成「未核验」，
    使用者会以为账号被 12306 限制了 —— 这是有过真实投诉的误导。
    """
    assert truthy_flag(raw) is expected


def test_parse_user_profile_normalizes_aliases_and_masks():
    profile = parse_user_profile(
        {
            "data": {
                "user_name": "张三",
                "sexCode": "M",
                "email": "zhangsan@example.com",
                "mobile_no": "13800008888",
                "id_no": "110101199001011234",
                "isUserVerified": "Y",
                "some_new_field": "x",
            }
        }
    )
    assert profile["username"] == "张三"
    assert profile["sex"] == "男"
    assert profile["mobile"] == "138****8888"
    assert profile["id_no"] == "110101********1234"
    assert profile["email"] == "z*******@example.com"
    assert profile["verified"] is True
    # 未识别字段只留键名（方便真机排查官方又加了什么），不留值
    assert "some_new_field" in profile["extra_keys"]


def test_parse_user_profile_verified_unknown_when_field_absent():
    profile = parse_user_profile({"data": {"user_name": "张三"}})
    assert profile["verified"] is None


def test_parse_user_profile_unwraps_nested_user_dto():
    """有的版本把用户信息再包一层 ``userDTO``。"""
    profile = parse_user_profile({"data": {"userDTO": {"user_name": "李四", "isUserVerified": "N"}}})
    assert profile["username"] == "李四"
    assert profile["verified"] is False


def test_parse_passengers_accepts_both_envelope_shapes():
    """两条链路（个人中心 / 下单）的信封不一样，都要能解析。"""
    directory = parse_passengers_payload(
        {"status": True, "data": {"datas": [{"passenger_name": "张三", "passenger_id_no": "110101199001011234"}], "count": 1}}
    )
    ordering = parse_passengers_payload(
        {"status": True, "data": {"normal_passengers": [{"passenger_name": "李四"}]}}
    )
    assert directory[0]["passenger_name"] == "张三"
    assert directory[0]["passenger_id_no"] == "110101********1234"
    assert ordering[0]["passenger_name"] == "李四"


def test_parse_passengers_skips_rows_without_name():
    assert parse_passengers_payload({"data": {"datas": [{"passenger_id_no": "x"}, "junk"]}}) == []


def test_parse_order_history_maps_fields_and_status_codes():
    result = parse_order_history(
        {
            "data": {
                "OrderDTODataList": [
                    {
                        "sequence_no": "E123",
                        "order_date": "2026-09-20 10:12:00",
                        "station_train_code": "G6102",
                        "train_date": "20261001",
                        "from_station_name": "广州南",
                        "to_station_name": "长沙南",
                        "ticket_total_price": "314.00",
                        "order_status_name": "已支付",
                        "pay_flag": "N",
                        "passengerDTOs": [
                            {
                                "passenger_name": "张三",
                                "seat_type_name": "二等座",
                                "coach_no": "05",
                                "seat_no": "12A",
                                "ticket_status": "a",
                            }
                        ],
                    }
                ],
                # 官方的 pageIndex 是 **0-based**（真机 train_order 页面 JS：pageIndex:"0"），
                # 这里给 1 = 第 2 页；解析层对外统一成 1-based。
                "pageIndex": 1,
                "pageCount": "3",
            }
        }
    )
    order = result["orders"][0]
    assert order["order_no"] == "E123"
    assert order["train_code"] == "G6102"
    # 官方给的 8 位日期要归一化成带横杠的，否则同一个日期会出现两种写法
    assert order["train_date"] == "2026-10-01"
    assert order["passenger_names"] == "张三"
    assert order["passengers"][0]["status"] == "已支付"     # 由状态码 a 翻译而来
    assert order["need_pay"] is False
    assert (result["page_index"], result["page_count"]) == (2, 3)


def test_parse_order_history_pay_flag_marks_need_pay():
    result = parse_order_history({"data": {"OrderDTODataList": [{"pay_flag": "Y"}], "pageIndex": 1, "pageCount": 1}})
    assert result["orders"][0]["need_pay"] is True


def test_parse_order_history_rejects_error_envelope():
    with pytest.raises(ResponseParseError):
        parse_order_history({"status": False, "messages": ["请重新登录"], "data": {}})
    with pytest.raises(ResponseParseError):
        parse_order_history("not a dict")


# --------------------------------------------------------------------------- #
# 协议层（对 mock 跑）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def api(app_context, mock_server):
    client = HttpClient(base_url=mock_server.base_url, retries=0)
    yield TicketApi(client, app_context.station_map)
    client.close()


@pytest.fixture()
def logged_in_api(api, mock_server):
    """把 mock 的会话置为已登录（这两个接口真机上都要求登录）。"""
    mock_server.state.current_login_user = "test_user"
    return api


def test_profile_uses_post_first(api, mock_server):
    """真机是 POST。

    这条断言的意义：客户端曾经照字面把 ``initMy12306Api`` 当 GET 调，
    在部分部署上直接拿到登录页 HTML。守住「优先 POST」这件事，
    免得日后有人看着路径里的 init 又给它改回 GET。
    """
    mock_server.state.current_login_user = "test_user"
    profile = api.get_user_profile()
    assert profile["username"] == "张三"
    methods = [r["payload"]["method"] for r in mock_server.state.requests if r["path"] == "/otn/index/initMy12306Api"]
    assert methods and methods[0] == "POST"


def test_profile_reports_login_error_when_session_is_not_established(api):
    """未登录时官方回 HTML 登录页，必须报成「需要登录」而不是「解析失败」。"""
    with pytest.raises(InterError) as excinfo:
        api.get_user_profile()
    assert "网页" in str(excinfo.value)


def test_query_passengers_paginates_and_masks(logged_in_api, mock_server):
    rows = logged_in_api.query_passengers(page_size=1)
    assert [r["passenger_name"] for r in rows] == ["张三", "李四"]      # 两页都取回来了
    assert all("*" in r["passenger_id_no"] for r in rows)              # 证件号一律打码
    pages = [
        r["payload"].get("pageIndex")
        for r in mock_server.state.requests
        if r["path"] == "/otn/passengers/query"
    ]
    assert pages == ["1", "2"]     # 表单字段原样是字符串，这里断言的是「翻了两页」


def test_query_order_history_filters_by_where(logged_in_api):
    pending = logged_in_api.query_order_history(where="G")
    history = logged_in_api.query_order_history(where="H")
    assert {o["order_no"] for o in pending["orders"]} == {"E900000001", "E900000002"}
    assert {o["order_no"] for o in history["orders"]} == {"E800000001"}
    assert pending["where"] == "G" and history["where"] == "H"


def test_query_order_history_clamps_end_date_for_history(logged_in_api, mock_server):
    """历史订单的结束日期必须是「昨天」。

    传今天的话官方会把今天算进未出行范围，结果就是「查历史订单什么都查不到」——
    这类静默空结果最难排查，所以在协议层就按死。
    """
    logged_in_api.query_order_history(where="H", end_date=date.today().strftime("%Y-%m-%d"))
    sent = [
        r["payload"] for r in mock_server.state.requests if r["path"] == "/otn/queryOrder/queryMyOrder"
    ][-1]
    assert sent["queryEndDate"] == (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


def test_query_order_history_keyword_search(logged_in_api):
    """关键字按「订单号 / 车次 / 乘客姓名」筛选。"""
    assert len(logged_in_api.query_order_history(where="G", keyword="张三")["orders"]) == 1
    assert len(logged_in_api.query_order_history(where="G", keyword="李四")["orders"]) == 1
    assert len(logged_in_api.query_order_history(where="G", keyword="G6102")["orders"]) == 1
    assert len(logged_in_api.query_order_history(where="G", keyword="E900000002")["orders"]) == 1
    assert logged_in_api.query_order_history(where="G", keyword="不存在的车次")["orders"] == []


def test_query_order_history_rejects_unknown_scope(logged_in_api):
    with pytest.raises(ValidationError):
        logged_in_api.query_order_history(where="X")


# --------------------------------------------------------------------------- #
# 聚合层：一块失败不能拖垮其它块
# --------------------------------------------------------------------------- #
class _StubUser:
    """替身：只暴露 profile 服务用到的属性。"""

    def __init__(self, api, username: str = "u1", online: bool = True) -> None:
        self.api = api
        self.username = username
        self.is_login = online


def test_fetch_keeps_other_sections_when_orders_fail(logged_in_api, monkeypatch):
    def _boom(**_kwargs):
        raise InterError("订单接口被风控")

    monkeypatch.setattr(logged_in_api, "query_order_history", _boom)
    service = AccountProfileService()
    payload = service.fetch(_StubUser(logged_in_api), where="G")

    assert payload["profile"]["ok"] is True
    assert payload["passengers"]["ok"] is True
    assert payload["orders"]["ok"] is False
    assert "风控" in payload["orders"]["error"]


def test_fetch_reports_not_logged_in_with_a_actionable_message(logged_in_api):
    service = AccountProfileService()
    payload = service.fetch(_StubUser(logged_in_api, online=False), with_orders=False)
    assert payload["profile"]["ok"] is False
    assert "未登录" in payload["profile"]["error"]
    assert payload["passengers"]["ok"] is False


def test_passengers_are_cached_then_invalidated(logged_in_api, mock_server):
    service = AccountProfileService(passengers_ttl=300)
    user = _StubUser(logged_in_api)
    first = service.passengers_of(user)
    calls_after_first = len([r for r in mock_server.state.requests if r["path"] == "/otn/passengers/query"])
    second = service.passengers_of(user)
    assert second["cached"] is True
    assert len([r for r in mock_server.state.requests if r["path"] == "/otn/passengers/query"]) == calls_after_first

    service.invalidate(user.username)
    service.passengers_of(user)
    assert len([r for r in mock_server.state.requests if r["path"] == "/otn/passengers/query"]) > calls_after_first
    assert first["ok"] is True


# --------------------------------------------------------------------------- #
# Web API
# --------------------------------------------------------------------------- #
def _web_client(app_context, mock_server):
    from ticket_helper.verify.service import CallbackSolver
    from ticket_helper.web.app import create_app

    app_context.verify_service.set_solver(CallbackSolver(lambda i, h, k: "112,45", name="fixed"))
    app = create_app(app_context, start_background=False)
    app.config["TESTING"] = True
    return app.test_client()


def _login(client) -> str:
    response = client.post("/api/auth/login", json={"user": "admin", "pass": "test-pass"})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["token"]


def _wait_until(predicate, timeout: float = 20.0, interval: float = 0.2):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def _make_web_account(app_context, mock_server, username: str, *, online: bool = True):
    """建一个账号并等到它真的登录成功，返回 (client, token, user_id)。

    **必须等登录完成**：保存账号触发的自动登录是异步的，
    不等就会在「账号还在登录中」时去拉个人中心，
    拿到的是「账号未登录」——然后你会去查一个根本不存在的 bug。
    """
    client = _web_client(app_context, mock_server)
    token = _login(client)
    created = client.post(
        "/api/users", json={"username": username, "password": "pw"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert created.status_code == 201, created.get_data(as_text=True)
    user_id = created.get_json()["id"]

    user = _wait_until(lambda: app_context.accounts.get(username))
    assert user is not None, "账号未加载到运行时"
    assert _wait_until(lambda: user.is_login), "账号未能登录（mock 登录链路可能有问题）"

    if online:
        mock_server.state.current_login_user = username
    else:
        # 会话「还在」，但服务端认不出当前用户 → 复现「登录态失效」时个人中心的报错
        mock_server.state.current_login_user = ""
    return client, token, user_id


@pytest.fixture()
def web_account(app_context, mock_server):
    return _make_web_account(app_context, mock_server, "profile_user")


def test_web_profile_endpoint_returns_all_sections(web_account):
    client, token, user_id = web_account
    payload = client.get(
        f"/api/users/{user_id}/profile?with_orders=1", headers={"Authorization": f"Bearer {token}"}
    ).get_json()
    assert payload["account"]["id"] == user_id
    assert payload["profile"]["ok"] is True
    assert payload["profile"]["data"]["username"] == "张三"
    assert [p["passenger_name"] for p in payload["passengers"]["data"]] == ["张三", "李四"]
    assert payload["orders"]["ok"] is True
    assert payload["pending"]["ok"] is True


def test_web_profile_section_error_is_reported_not_raised(app_context, mock_server):
    """订单失败时接口仍返回 200，失败原因放在 orders.error 里。

    若改成让整条请求 500，界面就只剩一句「请求失败」，
    完全看不出是「订单查询被风控」还是「账号信息接口挂了」。
    """
    client, token, user_id = _make_web_account(app_context, mock_server, "offline_user", online=False)
    payload = client.get(
        f"/api/users/{user_id}/profile?with_orders=1", headers={"Authorization": f"Bearer {token}"}
    ).get_json()
    assert payload["profile"]["ok"] is False
    assert payload["profile"]["error"]
    assert payload["orders"]["ok"] is False
    assert payload["orders"]["error"]


def test_web_orders_endpoint_pagination(web_account):
    client, token, user_id = web_account
    first = client.get(f"/api/users/{user_id}/orders?where=G&page=1&size=1", headers={"Authorization": f"Bearer {token}"}).get_json()
    assert first["ok"] is True
    assert first["data"]["page_count"] == 2
    assert len(first["data"]["orders"]) == 1
    assert first["account"] == "profile_user"

    second = client.get(f"/api/users/{user_id}/orders?where=G&page=2&size=1", headers={"Authorization": f"Bearer {token}"}).get_json()
    assert second["data"]["orders"][0]["order_no"] != first["data"]["orders"][0]["order_no"]


def test_web_orders_rejects_unknown_where(web_account):
    client, token, user_id = web_account
    response = client.get(f"/api/users/{user_id}/orders?where=X", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 400


def test_web_passengers_endpoint_requires_online(web_account, mock_server):
    client, token, user_id = web_account
    ok = client.get(f"/api/users/{user_id}/passengers", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    assert len(ok.get_json()["passengers"]) == 2

    # 服务端认不出当前用户（登录态在 12306 侧失效）→ 官方回登录页 HTML，
    # 必须报成「会话失效」而不是「解析失败」或「0 个乘车人」
    mock_server.state.current_login_user = ""
    bad = client.get(f"/api/users/{user_id}/passengers?refresh=1", headers={"Authorization": f"Bearer {token}"})
    assert bad.status_code == 400
    assert "网页" in bad.get_json()["message"]


def test_web_profile_404_for_unknown_user(web_account):
    client, token, _ = web_account
    response = client.get("/api/users/99999/profile", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404


def test_web_profile_requires_auth(web_account):
    client, _token, user_id = web_account
    assert client.get(f"/api/users/{user_id}/profile").status_code == 401
    assert client.get(f"/api/users/{user_id}/orders").status_code == 401
