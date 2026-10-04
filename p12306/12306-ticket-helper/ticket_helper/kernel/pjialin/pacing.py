"""查询节奏 —— **上游 pjialin/py12306 写法**。

三个数都来自生成物（逐字切出），**区间不是我们按印象写的公式**：
直接 ``exec`` 上游 ``helpers/func.py::init_interval_by_number`` 来算 ——

    QUERY_INTERVAL = 1   →   init_interval_by_number(1)   →   {'min': 0.5, 'max': 1.0}

⚠️ 上游那个函数用了 ``random``（``get_interval_num`` 里），所以 exec 的命名空间
**必须**带上 ``random`` —— 少了它会在第一次调用时 ``NameError``，
而那时它长得像「滑块/风控失败」，指不回这里。

⚠️ **上游那套比本项目激进得多**：0.5~1.0 秒一次，而本项目是 1~8 秒且带自适应。
所以这个后端**默认不启用**（默认仍是 `self`）—— 换默认值等于把请求频率提到上游水平，
那不是「更接近上游」，那是**更容易被限流**。
"""

from __future__ import annotations

import random

from ticket_helper.errors import ConfigError

from ..domains.pacing import PacingSpec
from .shim import install

#: 与注册表键一致的唯一名字
NAME = "pjialin"


def _exec_upstream_functions(namespace: dict[str, object]) -> None:
    """把上游那两个函数的**源码文本**exec 进给定命名空间。

    为什么用 exec 而不是「照它的公式自己写一遍」：
    需求是「上游有的禁止自己实现」，而公式抄错一个除数（``number / 2``）
    没人会发现 —— 只是间隔变得不对，而间隔不对**不是错误**，只是更慢或更危险。
    """
    install()
    import ticket_facts  # noqa: PLC0415  （生成物：位于 sys.path 上的 vendor/pjialin）

    for attr in ("INIT_INTERVAL_BY_NUMBER_SOURCE", "GET_INTERVAL_NUM_SOURCE"):
        source = getattr(ticket_facts, attr, "")
        if not source:
            raise ConfigError(
                f"生成物缺少 {attr}；请跑 python scripts/build_vendor_pjialin.py --init 重建"
            )
        exec(source, namespace)  # noqa: S102 - 源码来自 vendor 生成物（逐字切出 + sha256 可校验）


class PjialinPacing:
    """上游写法：``init_interval_by_number(QUERY_INTERVAL)`` + ``TIME_OUT_OF_REQUEST``。"""

    name = NAME

    def __init__(
        self,
        *,
        interval_min: float | None = None,
        interval_max: float | None = None,
        timeout: float | None = None,
    ) -> None:
        """收下本项目的配置值并**忽略**它们。

        为什么收下来：装配中心要用**同一句** ``registry.get(域, **本项目配置)``——
        若按后端分参数，切换点就跑到装配中心去了（那正是「唯一切换点」要避免的）。
        为什么忽略：切到上游的意义就是**用上游的节奏**；用了我们的数就还是我们自己那套。
        """
        self._ignored = (interval_min, interval_max, timeout)

    def spec(self) -> PacingSpec:
        namespace: dict[str, object] = {"random": random}
        _exec_upstream_functions(namespace)
        import ticket_facts  # noqa: PLC0415

        interval = namespace["init_interval_by_number"](ticket_facts.QUERY_INTERVAL)
        if not isinstance(interval, dict):
            raise ConfigError(f"上游 init_interval_by_number 的返回值不是区间字典：{interval!r}")
        return PacingSpec(
            name=NAME,
            interval_min=float(interval["min"]),
            interval_max=float(interval["max"]),
            timeout=float(ticket_facts.TIME_OUT_OF_REQUEST),
        )
