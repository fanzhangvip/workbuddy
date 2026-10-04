"""查询节奏域（A17）：**多久能发一次请求、一次最多等多久**。

这一域切的是**参数**，不是算法
------------------------------
上游只有「固定区间里取随机值」：

    QUERY_INTERVAL = 1  →  init_interval_by_number(1)  →  {'min': 0.5, 'max': 1.0}
    每轮 sleep(get_interval_num(interval))  =  round(uniform(min, max), 2)
    TIME_OUT_OF_REQUEST = 5

本项目在此基础上多了**自适应**（风控→放大、平稳→回落），上游没有那套
（决策 §A17 原话：**「用它的节奏参数，自适应那套保留」**）。

所以本域**只固定三个数**：``interval_min`` / ``interval_max`` / ``timeout``。
自适应的倍率、回落步长仍来自本项目配置 —— 那不是「两套实现的差异」，
而是「一边根本没有」的能力，**不抽用不上的东西**。

⚠️ 上游那三个值**是从 ``config.py`` 与 ``helpers/func.py`` 逐字切出来的**，
连「一个数字怎么换算成区间」（除以 2）都是**直接 exec 上游函数**算的 ——
不是我按印象写一个公式。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PacingSpec:
    """查询节奏的规格（**全是数据**）。"""

    #: 后端名（必须与 `kernel/registry.py` 的注册键一致，由契约测试核对）
    name: str
    #: 两次查询之间的**最短**间隔（秒）
    interval_min: float
    #: 自适应放大时的**上限**（秒）
    interval_max: float
    #: 单次请求的超时（秒）
    timeout: float

    def __post_init__(self) -> None:
        # 参数本身不合法（min<=0、max<min、超时<=0）在**这里**就炸：
        # 到运行期才发现的话，表现是「请求全超时」或「完全不限速」——
        # 前者像网络问题，后者像被风控，都指不回这里。
        if self.interval_min <= 0:
            raise ValueError(f"{self.name}: interval_min 必须大于 0，收到 {self.interval_min}")
        if self.interval_max < self.interval_min:
            raise ValueError(
                f"{self.name}: interval_max({self.interval_max}) 不能小于 interval_min({self.interval_min})"
            )
        if self.timeout <= 0:
            raise ValueError(f"{self.name}: timeout 必须大于 0，收到 {self.timeout}")


@runtime_checkable
class PacingProvider(Protocol):
    """查询节奏后端。两个后端都必须满足它。"""

    #: 后端名（必须与注册表键一致）
    name: str

    def spec(self) -> PacingSpec:
        """返回节奏规格。"""
        ...
