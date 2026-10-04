"""生成程序图标 assets/icon.ico。

用 Qt 直接画，不依赖任何外部素材：圆角方块 + 播放三角。
输出为多尺寸 ICO（16/24/32/48/64/128/256），每层用 PNG 压缩
（Vista 及以上支持，体积小且缩放清晰）。

用法:
    ./.venv/Scripts/python.exe tools/make_icon.py
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "icon.ico"

# 没桌面会话也要能跑
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QBrush,
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
)

#: ICO 里包含的尺寸。小尺寸给足三角占比，否则糊成一团。
SIZES = (16, 24, 32, 48, 64, 128, 256)

COLOR_TOP = "#5B9BFF"
COLOR_BOTTOM = "#1240B8"
COLOR_MARK = "#FFFFFF"


def render(size: int) -> QImage:
    """画一枚图标。"""
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # --- 圆角方块底 ---
    margin = size * 0.045
    body = QRectF(margin, margin, size - margin * 2, size - margin * 2)
    radius = size * 0.22

    gradient = QLinearGradient(body.topLeft(), body.bottomRight())
    gradient.setColorAt(0.0, QColor(COLOR_TOP))
    gradient.setColorAt(1.0, QColor(COLOR_BOTTOM))

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(gradient))
    painter.drawRoundedRect(body, radius, radius)

    # --- 播放三角 ---
    # 尺寸小时三角占比调大，保证辨识度
    ratio = 0.46 if size >= 32 else 0.56
    height = size * ratio
    width = height * 0.86

    # 三角形视觉重心偏左，整体右移一点才是"居中"
    center_x = size / 2 + width * 0.08
    center_y = size / 2
    left = center_x - width / 2

    triangle = QPainterPath()
    triangle.moveTo(left, center_y - height / 2)
    triangle.lineTo(center_x + width / 2, center_y)
    triangle.lineTo(left, center_y + height / 2)
    triangle.closeSubpath()

    painter.setBrush(QColor(COLOR_MARK))
    painter.drawPath(triangle)

    painter.end()
    return image


def png_bytes(image: QImage) -> bytes:
    """把 QImage 编码成 PNG 字节。"""
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError(f"PNG 编码失败: {image.width()}x{image.height()}")
    data = bytes(buffer.data())
    buffer.close()
    return data


def write_ico(path: Path, frames: list[QImage]) -> None:
    """按 ICO 格式写文件（每层为 PNG 数据）。"""
    payloads = [png_bytes(frame) for frame in frames]

    header = struct.pack("<HHH", 0, 1, len(frames))  # reserved, type=icon, count
    offset = len(header) + 16 * len(frames)

    entries = bytearray()
    for frame, payload in zip(frames, payloads):
        side = frame.width()
        # ICO 里 256 用 0 表示
        dimension = 0 if side >= 256 else side
        entries += struct.pack(
            "<BBBBHHII",
            dimension,  # 宽
            dimension,  # 高
            0,  # 调色板数量（真彩为 0）
            0,  # 保留
            1,  # 颜色平面
            32,  # 每像素位数
            len(payload),
            offset,
        )
        offset += len(payload)

    blob = bytes(header) + bytes(entries) + b"".join(payloads)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])

    frames = [render(size) for size in SIZES]
    write_ico(OUT, frames)

    print(f"已生成 {OUT}  （{OUT.stat().st_size / 1024:.1f} KB）")
    print(f"包含尺寸: {', '.join(str(s) for s in SIZES)}")

    # 顺带导出 PNG 便于肉眼检查（不进产物）
    preview = ROOT / "docs" / "screenshots" / "icon-preview.png"
    preview.parent.mkdir(parents=True, exist_ok=True)
    gap = 8
    strip_width = sum(frame.width() for frame in frames) + gap * (len(frames) - 1)
    strip_height = max(frame.height() for frame in frames)
    strip = QImage(strip_width, strip_height, QImage.Format.Format_ARGB32_Premultiplied)
    strip.fill(QColor("#F5F6F8"))  # 浅灰底，便于看清图标边缘
    painter = QPainter(strip)
    x = 0
    for frame in frames:
        painter.drawImage(x, strip_height - frame.height(), frame)
        x += frame.width() + gap
    painter.end()
    strip.save(str(preview), "PNG")
    print(f"预览图: {preview}  （{strip_width}x{strip_height}）")

    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
