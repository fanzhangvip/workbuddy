#!/usr/bin/env python
"""生成应用图标（app.ico）。

打包需要图标与版本信息（UI-14）。为了让仓库里不塞二进制文件，
这里用 Pillow 在构建时**动态生成**一个简洁的图标。

用法::

    python scripts/make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "ticket_helper" / "resources" / "app.ico"

# 配色：与界面主色一致（红底白字），辨识度高且符合购票场景
BACKGROUND = (192, 57, 43)
FOREGROUND = (255, 255, 255)
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def build_icon() -> Path:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("需要 Pillow：pip install pillow", file=sys.stderr)
        raise SystemExit(1)

    images = []
    for size in SIZES:
        image = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        radius = max(2, size[0] // 7)
        draw.rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=BACKGROUND)

        # 画一个简化的「车票」：中间一条虚线
        margin = max(2, size[0] // 6)
        y = size[1] // 2
        for x in range(margin, size[0] - margin, max(2, size[0] // 12)):
            draw.line([(x, y + size[1] // 5), (x + size[0] // 20, y + size[1] // 5)], fill=FOREGROUND, width=max(1, size[0] // 32))

        # 中央写「票」
        font = None
        for candidate in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            if Path(candidate).exists():
                try:
                    font = ImageFont.truetype(candidate, max(8, int(size[1] * 0.55)))
                    break
                except OSError:
                    continue
        text = "票"
        if font is not None:
            box = draw.textbbox((0, 0), text, font=font)
            draw.text(
                ((size[0] - (box[2] - box[0])) / 2 - box[0], (size[1] - (box[3] - box[1])) / 2 - box[1] - size[1] * 0.08),
                text,
                font=font,
                fill=FOREGROUND,
            )
        else:
            # 没有中文字体时退回画一个白色方块，保证图标仍然生成
            draw.rectangle([size[0] * 0.3, size[1] * 0.3, size[0] * 0.7, size[1] * 0.7], fill=FOREGROUND)
        images.append(image)

    # ICO 需要「一张基础图 + sizes 列表」，PIL 会据此生成多分辨率帧。
    # 若改用 append_images 会只保留第一帧（16x16），任务栏/资源管理器里会糊成一团。
    base = images[-1]  # 256x256
    base.save(TARGET, format="ICO", sizes=SIZES)
    print(f"图标已生成: {TARGET}")
    return TARGET


if __name__ == "__main__":
    build_icon()
