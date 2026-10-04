"""本项目后端（`self`）：适配 `ticket_helper/inter/`。

**为什么这个包可以 import `inter/`**
-----------------------------------
依赖铁律 R1 说「`kernel/` 不得 import `inter/`」，那一条保护的是
**门面**（`domains/` 与 `registry.py`）—— 门面一旦依赖具体实现，切换就失去意义。

而 `kernel/self/` 的职责**就是**适配本项目实现，它必须 import `inter/`。
所以规则精确化为（由 `tests/test_kernel_facade.py` 与 `tests/test_discipline.py` 机械核对）：

================================  ==========================  ==============================
目录                               可以 import `inter/` 吗      理由
================================  ==========================  ==============================
``kernel/domains/``               ❌                          门面必须与实现解耦
``kernel/registry.py``            ❌                          同上
``kernel/self/``                  ✅ **可以**                  它就是 inter 的适配器
``kernel/pjialin/``               ❌（可 import `py12306`）     它适配的是上游
``kernel/*.py``（顶层：候补门面）   ❌                          同上
================================  ==========================  ==============================
"""

from __future__ import annotations

from .captcha import SelfCaptchaSolver
from .heartbeat import SelfHeartbeat
from .login_check import SelfLoginProbe
from .pacing import SelfPacing
from .order_submit import SelfOrderSubmitter
from .qr_check import SelfQrCheck
from .slide import SelfSlideSolver
from .query_route import SelfQueryRoute
from .station import SelfStationTable

__all__ = [
    "SelfCaptchaSolver",
    "SelfHeartbeat",
    "SelfPacing",
    "SelfLoginProbe",
    "SelfOrderSubmitter",
    "SelfQrCheck",
    "SelfQueryRoute",
    "SelfSlideSolver",
    "SelfStationTable",
]
