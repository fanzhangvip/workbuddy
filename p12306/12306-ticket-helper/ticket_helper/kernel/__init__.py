"""候补请求体构造的**可切换内核门面**（灰度开关）。

统一说明
--------
- 功能：产生 ``confirmHB`` 的表单与 ``passengerInfo``；
- 默认后端：``testerSunshine``（上游 ``testerSunshine/12306``，MIT，**有完整候补实现**）。
  依据：需求方 2026-09-29 决策 B —— 「上游已有的功能禁止自己实现」，
  而 `pjialin/py12306` **没有候补**（``afterNate``/``confirmHB`` 全仓 0 处）。
- 一键回退：``TICKET_HELPER_KERNEL_STANDBY_PAYLOAD=self`` → 回到改造前的写法。

为什么只抽这一层
----------------
候补七步的**路径** 6/7 与上游一致；**请求体**有 4 处分歧（F1–F4），全部在这一层。
其余六步没有可切换的内容 —— **不抽用不上的东西**。

传输层（风控 HTML 识别 / 限速 / 会话 / 扫码登录）**不在这条门面上**，
也不允许通过本开关切换：上游对这几项是零实现，换过去会同时失去登录与风控识别。

依据：``docs/内核替换对照报告.md``、``docs/内核门面架构设计.md``、
``docs/pjialin内核替换能力清点与方案v2.md``。
"""

from __future__ import annotations

import os

from ticket_helper.errors import ConfigError

from .base import StandbyPayloadBuilder, build_jz_param
from .self_builder import SelfStandbyPayloadBuilder
from .testerSunshine_builder import TesterSunshineStandbyPayloadBuilder

__all__ = [
    "ENV_VAR",
    "DEFAULT_BACKEND",
    "ROLLBACK_BACKEND",
    "StandbyPayloadBuilder",
    "SelfStandbyPayloadBuilder",
    "TesterSunshineStandbyPayloadBuilder",
    "available_backends",
    "build_jz_param",
    "get_standby_payload",
]

#: 功能域注册表（站点表 / 打码 …）**刻意不在本模块顶层 import**：
#: 那会立刻触发 `registry → kernel/self/* → inter/` 这条链，
#: 而 `inter/__init__.py` 又会反过来
#: ``from ticket_helper.kernel import build_jz_param, get_standby_payload`` ——
#: 此时本模块尚未执行完，导入会直接失败（顺序性 bug，只在特定 import 顺序下炸）。
#:
#: 用法：``from ticket_helper.kernel import registry``（导入子模块，天然按需），
#: 再 ``registry.get("captcha", endpoint=...)``。
#: 不要写 ``from ticket_helper import kernel`` + ``kernel.registry`` ——
#: 后者依赖「有人先 import 过子模块」，是隐性耦合。

#: 灰度开关的环境变量名（唯一处定义）
ENV_VAR = "TICKET_HELPER_KERNEL_STANDBY_PAYLOAD"

#: 默认后端 = 上游实现（决策 B）。**唯一处定义**：改它等于改行为与测试基线。
DEFAULT_BACKEND = TesterSunshineStandbyPayloadBuilder.name

#: 一键回退用的后端名（写在这里，免得各处硬写字符串）
ROLLBACK_BACKEND = SelfStandbyPayloadBuilder.name

_BUILDERS: dict[str, StandbyPayloadBuilder] = {
    SelfStandbyPayloadBuilder.name: SelfStandbyPayloadBuilder(),
    TesterSunshineStandbyPayloadBuilder.name: TesterSunshineStandbyPayloadBuilder(),
}


def available_backends() -> tuple[str, ...]:
    """可选后端名（供界面/脚本展示，避免各处硬写字符串）。"""
    return tuple(_BUILDERS)


def get_standby_payload() -> StandbyPayloadBuilder:
    """取当前生效的候补请求体构造器。

    非法值**快速失败**（而不是静默回退到默认后端）：
    静默回退会让「我明明切了 X」变成一句无法查证的猜测。

    后端名**大小写敏感**：它是标识符，不是给人读的文案；
    大小写不敏感会让人以为 ``Self`` 和 ``self`` 是两个东西。
    """
    name = (os.environ.get(ENV_VAR) or DEFAULT_BACKEND).strip()
    builder = _BUILDERS.get(name)
    if builder is None:
        raise ConfigError(
            f"{ENV_VAR} 取值非法: {name!r}；可选 {', '.join(_BUILDERS)}"
        )
    return builder
