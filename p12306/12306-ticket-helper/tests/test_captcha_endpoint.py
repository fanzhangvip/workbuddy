"""登录验证码端点归属：**必须用真机可用的那条**。

这一组对应决策 §10-4（「允许只换端点」）。背景：

上游 ``helpers/api.py`` 的登录验证码用的是
``/passport/captcha/captcha-image64``（下载）与 ``/passport/captcha/captcha-check``（校验）。
本项目真机实测**这一族已被 WAF 挡**（回 HTML 错误页而不是 4xx）。

⇒ 照抄上游等于写一条注定走不通的路径，而症状只是「验证码不对」，
完全看不出是端点问题。所以这里钉住「运行时用的是 passcodeNew 那两条」。

⚠️ 这不是「不用上游源码」，而是「上游那份**在真机上不可用**」——
正确处理是保留可用的那条并把原因记录在案。
"""

from __future__ import annotations

import ast
from pathlib import Path

from ticket_helper.inter import api as api_mod
from ticket_helper.inter import urls as urls_mod
from ticket_helper.inter.api import TicketApi
from ticket_helper.inter.base import HttpClient

BASE = Path(__file__).resolve().parent.parent

#: 上游那份用的路径（真机已被 WAF 挡）—— 只许出现在 `urls.py` 的注释与常量定义处
WAF_BLOCKED_PATH = "/passport/captcha/captcha-check"
WORKING_PATH = "/otn/passcodeNew/checkRandCodeAnsyn"


def _api(handle) -> TicketApi:
    return TicketApi(HttpClient(base_url=handle.base_url, retries=1), None)  # type: ignore[arg-type]


def _paths(handle) -> list[str]:
    return [item["path"] for item in handle.state.requests]


def test_login_captcha_verification_uses_the_working_endpoint(mock_server):
    """★ 判据是**真实请求路径**，不是返回值。

    这个函数曾经单押被 WAF 挡的旧端点 —— 它恒返回 False，
    而调用方（`TicketApi.login`）只会看到「验证码不对」。
    「端点选错」与「验证码填错」在返回值上**完全同形**，
    所以判据只能落在路径上。
    """
    api = _api(mock_server)
    before = len(_paths(mock_server))
    assert api.check_login_captcha("112,45") is True
    sent = _paths(mock_server)[before:]
    assert WORKING_PATH in sent, sent
    assert WAF_BLOCKED_PATH not in sent, "不许再打那条已被 WAF 挡的旧端点"


def test_login_captcha_sends_both_answer_and_rand_code(mock_server):
    """请求体必须**同时**带 ``answer`` 与 ``randCode``。

    官方这两个字段名的历史包袱：下单那条链路读 ``randCode``，
    登录那条读 ``answer``。只带一个会在另一半场景里静默失败
    （返回值同样是 False，看不出原因）。
    """
    api = _api(mock_server)
    before = len(mock_server.state.requests)
    api.check_login_captcha("112,45")
    sent = mock_server.state.requests[before:]
    forms = [item["payload"] or {} for item in sent if item["path"] == WORKING_PATH]
    assert forms != [], sent  # 列表比较而非「非空」：判据要能区分「几条」
    assert forms[0].get("answer") == "112,45"
    assert forms[0].get("randCode") == "112,45"


def test_captcha_rejection_is_false_not_an_exception(mock_server):
    """答错时如实返回 ``False``（不抛）。

    调用方要靠这个布尔值决定「是重试还是换扫码登录」；
    抛异常会把它变成一个需要捕获的错误路径。
    """
    mock_server.set_scenario("captcha_fail")
    api = _api(mock_server)
    assert api.check_login_captcha("0000") is False


def test_old_endpoint_constant_has_no_runtime_reference():
    """★ 静态守卫：被 WAF 挡的那条路径**不许出现在运行时引用处**。

    允许它作为常量留在 `urls.py`（那里写清了「已失效」），
    但不许任何代码去调它 —— 否则就是「照着上游写了一条走不通的路径」。
    """
    offenders: list[str] = []
    for py in (BASE / "ticket_helper").rglob("*.py"):
        if py.name == "urls.py":
            continue  # 常量定义与说明留在这里
        source = py.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            # 只看「作为属性访问被真正用起来」的地方
            if isinstance(node, ast.Attribute) and node.attr == "CAPTCHA_CHECK":
                offenders.append(f"{py.relative_to(BASE)}:{node.lineno}")
    assert offenders == [], f"被 WAF 挡的旧端点被引用了：{offenders}"


def test_the_two_working_endpoints_are_declared_in_the_protocol_layer():
    """下载与校验各只有一条可用路径，且都在协议层声明（唯一真源）。"""
    assert urls_mod.LOGIN_PASS_CODE == "/otn/passcodeNew/getPassCodeNew"
    assert urls_mod.CHECK_RAND_CODE == WORKING_PATH
    assert api_mod.CAPTCHA_OK_CODES == ("4", 4)
