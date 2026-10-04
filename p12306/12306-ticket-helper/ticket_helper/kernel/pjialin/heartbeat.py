"""心跳 —— **上游 pjialin/py12306 写法**。

上游只有一个数：``USER_HEARTBEAT_INTERVAL = 120``（``config.py``，逐字切出），
**没有地板、也没有上限** —— 配置写 1 就真的一秒一次。

⚠️ 所以「切到这个后端」会**去掉本项目的地板**。这不是 bug，是**上游的确切语义**；
它必须留在差分表里被看见（差分表会显示 ``min_interval: 30.0 → 0.0``），
而不是等真机被限流才发现。

⚠️ 机制**不切**：上游是在自己的循环里判「距上次是否 < interval」来节流，
本项目用独立守护线程 ``wait(interval)``。两者效果相同，本域只搬「N 是多少」，
搬机制只会让代码更绕、换不来任何东西（见 `domains/heartbeat.py` 的说明）。
"""

from __future__ import annotations

from ticket_helper.errors import ConfigError

from ..domains.heartbeat import HeartbeatSpec
from .shim import install

#: 与注册表键一致的唯一名字
NAME = "pjialin"


class PjialinHeartbeat:
    """上游写法：``USER_HEARTBEAT_INTERVAL``，不设地板。"""

    name = NAME

    def __init__(self, *, interval: float | None = None) -> None:
        """收下本项目的 ``heartbeat`` 配置并**忽略**（理由同 `pjialin/pacing.py`）。"""
        self._ignored = interval

    def spec(self) -> HeartbeatSpec:
        install()
        import ticket_facts  # noqa: PLC0415  （生成物：位于 sys.path 上的 vendor/pjialin）

        interval = getattr(ticket_facts, "USER_HEARTBEAT_INTERVAL", 0)
        if not interval:
            raise ConfigError(
                "生成物缺少 USER_HEARTBEAT_INTERVAL；请跑 "
                "python scripts/build_vendor_pjialin.py --init 重建"
            )
        # `min_interval=0.0` 是**上游的确切语义**（它没有下限）—— 差异如实保留。
        return HeartbeatSpec(name=NAME, interval=float(interval), min_interval=0.0)
