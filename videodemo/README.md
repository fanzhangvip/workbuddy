# 万能 URL 视频播放器

粘贴一个视频地址就能播放的 Python 桌面播放器。基于 PySide6 与 QtMultimedia
（内置 FFmpeg 后端），**不需要额外安装 VLC、mpv 或 ffmpeg**。

## 当前支持范围

| 地址类型 | 示例 | 状态 |
|---|---|---|
| 本地文件 | `D:\video\a.mp4` | 支持 |
| 媒体直链 | `https://example.com/a.mp4` | 支持 |
| HLS 点播 | `https://example.com/a.m3u8` | 支持（含带 token 的地址） |
| HLS 直播 | 直播 m3u8（无总时长） | 支持（进度条切直播态） |
| RTSP | `rtsp://192.168.1.10:8554/live` | 支持（**只能走 UDP 传输**，见「已知限制」） |
| RTMP | `rtmp://example.com/live/stream` | 支持 |
| 局域网 HTTP | `http://192.168.1.10:8000/a.mp4` | 支持 |
| 视频网站页面（有音视频合一的流） | `https://vimeo.com/xxx`、普通网页里的 `<video src>` | 支持（自动解析后播放） |
| 视频网站页面（只有分离流的站点） | `https://www.bilibili.com/video/BVxxx` | 解析出画面/声音两条流后**自动本地合流同步播放**（音画都有，无需手动点按钮）；取不到声音流时可退到「仅播放画面」 |

已实现：粘贴/拖入即播、播放暂停、进度拖动、音量静音、倍速、全屏、截图、
上一/下一、错误降级兜底、分层地址诊断、网页地址解析、配置与窗口状态记忆，
以及下面这四块（P3–P6）：

| 功能 | 说明 |
|---|---|
| 播放列表与历史（P3） | 拖拽排序、右键菜单、`▶` 标记当前项、JSON 持久化；历史去重置顶、上限 300 条 |
| 下载到本地（P4） | 直链走 `requests` 流式下载（可取消、断点续传）；网页地址走 yt-dlp，有 ffmpeg 时自动合流 |
| 剪贴板监听 + 设置面板 + 防盗链代理（P5） | 复制链接即提示（去重、防误报）；5 个标签页的设置面板；本地反向代理补发 Referer/UA/Cookie |
| 可选 libmpv 后端（P6） | 装上 `python-mpv` + `libmpv-2.dll` 即可切换内核，用于 Qt 内核啃不动的流（RTSP 走 TCP、自定义请求头）；没装则静默用 Qt |

界面预览见 `docs/screenshots/`（启动态、播放态、解析中、分离流提示——
最后一张只在自动合流不可用时才会出现），
用 `./.venv/Scripts/python.exe tools/capture_ui.py --docs --offscreen` 可重复生成。
> 这几张图是离屏渲染出来的，所以**视频画面那块是空白的**——离屏模式没法建原生
> 视频表面，只有界面本身（地址栏、控制条、提示文字）是真实的。

## 安装

```bash
cd videodemo

# 用托管 Python 创建项目内虚拟环境
"C:/Users/Admin/.workbuddy/binaries/python/versions/3.13.12/python.exe" -m venv .venv

# 安装依赖（国内建议走镜像）
./.venv/Scripts/python.exe -m pip install -r requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple
```

> 注意：`QtMultimedia` **不在** `PySide6-Essentials` 里，必须同时安装
> `PySide6-Addons`，两者版本需一致。`requirements.txt` 已经写好了。

### 可选增强：装了更稳，不装也不影响基础播放

| 组件 | 装上之后能做什么 | 怎么装 |
|---|---|---|
| **ffmpeg** | 下载网页地址时把画面+声音**合流成一个带声音的文件**。B站这类只给分离流的站点，没它就是"下到一段没声音的画面"（程序会如实说明，不会悄悄给你半个文件） | `pip install imageio-ffmpeg`（自带可执行文件，最省事）；或装系统 ffmpeg 并加进 PATH；或在设置里指定路径 |
| **libmpv** | 多一个播放内核：RTSP **可以走 TCP**（Qt 内核只能 UDP）、能直接发自定义请求头、断流重连更稳 | `pip install python-mpv`；再从 [mpv-winbuild](https://github.com/shinchiro/mpv-winbuild-cmake/releases) 下载 `mpv-dev-x86_64-*.7z`，用 7-Zip 或 Windows 自带的 `tar.exe` 解压出 `libmpv-2.dll`（约 115 MB），放到 `<程序目录>/mpv/` 下 |

两处细节是实测踩出来的，装的时候照着做能省事：

- imageio-ffmpeg 自带的文件名叫 `ffmpeg-win-x86_64-v7.1.exe`，而 yt-dlp 只认
  `ffmpeg.exe` 这个名字。程序会自动建一个标准名入口并加进 `PATH`，不用手动改名。
- libmpv 必须在 `import mpv` **之前**进 `PATH`（python-mpv 是导入时加载动态库的），
  程序已在 `prepare_dll_search_path()` 里处理。

装完用自检确认：

```bash
./.venv/Scripts/python.exe main.py --self-check --offscreen
# 会逐项列出 ffmpeg 路径、libmpv 能否真实加载、代理是否启动成功
```

## 运行

```bash
# 直接启动
./.venv/Scripts/python.exe main.py

# 启动并立即播放某个地址
./.venv/Scripts/python.exe main.py "https://example.com/a.mp4"
```

在顶部地址栏粘贴地址后按回车即可播放；也可以把文件或链接直接拖进窗口。

快捷键：`空格` 播放/暂停 · `←`/`→` 快退/快进 5 秒 · `F11` 或双击画面 全屏 · `Esc` 退出全屏

命令行还有两个排查入口（不打开界面）：

```bash
# 解析网页地址，打印拿到了哪条流（不播放）
./.venv/Scripts/python.exe main.py --resolve "https://www.bilibili.com/video/BVxxx" --verbose

# 分层诊断（地址解析 → DNS → TCP → 协议握手）
./.venv/Scripts/python.exe main.py --net-check "rtsp://host:554/path"
```

## 网页地址解析（P2）

粘贴视频网站的**页面地址**（而不是直链）时，程序会先用 yt-dlp 解析出真实流地址，
再交给播放内核。这一步是必须的：页面本身是一堆 HTML，直接丢给播放器只会得到
"Could not open file"。

### 为什么只认"音视频合一的单文件流"

`QMediaPlayer` 一次只能打开**一条 URL**，它没有"下载两条流再合流"的能力
（yt-dlp 默认的 `bestvideo+bestaudio` 是两条流，交给它只能播其中一条：
要么没声音、要么没画面）。所以解析器只挑**音视频在同一个文件里**的格式：

```text
优先  acodec 与 vcodec 都在同一条流里（progressive，如 YouTube 的 360p mp4）
其次  站点只给分离流时——把画面、声音两条流都带回来，改由"本地合流"同时播
```

### 站点只给分离流怎么办（B 站就是这种）

B 站、YouTube 的 DASH 格式把画面和声音放在两条独立的流里。实测 B 站
**一条 progressive 都没有**（`-F` 列出的全是 video-only + audio-only），
而播放内核一次只能打开一条地址，所以单靠一条地址播不出完整的音画。

推荐做法是**本地双流合流**：画面那条流当主源，声音那条流挂成"附加音轨"，
由后端保持两路同步——不转码、不落盘、不引入任何外部依赖。**默认直接自动播放**，
粘贴地址就出音画，不需要点任何按钮（可在设置里关掉"自动合流播放"）。实测：

```text
主[state=PlayingState] 画面帧=156
音[state=PlayingState hasAudio=True] 声音位置=123690ms
```

同步**以画面为主时钟**：视频 seek 会看到明显跳帧，而音频 seek 几乎察觉不到，
所以偏差一律靠调整音频消除。另外两路必须在**同一次 `play()` 里启动**——
错开启动偏差会直接到上百毫秒。

**关于那点残留偏差（实测结论，改之前务必读一遍）**：两条流来自两个独立播放器
实例，声音那一路起播天然慢一些，于是**恒定落后画面 270~600ms**（每次运行数值
不同，但同一次运行内一动不动；关掉自动校正连测 30 秒都钉在同一个数上）。

直觉上应该把这点偏差校正掉，**实测恰恰相反**：把声音 seek 到画面位置要重新
定位，耗时 0.5~2s，这期间声音的 `position` 停在原地——校正**自己**制造出更大
的偏差（实测校正前 452ms → 校正后 992ms），然后每 3 秒再来一刀，偏差在
267↔1225ms 之间摆动，听感是"每 3 秒跳一下"，比不校正（恒定 400ms 左右、
完全不跳）差得多。

所以校正阈值取 1200ms —— **正常播放一次都不校正**（听感连续），只有缓冲欠载
这种秒级偏差才出手；且一刀没把偏差压下去就指数退避（3s → 6s → … → 30s），
一刀未生效绝不补刀。残留的几百毫秒偏差是这套"双播放器合流"方案的已知上限，
要彻底消掉得换 mpv 单播放器内核（由它自己做原生音画同步）。

合流能自动做掉，所以正常路径上**不弹任何框**。只有自动合流不可用时（后端不支持
附加音轨、或你在设置里关掉了这项自动行为）才会给出选择，按推荐程度排列：

1. **「同时播放画面与声音」**——音画都有，首选；
2. 「仅播放画面（无声音）」——省流量，或声音流取不到时的退路
   （实测 B 站 1080P 画面流可解码、时长 3:32、进度正常推进）；
3. 「外部打开」，交给 VLC / mpv 试试。

还有一个容易踩的点：**解析器对这类站点返回的是 `ok=False`**（它如实报告"没有单文件流"），
早期版本据此直接走失败弹框——明明两条流都拿到了，用户却每次都要手动点一下按钮。
现在控件的处理是"先把能做的做掉"：拿到两条流就自动合流开播，实在做不了才提示。

### 三个踩过的坑

1. **不要把 `format` 参数传给 yt-dlp。** 本想用
   `best[acodec!=none][vcodec!=none]` 一步到位，但 B 站上它会直接抛
   `Requested format is not available` 并**中断整个解析**——连 formats 列表都拿不到，
   我们连"为什么没有"都说不清。正确做法：让 yt-dlp 用默认格式（它可能选到需要合流
   的两条，无所谓，我们只用元信息），再自己从 `info["formats"]` 里挑能单独播的那条。
2. **解析必须能硬超时。** yt-dlp 的 `socket_timeout` 只管单个 socket，
   站点不停重定向/重试时仍可能长时间不返回，因此解析跑在子线程里用 `join(timeout)`
   兜住，超时就当失败返回（默认 25 秒）。
3. **`vcodec` / `acodec` 的三种取值不能混。** `"none"` 是明确没有这条轨（B 站视频流
   就是 `acodec="none"`），而 `None`/`""` 是"未知"（普通网页里的 `<video src>` 抓出来
   就是这样）。把 `None` 当成"没有"，会把一个好好的 mp4 误判成"分离流"。

### 无法判定的地址：失败后自动兜底解析

`http://` 地址如果看不出是什么类型（既不是媒体直链、也不在已知站点表里），
策略是**先当媒体直链试一次，失败后自动交给解析器再试一次**——很多站点自建的
播放页正属于这一类。每轮地址只重试一次，不会来回打转。

### 与配置项的联动

解析会读取配置里的 `network/proxy`、`network/cookie_file`、`network/user_agent`
（配置见 `%APPDATA%\UniversalURLPlayer\config.ini`）。需要登录才能拿到的播放地址
（会员内容、需要 cookies 的站点）就是靠 `cookie_file` 解决的。

未安装 yt-dlp 时程序照常能用，只是粘贴网页地址会明确提示"缺少解析组件"并给出
安装命令；打包版自带 yt-dlp，不需要单独安装。

## 打包为 exe（目标机器不需要装 Python）

已经打好了，直接双击就能用：

| 形态 | 位置 | 体积 | 说明 |
|---|---|---|---|
| **目录版**（推荐） | `dist/UniversalURLPlayer/UniversalURLPlayer.exe` | 约 143 MB | 启动快 |
| 单文件版 | `dist/UniversalURLPlayer-single.exe` | 约 63 MB | 一个文件好分发，但每次启动要自解压，冷启动明显更慢 |

> **分发时要把整个 `UniversalURLPlayer/` 目录一起拷走**，不能只拷那个 exe ——
> 依赖都在同级的 `_internal/` 里，只拷 exe 会启动失败。

目标机器上**不需要装 Python，不需要装 VLC / mpv / ffmpeg，也不需要装 VC++ 运行库**
（MSVC 运行库随产物一起打包了 —— 这点很关键，`ffmpegmediaplugin.dll` 依赖
`MSVCP140.dll` / `VCRUNTIME140.dll`，缺了就会"能启动但一播视频就报错"）。

### 自己重新打包

```bash
./.venv/Scripts/python.exe -m pip install pyinstaller -i https://mirrors.aliyun.com/pypi/simple
./.venv/Scripts/python.exe packaging/build.py
```

脚本会打包两种形态，并**自动运行产物自检**确认它能真的解码播放。

参数：

| 参数 | 作用 |
|---|---|
| `--onedir` / `--onefile` | 只打其中一种 |
| `--no-verify` | 跳过自动验证 |
| `--fresh-work` | 每次用全新中间目录、全程不做批量删除（受限/沙箱环境必备） |

### 产物自检

exe 是窗口程序（`console=False`），"一播视频就报错但看不到原因"是这类打包最常见的坑。
程序内置了自检入口：

```bash
# 结构自检
dist/UniversalURLPlayer/UniversalURLPlayer.exe --self-check

# 结构自检 + 实际播一个地址（决定性验证）
dist/UniversalURLPlayer/UniversalURLPlayer.exe --self-check "D:\video\a.mp4"

# 无桌面会话的机器上（需要显示时加 --offscreen）
dist/UniversalURLPlayer/UniversalURLPlayer.exe --self-check --offscreen
```

它检查：Qt 版本、环境变量、关键文件是否齐全、**用 `QPluginLoader` 真的加载一次媒体插件**、
界面层能否构造、目录可写性、P3–P6 各模块能否就位（下载/代理/播放列表/剪贴板/libmpv），
以及可选的实播测试。

> 为什么必须"真的加载一次"：只检查插件文件是否存在是不够的。插件的依赖 DLL
> 缺失时文件照样在，只有真正加载才暴露问题。这是同类项目最容易漏掉的一环。

结果会同时写到 `%APPDATA%\UniversalURLPlayer\logs\selfcheck.txt`。窗口程序没有自己的
控制台，自检会用 `AttachConsole` 把输出接回启动它的那个终端。

`packaging/build.py` 跑完自检后还会多做一步**符号核对**：从产物里解出 `PYZ.pyz`，
按 `packaging/pack_config.py` 的 `REQUIRED_SYMBOLS` 逐条确认 P3–P6 的模块与关键
函数真的在里面。原因是——**自检跑通不等于你这次改的那段代码在产物里**。

> ⚠️ 想确认某段代码是否打进了 exe，**不要对 exe 明文 grep**。模块收进 `PYZ.pyz`
> 后是 zlib 压缩的，grep 必然找不到，会得出"没打进去"的错误结论。
> 用 `tools/pyz_inspect.py`：

```bash
# 产物里有那些项目模块
./.venv/Scripts/python.exe tools/pyz_inspect.py dist/UniversalURLPlayer-single.exe --list

# 某个模块里有没有这些符号（子串匹配，全部命中才返回 0）
./.venv/Scripts/python.exe tools/pyz_inspect.py dist/UniversalURLPlayer-single.exe \
    --module src.core.local_proxy --find pick_header PARAM_USER_AGENT

# 全量扫一遍
./.venv/Scripts/python.exe tools/pyz_inspect.py dist/UniversalURLPlayer-single.exe \
    --find "已改走本地代理"
```


### 为什么产物只有 143 MB（PySide6 安装后有 641 MB）

`Qt6WebEngineCore.dll` 一个就占 194 MB，加上 3D / Quick / Qml / Charts / PDF 等等，
绝大部分与播放器无关。`packaging/pack_config.py` 会枚举环境里实际存在的 Qt 模块，
**只保留播放器用到的 6 个**（Core / Gui / Widgets / Multimedia / MultimediaWidgets /
Network），其余 53 个全部排除。

体积的构成大致是：Qt 精简后端 ~70 MB、FFmpeg 的 `avcodec/avformat/avutil`
（`avcodec-61.dll` 单个 13.4 MB）+ MSVC 运行库 ~25 MB、Python 运行时 ~15 MB、
**yt-dlp ~30 MB**（P2 加入，用于解析网页地址），其余是 Qt 插件与图标资源。
所以从 133 MB 涨到 143 MB 是 P2 带来的正常增量，不是打包变胖。

这么做的依据是实测的链接期依赖，不是猜的：

```
Qt6Multimedia.dll -> Qt6Core.dll, Qt6Gui.dll, Qt6Network.dll
Qt6Widgets.dll    -> Qt6Core.dll, Qt6Gui.dll
Qt6Gui.dll        -> Qt6Core.dll
```

除保留的这几个之外，QtMultimedia 不依赖任何其它 Qt6 库，所以排除是安全的。
`src/` 里也只 import 了这几个模块（已用脚本核对过）。

## 两个关键实现细节

这两点是本项目能否正常播放的成败点，改动时不要动它们：

1. **`QT_FFMPEG_PROTOCOL_WHITELIST` 必须在创建 `QApplication` 之前设置。**
   FFmpeg 出于安全考虑默认禁止"嵌套协议"，而 HLS 内部嵌套 `http` + `crypto`
   （AES-128 密钥）、RTSP 内部嵌套 `rtp/rtcp/udp`。不放行则 m3u8 与 rtsp 直接失败。
   见 `src/utils/env_setup.py`，由 `main.py` 在最顶部调用。

2. **`QMediaPlayer` 对打不开的地址常常不发 `errorOccurred`，会一直停在加载中。**
   必须用定时器 + 状态检查强制判定失败，否则界面会永久假死。
   见 `src/core/player_controller.py` 的 `_on_load_timeout`。

## 项目结构

```
videodemo/
├─ main.py                     入口：先设环境变量 → 再建 QApplication
├─ requirements.txt
├─ assets/icon.ico             程序图标（由 tools/make_icon.py 生成）
├─ jsrt/qjs.exe                内置 JavaScript 运行时（QuickJS-ng 0.17.0，约 2 MB，MIT）
│                              来源 github.com/quickjs-ng/quickjs 的 qjs-windows-x86_64.exe
│                              解析 YouTube 必需，详见下文"解析不了 YouTube"
├─ packaging/                  打包配置
│  ├─ build.py                 一键打包 + 自动验证产物
│  ├─ pack_config.py           排除清单、隐藏导入、符号核对清单（两个 spec 共用）
│  ├─ UniversalURLPlayer.spec          目录版
│  └─ UniversalURLPlayer-onefile.spec  单文件版
├─ src/
│  ├─ core/
│  │  ├─ url_router.py         URL 分类（只看 path 取扩展名，忽略 ?token=）
│  │  ├─ media_item.py         播放条目数据模型（含解析前后的地址与类型）
│  │  ├─ resolver.py           网页地址解析（yt-dlp）：挑单文件流、给出可读结论
│  │  ├─ player_controller.py  状态机、超时兜底、直播识别、解析调度、外部播放器兜底
│  │  ├─ playlist.py           播放列表/历史的模型与 JSON 持久化（P3）
│  │  ├─ downloader.py         下载管理器：requests 流式续传 + yt-dlp（P4）
│  │  ├─ local_proxy.py        本地反向代理：补发 Referer/UA/Cookie（P5）
│  │  └─ backends/
│  │     ├─ base.py            播放后端抽象接口
│  │     ├─ qt_backend.py      QtMultimedia 实现
│  │     ├─ mpv_backend.py     libmpv 实现（可选内核，P6）
│  │     └─ factory.py         后端工厂（选 mpv，失败必回退 Qt）
│  ├─ ui/
│  │  ├─ main_window.py        主窗口：地址栏 + 画面 + 控制条 + 列表 + 诊断/解析入口
│  │  ├─ video_widget.py       画面容器：双击全屏、拖放接收、显示失败原因
│  │  ├─ control_bar.py        播放控制条
│  │  ├─ playlist_panel.py     播放列表/历史面板（拖拽排序、右键菜单）
│  │  ├─ downloads_dialog.py   下载面板（进度条、取消、重试）
│  │  ├─ settings_dialog.py    设置面板（播放/网络/代理/下载/剪贴板）
│  │  └─ fonts.py              中文字体保障（避免缺字体显示方框）
│  └─ utils/
│     ├─ env_setup.py          环境变量（见上文第 1 点）
│     ├─ urls.py               地址规范化（本地路径 → file:// URL）
│     ├─ net_diagnose.py       分层连通性诊断（DNS/TCP/协议握手 + 建议）
│     ├─ net_proxy.py          代理探测（设置 > 环境变量 > Windows 系统代理）
│     ├─ proxy_check.py        VPN/代理体检：生效来源 + 直连/经代理对照实测
│     ├─ js_runtime.py         JS 运行时定位（内置 qjs 优先，其次 PATH）
│     ├─ settings.py           QSettings 封装 + 出厂默认值表
│     ├─ clipboard_watcher.py  剪贴板监听（类型过滤 + 去重，防误报）
│     ├─ paths.py              路径解析（兼容打包）
│     ├─ logger.py             日志 + Qt/FFmpeg 消息转发
│     ├─ playback_probe.py     单地址播放探测（源码与 exe 自检共用同一套判定）
│     └─ selfcheck.py          运行时自检（产物完整性 + 解析 + P3–P6 能力检查）
├─ tests/                      单元测试（不依赖外部网络与桌面）
│  ├─ httpfixture.py           真本地 HTTP 服务器（支持 Range/限速/记录请求头）
│  ├─ test_url_router.py       URL 分类、数据模型、解析前后的地址语义
│  ├─ test_resolver.py         解析器的格式筛选、结果组装、报错翻译
│  ├─ test_playlist.py         拖拽排序、去重、原子保存、坏文件留档
│  ├─ test_downloader.py       完成/取消/续传/请求头/并发/yt-dlp
│  ├─ test_local_proxy.py      转发、Range 透传、Referer 注入、可否代理判定
│  ├─ test_clipboard_watcher.py 识别规则与去重
│  ├─ test_backends.py         后端工厂与 libmpv 可用性（未装时的行为）
│  ├─ test_merged_playback.py  分离流合流 + 防盗链代理路由
│  ├─ test_selfcheck_target.py 自检"实播前准备地址"的判定（网页地址必须先解析）
│  ├─ test_net_diagnose.py     诊断分层判定
│  ├─ test_net_proxy.py        代理探测与 no_proxy 例外
│  ├─ test_proxy_check.py      VPN/代理体检的结论方向与设置面板联动
│  └─ test_js_runtime.py       内置 JS 运行时的定位与 yt-dlp 认可度
└─ tools/                      排查与验证工具
   ├─ net_check.py             命令行分层诊断（首选排查入口）
   ├─ vpn_check.py             VPN/代理体检：生效来源 + 直连/经代理对照实测
   ├─ site_check.py             网页地址一站式排查：预检 → 逐策略解析 → 能否拿到播放地址
   ├─ env_probe.py             环境能力探测 / 单地址播放测试（--resolve 可先解析）
   ├─ smoke_test.py            集成冒烟测试（离屏驱动真实窗口 + P3–P6 功能链路）
   ├─ page_play_check.py       网页地址全链路检查：解析 → 播放 → 合流 / 仅画面
   ├─ yt_node_scan.py          扫 Clash 节点，挑出能过 YouTube 反爬的那个（会自动还原选择）
   ├─ rtsp_shape_probe.py      验证 RTSP 请求路径是否被原样发送
   ├─ capture_ui.py            界面截图（--docs 直接产出 README 的预览图）
   ├─ pyz_inspect.py           从 exe 里取出模块，确认改动真的打进去了
   ├─ make_icon.py             生成 assets/icon.ico
   └─ install_wheel_offline.py 离线安装 wheel（见下文）
```

配置、日志与数据文件位于 `%APPDATA%\UniversalURLPlayer\`：
`config.ini`、`logs\app.log`、`playlist.json`、`history.json`。
默认下载目录是系统「下载」文件夹（可在设置里改；取注册表实际路径，
所以被 OneDrive 重定向过也能找对）。
截图默认存到项目下的 `snapshots\`。

## 排查问题

### 解析不了 YouTube：先分清"缺 JS 运行时"和"机器人校验"

这两件事看起来都表现为"YouTube 解析失败"，但成因和解决办法完全不同。
程序会在报错详情里直接说清是哪一种。

**① 缺 JavaScript 运行时** —— 内置的那份丢了，或被人为删掉。
yt-dlp 2026 版起，YouTube 的播放器挑战要跑一段 JS，没有真正的 JS 引擎就解析不了。
本程序自带 `jsrt/qjs.exe`（QuickJS-ng，约 2 MB），**正常情况下不需要你装任何东西**。
自检里应当看到：

```
JS 运行时   : quickjs（内置）、node
```

装过 Node.js 的话会优先用 Node（更快）。`jsrt/qjs.exe` 不在时按提示补回去，
或在设置里手填 `node:C:\path\to\node.exe`。

**② 机器人校验（Sign in to confirm you're not a bot）** —— 最常见的那个。
公开视频也会这样，**和视频本身无关**，换播放器接口也没有用：
实测 13 个 `player_client`、三种 JS 运行时（含内置 quickjs）全部被拒，
用浏览器 UA 直接请求观看页同样返回 `LOGIN_REQUIRED`。

原因是 **YouTube 不信任这个出口 IP 的声誉**。注意一个反直觉的实测结论：

> **不是"机房 IP 一律被拒"。** 同一份订阅里、同为机房 IP 的两个节点，
> 一个被拒（FDCservers），另一个直接放行（Psychz Networks）。
> 所以正确做法是**换节点逐个试**，而不是去找"住宅 IP"。

最快的判定方法：**用浏览器打开同一个视频**——浏览器也弹验证，就是 IP 的问题
（跟播放器、跟 yt-dlp 都无关）；浏览器能播，才是客户端侧的问题。

确认出口归属：

```bash
curl -x http://127.0.0.1:7897 "http://ip-api.com/json/?fields=country,isp,org,hosting"
```

**自动找可用的节点**（Clash Verge，仓库自带工具）：

```bash
./.venv/Scripts/python.exe tools/yt_node_scan.py            # 扫全部节点
./.venv/Scripts/python.exe tools/yt_node_scan.py --limit 10 # 只扫前 10 个
```

它通过 Clash Verge 的控制接口（命名管道）逐个切换节点，实测「出口归属 + YouTube
是否放行」，**最后一定会还原原来的选择**。输出像这样：

```
  [ 1/8] ❌ 🇺🇸美国洛杉矶1号   United States FDCservers.net [机房] 机器人校验
  [ 3/8] ✅ 🇺🇸美国洛杉矶3号   United States Psychz Networks [机房] 放行 · 2026年8月必聽新歌…
```

找到之后，在 Clash Verge 里把对应分组**固定**到那个节点即可。别留在「自动选择」——
自动测速挑的是延迟最低的，而最快的那台未必是 YouTube 放行的。

其它办法：

- 换网络（手机热点）快速区分是不是当前链路的问题；
- 补登录态：设置 → 网络 → 「cookies 文件」。**Windows 上新版 Chrome/Edge 存的是
  应用绑定加密的 cookie，yt-dlp 解不出来**，请改用 Firefox，或用浏览器插件
  导出 `cookies.txt` 再填进去。

**这些都不是"再多试几次就会好"**，所以程序不会去空转重试策略。

### 一条命令看清"为什么播不了"

播放器内置了**分层诊断**：`地址解析 → DNS → TCP 连接 → 协议握手`。
哪一层断了，就停在哪一层并给出对应的中文结论与建议。

```bash
./.venv/Scripts/python.exe tools/net_check.py "rtsp://host:554/path"
./.venv/Scripts/python.exe tools/net_check.py --timeout 8 "rtmp://ip:1935/livetv/cctv1"
./.venv/Scripts/python.exe tools/net_check.py --no-probe "http://x/a.mp4"   # 只测到 TCP
```

**`http/https` 地址如果当前有代理在生效，会改走「经代理探测」分支**，不再做直连
DNS/TCP 探针（代理模式下包是发往代理本机的，直连结论与能否播放无关）。

这一步是必须的：媒体地址改走本地反向代理后、画面声音都在正常播放，直连探测却
仍然超时——若不改，会把一条**正在播的好地址**报成"服务器已下线、地址可能已失效"。
现在同样的地址会得到：

```
[跳过] 代理检测
        检测到系统代理 http://127.0.0.1:7897，改走代理探测（直连结论不适用）
[通过] 经代理探测（676 ms）
        经代理 http://127.0.0.1:7897 得到 HTTP 206（676 ms）

结论：地址可达（经代理）——若仍无法播放，问题在解码或传输环节，不在网络。
```

`rtsp/rtmp` 等协议不受 HTTP 代理影响，仍按直连探测。

界面上也有对应入口：顶部「诊断」按钮。打包后的 exe 同样支持：

```bash
UniversalURLPlayer.exe --net-check "rtsp://host:554/path"
```

exe 是窗口程序没有控制台，所以结果会用弹窗呈现，并同时写入日志目录。

诊断只在 TCP 连通后继续做**协议层只读探测**（RTSP 发 `OPTIONS`、RTMP 只做握手、
HTTP 发 `HEAD`），不会真正拉流，因此很快（通常 1 秒内）。

**播放失败时不用手动去跑诊断**：每次加载都会在后台并行诊断一次，
一旦发现 DNS/TCP/TLS 这类硬性失败就立刻结束等待并弹出具体原因，
不再干等超时。诊断报告同时写进 `logs\app.log`。

### 网页地址为什么拿不到播放地址（site_check）

`net_check.py` 只管网络连通，网页地址还要再走一步**解析**。`site_check.py`
把这两步串起来，一条命令看清"到底卡在哪"：

```bash
# 一站式：网络可达性预检 → 逐策略解析 → 最终能否拿到播放地址
./.venv/Scripts/python.exe tools/site_check.py "https://www.youtube.com/watch?v=xxx"
./.venv/Scripts/python.exe tools/site_check.py --no-preflight "https://example.com/page"
./.venv/Scripts/python.exe tools/site_check.py --verbose "https://www.bilibili.com/video/BV1xx"
```

它会分三段输出：

1. **网络可达性预检** —— 在解析前先探一次 DNS + TCP。站点在当前网络下
   被整段拦掉时（典型表现：域名能解析出 IP，但 TCP 连不上，即"DNS 污染 +
   网络拦截"），直接给结论并跳过注定失败的解析，省下二十多秒空等。
   解析器内部也是同一条预检逻辑，所以"预检说不可达"和"播放器直接报连不上"
   是同一套判定。
2. **解析尝试** —— 逐条策略尝试：默认配置 → 备用播放器接口（YouTube）→
   浏览器指纹伪装（`curl_cffi`）→ 补发来源页 Referer。哪条拿得到就走哪条。
3. **结论** —— 拿到地址就给可播地址与类型；拿不到就说清是"要登录 / 地区
   限制 / 不支持该站"还是"网络根本不通"。

### 四类失败分别意味着什么

| 卡在哪一层 | 含义 | 该怎么办 |
|---|---|---|
| 地址解析 | 地址格式不对，或本地文件不存在 | 检查拼写 / 换「打开文件」 |
| DNS 解析 | 域名解析不出来 | 核对拼写、换 IP、检查 DNS 或代理 |
| TCP 连接超时 | 服务器不可达 | 地址多半已失效；也可能是网络/防火墙屏蔽了该端口 |
| TCP 连接被拒绝 | 端口上没有服务 | 端口号或地址写错了 |
| 协议握手失败 | 连上了但路径/应用名不对 | 核对路径、应用名、流名（大小写敏感） |
| 全部通过仍播不了 | 服务器正常，问题在解码或传输 | 看日志里 FFmpeg 的报错 |

### 环境与产物探测

```bash
# 静态检查：后端版本、多媒体插件、FFmpeg 协议支持、yt-dlp 可用性
./.venv/Scripts/python.exe tools/env_probe.py

# 实测某个地址能否播放（会打印每一步的加载状态）
./.venv/Scripts/python.exe tools/env_probe.py "rtsp://192.168.1.10:8554/live"

# 网页地址：默认就会先解析再播；--resolve 强制解析、--resolve-only 只解析不播
./.venv/Scripts/python.exe tools/env_probe.py "https://www.bilibili.com/video/BVxxx"
./.venv/Scripts/python.exe tools/env_probe.py "<地址>" --resolve-only

# 带 FFmpeg 详细日志
./.venv/Scripts/python.exe tools/env_probe.py "<地址>" --debug
```

判断标准：点播看是否拿到 `duration > 0`；直播没有时长，看是否真的收到画面帧。
网页地址先看解析结果（拿到了哪条流、是不是单文件），再看能否解码。

**网页地址走完整链路**（解析 → 播放 → 分离流时合流）：

```bash
# 把地址粘进界面后到底会发生什么，一条命令看全。
# 分离流时会看到"控制器已自动合流播放"——这一步验证的正是"不用点按钮"；
# 判定要求画面出帧**且**声音在推进，缺一都算失败
# （避免"画面能看但没声音"被当成成功）。
./.venv/Scripts/python.exe tools/page_play_check.py "https://www.bilibili.com/video/BVxxx?t=62"

# 只走「仅播放画面」，不尝试合流
./.venv/Scripts/python.exe tools/page_play_check.py "<页面地址>" --video-only

# 什么都不做，用来确认站点确实没有单文件流
./.venv/Scripts/python.exe tools/page_play_check.py "<页面地址>" --no-degrade
```

它的成功判定要求**真的解出过画面帧**——自己接一路 `QVideoSink.videoFrameChanged`
并至少收到 3 帧，或播放位置推进超过 1.5 秒；不接受"加载完成就算成功"。

> 注意别拿 `backend.grab_frame()` 来判"有没有出画面"：它内部有个"抓控件画面"的
> 兜底分支，离屏下控件 `grab()` 永远返回非空图，**没解码也会被判定成有画面**。
> 只有 `videoFrameChanged` 是硬证据。

**跑集成测试**：

```bash
# 不需要外部服务的场景（含"网页地址解析"：B 站地址必须走通解析流程）
# 跑完地址场景后，会再用真实窗口跑 7 项 P3–P6 功能链路检查
#（下载落盘、走代理播放、播放列表切换、历史去重、剪贴板、设置热生效、mpv 回退）
./.venv/Scripts/python.exe tools/smoke_test.py --suite

# 再加上需要本地服务的场景（服务不在、或流没在推，会自动跳过而不是报失败）
./.venv/Scripts/python.exe tools/smoke_test.py --suite --with-local

# 只跑地址场景，不碰真实播放列表/历史/配置（在别人机器上跑时更安全）
./.venv/Scripts/python.exe tools/smoke_test.py --suite --no-feature

# 单元测试（208 项，不依赖网络与桌面）
./.venv/Scripts/python.exe -m unittest discover -s tests
```

### 日志

`%APPDATA%\UniversalURLPlayer\logs\app.log` 里除了程序自身的日志，
还收录了 **Qt 与 FFmpeg 自身的报错**（通过 Qt 消息处理器转发）。
窗口程序默认看不到 stderr，"能连上服务器但播不了"这类问题几乎只能靠这些信息定位。

## 本地测试素材

需要一个本地 RTSP/RTMP/HLS 服务来验证实时流时（`mediamtx` 可从
https://github.com/bluenviron/mediamtx/releases 下载，解压出 `mediamtx.exe`）：

```bash
# 1) 启动流媒体服务（默认提供 rtsp:8554 / rtmp:1935 / hls:8888）
#    同时打开 HTTP 控制接口(:9997)——冒烟测试靠它判断某条流是否真的在推，
#    否则推流到期后会产生"假失败"
MTX_API=yes MTX_APIADDRESS=:9997 ./mediamtx.exe

# 2) 生成一段测试视频（此处用系统自带 ffmpeg，仅测试用途）
ffmpeg -f lavfi -i "testsrc2=size=640x360:rate=25" -f lavfi -i "sine=frequency=440" \
  -t 12 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest local_test.mp4

# 3) 推 RTSP 流（smoke_test 使用路径名 test）
ffmpeg -re -stream_loop -1 -i local_test.mp4 -c copy -f rtsp \
  -rtsp_transport tcp rtsp://127.0.0.1:8554/test

# 4) 推 RTMP 流（smoke_test 使用路径名 rtmptest）
ffmpeg -re -stream_loop -1 -i local_test.mp4 -c copy -f flv \
  rtmp://127.0.0.1:1935/rtmptest

# 5) 起本地 HTTP 服务，模拟局域网直链
#    必须指向 .tools/testmedia/ —— 冒烟测试访问的是
#    http://127.0.0.1:8811/local_test.mp4，指向项目根会 404
./.venv/Scripts/python.exe -m http.server 8811 --bind 127.0.0.1 --directory .tools/testmedia

# 6) 另起一个指向项目根目录的静态服务（:8821），供"网页地址解析"场景使用
#    冒烟测试会访问 http://127.0.0.1:8821/.tools/webtest/page.html
./.venv/Scripts/python.exe -m http.server 8821 --bind 127.0.0.1 --directory .
```

## 已知限制

- **RTSP 只能走 UDP 传输，无法改成 TCP。**
  Qt 的 FFmpeg 后端没有开放这个开关——实测扫描 `ffmpegmediaplugin.dll`
  里全部 `QT_*` 变量后确认它只识别 `QT_FFMPEG_PROTOCOL_WHITELIST`、
  `QT_FFMPEG_DEBUG` 等，**没有** `rtsp_transport` / `rtsp_flags` 这一类；
  自带 `avformat-61.dll` 里的 rtsp 协议别名也只有 `rtsp` 与 `rtsps`。
  后果：在丢弃 UDP 的网络里（部分公司内网、部分运营商移动网络），
  RTSP 会表现为"一直加载却不出画面"。
  绕法：点「外部打开」用 VLC / mpv，或后续接入 mpv 后端（P6）。

- **只给分离流的站点（B站、YouTube 的 DASH）靠"双流合流"播放。**
  粘贴地址后**自动开始**，不需要点按钮（设置里可关掉自动、改成每次询问）。
  做法是两个播放内核实例：主源播画面、附加音轨播声音，后端以画面为主时钟
  做同步校正（**偏差容忍 1200ms**，超出才把声音对齐到画面——实测声音起播
  固有落后 270~600ms，而校正一次的代价是 0.5~2s 的无声停顿，所以阈值必须
  明显高于固有差，否则"每 3 秒跳一下"；详见上文「关于那点残留偏差」）。
  两点代价：两条流都占带宽（B站 1080P 实测约 90MB 画面 + 16MB 声音）；
  且**需要两条流都能取到**——声音流取不到时自动降级为无声，并在状态栏说明原因，
  画面不受影响。拖进度条时两路一起 seek。
- **B站等防盗链直链不稳定**：其 CDN 直链按时间/区域分发到不同主机，且部分
  强制校验 `Referer`，而 `QMediaPlayer` 没有注入请求头的接口。实测同一视频
  有时能取到、有时 403。**本地反向代理（P5）默认就是开着的**，由它补发
  `Referer`/`User-Agent`，这类直链通常直接就能播；程序仍保留「外部打开」
  作为最后退路。关掉代理后会退回"取得到就播、取不到就说清楚"。
  代理只绑 `127.0.0.1`，且**不代理回环地址**（本机服务没有防盗链问题，
  绕一圈纯属多余）。
  代理的已知边界：**HLS 清单不代理**（`.m3u8` 里的分片地址若指向源站会绕过
  代理），rtsp/rtmp 本来就不走 HTTP，本地文件自然也不需要。
- **解析需要联网，且受站点改版影响。** yt-dlp 靠各站点的提取规则工作，
  站点改版后可能需要升级 yt-dlp（`pip install -U yt-dlp`）。解析默认 25 秒硬超时。
- **下载网页地址时，缺 ffmpeg 只能拿到"没声音的画面流"（或单独的声音流）。**
  B站等站点把画面和声音分成两条流，合流需要 ffmpeg。程序会自动在 PATH 与
  `imageio-ffmpeg` 里找，也可以在设置里指定路径；**找不到时会在下载前就告知**，
  不会让人以为下载成功了却只有画面。直链下载不受影响。
- **libmpv 是可选内核，不是默认内核。** 需要 `python-mpv` + `mpv-2.dll`；
  没装时所有相关入口都会说明原因并继续用 Qt 内核（绝不因为"想用 mpv"而播不了）。
  装上之后可在设置里选内核，或只对直播流优先使用。
- **截图**：若当前帧尚未渲染到画面控件上，可能拿到黑图或失败提示。
- **`tools/install_wheel_offline.py`**：本机沙箱有"批量删除保护"，pip 安装大包
  会在清理临时目录时被拦截，导致文件解压不全（表现为能 `import` 但插件缺失、
  运行时才报错）。该脚本只读 zip 写文件、不删除任何东西，可稳定完成安装。
  用法：先下载 wheel，再 `python tools/install_wheel_offline.py <wheel路径>`。

- **打包时的批量删除限制**：同一个限制也会打断 PyInstaller —— 它每次构建都要清空
  上次的中间目录（250+ 个文件），超额后构建会以 `exit code 1` 静默失败，报错信息
  很难看出根因。用 `packaging/build.py --fresh-work` 可绕开（改用全新中间目录、
  全程不做批量删除）。副作用是 `build/` 下会累积带时间戳的旧目录，可随时整个删掉。
  同理，`--fresh-work` 模式下旧产物会被改名成 `*.stale-<时间戳>` 而不是删除。

## 后续计划

- ~~**P2** 接入 yt-dlp 解析视频网站页面地址~~ —— 已完成，见上文「网页地址解析（P2）」
- ~~**P3** 播放列表与历史记录（拖拽排序、JSON 持久化）~~ —— 已完成，见下文「P3–P6」
- ~~**P4** 下载到本地（直链用 requests 流式，网页地址用 yt-dlp + 进度回调）~~ —— 已完成
- ~~**P5** 剪贴板监听、设置面板、本地反向代理解决防盗链~~ —— 已完成
- ~~**P6** 可选接入 libmpv 后端~~ —— 已完成
- ~~**P7** PyInstaller 打包为 exe~~ —— 已完成，见上文「打包为 exe」

## P3–P6 功能说明

### P3 播放列表与历史记录

- 地址栏右侧「加入」把当前地址（或正在播的那条）加进播放列表；拖文件到窗口时
  **第一个播放、其余自动入列**。
- 面板里可以直接**拖拽排序**（多选拖拽保持相对顺序），右键有上移/下移/移除；
  正在播的那条前面有 `▶`。
- 控制条的「上一/下一」按播放列表前后切换，到头**折返**；一条播完且开了
  「自动播下一条」时自动往后走。
- 历史记录是独立的一栏，**去重后置顶**（不在播放列表里也能看到播过什么），
  上限 300 条。
- 数据存在 `%APPDATA%\UniversalURLPlayer\playlist.json` / `history.json`。
  写入是**原子**的（先写 `.tmp` 再 `os.replace`），所以断电也不会留下半个文件；
  万一文件被外部改坏，程序会把坏文件**改名留档**成 `playlist.json.bad-<时间戳>`
  再重新开始，而不是静默清空。

### P4 下载到本地

- 「下载」按钮按地址类型自动选链路：**直链走 `requests` 流式**，**网页地址走
  yt-dlp**（带进度回调、取消、后处理合流）。
- 取消**不会删掉已下的部分**：保留 `.part` 文件，重试时自动带上 `Range` 续传
  （服务端返回 206/`Content-Range` 时接着写）。
- 进度、速度、剩余时间都在下载面板里；完成/失败都有明确提示。
  并发上限可调（默认 2）。
- 文件名规则：优先用标题（网页地址就是视频标题），直链在没有标题时取地址末段。
  注意**扩展名不会被拼两次**——`https://a.com/video.mp4` 存下来是 `video.mp4`
  而不是 `video.mp4.mp4`；重名时自动加 ` (2)`、` (3)`。
- 目标目录默认是系统「下载」文件夹（从注册表读，而不是硬写 `~/Downloads`），
  可在设置里改。
- 已知的取舍：`requests.iter_content(chunk_size)` 会**阻塞到凑满 chunk_size**
  才返回一次，所以 chunk 同时决定进度粒度与取消灵敏度——实测 256KB 时取消要等
  3 秒以上，最终定在 32KB。

### P5 剪贴板监听、设置面板、本地反向代理

- **剪贴板监听默认关闭**（后台读剪贴板是有侵入性的，必须用户明确开启）。
  开启后三层过滤防噪音：必须是能识别的播放地址、同一条内容不重复提示、
  超过 2048 字符或含换行的多行文本一律忽略。识别到可以只提示，也可以直接
  填进地址栏（不自动播放）。
- **设置面板** 5 个标签页：播放（含「恢复默认」）、网络（**VPN/代理**（含一键
  检测与连通性实测，见下节）/UA/Cookie/超时/RTSP 传输）、防盗链代理、下载、
  剪贴板。改完点「应用」即可热生效，少数需要重启的项会明确标出来。
- **本地反向代理**：`QMediaPlayer` 发不出 `Referer`，很多站点靠它防盗链（403）。
  打开后播放器从 `http://127.0.0.1:<port>/proxy?u=<目标>` 取数据，由代理补发
  `Referer`、`User-Agent`，也可带上 Cookie。
  只绑 `127.0.0.1`，绝不暴露到局域网。
  三个实现细节值得记：
  - `Range` 必须原样透传（否则拖不动进度条）；
  - 必须强制 `Accept-Encoding: identity`（压缩会让 `Content-Length` 与实际字节数
    对不上，播放器会判定流损坏）；
  - **请求头要按规范大小写发出去**（`Range` 不能写成 `range`，部分 CDN 会按字面比对）。
- `Referer` 的优先级是：**解析器从页面拿到的** > 设置里手填的 > 由目标地址
  自动推导的站点根地址。第一档很关键——B 站这类站点的 CDN 域名与页面域名不同，
  拿站点根当 Referer 等于发了个错的，防盗链照样过不去。
  同理，解析器给出的 `User-Agent` 也优先于通用设置。
- 代理开关打开后，**直接粘贴的直链**也会走代理，不需要地址自带请求头——
  这正是「我这条链播不了、想补个 Referer」的用法。早先的实现要求地址自带
  请求头才走代理，导致用户开了开关、填了 Referer 却毫无作用。
- 坦白一个局限：**HLS（`.m3u8`）不走代理**。清单里是相对路径，代理改写不了
  里面的分片地址，硬走只会把能播的地址搞坏，所以如实跳过。

### VPN / 代理（设置 → 网络）

**能用 VPN，而且多数情况什么都不用配**——关键在分清你用的是哪种模式：

- **TUN／全局模式**（Clash 的 TUN、多数系统级 VPN 客户端）：接管整机路由，
  程序的直连本来就在隧道里，设置里**留空即可**；
- **系统代理模式**（Clash Verge 的「系统代理」开关）：只改注册表，程序会
  **自动识别**（优先级：设置里填的 > 环境变量 > Windows 系统代理）。播放
  也会自动改走本地反向代理转发——播放内核（Qt 的 FFmpeg 后端）**不认任何
  代理配置**，不转发就必然"解析成功、播放却连不上"。

只有「既没开系统代理、又没开 TUN」才需要手填（Clash 的「混合端口」就是
HTTP 端口，通常是 `http://127.0.0.1:7897`）。

设置里那一栏不只是个输入框：

- **检测系统代理**：一键把环境变量／注册表里的代理读出来填上；什么都读不到
  往往说明是 TUN 模式，界面会直说"这种模式不用填"；
- **当前生效**：打字的同时实时显示三个来源里正在生效的是哪一条；
- **测试连通性**：对同一条地址**直连一次、经代理一次**，用对照结果给结论。
  "代理可用""走代理反而不通（去查端口）""本机有网但代理通路不通（换节点）"
  分得清；502/503/504 会单独指出是代理节点的问题，免得用户瞎改端口。

命令行同款：`python tools/vpn_check.py`（`--target` 换目标、`--proxy` 临时指定、
退出码可用于脚本）。`main.py --self-check` 也会报告三个来源与当前生效的那条。
体检的实现是只读的（`src/utils/proxy_check.py`）：不发写请求、不改配置，
直连探测会显式压住环境变量里的代理，保证测到的确实是"直连"。

### P6 可选 libmpv 内核

1. 装 Python 包：`pip install python-mpv`
2. 放一份 `mpv-2.dll`：放在程序目录、或用环境变量 `UNIVERSAL_PLAYER_MPV_DLL`
   指定、或在设置里填路径
3. 在设置里把内核切到 libmpv（也可只勾「直播流优先用 mpv」）

libmpv 后端用的是 `wid`（原生窗口句柄）渲染，`http-header-fields`、
`audio-file`、`rtsp-transport` 都是**加载期**选项，必须赶在 `loadfile` 之前设好；
所以 `set_source()` 只记录，真正的加载推迟到 `play()` 前的 `_ensure_loaded()`——
控制器的调用顺序是 `set_source → set_audio_source → play()`，信息到中间那步才齐。
它**比 Qt 多一项能力**：可以在 `loadfile` 时就带上请求头（`http-header-fields`），
所以用 mpv 内核时通常不需要本地代理。
不支持、或加载 DLL 失败时，后端工厂**静默回退 Qt**，界面不会弹错——
只是「内核」那一项会显示成 Qt。
