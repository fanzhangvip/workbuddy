"""通知网关模块。"""

from ticket_helper.notify.channels import (
    Channel,
    DingTalkChannel,
    EmailChannel,
    ServerChanChannel,
    TelegramChannel,
    VoiceChannel,
    WebChannel,
    WeComChannel,
    build_channels,
)
from ticket_helper.notify.gateway import (
    CRITICAL_EVENTS,
    DEFAULT_TEMPLATES,
    EventType,
    NotifyEvent,
    NotifyGateway,
)

__all__ = [
    "CRITICAL_EVENTS",
    "Channel",
    "DEFAULT_TEMPLATES",
    "DingTalkChannel",
    "EmailChannel",
    "EventType",
    "NotifyEvent",
    "NotifyGateway",
    "ServerChanChannel",
    "TelegramChannel",
    "VoiceChannel",
    "WeComChannel",
    "WebChannel",
    "build_channels",
]
