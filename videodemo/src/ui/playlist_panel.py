"""播放列表面板：播放列表 + 历史记录 两个标签页。

拖拽排序的实现在 `PlaylistView.dropEvent`：视图算出"哪几行拖到了哪一行"，
再交给 `PlaylistModel.move_rows`。这样模型层不依赖 Qt 的拖放细节，
排序逻辑（本项目最容易写出边界 bug 的地方）可以单独写单元测试。
"""

from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.playlist import PlaylistModel
from src.utils.logger import get_logger

log = get_logger(__name__)

#: 正在播放的行前面显示的标记
NOW_PLAYING_MARK = "▶"


class _RowDelegate(QStyledItemDelegate):
    """一行两段式显示：左边标题（会省略），右边灰字说明（类型 / 时长）。

    用 delegate 而不是往 DisplayRole 里拼字符串，是为了让说明文字右对齐且更淡——
    长标题截断后仍能一眼看出这是哪种地址、多长。
    """

    def __init__(self, view: "PlaylistView") -> None:
        super().__init__(view)
        self._view = view

    def paint(self, painter, option, index) -> None:  # noqa: ANN001 - Qt 签名
        # 交给基类画背景、选中态、焦点框，我们只覆盖文字部分
        style_option = option
        self.initStyleOption(style_option, index)
        style_option.text = ""
        style = style_option.widget.style() if style_option.widget else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, style_option, painter)

        model = index.model()
        title = str(index.data(PlaylistModel.TitleRole) or "")
        note = str(index.data(PlaylistModel.NoteRole) or "")
        url = str(index.data(PlaylistModel.UrlRole) or "")
        if url and url == self._view.playing_url():
            title = f"{NOW_PLAYING_MARK} {title}"

        rect = option.rect.adjusted(6, 0, -6, 0)
        metrics = option.fontMetrics

        # 右侧说明先量宽度，剩下的留给标题
        note_width = 0
        if note:
            note_width = metrics.horizontalAdvance(note) + 12

        painter.save()
        palette = option.palette
        note_color = palette.color(QPalette.ColorRole.PlaceholderText)

        if note:
            note_rect = rect.adjusted(rect.width() - note_width, 0, 0, 0)
            painter.setPen(note_color)
            painter.drawText(
                note_rect,
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                metrics.elidedText(note, Qt.TextElideMode.ElideRight, note_width),
            )

        title_rect = rect.adjusted(0, 0, -note_width, 0)
        font = QFont(option.font)
        if url and url == self._view.playing_url():
            font.setBold(True)
        painter.setFont(font)
        # 优先用系统的高亮前景色，保证在深/浅色主题下都看得清
        color = palette.color(
            QPalette.ColorRole.HighlightedText
            if option.state & QStyle.StateFlag.State_Selected
            else QPalette.ColorRole.Text
        )
        painter.setPen(color)
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(title, Qt.TextElideMode.ElideMiddle, max(20, title_rect.width())),
        )
        painter.restore()

    def sizeHint(self, option, index) -> QSize:  # noqa: N802, ANN001 - Qt 签名
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(24, size.height() + 4))


class PlaylistView(QListView):
    """支持内部拖拽排序、也接受外部文件/URL 拖入的列表视图。"""

    #: 内部拖拽结束：源行号列表, 插入位置（原始行号，等于行数表示末尾）
    rowsDropped = Signal(list, int)
    #: 从外部拖入的文件路径或地址
    filesDropped = Signal(list)
    #: 双击 / 回车
    rowActivated = Signal(int)
    #: 右键菜单里的动作
    removeRequested = Signal(list)
    clearRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._playing_url = ""

        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setUniformItemSizes(True)
        self.setAlternatingRowColors(True)
        self.setItemDelegate(_RowDelegate(self))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

        self.doubleClicked.connect(lambda index: self.rowActivated.emit(index.row()))

    # ------------------------------------------------------------ 播放标记
    def playing_url(self) -> str:
        return self._playing_url

    def set_playing_url(self, url: str) -> None:
        if url == self._playing_url:
            return
        self._playing_url = url or ""
        viewport = self.viewport()
        if viewport is not None:
            viewport.update()

    # ------------------------------------------------------------ 选中行
    def selected_rows(self) -> list[int]:
        return sorted({index.row() for index in self.selectionModel().selectedRows()})

    def rows_from_mime(self, mime) -> list[int]:  # noqa: ANN001
        model = self.model()
        if isinstance(model, PlaylistModel):
            return PlaylistModel.rows_from_mime(mime)
        return []

    # ------------------------------------------------------------ 拖放
    def dropEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        mime = event.mimeData()

        # 外部拖入（文件 / 网页链接）：内部拖拽走的是自定义 MIME，不会带 urls
        if mime is not None and mime.hasUrls() and not mime.hasFormat(PlaylistModel.MIME_TYPE):
            targets: list[str] = []
            for url in mime.urls():
                local = url.toLocalFile()
                targets.append(local or url.toString())
            if targets:
                self.filesDropped.emit(targets)
                event.acceptProposedAction()
                return

        rows = self.rows_from_mime(mime)
        if not rows:
            rows = self.selected_rows()
        if not rows:
            event.ignore()
            return

        self.rowsDropped.emit(rows, self._drop_row(event))
        event.acceptProposedAction()

    def _drop_row(self, event) -> int:  # noqa: ANN001
        """把拖放位置换算成"插入到第几行之前"。"""
        position = event.position().toPoint() if hasattr(event, "position") else event.pos()
        model = self.model()
        total = model.rowCount() if model is not None else 0

        index = self.indexAt(position)
        if not index.isValid():
            return total

        row = index.row()
        indicator = self.dropIndicatorPosition()
        # 落在某一行的下半部分 / 正中间，都视为"放到这一行之后"
        if indicator in (
            QAbstractItemView.DropIndicatorPosition.BelowItem,
            QAbstractItemView.DropIndicatorPosition.OnItem,
        ):
            row += 1
        return row

    # ------------------------------------------------------------ 右键菜单
    def _show_menu(self, pos: QPoint) -> None:
        rows = self.selected_rows()
        menu = QMenu(self)
        if rows:
            act_remove = menu.addAction(f"从列表中移除（{len(rows)} 项）")
            act_remove.triggered.connect(lambda: self.removeRequested.emit(rows))
            menu.addSeparator()
        act_clear = menu.addAction("清空列表")
        act_clear.triggered.connect(self.clearRequested)
        menu.exec(self.viewport().mapToGlobal(pos))


class PlaylistPanel(QWidget):
    """播放列表 / 历史记录 面板。"""

    #: 用户要求播放某一条
    playRequested = Signal(object)          # MediaItem
    #: 用户从外部拖入了文件/地址
    filesDropped = Signal(list)
    #: 用户点了「加入列表」（主窗口负责把输入框里的地址加进来）
    addRequested = Signal()
    #: 列表内容发生变化（播放列表为空时主窗口要禁用「上一/下一」）
    playlistChanged = Signal()

    def __init__(
        self,
        playlist: PlaylistModel,
        history: PlaylistModel,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.playlist = playlist
        self.history = history

        self.view_playlist = PlaylistView()
        self.view_playlist.setModel(playlist)

        self.view_history = PlaylistView()
        self.view_history.setModel(history)
        # 历史记录由时间倒序维护，手动排序没有意义，直接关掉拖拽
        self.view_history.setDragEnabled(False)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.view_playlist, "播放列表")
        self.tabs.addTab(self.view_history, "历史记录")
        self.tabs.setDocumentMode(True)

        self.label_empty = QLabel("列表为空。把文件或链接拖进来，或用「加入列表」按钮添加。")
        self.label_empty.setWordWrap(True)
        self.label_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_empty.setEnabled(False)

        self.btn_add = self._button("加入列表", "把输入框里的地址加入播放列表")
        self.btn_remove = self._button("移除", "移除选中的条目")
        self.btn_clear = self._button("清空", "清空当前标签页的列表")
        self.btn_up = self._button("上移", "把选中条目上移一位")
        self.btn_down = self._button("下移", "把选中条目下移一位")

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(4)
        for widget in (self.btn_add, self.btn_remove, self.btn_clear, self.btn_up, self.btn_down):
            buttons.addWidget(widget)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.label_empty)
        layout.addLayout(buttons)

        self._connect()
        self._refresh_empty_hint()

    def _button(self, text: str, tip: str) -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(tip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def _connect(self) -> None:
        for view in (self.view_playlist, self.view_history):
            view.rowActivated.connect(self._on_row_activated)
            view.rowsDropped.connect(self._on_rows_dropped)
            view.filesDropped.connect(self.filesDropped)
            view.removeRequested.connect(self._remove_rows)
            view.clearRequested.connect(self._clear_current)

        self.playlist.itemsChanged.connect(self._on_playlist_changed)
        self.history.itemsChanged.connect(self._refresh_empty_hint)

        self.btn_add.clicked.connect(self._add_from_request)
        self.btn_remove.clicked.connect(lambda: self._remove_rows(self._current_view().selected_rows()))
        self.btn_clear.clicked.connect(self._clear_current)
        self.btn_up.clicked.connect(lambda: self._nudge(-1))
        self.btn_down.clicked.connect(lambda: self._nudge(1))

    # ------------------------------------------------------------ 内部
    def _current_view(self) -> PlaylistView:
        return self.view_playlist if self.tabs.currentIndex() == 0 else self.view_history

    def _current_model(self) -> PlaylistModel:
        return self.playlist if self.tabs.currentIndex() == 0 else self.history

    def _on_row_activated(self, row: int) -> None:
        item = self._current_model().item_at(row)
        if item is not None:
            self.playRequested.emit(item)

    def _on_rows_dropped(self, rows: list[int], insert_before: int) -> None:
        model = self._current_model()
        if model.move_rows(rows, insert_before):
            self._refresh_empty_hint()

    def _remove_rows(self, rows: list[int]) -> None:
        model = self._current_model()
        if model.remove_rows(rows):
            self._refresh_empty_hint()

    def _clear_current(self) -> None:
        self._current_model().clear()
        self._refresh_empty_hint()

    def _add_from_request(self) -> None:
        self.addRequested.emit()

    def _nudge(self, delta: int) -> None:
        """把选中条目上移/下移一位（拖拽之外的键盘友好替代）。"""
        view = self._current_view()
        rows = view.selected_rows()
        if not rows:
            return
        model = self._current_model()
        total = model.rowCount()

        if delta < 0:
            first = rows[0]
            if first == 0:
                return
            model.move_rows(rows, first - 1)
            new_rows = range(first - 1, first - 1 + len(rows))
        else:
            last = rows[-1]
            if last >= total - 1:
                return
            model.move_rows(rows, last + 2)
            new_rows = range(last + 1, last + 1 + len(rows))

        selection = view.selectionModel()
        selection.clearSelection()
        for row in new_rows:
            index = model.index(row, 0)
            if index.isValid():
                selection.select(
                    index,
                    QItemSelectionModel.SelectionFlag.Select
                    | QItemSelectionModel.SelectionFlag.Rows,
                )

    def _on_playlist_changed(self) -> None:
        self._refresh_empty_hint()
        self.playlistChanged.emit()

    def _refresh_empty_hint(self) -> None:
        model = self._current_model()
        empty = model.rowCount() == 0
        self.label_empty.setVisible(empty)
        self.view_playlist.setVisible(not (empty and self.tabs.currentIndex() == 0))
        self.view_history.setVisible(not (empty and self.tabs.currentIndex() == 1))
        has_items = model.rowCount() > 0
        for button in (self.btn_remove, self.btn_clear, self.btn_up, self.btn_down):
            button.setEnabled(has_items)

    # ------------------------------------------------------------ 对外
    def set_playing_url(self, url: str) -> None:
        self.view_playlist.set_playing_url(url)
        self.view_history.set_playing_url(url)

    def select_url(self, url: str) -> None:
        """在播放列表里选中某条（用于「正在播放」时同步高亮）。"""
        row = self.playlist.index_of_url(url)
        if row < 0:
            return
        index = self.playlist.index(row, 0)
        if index.isValid():
            self.view_playlist.setCurrentIndex(index)
