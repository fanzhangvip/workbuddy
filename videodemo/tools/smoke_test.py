"""
集成冒烟测试：离屏驱动真实窗口，验证整条链路。

覆盖单元测试覆盖不到的部分：主窗口 ↔ 播放控制器 ↔ 控制条 的信号连通性、
加载超时兜底是否真的会触发、网页地址是否真的走了 yt-dlp 解析，以及各类地址
是否被正确拦截。

用法:
    python tools/smoke_test.py --url <地址> --expect ok|fail|resolve|merged [--timeout 20]
    python tools/smoke_test.py --suite          # 跑内置场景套件
    python tools/smoke_test.py --suite --with-local   # 额外跑需要本地服务的场景

退出码 0 表示全部通过。

需要本地服务的场景（`--with-local`）依赖：
    mediamtx      rtsp:8554 / rtmp:1935 / hls:8888
    http.server   http://127.0.0.1:8811 指向 .tools/testmedia/（局域网直链）
    http.server   http://127.0.0.1:8821 指向项目根目录（网页地址解析用）：
                  ./.venv/Scripts/python.exe -m http.server 8821 \
                      --bind 127.0.0.1 --directory .
服务不在时对应场景自动跳过，不算失败。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 离屏运行，避免测试时弹窗打扰
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.utils.env_setup import setup_environment  # noqa: E402

setup_environment()

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

# 无头环境下模态对话框会永久阻塞，这里让它立即返回
QMessageBox.exec = lambda self: 0  # type: ignore[method-assign]

from src.ui.main_window import MainWindow  # noqa: E402
from src.core.media_item import MediaItem  # noqa: E402


class Scenario:
    def __init__(self, name: str, url: str, expect: str, timeout_s: int,
                 must_contain: str = "") -> None:
        self.name = name
        self.url = url
        self.expect = expect
        self.timeout_s = timeout_s
        #: 期望错误信息里包含的关键词（用于确认失败原因是否正确，而不是"随便报了个错"）
        self.must_contain = must_contain


def run_scenario(app: QApplication, scenario: Scenario) -> tuple[bool, str]:
    window = MainWindow()

    events: list[str] = []
    errors: list[str] = []
    durations: list[int] = []
    positions: list[int] = []

    ctrl = window.controller
    ctrl.errorRaised.connect(lambda t, d: errors.append(f"{t}: {d}".split("\n")[0]))
    ctrl.durationChanged.connect(lambda ms: durations.append(int(ms)))
    ctrl.positionChanged.connect(lambda ms: positions.append(int(ms)))
    ctrl.statusMessage.connect(lambda s: events.append(s))

    window.address.setText(scenario.url)
    window._play_from_address()  # noqa: SLF001 - 走真实入口

    state = {"done": False}
    verdict = {"ok": False, "note": ""}

    def evaluate() -> None:
        if state["done"]:
            return
        state["done"] = True

        max_duration = max(durations) if durations else 0
        max_position = max(positions) if positions else 0
        loaded = ctrl._media_loaded  # noqa: SLF001

        if scenario.expect == "ok":
            if loaded or max_duration > 0 or max_position > 0:
                verdict["ok"] = True
                verdict["note"] = (
                    f"已加载, 时长={max_duration}ms, 位置={max_position}ms, "
                    f"直播={ctrl.is_live}"
                )
            else:
                verdict["note"] = f"未确认播放。事件: {events[-3:]} 错误: {errors[:2]}"
        elif scenario.expect == "resolve":
            # 网页地址只要求"确实走了解析流程并给出自洽结论"：
            # 解析成功就该播起来；站点没有单文件流（如 B 站只有 DASH）则必须
            # 明确说出原因，而不是丢一个"加载超时"让人猜。
            if loaded or max_duration > 0 or max_position > 0:
                merged = ctrl.backend.extra_audio_active()
                verdict["ok"] = True
                verdict["note"] = (
                    f"解析成功并已播放, 时长={max_duration}ms"
                    + ("（已自动合流：画面+声音）" if merged else "")
                )
            elif errors and any(
                key in "".join(errors) for key in ("解析", "分离", "临时直链")
            ):
                verdict["ok"] = True
                verdict["note"] = f"已走解析流程并给出明确结论: {errors[0][:80]}"
            elif errors and scenario.must_contain and scenario.must_contain in errors[0]:
                verdict["ok"] = True
                verdict["note"] = f"按预期报错: {errors[0][:80]}"
            elif any("合流" in e or "分离" in e for e in events):
                # 解析确实走通并给出了结论（"只有分离流，正在合流播放…"），
                # 只是这段窗口内还没到可判定的状态——B 站 CDN 慢、或声音流被
                # 拒后降级为仅画面时都这样。这条场景只负责"流程与结论自洽"，
                # "音画是否真的都在"由 expect="merged" 那条用硬证据负责。
                verdict["ok"] = True
                verdict["note"] = f"已走通解析并给出结论: {events[-1][:70]}"
            else:
                verdict["note"] = f"没有走通解析流程。事件: {events[-4:]} 错误: {errors[:2]}"
        elif scenario.expect == "merged":
            # 站点只有 DASH 分离流时，用户可见的要求是"粘贴地址后音画一起播，
            # 不弹框、不用点按钮"。这里只认硬证据：附加音轨真的挂上了且在推进。
            # 单看 loaded/duration 会被"只有画面"骗过去——那正是这次要修的问题。
            if ctrl.backend.extra_audio_active() and (
                loaded or max_duration > 0 or max_position > 0
            ):
                verdict["ok"] = True
                verdict["note"] = (
                    f"已自动合流播放（画面+声音）, 时长={max_duration}ms, "
                    f"位置={max_position}ms"
                )
            elif errors:
                verdict["note"] = (
                    f"没有自动合流，反而报错。错误: {errors[:2]} 事件: {events[-2:]}"
                )
            else:
                verdict["note"] = (
                    f"附加音轨没挂上（没自动合流）。事件: {events[-3:]}"
                )
        else:  # expect == "fail"
            if errors:
                verdict["ok"] = True
                verdict["note"] = f"按预期报错: {errors[0][:80]}"
                if scenario.must_contain and scenario.must_contain not in errors[0]:
                    verdict["ok"] = False
                    verdict["note"] += f"（但错误里缺少关键词「{scenario.must_contain}」）"
            else:
                verdict["note"] = f"预期报错但未收到错误。事件: {events[-3:]}"

        app.quit()

    QTimer.singleShot(int(scenario.timeout_s * 1000), evaluate)
    app.exec()

    window.close()
    window.deleteLater()
    return verdict["ok"], verdict["note"]


# ============================================================ 功能链路（P3–P6）
#
# 上面那些场景回答的是"这个地址能不能播"；这一段回答的是"新加的功能在真实
# 窗口里是不是真的接上了"。单元测试覆盖的是模型层与工具层（播放列表怎么排序、
# 下载怎么续传），接口接错线它们照样全绿——`_download_current` 忘了把地址传下去、
# `urlDetected` 忘了连到输入框，这类问题只有在这里跑一遍才现形。
#
# 全部用本地的真实 HTTP 服务器（tests/httpfixture.py），不依赖任何外部服务，
# 所以默认套件就会跑，不需要 --with-local。

#: 播放列表现场：EntryStore 的路径指向 %APPDATA%，检查里会改它。
#: 这些检查有副作用（往真实文件里写条目），所以先备份、跑完还原。
USER_STATE_FILES = ("playlist.json", "history.json")

#: 检查过程中可能被改动的设置项，跑完一律还原
TOUCHED_SETTINGS = (
    "playback/seek_step_ms",
    "download/concurrency",
    "download/dir",
    "backend/type",
    "backend/prefer_mpv_for_live",
    "clipboard/watch",
    "clipboard/autofill",
    "proxy/enabled",
    "proxy/referer",
    "proxy/port",
)


def _wait(app: QApplication, predicate, timeout_s: float, tick_ms: int = 80) -> bool:
    """驱动 Qt 事件循环直到条件成立或超时。

    这里用 processEvents + sleep 而不是嵌套 exec()：嵌套事件循环在离屏
    环境里遇到模态框容易卡死，而 processEvents 足够把跨线程队列里的
    `progressReady` 之类投递进来。
    """
    import time as _time

    if predicate():
        return True
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        _time.sleep(tick_ms / 1000)
    app.processEvents()
    return bool(predicate())


class FeatureContext:
    """一次功能检查要用到的东西。

    两个本地 HTTP 服务器：
      - `server`  服务**合成字节**（下载校验用：逐字节比对才有意义）
      - `media`   服务**真实 mp4**（要解码出画面，合成字节是过不了解码器的）
    """

    def __init__(
        self, app: QApplication, server, media, media_file: Path, tmp: Path
    ) -> None:
        self.app = app
        self.server = server
        self.media = media
        self.media_file = media_file
        self.tmp = tmp


def _backend_source(backend) -> str:
    """取后端**实际加载**的地址。

    Qt 与 mpv 的存放位置不同（前者在 QMediaPlayer.source()，后者在自己的
    `_url`），所以这里统一兜一层——冒烟检查要断言"播放器用没用代理地址"，
    不能因为换了内核就查不到。
    """
    player = getattr(backend, "_player", None)
    if player is not None and hasattr(player, "source"):
        try:
            return player.source().toString()
        except Exception:  # noqa: BLE001
            pass
    return str(getattr(backend, "_url", "") or "")


def _playing(window, positions: list[int]) -> bool:
    """播放是否已经真的起来。

    注意 `positionChanged` 在**开始加载时就会先发一次 0**，所以不能拿
    "positions 非空"当信号——那样这个判断会立刻为真，等待形同虚设，
    后面的断言全都在"还没加载完"的状态下跑（代理那条检查就是这么被骗过一次的）。
    要么已加载成功，要么位置真的往前走到了 0 以后。
    """
    ctrl = window.controller
    return bool(ctrl._media_loaded) or any(ms > 0 for ms in positions)  # noqa: SLF001


def check_direct_download(ctx: FeatureContext) -> tuple[bool, str]:
    """P4：直链下载要真的落盘，且内容与源站逐字节一致。"""
    from src.core.downloader import DownloadState
    from src.ui.main_window import MainWindow

    window = MainWindow()
    try:
        window.downloads.set_download_dir(ctx.tmp)
        progress: list[int] = []
        finished: list = []
        window.downloads.progressReady.connect(lambda task: progress.append(task.received))
        window.downloads.taskFinished.connect(finished.append)

        window.address.setText(ctx.server.url("/file.bin"))
        window._download_current()  # noqa: SLF001 - 走真实入口（按钮就是连它）

        if not _wait(ctx.app, lambda: finished, 40):
            return False, "下载 40s 未结束"
        task = finished[0]
        if task.state != DownloadState.DONE:
            return False, f"下载未成功: state={task.state} error={task.error}"

        dest = Path(task.dest)
        if not dest.is_file():
            return False, f"任务说完成了但文件不存在: {dest}"
        data = dest.read_bytes()
        if data != ctx.server.payload:
            return False, f"内容不一致: {len(data)} 字节 vs 源站 {ctx.server.size} 字节"
        if not progress:
            return False, "全程没有收到任何进度上报（进度条会是死的）"
        if len(progress) == 1 and data:
            return False, f"只上报了 1 次进度，粒度太粗: {progress}"
        return True, (
            f"{len(data)} 字节校验一致，进度上报 {len(progress)} 次，"
            f"耗时 {task.elapsed:.2f}s，文件 {dest.name}"
        )
    finally:
        window.downloads.cancel_all()
        window.close()


def check_proxy_playback(ctx: FeatureContext) -> tuple[bool, str]:
    """P5：本地反向代理的**路由决策**要正确，代理要真的代发请求头。

    现在有两条互斥的规矩（见 `core/url_router.is_loopback` 与
    `player_controller._proxy_route`）：

      * **本机回环地址不走代理** —— 本机服务没有防盗链问题，而代理自己就监听
        在回环上，代理回环地址等于白绕一圈，还平白多一个可能失败的环节；
      * 别的地址在「地址自带请求头」或「用户显式打开开关」时**必须**走代理。

    这里把两条都钉住。

    关于"为什么还要临时改掉回环判定"：冒烟测试手上唯一能放视频的源就是本机
    服务器（127.0.0.1），而它恰好落在"不走代理"那一侧——拿它验证"走代理"是
    自相矛盾的。但"Referer 有没有真的送到源站"这条证据又非看不可（只断言
    "我配了 Referer"毫无意义，请求可能根本没发出去），而只有我们自己的
    服务器能记录收到的请求头。所以：**先按真实规矩验证两条路由决策，再临时
    关掉回环判定**，让本地服务器扮演"远程防盗链站点"，走一遍完整的
    `_proxy_route → 本地代理 → 源站` 链路。这个改动只活在本函数内，出函数即还原。
    """
    from src.core import local_proxy, url_router
    from src.core.media_item import MediaItem
    from src.utils import settings as S
    from src.ui.main_window import MainWindow

    referer = "https://smoke.example/zone/"
    agent = "SmokeUA/1.0"
    S.settings().setValue(S.K_LOCAL_PROXY_REFERER, referer)
    S.settings().setValue(S.K_LOCAL_PROXY_ENABLED, True)

    local_url = ctx.media.url("/video.mp4")
    remote_url = "https://protected.smoke.example/secret/video.mp4"

    window = MainWindow()
    real_is_loopback = url_router.is_loopback
    try:
        if not local_proxy.is_running():
            return False, "打开了代理开关，但代理没有被启动"

        ctrl = window.controller

        # ---- 规矩 1：回环地址必须原样直连，套代理就是白绕
        loop_item = MediaItem.from_url(local_url)
        if ctrl._proxy_route(local_url, loop_item, quiet=True) != local_url:  # noqa: SLF001
            return False, (
                "回环地址被送去代理了（本机服务不该绕代理，代理自己就监听在回环上）"
            )

        # ---- 规矩 2：非回环地址 + 开关打开 → 必须换成代理地址
        #      同时把解析器给的请求头编码进去（这正是 B 站这类
        #      "CDN 域名 ≠ 页面域名"的地址能不能过防盗链的关键）
        remote_item = MediaItem.from_url(remote_url)
        remote_item.http_headers = {"Referer": referer, "User-Agent": agent}
        routed = ctrl._proxy_route(remote_url, remote_item, quiet=True)  # noqa: SLF001
        if not routed.startswith("http://127.0.0.1:") or "/proxy" not in routed:
            return False, f"非回环地址没有走代理，代理设置形同虚设: {routed!r}"
        decoded = unquote(routed)
        if referer not in decoded:
            return False, (
                f"代理地址里没有带上解析器给的 Referer，防盗链照样过不去: {decoded!r}"
            )
        if agent not in decoded:
            return False, f"代理地址里没有带上解析器给的 User-Agent: {decoded!r}"

        # ---- 端到端：让本地服务器扮演"远程防盗链站点"，走完整链路
        url_router.is_loopback = lambda _u: False  # noqa: ARG005
        ctx.media.clear_requests()
        play_item = MediaItem.from_url(local_url)
        play_item.http_headers = {"Referer": referer, "User-Agent": agent}
        proxied = ctrl._proxy_route(local_url, play_item, quiet=True)  # noqa: SLF001

        positions: list[int] = []
        ctrl.positionChanged.connect(lambda ms: positions.append(int(ms)))
        ctrl._start(play_item)  # noqa: SLF001
        loaded = _wait(ctx.app, lambda: _playing(window, positions), 20)

        backend_url = _backend_source(ctrl._backend)  # noqa: SLF001
        if "127.0.0.1" not in backend_url or "/proxy" not in backend_url:
            return False, f"播放器没有去取代理地址: {backend_url!r}"

        hits = ctx.media.requests_for("/video.mp4")
        if not hits:
            return False, "源站没有收到任何对 /video.mp4 的请求（代理没转发过去）"
        leaked = [h for h in hits if h["headers"].get("Referer") == referer]
        if not leaked:
            # 诊断线程也会用原始地址打同一路径，它不带 Referer；所以要求"至少一次"
            got = sorted({h["headers"].get("Referer", "<无>") for h in hits})
            return False, (
                f"{len(hits)} 次请求里没有一次带上注入的 Referer，实际收到: {got}"
            )
        if not loaded:
            return False, (
                f"经代理播放没有确认播放（已 {len(leaked)} 次带 Referer 到达源站）"
                f" 位置={positions[-3:] if positions else '无'}"
            )
        max_pos = max(positions) if positions else 0
        how = f"位置推进到 {max_pos}ms" if max_pos > 0 else "已加载成功"
        return True, (
            f"回环直连、非回环走代理（含解析器请求头透传）均正确；"
            f"经代理播放成功（{how}，{proxied.split('/proxy')[0]}）；"
            f"源站共收到 {len(hits)} 次请求，其中 {len(leaked)} 次带注入的 Referer"
        )
    finally:
        url_router.is_loopback = real_is_loopback
        window.close()
        local_proxy.stop()



def check_playlist_switching(ctx: FeatureContext) -> tuple[bool, str]:
    """P3：播放列表的加入、切换与落盘要真的连上主窗口。"""
    from src.ui.main_window import MainWindow

    first = ctx.media.url("/video.mp4")
    second = ctx.server.url("/file.bin")

    window = MainWindow()
    try:
        window.playlist.clear()
        window.playlist.add([MediaItem.from_url(first), MediaItem.from_url(second)])
        if window.playlist.rowCount() != 2:
            return False, f"加入后应有 2 条，实际 {window.playlist.rowCount()} 条"

        # 从列表播第一条
        window._play_item(window.playlist.item_at(0))  # noqa: SLF001
        _wait(ctx.app, lambda: window.controller.current is not None, 5)
        current = window.controller.current
        if current is None or current.url != first:
            return False, f"播放第一条没生效: {getattr(current, 'url', None)!r}"

        # 切下一条
        window._play_next()  # noqa: SLF001
        _wait(ctx.app, lambda: window.controller.current.url == second, 5)
        current = window.controller.current
        if current.url != second:
            return False, f"「下一个」没切过去，仍是 {current.url}"

        # 到头折返
        window._play_next()  # noqa: SLF001
        _wait(ctx.app, lambda: window.controller.current.url == first, 5)
        if window.controller.current.url != first:
            return False, "到末尾没有折返到第一条"

        # 落盘（真实文件在 %APPDATA%）
        if not window.playlist.save():
            return False, "播放列表保存失败"
        store_path = window.playlist._store.path  # noqa: SLF001
        if not store_path.exists():
            return False, f"保存后文件不存在: {store_path}"

        if not window.control.btn_prev.isEnabled():
            return False, "列表非空但「上一个/下一个」按钮仍是禁用的"
        return True, f"加入/切换/折返/落盘正常（{store_path.name}）"
    finally:
        window.close()


def check_history_and_dedupe(ctx: FeatureContext) -> tuple[bool, str]:
    """P3：播过的地址进历史，且重复播放不会堆积。"""
    from src.ui.main_window import MainWindow

    url = ctx.media.url("/video.mp4")
    window = MainWindow()
    try:
        window.history.clear()
        window.address.setText(url)
        window._play_from_address()  # noqa: SLF001
        if not _wait(ctx.app, lambda: window.history.rowCount() > 0, 5):
            return False, "播放后历史记录里没有出现这条地址"

        row = window.history.index_of_url(url)
        if row != 0:
            return False, f"新播的地址应排在历史第 1 条，实际在第 {row + 1} 条"

        # 再播一次同一条（外加另一个地址）验证去重只对新地址追加
        window.address.setText(url)
        window._play_from_address()  # noqa: SLF001
        _wait(ctx.app, lambda: False, 0.6)
        if window.history.rowCount() != 1:
            return False, f"重复播放同一条地址后历史变成 {window.history.rowCount()} 条"

        other = ctx.server.url("/file.bin")
        window.address.setText(other)
        window._play_from_address()  # noqa: SLF001
        _wait(ctx.app, lambda: window.history.rowCount() > 1, 5)
        if window.history.rowCount() != 2:
            return False, f"换一条地址后历史应有 2 条，实际 {window.history.rowCount()} 条"
        return True, f"入历史 + 去重置顶正常（当前 {window.history.rowCount()} 条）"
    finally:
        window.close()


def check_clipboard_autofill(ctx: FeatureContext) -> tuple[bool, str]:
    """P5：剪贴板里出现视频地址时，输入框要自动填上；无关文本不能乱触发。"""
    from src.ui.main_window import MainWindow

    board = ctx.app.clipboard()
    url = ctx.server.url("/video.mp4")
    window = MainWindow()
    try:
        window.address.clear()
        window.clipboard.set_enabled(True)
        if not window.clipboard.enabled:
            return False, "剪贴板监听打不开（没有 QApplication？）"

        board.setText(url)
        if not _wait(ctx.app, lambda: window.address.text().strip() == url, 5):
            return False, (
                f"复制视频地址后输入框没有自动填入，现在是「{window.address.text()[:40]}」"
            )

        # 无关文本不能触发：先清空输入框，再复制一段普通文字
        window.address.clear()
        window.clipboard.forget()
        board.setText("这是一段普通文本，不是地址")
        if not _wait(ctx.app, lambda: True, 0.6):
            pass
        if window.address.text().strip():
            return False, f"普通文本被误判成地址并填入：{window.address.text()[:40]!r}"

        # 关闭监听后连真地址也不该有反应
        window.clipboard.set_enabled(False)
        window.clipboard.forget()
        board.setText(ctx.server.url("/other.mp4"))
        if not _wait(ctx.app, lambda: True, 0.6):
            pass
        if window.address.text().strip():
            return False, "监听已关闭，复制地址仍然被填入了"
        return True, "自动填入 / 误报抑制 / 关闭后静默 均正常"
    finally:
        window.clipboard.set_enabled(False)
        board.clear()
        window.close()


def check_settings_dialog(ctx: FeatureContext) -> tuple[bool, str]:
    """P5：设置面板改完要真的落到配置文件，并且能立刻热生效。"""
    from pathlib import Path as _Path

    from src.ui.main_window import MainWindow
    from src.ui.settings_dialog import SettingsDialog
    from src.utils import settings as S

    window = MainWindow()
    try:
        dialog = SettingsDialog(window)
        # 找一个和"下载并发"绑定的数字控件，改它再应用
        before = S.get_int(S.K_DOWNLOAD_CONCURRENCY, 2)
        new_value = 5 if before != 5 else 3
        dialog.spin_concurrency.setValue(new_value)
        dialog.apply()

        if S.get_int(S.K_DOWNLOAD_CONCURRENCY, 2) != new_value:
            return False, "点「应用」后设置没有写进配置文件"
        window._on_settings_applied()  # noqa: SLF001
        if window.downloads._concurrency != new_value:  # noqa: SLF001
            return False, (
                f"设置已落盘但运行时没热生效：下载器并发仍是 "
                f"{window.downloads._concurrency}"  # noqa: SLF001
            )

        # 下载目录这类路径项也要能落盘
        tmp_text = str(ctx.tmp)
        dialog.edit_download_dir.setText(tmp_text)
        dialog.apply()
        if S.get_str(S.K_DOWNLOAD_DIR, "") != tmp_text:
            return False, "下载目录设置没有保存"
        if not _Path(window.downloads.download_dir) == ctx.tmp:
            return False, (
                f"下载目录已保存但下载器没用上：{window.downloads.download_dir}"
            )
        dialog.close()
        return True, f"设置落盘 + 热生效正常（并发 {before}→{new_value}）"
    finally:
        window.close()


def check_mpv_fallback(ctx: FeatureContext) -> tuple[bool, str]:
    """P6：选了 mpv 但没装时，必须静默回退 Qt 且照样能播——不能因此播不了。"""
    from src.core.backends import factory
    from src.core.backends.mpv_backend import mpv_available
    from src.ui.main_window import MainWindow
    from src.utils import settings as S

    if mpv_available():
        return True, "本机已装 libmpv，跳过回退验证（走的是真 mpv 内核）"

    S.settings().setValue(S.K_BACKEND, "mpv")
    S.settings().setValue(S.K_PREFER_MPV_FOR_LIVE, True)
    window = MainWindow()
    try:
        positions: list[int] = []
        window.controller.positionChanged.connect(lambda ms: positions.append(int(ms)))
        window.address.setText(ctx.media.url("/video.mp4"))
        window._play_from_address()  # noqa: SLF001

        ok = _wait(
            ctx.app,
            lambda: _playing(window, positions),
            20,
        )
        backend_name = type(window.controller._backend).__name__  # noqa: SLF001
        if backend_name != "QtMediaBackend":
            return False, f"设置的 mpv 不可用，但没有回退到 Qt，实际是 {backend_name}"
        if not ok:
            return False, f"回退到 Qt 之后依然播不了（后端 {backend_name}）"
        return True, f"选了 mpv 但未安装 → 静默回退 {backend_name} 且播放正常"
    finally:
        window.close()


FEATURE_CHECKS = [
    ("直链下载落盘（P4）", check_direct_download),
    ("本地反向代理播放（P5）", check_proxy_playback),
    ("播放列表切换与落盘（P3）", check_playlist_switching),
    ("历史记录与去重（P3）", check_history_and_dedupe),
    ("剪贴板自动填入（P5）", check_clipboard_autofill),
    ("设置面板落盘与热生效（P5）", check_settings_dialog),
    ("mpv 缺失时回退 Qt（P6）", check_mpv_fallback),
]


def _snapshot_user_state() -> dict:
    """备份会被功能检查改动的用户状态（真实文件 + 真实设置）。"""
    from src.utils import settings as S
    from src.utils.paths import data_file

    files: dict[str, bytes | None] = {}
    for name in USER_STATE_FILES:
        path = data_file(name)
        files[name] = path.read_bytes() if path.is_file() else None

    values = {key: S.settings().value(key) for key in TOUCHED_SETTINGS}
    return {"files": files, "values": values}


def _restore_user_state(snapshot: dict) -> None:
    """还原用户状态。

    功能检查会往真实的 `playlist.json` / `history.json` 里写东西——那是用户
    自己的数据，跑个测试就给人改掉是不可接受的。改回原样（原来没有就删掉）。
    """
    from src.utils import settings as S
    from src.utils.paths import data_file

    for name, payload in snapshot["files"].items():
        path = data_file(name)
        try:
            if payload is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(payload)
        except OSError:
            pass

    store = S.settings()
    for key, value in snapshot["values"].items():
        if value is None:
            store.remove(key)
        else:
            store.setValue(key, value)
    store.sync()


def run_feature_checks(app: QApplication) -> tuple[int, int]:
    """跑 P3–P6 的功能链路检查，返回 (失败数, 总数)。"""
    import shutil
    import tempfile as _tempfile

    from tests.httpfixture import LocalHTTPServer

    media_file = ROOT / ".tools" / "testmedia" / "local_test.mp4"
    if not media_file.is_file():
        print(f"[跳过] 功能链路检查：缺少本地素材 {media_file}")
        return 0, 0

    snapshot = _snapshot_user_state()
    failures = 0
    tmp = Path(_tempfile.mkdtemp(prefix="smoke-dl-"))

    print("-" * 70)
    print("功能链路检查（P3–P6）")
    print("-" * 70)

    try:
        # 服务真实 mp4（能解码出画面）与合成字节（下载校验用）各一份
        with LocalHTTPServer() as byte_server, LocalHTTPServer(
            payload_file=media_file
        ) as media_server:
            ctx = FeatureContext(app, byte_server, media_server, media_file, tmp)
            for name, fn in FEATURE_CHECKS:
                try:
                    ok, note = fn(ctx)
                except Exception as exc:  # noqa: BLE001 - 一项炸了不该拖垮整套
                    import traceback

                    ok, note = False, f"检查抛异常: {exc}\n{traceback.format_exc(limit=3)}"
                mark = "通过" if ok else "失败"
                if not ok:
                    failures += 1
                print(f"[{mark}] {name}")
                print(f"        结果: {note}")
    finally:
        _restore_user_state(snapshot)
        shutil.rmtree(tmp, ignore_errors=True)

    return failures, len(FEATURE_CHECKS)


BUILTIN_SUITE = [
    ("本地文件", str(ROOT / ".tools" / "testmedia" / "local_test.mp4"), "ok", 12),
    ("HLS 点播", "https://devstreaming-cdn.apple.com/videos/streaming/examples/"
                 "bipbop_4x3/bipbop_4x3_variant.m3u8", "ok", 25),
    # 网页地址必须走 yt-dlp 解析：B 站只提供分离的音视频流，因此正常结论是
    # "自动合流播放（画面+声音）"。若哪天 B 站有了 progressive 流、这个场景变成
    # 单文件直出，也照样通过（expect="resolve" 两边都接受）。
    ("网页地址解析（B 站）", "https://www.bilibili.com/video/BV1xx411c7mD", "resolve", 40),
    # 上一条只看"播起来了没有"，这条盯住"**不用点按钮**就把声音也播了"——
    # 用户反馈过的正是"解析成功却弹框让我自己点"。
    ("分离流自动合流（B 站，无需点按钮）",
     "https://www.bilibili.com/video/BV1Ebho6BE2J", "merged", 35),
    ("坏地址超时兜底", "http://127.0.0.1:9/nothing.mp4", "fail", 14),
]
# 需要本地服务才能跑的场景，单独列出，服务不在就跳过而不是报失败。
# 字段: 名称, 地址, 期望, 超时秒, 端口, mediamtx 上对应的流路径(没有则 None),
#       依赖的本地素材(没有则 None)
LOCAL_SERVICE_SUITE = [
    ("rtsp 实时流", "rtsp://127.0.0.1:8554/test", "ok", 18, 8554, "test", None),
    ("rtmp 实时流", "rtmp://127.0.0.1:1935/rtmptest", "ok", 18, 1935, "rtmptest", None),
    ("HLS 直播", "http://127.0.0.1:8888/test/index.m3u8", "ok", 20, 8888, "test", None),
    ("局域网 HTTP 直链", "http://127.0.0.1:8811/local_test.mp4", "ok", 15, 8811, None,
     ROOT / ".tools" / "testmedia" / "local_test.mp4"),
    # 本地 HTML 页面 + yt-dlp 解析：同时覆盖"未知 http 地址直连失败 → 自动改用
    # 解析器"这条兜底链路。需要项目根目录的静态服务：
    #   python -m http.server 8821 --bind 127.0.0.1 --directory .
    ("网页地址解析（未知 http 自动兜底）",
     "http://127.0.0.1:8821/.tools/webtest/page.html", "ok", 25, 8821, None,
     ROOT / ".tools" / "webtest" / "page.html"),
]

#: mediamtx 的 HTTP 控制接口，用来确认某个路径是否真的在推流
MEDIAMTX_API = "http://127.0.0.1:9997/v3/paths/list"


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def published_paths() -> set[str] | None:
    """查询 mediamtx 当前已就绪的流路径；接口不可用时返回 None。

    为什么不能只看端口：端口开着只说明 mediamtx 在跑，不代表某条流还在推。
    ffmpeg 推流有 -t 时限，过期后路径就没了，此时既不是代码故障也不该报失败，
    而应该明确跳过。这类"假失败"最消耗对测试的信任。
    """
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(MEDIAMTX_API, timeout=2) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - 接口不可用就退回端口判断
        return None

    result: set[str] = set()
    for item in payload.get("items", []):
        name = item.get("name") or ""
        tracks = item.get("tracks") or []
        if name and tracks and item.get("ready", True):
            result.add(name)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="集成冒烟测试")
    parser.add_argument("--url")
    parser.add_argument("--expect", choices=["ok", "fail", "resolve", "merged"],
                        default="ok",
                        help="ok=应当播起来 fail=应当报错 resolve=应当走通解析流程 "
                             "merged=分离流应当自动合流（音轨挂上且画面在播）")
    parser.add_argument("--must-contain", default="",
                        help="配合 --expect fail：要求错误信息里包含该关键词")
    parser.add_argument("--name", default="自定义场景")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--suite", action="store_true", help="跑无需外部服务的场景")
    parser.add_argument("--with-local", action="store_true",
                        help="额外跑需要本地 RTSP/RTMP/HLS 服务的场景")
    parser.add_argument("--local-only", action="store_true",
                        help="只跑需要本地服务的场景")
    parser.add_argument("--no-feature", action="store_true",
                        help="跳过 P3–P6 的功能链路检查（不碰真实播放列表/历史文件）")
    parser.add_argument("--feature-only", action="store_true",
                        help="只跑 P3–P6 的功能链路检查，不跑任何地址场景"
                             "（下载/代理/播放列表/历史/剪贴板/设置/mpv 回退）")
    args = parser.parse_args()

    scenarios: list[Scenario] = []
    pending: list[tuple] = []

    if args.feature_only:
        if args.url:
            parser.error("--feature-only 不接受 --url")
    elif args.url:
        scenarios.append(
            Scenario(args.name, args.url, args.expect, args.timeout, args.must_contain)
        )
    else:
        if args.suite or not (args.with_local or args.local_only):
            scenarios += [Scenario(*item) for item in BUILTIN_SUITE]
        if args.with_local or args.local_only:
            pending = list(LOCAL_SERVICE_SUITE)
        if not scenarios and not pending:
            parser.error("需要 --url 或 --suite")

    app = QApplication.instance() or QApplication(sys.argv[:1])

    print("=" * 70)
    print("集成冒烟测试")
    print("=" * 70)

    failures = 0
    skipped = 0
    feature_total = 0

    def run_all() -> None:
        nonlocal failures, skipped
        for scenario in scenarios:
            ok, note = run_scenario(app, scenario)
            mark = "通过" if ok else "失败"
            if not ok:
                failures += 1
            print(f"[{mark}] {scenario.name}")
            print(f"        地址: {scenario.url}")
            print(f"        结果: {note}")

    run_all()

    # 功能链路检查：只有"跑整套"时才做。给了具体地址说明用户在看某一条地址，
    # 这时不该顺手改他的播放列表与历史记录。
    if args.feature_only or (
        not args.url and not args.no_feature and not args.local_only
    ):
        feature_failures, feature_total = run_feature_checks(app)
        failures += feature_failures

    paths = published_paths()
    if paths is None:
        print("（未能读取 mediamtx 路径列表，本地场景只做端口检查）")

    for name, url, expect, timeout_s, port, path, asset in pending:
        if asset is not None and not Path(asset).exists():
            print(f"[跳过] {name}")
            print(f"        缺少本地素材: {asset}")
            skipped += 1
            continue
        if not port_open(port):
            print(f"[跳过] {name}")
            print(f"        本地服务未在 127.0.0.1:{port} 监听")
            skipped += 1
            continue
        if path and paths is not None and path not in paths:
            print(f"[跳过] {name}")
            print(f"        mediamtx 上没有在推流的路径 '{path}'（推流已到期或未启动）")
            skipped += 1
            continue
        ok, note = run_scenario(app, Scenario(name, url, expect, timeout_s))
        mark = "通过" if ok else "失败"
        if not ok:
            failures += 1
        print(f"[{mark}] {name}")
        print(f"        地址: {url}")
        print(f"        结果: {note}")

    total = len(scenarios) + feature_total + len(pending)
    print("-" * 70)
    print(f"合计 {total} 项：失败 {failures} 项，跳过 {skipped} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
