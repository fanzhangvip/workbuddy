"""车次候选拉取测试（任务配置：白名单 / 黑名单不再手填）。

覆盖两件事：
1. **服务层**（``TrainOptionsService``）：组合展开、跨日期合并、错误隔离、
   组合数封顶、风控即时中止、短时缓存；
2. **接口层**（``GET /api/trains/options``）：参数校验与返回结构。

全部跑在本地 mock-12306 上，不碰真实 12306。
"""

from __future__ import annotations

import pytest

from ticket_helper.errors import ValidationError
from ticket_helper.inter.api import TicketApi
from ticket_helper.inter.base import HttpClient
from ticket_helper.query.options import TrainOptionsService

DATES = ["2026-10-01", "2026-10-02"]


def _api(app_context, mock_server) -> TicketApi:
    return TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)


def _query_calls(mock_server) -> list:
    return [r for r in mock_server.state.requests if "leftTicket/query" in r["path"]]


# --------------------------------------------------------------------------- #
# 服务层
# --------------------------------------------------------------------------- #
def test_fetch_returns_trains_sorted_by_departure(app_context, mock_server):
    """mock 的 has_ticket 场景有 K512 / G6102 / G6140，应按发车时间升序返回。"""
    service = TrainOptionsService(app_context.station_map)
    payload = service.fetch(_api(app_context, mock_server), dates=DATES, from_stations=["广州"], to_stations=["长沙"])

    assert payload["errors"] == []
    assert payload["combos"] == 2
    assert payload["queried"] == 2
    codes = [t["train_code"] for t in payload["trains"]]
    assert codes == ["K512", "G6102", "G6140"], codes
    assert [t["start_time"] for t in payload["trains"]] == ["06:40", "08:12", "14:05"]
    assert payload["trains"][0]["train_type"] == "快速"
    assert payload["trains"][1]["train_type"] == "高铁"


def test_fetch_merges_same_train_across_dates(app_context, mock_server):
    """同一车次出现在多个日期时合并成一条，日期全部保留。"""
    service = TrainOptionsService(app_context.station_map)
    payload = service.fetch(_api(app_context, mock_server), dates=DATES, from_stations=["广州"], to_stations=["长沙"])

    g6102 = next(t for t in payload["trains"] if t["train_code"] == "G6102")
    assert g6102["dates"] == DATES
    assert g6102["buyable"] is True
    assert g6102["available_seats"], "应当带上「有票的席别」列表"


def test_fetch_keeps_sold_out_seat_out_of_available(app_context, mock_server):
    """G6140 的二等座在 mock 里是「无」，不能混进「有票席别」。"""
    service = TrainOptionsService(app_context.station_map)
    payload = service.fetch(_api(app_context, mock_server), dates=["2026-10-01"], from_stations=["广州"], to_stations=["长沙"])

    g6140 = next(t for t in payload["trains"] if t["train_code"] == "G6140")
    assert "二等座" not in g6140["available_seats"]
    assert "二等座" in g6140["seats"], "无票席别仍要展示（余票文本是「无」）"


def test_fetch_caps_combo_count(app_context, mock_server):
    """日期一多就必须封顶：一次把十几个组合打出去等于自找风控。"""
    service = TrainOptionsService(app_context.station_map, max_combos=2, cache_ttl=0)
    payload = service.fetch(
        _api(app_context, mock_server),
        dates=["2026-10-01", "2026-10-02", "2026-10-03"],
        from_stations=["广州"],
        to_stations=["长沙"],
    )

    assert payload["combos"] == 2
    assert payload["truncated"] is True
    assert len(_query_calls(mock_server)) == 2, "超出的组合不能再打接口"


def test_fetch_isolates_bad_station(app_context, mock_server):
    """一个站点名写错只影响它自己，其余组合照常出结果。"""
    service = TrainOptionsService(app_context.station_map)
    payload = service.fetch(
        _api(app_context, mock_server),
        dates=["2026-10-01"],
        from_stations=["火星站", "广州"],
        to_stations=["长沙"],
    )

    assert payload["errors"] and "未知站点" in payload["errors"][0]
    assert payload["queried"] == 1
    assert payload["trains"], "另一个组合的车次必须照常返回"


def test_fetch_stops_immediately_when_risk_controlled(app_context, mock_server):
    """被风控时立刻停手：剩下的日期一个都不发。"""
    service = TrainOptionsService(app_context.station_map, cache_ttl=0)
    mock_server.set_scenario("query_rate_limited")
    payload = service.fetch(
        _api(app_context, mock_server),
        dates=["2026-10-01", "2026-10-02", "2026-10-03"],
        from_stations=["广州"],
        to_stations=["长沙"],
    )

    assert payload["risk_limited"] is True
    assert payload["queried"] == 0
    assert payload["trains"] == []
    # 接口层被拒时会重试一次（含重新热身），因此是 2 次而不是 3×2 次
    assert len(_query_calls(mock_server)) == 2, "风控后必须停止后续组合"


def test_fetch_caches_and_can_be_refreshed(app_context, mock_server):
    service = TrainOptionsService(app_context.station_map)
    api = _api(app_context, mock_server)
    first = service.fetch(api, dates=["2026-10-01"], from_stations=["广州"], to_stations=["长沙"])
    assert first["cached"] is False
    assert len(_query_calls(mock_server)) == 1

    second = service.fetch(api, dates=["2026-10-01"], from_stations=["广州"], to_stations=["长沙"])
    assert second["cached"] is True
    assert len(_query_calls(mock_server)) == 1, "命中缓存不应再打接口"

    third = service.fetch(api, dates=["2026-10-01"], from_stations=["广州"], to_stations=["长沙"], refresh=True)
    assert third["cached"] is False
    assert len(_query_calls(mock_server)) == 2


def test_fetch_requires_all_three_inputs(app_context, mock_server):
    service = TrainOptionsService(app_context.station_map)
    api = _api(app_context, mock_server)
    with pytest.raises(ValidationError):
        service.fetch(api, dates=[], from_stations=["广州"], to_stations=["长沙"])
    with pytest.raises(ValidationError):
        service.fetch(api, dates=["2026-10-01"], from_stations=["广州"], to_stations=[])


# --------------------------------------------------------------------------- #
# 接口层
# --------------------------------------------------------------------------- #
class _StubUser:
    """替身账号：只暴露接口层用到的属性（免去登录流程，测试只验路由行为）。"""

    def __init__(self, api, username: str = "u1") -> None:
        self.api = api
        self.username = username
        self.is_login = True


@pytest.fixture()
def web_with_account(app_context, mock_server, monkeypatch):
    """把「取会话」换成替身，聚焦验证接口的参数与返回结构。"""
    from ticket_helper.verify.service import CallbackSolver
    from ticket_helper.web.app import create_app

    app_context.verify_service.set_solver(CallbackSolver(lambda i, h, k: "112,45", name="fixed"))
    stub_api = _api(app_context, mock_server)
    monkeypatch.setattr(
        app_context, "pull_session", lambda username="", force_anonymous=False: (stub_api, "u1")
    )

    app = create_app(app_context, start_background=False)
    app.config["TESTING"] = True
    with app.test_client() as client:
        token = client.post("/api/auth/login", json={"user": "admin", "pass": "test-pass"}).get_json()["token"]
        yield client, {"Authorization": f"Bearer {token}"}


def test_api_train_options_requires_auth(web_with_account):
    client, _ = web_with_account
    response = client.get("/api/trains/options?dates=2026-10-01&from=广州&to=长沙")
    assert response.status_code == 401


def test_api_train_options_rejects_missing_params(web_with_account):
    client, headers = web_with_account
    response = client.get("/api/trains/options?dates=2026-10-01", headers=headers)
    assert response.status_code == 400
    assert response.get_json()["code"] == "validation_error"


def test_api_train_options_returns_train_list(web_with_account):
    client, headers = web_with_account
    response = client.get(
        "/api/trains/options?dates=2026-10-01,2026-10-02&from=广州&to=长沙&refresh=1",
        headers=headers,
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert [t["train_code"] for t in body["trains"]] == ["K512", "G6102", "G6140"]
    assert body["account"] == "u1"
    assert body["anonymous"] is False
    assert body["truncated"] is False
    assert body["errors"] == []


def test_api_train_options_works_without_login(app_context, mock_server, monkeypatch):
    """**没有在线账号也能拉**：余票查询不要求登录态，退回匿名会话即可。

    这条是本次修复的核心 —— 之前没有在线账号就直接 400，而真机上密码登录走不通，
    于是「配置任务时拉个车次」变成了一件根本做不到的事。
    """
    from ticket_helper.web.app import create_app

    stub_api = _api(app_context, mock_server)
    monkeypatch.setattr(
        app_context, "pull_session", lambda username="", force_anonymous=False: (stub_api, "")
    )
    app = create_app(app_context, start_background=False)
    app.config["TESTING"] = True
    with app.test_client() as client:
        token = client.post("/api/auth/login", json={"user": "admin", "pass": "test-pass"}).get_json()["token"]
        response = client.get(
            "/api/trains/options?dates=2026-10-01&from=广州&to=长沙&refresh=1",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert body["anonymous"] is True and body["account"] == ""
    assert [t["train_code"] for t in body["trains"]] == ["K512", "G6102", "G6140"]


def test_api_train_options_falls_back_to_anonymous_when_account_session_is_dead(
    app_context, mock_server, monkeypatch
):
    """账号会话失效时自动改用匿名会话 —— 不该因为账号掉线就整个用不了。"""
    from ticket_helper.errors import LoginError
    from ticket_helper.web.app import create_app

    class _DeadSessionApi:
        """账号会话：任何查询都说明会话已失效。"""

        def __init__(self, station_map) -> None:
            self.stations = station_map

        def query_left_ticket(self, *_args, **_kwargs):
            raise LoginError("查询余票时发现会话已失效")

    live_api = _api(app_context, mock_server)
    dead_api = _DeadSessionApi(app_context.station_map)
    monkeypatch.setattr(
        app_context,
        "pull_session",
        lambda username="", force_anonymous=False: (live_api, "") if force_anonymous else (dead_api, "u1"),
    )

    app = create_app(app_context, start_background=False)
    app.config["TESTING"] = True
    with app.test_client() as client:
        token = client.post("/api/auth/login", json={"user": "admin", "pass": "test-pass"}).get_json()["token"]
        response = client.get(
            "/api/trains/options?dates=2026-10-01&from=广州&to=长沙&refresh=1",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert [t["train_code"] for t in body["trains"]] == ["K512", "G6102", "G6140"]
    assert body["anonymous"] is True
    assert body["fallback_from"] == "u1", "要说清是哪个账号的会话不可用"
