"""用户域（登录态检测 `login_check`）的契约测试。

每条用例都要满足「**撤掉实现就变红**」—— 跑 `python scripts/contract_mutation_check.py -k 登录态`
可以验证（见 `scripts/contract_mutation_check.py` 里的登记项）。

为什么这一域的测试要写得这么细
------------------------------
本项目在「登录态」上吃过一次亏（见 `docs/缺陷回流台账.md`）：判定写成
``payload is None → return response.ok``，而 ``response.ok`` 对 HTTP 200 恒为真 ——
于是「会话失效、官方回登录页 HTML」被判成「还活着」。后果很隐蔽：
账号一直显示在线，之后每个请求都被拒，使用者只看到「查询一直失败」。

所以这组用例咬住三件事：
1. **判定三态**（ALIVE / DEAD / UNKNOWN），且 UNKNOWN 不许并进 DEAD；
2. **上游字段 ``is_login`` 是字符串比较**（``== 'Y'``），按 Python 真值判会把
   ``"N"`` 判成「活着」—— 这个坑必须有一条用例专门钉住；
3. **切换真的换了请求**（不是「配置改了但打的还是同一条」）。
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from ticket_helper.errors import ConfigError
from ticket_helper.inter import parser as inter_parser
from ticket_helper.inter import urls as inter_urls
from ticket_helper.inter.api import TicketApi
from ticket_helper.kernel import registry
from ticket_helper.kernel.domains.login_check import (
    ALIVE,
    DEAD,
    UNKNOWN,
    alive_of,
)

DOMAIN = "login_check"
SELF = "self"
UP = "pjialin"


# --------------------------------------------------------------------------- #
# 工具：临时切后端取规格
# --------------------------------------------------------------------------- #
def _spec_with(backend: str) -> Any:
    previous = os.environ.get(registry.ENV_VAR)
    os.environ[registry.ENV_VAR] = f"{DOMAIN}={backend}"
    registry.reset_cache()
    try:
        return registry.get(DOMAIN).spec()
    finally:
        if previous is None:
            os.environ.pop(registry.ENV_VAR, None)
        else:
            os.environ[registry.ENV_VAR] = previous
        registry.reset_cache()


def _api_for(handle: Any, spec: Any) -> TicketApi:
    """按给定规格构造一个直连 mock 的接口对象。"""
    from ticket_helper.inter.base import HttpClient

    return TicketApi(
        HttpClient(base_url=handle.base_url, retries=1),
        None,  # type: ignore[arg-type]  check_user 不碰站点表
        login_spec=spec,
    )


def _paths(handle: Any) -> list[str]:
    return [item["path"] for item in handle.state.requests]


# --------------------------------------------------------------------------- #
# 1. 规格与协议层：不许各写一份
# --------------------------------------------------------------------------- #
def test_self_spec_equals_the_builtin_default():
    """防的是：`kernel/self` 与接口层的默认规格**分叉**。

    接口层在没有被注入策略时要自己组装默认规格（它不许 import 具体后端，会成环），
    于是「同一个规格」存在两个组装点。这条断言把两者钉成**必须逐字段相等** ——
    任何一边改了而另一边没跟，立刻变红。
    """
    from ticket_helper.kernel.self.login_check import SelfLoginProbe

    assert SelfLoginProbe().spec() == TicketApi._builtin_login_spec()  # noqa: SLF001


def test_self_spec_values_come_from_the_protocol_layer():
    """防的是：有人在适配层把路径/参数**写死一遍**（协议字面量漏进内核层）。

    值必须**来自协议层的常量**，而不是「恰好相等的字面量」：
    所以这里比对的是**恒等**（同一份数据），不是值相等。
    """
    spec = _spec_with(SELF)
    assert spec.path == inter_urls.CHECK_USER_PROBE["path"]
    assert spec.method == inter_urls.CHECK_USER_PROBE["method"]
    assert spec.data == inter_urls.CHECK_USER_PROBE["data"]
    assert spec.referer == inter_urls.CHECK_USER_PROBE["referer"]
    assert spec.alive_paths == inter_parser.CHECK_USER_ALIVE_PATHS
    # 改协议层常量必须同时改到规格 —— 下面这条是「真源唯一」的锚：
    assert inter_urls.CHECK_USER_PROBE["path"] == inter_urls.CHECK_USER


def test_upstream_spec_comes_from_the_vendor_facts():
    """防的是：上游规格**手抄**，而不是取生成物（手抄错了没人会发现）。"""
    from ticket_helper.kernel.pjialin.shim import install

    install()  # 把 vendor/pjialin 挂上 sys.path
    import ticket_facts  # type: ignore[import-not-found]

    spec = _spec_with(UP)
    assert spec.path == ticket_facts.LOGIN_CHECK_PATH
    assert spec.alive_paths == (ticket_facts.LOGIN_ALIVE_FIELD,)
    assert spec.alive_values == ticket_facts.LOGIN_ALIVE_VALUES


def test_upstream_spec_keeps_the_missing_referer():
    """★ 防的是：有人「顺手」给上游那条**补上 Referer**，把差异抹平。

    看着是好心，实际后果是：差分表从此显示「两边都带 Referer」，
    真机上把默认切过去时才发现上游那条会被 WAF 拒（回 HTML 错误页而不是 4xx）。
    **差异要留在能被看见的地方** —— 补平差异不是修 bug，是藏 bug。
    """
    assert _spec_with(SELF).referer != "", "本项目那条带 Referer"
    assert _spec_with(UP).referer == "", "上游那条不带，必须如实保留"


def test_upstream_spec_keeps_the_missing_referer():
    """★ 防的是：有人「顺手」给上游那条**补上 Referer**，把差异抹平。

    看着是好心，实际后果是：差分表从此显示「两边都带 Referer」，
    真机上把默认切过去时才发现上游那条会被 WAF 拒（回 HTML 错误页而不是 4xx）。
    **差异要留在能被看见的地方** —— 补平差异不是修 bug，是藏 bug。
    """
    assert _spec_with(SELF).referer != "", "本项目那条带 Referer"
    assert _spec_with(UP).referer == "", "上游那条不带，必须如实保留"


# --------------------------------------------------------------------------- #
# 2. 判定语义（三态）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"data": {"flag": True}}, ALIVE),
        ({"data": {"flag": False}}, DEAD),
        ({"status": True}, ALIVE),
        ({"status": False}, DEAD),
        ({"data": {}}, UNKNOWN),
        ({"data": {"unrelated": 1}}, UNKNOWN),
        ("not a dict", UNKNOWN),
    ],
    ids=["flag真", "flag假", "status真", "status假", "空data", "无关字段", "非字典"],
)
def test_self_verdicts(payload: Any, expected: str) -> None:
    assert alive_of(payload, _spec_with(SELF)) == expected


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"data": {"is_login": "Y"}}, ALIVE),
        ({"data": {"is_login": "N"}}, DEAD),
        ({"data": {"is_login": "y"}}, ALIVE),
        ({"data": {"is_login": ""}}, DEAD),
        ({"data": {}}, UNKNOWN),
    ],
    ids=["Y", "N", "小写y", "空串", "字段缺失"],
)
def test_upstream_verdicts(payload: Any, expected: str) -> None:
    assert alive_of(payload, _spec_with(UP)) == expected


def test_upstream_string_field_is_not_judged_by_python_truthiness():
    """★ 防的是：上游的 ``is_login`` 是**字符串比较**，被按 Python 真值判。

    ``bool("N")`` 是 ``True`` —— 非空字符串在 Python 里恒为真。
    如果上游规格不显式给 ``alive_values=("Y",)``，那么 ``is_login="N"``
    （= 明确未登录）会被判成 **ALIVE**。后果就是本项目吃过的那次：
    界面显示在线、之后每个请求都被拒。
    """
    assert bool("N") is True, "前提：Python 里非空字符串恒为真"
    spec = _spec_with(UP)
    assert spec.alive_values == ("Y",), "上游规格必须显式给取值集合"
    assert alive_of({"data": {"is_login": "N"}}, spec) == DEAD


def test_backends_do_not_recognise_each_others_fields():
    """防的是：有人「顺手」让一套实现也认另一套的字段。

    看似贴心，实际上会把**结构差异藏起来** —— 于是切后端时看不出「响应结构其实变了」，
    而真机上正好就是这一点会炸。差异要留在能被看见的地方。
    """
    self_spec, up_spec = _spec_with(SELF), _spec_with(UP)
    assert alive_of({"data": {"is_login": "Y"}}, self_spec) == UNKNOWN
    assert alive_of({"data": {"flag": True}}, up_spec) == UNKNOWN


def test_unknown_is_not_folded_into_dead():
    """三态必须真的存在：UNKNOWN 与 DEAD 是两个不同的常量，且判定会分别返回。

    合并它们会让「读不出结论」被当成「已失效」→ 反复重新登录；
    本项目在「人证核验」上就因为同样的合并，把正常账号永久挡在候补外。
    """
    assert UNKNOWN != DEAD
    assert {ALIVE, DEAD, UNKNOWN} == {"alive", "dead", "unknown"}
    assert alive_of({}, _spec_with(SELF)) == UNKNOWN


# --------------------------------------------------------------------------- #
# 3. 端到端：切换真的换了请求
# --------------------------------------------------------------------------- #
def test_the_strategy_decides_which_endpoint_is_requested(mock_server):
    """**本域最关键的判据**：判据是 mock 记下的真实请求路径。

    只看返回值不够 —— 两条端点都能返回「在线」，光看 `True` 分不出切没切。
    """
    self_spec, up_spec = _spec_with(SELF), _spec_with(UP)

    before = len(_paths(mock_server))
    assert _api_for(mock_server, self_spec).check_user() is True
    after_self = _paths(mock_server)[before:]
    assert inter_urls.CHECK_USER in after_self, after_self

    before = len(_paths(mock_server))
    assert _api_for(mock_server, up_spec).check_user() is True
    after_up = _paths(mock_server)[before:]
    assert up_spec.path in after_up, after_up
    assert inter_urls.CHECK_USER not in after_up, "切到上游后不该再打本项目那条端点"


def test_html_response_is_still_treated_as_dead(mock_server, monkeypatch):
    """真机硬约束：**回 HTML 一律判失效**（两套后端共用同一段判定）。

    这条最早是这样被发现的：会话失效时官方回登录页 HTML（HTTP 200），
    而当时判定写成 ``response.ok``，于是被判成「还活着」。
    """
    from ticket_helper.inter.base import HttpResponse

    api = _api_for(mock_server, _spec_with(UP))
    monkeypatch.setattr(
        api.client,
        "get",
        lambda *a, **k: HttpResponse(
            ok=True, status_code=200, text="<!DOCTYPE html><html>login</html>"
        ),
    )
    assert api.check_user() is False


def test_unparsable_response_is_dead_not_alive(mock_server, monkeypatch):
    """拿不到结论 → 判「不可用」（宁可多登一次，也不要带着坏会话继续跑）。"""
    from ticket_helper.inter.base import HttpResponse

    api = _api_for(mock_server, _spec_with(UP))
    monkeypatch.setattr(
        api.client,
        "get",
        lambda *a, **k: HttpResponse(ok=True, status_code=200, text="not json at all"),
    )
    assert api.check_user() is False


# --------------------------------------------------------------------------- #
# 4. 开关与默认值
# --------------------------------------------------------------------------- #
def test_switch_actually_swaps_the_spec():
    """防的是：改了开关但拿到的还是旧规格。"""
    assert _spec_with(SELF).path != _spec_with(UP).path


def test_default_backend_is_self_because_the_response_shape_differs():
    """**默认保持 `self`**，理由写在这里：

    两套实现的**判活字段互不认得**（见上面那条用例）。切后端 = 同时换端点与响应结构，
    而上游那条还**不带 Referer**（真机上很可能被 WAF 拒，回 HTML 错误页而不是 4xx）。
    在拿到真机证据之前不动默认值 —— 「更省请求」「更接近上游」都不是换行为的理由。
    """
    assert registry.DEFAULT_BACKENDS[DOMAIN] == SELF, (
        "改默认值 = 换行为。要先有真机证据（v3 §10-17），"
        "并把 `scripts/login_check_diff.py` 的结论写进方案文档。"
    )


def test_illegal_backend_name_fails_fast():
    """写错名字要**快速失败**，不能静默回退 —— 否则「我明明切了 X」无法查证。"""
    with pytest.raises(ConfigError):
        previous = os.environ.get(registry.ENV_VAR)
        os.environ[registry.ENV_VAR] = f"{DOMAIN}=not_a_backend"
        registry.reset_cache()
        try:
            registry.get(DOMAIN)
        finally:
            if previous is None:
                os.environ.pop(registry.ENV_VAR, None)
            else:
                os.environ[registry.ENV_VAR] = previous
            registry.reset_cache()


# --------------------------------------------------------------------------- #
# 5. 装配：策略必须真的送到接口层
# --------------------------------------------------------------------------- #
def test_composition_root_injects_the_spec(app_context):
    """防的是：装配处忘了注入 —— 于是线上跑的还是默认那套，没人会发现。"""
    from ticket_helper.account.manager import UserConfig

    user = app_context.upsert_user(UserConfig(username="login_probe", password="pw"))
    # 判据是「接口对象上真的有这个能力」，而不是「不是 None」——
    # 后者对一个空壳对象也成立。
    assert hasattr(user.api, "check_user"), "账号应挂上接口对象"
    spec = user.api._login_spec  # noqa: SLF001
    assert spec.path == inter_urls.CHECK_USER_PROBE["path"]
    assert spec.name == SELF


def test_hot_switch_reaches_new_sessions(app_context):
    """防的是：切了开关但已有/新建会话仍用旧规格。

    判据仍是**请求落到哪条路径**，不是「配置对象变了」。
    """
    from ticket_helper.kernel.self.login_check import SelfLoginProbe

    spec = app_context._build_login_spec()  # noqa: SLF001
    assert spec == SelfLoginProbe().spec()


# --------------------------------------------------------------------------- #
# 6. 构造完整性：`__init__` 声明的属性必须真的挂在实例上
# --------------------------------------------------------------------------- #
def test_every_attribute_declared_in_init_actually_exists(app_context):
    """★ 防的是：往 `__init__` 里**插代码时把后面的赋值挤进新方法体**。

    2026-09-30 真发生过：插入 `_builtin_login_spec` 方法时，
    `self._order_combo_hint = None` 被挤到了那个方法 `return` 之后，
    成了**永不执行的死代码**。后果链条很长、每一环都静默：

    1. `query_order_history` 抛 `AttributeError`；
    2. 被 `engine._check_fulfilled_by_order` 的 `log.debug` 吞掉；
    3. 候补监控**永远等不到「已兑现」，也不会报错** → 同步跑的 `_monitor_loop()`
       变成死循环，一条测试用例卡 20 分钟才被人发现。

    判据用 AST 取出 `__init__` 里声明的属性名，逐个查实例是否真有 ——
    所以「插错位置」会**立刻**变红，不必等到某条低频路径踩上。
    """
    import ast
    import inspect
    import textwrap

    from ticket_helper.inter.api import TicketApi

    tree = ast.parse(textwrap.dedent(inspect.getsource(TicketApi.__init__)))
    declared = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and isinstance(node.ctx, ast.Store)
    }
    assert "_order_combo_hint" in declared, (
        "AST 没抽出属性名？这条守卫本身失效了，先修它"
    )

    from ticket_helper.account.manager import UserConfig

    user = app_context.upsert_user(UserConfig(username="attr_probe", password="pw"))
    missing = sorted(name for name in declared if not hasattr(user.api, name))
    assert missing == [], (
        f"`TicketApi.__init__` 声明了但这些属性在实例上不存在：{missing}"
        "（多半是插代码时把它们挤进别的块里了）"
    )
