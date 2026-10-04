# pjialin/py12306 内核化 · 总体架构设计方案 v3

> 触发：需求方再次强调 —— 「**必须使用 `pjialin/py12306` 的源码；它有的功能必须用，禁止自己写。
> 它没有的、或我没有去使用它源码的地方，必须先和我沟通方案。**」
> 并指出「ddddocr / 云打码原来的实现是个假实现，根本没法用」。
>
> 本文是**动手前的全面梳理与架构设计**，等确认。核查时间 2026-09-29，
> 上游 `pjialin/py12306` master（`pushed_at` 2026-09-17），所有结论可复跑。

---

## 〇、一页结论

1. **可以按你的要求做的**：12 个功能域确实能用它的源码替换（§2 表 A），打码也在其中（§3）。
2. **必须你拍板的**：§3 打码只有三条路，**都绕不开「你选定一个打码服务」**——
   因为上游源码里**没有识别能力**，只有「把图发给谁」的渠道。
3. **它没有、只能自研的**：9 项（§2 表 C），其中 4 项是你真机验证过的资产。
4. **两处托管依赖**：都能**不用自建服务**解决（§6），各改 1 处源码。
5. **一个必须先说清的事实**：上游的**验证码下载/校验端点是被 WAF 挡掉的旧路径**（§3.3），
   照抄会打不通——这不是它的错，是 12306 变了。**这里需要你决定怎么办。**

---

## 一、上游事实（可复跑）

| 项 | 值 |
|---|---|
| 仓库 / 许可 | `pjialin/py12306` · **Apache-2.0** |
| 规模 | 44 个 `.py` / 5,020 行；`data/stations.txt`（112KB 站点表）+ `data/cdn.txt`（42KB / 400 IP） |
| 形态 | **配置驱动的独立应用**（无 `setup.py`），入口 `main.py` + `env.py` |
| 活跃度真相 | `pushed_at` 2026-09-17，但**核心逻辑停在 2019–2021**；2026-06 的提交只有「replace bare except」 |
| Python | 无 `python_requires`；依赖实测在 **3.13.14 全部可装**（`requests-html` / `pyppeteer` / `websockets 10.0` …） |

**逐文件最后实质改动**（GitHub commits API）：

| 文件 | 上次改动 | 说明 |
|---|---|---|
| `helpers/api.py`（接口表） | **2021-09-24** | 「添加扫码登录功能」 |
| `helpers/notification.py` | **2021-09-29** | 「增加二维码发送到邮箱」 |
| `helpers/OCR.py` / `auth_code.py` | 2021 之前 | 打码体系 |
| `order/order.py` / `query/query.py` / `user/job.py` | 2026-06-02 | **仅** `bare except` 语法修复 |

---

## 二、全量模块对照（**逐模块，不含糊**）

### 表 A —— 上游**有**，按你的要求**必须用它的源码**

| # | 功能域 | 上游位置 | 本项目现状 | 替换方式 |
|---|---|---|---|---|
| A1 | 接口/路径表 | `helpers/api.py`（60 行） | `inter/urls.py` | 直接 import 它的常量 |
| A2 | **扫码登录** | `user/job.py::qr_login` + `download_code` + `request_device_id` | `account/qrlogin.py` | 换它的（**这是唯一可靠的登录路径**） |
| A3 | 密码登录 | `user/job.py::login` / `login2` / `auth_uamtk` / `auth_uamauthclient` | `inter/api.py` | 换它的（但依赖被 WAF 挡的端点，见 §3.3） |
| A4 | 余票查询 | `query/query.py` + `query/job.py` | `query/engine.py` | 换它的 |
| A5 | **余票页预热** | `query/query.py:211` 调 `API_QUERY_INIT_PAGE` | `ensure_session_warm` | 换它的（**印证我们那条真机硬约束**） |
| A6 | 余票解析 | `query/query.py::handle_response` / `query/job.py::handle_seats` 等 20+ `get_info_of_*` | `inter/parser.py` | 换它的 |
| A7 | 站点表 | `helpers/station.py` + `data/stations.txt` | `inter/station.py` | 换它的（含数据文件） |
| A8 | 下单链路 | `order/order.py`（613 行，六步齐） | `inter/api.py` | 换它的（风险最高，最后做） |
| A9 | 乘车人 | `API_USER_PASSENGERS`（`getPassengerDTOs`） | `inter/api.py` | 换它的 |
| A10 | 用户信息 | `API_USER_INFO` | `account/profile.py` | 换它的 |
| A11 | 通知渠道 | `helpers/notification.py`（邮件 / Telegram / 钉钉 / Server酱 / PushBear / 语音 2 家） | `notify/channels.py` | 换它的（它有 PushBear，我们没有） |
| A12 | **打码** | `helpers/OCR.py` + `helpers/auth_code.py` + `vender/ruokuai/main.py` | `verify/service.py`（**你指出的假实现**） | **见 §3 专项** |
| A13 | 滑块风控 | `order/order.py::request_init_slide`（pyppeteer 真拖拽 `#slide_passcode`） | 无（走人工兜底） | 换它的（你已同意 pyppeteer） |
| A14 | CDN 优选 | `helpers/cdn.py` + `data/cdn.txt`（400 IP） | **无** | 新增（上游独有） |
| A15 | 心跳保活 | `user/job.py` | `account/manager.py` | 换它的 |
| A16 | 分布式集群 | `cluster/`（Redis 锁、多节点） | **无** | 新增（上游独有） |
| A17 | 限速（部分） | `query/job.py::update_interval` + `helpers/request.py` timeout | `ratelimit/limiter.py` | 用它的节奏参数，自适应那套保留（它没有） |

### 表 B —— 上游**有**，但**直接照抄会出问题** → 需你决定（§3、§6）

| # | 项 | 问题 |
|---|---|---|
| B1 | 打码渠道 `free` | 打 `https://12306-ocr.pjialin.com/check/` —— **你已禁止托管域** |
| B2 | 设备指纹 | 打 `https://12306-rail-id-v2.pjialin.com/` —— **你已禁止**；但上游**自己有**不用托管的路径（§6.1） |
| B3 | 验证码下载/校验端点 | 上游用 `/passport/captcha/captcha-image64` + `/passport/captcha/captcha-check`；**我们真机验证过旧路径已被 WAF 挡** |
| B4 | 打码渠道 `ruokuai` | 若快是**第三方商业平台**，需账密；源码里硬编码的 `soft_id='119671'` / `soft_key='6839cb…'` 是**上游作者的凭据** |
| B5 | 打码渠道 `user` | 上游支持「自填 URL」，但**服务由谁提供**没解决 |
| B6 | Web UI | 上游 `web/`（handler 风格）与我们的 `web/` + `desktop_ui/` **完全不同** —— 我们的 UI 是既有成果，**建议不换** → 请确认 |
| B7 | 日志 | 上游 `log/`（7 模块）与我们的 `logger/` 能力重叠，替换收益低、风险高 → 请确认是否必须 |

### 表 C —— 上游**没有**，只能自研（**请确认保留**）

| # | 缺什么 | 证据 | 我们的实现 | 换成上游的后果 |
|---|---|---|---|---|
| C1 | **候补整族** | `afterNate`/`confirmHB` **0 处** | `standby/engine.py` | 已按决策 B 用 `testerSunshine/12306` |
| C2 | 我的订单 / 未完成订单 | `queryMyOrder` **0 处** | `order/service.py` | 无法确认出票、无法认领候补单 |
| C3 | `checkUser` 登录态检测 | **0 处** | 会话恢复必调它验活 | 恢复的会话是否有效无从判断 |
| C4 | 人证核验 | `chechFace`/`face_flag`/`人证` **0 处** | `account/verify.py` 四态 | 候补前无法判定、无法给出路 |
| C5 | WAF / HTML 错误页识别 | **0 处** | `_reject_html` + `RiskControlError` | 「回 HTML 而不是 JSON」会被当业务响应 |
| C6 | 会话持久化到磁盘 | **0 处**（只有 Redis 内存态） | `account/session.py` | 每次重启都要重新登录 |
| C7 | 限速自适应 / 退避 | **0 处** | `ratelimit/limiter.py` | 被限流后无法自恢复 |
| C8 | 14 天预售期判断 | 只有一句「超出一个月预售期」文案 | `train_date.py`（真机实测 14 天） | 「日期不可售」被伪装成「被限流」 |
| C9 | 本地结构化存储 | **0 处**（`data/*.json` + Redis） | `storage/` | 任务/订单/记录无处落盘 |

### 表 D —— 非上游、但也不是我们自研的（基础设施）

`requests` / `Flask` / `PySide6` / `cryptography` / `onnxruntime+ddddocr`（§3 要处理的就是它）。

---

## 三、打码专项（**你指出的假实现**）

### 3.1 先承认事实

你说得对。我们已经核实过，本项目打码这块**确实是坏的**：

| 问题 | 证据（2026-09-29 抓真实请求体） |
|---|---|
| `CloudSolver`（超级鹰） | 发出的表单是 `user=&pass2=&softid=0&codetype=9004` —— **账号是空串、配置的 key 完全没进请求、图片根本没上传**（超级鹰要 multipart 的 `file`，而 `HttpClient` 没有文件上传能力）→ **必然失败** |
| `CloudSolver`（云码） | 图片传了，但 `username` 为空 + 缺题目类型 `type` → **必然认证失败** |
| `available()` 判据 | 只看「有没有 api_key」，不看配置是否完整 → 填了 key 就会去调、必然失败 |
| 失败被吞 | `log.warning` + 返回 None → 使用者只看到「识别失败」，**永远不知道请求压根没发出去** |
| `DdddocrSolver` | ddddocr 的通用检测模型对**点击型**验证码不适用 |

**⇒ 结论：这块整体换成上游的，是正确的决定。**

### 3.2 上游打码体系的真实构成（**关键认知**）

上游源码里**没有识别能力**。它只有三样东西：

```
helpers/OCR.py (76 行)
├── get_img_position(img)                    ← 按渠道分流
│   ├── get_image_by_free_site(img)          ← POST {img: b64} → {msg, result:[1..8]}
│   └── get_img_position_by_ruokuai(img)     ← 调 vender/ruokuai（若快，第三方）
└── get_image_position_by_offset(offsets)    ← 【本地】格子编号 → x,y
                                                4 列网格 / 75px / ±5 抖动
helpers/auth_code.py (85 行)
├── download_code()   → /passport/captcha/captcha-image64   ← B3：旧路径，WAF 挡
├── get_auth_code()   → 下载 → OCR → ','.join(position)
└── check_code()      → /passport/captcha/captcha-check     ← B3：同族旧路径
vender/ruokuai/main.py (54 行)  → RKClient.rk_create(image, im_type=6113)
config:  AUTO_CODE_PLATFORM ∈ {free, ruokuai, user}
         API_USER_CODE_QCR_API（user 模式自填 URL）
         AUTO_CODE_ACCOUNT = {user, pwd}
```

**两个要点：**

1. **识别能力来自外部服务，不来自源码。**
   `free` 打它的托管域（你已禁）；`ruokuai` 打若快（第三方商业平台，需账密）；
   `user` 让你**自己填 URL**（协议 = `POST {img} → {msg:'success', result:[格子编号]}`）。
2. **三个渠道返回的都是「格子编号 1..8」**，再由上游本地换算成 x,y。
   而 ddddocr 的点击检测**直接输出坐标** —— 接口不同。
   ⇒ 换上游 = **放弃 ddddocr 的坐标输出，改拿格子编号**。这恰好绕开了你说的「假实现」。

### 3.3 一个必须先说清的事实：它的验证码端点已经失效

| 用途 | 上游用的 | 真机现状 |
|---|---|---|
| 下载验证码 | `/passport/captcha/captcha-image64` | **被 WAF 挡**（任何头组合都回 HTML 错误页） |
| 校验验证码 | `/passport/captcha/captcha-check` | 同族旧路径 |
| 我们现在用的 | `/otn/passcodeNew/getPassCodeNew` + `/otn/passcodeNew/checkRandCodeAnsyn` | 真机可用 |

**照抄上游 → 登录验证码链路打不通。** 这不是上游的错，是 12306 变了。

**另一件事**：**登录验证码我们本来就用不上** —— 真机上密码登录走不通，扫码是唯一可靠路径。
真正需要打码的是**下单时的验证码**；而 pjialin 对下单验证码的答案是
**`randCode` 传空串 + pyppeteer 拖滑块过风控**（`order/order.py`）。

### 3.4 打码的三条路（**请你选**）

| 选项 | 内容 | 代价 | 是否满足「用上游源码」 |
|---|---|---|---|
| **P-A** | 用上游三渠道（**去掉 `free`**），识别走 `ruokuai` 或 `user`；本地换算用上游 `get_image_position_by_offset` | **需要你去若快注册拿账密**（源码里那对 soft_id/soft_key 是上游作者的，且可能要申请自己的）；或你指定一个符合该协议的打码服务给我填 URL | ✅ 完全满足 |
| **P-B** | 用上游**框架 + 换算**，但识别接「我们自选的云打码」，我按上游协议补一个适配 | 需要**你指定哪家**并给接口文档 | ⚠️ 框架/换算用上游，适配层是我们写的 |
| **P-C** | 保留 ddddocr，只把上游换算接上 | 无 | ❌ 违反你的要求 |

**我的建议：P-A + 若快（`ruokuai`）**，因为它是上游**已经写好并接好**的渠道，改动最小、
最符合你「禁止自己写」的要求。但**需要你提供若快账号**（或确认用上游那对硬编码凭据）。

**另外请一并决定（B3）**：是否允许把「验证码下载/校验**端点**」
换成我们真机验证过的 `passcodeNew/getPassCodeNew` + `checkRandCodeAnsyn`？
（**算法、渠道、换算仍然全部用上游**，只换两条 URL —— 因为上游那两条已失效。）

---

## 四、架构设计

### 4.1 分层与依赖方向

```
┌───────────────────────────────────────────────────────────────┐
│ 表现层    desktop_ui/            web/static/*                  │
├───────────────────────────────────────────────────────────────┤
│ 应用层    runtime/context.py      scheduler/                    │  组装 · 生命周期
├───────────────────────────────────────────────────────────────┤
│ 业务层    grab/ standby/ order/ query/ account/ notify/         │  不认识 12306 协议
├───────────────────────────────────────────────────────────────┤
│ 协议门面  inter/api.py  ← 业务层唯一入口                        │
├───────────────────────────────────────────────────────────────┤
│ 内核门面  kernel/registry.py  ← **唯一的切换点**                │  按功能域解析
│           kernel/domains/*.py  ← 每个功能域一个 Protocol         │
├───────────────────────────────────────────────────────────────┤
│ 后端实现  kernel/self/*     本项目实现（回退）                   │
│           kernel/pjialin/*  适配 vendor/pjialin（**默认**）      │
│           kernel/testerSunshine/*  候补（决策 B，已落地）        │
├───────────────────────────────────────────────────────────────┤
│ vendor    vendor/pjialin/py12306/**   ← 上游源码（逐字）         │
│           vendor/testerSunshine/**    ← 候补（决策 B，已落地）    │
└───────────────────────────────────────────────────────────────┘
```

**依赖铁律（全部可机器检查，现有 C1/C2 已落地）**

| # | 规则 | 防的是什么 |
|---|---|---|
| R1 | `kernel/` 不得 import `inter/` | 门面与实现绑死 → 切换失效 |
| R2 | 业务层不得 import `kernel/`、`vendor/` | 灰度泄漏进业务语义 |
| R3 | 切换点**只有** `kernel/registry.py` | 「我切了」无法查证 |
| R4 | 业务代码里不许出现 `if backend == "pjialin"` | 同上 |
| R5 | `vendor/**` 逐字保留上游内容；**任何改动记入 `PROVENANCE.json`** | 「用了上游」变成声明 |
| R6 | 每个功能域开关**独立**，可逐个切、可一键回退 | 一处坏掉拖垮全部 |

### 4.2 目录与文件清单

```
ticket_helper/kernel/
├── __init__.py                 # 兼容入口（候补开关，已落地）
├── base.py                     # 候补 Protocol（已落地）
├── registry.py                 # 【新增】功能域 → 后端 映射 + 开关解析
├── domains/                    # 【新增】每个功能域一个 Protocol（只定接口，不实现）
│   ├── login.py                #   qr_login / password_login / check_user / device_id
│   ├── query.py                #   query_left_ticket / warm_session / station_map
│   ├── order.py                #   submit / check / queue / confirm / slide
│   ├── passenger.py            #   list_passengers / user_profile
│   ├── notify.py               #   send
│   ├── captcha.py              #   solve（见 §3）
│   └── standby.py              #   候补（已落地）
├── self/                       # 本项目后端（回退）
├── pjialin/                    # 【新增】上游适配（**不含协议逻辑，只搬运**）
└── testerSunshine/             # 候补（已落地）

vendor/
├── testerSunshine/             # ✅ 已落地（standby.py + PROVENANCE + LICENSE）
└── pjialin/                    # 【新增】
    ├── py12306/**              #   上游整包（逐字）
    ├── data/                   #   stations.txt / cdn.txt（数据文件也要搬）
    ├── LICENSE                 #   Apache-2.0 全文
    ├── NOTICE                  #   Copyright the py12306 contributors
    ├── PROVENANCE.json         #   逐文件 sha256 + 改动清单
    └── README.md               #   改动说明（Apache-2.0 §4(b) 要求）
```

### 4.3 上游是「独立应用」而不是「库」：适配层怎么隔离

`py12306` 是配置驱动的应用，import 就会拉起全局单例（`Config()`、`Cluster()`、`App`）。
**不能直接 import 到我们的进程里不管**，否则：

- `Config()` 会去读它的 `env.py`，与我们的 `env.py` 冲突；
- `Cluster()` 会尝试连 Redis；
- `App.run_check()` 会启动它自己的线程池。

**隔离方案**（三种，请确认选哪个）：

| 方案 | 做法 | 代价 |
|---|---|---|
| **I-1 进程内 + 垫片**（建议） | 在 `vendor/pjialin/shim/` 提供 `Config` 垫片，把它的配置**从我们的配置对象投影过去**，禁止它启动自己的集群/线程；适配层只调它的**函数与类方法** | 需要逐个确认它哪些模块 import 时会起副作用 |
| **I-2 子进程 + IPC** | 上游跑在独立进程，我们用 JSON/IPC 调它 | 部署复杂、调试难、打包更重 |
| **I-3 只搬纯函数/纯类** | 只 vendor 无副作用的模块（如 `helpers/api.py`、`OCR.py`、`cdn.py`、`station.py`），有副作用的应用骨架不搬 | 最安全，但「用它的源码」覆盖度下降 |

### 4.4 数据流（以「查一次余票」为例）

```
query/engine.py
  └─ kernel.registry.get("query")           ← 唯一切换点
       ├─ self      → inter/api.py::query_left_ticket
       └─ pjialin   → vendor/py12306/query/query.py::Query.query_left_ticket
                      （它内部会调 request_device_id → leftTicket/init 预热 → query）
  └─ 结果统一转成我们内部的 TrainTicket 类型（适配层负责，业务层不知道来源）
```

**类型转换放适配层**：业务层只认我们的类型，避免上游类型泄漏到 grab / standby / UI。

---

## 五、分期与验收

| 期 | 内容 | 风险 | 验收（外部信号） |
|---|---|---|---|
| **P0** | vendor `pjialin/py12306` 整包 + Apache-2.0 合规 + `PROVENANCE.json` + `--check` 校验脚本；`registry.py` 骨架 | 无（不动行为） | `--check` 退出码 0；vendor 与原件 sha256 一致 |
| **P1** | 无状态域：站点表（A7）、CDN（A14）、通知（A11）、**打码（A12，按 §3 选定方案）** | 低 | 双后端差分表 + 契约测试（撤掉实现变红） |
| **P2** | 只读域：余票查询（A4/A5/A6）、乘车人（A9）、用户信息（A10）、扫码登录（A2） | 中 | 同上 + mock 端到端 |
| **P3** | 写域：下单链路（A8）+ pyppeteer 滑块（A13） | **高**（涉资金） | 同上 + 真机小流量 |
| **P4** | 参数/节奏域：限速（A17）、心跳（A15） | 中 | 同上 |
| **不换** | 表 C 的 C1–C9（上游 0 处） | — | 需你确认 |

**每期都必须有的三件套**（照 P0-a 已落地的那套）：
① 双后端**差分表**可打印可断言；② 契约测试（撤掉实现必须变红）；③ **一键回退**开关。

---

## 六、两处托管依赖的解法（**都不用自建服务**）

### 6.1 设备指纹（B2）

上游自己就留了「不用托管」的路径：

```python
# helpers/api.py:53 —— 官方地址被注释掉，换成了自家托管服务
# API_GET_BROWSER_DEVICE_ID = BASE_URL_OF_12306 + '/otn/HttpZF/logdevice'
API_GET_BROWSER_DEVICE_ID = 'https://12306-rail-id-v2.pjialin.com/'

# query.py:130 / user/job.py:331
if 'pjialin' not in API_GET_BROWSER_DEVICE_ID:
    return self.request_device_id2()        # ← 上游已实现的自建路径
```

⇒ **把 URL 换回官方地址即可**走上 `request_device_id2()`。
⚠️ 注意：`query.py` / `user/job.py` 用的是 `from ... import API_GET_BROWSER_DEVICE_ID`
（导入的是**值**不是模块），所以运行期改 `helpers.api` 的属性**无效** ——
必须改源码（**3 个文件**），并记入 `PROVENANCE.json`。

### 6.2 打码的 `free` 渠道（B1）

直接**删掉这个分支**（或在适配层禁用），改用 `ruokuai` / `user`（§3.4）。

---

## 七、**自我披露**：我「没有去使用它源码」的地方（请逐条核对）

你要我沟通「你没有去使用它源码的地方」。下面是我能想到的**全部**，分三类：

### 7.1 因为上游没有（表 C，C1–C9）—— 建议保留自研，**请确认**

候补（已决策 B 解决）、我的订单、`checkUser`、人证核验、WAF/HTML 识别、
会话持久化、限速自适应、14 天预售期、本地存储。

### 7.2 因为上游有但**照抄会坏**（表 B，B1–B7）—— 需你决定

`free` 打码渠道、设备指纹托管域、**验证码端点已失效**、`ruokuai` 凭据、
`user` 渠道的服务来源、Web UI 是否换、日志是否换。

### 7.3 因为**架构必要**而必须由我们写的一层（**请确认这是允许的**）

| 项 | 为什么必须是我们写 |
|---|---|
| **适配层**（`kernel/pjialin/*`） | 上游类型 / 全局单例 / 配置形态与我们不同，必须有翻译层。**这层不含协议逻辑**，只做类型与生命周期转换 |
| **`kernel/registry.py` 开关** | 灰度与回退本身就是你的要求（可切、可退） |
| **类型转换**（上游结果 → 我们的 `TrainTicket` / `Passenger` 等） | 否则上游类型会泄漏到业务层与 UI |
| **改动清单 / `PROVENANCE.json`** | Apache-2.0 §4(b) 合规要求 |

---

## 八、需要你决策的清单（**逐条答复即可，我按你的答复开工**）

| # | 问题 | 我的建议 |
|---|---|---|
| **Q1** | 打码走 **P-A / P-B / P-C**？ | **P-A**（用上游 `ruokuai` 或 `user` 渠道 + 上游换算） |
| **Q2** | 若快（`ruokuai`）账号由你提供，还是用源码里硬编码的那对凭据？ | 你提供 —— 那对是上游作者的，随时可能失效 |
| **Q3** | 若不走若快，你指定的打码服务 URL + 接口文档给我？ | 若 Q1 选 P-B 则需要 |
| **Q4** | 允许把**验证码下载/校验端点**换成真机可用的 `passcodeNew/getPassCodeNew` + `checkRandCodeAnsyn` 吗？（算法/渠道/换算仍用上游） | **允许** —— 否则登录打不通 |
| **Q5** | 上游「独立应用」的隔离方案选 **I-1 / I-2 / I-3**？ | **I-1**（进程内 + 垫片） |
| **Q6** | 表 C 的 C1–C9 保留自研，确认？ | 保留（上游 0 处，无处可换） |
| **Q7** | Web UI（B6）与日志（B7）是否也要换成上游的？ | **不换** —— 我们的 UI 是既有成果；日志替换收益低 |
| **Q8** | §7.3 的适配层 / 开关 / 类型转换由我们写，确认这是允许的？ | 必须允许，否则无法落地 |
| **Q9** | 分期顺序（P0→P4）与「每期三件套」的验收标准，认可？ | 认可 |

---

## 九、我承诺不做的事（写死）

1. 不再新写任何**上游已有**的协议/算法/渠道实现；
2. 不请求任何 `*.pjialin.com`（`free` 打码、设备指纹都按 §6 处理）；
3. 不在**没有你确认**的情况下改默认行为（当前默认：候补 = `testerSunshine`，其余域暂未切换）；
4. 不把上游代码**手抄**进项目 —— 一律 vendor + 生成器 + sha256 溯源；
5. 每期都保留**一键回退**，且回退必须逐字节等价于改造前。

---

## 十、**决策定案（2026-09-29，已逐条确认）**

| # | 问题 | 决策 | 说明 |
|---|---|---|---|
| 1 | 打码路线 | **P-A**：用上游渠道 + 上游本地换算，删掉被禁的 `free` 分支 | — |
| 2 | 打码渠道 | **不走若快**（第三方付费）→ **上游 `user` 模式** | 若快 softid/softkey 虽硬编码在源码里，但接口还需账号密码 |
| 3 | `user` 模式的服务地址 | **自建本机服务**：内部 ddddocr 识别 → 转成上游要的「格子编号」→ 交上游换算成 x,y | 修掉了老的假实现（图片发不出去 + 坐标不解析） |
| 4 | 验证码端点 | **允许换成** `passcodeNew/getPassCodeNew` + `checkRandCodeAnsyn`（算法/渠道/换算仍全用上游） | 上游两条旧端点已被 WAF 挡 |
| 5 | 上游隔离方案 | **I-1 进程内 + Config 垫片** | 封住它的集群/线程池副作用 |
| 6 | C1 候补 | 已按决策 B 用 `testerSunshine/12306` | — |
| 7 | C2 我的订单 | **部分换上游**：未完成订单用 testerSunshine 的源码；历史订单保留自研 | — |
| 8 | C3 `checkUser` | **请求构造用上游，判定保留**（「回 HTML ≡ 会话失效」是真机硬约束） | — |
| 9 | C4 人证核验 | **信号用上游 `ChechFace`，四态语义保留**（「判不出来必须放行」是真机踩出来的） | — |
| 10 | C5 WAF/HTML 识别 | **保留自研 + 上 pyppeteer 双保险** | 两家上游各 0 处 |
| 11 | C6 会话持久化 | **流程用上游**（`load_user`/`save_user`/`check_user_is_login` + `can_access_passengers` 双验活，比我们还严）**，格式保留加密 JSON** | 上游是 pickle 明文 + 无有效期 |
| 12 | C7 限速 | **节奏用上游**（testerSunshine 随机 1–3s）**，自适应退避与被拒即停保留** | 上游两家都没有退避 |
| 13 | C8 预售期 | **结构用上游 `judge_date_legal`**（已过去可拦 / 超前不拦）**，天数用真机实测 14** | 上游写死 `max_buy_time = 32`（月预售期是旧规则） |
| 14 | C9 本地存储 | **缓存类用上游**（乘车人缓存 / CDN 可用项，**路径迁到用户目录**）**，业务结构化存储保留 SQLite** | 上游 `RUNTIME_DIR = PROJECT_DIR + 'runtime/'` 打包后会写不进去 |
| 15 | Web UI / 日志 | **两个都不换** | UI 是既有成果；日志替换收益低风险高 |
| 16 | 适配层 / 开关 / 类型转换 | **允许写，但限定只管搬运** —— 适配层里**不得出现任何 12306 协议细节**（路径/参数/解析），由测试机械检查 | 这是唯一由我们写的一层，必须可验证 |
| 17 | 分期与验收 | **认可 P0→P4，逐期交付确认**；每期必交三件套（差分表 / 契约测试 / 一键回退） | — |

**由此确定的「由我们写」清单（全部限定为搬运性质）**：适配层 `kernel/pjialin/*`、
开关 `kernel/registry.py`、类型转换、`PROVENANCE.json` 与合规文件、本机打码服务（识别内核 ddddocr）。

---

## 十一、P0 执行结果与 P1 前置依赖（2026-09-29 实测）

### 11.1 P0 已完成（**未动任何业务行为**）

| 项 | 结果 |
|---|---|
| vendor 上游整包 | `vendor/pjialin/` **90 个文件**（44 个 `.py` + `data/stations.txt` 112KB + `data/cdn.txt` 42KB + LICENSE） |
| 锁定 commit | `52ccae8b206b828624f185bf8178e1057c6b5fb9`（2026-06-16） |
| 溯源 | `vendor/pjialin/PROVENANCE.json` —— 逐文件 sha256 + 上游出处 + 改动登记 |
| 生成/校验 | `python scripts/build_vendor_pjialin.py --init / --check` |
| 合规 | `LICENSE`（Apache-2.0 全文）+ `NOTICE` + `VENDOR.md`（改动说明，§4(b)） |
| 契约测试 | `tests/test_vendor_pjialin.py` **9 项全绿**，含 3 条**篡改检测**（改一字节 / 多一个文件 / 删合规文件，都必须变红） |
| 回归 | test_kernel_facade 12 / test_standby 21 / test_protocol_baseline 7 / test_contracts 11 **全绿** |

**排除项**：仅 `.github/`（CI 模板，非源码）。

### 11.2 P1 前置依赖（**实测 import 结果，不是估计**）

**机制**：上游用**绝对 import**（`from py12306.x import y`），
所以必须把 `vendor/pjialin` 注入 `sys.path` 才能 import。
测试 `test_vendored_url_table_is_importable_and_pins_the_banned_hosts` 已把这个机制钉住。

**① 可直接 import（无第三方依赖）**

```
✔ py12306.config
✔ py12306.helpers.api      ← URL 表，全局 60 行，纯常量，是最先要用的东西
✔ py12306.helpers.station
```

**② 需要补依赖才能 import**

| 缺的包 | 卡住谁 | 说明 |
|---|---|---|
| `requests-html` | `helpers/request.py` → 进而 `OCR.py` / `auth_code.py` / 几乎所有网络模块 | 上游 HTTP 层是 `Request(HTMLSession)` |
| `redis` | `cluster/cluster.py` → 被 `query/query.py`、`user/job.py`、`helpers/cdn.py` **无条件 import** | ⚠️ **即使集群关闭也照样 import** —— 所以 `redis` 是**硬依赖**，这点要提前知道 |
| `pyppeteer` | `order/order.py`（滑块 `request_slide`） | 你已同意引入；首次运行需下载 Chromium（~150MB） |

**③ 附带发现（P1 要处理的）**

- `helpers/api.py:53` 有一行**被注释掉的官方设备指纹地址**：
  `# API_GET_BROWSER_DEVICE_ID = BASE_URL_OF_12306 + '/otn/HttpZF/logdevice'`
  而 `query.py:130` / `user/job.py:331` 写着 `if 'pjialin' not in URL: return self.request_device_id2()`
  ⇒ **改回官方地址即走上自建路径**，不需要自建服务。
- `API_USER_LOGIN_CHECK = /otn/login/conf`（上游的登录态检测），
  与本项目用的 `/otn/login/checkUser` **不是同一条** —— C3 决策是「请求用上游、判定保留」，
  实施时两条都要能跑通（见 §10 第 8 条）。

### 11.3 P1 的第一批动作（等你确认后执行）

1. `requirements.txt` 加 `requests-html` / `redis` / `pyppeteer`（+ 上游 `requirements.txt` 里对应的固定版本）；
2. 建 `vendor/pjialin/shim/`（隔离方案 I-1：`Config` 垫片，封住集群与线程池副作用）；
3. 去掉两处托管依赖（设备指纹改官方地址 3 个文件 / 打码删掉 `free` 分支），
   **登记进 `KNOWN_MODIFICATIONS`** 并重跑 `--init`；
4. 建 `ticket_helper/kernel/registry.py` + `kernel/domains/*` 骨架；
5. 第一个可切换功能域：**站点表**（最无状态、最容易验证）。

---

## 十二、P1 第一批结果：站点表域（第一个可切换域）

### 12.1 交付物

| 文件 | 内容 |
|---|---|
| `ticket_helper/kernel/domains/station.py` | `StationTable` Protocol + 共享 `rank_candidates` / `resolve_code` |
| `ticket_helper/kernel/self/station.py` | 本项目后端（转发 `StationMap`） |
| `ticket_helper/kernel/pjialin/shim.py` | **I-1 隔离垫片**：sys.path 注入 + 可写路径重定向 + 屏蔽线程/集群/远端同步 |
| `ticket_helper/kernel/pjialin/station.py` | 上游后端（用上游站点表数据） |
| `ticket_helper/kernel/registry.py` | **唯一切换点**：`TICKET_HELPER_KERNEL_BACKENDS=station=pjialin` |
| `tests/test_kernel_station.py` | 10 条：V4 差分 + V2 契约 + 开关生效 + 默认值守卫 |

### 12.2 ★ 差分结果否决了「站点表默认切上游」

| 项 | self（我们，官方实时表） | pjialin（上游 2021 快照） |
|---|---|---|
| 站名数 | 3364 | 3319 |
| 交集 | — | 3148 |
| 只有上游有 | — | 171（切换后新出现） |
| 只有我们有 | 216 | —（**切换后使用者选不到**） |
| **交集内电报码不同** | — | **11 个** ← 最危险 |

那 11 个（站名相同、电报码不同）：
乌拉特前旗 `IQC`/`WQC`、乌海 `IAC`/`WVC`、乌鲁木齐南 `WNR`/`WMR`、五原 `IYC`/`WYC`、
信阳 `XYN`/`XUN`、天门 `TKN`/`TMN`、宜城 `YYN`/`YIN`、抚顺 `FET`/`FST`、
烟台西 `DCK`/`YTK`、石嘴山 `OZJ`/`QQJ`、黄陵 `KLY`/`ULY`。

⇒ **上游是 2021 年的快照，官方电报码后来改过。**
切过去会给这 11 个站发**错误的电报码** → 查不到车 / 下单失败，
而现象是「查询没结果」，**极难联想到是站表过期** ——
这与当年 405（路径写错却离线全绿）属于同一类：**错误被伪装成无关的症状**。

**结论：站点表默认保持 `self`**，并由
`tests/test_kernel_station.py::test_default_stays_self_because_of_the_11_wrong_codes`
守住 —— 只有清掉那 11 个错码后才允许改默认。

**这正是差分验证（V4）存在的意义**：它不是「证明我们对了」，
而是**在用上游之前发现用上游会坏在哪**。

---

## 十三、P1 剩余三项执行结果（2026-09-30）

### 13.1 补上游依赖（含一个**实测才暴露**的缺口）

| 包 | 谁需要它 | 备注 |
|---|---|---|
| `requests-html>=0.10.0` | `helpers/request.py` 的基类（`Request(HTMLSession)`） | 进而被 `OCR.py` / `auth_code.py` 依赖 |
| `redis>=5.0.0` | `cluster/cluster.py` ← 被 `query/query.py`、`user/job.py`、`helpers/cdn.py` **无条件** import | ⚠️ 即使集群关闭也必须装 |
| `pyppeteer>=2.0.0` | `order/order.py` 的滑块 | 首次运行需下载 Chromium（~150MB） |
| `lxml_html_clean>=0.4.0` | ⚠️ **容易漏**：`requests-html 0.10.0` 仍 `from lxml.html.clean import Cleaner`，而该模块在新版 lxml 里已拆成独立包 | 缺它在 **import 阶段**直接 `ImportError` |

最后一行是**实测发现的**：装完前三个，`import py12306.helpers.OCR` 直接报
`ImportError: lxml.html.clean module is now a separate project`。
只按上游 `requirements.txt` 照抄会漏掉它 —— 上游那几年还没拆包。

### 13.2 去掉两处 pjialin 托管域（改 vendor + 登记 + 机器守卫）

改法（**都在上游自己留的分支上**，不需要自建服务）：

| 项 | 原来的值 | 改成 | 生效机制 |
|---|---|---|---|
| 设备指纹 | `https://12306-rail-id-v2.pjialin.com/` | `BASE_URL_OF_12306 + '/otn/HttpZF/logdevice'`（上游注释里那一行） | `query.py:130` / `user/job.py:331` 的 `if 'pjialin' not in URL: return self.request_device_id2()` 判真 → 走直连官方路径 |
| 免费打码 | `API_FREE_CODE_QCR_API = 'https://12306-ocr.pjialin.com/check/'` | **整个常量删除**，`OCR.py` 的 `free` 分支也删掉 | 只剩 `user` 模式（自建服务） |

合规：两处都登记进 `scripts/build_vendor_pjialin.py` 的 `KNOWN_MODIFICATIONS`
（含「改了什么 / 为什么」），`--init` 重建后 `--check` 通过
（**90 个文件、2 个已登记修改**）；`VENDOR.md` §3 自动生成改动说明（Apache-2.0 §4(b)）。

**三条机器守卫**（改写后立刻生效）：

1. `test_no_pjialin_host_survives_anywhere_in_vendor` —— 用 AST 扫 `vendor/**/*.py` 的
   **字符串常量**，出现 `pjialin.com` 即失败（注释里引用原值是允许的，所以只查字符串）；
2. `test_vendored_url_table_is_importable_and_pins_the_protocol_facts` —— 断言
   `API_GET_BROWSER_DEVICE_ID == BASE_URL_OF_12306 + '/otn/HttpZF/logdevice'` 且
   `API_FREE_CODE_QCR_API` **不存在**；
3. `test_vendored_tree_is_complete_enough_to_import` —— `helpers.OCR` / `request` / `cdn`
   现在都必须能 import（缺任何依赖都会在这里炸，而不是等到运行期）。

### 13.3 ★ 打码域：让「识别」真的产出坐标

#### 为什么要这么切

上游 `helpers/OCR.py` 的源码里**没有识别能力**，只有「把图发给谁」的渠道；
三个渠道返回的都是**格子编号 1..8**，再由上游本地的
`get_image_position_by_offset` 换算成 x,y。所以：

```
图 ──[本机服务：ddddocr 识别]──► 格子编号 ──[★ 上游代码]──► x,y 坐标
```

**换算那一段一行都不是我们写的** —— 这正是修掉「假实现」的关键
（老实现自己算坐标，且请求根本没发对）。

#### 交付物

| 文件 | 职责 |
|---|---|
| `kernel/domains/captcha.py` | `CaptchaSolver` Protocol + `format_positions`（唯一拼接处） |
| `kernel/pjialin/captcha.py` | 调上游三连：`OCR.get_img_position` → POST(`img`) → 上游换算。只做 3 件搬运 |
| `kernel/self/captcha.py` | 回退后端，转发 `FreeSolver`（本项目自己的协议） |
| `verify/captcha_server.py` | 本机服务：`ddddocr` 识别 → 输出上游协议 `{"msg":"success","result":[编号]}` |
| `kernel/registry.py` | `captcha` 域注册；`get(domain, **kwargs)`，**地址进缓存键** |
| `runtime/context.py` | 装配点：解析地址 + 注入（`_build_external_solver`） |
| `tests/test_kernel_captcha.py` | **30 条**契约测试 |

#### 两个后端的差异（就是「老实现为什么用不了」的答案）

| 项 | `self`（本项目） | `pjialin`（上游，默认） |
|---|---|---|
| 请求字段 | `image` + `kind` | **`img`** |
| 响应约定 | `{"answer":"x,y,…"}` | `{"msg":"success","result":[编号…]}` |
| 坐标来源 | 服务端直接给 | **上游 `get_image_position_by_offset`** |

字段名不同 = 服务端拿不到图 = 必然失败，而调用方只看得到一句「识别失败」。
所以 `self` 后端遇到上游协议时会**明说协议不匹配**并给出切换指令，
而不是返回 `None` 了事。

#### 外部锚点（防自证）

格子换算必须与上游一致，而**只看「中心点落回原格」是抓不住错误的**：
`CELL` 从 75 改成 80 时，上游那 8 个理想中心仍然各自落在原来的格里。
所以真正的锚点是
`test_cell_size_equals_the_upstream_spacing`：把上游公式的**抖动置零**
（临时替换 `random.randint`），取理想中心，断言
**相邻格中心间距 == 本地 `CELL`**。变异检验证实：改成 80 → 这条变红。

#### 记录在案的**可观测性缺口**

上游 `Request.request()` 会把**连接失败也吞成空响应**（它捕获 `RequestException`
后伪造一个空 `HTMLResponse`）。所以我们**区分不出**「服务不可达」与「协议不对」。
处理方式：`last_error` 如实列出**两种可能** + 给出自检指令 + 带上地址，
不猜一个原因（猜错会让人往错的方向查）。

#### 尚未做的（如实）

- 上游的验证码下载/校验端点已被 WAF 挡，仍需换成 `passcodeNew` 那两条（决策 §10-4）；
- 真机未验证 —— 本域证据只到「协议与换算正确」，**不等于「识别准确」**；
- ddddocr 通用检测模型对 12306 点击型实测不适用，**人工兜底必须保留**。

---

## 十四、P2 第一批：余票查询域（2026-09-30）

### 14.1 ★ 上游给了两样**可验证的协议事实**——而且暴露了一个真差异

| 项 | 上游 | 本项目 | 结论 |
|---|---|---|---|
| **字段下标** | `query/job.py` 的 `INDEX_*`（10 个） | `inter/parser.py` 的 `IDX_*` | **10 项里 9 项名称与取值都对得上**，1 项名称不同但用法一致（见下） |
| **查询路径怎么取** | 读余票页 HTML 的 `var CLeftTicketUrl` | **固定候选列表盲试** 4 条 | **真差异**：盲试会发注定被拒的请求 |

**那 1 项命名陷阱**：上游 `INDEX_TICKET_NUM = 11`，名字是「余票数」，
但它**实际被拿去和 `'Y'` 比较**（`is_has_ticket` → `get_info_of_ticket_num()`），
即「可否网订」—— 与本项目的 `IDX_CAN_WEB_BUY = 11` 同义。
⇒ 两边**取值一致、语义一致，只有名字骗人**。测试里专门钉住：
按名字理解它，维护者会去 `int()` 一个 `'Y'`。**名字会骗人，取值不会。**

### 14.2 交付物

| 文件 | 职责 |
|---|---|
| `scripts/build_vendor_pjialin.py`（扩展） | 新增「协议事实」生成物：从上游**逐字切出** ① `INDEX_*` ② 读路径的正则 ③ 余票 URL 模板；`--check` 会**重新生成一遍再比对** |
| `vendor/pjialin/ticket_facts.py`（生成物） | 上述三条事实 + 逐条上游出处（文件 + sha256 前缀） |
| `tests/test_query_baseline.py` | **V5 参数级基线**（12 条）—— 本项目此前只有路径级基线，**参数级是空缺** |
| `kernel/domains/query_route.py` | `LeftTicketRoute` Protocol + `dedupe`（唯一去重处） |
| `kernel/self/query_route.py` | 回退后端：原样返回候选列表（**刻意忽略页面**，否则两个后端就没区别了） |
| `kernel/pjialin/query_route.py` | 用上游正则读路径 + **形态归一** |
| `scripts/query_route_diff.py` | 差分表：同一张页面下，两家各要**几次请求**才打到对的那条 |
| `tests/test_kernel_query.py` | 契约测试（17 条），含**端到端判据**（看 mock 记下的真实请求路径） |

### 14.3 差分表（实际输出）

```
[余票页宣称 leftTicket/queryA（= 官方当前路径）]
    self    : /otn/leftTicket/queryZ → queryA → query → queryO
    pjialin : /otn/leftTicket/queryA → queryZ → query → queryO
    打到「对的那条」需要几次请求：self=2  pjialin=1
```

⇒ 盲试会先发一次**注定被拒**的请求。而被拒的表现（HTML 错误页）与「被风控」**长得一样**，
所以这些无用请求既费配额又**污染风控判断**（本项目已有「被拒即停」的硬约束）。

**但默认后端仍是 `self`**：上游那套更省请求，可它换的是**行为**，
按 v3 §10-17 要先有差分与真机证据才切。`python scripts/query_route_diff.py` 可随时复现上表。

### 14.4 一个必须处理的**形态差异**（否则优化直接变成故障）

上游读出来的是 URL 模板里的 `{type}` 片段（`leftTicket/queryO`），
本项目候选列表是完整站内路径（`/otn/leftTicket/queryZ`）。
直接拿去 `client.get()` 会打到一个不存在的地址 —— 而症状是「查询失败」，
与「被风控」一模一样。

归一方式**不是我们发明的拼接**：用上游自己的 URL 模板
（`ticket_facts.ROUTE_URL_TEMPLATE`，从 `helpers/api.py` 逐字切出）把 `type` 还原成
上游会请求的 URL，再取路径部分。测试钉住「结果必须以 `/otn/` 开头」。

### 14.5 接进链路时防的三件事

1. **路径必须在热身之后取** —— 路径就来自热身那张页面（`ensure_session_warm` 顺带缓存正文）。
   读它**不额外发任何请求**。
2. **旁路不能漏传**：`attach_api()` 重建 `TicketApi` 时若忘了带策略，
   那条路径上的账号会**悄悄**退回候选列表，而其他账号已用上新策略。
   匿名会话（拉车次用）同理。两处都有测试与变异守着。
3. **取路径失败不能成为新的失败点**：解析器抛异常 / 给空列表 → 一律退回候选列表。

### 14.6 顺带发现并记录（未修）

`inter/parser.py` 里有一组 `IDX_RW_NUM` / `IDX_ZE_NUM` … 常量**当前没有任何调用方**，
它们按 12306 的**字母席别码**命名，与 `SEAT_INDEX`（按中文席别名）**描述同一件事**。
两套说法将来若分叉，表现是「界面有的席别查不到票」。
已写进 `docs/缺陷回流台账.md`，**没有**替它们编一张映射表 ——
编错了只会得到一条**错的**契约。

---

## 十五、P2 第二批：用户域（2026-09-30）

### 15.1 上游用户域协议事实（继续用生成物，不手抄）

`vendor/pjialin/ticket_facts.py` 新增三块，来源与 `--check` 机制同 §14：

| 事实 | 来源 | 内容 |
|---|---|---|
| `USER_ENDPOINTS` | `helpers/api.py` | 5 个端点的**路径**（已去 `BASE_URL` 前缀） |
| `USER_RESPONSE_FIELDS` | `user/job.py` | 上游读响应用的**点号字段路径**，按函数归类 |
| 语义别名 | 上面两张表 | `LOGIN_CHECK_PATH` / `LOGIN_ALIVE_FIELD` 等（让适配层不出现上游常量名） |

上游读响应是 **`result.get('data.is_login')` 这种点号路径直接取嵌套** ——
「数据嵌在哪一层」本身就是协议事实，而它恰好是本批最有价值的发现（见 15.3）。

### 15.2 端点对比：**四个一致，一个不同**

| 用途 | 上游 | 本项目 | 结论 |
|---|---|---|---|
| 用户信息 | `/otn/modifyUser/initQueryUserInfoApi` | 同 | ✅ 一致 |
| 乘车人 | `/otn/confirmPassenger/getPassengerDTOs` | 同 | ✅ 一致 |
| 生成二维码 | `/passport/web/create-qr64` | 同 | ✅ 一致 |
| 轮询扫码 | `/passport/web/checkqr` | 同 | ✅ 一致 |
| **登录态检测** | **`GET /otn/login/conf`** | **`POST /otn/login/checkUser`** | ⚠️ **不同** |

⇒ 「一致」也被写成**可验证的**（`tests/test_user_baseline.py` 逐项参数化断言）：
否则哪天有人改了一边，没人会发现 —— 而且改了还能跑，因为另一边才是真在用的。

**唯一的那个差异做成了可切换域** `login_check`（见 15.4）。

### 15.3 ★ 一个被上游出处逮到的真缺口：少剥一层

上游 `user/job.py::get_user_info` 读的是 **`data.userDTO.loginUserDTO`** ——
而本项目的用户信息解析只剥到 `userDTO` 一层。

实测（不是推测）：

```
扁平 data                   -> username='张三' sex='男'
多一层 userDTO              -> username='张三' sex='男'
data.userDTO.loginUserDTO  -> username=''     sex=''      ← 整片空白
```

三种结构的**字段名完全一样**，所以少剥一层**不会报任何错** ——
症状只是「账号信息页一片空白」，与「接口挂了」长得一模一样。

修法：把剥层**集中到一个函数**（`inter/parser.py::unwrap_user_payload`），
`parse_user_profile` 与 `_verify_flag_from_user_info` 共用它。

**为什么必须集中**：两处消费点各写一遍「先看这层再看那层」，就必然有一处会漏；
而漏的那处正好是**人证核验字段** → 被判成「无法判定」→ 那是个有严格语义的词
（见 `account/verify.py`），一旦取不到就可能走进「永久挡住正常账号」的老路。

### 15.4 `login_check` 域：**请求规格是数据，判定逻辑共享一份**

| 后端 | 请求 | 判活字段 | 取值要求 |
|---|---|---|---|
| `self`（**默认**） | `POST /otn/login/checkUser` + Referer | `data.flag` / `status` | Python 真值 |
| `pjialin` | `GET /otn/login/conf`（**不带 Referer**） | `data.is_login` | `'Y'`（字符串比较） |

设计要点（按决策 C3「请求用上游、判定保留」）：

1. 门面只固定两样 —— **怎么问**（`LoginProbeSpec` 的 path/method/data/referer）
   与**从响应哪读结论**（`alive_paths`/`alive_values`）；
2. 判定（HTML 一律失效 / 拿不到结论判不可用 / 三态语义）**两套后端共用一份**
   （`domains/login_check.py::alive_of`）—— 切换后端不可能顺手改掉判定；
3. 而「回 HTML ≡ 失效」这条真机硬约束留在 `inter/api.py`，**不在门面上** ——
   它是判定骨架，不是可切换项。

**三处差异都保留在能被看见的地方**（差分表 4 行）：

```
端点：/otn/login/checkUser  vs  /otn/login/conf
方法：POST  vs  GET
Referer：带  vs  不带
判活字段：['data.flag', 'status']  vs  ['data.is_login']
```

⚠️ **没有**给上游那条「顺手补上 Referer」。看着是好心，实际后果是差分表从此
显示「两边都带」，真机上切默认时才发现会被 WAF 拒。**补平差异不是修 bug，是藏 bug。**

### 15.5 实测到的两个语义陷阱

**① `bool("N")` 是 `True`。** 上游判活是 `data.is_login == 'Y'` 的**字符串比较**；
若照抄字段却按 Python 真值判，`is_login = "N"`（= 明确未登录）会被判成 **ALIVE**。
所以上游规格**必须显式给** `alive_values=("Y",)` —— 它不是一个可选的优化项。
这条有专门的用例与变异（撤掉 `alive_values` 就会变红）。

**② 两套实现的判活字段互不认得对方。** 本项目字段喂给上游规格 → `UNKNOWN`，
反之亦然。这不是缺陷，而是**结构事实**：切后端时**必须连响应结构一起换**，
只换端点会让两边都判成「判不出来」。差分表里有一条断言专门钉住它。

### 15.6 交付与验证

| 文件 | 职责 |
|---|---|
| `kernel/domains/login_check.py` | `LoginProbeSpec`（数据）+ `alive_of`（**唯一**判定实现）+ 三态常量 |
| `kernel/self/login_check.py` | 本项目规格（值全部引用 `inter/urls.py` 与 `inter/parser.py` 的常量） |
| `kernel/pjialin/login_check.py` | 上游规格（值全部取自生成物） |
| `inter/urls.py::CHECK_USER_PROBE` | 请求侧**唯一真源**（接口层未注入时也用这一份） |
| `inter/parser.py::CHECK_USER_ALIVE_PATHS` | 响应侧**唯一真源** |
| `tests/test_kernel_user.py` | 27 条契约测试（含**端到端看真实请求路径**） |
| `tests/test_user_baseline.py` | 11 条参数级基线 |
| `scripts/login_check_diff.py` | 差分表（退出码 0 = 差异与文档一致） |
| 变异检验 | `-k 登录态` 6/6、`-k 用户` 1/1 抓到 |

**默认仍是 `self`**：这是唯一「两套都自洽、但结论会不同」的域 ——
按 v3 §10-17，切默认要真机证据。

### 15.7 收尾时踩到的一起「静默死循环」（记在最显眼处）

**症状**：`test_standby_monitor_fulfills_and_notifies` **卡死 20 分钟** —— 不是失败，是永不结束。

**根因链**（每一环都静默，合起来才致命）：

1. 我往 `TicketApi.__init__` **插入新方法**时，把紧随其后的
   `self._order_combo_hint = None` 挤进了新方法体（`return` 之后）→ 永不执行的死代码；
2. `query_order_history` 每次调用抛 `AttributeError`；
3. 被 `engine._check_fulfilled_by_order` 的 `log.debug` **吞掉**（默认日志级别看不见）；
4. 监控**永远等不到「已兑现」，也不会报错** → 同步跑的 `_monitor_loop()` 变成死循环。

**定位手段**：`faulthandler.dump_traceback_later(25, exit=True)` ——
栈显示主线程正常停在 `self._stop_event.wait(delay)`，于是排除死锁，
结论变为「循环在跑、只是等不到兑现」。**这一步省掉了大量瞎猜。**

**修法与防复发**：

| 项 | 内容 |
|---|---|
| 修 | 属性移回 `__init__`；`log.debug` → `log.warning`（「兑现判定失效」必须可见） |
| 守卫 1 | `test_every_attribute_declared_in_init_actually_exists`：AST 取 `__init__` 里声明的属性名，逐个 `hasattr(实例)` —— **「插错位置」立刻变红** |
| 守卫 2 | `test_order_query_failure_is_visible_instead_of_swallowed`：caplog 断言 WARNING |
| 变异 | 2/2 抓到（把属性挤走 / 把 warning 降回 debug） |

⇒ **教训**：**「卡住」比「失败」危险**。失败会变红、会有人修；
卡住只是永远不结束，最容易被当成「机器慢」放过去。

**顺带被门抓到的两处**（我这次的改动）：

- 变异脚本 `contract_mutation_check.py` 被我用 `read_text` + `write_text` 改成了**纯 LF**
  （`read_text` 做 universal newlines）→ V0 门报出来 → 已归一。
- `domains/login_check.py` 的 docstring 里出现 `ticket_helper.inter` →
  C1 门报出来 → 已改写。**没有放宽门**：注释里的引用会腐烂成真的 import，门的规则是对的。

---

## 十六、P2 第三批：扫码轮询域 + 关闭 P1 遗留的验证码端点项（2026-09-30）

### 16.1 扫码：**端点一致，差异只在请求体**

| 项 | 上游 | 本项目 | 结论 |
|---|---|---|---|
| `create-qr64` | `/passport/web/create-qr64` | 同 | ✅ 一致 |
| `checkqr` | `/passport/web/checkqr` | 同 | ✅ 一致 |
| `appid` | `otn` | 同 | ✅ 一致 |
| `result_code` 语义 | 0 待扫 / 1 已扫 / 2 成功 / 3 过期 | 同 | ✅ 一致 |
| **`checkqr` 表单** | `RAIL_DEVICEID, RAIL_EXPIRATION, uuid, appid` | `uuid, appid, _json_att` | ⚠️ **上游多带两个设备指纹** |

⇒ 这个域的差异**特别不显眼**：端点相同、返回值也相同，
「切了没切」既不体现在 URL 上也不体现在结果上 —— **只能靠真实表单判**。
新增域 `qr_check`（默认 `self`），`scripts/qr_check_diff.py` 把它摆出来。

**设计取舍**：域里**只有请求体**，没有状态判定。因为 `result_code` 语义两边一致，
映射表在 `inter/parser.py` 已是唯一实现 —— 再抽一层只会多一处要同步的地方
（「不抽用不上的东西」）。

### 16.2 ★ 又一次「判据不可观测」：mock 只记了 3 个字段

写端到端测试时发现：mock 的 `checkqr` 只记 `{uuid, appid, method}` ——
于是「上游那套到底有没有带设备指纹」在离线**根本看不出来**（两套请求一模一样）。

⇒ 改成记录**完整表单**。这条与之前几处是同一类问题：
**判据要素没被记下来，测试就只能证明自洽**（回流台账 #26）。

### 16.3 ★ P1 遗留项的真身：登录验证码**校验**端点仍在用被 WAF 挡的旧路径

P1 时只处理了「下载」那半截（`get_login_captcha` 新路径优先），
但 `check_login_captcha` **单押** `/passport/captcha/captcha-check` ——
那正是上游 `helpers/api.py::API_AUTH_CODE_CHECK` 用的路径，真机已被 WAF 挡。

**于是这个函数恒返回 False**，而调用方只看到「验证码不对」——
「端点选错」与「验证码填错」在返回值上**完全同形**。

修法与决策依据：

| 项 | 内容 |
|---|---|
| 改 | 与下单验证码共用 `/otn/passcodeNew/checkRandCodeAnsyn`；请求体同时带 `answer` 与 `randCode`（官方两条链路各读一个） |
| 授权 | 决策 §10-4（「允许只换端点；算法/渠道/换算仍全用上游」） |
| 守卫 | `tests/test_captcha_endpoint.py`（5 条）：端到端看真实路径 + 静态守卫「旧常量不许被运行时引用」 |
| 变异 | 2/2（退回旧端点 / 只带 `answer`） |

**关于「必须用上游源码」这条要求，这里要讲清**：

上游那份验证码实现在真机上**不可用**（WAF 挡）。
正确处理是「保留可用的那条 + 把上游为何不可用记录在案」，
而不是照着上游写一条注定走不通的路径 —— 那只是让源码**看起来**用了上游。
本批**没有**去替换 vendor 里那两条常量（换了也没人调），
而是在 `urls.py` 保留常量并注明「已失效」，再由守卫禁止运行时代码引用它。

⇒ **P1 遗留项关闭**（当初记的是「换端点」；核实后发现真正的缺口是「校验那半截没换」，已修）。

### 16.4 交付与验证

| 文件 | 职责 |
|---|---|
| `kernel/domains/qr_check.py` | `QrCheckSpec`（数据）+ `form()`（**两套共用**的组装） |
| `kernel/self/qr_check.py` / `kernel/pjialin/qr_check.py` | 两套规格（值全部来自唯一真源） |
| `inter/urls.py::QR_CHECK_FORM_KEYS` / `QR_CHECK_CONSTANTS` | 扫码请求侧的**唯一真源** |
| `inter/base.py::cookie_of()` | 读单个 cookie（避免穿透 `session`） |
| `tests/test_kernel_qr.py` | 14 条契约测试（含端到端看真实表单） |
| `tests/test_qr_baseline.py` | 9 条参数级基线（含「一致」也断言 + 逐个码语义） |
| `tests/test_captcha_endpoint.py` | 5 条（验证码端点归属） |
| `scripts/qr_check_diff.py` | 差分表 |
| 变异 | 扫码 6/6、验证码 2/2 |
| 变异脚本自检 | **新增「变异名唯一」检查** —— 本轮发现同名变异被写了两份（见 16.5） |

### 16.5 顺带修掉的：变异脚本里同名变异被写了两份

`MUTATIONS` 里「构造完整性 / 失败可见性」两组被**写了两遍**（执行环境重复执行所致）。
后果：同一变异跑两遍（耗时翻倍），而「共 N 个变异」这个数字**虚高**，
看的人会以为覆盖得更全。

⇒ 给变异脚本加了**自检**：变异名重复即报错退出（并已验证「制造重名 → 变红」）。
**变异脚本要自检**这条纪律，这次落在了一个没想到的地方。
