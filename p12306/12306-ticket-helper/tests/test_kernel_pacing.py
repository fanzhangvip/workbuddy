"""P4 节奏/参数域（`pacing` / `heartbeat`）的契约测试。

每条都要满足「撤掉实现就变红」（`scripts/contract_mutation_check.py -k 节奏`）。

判据分两层：
- **规格层**：两套后端给出的数字（对上游出处，见 `test_pacing_baseline.py`）；
- **装配层**：这些数字**真的传到了限速器/心跳/传输层超时** ——
  只换规格不接线的话，「我切了」会是一句无法查证的话。
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from ticket_helper.config.settings import RateLimitConfig, UserConfig
from ticket_helper.errors import ConfigError
from ticket_helper.kernel import registry
from ticket_helper.kernel.domains.heartbeat import HeartbeatSpec
from ticket_helper.kernel.domains.pacing import PacingSpec
from ticket_helper.ratelimit.limiter import RateLimiter

SELF = "self"
UP = "pjialin"


def _spec_with(domain: str, backend: str, **kwargs: Any) -> Any:
    previous = os.environ.get(registry.ENV_VAR)
    os.environ[registry.ENV_VAR] = f"{domain}={backend}"
    registry.reset_cache()
    try:
        return registry.get(domain, **kwargs).spec()
    finally:
        if previous is None:
            os.environ.pop(registry.ENV_VAR, None)
        else:
            os.environ[registry.ENV_VAR] = previous
        registry.reset_cache()


# --------------------------------------------------------------------------- #
# 1. 规格本身
# --------------------------------------------------------------------------- #
def test_both_domains_are_registered() -> None:
    backends = registry.available_backends()
    assert sorted(backends["pacing"]) == [UP, SELF]
    assert sorted(backends["heartbeat"]) == [UP, SELF]


@pytest.mark.parametrize("domain", ["pacing", "heartbeat"])
def test_default_backend_is_self(domain: str) -> None:
    """默认仍是 `self`，理由在注册表里：上游那套**更激进**（0.5~1 秒一次、无心跳地板），
    换默认值不是「更接近上游」，是**更容易被限流**。"""
    previous = os.environ.pop(registry.ENV_VAR, None)
    registry.reset_cache()
    try:
        assert registry.backend_name(domain) == SELF
    finally:
        if previous is not None:
            os.environ[registry.ENV_VAR] = previous
        registry.reset_cache()


@pytest.mark.parametrize("domain,value", [("pacing", "fast"), ("heartbeat", "slow")])
def test_bad_backend_name_fails_fast(domain: str, value: str) -> None:
    previous = os.environ.get(registry.ENV_VAR)
    os.environ[registry.ENV_VAR] = f"{domain}={value}"
    registry.reset_cache()
    try:
        with pytest.raises(ConfigError) as excinfo:
            registry.backend_name(domain)
        assert value in str(excinfo.value)
    finally:
        if previous is None:
            os.environ.pop(registry.ENV_VAR, None)
        else:
            os.environ[registry.ENV_VAR] = previous
        registry.reset_cache()


def test_pacing_spec_rejects_impossible_numbers() -> None:
    """不合法的参数**在构造时就炸**。

    拖到运行期才发现的话：``interval_max < interval_min`` 会让间隔落在空区间
    （表现为「完全不限速」），``timeout<=0`` 会让每个请求都超时（表现为「网络有问题」）——
    两种症状都指不回这里。
    """
    with pytest.raises(ValueError, match="interval_min"):
        PacingSpec(name="t", interval_min=0.0, interval_max=1.0, timeout=5.0)
    with pytest.raises(ValueError, match="interval_max"):
        PacingSpec(name="t", interval_min=2.0, interval_max=1.0, timeout=5.0)
    with pytest.raises(ValueError, match="timeout"):
        PacingSpec(name="t", interval_min=1.0, interval_max=2.0, timeout=0.0)


# --------------------------------------------------------------------------- #
# 2. 切换点：本项目配置只喂给本项目那套；上游那套忽略它
# --------------------------------------------------------------------------- #
def test_our_config_is_accepted_by_both_backends_and_only_self_uses_it() -> None:
    """★ 装配中心用**同一句** ``registry.get(域, **本项目配置)``，两套后端都要能吃下。

    若按后端分参数（self 吃配置、pjialin 不接受），切换点就跑到了装配中心 ——
    那正是「唯一切换点」要避免的。
    """
    mine = _spec_with("pacing", SELF, interval_min=3.0, interval_max=7.0)
    theirs = _spec_with("pacing", UP, interval_min=3.0, interval_max=7.0)
    assert (mine.interval_min, mine.interval_max) == (3.0, 7.0)
    assert (theirs.interval_min, theirs.interval_max) == (0.5, 1.0), "上游那套应当忽略我们的配置"


def test_heartbeat_backend_ignores_the_account_setting() -> None:
    """心跳同理：给同样的账号配置，只有本项目那套会采纳它。"""
    mine = _spec_with("heartbeat", SELF, interval=600)
    theirs = _spec_with("heartbeat", UP, interval=600)
    assert mine.interval == 600.0
    assert theirs.interval == 120.0


# --------------------------------------------------------------------------- #
# 3. ★ 装配层：数字真的传下去了
# --------------------------------------------------------------------------- #
def test_the_switch_actually_changes_the_limiter() -> None:
    """★ 切 `pacing` 之后，**限速器**的最小/最大间隔真的变了。

    只换规格不接线的话，「切了」不会有任何症状 —— 请求频率跟以前一模一样，
    而使用者以为自己已经用了上游的节奏。
    """
    mine = RateLimiter(RateLimitConfig(), pacing=_spec_with("pacing", SELF))
    theirs = RateLimiter(RateLimitConfig(), pacing=_spec_with("pacing", UP))
    assert (mine._min, mine._max) == (1.0, 8.0)  # noqa: SLF001
    assert (theirs._min, theirs._max) == (0.5, 1.0)  # noqa: SLF001
    assert theirs.interval < mine.interval, "上游那套的起始间隔应当更小"


def test_limiter_without_pacing_keeps_the_old_behaviour() -> None:
    """**一键回退的硬证据**：不注入规格时的行为与改造前逐字段一致。"""
    limiter = RateLimiter(RateLimitConfig(min_interval=2.0, max_interval=6.0))
    assert (limiter._base_interval, limiter._min, limiter._max) == (2.0, 2.0, 6.0)  # noqa: SLF001


def test_the_switch_actually_changes_the_heartbeat_and_the_timeout(mock_server) -> None:
    """★ 切 `heartbeat` / `pacing` 之后，账号的**心跳间隔**与**请求超时**真的变了。

    两处都容易「看着切了、实际没接」：心跳间隔在构造时算一次就再没人看；
    超时是传输层的属性，漏赋值的话切 pacing 只影响了间隔、不影响超时 —— **一半生效**。
    """
    from ticket_helper.account.manager import User
    from ticket_helper.account.manager import User as _User  # noqa: F401

    config = UserConfig(username="hb", password="p", heartbeat=600)

    def build(*, heartbeat_spec: Any, pacing_spec: Any) -> Any:
        return User(
            config,
            base_url=mock_server.base_url,
            heartbeat_spec=heartbeat_spec,
            pacing_spec=pacing_spec,
        )

    mine = build(heartbeat_spec=_spec_with("heartbeat", SELF, interval=600),
                 pacing_spec=_spec_with("pacing", SELF))
    theirs = build(heartbeat_spec=_spec_with("heartbeat", UP, interval=600),
                   pacing_spec=_spec_with("pacing", UP))
    try:
        assert mine.heartbeat_interval == 600
        assert theirs.heartbeat_interval == 120, "上游那套不采纳账号配置"
        assert mine.client.timeout == 12.0
        assert theirs.client.timeout == 5.0
    finally:
        mine.close()
        theirs.close()


def test_heartbeat_floor_still_holds_on_the_default_path(mock_server) -> None:
    """没注入规格时（= 改造前）地板照样在：配置写 10 也会被抬到 30。"""
    from ticket_helper.account.manager import User

    user = User(UserConfig(username="hb2", password="p", heartbeat=10), base_url=mock_server.base_url)
    try:
        assert user.heartbeat_interval == 30
    finally:
        user.close()


def test_switching_heartbeat_removes_the_floor_only_when_asked(mock_server) -> None:
    """★ 「切到上游 = 去掉地板」这件事必须**在差分里看得见**，且只在我们主动切时发生。"""
    from ticket_helper.account.manager import User

    user = User(
        UserConfig(username="hb3", password="p", heartbeat=1),
        base_url=mock_server.base_url,
        heartbeat_spec=HeartbeatSpec(name=UP, interval=120.0, min_interval=0.0),
    )
    try:
        assert user.heartbeat_interval == 120, "上游后端忽略账号配置（也就不再有地板）"
    finally:
        user.close()


# --------------------------------------------------------------------------- #
# 4. 装配中心默认值（走真实 AppContext）
# --------------------------------------------------------------------------- #
def test_app_context_uses_our_own_pacing_by_default(app_context) -> None:
    """默认装配出来的限速器用**本项目**的两个数（config.ratelimit）。"""
    limiter = app_context.rate_limiter
    assert (limiter._min, limiter._max) == (  # noqa: SLF001
        float(app_context.config.ratelimit.min_interval),
        float(app_context.config.ratelimit.max_interval),
    )


def test_composition_root_hands_the_specs_to_the_account_manager(app_context) -> None:
    """★ 防的是：装配中心**造了规格却没往下传**。

    这条与上面那条的区别：上面只看限速器（它由装配中心直接构造），
    这一条看的是**账号那条链**（context → AccountManager → User）。
    漏传的症状是「限速跟着切了、心跳与超时没跟着切」—— 一半生效最难查。
    """
    manager = app_context.accounts
    assert isinstance(manager.pacing_spec, PacingSpec), manager.pacing_spec
    assert manager.pacing_spec.interval_min == float(app_context.config.ratelimit.min_interval)
    assert callable(manager.heartbeat_spec_for), "心跳规格必须按账号解析（可调用对象）"
    # 用同一个账号配置走一遍：默认（self）应当采纳它，且地板仍在
    spec = manager.heartbeat_spec_for(10)
    assert (spec.interval, spec.min_interval) == (10.0, 30.0)
    assert spec.effective_interval() == 30.0
