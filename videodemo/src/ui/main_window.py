"""主窗口：地址栏 + 画面区 + 播放列表面板 + 控制条。"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from src.core.downloader import DownloadManager
from src.core.media_item import MediaItem
from src.core.player_controller import PlayerController, open_externally
from src.core.playlist import (
    DEFAULT_HISTORY_LIMIT,
    EntryStore,
    PlaylistModel,
)
from src.ui.control_bar import ControlBar
from src.ui.downloads_dialog import DownloadsDialog
from src.ui.fonts import ensure_app_font
from src.ui.playlist_panel import PlaylistPanel
from src.ui.settings_dialog import SettingsDialog
from src.ui.video_widget import VideoWidget
from src.utils import settings as S
from src.utils.clipboard_watcher import ClipboardWatcher
from src.utils.logger import get_logger
from src.utils.paths import data_file

log = get_logger(__name__)

SEEK_STEP_MS = 5_000

#: 持久化文件名（放在 %APPDATA%/UniversalURLPlayer/ 下）
PLAYLIST_FILE = "playlist.json"
HISTORY_FILE = "history.json"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        # 保证中文可读，避免缺字体时全是方框
        ensure_app_font()
        self.setWindowTitle("万能 URL 视频播放器")
        self.resize(1180, 700)

        self.controller = PlayerController(self)

        #: 用户点了「诊断」后，下一次诊断完成时弹窗展示完整报告
        self._manual_diag_pending = False

        # --- 播放列表与历史记录（JSON 持久化，见 core/playlist.py） ---
        self.playlist = PlaylistModel(EntryStore(data_file(PLAYLIST_FILE)))
        self.history = PlaylistModel(
            EntryStore(data_file(HISTORY_FILE), dedupe=True, max_items=DEFAULT_HISTORY_LIMIT),
            dedupe=True,
            max_items=DEFAULT_HISTORY_LIMIT,
        )

        # --- 下载（P4）---
        self.downloads = DownloadManager(self)
        self.downloads.set_concurrency(S.get_int(S.K_DOWNLOAD_CONCURRENCY, 2))
        self._downloads_dialog: DownloadsDialog | None = None

        # --- 剪贴板监听（P5）---
        self.clipboard = ClipboardWatcher(self)

        self._build_ui()
        self._connect_signals()
        self._restore_window_state()
        self._load_lists()
        self._apply_startup_settings()

        # 初始音量/静音/倍速同步到控件
        self.control.set_volume(S.get_float(S.K_VOLUME, 0.8))
        self.control.set_muted(S.get_bool(S.K_MUTED, False))
        self.control.set_rate(S.get_float(S.K_RATE, 1.0))

        self.statusBar().showMessage("就绪。粘贴视频地址后按回车即可播放。")

    # ------------------------------------------------------------ 启动设置
    def _apply_startup_settings(self) -> None:
        """把需要"运行时生效"的设置应用一遍。"""
        self.control.set_seek_step_ms(S.get_int(S.K_SEEK_STEP_MS, 5000))

        # 剪贴板监听默认关闭：后台偷看剪贴板是件有侵入性的事，
        # 必须由用户在设置里明确开启
        if S.get_bool(S.K_CLIPBOARD_WATCH, False):
            self.clipboard.set_enabled(True)

        # 上次播放的地址填回输入框（只填不播，避免一启动就联网）
        if S.get_bool(S.K_RESUME_LAST, False):
            last = S.get_str("playback/last_url", "")
            if last:
                self.address.setText(last)
                self.statusBar().showMessage("已填回上次的地址，按回车开始播放")

        # 代理要是已经开着，顺手把它拉起来，省得第一次播放多等一次启动
        if S.local_proxy_enabled():
            from src.core import local_proxy

            local_proxy.ensure_started(S.get_int(S.K_LOCAL_PROXY_PORT, 0))

    # ------------------------------------------------------------ 列表载入
    def _load_lists(self) -> None:
        count = self.playlist.load()
        self.history.load()
        self._refresh_nav_buttons()
        if count:
            log.info("已载入播放列表，共 %d 条", count)

    # ------------------------------------------------------------ 构建界面
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # --- 顶部地址栏 ---
        self.address = QLineEdit()
        self.address.setPlaceholderText(
            "粘贴视频地址：网页地址（B站/YouTube…）· 直链 …mp4 · HLS …m3u8 · rtsp:// · 本地路径"
        )
        self.address.setClearButtonEnabled(True)

        self.btn_play_url = QPushButton("播放")
        self.btn_play_url.setMinimumWidth(64)
        self.btn_paste = QPushButton("粘贴")
        self.btn_paste.setMinimumWidth(64)
        self.btn_paste.setToolTip("从剪贴板读取地址并填入输入框")
        self.btn_open = QPushButton("打开文件")
        self.btn_open.setMinimumWidth(80)
        self.btn_add = QPushButton("加入列表")
        self.btn_add.setMinimumWidth(80)
        self.btn_add.setToolTip("把当前地址加入播放列表（也可直接把文件拖进列表）")
        self.btn_download = QPushButton("下载")
        self.btn_download.setMinimumWidth(64)
        self.btn_download.setToolTip("把当前地址下载到本地")
        self.btn_diagnose = QPushButton("诊断")
        self.btn_diagnose.setMinimumWidth(64)
        self.btn_diagnose.setToolTip(
            "对当前地址做分层连通性诊断：地址解析 → DNS → TCP 连接 → 协议握手"
        )
        self.btn_panel = QPushButton("列表")
        self.btn_panel.setMinimumWidth(56)
        self.btn_panel.setCheckable(True)
        self.btn_panel.setToolTip("显示 / 隐藏播放列表面板")
        self.btn_settings = QPushButton("设置")
        self.btn_settings.setMinimumWidth(56)

        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(self.address, 1)
        top.addWidget(self.btn_play_url)
        top.addWidget(self.btn_paste)
        top.addWidget(self.btn_open)
        top.addWidget(self.btn_add)
        top.addWidget(self.btn_download)
        top.addWidget(self.btn_diagnose)
        top.addWidget(self.btn_panel)
        top.addWidget(self.btn_settings)

        # --- 画面区 + 播放列表面板 ---
        self.video = VideoWidget()
        self.video.set_video_widget(self.controller.backend.video_widget())
        # 还没开始播放时显示引导文字，比一上来一块黑的更友好
        self.video.show_placeholder()

        self.panel = PlaylistPanel(self.playlist, self.history)
        self.panel.setMinimumWidth(280)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.video)
        self.splitter.addWidget(self.panel)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setChildrenCollapsible(False)

        self.control = ControlBar()

        root.addLayout(top)
        root.addWidget(self.splitter, 1)
        root.addWidget(self.control)

        self.setCentralWidget(central)

        # 面板可见性按上次的选择恢复
        visible = S.get_bool(S.K_PLAYLIST_PANEL_VISIBLE, True)
        self.panel.setVisible(visible)
        self.btn_panel.setChecked(visible)

        # --- 快捷键 ---
        # 只在主窗口层面处理空格/方向键，不用 QShortcut：
        # QShortcut 会在地址栏输入空格时把按键吞掉，而按键冒泡到窗口则天然
        # 避开了"输入框有焦点"的情况（输入框自己会消费掉这些键）。
        act_fs = QAction("全屏", self)
        act_fs.setShortcut(QKeySequence("F11"))
        act_fs.triggered.connect(self.toggle_fullscreen)
        self.addAction(act_fs)

        # Ctrl+, 是各平台通用的「打开设置」
        act_settings = QAction("设置", self)
        act_settings.setShortcut(QKeySequence("Ctrl+,"))
        act_settings.triggered.connect(self.open_settings)
        self.addAction(act_settings)

        # Ctrl+J：只看下载面板（不新建任务）
        act_downloads = QAction("下载列表", self)
        act_downloads.setShortcut(QKeySequence("Ctrl+J"))
        act_downloads.triggered.connect(self._open_downloads)
        self.addAction(act_downloads)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.controller.toggle_play_pause()
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            self._exit_fullscreen()
            event.accept()
            return
        if key == Qt.Key.Key_Left:
            self.controller.seek_by(-SEEK_STEP_MS)
            event.accept()
            return
        if key == Qt.Key.Key_Right:
            self.controller.seek_by(SEEK_STEP_MS)
            event.accept()
            return
        super().keyPressEvent(event)

    def _connect_signals(self) -> None:
        self.address.returnPressed.connect(self._play_from_address)
        self.btn_play_url.clicked.connect(self._play_from_address)
        self.btn_paste.clicked.connect(self._paste)
        self.btn_open.clicked.connect(self._choose_file)
        self.btn_add.clicked.connect(self._add_current_to_playlist)
        self.btn_download.clicked.connect(self._download_current)
        self.btn_diagnose.clicked.connect(self._diagnose)
        self.btn_panel.toggled.connect(self._toggle_panel)
        self.btn_settings.clicked.connect(self.open_settings)

        self.video.doubleClicked.connect(self.toggle_fullscreen)
        self.video.filesDropped.connect(self._on_dropped)

        c = self.control
        c.playPauseClicked.connect(self.controller.toggle_play_pause)
        c.stopClicked.connect(self.controller.stop)
        c.prevClicked.connect(self._play_prev)
        c.nextClicked.connect(self._play_next)
        c.seekRequested.connect(self.controller.set_position)
        c.seekStepRequested.connect(self.controller.seek_by)
        c.volumeChanged.connect(self.controller.set_volume)
        c.mutedToggled.connect(self.controller.set_muted)
        c.rateChanged.connect(self.controller.set_rate)
        c.snapshotClicked.connect(self._snapshot)
        c.openExternalClicked.connect(self._open_external)
        c.fullscreenClicked.connect(self.toggle_fullscreen)

        panel = self.panel
        panel.playRequested.connect(self._play_item)
        panel.filesDropped.connect(self._on_dropped)
        panel.addRequested.connect(self._add_current_to_playlist)
        panel.playlistChanged.connect(self._refresh_nav_buttons)

        # 下载与剪贴板
        self.downloads.statusMessage.connect(self.statusBar().showMessage)
        self.clipboard.urlDetected.connect(self._on_clipboard_url)

        ctrl = self.controller
        ctrl.statusMessage.connect(self.statusBar().showMessage)
        ctrl.itemChanged.connect(self._on_item_changed)
        ctrl.positionChanged.connect(c.set_position)
        ctrl.durationChanged.connect(c.set_duration)
        ctrl.playbackStateChanged.connect(c.set_playback_state)
        ctrl.liveChanged.connect(c.set_live)
        ctrl.seekableChanged.connect(c.set_seekable)
        ctrl.errorRaised.connect(self._show_error)
        ctrl.diagnosisFinished.connect(self._on_diagnosis_finished)
        ctrl.resolveStarted.connect(self._on_resolve_started)
        ctrl.endOfMedia.connect(self._on_end_of_media)

    # ------------------------------------------------------------ 操作
    def _play_from_address(self) -> None:
        text = self.address.text().strip()
        if self.controller.play_text(text):
            # 保留地址便于查看与「外部打开」，全选以便下次粘贴直接覆盖
            self.address.setSelection(0, len(self.address.text()))
            # 记下最近一次真正开始播放的地址（设置里可让它下次启动时填回）
            S.settings().setValue("playback/last_url", text)

    def _paste(self) -> None:
        from PySide6.QtWidgets import QApplication

        text = QApplication.clipboard().text().strip()
        if not text:
            self.statusBar().showMessage("剪贴板里没有文本")
            return
        self.address.setText(text)
        self.statusBar().showMessage("已从剪贴板粘贴，按回车开始播放")

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择视频文件",
            os.path.expanduser("~"),
            "视频文件 (*.mp4 *.mkv *.flv *.webm *.mov *.avi *.ts *.m4v *.wmv *.mp3 *.m4a);;所有文件 (*)",
        )
        if path:
            self.address.setText(path)
            self._play_from_address()

    def _on_dropped(self, payload: list[str]) -> None:
        """拖入文件或链接。多个文件时第一个播放、其余全部进播放列表。"""
        if not payload:
            return
        self.address.setText(payload[0])
        self._play_from_address()

        if len(payload) > 1:
            # 拖进来的东西默认就是想做成一个播放列表，直接全部入列更省事
            items = [MediaItem.from_url(text) for text in payload if text.strip()]
            added = self.playlist.add(items)
            self._show_panel(True)
            self.statusBar().showMessage(
                f"已播放第 1 项；其余 {added} 项已加入播放列表"
            )

    def _snapshot(self) -> None:
        self.controller.take_snapshot()

    # ------------------------------------------------------------ 播放列表
    def _on_item_changed(self, item: object) -> None:
        """播放器换了一条新的媒体。"""
        self.video.show_video()
        if item is None:
            return

        url = getattr(item, "url", "")
        title = getattr(item, "title", "")
        if not url:
            return

        self.panel.set_playing_url(url)
        self.panel.select_url(url)
        # 播过的地址进历史：去重后置顶，所以重复播放同一个地址不会堆积
        self.history.add(MediaItem.from_url(url, title), to_top=True)

    def _add_current_to_playlist(self) -> None:
        """把输入框（或当前正在播）的地址加入播放列表。"""
        item = self.controller.current
        text = self.address.text().strip() or (item.url if item else "")
        if not text:
            self.statusBar().showMessage("请先填入一个地址，再加入播放列表")
            return

        if self.playlist.index_of_url(text) >= 0:
            self.statusBar().showMessage("该地址已经在播放列表里了")
            self._show_panel(True)
            return

        title = item.title if item is not None and item.url == text else ""
        count = self.playlist.add(MediaItem.from_url(text, title))
        if count:
            self._show_panel(True)
            self.statusBar().showMessage(
                f"已加入播放列表（共 {self.playlist.rowCount()} 条）"
            )
        self._refresh_nav_buttons()

    def _play_item(self, item: object) -> None:
        url = getattr(item, "url", "")
        if not url:
            return
        self.address.setText(url)
        self._play_from_address()

    def _play_prev(self) -> None:
        self._step_playlist(-1)

    def _play_next(self) -> None:
        self._step_playlist(1)

    def _step_playlist(self, delta: int) -> None:
        """在播放列表里前后切换（到头折返）。"""
        total = self.playlist.rowCount()
        if total == 0:
            self.statusBar().showMessage("播放列表为空，先加入几个地址")
            return

        current = self.controller.current
        index = self.playlist.index_of_url(current.url) if current else -1
        if index < 0:
            # 当前播放的不在列表里（从地址栏直接播的）：从头/尾进入列表
            target = 0 if delta > 0 else total - 1
        else:
            target = (index + delta) % total

        item = self.playlist.item_at(target)
        if item is not None:
            self.panel.select_url(item.url)
            self._play_item(item)

    def _on_end_of_media(self) -> None:
        """一条播完：按设置决定是否自动播下一条。"""
        if not S.get_bool(S.K_AUTOPLAY_NEXT, True):
            self.statusBar().showMessage("播放结束")
            return

        current = self.controller.current
        index = self.playlist.index_of_url(current.url) if current else -1
        if index < 0 or self.playlist.rowCount() <= 1:
            # 不是从列表里播的，或列表只有一条——没有"下一条"可播
            self.statusBar().showMessage("播放结束")
            return
        self._play_next()

    def _refresh_nav_buttons(self) -> None:
        self.control.set_nav_enabled(self.playlist.rowCount() > 0)

    def _toggle_panel(self, visible: bool) -> None:
        self.panel.setVisible(bool(visible))
        S.settings().setValue(S.K_PLAYLIST_PANEL_VISIBLE, bool(visible))

    def _show_panel(self, visible: bool) -> None:
        """显示/隐藏面板。setChecked 会触发 toggled，进而走到 _toggle_panel。"""
        self.btn_panel.setChecked(bool(visible))

    # ------------------------------------------------------------ 下载
    def _download_current(self) -> None:
        """下载当前地址（输入框优先，其次正在播放的那条）。

        网页地址与直链走的是两条完全不同的链路（见 core/downloader.py），
        这里负责判断该走哪条，并把已知的请求头一并带下去。
        """
        item = self.controller.current
        text = self.address.text().strip()
        if not text and item is not None:
            text = item.external_url

        if not text:
            QMessageBox.information(self, "下载", "请先填入一个地址，或先播放一个视频。")
            return

        from src.core.url_router import UrlKind, classify

        try:
            kind = classify(text)
        except Exception:  # noqa: BLE001
            kind = UrlKind.UNKNOWN

        if kind is UrlKind.UNKNOWN:
            QMessageBox.warning(
                self, "下载",
                f"这个地址无法识别，不知道该怎么下载：\n{text}",
            )
            return

        is_web = kind is UrlKind.WEB_PAGE
        # 解析出来的临时直链有有效期，下载它通常几分钟后就失效；
        # 所以更稳妥的是记下"用户输入的那个页面地址"
        page_url = item.external_url if item is not None else text
        title = item.title if item is not None and item.url == page_url else ""
        headers = item.http_headers if item is not None else None

        task = self.downloads.start(
            page_url if is_web else text,
            title=title or "",
            headers=headers,
            is_web=is_web,
        )
        if task is None:
            return

        self._open_downloads()
        self.statusBar().showMessage(f"已开始下载：{task.dest.name}")

    def _open_downloads(self) -> None:
        """打开下载面板（已经开着就拉到前台）。"""
        if self._downloads_dialog is None:
            self._downloads_dialog = DownloadsDialog(self.downloads, self)
        self._downloads_dialog.show()
        self._downloads_dialog.raise_()
        self._downloads_dialog.activateWindow()

    # ------------------------------------------------------------ 设置
    def open_settings(self) -> None:
        """打开设置面板，并在关闭后把可热生效的项应用上。"""
        dialog = SettingsDialog(self)
        dialog.settingsApplied.connect(self._on_settings_applied)
        dialog.exec()
        # 无论确定还是取消，都要重读一次：用户可能点了「应用」再取消
        self._on_settings_applied()

    def _on_settings_applied(self) -> None:
        """把设置里"改了就能立刻生效"的项同步到运行时。"""
        self.control.set_seek_step_ms(S.get_int(S.K_SEEK_STEP_MS, 5000))
        self.downloads.set_concurrency(S.get_int(S.K_DOWNLOAD_CONCURRENCY, 2))

        if S.get_bool(S.K_CLIPBOARD_WATCH, False):
            self.clipboard.set_enabled(True)
        else:
            self.clipboard.set_enabled(False)

        # 代理开关变化：开了就启动，关了就把端口收回来
        from src.core import local_proxy

        if S.local_proxy_enabled():
            local_proxy.ensure_started(S.get_int(S.K_LOCAL_PROXY_PORT, 0))
        elif local_proxy.is_running():
            local_proxy.stop()

    # ------------------------------------------------------------ 剪贴板
    def _on_clipboard_url(self, url: str) -> None:
        """剪贴板里发现了可播放地址。"""
        self.statusBar().showMessage(f"剪贴板里发现视频地址：{url}")

        if not S.get_bool(S.K_CLIPBOARD_AUTOFILL, True):
            return
        # 只在用户没在输入地址时填入，避免把他正在敲的内容冲掉
        if self.address.text().strip():
            return
        self.address.setText(url)
        self.address.setFocus()

    # ------------------------------------------------------------ 网页地址解析
    def _on_resolve_started(self, page_url: str) -> None:
        """解析期间把画面区切成说明文字。

        解析要联网、可能好几秒，画面上如果还留着上一轮的残留（或一片黑），
        用户会以为程序卡死了。
        """
        self.video.show_message(
            "正在解析网页地址，取出可以直接播放的流地址……\n\n"
            f"{page_url}\n\n"
            "解析由 yt-dlp 完成，通常是几秒钟；若站点响应慢会久一些。",
            title="正在解析",
        )

    # ------------------------------------------------------------ 诊断
    def _diagnose(self) -> None:
        """对输入框里的地址（或当前播放地址）做一次分层诊断。"""
        text = self.address.text().strip()
        if not text:
            item = self.controller.current
            text = item.play_url if item else ""
        if not text:
            self.statusBar().showMessage("请先填入或粘贴一个地址，再点「诊断」")
            return

        self._manual_diag_pending = True
        self.statusBar().showMessage(f"正在诊断：{text}")
        self.controller.run_diagnosis_now(text)

    def _on_diagnosis_finished(self, diag: object) -> None:
        # 自动诊断（每次加载都会跑）结果不在这里展示：
        # 播放成功时展示会把画面区从视频切回文字，反而破坏正常播放；
        # 播放失败时由 _show_error 负责呈现。这里只处理用户主动点的诊断。
        if not self._manual_diag_pending:
            return

        report = getattr(diag, "report", None)
        if not callable(report):
            return
        text = report()
        headline = getattr(diag, "headline", "")

        self._manual_diag_pending = False
        self.statusBar().showMessage(headline or "诊断完成")
        self.video.show_message(text, title="地址诊断")

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("地址诊断")
        box.setText(headline or "诊断完成")
        box.setDetailedText(text)
        box.addButton("知道了", QMessageBox.ButtonRole.AcceptRole)
        box.exec()

    def _open_external(self) -> None:
        item = self.controller.current
        # 解析过的地址交给外部播放器时用回原页面地址：直链是带签名的临时地址，
        # 而 mpv 这类播放器自己就能解析页面（VLC 则至少能报出真实错误）。
        url = item.external_url if item else self.address.text().strip()
        ok, message = open_externally(url)
        self.statusBar().showMessage(message)
        if not ok:
            QMessageBox.warning(self, "无法打开", message)

    def _on_screen_reason(self, title: str, detail: str) -> str:
        """挑一段适合直接显示在画面区上的说明。

        画面区是给"结论"用的，不是给原始报错刷屏用的：解析失败时原始详情里
        带着两条几百字符的签名直链，整段贴上去没人看得下去，所以这里只取
        结论 + 可操作的几条建议，完整内容留在弹窗的「详细信息」里。
        """
        result = self.controller.last_resolution()
        if result is not None and not result.ok and result.error:
            advice = [
                line for line in (result.detail or "").splitlines()
                if line.startswith("· ")
            ]
            text = result.error
            if advice:
                text += "\n\n" + "\n".join(advice)
            return text

        diag = self.controller.diagnosis()
        if diag is not None:
            text = diag.headline
            if diag.advice:
                text += "\n\n" + "\n".join(f"· {item}" for item in diag.advice[:3])
            return text

        return detail if len(detail) <= 400 else detail[:400] + "…"

    def _show_error(self, title: str, detail: str) -> None:
        # 画面区在被判定失败后是空的，把结论留在那里，比只弹窗更不容易被忽略
        self.video.show_message(self._on_screen_reason(title, detail), title=title)

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(title)
        box.setDetailedText(detail)
        # 站点只提供分离的音视频流时，最理想的是"两路一起播"（音画都在），
        # 退一步才是"仅播画面"。这比"直接失败"有用得多，所以给出明确入口。
        merged = None
        if self.controller.can_play_merged():
            merged = box.addButton(
                "同时播放画面与声音（推荐）", QMessageBox.ButtonRole.ActionRole
            )
        video_only = None
        if self.controller.can_play_video_only():
            video_only = box.addButton(
                "仅播放画面（无声音）", QMessageBox.ButtonRole.ActionRole
            )
        open_btn = box.addButton("外部打开", QMessageBox.ButtonRole.ActionRole)
        box.addButton("知道了", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is merged:
            self.controller.play_merged()
        elif clicked is video_only:
            self.controller.play_video_only()
        elif clicked is open_btn:
            self._open_external()

    # ------------------------------------------------------------ 全屏
    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self) -> None:
        self._was_maximized = self.isMaximized()
        self.statusBar().hide()
        self.control.hide()
        self.showFullScreen()
        self.control.set_fullscreen(True)

    def _exit_fullscreen(self) -> None:
        if not self.isFullScreen():
            return
        self.showNormal()
        self.statusBar().show()
        self.control.show()
        self.control.set_fullscreen(False)
        if getattr(self, "_was_maximized", False):
            self.showMaximized()

    # ------------------------------------------------------------ 窗口状态
    def _restore_window_state(self) -> None:
        geometry = S.settings().value(S.K_WINDOW_GEOMETRY)
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = S.settings().value(S.K_WINDOW_STATE)
        if state is not None:
            self.splitter.restoreState(state)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        # 必须先退出全屏再保存几何信息，否则会把"全屏时的尺寸"存成下次启动的
        # 窗口尺寸，下次打开就是一个尺寸很奇怪的窗口。
        if self.isFullScreen():
            self._exit_fullscreen()
        store = S.settings()
        store.setValue(S.K_WINDOW_GEOMETRY, self.saveGeometry())
        store.setValue(S.K_WINDOW_STATE, self.splitter.saveState())
        store.sync()
        # 列表本身是"改动即落盘"，这里再存一次是为了兜住异常退出的边界情况
        self.playlist.save()
        self.history.save()

        # 下载任务必须显式取消：它们是常驻线程，不收拾会让进程退不干净
        if self.downloads.has_active():
            self.downloads.cancel_all()
        # 剪贴板监听与本地代理都是"程序活着才有意义"的东西
        self.clipboard.set_enabled(False)
        from src.core import local_proxy

        local_proxy.stop()

        self.controller.shutdown()
        log.info("窗口关闭，配置已保存")
        super().closeEvent(event)
