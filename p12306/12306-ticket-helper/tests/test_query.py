"""余票查询引擎测试（QY-T2 ~ QY-T7）。"""

from __future__ import annotations

import pytest

from ticket_helper.config.settings import TaskConfig
from ticket_helper.errors import InterError, TrainDateUnavailableError
from ticket_helper.inter.api import TicketApi
from ticket_helper.inter.base import HttpClient
from ticket_helper.query.engine import QueryEngine, TrainFilter
from mock_server.server import build_ticket_row


def _ticket(**overrides):
    base = dict(
        train_code="G6102",
        secret_seed="s1",
        from_code="GZQ",
        to_code="CWQ",
        start_time="08:12",
        arrive_time="10:47",
        duration="02:35",
        train_date="2026-10-01",
        seats={"二等座": "5", "一等座": "无", "无座": "有"},
    )
    base.update(overrides)
    from ticket_helper.inter.parser import parse_left_ticket_result

    return parse_left_ticket_result(build_ticket_row(**base))


# --------------------------------------------------------------------------- #
# QY-T4 / QY-T5 / 时间区间：纯筛选逻辑
# --------------------------------------------------------------------------- #
def _filter_task(**kwargs) -> TaskConfig:
    base = dict(
        name="t",
        left_dates=["2026-10-01"],
        from_stations=["广州"],
        to_stations=["长沙"],
        seat_types=["二等座"],
    )
    base.update(kwargs)
    return TaskConfig(**base)


def test_whitelist_only_keeps_listed_trains():
    filt = TrainFilter(_filter_task(trains_whitelist=["G6102"]))
    assert filt.accept_train(_ticket(train_code="G6102")) is True
    assert filt.accept_train(_ticket(train_code="G6140")) is False


def test_blacklist_excludes_trains():
    filt = TrainFilter(_filter_task(trains_blacklist=["G6140"]))
    assert filt.accept_train(_ticket(train_code="G6102")) is True
    assert filt.accept_train(_ticket(train_code="G6140")) is False


def test_train_type_filter():
    filt = TrainFilter(_filter_task(train_types=["高铁"]))
    assert filt.accept_train(_ticket(train_code="G1")) is True
    assert filt.accept_train(_ticket(train_code="D1")) is False
    assert filt.train_type_of("K512") == "快速"
    assert filt.train_type_of("Z9") == "直达"
    assert filt.train_type_of("X1") == "其他"


def test_depart_time_range_filter():
    filt = TrainFilter(_filter_task(depart_time_range=["07:00", "09:00"]))
    assert filt.accept_train(_ticket(start_time="08:12")) is True
    assert filt.accept_train(_ticket(start_time="14:05")) is False


def test_depart_time_range_cross_midnight():
    filt = TrainFilter(_filter_task(depart_time_range=["22:00", "06:00"]))
    assert filt.accept_train(_ticket(start_time="23:30")) is True
    assert filt.accept_train(_ticket(start_time="03:00")) is True
    assert filt.accept_train(_ticket(start_time="12:00")) is False


def test_time_range_invalid_is_ignored():
    filt = TrainFilter(_filter_task(depart_time_range=["不是时间", "09:00"]))
    assert filt.depart_range is None  # 解析失败 → 视为不限制，而不是拒绝全部车次


# --------------------------------------------------------------------------- #
# QY-T5 席别优先级 → 候选顺序
# --------------------------------------------------------------------------- #
def test_candidates_respect_seat_priority():
    task = _filter_task(seat_types=["一等座", "无座", "二等座"])
    engine = QueryEngine(api=None, task=task)  # type: ignore[arg-type]
    tickets = [_ticket(seats={"二等座": "5", "一等座": "3", "无座": "有"})]
    candidates = engine.build_candidates(tickets)
    assert [c.seat.seat_type for c in candidates] == ["一等座", "无座", "二等座"]


def test_candidates_respect_train_priority():
    task = _filter_task(trains_whitelist=["G6140", "G6102"], seat_types=["二等座"])
    engine = QueryEngine(api=None, task=task)  # type: ignore[arg-type]
    tickets = [_ticket(train_code="G6102"), _ticket(train_code="G6140")]
    candidates = engine.build_candidates(tickets)
    assert [c.ticket.train_code for c in candidates] == ["G6140", "G6102"]


def test_candidates_skip_unbuyable_when_looking_for_available():
    task = _filter_task(seat_types=["二等座"])
    engine = QueryEngine(api=None, task=task)  # type: ignore[arg-type]
    ticket = _ticket()
    ticket.can_web_buy = "IS_TIME_NOT_BUY"
    assert engine.build_candidates([ticket]) == []
    # 但候补场景仍能看到「仅候补」的席别
    standby_ticket = _ticket(seats={"二等座": "候补"})
    assert engine.build_candidates([standby_ticket], seat_scope="standby")


# --------------------------------------------------------------------------- #
# QY-T2 / QY-T3 多日期、多起止站（走 mock）
# --------------------------------------------------------------------------- #
def test_query_multi_date_and_station(app_context, mock_server):
    api = TicketApi(
        HttpClient(base_url=mock_server.base_url, retries=0, proxy_provider=None),
        app_context.station_map,
    )
    task = TaskConfig(
        name="multi",
        left_dates=["2026-10-01", "2026-10-02"],
        from_stations=["广州", "广州南"],
        to_stations=["长沙南"],
        seat_types=["二等座"],
    )
    engine = QueryEngine(api, task, db=app_context.db)
    result = engine.query_once()

    assert result.combos == 4  # 2 日期 × 2 出发站 × 1 到达站
    assert result.errors == []
    assert result.tickets
    assert result.latency_ms >= 0
    # 每个组合都应返回车次
    dates = {t.train_date for t in result.tickets}
    assert dates.issubset({"2026-10-01", "2026-10-02"})
    # 查询记录已落库（QY-07）
    assert app_context.db.query_records(limit=500)


def test_query_reports_unknown_station(app_context, mock_server):
    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)
    task = TaskConfig(
        name="bad", left_dates=["2026-10-01"],
        from_stations=["火星站"], to_stations=["长沙南"], seat_types=["二等座"],
    )
    result = QueryEngine(api, task).query_once()
    assert result.tickets == []
    assert result.errors and "未知站点" in result.errors[0]


# --------------------------------------------------------------------------- #
# QY-T7 查询异常处理
# --------------------------------------------------------------------------- #
def test_query_handles_http_500(app_context, mock_server):
    mock_server.set_scenario("error500")
    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)
    task = _filter_task()
    engine = QueryEngine(api, task)
    result = engine.query_once()
    assert result.tickets == []
    assert result.errors  # 被捕获并记录，而不是抛出去把任务打挂
    assert engine.error_count >= 1


def test_query_handles_sold_out(app_context, mock_server):
    mock_server.set_scenario("sold_out")
    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)
    result = QueryEngine(api, _filter_task()).query_once()
    assert result.errors == []
    assert result.tickets
    assert result.has_ticket is False


def test_api_query_login_required_detection(app_context, mock_server):
    """会话失效必须被识别为 LoginError 语义（让上层触发重登），而不是静默返回空列表。"""
    from ticket_helper.errors import LoginError, ResponseParseError
    from ticket_helper.inter.parser import parse_left_ticket_payload

    # 响应体带登录提示 → 解析层识别为查询失败
    with pytest.raises(ResponseParseError):
        parse_left_ticket_payload({"status": False, "messages": ["请登录"]})

    # LoginError 属于 InterError 家族，可被统一错误处理捕获
    assert issubclass(LoginError, InterError)

    # 真实路径：checkUser 返回未登录时，User 能感知到会话失效
    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)
    mock_server.set_scenario("risk")
    assert api.check_user() is False


# --------------------------------------------------------------------------- #
# 会话预热：真机上必须先「逛一次页面」才肯服务 query 接口
# --------------------------------------------------------------------------- #
def test_query_warms_up_session_before_first_query(app_context, mock_server):
    """首次查询前要先加载一次余票页。

    真机实测（同一时刻并排对照）：从未加载过页面的会话直接调 query 会被 WAF 拒并回
    HTML 错误页；先真实加载一次页面再调就正常。浏览器天然满足这个条件
    （用户总是先打开页面），脚本必须自己补上 —— 否则第一次查询必然失败，
    而症状只是一个 HTML 错误页，很难联想到"没逛过页面"。

    mock 现在要求热身（未热身就回 HTML），所以这条用例挂了就说明预热被去掉了。
    """
    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)

    tickets = api.query_left_ticket("2026-10-01", "GZQ", "CWQ")
    assert tickets, "预热之后应当能正常拿到余票"

    paths = [r["path"] for r in mock_server.state.requests]
    assert "/otn/leftTicket/init" in paths, "应当先加载一次余票页"
    assert paths.index("/otn/leftTicket/init") < next(
        i for i, p in enumerate(paths) if "leftTicket/query" in p
    ), "预热必须排在查询之前"


def test_warm_up_happens_only_once_per_session(app_context, mock_server):
    """预热是会话级的：多次查询不应每次都重新加载页面（那是白打接口）。"""
    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)

    for _ in range(3):
        assert api.query_left_ticket("2026-10-01", "GZQ", "CWQ")

    warm_calls = [r for r in mock_server.state.requests if r["path"] == "/otn/leftTicket/init"]
    assert len(warm_calls) == 1, f"预热只应做一次，实际 {len(warm_calls)} 次"


def test_warm_up_can_be_forced(app_context, mock_server):
    """``force`` 用于「被拒后重新热身」——必须能重新发一次。"""
    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)

    assert api.ensure_session_warm() is True      # 首次真的发了
    assert api.ensure_session_warm() is False     # 已热身 → 不发（返回 False 表示没发请求）
    assert api.ensure_session_warm(force=True) is True  # 强制重发


# --------------------------------------------------------------------------- #
# 查询被拒 → 重新热身重试一次；仍被拒则升级为风控信号，并停止轮换
# --------------------------------------------------------------------------- #
def test_query_rejection_raises_risk_control_and_bounds_requests(app_context, mock_server):
    """一直被拒时：重试一次（含重新热身）就放弃，**不能**把候选路径全打一遍。

    真机实测：被拒是会话/风控层面的问题，与具体路径无关。继续把剩下 3 条候选路径
    打完只会让突发更大，把短暂限流变成持续限流。正确做法是抛 ``RiskControlError``，
    让上层的限速器降频（grab 引擎会 report_risk + 退避 + 通知）。
    """
    from ticket_helper.errors import RiskControlError

    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)
    mock_server.set_scenario("query_rate_limited")   # 热身后仍然被拒

    with pytest.raises(RiskControlError) as exc:
        api.query_left_ticket("2026-10-01", "GZQ", "CWQ")
    assert "拒绝" in exc.value.message and "限流" in exc.value.message, exc.value.message

    query_calls = [r for r in mock_server.state.requests if "leftTicket/query" in r["path"]]
    # 2 次尝试各打 1 条路径；4 条候选路径的全量轮换会是 4+ 次
    assert len(query_calls) == 2, f"被拒后应尽快放弃，实际打了 {len(query_calls)} 个查询请求"

    warm_calls = [r for r in mock_server.state.requests if r["path"] == "/otn/leftTicket/init"]
    assert len(warm_calls) == 2, "重试前应当重新热身一次"


def test_query_rejection_stops_the_whole_task_early(app_context, mock_server):
    """多组合任务里一旦被拒，不应继续尝试其它日期/站点（否则突发被放大 N 倍）。"""
    from ticket_helper.errors import RiskControlError

    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)
    task = TaskConfig(
        name="多组合",
        left_dates=["2026-10-01", "2026-10-02"],
        from_stations=["广州", "广州南"],
        to_stations=["重庆"],
    )
    engine = QueryEngine(api, task)
    mock_server.set_scenario("query_rate_limited")

    with pytest.raises(RiskControlError):
        engine.query_once()

    query_calls = [r for r in mock_server.state.requests if "leftTicket/query" in r["path"]]
    assert len(query_calls) == 2, f"被拒后应立刻中止，实际打了 {len(query_calls)} 个查询请求"


# --------------------------------------------------------------------------- #
# 日期不可查 ≠ 被风控（真机实测：两者回的是同一张 HTML 错误页）
#
# 这一组防的是一个**真发生过**的故障：任务日期填成了上一年，
# 官方回 HTML，代码一律当成风控 → 界面报「🚨 风控告警」，
# 使用者去降频、换账号、干等，全部无效；每次重试还都在给 12306 添一次
# 注定被拒的请求。判据都不是「异常类型对不对」，而是**有没有白打请求**。
# --------------------------------------------------------------------------- #
def test_past_date_never_reaches_12306(app_context, mock_server):
    """已过去的日期**一个请求都不该发**：它永远不会变可查。"""
    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)
    task = TaskConfig(
        name="去年", left_dates=["2025-10-03"],
        from_stations=["广州"], to_stations=["长沙南"], seat_types=["二等座"],
    )
    result = QueryEngine(api, task).query_once()

    assert result.tickets == []
    assert result.errors and "已经过去" in result.errors[0], result.errors
    sent = [r for r in mock_server.state.requests if "leftTicket" in r["path"]]
    assert sent == [], f"过去的日期不该发任何请求，实际发了 {sent}"


def test_out_of_presale_date_raises_date_error_not_risk(app_context, mock_server):
    """超出预售期：必须报「还没开售」，**不能**报风控 —— 且不要为它多热身一次。

    真机上这两种情况的响应是同一张 HTML 错误页，所以这条断言只能靠
    「异常类型 + 请求次数」来钉住：报错类型错了，使用者就会去做无用功。
    """
    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)
    # mock 按真机行为回 HTML 错误页（见 mock_server 的日期窗口复现）
    with pytest.raises(TrainDateUnavailableError) as exc:
        api.query_left_ticket("2026-11-01", "GZQ", "CWQ")
    assert "还没开售" in exc.value.message, exc.value.message
    assert "风控" not in exc.value.message

    query_calls = [r for r in mock_server.state.requests if "leftTicket/query" in r["path"]]
    assert len(query_calls) == 1, f"日期不可查时不该重试，实际打了 {len(query_calls)} 次"
    warm_calls = [r for r in mock_server.state.requests if r["path"] == "/otn/leftTicket/init"]
    assert len(warm_calls) == 1, "日期不可查时不该再热身重试一次"


def test_within_window_rejection_is_still_risk_control(app_context, mock_server):
    """反面：可查范围内被拒**仍然**是风控。

    防的是这次改动把日期分支放得太宽，把真风控也说成「日期没开售」
    —— 那会让使用者去改一个本来正确的日期，同样修不好问题。
    """
    from ticket_helper.errors import RiskControlError

    mock_server.set_scenario("query_rate_limited")
    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)
    with pytest.raises(RiskControlError):
        api.query_left_ticket("2026-10-01", "GZQ", "CWQ")


def test_mixed_dates_skip_only_the_past_one(app_context, mock_server):
    """多日期任务：只跳过过去的那一天，其余照查 —— 不能因为一天坏了就整个不查。"""
    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)
    task = TaskConfig(
        name="混合", left_dates=["2025-10-03", "2026-10-01"],
        from_stations=["广州"], to_stations=["长沙南"], seat_types=["二等座"],
    )
    result = QueryEngine(api, task).query_once()

    assert result.tickets, "可查的那一天应当照常拿到车次"
    queried = {
        r["payload"]["date"]
        for r in mock_server.state.requests
        if "leftTicket/query" in r["path"] and isinstance(r.get("payload"), dict)
    }
    assert queried == {"2026-10-01"}, f"只该查可查的那天，实际查了 {queried}"
    assert any("已经过去" in e for e in result.errors)
