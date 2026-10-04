"""下载任务面板。

任务列表用 QTreeWidget + 单元格内嵌 QProgressBar：下载是个"过程型"操作，
进度条比百分比数字更容易一眼看懂还剩多少。

面板不做任何下载逻辑，只是 `DownloadManager` 的一面镜子——所有状态都从
manager 的信号推过来，UI 侧不自己维护一份状态，也就不存在两边不一致的问题。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.downloader import (
    DownloadManager,
    DownloadState,
    DownloadTask,
    format_size,
)
from src.utils.logger import get_logger

log = get_logger(__name__)

#: 表格列
_COLUMNS = ("名称", "进度", "大小", "状态")


class DownloadsDialog(QDialog):
    """下载任务列表。"""

    #: 请求下载某个地址（重试时用）
    retryRequested = Signal(object)   # DownloadTask

    def __init__(self, manager: DownloadManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle("下载")
        self.resize(760, 420)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(_COLUMNS))
        self.tree.setHeaderLabels(list(_COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setColumnWidth(1, 200)

        self.label_summary = QLabel("")
        self.label_summary.setStyleSheet("color: palette(mid);")

        self.btn_open_dir = QPushButton("打开下载目录")
        self.btn_open_file = QPushButton("打开文件")
        self.btn_cancel = QPushButton("取消选中")
        self.btn_retry = QPushButton("重试选中")
        self.btn_clear = QPushButton("清除已结束")
        self.btn_cancel_all = QPushButton("全部取消")

        actions = QHBoxLayout()
        for button in (
            self.btn_open_dir, self.btn_open_file, self.btn_cancel,
            self.btn_retry, self.btn_clear, self.btn_cancel_all,
        ):
            actions.addWidget(button)
        actions.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.label_summary)
        layout.addLayout(actions)
        layout.addWidget(buttons)

        self.btn_open_dir.clicked.connect(self._open_dir)
        self.btn_open_file.clicked.connect(self._open_file)
        self.btn_cancel.clicked.connect(self._cancel_selected)
        self.btn_retry.clicked.connect(self._retry_selected)
        self.btn_clear.clicked.connect(self._clear_finished)
        self.btn_cancel_all.clicked.connect(self.manager.cancel_all)

        manager.taskAdded.connect(self._on_added)
        manager.taskUpdated.connect(self._on_updated)
        manager.taskFinished.connect(self._on_updated)

        for task in manager.tasks():
            self._on_added(task)
        self._refresh_summary()

    # ------------------------------------------------------------ 行管理
    def _row_of(self, task_id: int) -> int:
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            if item is not None and item.data(0, Qt.ItemDataRole.UserRole) == task_id:
                return row
        return -1

    def _on_added(self, task: object) -> None:
        if not isinstance(task, DownloadTask):
            return
        item = QTreeWidgetItem()
        item.setData(0, Qt.ItemDataRole.UserRole, task.task_id)
        item.setText(0, task.title or task.dest.name)
        item.setToolTip(0, f"{task.url}\n→ {task.dest}")
        item.setText(1, "")
        item.setText(2, "")
        item.setText(3, "等待中")
        self.tree.addTopLevelItem(item)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(True)
        self.tree.setItemWidget(item, 1, bar)

        self._update_row(task)
        self._refresh_summary()

    def _on_updated(self, task: object) -> None:
        if not isinstance(task, DownloadTask):
            return
        self._update_row(task)
        self._refresh_summary()

    def _update_row(self, task: DownloadTask) -> None:
        row = self._row_of(task.task_id)
        if row < 0:
            self._on_added(task)
            return
        item = self.tree.topLevelItem(row)
        if item is None:
            return

        widget = self.tree.itemWidget(item, 1)
        bar = widget if isinstance(widget, QProgressBar) else None

        if task.state == DownloadState.DONE:
            if bar is not None:
                bar.setRange(0, 100)
                bar.setValue(100)
                bar.setFormat("完成")
        elif task.state in (DownloadState.CANCELLED, DownloadState.FAILED):
            if bar is not None:
                bar.setFormat("已取消" if task.state == DownloadState.CANCELLED else "失败")
        elif task.total > 0:
            if bar is not None:
                bar.setRange(0, 100)
                bar.setValue(int(task.percent))
                bar.setFormat(f"{task.percent:.1f}%")
        else:
            # 服务端没给长度：用不确定态的滚动条，比一个永远 0% 的进度条诚实
            if bar is not None and bar.maximum() != 0:
                bar.setRange(0, 0)
                bar.setFormat("下载中（总大小未知）")

        size_text = format_size(task.received)
        if task.total > 0:
            size_text += f" / {format_size(task.total)}"
        item.setText(2, size_text)

        status = task.progress_text()
        if task.note and task.state in (DownloadState.RUNNING, DownloadState.DONE):
            status += f" · {task.note}"
        item.setText(3, status)
        item.setToolTip(3, task.error or status)

    # ------------------------------------------------------------ 汇总
    def _refresh_summary(self) -> None:
        tasks = self.manager.tasks()
        active = sum(1 for t in tasks if t.state in DownloadState.ACTIVE)
        done = sum(1 for t in tasks if t.state == DownloadState.DONE)
        failed = sum(1 for t in tasks if t.state == DownloadState.FAILED)
        self.label_summary.setText(
            f"共 {len(tasks)} 个任务 · 进行中 {active} · 已完成 {done} · 失败 {failed}"
            f"　下载目录：{self.manager.download_dir}"
        )

    # ------------------------------------------------------------ 选中项
    def _selected_tasks(self) -> list[DownloadTask]:
        result: list[DownloadTask] = []
        for item in self.tree.selectedItems():
            task = self.manager.task(int(item.data(0, Qt.ItemDataRole.UserRole)))
            if task is not None:
                result.append(task)
        return result

    def _cancel_selected(self) -> None:
        for task in self._selected_tasks():
            if task.state in DownloadState.ACTIVE:
                self.manager.cancel(task.task_id)
        self._refresh_summary()

    def _retry_selected(self) -> None:
        picked = [t for t in self._selected_tasks() if t.state in DownloadState.FINISHED]
        if not picked:
            QMessageBox.information(self, "重试", "请先选中已结束（完成 / 失败 / 已取消）的任务。")
            return
        for task in picked:
            self.manager.start(
                task.url,
                title=task.title,
                headers=task.headers,
                is_web=task.is_web,
                # 复用原来的目标路径：留着 .part 才能接着下，而不是从零再下一遍
                dest=task.dest,
            )
        self._refresh_summary()

    def _clear_finished(self) -> None:
        removed = self.manager.clear_finished()
        if not removed:
            return
        for row in range(self.tree.topLevelItemCount() - 1, -1, -1):
            item = self.tree.topLevelItem(row)
            if item is None:
                continue
            task = self.manager.task(int(item.data(0, Qt.ItemDataRole.UserRole)))
            if task is None:
                self.tree.takeTopLevelItem(row)
        self._refresh_summary()

    # ------------------------------------------------------------ 打开
    def _open_dir(self) -> None:
        self._open_path(self.manager.download_dir)

    def _open_file(self) -> None:
        tasks = self._selected_tasks()
        if not tasks:
            QMessageBox.information(self, "打开文件", "请先选中一个任务。")
            return
        task = tasks[0]
        if not task.dest.is_file():
            QMessageBox.information(
                self, "打开文件", f"文件还不存在（下载未完成或已失败）：\n{task.dest}"
            )
            return
        self._open_path(task.dest)

    @staticmethod
    def _open_path(path: Path) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            log.warning("打开 %s 失败: %s", path, exc)
