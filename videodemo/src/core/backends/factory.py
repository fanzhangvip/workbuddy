"""播放后端工厂。

选择规则（按优先级）：
1. 设置里明确选了 mpv，且 mpv 真的可用 → mpv；
2. 设置里没动过，但开了「直播流优先用 mpv」且这条是实时流（rtsp/rtmp）→ mpv；
3. 其余一律用 Qt 内核。

**任何一步失败都回退到 Qt，绝不因为"想用 mpv"而让播放不可用。**
所以这里把 mpv 的构造整个包在 try 里——它要加载外部 mpv-2.dll，
缺库时是在构造阶段才炸，不是 import 阶段。
"""

from __future__ import annotations

from PySide6.QtCore import QObject

from src.core.backends.base import PlayerBackend
from src.core.url_router import UrlKind
from src.utils.logger import get_logger

log = get_logger(__name__)


def mpv_available() -> bool:
    """python-mpv 是否可用（不校验 mpv-2.dll，那要真构造才知道）。"""
    from src.core.backends.mpv_backend import mpv_available as _available

    return _available()


def _prefer_mpv(kind: UrlKind) -> tuple[bool, str]:
    """要不要用 mpv，以及原因（原因用于日志与状态栏提示）。"""
    from src.utils import settings as S

    if not mpv_available():
        return False, ""

    choice = S.get_str(S.K_BACKEND, "qt").lower()
    if choice == "mpv":
        return True, "设置里指定了 mpv 内核"
    if S.get_bool(S.K_PREFER_MPV_FOR_LIVE, False) and kind is UrlKind.LIVE_STREAM:
        return True, "直播流优先使用 mpv 内核"
    return False, ""


def create_backend(kind: UrlKind, parent: QObject | None = None) -> PlayerBackend:
    """按地址类型与配置选择播放后端。"""
    wanted, reason = _prefer_mpv(kind)

    if wanted:
        try:
            from src.core.backends.mpv_backend import MpvBackend

            log.info("使用 mpv 后端播放 %s（%s）", kind.value, reason)
            return MpvBackend(parent)
        except Exception as exc:  # noqa: BLE001 - 后端初始化失败必须能回退
            log.warning("mpv 后端初始化失败，回退到 Qt 后端: %s", exc)

    from src.core.backends.qt_backend import QtMediaBackend

    return QtMediaBackend(parent)


def fallback_to_qt(reason: str) -> PlayerBackend:
    """运行期从 mpv 掉回 Qt 时用（当前由调用方按需使用）。"""
    log.warning("回退到 Qt 后端: %s", reason)
    from src.core.backends.qt_backend import QtMediaBackend

    return QtMediaBackend()


def describe_backends() -> str:
    """后端可用性摘要，供自检、启动日志与设置面板展示。"""
    from src.core.backends.mpv_backend import describe as describe_mpv

    return f"QtMultimedia: 可用 | {describe_mpv()}"
