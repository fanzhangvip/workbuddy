"""心跳保活域（A15）：**多久保活一次**（AC-03）。

上游的做法（``user/job.py::check_heartbeat``）::

    USER_HEARTBEAT_INTERVAL = 120
    if get_last_heartbeat() and (now - last) < USER_HEARTBEAT_INTERVAL:
        return True          # 还没到点，**什么都不做**
    ... 到期才真去探测（必要时重新登录），成功后记下时间戳 ...

本项目的做法：**独立守护线程**按 ``config.heartbeat`` 周期直接调 ``keep_alive_once()``。

⇒ 两者机制不同、效果相同（都是「每 N 秒保活一次」）。
所以本域**只切「N 是多少」这一个数**，**不切机制** ——
把我们那套换成上游的「循环里判时间戳」，除了让代码更绕之外换不来任何东西。

⚠️ 一处**真实差异**要留着：上游**没有下限**（配 1 秒它就 1 秒一次），
本项目有 ``max(30, ...)` 这个地板。地板是我们加的防护（防止把 12306 打成 DDoS），
所以它属于「本项目规格」而不是「上游规格」—— 换成上游后端时地板会消失，
这件事必须在差分表里看得见，而不是等真机被限流才发现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class HeartbeatSpec:
    """心跳规格（**全是数据**）。"""

    #: 后端名（必须与 `kernel/registry.py` 的注册键一致，由契约测试核对）
    name: str
    #: 期望的保活间隔（秒）
    interval: float
    #: 本项目加的**地板**：实际间隔不低于它。上游为 0（不设下限）。
    min_interval: float = 0.0

    def effective_interval(self) -> float:
        """实际生效的间隔（**唯一一处实现**，两套后端共用）。

        「地板」这件事只能有一处实现：写在配置解析、用户构造、循环里各一份的话，
        以后改地板值必然漏一处，而漏掉的表现是**心跳比预期更频繁**（更招风控）——
        不报错、只是更危险。
        """
        return max(float(self.min_interval), float(self.interval))


@runtime_checkable
class HeartbeatProvider(Protocol):
    """心跳后端。两个后端都必须满足它。"""

    #: 后端名（必须与注册表键一致）
    name: str

    def spec(self) -> HeartbeatSpec:
        """返回心跳规格。"""
        ...
