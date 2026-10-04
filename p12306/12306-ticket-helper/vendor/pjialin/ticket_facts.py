"""从上游原件**逐字切出**的协议事实（生成物，不要手改）。

上游出处：

- ``py12306/query/job.py`` —— INDEX_* 字段下标常量（sha256 ``52ce4227a538``）
- ``py12306/query/query.py`` —— 从余票页 HTML 读查询路径的正则（sha256 ``ca0c915c30f4``）
- ``py12306/helpers/api.py`` —— 余票查询的 URL 模板（含 {type} 占位）（sha256 ``125bcd97c427``）
- ``py12306/helpers/api.py`` —— 用户域端点路径（登录态 / 用户信息 / 乘车人 / 扫码）（sha256 ``125bcd97c427``）
- ``py12306/user/job.py`` —— 上游读响应用的点号字段路径（sha256 ``89e4560fcf11``）
- ``py12306/user/job.py`` —— 扫码轮询的请求字段名与 result_code 分支值（sha256 ``89e4560fcf11``）
- ``py12306/helpers/api.py`` —— 下单六步的端点路径（sha256 ``125bcd97c427``）
- ``py12306/order/order.py`` —— 下单五步的表单字段名与固定取值（sha256 ``7f225ec58a7a``）
- ``py12306/user/job.py`` —— initDc 表单与 token/滑块标记的取法（sha256 ``89e4560fcf11``）
- ``py12306/order/order.py`` —— 滑块风控的表单附加字段与 scene 取值（sha256 ``7f225ec58a7a``）
- ``py12306/config.py`` —— 查询节奏与请求超时的默认值（sha256 ``1d082d3ba100``）
- ``py12306/helpers/func.py`` —— 数字→间隔区间、区间→随机等待的两个函数（sha256 ``cbfc35f6f8aa``）

为什么单独生成这一份
--------------------
`py12306.query.job` 一 import 就拉起 `order` / `user` / `helpers.qrcode`
（→ `png` / `pyppeteer`）一大片，而这里要的只是 10 个整数常量与一条正则。
生成（而不是手抄）才能被机器验证：本文件由
`scripts/build_vendor_pjialin.py` 产出，`--check` 会**重新生成一遍再比对**。

用途：`tests/test_query_baseline.py` 拿它当**外部锚点**校验本项目的字段下标，
`kernel/pjialin/query_route.py` 用它读余票页给出的查询路径。
"""

from __future__ import annotations

import ast

#: 余票结果（``|`` 分隔）的字段下标 —— 变量名与取值都保持上游原样。
#: ⚠️ 名字只反映上游当年的理解，**不要**按名字推断语义：
#: 例如 ``INDEX_TICKET_NUM``（下标 11）实际被上游拿去和 ``'Y'`` 比较
#: （见 ``is_has_ticket``），那是「可否网订」而不是「余票数」。
INDEX_TICKET_NUM = 11
INDEX_TRAIN_NUMBER = 3
INDEX_TRAIN_NO = 2
INDEX_LEFT_DATE = 13
INDEX_LEFT_STATION = 6
INDEX_ARRIVE_STATION = 7
INDEX_ORDER_TEXT = 1
INDEX_SECRET_STR = 0
INDEX_LEFT_TIME = 8
INDEX_ARRIVE_TIME = 9

#: 上游从余票页 HTML 里取查询路径的正则**源码文本**（逐字，含原始转义）。
ROUTE_REGEX_SOURCE = r"""r'var CLeftTicketUrl = \'(.*)\';'"""

#: 真正可用的正则：对上面的源码文本求值（它本来就是 Python 字符串字面量）。
#: 这样「逐字」与「可用」两件事不用二选一 —— 也避免了手抄转义。
ROUTE_PATTERN = ast.literal_eval(ROUTE_REGEX_SOURCE)

#: 上游从中取出的分组就是查询路径，例如 ``leftTicket/queryZ``。
ROUTE_GROUP = 1

#: 余票查询的 URL 模板（上游 ``helpers/api.py`` 的 ``LEFT_TICKETS['url']``，
#: **已去掉 BASE_URL_OF_12306 前缀**，逐字切出）。
#: 用它把页面给的 ``type`` 还原成上游会请求的完整路径 ——
#: 这样「上游片段 → 本项目路径」的转换有出处，不是我们自己发明的拼接。
ROUTE_URL_TEMPLATE = r"""/otn/{type}?leftTicketDTO.train_date={left_date}&leftTicketDTO.from_station={left_station}&leftTicketDTO.to_station={arrive_station}&purpose_codes=ADULT"""

#: 用户域端点**路径**（上游 ``helpers/api.py``，逐字切出，已去掉 BASE_URL 前缀）。
#: ⚠️ ``API_USER_LOGIN_CHECK`` 指 ``/otn/login/conf`` —— 与本项目登录态检测用的
#: ``/otn/login/checkUser`` **不是同一条**。这正是 ``login_check`` 域的差异点。
USER_ENDPOINTS = {
    'API_USER_LOGIN_CHECK': '/otn/login/conf',
    'API_AUTH_QRCODE_BASE64_DOWNLOAD': '/passport/web/create-qr64',
    'API_AUTH_QRCODE_CHECK': '/passport/web/checkqr',
    'API_USER_INFO': '/otn/modifyUser/initQueryUserInfoApi',
    'API_USER_PASSENGERS': '/otn/confirmPassenger/getPassengerDTOs',
}

#: 上游读响应用的**点号字段路径**（上游 ``user/job.py``，按函数归类）。
#: 上游是「点号路径直接取嵌套」（``result.get('data.is_login')``），
#: 本项目是逐层取（``first_of``）—— 两种写法必须能对上，
#: 由 ``tests/test_user_baseline.py`` 逐项校验。
USER_RESPONSE_FIELDS = {
    'check_user_is_login': 'data.is_login',
    'get_user_info': 'data.userDTO.loginUserDTO',
    'response_login_check': 'data.noLogin',
    'get_user_passengers': 'data.normal_passengers',
    'can_access_passengers': 'data.normal_passengers',
}

#: 语义别名 —— 把上游常量名翻译成本项目调用点用的名字。
#: 有了它们，`kernel/pjialin/**` 里就不必出现上游常量名（少一处将来会腐烂的耦合）。
#: 值仍然来自上面两张表，**不是重新抄一遍**。
LOGIN_CHECK_PATH = USER_ENDPOINTS["API_USER_LOGIN_CHECK"]
USER_INFO_PATH = USER_ENDPOINTS["API_USER_INFO"]
PASSENGERS_PATH = USER_ENDPOINTS["API_USER_PASSENGERS"]
QR_CREATE_PATH = USER_ENDPOINTS["API_AUTH_QRCODE_BASE64_DOWNLOAD"]
QR_CHECK_PATH = USER_ENDPOINTS["API_AUTH_QRCODE_CHECK"]

#: 上游判读「是否登录」用的字段：``data.is_login``，且与 ``'Y'`` 比较。
LOGIN_ALIVE_FIELD = USER_RESPONSE_FIELDS["check_user_is_login"]
LOGIN_ALIVE_VALUES = ("Y",)
#: 上游读用户信息用的是**点号路径直接取嵌套**（``data.userDTO.loginUserDTO``）。
USER_INFO_FIELD = USER_RESPONSE_FIELDS["get_user_info"]
#: 乘车人列表所在字段。
PASSENGERS_FIELD = USER_RESPONSE_FIELDS["get_user_passengers"]

#: 扫码轮询（``checkqr``）上游往表单里放的字段名（逐字切出）。
#: ⚠️ 前两个是本项目**没有**放的 —— 差异留档在此，供基线对照。
QR_CHECK_FIELDS = (
    'RAIL_DEVICEID',
    'RAIL_EXPIRATION',
    'uuid',
    'appid',
)

#: 上游 ``qr_login`` 按这几个 ``result_code`` 值分支：
#: 0 继续等 / 1 提示「请确认登录」/ 2 跳出（成功）/ 3 重新下载（过期）。
#: 本项目的 ``QR_STATE_MAP`` 必须指向**同一组数字**。
QR_RESULT_CODES = (0, 1, 2, 3)

#: 上游扫码用的 ``appid`` 取值（官方固定值）。
QR_APPID = 'otn'

#: 下单六步的端点**路径**（上游 ``helpers/api.py``，逐字切出，已去掉 BASE_URL 前缀）。
#: ⚠️ 上游把查询串写进了 ``API_QUERY_ORDER_WAIT_TIME``（``...?{}``）；这里只留路径。
ORDER_PATHS = {
    'submit': '/otn/leftTicket/submitOrderRequest',
    'init_dc': '/otn/confirmPassenger/initDc',
    'check_order_info': '/otn/confirmPassenger/checkOrderInfo',
    'get_queue_count': '/otn/confirmPassenger/getQueueCount',
    'confirm_single_for_queue': '/otn/confirmPassenger/confirmSingleForQueue',
    'query_wait_time': '/otn/confirmPassenger/queryOrderWaitTime',
}

#: 下单各步的表单**字段名**（顺序 = 上游拼装顺序）。
ORDER_FORM_FIELDS = {
    'submit': (
        'secretStr',
        'train_date',
        'back_train_date',
        'tour_flag',
        'purpose_codes',
        'query_from_station_name',
        'query_to_station_name',
    ),
    'check_order_info': (
        'cancel_flag',
        'bed_level_order_num',
        'passengerTicketStr',
        'oldPassengerStr',
        'tour_flag',
        'randCode',
        'whatsSelect',
        '_json_att',
        'REPEAT_SUBMIT_TOKEN',
    ),
    'get_queue_count': (
        'train_date',
        'train_no',
        'stationTrainCode',
        'seatType',
        'fromStationTelecode',
        'toStationTelecode',
        'leftTicket',
        'purpose_codes',
        'train_location',
        '_json_att',
        'REPEAT_SUBMIT_TOKEN',
    ),
    'confirm_single_for_queue': (
        'passengerTicketStr',
        'oldPassengerStr',
        'randCode',
        'purpose_codes',
        'key_check_isChange',
        'leftTicketStr',
        'train_location',
        'choose_seats',
        'seatDetailType',
        'whatsSelect',
        'roomType',
        'dwAll',
        '_json_att',
        'REPEAT_SUBMIT_TOKEN',
    ),
    'query_wait_time': (
        'random',
        'tourFlag',
        '_json_att',
        'REPEAT_SUBMIT_TOKEN',
    ),
    'init_dc': (
        '_json_att',
    ),
}

#: 表单里上游**写死的取值**（只收字面量）。
#: 动态取值不在其中 —— 那正是要与本项目逐项对照的部分。
#: ⚠️ ``check_order_info.cancel_flag`` 上游写的是**整数** ``2``，本项目写的是字符串 ``"2"``：
#: 表单编码后都是 ``2``，但这个差异要留在能被看见的地方。
ORDER_FORM_CONST = {
    'submit': {'tour_flag': 'dc', 'purpose_codes': 'ADULT'},
    'check_order_info': {'cancel_flag': 2, 'bed_level_order_num': '000000000000000000000000000000', 'tour_flag': 'dc', 'randCode': '', 'whatsSelect': '1', '_json_att': ''},
    'get_queue_count': {'_json_att': ''},
    'confirm_single_for_queue': {'randCode': '', 'choose_seats': '', 'seatDetailType': '000', 'whatsSelect': '1', 'roomType': '00', 'dwAll': 'N', '_json_att': ''},
    'query_wait_time': {'tourFlag': 'dc', '_json_att': ''},
    'init_dc': {'_json_att': ''},
}

#: 滑块风控：``check_order_info`` 里 ``data.update({...})`` 追加的字段
#: （只在 ``is_slide`` 为真时追加 —— 上游至此才知道这一单要不要过滑块）。
ORDER_SLIDE_FIELDS = (
    'sessionId',
    'sig',
    'scene',
)
ORDER_SLIDE_CONST = {'scene': 'nc_login'}
#: 上游等滑块时用的 DOM 选择器。
ORDER_SLIDE_SELECTOR = '#slide_passcode .nc-lang-cnt'
#: 上游判「这一单要不要过滑块」的正则**源码文本**（逐字，含原始转义）。
ORDER_SLIDE_STATE_REGEX_SOURCE = 'r"var if_check_slide_passcode.*=\'(\\d?)\'"'

#: ---- P4 节奏 / 参数（A15 / A17）----
#: 上游 ``config.py`` 的默认值（逐字切出）。
QUERY_INTERVAL = 1
USER_HEARTBEAT_INTERVAL = 120
TIME_OUT_OF_REQUEST = 5

#: 上游那两个函数的**源码文本**（逐字）。适配层 exec 它们来算间隔区间 ——
#: 「一个数字怎么换算成区间」的权威实现是上游那一份，不是我们抄的公式。


INIT_INTERVAL_BY_NUMBER_SOURCE = r"""
def init_interval_by_number(number):
    if isinstance(number, dict):
        min = float(number.get('min'))
        max = float(number.get('max'))
    else:
        min = number / 2
        max = number
    return {
        'min': min,
        'max': max
    }
"""

GET_INTERVAL_NUM_SOURCE = r"""
def get_interval_num(interval, decimal=2):
    return round(random.uniform(interval.get('min'), interval.get('max')), decimal)
"""
