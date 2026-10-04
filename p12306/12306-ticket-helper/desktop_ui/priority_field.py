"""有序多选控件（任务配置：席别优先级）。

为什么不能用勾选框
------------------
席别的**顺序就是抢票优先级**：有票时先要第一个，没有再要第二个。原来的实现是
十个 ``QCheckBox`` 竖着排，标题还写着「从下往上勾选，勾选顺序即优先级」——
这句话是错的，勾选框跟勾选先后毫无关系，取值时永远是按布局顺序扫一遍。
于是「把二等座排在硬座前面」这件事在界面上根本做不到。

这个控件把「顺序」变成看得见、点得动的东西：
- 左边是**会抢的席别**，从上到下就是优先级，选中一行后可以 ↑↓ 调整；
- 右边是**还没选的席别**，双击（或点按钮）加进左边；
- 左边双击一行即移出。

对外用逗号串表达（``text()`` / ``setText()``），与其它表单控件一致。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

#: 两个列表的高度（约 5 行），再多各自滚动
LIST_HEIGHT = 124


class PriorityList(QWidget):
    """左侧有序选择 + 右侧可选池。"""

    changed = Signal()

    def __init__(
        self,
        options: list[str],
        *,
        default: list[str] | None = None,
        picked_title: str = "会抢的席别（从上到下就是优先级）",
        avail_title: str = "可选席别（双击加进去）",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._options = list(options)
        self._values: list[str] = []
        for name in default or []:
            if name in self._options and name not in self._values:
                self._values.append(name)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # ---------- 左：已选（有序） ---------- #
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)
        left_layout.addWidget(self._muted(picked_title, name="fieldLabel"))

        picked_row = QHBoxLayout()
        picked_row.setSpacing(4)
        self.list_picked = QListWidget()
        self.list_picked.setFixedHeight(LIST_HEIGHT)
        self.list_picked.setToolTip("选中一行后可以 ↑↓ 调整优先级；双击一行即移出")
        picked_row.addWidget(self.list_picked, 1)

        moves = QVBoxLayout()
        moves.setSpacing(4)
        self.btn_up = QPushButton("↑")
        self.btn_up.setObjectName("tinyButton")
        self.btn_up.setToolTip("提高优先级（往前挪一位）")
        self.btn_up.clicked.connect(lambda: self.move_selected(-1))
        self.btn_down = QPushButton("↓")
        self.btn_down.setObjectName("tinyButton")
        self.btn_down.setToolTip("降低优先级（往后挪一位）")
        self.btn_down.clicked.connect(lambda: self.move_selected(1))
        self.btn_drop = QPushButton("✕")
        self.btn_drop.setObjectName("tinyButton")
        self.btn_drop.setToolTip("从会抢的席别里移出")
        self.btn_drop.clicked.connect(self.remove_selected)
        for button in (self.btn_up, self.btn_down, self.btn_drop):
            moves.addWidget(button)
        moves.addStretch(1)
        picked_row.addLayout(moves)
        left_layout.addLayout(picked_row)
        root.addWidget(left, 1)

        # ---------- 右：可选 ---------- #
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)
        right_layout.addWidget(self._muted(avail_title))
        self.list_available = QListWidget()
        self.list_available.setFixedHeight(LIST_HEIGHT)
        self.list_available.setToolTip("双击加进左边")
        right_layout.addWidget(self.list_available)
        root.addWidget(right, 1)

        self.list_available.itemDoubleClicked.connect(self._on_available_double_clicked)
        self.list_picked.itemDoubleClicked.connect(self._on_picked_double_clicked)
        # 选中变化时刷新按钮可用状态，避免对着第一行点 ↑ 却毫无反应
        self.list_picked.itemSelectionChanged.connect(self._refresh_buttons)
        self._render()

    # ------------------------------------------------------------------ #
    # 与其它表单控件一致的外部接口
    # ------------------------------------------------------------------ #
    def text(self) -> str:
        return ",".join(self._values)

    def setText(self, text: str) -> None:  # noqa: N802 - 与 QLineEdit 同名
        self._values = []
        for raw in str(text or "").replace("，", ",").replace(";", ",").split(","):
            name = raw.strip()
            if name in self._options and name not in self._values:
                self._values.append(name)
        self._render()

    def clear(self) -> None:
        self._values = []
        self._render()

    def values(self) -> list[str]:
        return list(self._values)

    # ------------------------------------------------------------------ #
    def add(self, name: str) -> bool:
        name = str(name or "").strip()
        if name not in self._options or name in self._values:
            return False
        self._values.append(name)
        self._render()
        return True

    def remove(self, name: str) -> None:
        if name in self._values:
            self._values.remove(name)
            self._render()

    def remove_selected(self) -> None:
        row = self.list_picked.currentRow()
        if 0 <= row < len(self._values):
            self._values.pop(row)
            self._render()

    def move_selected(self, delta: int) -> None:
        """把选中行往前后挪一位（越界则什么都不做）。"""
        row = self.list_picked.currentRow()
        target = row + delta
        if not (0 <= row < len(self._values) and 0 <= target < len(self._values)):
            return
        self._values[row], self._values[target] = self._values[target], self._values[row]
        self._render()
        self.list_picked.setCurrentRow(target)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _muted(text: str, *, name: str = "muted") -> QLabel:
        """小标题。``name`` 用来分级：主区域用 ``fieldLabel``（深一档），
        次区域用 ``muted`` —— 眼睛先看到「会抢的席别」，再看「可选席别」。"""
        label = QLabel(text)
        label.setObjectName(name)
        label.setWordWrap(True)
        return label

    def _on_available_double_clicked(self, item: QListWidgetItem) -> None:
        self.add(item.text())

    def _on_picked_double_clicked(self, item: QListWidgetItem) -> None:
        # 左边显示的是「1. 二等座」这种带序号的文本，真名在 UserRole 里
        name = item.data(Qt.ItemDataRole.UserRole)
        if name:
            self.remove(str(name))

    def _refresh_buttons(self) -> None:
        row = self.list_picked.currentRow()
        total = len(self._values)
        self.btn_up.setEnabled(row > 0)
        self.btn_down.setEnabled(0 <= row < total - 1)
        self.btn_drop.setEnabled(0 <= row < total)

    def _render(self) -> None:
        self.list_picked.clear()
        for index, name in enumerate(self._values, start=1):
            self.list_picked.addItem(QListWidgetItem(f"{index}. {name}"))
        # 顺序号写进文本会让「当前行取名字」变麻烦，这里用 UserRole 存原始名字
        for i in range(self.list_picked.count()):
            self.list_picked.item(i).setData(Qt.ItemDataRole.UserRole, self._values[i])
        if not self._values:
            placeholder = QListWidgetItem("还没有选席别 —— 至少加一个")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_picked.addItem(placeholder)

        self.list_available.clear()
        for name in self._options:
            if name not in self._values:
                self.list_available.addItem(QListWidgetItem(name))
        if self.list_available.count() == 0:
            placeholder = QListWidgetItem("都已加入")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_available.addItem(placeholder)

        self._refresh_buttons()
        self.changed.emit()
