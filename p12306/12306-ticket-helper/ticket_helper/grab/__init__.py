"""抢票引擎模块。"""

from ticket_helper.grab.engine import (
    STATE_ERROR,
    STATE_GRABBING,
    STATE_IDLE,
    STATE_READY,
    STATE_STOPPED,
    STATE_SUCCESS,
    STATE_TEXT,
    GrabEngine,
    GrabStats,
)
from ticket_helper.grab.strategy import DEFAULT_SEAT_PREFERENCE, PickStrategy

__all__ = [
    "DEFAULT_SEAT_PREFERENCE",
    "GrabEngine",
    "GrabStats",
    "PickStrategy",
    "STATE_ERROR",
    "STATE_GRABBING",
    "STATE_IDLE",
    "STATE_READY",
    "STATE_STOPPED",
    "STATE_SUCCESS",
    "STATE_TEXT",
]
