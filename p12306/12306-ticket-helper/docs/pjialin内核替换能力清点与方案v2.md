# pjialin/py12306 内核替换 · 能力清点与方案 v2

> 触发：需求方明确「**这次重构必须改成依赖 `pjialin/py12306` 的源码；它里面已有的功能禁止自己实现，必须用它的**」。
> 本文先给**能不能执行**（含一条必须你定义才能继续的硬阻塞），再给可执行部分的方案。
> 核查时间：2026-09-29，上游 `master`，所有结论都可按文中命令复跑。

---

## 〇、先说会挡住你的那件事

**`pjialin/py12306` 里没有候补（standby）—— 一行都没有。**

```
$ grep -rn "afterNate\|confirmHB" --include=*.py . | wc -l
0
```

而**候补是本项目的核心功能**（`standby/engine.py` 七步链路 + 人证核验 + 排队轮询）。

⇒ 「**候补禁止自己实现、必须用 pjialin 的**」这条要求，
**在当前上游状态下无法执行**：上游没有可用的实现，也就没有「用它」这个选项。

这一条我不能替你绕过去（绕过去就是「假装用了上游」，正是你要避免的那件事）。
出路见 §5.1，需要你选。

**其余「上游有」的功能，可以按你的要求换成上游实现** —— 见 §2 与 §6。

---

## 一、上游事实（可复跑）

| 项 | 值 |
|---|---|
| 仓库 / 许可 | `pjialin/py12306` · **Apache-2.0** |
| 活跃度 | 未归档，14,963 star，`pushed_at` 2026-09-17 |
| 规模 | 44 个 `.py` / 5,020 行 |
| 形态 | **配置驱动的独立应用**（无 `setup.py`），入口 `main.py` + `env.py` |
| Python | 无 `python_requires`；requirements 新旧混排（Flask 3.1.3 / requests 2.33 / **Jinja2 2.10** / **websockets 10.0** / `pyppeteer-box` / `requests-html`） |

**关键文件的最后实质改动**（GitHub commits API 逐文件查）：

| 文件 | 最后提交 | 说明 |
|---|---|---|
| `helpers/api.py`（接口表） | **2021-09-24** | 「添加扫码登录功能」 |
| `helpers/notification.py` | **2021-09-29** | 「增加二维码发送到邮箱」 |
| `order/order.py`（下单） | 2026-06-02 | 「fix: replace bare except」→ **仅语法修复** |
| `query/query.py`（查询） | 2026-06-02 | 同上，仅语法修复 |
| `user/job.py`（登录） | 2026-06-02 | 同上，仅语法修复 |

⇒ **核心业务逻辑停在 2019–2021；2026 年的提交只把裸 `except` 改成 `except Exception`。**
「仓库还活跃」不等于「逻辑还新」，这两件事必须分开看。

复跑：

```bash
curl -sSL -o r.tgz https://codeload.github.com/pjialin/py12306/tar.gz/refs/heads/master
tar xzf r.tgz && cd py12306-master
grep -rn "afterNate\|confirmHB" --include=*.py . | wc -l     # → 0
for f in py12306/helpers/api.py py12306/order/order.py; do
  curl -sS "https://api.github.com/repos/pjialin/py12306/commits?path=$f&per_page=1" | head -20
done
```

---

## 二、上游**有**的能力 → 这部分可以按你的要求换成上游实现

| 能力 | 上游位置 | 我们现在在哪 | 可否替换 |
|---|---|---|---|
| 余票查询 | `query/query.py`、`query/job.py` | `query/engine.py`、`inter/api.py` | ✅ 可 |
| **余票页预热** | `query/query.py:211` 调 `API_QUERY_INIT_PAGE` | `ensure_session_warm` | ✅ **上游也有**（印证我们那条真机硬约束不是臆测） |
| 站点表 | `helpers/station.py` | `inter/station.py` | ✅ 可 |
| **扫码登录** | `helpers/api.py`（`create-qr64`/`checkqr`）+ `user/job.py` | `account/qrlogin.py` | ✅ 可 |
| 密码登录 | `user/user.py`（`/otn/login/userLogin`） | `inter/api.py` | ✅ 可（真机走不通，见 §3） |
| 登录验证码 | `helpers/auth_code.py`（旧 `/passport/captcha/...`） | `verify/service.py` | ⚠️ 路径已被 WAF 挡 |
| 用户信息 | `API_USER_INFO` | `account/profile.py` | ✅ 可 |
| 乘车人 | `API_USER_PASSENGERS`（`getPassengerDTOs`） | `inter/api.py` | ✅ 可 |
| **下单链路** | `order/order.py`（`submitOrderRequest`→`initDc`→`checkOrderInfo`→`getQueueCount`→`confirmSingleForQueue`→`queryOrderWaitTime`） | `inter/api.py` | ✅ 可（风险最高，放最后） |
| **滑块风控** | `order/order.py::request_init_slide` —— 用 **pyppeteer 无头浏览器**真拖拽 `#slide_passcode` | 我们不自行计算，走人工兜底 | ⚠️ 有实现，代价见 §4.3 |
| CDN 优选 | `helpers/cdn.py` + `data/cdn.txt`（400 个 IP） | 无 | ➕ 上游独有，我们没有 |
| 通知渠道 | `helpers/notification.py`（邮件 / Server酱 / 钉钉 / 企微 / Telegram / 语音） | `notify/channels.py` | ✅ 可 |
| 云打码 | `helpers/OCR.py` + `vender/ruokuai` | **无**（本地 ddddocr + 人工兜底；云打码渠道 2026-09-29 已移除） | ⚠️ 见 §4.2 |
| 心跳保活 | `user/job.py` | `account/manager.py` | ✅ 可 |
| 分布式集群 | `cluster/`（Redis 锁） | 无 | ➕ 上游独有 |
| 日志 | `log/`（7 个模块） | `logger/` | ⚠️ 收益低 |
| Web UI | `web/`（handler 风格） | `web/` + `desktop_ui/` | ❌ 不建议（我们的 UI 是**本轮之前专门做过的成果**） |
| 设备指纹 | `API_GET_BROWSER_DEVICE_ID` = `https://12306-rail-id-v2.pjialin.com/` | 无 | ⚠️ 见 §4.2 |

---

## 三、上游**没有**的能力 → 这些「用上游实现」这条路不存在

| 缺什么 | 证据 | 我们的实现 | 缺了会怎样 |
|---|---|---|---|
| **候补整族** | `afterNate`/`confirmHB` **0 处** | `standby/engine.py` + HB 七步 | **核心功能消失** |
| 我的订单 / 未完成订单 | `queryMyOrder` **0 处** | `order/service.py` | 无法确认出票、无法认领候补单 |
| `checkUser` 登录态检测 | **0 处** | 会话恢复必调它验活 | 恢复的会话是否有效无从判断 |
| 人证核验 | `chechFace`/`face_flag`/`人证` **0 处** | `account/verify.py` 四态 | 候补前无法判定、无法给出路 |
| WAF / HTML 错误页识别 | **0 处** | `_reject_html` + `RiskControlError` | 「回 HTML 而不是 JSON」会被当业务响应解析 |
| 会话持久化到磁盘 | **0 处**（只有 Redis 集群内的内存态） | `account/session.py` | 每次重启都要重新登录 |
| 限速 / 退避 | **0 处** | `ratelimit/limiter.py` | 被风控限流后无法自恢复 |
| 14 天预售期判断 | 只有一句「超出一个月预售期」文案 | `train_date.py`（真机实测 14 天） | 「日期不可售」被伪装成「被限流」 |
| 本地结构化存储 | **0 处**（`data/*.json` + Redis） | `storage/` | 任务/订单/记录无处落盘 |

**这九条是「本项目已有的、真机验证过的资产」。** 按「上游有就必须用上游」的字面执行，
它们无处可换 —— 只能保持自研。这一点必须写清楚，否则会变成「偷偷留着不说」。

---

## 四、三个必须你定义的问题（我不自行决定）

### 4.1 候补怎么办（**硬阻塞**）

| 选项 | 代价 |
|---|---|
| **A. 候补保持自研**，其余按你的要求切上游 | 与「禁止自己实现」冲突，但保住了核心功能 |
| **B. 候补改用 `testerSunshine/12306` @`a495af88`**（MIT，**有完整候补**：`ChechFace`/`ConfirmHB`/`PassengerInitApi`/`GetSuccessRate`/`GetQueueCount`/`QueryOrderWaitTime`） | 与「必须用 pjialin」冲突，但**确实换成了上游代码**，且我们已存档原件 + 做了对照报告 |
| **C. 砍掉候补功能** | 直接执行你的要求，但项目少掉核心能力 |

> 我的判断：**B 最接近你的原意** —— 你要的是「内核用成熟上游、不要自己写」，
> 而候补这件事上**只有 `testerSunshine/12306` 有成熟上游实现**。
> 但这是产品级取舍，必须你定。

### 4.2 是否允许请求 pjialin 的托管服务

上游代码里有两个**第三方托管域名**（不是 12306 官方）：

```
API_GET_BROWSER_DEVICE_ID = 'https://12306-rail-id-v2.pjialin.com/'   # 设备指纹
API_FREE_CODE_QCR_API     = 'https://12306-ocr.pjialin.com/check/'    # 免费打码
```

用上游代码就会打这两个域名。你此前说过**不依赖其托管服务** —— 现在需要你明确：
**允许 / 不允许 / 允许但可关闭**。

### 4.3 是否接受 pyppeteer 真浏览器

上游 HTTP 层是 `Request(HTMLSession)`（`requests-html` + `pyppeteer`），
滑块风控靠**真的开一个无头 Chromium 去拖**。换它意味着：

- 打包要带 Chromium（体积 +100MB 量级）或首次运行联网下载；
- 与「双击即用的 `TicketHelper.exe`」这条既有约定冲突；
- 好处是真的能过滑块（这是我们现在做不到的）。

**接受 / 不接受 / 只在下单那一条链路用。**

**实测（Python 3.13.14 干净虚拟环境，已跑）**：

```
requests-html  0.10.0    ✅ 装上
pyppeteer      2.0.0     ✅ 装上
pyppeteer-box  0.0.27    ✅ 装上
websockets     10.0      ✅ 装上
pypng / pyquery / lightpush / DingtalkChatbot / beautifulsoup4   ✅ 装上
```

⇒ **依赖本身不再是「装不上」的障碍**（这与我此前对 `testerSunshine` 侧 TF1.14 的结论不同，
两边要分开看）。**尚未验证**的是 `pyppeteer.launch()`：
Chromium 二进制不在 pip 包里，首次调用才联网下载（约 150MB）。
按本项目的纪律，这条标为 **【待验证】** —— 要真跑一次才算数，
而且它直接决定「双击即用的 exe」是否还成立。

---

## 五、方案 v2（架构修订）

### 5.1 分层不变，门面**扩展**

v1 的门面只覆盖「候补请求体」（2 个方法）。现在要按功能域切，门面扩为：

```
ticket_helper/kernel/
├── __init__.py            # 注册表 + 开关（唯一取值入口，新增按域解析）
├── registry.py            # 功能域 → 后端 的映射与校验
├── domains/
│   ├── login.py           # Protocol: qr_login / password_login / check_user
│   ├── query.py           # Protocol: query_left_ticket / station_map
│   ├── order.py           # Protocol: submit / check / queue / confirm
│   ├── passenger.py       # Protocol: list_passengers
│   ├── notify.py          # Protocol: send
│   └── standby_payload.py # ← v1 已落地的那两个方法
├── self/                  # 各域的「本项目实现」适配器
└── pjialin/               # 各域的「上游实现」适配器（调用 vendor/pjialin/）
vendor/pjialin/            # 真正被 import 的上游代码（Apache-2.0）
```

**依赖铁律（沿用 v1，全部可机器检查）**

1. `kernel/` 不得 import `inter/`（门面与实现解耦）；
2. 业务层不得 import `vendor/`；只能通过 `kernel` 取实现；
3. **切换点收敛在 `kernel/registry.py`**，业务代码里不许出现 `if backend == "pjialin"`；
4. 每个功能域的开关**独立**，可逐个切、可一键回退；
5. `vendor/pjialin/` 里的文件**逐字保留上游内容**，适配只写在 `kernel/pjialin/`。

### 5.2 Apache-2.0 合规（必须做，不是可选项）

| 要求 | 做法 |
|---|---|
| 保留版权头 | `vendor/pjialin/` 每个文件保留原头部 |
| 带 `NOTICE` | 新增 `vendor/pjialin/NOTICE`（Copyright the py12306 contributors） |
| 写明修改 | `vendor/pjialin/README.md` 逐文件列出「改了哪几行、为什么」 |
| 许可全文 | `vendor/pjialin/LICENSE`（Apache-2.0 全文） |
| 打包声明 | `TicketHelper.spec` / README 里声明第三方组件 |

### 5.3 分期（先无状态、后只读、最后写操作）

| 期 | 内容 | 为什么这个顺序 |
|---|---|---|
| P0 | `vendor/pjialin/` + 合规文件 + 注册表骨架 | 先立规矩，后搬代码 |
| P1 | 无状态域：站点表、通知、CDN、打码 | 错了不会伤数据 |
| P2 | 只读域：余票查询、乘车人、用户信息 | 有 mock 可对照 |
| P3 | 写域：下单链路 | **最后做**，涉及资金 |
| P4 | 候补 | **等 §5.1 的定义** |
| 不切 | WAF 识别、会话持久化、限速、14 天判断、人证核验、存储 | 上游 0 处，无处可换 |

---

## 六、验收标准（外部信号，不由我定）

| 级别 | 信号 | 谁给 |
|---|---|---|
| A | 每个功能域的「双后端差分表」可打印、可断言（照 v1 `scripts/kernel_diff.py` 的做法） | 离线，本轮可做 |
| A | `vendor/pjialin/` 文件与上游原件 **逐字节一致**（`cmp`），适配层改动逐条列在 README | 离线，可机器检查 |
| A | 撤掉任一适配器 → 对应测试**必须变红** | 离线 |
| B | 真机：切到 pjialin 后端后，登录 / 查余票 / 下单是否仍可用 | **仅真机，由你跑** |
| B | 真机：候补链路的 F1–F4（见 `docs/内核替换对照报告.md`） | **仅真机，由你跑** |

**A 级本轮完成；B 级必须真机，离线测试不能替代 —— 这是本项目 405 事故留下的教训。**

---

## 七、我现在的判断（供你决策，不代替你决策）

1. **候补这条必须由你定**（§4.1）。这是唯一真正卡住的地方。
2. **其余部分按你的要求切上游是可行的**，我建议按 §5.3 的 P0→P3 分期做，每期都能回退。
3. **三件事我建议保留自研、并写进文档明示**：WAF/HTML 识别、会话持久化、14 天预售期。
   它们在 §3 里，上游 0 处 —— 不是我不想换，是没有可换的。
4. 你确认后我从 **P0** 开始（合规文件 + 注册表骨架），不动业务代码。

---

## 八、决策记录与执行状态（2026-09-29）

需求方对 §4 三个问题给出的答复：

| # | 问题 | 决策 | 执行情况 |
|---|---|---|---|
| 1 | 候补出路 | **B —— 改用 `testerSunshine/12306`**（MIT，有完整候补） | ✅ 已落地（见下） |
| 2 | 是否允许请求 pjialin 托管域 | **否**；「如果需要就把源码拿过来自己弄」 | ✅ 方案已定（见 §8.2），P1 期实施 |
| 3 | 是否接受 pyppeteer 真浏览器 | **接受** | ⏳ P3 期（下单链路）实施 |

### 8.1 决策 1 已落地：候补默认切到上游

| 项 | 内容 |
|---|---|
| 上游 | `testerSunshine/12306` @ `a495af88`（MIT），原件逐字存档 `reference/upstream/testerSunshine_standby/` |
| vendor | `vendor/testerSunshine/standby.py` —— **由生成器从上游原件切出，不手抄** |
| 生成/校验 | `python scripts/build_vendor_testerSunshine.py [--check]` |
| 溯源 | `vendor/testerSunshine/PROVENANCE.json`（逐文件 sha256 + 改动清单）；测试 C5c 校验它没过期 |
| 适配 | `ticket_helper/kernel/testerSunshine_builder.py`（**不含协议逻辑**，只转发） |
| 默认 | `DEFAULT_BACKEND = "testerSunshine"`；回退 `TICKET_HELPER_KERNEL_STANDBY_PAYLOAD=self` |
| 契约测试 | `tests/test_kernel_facade.py` C1–C8，**12 项全绿**；变异检验 4/4 抓到 |
| 差分表 | `python scripts/kernel_diff.py` → 4 项差异与文档一致，退出码 0 |

**改动清单（MIT 要求写明，同时记在 `PROVENANCE.json`）**：
删除 `sendChechFace`（它级联 import `verify/` → tensorflow/keras/cv2，Python 3.13 装不上，
且属传输层——本项目传输层不换）；加 `_TickerConfig` 垫片（值逐字取自上游 `TickerConfig.py`）；
把候补分支的 `for` 循环原样移入 `build_passenger_info`（只去行首多余缩进 + 循环前补参数校验）。
**载荷定义本身（字段名 / 顺序 / 拼接格式）一个字没改。**

### 8.2 决策 2 的方案：两处托管依赖都不用自建服务（各改一行/一段）

**① 设备指纹** —— 上游自己就留了「不用托管」的代码路径：

```python
# helpers/api.py:53（上游注释掉了官方地址，换成了自家托管服务）
# API_GET_BROWSER_DEVICE_ID = BASE_URL_OF_12306 + '/otn/HttpZF/logdevice'
API_GET_BROWSER_DEVICE_ID = 'https://12306-rail-id-v2.pjialin.com/'

# query.py:130 / user/job.py:331
if 'pjialin' not in API_GET_BROWSER_DEVICE_ID:
    return self.request_device_id2()          # ← 上游已实现的自建路径
```

⇒ 把 URL 换回**官方地址**即可走上 `request_device_id2()`，**不需要自建服务**。
实施要点：`query.py` / `user/job.py` 用的是 `from ... import API_GET_BROWSER_DEVICE_ID`
（导入的是**值**不是模块），所以运行期改 `helpers.api` 的属性无效 ——
改动必须落在源码上（3 个文件），并记入 `PROVENANCE.json`。

**② 免费打码** —— 托管只提供「格子编号」，换算在本地：

```python
# helpers/OCR.py
response = self.session.post(API_FREE_CODE_QCR_API, data={'img': img})   # 托管：返回编号
...
def get_image_position_by_offset(self, offsets):                          # 本地：编号 → x,y
    width = height = 75
    x = width * ((offset - 1) % 4 + 1) - width / 2 + random_x
    y = height * math.ceil(offset / 4) - height / 2 + random_y
```

⇒ **复用上游的 `get_image_position_by_offset`**（这正是本项目 ddddocr 缺的「点击点换算」），
把托管调用换成 `ddddocr` 本地识别。**不需要自建服务。**
（`OCR.py` 里 `AUTO_CODE_PLATFORM='user'` 分支已支持「换成自己的 URL」，协议一致。）

### 8.3 下一步（P0 → P1）

| 期 | 动作 | 状态 |
|---|---|---|
| P0-a | vendor `testerSunshine` 候补 + 合规 + 门面 + 契约测试 | ✅ 完成 |
| P0-b | vendor `pjialin/py12306` 整包（Apache-2.0：LICENSE / NOTICE / 逐文件改动说明） | ⏳ 下一步 |
| P1 | 无状态域先切：站点表、通知、CDN、打码（**含上面两处托管依赖的去除**） | ⏳ |
| P2 | 只读域：余票查询、乘车人、用户信息 | ⏳ |
| P3 | 写域：下单链路 + pyppeteer（决策 3） | ⏳ |
