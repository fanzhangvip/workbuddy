# -*- coding: utf-8 -*-
"""桌面端主题：天蓝玻璃（唯一来源）。

为什么收敛到这里：换肤前颜色散在 ``app.py`` 的 STYLESHEET 和七八个页面的
内联 ``setStyleSheet`` 里 —— 改一个主色要追着六处跑，而且必然会漏。
现在「颜色语义」与「全局 QSS」都从本模块取，代码里需要着色的地方
一律 ``from desktop_ui.theme import C`` 后用语义键。

关于「玻璃质感」的实现边界（要诚实）：Qt 的 QSS **没有** CSS 那样的
``backdrop-filter``，做不了真·背景模糊。这里的玻璃感由三层叠加模拟：
窗口底层一条天蓝渐变 → 卡片用半透明白（下层颜色透出来）→ 细白描边 +
大圆角 + 亮色高光边。视觉上是磨砂玻璃，实现上零自绘、零性能开销。

布局纪律（防「挤占」）：
- 所有留白走 4 的倍数（8 / 12 / 16 / 24），圆角走 8 / 12 / 14；
- 输入类控件统一 ``padding: 9px 10px``，按钮 ``8px 16px``，小按钮 ``5px 12px``；
- 禁用态必须显式给出（灰化文字 + 降透明度），否则玻璃底上「看着能点其实不能」；
- 滚动条细化到 10px，避免粗滚动条在两栏布局里吃掉内容宽度。
"""

from __future__ import annotations

#: 语义色：代码里着色只许用这些键（不许再写字面量十六进制）。
C: dict[str, str] = {
    # 状态（语义不许随主题变：成功是绿、危险是红、警示是琥珀）
    "ok": "#15803d",
    "warn": "#b45309",
    "bad": "#b91c1c",
    "info": "#1d4ed8",
    # 文本
    "ink": "#16324a",        # 正文（比纯黑蓝一档，与天蓝底相融）
    "ink_strong": "#0b2540",  # 标题
    "muted": "#5b7a93",      # 次要说明
    "faint": "#8aa9bf",      # 更淡的提示
    # 主题色（天蓝）
    "accent": "#0284c7",      # 主色（边框/选中/焦点）
    "accent_deep": "#0369a1", # 按下/深一档
    "accent_bright": "#0ea5e9",
    "accent_soft": "#7dd3fc",
    # 深色玻璃侧栏上的文字
    "nav_text": "#cfe8fa",
    "nav_group": "#8fb8d4",
}

STYLESHEET = """
/* ============ 基调：天蓝渐变底 + 半透明白玻璃面 ============ */
QWidget {
    font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    font-size: 13px;
    color: #16324a;
}
QMainWindow, QDialog {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #d8ecfb, stop:0.45 #e6f4fe, stop:1 #d2e9fa);
}
QStackedWidget, QStackedWidget > QWidget { background: transparent; }
QMessageBox { background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
    stop:0 #e2f1fd, stop:1 #d6ebfa); }
QToolTip {
    background: rgba(11, 37, 64, 0.92); color: #d9efff;
    border: 1px solid rgba(125, 211, 252, 0.45); border-radius: 6px; padding: 6px 9px;
}

/* ============ 左侧导航：深海军蓝玻璃 ============ */
QListWidget#navList {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(8, 61, 100, 0.94), stop:1 rgba(10, 92, 134, 0.90));
    color: #cfe8fa; border: none; outline: none; padding: 14px 8px;
}
QListWidget#navList::item { padding: 11px 16px; border-radius: 8px; margin: 1px 6px; }
QListWidget#navList::item:selected {
    background: rgba(255, 255, 255, 0.18);
    color: #ffffff; font-weight: 600;
    border-left: 3px solid #7dd3fc; padding-left: 13px;
}
QListWidget#navList::item:hover { background: rgba(255, 255, 255, 0.10); color: #ffffff; }

/* ============ 按钮：玻璃白 ============ */
QPushButton {
    background: rgba(255, 255, 255, 0.85);
    border: 1px solid rgba(2, 132, 199, 0.28); border-radius: 8px;
    padding: 8px 16px; color: #16324a;
}
QPushButton:hover { background: #ffffff; border-color: rgba(2, 132, 199, 0.55); }
QPushButton:pressed { background: #dcf0fc; }
QPushButton:disabled { background: rgba(255, 255, 255, 0.45); color: #9db4c4; border-color: rgba(2, 132, 199, 0.12); }

/* 主按钮：天蓝渐变，页面上最重的动作 */
QPushButton#primaryButton {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #38bdf8, stop:1 #0284c7);
    border: 1px solid #0284c7; border-radius: 8px; color: #ffffff; font-weight: 600;
}
QPushButton#primaryButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #0ea5e9, stop:1 #0369a1);
    border-color: #0369a1;
}
QPushButton#primaryButton:pressed { background: #0369a1; }
QPushButton#primaryButton:disabled {
    background: rgba(148, 176, 196, 0.5); border-color: rgba(148, 176, 196, 0.4); color: #e5f2fa;
}
QPushButton#tinyButton { padding: 5px 12px; font-size: 12px; border-radius: 7px; }

/* 任务卡上的「启动」：这一页最该被点到的按钮 */
QPushButton#tinyPrimary {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #38bdf8, stop:1 #0284c7);
    border: 1px solid #0284c7; border-radius: 7px; color: #ffffff;
    padding: 5px 14px; font-size: 12px; font-weight: 600;
}
QPushButton#tinyPrimary:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #0ea5e9, stop:1 #0369a1);
}
QPushButton#tinyPrimary:disabled {
    background: rgba(148, 176, 196, 0.5); border-color: rgba(148, 176, 196, 0.4); color: #e5f2fa;
}

/* ============ 玻璃卡片 ============ */
QFrame#statCard, QFrame#taskCard {
    background: rgba(255, 255, 255, 0.72);
    border: 1px solid rgba(255, 255, 255, 0.90);
    border-radius: 12px;
}
QFrame#statCard:hover, QFrame#taskCard:hover { background: rgba(255, 255, 255, 0.86); }
/* 总览页顶部的「下一步」引导条：左侧一条天蓝杠区别于普通卡片 */
QFrame#heroCard {
    background: rgba(255, 255, 255, 0.78);
    border: 1px solid rgba(255, 255, 255, 0.90);
    border-left: 4px solid #0ea5e9;
    border-radius: 12px;
}

/* ============ 文本层级 ============ */
QLabel#heroTitle { font-size: 15px; font-weight: 600; color: #0b2540; }
QLabel#statTitle { color: #5b7a93; font-size: 12px; }
QLabel#statValue { font-size: 20px; font-weight: 600; color: #0b2540; }
QLabel#statValueBig { font-size: 28px; font-weight: 600; color: #0b2540; }
QLabel#sectionTitle { font-size: 15px; font-weight: 600; margin-top: 6px; color: #0b2540; }
QLabel#pageTitle { font-size: 17px; font-weight: 600; color: #0b2540; }
QLabel#muted { color: #5b7a93; }
QLabel#hint { color: #8aa9bf; font-size: 12px; }
QLabel#fieldLabel { color: #40617c; font-size: 13px; }

/* ============ 任务配置页：玻璃表单卡 ============ */
QFrame#formSheet {
    background: rgba(255, 255, 255, 0.75);
    border: 1px solid rgba(255, 255, 255, 0.90);
    border-radius: 14px;
}
QLabel#stepBadge {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #38bdf8, stop:1 #0284c7);
    color: #ffffff; border-radius: 11px;
    font-size: 12px; font-weight: 700;
}
QLabel#sectionName { font-size: 14px; font-weight: 600; color: #0b2540; }
QFrame#sectionRule { background: rgba(2, 132, 199, 0.15); border: none; }
/* 底部操作条：固定在滚动区外，永远看得见「保存任务」 */
QFrame#actionBar {
    background: rgba(255, 255, 255, 0.80);
    border-top: 1px solid rgba(2, 132, 199, 0.18);
}

/* ============ 分段切换（白/黑名单） ============ */
QPushButton#segmentLeft, QPushButton#segmentRight {
    background: rgba(255, 255, 255, 0.55); border: 1px solid rgba(2, 132, 199, 0.25);
    color: #40617c; padding: 7px 18px; font-size: 13px;
}
QPushButton#segmentLeft { border-top-right-radius: 0; border-bottom-right-radius: 0; border-right: none; }
QPushButton#segmentRight { border-top-left-radius: 0; border-bottom-left-radius: 0; }
QPushButton#segmentLeft:hover, QPushButton#segmentRight:hover { background: rgba(255, 255, 255, 0.8); }
QPushButton#segmentLeft:checked, QPushButton#segmentRight:checked {
    background: #ffffff; color: #0284c7; font-weight: 600; border-color: rgba(2, 132, 199, 0.55);
}

/* ============ 车次类型小标签 ============ */
QCheckBox#chipCheck {
    background: rgba(255, 255, 255, 0.8); border: 1px solid rgba(2, 132, 199, 0.25);
    border-radius: 14px; padding: 4px 9px; color: #40617c; font-size: 12px;
}
QCheckBox#chipCheck:hover { border-color: rgba(2, 132, 199, 0.5); background: #ffffff; }
QCheckBox#chipCheck:checked {
    background: #e0f2fe; border-color: #0284c7; color: #0369a1; font-weight: 600;
}

/* ============ 任务卡文本 ============ */
QLabel#taskName { font-size: 14px; font-weight: 600; color: #0b2540; }
QLabel#stateBadgeOn, QLabel#stateBadgeOff { border-radius: 9px; padding: 2px 10px; font-size: 12px; }
QLabel#stateBadgeOn { background: #dcf3e6; color: #1a7f4b; }
QLabel#stateBadgeOff { background: rgba(226, 239, 248, 0.9); color: #5b7a93; }
QLabel#metaKey { color: #8aa9bf; font-size: 12px; }
QLabel#metaValue { color: #2c5170; font-size: 12.5px; }
QLabel#emptyState { color: #8aa9bf; font-size: 13px; padding: 16px 2px; }

/* ============ 输入类：统一手感（9px 10px，焦点天蓝） ============ */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: rgba(255, 255, 255, 0.88);
    border: 1px solid rgba(2, 132, 199, 0.28); border-radius: 8px; padding: 9px 10px;
    selection-background-color: #0284c7; selection-color: #ffffff;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {
    border-color: rgba(2, 132, 199, 0.5); background: #ffffff;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 2px solid #0ea5e9; padding: 8px 9px; background: #ffffff;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    background: rgba(236, 244, 250, 0.6); color: #9db4c4; border-color: rgba(2, 132, 199, 0.12);
}
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView {
    background: rgba(255, 255, 255, 0.97);
    border: 1px solid rgba(2, 132, 199, 0.35); border-radius: 8px;
    selection-background-color: #e0f2fe; selection-color: #0369a1; outline: none;
}

/* 列表 / 表格 / 多行文本保持紧凑 */
QTextEdit, QListWidget, QTableWidget {
    background: rgba(255, 255, 255, 0.82);
    border: 1px solid rgba(2, 132, 199, 0.22); border-radius: 8px; padding: 5px 7px;
}
/* 标签类列表（日期/站点/车次/席别）：纵向不留内边距 —— 行高由 item 自己给，
   列表再留一层就会让「外框占用」不可预测，最后一行会被切成半截（回流 #66）。 */
QListWidget#chipList, QListWidget#chipList:hover, QListWidget#chipList:focus {
    padding: 0px 2px;
}
QListWidget::item { border-radius: 6px; padding: 3px 4px; }
QListWidget::item:selected { background: #e0f2fe; color: #0369a1; }
QListWidget::item:hover:!selected { background: rgba(224, 242, 254, 0.55); }

/* ============ 分组框 ============ */
QGroupBox {
    border: 1px solid rgba(2, 132, 199, 0.20);
    border-radius: 12px; margin-top: 14px;
    padding: 18px 14px 14px 14px;
    background: rgba(255, 255, 255, 0.65); font-weight: 600;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #0b2540; }
QGroupBox QLabel { font-weight: 400; }

/* ============ 滚动区与滚动条（细化，不吃内容宽度） ============ */
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px 3px; }
QScrollBar::handle:vertical { background: rgba(2, 132, 199, 0.30); border-radius: 4px; min-height: 32px; }
QScrollBar::handle:vertical:hover { background: rgba(2, 132, 199, 0.55); }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 3px 2px; }
QScrollBar::handle:horizontal { background: rgba(2, 132, 199, 0.30); border-radius: 4px; min-width: 32px; }
QScrollBar::handle:horizontal:hover { background: rgba(2, 132, 199, 0.55); }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ============ 选项卡 ============ */
QTabWidget::pane {
    border: 1px solid rgba(2, 132, 199, 0.22); border-radius: 10px;
    background: rgba(255, 255, 255, 0.72); top: -1px;
}
QTabBar::tab {
    padding: 8px 16px; background: rgba(224, 242, 254, 0.65);
    border: 1px solid rgba(2, 132, 199, 0.18); border-bottom: none;
    border-top-left-radius: 8px; border-top-right-radius: 8px;
    margin-right: 3px; color: #40617c;
}
QTabBar::tab:selected { background: #ffffff; color: #0284c7; font-weight: 600; }
QTabBar::tab:hover:!selected { background: rgba(224, 242, 254, 0.9); }

/* ============ 表格 ============ */
QTableWidget { gridline-color: rgba(2, 132, 199, 0.12); }
QHeaderView::section {
    background: rgba(224, 242, 254, 0.85); color: #40617c;
    padding: 7px 6px; border: none; border-bottom: 1px solid rgba(2, 132, 199, 0.25);
}

/* ============ 实时日志：深蓝玻璃终端 ============ */
QTextEdit#logView {
    background: rgba(8, 27, 48, 0.88); color: #d3ecff;
    font-family: Consolas, "Courier New", monospace;
    font-size: 12.5px; border: 1px solid rgba(125, 211, 252, 0.25); border-radius: 10px;
}

/* ============ 其它 ============ */
QStatusBar {
    background: rgba(255, 255, 255, 0.78);
    border-top: 1px solid rgba(2, 132, 199, 0.18); color: #40617c;
}
QProgressBar {
    background: rgba(255, 255, 255, 0.6); border: 1px solid rgba(2, 132, 199, 0.25);
    border-radius: 7px; text-align: center; color: #40617c; height: 14px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #38bdf8, stop:1 #0284c7);
    border-radius: 6px;
}
"""
