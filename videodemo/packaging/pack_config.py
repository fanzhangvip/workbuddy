"""打包配置（被 packaging/*.spec 与 build.py 共用）。

## 为什么可以大刀阔斧地排除 Qt 模块

用 pefile 实测了本环境里 Qt6 DLL 之间的**链接期依赖**（不是猜的）:

    Qt6Multimedia.dll -> Qt6Core.dll, Qt6Gui.dll, Qt6Network.dll
    Qt6Widgets.dll    -> Qt6Core.dll, Qt6Gui.dll
    Qt6Gui.dll        -> Qt6Core.dll
    Qt6Network.dll    -> Qt6Core.dll

也就是说除 Core / Gui / Widgets / Network 之外，QtMultimedia 不依赖任何其它
Qt6 库。src/ 里实际只 import 了 Core / Gui / Widgets / Multimedia /
MultimediaWidgets（QtNetwork 由 Multimedia 间接带入），因此把其余模块全部排除
不可能破坏链接依赖。

收益很实在：PySide6 安装目录共 641 MB，其中 Qt6WebEngineCore.dll 一个就占
194 MB，而这些全都与播放器无关。
"""

from __future__ import annotations

from pathlib import Path

# ------------------------------------------------------------------ 路径
PACKAGING_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGING_DIR.parent
ENTRY_SCRIPT = PROJECT_ROOT / "main.py"
ICON_FILE = PROJECT_ROOT / "assets" / "icon.ico"

# ------------------------------------------------------------------ 命名
APP_NAME = "UniversalURLPlayer"
DISPLAY_NAME = "万能 URL 视频播放器"
VERSION = "0.1.0"

# ------------------------------------------------------------------ 必须保留
#: 播放器真正用到的 Qt 模块，其余一律排除
KEEP_QT_MODULES = frozenset(
    {
        "QtCore",
        "QtGui",
        "QtWidgets",
        "QtMultimedia",
        "QtMultimediaWidgets",
        "QtNetwork",
    }
)

#: 这些模块被 `import` 时不显眼（在函数体内或由 hook 推导），必须显式声明
HIDDENIMPORTS = [
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetwork",
    "PySide6.support.deprecated",  # | 与 & 操作 Qt 枚举时需要
    "shiboken6",
    "inspect",
    # --- P4/P5 起新增：这几条都是在**函数体内**才 import 的 ---
    # 下载（直链走 requests，网页地址走 yt-dlp）
    "requests",
    "urllib3",
    "yt_dlp",
    # 本地反向代理与后端工厂里的惰性导入
    "src.core.local_proxy",
    "src.core.backends.mpv_backend",
    # 解析前的可达性预检（DoH 用的是标准库 http.client，无需额外声明）
    "src.core.reachability",
]

#: 可选依赖：**装上了才**加进隐藏导入。
#:
#: 这几个都不是必须的（缺了就优雅降级），所以没装时不该塞进 hiddenimports——
#: PyInstaller 会打印 "hidden import 'mpv' not found" 之类的警告，让人误以为
#: 打包出了问题。装了就一起带上：
#:   · `mpv`(python-mpv)      —— 让用户能用 libmpv 内核；
#:   · `imageio_ffmpeg`       —— 让网页地址下载能自动找到 ffmpeg 合流；
#:   · `curl_cffi`            —— 让 yt-dlp 能伪装浏览器 TLS 指纹。
#:
#: `curl_cffi` 必须显式声明：它是**编译扩展**，而且 yt-dlp 是在
#: `networking/_curlcffi.py` 里用 try/except 惰性导入的，
#: PyInstaller 的静态分析追不到，漏了它打包版就退化成"装没装一样"。
OPTIONAL_MODULES = ("mpv", "imageio_ffmpeg", "curl_cffi")

#: streamlink 是**插件式**的：约 130 个站点插件在 `streamlink.plugins.*` 下
#: 惰性注册，PyInstaller 静态分析完全追不到。它也不像上面三个是"单模块"，
#: 所以单列出来，用 `collect_submodules` 一次性把整个包（含插件）都收进来。
STREAMLINK_PACKAGE = "streamlink"


def optional_hiddenimports() -> list[str]:
    """当前环境里真实存在的可选依赖。"""
    import importlib.util  # noqa: PLC0415

    found: list[str] = []
    for name in OPTIONAL_MODULES:
        try:
            if importlib.util.find_spec(name) is not None:
                found.append(name)
        except (ImportError, ValueError):
            continue
    # streamlink 装了就整包收集（插件是动态注册的，不 collect 会静默缺失）
    try:
        if importlib.util.find_spec(STREAMLINK_PACKAGE) is not None:
            from PyInstaller.utils.hooks import collect_submodules

            found.extend(collect_submodules(STREAMLINK_PACKAGE))
    except (ImportError, ValueError):
        pass
    return found


#: libmpv 动态库可能出现的几个位置（Windows 命名 mpv-2.dll / libmpv-2.dll）。
#: 打包时若发现它，就作为 binary 一起收集，让打包版的 mpv 后端完整可用
#: （否则会变成"绑定了 python-mpv 却找不到库"的半残状态，自检会报错）。
_MPV_DLL_CANDIDATES = (
    PROJECT_ROOT / "mpv" / "libmpv-2.dll",
    PROJECT_ROOT / "mpv" / "mpv-2.dll",
    PROJECT_ROOT / "libmpv-2.dll",
    PROJECT_ROOT / "mpv-2.dll",
)

#: 随程序发布的 JavaScript 运行时（QuickJS-ng，约 2 MB，MIT）。
#:
#: 为什么要内置：yt-dlp 解析 YouTube 需要真正的 JS 引擎（跑挑战脚本），它自己
#: 只默认启用 deno，而用户机器上通常什么都没有——开发机上有 node，用户机上没有，
#: 于是"源码跑得好好的、打包版 YouTube 就废了"。详见 `src/utils/js_runtime.py`。
#:
#: 目标目录用 ``"jsrt"``：与仓库里的摆法一致，也不至于把 exe 同级目录搞乱。
#: `js_runtime.find_bundled()` 在 onedir / onefile 两种形态下都会查这个子目录。
_JS_RUNTIME_CANDIDATES = (
    PROJECT_ROOT / "jsrt" / "qjs.exe",
)


def optional_binaries() -> list[tuple[str, str]]:
    """当前环境里真实存在的可选二进制，形如 [(源路径, 目标子目录), ...]。

    libmpv 的目标目录用 ``"."``：onedir 版落在 exe 同级、onefile 版解压到
    `_MEIPASS`，这两处 `find_mpv_dll()` 都能找到。
    """
    found: list[tuple[str, str]] = []
    for candidate in _MPV_DLL_CANDIDATES:
        if candidate.is_file():
            found.append((str(candidate), "."))
            break
    for candidate in _JS_RUNTIME_CANDIDATES:
        if candidate.is_file():
            found.append((str(candidate), "jsrt"))
            break
    return found


#: 构建后要逐个确认"确实在产物里"的模块与符号。
#:
#: ## 为什么需要这个
#:
#: 自检能证明"产物能跑"，但证明不了"我这次改的那段代码在里面"。而**明文 grep
#: exe 是查不出来的**——模块被收进 `PYZ.pyz` 后是 zlib 压缩的，grep 必然找不到，
#: 于是很容易得出"没打进去"的错误结论（本项目真发生过这种误判）。
#:
#: 这份清单由 `tools/pyz_inspect.py` 解出 PYZ 后逐条核对，是较低成本的一道
#: 回归网：模块被漏收、被缓存成旧版、或某个关键函数被改名，构建时就会报出来。
#: 加新模块/新关键函数时顺手往这里补一条。
REQUIRED_SYMBOLS: dict[str, tuple[str, ...]] = {
    # P3 播放列表与历史
    "src.core.playlist": ("PlaylistModel", "EntryStore"),
    "src.ui.playlist_panel": ("PlaylistPanel", "PlaylistView"),
    # P4 下载
    "src.core.downloader": ("DownloadManager", "DownloadTask", "find_ffmpeg"),
    "src.ui.downloads_dialog": ("DownloadsDialog",),
    # P5 本地反向代理 / 设置 / 剪贴板
    "src.core.local_proxy": ("pick_header", "PARAM_USER_AGENT", "LocalProxyServer"),
    "src.ui.settings_dialog": ("SettingsDialog",),
    "src.utils.clipboard_watcher": ("ClipboardWatcher", "looks_like_playable"),
    # P6 可选 libmpv 后端
    "src.core.backends.mpv_backend": ("MpvBackend", "find_mpv_dll", "mpv_available"),
    "src.core.backends.factory": ("create_backend", "fallback_to_qt"),
    # P7 "尽可能拿到视频地址"：可达性预检 + DoH 兜底 + 多策略重试
    "src.core.reachability": ("probe", "doh_lookup", "register_override",
                              "_install_patcher", "Reachability"),
    "src.core.resolver": ("strategies_for", "is_retryable", "detect_js_runtime",
                          "impersonation_available", "Strategy",
                          "_impersonate_option", "_error_rank"),
    # 代理探测与内置 JS 运行时定位（打包版最容易在这两处"开发能跑、用户不能跑"）
    "src.utils.net_proxy": ("proxy_for", "system_proxy", "ProxyHit"),
    "src.utils.js_runtime": ("find_bundled", "auto_select", "BUNDLED_EXE"),
}

#: 与播放器无关的通用模块（Qt 与第三方）
#:
#: ⚠️ 硬性约束：**绝不能排除 setuptools 内部 vendor 的模块**。
#: 这类模块的完整名单见 setuptools/_vendor/，常见的有：
#: distutils, wheel, packaging, pyparsing, jaraco, backports, tomli,
#: typing_extensions, importlib_metadata, zipp, more_itertools, ordered_set,
#: platformdirs。
#:
#: 原因：PyInstaller 的 `utils/hooks/setuptools.py` 有一个 pre-safe-import 钩子，
#: 一旦依赖图里出现 setuptools（例如 yt-dlp 的 hook 会拉入 requests/urllib3/
#: setuptools），它就会把这些模块别名到 `setuptools._vendor.*`：
#:
#:     modulegraph.alias_module(real="setuptools._vendor.wheel", alias="wheel")
#:
#: 而如果 `wheel` 已被声明为排除项，别名动作会直接抛异常、整个构建以
#: exit code 1 失败，且报错非常难懂：
#:
#:     ValueError: Target module "wheel" already imported as
#:                 "ExcludedModule('wheel',)"
#:
#: 更麻烦的是：这个错误**取决于依赖图的遍历顺序**——同一份配置可能
#: "这次过了、下次不过"，或"目录版过了、单文件版不过"（本项目两种形态都真实发生过，
#: 因为两次构建之间源码里新增了一个 `import yt_dlp`）。
#: 触碰这个雷区的代价远大于省下的几 MB，所以这里只保留确定安全的条目。
OTHER_EXCLUDES = [
    # 标准库里体积大又用不到的（tkinter 会连带 tcl/tk 的 DLL，值得排）
    "tkinter",
    "_tkinter",
    "unittest",
    "pydoc",
    "doctest",
    "test",
    # 打包链自身，不该进产物（注意：pip 安全，wheel 不安全，见上方说明）
    "pip",
    # 若开发环境装过，避免被牵连进来
    "numpy",
    "PIL",
    "matplotlib",
    "cv2",
    "scipy",
    "pandas",
    "IPython",
    "pytest",
    "pdb",
]


def discover_qt_modules() -> list[str]:
    """列出当前环境里的 PySide6 Qt 扩展模块名。

    取不到时返回空列表 —— 此时"不排除任何模块"，产物偏大但一定可用。
    选择"偏大"而不是"可能坏掉"作为失败方向。
    """
    try:
        import PySide6  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []

    base = Path(PySide6.__file__).resolve().parent
    try:
        return sorted(path.stem for path in base.glob("Qt*.pyd"))
    except OSError:
        return []


def excluded_qt_modules() -> list[str]:
    """需要排除的 PySide6 模块全名。"""
    return [f"PySide6.{name}" for name in discover_qt_modules() if name not in KEEP_QT_MODULES]


def build_excludes() -> list[str]:
    qt = excluded_qt_modules()
    if not qt:
        print("[pack_config] 警告: 无法枚举 PySide6 模块，跳过 Qt 模块排除（产物会偏大）")
    else:
        print(f"[pack_config] 排除 {len(qt)} 个未使用的 Qt 模块")
    return qt + OTHER_EXCLUDES


def icon_path() -> str | None:
    """图标文件路径；不存在则返回 None（用 PyInstaller 默认图标）。"""
    return str(ICON_FILE) if ICON_FILE.is_file() else None


if __name__ == "__main__":  # 便于手动检查排除清单
    print(f"项目根目录: {PROJECT_ROOT}")
    print(f"入口脚本  : {ENTRY_SCRIPT}  (存在: {ENTRY_SCRIPT.is_file()})")
    print(f"图标      : {icon_path() or '<无，将使用默认图标>'}")
    print()
    print(f"保留的 Qt 模块: {', '.join(sorted(KEEP_QT_MODULES))}")
    print()
    print("排除的 Qt 模块:")
    for name in excluded_qt_modules():
        print(f"  {name}")
