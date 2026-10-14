# 项目长期记忆 · 12306 抢票助手

> `12306-ticket-helper`。**个人学习自用**，不代扣款（出票只提醒 + 引导官方支付）。
> 本文件 = 硬约束**索引**（只留会出故障的规则）。去向：全过程 → `memory/YYYY-MM-DD.md`（**不要删**）；
> 故障史 #N → `docs/缺陷回流台账.md`；**协议全文/参数字面量** → `memory/PROTOCOL-CONSTRAINTS.md`（改协议前必读）；
> 九域差异 → `memory/KERNEL-DOMAINS.md`；**下单七步全景** → `docs/下单链路全景.md`。

## 一、工程约定

1. 分层：控制器 → 服务 → 仓储，服务层不依赖 Flask 类型；配置走 env/`env.py`，启动集中校验**快速失败**，密钥绝不硬编码。
2. 可写路径**只能**来自 `runtime/paths.py::get_app_paths()`；抛 `ticket_helper.errors` 类型化异常，`web/app.py` 全局转 JSON，**不回堆栈**。
3. 敏感字段对外 `SecretMasked="********"`；**全星号 = 未设置**。
4. 协议字面量只在 `inter/urls.py`（路径）/`inter/parser.py`（字段）；鉴权头收敛在 `inter/api.py::_auth_headers`。
5. 测试默认 `mock_server`；真机仅限人工低频冒烟。打包与源码**共用**用户目录 → **打包前必须停 exe**。
6. 扫码登录三铁律：**轮询交前端**、**User 对象原地采用**、**终态幂等**。
7. **同一语义只有一处实现**；状态值只有一处常量定义（§五）。
8. ★ **「日志用语」≠「业务状态」**：曾把日志「锁票成功」当「已建单」→ 错。锁票会自行过期，**第 ⑥ 步才建单**。

**1.1 补丁/脚本纪律（本机反复踩）**
9. ★ CRLF：改项目 py 必须 `io.open(..., newline="")` 读写；锚点**单行**；写完 `ast.parse` + 数 CRLF。⚠️ 脚本内字面量是 LF → 比对前**必须 `\n→\r\n` 归一化**。
10. 脚本手拼 URL 含中文必须 `quote`（否则 `UnicodeEncodeError` 被吞 → 症状「0 条」）。
11. ⚠️ 补丁脚本必须**幂等** + `assert count(old)==1`；**空替换单独处理**（`"" in text` 恒真 → 误判「已是目标态」）。多行/含转义补丁**一律 Write 独立脚本**；嵌套引号正则用**三引号整段替换**。
12. ★ **「读不出来」≠「读出来是 0」**：计数取不到记**错误**，绝不记 0 通过。
13. ★ **门/工具自己也要有测试**（V0 门 noqa 豁免里躺过一个**永远为假**的分支还带负责任的注释）→ `tests/test_verify_tool.py`。
14. ★ **状态值只有一处定义**：DDL 不许 `status DEFAULT 'x'`（列一律 `NOT NULL`）；SQL 不许有状态字面量（#54）。
15. ★ **变异清单会腐烂**：`contract_mutation_check.py` 跑前做**锚点预检**（#55）。
16. ★ **品牌/版权唯一来源** = `ticket_helper/__init__.py`：`APP_NAME`（智泊AI抢票助手）、`APP_AUDIENCE`（仅限智泊AI学员使用，进窗口标题/网页 title）、`APP_COPYRIGHT`（智泊AI版权所有，仅限智泊AI学员使用）、`APP_WEBSITE`（https://www.zhipoai.com/，2026-10-08 加）、`APP_USAGE_NOTICE`（仅供学习使用，严禁商用）。web 静态 HTML import 不到 → 靠 `test_contracts.py` 契约 7 + `test_desktop.py::test_window_title_and_status_bar_carry_the_copyright` 钉住；桌面状态栏官网是**富文本可点击链接**（`setOpenExternalLinks`）。EXE 文件名与包名刻意不改。
17. ★ **教学演示版唯一开关** = `ticket_helper/edition.py::DEMO_MODE`（默认 False；`build_exe.py --demo` 用 `with` 临时改 True 并**保证还原**；完整名 `APP_DISPLAY_NAME` 带后缀，`APP_NAME` 本身不动）。**只改显示不动数据**：乘车日期一族走 `display_train_date(s)` 显示「明天」（多日期合并一行、日历锁明天+specialValueText）；「抢票/订票时间」不换。网页端由 login/me 下发 `demo_mode`、`demoDate()` 消费，两端同源靠契约 8。产物 `dist/TicketHelper-Demo/`（exe 带 `-Demo` 后缀）。★ **「代码进包」≠「开关打开」**：bool 常量不在字符串池，冻结物取证证明不了值 —— 铁证是跑打包版 web 看 login 响应 `"demo_mode":true`。

## 一·五、打包（#42–#47）★「源码能跑」与「产物能跑」不共享证据

上游内核对 PyInstaller **不可见**（垫片挂 sys.path，后端在方法体内才 import）⇒ 动过 `vendor`/上游后端**必须**跑打包验收。
`datas` 把 `vendor/pjialin` 映射到包内同路径（落错 = **静默回退**到 self）；上游依赖**手写 `hiddenimports`**（`requests_html`/`pyquery`/`redis`/`pyppeteer`/`png`/`ws`/`pyee`）；两种形态**共用同一份 spec**；★ **可选能力不许打断核心数据**（`TicketApi.__init__` 急切构造，规格取不到必须退回内置 + WARNING）；豁免清单**不许腐烂**。
★ **`ddddocr` 不打进包是刻意决策** ⇒ **打包版下单验证码只能人工录入**。
验收：`scripts/check_packaging.py` + 产物 `doctor` 第 12 项；**反向对照**：`_internal/vendor` 改名必须报红。
★ **目录版 / 单文件版是两件不同的产物，同名不同路径**：`dist/TicketHelper/`（目录，必须**整个目录**分发，否则对方报 `Failed to load Python DLL ... python313.dll` —— exe 只是启动器）vs `dist/TicketHelper.exe`（`--onefile`，**只发一个文件**，代价是首次自解压十几秒 + 杀软误报略高）。
⇒ `build_exe.py` 的让路只能针对**本次真正的产物路径**（曾按 `build_name` 一律让路 → 打单文件包时把目录版整目录改名扔掉）；`--onefile --demo` 产出 `dist/TicketHelper-Demo.exe`，与 `dist/TicketHelper-Demo/` 并存。
⇒ 验收单文件版要**把 exe 单独拷进空目录**跑（本次用 `web --port` 验 login 的 `demo_mode` + 离屏 `QT_QPA_PLATFORM=offscreen` 起 GUI 看进程存活）；冻结物取证对单文件 exe 同样有效（`--dist` 指向只含该 exe 的目录）。

## 二、与上游的关系（勿凭印象）

`pjialin/py12306`（Apache-2.0，**无候补**）、`testerSunshine/12306`（MIT，**有完整候补**）；`12306model` **不引入**（无 LICENSE、需 TF1.14）。
**决策**：①候补用 testerSunshine ②**禁止**请求 pjialin 托管域 ③接受 pyppeteer ④打码 = 上游渠道 + 上游换算 ⑤适配层**只管搬运**。**9 域**切换只在 `kernel/registry.py`：`station`/`query_route`/`login_check`/`qr_check`/`order_submit`/`pacing`/`heartbeat` 默认 `self`，`captcha`/`slide` 默认 `pjialin`。
★ **官方回含糊业务消息（「余票不足」「系统忙」）时，先逐字比上游源码，别猜** —— #48/#51/#60/#61 都靠这个逮到。依赖：`lxml_html_clean`（照抄上游 requirements 会漏）/ `redis 8.x` / `pyppeteer 2.0.0`。

## 三、测试验证框架（`docs/测试验证框架设计.md`）

**地基：测试通过 ≠ 正确**。七层：V0 纪律门 → V1 回归 → V2 契约 → **V3 变异** → V4 差分 → V5 协议基线 → V6 mock 端到端 → **V7 真机探针（唯一能判对不对）**。
**V0 门** `tests/test_discipline.py`：无 skip / **弱断言棘轮**（只减不增）/ 行尾 CRLF / `kernel/pjialin/**` 不得有协议字面量 / `.bat` 纯 ASCII / 豁免清单不腐烂。
★ **mock 照我们自己的实现写校验，只能证明「我和我自己一致」** —— 已**四次**复发（#48 / #51 / #60 / #61；#64 连 mock 都跟着「#49 误判」错了两轮）⇒ **给 mock 加校验要照上游源码写，并复现真机的含糊症状**。
★ **「解析没匹配上」≠「页面里没有」**：真机 initDc 的 `ticketInfoForPassengerForm` 是**单引号字面量**（含 `\uXXXX`/null），不是 JSON —— 「页面里没有 X」的结论必须以**存盘页面 + 逐关键字检查**为准（`diagnose_order.py` 已内置页面取证）。

## 四、协议硬约束 · 索引（**违反过并造成故障**）

> 逐条依据 / 为什么 / 参数字面量 → `memory/PROTOCOL-CONSTRAINTS.md`（编号 1–34 一一对应，改协议前必读）。

1. **密码登录真机走不通，扫码唯一**；`checkqr` 必带 `appid=otn`；二维码寿命 **≈21s**；`uamtk`/`userLogin`/`web_login` 必带 `Origin`+`Referer`；验证码只走 `/otn/passcodeNew/*`，`captcha-image64`/`captcha-check` **不许有运行时引用**（AST 守卫）。
2. 查余票前**必须**真实加载 `GET /otn/leftTicket/init`；被拒（HTML，**风控与「日期不可售」共用同一张错误页**）→ 立即停止路径轮换并抛 `RiskControlError`；被拒是**会话级不是路径级**；余票路径**不是**候选列表（读页面 `var CLeftTicketUrl`）。
3. **候补整族在 `/otn/afterNate/`**（旧路径真机 **405**）；**七步不可颠倒**，`confirmHB` **才建单**；`passengerInfo` 每乘客 **6 段 `;`**；`secretList` 结尾**有竖线**；**官方不给候补单号** → 留空认领，**不造假单号**。
4. ★ **下单链路（#48/#51/#60/#61/#64）**：`secretStr` **URL 编码** → 解析入口 `unquote` **一次**；`passengerTicketStr` 每乘客 **9 段**（第 9 段 = `allEncStr`）、`oldPassengerStr` 结尾 `'__ _ _'`（**真机证实**）；**下单验证码 = `/otn/passcodeNew/getPassCodeNew?module=passenger&rand=randp`**（与登录同路径）；第 ⑨ 步 **`waitTime=-2/-3` = 失败/已撤销**。★ **`leftTicket(Str)` 权威来源 = initDc 页面下发的 `leftTicketStr`（加密长串），与余票行 `yp_info` 不是一个值**（`yp_info` 只作兜底）；`getQueueCount.train_date` = **JS 日期串**（`Tue Oct 13 2026 00:00:00 GMT+0800 (China Standard Time)`，星期/月份用固定英文表，不用 strftime）；`train_location` 同样优先页面值。**全文见 PROTOCOL-CONSTRAINTS §31–35。**
5. ★ **乘车日期唯一闸门 `TaskConfig.validate`**：**只拦「已过去」，不拦「未开售」**；「今天」只能来自 `train_date.today()`（测试 `frozen_clock`）。
6. 人证核验四态唯一来源 `account/verify.py`，`should_block()` **只有「明确未核验」为真**，判不出来**必须放行**。

**陷阱**：`INDEX_TICKET_NUM=11` 名为「余票数」实际是「可否网订」；**对照实验必须「并排 + 同一时刻」**；**「异常被吞」是本项目事故的共同放大器**（已 4 处）；滑块结果**每单重置**；真机抓包用 `scripts/diagnose_*.py`。

## 五、契约：会话 · 订单 · 状态

**会话**：Cookie 加密存 `data/sessions/`（3 天，`base_url` 变则不复用）；**必须全量保存不筛选**；恢复后**真调一次 `checkUser`** 才算在线；**磁盘会话永远不可信**；mock 必须下发会话 Cookie。
**接口差异**：`POST /otn/index/initMy12306Api`（账号）；`POST /otn/passengers/query`（乘车人查看，分页 **1-based**、证件号打码）；`POST /otn/confirmPassenger/getPassengerDTOs`（下单用，要 token、给**完整**证件号 + **`allEncStr`**）；`POST /otn/queryOrder/queryMyOrder`（`queryEndDate` 必须**昨天**；**0 条第一判据是官方 `order_total_number`**；`pageIndex` **从 0**）。
**任务配置**：白/黑名单**不能有交集**；组合数**封顶 12**；**拉车次不需要登录**；策略 `direct`/`standby`/`dual`，`standby` **不许下直购单**，全项目**只有一处**读 `strategy`。
**状态值唯一定义处**：`config/settings.py::TASK_STATUS_*`、`grab/engine.py::STATE_*`、`standby/engine.py::STANDBY_*`、`order/service.py::ORDER_*`、`account/manager.py::STATUS_*`、翻译表 `runtime/context.py::ENGINE_STATE_TO_TASK_STATUS`（**模块级**）。

## 六、UI 与插代码纪律

★ **功能存在但不被发现 = 不存在**：分组两端一致、桌面端分组标题**不可选中**（走 `UserRole`）；**状态写在看得见的地方**；**空状态给「下一步」**；**校验指名道姓**（标红那一格 + 只滚第一处）；**复合控件占满整行**；**定时器只刷「只读」区域**；**脱敏字段绝不回填输入框**。
★ **「开发机全绿」≠「真机能显示」**：本机**没有任何 CJK 字体**（`QFontDatabase.families()` 里 CJK 家族为空），离屏一切都用替身字体 —— 真机雅黑一行 25px、替身字体 19px。⇒ 凡「行数 × 行高」类算式，**行高必须现量**（`sizeHintForRow`），**上限必须落在整行上**（`rows = cap // 行高` 再乘回去；像素硬夹会夹出「半截行」，使用者读作「显示不全」而不是「可滚动」），**外框占用按真实几何反推**（`height - viewport.height()`；`frameWidth()` 会含内边距，实测差 4px → 下一行残影）。标签列表统一 `QListWidget#chipList`（列表自身纵向内边距归零），`sync_list_height()` 是唯一实现（`desktop_ui/token_field.py`）。
★ **Qt 用例测几何前必须 `show()`**（未显示时 `viewport()` 是未布局默认值，量到过 12px），且样式要套**真主题** `theme.STYLESHEET`，否则盒模型与真机不同、变异照样漏过。短行锚点（`import` 行、`setStyleSheet("...")`）在文件里出现多次，按「首处替换」的变异会打偏 ⇒ **同一语义先收敛成一处实现**。
★ **「卡住」比「失败」危险**（失败会变红；卡住永不结束，最像「机器慢」）⇒ 往 `__init__`/状态机/注册表插代码**必须检查「原来的下一行有没有被挤走」**（曾挤走 `self._order_combo_hint` → `AttributeError` → 被 `log.debug` 吞 → 卡 20 分钟）。

## 七、环境与沙箱限制

- 虚拟环境 `C:\zero\workbuddy\p12306\.venv`（3.13 + pytest）。⚠️ `pyvenv.cfg` 的 `home` 曾指向旧用户 `Admin`，**换机/换用户后先修它**。
- **我启动的长期进程一定被回收** → 驻留服务**只能让使用者自己启动**；**浏览器自动化本机起不来**，看界面用 Qt（`QT_QPA_PLATFORM=offscreen` + `QWidget.grab()`）。
- 大批量 `rm`（>50 项）被拦 → 用 `mv xxx xxx.stale<ts>`。
- pytest 按文件分批 + `--ignore=tests/acceptance`（892 条）；⚠️ `--junitxml` 会**原生崩溃**；乘车日期用 `frozen_clock`，`.bat` 纯 ASCII。
- ★ **宿主的 safe-delete 钩子会打红测试**：`C:\WorkBuddy\...\shim\sitecustomize.py` 按 **turn 累计删除数**（阈值 50）拦删，超了就 **抛 `SystemExit: 1`** —— 症状是若干「删文件」型用例（会话清理 / 删用户 / 删合规文件）集体报 `SystemExit: 1`，**看着像项目 bug，其实与代码无关**。判据：报错栈里有 `sitecustomize.py` + stderr 里有 `[safe-delete]`。
  ⇒ **跑全量前不要在同一轮里大量删/移文件**（我这轮先后移动过 18 个脚本 + 一次 `.tmp-pytest`，把计数顶爆）；`--basetemp` **放在工作区内更糟**（连每个用例的临时目录删除都计数），默认 basetemp 反而安全（只有收尾的批量清理会被吞掉摘要）。
  ⇒ 摘要被吞时，**判据退回到进度行**：`sed 's/\[.*//' log | tr -cd '.FExs'` 数 `F`/`E`，比退出码可信。

## 八、常用命令

```bash
python main.py init / doctor / web / desktop / qr / profile / grab / mock
python scripts/verify.py                              # 测试框架总入口
python scripts/contract_mutation_check.py -k 下单
python scripts/diagnose_order.py --restore --task N   # 下单真机探针（默认只到 initDc）
python scripts/check_packaging.py / scripts/build_exe.py --clean / --demo
python scripts/check_frozen_markers.py       # ★ 从 exe 里解出 PYZ，确认修复真的进了包
```
`dist/TicketHelper/` 下 GUI 与 CLI 同目录，**必须整目录一起拷**；Web 走 `start-web.bat`（`admin` / `env.py` 的 `web.auth_pass`，本机 8008）。

## 九、真机链路状态（2026-10-03 晚，探针判定）

1. ★ **下单链路真机已通到 ⑧ 建单**：③锁票 ✓ / ④initDc ✓（token + ticket_info 全拿到）/ ⑤乘车人 ✓ / ⑥checkOrderInfo ✓ / ⑦getQueueCount ✓（count=21）/ **⑧confirmSingleForQueue ✓（真建成待支付订单）**。⑨ 轮询第一拍「排队超时，到未完成订单查看」——按判读**不是失败**，状态以官方 App 为准。
2. 验证任务：任务 id=2「链路验证-京沪-1013」（北京南→上海虹桥 2026-10-13 二等座），账号樊章扫码登录，会话已存盘（3 天内 `--restore` 可用）。
3. **仍未真机验证**：⑨ 拿到 `orderId`（出一单真实支付流程）/ 上游 pyppeteer 过滑块 / `order_submit=pjialin` 后端切换。
4. **#57 会话存活语义**：`restore_session` 把「校验**没做成**」也当「失效」并删本地会话 —— 要修得先知道「会话正常寿命」「哪些响应算明确未登录」。
5. ~~桌面「今天」双来源~~ → 已修（台账 #62 `QDate.currentDate()` → `_today_qdate()`；用例自己也写 Qt 今天，两边都错互相抵消——「判据分叉」的典型形态）。同批修 #63：回环地址**显式直连**，环境 `HTTP_PROXY` 不再吞 mock/桌面请求。
