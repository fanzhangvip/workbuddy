"""任务配置页（UI-03）：可视化配置日期/起止站/车次/席别/乘客/策略。

设计说明
--------
这一页的目标是「**一屏能填完，且知道下一步点哪里**」。为此有三条硬规则：

1. **分组标题自带序号徽章**
   ``① 行程 ② 车次 ③ 席别与乘客 ④ 运行参数``，序号是彩色的圆点，
   标题右边跟一句「这组是干嘛的」。用带边框的 ``QGroupBox`` 时，
   一屏四五个方框套在一起，眼睛没有落点。

2. **两种行型分开用**（这是最省高度的一处判断）
   - **简单控件**（一个输入框 / 下拉 / 数字框）→ 「标签在左、控件在右」一行，
     高度约 34px；一行能放下两个。
   - **复合控件**（自带列表＋按钮行的日期 / 站点 / 车次）→ 「标题在上、控件占满整行」。
     它们一旦塞进窄列，内部输入框会被压到几十像素（实测过 61px，根本没法打字）。
   以前所有字段一律用第二种，连「任务名称」这种一个词的输入框也独占一整行，
   于是表单被拉到 1262px —— 而可视高度只有 800px 上下。

3. **保存条固定在底部，不跟表单滚**
   滚动区高度只给表单；「保存任务 / 清空表单 / 状态提示」放在滚动区外面。
   以前它们长在滚动区末尾，填完得先滚两屏才找得到保存按钮。

页面的字段顺序按「使用者从上往下会怎么填」排，前三组填完就能存；
第 4 组全都有默认值，默认收起 —— 但**编辑已有任务时自动摊开**，
那里面很可能有他上次特意改过的值，收起来会让人以为「我设的东西丢了」。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from desktop_ui.api_client import ApiClient, ApiError
from desktop_ui.priority_field import PriorityList
from desktop_ui.token_field import DateField, StationField, TrainListField

log = logging.getLogger(__name__)

SEAT_TYPES = [
    "商务座", "特等座", "一等座", "二等座",
    "高级软卧", "软卧", "硬卧", "软座", "硬座", "无座",
]
DEFAULT_SEATS = ["二等座"]
TRAIN_TYPES = ["高铁", "动车", "直达", "特快", "快速", "普快", "其他"]
STRATEGIES = [
    ("direct", "direct · 直接抢（有余票立即下单）"),
    ("standby", "standby · 仅候补"),
    ("dual", "dual · 候补 + 捡漏双轨"),
]

#: 「标签在左」那一行里标签列的宽度。定死它，长短字段的控件起点才对齐
LABEL_WIDTH = 76


class SectionHeader(QWidget):
    """分组标题：圆形序号 + 标题 + 右侧一句说明（+ 可选操作按钮）+ 细分隔线。

    为什么不直接用 ``QGroupBox``：它的标题是一个长方形的框线标题，
    四个分组叠起来就是「框套框」，视觉噪音大；而且标题里塞不进「右侧说明 + 按钮」。
    """

    def __init__(
        self,
        index: str,
        title: str,
        hint: str = "",
        *,
        action: QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(7)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(9)

        self.badge = QLabel(index)
        self.badge.setObjectName("stepBadge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setFixedSize(22, 22)
        row.addWidget(self.badge)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("sectionName")
        row.addWidget(self.title_label)

        if hint:
            tip = QLabel(hint)
            tip.setObjectName("hint")
            tip.setWordWrap(False)
            row.addWidget(tip)

        row.addStretch(1)
        if action is not None:
            row.addWidget(action)
        box.addLayout(row)

        rule = QFrame()
        rule.setObjectName("sectionRule")
        rule.setFixedHeight(1)
        box.addWidget(rule)


class TaskConfigPage(QWidget):
    """任务配置。"""

    def __init__(self, api: ApiClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self.editing_id: int | None = None
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(6000)
        self.timer.timeout.connect(self.refresh_list)
        self.timer.start()
        self.refresh_list()

    # ------------------------------------------------------------------ #
    # 骨架
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_form_column())
        splitter.addWidget(self._build_list_column())
        # 分栏比例偏向左侧表单：右栏只是「已有任务」列表，窄一点没关系；
        # 表单里那七个车次类型标签一旦被压窄就会文字截断。
        splitter.setSizes([760, 360])

        # ⚠️ 这一行是 stretch=1：不给的话，底下的状态栏会和主内容区平分高度，
        # 实测状态栏被撑到 434px（它自己的 sizeHint 只有 15px），
        # 于是表单被挤在上半屏、下半屏空一大块 —— 看起来就是「挤」和「空」同时存在。
        root.addWidget(splitter, 1)
        root.addWidget(self._build_action_bar())

    def _build_form_column(self) -> QScrollArea:
        self.form_sheet = QFrame()
        self.form_sheet.setObjectName("formSheet")
        self.form_sheet.setFrameShape(QFrame.Shape.NoFrame)
        column = QVBoxLayout(self.form_sheet)
        column.setContentsMargins(18, 14, 18, 14)
        # 分组**之间**的间距（比组内大得多）：这样「一眼看得出这几项是一组的」
        column.setSpacing(18)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(10)
        self.form_title = QLabel("新建任务")
        self.form_title.setObjectName("pageTitle")
        head.addWidget(self.form_title)
        self.btn_cancel_edit = QPushButton("取消编辑")
        self.btn_cancel_edit.setObjectName("tinyButton")
        self.btn_cancel_edit.setToolTip("放弃修改，回到「新建任务」")
        self.btn_cancel_edit.setVisible(False)
        self.btn_cancel_edit.clicked.connect(self.reset_form)
        head.addWidget(self.btn_cancel_edit)
        head.addStretch(1)
        column.addLayout(head)

        self.form_lead = QLabel("前三组填完就能保存；带 * 的是必填。")
        self.form_lead.setObjectName("muted")
        self.form_lead.setWordWrap(True)
        column.addWidget(self.form_lead)

        self._build_trip_section(column)
        self._build_train_section(column)
        self._build_seat_section(column)
        self._build_advanced_section(column)
        column.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.form_sheet)
        # 最小值要挂在**滚动区**上，挂在里面的控件上是没用的 ——
        # QScrollArea 会宁可给你一根横向滚动条，也不上报表纸的最小宽度。
        # 600 是「车次类型」那七个标签不被压到截断的底线；它一路上报成窗口的最小宽度，
        # 于是窗口压根拖不到更窄（横向滚动条在表单里是最难用的东西）。
        scroll.setMinimumWidth(600)
        return scroll

    @staticmethod
    def _section_column() -> tuple[QWidget, QVBoxLayout]:
        """一个分组的容器：组内间距 10，比组间的 22 小得多。

        以前所有控件（含分组标题）都平铺在同一个 20px 间距的列里，
        组内和组间的间距一样大，于是「哪几项是一组的」只能靠猜。
        """
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        return box, layout

    def _build_list_column(self) -> QWidget:
        right = QWidget()
        # 右栏也有下限：再窄任务卡片里的「路线 / 日期」就会一个字一行，
        # 与其挤成那样，不如让窗口本身有个最小宽度。
        right.setMinimumWidth(270)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(2, 16, 18, 16)
        right_layout.setSpacing(12)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        title = QLabel("已有任务")
        title.setObjectName("pageTitle")
        head.addWidget(title)
        self.list_count = QLabel("")
        self.list_count.setObjectName("muted")
        head.addWidget(self.list_count)
        head.addStretch(1)
        right_layout.addLayout(head)

        self.task_container = QWidget()
        self.task_layout = QVBoxLayout(self.task_container)
        self.task_layout.setContentsMargins(0, 0, 0, 0)
        self.task_layout.setSpacing(10)
        self.task_layout.addStretch(1)
        list_scroll = QScrollArea()
        list_scroll.setWidgetResizable(True)
        list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        list_scroll.setWidget(self.task_container)
        right_layout.addWidget(list_scroll, 1)
        return right

    def _build_action_bar(self) -> QFrame:
        """固定在底部的操作条：它**不跟着表单滚**。

        以前保存按钮长在滚动区末尾，表单有 1262px 高、可视区 800px 上下，
        意味着填完必须再滚两屏才能保存 —— 这是「不好操作」最直接的一处。
        """
        bar = QFrame()
        bar.setObjectName("actionBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(10)

        self.btn_save = QPushButton("保存任务")
        self.btn_save.setObjectName("primaryButton")
        self.btn_save.setMinimumWidth(104)
        self.btn_reset = QPushButton("清空表单")
        layout.addWidget(self.btn_save)
        layout.addWidget(self.btn_reset)

        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label, 1)

        self.btn_save.clicked.connect(self._save)
        self.btn_reset.clicked.connect(self.reset_form)
        return bar

    # ------------------------------------------------------------------ #
    # 行型
    # ------------------------------------------------------------------ #
    @staticmethod
    def _inline(label: str, widget: QWidget, *, tip: str = "", label_width: int = LABEL_WIDTH) -> QWidget:
        """「标签在左、控件在右」的单行。

        只给**简单控件**（输入框 / 下拉 / 数字框）用：它们没有内部按钮行，
        右列再窄也放得下。一行约占 34px，两个并排就能再省一行。
        复合控件绝不能用这个 —— 见模块开头的说明。
        """
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        holder = QLabel(label)
        holder.setObjectName("fieldLabel")
        holder.setFixedWidth(label_width)
        holder.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if tip:
            holder.setToolTip(tip)
            widget.setToolTip(tip)
        layout.addWidget(holder)
        layout.addWidget(widget, 1)
        return box

    @staticmethod
    def _field(label: str, widget: QWidget, *, hint: str = "") -> QWidget:
        """「小标题在上、控件占满整行」，给复合控件用。

        复合控件（日期 / 站点 / 车次）内部已经有一行按钮，塞进窄列会被挤扁，
        必须占满整行。标签放上方还有个附带好处：长短字段的控件起点是对齐的。
        """
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        if label:
            title = QLabel(label)
            title.setObjectName("fieldLabel")
            head.addWidget(title)
        if hint:
            tip = QLabel(hint)
            tip.setObjectName("hint")
            tip.setWordWrap(True)
            head.addWidget(tip, 1)
        else:
            head.addStretch(1)
        if label or hint:
            layout.addLayout(head)
        layout.addWidget(widget)
        return box

    @staticmethod
    def _two_column() -> QGridLayout:
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        return grid

    # ------------------------------------------------------------------ #
    # ① 行程
    # ------------------------------------------------------------------ #
    def _build_trip_section(self, column: QVBoxLayout) -> None:
        section, box = self._section_column()
        box.addWidget(SectionHeader("1", "行程", "哪天出发、从哪到哪 —— 这三项必填"))

        self.input_name = QLineEdit()
        self.input_name.setPlaceholderText("例如 广州→长沙 国庆")
        box.addWidget(self._inline("任务名称 *", self.input_name, tip="自己认得出就行"))

        # 日期与站点都改成「选」而不是「敲」：两者都极易敲错，
        # 而敲错的症状是「任务跑了半天一条也没查到」，很难归因。
        self.input_dates = DateField(label="乘车日期 *")
        # 被拒绝时要说出来 —— 否则「点了添加没反应」既看不出错、也不知道为什么
        self.input_dates.rejected.connect(
            lambda reason: QMessageBox.warning(self, "乘车日期", reason)
        )
        box.addWidget(self.input_dates)

        # 出发站 / 到达站并排：各自只有一个按钮，半幅宽度够用，还省一整行高度
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(18)
        self.input_from = StationField(self.api, label="出发站 *")
        self.input_to = StationField(self.api, label="到达站 *")
        row.addWidget(self.input_from, 1)
        row.addWidget(self.input_to, 1)
        box.addLayout(row)
        column.addWidget(section)

    # ------------------------------------------------------------------ #
    # ② 车次
    # ------------------------------------------------------------------ #
    def _build_train_section(self, column: QVBoxLayout) -> None:
        section, body = self._section_column()
        body.addWidget(
            SectionHeader("2", "车次", "留空表示不限；白名单的列表顺序就是抢票优先级")
        )

        # 两个名单各占约 88px，并排出现还互相干扰（它们本来就互斥：同一趟车不能同时在两边）。
        # 改成分段切换：一次只看一个，按钮上带数量 —— 一眼知道另一边有没有东西。
        self.input_white = TrainListField(empty_text="不限车次（留空即可）")
        self.input_black = TrainListField(empty_text="没有排除任何车次")
        self.input_white.pick_requested.connect(lambda: self._open_train_picker("white"))
        self.input_black.pick_requested.connect(lambda: self._open_train_picker("black"))
        self.input_white.changed.connect(self._refresh_train_tabs)
        self.input_black.changed.connect(self._refresh_train_tabs)

        seg = QHBoxLayout()
        seg.setContentsMargins(0, 0, 0, 0)
        seg.setSpacing(0)
        self.btn_tab_white = QPushButton()
        self.btn_tab_white.setObjectName("segmentLeft")
        self.btn_tab_white.setCheckable(True)
        self.btn_tab_white.setChecked(True)
        self.btn_tab_white.setToolTip("只抢这些车次；列表顺序就是抢票优先级")
        self.btn_tab_black = QPushButton()
        self.btn_tab_black.setObjectName("segmentRight")
        self.btn_tab_black.setCheckable(True)
        self.btn_tab_black.setToolTip("这些车次一律不抢")
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self.btn_tab_white, 0)
        group.addButton(self.btn_tab_black, 1)
        self.train_tab_group = group
        seg.addWidget(self.btn_tab_white)
        seg.addWidget(self.btn_tab_black)
        seg.addStretch(1)
        body.addLayout(seg)

        self.train_stack = QStackedWidget()
        self.train_stack.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.train_stack.addWidget(self.input_white)
        self.train_stack.addWidget(self.input_black)
        body.addWidget(self.train_stack)
        group.idClicked.connect(self.train_stack.setCurrentIndex)
        self._refresh_train_tabs()

        # 原来是「一个 96px 高的勾选列表，只看得见四项还要滚动」——
        # 七个选项直接铺成网格，一眼看全；并且做成可点的小标签（原高度只有 18px）。
        types_box = QWidget()
        grid = QGridLayout(types_box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self.train_type_boxes: list[QCheckBox] = []
        for index, name in enumerate(TRAIN_TYPES):
            chip = QCheckBox(name)
            chip.setObjectName("chipCheck")
            self.train_type_boxes.append(chip)
            # **一行七个**：做成小标签之后每个只要几十像素，一行放得下。
            # 原来用 4 列网格会折成两行，白多出 30 多像素高。
            # 七列等宽（不额外加弹簧列）：既不会把文字挤掉，看起来也整齐。
            grid.addWidget(chip, 0, index)
        body.addWidget(self._inline("车次类型", types_box, tip="不勾表示不限车次类型"))
        column.addWidget(section)

    def _refresh_train_tabs(self) -> None:
        white = len(self.input_white.values())
        black = len(self.input_black.values())
        self.btn_tab_white.setText(f"只抢这些（{white}）" if white else "只抢这些 · 不限")
        self.btn_tab_black.setText(f"不抢这些（{black}）" if black else "不抢这些 · 无")

    # ------------------------------------------------------------------ #
    # ③ 席别与乘客
    # ------------------------------------------------------------------ #
    def _build_seat_section(self, column: QVBoxLayout) -> None:
        section, body = self._section_column()
        body.addWidget(
            SectionHeader("3", "席别与乘客", "席别从上到下就是优先级：有票时先要排在前面的")
        )

        # 席别的顺序就是「有票时先要哪个」，所以必须是一个能表达顺序的控件
        self.seats = PriorityList(SEAT_TYPES, default=DEFAULT_SEATS)
        body.addWidget(self.seats)

        self.input_passengers = QLineEdit()
        self.input_passengers.setPlaceholderText("留空则用账号下全部乘车人")
        self.input_time = QLineEdit()
        self.input_time.setPlaceholderText("06:00,23:00（可留空）")

        grid = self._two_column()
        grid.addWidget(self._inline("乘客姓名", self.input_passengers), 0, 0)
        grid.addWidget(self._inline("出发时间", self.input_time), 0, 1)
        body.addLayout(grid)
        column.addWidget(section)

    # ------------------------------------------------------------------ #
    # ④ 运行参数
    # ------------------------------------------------------------------ #
    def _build_advanced_section(self, column: QVBoxLayout) -> None:
        section, body = self._section_column()
        self.btn_adv = QPushButton("展开设置")
        self.btn_adv.setObjectName("tinyButton")
        self.btn_adv.setCheckable(True)
        self.btn_adv.toggled.connect(self._set_adv_open)

        body.addWidget(
            SectionHeader("4", "运行参数", "全都有默认值，不确定就别改", action=self.btn_adv)
        )

        self.adv_body = QWidget()
        adv = QVBoxLayout(self.adv_body)
        adv.setContentsMargins(0, 0, 0, 0)
        adv.setSpacing(12)

        self.combo_strategy = QComboBox()
        for value, label in STRATEGIES:
            self.combo_strategy.addItem(label, value)
        self.spin_interval = QDoubleSpinBox()
        self.spin_interval.setRange(0.3, 60.0)
        self.spin_interval.setSingleStep(0.1)
        self.spin_interval.setValue(1.0)
        self.spin_interval.setSuffix(" 秒")
        self.spin_interval.setToolTip("两次余票查询之间的间隔。调小抢得快但更容易被限流")
        self.input_sale = QLineEdit()
        self.input_sale.setPlaceholderText("2026-10-01 08:00:00（可留空）")
        self.spin_prepare = QSpinBox()
        self.spin_prepare.setRange(0, 600)
        self.spin_prepare.setValue(30)
        self.spin_prepare.setSuffix(" 秒")
        self.spin_prepare.setToolTip("准点开售时提前多久开始准备（只对填了开售时间的任务生效）")
        self.input_accounts = QLineEdit()
        self.input_accounts.setPlaceholderText("留空则自动分配在线账号")
        self.check_enabled = QCheckBox("启用该任务（保存后即可在右侧点「启动」）")
        self.check_enabled.setChecked(True)

        grid = self._two_column()
        grid.addWidget(self._inline("抢票策略", self.combo_strategy), 0, 0)
        grid.addWidget(self._inline("查询间隔", self.spin_interval), 0, 1)
        grid.addWidget(self._inline("准点开售", self.input_sale), 1, 0)
        grid.addWidget(self._inline("开售前提前", self.spin_prepare), 1, 1)
        grid.addWidget(self._inline("指定账号", self.input_accounts), 2, 0, 1, 2)
        adv.addLayout(grid)
        adv.addWidget(self.check_enabled)

        self.adv_body.setVisible(False)
        # 折叠时不留空档：adv_body 隐藏后，这一组就只有标题那一行
        adv.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)
        body.addWidget(self.adv_body)
        column.addWidget(section)

    def _set_adv_open(self, open_: bool) -> None:
        self.adv_body.setVisible(bool(open_))
        self.btn_adv.setText("收起设置" if open_ else "展开设置")

    # ------------------------------------------------------------------ #
    # 从 12306 拉取车次（白/黑名单不用手敲）
    # ------------------------------------------------------------------ #
    def _open_train_picker(self, target: str = "white") -> None:
        """按当前表单里的「日期 + 起止站」拉取车次，勾选后写回白/黑名单。"""
        dates = self.input_dates.text().strip()
        from_stations = self.input_from.text().strip()
        to_stations = self.input_to.text().strip()
        if not (dates and from_stations and to_stations):
            # 缺哪项就点明哪项 —— 只说「请先填好基础信息」，人还得自己找是哪一项
            missing = [
                name for name, value in
                (("乘车日期", dates), ("出发站", from_stations), ("到达站", to_stations))
                if not value
            ]
            self.status_label.setText("还缺：" + "、".join(missing) + " —— 拉取车次要先知道是哪天的哪条线路")
            return

        from desktop_ui.train_picker import TrainPickerDialog

        dialog = TrainPickerDialog(
            self.api,
            dates=dates,
            from_stations=from_stations,
            to_stations=to_stations,
            target=target,
            parent=self.window(),
        )
        if dialog.exec() != TrainPickerDialog.DialogCode.Accepted:
            return
        self._merge_trains(dialog.selected_codes(), dialog.target())

    def _merge_trains(self, codes: list[str], target: str) -> None:
        """把勾选的车次并入目标名单。

        两条规则：
        - **保留已有顺序**（白名单的顺序就是抢票优先级），新勾选的追加在后面；
        - 同一车次不能同时出现在两个名单里（后端会直接判配置非法），
          所以从另一个名单里移出去，并在状态栏说明动了什么，
          免得用户看到「白名单莫名其妙少了一个车次」。
        """
        codes = [c.strip().upper() for c in codes if c.strip()]
        if not codes:
            self.status_label.setText("没有勾选任何车次")
            return

        to_black = target == "black"
        label = "黑名单" if to_black else "白名单"
        other_label = "白名单" if to_black else "黑名单"
        field = self.input_black if to_black else self.input_white
        other = self.input_white if to_black else self.input_black

        existing = self._split(field.text())
        added = [code for code in codes if code not in existing]
        field.setText(",".join(existing + added))

        other_values = self._split(other.text())
        conflict = [code for code in codes if code in other_values]
        if conflict:
            other.setText(",".join(c for c in other_values if c not in codes))

        # 拉来的车次进了黑名单，就把视图切到黑名单 —— 否则他看不到刚加的东西
        if to_black:
            self.btn_tab_black.setChecked(True)
            self.train_stack.setCurrentIndex(1)

        message = f"已加入{label}：{','.join(added) or '（均已存在，无新增）'}"
        if conflict:
            message += f"；已从{other_label}移出：{','.join(conflict)}"
        message += "　—— 记得点「保存任务」"
        self.status_label.setText(message)

    # ------------------------------------------------------------------ #
    # 表单 ⇄ 数据
    # ------------------------------------------------------------------ #
    @staticmethod
    def _split(text: str) -> list[str]:
        return [part.strip() for part in text.replace("，", ",").replace(";", ",").split(",") if part.strip()]

    def _payload(self) -> dict[str, Any]:
        return {
            "name": self.input_name.text().strip(),
            "left_dates": self.input_dates.text().strip(),
            "from_stations": self.input_from.text().strip(),
            "to_stations": self.input_to.text().strip(),
            "trains_whitelist": self.input_white.text().strip(),
            "trains_blacklist": self.input_black.text().strip(),
            "train_types": [box.text() for box in self.train_type_boxes if box.isChecked()],
            "seat_types": self.seats.values(),
            "depart_time_range": self.input_time.text().strip(),
            "passengers": self.input_passengers.text().strip(),
            "accounts": self.input_accounts.text().strip(),
            "strategy": self.combo_strategy.currentData(),
            "query_interval": self.spin_interval.value(),
            "sale_open_time": self.input_sale.text().strip(),
            "sale_prepare_seconds": self.spin_prepare.value(),
            "enabled": self.check_enabled.isChecked(),
        }

    def _load_into_form(self, task: dict[str, Any]) -> None:
        self.editing_id = int(task["id"])
        self.form_title.setText(f"编辑任务 #{task['id']}")
        self.form_lead.setText(
            f"正在修改「{task.get('name', '')}」。改完点底部「保存任务」，运行中的任务会立即生效。"
        )
        self.btn_cancel_edit.setVisible(True)
        self.input_name.setText(task.get("name", ""))
        self.input_dates.setText(",".join(task.get("left_dates") or []))
        self.input_from.setText(",".join(task.get("from_stations") or []))
        self.input_to.setText(",".join(task.get("to_stations") or []))
        self.input_white.setText(",".join(task.get("trains_whitelist") or []))
        self.input_black.setText(",".join(task.get("trains_blacklist") or []))
        wanted_types = set(task.get("train_types") or [])
        for box in self.train_type_boxes:
            box.setChecked(box.text() in wanted_types)
        self.seats.setText(",".join(task.get("seat_types") or []) or ",".join(DEFAULT_SEATS))
        self.input_time.setText(",".join(task.get("depart_time_range") or []))
        self.input_passengers.setText(",".join(task.get("passengers") or []))
        self.input_accounts.setText(",".join(task.get("accounts") or []))
        index = self.combo_strategy.findData(task.get("strategy", "direct"))
        self.combo_strategy.setCurrentIndex(max(0, index))
        self.spin_interval.setValue(float(task.get("query_interval") or 1.0))
        self.input_sale.setText(task.get("sale_open_time", "") or "")
        self.spin_prepare.setValue(int(task.get("sale_prepare_seconds") or 30))
        self.check_enabled.setChecked(bool(task.get("enabled", True)))
        # 编辑已有任务时把运行参数摊开：这一组里很可能有他上次特意改过的值，
        # 收起来会让人以为「我设的东西丢了」。
        self.btn_adv.setChecked(True)
        self.status_label.setText(f"已载入任务 #{task['id']}")

    def reset_form(self) -> None:
        self.editing_id = None
        self.form_title.setText("新建任务")
        self.form_lead.setText("前三组填完就能保存；带 * 的是必填。")
        self.btn_cancel_edit.setVisible(False)
        for widget in (self.input_name, self.input_dates, self.input_from, self.input_to,
                       self.input_white, self.input_black, self.input_time,
                       self.input_passengers, self.input_accounts, self.input_sale):
            widget.clear()
        self.seats.setText(",".join(DEFAULT_SEATS))
        for box in self.train_type_boxes:
            box.setChecked(False)
        self.combo_strategy.setCurrentIndex(0)
        self.spin_interval.setValue(1.0)
        self.spin_prepare.setValue(30)
        self.check_enabled.setChecked(True)
        self.btn_adv.setChecked(False)
        self.btn_tab_white.setChecked(True)
        self.train_stack.setCurrentIndex(0)
        self._refresh_train_tabs()
        self.status_label.setText("表单已清空")

    # ------------------------------------------------------------------ #
    def _save(self) -> None:
        payload = self._payload()
        # 缺哪项就点明哪项，并把焦点送过去 —— 比一句「请至少填写…」有用得多
        missing = [
            label for label, value in
            (("任务名称", payload["name"]), ("乘车日期", payload["left_dates"]),
             ("出发站", payload["from_stations"]), ("到达站", payload["to_stations"]))
            if not value
        ]
        if missing:
            self.status_label.setText("还缺：" + "、".join(missing))
            self._focus_first_missing(payload)
            return
        if not payload["seat_types"]:
            self.status_label.setText("还缺：席别（在「3 席别与乘客」里，左边点一下就能加进去）")
            self.seats.setFocus()
            return
        try:
            if self.editing_id:
                self.api.update_task(self.editing_id, payload)
                self.status_label.setText("任务已更新（运行中的任务会立即生效）")
            else:
                created = self.api.create_task(payload)
                self.status_label.setText(
                    f"任务已创建（id={created.get('id')}）—— 到右侧任务卡片点「启动」它才会开始抢票"
                )
            self.reset_form()
            self.refresh_list()
        except ApiError as exc:
            self.status_label.setText(exc.message)

    def _focus_first_missing(self, payload: dict[str, Any]) -> None:
        if not payload["name"]:
            self.input_name.setFocus()
        elif not payload["left_dates"]:
            self.input_dates.setFocus()
        elif not payload["from_stations"]:
            self.input_from.setFocus()
        else:
            self.input_to.setFocus()

    # ------------------------------------------------------------------ #
    # 右栏：已有任务
    # ------------------------------------------------------------------ #
    def refresh_list(self) -> None:
        try:
            tasks = self.api.list_tasks()
        except ApiError as exc:
            self.status_label.setText(exc.message)
            return
        self._render(tasks)

    def _render(self, tasks: list[dict[str, Any]]) -> None:
        while self.task_layout.count() > 1:
            item = self.task_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        running = sum(1 for t in tasks if (t.get("runtime") or {}).get("running"))
        self.list_count.setText(f"{len(tasks)} 个 · 运行中 {running}" if tasks else "")

        if not tasks:
            # 空列表不要只写「还没有任务」——直接告诉他去哪儿、点什么
            placeholder = QLabel(
                "还没有任务。\n\n在左边按 ① 行程 → ② 车次 → ③ 席别与乘客 填好，"
                "点底部的「保存任务」，任务就会出现在这里。"
            )
            placeholder.setObjectName("emptyState")
            placeholder.setWordWrap(True)
            placeholder.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            self.task_layout.insertWidget(0, placeholder)
            return
        for task in tasks:
            self.task_layout.insertWidget(self.task_layout.count() - 1, self._task_widget(task))

    def _task_widget(self, task: dict[str, Any]) -> QWidget:
        frame = QFrame()
        frame.setObjectName("taskCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(9)

        runtime = task.get("runtime") or {}
        running = bool(runtime.get("running"))
        state_text = "运行中" if running else ("已停止" if runtime.get("state") else "未启动")

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        name = QLabel(task.get("name", "") or "(未命名)")
        name.setObjectName("taskName")
        name.setWordWrap(True)
        head.addWidget(name, 1)
        badge = QLabel(state_text)
        badge.setObjectName("stateBadgeOn" if running else "stateBadgeOff")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(badge, 0)
        layout.addLayout(head)

        froms = "/".join(task.get("from_stations") or []) or "?"
        tos = "/".join(task.get("to_stations") or []) or "?"
        # ⚠️ 分隔符必须带空格："2026-10-01/2026-10-02" 这种没有断点的长串，
        # Qt 在换行标签里算出的**最小宽度就是整串的宽度**（实测 273px），
        # 会把右栏顶出横向滚动条。加了空格才有换行点。
        lines = [
            ("路线", f"{froms} → {tos}"),
            ("日期", " / ".join(task.get("left_dates") or []) or "-"),
            ("席别", " > ".join(task.get("seat_types") or []) or "-"),
        ]
        white = task.get("trains_whitelist") or []
        black = task.get("trains_blacklist") or []
        if white:
            lines.append(("只抢", ", ".join(white)))
        if black:
            lines.append(("不抢", ", ".join(black)))
        extra = f"策略 {task.get('strategy')}　间隔 {task.get('query_interval')}s"
        if task.get("sale_open_time"):
            extra += f"　准点 {task.get('sale_open_time')}"
        lines.append(("运行", extra))

        meta = QGridLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setHorizontalSpacing(8)
        meta.setVerticalSpacing(3)
        for row, (key, value) in enumerate(lines):
            key_label = QLabel(key)
            key_label.setObjectName("metaKey")
            key_label.setFixedWidth(30)
            value_label = QLabel(str(value))
            value_label.setObjectName("metaValue")
            value_label.setWordWrap(True)
            meta.addWidget(key_label, row, 0, Qt.AlignmentFlag.AlignTop)
            meta.addWidget(value_label, row, 1)
        meta.setColumnStretch(1, 1)
        layout.addLayout(meta)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        btn_start = QPushButton("停止" if running else "启动")
        btn_start.setObjectName("tinyButton" if running else "tinyPrimary")
        btn_start.setToolTip("停止后任务不再盯票（已提交的候补单需要到 12306 处理）"
                             if running else "开始盯票；抢到会在通知里提醒你去 12306 付款")
        btn_start.clicked.connect(
            lambda _=False, tid=int(task["id"]), on=running:
            (self._stop(tid) if on else self._start(tid))
        )
        btn_edit = QPushButton("编辑")
        btn_edit.setObjectName("tinyButton")
        btn_edit.clicked.connect(lambda _=False, t=task: self._load_into_form(t))
        btn_delete = QPushButton("删除")
        btn_delete.setObjectName("tinyButton")
        btn_delete.clicked.connect(lambda _=False, tid=int(task["id"]): self._delete(tid))
        row.addWidget(btn_start)
        row.addWidget(btn_edit)
        row.addStretch(1)
        row.addWidget(btn_delete)
        layout.addLayout(row)
        return frame

    def _start(self, task_id: int) -> None:
        try:
            self.api.start_task(task_id)
            self.status_label.setText(f"任务 {task_id} 已启动，正在盯票")
            QTimer.singleShot(600, self.refresh_list)
        except ApiError as exc:
            self.status_label.setText(exc.message)

    def _stop(self, task_id: int) -> None:
        try:
            self.api.stop_task(task_id)
            self.status_label.setText(f"任务 {task_id} 已停止")
            QTimer.singleShot(600, self.refresh_list)
        except ApiError as exc:
            self.status_label.setText(exc.message)

    def _delete(self, task_id: int) -> None:
        confirm = QMessageBox.question(
            self, "确认删除", f"确定删除任务 #{task_id} 吗？删掉后要重新配一遍。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self.api.delete_task(task_id)
            self.refresh_list()
        except ApiError as exc:
            self.status_label.setText(exc.message)
