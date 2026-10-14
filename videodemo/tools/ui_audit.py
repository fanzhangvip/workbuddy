"""界面排版体检：在多个窗口尺寸下量每个控件的实际几何。

"挤占"是个主观说法，但它背后是**可以量化**的几件事：

1. **控件被压到小于自己的最小值**——`width < minimumSizeHint().width()`，
   表现是文字被截断。这是最严重的一种。
2. **控件之间重叠**——同一父控件下的兄弟控件矩形相交。
3. **控件溢出父容器**——右/下边缘超出父控件可视区，表现是被裁掉一半。
4. **控件被压成 0 宽/高**——彻底看不见了，但用户以为它还在（"按钮消失了，
   但我记得这里应该有东西"）。

本工具把这几件事逐个量出来并打印。用法：

    python tools/ui_audit.py                  # 默认扫主窗口 + 设置 + 下载
    python tools/ui_audit.py --shot           # 顺便把每个尺寸截一张图到 .tools/ui_shots/

离屏渲染下 Qt 会真实地做一遍布局计算，量出来的几何与实际运行一致，
所以这里的结果是可以当结论用的（只有字形渲染会是方框，与布局无关）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.utils.env_setup import setup_environment  # noqa: E402

setup_environment()

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from src.ui.theme import apply_theme  # noqa: E402

SHOT_DIR = ROOT / ".tools" / "ui_shots"

#: 主窗口要试的尺寸。最后两个是"极限"：比 minimumSize 大不了多少，
#: 用来暴露"最小尺寸之下会不会互相压"这类问题（真实用户拉窗口就会到这）。
MAIN_SIZES = ((1440, 860), (1180, 700), (1024, 640), (880, 560), (760, 500))

#: 设置 / 下载对话框的尺寸。560x420 是设置面板自身的最小宽度那一档。
DIALOG_SIZES = ((620, 620), (600, 560), (560, 430))

#: 允许忽略的控件类型：这些是 Qt 内部的螺栓螺母，宽度被压不算问题
_SKIP_CLASSES = {
    "QSliderPrivate", "QLineEditIconButton", "QComboBoxPrivateContainer",
    "QAbstractSpinBoxPrivate", "QFocusFrame", "QToolTip",
}


#: 输入类控件的"还能用"宽度下限。
#:
#: 这一类控件的 `minimumSizeHint()` 低得离谱——`QLineEdit` 无论内容多长都只报
#: 37px（实测），也就是说 Qt 允许把它压到只看得见两三个字符。而它偏偏是
#: 布局里"唯一愿意让步"的那个（按钮有 minimumWidth 撑着不让），
#: 于是窗口一窄，输入的损失全落在它一个人身上。
#:
#: 实测改造前的地址栏：窗口 1180px 时宽 588px，缩到 760px 时只剩 168px
#: （建议值 245px 的 69%），`minimumSizeHint()` 那条判据完全不会报警。
#: 所以这里按控件类型给一个可用下限，低于它就算"挤占"。
_USABLE_WIDTH = {
    "QLineEdit": 180,       # 放得下一条 https 地址的开头，能看出自己填了什么
    "QComboBox": 72,        # 至少露出"1.25x"这种值，不能只剩箭头
    "QSpinBox": 70,         # 数字 + 单位
    "QDoubleSpinBox": 70,
    "QLabel": 24,
    "QPushButton": 44,      # 两个字 + 内边距（"播放"）
}

#: 纵向可用下限（像素）。比这更矮的按钮/输入框点起来很别扭。
_USABLE_HEIGHT = {
    "QPushButton": 24,
    "QLineEdit": 24,
    "QComboBox": 24,
    "QSpinBox": 24,
}


def _interesting(widget: QWidget) -> bool:
    """这个控件值不值得体检。

    纯容器（QWidget / QFrame）本身不显示任何内容，被压缩不影响可读性，
    但它们下面的孩子会被逐个检查到，所以跳过容器不会漏掉真正的问题。

    **父链上有隐藏控件的一律跳过。** 一个被 `hide()` 的控件不会重新走布局，
    它的尺寸会冻结在隐藏前那一刻——`PlaylistView` 与其内部 `Viewport` 就是
    典型例子：列表为空时面板把列表藏起来改显示提示文字，此时
    `PlaylistView.geometry() == 521x333` 而它的 `Viewport` 还停在 `632x472`，
    比自己的父控件还大。这种数据拿去判"溢出"会得到纯属虚构的告警。
    隐藏的控件本来就不占屏幕，跳过它不是放水。
    """
    if not widget.isVisible():
        return False
    if widget.__class__.__name__ in _SKIP_CLASSES:
        return False
    if widget.objectName() in _SKIP_CLASSES:
        return False
    # 父链上任何一环被隐藏，本控件就处于"冻结的旧布局"里
    ancestor = widget.parentWidget()
    while ancestor is not None:
        if ancestor.isHidden():
            return False
        # QStackedWidget / QTabWidget 里"不是当前页"的那一页同样是冻结布局。
        # 它们在 Qt 看来仍然 isVisible()==True（因为父链都是可见的），
        # 但从来没走过一次 resize——`geom` 会一直停在构造时的默认 640x480。
        # 实测：面板默认停在第 0 页（播放列表），而"历史记录"那个
        # PlaylistView 报的就是 640x480，跟父控件的 521x333 对不上，
        # 于是被判成"溢出"。它其实只是还没轮到显示而已。
        stack = ancestor
        if hasattr(stack, "currentWidget") and hasattr(stack, "indexOf"):
            try:
                if stack.currentWidget() is not widget and not widget.isAncestorOf(
                    stack.currentWidget()
                ):
                    # 本控件不在当前页里 → 跳过
                    if not stack.currentWidget().isAncestorOf(widget):
                        return False
            except Exception:  # noqa: BLE001
                pass
        ancestor = ancestor.parentWidget()
    return widget.__class__.__name__ not in (
        "QWidget", "QFrame", "QStackedLayout", "QSplitterHandle",
    ) or bool(widget.objectName())


def _size_hint_floor(widget: QWidget) -> tuple[int, int]:
    """控件"再小就不可读"的尺寸下限。

    这里刻意**不用** `minimumSizeHint()`，因为它在两类控件上会骗人：

    - `QHeaderView`：`minimumSizeHint()` 会把所有 section 的宽度加起来当"最小宽度"
      （实测 62x62），而它实际只需要一行的高度（34px）。照它判会得到
      "表头比最小值矮一半"的假告警。
    - 带 `padding`/`border` 的视图：frame 的 `minimumSizeHint()` 不含样式表里
      加的那圈内边距，而 `geometry()` 含，两边口径不同。

    所以下限统一取 `sizeHint()`：它反映的是"控件自己认为舒服的尺寸"。

    **开了自动折行的 QLabel 要单独处理**：这种标签的 `sizeHint()` 报的是
    "一行都不折、全部铺开"的高度（一段两行的说明文字会报 102px），
    而实际情况是它按可用宽度折成两三行、总高反而更小。照 `sizeHint()`
    判会把所有正常的说明文字都报成"过矮"。对这种标签只要求
    "至少放得下一行文字"——真放不下的话，行高会被压到字体高度以下，
    那才是需要报的问题。
    """
    from PySide6.QtWidgets import QLabel

    cls = widget.__class__.__name__

    if isinstance(widget, QLabel) and widget.wordWrap():
        metrics = widget.fontMetrics()
        return 24, max(12, metrics.height())

    hinted = widget.sizeHint()
    floor_w = max(1, int(hinted.width() * 0.6))
    floor_h = max(1, int(hinted.height() * 0.6))

    # 输入类控件用"可用下限"兜底：它们的 minimumSizeHint 低到没有意义，
    # 只按比例算会漏掉真实的挤压（见 _USABLE_WIDTH 的注释说明）。
    usable_w = _USABLE_WIDTH.get(cls)
    if usable_w is not None:
        floor_w = max(floor_w, usable_w) if hinted.width() >= usable_w else floor_w
    usable_h = _USABLE_HEIGHT.get(cls)
    if usable_h is not None:
        floor_h = max(floor_h, usable_h) if hinted.height() >= usable_h else floor_h

    return floor_w, floor_h


def _visible_area(widget: QWidget) -> QRect:
    """控件真正能被用户看到的区域，坐标已换算到父控件坐标系。

    滚动区、列表这类控件内部有一个 `viewport()`，`geometry()` 量到的是连
    边框与滚动条在内的外框。判"挤占"要看可见区。

    **但视口的尺寸不能无条件信。** 视口只在控件自己被 resize 时才跟着 resize；
    一个被隐藏（例如播放列表为空时，面板把列表藏起来改显示空提示文字）、
    或者曾经有内容后来又变空的视图，会一直保留**上一次的视口尺寸**。
    实测过：`view.size() == 521x333` 而 `view.viewport().size() == 632x472`，
    差了一倍，照视口判就会报"溢出父控件 111 像素"这种根本不存在的告警。

    所以这里做一次一致性检查：只有当视口确实落在控件外框之内时，
    才用视口（这样能正确排除边框与滚动条）；否则说明它是脏数据，
    直接退回用控件外框——外框尺寸永远由布局管理器维护，不会过期。
    """
    viewport = getattr(widget, "viewport", None)
    if callable(viewport):
        try:
            vp = viewport()
            if isinstance(vp, QWidget) and vp.isVisible():
                # 视口必须真的在控件内部（差值 = 边框 + 滚动条，是几个像素的量级）
                if (
                    vp.width() <= widget.width()
                    and vp.height() <= widget.height()
                    and vp.width() > 0
                    and vp.height() > 0
                ):
                    parent = widget.parentWidget()
                    if parent is not None:
                        origin = vp.mapTo(parent, vp.rect().topLeft())
                        return QRect(origin, vp.size())
        except Exception:  # noqa: BLE001 - 量不出来就退回外框
            pass
    parent = widget.parentWidget()
    if parent is None:
        return QRect(0, 0, widget.width(), widget.height())
    return QRect(widget.mapTo(parent, QRect().topLeft()), widget.size())


def _label(widget: QWidget) -> str:
    name = widget.objectName()
    text = ""
    for getter in ("text", "currentText", "windowTitle"):
        value = getattr(widget, getter, None)
        if callable(value):
            try:
                text = str(value())
                break
            except Exception:  # noqa: BLE001 - 只为打印
                pass
    if len(text) > 26:
        text = text[:26] + "…"
    cls = widget.__class__.__name__
    parts = [cls]
    if name:
        parts.append(f"#{name}")
    if text:
        parts.append(f'"{text}"')
    return " ".join(parts)


def audit(widget: QWidget, *, label: str, verbose: bool) -> list[str]:
    """量一个控件树，返回问题描述列表（空列表 = 排版没问题）。

    判定分五类，都对应一种"用户看得出来"的坏现象：

    | 现象 | 用户看到什么 |
    |---|---|
    | 过窄 / 过矮 | 文字被省略号吃掉 |
    | 塌陷 | 控件消失了，但记得这里本来有东西 |
    | 溢出 | 控件被父容器裁掉一半 |
    | 挤压 | 内容还在，但挤成一团、比例明显不对 |
    | 重叠 | 两个控件压在一起，互相盖住 |
    """
    problems: list[str] = []
    root_rect = widget.rect()

    for child in widget.findChildren(QWidget):
        if not _interesting(child):
            continue
        desc = _label(child)
        rect = _visible_area(child)
        floor_w, floor_h = _size_hint_floor(child)
        hinted = child.sizeHint()

        # 1) 被压到小于"再小就不可读"的下限
        if floor_w > 12 and rect.width() < floor_w:
            problems.append(
                f"[过窄] {desc}: 实宽 {rect.width()} < 下限 {floor_w}"
                f"（建议 {hinted.width()}）"
            )
        if floor_h > 12 and rect.height() < floor_h:
            problems.append(
                f"[过矮] {desc}: 实高 {rect.height()} < 下限 {floor_h}"
                f"（建议 {hinted.height()}）"
            )

        # 2) 被压成不可见（用户以为控件不见了）
        if rect.width() <= 1 or rect.height() <= 1:
            if child.maximumWidth() > 1 and child.maximumHeight() > 1:
                problems.append(f"[塌陷] {desc}: {rect.width()}x{rect.height()}")

        # 3) 溢出父容器可视区
        parent = child.parentWidget()
        if parent is not None and parent is not widget:
            prect = parent.rect()
            if rect.right() > prect.right() + 2 or rect.bottom() > prect.bottom() + 2:
                problems.append(
                    f"[溢出] {desc}: {rect.getRect()} 超出父控件 {prect.getRect()}"
                )

        # 4) 明显比自己的建议尺寸小（>35%），说明被别的东西挤了
        if hinted.width() > 40 and rect.width() > 1:
            ratio = rect.width() / hinted.width()
            if ratio < 0.65:
                problems.append(
                    f"[挤压] {desc}: 实宽 {rect.width()} 仅为建议宽 {hinted.width()} 的"
                    f" {ratio:.0%}"
                )

    # 5) 兄弟控件两两重叠（同一父下的直接子控件）
    for parent in [widget, *widget.findChildren(QWidget)]:
        kids = [
            c for c in parent.children()
            if isinstance(c, QWidget) and c.isVisible() and _interesting(c)
            and c.parentWidget() is parent
        ]
        for i, a in enumerate(kids):
            for b in kids[i + 1:]:
                inter = a.geometry().intersected(b.geometry())
                if inter.width() > 2 and inter.height() > 2:
                    problems.append(
                        f"[重叠] {_label(a)} ∩ {_label(b)} = "
                        f"{inter.width()}x{inter.height()}"
                    )

    if verbose:
        print(f"  {label}: 检查了 {len(widget.findChildren(QWidget))} 个控件, "
              f"根尺寸 {root_rect.width()}x{root_rect.height()}, "
              f"问题 {len(problems)} 条")
    return problems


def run(*, shot: bool, verbose: bool) -> int:
    app = QApplication([sys.argv[0]])
    apply_theme(app)

    from src.ui.downloads_dialog import DownloadsDialog
    from src.ui.main_window import MainWindow
    from src.ui.settings_dialog import SettingsDialog

    if shot:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)

    total = 0

    # ---------------------------------------------------------- 主窗口
    print("=" * 74)
    print("主窗口")
    print("=" * 74)
    for width, height in MAIN_SIZES:
        window = MainWindow()
        window.resize(width, height)
        window.show()
        app.processEvents()
        # resize 之后 Qt 要跑一次布局才更新 geometry
        window.layout().activate() if window.layout() else None
        app.processEvents()

        problems = audit(window, label=f"MainWindow {width}x{height}", verbose=verbose)
        actual = f"{window.width()}x{window.height()}"
        print(f"\n-- 请求 {width}x{height} → 实际 {actual}，问题 {len(problems)} 条")
        for line in problems:
            print(f"   {line}")
        total += len(problems)

        if shot:
            path = SHOT_DIR / f"main-{width}x{height}.png"
            window.grab().save(str(path), "PNG")
            print(f"   截图: {path.name}")
        window.close()

    # ---------------------------------------------------------- 设置面板
    print("\n" + "=" * 74)
    print("设置面板（逐标签页）")
    print("=" * 74)
    for width, height in DIALOG_SIZES:
        dialog = SettingsDialog()
        dialog.resize(width, height)
        dialog.show()
        app.processEvents()
        for index in range(dialog.tabs.count()):
            dialog.tabs.setCurrentIndex(index)
            app.processEvents()
            name = dialog.tabs.tabText(index)
            problems = audit(
                dialog, label=f"Settings[{name}] {width}x{height}", verbose=verbose
            )
            print(f"\n-- {name} @ {width}x{height}，问题 {len(problems)} 条")
            for line in problems:
                print(f"   {line}")
            total += len(problems)
            if shot:
                path = SHOT_DIR / f"settings-{index}-{name}-{width}x{height}.png"
                dialog.grab().save(str(path), "PNG")
        dialog.close()

    # ---------------------------------------------------------- 下载面板
    print("\n" + "=" * 74)
    print("下载面板")
    print("=" * 74)
    window = MainWindow()
    for width, height in DIALOG_SIZES:
        dialog = DownloadsDialog(window.downloads)
        dialog.resize(width, height)
        dialog.show()
        app.processEvents()
        problems = audit(dialog, label=f"Downloads {width}x{height}", verbose=verbose)
        print(f"\n-- {width}x{height}，问题 {len(problems)} 条")
        for line in problems:
            print(f"   {line}")
        total += len(problems)
        if shot:
            dialog.grab().save(str(SHOT_DIR / f"downloads-{width}x{height}.png"), "PNG")
        dialog.close()

    print("\n" + "=" * 74)
    print(f"合计问题: {total} 条")
    if shot:
        print(f"截图目录: {SHOT_DIR}")
    print("=" * 74)
    return 1 if total else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="界面排版体检（量几何，找挤占）")
    parser.add_argument("--shot", action="store_true", help="顺便保存每个尺寸的截图")
    parser.add_argument("--quiet", action="store_true", help="不打印控件计数")
    args = parser.parse_args()
    return run(shot=args.shot, verbose=not args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
