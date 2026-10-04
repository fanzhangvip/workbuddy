"""视频画面容器。

职责:
- 承载播放后端提供的画面控件，并按窗口尺寸保持比例（黑边填充）
- 双击切换全屏
- 接收拖入的本地文件与地址（拖进来直接播放）
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QLabel, QStackedLayout, QWidget

_PLACEHOLDER_TEXT = "把视频地址粘贴到上方输入框，\n或直接把文件 / 链接拖到这里"
_PLACEHOLDER_STYLE = "color: #9aa0a6; font-size: 14px;"
_MESSAGE_STYLE = "color: #e8eaed; font-size: 13px;"


class VideoWidget(QWidget):
    """黑底画面区。"""

    doubleClicked = Signal()
    filesDropped = Signal(list)   # list[str]，本地路径或 URL 文本

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumSize(320, 180)

        self._placeholder = QLabel(_PLACEHOLDER_TEXT)
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._placeholder.setContentsMargins(24, 16, 24, 16)
        self._placeholder.setStyleSheet(_PLACEHOLDER_STYLE)

        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(self._placeholder)
        self._video_widget: QWidget | None = None

        self.setStyleSheet("background-color: #000;")

    def set_video_widget(self, widget: QWidget) -> None:
        """挂载播放后端的画面控件。

        画面控件会完全覆盖本控件，鼠标事件不会落到本控件上，
        因此必须装事件过滤器来接管双击与拖放。
        """
        if self._video_widget is widget:
            return
        if self._video_widget is not None:
            self._video_widget.removeEventFilter(self)
            self._stack.removeWidget(self._video_widget)
            self._video_widget.setParent(None)
        self._video_widget = widget
        widget.installEventFilter(self)
        # 只接事件、不抢焦点，键盘事件继续冒泡到主窗口
        widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._stack.addWidget(widget)
        self._stack.setCurrentWidget(widget)

    def show_placeholder(self) -> None:
        """回到引导文字（尚未开始播放时）。"""
        self._placeholder.setText(_PLACEHOLDER_TEXT)
        self._placeholder.setStyleSheet(_PLACEHOLDER_STYLE)
        self._stack.setCurrentWidget(self._placeholder)

    def show_message(self, text: str, title: str = "") -> None:
        """在画面区显示一段说明（如具体失败原因）。

        画面区在播放失败时本来就是空的，把原因显示在这里比只弹一个对话框
        更不容易被忽略——用户关掉弹窗后仍能看到问题出在哪。
        """
        body = f"{title}\n\n{text}" if title else text
        self._placeholder.setText(body)
        self._placeholder.setStyleSheet(_MESSAGE_STYLE)
        self._stack.setCurrentWidget(self._placeholder)

    def show_video(self) -> None:
        """切到画面（开始播放时）。"""
        if self._video_widget is not None:
            self._stack.setCurrentWidget(self._video_widget)

    # ------------------------------------------------------------ 事件
    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 命名
        if obj is self._video_widget:
            etype = event.type()
            if etype == QEvent.Type.MouseButtonDblClick:
                self.doubleClicked.emit()
                return True
            if etype == QEvent.Type.DragEnter:
                return self._handle_drag_enter(event)
            if etype == QEvent.Type.Drop:
                return self._handle_drop(event)
        return super().eventFilter(obj, event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if not self._handle_drag_enter(event):
            super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        if not self._handle_drop(event):
            super().dropEvent(event)

    # ------------------------------------------------------------ 拖放
    def _handle_drag_enter(self, event) -> bool:
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasText():
            event.acceptProposedAction()
            return True
        return False

    def _handle_drop(self, event) -> bool:
        mime = event.mimeData()
        payload: list[str] = []
        if mime.hasUrls():
            for url in mime.urls():
                local = url.toLocalFile()
                payload.append(local or url.toString())
        elif mime.hasText():
            payload = [line.strip() for line in mime.text().splitlines() if line.strip()]
        if not payload:
            return False
        self.filesDropped.emit(payload)
        event.acceptProposedAction()
        return True
