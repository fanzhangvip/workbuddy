"""滑块风控域（`slide`）的契约测试。

每条都要满足「撤掉实现就变红」（`scripts/contract_mutation_check.py -k 滑块`）。

本域有个**特别**的地方：上游那套要起真浏览器，本机**跑不起来**
（沙箱里连 `open` 都被 SIGTERM，更没有 Chromium）。所以这一份测试
**不假装**验证了拖拽本身，只钉住三件能钉住的事：

1. **判断规格两边同源** —— 「要不要过滑块」不该因为选哪个后端而变；
2. **回退语义** —— `self` 后端不做事、**也不报错**，流程照旧（回退不引入新失败点）；
3. **接线** —— 真拿到 sessionId/sig 时，它们确实进了 `checkOrderInfo` 的表单。

⚠️ 「拖拽真的能过滑块」这件事**离线不可验证**，属未验证声明：
判据在真机上（见 `scripts/diagnose_order.py`），不在本文件里。
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from ticket_helper.inter import urls as inter_urls
from ticket_helper.inter.api import TicketApi
from ticket_helper.inter.base import HttpClient
from ticket_helper.inter.parser import parse_left_ticket_result
from ticket_helper.kernel import registry
from ticket_helper.kernel.domains.slide import SlideResult, needs_slide
from ticket_helper.kernel.pjialin.shim import install
from mock_server.server import build_ticket_row

DOMAIN = "slide"
SELF = "self"
UP = "pjialin"

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "pjialin"

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


@pytest.fixture(scope="module")
def facts():
    install()
    # 见 test_query_baseline.py 的说明：上游目录**只能 append**
    # （vendor 里也有 main.py，插到前面会抢走 `import main`）
    sys.path.append(str(VENDOR))
    try:
        import ticket_facts  # type: ignore[import-not-found]

        yield ticket_facts
    finally:
        sys.path.remove(str(VENDOR))


def _solver_with(backend: str):
    previous = os.environ.get(registry.ENV_VAR)
    os.environ[registry.ENV_VAR] = f"{DOMAIN}={backend}"
    registry.reset_cache()
    try:
        return registry.get(DOMAIN)
    finally:
        if previous is None:
            os.environ.pop(registry.ENV_VAR, None)
        else:
            os.environ[registry.ENV_VAR] = previous
        registry.reset_cache()


class _FakeSolver:
    """假的滑块后端：直接给结果，用来验证**接线**（不碰真浏览器）。"""

    name = "fake"

    def __init__(self, result: SlideResult | None) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def spec(self):
        from ticket_helper.kernel.domains.slide import SlideSpec

        return SlideSpec(
            name=self.name,
            state_pattern_source=inter_urls.ORDER_SLIDE_STATE_PATTERN,
            selector=inter_urls.ORDER_SLIDE_SELECTOR,
            scene=inter_urls.ORDER_SLIDE_SCENE,
        )

    def available(self) -> tuple[bool, str]:
        return True, "假后端（测试用）"

    def solve(self, *, html: str, cookies):
        self.calls.append({"html_len": len(html), "cookies": list(cookies)})
        return self._result


def _api_for(handle: Any, *, order_spec: Any = None, slide_solver: Any = None) -> TicketApi:
    return TicketApi(
        HttpClient(base_url=handle.base_url, retries=0),
        None,
        order_spec=order_spec,
        slide_solver=slide_solver,
    )  # type: ignore[arg-type]


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


# --------------------------------------------------------------------------- #
# 1. 判断规格：两边同源（不许各写一份正则）
# --------------------------------------------------------------------------- #
def test_the_two_backends_compile_the_same_pattern(facts) -> None:
    """★ 防的是：「要不要过滑块」的判读因为选了不同后端而变。

    上游那份是**字面量写法**（`r"var if_check_slide_passcode.*='(\\d?)'"`），
    本项目那份写在 `inter/urls.py`。这里要求两者**编译出来的模式一模一样** ——
    任何一边改了而另一边没跟，立刻变红。
    """
    upstream = _solver_with(UP).spec()
    ours = _solver_with(SELF).spec()
    assert upstream.state_pattern().pattern == ours.state_pattern().pattern
    assert upstream.state_pattern().pattern == (
        ast.literal_eval(facts.ORDER_SLIDE_STATE_REGEX_SOURCE)
    )


def test_selector_and_scene_match_upstream(facts) -> None:
    """选择器与 scene 也逐项对上游出处（scene 正是要填进表单的那个值）。"""
    upstream = _solver_with(UP).spec()
    assert upstream.selector == facts.ORDER_SLIDE_SELECTOR == inter_urls.ORDER_SLIDE_SELECTOR
    assert upstream.scene == facts.ORDER_SLIDE_CONST["scene"] == inter_urls.ORDER_SLIDE_SCENE
    assert upstream.scene == "nc_login"


# --------------------------------------------------------------------------- #
# 2. 判读本身
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<script>var if_check_slide_passcode  ='1';</script>", True),
        ("<script>var if_check_slide_passcode  ='0';</script>", False),
        # 判不出来 → **当不需要**（刻意的：反过来会凭空拦住一单）
        ("<html>没有这一行</html>", False),
        ("", False),
    ],
)
def test_needs_slide_reads_the_page_flag(html: str, expected: bool) -> None:
    """判据只来自 initDc 页面正文里的那一个数字。

    ⚠️ 「页面里没有这一行」→ **False**，不是异常、也不是 True。
    理由：这一行缺失只可能是页面版本变了；此时**让官方去判**
    （需要滑块它会拒），比我们自己凭空拦下一单要好 ——
    本项目在「人证核验」上正是踩了反过来的那个坑。
    """
    assert needs_slide(html, _solver_with(SELF).spec()) is expected


# --------------------------------------------------------------------------- #
# 3. 切换点与**回退语义**
# --------------------------------------------------------------------------- #
def test_default_backend_is_upstream_because_self_has_no_implementation() -> None:
    """默认就是上游：本项目**根本没有程序化解法** —— 这一项正是
    「上游有的禁止自己实现」最直白的一例。"""
    previous = os.environ.pop(registry.ENV_VAR, None)
    registry.reset_cache()
    try:
        assert registry.backend_name(DOMAIN) == UP
    finally:
        if previous is not None:
            os.environ[registry.ENV_VAR] = previous
        registry.reset_cache()


def test_rollback_backend_is_unavailable_but_does_not_raise() -> None:
    """★ 回退后端：``available()`` 为假、``solve()`` 返回 ``None`` 且**不抛**。

    回退的意义是「回到改造前的行为」。改造前遇到滑块就是没办法 ——
    如果回退后端在这里抛异常，那就**引入了一个新的失败点**，那不是回退。
    """
    solver = _solver_with(SELF)
    ok, why = solver.available()
    assert ok is False
    assert "TICKET_HELPER_KERNEL_BACKENDS" in why, "「不可用」必须给出路（切成什么能用）"
    # `None` 在这里**就是契约**（回退后端不做事、也不抛）：真正的判据是上面
    # `available()` 为假 + 下面 `solve()` 不抛异常。所以显式豁免弱断言门。
    assert solver.solve(html="<html></html>", cookies=[]) is None  # noqa: WEAK-ASSERT - None 是本域的契约值


def test_a_broken_solver_cannot_break_account_restore(mock_server, caplog) -> None:
    """★ 防的是：**可选能力**的故障把**账号恢复**整条打断（真机上表现为「0 个账号」）。

    排查 P5 打包时抓到的真实链路（把产物里的 `vendor/` 移走即可复现）：

        pjialin/slide.py::spec()  →  import ticket_facts  →  ModuleNotFoundError
          →  TicketApi.__init__ 抛
          →  build_user 抛
          →  _bootstrap_accounts 吞掉、只记一句「恢复账号 X 失败」
          →  界面 / doctor 显示「0 个账号」

    看起来像数据丢了，而根因在两跳之外（vendor 没进包）。滑块的设计本来就是
    「**回退不引入新失败点**」，所以判读规格取不到时必须退回内置那份
    （判读与后端无关），并留下一条说得出原因的 WARNING。
    """

    class _BrokenSolver:
        name = "pjialin"

        def spec(self) -> Any:
            raise ModuleNotFoundError("No module named 'ticket_facts'")

        def available(self) -> tuple[bool, str]:
            return False, "上游 vendor 未就位"

        def solve(self, *, html: str, cookies: list[Any]) -> Any:
            return None

    reference = _api_for(mock_server)._slide_spec  # noqa: SLF001
    with caplog.at_level("WARNING"):
        api = _api_for(mock_server, slide_solver=_BrokenSolver())

    assert type(api._slide_spec) is type(reference)  # noqa: SLF001
    assert api._slide_spec.state_pattern_source == reference.state_pattern_source  # noqa: SLF001
    messages = [record.getMessage() for record in caplog.records]
    assert any("ticket_facts" in m for m in messages), (
        f"退回内置规格时必须把**原因**写出来（否则又是一句查不动的日志），实际 {messages}"
    )
    assert any("不会过滑块" in m for m in messages), messages


def test_rollback_keeps_the_order_flow_alive(mock_server, ticket, caplog) -> None:
    """★ 端到端：**需要过滑块 + 回退后端** 时，流程必须照旧走下去。

    这条防的是「回退之后每单都炸」——那等于把人锁死在半路上，
    比「滑不过去」更难查（因为看起来是我们自己抛的错）。
    """
    mock_server.state.scenario = "need_slide"
    api = _api_for(mock_server, slide_solver=_solver_with(SELF))
    with caplog.at_level("WARNING"):
        dc = api.init_dc()
    assert dc["needs_slide"] is True, "判读本身不该受后端影响"
    assert api._slide_values == {}  # noqa: SLF001
    messages = [record.getMessage() for record in caplog.records]
    assert any("滑块" in m and "slide" in m for m in messages), (
        f"回退时必须留下一条能看懂、且给出路的 WARNING，实际 {messages}"
    )
    mock_server.state.scenario = None


def test_no_solver_at_all_behaves_like_the_rollback(mock_server, ticket) -> None:
    """没注入后端（= 改造前的状态）也一样：不抛、不下滑、流程继续。"""
    mock_server.state.scenario = "need_slide"
    api = _api_for(mock_server)
    dc = api.init_dc()
    assert dc["needs_slide"] is True
    assert api._slide_values == {}  # noqa: SLF001
    mock_server.state.scenario = None


# --------------------------------------------------------------------------- #
# 4. 接线：拿到了 sessionId/sig，就必须进 checkOrderInfo 的表单
# --------------------------------------------------------------------------- #
def test_slide_result_maps_to_the_form_keys(facts) -> None:
    """结果映射出来的键名**必须与上游表单里的字段名一致**。

    名字不一致会在「填表单」那一步变成一次静默的字段名错配 ——
    官方只看得到「表单里没有 sessionId」，回一句「系统忙」。
    """
    mapped = SlideResult(session_id="S1", sig="G1").as_form_values()
    assert set(mapped) == {"sessionId", "sig"}
    for key in mapped:
        assert key in facts.ORDER_SLIDE_FIELDS


def test_slide_values_reach_check_order_info(mock_server, ticket) -> None:
    """★ 滑块结果真的进了 ``checkOrderInfo`` 的表单（上游后端才用这三个键）。"""
    from ticket_helper.kernel.domains.order_submit import STEP_CHECK_ORDER_INFO

    mock_server.state.scenario = "need_slide"
    mock_server.state.requests.clear()

    orders = _api_for(
        mock_server,
        order_spec=_order_forms_with(UP),
        slide_solver=_FakeSolver(SlideResult(session_id="SID-1", sig="SIG-1")),
    )
    orders.submit_order_request(ticket, from_name="广州南", to_name="上海虹桥")
    dc = orders.init_dc()
    orders.check_order_info(MOCK_PASSENGERS, "O", token=dc.get("repeat_submit_token", ""))

    recorded = _recorded(mock_server, inter_urls.CHECK_ORDER_INFO)
    assert recorded["sessionId"] == "SID-1", recorded
    assert recorded["sig"] == "SIG-1", recorded
    assert recorded["scene"] == "nc_login", recorded
    mock_server.state.scenario = None


def test_slide_values_are_reset_for_every_order(mock_server, ticket) -> None:
    """★ 上一单的 ``sessionId``/``sig`` 绝不能带到下一单。

    带过去必被拒，而拒的原因仍是一句「系统忙，请稍后重试」——
    这种「跨单串味」的 bug 只能靠「每单重置」来防。
    """
    api = _api_for(
        mock_server,
        order_spec=_order_forms_with(UP),
        slide_solver=_FakeSolver(SlideResult(session_id="S-A", sig="G-A")),
    )
    mock_server.state.scenario = "need_slide"
    api.init_dc()
    assert api._slide_values == {"sessionId": "S-A", "sig": "G-A"}  # noqa: SLF001

    mock_server.state.scenario = None  # 下一单不需要过滑块
    api.init_dc()
    assert api._slide_values == {}, "新的一单必须清掉上一单的滑块结果"


# --------------------------------------------------------------------------- #
# 5. Cookie 导出：给浏览器的那份必须**全量**
# --------------------------------------------------------------------------- #
def test_cookie_dump_is_complete_and_browser_ready() -> None:
    """★ ``dump_cookies`` 必须导**全部** Cookie，且每项都带 ``url`` 或 ``domain``。

    两个都踩过的坑：

    - **筛掉 Cookie**：会话持久化当初只留了 5 个，恢复后 ``checkUser`` 回
      ``flag:false``；设备指纹少一个就可能被 WAF 拒（症状是滑块一直过不去）。
    - **缺 url/domain**：pyppeteer 的 ``setCookie`` 会直接抛错。
    """
    client = HttpClient(base_url="https://kyfw.12306.cn", retries=0)
    try:
        client.session.cookies.set("RAIL_DEVICEID", "DEV-1", domain="kyfw.12306.cn")
        client.session.cookies.set("RAIL_EXPIRATION", "9999", domain="kyfw.12306.cn")
        client.session.cookies.set("tk", "TK-1", domain="kyfw.12306.cn")
        dumped = client.dump_cookies()
        names = {item["name"] for item in dumped}
        assert names == {"RAIL_DEVICEID", "RAIL_EXPIRATION", "tk"}, names
        for item in dumped:
            assert "url" in item or "domain" in item, item
    finally:
        client.session.close()


def test_upstream_solver_hands_cookies_through_the_shim() -> None:
    """垫片只做搬运：给它的 Cookie 必须原样出现在 ``dump_cookies()`` 里。"""
    from ticket_helper.kernel.pjialin.slide import _SessionShim

    shim = _SessionShim([{"name": "a", "value": "1", "url": "https://x"}])
    assert shim.dump_cookies() == [{"name": "a", "value": "1", "url": "https://x"}]
    # 上游会拿去 `page.setCookie(*session.dump_cookies())`
    assert len(shim.dump_cookies()) == 1


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _order_forms_with(backend: str):
    previous = os.environ.get(registry.ENV_VAR)
    os.environ[registry.ENV_VAR] = f"order_submit={backend}"
    registry.reset_cache()
    try:
        return registry.get("order_submit").forms()
    finally:
        if previous is None:
            os.environ.pop(registry.ENV_VAR, None)
        else:
            os.environ[registry.ENV_VAR] = previous
        registry.reset_cache()


def _recorded(handle: Any, path: str) -> dict[str, Any]:
    for item in reversed(handle.state.requests):
        if item["path"] == path:
            return dict(item["payload"] or {})
    return {}
