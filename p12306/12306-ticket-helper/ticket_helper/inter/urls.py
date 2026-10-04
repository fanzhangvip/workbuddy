"""12306 接口地址注册表。

**单一改动点**：12306 的路径与参数会随版本调整，全部收敛到本文件，
接口升级时只需改这里（对应技术设计「预留接口适配层」的风险对策）。
"""

from __future__ import annotations

# 下单六步的**步骤名**只有一处定义（`kernel/domains/order_submit.py`）。
# 这里只借用它们的名字当键 —— 方向是 inter → kernel.**domains**（纯数据 + 纯函数，
# 它自己不 import 任何 inter，由 `tests/test_kernel_facade.py::test_c1` 守住），
# 所以不会成环；**反向**（domains import inter）是被明令禁止的。
from ticket_helper.kernel.domains.order_submit import (
    STEP_CHECK_ORDER_INFO,
    STEP_CONFIRM_SINGLE_FOR_QUEUE,
    STEP_GET_QUEUE_COUNT,
    STEP_INIT_DC,
    STEP_QUERY_WAIT_TIME,
    STEP_SUBMIT,
)

#: 官方站点
BASE_OFFICIAL = "https://kyfw.12306.cn"

#: 静态资源（站名表）
STATION_NAME_JS = "/otn/resources/js/framework/station_name.js"

# --------------------------------------------------------------------------- #
# 登录 / 会话
# --------------------------------------------------------------------------- #
LOGIN_CONF = "/otn/login/conf"                       # 登录页配置（含 is_uam_login）

#: 登录验证码 —— **2026 版登录页实际在用的路径**（返回图片字节流）。
#: 依据：`login_new_v20260207.js` 里该接口是「本地登录」组的活跃项：
#:   'getPassCodeNew': popup_baseUrl + popup_publicName + '/passcodeNew/getPassCodeNew?module=login&rand=sjrand&'
LOGIN_PASS_CODE = "/otn/passcodeNew/getPassCodeNew"

#: 登录验证码 —— 旧 passport 路径（返回 base64 JSON）。
#: 真机实测已**不再被接受**：任何请求头组合都返回 WAF 的 HTML 错误页
#: （"网络可能存在问题，请您重试一下！"）。保留它仅作兜底与回归对照，
#: 新流程请用 LOGIN_PASS_CODE。
CAPTCHA_IMAGE_64 = "/passport/captcha/captcha-image64"

#: 登录验证码校验。旧路径同样已失效，新流程与下单验证码共用
#: ``/otn/passcodeNew/checkRandCodeAnsyn``（见 CHECK_RAND_CODE）。
CAPTCHA_CHECK = "/passport/captcha/captcha-check"
WEB_LOGIN = "/passport/web/login"                        # 账号密码登录
UAMTk = "/passport/web/auth/uamtk"                       # 换取 uamtk
UAMAUTH_CLIENT = "/otn/uamauthclient"                    # 客户端鉴权（写 Cookie）
USER_LOGIN = "/otn/login/userLogin"                      # 旧版登录
CHECK_USER = "/otn/login/checkUser"                      # 检测当前会话是否登录
LOGIN_OUT = "/otn/login/loginOut"

#: 扫码登录（12306 App 扫二维码，免密码登录）
CREATE_QR = "/passport/web/create-qr64"                  # 生成登录二维码（返回 uuid + base64 图片）
CHECK_QR = "/passport/web/checkqr"                       # 轮询扫码状态（0待扫/1已扫/2成功/3过期）
#: 扫码接口要求的 appid（官方固定值）。**唯一真源** —— 少了它官方不报错，
#: 只回 ``{"result_message":"操作失败","result_code":9}``，极难归因（真机踩过）。
QR_APPID = "otn"

#: `checkqr` 表单里要放的键与固定值（唯一真源）。
#: 调用方按 ``form_keys`` 顺序取值：``uuid`` 由参数给，``appid``/``_json_att`` 取固定值。
QR_CHECK_FORM_KEYS = ("uuid", "appid", "_json_att")
QR_CHECK_CONSTANTS = {"appid": QR_APPID, "_json_att": ""}

#: 心跳 / 用户状态（会话保活 AC-03）
QUERY_USER_INFO = "/otn/modifyUser/initQueryUserInfoApi"

#: 个人中心首页接口（账号信息：姓名、手机、邮箱、核验状态等）。
#: **方法上有个坑**：路径里带 `init` 字样，但真机与上游实现都用 **POST**
#: （见 ``TicketApi.get_user_profile``，先 POST 后回退 GET）。
USER_INFO_JSON = "/otn/index/initMy12306Api"
#: 个人中心页面（账号信息的 Referer）
MY_12306_PAGE = "/otn/index/initMy12306"

# --------------------------------------------------------------------------- #
# 乘车人（个人中心查看用）
# --------------------------------------------------------------------------- #
#: 常用联系人列表。**分页**接口，`pageIndex` 从 1 开始、`pageSize` 官方页面用 10。
#: 与下单链路用的 ``GET_PASSENGER_DTOS`` 有两点关键差异（别混用）：
#: 1) 这条不需要 ``REPEAT_SUBMIT_TOKEN``，也不依赖「先锁票」；
#: 2) **证件号是打码的**（形如 ``110101********1234``）。
#:    下单必须用完整证件号，所以抢票链路仍然走 ``GET_PASSENGER_DTOS``。
PASSENGERS_QUERY = "/otn/passengers/query"
#: 常用联系人页面（Referer）
PASSENGERS_PAGE = "/otn/passengers/init"

# --------------------------------------------------------------------------- #
# 余票查询
# --------------------------------------------------------------------------- #
#: 余票页本身。**必须先真实加载一次再调 query 接口**（真机实测）：
#: 同一时刻并排对照，从未加载过页面的会话调 query 会被 WAF 拒绝并回 HTML 错误页，
#: 而先加载一次本页之后立刻正常。浏览器天然满足这个条件（用户总是先打开页面），
#: 脚本必须自己补上，见 ``TicketApi.ensure_session_warm``。
LEFT_TICKET_INIT = "/otn/leftTicket/init"

#: 登录态探测的**请求规格**（唯一真源，四个值只在这里写一次）。
#:
#: 为什么落在协议层而不是内核层：`TicketApi.check_user` 在**没有被注入策略**时
#: 也要能用（接口层不许 import 具体后端 —— `kernel/self` → `inter` 是允许的方向，
#: 反向会成环）。所以规格值必须在同层可取；`kernel/self/login_check.py` 引用
#: **同一份**，两处绝不各写一遍。
#:
#: 分叉的症状是「切了后端但请求没变」—— 不会有任何报错，只会让人觉得开关坏了。
CHECK_USER_PROBE = {
    "path": CHECK_USER,
    "method": "post",
    "data": {"_json_att": ""},
    # 真机上「没逛过页面」的会话会被 WAF 拒（回 HTML 错误页而不是 4xx），
    # 所以带同源 Referer。上游那条 `GET /otn/login/conf` **没带**，
    # 差异如实保留，由 `scripts/login_check_diff.py` 显示出来。
    "referer": LEFT_TICKET_INIT,
}

#: 注意：queryZ / queryA / query 为官方灰度轮换路径，接口层支持候选列表依次重试
LEFT_TICKET_QUERY_CANDIDATES = (
    "/otn/leftTicket/queryZ",
    "/otn/leftTicket/queryA",
    "/otn/leftTicket/query",
    "/otn/leftTicket/queryO",
)
LEFT_TICKET_PRICE = "/otn/leftTicket/queryTicketPrice"
LEFT_TICKET_QUERY_BY_TRAIN = "/otn/leftTicket/queryTicketPrice"

# --------------------------------------------------------------------------- #
# 下单
# --------------------------------------------------------------------------- #
SUBMIT_ORDER_REQUEST = "/otn/leftTicket/submitOrderRequest"   # 提交订单请求（锁票）
INIT_DC = "/otn/confirmPassenger/initDc"                      # 初始化下单页（拿 token）
CHECK_ORDER_INFO = "/otn/confirmPassenger/checkOrderInfo"     # 校验订单信息
GET_QUEUE_COUNT = "/otn/confirmPassenger/getQueueCount"       # 排队人数
CONFIRM_SINGLE_FOR_QUEUE = "/otn/confirmPassenger/confirmSingleForQueue"  # 确认排队
QUERY_ORDER_WAIT_TIME = "/otn/confirmPassenger/queryOrderWaitTime"        # 排队结果
RESULT_ORDER_QUEUE = "/otn/confirmPassenger/resultOrderForDcQueue"        # 排队完成回调
GET_PASSENGER_DTOS = "/otn/confirmPassenger/getPassengerDTOs"             # 乘车人列表
#: 下单图形验证码（`module=passenger&rand=randp`）。
#:
#: ⚠️ **与登录验证码是同一个路径**，靠 `module`/`rand` 区分（回流 #60）：
#: 原实现把它写成 ``/otn/confirmPassenger/getPassCodeNew``，而两个上游的地址表里
#: **都没有这条路径**（`reference/upstream/testerSunshine_urlConf.py:334` 的
#: ``codeImgByOrder`` = ``/otn/passcodeNew/getPassCodeNew?module=passenger&rand=randp``，
#: 社区抓包同形）。真机于是只回一张 HTML 错误页 ⇒ 图片永远拿不到 ⇒ 每次都退到人工兜底，
#: 而人工弹窗里显示的正是那张 HTML 的字节（症状「验证码图片无法显示」，见 #59）。
#: 改回本条后，该路径**落进上游基线**（`upstream_endpoints.json` 有 passcodeNew/getPassCodeNew）。
GET_PASS_CODE_NEW = "/otn/passcodeNew/getPassCodeNew"                     # 下单图形验证码
#: 下单验证码的 `rand`。上游 ``codeImgByOrder`` 写死 ``randp``；登录那条才是 ``sjrand``。
#: 取值错误同样只会得到 HTML 错误页（真机形状由 mock 复现）。
ORDER_PASS_CODE_RAND = "randp"
CHECK_RAND_CODE = "/otn/passcodeNew/checkRandCodeAnsyn"                   # 下单验证码校验

#: 下单六步的**请求规格** —— **单一定义处**。
#:
#: 为什么放在协议层：接口层（`inter/api.py`）与「本项目后端」
#: （`kernel/self/order_submit.py`）都要读它，而接口层**不许** import 具体后端
#: （会成环）。放在这里两边都只是**读同一份数据**，不存在「两处各写一遍」。
#:
#: 每个步骤：
#:   ``path``   站内路径
#:   ``method`` ``post`` / ``get``
#:   ``referer`` 同源 Referer 路径（空串 = 不带）
#:   ``keys``   表单字段名，**顺序 = 上报顺序**
#:   ``const``  固定取值
#:   ``alias``  键 → 从「取值袋」里的哪个名字取（默认同名）
#: 不在 ``const`` 里的键由调用方从取值袋里给（见 `TicketApi._order_context`）。
#:
#: ⚠️ ``tour_flag`` / ``randCode`` / 各类 ``*Ticket*`` **刻意不放进 ``const``**：
#: 它们是（或将来可能是）调用方传进来的参数，写成常量会让参数**静默失效** ——
#: 表现是「传了没用」，而官方只会回一句「系统忙，请稍后重试」。
#:
#: 与上游的差异见 `kernel/domains/order_submit.py` 的模块 docstring 与
#: `scripts/order_diff.py`：**差异逐条留档，不抹平**。
ORDER_STEP_FORMS: dict[str, dict] = {
    STEP_SUBMIT: {
        "path": SUBMIT_ORDER_REQUEST,
        "method": "post",
        # 上游这条不带 Referer；本项目带余票页 —— 真机上「没逛过页面」会被 WAF 拒
        "referer": LEFT_TICKET_INIT,
        "keys": ("secretStr", "train_date", "back_train_date", "tour_flag", "purpose_codes",
                 "query_from_station_name", "query_to_station_name", "undefined"),
        # `undefined` 是本项目**多传**的（上游没有）；`purpose_codes` 两边都固定 ADULT
        "const": {"purpose_codes": "ADULT", "undefined": ""},
        "alias": {},
    },
    STEP_INIT_DC: {
        "path": INIT_DC,
        "method": "post",
        "referer": INIT_DC,
        "keys": ("_json_att",),
        "const": {"_json_att": ""},
        "alias": {},
    },
    STEP_CHECK_ORDER_INFO: {
        "path": CHECK_ORDER_INFO,
        "method": "post",
        "referer": INIT_DC,
        "keys": ("cancel_flag", "bed_level_order_num", "passengerTicketStr", "oldPassengerStr",
                 "tour_flag", "randCode", "whatsSelect", "_json_att", "REPEAT_SUBMIT_TOKEN"),
        # ⚠️ 上游同一字段写的是**整数** 2，这里写字符串 "2"：表单编码后都是 `2`，
        #    但差异要留在能被看见的地方（由基线测试逐项对照）。
        "const": {"cancel_flag": "2", "bed_level_order_num": "000000000000000000000000000000",
                  "whatsSelect": "1", "_json_att": ""},
        "alias": {},
    },
    STEP_GET_QUEUE_COUNT: {
        "path": GET_QUEUE_COUNT,
        "method": "post",
        "referer": INIT_DC,
        "keys": ("train_date", "train_no", "stationTrainCode", "seatType", "fromStationTelecode",
                 "toStationTelecode", "leftTicket", "purpose_codes", "train_location",
                 "_json_att", "REPEAT_SUBMIT_TOKEN"),
        "const": {"purpose_codes": "00", "_json_att": ""},
        "alias": {},
    },
    STEP_CONFIRM_SINGLE_FOR_QUEUE: {
        "path": CONFIRM_SINGLE_FOR_QUEUE,
        "method": "post",
        "referer": INIT_DC,
        "keys": ("passengerTicketStr", "oldPassengerStr", "randCode", "purpose_codes",
                 "key_check_isChange", "leftTicketStr", "train_location", "choose_seats",
                 "seatDetailType", "whatsSelect", "roomType", "dwAll", "_json_att",
                 "REPEAT_SUBMIT_TOKEN"),
        "const": {"purpose_codes": "00", "seatDetailType": "000", "whatsSelect": "1",
                  "roomType": "00", "dwAll": "N", "_json_att": ""},
        "alias": {},
    },
    STEP_QUERY_WAIT_TIME: {
        "path": QUERY_ORDER_WAIT_TIME,
        "method": "get",
        "referer": INIT_DC,
        "keys": ("random", "tourFlag", "_json_att", "REPEAT_SUBMIT_TOKEN"),
        "const": {"_json_att": ""},
        # 本项目用**毫秒时间戳**；上游用随机小数串（`str(random.random())[2:]`）。
        # 两种取值来源都摆在台面上，切后端不会因为「字段同名」而误以为一样。
        "alias": {"random": "random_ms"},
    },
}

# --------------------------------------------------------------------------- #
# 下单滑块风控（A13）
# --------------------------------------------------------------------------- #
#: ``initDc`` 页里判「这一单要不要过滑块」的正则。
#: 上游原件里它写作 ``r"var if_check_slide_passcode.*='(\d?)'"``
#: （逐字切出在 `vendor/pjialin/ticket_facts.py::ORDER_SLIDE_STATE_REGEX_SOURCE`）。
#: 这里存的是**求值后的模式**，两者必须一致 —— 由
#: `tests/test_kernel_slide.py::test_the_two_backends_compile_the_same_pattern` 钉住。
ORDER_SLIDE_STATE_PATTERN = "var if_check_slide_passcode.*='(\\d?)'"
#: 滑块 DOM 选择器（上游用 ``page.waitForSelector`` 等它出现）。
ORDER_SLIDE_SELECTOR = "#slide_passcode .nc-lang-cnt"
#: 过滑块后随 ``checkOrderInfo`` 一起提交的场景名（上游写死这个值）。
ORDER_SLIDE_SCENE = "nc_login"

# --------------------------------------------------------------------------- #
# 订单
# --------------------------------------------------------------------------- #
QUERY_MY_ORDER_NO_COMPLETE = "/otn/queryOrder/queryMyOrderNoComplete"   # 未完成订单（待支付）
#: 我的订单（分页查询）。参数见 ``TicketApi.query_order_history``：
#: ``queryType`` 1 按订票日期 / 2 按乘车日期；``query_where`` G 未出行、H 历史；
#: ``come_from_flag`` my_order 全部 / my_resign 可改签 / my_refund 可退票。
QUERY_ORDER_PAGE = "/otn/queryOrder/queryMyOrder"
#: 未完成订单页面（Referer）
QUERY_ORDER_NO_COMPLETE_PAGE = "/otn/queryOrder/queryMyOrderNoComplete"
PAY_GATEWAY = "https://kyfw.12306.cn/otn/payOrder/initPay"

# --------------------------------------------------------------------------- #
# 候补（HB-01~HB-06）
#
# ⚠️ **候补接口不在 confirmPassenger 下，而在 ``/otn/afterNate/`` 下**
# （2026-09-29 真机实测）。原来这里写的是 `/otn/confirmPassenger/confirmHB`，
# 真机直接回 **HTTP 405**（路径不存在），于是「候补一直提交失败」而日志只有一句
# 「响应不是合法 JSON (HTTP 405)」—— 看不出是路径写错了。
#
# 真机验证：`afterNate/getQueueNum`、`afterNate/queryQueue`、`afterNate/chechFace`
# 都返回合法 JSON（status:true），而 `confirmPassenger/confirmHB` 是 405。
# 排障脚本：``scripts/diagnose_standby.py``。
#
# 完整链路（顺序不能颠倒）：
#   chechFace（登录/核验标志）→ getSuccessRate（成功率）→ submitOrderRequest（提交需求）
#   → passengerInitApi（乘车人初始化）→ getQueueNum（排队前置）→ confirmHB（确认订单）
#   → queryQueue（排队轮询，status=1 即候补单生效待支付）
# --------------------------------------------------------------------------- #
#: 统一 Referer：候补全流程都从余票页发起
HB_REFERER = "/otn/leftTicket/init"
HB_CHECK_FACE = "/otn/afterNate/chechFace"              # ① 登录/人脸核验标志（**核验的唯一可靠信号**）
HB_SUCCESS_RATE = "/otn/afterNate/getSuccessRate"       # ② 候补成功率（level/info）
HB_SUBMIT_REQUEST = "/otn/afterNate/submitOrderRequest"  # ③ 提交候补需求
HB_PASSENGER_INIT = "/otn/afterNate/passengerInitApi"   # ④ 候补乘车人初始化
HB_QUEUE_NUM = "/otn/afterNate/getQueueNum"             # ⑤ 排队前置检查
HB_CONFIRM = "/otn/afterNate/confirmHB"                 # ⑥ 确认候补订单（设置订单信息）
HB_QUERY_QUEUE = "/otn/afterNate/queryQueue"            # ⑦ 排队轮询
#: 候补结果落在「未完成订单」里（候补单生效后即待支付订单）
QUERY_HB_RESULT = "/otn/queryOrder/queryMyOrderNoComplete"
#: 取消候补：**真机未验证**。候补单取消走改签/退票那条链，等真机确认后再改；
#: 调用处必须容忍失败（取消不掉也不该影响主流程），见 ``TicketApi.cancel_hb``。
CANCEL_HB = "/otn/queryOrder/cancelNoCompleteMyOrder"

# --------------------------------------------------------------------------- #
# 其他
# --------------------------------------------------------------------------- #
CHECK_TRAIN_AVAILABILITY = "/otn/leftTicket/queryTrainStop"
TRAIN_STOP_STATIONS = "/otn/czxx/queryByTrainNo"   # 经停站信息（QY-07）
CHECK_VERIFY_STATUS = "/otn/modifyUser/initQueryUserInfoApi"
