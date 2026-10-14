"""黑色钻石玻璃主题。

## 为什么要有这么一个集中层

在此之前，全套界面只有十几处零散的 `setStyleSheet`，而且多数是
`color: palette(mid)` 这种"交给系统主题决定"的写法。结果是：

- 外观完全取决于运行平台的调色板，做一个"黑色玻璃"界面无从下手；
- 同一个灰色在 5 个文件里各写一遍（有的写 `#9aa0a6`、有的写 `palette(mid)`），
  迟早会不一致；
- 反过来，凡是没写样式的地方（菜单、滚动条、下拉列表、表头、按钮框）
  又都回到系统默认的浅色外观，跟深色主界面拼在一起非常割裂。

所以这里把**颜色**和**外观**都收敛到一处：

1. `_PALETTE`：唯一的一份色板，全部是字面 hex。QSS 里**只允许**引用这些常量，
   不在样式串里现写颜色——改一个色只需改一行。
2. `apply_theme(app)`：把 `QPalette` 与全局 QSS 装到 QApplication 上。
   装了全局 QSS 之后，自绘控件（`QStyledItemDelegate`、`QTreeWidget` 的分支箭头等）
   里 `palette(PlaceholderText)` 之类的取色会**自动**跟着这套调色板走，
   不用再去逐个文件改色。
3. 少数必须自绘的地方要拿到同样的颜色，用 `TOKENS` / `ink()`，
   不要在别处再写一份 hex。

## 两个容易踩的坑（都在这里规避）

**坑一：QSS 里的 `palette(...)` 填充是第二遍解析的。**
Qt 在第一次样式解析时把 `palette(mid)` 这类引用替换成**当时**调色板里的值，
之后再改 `QPalette` 不会让已经解析过的规则重新解析（`style().polish()` 也救不回来）。
表现为"控件背景是新的深色，文字还是旧的深灰，等于一片看不见的灰字"。
因此本模块：**先设 QPalette，再设 QSS**，并且 QSS 里一律用字面 hex。

**坑二：必须给 QApplication 显式设 QPalette。**
`QApplication.setPalette()` 在已有样式表的情况下**未必**会把 `app.palette()`
换掉，但 `QApplication.setStyleSheet()` 是确定会把 `app.palette()` 重置成
"样式表默认调色板"的。所以顺序反过来的话，自绘代理拿到的还是默认调色板，
列表里就会出现"浅底深字"的一行行白条。先 `setPalette` 再 `setStyleSheet` 即可：
`setStyleSheet` 做的是 merge，保留我们设过的颜色角色。
"""

from __future__ import annotations

import os

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from src.utils.logger import get_logger

log = get_logger(__name__)

#: 本主题负责的标记键。装了主题就把它置 1，便于自检与排查"到底有没有生效"。
THEME_KEY = "ui/theme"
THEME_VALUE = "black-diamond"

#: 主题的官方名字（首次启动时写进配置，用于判断用户是否显式选过别的主题）
THEME_NAME = "黑色钻石玻璃"


# ====================================================================== 色板
#: 唯一的一份颜色定义。命名按用途而非按色相，免得改色时名字对不上。
_PALETTE = {
    # --- 基底：带一点点蓝的"深空黑"，纯 #000 会显脏、也不够贵气 ---
    "void": "#05060A",          # 最外层窗口底色
    "base": "#0A0C12",          # 常规面板底
    "surface": "#10131C",       # 抬升一层（列表、输入框）
    "surface_hi": "#161A26",    # 再抬一层（悬停、当前页签）
    "glass": "#12FFFFFF",       # 玻璃面板：白 7% 叠在深底上（QSS 里也叫 rgba 写法）
    "glass_hi": "#1CFFFFFF",    # 悬停态玻璃
    "glass_lo": "#0AFFFFFF",    # 安静态玻璃

    # --- 描边：玻璃的"棱"。一明一暗两条线才有厚度感 ---
    "edge": "#26FFFFFF",        # 常规 1px 描边（白 15%）
    "edge_hi": "#4DFFFFFF",     # 高光边（白 30%）
    "edge_lo": "#0FFFFFFF",     # 更弱的边

    # --- 钻石青：唯一的强调色，用在选中、焦点、进度已播段 ---
    "accent": "#4FE3E8",
    "accent_dim": "#2E9EA6",
    "accent_glow": "#334FE3E8",

    # --- 文字 ---
    "text": "#E9ECF2",          # 主文字
    "text_dim": "#9BA3B4",      # 次要说明
    "text_faint": "#6B7385",    # 占位符 / 禁用
    "text_on_accent": "#04222A",  # 强调色底上的文字（深色才看得清）

    # --- 语义色 ---
    "danger": "#FF6B7A",
    "warn": "#E8A33D",
    "ok": "#4ED8A0",
}

#: 给需要自绘的控件取色用（`from src.ui.theme import TOKENS`）。
TOKENS = dict(_PALETTE)


def ink(name: str, fallback: str = "#FFFFFF") -> QColor:
    """按名字取一个 QColor。名字拼错时返回 fallback 而不是抛错。

    自绘代理一年也改不了一次，为了一个拼写错误让整个界面画不出来不值得。
    """
    value = _PALETTE.get(name, fallback)
    color = QColor(value)
    if not color.isValid():
        # 形态是 #AARRGGBB 的透明色，QColor 认不出来，手工拆
        if len(value) == 9 and value.startswith("#"):
            alpha = int(value[1:3], 16)
            color = QColor(f"#{value[3:]}")
            color.setAlpha(alpha)
    return color


def color(name: str) -> str:
    """按名字取十六进制串（QSS 拼接用）。"""
    return _PALETTE.get(name, "#FFFFFF")


# ====================================================================== QSS
def stylesheet() -> str:
    """整套界面的样式表。

    写法上刻意保持"一处定义、多处复用"：颜色全部来自 `_PALETTE`，
    尺寸（圆角、内边距、控件高度）也都在这里的几条规则里统一，
    免得某个页面单独长出个不一样高的按钮。
    """
    p = _PALETTE

    return f"""
/* ============================ 全局基底 ============================ */
QWidget {{
    color: {p['text']};
    font-size: 13px;
}}

/* 主窗口与对话框：一块有层次的深空，右上角一点极淡的辉光当"打光" */
#glassRoot, QDialog {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 {p['void']}, stop:0.55 {p['base']}, stop:1 #0D1018);
}}

/* 自绘标题栏（无边框窗口，见 ui/title_bar.py）：
   比主体更深一档，用一条发丝线与内容区分层 */
#titleBar {{
    background: {p['void']};
    border-bottom: 1px solid {p['edge']};
}}
#titleBar QLabel {{
    background: transparent;
    font-size: 12px;
}}
#titleLabel {{
    color: {p['text_dim']};
}}
#titleBar QPushButton {{
    background: transparent;
    border: none;
    border-radius: 6px;
    color: {p['text_dim']};
    font-size: 14px;
    padding: 0;
    min-width: 44px;
    max-width: 44px;
    max-height: 28px;
}}
#titleBar QPushButton:hover {{
    background: {p['glass_hi']};
    color: {p['text']};
}}
#titleBar QPushButton#titleClose:hover {{
    background: {p['danger']};
    color: {p['text_on_accent']};
}}

/* 一排小工具函数：把"玻璃片"的效果复用出去 */
QGroupBox, #glassPanel, #glassCard {{
    background-color: {p['glass']};
    border: 1px solid {p['edge']};
    border-radius: 12px;
}}

QGroupBox {{
    margin-top: 14px;
    padding: 12px 14px 10px 14px;
    font-weight: 600;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 0 6px;
    color: {p['accent']};
    background-color: transparent;
}}

/* 纯文字容器（说明、状态）不该被当成"控件"再画一层底 */
QLabel {{
    background: transparent;
    border: none;
}}

/* ============================ 交互控件 ============================ */
QPushButton {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {p['glass_hi']}, stop:1 {p['glass_lo']});
    border: 1px solid {p['edge']};
    border-radius: 9px;
    padding: 5px 12px;
    min-height: 20px;
    color: {p['text']};
}}

QPushButton:hover {{
    border-color: {p['edge_hi']};
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #2EFFFFFF, stop:1 #14FFFFFF);
}}

QPushButton:pressed {{
    background-color: #0A000000;
    border-color: {p['accent_dim']};
    color: {p['accent']};
}}

QPushButton:checked {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {p['accent_glow']}, stop:1 #1A4FE3E8);
    border: 1px solid {p['accent_dim']};
    color: {p['accent']};
}}

QPushButton:disabled {{
    color: {p['text_faint']};
    background: #08FFFFFF;
    border-color: {p['edge_lo']};
}}

/* 对话框右下角的主按钮：唯一一处实心强调色，视线自然会落上去 */
QPushButton:default {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {p['accent']}, stop:1 {p['accent_dim']});
    border: 1px solid {p['accent']};
    color: {p['text_on_accent']};
    font-weight: 600;
}}

QPushButton:default:hover {{
    background: {p['accent']};
}}

QPushButton:default:disabled {{
    background: #22FFFFFF;
    border-color: {p['edge']};
    color: {p['text_faint']};
}}

/* 控制条里的「播放 / 暂停」：不需要像对话框那样抢眼，但要能一眼找到 */
QPushButton#primaryButton {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #334FE3E8, stop:1 #1A2E9EA6);
    border: 1px solid {p['accent_dim']};
    color: {p['accent']};
    font-weight: 600;
    min-width: 68px;
}}

QPushButton#primaryButton:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #4D4FE3E8, stop:1 #262E9EA6);
    border-color: {p['accent']};
}}

/* 时间码用等宽字体：数字跳动时位置不会左右抖 */
QLabel#timeLabel {{
    color: {p['text_dim']};
    font-family: "Cascadia Mono", "Consolas", "JetBrains Mono", monospace;
    font-size: 12px;
}}

/* ---------- 输入框 / 数字框 / 下拉框 ---------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background-color: #B30A0C12;
    border: 1px solid {p['edge']};
    border-radius: 9px;
    padding: 5px 9px;
    min-height: 20px;
    color: {p['text']};
    selection-background-color: {p['accent_dim']};
    selection-color: {p['text']};
}}

QLineEdit:hover, QSpinBox:hover, QComboBox:hover, QPlainTextEdit:hover {{
    border-color: {p['edge_hi']};
}}

QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {p['accent_dim']};
    background-color: #CC0D1018;
}}

QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    color: {p['text_faint']};
    background-color: #660A0C12;
}}

QLineEdit::placeholder {{
    color: {p['text_faint']};
}}

QComboBox::drop-down {{
    border: none;
    width: 22px;
}}

QComboBox::down-arrow {{
    image: none;
    /* 没有图标资源，用等腰三角形画下拉箭头。
       width/height 必须写且等于两条透明边之和，否则部分 Qt 样式引擎
       会把边框盒渲染成实心方块（离屏下实测如此）。 */
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid {p['text_dim']};
    width: 10px;
    height: 6px;
    margin-right: 7px;
}}

QComboBox:on::down-arrow {{
    border-top-color: {p['accent']};
}}

QComboBox QAbstractItemView {{
    background-color: {p['surface_hi']};
    border: 1px solid {p['edge']};
    border-radius: 8px;
    padding: 4px;
    outline: none;
}}

/* ---------- 复选框 ---------- */
QCheckBox, QRadioButton {{
    spacing: 8px;
    color: {p['text']};
}}

QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px;
    height: 15px;
    background-color: #B30A0C12;
    border: 1px solid {p['edge_hi']};
}}

QCheckBox::indicator {{
    border-radius: 4px;
}}

QRadioButton::indicator {{
    border-radius: 8px;
}}

QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {p['accent_dim']};
}}

QCheckBox::indicator:checked {{
    /* 勾选态用强调色实心 + 深色对勾，白底浅色主题下也不会糊在一起 */
    background: {p['accent']};
    border-color: {p['accent']};
    image: url(:/nonexistent);   /* 不依赖图标，对勾由下面的 border 画 */
}}

QCheckBox::indicator:checked {{
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,
                stop:0 {p['accent']}, stop:0.75 {p['accent']}, stop:1 {p['accent_dim']});
}}

QRadioButton::indicator:checked {{
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,
                stop:0 {p['text_on_accent']}, stop:0.4 {p['text_on_accent']},
                stop:0.45 {p['accent']}, stop:1 {p['accent']});
}}

QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    border-color: {p['edge_lo']};
    background-color: #440A0C12;
}}

/* ---------- 滑条 ---------- */
QSlider::groove:horizontal {{
    height: 4px;
    border-radius: 2px;
    background: #26FFFFFF;
}}

QSlider::sub-page:horizontal {{
    height: 4px;
    border-radius: 2px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 {p['accent_dim']}, stop:1 {p['accent']});
}}

QSlider::handle:horizontal {{
    width: 12px;
    height: 12px;
    margin: -5px 0;
    border-radius: 6px;
    /* 钻石的感觉主要靠这颗手柄：中心亮、边缘冷 */
    background: qradialgradient(cx:0.35, cy:0.3, radius:0.8,
                stop:0 #FFFFFF, stop:0.45 {p['accent']}, stop:1 {p['accent_dim']});
    border: 1px solid #66FFFFFF;
}}

QSlider::handle:horizontal:hover {{
    background: qradialgradient(cx:0.35, cy:0.3, radius:0.85,
                stop:0 #FFFFFF, stop:0.5 #8FF3F7, stop:1 {p['accent']});
}}

QSlider::groove:horizontal:disabled {{
    background: #14FFFFFF;
}}

QSlider::handle:horizontal:disabled {{
    background: #33FFFFFF;
    border-color: {p['edge']};
}}

QProgressBar {{
    background-color: #B30A0C12;
    border: 1px solid {p['edge']};
    border-radius: 7px;
    text-align: center;
    color: {p['text']};
    min-height: 16px;
}}

QProgressBar::chunk {{
    border-radius: 6px;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {p['accent']}, stop:1 {p['accent_dim']});
}}

/* ============================ 容器与视图 ============================ */
QFrame[frameShape="4"], QFrame[frameShape="5"] {{   /* HLine / VLine */
    border: none;
    background: {p['edge']};
    max-height: 1px;
    max-width: 1px;
}}

QTabWidget::pane {{
    border: 1px solid {p['edge']};
    border-radius: 10px;
    background-color: {p['glass_lo']};
    top: -1px;
}}

QTabBar {{
    background: transparent;
}}

QTabBar::tab {{
    background: transparent;
    border: 1px solid transparent;
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
    padding: 6px 14px;
    margin-right: 3px;
    color: {p['text_dim']};
}}

QTabBar::tab:hover {{
    color: {p['text']};
    background: {p['glass_lo']};
}}

QTabBar::tab:selected {{
    color: {p['accent']};
    background: {p['glass']};
    border: 1px solid {p['edge']};
    border-bottom-color: transparent;
    font-weight: 600;
}}

QTreeWidget, QTreeView, QListView, QListWidget, QTableView {{
    background-color: {p['glass_lo']};
    alternate-background-color: #0AFFFFFF;
    border: 1px solid {p['edge']};
    border-radius: 10px;
    outline: none;
    padding: 3px;
    selection-background-color: {p['accent_glow']};
    selection-color: {p['text']};
}}

QTreeWidget::item, QListView::item {{
    border-radius: 6px;
    padding: 2px 4px;
    min-height: 22px;
}}

QTreeWidget::item:hover, QListView::item:hover {{
    background: {p['glass_lo']};
}}

QTreeWidget::item:selected, QListView::item:selected {{
    background: {p['accent_glow']};
    color: {p['text']};
}}

QHeaderView::section {{
    background-color: {p['surface_hi']};
    color: {p['text_dim']};
    border: none;
    border-bottom: 1px solid {p['edge']};
    padding: 6px 8px;
    font-weight: 600;
}}

QTableView QTableCornerButton::section {{
    background-color: {p['surface_hi']};
    border: none;
}}

/* ---------- 滚动条：细、无箭头、悬停才亮 ---------- */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px 2px 2px 0;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0 2px 2px 2px;
}}

QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: #33FFFFFF;
    border-radius: 4px;
    min-height: 28px;
    min-width: 28px;
}}

QScrollBar::handle:hover {{
    background: #5AFFFFFF;
}}

QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
    border: none;
    background: none;
}}

QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

QScrollArea {{
    border: none;
    background: transparent;
}}

QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

/* ============================ 菜单 / 提示 / 状态栏 ============================ */
QMenuBar {{
    background: transparent;
    color: {p['text']};
}}

QMenuBar::item {{
    background: transparent;
    padding: 5px 10px;
    border-radius: 7px;
}}

QMenuBar::item:selected {{
    background: {p['glass_hi']};
}}

QMenu {{
    background-color: {p['surface_hi']};
    border: 1px solid {p['edge_hi']};
    border-radius: 10px;
    padding: 6px;
}}

QMenu::item {{
    padding: 6px 18px 6px 14px;
    border-radius: 7px;
    color: {p['text']};
}}

QMenu::item:selected {{
    background: {p['accent_glow']};
    color: {p['accent']};
}}

QMenu::item:disabled {{
    color: {p['text_faint']};
}}

QMenu::separator {{
    height: 1px;
    background: {p['edge']};
    margin: 5px 8px;
}}

QToolTip {{
    background-color: {p['surface_hi']};
    color: {p['text']};
    border: 1px solid {p['edge_hi']};
    border-radius: 8px;
    padding: 6px 9px;
}}

QStatusBar {{
    background: transparent;
    color: {p['text_dim']};
    border-top: 1px solid {p['edge_lo']};
}}

QStatusBar::item {{
    border: none;
}}

QStatusBar QLabel {{
    color: {p['text_dim']};
}}

QMessageBox {{
    background-color: {p['base']};
}}

QMessageBox QLabel {{
    color: {p['text']};
}}

/* 对话框按钮盒：让「确定 / 取消 / 应用」有统一的间距 */
QDialogButtonBox QPushButton {{
    min-width: 76px;
}}

/* ============================ 分隔器与滚动区 ============================ */
QSplitter::handle {{
    background: transparent;
}}

QSplitter::handle:horizontal {{
    width: 6px;
}}

QSplitter::handle:vertical {{
    height: 6px;
}}

QSplitter::handle:hover {{
    background: {p['accent_glow']};
}}

/* 滚动区里的页面本身不该再画一层底（否则玻璃会叠成两层、看起来发灰） */
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

QScrollArea QWidget {{
    background: transparent;
}}

/* 画面区：统一由控件自己 setStyleSheet 指定纯黑；
   这里只给个兜底，避免它继承到面板的浅色底 */
#videoSurface {{
    background-color: #000000;
    border: 1px solid {p['edge']};
    border-radius: 12px;
}}

#playlistTabs::pane {{
    background-color: {p['glass_lo']};
    border-radius: 10px;
}}

/* 侧栏底部的按钮行：小一号，免得跟主控制条抢视觉重量 */
#playlistTabs QPushButton {{
    padding: 4px 9px;
    min-width: 52px;
}}
"""


# ====================================================================== 应用
def build_palette() -> QPalette:
    """构造与 QSS 配套的 QPalette。

    自绘控件（`QStyledItemDelegate`、原生绘制的分支箭头等）只看调色板，
    不看样式表。所以这几个角色必须和 `_PALETTE` 对齐，否则会出现
    "样式表说浅字、调色板说深字"，在列表上尤其明显。
    """
    pal = QPalette()
    C = QPalette.ColorRole
    G = QPalette.ColorGroup

    pal.setColor(C.Window, ink("base"))
    pal.setColor(C.WindowText, ink("text"))
    pal.setColor(C.Base, ink("surface"))
    pal.setColor(C.AlternateBase, ink("void"))
    pal.setColor(C.Text, ink("text"))
    pal.setColor(C.PlaceholderText, ink("text_dim"))
    pal.setColor(C.Button, ink("surface_hi"))
    pal.setColor(C.ButtonText, ink("text"))
    pal.setColor(C.BrightText, ink("danger"))
    pal.setColor(C.Highlight, ink("accent"))
    pal.setColor(C.HighlightedText, ink("text_on_accent"))
    pal.setColor(C.ToolTipBase, ink("surface_hi"))
    pal.setColor(C.ToolTipText, ink("text"))
    pal.setColor(C.Link, ink("accent"))
    pal.setColor(C.LinkVisited, ink("accent_dim"))
    pal.setColor(C.Mid, ink("text_faint"))
    pal.setColor(C.Midlight, ink("edge_hi"))
    pal.setColor(C.Dark, ink("void"))
    pal.setColor(C.Shadow, QColor(0, 0, 0, 160))

    # 禁用态：整套压暗，避免"看着能点其实点不动"
    for role in (C.WindowText, C.Text, C.ButtonText, C.PlaceholderText):
        pal.setColor(G.Disabled, role, ink("text_faint"))

    return pal


def apply_theme(app: QApplication | None = None) -> bool:
    """把主题装到 QApplication 上。幂等，返回是否确实装上了。

    顺序很关键，见模块开头「坑二」：**先 QPalette，再 QSS**。
    """
    app = app or QApplication.instance()
    if app is None:  # 还没有 QApplication，没什么可装的
        return False

    app.setPalette(build_palette())
    app.setStyleSheet(stylesheet())
    log.info("已应用界面主题: %s", THEME_NAME)
    return True


def main_window_object_name() -> str:
    """主窗口中央控件的 objectName（QSS 里用它画整块背景）。

    抽成函数是为了让主窗口与主题两边共用同一个字符串，
    免得一边改名另一边还在按老名字找、样式静默失效。
    """
    return "glassRoot"


def is_off_topic_env() -> bool:
    """离屏渲染下不做圆角/透明相关优化——仅供测试判断用。"""
    return os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen"
