# 内核九域差异 & 门面分工（从 `MEMORY.md` §二 拆出）

> 动手动 `kernel/` 或切换后端**前必读**。本文件逐字核对上游得出；**差异不抹平**。
> 上游声明与依赖见 `MEMORY.md` §二；协议硬约束编号见 `MEMORY.md` §四 / `PROTOCOL-CONSTRAINTS.md`。

## 一、九域差异总表

| 域 | self（默认） | pjialin（上游） | 切过去意味着 |
|---|---|---|---|
| `station` | 官方 3372 站 | **2021 快照，11 站电报码不同**（信阳 XYN/XUN、抚顺 FET/FST、乌海 IAC/WVC、天门 TKN/TMN、宜城 YYN/YIN、五原 IYC/WYC、乌拉特前旗 IQC/WQC、乌鲁木齐南 WNR/WMR、烟台西 DCK/YTK、石嘴山 OZJ/QQJ、黄陵 KLY/ULY） | 「查不到车」，**极难联想** → **永不切** |
| `query_route` | 候选列表 | 读余票页 `var CLeftTicketUrl` | 更省请求，需真机证据 |
| `login_check` | `POST /otn/login/checkUser` + `data.flag` | `GET /otn/login/conf` + `data.is_login=='Y'`（**不带 Referer**） | 判活字段互不认得，须连响应结构一起换 |
| `qr_check` | 表单不带设备指纹 | 多带 `RAIL_DEVICEID`/`RAIL_EXPIRATION` | 双保险，差异留档 |
| `order_submit` | 每步带 Referer；多传 `undefined`；`back_train_date`=今天；`cancel_flag`="2"(str)；`random`=毫秒时间戳；`purpose_codes` 写死 "00" | **六步都不带 Referer**；无 `undefined`；`back_train_date`=出发日；`cancel_flag`=2(int)；有**滑块三件套**；`random`=随机小数串；`purpose_codes` 动态 | 上游很可能被 WAF 拒；**默认保持 self** |
| `pacing`(A17) | 1.0~8.0s（自适应）/ 超时 12s | 0.5~1.0s（固定区间随机）/ 超时 5s | **请求密一倍以上 → 更容易被限流** |
| `heartbeat`(A15) | 采纳账号配置，**地板 30s** | 固定 120s，**无地板** | **去掉一层防护** |
| `captcha` | 字段 `image` + `{"answer":"x,y"}` | 字段 `img` + `{"msg","result":[格子编号]}` | **协议不同，混用必失败**（老「假实现」根因） |
| `slide`(A13) | **无程序化解法**（不可用的回退后端） | 上游 pyppeteer 真拖拽 `#slide_passcode` | 上游有、我们没有 → 默认就是 pjialin |

**上游没有的（出路已定）**：候补整族 → `testerSunshine`；我的订单 → 部分用 `testerSunshine`；
`checkUser` → 请求用上游、判定保留；人证核验 → 信号用 `chechFace`、四态保留；
WAF/HTML 识别、会话持久化、退避自适应、14 天预售期、本地存储 → **全部保留自研**
（换过去会同时失去登录与风控识别）。

## 二、内核门面分工（分层规则是「按目录」）

| 目录 | import `inter/` | import `py12306` | 职责 |
|---|---|---|---|
| `kernel/domains/`、`kernel/*.py`、`registry.py` | ❌ | ❌ | Protocol / 候补门面 / **唯一切换点** |
| `kernel/self/` | ✅ 可以 | ❌ | 适配本项目实现 |
| `kernel/pjialin/` | ❌ | ✅ 可以 | 适配上游（**不得含协议字面量**，含 `shim`） |

- ⚠️ `kernel/__init__.py` **不许**顶层 import `registry`（会成环）；用法 `from ticket_helper.kernel import registry`。
- ⚠️ 门面定式：**规格（数据）+ 共享的判定/组装纯函数**；`build_form`/`alive_of` 这类**只能有一处**；
  数据放协议层或生成物，**适配层只搬运**。
- ⚠️ **`shim` 必须 append 而不是 insert(0)**：vendor 根目录有 `main.py`，本项目也有 —— 插到最前面会让
  `import main` 拿到**上游入口**，报错却是 `ModuleNotFoundError: flask_jwt_extended`（回流 #36 / #52）。
- ⚠️ `shim.install()` 把 vendor **永久**挂上 `sys.path` → 断言「它不在 path 里」的测试都是
  **顺序依赖的假测试**（已修）。
- ⚠️ **I-1 垫片**：sys.path 注入 + 可写路径重定向到用户目录 + 屏蔽线程/集群/远端同步；副作用必须在
  **首次构造 `Config()` 之前**屏蔽（`singleton` 之后改类属性无效）；上游 `Config.__init__` 会
  `get_file_modify_time(CONFIG_FILE)`，**文件不存在会抛异常**，须先造一个空文件。
