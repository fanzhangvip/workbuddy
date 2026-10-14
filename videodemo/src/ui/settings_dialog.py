"""设置面板。

## 设计原则

**每一项都要能说清"改了会发生什么"。** 设置项如果只有名字没有解释，用户只能靠试，
而这里的不少选项（RTSP 传输方式、本地代理、播放内核）试错的代价是"视频打不开"，
很难自己定位回来。所以每个控件都带 tooltip，关键项旁边直接给出当前状态。

**后端切换需要重启。** 播放内核是在 `PlayerController.__init__` 里创建并连线的，
运行中换掉它要重建整套信号连接与画面控件，风险远大于收益。所以这里如实提示
"重启后生效"，而不是假装热切换成功。
"""

from __future__ import annotations

from pathlib import Path
from threading import Thread

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.ui import branding
from src.ui.theme import TOKENS
from src.utils import js_runtime
from src.utils import settings as S
from src.utils.logger import get_logger

log = get_logger(__name__)

#: 需要重启才能生效的键（改了就提示用户）
RESTART_KEYS = (S.K_BACKEND, S.K_MPV_DLL, S.K_PREFER_MPV_FOR_LIVE)


class SettingsDialog(QDialog):
    """分类设置对话框。"""

    #: 用户点了「应用」或「确定」，设置已写入
    settingsApplied = Signal()
    #: 需要重启的项被改动过
    restartRequired = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        # 560 是"标签页 + 一行「标签 + 输入框 + 浏览按钮」"刚好不挤的宽度。
        # 比这更窄的话，QFormLayout 会把输入框压到几十像素，
        # 路径类内容完全看不出来是自己填错了还是被截了。
        self.setMinimumWidth(560)
        self.resize(620, 560)

        self._original: dict[str, object] = {}
        self._restart_keys_changed = False

        self.tabs = QTabWidget()
        self.tabs.addTab(self._scrollable(self._build_playback_tab()), "播放")
        self.tabs.addTab(self._scrollable(self._build_network_tab()), "网络")
        self.tabs.addTab(self._scrollable(self._build_proxy_tab()), "防盗链代理")
        self.tabs.addTab(self._scrollable(self._build_download_tab()), "下载")
        self.tabs.addTab(self._scrollable(self._build_clipboard_tab()), "剪贴板")
        self.tabs.addTab(self._scrollable(self._build_about_tab()), "关于")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        self.btn_defaults = QPushButton("恢复默认")
        self.btn_defaults.setToolTip("把下面所有设置项恢复为出厂值")
        buttons.addButton(self.btn_defaults, QDialogButtonBox.ButtonRole.ResetRole)

        self.label_hint = QLabel("")
        self.label_hint.setWordWrap(True)
        self.label_hint.setStyleSheet(f"color: {TOKENS['text_dim']};")

        # 底部区与标签页之间用一条分隔线划清界限：
        # 「确定 / 取消 / 应用」是整张对话框的，不属于任何一个标签页
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Plain)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(9)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(divider)
        layout.addWidget(self.label_hint)
        layout.addWidget(buttons)

        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)
        self.btn_defaults.clicked.connect(self._on_defaults)

        self._load()

    @staticmethod
    def _scrollable(page: QWidget) -> QScrollArea:
        """把一页设置塞进滚动区。

        网络页与播放页的内容高度都超过了 500px。窗口一矮（笔记本 768 高的屏上
        很常见），Qt 会把放不下的控件压到最小高度——文字被裁、控件重叠，
        这正是"挤占"。包一层滚动区之后，页面保持自己的自然高度，
        装不下就出现滚动条，任何一个控件都不会被压变形。

        `setWidgetResizable(True)` 是必须的：否则页面会保持自己的
        sizeHint 宽度，窄窗口下横向溢出，右侧输入框被切掉。
        """
        area = QScrollArea()
        area.setWidget(page)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return area

    # ------------------------------------------------------------ 播放
    def _build_playback_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.combo_backend = QComboBox()
        self.combo_backend.addItem("QtMultimedia（内置，开箱可用）", "qt")
        self.combo_backend.addItem("libmpv（需自行安装，功能更强）", "mpv")
        self.combo_backend.setToolTip(
            "libmpv 支持强制 RTSP over TCP，也能注入 Referer 等请求头。\n"
            "需要安装 python-mpv 与 mpv-2.dll，详见设置里的说明。\n"
            "切换后需要重启程序。"
        )
        form.addRow("播放内核", self.combo_backend)

        self.label_backend_status = QLabel("")
        self.label_backend_status.setWordWrap(True)
        self.label_backend_status.setStyleSheet(f"color: {TOKENS['text_dim']};")
        form.addRow("", self.label_backend_status)

        self.check_prefer_mpv_live = QCheckBox("直播流优先使用 mpv 内核")
        self.check_prefer_mpv_live.setToolTip(
            "rtsp:// / rtmp:// 这类地址交给 libmpv 处理，容错与重连通常更好。\n"
            "未安装 mpv 时此项无效（会自动用回 Qt 内核）。"
        )
        form.addRow("", self.check_prefer_mpv_live)

        self.check_autoplay_next = QCheckBox("播完自动播放列表里的下一条")
        form.addRow("", self.check_autoplay_next)

        self.check_resume_last = QCheckBox("启动时把上次播放的地址填回地址栏")
        self.check_resume_last.setToolTip("只填入，不自动播放。")
        form.addRow("", self.check_resume_last)

        self.check_auto_merge = QCheckBox("网页地址只有分离流时，自动合流播放（画面+声音）")
        self.check_auto_merge.setToolTip(
            "B 站、YouTube 这类站点把画面和声音放在两条流里（DASH）。\n"
            "勾选后粘贴地址会直接开播，由播放器把两条流同步播出来；\n"
            "取消勾选则每次弹框让你自己选「同时播放画面与声音」还是「仅播放画面」。"
        )
        form.addRow("", self.check_auto_merge)

        self.check_auto_external = QCheckBox("内置内核播放失败后，自动改用系统 VLC/mpv 播放")
        self.check_auto_external.setToolTip(
            "有些格式（RTSP 走 TCP、带鉴权的流、特殊编码）内置内核啃不动，\n"
            "但系统上的 VLC / mpv 能播。勾选后，内置内核彻底失败时会自动\n"
            "把原始地址丢给外部播放器再试一次，而不是只弹错误框。\n"
            "需要本机已安装 VLC / mpv / PotPlayer 之一。"
        )
        form.addRow("", self.check_auto_external)

        self.spin_seek_step = QSpinBox()
        self.spin_seek_step.setRange(1, 300)
        self.spin_seek_step.setSuffix(" 秒")
        self.spin_seek_step.setToolTip("按 ← / → 时快进或快退的秒数")
        form.addRow("方向键步长", self.spin_seek_step)

        return page

    # ------------------------------------------------------------ VPN / 代理
    def _build_vpn_box(self) -> QWidget:
        """VPN／代理区。

        单放一个输入框是不够的：用户真正想知道的是"我现在到底走没走代理、
        那条路通不通"，而这两件事光看配置看不出来（TUN 模式下配置是空的却
        已经走 VPN，系统代理模式下配置也是空的却**没**走）。所以这里除了
        地址栏，还给出「当前生效来源」与一次真实连通性实测。
        """
        box = QGroupBox("VPN / 代理")
        outer = QVBoxLayout(box)

        intro = QLabel(
            "用 VPN 时多数情况下什么都不用填：\n"
            "· TUN／全局模式：接管整机路由，本程序直连就在隧道里，无需配置；\n"
            "· 系统代理模式：程序会自动识别（包括 Clash 的「系统代理」开关），"
            "播放也会自动改走本地反向代理转发。\n"
            "只有「既没开系统代理、又没开 TUN」时才需要在这里手填地址。"
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        row = QHBoxLayout()
        self.edit_proxy = QLineEdit()
        self.edit_proxy.setPlaceholderText("如 http://127.0.0.1:7897，留空表示不指定")
        self.edit_proxy.setToolTip(
            "代理软件的监听地址（Clash 的「混合端口」就是 HTTP 端口，常见为 7897）。\n"
            "支持 http:// 与 socks5:// 两种协议。\n\n"
            "同时用于解析、下载与播放。填错会让本来能播的地址播不了——\n"
            "拿不准就清空，清空后程序仍会自动识别系统代理。"
        )
        self.btn_detect_proxy = QPushButton("检测系统代理")
        self.btn_detect_proxy.setToolTip(
            "读一次环境变量与 Windows 系统代理配置，把找到的地址填进上面。"
        )
        row.addWidget(self.edit_proxy, 1)
        row.addWidget(self.btn_detect_proxy)
        outer.addLayout(row)

        self.label_net_status = QLabel()
        self.label_net_status.setWordWrap(True)
        outer.addWidget(self.label_net_status)

        test_row = QHBoxLayout()
        self.label_net_result = QLabel()
        self.label_net_result.setWordWrap(True)
        self.btn_net_test = QPushButton("测试连通性")
        self.btn_net_test.setToolTip(
            "对同一条地址直连一次、经代理再一次，用对照结果判断问题出在"
            "网络还是代理配置。\n不写地址时默认测 YouTube（被墙的代表站）。"
        )
        test_row.addWidget(self.label_net_result, 1)
        test_row.addWidget(self.btn_net_test, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(test_row)

        self._net_poll = QTimer(self)
        self._net_poll.setInterval(150)
        self._net_poll.timeout.connect(self._poll_net_test)
        self._net_result: list[object] = []

        self.btn_detect_proxy.clicked.connect(self._detect_system_proxy)
        self.btn_net_test.clicked.connect(self._test_network)
        self.edit_proxy.textChanged.connect(self._refresh_net_status)
        return box

    def _detect_system_proxy(self) -> None:
        """把系统里已有的代理配置读出来填进输入框。

        优先级与探测逻辑一致（环境变量 > 注册表）。两者都没有时**不是错误**：
        TUN／全局模式下本来就没有代理配置，那种模式什么都不用填。
        """
        from src.utils import net_proxy

        env = net_proxy.env_proxy()
        system = net_proxy.system_proxy()
        found = env or system
        if not found:
            self.label_net_status.setText(
                "没有检测到环境变量或系统代理配置。\n"
                "如果你正在用 VPN，这通常说明它是 TUN／全局模式——那种模式不需要"
                "在这里填任何东西，直接点「测试连通性」看结果即可。"
            )
            return

        self.edit_proxy.setText(found)
        origin = "环境变量" if env else "Windows 系统代理"
        # 放在 setText 之后：上面那句会通过 textChanged 触发一次状态刷新并覆盖本行
        self.label_net_status.setText(
            f"已填入（来源：{origin}）· {found}\n"
            "记得点「应用」保存；点「测试连通性」可以立刻验证它通不通。"
        )

    def _refresh_net_status(self) -> None:
        from src.utils import proxy_check

        self.label_net_status.setText(
            proxy_check.describe_active(self.edit_proxy.text().strip())
        )

    def _test_network(self) -> None:
        """实测一次（直连 + 经代理，各最多 6 秒）。

        放后台线程跑：两次 HTTP 请求最坏要十几秒，卡在界面线程里会像"死机"。
        线程里不碰任何 Qt 对象，结果用轮询取回——跨线程发信号在这类短命
        对话框上容易在关闭时崩，不如轮询省心。
        """
        if self._net_poll.isActive():
            return
        proxy = self.edit_proxy.text().strip()
        self.btn_net_test.setEnabled(False)
        self.label_net_result.setText("正在测试…（直连一次、经代理一次，各最多 6 秒）")
        self._net_result.clear()

        def work() -> None:
            from src.utils import proxy_check

            try:
                self._net_result.append(proxy_check.check(app_proxy=proxy))
            except Exception as exc:  # noqa: BLE001 - 体检失败不该影响设置面板
                self._net_result.append(exc)

        Thread(target=work, daemon=True).start()
        self._net_poll.start()

    def _poll_net_test(self) -> None:
        if not self._net_result:
            return
        self._net_poll.stop()
        self.btn_net_test.setEnabled(True)
        result = self._net_result[0]
        if isinstance(result, Exception):
            self.label_net_result.setText(f"测试失败：{result}")
            return
        self.label_net_result.setText(result.text())
        self._refresh_net_status()

    # ------------------------------------------------------------ 网络
    def _build_network_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(self._build_vpn_box())

        form = QFormLayout()

        self.edit_ua = QLineEdit()
        self.edit_ua.setPlaceholderText("留空表示使用默认浏览器 UA")
        self.edit_ua.setToolTip("部分站点会拒绝不认识的 User-Agent。")
        form.addRow("User-Agent", self.edit_ua)

        self.edit_cookie, cookie_row = self._file_row(
            "cookies 文件（Netscape 格式）",
            "需要登录才能看的视频（会员内容、私密视频）要在这里指定 cookies。\n"
            "浏览器插件「Get cookies.txt」可以直接导出成这个格式。",
        )
        form.addRow("cookies 文件", cookie_row)

        self.combo_cookie_browser = QComboBox()
        self.combo_cookie_browser.addItem("不使用（手动指定 cookies 文件）", "")
        # Firefox 排在最前：Windows 上新版 Chrome/Edge 的 cookie 已改用「应用绑定
        # 加密」，yt-dlp 解不开（只认 v10），加上浏览器运行时会独占锁住数据库，
        # 实际用不了。Firefox 的 cookie 是明文 SQLite，是目前唯一稳定可读的。
        for label, name in (
            ("Firefox（Windows 上最稳，推荐）", "firefox"),
            ("Chrome（新版加密，通常读不出来）", "chrome"),
            ("Edge（新版加密，通常读不出来）", "edge"),
            ("Brave（同上，通常读不出来）", "brave"),
            ("Opera", "opera"),
            ("Vivaldi", "vivaldi"),
        ):
            self.combo_cookie_browser.addItem(label, name)
        self.combo_cookie_browser.setToolTip(
            "直接从浏览器读登录态，省去手动导出 cookies。\n"
            "优先于「cookies 文件」——文件留空时才生效。\n\n"
            "已知限制：\n"
            "· Windows 上新版 Chrome/Edge 用「应用绑定加密」保存 cookie，"
            "yt-dlp 解不出来，而且浏览器运行时会独占锁住数据库；\n"
            "  要读 Chrome/Edge 的登录态，请改用 Firefox，或用插件导出 cookies.txt 填到上面；\n"
            "· Firefox 读 cookie 前建议先关闭浏览器；\n"
            "· Cookie 有有效期，过期后需重新在浏览器登录；\n"
            "· 频繁请求可能触发网站风控，临时限制访问。"
        )
        form.addRow("从浏览器读 cookie", self.combo_cookie_browser)

        cookie_safety = QLabel(
            "安全提醒：Cookie 包含你的账号登录凭证，读取与解析全部在本机完成，"
            "不会上传到任何服务器。仅用于个人学习测试，请勿分享导出的 Cookie 文件。"
        )
        cookie_safety.setWordWrap(True)
        cookie_safety.setStyleSheet(f"color: {TOKENS['warn']};")
        form.addRow("", cookie_safety)

        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(3, 120)
        self.spin_timeout.setSuffix(" 秒")
        self.spin_timeout.setToolTip("解析 / 下载时单个网络请求的超时时间。")
        form.addRow("网络超时", self.spin_timeout)

        self.spin_resolve_timeout = QSpinBox()
        self.spin_resolve_timeout.setRange(10, 300)
        self.spin_resolve_timeout.setSuffix(" 秒")
        self.spin_resolve_timeout.setToolTip(
            "网页地址解析的总超时（不含下载）。站点响应慢、或解析器要换几种方式\n"
            "重试时，时间给足才拿得到地址；设得太短会把「慢但能成功」误报成「解析超时」。"
        )
        form.addRow("解析超时", self.spin_resolve_timeout)

        self.combo_rtsp = QComboBox()
        self.combo_rtsp.addItem("UDP（Qt 内核的实际行为）", "udp")
        self.combo_rtsp.addItem("TCP（仅 mpv 内核生效）", "tcp")
        self.combo_rtsp.addItem("自动（交给内核决定）", "auto")
        self.combo_rtsp.setToolTip(
            "Qt 内核无法更改 RTSP 传输方式，选 TCP 也不生效——这一点无法绕过。\n"
            "想真正走 TCP，需要在「播放内核」里选 libmpv。"
        )
        form.addRow("RTSP 传输方式", self.combo_rtsp)

        layout.addLayout(form)

        # ---- 网页地址解析：尽可能把地址拿到 ----
        box = QGroupBox("网页地址解析")
        box_form = QFormLayout(box)

        self.edit_js_runtime = QLineEdit()
        self.edit_js_runtime.setPlaceholderText(
            "留空自动探测（内置 quickjs → PATH 里的 deno / node / bun）"
        )
        self.edit_js_runtime.setToolTip(
            "yt-dlp 用它来执行站点页面里的 JavaScript。\n"
            "2026 版 yt-dlp 起，**没有可用的运行时 YouTube 会直接解析失败**"
            "（报「需要确认不是机器人」），而它默认只启用 deno——大多数机器上并没有装 deno。\n"
            "本程序自带一个（jsrt/qjs.exe，约 2 MB），留空就会自动用它；"
            "装了 Node.js 的话会优先用 Node。想指定路径就写 node:C:\\path\\to\\node.exe，"
            "想关掉写 none。"
        )
        box_form.addRow("JS 运行时", self.edit_js_runtime)

        self.check_preflight = QCheckBox("解析前先检查网络可达性")
        self.check_preflight.setToolTip(
            "站点在当前网络连不上时，立刻给出具体原因（是解析不到、还是连不上、"
            "还是 DNS 被污染），\n而不是让解析器白跑二十多秒再报一句「超时」。\n"
            "已配置代理时会自动跳过这项检查。"
        )
        box_form.addRow("", self.check_preflight)

        self.check_doh = QCheckBox("DNS 不可靠时用加密 DNS（DoH）兜底")
        self.check_doh.setToolTip(
            "有些网络会把域名解析成假地址（表现为「域名能解析但连不上」）。\n"
            "开启后会在这种时候用 DoH 再查一次；**只有查到的地址确实能连上**"
            "才会启用，健康网络下不会有任何改动。"
        )
        box_form.addRow("", self.check_doh)

        self.edit_doh_endpoint = QLineEdit()
        self.edit_doh_endpoint.setPlaceholderText("留空使用默认（doh.pub → dns.alidns.com）")
        self.edit_doh_endpoint.setToolTip("也可以填 Cloudflare 的 https://1.1.1.1/dns-query")
        box_form.addRow("DoH 端点", self.edit_doh_endpoint)

        self.check_retry = QCheckBox("被站点拒绝时换一种方式重试")
        self.check_retry.setToolTip(
            "依次尝试：YouTube 备用播放器接口 → 浏览器 TLS 指纹伪装 → 换 UA → 补来源页。\n"
            "只对「换种方式有可能绕过去」的失败重试（机器人校验、403、连接被重置）；\n"
            "网络不通、视频已删除这类不会重试，免得白等。"
        )
        box_form.addRow("", self.check_retry)

        layout.addWidget(box)

        self.label_resolve_status = QLabel()
        self.label_resolve_status.setWordWrap(True)
        self.label_resolve_status.setStyleSheet(f"color: {TOKENS['text_dim']};")
        layout.addWidget(self.label_resolve_status)

        note = QLabel(
            "提示：上面的代理对「解析」「下载」「播放」三处同时生效——检测到代理在用时，"
            "播放会自动改走本地反向代理转发（播放内核自己不认代理配置）。"
            "直链播放若因防盗链失败，请改用「防盗链代理」页的本地反向代理。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {TOKENS['text_dim']};")
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------ 防盗链代理
    def _build_proxy_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        intro = QLabel(
            "播放内核发不出 Referer 请求头，而很多站点正是靠它防盗链（表现为 403）。\n"
            "开启后，播放器会改从本机的一个 HTTP 代理取数据，由代理补上正确的请求头。\n"
            "本地文件、rtsp/rtmp 不受影响；HLS 清单本身不做代理（见下方限制）。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        box = QGroupBox("本地反向代理")
        form = QFormLayout(box)

        self.check_proxy = QCheckBox("启用（http 直链与分片都改走本地代理）")
        form.addRow("", self.check_proxy)

        self.spin_proxy_port = QSpinBox()
        self.spin_proxy_port.setRange(0, 65535)
        self.spin_proxy_port.setToolTip("0 表示由系统自动分配一个空闲端口。")
        form.addRow("监听端口", self.spin_proxy_port)

        self.edit_referer = QLineEdit()
        self.edit_referer.setPlaceholderText("留空表示按目标地址自动推导（推荐）")
        self.edit_referer.setToolTip(
            "例如 https://www.example.com/ 。\n"
            "留空时会用目标地址的站点根地址作为 Referer，多数站点即可通过。"
        )
        form.addRow("Referer", self.edit_referer)

        status_row = QHBoxLayout()
        self.label_proxy_status = QLabel("")
        self.label_proxy_status.setWordWrap(True)
        self.btn_proxy_test = QPushButton("启动并测试")
        self.btn_proxy_test.setToolTip("立刻启动代理并访问一次它的健康检查接口")
        status_row.addWidget(self.label_proxy_status, 1)
        status_row.addWidget(self.btn_proxy_test)
        form.addRow("状态", status_row)

        layout.addWidget(box)

        limit = QLabel(
            "已知限制：HLS（.m3u8）清单里的分片地址若指向源站，会绕过本代理，"
            "此时仍需 mpv 内核或外部播放器。单文件直链与分片直链不受影响。"
        )
        limit.setWordWrap(True)
        limit.setStyleSheet(f"color: {TOKENS['text_dim']};")
        layout.addWidget(limit)
        layout.addStretch(1)

        self.btn_proxy_test.clicked.connect(self._test_proxy)
        return page

    # ------------------------------------------------------------ 下载
    def _build_download_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()

        self.edit_download_dir, download_row = self._file_row(
            "下载目录",
            "留空表示使用系统「下载」文件夹。",
            directory=True,
        )
        form.addRow("下载目录", download_row)

        self.spin_concurrency = QSpinBox()
        self.spin_concurrency.setRange(1, 8)
        self.spin_concurrency.setSuffix(" 个")
        self.spin_concurrency.setToolTip("同时进行的下载任务数。")
        form.addRow("并行下载数", self.spin_concurrency)

        self.edit_ffmpeg, ffmpeg_row = self._file_row(
            "ffmpeg 路径",
            "网页地址（B站等）的画面与声音是两条流，合并成带声音的文件需要 ffmpeg。\n"
            "留空会自动在 PATH 与 imageio-ffmpeg 里查找。",
        )
        form.addRow("ffmpeg 路径", ffmpeg_row)

        layout.addLayout(form)

        self.label_ffmpeg_status = QLabel("")
        self.label_ffmpeg_status.setWordWrap(True)
        self.label_ffmpeg_status.setStyleSheet(f"color: {TOKENS['text_dim']};")
        layout.addWidget(self.label_ffmpeg_status)

        self.btn_ffmpeg_check = QPushButton("检测 ffmpeg")
        self.btn_ffmpeg_check.clicked.connect(self._check_ffmpeg)
        layout.addWidget(self.btn_ffmpeg_check)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------ 剪贴板
    def _build_clipboard_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.check_clipboard = QCheckBox("监听剪贴板，复制视频链接时提示")
        self.check_clipboard.setToolTip(
            "只对「能识别的播放地址」提示（直链 / m3u8 / rtsp / 视频网站页面），\n"
            "普通文本、多行文本、重复内容都不会打扰你。"
        )
        layout.addWidget(self.check_clipboard)

        self.check_clipboard_fill = QCheckBox("识别到之后直接填入地址栏（不自动播放）")
        layout.addWidget(self.check_clipboard_fill)

        note = QLabel(
            "示例：在浏览器里复制 https://www.bilibili.com/video/BVxxxx 后，"
            "程序会提示「剪贴板里发现视频地址」，按回车即可播放。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {TOKENS['text_dim']};")
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------ 关于
    def _build_about_tab(self) -> QWidget:
        """版权与官网。

        放在设置里而不是单独弹一个「关于」对话框：用户想找"这是谁做的、
        去哪找官方"时第一反应就是翻设置，单独做个对话框反而要多找一个入口。
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(9)

        title = QLabel(branding.APP_TITLE)
        title.setStyleSheet(
            f"color: {TOKENS['text']}; font-size: 18px; font-weight: 600;"
        )
        layout.addWidget(title)

        usage = QLabel(branding.USAGE_NOTICE)
        usage.setStyleSheet(f"color: {TOKENS['warn']};")
        layout.addWidget(usage)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Plain)
        layout.addWidget(divider)

        notice = QLabel(branding.COPYRIGHT)
        notice.setWordWrap(True)
        notice.setStyleSheet(f"color: {TOKENS['text']};")
        layout.addWidget(notice)

        site = QLabel(
            f'官方网址：<a href="{branding.WEBSITE}" '
            f'style="color:{TOKENS["accent"]}; text-decoration:none">'
            f"{branding.WEBSITE}</a>"
        )
        site.setTextFormat(Qt.TextFormat.RichText)
        site.setOpenExternalLinks(True)
        site.setWordWrap(True)
        layout.addWidget(site)

        row = QHBoxLayout()
        btn_open = QPushButton("打开官网")
        btn_open.clicked.connect(self._open_website)
        self.btn_copy_site = QPushButton("复制网址")
        self.btn_copy_site.clicked.connect(self._copy_website)
        row.addWidget(btn_open)
        row.addWidget(self.btn_copy_site)
        row.addStretch(1)
        layout.addLayout(row)

        layout.addStretch(1)
        return page

    @staticmethod
    def _open_website() -> None:
        QDesktopServices.openUrl(QUrl(branding.WEBSITE))

    def _copy_website(self) -> None:
        """复制官网地址，并把按钮临时改成「已复制」当反馈。

        用 QTimer 而不是 QMessageBox：复制网址是顺手动作，
        再弹个框要用户点「确定」，比不反馈还烦。
        """
        QApplication.clipboard().setText(branding.WEBSITE)
        self.btn_copy_site.setText("已复制")
        QTimer.singleShot(1500, lambda: self.btn_copy_site.setText("复制网址"))

    # ------------------------------------------------------------ 小工具
    def _file_row(
        self, _label: str, tooltip: str, *, directory: bool = False
    ) -> tuple[QLineEdit, QWidget]:
        edit = QLineEdit()
        edit.setToolTip(tooltip)
        button = QPushButton("浏览…")
        button.setToolTip(tooltip)
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        layout.addWidget(button)

        def choose() -> None:
            if directory:
                path = QFileDialog.getExistingDirectory(self, "选择目录", edit.text() or "")
            else:
                path, _ = QFileDialog.getOpenFileName(self, "选择文件", edit.text() or "")
            if path:
                edit.setText(path)

        button.clicked.connect(choose)
        return edit, row

    # ------------------------------------------------------------ 读写
    def _load(self) -> None:
        store = S.settings()
        self._original = S.as_dict()

        self._select_data(self.combo_backend, S.get_str(S.K_BACKEND, "qt"))
        self.check_prefer_mpv_live.setChecked(S.get_bool(S.K_PREFER_MPV_FOR_LIVE, False))
        self.check_autoplay_next.setChecked(S.get_bool(S.K_AUTOPLAY_NEXT, True))
        self.check_resume_last.setChecked(S.get_bool(S.K_RESUME_LAST, False))
        self.check_auto_merge.setChecked(S.get_bool(S.K_AUTO_MERGE_SEPARATE, True))
        self.check_auto_external.setChecked(S.auto_external_fallback_enabled())
        self.spin_seek_step.setValue(max(1, S.get_int(S.K_SEEK_STEP_MS, 5000) // 1000))

        self.edit_proxy.setText(S.get_str(S.K_PROXY, ""))
        self.edit_ua.setText(S.get_str(S.K_USER_AGENT, ""))
        self.edit_cookie.setText(S.get_str(S.K_COOKIE_FILE, ""))
        self._select_data(self.combo_cookie_browser, S.get_str(S.K_COOKIE_FROM_BROWSER, ""))
        self.spin_timeout.setValue(max(3, S.get_int(S.K_SOCKET_TIMEOUT, 10)))
        self.spin_resolve_timeout.setValue(
            max(10, S.get_int(S.K_RESOLVE_TIMEOUT_S, 60))
        )
        self._select_data(self.combo_rtsp, S.get_str(S.K_RTSP_TRANSPORT, "udp"))

        self.edit_js_runtime.setText(S.get_str(S.K_JS_RUNTIME, ""))
        self.check_preflight.setChecked(S.resolve_preflight_enabled())
        self.check_doh.setChecked(S.doh_fallback_enabled())
        self.edit_doh_endpoint.setText(S.doh_endpoint())
        self.check_retry.setChecked(S.resolve_retry_strategies_enabled())

        self.check_proxy.setChecked(S.local_proxy_enabled())
        self.spin_proxy_port.setValue(max(0, S.get_int(S.K_LOCAL_PROXY_PORT, 0)))
        self.edit_referer.setText(S.get_str(S.K_LOCAL_PROXY_REFERER, ""))

        self.edit_download_dir.setText(S.get_str(S.K_DOWNLOAD_DIR, ""))
        self.spin_concurrency.setValue(max(1, S.get_int(S.K_DOWNLOAD_CONCURRENCY, 2)))
        self.edit_ffmpeg.setText(S.get_str(S.K_FFMPEG_PATH, ""))

        self.check_clipboard.setChecked(S.get_bool(S.K_CLIPBOARD_WATCH, False))
        self.check_clipboard_fill.setChecked(S.get_bool(S.K_CLIPBOARD_AUTOFILL, True))

        # 剪贴板联动：不监听时"自动填入"没有意义
        self.check_clipboard_fill.setEnabled(self.check_clipboard.isChecked())
        self.check_clipboard.toggled.connect(self.check_clipboard_fill.setEnabled)

        self._refresh_status_labels()
        del store

    @staticmethod
    def _select_data(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _refresh_status_labels(self) -> None:
        from src.core.backends.mpv_backend import describe as describe_mpv

        self._refresh_net_status()

        self.label_backend_status.setText(
            describe_mpv()
            + "\n（切换播放内核需要重启程序才会生效。）"
        )

        from src.core import local_proxy

        self.label_proxy_status.setText(local_proxy.describe())

        from src.core.downloader import find_ffmpeg

        found, path = find_ffmpeg()
        self.label_ffmpeg_status.setText(
            f"当前检测到 ffmpeg：{path}" if found
            else "未检测到 ffmpeg：网页地址只能下载到没有声音的画面流。"
        )

        from src.core import resolver

        runtime = resolver.detect_js_runtime()
        if runtime:
            shown = "、".join(
                f"{name}（内置）"
                if str(cfg.get("path", "")).endswith(js_runtime.BUNDLED_EXE)
                else f"{name}（{cfg.get('path') or 'PATH'}）"
                for name, cfg in runtime.items()
            )
            runtime_text = f"JS 运行时：{shown}"
        else:
            runtime_text = (
                "JS 运行时：未找到 —— YouTube 会因此解析失败"
                "（报「需要确认不是机器人」）。装一个 Node.js 即可，"
                "或把 qjs.exe 放到 jsrt/ 目录下。"
            )
        self.label_resolve_status.setText(
            f"{runtime_text}\n{resolver.describe_impersonation()}"
        )

    def _collect(self) -> dict[str, object]:
        return {
            S.K_BACKEND: self.combo_backend.currentData(),
            S.K_PREFER_MPV_FOR_LIVE: self.check_prefer_mpv_live.isChecked(),
            S.K_AUTOPLAY_NEXT: self.check_autoplay_next.isChecked(),
            S.K_RESUME_LAST: self.check_resume_last.isChecked(),
            S.K_AUTO_MERGE_SEPARATE: self.check_auto_merge.isChecked(),
            S.K_AUTO_EXTERNAL_FALLBACK: self.check_auto_external.isChecked(),
            S.K_SEEK_STEP_MS: self.spin_seek_step.value() * 1000,
            S.K_PROXY: self.edit_proxy.text().strip(),
            S.K_USER_AGENT: self.edit_ua.text().strip(),
            S.K_COOKIE_FILE: self.edit_cookie.text().strip(),
            S.K_COOKIE_FROM_BROWSER: self.combo_cookie_browser.currentData(),
            S.K_SOCKET_TIMEOUT: self.spin_timeout.value(),
            S.K_RESOLVE_TIMEOUT_S: self.spin_resolve_timeout.value(),
            S.K_RTSP_TRANSPORT: self.combo_rtsp.currentData(),
            S.K_JS_RUNTIME: self.edit_js_runtime.text().strip(),
            S.K_RESOLVE_PREFLIGHT: self.check_preflight.isChecked(),
            S.K_DOH_FALLBACK: self.check_doh.isChecked(),
            S.K_DOH_ENDPOINT: self.edit_doh_endpoint.text().strip(),
            S.K_RESOLVE_RETRY_STRATEGIES: self.check_retry.isChecked(),
            S.K_LOCAL_PROXY_ENABLED: self.check_proxy.isChecked(),
            S.K_LOCAL_PROXY_PORT: self.spin_proxy_port.value(),
            S.K_LOCAL_PROXY_REFERER: self.edit_referer.text().strip(),
            S.K_DOWNLOAD_DIR: self.edit_download_dir.text().strip(),
            S.K_DOWNLOAD_CONCURRENCY: self.spin_concurrency.value(),
            S.K_FFMPEG_PATH: self.edit_ffmpeg.text().strip(),
            S.K_CLIPBOARD_WATCH: self.check_clipboard.isChecked(),
            S.K_CLIPBOARD_AUTOFILL: self.check_clipboard_fill.isChecked(),
        }

    def apply(self) -> None:
        """把界面上的值写进配置。"""
        values = self._collect()
        store = S.settings()
        for key, value in values.items():
            changed = str(self._original.get(key, "")) != str(value)
            if changed and key in RESTART_KEYS:
                self._restart_keys_changed = True
            store.setValue(key, value)
        store.sync()

        # 这些模块内部有缓存，改了配置要清掉：ffmpeg 路径、JS 运行时探测结果、
        # 指纹伪装可用性、以及按主机名缓存的可达性结论与 DNS 改写表。
        from src.core import reachability, resolver
        from src.core.downloader import reset_ffmpeg_cache

        reset_ffmpeg_cache()
        resolver.reset_js_runtime_cache()
        resolver.reset_impersonation_cache()
        reachability.reset()

        self._original = S.as_dict()
        log.info("设置已保存: %s", values)
        self.settingsApplied.emit()

        if self._restart_keys_changed:
            self.label_hint.setText("已保存。播放内核相关的改动需要重启程序后生效。")
            self.restartRequired.emit()
        else:
            self.label_hint.setText("设置已保存。")

    def _on_ok(self) -> None:
        self.apply()
        self.accept()

    def _on_defaults(self) -> None:
        answer = QMessageBox.question(
            self,
            "恢复默认设置",
            "确定要把所有设置恢复为出厂默认值吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        S.reset_to_defaults()
        self._load()
        self.label_hint.setText("已恢复出厂默认值（点「应用」或「确定」保留本次修改）。")

    # ------------------------------------------------------------ 测试动作
    def _test_proxy(self) -> None:
        from src.core import local_proxy

        enabled = self.check_proxy.isChecked()
        port = self.spin_proxy_port.value()
        server = local_proxy.ensure_started(port)
        if server is None:
            self.label_proxy_status.setText(
                "启动失败：端口可能被占用，换个端口或设为 0（自动分配）再试。"
            )
            return

        healthy = server.health_check()
        self.label_proxy_status.setText(
            f"{'运行中' if healthy else '已启动但健康检查失败'} · {server.status_url()}"
        )
        if not enabled:
            self.label_proxy_status.setText(
                self.label_proxy_status.text()
                + "\n注意：还没勾选「启用」，播放时不会走这个代理。"
            )

    def _check_ffmpeg(self) -> None:
        from src.core.downloader import find_ffmpeg, reset_ffmpeg_cache

        configured = self.edit_ffmpeg.text().strip()
        if configured and not Path(configured).is_file():
            self.label_ffmpeg_status.setText(f"指定的路径不存在：{configured}")
            return

        reset_ffmpeg_cache()
        found, path = find_ffmpeg()
        if found:
            self.label_ffmpeg_status.setText(f"检测到 ffmpeg：{path}")
        else:
            self.label_ffmpeg_status.setText(
                "未找到 ffmpeg。网页地址下载将只能得到没有声音的画面流。"
            )
