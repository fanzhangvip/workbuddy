"""P4 参数级基线（A15 心跳 / A17 限速）：上游那一份**逐项**对出处。

锚点仍是 ``vendor/pjialin/ticket_facts.py``（生成器从上游原件逐字切出，
``--check`` 可复现）。这里比的是「我们 vs 上游」，不是「我们 vs 我们自己的期望值」。

⚠️ A17 的「一个数字怎么换算成区间」（``QUERY_INTERVAL / 2``）**不是我们写死的公式**：
本测试自己 ``exec`` 上游函数重算一遍，再与后端产出的规格比对 ——
所以「适配层真的用了上游实现」这件事是被证明的，不是被声明的。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

from ticket_helper.config.settings import UserConfig
from ticket_helper.inter.base import HttpClient
from ticket_helper.kernel.pjialin.shim import install

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "pjialin"


@pytest.fixture(scope="module")
def facts():
    install()
    sys.path.append(str(VENDOR))  # 只能 append：vendor 里也有 main.py
    try:
        import ticket_facts  # type: ignore[import-not-found]

        yield ticket_facts
    finally:
        sys.path.remove(str(VENDOR))


@pytest.fixture(scope="module")
def upstream_pacing():
    from ticket_helper.kernel.pjialin import PjialinPacing

    return PjialinPacing().spec()


@pytest.fixture(scope="module")
def upstream_heartbeat():
    from ticket_helper.kernel.pjialin import PjialinHeartbeat

    return PjialinHeartbeat().spec()


# --------------------------------------------------------------------------- #
# 一、A17：三个数逐项对上游
# --------------------------------------------------------------------------- #
def test_upstream_facts_are_the_documented_numbers(facts) -> None:
    """上游三个常量（逐字切出）就是文档里写的：1 / 5 / 120。"""
    assert facts.QUERY_INTERVAL == 1
    assert facts.TIME_OUT_OF_REQUEST == 5
    assert facts.USER_HEARTBEAT_INTERVAL == 120


def test_interval_range_is_computed_by_the_upstream_function(facts, upstream_pacing) -> None:
    """★ 区间来自**上游函数本身**（exec 出来重算一遍作对照）。

    这条防的是「适配层顺手自己写一个公式」：0.5/1.0 这两个数**只能**由
    ``init_interval_by_number(QUERY_INTERVAL)`` 得出。抄错一个除数
    （``/2`` 写成 ``/3``）不会报错，只会让节奏不对 —— 而节奏不对不是错误，
    只是更慢或更危险。
    """
    namespace: dict[str, object] = {"random": random}
    exec(facts.INIT_INTERVAL_BY_NUMBER_SOURCE, namespace)  # noqa: S102 - vendor 生成物
    expected = namespace["init_interval_by_number"](facts.QUERY_INTERVAL)
    assert upstream_pacing.interval_min == pytest.approx(float(expected["min"]))
    assert upstream_pacing.interval_max == pytest.approx(float(expected["max"]))
    assert (upstream_pacing.interval_min, upstream_pacing.interval_max) == (0.5, 1.0)


def test_upstream_timeout_matches_the_fact(facts, upstream_pacing) -> None:
    """超时就是 ``TIME_OUT_OF_REQUEST``（不是我们自己填的 12 秒）。"""
    assert upstream_pacing.timeout == float(facts.TIME_OUT_OF_REQUEST) == 5.0


def test_self_timeout_is_the_transport_default() -> None:
    """★ 本项目那份超时**必须等于传输层的默认值**。

    本域不许 import ``inter/``（分层铁律），所以这个 12.0 是**重复写的一个数**。
    重复可以被机器发现，就不会漂 —— 这条就是那个机器检查。
    """
    import inspect

    from ticket_helper.kernel.self.pacing import DEFAULT_TIMEOUT

    signature = inspect.signature(HttpClient.__init__)
    assert DEFAULT_TIMEOUT == float(signature.parameters["timeout"].default)


def test_self_pacing_uses_the_ratelimit_config() -> None:
    """本项目那份的两个数来自 ``config.ratelimit``（不是写死的）。"""
    from ticket_helper.config.settings import RateLimitConfig
    from ticket_helper.kernel.self.pacing import SelfPacing

    spec = SelfPacing.from_ratelimit(RateLimitConfig(min_interval=2.0, max_interval=9.0)).spec()
    assert (spec.interval_min, spec.interval_max) == (2.0, 9.0)


# --------------------------------------------------------------------------- #
# 二、A15：心跳
# --------------------------------------------------------------------------- #
def test_upstream_heartbeat_interval_and_no_floor(facts, upstream_heartbeat) -> None:
    """★ 上游心跳：120 秒，**且没有地板**。

    「没有地板」是上游的确切语义（配 1 秒它就 1 秒一次）。差分表里必须看得见，
    而不是等真机被限流才发现。
    """
    assert upstream_heartbeat.interval == float(facts.USER_HEARTBEAT_INTERVAL) == 120.0
    assert upstream_heartbeat.min_interval == 0.0
    assert upstream_heartbeat.effective_interval() == 120.0


def test_self_heartbeat_has_a_floor_that_the_config_agrees_with() -> None:
    """★ 本项目的地板 30 秒，必须与**配置校验里的那条规则**一致。

    同一件事写在两处（配置校验「<30 不许保存」+ 规格地板「<30 抬到 30」）时，
    两处会漂；漂的表现是**配了 10 秒心跳却不生效**（或反过来被打成 10 秒一次）。
    这条把它们钉在一起。
    """
    from ticket_helper.kernel.self.heartbeat import MIN_INTERVAL, SelfHeartbeat

    assert MIN_INTERVAL == 30.0
    assert UserConfig(username="u", heartbeat=29).validate(), "配置校验应当拒绝 <30 的心跳"
    assert UserConfig(username="u", heartbeat=30).validate() == []
    spec = SelfHeartbeat(interval=1.0).spec()
    assert spec.effective_interval() == MIN_INTERVAL


def test_effective_interval_has_exactly_one_implementation() -> None:
    """地板只在 :meth:`HeartbeatSpec.effective_interval` 里实现一次。

    两个构造点（``interval=1, min=30`` 与 ``interval=30, min=30``）必须给出同一个数 ——
    若谁在别处又写了一遍 ``max(30, ...)``，那条路径的值会与这里不同（或相同却无处校验）。
    """
    from ticket_helper.kernel.domains.heartbeat import HeartbeatSpec

    a = HeartbeatSpec(name="t", interval=1.0, min_interval=30.0).effective_interval()
    b = HeartbeatSpec(name="t", interval=30.0, min_interval=30.0).effective_interval()
    c = HeartbeatSpec(name="t", interval=99.0, min_interval=30.0).effective_interval()
    assert (a, b, c) == (30.0, 30.0, 99.0)
