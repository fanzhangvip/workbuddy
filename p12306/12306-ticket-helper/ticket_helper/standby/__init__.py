"""智能候补模块。"""

from ticket_helper.standby.engine import (
    STANDBY_CANCELLED,
    STANDBY_FAILED,
    STANDBY_QUEUING,
    STANDBY_STATES,
    STANDBY_STOPPED,
    STANDBY_SUCCESS,
    STANDBY_TERMINAL_STATES,
    StandbyEngine,
    StandbyPlan,
)

__all__ = [
    "STANDBY_CANCELLED",
    "STANDBY_FAILED",
    "STANDBY_QUEUING",
    "STANDBY_STATES",
    "STANDBY_STOPPED",
    "STANDBY_SUCCESS",
    "STANDBY_TERMINAL_STATES",
    "StandbyEngine",
    "StandbyPlan",
]
