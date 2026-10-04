# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（UI-10 ~ UI-16）。

设计要点：
1. **onedir 为主**：挂机类工具追求「启动快、杀软误报低」，目录版比单文件版更稳；
   如需单文件绿色版，用 ``--onefile`` 覆盖（见 scripts/build_exe.py）。
2. **数据写用户目录**：程序里所有可写路径都走 ``%USERPROFILE%\\.ticket-helper``，
   绝不写 exe 所在目录，因此无需管理员权限、也不怕 Program Files 只读（UI-13）。
3. **资源随包**：站点表、Web 前端静态文件、图标一并打进包，开箱即用（UI-12）。
4. **隐藏导入**：werkzeug/simple_websocket 等存在动态导入，必须显式声明，
   否则运行时才会报 ModuleNotFoundError。
5. **体积优化**：剔除测试框架、科学计算等无关大库（UI-15）。
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# PyInstaller 注入的 SPECPATH 就是「spec 文件所在目录」，也就是项目根目录。
# 注意不要再对它取 .parent —— 那会指到项目外面去，导致找不到 main.py、
# 资源文件也会被静默漏掉（打包成功但运行时缺文件，非常难排查）。
PROJECT_ROOT = Path(SPECPATH).resolve()  # noqa: F821 - SPECPATH 由 PyInstaller 注入

if not (PROJECT_ROOT / "main.py").exists():
    raise SystemExit(f"打包配置错误：在 {PROJECT_ROOT} 下找不到 main.py")

# --------------------------------------------------------------------------- #
# 数据文件：源码路径 -> 包内相对目录
# --------------------------------------------------------------------------- #
datas = [
    (str(PROJECT_ROOT / "ticket_helper" / "resources"), "ticket_helper/resources"),
    (str(PROJECT_ROOT / "ticket_helper" / "web" / "static"), "ticket_helper/web/static"),
    (str(PROJECT_ROOT / "ticket_helper" / "env.example.py"), "ticket_helper"),
    # 上游内核整包（P0 引入）。对它来说**只能是数据**：
    # 上游用绝对 import（`from py12306.x import y`），本项目靠垫片把
    # `vendor/pjialin` 挂到 sys.path 末尾才 import 得动（见
    # ticket_helper/kernel/pjialin/shim.py），PyInstaller 的静态分析看不见这条路。
    #
    # ⚠️ 包内目标路径**必须**是 `vendor/pjialin`：shim 的 VENDOR_ROOT 是
    # `Path(__file__).parents[3] / "vendor" / "pjialin"`，冻结后 parents[3]
    # 就是 sys._MEIPASS。目标路径换个名字 → VENDOR_ROOT 指向空目录 →
    # `shim.is_available()` 为假 → **所有 pjialin 后端静默回退到 self**
    # （不报错，行为变了）。由 scripts/check_packaging.py 与
    # tests/test_packaging.py 守住。
    (str(PROJECT_ROOT / "vendor" / "pjialin"), "vendor/pjialin"),
]

icon_path = PROJECT_ROOT / "ticket_helper" / "resources" / "app.ico"
icon = str(icon_path) if icon_path.exists() else None

# --------------------------------------------------------------------------- #
# 隐藏导入
# --------------------------------------------------------------------------- #
hiddenimports = [
    # Flask 生态的动态导入
    "flask",
    "flask_cors",
    "flask_sock",
    "simple_websocket",
    "simple_websocket.ws",
    "werkzeug",
    "werkzeug.serving",
    "werkzeug.middleware.proxy_fix",
    "jinja2",
    "markupsafe",
    "itsdangerous",
    "click",
    "blinker",
    # requests 生态
    "requests",
    "urllib3",
    "charset_normalizer",
    "idna",
    "certifi",
    # 加密
    "cryptography",
    "cryptography.fernet",
    "cryptography.hazmat.backends.openssl",
    # stdlib 中被动态引用的部分
    "sqlite3",
    "smtplib",
    "ssl",
    "email.mime.text",
    "email.header",
    "email.utils",
    # 桌面端
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    # 实时通道（桌面端状态栏「已连接（WebSocket）」）
    # QtWebSockets 依赖 QtNetwork —— 两个都要显式声明，
    # 少任何一个都只会在**打包后**才暴露（源码运行不会）。
    "PySide6.QtNetwork",
    "PySide6.QtWebSockets",
]

# 把本项目所有子模块显式收集，防止「字符串拼接导入」被漏掉
hiddenimports += collect_submodules("ticket_helper")
hiddenimports += collect_submodules("desktop_ui")

# --------------------------------------------------------------------------- #
# 上游内核的第三方依赖（P5）
#
# 为什么必须**手写**在这里：上游模块是运行时从 sys.path 上 import 的
# （见上面 datas 里的说明），PyInstaller 不会去分析 vendor 源码，
# 于是这些包不会被自动收进来。少一个的症状是
# **源码跑得好、打包后一用就打码/过滑块就 ModuleNotFoundError**。
#
# 这份清单**不是靠印象写的**：scripts/check_packaging.py 会对
# vendor 做可达性分析并与本 spec 对账，少一个就报错（`--quiet` 可直接进门）。
# 改动这里之后请重跑那个脚本。
#
# `png`（pypng）看着意外，其实是真的：上游 `helpers/qrcode.py` 顶层
# `import png`，而 `user/job.py` 顶层 `from py12306.helpers.qrcode import print_qrcode`
# —— 所以我们一 import 扫码那条链就把它带进来了。
# --------------------------------------------------------------------------- #
hiddenimports += [
    "requests_html",
    "pyquery",
    "cssselect",
    "w3lib",
    "bs4",
    "parse",
    "lxml",
    "lxml_html_clean",
    "pyppeteer",
    "websockets",
    "pyee",
    "redis",
    "png",
]

# --------------------------------------------------------------------------- #
# 排除项（体积优化 + 避免误报）
# --------------------------------------------------------------------------- #
excludes = [
    "pytest",
    "pytest_cov",
    "requests_mock",
    "freezegun",
    "_pytest",
    "unittest",
    "pydoc",
    "doctest",
    "test",
    "tests",
    # 未使用到的大体积科学计算/图形库（防止被间接拉进来）
    "numpy",
    "scipy",
    "pandas",
    "matplotlib",
    "tensorflow",
    "torch",
    # 可选的本机打码依赖：**必须排除**。
    # 它们只在用户自己 pip 安装后才存在（源码运行方式可用），
    # 一旦被 PyInstaller 顺着 `import ddddocr` 拉进来，
    # 产物会从 ~128MB 涨到 ~277MB（实测：cv2 112MB + onnxruntime 36MB），
    # 而这点能力对点击型登录验证码本就不可靠 —— 不值得这个体积。
    # 打包版按设计走「人工兜底」。
    "ddddocr",
    "cv2",
    "onnxruntime",
    "tkinter",
    # 注意：**不要**排除 PySide6.QtNetwork。
    # QtWebSockets 依赖它（少了会报
    # ``libshiboken: could not import module 'PySide6.QtNetwork'``），
    # 而桌面端的实时通道用的就是 QtWebSockets。
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtMultimedia",
    "PySide6.QtPdf",
    "PySide6.QtDesigner",
    "PySide6.QtTest",
    "PySide6.QtBluetooth",
    "PySide6.QtSql",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
]

# --------------------------------------------------------------------------- #
# 版本信息（UI-14：文件说明/版本号，同时有助于降低杀软误报）
# --------------------------------------------------------------------------- #
version_info = """
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(1, 0, 0, 0),
    prodvers=(1, 0, 0, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'080404B0',
        [StringStruct(u'CompanyName', u'个人项目'),
         StringStruct(u'FileDescription', u'12306 抢票助手（个人自用学习工具）'),
         StringStruct(u'FileVersion', u'1.0.0.0'),
         StringStruct(u'InternalName', u'ticket_helper'),
         StringStruct(u'LegalCopyright', u'仅供个人学习交流使用，禁止商业用途'),
         StringStruct(u'OriginalFilename', u'TicketHelper.exe'),
         StringStruct(u'ProductName', u'12306 抢票助手'),
         StringStruct(u'ProductVersion', u'1.0.0.0')])
    ]),
    VarFileInfo([VarStruct(u'Translation', [2052, 1200])])
  ]
)
"""

# PyInstaller 的 version= 需要的是**版本信息文件的路径**，不是字符串本身。
# 直接传字符串会被当成文件名去 open()，报出莫名其妙的一句 "Invalid argument"。
# 所以这里在构建期把内容落到 build/version_info.txt 再引用它。
_version_file = PROJECT_ROOT / "build" / "version_info.txt"
_version_file.parent.mkdir(parents=True, exist_ok=True)
_version_file.write_text(version_info.strip() + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
block_cipher = None

a = Analysis(  # noqa: F821 - PyInstaller 注入的类
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

# --------------------------------------------------------------------------- #
# 两个入口，共用同一份依赖目录（_internal），只多出几百 KB 的 exe 本身
#
# 为什么必须拆成两个：
#   - 只做 console=True：双击时会先弹出一个黑色控制台窗口，很吓人（甚至被当成病毒行为）；
#   - 只做 console=False：窗口模式下从命令行运行会丢失所有 print 输出，
#     而且 sys.stdout 为 None 会让未加防护的 print 直接抛异常；
#   - 分成「双击用的窗口版」+「命令行版」，两种用法各自都是正常的。
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 产物形态：onedir（默认）或 onefile（`TICKET_HELPER_ONEFILE=1`）
#
# ⚠️ onefile **也必须走这份 spec**。改造前 onefile 是在 scripts/build_exe.py 里
# 另写的一套 `--add-data / --hidden-import` 命令行参数 —— 于是 P0 引入的上游内核
# 只加进了 spec，**onefile 那条路会悄悄产出一个没有上游内核的包**
# （源码模式完全看不出来，装上才发现「打码/过滑块全废」）。
# 「同一语义只有一处实现」在这里的直接收益就是这个。
# --------------------------------------------------------------------------- #
_exe_kwargs = dict(
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # 不压缩：UPX 会显著提高杀软误报率（UI-16）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
    version=str(_version_file),
)

if os.environ.get("TICKET_HELPER_ONEFILE") == "1":
    # 单文件：依赖一起塞进 EXE，不产出 COLLECT 目录。
    # 只出图形界面版 —— 命令行子命令请用目录版的 TicketHelper-CLI.exe。
    exe_gui = EXE(  # noqa: F821
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="TicketHelper",
        console=False,
        exclude_binaries=False,
        **_exe_kwargs,
    )
else:
    # 双击即用的图形界面版（无控制台；不带参数启动会直接打开桌面客户端）
    exe_gui = EXE(  # noqa: F821
        pyz, a.scripts, [], name="TicketHelper", console=False, exclude_binaries=True, **_exe_kwargs
    )

    # 命令行版（保留控制台，用于 web/grab/doctor/init/mock 等子命令）
    exe_cli = EXE(  # noqa: F821
        pyz, a.scripts, [], name="TicketHelper-CLI", console=True, exclude_binaries=True, **_exe_kwargs
    )

    coll = COLLECT(  # noqa: F821
        exe_gui,
        exe_cli,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="TicketHelper",
    )
