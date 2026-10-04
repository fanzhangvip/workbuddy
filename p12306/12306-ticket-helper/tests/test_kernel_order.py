"""下单六步请求规格域（`order_submit`）的契约测试。

每条都要满足「撤掉实现就变红」（`scripts/contract_mutation_check.py -k 下单`）。

判据分两层：

- **规格层**：六步的路径 / 字段名 / 固定取值，对着协议层与上游生成物逐项比；
- **线缆层**：真正发出去的那份表单（mock 记下来的）——
  「切了没切」只在这一层看得见（六步的路径两边完全相同）。
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from ticket_helper.errors import InterError
from ticket_helper.inter import api as api_mod
from ticket_helper.inter import urls as inter_urls
from ticket_helper.inter.api import TicketApi
from ticket_helper.inter.base import HttpClient
from ticket_helper.inter.parser import parse_left_ticket_result
from ticket_helper.kernel import registry
from ticket_helper.kernel.domains.order_submit import (
    STEP_CHECK_ORDER_INFO,
    STEP_CONFIRM_SINGLE_FOR_QUEUE,
    STEP_GET_QUEUE_COUNT,
    STEP_INIT_DC,
    STEP_QUERY_WAIT_TIME,
    STEP_SUBMIT,
    STEPS,
    build_form,
)
from mock_server.server import build_ticket_row

DOMAIN = "order_submit"
SELF = "self"
UP = "pjialin"

MOCK_PASSENGERS = [
    {
        "passenger_name": "张三",
        "passenger_id_type_code": "1",
        "passenger_id_no": "110101199001011234",
        "passenger_type": "1",
        "mobile_no": "13800000000",
        # 真机 getPassengerDTOs 会下发它；缺了它 checkOrderInfo 必被拒（回流 #51）
        "allEncStr": "mock-all-enc-zhangsan",
    }
]

#: 造一份「一次真实下单」的取值袋
CTX: dict[str, Any] = {
    "secretStr": "<secretStr>",
    "train_date": "2026-10-05",
    "back_train_date": "2026-09-30",
    "today": "2026-09-30",
    "tour_flag": "dc",
    "tourFlag": "dc",
    "query_from_station_name": "北京南",
    "query_to_station_name": "上海虹桥",
    "passengerTicketStr": "<ticketStr>",
    "oldPassengerStr": "<oldStr>",
    "randCode": "",
    "REPEAT_SUBMIT_TOKEN": "<token>",
    "train_no": "24000G610200",
    "stationTrainCode": "G6102",
    "seatType": "O",
    "fromStationTelecode": "VNP",
    "toStationTelecode": "AOH",
    "leftTicket": "<leftTicketStr>",
    "purpose_codes": "00",
    "train_location": "P2",
    "key_check_isChange": "<key>",
    "leftTicketStr": "<leftTicketStr>",
    "choose_seats": "",
    "random_ms": 1759199999000,
    "random_fraction": "8609371124501",
}


def _forms_with(backend: str):
    previous = os.environ.get(registry.ENV_VAR)
    os.environ[registry.ENV_VAR] = f"{DOMAIN}={backend}"
    registry.reset_cache()
    try:
        return registry.get(DOMAIN).forms()
    finally:
        if previous is None:
            os.environ.pop(registry.ENV_VAR, None)
        else:
            os.environ[registry.ENV_VAR] = previous
        registry.reset_cache()


def _api_for(handle: Any, spec: Any) -> TicketApi:
    """直连 mock 的接口对象，并注入给定规格。"""
    return TicketApi(HttpClient(base_url=handle.base_url, retries=0), None, order_spec=spec)  # type: ignore[arg-type]


def _recorded(handle: Any, path: str) -> dict[str, Any]:
    """mock 记下的**完整表单**（这一步的判据）。"""
    for item in reversed(handle.state.requests):
        if item["path"] == path:
            return dict(item["payload"] or {})
    return {}


def test_init_dc_matches_the_real_page_and_tolerates_a_page_without_it(
    mock_server, ticket
) -> None:
    """★ 防的是：mock 跟着**错误结论**走，把「页面有」当成「页面没有」。

    2026-09-30 曾据真机断言「页面里没有 ticketInfoForPassengerForm」——
    2026-10-03 带页面取证重测推翻：**页面有**，而且是单引号字面量，
    当时的真相是**解析失败被误判成「页面没有」**（回流 #49 二次复核）。
    mock 自 2026-09-30 起默认不发这个变量，跟着错了一轮（回流 #64）。

    判据两条：默认（= 真机形状，单引号字面量）要解析出关键值；
    页面没下发（场景 ``no_initdc_form``）时流程照旧、不能抛。
    """
    api = _api_for(mock_server, _forms_with(SELF))
    dc = api.init_dc()
    assert dc["repeat_submit_token"], "token 与那个变量无关，必须照常拿到"
    assert dc["ticket_info"].get("key_check_isChange"), (
        f"默认场景 = 真机形状，必须解析出关键值：{dc['ticket_info']}"
    )

    # 页面没下发的那条分支仍然要能走（防御，不许静默崩溃）
    mock_server.state.scenario = "no_initdc_form"
    mock_server.state.requests.clear()
    legacy = _api_for(mock_server, _forms_with(SELF)).init_dc()
    assert legacy["ticket_info"] == {}, legacy["ticket_info"]
    mock_server.state.scenario = None


def test_submitted_secret_str_is_the_decoded_one(mock_server, app_context) -> None:
    """★ 防的是：把**响应里那份 URL 编码**的 secretStr 原样发回去。

    真机实测（2026-09-30）：余票响应里 **84/84 行**的 secretStr 都是 URL 编码的
    （`tuq87mx%2FclTrefw%2BoWU...`，410 字符；解开是 368 字符）。
    原样发出去时 `requests` 会把 `%` 再编码成 `%25`，官方解一次拿到的仍是字面的
    `%2F` —— 等于给了一串不存在的 secretStr，官方只回一句
    **「提交失败，请重试...」**：既不是 4xx，也指不出是哪个字段
    （真机第一次跑 `diagnose_order.py` 就栽在这里）。
    上游 `pjialin/order/order.py` 里那行 `urllib.parse.unquote(...)` 就是干这个的。

    判据落在**线缆上**：只看 `ticket.secret_str` 只能证明「解析层解了码」，
    证明不了「发出去的是解码后的值」——中间完全可能被再编码一次。
    """
    row = build_ticket_row(
        train_code="G6102",
        secret_seed="a+b/c=d",
        from_code="GZQ",
        to_code="CWQ",
        start_time="08:12",
        arrive_time="10:47",
        duration="02:35",
        train_date="2026-10-01",
        seats={"二等座": "5"},
    )
    assert row.split("|")[0] == "a%2Bb%2Fc%3Dd", "mock 必须先复现真机的编码形状"

    decoded = parse_left_ticket_result(row, station_map=app_context.station_map)
    api = _api_for(mock_server, _forms_with(SELF))
    api.submit_order_request(decoded, from_name="广州南", to_name="长沙南")

    sent = _recorded(mock_server, inter_urls.SUBMIT_ORDER_REQUEST)["secretStr"]
    assert sent == "a+b/c=d", f"发出去的必须是解码后的串，实际 {sent!r}"


@pytest.fixture()
def ticket(app_context):
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


@pytest.fixture()
def frozen_api_clock(monkeypatch):
    """把接口层看到的时钟冻结（顺带让 `time.sleep` 变成空操作，测试不用真等 1 秒）。"""

    class _Clock:
        def time(self) -> float:
            return 1759199999.0

        def strftime(self, fmt: str) -> str:
            return "2026-09-30" if fmt == "%Y-%m-%d" else ""

        def sleep(self, _seconds: float) -> None:  # pragma: no cover - 只为不真睡
            return None

    monkeypatch.setattr(api_mod, "time", _Clock())
    monkeypatch.setattr(api_mod.random, "random", lambda: 0.8609371124501)


def _run_chain(api: TicketApi, ticket: Any) -> None:
    """按顺序跑完六步（mock 支持整条链）。"""
    api.submit_order_request(ticket, from_name="广州南", to_name="上海虹桥")
    dc = api.init_dc()
    token = dc.get("repeat_submit_token", "")
    api.check_order_info(MOCK_PASSENGERS, "O", token=token)
    api.get_queue_count(ticket, "O", token=token)
    api.confirm_single_for_queue(MOCK_PASSENGERS, "O", ticket, token=token)
    api.query_order_wait_time(token=token, max_polls=1)


# --------------------------------------------------------------------------- #
# 1. 规格层：本项目后端的产出 == 接口层默认规格（不许各写一份）
# --------------------------------------------------------------------------- #
def test_self_spec_equals_the_builtin_default() -> None:
    """防的是：`kernel/self` 与接口层的默认规格**分叉**。

    接口层不许 import 具体后端（会成环），所以「同一个规格」有两个组装点；
    这条把两者钉成必须逐项相等 —— 任何一边改了而另一边没跟，立刻变红。
    """
    from ticket_helper.kernel.self.order_submit import SelfOrderSubmitter

    assert SelfOrderSubmitter().forms() == TicketApi._builtin_order_spec()  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 2. ★ 行为不变：本项目规格组装出来的表单 == 改造前写死的字面量
# --------------------------------------------------------------------------- #
#: 改造前 `inter/api.py` 里**逐步写死**的那六份表单（逐字抄自改造前的代码）。
LEGACY_FORMS: dict[str, dict[str, Any]] = {
    STEP_SUBMIT: {
        "secretStr": "<secretStr>", "train_date": "2026-10-05", "back_train_date": "2026-09-30",
        "tour_flag": "dc", "purpose_codes": "ADULT", "query_from_station_name": "北京南",
        "query_to_station_name": "上海虹桥", "undefined": "",
    },
    STEP_INIT_DC: {"_json_att": ""},
    STEP_CHECK_ORDER_INFO: {
        "cancel_flag": "2", "bed_level_order_num": "000000000000000000000000000000",
        "passengerTicketStr": "<ticketStr>", "oldPassengerStr": "<oldStr>", "tour_flag": "dc",
        "randCode": "", "whatsSelect": "1", "_json_att": "", "REPEAT_SUBMIT_TOKEN": "<token>",
    },
    STEP_GET_QUEUE_COUNT: {
        "train_date": "2026-10-05", "train_no": "24000G610200", "stationTrainCode": "G6102",
        "seatType": "O", "fromStationTelecode": "VNP", "toStationTelecode": "AOH",
        "leftTicket": "<leftTicketStr>", "purpose_codes": "00", "train_location": "P2",
        "_json_att": "", "REPEAT_SUBMIT_TOKEN": "<token>",
    },
    STEP_CONFIRM_SINGLE_FOR_QUEUE: {
        "passengerTicketStr": "<ticketStr>", "oldPassengerStr": "<oldStr>", "randCode": "",
        "purpose_codes": "00", "key_check_isChange": "<key>", "leftTicketStr": "<leftTicketStr>",
        "train_location": "P2", "choose_seats": "", "seatDetailType": "000", "whatsSelect": "1",
        "roomType": "00", "dwAll": "N", "_json_att": "", "REPEAT_SUBMIT_TOKEN": "<token>",
    },
    STEP_QUERY_WAIT_TIME: {
        "random": 1759199999000, "tourFlag": "dc", "_json_att": "", "REPEAT_SUBMIT_TOKEN": "<token>",
    },
}


@pytest.mark.parametrize("step", STEPS)
def test_self_forms_reproduce_the_legacy_literals(step: str) -> None:
    """★ 防的是：改造把**真实发出去的表单**改掉了（静默行为变更）。

    改造前六份表单是逐条写在 `inter/api.py` 里的字面量；现在由规格组装。
    这条把「组装结果」与「改造前的字面量」钉成必须**逐字段相等、顺序也相同** ——
    字段少一个、值变一下、多一个，都立刻变红。

    这是「重构不许改行为」在**下单**这条链上的唯一硬证据：
    其余测试都只证明「重构后仍能跑通」，证明不了「发出去的东西没变」。
    """
    built = build_form(_forms_with(SELF).form(step), CTX)
    expected = LEGACY_FORMS[step]
    assert list(built) == list(expected), "字段顺序变了（diff 会看不见真差异）"
    assert built == expected


# --------------------------------------------------------------------------- #
# 3. 失败要**指名道姓**
# --------------------------------------------------------------------------- #
def test_bad_step_name_fails_fast() -> None:
    """步骤名写错不许静默给默认值：那会让「少发一步」变成一句「系统忙」。"""
    spec = _forms_with(SELF)
    with pytest.raises(InterError) as excinfo:
        spec.form("submt")
    assert "submt" in str(excinfo.value)
    assert STEP_SUBMIT in str(excinfo.value)


def test_missing_value_names_the_step_and_field() -> None:
    """缺值时错误必须说清「哪一步、哪个字段、想从哪取」——
    否则切了后端只看到官方那句『系统忙，请稍后重试』，无从下手。"""
    broken = dict(CTX)
    broken.pop("secretStr")
    with pytest.raises(InterError) as excinfo:
        build_form(_forms_with(SELF).form(STEP_SUBMIT), broken)
    message = str(excinfo.value)
    assert STEP_SUBMIT in message
    assert "secretStr" in message


# --------------------------------------------------------------------------- #
# 4. 滑块三件套：半套必须被拒
# --------------------------------------------------------------------------- #
def test_half_slide_group_is_rejected() -> None:
    """只给 ``sessionId`` 不给 ``sig`` → 必须报错。

    半套滑块参数发出去官方必然拒绝，而回的是那句万能的「系统忙」——
    与其让使用者去猜，不如在本地拦下并说清缺什么。
    """
    form = _forms_with(UP).form(STEP_CHECK_ORDER_INFO)
    half = dict(CTX, sessionId="<sid>")
    with pytest.raises(InterError) as excinfo:
        build_form(form, half)
    assert "sig" in str(excinfo.value)


def test_slide_group_appears_when_provided() -> None:
    """给了 ``sessionId`` + ``sig`` → 三件套整体出现，``scene`` 取上游写死的 ``nc_login``。"""
    form = _forms_with(UP).form(STEP_CHECK_ORDER_INFO)
    built = build_form(form, dict(CTX, sessionId="<sid>", sig="<sig>"))
    assert built["sessionId"] == "<sid>"
    assert built["sig"] == "<sig>"
    assert built["scene"] == "nc_login"
    # 不给就整体不出现（上游只在 is_slide 为真时才追加）
    plain = build_form(form, CTX)
    assert "sessionId" not in plain and "scene" not in plain


def test_self_backend_has_no_slide_fields_at_all() -> None:
    """本项目这套**没有**滑块三件套 —— 这是事实，不是遗漏（此前走人工兜底）。"""
    form = _forms_with(SELF).form(STEP_CHECK_ORDER_INFO)
    built = build_form(form, dict(CTX, sessionId="<sid>", sig="<sig>"))
    assert "sessionId" not in built


# --------------------------------------------------------------------------- #
# 5. 切换点
# --------------------------------------------------------------------------- #
def test_default_backend_is_self() -> None:
    """默认仍是 `self`：上游那套**六步都不带 Referer**，真机上很可能被 WAF 拒。

    换默认值 = 换行为，要真机证据（v3 §10-17）。这条防的是「悄悄切了默认」。
    """
    previous = os.environ.pop(registry.ENV_VAR, None)
    registry.reset_cache()
    try:
        assert registry.backend_name(DOMAIN) == SELF
    finally:
        if previous is not None:
            os.environ[registry.ENV_VAR] = previous
        registry.reset_cache()


def test_attaching_api_keeps_the_order_spec(app_context, mock_server) -> None:
    """防的是：`User.attach_api` 重建 `TicketApi` 时**漏传** ``order_spec``。

    这条路径只在「先有账号、后来才拿到站名表」时走到（延迟初始化）。
    漏传的表现是**这条路径上的账号悄悄退回默认后端**，而其它账号用的是新策略 ——
    和 `route_resolver`/`login_spec` 当初踩的是同一个坑（漏一个就静默降级）。
    """
    from ticket_helper.account.manager import User
    from ticket_helper.config.settings import UserConfig

    spec = _forms_with(UP)
    user = User(
        UserConfig(username="t_attach", password="p"),
        base_url=mock_server.base_url,
        order_spec=spec,
    )
    try:
        user.attach_api(app_context.station_map)
        assert user.api._order_spec is spec  # noqa: SLF001
    finally:
        user.close()


def test_bad_backend_name_fails_fast() -> None:
    previous = os.environ.get(registry.ENV_VAR)
    os.environ[registry.ENV_VAR] = f"{DOMAIN}=py12306"
    registry.reset_cache()
    try:
        with pytest.raises(Exception) as excinfo:
            registry.backend_name(DOMAIN)
        assert "py12306" in str(excinfo.value)
    finally:
        if previous is None:
            os.environ.pop(registry.ENV_VAR, None)
        else:
            os.environ[registry.ENV_VAR] = previous
        registry.reset_cache()


def test_the_switch_does_not_change_the_endpoint() -> None:
    """★ 结构事实：切这个域**换的只有表单与 Referer，端点不变**。

    六步的路径两套完全一致（对照的是 `inter/urls.py` 里的协议常量）。
    写下来是为了免得有人以为「切了后端 = 换了一条官方接口」。
    """
    ours = _forms_with(SELF)
    theirs = _forms_with(UP)
    for step in STEPS:
        assert ours.form(step).path == theirs.form(step).path == inter_urls.ORDER_STEP_FORMS[step]["path"]
        assert ours.form(step).method == theirs.form(step).method


# --------------------------------------------------------------------------- #
# 6. ★ 线缆层：切换真的换了发出去的请求
# --------------------------------------------------------------------------- #
def test_switch_actually_changes_the_wire_form(mock_server, ticket, frozen_api_clock) -> None:
    """★ 本域最关键的判据：**mock 记下的表单长什么样**。

    两套的路径完全相同、返回值也相同 —— 只看返回值分不出切没切。
    差异必须落在「真实发出去的表单」上：

    - 本项目多传 ``undefined``、``back_train_date`` 传**今天**；
    - 上游不传 ``undefined``、``back_train_date`` 传**出发日**。
    """
    mock_server.state.requests.clear()
    _run_chain(_api_for(mock_server, _forms_with(SELF)), ticket)
    mine = _recorded(mock_server, inter_urls.SUBMIT_ORDER_REQUEST)
    assert mine["undefined"] == ""
    assert mine["back_train_date"] == "2026-09-30", mine  # 冻结的「今天」
    assert mine["train_date"] == ticket.train_date

    mock_server.state.requests.clear()
    _run_chain(_api_for(mock_server, _forms_with(UP)), ticket)
    theirs = _recorded(mock_server, inter_urls.SUBMIT_ORDER_REQUEST)
    assert "undefined" not in theirs, theirs
    assert theirs["back_train_date"] == ticket.train_date, theirs  # 出发日


def test_upstream_backend_sends_no_referer_and_self_does(mock_server, ticket, monkeypatch) -> None:
    """★ Referer 差异必须在**真实请求**里成立（不只是规格里的一个字段）。

    上游六步都不带 Referer。本项目每道都带 —— 真机上「没逛过页面」会被 WAF 拒。
    这里挂探针在客户端的 `get/post` 上看**真的带没带**。
    """
    captured: list[dict[str, Any]] = []

    def _probe(api: TicketApi) -> None:
        for name in ("get", "post"):
            original = getattr(api.client, name)

            def wrapper(path, *args, _original=original, _name=name, **kwargs):
                captured.append({"method": _name, "path": path, "referer": kwargs.get("referer")})
                return _original(path, *args, **kwargs)

            monkeypatch.setattr(api.client, name, wrapper)

    mine = _api_for(mock_server, _forms_with(SELF))
    _probe(mine)
    _run_chain(mine, ticket)
    self_referers = {item["referer"] for item in captured}
    assert None not in self_referers, f"本项目每步都该带 Referer，实际 {captured}"
    assert all(r and r.endswith(("/otn/leftTicket/init", "/otn/confirmPassenger/initDc")) for r in self_referers)

    captured.clear()
    theirs = _api_for(mock_server, _forms_with(UP))
    _probe(theirs)
    _run_chain(theirs, ticket)
    assert len(captured) == 6, f"探针应当抓到六步请求，实际 {len(captured)} 条 —— 这条测试就变成假的了"
    assert all(item["referer"] is None for item in captured), (
        f"上游那套**不该**带 Referer（差异要留在能被看见的地方），实际 {captured}"
    )


def test_wait_time_random_comes_from_the_recorded_source(
    mock_server, ticket, frozen_api_clock
) -> None:
    """``random`` 这一个字段、两套**取值来源不同**，而且在真实查询串里能看到。

    （冻结时钟与 ``random.random`` 之后，两边的取值都是确定的 ——
    所以这条能区分「来源」而不只是「有没有值」。）
    """
    mock_server.state.requests.clear()
    _run_chain(_api_for(mock_server, _forms_with(SELF)), ticket)
    mine = _recorded(mock_server, inter_urls.QUERY_ORDER_WAIT_TIME)
    assert mine["random"] == "1759199999000", mine  # 毫秒时间戳

    mock_server.state.requests.clear()
    _run_chain(_api_for(mock_server, _forms_with(UP)), ticket)
    theirs = _recorded(mock_server, inter_urls.QUERY_ORDER_WAIT_TIME)
    assert theirs["random"] == "8609371124501", theirs  # 随机小数串去掉 "0."


def test_wire_forms_of_all_six_steps_are_recorded(mock_server, ticket, frozen_api_clock) -> None:
    """六步都真的发出去了，而且 mock 记下的字段**与规格一致**（不多不少）。

    「不多不少」很重要：多一个字段说明有人绕过规格改了表单，
    少一个字段说明规格里有键没被组装 —— 两种都会让差异表失真。
    """
    mock_server.state.requests.clear()
    spec = _forms_with(SELF)
    _run_chain(_api_for(mock_server, spec), ticket)

    expected = {
        inter_urls.SUBMIT_ORDER_REQUEST: STEP_SUBMIT,
        inter_urls.CHECK_ORDER_INFO: STEP_CHECK_ORDER_INFO,
        inter_urls.GET_QUEUE_COUNT: STEP_GET_QUEUE_COUNT,
        inter_urls.CONFIRM_SINGLE_FOR_QUEUE: STEP_CONFIRM_SINGLE_FOR_QUEUE,
    }
    for path, step in expected.items():
        recorded = _recorded(mock_server, path)
        assert len(recorded) > 0, f"mock 没记下 {path}（这一步没发出去？）"
        assert set(recorded) == set(spec.form(step).keys), (path, sorted(recorded))

    wait = _recorded(mock_server, inter_urls.QUERY_ORDER_WAIT_TIME)
    assert set(wait) == set(spec.form(STEP_QUERY_WAIT_TIME).keys), sorted(wait)
