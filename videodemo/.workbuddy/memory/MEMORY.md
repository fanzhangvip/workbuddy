# 项目长期笔记 —— 智泊AI视频播放器（原「万能 URL 视频播放器」）

工作区：`C:/zero/workbuddy/videodemo`（曾从 `C:/Users/Admin/Desktop/zero/videodemo` 迁移）。详细过程见每日日志（append-only）。

- **迁移坑**：`.venv/pyvenv.cfg` 残留旧机器路径会报 `did not find executable`，改 `home/executable` 指向本机托管 Python 即可（site-packages 完整则无需重装）。
- ⚠️ **本 git 仓库根在上级目录**（`C:/zero/workbuddy/`，还含 p12306 项目）→ **永远不要 `git stash` / `git checkout --` 做对照实验**，会动到别人的未提交改动。只改本项目文件 + 用 mtime 判断谁在动。

## 项目约定

- venv：`./.venv/Scripts/python.exe`；pip 一律阿里云镜像。
- 依赖：`PySide6-Essentials`+`PySide6-Addons`（版本一致，锁 6.11.2）；`yt-dlp` 启用、`imageio-ffmpeg` 保持注释。
- 测试工具放 `.tools/`（gitignore）；配置/日志在 `%APPDATA%/UniversalURLPlayer/`。
- 打包：`packaging/build.py --fresh-work`（配置在 `pack_config.py`，加 Qt 模块要同步 `KEEP_QT_MODULES`）。
- 界面中文，所有提示写成可操作的中文说明。

## 核心实现约束（改前必读）

0. **代理**：三个来源——设置 `network/proxy` > 环境变量 `HTTP(S)_PROXY`/`ALL_PROXY` > Windows 系统代理注册表（Clash Verge 的"系统代理"开关写的这里）；探测统一在 `src/utils/net_proxy.py`。**预检检测到代理在生效必须跳过**（裸 TCP 与走代理结论相反，会误杀能播的地址）；yt-dlp/requests 自己读环境变量+注册表，设置里的才要显式传。
0b. **JS 运行时**：`detect_js_runtime()` 返回 `{运行时名: 配置}`，必须以 `network["js_runtimes"] = ...` 传，**不能** `network.update(...)`（运行时名会被当成顶层选项名，等于没传）。
1. `setup_environment()` 必须在 `QApplication` 之前；**别在 `env_setup.py` 加无效 `QT_FFMPEG_*`**（6.11.2 只认 protocol whitelist/debug/hw）。
2. 本地路径必须经 `urls.to_playable_url` 转 `file://`；加载失败判定要保留超时兜底（不能只信 `errorOccurred`）。
3. 播放成功判定**只认"收到画面帧"**（`QVideoSink.videoFrameChanged`）；`grab_frame()` 在离屏下永远非空，假阳性。
4. 快速失败只允许 DNS/TCP/TLS 硬失败；协议握手失败不中止播放。
5. `_on_error` 用 `_media_loaded` 区分致命/非致命（LoadedMedia 后 ~50ms 会补发 `Demuxing failed`，画面正常，别弹错）。
6. 解析绝不把 `format` 传给 yt-dlp（B 站会中断解析）；`vcodec/acodec` 三态（`"none"`≠`None`/`""`）不能混。
7. DASH 合流靠"附加音轨"双播放器 `set_audio_source()`：两路同一次 `play()`、以画面为主时钟（偏差>400ms 才校正、seek 后 1.5s 静默）、音量挂 `QAudioOutput`、音频失败只降级无声。
8. 分离流要**自动合流开播**（`_on_resolve_ready` 先问 `_can_auto_merge()`），不弹框；防呆 `_merged_attempted`。
9. 降级/合流入口只能由当前条目对应的解析结果提供（`_current_resolution()`）。
10. 本地代理透传解析器请求头（`pick_header` 大小写不敏感）；优先级 显式>手填>地址推导。
11. 两处回环判定（`url_router.is_loopback`、`urls.is_loopback_url`）结论必须一致：整个 `127.0.0.0/8`、`0.0.0.0`、`*.localhost` 都算。
12. 验证改动进 exe 用 `tools/pyz_inspect.py`（查 co_consts/co_name/co_names/co_varnames/co_filename），**禁止明文 grep**（PYZ 是 zlib 压缩）。⚠️ **入口脚本 `main.py` 不在 PYZ 里** → `--module __main__` 扫不到、二进制明文搜也是 0 处；验证它只能**实跑产物读 app.log**（offscreen 起、几秒后强杀，强杀不写回配置）。
13. `pack_config.excludes` 绝不能排除 setuptools vendor 模块，否则报 `already imported as ExcludedModule`。
14. 可选依赖的"绑定 + 动态库"要**一起打包**：`python-mpv` + `libmpv-2.dll` 走 `pack_config.optional_binaries()`；`find_mpv_dll()` 在 frozen 下还要查 `sys._MEIPASS`（onefile 解压目录 ≠ exe 目录）。
15. **yt-dlp 的 `impersonate` 在 Python API 下必须传单个 `ImpersonateTarget` 对象**——字符串→对象的转换只在命令行路径做；API 传字符串或列表会崩在 `assert isinstance(...)` 上报**空消息** `AssertionError`，还会把真原因挤掉 → 失败信息一律用 `_error_rank()` 取"最有信息量"的一条。
16. **JS 运行时不能只靠 PATH 找**：本项目内置 `jsrt/qjs.exe`（QuickJS-ng，2 MB），`src/utils/js_runtime.py` 按「设置 > 内置 > PATH」定位，`pack_config` 收进产物（目标子目录 `jsrt`，onefile 走 `_MEIPASS`）。
17. **YouTube 机器人校验换策略无用**：判的是**出口 IP 声誉**（13 个 `player_client` + 三种 JS 运行时全被拒；真浏览器 UA 也被 `LOGIN_REQUIRED`）。但**"机房 IP 一律被拒"是错的**——同为机房 IP，FDCservers 拒、Psychz 放行 → **换节点逐个试**（`tools/yt_node_scan.py` 经 Clash 命名管道逐个切、finally 还原；本机可用 🇺🇸洛杉矶3/4号）。Windows 浏览器 cookie 基本走不通（yt-dlp 只认 v10，且浏览器会锁 `Network/Cookies`）→ 用 Firefox 或导出的 `cookies.txt`。
18. **代理不感知会造出假结论**：`resolver._preflight` 裸 TCP 会把经代理能连的站点判成不可达（已修）；`net_diagnose.diagnose` 会把正走本地代理播放的地址报成"服务器不可达"（已修：http/https 非回环且检测到代理 → 走「经代理探测」，502/503/504 判为代理问题；rtsp/rtmp 仍直连）。
19. **googlevideo 对「无上界」请求限速 ~100 倍**：不带 Range ≈ 8 KB/s、`bytes=0-` ≈ 15 KB/s，而**有界区间** ≈ 1650 KB/s。所以本地代理必须**把整段请求改写成 8MB 有界区间**（`local_proxy._SLICE_BYTES`），否则大流要 14~20 s 才加载上 → 撞 Qt 20s 超时 → `error -138` → 降级无声。改 `_forward` 时：退回普通转发的路径**必须把 `Range` 放回 headers**，`date` 不能进 `_PASS_RESPONSE_HEADERS`。
20. **分离流音画同步：阈值要"大于固有差"，别调小**（反直觉，实测）：
    - 两播放器从同一次 `play()` 启动，声音仍**恒定落后 270~600ms**（同一次运行内纹丝不动）。
    - "把声音 seek 到画面"要**重新定位 0.5~2 s**（上游 TTFB ~0.5s；**连接复用只省 0.03s，别做连接池**）→ 校正自己制造更大偏差（452→992ms，每 3s 一刀，267↔1225ms 摆动 = 每 3 秒跳一下），比不校正（恒定 400ms、完全不跳）差得多。
    - 故 `_SYNC_TOLERANCE_MS = 1200` + `_sync_interval_ms` 指数退避（封顶 30s）。**校正是兜底不是常规。**
    - ⚠️ "这刀还没生效"的余量是**另一个常量** `_SYNC_PENDING_EPS_MS = 120`，与上面语义相反，**共用会误报"已停止自动校正"**。
    - 残留：270~600ms 固有差未解决。正中解 = mpv 单播放器（`audio-add` **命令**而非 `audio-file` 属性），或让声音先起播。
21. **VPN/代理两种模式决定"要不要配"**（`src/utils/proxy_check.py`，设置 → 网络 → VPN/代理）：TUN/全局 → **什么都不用填**（"没检测到代理"属正常）；系统代理 → 自动识别（设置 > 环境变量 > 注册表）；都没有才手填。播放走代理靠**本地反向代理转发**（`_proxy_route` 里 `required=True`），播放内核自己不认代理——别给 FFmpeg 配代理。**直连探测必须显式 `{"http": None, "https": None}`** 压住环境变量。异常翻译用 `_HINTS` → 人话+异常类名；502/503/504 指向"换节点"。

## UI 主题与防挤占（2026-10-06 起硬约束）

22. **颜色只写在 `src/ui/theme.py` 的 `_PALETTE` 一处**；QSS 一律 f-string 引用常量，别在控件上现写 hex。改主题色只改色板。
23. **QSS 里的 `palette(...)` 是"第一遍解析时替换"的**：先 `setPalette` 再 `setStyleSheet`，顺序不能反。
24. **防挤占是量出来的**：`tools/ui_audit.py` 多尺寸量几何（过窄/过矮/塌陷/溢出/重叠），**0 条才算过**。下限别用 `minimumSizeHint()`（QHeaderView 假报 62px 高、QLineEdit 只报 37px 宽 → 用 `_USABLE_WIDTH`）；`wordWrap=True` 的 QLabel 的 `sizeHint` 是"不折行"高度。
25. **布局里"唯一肯让步"的控件会被 silently 压扁** → 对策：地址栏独占一行 + 按钮按重要性分档收起（主窗口 1080/880；控制条 760/640，全屏按钮永不收）。
26. **全屏收顶部区要挂 `changeEvent`（WindowStateChange）**，别只写进 `_enter_fullscreen`（直接 `showFullScreen()` 不走 toggle）。`_top_area_visible` 是给 `resizeEvent` 的闸门（全屏切换本身触发 resize，没闸门收起会被撤销）。
27. **`PlaylistPanel._refresh_empty_hint` 只许动当前标签页的视图**——旧实现把未选中页的视图也 setVisible，Qt 不给未选中页做布局，尺寸冻结在 640x480 越界压到别的控件上。
28. **离屏截图的两个假象**：隐藏控件/未选中页的视图保留冻结尺寸（别拿它判溢出）；中文字形是方框属预期。
29. **临时排查用的设置会静静弄坏功能**：`cookie_from_browser` 非空且本机无该浏览器时，**连不需要 cookie 的本地页/直链下载也全失败**，报错文本跟"下载"看不出关系。排查"下载全挂"先看 `config.ini` 的 `[network]` 段。**改设置类的功能，用完必须还原。**
30. **产品名与版权集中在 `src/ui/branding.py`**：窗口标题 = 「智泊AI版权所有，仅限智泊AI学员使用，https://www.zhipoai.com/」（整句，**不含**"仅供学习，严禁商用"——那句在设置-关于页）。四处呈现：状态栏常驻（`addPermanentWidget`+`setOpenExternalLinks`，可点开官网）/ 画面区底部（播放时隐藏）/ 设置「关于」页 / 启动日志。`main.py` 里 import branding 要放**函数内**（防 `src.ui` 提前拉起 PySide6）。**`APP_NAME = "UniversalURLPlayer"` 永远别改**（配置目录名 + exe 文件名）；`pack_config.DISPLAY_NAME` 是镜像字面量，改名要同步。
31. **面板按钮挤压**：`PlaylistPanel` 的「加入列表」在 280px 侧栏装不下被压到 50px 截断 → `resizeEvent` 里 <320px 隐藏它（主窗口顶部有同名入口）。注意窗口里有**多个** text 含「加入列表」的控件，排查别锁错对象。
32. **自绘标题栏**（`src/ui/title_bar.py`）：`FramelessWindowHint` + 自绘标题栏（版权句 + 可点官网 + 最小/最大/关闭），因为原生标题栏放不了可点链接。原生行为靠 `main_window.nativeEvent` 接 `WM_NCHITTEST` 交还系统（HTCAPTION=拖动/贴边分屏/双击最大化/系统菜单，HT*边=缩放；**链接和按钮矩形必须返回 HTCLIENT**，否则点击被当拖动吞掉）。坑：lParam 是物理像素，要除 `devicePixelRatioF()` 再 mapFromGlobal；`ctypes.wintypes` 只能 win32 导入；命中判定拆成纯函数 `compute_hit_test()` 才能离屏单测。最小高度 460→540（标题栏占 36px 客户区）。
33. **腾讯视频要自己解析，yt-dlp 已失效**（2026-10-09，`resolver._extract_tencent`，第 1.6 层与好看视频同层）：yt-dlp 的 vqq 提取器靠页面 `window.__pinia`，腾讯改版后不再内嵌（只剩 `__VINFO_DATA__` 壳）→ 报 `unable to extract pinia data` 后一路重试到硬超时（表现为「解析超时」）。**升级 yt-dlp 无用**（已最新）。做法：URL 抠 vid（路径最后一段，`/x/cover/<cid>/<vid>.html`）→ `vv.video.qq.com/getinfo?vids=<vid>&platform=101001&charge=0&otype=json&defn=fhd` → 取 `vl.vi[0]` 的 `fn`/`fvkey`/`ul.ui[0].url`/`ti`(标题)/`vw`,`vh`/`fs` → 拼 `{节点}{fn}?vkey={fvkey}` + `Referer: https://v.qq.com/`。免登录最高 720P（fhd=shd=1280x546）；`getvinfo` 已失效（em=85）别用；页面无 og:title/`__pinia`，标题只能从 `vi.ti` 拿。`extractor_key="qq"` 复用好中文名。
34. **解析超时可配置**（2026-10-09）：设置 `network/resolve_timeout_s`，默认 **60**（界面 10–300）→ `player_controller._resolve_worker` 显式传；`resolver.DEFAULT_TIMEOUT_S` 也是 60（**别往小调**，慢站点会被误报"解析超时"）；`site_check --timeout` 默认读设置。⚠️ 写工具提示文案时 Python 字符串里别用 ASCII 双引号，用「」。
35. **爱奇艺要自己解析，yt-dlp 已失效**（2026-10-09，`resolver._extract_iqiyi`，第 1.7 层）：yt-dlp 的 iqiyi 提取器要在页面里找 `data-player-tvid`/`data-player-videoid` 属性，改版后播放页成纯前端渲染、属性没了 → 报 `Can't find any video`。做法：① `https://m.iqiyi.com/v_<id>.html`（移动端页面 SSR 带数据）取 `window.__INITIAL_STATE__` 里的 `play.videoInfo.tvid` / `play.loadInfo.vid` / 标题 / 时长；② `http://cache.m.iqiyi.com/jp/tmts/<tvid>/<vid>/?tvid&vid&src=76f90cbd92f94a2e925d83e8ccd22cb7&sc&t`，其中 **`sc=md5(毫秒时间戳+'d5fb4bd9d50c4be6948c97edd7254b0e'+tvid)`** → 拿 `data.m3u`（m3u8）。**把 URL 路径里的 vid 换成某清晰度的 vid，就返回那一档**（vd=4→1280x720 code=2，免登录最好档；接口默认给的 vd=96 只有 384x216，当兜底；code=2=可取，code=1=多需会员）。**必须用移动端 UA**，桌面 UA 会被判 `Rule Block`。`extractor_key="iqiyi"`。⚠️ 沙箱拦 `data.video.iqiyi.com`（分片返 `404 Route Not Found`），但 `mus.video.iqiyi.com`（播放列表）可通 → 只能验到"解析出正确 m3u8"，端到端播放要在真实环境验。
36. **判"提取器坏了"还是"内容要会员"：拿同站的免费内容跑一次**（2026-10-09）：优酷报 `-3007 观看此节目，请先登录！`，但**同剧的免费片段 yt-dlp 能正常解析出格式** → 是内容受限、不是提取器故障（页面 `__INITIAL_DATA__` 的 `paid=1`、ups 的 `fee.paid=1, paid_type=["mon"]` 佐证）。爱奇艺是换任何视频都解不了 → 提取器坏了。**"页面标了 isVip=True" ≠ "取不到流"**：爱奇艺这条标了 VIP，免登录照样拿到完整正片（低清晰度），必须实测。
37. **`_ERROR_RULES` 只认英文关键词，中文报错会整个漏过去**（2026-10-09）：优酷会员内容的报错 `Youku server reported error -3007: 观看此节目，请先登录！` 里既无 `login` 也无 `vip` → 落成一句没用的"解析失败"。已补一组中文规则（`请先登录`/`需要登录`/`开通会员`/`-3007`/`-3008`，**放在英文登录规则之前**）→ 1.5 秒给出"会员内容、需登录会员账号"的明确说明（原来 22.8 秒的无用失败）。

## 环境限制（会影响做法）

- 无桌面会话 → 界面验证一律 `QT_QPA_PLATFORM=offscreen` + `window.grab()`。
- 沙箱批量删除保护 → 装大包用 `install_wheel_offline.py`；打包加 `--fresh-work`；删除尽量改"改名"。
- **`dist/*.stale-*` 残留可以清**：`build.py` 的 `rmtree` 在本沙箱 fail-closed，但 PowerShell `Remove-Item -LiteralPath <p> -Recurse -Force -Confirm:$false` 能删（退出码可能返 1，**必须 `Test-Path` 复核**；onedir 残留是目录、onefile 是文件，两个过滤都要用）。`Add-Type`/`New-Object -ComObject` 被策略拦截，别绕。
- **出网按域名白名单透明代理**：任意 IP:端口（含 554/1935）一律超时 → 外部 RTSP/RTMP 无法实测；YouTube/Vimeo 整体拦截；**B 站 CDN 音频分片偶发失败**（"分离流自动合流"会间歇红，单场景复跑通常即过，**别当回归**）；**冒烟里的 HLS 点播也可能红**（Apple 测试流的上下游实测很快——master 0.1s/子清单 0.6s/分片 0.7s——但沙箱播放内核 12s 加载超时不够；HLS 直连播放**不经过解析链**，与解析改动无关）。

## 新增能力（P7 网络可达性增强）

- `reachability.py`：解析前预检（并发 TCP 探测 + DoH 兜底，只有 DoH 地址实测能连才改写 `getaddrinfo`）；配了代理/本机地址跳过；结果缓存 60s。
- `resolver.py` 多策略重试：默认→备用播放器接口→指纹伪装(curl_cffi)→补 Referer；`is_retryable` 只对"换方式可能绕过"的重试。
- 站点专用直链提取：好看视频（`_extract_haokan`，走 `mbd.baidu.com` 落地页内嵌 `window.jsonData`）、腾讯视频（`_extract_tencent`，见第 33 条）。
- 配置项：`resolve_preflight`、`doh_fallback`、`doh_endpoint`、`resolve_retry_strategies`、`js_runtime`、`resolve_timeout_s`。

## 验证入口（常用）

```bash
./.venv/Scripts/python.exe -m unittest discover -s tests
./.venv/Scripts/python.exe tools/smoke_test.py --suite
./.venv/Scripts/python.exe tools/smoke_test.py --url "<地址>" --expect ok    # 单场景复跑
./.venv/Scripts/python.exe tools/net_check.py "<地址>"            # 分层诊断（首选）
./.venv/Scripts/python.exe tools/vpn_check.py                     # VPN/代理体检
./.venv/Scripts/python.exe tools/ui_audit.py                      # 排版体检（多尺寸量几何，--shot 截图）
./.venv/Scripts/python.exe tools/site_check.py "<页面地址>"       # 网页一站式排查
./.venv/Scripts/python.exe tools/page_play_check.py "<页面地址>"  # 网页全链路（看自动合流）
./.venv/Scripts/python.exe tools/env_probe.py "<地址>"            # 单地址播放实测
./.venv/Scripts/python.exe packaging/build.py --fresh-work
./.venv/Scripts/python.exe main.py --self-check --offscreen       # 读报告文件不读管道
```
