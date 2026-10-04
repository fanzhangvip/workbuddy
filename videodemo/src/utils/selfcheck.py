"""运行时自检 —— 源码运行与打包后的 exe 都能用。

为什么需要它:
    打包失败最阴险的形态是「程序能启动、界面也出来了，但一播视频就报错」。
    典型原因是 QtMultimedia 的 FFmpeg 插件文件虽被复制进产物，但它依赖的
    avcodec / avformat 等 DLL 缺失，只有在真正加载插件时才暴露。
    因此这里不止检查"文件是否存在"，还用 QPluginLoader **实际加载**该插件。

用法:
    python main.py --self-check              # 结构自检
    python main.py --self-check <url>        # 结构自检 + 实播一个地址
    UniversalURLPlayer.exe --self-check <url> --offscreen

退出码: 0 表示通过，1 表示存在问题。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from src.utils.env_setup import describe_environment
from src.utils.paths import app_data_dir, is_frozen, log_dir

#: 自检报告文件名（写入日志目录，便于用户反馈问题时直接附上）
REPORT_NAME = "selfcheck.txt"

#: 可用环境变量覆盖报告路径。
#: 打包脚本借此把报告写到产物旁边，避免"先删旧报告再跑"——在受限环境（沙箱
#: 对批量删除有配额）里，删除动作会让整个验证环节失败。
REPORT_ENV = "VIDEODEMO_SELFCHECK_REPORT"


def report_path() -> Path:
    """自检报告写到哪里。"""
    override = os.environ.get(REPORT_ENV, "").strip()
    if override:
        return Path(override)
    return log_dir() / REPORT_NAME

#: 判断产物是否完整的关键文件（glob 模式）
CRITICAL_PATTERNS = (
    "QtMultimedia.pyd",
    "Qt6Multimedia.dll",
    "ffmpegmediaplugin.dll",
    "qwindows.dll",
    "avcodec-*.dll",
    "avformat-*.dll",
    "avutil-*.dll",
)


def _attach_parent_console() -> None:
    """打包为窗口程序后，把输出接回调用它的终端。

    console=False 的 exe 没有自己的控制台，直接 print 会丢弃内容。
    AttachConsole(ATTACH_PARENT_PROCESS) 可以复用启动它的那个终端。
    """
    if sys.platform != "win32" or not is_frozen():
        return
    try:
        import ctypes

        if not ctypes.windll.kernel32.AttachConsole(-1):
            return
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
    except Exception:  # noqa: BLE001 - 接不上终端不影响自检本身
        pass


def _resolve_page(url: str, say, problems: list[str]) -> str:
    """解析网页地址，成功返回可播放的流地址，失败返回空串（并记入 problems）。"""
    say("这是视频网站页面地址，先解析出真实流地址…")
    try:
        from src.core import resolver  # noqa: PLC0415

        result = resolver.resolve(url, log=say, verbose=True)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"网页地址解析异常: {url} -> {exc}")
        say(f"网页地址解析异常 -> {exc}")
        return ""

    for line in result.report().splitlines():
        say("  " + line)
    if not result.ok:
        problems.append(f"网页地址未取到单文件流: {url} -> {result.error}")
        return ""
    return result.url


def _prepare_probe_target(url: str, say, problems: list[str]) -> str:
    """实播测试前把地址准备好：已知网页地址先解析成真实流地址。

    不这样做的话，测的其实是"能不能打开一个 HTML 页面"——那种失败
    对判断播放能力毫无意义。

    注意这里**只处理已知站点**（``WEB_PAGE``）。看不出类型的 http 地址
    先按直链试、失败后再兜底解析，那一步在实播测试里做（与界面策略一致）。
    """
    from src.core.url_router import UrlKind  # noqa: PLC0415

    if _kind_of(url) is not UrlKind.WEB_PAGE:
        return url
    return _resolve_page(url, say, problems)


def _kind_of(url: str):
    """地址分类；分类失败返回 None（调用方当作"不值得兜底解析"处理）。"""
    from src.core.url_router import classify  # noqa: PLC0415

    try:
        return classify(url)
    except Exception:  # noqa: BLE001
        return None


def _probe_once(target: str, timeout_s: int, say, app) -> tuple[bool, str]:
    """跑一次实播探测，返回 (是否成功, 结论文字)。异常也当失败返回。"""
    try:
        from src.utils.playback_probe import probe_url  # noqa: PLC0415

        result = probe_url(target, timeout_s, log=say, app=app)
    except Exception as exc:  # noqa: BLE001
        say(f"实播测试异常 -> {exc}")
        return False, f"实播测试异常: {exc}"
    return result.ok, result.text


def _bundle_roots() -> list[Path]:
    """Qt 插件所在根目录（去重后返回）。

    - 打包运行: 产物目录（PyInstaller 6 的 onedir 把文件放在 exe 同级的
      ``_internal`` 下，故 exe 同级与 _MEIPASS 都要找）
    - 源码运行: PySide6 包目录，插件在其 ``plugins/`` 子目录下
    """
    candidates: list[Path] = []
    if is_frozen():
        # _MEIPASS 就是真实的打包根目录：目录版指向 `_internal`，单文件版指向
        # 解包出的临时目录。只用它，免得把旁边别的产物也算进来。
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and Path(meipass).is_dir():
            candidates.append(Path(meipass))
        else:
            candidates.append(Path(sys.executable).resolve().parent)
    else:
        try:
            import PySide6

            candidates.append(Path(PySide6.__file__).resolve().parent)
        except Exception:  # noqa: BLE001
            pass

    roots: list[Path] = []
    for path in candidates:
        if path and path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def _find_files(patterns: tuple[str, ...]) -> dict[str, list[Path]]:
    """在产物里按模式查找关键文件。"""
    found: dict[str, list[Path]] = {name: [] for name in patterns}
    for root in _bundle_roots():
        for name in patterns:
            try:
                found[name].extend(root.rglob(name))
            except OSError:
                pass
    return found


def _check_extra_features(say, problems: list[str], qapp) -> None:
    """P3–P6 附加功能的运行时自检。

    这一段的原则和上面几段一致：**只报"真的能不能用"，不报"配置写没写"**。
    所以每一项都尽量落到一个动作上——真去写一次文件、真去绑一次端口、
    真去读一次剪贴板。光看配置值等于没查。

    同时刻意**不制造副作用**：播放列表/历史文件只读不写（`EntryStore.load()`
    遇到坏文件会改名留档，那是用户点开程序时才该发生的事，不该由自检代劳）。
    """
    from src.utils import settings as S  # noqa: PLC0415
    from src.utils.paths import app_data_dir, data_file, default_download_dir  # noqa: PLC0415

    say()
    say("=" * 68)
    say("附加功能（P3–P6）")
    say("=" * 68)

    def probe_dir(label: str, path: Path) -> None:
        """目录可写性：真写一个探针文件再删掉。"""
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            say(f"  [可写] {label:<10} {path}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{label}不可写: {path} -> {exc}")
            say(f"  [不可写] {label:<10} {path} -> {exc}")

    # ---------------------------------------------------------- 播放后端
    say("播放后端:")
    try:
        from src.core.backends import factory  # noqa: PLC0415

        say("  " + factory.describe_backends())
        choice = S.get_str(S.K_BACKEND, "qt").lower()
        say(f"  设置选择 : {choice}")
        if choice == "mpv" and not factory.mpv_available():
            problems.append(
                "设置里选了 mpv 内核，但 python-mpv 不可用；"
                "运行时会被静默回退到 Qt（RTSP 只能走 UDP）"
            )
    except Exception as exc:  # noqa: BLE001
        problems.append(f"后端工厂不可用: {exc}")
        say(f"  后端工厂不可用 -> {exc}")

    # ---------------------------------------------------------- libmpv 探测
    say("libmpv（可选）:")
    try:
        from src.core.backends.mpv_backend import (  # noqa: PLC0415
            INSTALL_HINT,
            binding_installed,
            find_mpv_dll,
            mpv_available,
            probe_dll,
        )

        bound = binding_installed()
        dll = find_mpv_dll()
        usable = mpv_available()
        say(f"  python-mpv  : {'已安装' if bound else '未安装（可选，不影响 Qt 内核）'}")
        say(f"  libmpv 库   : {dll or '未找到'}")
        if usable:
            ok, message = probe_dll()
            say(f"  实际加载    : {'成功' if ok else '失败'} {message}")
            if not ok:
                problems.append(f"libmpv 已配置但加载失败: {message}")
        elif bound:
            # 装了绑定却加载不了库：这是"配了却用不起来"，必须当成问题报出来。
            # 此时用户若在设置里选了 mpv 内核，只会被静默回退到 Qt，
            # 而 RTSP 只能走 UDP 这个限制会原封不动地留着。
            _, message = probe_dll()
            say(f"  实际加载    : 失败 {message}")
            problems.append(f"python-mpv 已安装但 libmpv 动态库不可用: {message}")
            say("  安装说明:")
            for line in INSTALL_HINT.strip().splitlines():
                say(f"    {line}")
        else:
            say("  实际加载    : 跳过（未装绑定）")
            say("  安装说明:")
            for line in INSTALL_HINT.strip().splitlines():
                say(f"    {line}")
    except Exception as exc:  # noqa: BLE001
        say(f"  mpv 探测异常 -> {exc}")

    # ---------------------------------------------------------- 网页地址解析
    say("网页地址解析（P2/P7）:")
    try:
        from src.core import reachability, resolver  # noqa: PLC0415

        say("  " + resolver.describe_availability())
        say("  " + resolver.describe_streamlink())

        runtime = resolver.detect_js_runtime()
        if runtime:
            shown = "、".join(
                f"{name}{'（内置）' if str(cfg.get('path', '')).endswith('qjs.exe') else ''}"
                for name, cfg in runtime.items()
            )
        else:
            shown = ""
        say(f"  JS 运行时   : {shown or '**未找到**'}")
        if not runtime:
            # 如实说：以前写成"少拿到一些格式"，实际表现是 YouTube 直接解析失败
            problems.append(
                "没有可用的 JavaScript 运行时：YouTube 会报「需要确认不是机器人」而"
                "解析失败。装一个 Node.js 即可（或把 qjs.exe 放到 jsrt/ 下）"
            )

        say("  " + resolver.describe_impersonation())
        if not resolver.impersonation_available():
            problems.append(
                "缺少 curl_cffi：无法伪装浏览器 TLS 指纹，"
                "按指纹拦截的站点（Cloudflare 一类）会解析失败"
            )

        say(f"  解析前预检  : {'开' if S.resolve_preflight_enabled() else '关'}")
        say(f"  DNS 兜底    : {'开' if S.doh_fallback_enabled() else '关'}"
            + (f"（{S.doh_endpoint()}）" if S.doh_endpoint() else "（内置端点）"))
        say(f"  失败重试    : {'开' if S.resolve_retry_strategies_enabled() else '关'}"
            f"（{len(resolver.strategies_for('https://example.com/a'))} 种方式）")

        # 真查一次 DoH：光看配置值说明不了"这个网络里 DoH 通不通"
        if S.doh_fallback_enabled():
            import time as _time  # noqa: PLC0415

            started = _time.monotonic()
            ips = reachability.doh_lookup("example.com")
            cost = int((_time.monotonic() - started) * 1000)
            if ips:
                say(f"  DoH 实测    : 可用（{cost} ms）-> {', '.join(ips[:3])}")
            else:
                say(f"  DoH 实测    : 不可用（{cost} ms）—— 该网络里所有端点都没响应")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"网页地址解析相关模块不可用: {exc}")
        say(f"  不可用 -> {exc}")

    # ---------------------------------------------------------- 下载
    say("下载（P4）:")
    try:
        from src.core import downloader  # noqa: PLC0415

        try:
            import requests  # noqa: PLC0415

            say(f"  requests    : {requests.__version__}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"requests 不可用，直链下载会失败: {exc}")
            say(f"  requests    : 不可用 -> {exc}")

        found_ffmpeg, ffmpeg_text = downloader.find_ffmpeg()
        say(f"  ffmpeg      : {'已找到' if found_ffmpeg else '未找到'} {ffmpeg_text}")
        if not found_ffmpeg:
            # 缺 ffmpeg 不是致命问题：直链下载不受影响，网页地址只是拿不到
            # 合并了音视频的版本（会退化成无声画面或单独音频）。如实说明即可。
            say("    （影响：网页地址只能取到音视频分离的版本之一；直链下载不受影响）")
            say("    安装办法：")
            for line in downloader.FFMPEG_HINT.strip().splitlines():
                say(f"      {line}")

        concurrency = S.get_int(S.K_DOWNLOAD_CONCURRENCY, 2)
        say(f"  并发上限    : {concurrency}")
        if concurrency < 1:
            problems.append(f"下载并发上限配置无效: {concurrency}")
        probe_dir("下载目录", downloader.DownloadManager().download_dir)
        configured_dir = S.get_str(S.K_DOWNLOAD_DIR, "")
        say(f"  默认下载目录: {default_download_dir()}")
        if configured_dir:
            say(f"  已配置目录  : {configured_dir}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"下载模块不可用: {exc}")
        say(f"  下载模块不可用 -> {exc}")

    # ---------------------------------------------------------- 播放列表 / 历史
    say("播放列表与历史（P3）:")
    for label, name in (("播放列表", "playlist.json"), ("历史记录", "history.json")):
        path = data_file(name)
        if not path.exists():
            say(f"  {label:<8}: 尚无文件（首次使用时创建）{path}")
            continue
        try:
            import json  # noqa: PLC0415

            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                count = len(payload.get("items") or [])
            elif isinstance(payload, list):
                count = len(payload)
            else:
                count = 0
            say(f"  {label:<8}: {count} 条 {path}")
        except Exception as exc:  # noqa: BLE001
            # 只报告，不改名：留档动作交给真正打开程序时的 EntryStore
            problems.append(f"{label}文件无法解析: {path} -> {exc}")
            say(f"  {label:<8}: 无法解析 -> {exc}")
    # 数据目录单独探一次：EntryStore.save() 就是往这里写，写不了等于
    # 播放列表和历史全是"关掉就丢"，必须报出来。
    probe_dir("数据目录", app_data_dir())

    # ---------------------------------------------------------- VPN / 代理
    # 只读，不发请求：自检要能在断网机器上跑完（网络连通性交给
    # 设置面板的「测试连通性」与 tools/vpn_check.py）。
    say("VPN / 代理:")
    try:
        from src.utils import proxy_check  # noqa: PLC0415

        app_proxy = S.get_str(S.K_PROXY, "").strip()
        for item in proxy_check.sources(app_proxy):
            say(f"  {item.label:<12}: {item.value or '（未配置）'}")
        hit = proxy_check.active_hit(app_proxy)
        if hit is None:
            say("  当前生效    : 无 —— 解析/下载/播放都走直连")
            say("               （用 TUN／全局模式的 VPN 时，这是正常现象）")
        else:
            say(f"  当前生效    : {hit.source} · {hit.proxy}")
            say("               （播放会自动改走本地反向代理转发代理流量）")
        if not proxy_check.socks_ready():
            say("  SOCKS 支持  : 缺少 PySocks —— socks5:// 代理不可用")
    except Exception as exc:  # noqa: BLE001
        say(f"  检测失败 -> {exc}")

    # ---------------------------------------------------------- 本地反向代理
    say("本地反向代理（P5）:")
    try:
        from src.core import local_proxy  # noqa: PLC0415

        enabled = S.local_proxy_enabled()
        wanted_port = S.get_int(S.K_LOCAL_PROXY_PORT, 0)
        say(f"  设置        : {'启用' if enabled else '未启用'}"
            f" · 端口 {'自动' if wanted_port == 0 else wanted_port}")
        server = local_proxy.ensure_started(wanted_port)
        if server is None:
            message = f"本地反向代理无法启动（端口 {wanted_port or '自动'}）"
            if enabled:
                problems.append(
                    f"{message}——代理处于启用状态，防盗链地址将无法播放"
                    "（端口可能被占用）"
                )
            say(f"  实际启动    : 失败 -> {message}")
        else:
            ok = server.health_check()
            say(f"  实际启动    : {'成功' if ok else '已监听但健康检查失败'}"
                f" · {server.status_url()}")
            if not ok:
                problems.append("本地反向代理已监听但 /status 健康检查失败")
            local_proxy.stop()
            say("  已关闭（自检用，不属于运行状态）")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"本地反向代理不可用: {exc}")
        say(f"  不可用 -> {exc}")

    # ---------------------------------------------------------- 剪贴板
    say("剪贴板监听（P5）:")
    try:
        from src.utils.clipboard_watcher import ClipboardWatcher, looks_like_playable  # noqa: PLC0415

        say(f"  设置        : 监听{'开启' if S.get_bool(S.K_CLIPBOARD_WATCH, False) else '关闭'}"
            f" · 自动填入{'开启' if S.get_bool(S.K_CLIPBOARD_AUTOFILL, True) else '关闭'}")
        watcher = ClipboardWatcher()
        if qapp is None:
            say("  可用性      : 跳过（没有 QApplication）")
        else:
            # 不 set_enabled：自检不该真的开始监听用户剪贴板
            board = qapp.clipboard()
            sample = board.text() if board is not None else ""
            say(f"  剪贴板可读  : 是（当前 {len(sample)} 字符）")
            verdict = looks_like_playable("https://example.com/a.mp4")
            say(f"  识别逻辑    : {'正常' if verdict else '异常'}")
            if not verdict:
                problems.append("剪贴板识别逻辑异常：普通媒体直链未被识别")
        watcher.deleteLater()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"剪贴板模块不可用: {exc}")
        say(f"  不可用 -> {exc}")

    # ---------------------------------------------------------- 设置项
    say("设置项完整性:")
    try:
        snapshot = S.as_dict()
        say(f"  已登记 {len(snapshot)} 项 · 配置文件 {S.settings().fileName()}")
        missing = [k for k in S.DEFAULTS if S.default_for(k) is None]
        if missing:
            problems.append(f"以下设置项没有默认值: {', '.join(missing)}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"设置模块不可用: {exc}")
        say(f"  不可用 -> {exc}")


def run_self_check(url: str | None = None, timeout_s: int = 15) -> int:
    """执行自检。返回进程退出码。"""
    _attach_parent_console()

    try:  # 中文输出到 cp936 控制台时会炸，降低编码要求
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    report: list[str] = []
    problems: list[str] = []

    def say(line: str = "") -> None:
        report.append(line)
        print(line)

    def banner(title: str) -> None:
        say()
        say("=" * 68)
        say(title)
        say("=" * 68)

    # ---------------------------------------------------------- 1. 运行形态
    banner("运行形态")
    frozen = is_frozen()
    say(f"运行方式 : {'打包后的可执行文件' if frozen else '源码'}")
    say(f"解释器   : {sys.executable}")
    if frozen:
        say(f"产物根目录: {Path(sys.executable).resolve().parent}")
        say(f"_MEIPASS  : {getattr(sys, '_MEIPASS', '<无>')}")

    # ---------------------------------------------------------- 2. 版本
    banner("依赖与版本")
    try:
        import PySide6
        import shiboken6
        from PySide6.QtCore import qVersion

        say(f"Python   : {sys.version.split()[0]}")
        say(f"PySide6  : {PySide6.__version__}")
        say(f"shiboken6: {shiboken6.__version__}")
        say(f"Qt       : {qVersion()}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"PySide6 导入失败: {exc}")
        say(f"PySide6  : 导入失败 -> {exc}")

    # ---------------------------------------------------------- 3. 环境变量
    banner("环境变量（必须在 QApplication 之前生效）")
    say(describe_environment())

    # ---------------------------------------------------------- 4. 关键文件
    if frozen:
        banner("产物完整性（关键文件）")
        found = _find_files(CRITICAL_PATTERNS)
        for name, paths in found.items():
            if paths:
                for path in paths:
                    size = path.stat().st_size / 1024 / 1024
                    say(f"  [有] {name:<24} {size:6.1f} MB  {path}")
            else:
                say(f"  [无] {name}")
                problems.append(f"产物缺少 {name}")

    # ---------------------------------------------------------- 5. Qt 应用与插件真实加载
    banner("QtMultimedia 插件加载（决定性检查）")
    qapp = None
    try:
        from PySide6.QtCore import QCoreApplication, QLibraryInfo
        from PySide6.QtWidgets import QApplication

        say("Qt 插件搜索路径:")
        for path in QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath), *QCoreApplication.libraryPaths():
            say(f"  {path}")

        qapp = QApplication.instance() or QApplication(sys.argv[:1])
        say("QApplication 创建成功")

        plugin_candidates = _find_files(("ffmpegmediaplugin.dll",))["ffmpegmediaplugin.dll"]
        if not plugin_candidates:
            problems.append("未找到 ffmpegmediaplugin.dll（FFmpeg 媒体后端插件）")
            say("未找到 ffmpegmediaplugin.dll —— 视频将无法解码")
        else:
            from PySide6.QtCore import QPluginLoader

            for candidate in plugin_candidates:
                loader = QPluginLoader(str(candidate))
                loaded = loader.load()
                mark = "可加载" if loaded else "加载失败"
                say(f"  {mark}: {candidate.name}")
                if not loaded:
                    message = loader.errorString()
                    say(f"         原因: {message}")
                    problems.append(f"媒体插件加载失败: {message}")
                else:
                    metadata = loader.metaData()
                    # PySide6 把 QJsonObject 暴露成 dict，PyQt 则返回对象，两者都兼容
                    iid = (
                        metadata.get("IID", "")
                        if isinstance(metadata, dict)
                        else metadata.value("IID", "")
                    )
                    say(f"         元数据: {iid}")
                    loader.unload()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"Qt 初始化失败: {exc}")
        say(f"Qt 初始化失败 -> {exc}")

    # ---------------------------------------------------------- 6. 界面层构造
    banner("界面层构造")
    if qapp is not None:
        try:
            from PySide6.QtMultimediaWidgets import QVideoWidget

            probe_widget = QVideoWidget()
            say("QVideoWidget 创建成功（QtMultimediaWidgets 可用）")
            probe_widget.deleteLater()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"QVideoWidget 不可用: {exc}")
            say(f"QVideoWidget 失败 -> {exc}")

        try:
            from src.ui.main_window import MainWindow

            window = MainWindow()
            say(f"主窗口构造成功（标题: {window.windowTitle()}）")
            window.close()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"主窗口构造失败: {exc}")
            say(f"主窗口构造失败 -> {exc}")

    # ---------------------------------------------------------- 7. 目录可写性
    banner("读写目录")
    for label, path in (("配置/日志", app_data_dir()), ("日志", log_dir())):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe_file = path / ".write_probe"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink()
            say(f"  [可写] {label:<8} {path}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{label}目录不可写: {path} -> {exc}")
            say(f"  [不可写] {label:<8} {path} -> {exc}")

    # ---------------------------------------------------------- 8. 网页地址解析
    banner("网页地址解析（yt-dlp）")
    try:
        from src.core import resolver  # noqa: PLC0415

        say(resolver.describe_availability())
        if not resolver.is_available():
            problems.append("yt-dlp 不可用：网页地址（B站/YouTube 等）将无法解析")
            say("安装办法：")
            say("  " + resolver.INSTALL_HINT.replace("\n", "\n  "))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"解析模块不可用: {exc}")
        say(f"解析模块不可用 -> {exc}")

    # ---------------------------------------------------------- 9. 附加功能（P3–P6）
    _check_extra_features(say, problems, qapp)

    # ---------------------------------------------------------- 10. 实播测试
    if url:
        banner(f"实播测试: {url}")
        target = _prepare_probe_target(url, say, problems)
        if target:
            ok, text = _probe_once(target, timeout_s, say, qapp)
            if not ok:
                # 与界面一致：看不出类型的 http 地址（既不是媒体直链、也不在已知
                # 站点表里）先直连试一次，失败后自动交给解析器再试一次。
                # 很多站点自建的播放页正属于这一类，所以这条兜底必须一起验证。
                resolved = ""
                from src.core.url_router import UrlKind  # noqa: PLC0415

                if target == url and _kind_of(url) is UrlKind.UNKNOWN_HTTP:
                    say("直连未成功，按界面策略改用解析器再试一次…")
                    resolved = _resolve_page(url, say, problems)
                    if resolved:
                        ok, text = _probe_once(resolved, timeout_s, say, qapp)
                        target = resolved
                if not ok:
                    problems.append(f"地址无法播放: {target} -> {text}")
    else:
        banner("实播测试")
        say("未提供地址，跳过。可这样验证解码链路：")
        say(f"  {Path(sys.executable).name} --self-check <视频地址>")

    # ---------------------------------------------------------- 结论
    banner("自检结论")
    if problems:
        say(f"发现 {len(problems)} 个问题:")
        for index, item in enumerate(problems, 1):
            say(f"  {index}. {item}")
    else:
        say("未发现问题，具备播放能力。")

    text = "\n".join(report) + "\n"
    try:
        target = report_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"\n报告已写入: {target}")
    except Exception:  # noqa: BLE001
        pass

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(run_self_check(sys.argv[1] if len(sys.argv) > 1 else None))
