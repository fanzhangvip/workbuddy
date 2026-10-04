"""查询节奏 —— **本项目写法（默认后端）**。

参数来自本项目配置 ``config.ratelimit``（``min_interval`` / ``max_interval``）与
``HttpClient`` 的默认超时；**自适应那套倍率仍留在本项目**（上游没有）。

构造参数走「可调用对象」而不是「配置对象」：本域只要两个数，
拿整个配置进来会让「改了哪个数」变得难查（而且配置是可变对象，
把它冻进缓存键会出幽灵实例）。
"""

from __future__ import annotations

from collections.abc import Callable

from ..domains.pacing import PacingSpec

#: 与注册表键一致的唯一名字
NAME = "self"

#: 本项目的 HTTP 默认超时（与 `inter/base.py::HttpClient` 的默认值一致）。
#: ⚠️ 重复了一个数字是有意的取舍：本域**不许** import `inter/`（分层铁律），
#: 而由 `tests/test_kernel_pacing.py::test_self_timeout_is_the_transport_default`
#: 断言「这个数 == 传输层默认值」—— 重复可以被机器发现，就不会漂。
DEFAULT_TIMEOUT = 12.0


class SelfPacing:
    """本项目写法：``config.ratelimit`` 的两个数 + 传输层默认超时。"""

    name = NAME

    def __init__(
        self,
        *,
        interval_min: float = 1.0,
        interval_max: float = 8.0,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._interval_min = float(interval_min)
        self._interval_max = float(interval_max)
        self._timeout = float(timeout)

    @classmethod
    def from_ratelimit(cls, ratelimit: object) -> SelfPacing:
        """从 ``RateLimitConfig`` 构造（唯一一处「配置 → 规格」的搬运）。"""
        return cls(
            interval_min=float(getattr(ratelimit, "min_interval", 1.0)),
            interval_max=float(getattr(ratelimit, "max_interval", 8.0)),
        )

    def spec(self) -> PacingSpec:
        return PacingSpec(
            name=NAME,
            interval_min=self._interval_min,
            interval_max=self._interval_max,
            timeout=self._timeout,
        )


def build(*, interval_min: float, interval_max: float, timeout: float = DEFAULT_TIMEOUT) -> SelfPacing:
    """给装配中心用的简写（避免它去记构造签名）。"""
    return SelfPacing(interval_min=interval_min, interval_max=interval_max, timeout=timeout)


#: 让类型检查器知道这个模块的可调用形状（装配中心只按它构造）
Builder = Callable[..., SelfPacing]
