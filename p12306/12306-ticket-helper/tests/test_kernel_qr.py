"""扫码轮询请求体域（`qr_check`）的契约测试。

每条都要满足「撤掉实现就变红」（`scripts/contract_mutation_check.py -k 扫码`）。

判据是**发出去的表单长什么样**，不是「函数返回了什么」——
两套实现的端点相同、返回值也相同（都返回扫码状态），
只有真实表单能区分「切了没切」。
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from ticket_helper.errors import ConfigError
from ticket_helper.inter import urls as inter_urls
from ticket_helper.inter.api import TicketApi
from ticket_helper.kernel import registry

DOMAIN = "qr_check"
SELF = "self"
UP = "pjialin"


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


def _api_for(handle: Any, spec: Any, *, device: str = "", expiration: str = "") -> TicketApi:
    """按给定规格构造一个直连 mock 的接口对象（可选塞入设备指纹 Cookie）。"""
    from ticket_helper.inter.base import HttpClient

    client = HttpClient(base_url=handle.base_url, retries=1)
    if device:
        client.session.cookies.set("RAIL_DEVICEID", device, domain="127.0.0.1")
    if expiration:
        client.session.cookies.set("RAIL_EXPIRATION", expiration, domain="127.0.0.1")
    return TicketApi(client, None, qr_spec=spec)  # type: ignore[arg-type]


def _last_form(handle: Any) -> dict[str, str]:
    """mock 记下的**完整表单**（含 mock 自己加的 ``method`` 标记）。

    ⚠️ 这里能读到完整表单，依赖 mock 把**整个表单**记下来 ——
    原来它只记 uuid/appid/method，于是本域的判据在离线**不可观测**。
    """
    for item in reversed(handle.state.requests):
        if item["path"] == inter_urls.CHECK_QR:
            return dict(item["payload"] or {})
    return {}


# --------------------------------------------------------------------------- #
# 1. 规格与协议层：不许各写一份
# --------------------------------------------------------------------------- #
def test_self_spec_equals_the_builtin_default():
    """防的是：`kernel/self` 与接口层的默认规格**分叉**。

    接口层不许 import 具体后端（会成环），所以「同一个规格」有两个组装点；
    这条把两者钉成必须逐字段相等 —— 任何一边改了而另一边没跟，立刻变红。
    """
    from ticket_helper.kernel.self.qr_check import SelfQrCheck

    assert SelfQrCheck().spec() == TicketApi._builtin_qr_spec()  # noqa: SLF001


def test_self_spec_values_come_from_the_protocol_layer():
    """防的是：适配层把路径/字段名**写死一遍**（协议字面量漏进内核层）。"""
    spec = _spec_with(SELF)
    assert spec.path == inter_urls.CHECK_QR
    assert spec.form_keys == inter_urls.QR_CHECK_FORM_KEYS
    assert spec.constants == inter_urls.QR_CHECK_CONSTANTS


def test_upstream_spec_comes_from_the_vendor_facts():
    """防的是：上游规格**手抄**，而不是取生成物。"""
    from ticket_helper.kernel.pjialin.shim import install

    install()
    import ticket_facts  # type: ignore[import-not-found]

    spec = _spec_with(UP)
    assert spec.path == ticket_facts.QR_CHECK_PATH
    assert spec.form_keys == ticket_facts.QR_CHECK_FIELDS
    assert spec.constants["appid"] == ticket_facts.QR_APPID


# --------------------------------------------------------------------------- #
# 2. form() 组装（两套后端共用一份实现）
# --------------------------------------------------------------------------- #
def test_form_only_contains_the_declared_keys():
    """防的是：组装时顺手多塞键 —— 那等于**悄悄改了协议**。

    `self` 那套**刻意不带**设备指纹，所以即使值传进来了也不许出现在表单里。
    """
    spec = _spec_with(SELF)
    form = spec.form(uuid="U1", device_id="D1", expiration="E1")
    assert set(form) == set(spec.form_keys)
    assert form == {"uuid": "U1", "appid": "otn", "_json_att": ""}


def test_form_key_order_follows_the_spec():
    """键的顺序按规格走（上游的字段顺序是它自己的，我们照搬）。"""
    spec = _spec_with(UP)
    form = spec.form(uuid="U1", device_id="D1", expiration="E1")
    assert list(form) == list(spec.form_keys)


def test_missing_values_become_empty_strings_not_none():
    """取不到的值一律空串，**不许是** ``None`` 或字符串 ``"None"``。

    上游用 ``cookies.get(...)``，取不到得到 ``None``；本项目统一成空串 ——
    让官方收到字面量 ``"None"`` 只会更难归因。
    """
    spec = _spec_with(UP)
    form = spec.form(uuid="", device_id="", expiration="")
    assert form["RAIL_DEVICEID"] == ""
    assert "None" not in form.values()


# --------------------------------------------------------------------------- #
# 3. 端到端：切换真的换了发出去的请求 ★
# --------------------------------------------------------------------------- #
def test_the_strategy_decides_what_is_posted(mock_server):
    """★ 本域最关键的判据：**真实表单里有没有设备指纹**。

    两套的端点相同、返回值相同，只看返回值分不出切没切 ——
    所以判据必须落在「mock 记下的表单」上。
    """
    mock_server.state.qr_state = "waiting"

    mine = _last_form(mock_server)
    assert mine == {}, "前置条件：一开始不该有 checkqr 记录"

    _api_for(mock_server, _spec_with(SELF)).check_qr_login("UUID-SELF")
    self_form = _last_form(mock_server)
    assert self_form.get("uuid") == "UUID-SELF"
    assert "RAIL_DEVICEID" not in self_form, (
        "默认那套**刻意不**在表单里放设备指纹（真机靠 Cookie 就够）"
    )

    _api_for(
        mock_server, _spec_with(UP), device="DEV-XYZ", expiration="9999"
    ).check_qr_login("UUID-UP")
    up_form = _last_form(mock_server)
    assert up_form.get("uuid") == "UUID-UP"
    assert up_form.get("RAIL_DEVICEID") == "DEV-XYZ", up_form
    assert up_form.get("RAIL_EXPIRATION") == "9999", up_form


def test_the_switch_does_not_change_the_endpoint(mock_server):
    """★ 附带的**结构事实**：切这个域**换的只有请求体，端点不变**。

    因为两套的 ``checkqr`` 端点本来就一致。把这条写下来，
    免得有人以为「切了后端 = 换了一条官方接口」（那是 `login_check` 的情况，不是这里）。
    """
    from ticket_helper.kernel.self.qr_check import SelfQrCheck
    from ticket_helper.kernel.pjialin.qr_check import PjialinQrCheck

    assert SelfQrCheck().spec().path == PjialinQrCheck().spec().path


def test_default_path_does_not_read_device_cookies(mock_server, monkeypatch):
    """防的是：默认那套**平白多出一个依赖**（去读 Cookie）。

    「不读」不是洁癖：读 Cookie 会走会话对象，而默认路径改造前完全不碰它 ——
    多一个依赖就多一个可能的失败点，而它对这个后端毫无用处。
    """
    api = _api_for(mock_server, _spec_with(SELF))
    calls: list[str] = []
    monkeypatch.setattr(api.client, "cookie_of", lambda name: calls.append(name) or "")
    mock_server.state.qr_state = "waiting"
    api.check_qr_login("UUID-NOCOOKIE")
    assert calls == [], f"默认那套不该去读 Cookie，实际读了 {calls}"


def test_upstream_path_reads_device_cookies(mock_server, monkeypatch):
    """反过来：上游那套**必须**去读（它要把值放进表单）。"""
    api = _api_for(mock_server, _spec_with(UP))
    calls: list[str] = []
    monkeypatch.setattr(api.client, "cookie_of", lambda name: calls.append(name) or "")
    mock_server.state.qr_state = "waiting"
    api.check_qr_login("UUID-COOKIE")
    assert sorted(calls) == ["RAIL_DEVICEID", "RAIL_EXPIRATION"]


# --------------------------------------------------------------------------- #
# 4. 开关与默认值
# --------------------------------------------------------------------------- #
def test_switch_actually_swaps_the_spec():
    assert _spec_with(SELF).form_keys != _spec_with(UP).form_keys


def test_default_backend_is_self():
    """**默认保持 `self`**：上游多带两个设备指纹，而本项目不带也真机跑通了。

    「更接近上游」不是换行为的理由 —— 要真机证据（v3 §10-17）。
    """
    assert registry.DEFAULT_BACKENDS[DOMAIN] == SELF, (
        "改默认值 = 换行为。改之前请先补真机证据，并同步 docs 与差分表。"
    )


def test_illegal_backend_name_fails_fast():
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
# 5. 装配
# --------------------------------------------------------------------------- #
def test_composition_root_injects_the_spec(app_context):
    """防的是：装配处忘了注入 —— 于是线上跑的还是默认那套，没人会发现。"""
    from ticket_helper.account.manager import UserConfig
    from ticket_helper.kernel.self.qr_check import SelfQrCheck

    user = app_context.upsert_user(UserConfig(username="qr_probe", password="pw"))
    assert hasattr(user.api, "check_qr_login"), "账号应挂上接口对象"
    assert user.api._qr_spec == SelfQrCheck().spec()  # noqa: SLF001
    assert app_context._build_qr_spec() == SelfQrCheck().spec()  # noqa: SLF001
