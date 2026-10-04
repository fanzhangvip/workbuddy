# 项目长期笔记 —— 万能 URL 视频播放器

工作区：`C:/zero/workbuddy/videodemo`（曾从 `C:/Users/Admin/Desktop/zero/videodemo` 迁移）。详细过程见每日日志（append-only）。

- **迁移坑**：`.venv/pyvenv.cfg` 可能残留旧机器路径，报 `did not find executable` 时先改 `home/executable` 指向本机托管 Python，site-packages 完整则无需重装。

## 项目约定

- venv：`./.venv/Scripts/python.exe`（托管 Python 3.13.12 创建）；pip 一律阿里云镜像。
- 依赖：`PySide6-Essentials`+`PySide6-Addons`（版本一致，锁 6.11.2）；`yt-dlp` 启用、`imageio-ffmpeg` 保持注释。
- 本地测试工具放 `.tools/`（gitignore）；配置/日志在 `%APPDATA%/UniversalURLPlayer/`。
- 打包：`packaging/build.py --fresh-work`（配置在 `pack_config.py`，加 Qt 模块要同步 `KEEP_QT_MODULES`）。
- 界面中文，所有提示写成可操作的中文说明。

## 核心实现约束（改前必读）

0. **代理**：能生效的代理有三个来源——设置 `network/proxy` > 环境变量 `HTTP(S)_PROXY`/`ALL_PROXY` > Windows 系统代理注册表（Clash Verge 等"系统代理"开关写的就是这里）；探测统一在 `src/utils/net_proxy.py`。**可达性预检检测到代理在生效必须跳过**（裸 TCP 与走代理的结论相反，会误杀能播的地址）；yt-dlp/requests 自己会读环境变量+注册表，不必手动传（但设置里的要显式传）。
0b. **JS 运行时**：`detect_js_runtime()` 返回 `{运行时名: 配置}`，必须以 `network["js_runtimes"] = ...` 传，**不能** `network.update(...)`（否则运行时名被当成顶层选项名，等于没传，YouTube 会静默退化甚至触发 bot 校验）。

1. `setup_environment()` 必须在 `QApplication` 之前调；**不要在 `env_setup.py` 加无效的 `QT_FFMPEG_*`**（6.11.2 只认 protocol whitelist/debug/hw 那几个，RTSP 只能走 UDP 无法配）。
2. 本地路径必须经 `urls.to_playable_url` 转 `file://`；加载失败判定必须保留超时兜底（不能只信 `errorOccurred`）。
3. 直播/播放成功判定**只认"收到画面帧"**（`QVideoSink.videoFrameChanged`），不能用 `grab_frame()`（离屏下控件 `grab()` 永远非空，假阳性）。
4. 快速失败只允许 DNS/TCP/TLS 硬性失败；协议握手失败不中止播放。
5. `_on_error` 用 `_media_loaded` 区分致命/非致命（LoadedMedia 后约 50ms 会补发 `Demuxing failed`，画面正常，别弹错）。
6. 解析绝不把 `format` 传给 yt-dlp（B 站会中断解析）；`vcodec/acodec` 三态（`"none"`≠`None`/`""`）不能混。
7. DASH 合流靠"附加音轨"双播放器：`set_audio_source()`；两路同一次 `play()` 启动、以画面为主时钟（偏差>400ms 才校正、seek 后 1.5s 静默）、音量挂 `QAudioOutput`、音频失败只降级无声。
8. 分离流要**自动合流开播**（`_on_resolve_ready` 先问 `_can_auto_merge()`），不弹框；防呆 `_merged_attempted`。
9. 降级/合流入口只能由当前条目对应的解析结果提供（`_current_resolution()`）。
10. 本地代理透传解析器请求头（`pick_header` 大小写不敏感）；优先级：显式指定>手填>地址推导。
11. 两处回环判定（`url_router.is_loopback` 与 `urls.is_loopback_url`）结论必须一致：整个 `127.0.0.0/8` 都算环回，`0.0.0.0`/`*.localhost` 也是。
12. 验证改动进 exe 用 `tools/pyz_inspect.py`（查 co_consts/co_name/co_names/co_varnames/co_filename 五处），**禁止明文 grep**（PYZ 是 zlib 压缩）。
13. `pack_config.excludes` 绝不能排除 setuptools vendor 模块（distutils/wheel/pkg_resources 等），否则报 `already imported as ExcludedModule`。
14. **本仓库可能被并行会话编辑**：动手前先看 mtime，别 revert 对方改动。
15. 可选依赖分"绑定"和"动态库"时要**一起打包**：`python-mpv`（绑定）+ `libmpv-2.dll`（库）走 `pack_config.optional_binaries()` 收进 spec；`find_mpv_dll()` 在 frozen 下还要查 `sys._MEIPASS`（onefile 解压目录 ≠ `app_root()` 的 exe 目录）。
16. **yt-dlp 的 `impersonate` 在 Python API 下必须传单个 `ImpersonateTarget` 对象**（`ImpersonateTarget.from_str("chrome")`）。字符串→对象的转换只在**命令行**路径做（`yt_dlp/__init__.py`）；API 传字符串或对象**列表**都会在 `YoutubeDL.__init__` 崩在 `assert isinstance(target, ImpersonateTarget)` 上，报一个**空消息** `AssertionError`。多策略场景下它还会把真正的失败原因挤掉——所以失败信息一律用 `_error_rank()` 取"最有信息量"的一条，不用最后一条。
17. **JS 运行时不能只靠 PATH 找**：用户机器上常常没有 deno/node（工具链自带的 node 不在持久 PATH，只有开发会话看得见）。本项目内置 `jsrt/qjs.exe`（QuickJS-ng，2 MB），`src/utils/js_runtime.py` 按「设置 > 内置 > PATH」定位，`pack_config` 收进产物（目标子目录 `jsrt`，onefile 走 `_MEIPASS`）。
18. **YouTube 的机器人校验换策略无用**（实测 13 个 `player_client` + 三种 JS 运行时全被拒；用真实浏览器 UA 请求观看页也返回 `LOGIN_REQUIRED`，即**浏览器同样会被拦**）。判的是**出口 IP 声誉**，排查别在客户端参数上打转。
    - ⚠️ **但"机房 IP 一律被拒"是错的**：实测同为机房 IP，FDCservers 的两个节点被拒、Psychz Networks 的两个节点放行。所以正确做法是**换节点逐个试**，不是去找"住宅 IP"。
    - 工具 `tools/yt_node_scan.py` 能自动扫：经 Clash Verge 的命名管道逐个切节点实测，**finally 还原原选择**。本机可用的是 🇺🇸美国洛杉矶3/4号。
    - 浏览器 cookie 在 Windows 上基本走不通：yt-dlp 只认 v10，没有 v20/app-bound 处理；且浏览器运行时会独占锁住 `Default/Network/Cookies`。只能用 Firefox 或导出的 `cookies.txt`。
19. **代理不感知会造出两种假结论**，改任何"探测/诊断"代码前先想一遍：
    - `resolver._preflight`：裸 TCP 把经代理明明能连的站点判成不可达（已修，`src/utils/net_proxy.py`）。
    - `net_diagnose.diagnose`：媒体地址已走本地反向代理、正在正常播放，裸 TCP 却把它报成"服务器不可达、地址可能已失效"（已修：`http/https` 非回环且检测到代理 → 走「经代理探测」，502/503/504 判为代理的问题；`rtsp/rtmp` 仍直连探测）。
20. **googlevideo 对「无上界」请求限速 ~100 倍**：`Range: bytes=0-` ≈ 15 KB/s、不带 Range ≈ 8 KB/s，而**有界区间** `bytes=0-1MB` ≈ 1650 KB/s。所以本地代理必须**把整段请求改写成 8MB 有界区间**再转发（`local_proxy._SLICE_BYTES`），否则大流（音频 129 MB）要 14~20 s 才加载得上，直接撞 Qt 的 20 s 超时 → `error -138` → 降级无声。改 `_forward` 时注意：退回普通转发的路径**必须把 `Range` 放回 headers**，且 `date` 不能进 `_PASS_RESPONSE_HEADERS`（会重复）。工具 `tools/upstream_rate.py` 量投递节奏。
21. **分离流的音画同步：阈值要"大于固有差"，别调小**（这条反直觉，实测得出）：
    - 两个播放器实例从同一次 `play()` 启动，声音仍**恒定落后 270~600ms**（起播管线延迟差；同一次运行内纹丝不动，关掉校正连测 30 s 都不增大）。单文件流不受影响。
    - 而"把声音 seek 到画面"要**重新定位 0.5~2 s**（代理每个上游请求 TTFB ~0.5s；**连接复用只省 0.03 s，已实测，别去做连接池**）。定位期间声音 position 停在原地 → 校正自己制造更大的偏差：实测 452ms → 992ms，然后每 3s 一刀、偏差在 267↔1225ms 摆动 = **每 3 秒跳一下**，比不校正（恒定 400ms、完全不跳）差得多。
    - 所以 `_SYNC_TOLERANCE_MS = 1200`（固有差两倍余量）+ `_sync_interval_ms` 指数退避（没压下去就 ×2、封顶 30s）。**校正是兜底，不是常规。**
    - ⚠️ 判定"这一刀还没生效"的余量是**另一个常量** `_SYNC_PENDING_EPS_MS = 120`。两者语义相反（一个大=允许不同步，一个小=还停在原地），**共用会让偏差 400ms 被误判成"seek 没生效"并误报"已停止自动校正"**（我踩过）。
    - 残留：那 270~600ms 固有差未解决。正中解 = mpv 单播放器（原生同步，用 `audio-add` **命令**而不是 `audio-file` 属性），或让声音先起播、画面等它出声再起播。
22. **VPN/代理的两种模式决定"要不要配"**（`src/utils/proxy_check.py`，设置 → 网络 → VPN/代理）：
    - TUN/全局模式：接管整机路由，**什么都不用填**（设置留空 + "没检测到代理"提示属正常）；系统代理模式：自动识别（设置 > 环境变量 > 注册表）。只有"都没有"才手填。
    - 播放走代理靠的是**本地反向代理转发**（`_proxy_route` 里 `required=True`），播放内核自己不认代理——别试图给 FFmpeg 配代理。
    - **直连探测必须显式 `{"http": None, "https": None}`** 压住环境变量，否则测到的不是直连。
    - 翻译网络异常用 `_HINTS` 关键词表 → 人话 + 异常类名；502/503/504 指向"换节点"不是"改端口"。

## 环境限制（会影响做法）

- 无桌面会话 → 界面验证一律 `QT_QPA_PLATFORM=offscreen` + `window.grab()`。
- 沙箱批量删除保护 → 装大包用 `install_wheel_offline.py`；打包加 `--fresh-work`；删除尽量改"改名"。
- **出网按域名白名单透明代理**：任意 IP:端口（含 554/1935）一律超时，外部 RTSP/RTMP 无法实测；YouTube/Vimeo 被整体拦截（DNS 污染+TCP 拦），沙箱内拿不到地址。
- 删除走回收站，几十 MB 单文件 exe 删不掉（fail-closed），留着别硬来；`build/` 会堆到 500MB 要定期清时间戳目录。

## 新增能力（P7 网络可达性增强）

- `reachability.py`：解析前预检（并发 TCP 探测 + DoH 兜底，只有 DoH 地址实测能连才改写 `getaddrinfo`）；配了代理/本机地址跳过；结果缓存 60s。
- `resolver.py` 多策略重试：默认→备用播放器接口→指纹伪装(curl_cffi)→补 Referer；`is_retryable` 只对"换方式可能绕过"的重试。
- 配置项：`network/resolve_preflight`、`doh_fallback`、`doh_endpoint`、`resolve_retry_strategies`、`js_runtime`（yt-dlp 2026 无 JS 运行时 YouTube 退化，本机用 node）。
- 工具 `tools/site_check.py`：预检→逐策略解析→能否拿地址。

## 验证入口（常用）

```bash
./.venv/Scripts/python.exe -m unittest discover -s tests
./.venv/Scripts/python.exe tools/smoke_test.py --suite
./.venv/Scripts/python.exe tools/net_check.py "<地址>"            # 分层诊断（首选）
./.venv/Scripts/python.exe tools/vpn_check.py                     # VPN/代理体检
./.venv/Scripts/python.exe tools/site_check.py "<页面地址>"       # 网页一站式排查
./.venv/Scripts/python.exe tools/page_play_check.py "<页面地址>"  # 网页全链路（看自动合流）
./.venv/Scripts/python.exe tools/env_probe.py "<地址>"            # 单地址播放实测
./.venv/Scripts/python.exe packaging/build.py --fresh-work
./.venv/Scripts/python.exe main.py --self-check --offscreen       # 读报告文件不读管道
```
