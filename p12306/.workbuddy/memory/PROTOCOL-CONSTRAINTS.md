# 协议硬约束 · 全文与「为什么」

> 从 `MEMORY.md` §四 拆出来的（那里只留一行一条的**禁止清单**）。
> 需要动手改这些地方时**必须读本文件** —— 每一段都对应一次真实故障，
> 编号与台账 `docs/缺陷回流台账.md` 的 #N 对应。

## 四、协议硬约束（违反过并造成故障，mock 已复现）

**登录/扫码**
1. `checkqr` 必须带 `appid=otn`（否则 `result_code:9`「还没扫就失败」）。
2. 二维码寿命**约 21 秒**（不是 2 分钟）→ `QrLoginSession.auto_refresh`（上限 30；已扫码不换）。
3. `uamtk`/`uamauthclient`/`userLogin`/`web_login` 必须带 `Origin`+`Referer=/otn/passport?redirect=/otn/login/userLogin`
   （否则**不报 4xx**、回 HTML 错误页 →「uamtk 票据内容为空」）。
4. 收尾顺序：扫码 `GET userLogin`→`uamtk`→`uamauthclient`；密码 `POST userLogin`→同上。
5. `checkqr` 的 `uamtk` ≠ `uamtk` 接口的 `newapptk`。
6. 登录验证码走 `/otn/passcodeNew/getPassCodeNew` + 校验 `/otn/passcodeNew/checkRandCodeAnsyn`；被 WAF 挡的
   `captcha-image64`/`captcha-check` **不许有运行时引用**（AST 守卫）；请求体**同时**带 `answer` 与 `randCode`。
   **密码登录真机走不通，扫码是唯一可靠路径**；登录页已上 `nc.js`。

**余票**
7. **查余票前必须真实加载 `GET /otn/leftTicket/init`**（`ensure_session_warm`）；否则被 WAF 拒回
   **HTML 错误页（非 4xx）** →「查询偶发失败」，极难联想。
8. 查询被拒（HTML）**立即停止候选路径轮换**并抛 `RiskControlError`；否则把短暂限流打成持续限流。
9. 上游余票路径**不是**候选列表：从余票页 HTML 的 `var CLeftTicketUrl` 读。
10. `INDEX_TICKET_NUM = 11` 名字是「余票数」但实际与 `'Y'` 比较（=可否网订）→ **名字会骗人，取值不会**。

**排障通则**
11. ①「回 HTML 而非 JSON」= 请求不被接受（头/会话），不是业务错误；②被拒是**会话级不是路径级**；
    ③**对照实验必须「并排 + 同一时刻」**；④未登录调鉴权接口回 `{"result_code":1,…未登录}` = 请求已被接受
    （免扫码验证法）；⑤抓报文用 `scripts/diagnose_*.py`。

**人证核验（AC-07）**
12. 官方：候补前需人证一致性核验，须**本人 App 人脸识别**；真机两个用户信息接口都**不返回**核验字段
    → `verify_known` 恒 False。
13. **四态语义唯一来源 = `account/verify.py`**；`should_block()` **只有「明确未核验」为真**；
    判不出来**必须放行**（曾写 `if not is_checked: 拦` → 正常账号被**永久**挡在候补外）。
14. 出路三件套：通知带 App 路径 → 桌面端「人证核验怎么做？」→ 网页端账号页折叠说明；
    另 `POST /api/users/<id>/verify/refresh` + 两端「重测核验」按钮。

**候补（整族在 `/otn/afterNate/`，不在 `confirmPassenger` 下）**
15. 旧路径 `/otn/confirmPassenger/confirmHB` 真机回 **HTTP 405**（日志只有「响应不是合法 JSON (HTTP 405)」）。
    **七步不可颠倒**（`inter/urls.py::HB_*`，统一 Referer `/otn/leftTicket/init`）：① `chechFace`（**核验唯一权威信号**
    `login_flag`/`face_flag`）② `getSuccessRate` ③ `submitOrderRequest` ④ `passengerInitApi`（**顺序敏感**）
    ⑤ `getQueueNum` ⑥ `confirmHB`（**这一步才创建候补单**）⑦ `queryQueue`（`status==1` 待支付）。
16. `secretList = f"{secretStr}#{席别码}|"`（**有竖线**）；`successSecret` 无竖线；少一个 → 官方回「系统忙，请稍后重试」。
17. `confirmHB` 的 `passengerInfo`：**每乘客 6 段 `;`** = `<票种>#<姓名>#<证件类型>#<证件号>#<allEncStr>#<是否60岁>;`
    与普通下单 `passengerTicketStr` **完全不同**；`hbTrain=f"{train_no},{席别码}#"`、`jzParam/lkParam/sessionId/sig/encryptedData`
    空串、`if_receive_wseat=N`、`realize_limit_time_diff=1440`。
18. **官方不给候补单号** → `hb_order_id` 留空，由「未完成订单」认领，**不造假日单号**。
19. 候补目标必须**当前可网订**（停售/未起售的 `secretStr` 是**空**的）。
20. `VerifyRequiredError` 重试无用，必须给 App 路径出路。
21. `scripts/diagnose_standby.py --restore [--task N] [--date] [--until-confirm]`；**别在真机反复探测**。

**打码（云打码整体删除；改成上游 `user` 模式 + 上游换算）**
22. `codec.mode` 合法值 = free/local/manual（`cloud` 已删）；`codec.user_endpoint` 留空 → `http://127.0.0.1:8899/check`。
23. `free` = 跑上游 `user` 模式：域 `captcha`（默认 `pjialin`）POST 图 → 拿回**格子编号** → 由**上游**
    `get_image_position_by_offset` 换算 x,y（`75`、4 列 2 行、±5 抖动）；本机服务
    `python -m ticket_helper.verify.captcha_server`（ddddocr）负责「图 → 编号」。
24. ⚠️ 上游 `Request.request()` **把连接失败吞成空响应** → 区分不出「不可达」与「协议不对」。
    （**「异常被吞」是本项目事故的共同放大器**：云打码/`FreeSolver`/notify/`_check_fulfilled_by_order`，已 4 处。）
25. `order` 型验证码上游**没有实现**，`pjialin` 后端如实降级；登录链本就不需要打码。

**下单滑块（A13）**
26. `initDc` 页 `var if_check_slide_passcode='1'` 才需要；`self` 是**不可用的回退后端**
    （`available()` 假、`solve()` 返 None 且**不抛**，调用方留 WARNING 照旧走 —— **回退不引入新失败点**）。
27. `HttpClient.dump_cookies()` 必须**全量**且每项有 `url`/`domain`（pyppeteer `setCookie` 否则直接抛）。
28. 滑块结果**每单重置**（上一单的 `sessionId`/`sig` 带到下一单必被拒，而拒绝话术仍是「系统忙」）。

**乘车日期可查性（唯一定义处 `ticket_helper/train_date.py`）**
29. ⚠️ 12306 用**同一张 HTML 错误页**表达两件事：①真风控 ②日期不在可售范围。实测只有 `[今天, 今天+14]` 可查。
    日期规则**只在 `train_date.py`**；「今天」只能来自 `train_date.today()`（测试用 `frozen_clock`）；
    「已过去」可拦（0 请求），「未开售」**只定性不拦**；全部日期已过 → **拒绝启动** + 出路文案。
30. `stats.queries` 记**尝试次数**（自增放在请求**之前**）；「本轮结论」用 `_log_round` 按原因去重打 INFO；
    看板 `task_remain` 必须带 `tickets/last_query/empty_reason/date_window`（否则四种成因长成同一句空话）。
**下单链路（2026-09-30 / 2026-10-03 真机实测回流 #48/#51/#60/#61）**

> 七步：`submitOrderRequest`（锁票，**不建单**）→ `initDc`（拿 `REPEAT_SUBMIT_TOKEN`）→ 下单验证码 →
> `checkOrderInfo` → `getQueueCount` → `confirmSingleForQueue`（**唯一真的建单**，产生待支付订单）→
> `queryOrderWaitTime`（轮询出票，拿 `orderId`）。全流程见 `docs/下单链路全景.md`。

31. ★ **「取值来源」是本链路最容易错的维度**（同名字段在不同步骤来源不同）：
    - 余票行是**定长 `|` 分隔 37 字段**：`0`=`secretStr`、`11`=`can_web_buy`、**`12`=`yp_info`（= `leftTicketStr`）**、
      `15`=`seat_types`（`O9M3`）、`16`=`train_location`（= `location_code`）。
    - 第 ⑤ `getQueueCount` / 第 ⑥ `confirmSingleForQueue` 的 `leftTicket(Str)` **必须取下标 12（`yp_info`）**，
      ⚠️ 曾误取下标 15 的席别列表 `O9M3` → 官方只回「系统忙，请稍后重试」（#61a）。
    - 余票响应的 `secretStr` 是 **URL 编码**（84/84 行带 `%2F`）→ **必须在解析入口 `unquote` 一次**；
      不解码的症状是官方只回「提交失败，请重试...」（#48）。
    - `passengerTicketStr` 每乘客 **9 段**，第 9 段 = 乘车人 `allEncStr`（来自 `getPassengerDTOs`）；
      `oldPassengerStr` 拼完须 `rstrip("_") + "__ _ _"`（#51，另一个独立实现也确认此格式）。
      ⚠️ 我们目前是 8 段 / `…,1_`，**与 `checkOrderInfo` 回「余票不足！」的关系尚未证实**，未擅自改（涉资金路径）。
32. ★ **下单图形验证码 = `/otn/passcodeNew/getPassCodeNew?module=passenger&rand=randp`**
    （**与登录验证码同路径**，靠 `module`/`rand` 区分）。曾写成 `/otn/confirmPassenger/getPassCodeNew`（上游无此路径）
    + `rand=sjrand` → **图片永远拿不到**（#60）。错路径/错 `rand` 真机只回 **HTML 错误页**，不是 4xx。
33. ★ 第 ⑦ 步 `queryOrderWaitTime`：**`waitTime=-2` = 排队失败、`-3` = 订单已撤销**（判读逐字照上游
    `order/order.py:549`）。不认负数语义就会把**确定的失败**报成「排队超时，请到未完成订单查看」（#61b）。
34. `key_check_isChange` 唯一来源是 `initDc` 的 `ticketInfoForPassengerForm`；
    ⚠️ **现代页面可能不再下发该结构**（#49 的「页面里没有」**证据不足**，只证明「我们的解析没匹配到」）。
    处置：多形态匹配 + 整页抢救 + WARNING；探针 `diagnose_order.py --until initdc` 存页面到 `<home>/export/`
    并报告 7 个候选关键字（把「猜」变成「看」）。三种可能见 `docs/下单链路全景.md` §5。
35. ★（#64，真机 2026-10-03 判定）**取值来源的权威级**：`leftTicket(Str)` 的权威来源是
    **initDc 页面下发的 `leftTicketStr`**（82 字符 URL 编码加密串，如 `M0fCbVVx...%2F...%3D`），
    **与余票行 `yp_info` 不是一个值** —— #61 的「= yp_info」只对了一半（字段下标对了，
    来源错了）；`yp_info` 仅作页面未下发时的兜底。`train_location` 同样优先页面值（真机 `P4`）。
    `getQueueCount.train_date` 必须**官方浏览器同款 JS 日期串**
    （`Tue Oct 13 2026 00:00:00 GMT+0800 (China Standard Time)`）；
    发 `YYYY-MM-DD` 被回「系统忙，请稍后重试」。星期/月份用**固定英文表**（strftime 受 locale 影响）。
    `key_check_isChange` 真机**有下发**（64 位十六进制）—— #34 的「现代页面可能不下发」被推翻：
    真相是**单引号字面量解析失败被误判成「页面没有」**；整页抢救可从单引号字面量捞出全部 4 值。
    真机验收：③–⑧ 全通（⑧ 真建成待支付订单），⑨ 第一拍「排队超时到未完成订单查看」（按判读非失败）。
36. ★（真机 2026-10-03）**未完成订单查询**：不在 `queryMyOrder(G)` 结果里，必须走三步
    `POST /otn/queryOrder/initNoComplete` → `POST /otn/queryOrder/queryMyOrderNoComplete`
    （取 `data.orderDBList`）→ 取消 `POST /otn/queryOrder/cancelNoCompleteMyOrder`
    （`sequence_no`+`cancel_flag=cancel_order`，成功=`data.existError=="N"`；三条 Referer 都是
    `/otn/queryOrder/initNoComplete`）。出处 testerSunshine `QueryOrderWaitTime.py` 逐字。
    **排队超时的单官方会自动释放**，不会出现在任何订单列表——别为它写取消逻辑。
