"""心跳 —— **本项目写法（默认后端）**。

间隔来自 ``config.heartbeat``，**外加一个地板 30 秒**：配置文件里写 1 也会被抬到 30。
地板不是洁癖 —— 心跳是**周期性无人值守请求**，写小了就是把 12306 当压测目标，
而它的反应是限流，症状却表现为「查询偶发失败」（这条路本项目踩过）。

⚠️ 上限**不做**：想设 5 分钟保活一次是使用者的正当选择，不该被我们挡住。
"""

from __future__ import annotations

from ..domains.heartbeat import HeartbeatSpec

#: 与注册表键一致的唯一名字
NAME = "self"

#: 心跳间隔的地板（秒）。**唯一处定义**（别处不许再写 30）。
MIN_INTERVAL = 30.0

#: 配置缺失时的兜底间隔（秒）
DEFAULT_INTERVAL = 300.0


class SelfHeartbeat:
    """本项目写法：``max(30, config.heartbeat)``。"""

    name = NAME

    def __init__(self, *, interval: float = DEFAULT_INTERVAL, min_interval: float = MIN_INTERVAL) -> None:
        self._interval = float(interval)
        self._min = float(min_interval)

    @classmethod
    def from_value(cls, value: object) -> SelfHeartbeat:
        """从配置里的 ``heartbeat`` 值构造（唯一一处搬运）。

        ⚠️ 这里**不做** ``max(30, ...)`` —— 地板在 :meth:`HeartbeatSpec.effective_interval`
        那一处实现。写两处就会漂：一处改了一处没改，心跳频率悄悄变了而没人知道。
        """
        try:
            interval = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL
        return cls(interval=interval)

    def spec(self) -> HeartbeatSpec:
        return HeartbeatSpec(name=NAME, interval=self._interval, min_interval=self._min)
