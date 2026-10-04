"""上游后端（`pjialin/py12306`）：适配 `vendor/pjialin/`。

**本包可以 import `py12306`（上游），但不得出现任何 12306 协议字面量**
（路径 / 参数 / 解析）—— 决策 §10-16 要求适配层「只管搬运」。
这条由 `tests/test_discipline.py::test_adapter_layers_contain_no_protocol_details` 机械检查。
"""

from __future__ import annotations

from .captcha import PjialinCaptchaSolver
from .heartbeat import PjialinHeartbeat
from .login_check import PjialinLoginProbe
from .pacing import PjialinPacing
from .order_submit import PjialinOrderSubmitter
from .qr_check import PjialinQrCheck
from .query_route import PjialinQueryRoute
from .shim import install, is_available, upstream_module
from .slide import PjialinSlideSolver
from .station import PjialinStationTable

__all__ = [
    "PjialinCaptchaSolver",
    "PjialinHeartbeat",
    "PjialinPacing",
    "PjialinLoginProbe",
    "PjialinOrderSubmitter",
    "PjialinQrCheck",
    "PjialinQueryRoute",
    "PjialinSlideSolver",
    "PjialinStationTable",
    "install",
    "is_available",
    "upstream_module",
]
