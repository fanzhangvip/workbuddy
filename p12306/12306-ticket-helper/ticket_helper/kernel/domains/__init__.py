"""功能域的 Protocol 集合。

每个功能域一个文件，只放**接口**（Protocol + 数据形状 + 共享纯函数），不放实现。
实现放在 `kernel/self/`（本项目）与 `kernel/pjialin/`（上游）。

依据：``docs/pjialin内核化总体架构设计方案v3.md`` §4.2。
"""

from __future__ import annotations

from .captcha import KIND_LOGIN, KIND_ORDER, SUPPORTED_KINDS, CaptchaSolver, format_positions
from .heartbeat import HeartbeatProvider, HeartbeatSpec
from .pacing import PacingProvider, PacingSpec
from .query_route import LeftTicketRoute, dedupe
from .order_submit import (
    STEPS,
    OrderFormsSpec,
    OrderStepForm,
    OrderSubmitter,
    build_form,
)
from .qr_check import QrCheckBuilder, QrCheckSpec
from .slide import SlideResult, SlideSolver, SlideSpec, needs_slide
from .login_check import (
    ALIVE,
    DEAD,
    UNKNOWN,
    LoginProbe,
    LoginProbeSpec,
    alive_of,
)
from .station import StationTable, rank_candidates

__all__ = [
    "KIND_LOGIN",
    "KIND_ORDER",
    "SUPPORTED_KINDS",
    "CaptchaSolver",
    "HeartbeatProvider",
    "HeartbeatSpec",
    "PacingProvider",
    "PacingSpec",
    "ALIVE",
    "DEAD",
    "LoginProbe",
    "LoginProbeSpec",
    "LeftTicketRoute",
    "OrderFormsSpec",
    "OrderStepForm",
    "OrderSubmitter",
    "QrCheckBuilder",
    "QrCheckSpec",
    "STEPS",
    "SlideResult",
    "SlideSolver",
    "SlideSpec",
    "needs_slide",
    "UNKNOWN",
    "StationTable",
    "alive_of",
    "build_form",
    "dedupe",
    "format_positions",
    "rank_candidates",
]
