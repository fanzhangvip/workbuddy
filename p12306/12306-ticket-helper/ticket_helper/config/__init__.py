"""配置模块：数据结构、加密、加载与热更新。"""

from ticket_helper.config.crypto import CryptoError, SecretCipher
from ticket_helper.config.loader import ConfigLoader, get_loader
from ticket_helper.config.settings import (
    STANDBY_MAX_COMBOS_PER_ORDER,
    STANDBY_MAX_DATES_PER_ORDER,
    STANDBY_MAX_ORDERS,
    AppConfig,
    ClusterConfig,
    CodecConfig,
    EmailNotifyConfig,
    NotifyConfig,
    RateLimitConfig,
    StorageConfig,
    TaskConfig,
    UserConfig,
    VoiceNotifyConfig,
    WebConfig,
)

__all__ = [
    "AppConfig",
    "ClusterConfig",
    "CodecConfig",
    "ConfigLoader",
    "CryptoError",
    "EmailNotifyConfig",
    "NotifyConfig",
    "RateLimitConfig",
    "STANDBY_MAX_COMBOS_PER_ORDER",
    "STANDBY_MAX_DATES_PER_ORDER",
    "STANDBY_MAX_ORDERS",
    "SecretCipher",
    "StorageConfig",
    "TaskConfig",
    "UserConfig",
    "VoiceNotifyConfig",
    "WebConfig",
    "get_loader",
]
