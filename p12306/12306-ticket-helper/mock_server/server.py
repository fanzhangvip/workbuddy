"""mock-12306 服务。

用途（对应测试方案 1.2 / 第五节）：
- 让「登录 → 查询 → 抢票 → 排队 → 出票」「候补提交 → 兑现」等全链路
  **完全离线可重复**地跑通，不触碰真实 12306、不触发任何风控；
- 支持注入各类异常场景：无票 / 有票 / 排队失败 / 风控 / 超时 / 500 /
  验证码失败 / 登录失败 / 候补兑现，用于验证自愈与错误处理分支。

用法::

    python main.py mock --port 18006
    # 然后把 env.py 的 api_base 设为 http://127.0.0.1:18006

运行时切换场景::

    curl -X POST http://127.0.0.1:18006/__mock__/scenario -d '{"scenario":"sold_out"}'

注意：本 mock 只实现了本项目接口层实际调用到的路径与字段，
目的是「验证自己的解析与流程」，不代表官方协议全集。
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import logging
import random
import threading
import time
import uuid as uuid_mod
from typing import Any

from flask import Flask, Response, jsonify, request

from ticket_helper import train_date as train_date_util

log = logging.getLogger("mock12306")

#: 支持的场景
SCENARIOS = (
    "has_ticket",     # 有票（默认）
    "sold_out",       # 全部无票（进入候补/捡漏）
    "standby",        # 无票但可候补
    "queue_fail",     # 排队失败
    "queue_cancelled",  # 排队被中止（waitTime=-3 订单已撤销）
    "no_initdc_form",   # initDc 页面不下发 ticketInfoForPassengerForm（兜底分支）
    "queue_timeout",  # 排队一直不返回结果
    "risk",           # 触发风控（429 / 风控文案）
    "timeout",        # 请求超时（模拟网络慢）
    "error500",       # 服务端 500
    "captcha_fail",   # 验证码校验失败
    "login_fail",     # 账号密码错误
    "qr_expired",     # 扫码：二维码立即过期
    "qr_timeout",     # 扫码：一直停留在「等待扫描」
    "qr_bad_response",  # 扫码：返回不可识别的错误信封（验证不会误判为失败）
    "uamauth_fail",     # 客户端鉴权被拒（票据无效）
    "login_captcha_blocked",  # 登录验证码：接口被风控拒绝（真机现状）
    "user_info_no_flag",      # 用户信息里没有核验字段（此时必须「无法判定」，不能猜）
    "face_required",          # chechFace.face_flag=false：官方要求先做人证核验
    "standby_rejected",       # submitOrderRequest 明确不接受该候补需求
    "query_rate_limited",     # 余票查询一直被拒（回 HTML 错误页），热身后仍然被拒
    "legacy_initdc_form",     # 老版本官方下单页才有的 ticketInfoForPassengerForm（见 initDc 处理器）
)

#: 扫码登录可选状态（mock 用）
QR_MOCK_STATES = ("waiting", "scanned", "confirmed", "expired")

#: 固定测试账号（仅存在于 mock 内，不入真实库）
MOCK_USERS = {"test_user": "test_pass", "fan_0723@qq.com": "demo_pass"}
MOCK_PASSENGERS = [
    {
        "passenger_name": "张三",
        "passenger_id_type_code": "1",
        "passenger_id_no": "110101199001011234",
        "passenger_type": "1",
        "passenger_type_name": "成人",
        "mobile_no": "13800000000",
        "isUserSelf": "Y",
        # 候补的 passengerInfo 里要带官方加密串与出生日期（后者用来算「是否 60 岁以上」）。
        # **真机的下单链路响应里确实有 allEncStr**；mock 少了它，整条候补链路在离线
        # 环境就永远走不通，而真机上却会因为参数不全被判「乘车人信息格式不正确」。
        "born_date": "1988-07-23",
        "allEncStr": "mock-all-enc-zhangsan",
    },
    {
        "passenger_name": "李四",
        "passenger_id_type_code": "1",
        "passenger_id_no": "110101199202022345",
        "passenger_type": "1",
        "passenger_type_name": "成人",
        "mobile_no": "13800000001",
        "isUserSelf": "N",
        "born_date": "1975-02-11",
        "allEncStr": "mock-all-enc-lisi",
    },
]

#: 个人中心的乘车人列表。**证件号照官方打码**（``/otn/passengers/query`` 返回的就是
#: 打码值，完整证件号只有下单链路 ``getPassengerDTOs`` 才给）——
#: mock 如果在这里给明文，就会掩盖「拿个人中心链路去下单」这个必然踩的坑。
MOCK_PASSENGER_DIRECTORY = [
    {
        "passenger_name": "张三",
        "passenger_id_type_code": "1",
        "passenger_id_type_name": "中国居民身份证",
        "passenger_id_no": "110101********1234",
        "passenger_type": "1",
        "passenger_type_name": "成人",
        "mobile_no": "138****0000",
        "sex_code": "M",
        "passenger_verify_status": "1",
        "passenger_verify_status_name": "已通过",
        "isUserSelf": "Y",
        "total_times": "36",
        "index_id": "P1001",
    },
    {
        "passenger_name": "李四",
        "passenger_id_type_code": "1",
        "passenger_id_type_name": "中国居民身份证",
        "passenger_id_no": "110101********2345",
        "passenger_type": "1",
        "passenger_type_name": "成人",
        "mobile_no": "138****0001",
        "sex_code": "F",
        "passenger_verify_status": "3",
        "passenger_verify_status_name": "待核验",
        "isUserSelf": "N",
        "total_times": "8",
        "index_id": "P1002",
    },
]

#: 「我的订单」样例。**字段名照着真机抄**（2026-09-29 用真实账号抓下来的原始记录）：
#:
#: - 车次 ``train_code_page``、区间 ``from_station_name_page``（**数组**）、
#:   乘车日期 ``start_train_date_page``（**带时间**）、金额 ``ticket_total_price_page``、
#:   乘客姓名 ``array_passser_name_page``（官方拼写就是 ``passser``）；
#: - 票的明细在 ``tickets[]``，席别 / 座位号 / 票种 / 状态都在里面。
#:
#: 以前这里是按「直觉」写的字段名（``train_code`` / ``from_station_name`` /
#: ``passengerDTOs``），于是真机上「订单取得到、但每一列都是空的」这个 bug
#: 在 mock 上**永远测不出来** —— mock 的字段名必须与真机一致，
#: 否则它只是在验证自己那份假设。
MOCK_ORDER_HISTORY = {
    "G": [
        {
            "sequence_no": "E900000001",
            "order_date": "2026-09-20 10:12:00",
            "train_code_page": "G6102",
            "start_train_date_page": "2026-10-01 08:00",
            "from_station_name_page": ["广州南"],
            "to_station_name_page": ["长沙南"],
            "start_time_page": "08:00",
            "arrive_time_page": "10:35",
            "ticket_total_price_page": "314.0",
            "recordCount": "1",
            "array_passser_name_page": ["张三"],
            "pay_flag": "N",
            "return_flag": "Y",
            "resign_flag": "2",
            "tickets": [
                {
                    "seatTypeName": "二等座",
                    "coachNo": "05",
                    "seatNo": "12A",
                    "ticketTypeName": "成人票",
                    "ticket_status": "a",
                    "ticket_status_name": "已支付",
                    "passengerDTO": {"passenger_name": "张三"},
                }
            ],
        },
        {
            # 待支付：pay_flag=Y，前端据此给「去官方支付」入口
            "sequence_no": "E900000002",
            "order_date": "2026-09-21 09:30:00",
            "train_code_page": "G6140",
            "start_train_date_page": "2026-10-02 09:05",
            "from_station_name_page": ["广州南"],
            "to_station_name_page": ["武汉"],
            "start_time_page": "09:05",
            "arrive_time_page": "13:20",
            "ticket_total_price_page": "738.0",
            "recordCount": "1",
            "array_passser_name_page": ["李四"],
            "pay_flag": "Y",
            "tickets": [
                {
                    "seatTypeName": "一等座",
                    "coachNo": "02",
                    "seatNo": "08F",
                    "ticketTypeName": "成人票",
                    "ticket_status": "i",
                    "ticket_status_name": "待支付",
                    "passengerDTO": {"passenger_name": "李四"},
                }
            ],
        },
    ],
    "H": [
        {
            "sequence_no": "E800000001",
            "order_date": "2026-08-01 08:00:00",
            "train_code_page": "G1001",
            "start_train_date_page": "2026-08-10 07:00",
            "from_station_name_page": ["北京西"],
            "to_station_name_page": ["上海虹桥"],
            "start_time_page": "07:00",
            "arrive_time_page": "11:29",
            "ticket_total_price_page": "553.0",
            "recordCount": "1",
            "array_passser_name_page": ["张三"],
            "pay_flag": "N",
            "return_flag": "N",
            "tickets": [
                {
                    "seatTypeName": "二等座",
                    "coachNo": "08",
                    "seatNo": "03C",
                    "ticketTypeName": "成人票",
                    "ticket_status": "c",
                    "ticket_status_name": "已退票",
                    "passengerDTO": {"passenger_name": "张三"},
                }
            ],
        }
    ],
}

#: 真机上「建立会话」的环节都会下发这批 Cookie（JSESSIONID + tk + 有效期）。
#: mock 以前一个 Cookie 都不发，导致「重启后免扫码（会话留存）」这条链路
#: 在离线环境里**根本没法验证** —— 冒烟测试正是在这里红掉的。
#: Cookie 值固定即可：mock 要验的是「客户端有没有把会话存下来并带回来」，
#: 不是做一个完整的鉴权模拟器。
MOCK_SESSION_COOKIES = {
    "JSESSIONID": "MOCK-SESSION-ID",
    "tk": "MOCK-TK-VALUE",
    "RAIL_EXPIRATION": "9999999999999",
}


def with_session_cookies(response):
    """给响应挂上会话 Cookie 并返回它（保持 return 一行写完）。"""
    for name, value in MOCK_SESSION_COOKIES.items():
        response.set_cookie(name, value, path="/")
    return response


#: 1x1 透明 PNG（登录验证码占位图）
_PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00"
    b"\x00\x00IEND\xaeB`\x82"
)


class MockState:
    """mock 的全局可变状态（线程安全）。"""

    def __init__(self, scenario: str = "has_ticket") -> None:
        self.lock = threading.RLock()
        #: 最近一次 chechFace 的 face_flag（核验标志），供测试与排障观察
        self.face_flag: bool | None = None
        self.scenario = scenario if scenario in SCENARIOS else "has_ticket"
        self.session_token = ""
        self.logged_in_users: set[str] = set()
        self.order_seq = 100000
        self.queue_polls: dict[str, int] = {}
        self.issued_orders: dict[str, dict[str, Any]] = {}
        self.standby_orders: dict[str, dict[str, Any]] = {}
        #: 最近一次余票查询的行程（候补兑现后拿它合成一条「未出行订单」）
        self.last_query: dict[str, str] = {}
        #: 候补兑现后生成的订单行。真机上兑现是异步的，**只在「我的订单」里看得见**，
        #: 所以 mock 也从那里给出来 —— 客户端的兑现判据才不会被测成错的。
        self.fulfilled_orders: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.ticket_stock = {"二等座": 5, "一等座": 2, "商务座": 1, "硬卧": 3, "硬座": 20, "无座": 50}
        # 扫码登录（AC-08）
        self.qr_polls: dict[str, int] = {}
        self.qr_override: dict[str, str] = {}
        #: 未指定 uuid 时的统一状态（测试用「一键设成某状态」）
        self.qr_default_state = ""
        #: 真机的票据流转（照实测行为建模，别简化）：
        #: - qr_ticket：checkqr 确认时响应里那个 uamtk 字段。**直接拿去换身份会被拒**；
        #: - apptk：/passport/web/auth/uamtk 发票台发的票，才是能换身份的那张；
        #: 会话是否已「热身」：真机上必须先真实加载一次余票页，才肯服务 query 接口。
        self.session_warmed = False
        #: 余票页里 ``var CLeftTicketUrl`` 的取值（空 = 用默认 queryZ）。
        #: 字段名**有外部出处**：上游 ``query/query.py::get_query_api_type``
        #: 正是读它拿查询路径（见 ``vendor/pjialin/ticket_facts.py``）。
        #: 可覆盖是为了让「不同策略最终打到哪条路径」在离线**可观测** ——
        #: 否则「取了路径却没接上」这类问题在 mock 里根本测不出来。
        self.query_route = ""
        self.apptk = ""
        self.qr_ticket = ""
        #: 鉴权接口收到的同源头（用于断言客户端确实带了）
        self.auth_headers: list[dict[str, str]] = []
        #: 让二维码在轮询 N 次后过期（真机约 20 秒就过期，用于验证自动刷新）
        self.qr_expire_after_polls = 0
        #: 扫码登录成功后返回的账号名
        self.qr_username = "qr_user_001"
        #: 当前会话所属账号。真实 12306 里一条会话只对应一个登录用户，
        #: 用 logged_in_users 集合去猜（next(iter(...))）是错的 ——
        #: 集合无序，多账号演练时会返回上一个登录的用户，导致假失败。
        self.current_login_user = ""

    # ------------------------------------------------------------------ #
    def origin_guard_ok(self) -> bool:
        """登录鉴权接口是否带了真机要求的同源头。"""
        origin = request.headers.get("Origin", "")
        referer = request.headers.get("Referer", "")
        return bool(origin) and "passport?redirect=" in referer

    def set_scenario(self, scenario: str) -> bool:
        if scenario not in SCENARIOS:
            return False
        with self.lock:
            self.scenario = scenario
        log.info("mock 场景切换为: %s", scenario)
        return True

    # ------------------------------------------------------------------ #
    # 扫码登录（AC-08）
    # ------------------------------------------------------------------ #
    def new_qr(self) -> str:
        """签发一个二维码 uuid。"""
        with self.lock:
            token = f"mock-qr-{uuid_mod.uuid4().hex[:16]}"
            self.qr_polls[token] = 0
            if len(self.qr_polls) > 200:
                # 防止长时间运行攒下大量查询记录
                for stale in list(self.qr_polls)[:100]:
                    self.qr_polls.pop(stale, None)
                    self.qr_override.pop(stale, None)
            return token

    def set_qr_state(self, state: str, uuid: str = "") -> bool:
        """测试/演示用：把扫码状态固定成指定值。

        不传 uuid 时对所有二维码生效（``qr_default_state``），
        这样自动化测试不必先去抓 uuid 就能构造「已过期」「已确认」等场景。
        """
        if state not in QR_MOCK_STATES:
            return False
        with self.lock:
            if uuid:
                self.qr_override[uuid] = state
            else:
                self.qr_default_state = state
        log.info("mock 扫码状态设为: %s (uuid=%s)", state, uuid or "全部")
        return True

    def advance_qr(self, uuid: str) -> str:
        """按轮询次数推进扫码状态。

        默认节奏：前 2 次「等待扫描」→ 2 次「已扫码」→ 确认成功。
        这样人工演练时能完整体验状态流转，自动化测试也能直接轮询到成功。

        ``qr_expire_after_polls`` 用来如实复现真机行为：官方二维码**只有约 20 秒寿命**
        （真机实测，与轮询频率无关），过期后必须自动换一张新码。
        """
        with self.lock:
            if self.scenario == "qr_expired":
                return "expired"
            if self.scenario == "qr_timeout":
                return "waiting"
            if uuid and uuid in self.qr_override:
                return self.qr_override[uuid]
            if self.qr_default_state:
                return self.qr_default_state
            self.qr_polls[uuid] = self.qr_polls.get(uuid, 0) + 1
            count = self.qr_polls[uuid]
            limit = self.qr_expire_after_polls
            if limit and count > limit:
                return "expired"
            if count >= 5:
                return "confirmed"
            if count >= 3:
                return "scanned"
            return "waiting"

    def record(self, path: str, payload: Any = None) -> None:
        with self.lock:
            self.requests.append({"path": path, "ts": time.time(), "payload": payload})
            if len(self.requests) > 500:
                del self.requests[:-500]

    def consume_ticket(self, seat_name: str) -> bool:
        """扣减库存，模拟真实抢票的竞争（库存为 0 时后续请求变为无票）。"""
        with self.lock:
            if self.scenario in ("sold_out", "standby"):
                return False
            stock = self.ticket_stock.get(seat_name, 0)
            if stock <= 0:
                return False
            if self.scenario == "has_ticket":
                return True
            self.ticket_stock[seat_name] = stock - 1
            return True


# --------------------------------------------------------------------------- #
# 余票行构造
# --------------------------------------------------------------------------- #
#: 余票行里 `yp_info`（下标 12）的取值。
#:
#: ★ 把这个值抽成常量，是为了让 mock 能**对第 ⑤/⑧ 步的 leftTicket/leftTicketStr
#: 做判据**：上游那两步传的就是它（`ticketInfoForPassengerForm.leftTicketStr`
#: = 余票响应里该车次的 `yp_info`），而不是下标 15 的席别列表（`O9M3`）。
#: 收回流 #61 —— 两值必须**可分辨**，判据才抓得住「取错字段」。
MOCK_YP_INFO = "308d0080"
# ★ 真机（2026-10-03 存盘页面）：initDc 下发的 ``leftTicketStr`` 是一长串
# URL 编码的加密串，与余票行的 ``yp_info`` **不是一个值**。mock 里两者
# 必须可分辨，否则「取值来源」这条判据在离线永远测不出来（回流 #64）。
MOCK_LEFT_TICKET_STR = "Qm0ckL3ftT1ck3t%2FStr%2Bfrom%2FinitDc%3D"
MOCK_KEY_CHECK_IS_CHANGE = (
    "F1E2D3C4B5A6978877665544332211FF00AABBCCDDEEFF1234567890ABCDEF12"
)


def _js_train_date(value: str) -> str:
    """把 ``YYYY-MM-DD`` 转成官方浏览器同款 JS 日期串（回流 #64）。

    星期/月份用固定英文表，不赌 locale。
    """
    import datetime as _datetime

    match = _datetime.datetime.strptime(value, "%Y-%m-%d").date()
    week = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    month_names = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return (
        f"{week[match.weekday()]} {month_names[match.month - 1]} {match.day:02d}"
        f" {match.year} 00:00:00 GMT+0800 (China Standard Time)"
    )


def build_ticket_row(
    *,
    train_code: str,
    secret_seed: str,
    from_code: str,
    to_code: str,
    start_time: str,
    arrive_time: str,
    duration: str,
    train_date: str,
    seats: dict[str, str],
    can_web_buy: str = "Y",
    location: str = "H1",
) -> str:
    """按官方 37 字段协议构造一条余票记录。"""
    fields = [""] * 37
    # secretStr 在**真机响应里是 URL 编码的**（2026-10-03 实测：84/84 行都带 %2F/%2B），
    # 所以这里也编码一次 —— mock 必须复现真机的**形状**，否则「忘了解码」这种
    # 只在真机上表现的 bug，离线永远测不出来（本项目栽过好几次）。
    # 同时它**不能含裸 |**，否则会破坏定长字段协议。
    fields[0] = urllib.parse.quote(secret_seed, safe="")
    fields[1] = "预订"
    fields[2] = f"24000{train_code}A0"
    fields[3] = train_code
    fields[4] = from_code
    fields[5] = to_code
    fields[6] = from_code
    fields[7] = to_code
    fields[8] = start_time
    fields[9] = arrive_time
    fields[10] = duration
    fields[11] = can_web_buy
    fields[12] = MOCK_YP_INFO
    fields[13] = train_date.replace("-", "")
    fields[14] = "1"
    fields[15] = "O9M3"
    fields[16] = location
    fields[17] = "01"
    fields[18] = "10"
    fields[19] = "1"
    fields[20] = "0"
    for index in range(21, 35):
        fields[index] = ""
    fields[21] = seats.get("高级动卧", "")
    fields[22] = seats.get("高级软卧", "")
    fields[23] = ""
    fields[24] = seats.get("软卧", "")
    fields[25] = seats.get("软座", "")
    fields[26] = seats.get("特等座", "")
    fields[27] = seats.get("无座", "")
    fields[28] = ""
    fields[29] = seats.get("硬卧", "")
    fields[30] = seats.get("硬座", "")
    fields[31] = seats.get("二等座", "")
    fields[32] = seats.get("一等座", "")
    fields[33] = seats.get("商务座", "")
    fields[34] = seats.get("动卧", "")
    fields[35] = "O9M3"
    fields[36] = "O9M3"
    return "|".join(fields)


def default_tickets(train_date: str, scenario: str) -> list[str]:
    """按场景生成余票列表。"""
    if scenario == "sold_out":
        empty = {k: "无" for k in ("二等座", "一等座", "商务座", "硬卧", "硬座", "无座")}
        return [
            build_ticket_row(
                train_code="G6102", secret_seed="mock-secret-1", from_code="GZQ", to_code="CWQ",
                start_time="08:12", arrive_time="10:47", duration="02:35", train_date=train_date, seats=empty,
            ),
            build_ticket_row(
                train_code="G6140", secret_seed="mock-secret-2", from_code="GZQ", to_code="CWQ",
                start_time="14:05", arrive_time="16:52", duration="02:47", train_date=train_date, seats=empty,
            ),
        ]

    # 这些场景都是在讲**候补链路**的事，必须给「无票、可候补」的车次 ——
    # 否则引擎在挑目标那一步就退出了，后面的分支根本走不到，
    # 测试会以「未找到可候补的车次」这种与主题无关的方式失败。
    #
    # ⚠️ ``queue_fail`` / ``queue_timeout`` **不属于这一组**。
    # 它们描述的是**直购的排队阶段**失败（``confirmSingleForQueue`` /
    # ``queryOrderWaitTime``），前提是车次**本来可购** ——
    # 若也给「无票可候补」的车次，直购路径连候选都选不出来，
    # 根本走不到排队那一步，于是 test_grab 里「排队失败要换下一个候选」
    # 会以 ``candidates_seen=0`` 这种与主题完全无关的方式失败。
    if scenario in ("standby", "face_required", "standby_rejected"):
        return [
            build_ticket_row(
                train_code="G6102", secret_seed="mock-secret-1", from_code="GZQ", to_code="CWQ",
                start_time="08:12", arrive_time="10:47", duration="02:35", train_date=train_date,
                seats={"二等座": "候补", "一等座": "候补", "商务座": "无", "无座": "无"},
            ),
        ]

    seats = {"二等座": "5", "一等座": "2", "商务座": "1", "硬卧": "3", "硬座": "20", "无座": "有"}
    return [
        build_ticket_row(
            train_code="K512", secret_seed="mock-secret-k", from_code="GZQ", to_code="CWQ",
            start_time="06:40", arrive_time="14:20", duration="07:40", train_date=train_date, seats=seats,
        ),
        build_ticket_row(
            train_code="G6102", secret_seed="mock-secret-1", from_code="GZQ", to_code="CWQ",
            start_time="08:12", arrive_time="10:47", duration="02:35", train_date=train_date, seats=seats,
        ),
        build_ticket_row(
            train_code="G6140", secret_seed="mock-secret-2", from_code="GZQ", to_code="CWQ",
            start_time="14:05", arrive_time="16:52", duration="02:47", train_date=train_date,
            seats={**seats, "二等座": "无"},
        ),
    ]


# --------------------------------------------------------------------------- #
def create_mock_app(scenario: str = "has_ticket") -> Flask:
    app = Flask("mock12306")
    state = MockState(scenario)
    app.config["MOCK_STATE"] = state

    # ------------------------------------------------------------------ #
    def _auth_origin_guard():
        """登录鉴权接口的同源头校验（**真机实测行为**）。

        真机上不带 ``Origin`` 或 Referer 不是 passport 跳转页时，服务端
        **不返回 4xx**，而是回一个 HTML 错误页：
        "网络可能存在问题，请您重试一下！"。
        后果是「换票永远拿不到 newapptk」，却看起来像票据有问题。
        客户端以前正是用登录页 Referer，导致扫码后必然失败。
        """
        state.auth_headers.append(
            {"origin": request.headers.get("Origin", ""), "referer": request.headers.get("Referer", "")}
        )
        if state.origin_guard_ok():
            return None
        return Response(
            "<!DOCTYPE html><html><li>网络可能存在问题，请您重试一下！</li></html>",
            mimetype="text/html",
        )

    def _maybe_fail():
        """按当前场景注入故障；返回 Response 表示应当直接短路返回。"""
        scenario_now = state.scenario
        if scenario_now == "risk":
            return jsonify({"status": False, "httpstatus": 429,
                            "messages": ["您的操作过于频繁，请稍后重试"]}), 429
        if scenario_now == "error500":
            return jsonify({"status": False, "httpstatus": 500, "messages": ["系统繁忙"]}), 500
        if scenario_now == "timeout":
            time.sleep(6)  # 客户端超时（默认 12s，本地测试用较短超时即可命中）
        return None

    # ---------------- 登录 ---------------- #
    @app.get("/otn/login/conf")
    def login_conf():
        """登录页配置 —— **上游就是拿它当「登录态检测」用的**（`data.is_login`）。

        与 `/otn/login/checkUser` 并存不是重复实现：真机上这是两条不同的官方接口，
        `login_check` 域正是要在它们之间切换，所以两条都必须能被观测到。
        """
        state.record("/otn/login/conf")
        if state.scenario == "logout":
            return jsonify({"data": {"is_uam_login": "Y", "is_login": "N"}})
        return jsonify({"data": {"is_uam_login": "Y", "is_login": "Y"}})

    @app.get("/otn/passcodeNew/getPassCodeNew")
    def pass_code():
        """图形验证码 —— **登录与下单共用这一个路径**，靠 ``module``/``rand`` 区分。

        真机行为（实测 + 上游出处，回流 #60）：
        - 登录：``module=login&rand=sjrand``
        - 下单：``module=passenger&rand=randp``
          （``reference/upstream/testerSunshine_urlConf.py:334`` 的 ``codeImgByOrder``）
        - 正常时 ``Content-Type: image/jpeg``，直接是图片；
        - 缺少设备指纹上下文时返回
          ``{"status":false,"messages":["系统忙，请稍后重试"]}``（HTTP 200，不是 4xx）。

        ``rand`` **必须如实校验**：写错 rand 真机同样只给一张 HTML 错误页，
        而这正是「下单验证码永远拿不到图」的一个可能成因 —— 判据得复现它。
        """
        module = request.values.get("module", "")
        rand = request.values.get("rand", "")
        # 判据要素必须记全：只记 module 的话，「rand 写错」在离线不可观测（回流 #26）
        state.record("/otn/passcodeNew/getPassCodeNew", {"module": module, "rand": rand})
        if module == "passenger" and rand != "randp":
            return Response(
                "<!DOCTYPE html><html><li>网络可能存在问题，请您重试一下！</li></html>",
                mimetype="text/html",
            )
        if state.scenario == "login_captcha_blocked":
            return jsonify(
                {
                    "validateMessagesShowId": "_validatorMessage",
                    "url": "/leftTicket/init",
                    "status": False,
                    "httpstatus": 200,
                    "messages": ["系统忙，请稍后重试"],
                    "validateMessages": {},
                }
            )
        return Response(_PNG_1PX, mimetype="image/png")

    @app.get("/passport/captcha/captcha-image64")
    def captcha_image64():
        state.record("/passport/captcha/captcha-image64")
        # 真机实测：该旧路径已被 WAF 挡掉，任何请求头组合都返回 HTML 错误页。
        # mock 如实复现，避免客户端再把它当成主路径。
        return Response(
            "<!DOCTYPE html><html><li>网络可能存在问题，请您重试一下！</li></html>",
            mimetype="text/html",
        )

    @app.post("/passport/captcha/captcha-check")
    def captcha_check():
        state.record("/passport/captcha/captcha-check", request.form.to_dict())
        if state.scenario == "captcha_fail":
            return jsonify({"result_code": "5", "result_message": "验证码校验失败"})
        # mock 接受任意答案，便于自动化测试
        return jsonify({"result_code": "4", "result_message": "验证码校验成功"})

    @app.post("/passport/web/login")
    def web_login():
        guarded = _auth_origin_guard()
        if guarded is not None:
            return guarded
        form = request.form.to_dict()
        state.record("/passport/web/login", {"username": form.get("username")})
        if state.scenario == "login_fail":
            return jsonify({"result_code": "1", "result_message": "用户名或密码错误"})
        username = form.get("username", "")
        password = form.get("password", "")
        expected = MOCK_USERS.get(username)
        if expected is None or (expected and password != expected):
            # mock 对未知账号宽松放行（便于用任意账号演练），仅在 login_fail 场景拒绝
            log.debug("mock 放行未知账号: %s", username)
        state.logged_in_users.add(username)
        state.current_login_user = username
        state.session_token = f"mock-uamtk-{int(time.time())}"
        state.qr_ticket = ""          # 密码登录是另一条链路，清掉扫码残留
        state.apptk = ""
        return with_session_cookies(
            jsonify({"result_code": 0, "uamtk": state.session_token, "result_message": "登录成功"})
        )

    @app.post("/passport/web/auth/uamtk")
    def auth_uamtk():
        state.record("/passport/web/auth/uamtk", {"appid": request.values.get("appid", "")})
        guarded = _auth_origin_guard()
        if guarded is not None:
            return guarded
        state.apptk = f"mock-apptk-{int(time.time() * 1000)}"
        # 官方这个响应里 apptk 是 null，真正要用的是 newapptk
        return jsonify({"result_code": 0, "apptk": None, "newapptk": state.apptk})

    @app.post("/otn/uamauthclient")
    def uamauthclient():
        state.record("/otn/uamauthclient", {"tk": request.values.get("tk", "")})
        guarded = _auth_origin_guard()
        if guarded is not None:
            return guarded
        # 如实复现真机行为：**没有先调用 userLogin 就换票**时，官方并不报「顺序错了」，
        # 而是返回 {"result_code":2,"result_message":"uamtk票据内容为空"} ——
        # 票明明拿到了却"内容为空"，极具误导性。这条以前 mock 不检查，
        # 所以「先换票再建会话」的错误顺序才一路溜到了真机上。
        if state.scenario == "uamauth_fail":
            return jsonify({"result_code": 2, "result_message": "uamtk票据内容为空"})
        # 真机行为：只认「换票接口」发出来的那张票。
        # 把 checkqr 响应里的 uamtk 直接拿来用，会被判「票据内容为空」
        # （实测确认：两个字段同名但不等价）。
        ticket = request.values.get("tk", "")
        if not ticket or ticket != state.apptk:
            return jsonify({"result_code": 2, "result_message": "uamtk票据内容为空"})
        # 返回「当前会话」的账号名，而不是集合里的任意一个
        username = state.current_login_user or next(iter(state.logged_in_users), "mock_user")
        return with_session_cookies(jsonify({"result_code": 0, "username": username, "apptk": "mock"}))

    @app.post("/otn/login/userLogin")
    def user_login_post():
        state.record("/otn/login/userLogin", {"method": "POST"})
        guarded = _auth_origin_guard()
        if guarded is not None:
            return guarded
        return with_session_cookies(Response("1", mimetype="text/html"))

    @app.get("/otn/login/userLogin")
    def user_login():
        # 真机是 POST；保留 GET 仅为兼容，但**不**视为会话已建立
        state.record("/otn/login/userLogin", {"method": "GET"})
        return with_session_cookies(Response("1", mimetype="text/html"))

    @app.post("/otn/login/checkUser")
    def check_user():
        # 必须 record：`login_check` 域的判据就是「请求落到了哪条端点」，
        # 不记下来这个判据就没法验（而返回值两条端点都是「在线」，分不出来）。
        state.record("/otn/login/checkUser", {"scenario": state.scenario})
        if state.scenario in ("risk", "logout"):
            return jsonify({"data": {"flag": False}, "messages": ["请重新登录"]}), 200
        return jsonify({"data": {"flag": True}, "messages": []})

    @app.get("/otn/modifyUser/initQueryUserInfoApi")
    def query_user_info():
        # scenario=user_info_no_flag：复现「官方没返回核验字段」的情况。
        # 真机上扫码登录返回的 user_name 就是真实姓名，所以**不能**再用
        # 「姓名 ≠ 账号名」去猜核验状态 —— 那样已实名的账号会被误判成「未核验」。
        if state.scenario == "user_info_no_flag":
            return jsonify({"data": {"user_name": "张三", "sexCode": "M"}})
        return jsonify(
            {
                "data": {
                    "user_name": "张三",
                    "isUserVerified": "Y",
                    "isCheckEmail": "Y",
                    "sexCode": "M",
                }
            }
        )

    @app.post("/otn/index/initMy12306Api")
    @app.get("/otn/index/initMy12306Api")
    def init_my():
        """账号信息。

        **注意 mock 同时接受 POST 与 GET**，但真机的实际调用是 POST ——
        客户端实现里刻意做了 POST→GET 的回退，这里两种都挂上，
        才能验证「回退逻辑确实被走到」而不是永远命中第一条。
        """
        failed = _maybe_fail()
        if failed is not None:
            return failed
        state.record("/otn/index/initMy12306Api", {"method": request.method})
        if not state.current_login_user:
            # 未登录时官方回的是登录页 HTML，不是 4xx。如实复现，
            # 免得客户端把「没登录」当成「解析失败」。
            return Response("<html>登录</html>", mimetype="text/html")
        return jsonify(
            {
                "data": {
                    "user_name": "张三",
                    "real_name": "张三",
                    "user_type_name": "成人",
                    "sexCode": "M",
                    "email": "zhangsan@example.com",
                    "mobile_no": "13800000000",
                    "id_type_name": "中国居民身份证",
                    "id_no": "110101199001011234",
                    "isUserVerified": "Y",
                    "isCheckEmail": "Y",
                    "points": "1280",
                    "register_time": "2015-03-08 12:00:00",
                    # 故意留一个「客户端还没映射」的字段，用来覆盖 extra_keys 的收集
                    "some_new_field": "x",
                }
            }
        )

    # ---------------- 个人中心：乘车人 / 我的订单 ---------------- #
    @app.post("/otn/passengers/query")
    @app.get("/otn/passengers/query")
    def passengers_query():
        """常用乘车人（个人中心链路，分页）。"""
        failed = _maybe_fail()
        if failed is not None:
            return failed
        state.record("/otn/passengers/query", {"method": request.method, **request.values.to_dict()})
        if not state.current_login_user:
            return Response("<html>登录</html>", mimetype="text/html")
        try:
            page_index = max(1, int(request.values.get("pageIndex", 1) or 1))
            page_size = max(1, int(request.values.get("pageSize", 10) or 10))
        except (TypeError, ValueError):
            page_index, page_size = 1, 10
        rows = MOCK_PASSENGER_DIRECTORY
        start = (page_index - 1) * page_size
        return jsonify(
            {
                "status": True,
                "data": {
                    "datas": rows[start:start + page_size],
                    "count": len(rows),
                    "pageIndex": page_index,
                    "pageSize": page_size,
                },
            }
        )

    @app.post("/otn/queryOrder/queryMyOrder")
    def query_my_order():
        """「我的订单」分页查询（未出行 G / 历史 H）。"""
        failed = _maybe_fail()
        if failed is not None:
            return failed
        state.record("/otn/queryOrder/queryMyOrder", request.form.to_dict())
        if not state.current_login_user:
            return Response("<html>登录</html>", mimetype="text/html")
        where = (request.form.get("query_where", "G") or "G").upper()
        if where not in ("G", "H"):
            return jsonify({"status": False, "messages": [f"查询范围非法: {where}"], "data": {}})
        try:
            # **pageIndex 从 0 开始**（真机语义，依据官方 train_order 页面 JS：
            # ``data:{pageIndex:"0",...}``、分页回调 ``getCurrent()-1``）。
            # mock 以前写成 1-based，于是「把 1-based 传给官方」这个真机 bug
            # 在 mock 上永远测不出来 —— 让 mock 与真机同语义，才能抓住它。
            page_index = max(0, int(request.form.get("pageIndex", 0) or 0))
            page_size = max(1, int(request.form.get("pageSize", 10) or 10))
        except (TypeError, ValueError):
            page_index, page_size = 0, 10
        rows = list(MOCK_ORDER_HISTORY.get(where, []))
        if where == "G" and state.fulfilled_orders:
            # 候补兑现生成的订单也出现在「未出行」里（真机就是这样）
            rows = list(state.fulfilled_orders) + rows
        keyword = (request.form.get("sequeue_train_name") or "").strip()
        if keyword:
            # 按**真机字段**筛选（车次 / 订单号 / 乘客姓名）
            rows = [
                r for r in rows
                if keyword in str(r.get("train_code_page", ""))
                or keyword in str(r.get("sequence_no", ""))
                or any(keyword in str(name) for name in r.get("array_passser_name_page", []))
            ]
        page_count = max(1, (len(rows) + page_size - 1) // page_size)
        # 0-based：第 0 页 = 前 page_size 条
        start = page_index * page_size
        return jsonify(
            {
                "status": True,
                "data": {
                    "OrderDTODataList": rows[start:start + page_size],
                    # 真机也有这个（没订单时是空串）——界面靠它区分
                    # 「真的没订单」与「有单但这页没取到」，mock 必须一起给，
                    # 否则「pageIndex 传成 1-based」这类错误在离线环境下看不出后果。
                    "order_total_number": str(len(rows)),
                    "pageIndex": page_index,
                    "pageCount": page_count,
                    "train_start_date": request.form.get("queryStartDate", ""),
                    "train_end_date": request.form.get("queryEndDate", ""),
                    "result": "0",
                },
                "messages": [],
            }
        )

    @app.get("/otn/login/loginOut")
    def login_out():
        state.logged_in_users.clear()
        state.current_login_user = ""
        return Response("1", mimetype="text/html")

    # ---------------- 扫码登录（AC-08） ---------------- #
    @app.post("/passport/web/create-qr64")
    def create_qr():
        state.qr_ticket = ""          # 重新申请二维码 = 新的扫码上下文
        state.apptk = ""
        failed = _maybe_fail()
        if failed is not None:
            return failed
        state.record("/passport/web/create-qr64", request.form.to_dict())
        uuid = state.new_qr()
        return jsonify(
            {
                "result_code": 0,
                "result_message": "扫码登录二维码生成成功",
                "uuid": uuid,
                # 官方返回裸 base64（不带 data: 前缀），这里保持一致
                "image": base64.b64encode(_PNG_1PX).decode("ascii"),
            }
        )

    @app.post("/passport/web/checkqr")
    @app.get("/passport/web/checkqr")
    def check_qr():
        """官方是 POST 表单；同时接受 GET，用于验证客户端的回退逻辑。"""
        failed = _maybe_fail()
        if failed is not None:
            return failed
        uuid = request.values.get("uuid", "")
        # ⚠️ **记完整表单**，不要挑几个字段记。
        # `qr_check` 域的判据就是「表单里到底放了哪些键」——
        # 原来这里只记 uuid/appid/method，于是「上游那套有没有带上设备指纹 Cookie」
        # 在离线**根本不可观测**（两套请求看起来一模一样）。
        state.record(
            "/passport/web/checkqr",
            {**request.values.to_dict(), "method": request.method},
        )

        # 如实复现真机行为：**不带 appid 时官方不报错，而是稳定返回 result_code=9。
        # 这条以前没被 mock 覆盖，结果客户端漏传 appid 时表现成「操作失败」，
        # 排查方向被彻底带偏（看起来像服务端故障，实际只是少了个参数）。
        if request.values.get("appid", "") != "otn":
            return jsonify({"result_message": "操作失败", "result_code": 9, "user": None})

        # 模拟「返回不可识别的错误信封」：用于验证客户端不会把它当成终态失败
        if state.scenario == "qr_bad_response":
            return jsonify({"status": False, "messages": ["操作失败"]})

        qr_state = state.advance_qr(uuid)
        if qr_state == "expired":
            return jsonify({"result_code": "3", "result_message": "二维码已过期"})
        if qr_state == "scanned":
            return jsonify({"result_code": "1", "result_message": "已扫码，请确认"})
        if qr_state == "confirmed":
            # 确认成功时官方会带上 uamtk，据此完成后续鉴权
            username = state.qr_username or "qr_user_001"
            state.logged_in_users.add(username)
            state.current_login_user = username
            # 真机确认响应长这样（实测）：
            #   {"result_message":"扫码登录成功","uamtk":"t8_xXJs...","result_code":"2"}
            # 这里的 uamtk **不能**直接拿去换身份，必须再走 /passport/web/auth/uamtk 换 newapptk
            state.qr_ticket = f"mock-qr-uamtk-{int(time.time() * 1000)}"
            return with_session_cookies(
                jsonify(
                    {
                        "result_code": "2",
                        "result_message": "扫码登录成功",
                        "uamtk": state.qr_ticket,
                        "username": username,
                    }
                )
            )
        return jsonify({"result_code": "0", "result_message": "等待扫描"})

    # ---------------- 余票查询 ---------------- #
    @app.get("/otn/leftTicket/init")
    def left_ticket_init():
        """余票页。**打开它才算是「逛过页面」**——真机上这是 query 接口被接受的前提。

        页面里带 ``var CLeftTicketUrl``：真机页面就是靠它告诉客户端当前该用哪条
        查询路径（上游正是这么读的）。mock 如实复现，否则
        「从页面取路径」这条策略在离线**根本无法被观测**。
        """
        state.session_warmed = True
        state.record("/otn/leftTicket/init")
        route = state.query_route or "leftTicket/queryZ"
        return Response(
            "<!DOCTYPE html><html><head><script>"
            f"var CLeftTicketUrl = '{route}';"
            "</script></head><body>left ticket</body></html>",
            mimetype="text/html",
        )

    @app.get("/otn/leftTicket/queryZ")
    @app.get("/otn/leftTicket/queryA")
    @app.get("/otn/leftTicket/query")
    @app.get("/otn/leftTicket/queryO")
    def left_ticket():
        failed = _maybe_fail()
        if failed is not None:
            return failed
        # 复现真机现象（同一时刻并排对照得出的结论）：
        #   未加载过页面的会话直接查 → WAF 回 HTML 错误页；
        #   先真实加载一次余票页再查  → 正常返回 JSON。
        # 以及：被拒后继续连发候选路径只会加重限流。
        if state.scenario == "query_rate_limited" or not state.session_warmed:
            state.record(request.path, {"rejected": True})
            return Response(
                "<!DOCTYPE html><html><li>网络可能存在问题，请您重试一下！</li></html>",
                mimetype="text/html",
            )
        train_date = request.args.get("leftTicketDTO.train_date", time.strftime("%Y-%m-%d"))
        from_code = request.args.get("leftTicketDTO.from_station", "GZQ")
        to_code = request.args.get("leftTicketDTO.to_station", "CWQ")
        state.record(request.path, {"date": train_date, "from": from_code, "to": to_code})
        state.last_query = {"date": train_date, "from": from_code, "to": to_code}

        # 真机行为（2026-09-29 并排实测）：**已过去**与**超出预售期**的日期，
        # 官方回的是与风控**完全相同**的那张 HTML 错误页，HTTP 200。
        # 必须在离线环境复现这一条，否则「任务日期填成去年 → 被误报成风控」
        # 这类 bug 在 mock 上永远测不出来（客户端看到的只是一个空列表）。
        if not train_date_util.queryable(train_date):
            log.info("mock: 日期 %s 不在可售范围，按真机行为回 HTML 错误页", train_date)
            return Response(
                "<!DOCTYPE html><html><li>网络可能存在问题，请您重试一下！</li></html>",
                mimetype="text/html",
            )

        rows = default_tickets(train_date, state.scenario)
        # 支持「实际起止站」——mock 里把查询参数作为首末站透传，让站名校验也走到
        adjusted: list[str] = []
        for row in rows:
            parts = row.split("|")
            parts[6] = from_code
            parts[7] = to_code
            parts[4] = from_code
            parts[5] = to_code
            adjusted.append("|".join(parts))
        return jsonify(
            {
                "httpstatus": 200,
                "status": True,
                "messages": [],
                "data": {"result": adjusted, "map": {}, "flag": "1"},
            }
        )

    @app.get("/otn/leftTicket/queryTicketPrice")
    def ticket_price():
        seat_types = (request.args.get("seat_types") or "O,M,9").split(",")
        data: dict[str, Any] = {}
        for code in seat_types:
            if code:
                data[f"{code}_price"] = {"O": 314.0, "M": 502.0, "9": 968.0}.get(code, 300.0)
        return jsonify({"status": True, "data": data})

    @app.get("/otn/czxx/queryByTrainNo")
    def stop_stations():
        return jsonify(
            {
                "status": True,
                "data": {
                    "data": [
                        {"station_name": "广州南", "arrive_time": "----", "start_time": "08:12", "stopover_time": "---"},
                        {"station_name": "长沙南", "arrive_time": "10:47", "start_time": "10:50", "stopover_time": "3"},
                    ]
                },
            }
        )

    # ---------------- 下单 ---------------- #
    @app.post("/otn/leftTicket/submitOrderRequest")
    def submit_order_request():
        failed = _maybe_fail()
        if failed is not None:
            return failed
        state.record("/otn/leftTicket/submitOrderRequest", request.form.to_dict())
        if state.scenario in ("sold_out",):
            return jsonify({"status": True, "httpstatus": 200, "data": "1",
                            "messages": ["没有足够的票"], "validateMessages": {}})
        return jsonify({"status": True, "httpstatus": 200, "data": "N",
                        "messages": [], "validateMessages": {}})

    def _last_submitted_train_date() -> str:
        """取上次 ``submitOrderRequest`` 用的乘车日期（没有就回测试默认）。"""
        for item in reversed(state.requests):
            if item["path"] == "/otn/leftTicket/submitOrderRequest":
                value = (item.get("payload") or {}).get("train_date") or ""
                if value:
                    return value
        return "2026-10-01"

    @app.post("/otn/confirmPassenger/initDc")
    def init_dc():
        token = f"mock-token-{int(time.time() * 1000)}"
        html = "<html><script>" + f"var globalRepeatSubmitToken = '{token}';"
        # ★ 回流 #64（2026-10-03 真机存盘页面复核）：initDc **会**下发
        # `ticketInfoForPassengerForm`，而且是**单引号字面量**（含 \uXXXX 转义
        # 与 null），不是 JSON。2026-09-30 那次「页面里没有这个词」的结论
        # 是**解析失败被误判成「页面没有」** —— mock 跟着错结论走，
        # 默认不发这个变量，是第 4 次「mock 替官方造事实」。
        # 现在默认按真机形状发；要测「页面没下发」的兜底分支，
        # 显式切场景 `no_initdc_form`。
        if state.scenario != "no_initdc_form":
            info = (
                "{'cardTypes':[{'end_station_name':null,'end_time':null,'id':'1',"
                "'start_station_name':null,'start_time':null,"
                "'value':'\\u5C45\\u6C11\\u8EAB\\u4EFD\\u8BC1'}],"
                f"'key_check_isChange':'{MOCK_KEY_CHECK_IS_CHANGE}',"
                f"'leftTicketStr':'{MOCK_LEFT_TICKET_STR}',"
                "'queryLeftTicketRequestDTO':{'from_station':'GZQ',"
                "'station_train_code':'G6102',"
                f"'train_date':'{_js_train_date(_last_submitted_train_date())}',"
                "'train_no':'690000G610200','to_station':'CWQ'},"
                "'purpose_codes':'00','train_location':'H1'}"
            )
            html += f"var ticketInfoForPassengerForm = {info};"
        html += "</script></html>"
        if state.scenario == "need_slide":
            # 真机上下单页会带这一行：'1' 表示这一单必须过滑块风控。
            # 离线要能复现它，否则「要不要过滑块」这条判据在 mock 里永远测不到
            # （判据复现不了 = 这条能力只能靠真机，而真机一单一票）。
            html = html.replace(
                "</script>", "var if_check_slide_passcode  ='1';</script>"
            )
        return Response(html, mimetype="text/html")

    @app.post("/otn/confirmPassenger/getPassengerDTOs")
    def get_passengers():
        token = request.form.get("REPEAT_SUBMIT_TOKEN", "")
        if not token:
            return jsonify({"status": False, "messages": ["缺少 REPEAT_SUBMIT_TOKEN"]})
        return jsonify({"status": True, "data": {"normal_passengers": MOCK_PASSENGERS, "isExist": True}})

    @app.get("/otn/confirmPassenger/getPassCodeNew")
    def order_captcha_wrong_path():
        """★ 这条路径**真机不存在**（回流 #60）。

        本项目原实现把「下单验证码」写在这里，于是真机永远只回一张 HTML 错误页 ——
        而 mock 以前**照着我们自己的错路径实现**（返回 PNG），于是「路径写错」
        在离线永远绿。这是「mock 替官方造事实」的第 4 次复发。
        现在如实复现真机形状：**HTTP 200 + HTML 错误页**。
        谁再改回这条路径，`test_order_chain_fixes.py` 立刻变红。
        """
        state.record(
            "/otn/confirmPassenger/getPassCodeNew",
            {"module": request.values.get("module", ""), "rand": request.values.get("rand", "")},
        )
        return Response(
            "<!DOCTYPE html><html><li>网络可能存在问题，请您重试一下！</li></html>",
            mimetype="text/html",
        )

    @app.post("/otn/passcodeNew/checkRandCodeAnsyn")
    def check_rand_code():
        state.record("/otn/passcodeNew/checkRandCodeAnsyn", request.form.to_dict())
        if state.scenario == "captcha_fail":
            return jsonify({"result_code": "5", "data": {"result": "0"}})
        return jsonify({"result_code": "4", "data": {"result": "1"}})

    @app.post("/otn/confirmPassenger/checkOrderInfo")
    def check_order_info():
        form = request.form.to_dict()
        state.record("/otn/confirmPassenger/checkOrderInfo", form)
        if not form.get("REPEAT_SUBMIT_TOKEN"):
            return jsonify({"status": False, "data": {"submitStatus": False, "errMsg": "REPEAT_SUBMIT_TOKEN 缺失"}})

        # ⚠️ 本步以前**完全不校验参数**，于是「passengerTicketStr 漏发第 9 段官方加密串、
        # oldPassengerStr 没补 '__ _ _'」这种真机必被拒的事，离线永远全绿 ——
        # 与 #48（secretStr 没解码，mock 造的 secret 没有 '%'）是**同一个机制**：
        # mock 按我们自己的形状出卷子，就只能证明「我和我自己一致」。回流 #51。
        #
        # 判据**照上游来**（``vendor/pjialin/py12306/order/order.py:595-613`` 逐字）：
        #   ① 每名乘客 9 段，第 9 段（下标 8）= 官方加密串，非空；
        #   ② oldPassengerStr 以 '__ _ _' 结尾（上游注释：不加后面请求会出错）。
        # 拒绝时**回真机那句含糊话术**（「余票不足」）——
        # 复现真机症状，才能让这条判据真的防住未来的回归。
        # 只看**第一个**乘客段：乘客段以 '_' 分隔，而加密串理论上可能含 '_'，
        # 全串切分会把它切碎造成误拒；第一个段的下标 8 恒为该乘客的加密串。
        first_chunk = form.get("passengerTicketStr", "").split("_")[0]
        fields = first_chunk.split(",")
        if len(fields) < 9 or not fields[8].strip():
            return jsonify({"status": False, "data": {"submitStatus": False, "errMsg": "余票不足！"},
                            "messages": []})
        if not form.get("oldPassengerStr", "").endswith("__ _ _"):
            return jsonify({"status": False, "data": {"submitStatus": False, "errMsg": "余票不足！"},
                            "messages": []})
        return jsonify({"status": True, "data": {"submitStatus": True, "isExist": False}, "messages": []})

    @app.post("/otn/confirmPassenger/getQueueCount")
    def get_queue_count():
        form = request.form.to_dict()
        state.record("/otn/confirmPassenger/getQueueCount", form)
        # ⚠️ 这一步以前**只记录不校验**（回流 #61）；#64 起判据对齐真机：
        # `leftTicket` = initDc 下发的 `leftTicketStr`（与 `yp_info` 不是一个值），
        # 页面没下发（场景 no_initdc_form）时才允许兜底 `yp_info`；
        # `train_date` 必须是官方浏览器同款 JS 日期串（发 `2026-10-01` 会被拒，
        # 真机 2026-10-03 实测第 ⑤ 步「系统忙」的取值来源错误之一）。
        if state.scenario == "no_initdc_form":
            allowed_left: tuple[str, ...] = (MOCK_YP_INFO,)
        else:
            allowed_left = (MOCK_LEFT_TICKET_STR,)
        expected_date = _js_train_date(_last_submitted_train_date())
        if (
            form.get("leftTicket") not in allowed_left
            or not form.get("train_location")
            or form.get("train_date") != expected_date
        ):
            return jsonify({"status": False, "messages": ["系统忙，请稍后重试"]})
        return jsonify(
            {
                "status": True,
                "data": {"count": str(random.randint(0, 30)), "ticket": "100",
                         "op_2": "false", "countT": "0", "op_1": "true"},
                "messages": [],
            }
        )

    @app.post("/otn/confirmPassenger/confirmSingleForQueue")
    def confirm_single():
        failed = _maybe_fail()
        if failed is not None:
            return failed
        form = request.form.to_dict()
        state.record("/otn/confirmPassenger/confirmSingleForQueue", form)
        # ⚠️ 与 getQueueCount 同理（回流 #61/#64）：判据对齐真机取值来源。
        # 它是**唯一真的建单**的一步，把错的 leftTicketStr 发出去在真机不会报错、
        # 只会静默失败，所以判据必须留在这里。
        if state.scenario == "no_initdc_form":
            allowed_left_csf: tuple[str, ...] = (MOCK_YP_INFO,)
        else:
            allowed_left_csf = (MOCK_LEFT_TICKET_STR,)
        if form.get("leftTicketStr") not in allowed_left_csf or not form.get("train_location"):
            return jsonify({"status": False, "data": {"submitStatus": False,
                                                      "errMsg": "系统忙，请稍后重试"},
                            "messages": []})
        if state.scenario == "sold_out":
            return jsonify({"status": False, "data": {"submitStatus": False, "errMsg": "余票不足"},
                            "messages": ["余票不足"]})
        order_id = f"E{state.order_seq}"
        state.order_seq += 1
        state.queue_polls[order_id] = 0
        state.issued_orders[order_id] = {
            "order_no": order_id,
            "status": "queued",
            "created_at": time.time(),
            "seat_type": request.form.get("passengerTicketStr", "O")[:1],
        }
        return jsonify({"status": True, "data": {"submitStatus": True, "result": "0"}, "messages": []})

    @app.get("/otn/confirmPassenger/queryOrderWaitTime")
    def query_order_wait_time():
        failed = _maybe_fail()
        if failed is not None:
            return failed
        token = request.args.get("REPEAT_SUBMIT_TOKEN", "")
        # ⚠️ **必须记完整查询串**，不能只记 token：两套实现在这一步的
        # `random` 取值**来源不同**（本项目毫秒时间戳 / 上游随机小数串）。
        # 只记 token 的话，这个差异在离线**不可观测** ——
        # 「判据要素没被记下来，测试就只能证明自洽」（回流 #26）。
        state.record("/otn/confirmPassenger/queryOrderWaitTime", request.args.to_dict())
        if not state.queue_polls:
            return jsonify({"status": True, "data": {"queryOrderWaitTimeStatus": True, "count": 0,
                                                     "waitTime": -1, "waitCount": 0}, "messages": []})

        order_id = next(reversed(state.queue_polls))
        state.queue_polls[order_id] += 1
        polls = state.queue_polls[order_id]

        if state.scenario == "queue_cancelled":
            # ★ 真机形状：`queryOrderWaitTimeStatus` 仍是 `true`，
            # 靠 `waitTime` 的**负数语义**表达终止（-2 失败 / -3 订单已撤销，
            # 上游 `order/order.py:549` 的判读）。
            # 以前 mock 只有 `queryOrderWaitTimeStatus=False` 这一种失败形状，
            # 于是「把已撤销报成排队超时」在离线永远测不出来（回流 #61）。
            return jsonify({"status": True, "data": {"queryOrderWaitTimeStatus": True,
                                                     "waitTime": -3, "waitCount": 0,
                                                     "msg": "订单已撤销"}, "messages": []})
        if state.scenario == "queue_fail":
            return jsonify({"status": True, "data": {"queryOrderWaitTimeStatus": False,
                                                     "msg": "排队失败：余票已被抢完"}, "messages": []})

        if state.scenario == "queue_timeout" or polls < 3:
            return jsonify(
                {
                    "status": True,
                    "data": {
                        "queryOrderWaitTimeStatus": True,
                        "count": max(0, 5 - polls),
                        "waitTime": max(0, 3 - polls),
                        "waitCount": max(0, 5 - polls),
                        "requestId": int(time.time() * 1000),
                        "tourFlag": "dc",
                    },
                    "messages": [],
                }
            )

        state.issued_orders[order_id]["status"] = "success"
        return jsonify(
            {
                "status": True,
                "data": {
                    "queryOrderWaitTimeStatus": True,
                    "count": 0,
                    "waitTime": -1,
                    "waitCount": 0,
                    "requestId": int(time.time() * 1000),
                    "tourFlag": "dc",
                    "orderId": order_id,
                },
                "messages": [],
            }
        )

    @app.post("/otn/confirmPassenger/resultOrderForDcQueue")
    def result_order_queue():
        return jsonify({"status": True, "data": 1, "messages": []})

    # ---------------- 订单 ---------------- #
    @app.get("/otn/queryOrder/queryMyOrderNoComplete")
    def unfinished_orders():
        pending = [
            {
                "sequence_no": order_id,
                "order_no": order_id,
                "train_code": "G6102",
                "start_train_date": time.strftime("%Y-%m-%d"),
                "from_station_name": "广州南",
                "to_station_name": "长沙南",
                "status_name": "待支付",
            }
            for order_id, payload in state.issued_orders.items()
            if payload.get("status") in ("queued", "success")
        ]
        return jsonify({"status": True, "data": {"orderDBList": pending} if pending else None, "messages": []})

    # ---------------- 候补（afterNate 家族） ----------------
    #
    # ⚠️ 真机的候补接口**不在 confirmPassenger 下，而在 /otn/afterNate/ 下**，
    # 且是一串调用。mock 必须照这个来 —— 之前 mock 按直觉把 confirmHB 挂在
    # confirmPassenger 下，于是「路径写错」在离线环境**永远测不出来**，
    # 真机才以 405 暴露出来（2026-09-29）。
    #
    # 真机链路：chechFace → getSuccessRate → submitOrderRequest
    #          → passengerInitApi → getQueueNum → confirmHB → queryQueue
    @app.post("/otn/confirmPassenger/confirmHB")
    def legacy_confirm_hb():
        """旧路径：真机实测返回 **405**，这里如实复现。

        留着它是为了让「路径又写错了」这类错误在离线测试里立刻现形，
        而不是等到真机上收到一句「响应不是合法 JSON (HTTP 405)」。
        """
        return Response("Method Not Allowed", status=405, mimetype="text/html")

    @app.post("/otn/afterNate/chechFace")
    def hb_check_face():
        failed = _maybe_fail()
        if failed is not None:
            return failed
        form = request.form.to_dict()
        state.record("/otn/afterNate/chechFace", {"secretList_len": len(form.get("secretList", ""))})
        if state.scenario == "risk":
            return jsonify({"status": False, "messages": ["系统忙，请稍后重试"]})
        if state.scenario == "face_required":
            # 复现真机的「需要人证核验」：login_flag 真、face_flag 假
            state.face_flag = False
            return jsonify(
                {
                    "status": True,
                    "data": {"is_show_qrcode": True, "face_flag": False,
                             "login_flag": True, "face_check_code": "10"},
                    "messages": [],
                }
            )
        if not form.get("secretList") or form["secretList"] == "#|":
            # 参数无效时官方给的就是这句（伪装成「忙」）
            return jsonify({"status": False, "messages": ["系统忙，请稍后重试"]})
        state.face_flag = True
        return jsonify(
            {
                "status": True,
                "data": {"is_show_qrcode": True, "face_flag": True,
                         "login_flag": True, "face_check_code": "12"},
                "messages": [],
            }
        )

    @app.post("/otn/afterNate/getSuccessRate")
    def hb_success_rate():
        failed = _maybe_fail()
        if failed is not None:
            return failed
        form = request.form.to_dict()
        state.record("/otn/afterNate/getSuccessRate", {"has_secret": bool(form.get("successSecret"))})
        if not form.get("successSecret"):
            return jsonify({"status": False, "messages": ["系统忙，请稍后重试"]})
        return jsonify(
            {
                "status": True,
                # ``train_no`` 是**官方内部车次号**（真机形如 ``5l000G61020A``），
                # 与明文车次（``G6102``）**不是同一个东西**。这里给一个明确可读的
                # 合成值，唯一目的就是让「hbTrain 到底该用哪个车次号」这件事
                # （对照报告里的 F1）在离线也**可观测** —— 原来回的是
                # secretStr 截断，读起来像 bug，也没人能说清它想表达什么。
                "data": {"flag": [{"level": "1", "info": "候补人数较少", "seat_type_code": "O",
                                   "train_no": "5l000G61020A"}]},
                "messages": [],
            }
        )

    @app.post("/otn/afterNate/submitOrderRequest")
    def hb_submit_request():
        failed = _maybe_fail()
        if failed is not None:
            return failed
        form = request.form.to_dict()
        state.record("/otn/afterNate/submitOrderRequest", {"secretList_len": len(form.get("secretList", ""))})
        if state.scenario == "risk":
            return jsonify({"status": False, "messages": ["操作过于频繁，请稍后再试"]})
        if state.scenario == "standby_rejected":
            return jsonify({"status": True, "data": {"flag": False, "msg": "该车次暂不支持候补"}})
        return jsonify({"status": True, "data": {"flag": True}, "messages": []})

    @app.post("/otn/afterNate/passengerInitApi")
    def hb_passenger_init():
        failed = _maybe_fail()
        if failed is not None:
            return failed
        state.record("/otn/afterNate/passengerInitApi", {})
        # 字段名 ``jzdhDateE`` / ``jzdhHourE`` **有外部出处**，不是本项目猜的：
        # 上游 testerSunshine/12306 的 ``inter/PassengerInitApi.py`` 正是读这两个键
        # 去拼 ``confirmHB`` 的 ``jzParam``（上游 docstring 举例 ``2019-08-31#19#00``）。
        # 原来这里回空对象 → 离线永远测不出「jzParam 该填什么」，
        # 也就是 F2 在离线侧根本不可观测。
        date = (state.last_query or {}).get("date") or time.strftime("%Y-%m-%d")
        return jsonify(
            {
                "status": True,
                "data": {"jzdhDateE": date, "jzdhHourE": "19:00"},
                "messages": [],
            }
        )

    @app.post("/otn/afterNate/getQueueNum")
    def hb_queue_num():
        failed = _maybe_fail()
        if failed is not None:
            return failed
        state.record("/otn/afterNate/getQueueNum", {})
        if state.scenario == "queue_fail":
            return jsonify({"status": True, "data": {"queueNum": [], "flag": False}})
        return jsonify({"status": True, "data": {"queueNum": [], "flag": True}, "messages": []})

    @app.post("/otn/afterNate/confirmHB")
    def confirm_hb():
        failed = _maybe_fail()
        if failed is not None:
            return failed
        form = request.form.to_dict()
        state.record(
            "/otn/afterNate/confirmHB",
            {k: v for k, v in form.items() if k not in ("passengerInfo", "encryptedData")},
        )
        # ⚠️ 这里**故意不校验段数** —— 这是一个必须写下来的教训。
        #
        # 2026-09-29：本 mock 曾硬校验「每名乘客 6 段」，而那个 6 段是**照我们自己的
        # 实现写的**。于是它只证明「我和我自己一致」，与当年 confirmHB 路径整族写错
        # （写成 confirmPassenger/confirmHB）却在离线全绿，是**同一个机制**。
        # 逐接口对照上游后的事实：testerSunshine/12306 是 **5 段**，本项目旧写法是 6 段，
        # **谁对尚无真机证据**。所以 mock 不能参与判定 —— 段数之争交给真机探针。
        #
        # mock 只校验两个两边都成立的结构事实：
        #   ① 至少到 allEncStr 那一段（上游 5 段写法的下界）；
        #   ② allEncStr（下标 4，两种写法的位置一致）非空 —— 空串官方必拒。
        info = form.get("passengerInfo", "")
        chunks = [c for c in info.split(";") if c.strip()]
        if not chunks or any(len(c.split("#")) < 5 for c in chunks):
            return jsonify({"status": True, "data": {"flag": False, "msg": "乘车人信息格式不正确"}})
        if any(not c.split("#")[4].strip() for c in chunks):
            return jsonify({"status": True, "data": {"flag": False, "msg": "乘车人缺少加密串，无法候补"}})
        if state.scenario == "risk":
            return jsonify({"status": False, "messages": ["操作过于频繁，请稍后再试"]})
        if len(state.standby_orders) >= 6:
            return jsonify({"status": True, "data": {"flag": False, "msg": "候补订单已达上限"}})
        hb_id = f"HB{int(time.time() * 1000)}"
        state.standby_orders[hb_id] = {
            "hborderId": hb_id, "status": "queuing", "created_at": time.time(), "polls": 0,
        }
        return jsonify({"status": True, "data": {"flag": True, "isAsync": True, "hborderId": hb_id}, "messages": []})

    @app.post("/otn/afterNate/queryQueue")
    def hb_query_queue():
        failed = _maybe_fail()
        if failed is not None:
            return failed
        state.record("/otn/afterNate/queryQueue", {})
        if state.scenario == "queue_fail":
            return jsonify({"status": True, "data": {"flag": True, "isAsync": True, "status": -1}})
        if state.scenario == "queue_timeout":
            return jsonify({"status": True, "data": {"flag": True, "isAsync": True, "status": 2, "waitTime": 9}})
        # 排队第 3 次时「兑现」—— 兑现是异步的，而且**只在订单里体现**。
        # 客户端判断兑现的正确依据就是「我的订单里有没有出现这一趟」，
        # mock 必须照这个来，否则会把错误的判据测成绿的。
        for hb_id, payload in state.standby_orders.items():
            payload["polls"] = payload.get("polls", 0) + 1
            if payload["polls"] >= 3 and not payload.get("fulfilled"):
                payload["fulfilled"] = True
                query = state.last_query or {}
                state.fulfilled_orders.append(
                    {
                        "sequence_no": hb_id.replace("HB", "E"),
                        "train_code_page": "G6102",
                        "start_train_date_page": f"{query.get('date', time.strftime('%Y-%m-%d'))} 08:12",
                        "from_station_name_page": ["广州南"],
                        "to_station_name_page": ["长沙南"],
                        "ticket_total_price_page": "314.00",
                        "array_passser_name_page": ["张三"],
                        "order_status_name": "已支付",
                    }
                )
        # 默认：候补单已生效、待支付（status==1，真机语义）
        return jsonify({"status": True, "data": {"flag": True, "isAsync": True, "status": 1}})

    @app.post("/otn/queryOrder/cancelNoCompleteMyOrder")
    def cancel_no_complete_order():
        """取消未完成订单 —— 候补单取消走的也是这里。

        （``cancelNoCompleteMyOrder`` 是官方 train_order 页面用的接口；
        真机上候补单取消的确切参数尚未核对，所以这条只保证「离线够用」。）
        """
        order_id = request.form.get("orderId") or request.form.get("sequence_no", "")
        payload = state.standby_orders.get(order_id)
        if payload:
            payload["status"] = "cancelled"
        state.record("/otn/queryOrder/cancelNoCompleteMyOrder", {"orderId": order_id})
        return jsonify({"status": True, "data": {"flag": True}, "messages": []})

    @app.post("/otn/afterNate/cancelHB")
    def cancel_hb():
        hb_id = request.form.get("hborderId") or request.form.get("orderId", "")
        payload = state.standby_orders.get(hb_id)
        if payload:
            payload["status"] = "cancelled"
        state.record("/otn/afterNate/cancelHB", {"hborderId": hb_id})
        return jsonify({"status": True, "data": payload, "messages": []})

    # ---------------- 控制面 ---------------- #
    @app.post("/__mock__/scenario")
    def set_scenario():
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        scenario = str(data.get("scenario", ""))
        ok = state.set_scenario(scenario)
        return jsonify({"ok": ok, "scenario": state.scenario, "available": list(SCENARIOS)})

    @app.post("/__mock__/qr")
    def set_qr():
        """测试/演示用：直接指定扫码状态，或指定扫码成功后返回的账号名。

        例：``{"state": "confirmed", "username": "fan_0723@qq.com"}``
        """
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        state_name = str(data.get("state", ""))
        uuid = str(data.get("uuid", ""))
        username = str(data.get("username", ""))
        with state.lock:
            if username:
                state.qr_username = username
        ok = state.set_qr_state(state_name, uuid) if state_name else True
        return jsonify(
            {
                "ok": ok,
                "state": state_name or state.qr_default_state,
                "username": state.qr_username,
                "available": list(QR_MOCK_STATES),
            }
        )

    @app.post("/__mock__/query-route")
    def set_query_route():
        """测试/演示用：指定余票页里 ``var CLeftTicketUrl`` 的取值。

        用来让「不同路径策略最终打到哪条查询路径」变成**可观测**的事实：
        设成 ``leftTicket/queryA`` 后，只有真去读了页面并采用它的客户端
        才会打到 queryA；盲试候选列表的客户端仍会先打 queryZ。
        """
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        with state.lock:
            state.query_route = str(data.get("route", "")).strip()
        return jsonify({"ok": True, "query_route": state.query_route})

    @app.get("/__mock__/state")
    def get_state():
        with state.lock:
            return jsonify(
                {
                    "scenario": state.scenario,
                    "orders": state.issued_orders,
                    "standby_orders": state.standby_orders,
                    "requests": len(state.requests),
                    "logged_in": sorted(state.logged_in_users),
                    "qr_polls": dict(state.qr_polls),
                    "qr_username": state.qr_username,
                    "query_route": state.query_route,
                }
            )

    @app.post("/__mock__/reset")
    def reset():
        with state.lock:
            state.order_seq = 100000
            state.queue_polls.clear()
            state.issued_orders.clear()
            state.standby_orders.clear()
            state.requests.clear()
            state.ticket_stock = {"二等座": 5, "一等座": 2, "商务座": 1, "硬卧": 3, "硬座": 20, "无座": 50}
            state.qr_polls.clear()
            state.qr_override.clear()
            state.qr_default_state = ""
            state.qr_expire_after_polls = 0
            state.session_warmed = False
            state.apptk = ""
            state.qr_ticket = ""
        return jsonify({"ok": True})

    return app


def run_mock_server(port: int = 18006, scenario: str = "has_ticket", *, host: str = "127.0.0.1") -> None:
    app = create_mock_app(scenario)
    app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_mock_server()
