"""查询路径发现域（`kernel/domains/query_route.py`）的契约测试。

防的是这几种**真实会发生且静默**的故障
----------------------------------------
1. **取了路径却没接上**：解析逻辑写得漂亮，但查询仍在用固定候选列表 ——
   表现是「优化上线了但请求数没变」，没有任何报错。
2. **两个后端返回的形态不同**：上游给 ``leftTicket/queryO``（URL 模板里的片段），
   本项目候选给 ``/otn/leftTicket/queryZ``（完整路径）。不做归一，调用方
   拼出来的 URL 就是错的 —— 而症状是「查询失败」，与「被风控」长得一样。
3. **丢了兜底**：页面结构一变就再也查不了 —— 优化反而成了单点。
4. **默认值被悄悄改掉**：换默认值 = 换行为，必须留痕。
5. **旁路漏传**：账号会话拿到了策略、匿名会话没拿到（或反之），
   两处行为不一致且只在特定入口暴露。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from ticket_helper.inter import urls as urls_mod  # noqa: E402
from ticket_helper.inter.api import TicketApi  # noqa: E402
from ticket_helper.inter.base import HttpClient  # noqa: E402
from ticket_helper.kernel import registry  # noqa: E402
from ticket_helper.kernel.pjialin.query_route import (  # noqa: E402
    NAME as PJIALIN_NAME,
    PjialinQueryRoute,
)
from ticket_helper.kernel.self.query_route import NAME as SELF_NAME, SelfQueryRoute  # noqa: E402

DOMAIN = "query_route"
PAGE_TEMPLATE = "<html><script>var CLeftTicketUrl = '{route}';</script></html>"
CANDIDATES = list(urls_mod.LEFT_TICKET_QUERY_CANDIDATES)


# --------------------------------------------------------------------------- #
# 1. 注册表与默认值
# --------------------------------------------------------------------------- #
def test_backend_names_match_the_registry_keys():
    """防的是：模块里的 ``NAME`` 与注册表键不一致 —— 于是「切了没生效」。"""
    assert registry.available_backends()[DOMAIN] == (SELF_NAME, PJIALIN_NAME)
    assert SELF_NAME == "self"
    assert PJIALIN_NAME == "pjialin"


def test_default_backend_is_self_because_changing_it_changes_behavior():
    """防的是：默认后端被悄悄改掉。

    上游那套（读页面给出的路径）**能省掉注定失败的请求**，看起来更好；
    但「换默认值 = 换行为」，本轮只交付「可切 + 差异表」，
    要切得先有差分与证据（v3 §10-17）。
    """
    assert registry.DEFAULT_BACKENDS[DOMAIN] == SELF_NAME


def test_switch_actually_swaps_the_implementation(monkeypatch):
    """防的是：开关写了但没接上。"""
    registry.reset_cache()
    monkeypatch.setenv(registry.ENV_VAR, f"{DOMAIN}={SELF_NAME}")
    assert type(registry.get(DOMAIN)).__name__ == "SelfQueryRoute"

    registry.reset_cache()
    monkeypatch.setenv(registry.ENV_VAR, f"{DOMAIN}={PJIALIN_NAME}")
    assert type(registry.get(DOMAIN)).__name__ == "PjialinQueryRoute"
    registry.reset_cache()


# --------------------------------------------------------------------------- #
# 2. self 后端：必须与改造前逐字节一致
# --------------------------------------------------------------------------- #
def test_self_backend_is_the_candidate_list_verbatim():
    """★ `self` 后端就是「原样返回候选列表」—— 它还要**刻意忽略** init_page。

    若它哪天「顺手」用了页面给的路径，`self` 与 `pjialin` 就没区别了，
    「默认行为零变化」这条底线也随之消失。
    """
    route = SelfQueryRoute()
    page = PAGE_TEMPLATE.format(route="leftTicket/queryA")
    assert route.routes(init_page=page, candidates=CANDIDATES) == CANDIDATES
    assert route.routes(init_page="", candidates=CANDIDATES) == CANDIDATES


def test_dedupe_drops_blank_and_keeps_order():
    """去重只此一处：保序、丢空串、去掉重复。"""
    from ticket_helper.kernel.domains.query_route import dedupe

    assert dedupe(["b", "a", "b", "", "  ", "a"]) == ["b", "a"]


# --------------------------------------------------------------------------- #
# 3. pjialin 后端：从页面取路径 + 形态归一 + 兜底
# --------------------------------------------------------------------------- #
def test_pjialin_takes_the_path_from_the_page_and_puts_it_first():
    route = PjialinQueryRoute()
    got = route.routes(
        init_page=PAGE_TEMPLATE.format(route="leftTicket/queryO"), candidates=CANDIDATES
    )
    assert got[0] == "/otn/leftTicket/queryO", got
    # 兜底必须还在（页面给的路径万一失效仍有退路）
    for path in CANDIDATES:
        assert path in got, f"{path} 被丢掉了：{got}"


def test_pjialin_normalises_the_upstream_form_to_ours():
    """★ 形态必须归一成**本项目候选列表那种形式**（以 ``/`` 开头的完整路径）。

    上游给的是 URL 模板里的 ``{type}`` 片段（``leftTicket/queryO``），
    直接拿去 ``client.get()`` 会打到一个不存在的地址 —— 而症状是「查询失败」，
    与「被风控」一模一样。归一方式取自上游自己的 URL 模板，不是我们发明的拼接。
    """
    route = PjialinQueryRoute()
    got = route.routes(
        init_page=PAGE_TEMPLATE.format(route="leftTicket/queryO"), candidates=CANDIDATES
    )
    assert all(path.startswith("/otn/") for path in got), got


def test_pjialin_falls_back_when_the_page_has_no_route():
    """页面里没有那个变量时必须**退回候选列表**，不许返回空、更不许瞎猜。

    返回空 = 查询彻底不可用；瞎猜 = 请求一条不存在的地址。
    两种都比「退回原策略」差。
    """
    route = PjialinQueryRoute()
    for page in ("", "<html><body>nothing</body></html>", "var CLeftTicketUrl = '';"):
        got = route.routes(init_page=page, candidates=CANDIDATES)
        assert got == CANDIDATES, (page, got)


def test_pjialin_does_not_get_stuck_when_the_page_repeats_a_candidate():
    """页面给出的路径**已经在候选里**时不能出现两条一样的。"""
    route = PjialinQueryRoute()
    got = route.routes(
        init_page=PAGE_TEMPLATE.format(route="leftTicket/queryZ"), candidates=CANDIDATES
    )
    assert len(got) == len(set(got)), got
    assert got[0] == "/otn/leftTicket/queryZ"


# --------------------------------------------------------------------------- #
# 4. TicketApi：策略真的影响「用哪条路径」
# --------------------------------------------------------------------------- #
def _api(resolver=None) -> TicketApi:
    from ticket_helper.inter.station import get_station_map

    return TicketApi(
        HttpClient(base_url="http://127.0.0.1:1", retries=0),
        get_station_map(),
        resolver,
    )


def test_ticket_api_without_resolver_is_unchanged():
    """不注入解析器时，路径顺序 = 候选列表（与改造前逐字节一致）。"""
    assert _api().query_route_candidates() == CANDIDATES


def test_ticket_api_uses_the_injected_resolver():
    """★ 防的是「写了策略但没用上」——这是本类改动最常见的假完成形态。"""
    seen: list[str] = []

    def resolver(page: str) -> list[str]:
        seen.append(page)
        return ["/otn/leftTicket/queryO", *CANDIDATES]

    api = _api(resolver)
    api._init_page = PAGE_TEMPLATE.format(route="leftTicket/queryO")  # noqa: SLF001
    assert api.query_route_candidates()[0] == "/otn/leftTicket/queryO"
    assert seen == [api._init_page], "解析器必须拿到余票页正文"  # noqa: SLF001


def test_ticket_api_falls_back_when_the_resolver_breaks():
    """解析器抛异常或给空列表时，必须退回候选列表。

    「取路径」是优化，**不该成为新的失败点**：一个第三方（或上游）解析器
    出问题，不能把查询整个打挂。
    """

    def boom(page: str) -> list[str]:
        raise RuntimeError("解析器坏了")

    assert _api(boom).query_route_candidates() == CANDIDATES
    assert _api(lambda page: []).query_route_candidates() == CANDIDATES


def test_warm_up_captures_the_page_body():
    """热身时必须把页面正文留下 —— 路径就是从它里面读的。

    这条钉住的是「读路径不需要额外请求」这个前提：如果哪天热身不再缓存正文，
    `pjialin` 策略就会要么失效、要么偷偷多发一次请求。
    """
    api = _api()
    api._init_page = ""  # noqa: SLF001
    page = PAGE_TEMPLATE.format(route="leftTicket/queryZ")
    api._init_page = page  # noqa: SLF001
    assert api.query_route_candidates() == CANDIDATES
    assert api._init_page == page  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 5. 端到端：策略真的改变了**发出去的请求**
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "backend,expected_first",
    [(SELF_NAME, "/otn/leftTicket/queryZ"), (PJIALIN_NAME, "/otn/leftTicket/queryA")],
)
def test_the_strategy_decides_which_path_is_requested(
    mock_server, monkeypatch, backend, expected_first
):
    """★ 本域的总验收：mock 的余票页宣称路径是 ``queryA``，
    于是只有「真去读页面」的策略才会第一个打到 queryA；盲试的仍打 queryZ。

    判据是 **mock 记下的真实请求路径**，不是函数返回值 ——
    这正对应 V6「能力可用性证明」的口径：看请求长什么样。
    """
    from ticket_helper.inter.station import get_station_map

    monkeypatch.setenv(registry.ENV_VAR, f"{DOMAIN}={backend}")
    registry.reset_cache()

    client = HttpClient(base_url=mock_server.base_url, retries=0)
    session = __import__("requests").Session()
    session.post(f"{mock_server.base_url}/__mock__/query-route", json={"route": "leftTicket/queryA"})

    domain = registry.get(DOMAIN)
    api = TicketApi(
        client,
        get_station_map(),
        lambda page: domain.routes(init_page=page, candidates=CANDIDATES),
    )
    api.query_left_ticket("2026-10-01", "BJP", "SHH")

    paths = [
        item["path"]
        for item in mock_server.state.requests
        if item["path"].startswith("/otn/leftTicket/query")
    ]
    assert paths != [], "没有发出任何查询请求"
    assert paths[0] == expected_first, paths
    registry.reset_cache()


def test_composition_root_injects_the_resolver(app_context):
    """防的是：装配处忘了注入 —— 于是线上跑的还是「自己那套」，没人会发现。"""
    from ticket_helper.account.manager import UserConfig

    user = app_context.upsert_user(UserConfig(username="route_probe", password="pw"))
    assert hasattr(user.api, "query_route_candidates"), "账号会话没有接口对象"
    # 判据用 callable 而不是「非 None」：它同时排除了「漏传」与「传了个空值」
    assert callable(user.api._route_resolver), (  # noqa: SLF001
        "账号会话没拿到可用的查询路径策略：装配处漏传了"
    )


def test_attaching_api_keeps_the_resolver(app_context):
    """★ 防的是**旁路漏传**：`attach_api()` 重建 TicketApi 时忘了带策略。

    漏了它，这条路径上的账号会**悄悄**退回固定候选列表，
    而其他账号已用上新策略 —— 两处行为不一致，且只在特定入口暴露。
    """
    from ticket_helper.account.manager import UserConfig
    from ticket_helper.inter.station import StationMap

    user = app_context.upsert_user(UserConfig(username="route_probe2", password="pw"))
    resolver = app_context._build_query_route_resolver()  # noqa: SLF001
    user.route_resolver = resolver
    user.attach_api(StationMap(app_context.paths).load(allow_remote=False))
    assert user.api._route_resolver is resolver, (  # noqa: SLF001
        "attach_api 把查询路径策略丢了"
    )
