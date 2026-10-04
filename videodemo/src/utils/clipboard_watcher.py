"""剪贴板监听：复制了视频链接就提示。

## 为什么不能"一变化就弹窗"

剪贴板是个高频变化的东西——用户复制一段文字、复制一个文件名、复制代码，
全都算变化。如果每次都弹提示，这个功能会立刻变成噪音，用户第一件事就是关掉它。
所以这里做了三层过滤：

1. **必须是能播放的地址**：用 `url_router.classify` 判断，UNKNOWN 直接忽略
   （`.mp4`/`.m3u8`/`rtsp://`/已知视频站点域名 才算数）；
2. **去重**：和上次提示过的内容一样就不再提示（复制同一链接两次只提示一次）；
3. **长度与形态上限**：超过 2048 字符、或含换行的多行文本一律不算地址
   （复制一整篇文章时里面总会带几个链接，但那不是"用户想播放这条"）。

判定为"用户想播放"之后，是否自动填进地址栏由 `K_CLIPBOARD_AUTOFILL` 决定：
开了就直接填入（不自动播放），关了则只在状态栏提示一句，让用户自己点。
"""

from __future__ import annotations

import hashlib

from PySide6.QtCore import QObject, Signal

from src.core.url_router import UrlKind, classify
from src.utils.logger import get_logger

log = get_logger(__name__)

#: 超过这个长度的文本一律不当作地址（多半是复制了整篇文章）
MAX_LENGTH = 2048

#: 不提示的类别：判断不出类型的东西提示了也没用
_IGNORED_KINDS = frozenset({UrlKind.UNKNOWN})


def looks_like_playable(text: str) -> bool:
    """这段文本是否像"一个可以播放的地址"。"""
    candidate = (text or "").strip()
    if not candidate or len(candidate) > MAX_LENGTH:
        return False
    # 多行文本不是"一个地址"（拖进来的多文件走另一条路径）
    if "\n" in candidate or "\r" in candidate:
        return False
    # 本地文件也要能真的存在才提示，否则复制一个普通句子里的相对路径会误报
    kind = classify(candidate)
    if kind in _IGNORED_KINDS:
        return False
    if kind is UrlKind.LOCAL:
        import os

        return os.path.exists(candidate)
    # 必须真的带协议头，避免把 "foo/bar.mp4" 这种相对路径当成地址
    return "://" in candidate or candidate.lower().startswith("file:")


class ClipboardWatcher(QObject):
    """监听剪贴板里的可播放地址。"""

    #: 发现一个新的、像样的播放地址（参数是规范前的原始文本）
    urlDetected = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._enabled = False
        self._connected = False
        self._last_digest = ""

    # ------------------------------------------------------------ 开关
    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """开启/关闭监听。重复调用安全。"""
        enabled = bool(enabled)
        if enabled == self._enabled and self._connected:
            return
        self._enabled = enabled

        if enabled:
            self._connect()
        else:
            self._disconnect()
        log.info("剪贴板监听%s", "已开启" if enabled else "已关闭")

    def _clipboard(self):
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return None
        return app.clipboard()

    def _connect(self) -> None:
        if self._connected:
            return
        board = self._clipboard()
        if board is None:
            log.warning("没有 QApplication，剪贴板监听无法开启")
            self._enabled = False
            return
        board.dataChanged.connect(self._on_changed)
        self._connected = True
        # 打开时如果剪贴板里已经有一条可用地址，直接提示（省一次复制）
        self.check_now()

    def _disconnect(self) -> None:
        if not self._connected:
            return
        board = self._clipboard()
        if board is not None:
            try:
                board.dataChanged.disconnect(self._on_changed)
            except (RuntimeError, TypeError):
                pass
        self._connected = False

    # ------------------------------------------------------------ 回调
    def _on_changed(self) -> None:
        if not self._enabled:
            return
        self.check_now()

    def check_now(self) -> bool:
        """立刻检查一次剪贴板。返回是否触发了提示。

        关闭状态下**永不**发信号：这个约定要硬，因为以后很可能加一条
        "平台不发 dataChanged 时用定时器兜底"的轮询路径，
        那时如果 `check_now` 不自己把关，就会在用户关掉功能后照样弹提示。
        """
        if not self._enabled:
            return False
        text = self._read_text()
        if not looks_like_playable(text):
            return False

        digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
        if digest == self._last_digest:
            # 同一条内容重复出现（包括我们自己填入地址栏引发的联动）不再提示
            return False
        self._last_digest = digest

        log.info("剪贴板里发现可播放地址: %s", text[:120])
        self.urlDetected.emit(text)
        return True

    def _read_text(self) -> str:
        board = self._clipboard()
        if board is None:
            return ""
        try:
            return board.text()
        except Exception:  # noqa: BLE001 - 剪贴板被别的进程占用时可能抛异常
            return ""

    def forget(self) -> None:
        """清掉"上次提示过"的记忆（用户手动点过之后再复制同一条仍会提示）。"""
        self._last_digest = ""
