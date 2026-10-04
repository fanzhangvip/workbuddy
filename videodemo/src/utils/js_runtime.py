"""JavaScript 运行时（JS runtime）的定位：内置优先，其次 PATH。

## 为什么需要这个模块

yt-dlp 从 2026 版起，**没有 JS 运行时的 YouTube 解析就废了**：它内部只默认启用
`deno`，而绝大多数 Windows 机器上并没有 deno（Google 的登录页/播放器挑战要跑
一段 JS，yt-dlp 得有个真正的 JS 引擎去执行）。缺了运行时的表现是：

    [youtube] No supported JavaScript runtime could be found. Only deno is
    enabled by default; ...
    [youtube] No title found in player responses; falling back to title from
    initial data. Other metadata may also be missing

元数据缺失、可用格式变少，YouTube 还会直接回一个机器人校验。

## 为什么不能只靠 PATH

"从 PATH 里找 node"在开发者机器上总是成立，在**用户机器上经常不成立**：装了
Node.js 的人不少，没装的更多；而 nvm / fnm / scoop / 各种工具链自带的 node
往往只存在于某个会话的 PATH 里，用户双击 exe 启动时根本看不见。

这个坑本项目真踩过：整台机器上唯一的 node 来自工具运行时、不在持久 PATH 中，
于是**开发会话里 YouTube 一切正常，打包版却报"没有可用的 JavaScript 运行时"**。
所以这里优先用随程序一起发布的运行时，找不到再退到 PATH 探测。

## 内置的是什么

`jsrt/qjs.exe` —— QuickJS-ng（MIT），Windows x64 静态单文件，约 2 MB。
选它而不是 deno/node 的原因只有一个：**体积**（deno 约 40 MB、node 约 100 MB），
而三者在 yt-dlp 眼里是等价的执行后端。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

#: 随程序发布的运行时文件名与它在 yt-dlp 里的运行时名。
#: 名字必须落在 `yt_dlp.globals.supported_js_runtimes` 里（deno/node/bun/quickjs），
#: 否则 yt-dlp 会直接忽略这个键。
BUNDLED_EXE = "qjs.exe"
BUNDLED_NAME = "quickjs"

#: 从 PATH 里探测的顺序。yt-dlp 会按自己的偏好（node > quickjs）挑，顺序只影响日志。
PATH_RUNTIMES = ("deno", "node", "bun", "quickjs")

#: 全都找不到时给用户看的一行话。**措辞要如实**：以前写的是"不影响多数站点，
#: 但 YouTube 会少一些格式"，而实际表现是 YouTube 直接解析失败（机器人校验），
#: 说轻了会让人以为不用管。
MISSING_HINT = (
    "没有可用的 JavaScript 运行时（deno / node / bun / quickjs 都没找到，"
    "内置的 jsrt/qjs.exe 也不在）；YouTube 会退化成无 JS 的接口并报"
    "「需要确认不是机器人」。装一个 Node.js 或在设置里指定 JS 运行时路径都可以。"
)


def _search_dirs() -> list[Path]:
    """内置运行时的候选目录，按优先级排列。

    覆盖三种运行形态：
      · 源码运行   —— `<项目根>/jsrt/`
      · 目录版 exe —— `<exe 同级>/jsrt/`（spec 把二进制收在根目录，兼容两种摆法）
      · 单文件 exe —— `_MEIPASS/jsrt/`（onefile 会解压到临时目录）
    """
    from src.utils.paths import app_root, is_frozen

    root = app_root()
    dirs = [root / "jsrt", root]
    if is_frozen():
        meipass = Path(getattr(sys, "_MEIPASS", root))
        if meipass != root:
            dirs += [meipass / "jsrt", meipass]
    return dirs


def find_bundled() -> Path | None:
    """随程序发布的那份运行时；没有则返回 None。"""
    for directory in _search_dirs():
        candidate = directory / BUNDLED_EXE
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def auto_select() -> dict:
    """自动挑可用的运行时，返回可以直接塞进 yt-dlp `js_runtimes` 的字典。

    内置的排第一，PATH 里探测到的也一并交上去——yt-dlp 会按自己的偏好
    （node > quickjs）选一个，版本太旧/不可用的会被它自己跳过，所以多给几个
    不会出错，反而能"有 node 就用 node，没有就用内置的"。

    一个都没有时返回空字典（沿用 yt-dlp 默认），**绝不报错**。
    """
    found: dict = {}

    bundled = find_bundled()
    if bundled is not None:
        found[BUNDLED_NAME] = {"path": str(bundled)}

    for name in PATH_RUNTIMES:
        if name in found:
            continue
        exe = shutil.which(name)
        if exe:
            found[name] = {"path": exe}

    return found
