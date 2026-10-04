"""12306 官方接口封装（继承 Py12306 思路，改造为可注入 base_url 以支持 mock）。

调用约定：
- 所有方法返回结构化结果或抛出 :mod:`ticket_helper.errors` 下的类型化错误；
- 不在此层做业务判断（如「该不该抢票」），只负责协议交互与协议级解析；
- 会话（Cookie）由传入的 :class:`~ticket_helper.inter.base.HttpClient` 持有，
  每个账号一个客户端，天然隔离。
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Mapping, Sequence

from ticket_helper.errors import (
    CaptchaUnavailableError,
    InterError,
    LoginError,
    QueueError,
    ResponseParseError,
    RiskControlError,
    SoldOutError,
    TrainDateUnavailableError,
    ValidationError,
    VerifyCodeError,
    VerifyRequiredError,
)
from ticket_helper.inter import urls
from ticket_helper.inter.base import HttpClient, HttpResponse, expect_json, is_login_required
from ticket_helper import train_date as train_date_util
from ticket_helper.kernel import build_jz_param, get_standby_payload
from ticket_helper.kernel.domains.login_check import (
    ALIVE,
    DEAD,
    LoginProbeSpec,
    alive_of,
)
from ticket_helper.kernel.domains.order_submit import (
    STEP_CHECK_ORDER_INFO,
    STEP_CONFIRM_SINGLE_FOR_QUEUE,
    STEP_GET_QUEUE_COUNT,
    STEP_INIT_DC,
    STEP_QUERY_WAIT_TIME,
    STEP_SUBMIT,
    STEPS,
    OrderFormsSpec,
    OrderStepForm,
    build_form,
)
from ticket_helper.kernel.domains.slide import SlideResult, SlideSpec, needs_slide
from ticket_helper.kernel.domains.qr_check import QrCheckSpec
from ticket_helper.inter.parser import (
    CHECK_USER_ALIVE_PATHS,
    SEAT_CODE,
    TrainTicket,
    build_old_passenger_str,
    build_passenger_ticket_str,
    parse_init_dc_html,
    parse_left_ticket_payload,
    parse_order_history,
    parse_passengers_payload,
    parse_qr_check,
    parse_qr_create,
    parse_user_profile,
    qr_state_of,
    truthy_flag,
    unwrap_user_payload,
)
from ticket_helper.inter.station import StationMap

log = logging.getLogger(__name__)

#: 登录鉴权类接口要求的 Referer（实测：换成登录页 Referer 会被 12306 判为非法请求，
#: 返回 HTML 错误页而不是 JSON）。详见 ``TicketApi._auth_headers``。
PASSPORT_REFERER_PATH = "/otn/passport?redirect=/otn/login/userLogin"

#: 验证码校验成功码
CAPTCHA_OK_CODES = ("4", 4)

#: 排队轮询上限（次）与间隔（秒）
QUEUE_MAX_POLLS = 60

#: 订单查询「参数组合兜底」两次尝试之间要隔多久（秒）。
#: 真机实测订单接口对连续请求很敏感：一口气打 4 次就会被 WAF 拦
#: （返回空 ``data`` 或 HTML 错误页），于是「查不到」被自己放大成「一直被拒」。
ORDER_RETRY_INTERVAL = 0.6
QUEUE_POLL_INTERVAL = 1.0


@dataclass
class LoginResult:
    ok: bool = False
    username: str = ""
    message: str = ""
    need_captcha: bool = False
    risk: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "username": self.username,
            "message": self.message,
            "need_captcha": self.need_captcha,
            "risk": self.risk,
        }


@dataclass
class Captcha:
    """一次验证码挑战。"""

    rand: str = "sjrand"
    image_base64: str = ""
    image_bytes: bytes = b""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def answer_hint(self) -> str:
        """部分接口会返回提示语（例如「请点击图中的『小笼包』」）。"""
        return str(self.raw.get("result_message") or "")


@dataclass
class QrTicket:
    """一次扫码登录的二维码。

    ``uuid`` 是后续轮询扫码状态的凭据；``image_base64`` 是**裸 base64**，
    展示层自行拼 ``data:image/png;base64,`` 前缀。
    """

    uuid: str = ""
    image_base64: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.uuid)


@dataclass
class QueueResult:
    """排队结果。

    ``status`` 语义：
    - ``waiting`` 仍在排队
    - ``success`` 出票成功（带 order_id）
    - ``failed``  排队明确失败（如余票被抢完）
    - ``timeout`` 轮询耗尽但结果未知 —— **不能当作失败**，需要人工去未完成订单确认
    """

    status: str = "waiting"
    order_id: str = ""
    wait_count: int = 0
    wait_time: int = 0
    message: str = ""
    polls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "order_id": self.order_id,
            "wait_count": self.wait_count,
            "wait_time": self.wait_time,
            "message": self.message,
            "polls": self.polls,
        }


class TicketApi:
    """12306 接口门面。"""

    def __init__(
        self,
        client: HttpClient,
        station_map: StationMap,
        route_resolver: Callable[[str], Sequence[str]] | None = None,
        login_spec: LoginProbeSpec | None = None,
        qr_spec: QrCheckSpec | None = None,
        order_spec: OrderFormsSpec | None = None,
        slide_solver: Any | None = None,
    ) -> None:
        self.client = client
        self.stations = station_map
        #: 由 initDc 获得，下单流程内共享
        self.repeat_submit_token: str = ""
        self.ticket_info: dict[str, Any] = {}
        #: 会话是否已经「热身」过（加载过一次页面）。
        #: 真机上没热身就直接查余票会被 WAF 拒，见 ``ensure_session_warm``。
        self._warmed = False
        #: 余票页正文（热身时顺手缓存）。**它携带官方当前的查询路径**，
        #: 见 :meth:`query_route_candidates` —— 读它不多发任何请求。
        self._init_page: str = ""
        #: 「余票查询路径怎么取」的策略，由装配方（composition root）注入。
        #: 不注入时退回固定候选列表（= 改造前的行为）。
        #: 参数是余票页正文，返回**按优先级排列**的路径列表。
        #: 这里收的是**可调用对象**而不是某个具体实现 —— 接口层不认识「哪套内核」，
        #: 灰度配置也就不会泄漏进接口层。
        self._route_resolver = route_resolver
        #: 登录态探测的规格（数据，不是实现）。未注入时用协议层的默认规格 ——
        #: 与 `kernel/self/login_check.py` 产出的是**同一个**，由契约测试钉住。
        self._login_spec = login_spec or self._builtin_login_spec()
        #: 订单查询里「哪一档参数曾经成功过」——记住它，下次直接用，
        #: 免得每次都从 30 天窗口开始把 3 档全试一遍（那本身就会招来风控）。
        self._order_combo_hint: tuple[int, int] | None = None
        #: 扫码轮询请求体的规格（数据，不是实现）。未注入时用协议层的默认规格 ——
        #: 与 `kernel/self/qr_check.py` 产出的是**同一个**，由契约测试钉住。
        self._qr_spec = qr_spec or self._builtin_qr_spec()
        #: 下单六步的请求规格（数据，不是实现）。未注入时用协议层的默认规格 ——
        #: 与 `kernel/self/order_submit.py` 产出的是**同一个**，由契约测试钉住。
        #:
        #: ⚠️ 注意本行**必须**在 `_order_combo_hint` 之后：往 `__init__` 里插东西
        #: 时必须检查「原来紧跟在后面的那一行有没有被挤进函数体」——
        #: 本项目真出过一次（见 `docs/缺陷回流台账.md` #23：被挤走的属性变成死代码，
        #: 抛出的 AttributeError 又被 `log.debug` 吞掉，监控死循环 20 分钟）。
        self._order_spec = order_spec or self._builtin_order_spec()
        #: 过滑块的后端（**对象**，不是规格：它要起一个真浏览器）。
        #: 未注入 = 本项目没有程序化解法 —— 这时只留一条 WARNING、照旧往下走，
        #: 与改造前的行为完全一致（**回退不允许引入新的失败点**）。
        self._slide_solver = slide_solver
        #: 判断「这一单要不要过滑块」的规格。**未注入也要有** ——
        #: 判读本身不该取决于用哪套实现（它只是读 initDc 页面里的一个数字）。
        #: ★ 取不到时必须退回内置那份，理由见 `_resolve_slide_spec` 的 docstring。
        self._slide_spec = self._resolve_slide_spec(slide_solver)
        #: 本单的滑块结果（``sessionId``/``sig``）。**必须在这里声明**：
        #: 往 `__init__` 里加属性时若把它挤进别的方法体，会变成永不执行的死代码
        #: —— 本项目出过一模一样的事故（回流 #23）。
        self._slide_values: dict[str, str] = {}

    @staticmethod
    def _builtin_login_spec() -> LoginProbeSpec:
        """没有被注入策略时用的默认规格。

        **请求侧的四个值只从协议层取**（`inter/urls.py::CHECK_USER_PROBE`）、
        判活字段只从 `inter/parser.py::CHECK_USER_ALIVE_PATHS` 取 ——
        这里一个协议字面量都不写。否则「切了后端但请求没变」不会有任何征兆。
        """
        return LoginProbeSpec(
            name="self",
            alive_paths=CHECK_USER_ALIVE_PATHS,
            **urls.CHECK_USER_PROBE,
        )

    # ================================================================== #
    # 登录
    # ================================================================== #
    @staticmethod
    def _builtin_qr_spec() -> QrCheckSpec:
        """没有被注入策略时用的默认规格。

        与 `_builtin_login_spec` 同样是「接口层不能 import 具体后端（会成环），
        所以规格值只能从协议层取」——这里只引用 `inter/urls.py` 的常量。
        """
        return QrCheckSpec(
            name="self",
            path=urls.CHECK_QR,
            form_keys=urls.QR_CHECK_FORM_KEYS,
            constants=urls.QR_CHECK_CONSTANTS,
        )

    @staticmethod
    def _builtin_order_spec() -> OrderFormsSpec:
        """没有被注入下单规格时用的默认规格。

        与 `_builtin_login_spec` / `_builtin_qr_spec` 同理：接口层不能 import
        具体后端（会成环），所以规格值**只从协议层取**
        （`inter/urls.py::ORDER_STEP_FORMS`）。本文件里一个协议字面量都不写 ——
        否则「切了后端但请求没变」不会有任何征兆。
        """
        return OrderFormsSpec(
            name="self",
            forms={
                step: OrderStepForm(step=step, **urls.ORDER_STEP_FORMS[step])
                for step in STEPS
            },
        )

    @classmethod
    def _resolve_slide_spec(cls, slide_solver: Any) -> SlideSpec:
        """取「这一单要不要过滑块」的判读规格；**任何情况下都要拿到一份可用的**。

        ★ 为什么不能让它抛：`TicketApi` 是在**构造 User** 时建的，而 User 是在
        **恢复账号**时建的。滑块只是「可选能力」——它的后端拿不到规格时
        （最现实的场景：vendor 没进包 / 依赖没收全，`import ticket_facts` 失败），
        正确行为是「这一单不过滑块」，**不是让整条账号恢复失败**。

        真机上这一幕极难查（P5 打包排查时抓到过）：日志里只有一句
        「恢复账号 X 失败: No module named 'ticket_facts'」，界面上是「0 个账号」
        —— 看起来像数据丢了，而根因在两跳之外。

        判读规格本身**与后端无关**（就是读 initDc 页面里的一个数字），
        所以退回内置那份不改变判读结果（两边同源由契约测试钉住）。
        这与该域既有的设计一致：**回退不允许引入新的失败点**。
        """
        if slide_solver is None:
            return cls._builtin_slide_spec()
        try:
            return slide_solver.spec()
        except Exception as exc:  # noqa: BLE001 - 任何失败都不该打断账号恢复
            log.warning(
                "滑块后端 %s 的判读规格取不到（%s: %s），已退回内置规格；"
                "这一单不会过滑块（人工兜底仍然有效）",
                getattr(slide_solver, "name", "?"),
                type(exc).__name__,
                exc,
            )
            return cls._builtin_slide_spec()

    @staticmethod
    def _builtin_slide_spec() -> SlideSpec:
        """没有被注入滑块后端时用的判断规格。

        与另外几个默认规格同理：接口层不许 import 具体后端（会成环），
        所以只从协议层取（`inter/urls.py` 的两条常量）。
        """
        return SlideSpec(
            name="self",
            state_pattern_source=urls.ORDER_SLIDE_STATE_PATTERN,
            selector=urls.ORDER_SLIDE_SELECTOR,
            scene=urls.ORDER_SLIDE_SCENE,
        )

    def _order_form(self, step: str) -> OrderStepForm:
        """取某一步的请求规格（步骤名非法抛 `InterError`，不静默给默认值）。"""
        return self._order_spec.form(step)

    def _order_context(self, **values: Any) -> dict[str, Any]:
        """下单各步的「取值袋」。

        规格（`kernel/domains/order_submit.py`）决定**用哪一个、要不要用**；
        这里只管把这次会话拿得到的值都放进来。两套后端需要的取值不同 ——
        上游 `back_train_date` 用**出发日**、本项目用**今天**；
        上游 `random` 用**随机小数串**、本项目用**毫秒时间戳** ——
        所以袋子必须**同时装着**两者，否则切后端会在第一步就报缺值。
        """
        today = time.strftime("%Y-%m-%d")
        bag: dict[str, Any] = {
            "today": today,
            "back_train_date": today,
            "random_ms": int(time.time() * 1000),
            "random_fraction": str(random.random())[2:],
            "randCode": "",
            "choose_seats": "",
            "tour_flag": "dc",
        }
        bag.update(values)
        return bag

    def _order_request(self, form: OrderStepForm, data: Mapping[str, Any]) -> HttpResponse:
        """按规格发一次下单请求（路径 / 方法 / Referer 全部来自规格）。"""
        kwargs: dict[str, Any] = {"allow_retry": False}
        if form.referer:
            kwargs["referer"] = self.client.url(form.referer)
        if form.method == "get":
            return self.client.get(form.path, params=dict(data), **kwargs)
        return self.client.post(form.path, data=dict(data), **kwargs)

    def login_conf(self) -> dict[str, Any]:
        response = self.client.get(urls.LOGIN_CONF, allow_retry=False)
        payload = expect_json(response, allow_empty=True) or {}
        return payload.get("data") or payload

    def get_login_captcha(self, *, module: str = "login") -> Captcha:
        """获取登录图形验证码（VC-01）。

        两个候选路径，**新路径优先**：

        1. ``GET /otn/passcodeNew/getPassCodeNew`` —— 2026 版登录页在用的接口，
           直接返回图片字节流；
        2. ``GET /passport/captcha/captcha-image64`` —— 旧接口，返回 base64 JSON。

        为什么要有第 2 条：真机实测旧路径**已经被 WAF 挡掉**（任何请求头组合都回
        HTML 错误页），新路径在缺少设备指纹上下文时会回
        ``{"status":false,"messages":["系统忙，请稍后重试"]}``。
        也就是说**两条都可能拿不到图**，这时必须把「被风控拒绝」和「识别失败」分开报，
        否则使用者只会看到一句没有信息量的「验证码获取失败」，无从判断该换扫码登录。
        """
        stamp = int(time.time() * 1000)
        attempts: list[str] = []

        for path, params in (
            (urls.LOGIN_PASS_CODE, {"module": module, "rand": "sjrand", "_": stamp}),
            (urls.CAPTCHA_IMAGE_64,
             {"login_site": "E", "module": module, "rand": "sjrand", "_": stamp}),
        ):
            try:
                response = self.client.get(path, params=params, allow_retry=False)
            except InterError as exc:
                attempts.append(f"{path}: 请求异常({exc})")
                continue

            if self._is_html_response(response):
                # 12306 用 HTML 错误页表达「请求不被接受」，不是业务错误
                attempts.append(f"{path}: 被风控拒绝（返回 HTML 错误页）")
                continue

            content_type = ""
            raw = getattr(response, "raw", None)
            if raw is not None and getattr(raw, "headers", None):
                content_type = str(raw.headers.get("Content-Type", "") or "")

            image_bytes = b""
            payload: dict[str, Any] = {}
            if "image" in content_type.lower():
                image_bytes = raw.content if raw is not None else b""
            else:
                payload = expect_json(response, allow_empty=True) or {}
                image_b64 = payload.get("image") or ""
                if image_b64:
                    try:
                        image_bytes = base64.b64decode(image_b64)
                    except Exception:  # noqa: BLE001 - 图片坏了就当这次失败
                        image_bytes = b""

            if image_bytes:
                return Captcha(
                    rand="sjrand",
                    image_base64=base64.b64encode(image_bytes).decode("ascii"),
                    image_bytes=image_bytes,
                    raw=payload,
                )

            reason = (
                payload.get("result_message")
                or (payload.get("messages") or [None])[0]
                or f"未返回图片（Content-Type={content_type or '未知'}）"
            )
            attempts.append(f"{path}: {reason}")

        raise CaptchaUnavailableError(
            "登录验证码获取失败：官方接口当前不接受程序化请求。"
            "建议改用「扫码登录」（无需验证码）。",
            detail="; ".join(attempts),
        )

    def check_login_captcha(self, answer: str, rand: str = "sjrand") -> bool:
        """校验登录验证码答案（如 ``"112,45"``）。

        ⚠️ **端点换过**：原先用 ``/passport/captcha/captcha-check`` —— 那正是
        上游 ``helpers/api.py::API_AUTH_CODE_CHECK`` 用的路径。但真机实测这一族
        **已被 WAF 挡**（回 HTML 错误页而不是 4xx），于是这个函数**必然失败**，
        而症状只是「验证码不对」，完全看不出是端点问题。

        现在与下单验证码共用 ``/otn/passcodeNew/checkRandCodeAnsyn``。
        换取端点由决策 §10-4 授权（「算法/渠道/换算仍全用上游」）——
        因为照抄上游那条在真机上根本走不通。

        ⇒ 这是「**上游有的功能，在真机上不可用**」的实例之一：
        正确处理是保留可用的那条并把原因记录在案，而不是照着上游写一条
        注定走不通的路径（那只是让源码看起来「用了上游」）。
        """
        response = self.client.post(
            # 登录验证码校验：与下单共用这条（旧端点已被 WAF 挡，见上）
            urls.CHECK_RAND_CODE,  # LOGIN_CAPTCHA_CHECK
            data={"answer": answer, "randCode": answer, "rand": rand, "_json_att": ""},
            allow_retry=False,
            referer=self.client.url("/otn/resources/login.html"),
        )
        payload = expect_json(response, allow_empty=True) or {}
        if str(payload.get("result_code")) in [str(c) for c in CAPTCHA_OK_CODES]:
            return True
        # 有的部署只在 ``data.result`` 上给结论（与下单那条一致）
        data = payload.get("data")
        if isinstance(data, dict) and data.get("result") is not None:
            return bool(truthy_flag(data.get("result")))
        log.debug("验证码校验未通过: %s", payload.get("result_message"))
        return False

    def _auth_headers(self) -> dict[str, str]:
        """登录鉴权类接口必需的同源头（``Origin`` + 特定 ``Referer``）。

        12306 会校验请求的同源性。Referer 不对时它**不返回 4xx**，
        而是回一个 HTML 错误页（"网络可能存在问题，请您重试一下！"）——
        于是「换票接口拿不到 newapptk」会被误读成「票据有问题」。

        实测对照（``/passport/web/auth/uamtk``，冷/热会话结果一致）：

        | 请求头 | 结果 |
        |---|---|
        | ``Origin`` + ``Referer=<passport 跳转页>`` | ✅ 正常 JSON |
        | 只有 ``X-Requested-With`` + passport Referer | ❌ HTML 错误页 |
        | 只有 ``Origin``（无 Referer） | ❌ HTML 错误页 |
        | 登录页 Referer（本轮之前一直用的） | ❌ HTML 错误页 |

        所以这里固定带上 ``Origin`` 与 passport 跳转页 Referer。
        之前用登录页 Referer 正是「扫码后换票永远失败」的根因。
        """
        return {
            "Origin": self.client.url("/").rstrip("/"),
            "Referer": self.client.url(PASSPORT_REFERER_PATH),
        }

    def _is_html_response(self, response: HttpResponse) -> bool:
        """响应是不是 HTML（12306 用 HTML 错误页表达「请求不被接受」）。"""
        text = (response.text or "").lstrip("\ufeff").lstrip()
        content_type = ""
        try:
            content_type = str(response.raw.headers.get("Content-Type", ""))
        except Exception:  # noqa: BLE001
            pass
        if "html" in content_type.lower():
            return True
        # 「什么算那张拒绝页」只有一处定义，避免两处判断漂移
        return train_date_util.is_reject_page(text)

    def _reject_html(self, response: HttpResponse, what: str) -> None:
        """响应是 HTML 时抛出可读的错误，否则什么也不做。

        12306 对「不被接受的请求」**不回 4xx**，而是回 HTTP 200 + 登录页/错误页 HTML。
        真机实测（未登录时直连三个个人中心接口）：三个接口全部返回
        ``200 + text/html``，body 是登录页。所以这里必须按「看内容而不是看状态码」判断，
        否则会走进「把一整个 HTML 页面当 JSON 解析」然后报一句
        「响应格式不对」的死胡同 —— 那句话对使用者没有任何指向性。
        """
        if self._is_html_response(response):
            raise InterError(f"{what}返回了网页而不是数据（会话可能已失效，请重新登录）")

    def _establish_login_session(self, *, method: str = "post") -> None:
        """触发 ``/otn/login/userLogin``，把登录态写进会话 Cookie。

        - 密码登录用 **POST**（抓包里带 ``_json_att=``）；
        - 扫码登录用 **GET + 跟随跳转**（与上游 py12306 的 ``qr_login`` 一致）。

        注：曾有一段时间我以为「扫码流程调它会打断会话」，那是**误判** ——
        当时鉴权接口的请求头是错的，换票无论怎样都会失败，原因无法区分。
        修正请求头后做过对照实验：调与不调、POST 与 GET，换票接口都正常返回 JSON。
        """
        try:
            if method == "get":
                # requests 默认跟随重定向，这里不传额外的开关
                self.client.get(
                    urls.USER_LOGIN,
                    headers=self._auth_headers(),
                    allow_retry=False,
                    check_risk=False,
                )
            else:
                self.client.post(
                    urls.USER_LOGIN,
                    data={"_json_att": ""},
                    headers=self._auth_headers(),
                    allow_retry=False,
                    check_risk=False,
                )
        except InterError as exc:
            # 这一步失败不一定致命，记录下来继续走，
            # 让真正的失败原因在后续步骤暴露出来。
            log.debug("触发 userLogin 失败（继续尝试换取票据）: %s", exc)

    def _exchange_apptk(self) -> str:
        """``uamtk`` → ``newapptk``。返回空串表示没换到。"""
        response = self.client.post(
            urls.UAMTk,
            data={"appid": "otn"},
            headers=self._auth_headers(),
            allow_retry=False,
        )
        if self._is_html_response(response):
            # 这一条日志能省掉大量排查：错误页几乎都是同源头不对导致的
            log.warning(
                "换取 uamtk 返回的是 HTML 错误页（通常是 Origin/Referer 不被服务端接受）: %s",
                (response.text or "")[:120].replace("\n", " "),
            )
            return ""
        payload = expect_json(response, allow_empty=True) or {}
        if str(payload.get("result_code", "")) != "0":
            log.warning("换取 uamtk 未通过: %s", payload)
            return ""
        # 注意：官方这个响应里 ``apptk`` 是 null，真正要带的是 ``newapptk``
        apptk = str(payload.get("newapptk") or payload.get("apptk") or payload.get("uamtk") or "")
        if not apptk:
            log.warning("uamtk 响应里没有可用票据: %s", payload)
        return apptk

    def login(self, username: str, password: str, *, captcha_answer: str = "", rand: str = "sjrand") -> LoginResult:
        """完整登录流程：验证码校验 → 账号密码 → userLogin → uamtk → uamauthclient。"""
        if captcha_answer:
            if not self.check_login_captcha(captcha_answer, rand):
                return LoginResult(ok=False, username=username, message="验证码校验失败", need_captcha=True)

        response = self.client.post(
            urls.WEB_LOGIN,
            data={"username": username, "password": password, "appid": "otn"},
            headers=self._auth_headers(),
            allow_retry=False,
        )
        payload = expect_json(response, allow_empty=True) or {}
        result_code = str(payload.get("result_code", ""))
        if result_code != "0":
            message = str(payload.get("result_message") or payload.get("messages") or "登录失败")
            risk = any(k in message for k in ("频繁", "验证", "风险", "异常"))
            if risk:
                raise RiskControlError(f"登录被风控拦截: {message}")
            need_captcha = "验证码" in message
            return LoginResult(ok=False, username=username, message=message, need_captcha=need_captcha)

        # 顺序必须是 userLogin → uamtk → uamauthclient（见 _establish_login_session 说明）
        self._establish_login_session()
        new_apptk = self._exchange_apptk()
        if not new_apptk:
            return LoginResult(ok=False, username=username, message="换取 uamtk 失败")

        auth_response = self.client.post(
            urls.UAMAUTH_CLIENT,
            data={"tk": new_apptk},
            headers=self._auth_headers(),
            allow_retry=False,
        )
        auth_payload = expect_json(auth_response, allow_empty=True) or {}
        if str(auth_payload.get("result_code", "")) != "0":
            # 把服务端的原话带出来：「uamtk票据内容为空」这类提示本身就是排查线索，
            # 换成笼统的「客户端鉴权失败」等于把线索删掉。
            server_message = str(auth_payload.get("result_message") or "")
            log.warning("uamauthclient 未通过: %s", auth_payload)
            return LoginResult(
                ok=False,
                username=username,
                message=f"客户端鉴权失败{('：' + server_message) if server_message else ''}",
            )
        return LoginResult(ok=True, username=auth_payload.get("username") or username, message="登录成功")

    def check_user(self) -> bool:
        """检测会话是否仍然有效（心跳 AC-03）。

        **不能把 HTML 当「有效」**。这里以前是
        ``payload is None → return response.ok``，而 ``response.ok`` 对
        HTTP 200 恒为 True —— 于是「会话失效、官方回登录页 HTML」这种情况
        会被判成「还活着」。后果很隐蔽：账号一直显示在线，
        但之后每一个业务请求都会被拒，使用者只会看到「查询一直失败」
        而想不到是登录态早就没了。
        """
        spec = self._login_spec
        request_kwargs: dict[str, Any] = {"allow_retry": False, "check_risk": False}
        if spec.referer:
            # 空 Referer 就不传 —— 有些部署会拿空 Referer 判非法请求
            # （上游那条本来就不带，差异如实保留）。
            request_kwargs["referer"] = self.client.url(spec.referer)
        if spec.method == "post":
            response = self.client.post(spec.path, data=dict(spec.data), **request_kwargs)
        else:
            response = self.client.get(spec.path, params=dict(spec.data), **request_kwargs)

        if self._is_html_response(response):
            # 官方用登录页/错误页 HTML 表达「这个会话不认」，不是业务失败
            log.info("会话检查返回网页（登录态已失效）")
            return False
        payload = expect_json(response, allow_empty=True)
        if payload is None:
            # 非 HTML 又解析不出 JSON：拿不到结论，按「不可用」处理，
            # 交给上层重新登录（宁可多登一次，也不要带着坏会话继续跑）。
            return False
        if is_login_required(payload):
            return False
        # **必须拿到明确的肯定信号才算「活着」**。
        # 之前是 ``bool(payload.get("status", True))``：既没有 flag 也没有 status 的
        # 怪响应会被默认判成 True。会话有效性的判断里，「拿不到结论」只能当成
        # 「不可用」——多登一次的成本是几秒，判错的成本是「界面显示在线、
        # 但所有请求都被拒」。
        #
        # 判读本身在 `kernel/domains/login_check.py::alive_of`：**两套后端共用一份**，
        # 所以切换后端不可能顺手改掉判定（C3 决策：请求用上游，判定保留）。
        verdict = alive_of(payload, spec)
        if verdict == ALIVE:
            return True
        if verdict != DEAD:
            log.info(
                "会话检查响应里没有 %s，按「不可用」处理: keys=%s",
                "/".join(spec.alive_paths),
                list(payload)[:6],
            )
        return False

    def get_user_info(self) -> dict[str, Any]:
        """拉取用户信息，含人证核验状态（AC-06 / AC-07）。"""
        response = self.client.get(urls.QUERY_USER_INFO, allow_retry=False, check_risk=False)
        payload = expect_json(response, allow_empty=True) or {}
        return payload.get("data") or payload

    def logout(self) -> None:
        try:
            self.client.get(urls.LOGIN_OUT, allow_retry=False, check_risk=False)
        except InterError:
            pass

    # ================================================================== #
    # 个人中心：账号信息 / 乘车人 / 我的订单
    # ================================================================== #
    def get_user_profile(self) -> dict[str, Any]:
        """拉取账号信息（个人中心首页），返回归一化后的字段。

        **请求方法上有个坑**：``initMy12306Api`` 路径里带 ``init``，看起来像页面类
        接口（GET），但真机与上游实现都按 **POST** 调。这里先 POST、再回退 GET ——
        官方对这类接口有历史兼容，两种方法在部分部署上都能通，
        但只押一种会在换部署时变成「账号信息永远空白」这种很难归因的故障。
        """
        referer = self.client.url(urls.MY_12306_PAGE)
        # 同 query_order_history：真机上「没逛过页面」的会话会被 WAF 拒
        self.ensure_session_warm()
        attempts: tuple[tuple[str, dict[str, Any]], ...] = (
            ("post", {"_json_att": ""}),
            ("get", {"_json_att": ""}),
        )
        last_error: InterError | None = None
        for method, data in attempts:
            try:
                if method == "post":
                    response = self.client.post(
                        urls.USER_INFO_JSON, data=data, allow_retry=False, referer=referer
                    )
                else:
                    response = self.client.get(
                        urls.USER_INFO_JSON, params=data, allow_retry=False, referer=referer
                    )
            except RiskControlError:
                raise
            except InterError as exc:
                last_error = exc
                log.debug("账号信息以 %s 方式请求失败: %s", method.upper(), exc)
                continue

            if self._is_html_response(response):
                # 官方对「不被接受的请求」回的是登录页 / 错误页 HTML，而不是 4xx。
                # 当成业务错误处理会把排查方向带偏，这里只记录并换下一种方式。
                log.debug("账号信息以 %s 方式返回 HTML（会话或请求头不被接受）", method.upper())
                last_error = InterError("账号信息返回了网页而不是数据（会话可能已失效，请重新登录）")
                continue


            payload = expect_json(response, allow_empty=True) or {}
            if is_login_required(payload):
                raise LoginError(_extract_message(payload) or "登录状态已失效，请重新登录")
            profile = parse_user_profile(payload)
            # 核验状态在 initMy12306Api 上不一定会给，缺了就补一次
            # ``initQueryUserInfoApi``（登录后已经在用的那条）。
            if profile.get("verified") is None:
                profile["verified"] = truthy_flag(self._verify_flag_from_user_info())
            return profile

        if last_error is not None:
            raise last_error
        raise InterError("获取账号信息失败")

    def _verify_flag_from_user_info(self) -> Any:
        """从 ``initQueryUserInfoApi`` 里取人证核验字段；取不到返回 ``None``。"""
        try:
            info = self.get_user_info()
        except InterError as exc:
            log.debug("补充核验状态失败: %s", exc)
            return None
        # 走与 parse_user_profile **同一个**剥层函数：嵌套层数不固定
        # （data / data.userDTO / data.userDTO.loginUserDTO），
        # 各自实现一遍必然漏一处，而漏的症状是「核验状态取不到」→ 被判为
        # 「无法判定」（本项目对这个词有严格语义，见 account/verify.py）。
        data = unwrap_user_payload(info)
        for key in ("isUserVerified", "userVerified", "isChecked", "verifyStatus", "face_flag"):
            if data.get(key) not in (None, ""):
                return data.get(key)
        return None

    def query_passengers(self, *, page_index: int = 1, page_size: int = 50, max_pages: int = 20) -> list[dict[str, Any]]:
        """查询常用乘车人（个人中心链路，**分页**）。

        与下单链路用的 :meth:`get_passengers` 是两条不同的接口，不要互相替代：
        - 本方法走的 ``/otn/passengers/query`` 不需要先锁票、也不需要 token，
          适合「随时看一眼我有哪些乘车人」；代价是**证件号被官方打码**；
        - 下单必须拿完整证件号，所以 ``get_passengers`` 继续走 ``getPassengerDTOs``。
        """
        referer = self.client.url(urls.PASSENGERS_PAGE)
        # 同 query_order_history：真机上「没逛过页面」的会话会被 WAF 拒
        self.ensure_session_warm()
        collected: list[dict[str, Any]] = []
        page = max(1, int(page_index))
        total = None

        while page <= max(1, int(max_pages)):
            data = {"pageIndex": page, "pageSize": max(1, int(page_size)), "_json_att": ""}
            response = self.client.post(
                urls.PASSENGERS_QUERY, data=data, allow_retry=False, referer=referer
            )
            self._reject_html(response, "乘车人")
            payload = expect_json(response, allow_empty=True) or {}
            if is_login_required(payload):
                raise LoginError(_extract_message(payload) or "登录状态已失效，请重新登录")

            rows = parse_passengers_payload(payload)
            collected.extend(rows)
            if total is None:
                body = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                try:
                    total = int(str(body.get("count") or "").strip() or 0)
                except (TypeError, ValueError):
                    total = 0
            # 官方返回的每页条数可能小于请求值；只要本页没满页，就说明到底了
            if len(rows) < max(1, int(page_size)):
                break
            if total and len(collected) >= total:
                break
            page += 1

        log.info("已获取乘车人 %d 人", len(collected))
        return collected

    def query_order_history(
        self,
        *,
        where: str = "G",
        query_type: int = 1,
        start_date: str = "",
        end_date: str = "",
        page_index: int = 1,
        page_size: int = 10,
        come_from_flag: str = "my_order",
        keyword: str = "",
    ) -> dict[str, Any]:
        """查询「我的订单」（分页），返回 :func:`parse_order_history` 的结果。

        参数（官方语义，容易记反，列在这里免得每次去翻）：
        - ``where``：``G`` 未出行订单 / ``H`` 历史订单；
        - ``query_type``：``1`` 按订票日期 / ``2`` 按乘车日期；
        - ``come_from_flag``：``my_order`` 全部 / ``my_resign`` 可改签 /
          ``my_cs_resign`` 可变更到站 / ``my_refund`` 可退票；
        - ``keyword``：订单号 / 车次 / 乘客姓名（对应字段 ``sequeue_train_name``，
          官方原文就是拼错的 ``sequeue``，不要「顺手修正」成 ``sequence``）。

        **历史订单的日期上限是「昨天」**：查 ``H`` 时官方会把 ``queryEndDate`` 当天
        算作未出行范围，传今天会得到「查不到今天之前的订单」这种莫名其妙的结果。
        这里统一收敛到昨天。
        """
        where = (where or "G").upper()
        if where not in ("G", "H"):
            raise ValidationError(f"订单查询范围只能是 G（未出行）或 H（历史），收到: {where}")

        # 先热身：真机上「从未加载过页面的会话」直接打业务接口会被 WAF 拒，
        # 返回的是「网络可能存在问题，请您重试一下！」这个 HTML 页而不是 4xx。
        # 这条经验本来是给余票查询加的（见 ensure_session_warm），
        # 订单/个人中心这几个新接口一开始漏了 —— 同一个坑不该踩第二次。
        self.ensure_session_warm()

        explicit_range = bool((start_date or "").strip() or (end_date or "").strip())

        # 参数组合兜底（**只在 0 条时**才继续往下试）：
        # 「查不到订单」既可能是真的没订单，也可能是窗口太窄 / 用了错的 queryType
        # （1=按订票日期，2=按乘车日期；未出行订单在两种口径下结果不同）。
        #
        # 但**重试本身有代价**：真机实测订单接口对连续请求很敏感，
        # 一口气打 4 次就会被 WAF 拦（返回空 ``data`` 或 HTML 错误页）——
        # 于是「查不到」被自己放大成「一直被拒」。所以：
        #   1) 档次压到 3 个（30 天 / 365 天 / 按乘车日期 365 天）；
        #   2) 档与档之间隔一下，别把突发做大；
        #   3) 记住「这次会话里哪一档成功过」，下次直接用它，不再从头试。
        combos: list[tuple[int, int]] = [(int(query_type), 30), (int(query_type), 365), (2, 365)]
        if self._order_combo_hint in combos:
            combos.remove(self._order_combo_hint)
            combos.insert(0, self._order_combo_hint)

        seen: set[tuple[int, int]] = set()
        result: dict[str, Any] | None = None
        for index, combo in enumerate(combos):
            if combo in seen:
                continue
            seen.add(combo)
            if index:
                # 给官方留点喘息；这段延时换来的成功率比多打两个请求值。
                time.sleep(ORDER_RETRY_INTERVAL)
            result = self._query_order_once(
                where=where, query_type=combo[0], start_date=start_date, end_date=end_date,
                page_index=page_index, page_size=page_size, come_from_flag=come_from_flag,
                keyword=keyword, days=combo[1],
            )
            if result["orders"] or explicit_range:
                self._order_combo_hint = combo
                break

        assert result is not None
        if not result["orders"]:
            log.warning(
                "订单查询在 %d 种参数组合下都是 0 条（%s）—— "
                "若使用者确认有订单，说明参数或解析假设需要核对",
                len(seen), where,
            )
        return result

    def _query_order_once(
        self,
        *,
        where: str,
        query_type: int,
        start_date: str = "",
        end_date: str = "",
        page_index: int = 1,
        page_size: int = 10,
        come_from_flag: str = "my_order",
        keyword: str = "",
        days: int = 30,
    ) -> dict[str, Any]:
        """按给定窗口查一次订单（``query_order_history`` 的底层实现）。"""
        end = (end_date or "").strip() or date.today().strftime("%Y-%m-%d")
        if where == "H":
            yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
            end = min(end, yesterday)
        start = (start_date or "").strip() or (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")

        response = self.client.post(
            urls.QUERY_ORDER_PAGE,
            data={
                "queryType": int(query_type),
                "queryStartDate": start,
                "queryEndDate": end,
                "come_from_flag": come_from_flag or "my_order",
                "pageSize": max(1, min(50, int(page_size))),
                # **pageIndex 官方从 0 开始**（依据：官方订单页
                # ``/otn/view/train_order.html`` 引入的
                # ``personalJS/dist/train_order/main_v30108.js``：
                # ``data:{pageIndex:"0",...}``，分页回调也是 ``getCurrent()-1``）。
                # 我们对外一律 1-based（界面显示「第 1 页」），只在这里减 1。
                #
                # 这个差 1 曾经让**订单一条都查不到**：``pageIndex=1`` 在官方看来是
                # 「第 2 页」，订单不足一页时返回空数组 —— 现象与「真的没订单」
                # 一模一样（HTTP 200、``OrderDTODataList: []``），所以极难归因。
                "pageIndex": max(0, int(page_index) - 1),
                "query_where": where,
                "sequeue_train_name": keyword or "",
                "searched_phone": "",
                "_json_att": "",
            },
            allow_retry=False,
            referer=self.client.url(urls.QUERY_ORDER_NO_COMPLETE_PAGE),
        )
        self._reject_html(response, "订单")
        payload = expect_json(response, allow_empty=True) or {}
        if is_login_required(payload):
            raise LoginError(_extract_message(payload) or "登录状态已失效，请重新登录")
        result = parse_order_history(payload)
        result["where"] = where
        result["query_type"] = int(query_type)
        #: 本次实际生效的窗口天数（界面上可以据此告诉使用者「查的是近 N 天」）
        result["window_days"] = days
        log.info(
            "已获取订单 %d 条（%s，近 %d 天，按%s，第 %s/%s 页）",
            len(result["orders"]), where, days,
            "乘车日期" if int(query_type) == 2 else "订票日期",
            result["page_index"], result["page_count"],
        )
        if not result["orders"]:
            # **0 条必须留下可排查的痕迹**。「静默空结果」是最难查的一类问题：
            # 界面只会显示「没有符合条件的订单」，看不出到底是
            # 「真的没订单」还是「官方换了返回字段名、解析全落空」。
            #
            # 关键指标是官方的 ``order_total_number``（它说名下一共有几单）：
            #   total=0                    → 官方认为没有订单，程序没问题；
            #   total>0 且列表为空          → 时间窗口 / 分页 / queryType 不对；
            #   total>0 且列表非空但解析 0  → **解析假设与官方不一致**（必须修代码）。
            # 三者要采取的动作完全不同，所以三个数都要打出来。
            body = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            total = body.get("order_total_number")
            raw_rows = body.get("OrderDTODataList")
            raw_len = len(raw_rows) if isinstance(raw_rows, list) else "非列表"
            log.warning(
                "订单查询返回 0 条 —— 官方 order_total_number=%s，OrderDTODataList 长度=%s；"
                "顶层键=%s | data 键=%s | 本次查询: where=%s %s~%s type=%s page=%s size=%s",
                total, raw_len,
                list(payload)[:12],
                list(body)[:20],
                where, start, end, query_type, page_index, page_size,
            )
            if isinstance(raw_rows, list) and raw_rows:
                log.error(
                    "订单解析落空：官方返回了 %d 条，但解析出 0 条 —— 字段名大概率变了，"
                    "首条键=%s",
                    len(raw_rows),
                    list(raw_rows[0])[:24] if isinstance(raw_rows[0], dict) else type(raw_rows[0]).__name__,
                )
            # 原始响应片段用 INFO 落盘（而不是 debug）：默认日志级别就是 INFO，
            # 「订单为空」这种一次性的现场**必须留得住**——不然只能靠使用者复现，
            # 而他下次登录后看到的又是一个新的空结果。
            log.info("订单原始响应片段: %s", json.dumps(payload, ensure_ascii=False)[:2000])
        return result

    # ================================================================== #
    # 扫码登录（AC-08）
    # ================================================================== #
    def create_qr_login(self) -> QrTicket:
        """申请一个登录二维码（返回 uuid + 图片）。"""
        response = self.client.post(
            urls.CREATE_QR,
            data={"appid": "otn", "_json_att": ""},
            allow_retry=False,
            referer=self.client.url("/otn/resources/login.html"),
        )
        payload = expect_json(response, allow_empty=True) or {}
        uuid, image = parse_qr_create(payload)
        log.info("已生成登录二维码: uuid=%s", uuid)
        return QrTicket(uuid=uuid, image_base64=image, raw=payload)

    def check_qr_login(self, uuid: str) -> tuple[str, str, str]:
        """查询一次扫码状态，返回 ``(state, uamtk, message)``。

        只查一次、不做等待：轮询节奏交给上层（Web/桌面端的 UI 定时器），
        这样「用户关掉弹窗」时能立刻停止请求，而不是留一个后台循环继续打接口。

        **必须带 ``appid=otn``**：这一个是拿真机 trial 出来的关键参数。
        不带它时官方**不会报错**，而是稳定返回
        ``{"result_message":"操作失败","result_code":9}`` —— 看起来像「服务端异常」，
        实际只是少了一个参数。使用者会以为是自己没操作对，排查方向完全被带偏。

        **请求方式**：POST 表单与 GET query 都能拿到状态（实测等价），
        这里先 POST，响应不可识别时再回退 GET。
        """
        if not uuid:
            raise ValidationError("缺少二维码 uuid")
        referer = self.client.url("/otn/resources/login.html")

        spec = self._qr_spec
        # 设备指纹**只在规格真的要用它时**才去读 Cookie：默认那套不用，
        # 保持「改造前一个字节都不差」；上游那套要用，就读同一份 Cookie（取不到是空串）。
        form = spec.form(
            uuid=uuid,
            device_id=(
                self.client.cookie_of("RAIL_DEVICEID")
                if "RAIL_DEVICEID" in spec.form_keys
                else ""
            ),
            expiration=(
                self.client.cookie_of("RAIL_EXPIRATION")
                if "RAIL_EXPIRATION" in spec.form_keys
                else ""
            ),
        )
        attempts = (
            ("post", dict(form)),
            # 回退用的 GET：补一个时间戳，避免中间层缓存导致状态不更新
            ("get", {**form, "_": int(time.time() * 1000)}),
        )
        last_payload: dict[str, Any] = {}
        last_error: InterError | None = None
        for method, data in attempts:
            try:
                if method == "post":
                    response = self.client.post(urls.CHECK_QR, data=data, allow_retry=False, referer=referer)
                else:
                    response = self.client.get(urls.CHECK_QR, params=data, allow_retry=False, referer=referer)
            except RiskControlError:
                # 风控必须向上抛：它是「需要降频」的明确信号，
                # 被当成「换个方式重试」吞掉会同时丢掉风控信号和真实原因
                # （RiskControlError 是 InterError 的子类，所以必须排在前面单独捕获）。
                raise
            except InterError as exc:
                # 单纯的请求失败不代表整体失败，换下一种方式继续试
                last_error = exc
                log.debug("checkqr 以 %s 方式请求失败: %s", method.upper(), exc)
                continue

            payload = expect_json(response, allow_empty=True) or {}
            last_payload = payload
            if qr_state_of(payload) is not None:
                return parse_qr_check(payload)
            log.debug("checkqr 以 %s 方式返回不可识别响应，尝试其它方式", method.upper())

        if last_payload:
            # 拿到了响应但都不含可识别状态：交给上层按「未知」处理（不终结会话）
            return parse_qr_check(last_payload)
        if last_error is not None:
            # 两种方式都没拿到任何响应：这是网络层面的问题，如实抛出
            raise last_error
        return parse_qr_check({})

    def _uamauthclient(self, ticket: str) -> tuple[bool, str, str]:
        """用票据换身份。返回 ``(是否成功, 用户名, 失败原因)``。"""
        response = self.client.post(
            urls.UAMAUTH_CLIENT,
            data={"tk": ticket},
            headers=self._auth_headers(),
            allow_retry=False,
        )
        if self._is_html_response(response):
            # 未登录时官方会回登录页 HTML，属于「没建立会话」，不是票据问题
            log.warning("uamauthclient 返回 HTML（会话未建立或同源头不对）")
            return False, "", "换身份时被要求重新登录（会话未建立）"
        payload = expect_json(response, allow_empty=True) or {}
        code = str(payload.get("result_code", ""))
        if code == "0":
            return True, str(payload.get("username") or ""), ""
        # 服务端原话必须带出来：「uamtk票据内容为空」这类提示本身就是排查线索
        reason = str(payload.get("result_message") or "客户端鉴权未通过")
        log.warning("uamauthclient 未通过: %s", payload)
        return False, "", reason

    def finish_qr_login(self, *, uamtk: str = "") -> LoginResult:
        """扫码确认后完成登录：``uamtk`` 换票 → ``uamauthclient`` 换身份。

        顺序对齐上游 py12306 的 ``qr_login``（真机验证过它可用）：

        1. ``GET /otn/login/userLogin``（跟随跳转）建立/刷新会话；
        2. ``POST /passport/web/auth/uamtk`` 换票 → **用 ``newapptk``**
           （该响应里 ``apptk`` 是 ``null``）；
        3. ``POST /otn/uamauthclient`` 用票换身份。

        两个容易踩的点：

        - **优先用换票接口拿到的 ``newapptk``**。``checkqr`` 确认时的响应里也有个
          叫 ``uamtk`` 的字段，直接拿去换身份会被判「内容为空」（实测），只作兜底。
        - 鉴权接口必须带同源头（见 ``_auth_headers``），否则服务端只回 HTML 错误页，
          症状会被伪装成「票据有问题」。
        """
        self._establish_login_session(method="get")
        tickets: list[tuple[str, str]] = []
        fetched = self._exchange_apptk()
        if fetched:
            tickets.append(("uamtk 接口", fetched))
        if uamtk and uamtk != fetched:
            tickets.append(("checkqr 票据", uamtk))
        if not tickets:
            return LoginResult(ok=False, message="扫码登录未获得有效凭据（uamtk 换取失败）")

        reason = ""
        for label, ticket in tickets:
            ok, username, reason = self._uamauthclient(ticket)
            if ok:
                log.info("扫码登录成功: %s（凭据来源=%s）", username or "(未知账号)", label)
                return LoginResult(ok=True, username=username, message="扫码登录成功")
            log.warning("以「%s」换身份未通过: %s", label, reason)

        return LoginResult(ok=False, message=reason or "扫码登录客户端鉴权失败")

    # ================================================================== #
    # 余票查询
    # ================================================================== #
    def ensure_session_warm(self, *, force: bool = False) -> bool:
        """加载一次余票页「热身」会话，返回是否真的发了请求。

        **真机实测（同一时刻并排对照）**：

        ==============================  ==========  ==================
        请求                             结果        Cookie
        ==============================  ==========  ==================
        新会话直接调 query               ❌ HTML     （空）
        同一时刻，先加载一次页面再调     ✅ JSON     有 JSESSIONID 等
        ==============================  ==========  ==================

        也就是说 12306 要求会话里**先有一次真实页面访问**才肯服务 query 接口。
        浏览器天然满足（用户总是先打开页面），脚本必须自己补这一步，
        否则第一次查询必然被拒，而症状是一个 HTML 错误页 —— 很难联想到"没逛过页面"。

        预热失败不抛异常：让后续步骤暴露真实原因，避免在预热这一步就掩盖问题。
        """
        if self._warmed and not force:
            return False
        try:
            response = self.client.get(
                urls.LEFT_TICKET_INIT,
                allow_retry=False,
                check_risk=False,
                referer=self.client.url(urls.LEFT_TICKET_INIT),
            )
        except InterError as exc:
            log.debug("会话预热失败（继续尝试查询）: %s", exc)
            return False
        # 页面正文顺手留下：里面的 ``var CLeftTicketUrl`` 就是官方当前的查询路径。
        # **白拿的信息** —— 读它不额外发任何请求。
        self._init_page = response.text or ""
        self._warmed = True
        return True

    def query_route_candidates(self) -> list[str]:
        """本次要按顺序尝试的余票查询路径。

        没注入解析器时返回固定候选列表（与改造前逐字节一致）；
        解析器抛异常或给出空列表时同样退回候选列表 ——
        「取路径」是优化，**不该成为新的失败点**。
        """
        if self._route_resolver is None:
            return list(urls.LEFT_TICKET_QUERY_CANDIDATES)
        try:
            resolved = list(self._route_resolver(self._init_page))
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            log.warning("解析余票查询路径失败，退回固定候选列表: %s", exc)
            return list(urls.LEFT_TICKET_QUERY_CANDIDATES)
        return resolved or list(urls.LEFT_TICKET_QUERY_CANDIDATES)

    def query_left_ticket(self, train_date: str, from_code: str, to_code: str, *, purpose_codes: str = "ADULT") -> list[TrainTicket]:
        """查询余票。

        官方 ``queryZ/queryA/query/queryO`` 为灰度轮换路径，这里按候选列表依次尝试，
        避免某一路径被下线导致整体不可用。

        **两类失败必须分开处理**（都是真机实测出来的）：

        1. **请求未被接受**（返回 HTML 错误页）—— 但这一张页面**同时**表达两件事，
           必须先分开才能给出正确的动作：

           - **日期不在可售范围**（已过去 / 还没开售）→ 抛
             :class:`TrainDateUnavailableError`。**等一万年也不会好**，要换日期；
           - **真风控**（限流）→ 先补一次「页面热身」再重试（见
             :meth:`ensure_session_warm`），仍然被拒就抛 :class:`RiskControlError`
             交给上层降频。**不能**继续把剩下 3 条路径打完 —— 那只会把突发做大，
             让短暂限流变持续限流。

           ⚠️ 历史上这里把 HTML **一律**当风控，于是「任务日期填成了去年」
           被报成「🚨 风控告警」：使用者降频、换账号、干等，全部无效，
           而且每次重试都在给 12306 添一次注定被拒的请求。判据见
           :mod:`ticket_helper.train_date`（真机并排实测）。
        2. **路径级问题**（404 / JSON 但解析失败）—— 换下一条候选路径继续。
        """
        params = {
            "leftTicketDTO.train_date": train_date,
            "leftTicketDTO.from_station": from_code,
            "leftTicketDTO.to_station": to_code,
            "purpose_codes": purpose_codes,
        }
        last_error: Exception | None = None
        #: 该日期本身就不在可售范围 → 与风控无关，重试无意义
        date_problem = train_date_util.explain(train_date)

        # 第二轮 = 重新热身之后的一次重试
        for attempt in (1, 2):
            self.ensure_session_warm(force=(attempt > 1))
            rejected = False

            # ⚠️ 必须在热身**之后**取路径：路径就来自热身那张页面。
            for path in self.query_route_candidates():
                try:
                    response = self.client.get(
                        path,
                        params=params,
                        allow_retry=False,
                        referer=self.client.url(urls.LEFT_TICKET_INIT),
                    )
                except InterError as exc:
                    last_error = exc
                    continue

                if response.status_code in (404, 502, 503):
                    last_error = ResponseParseError(f"{path} 不可用 (HTTP {response.status_code})")
                    continue

                if self._is_html_response(response):
                    # 请求没被接受 → 会话/风控层面，与具体路径无关，停止轮换
                    rejected = True
                    log.warning("余票查询请求未被接受（%s 返回 HTML 错误页）", path)
                    break

                payload = response.json
                if payload is None:
                    last_error = ResponseParseError(f"{path} 返回非 JSON")
                    continue
                if is_login_required(payload):
                    raise LoginError("查询余票时发现会话已失效")
                try:
                    tickets = parse_left_ticket_payload(
                        payload, station_map=self.stations, train_date=train_date
                    )
                except ResponseParseError as exc:
                    last_error = exc
                    continue
                return tickets

            if rejected and attempt == 1 and not date_problem:
                log.warning("余票查询被拒，重新加载余票页热身后再试一次")
                continue
            if rejected:
                if date_problem:
                    # 同一张 HTML 页面，成因完全不同：日期不在可售范围。
                    # **不热身重试、不轮换路径、不报风控** —— 那三件事对它全都无效。
                    log.warning("余票查询被拒，且日期本身不可查: %s", date_problem)
                    raise TrainDateUnavailableError(date_problem)
                raise RiskControlError(
                    "余票查询被官方拒绝（返回 HTML 错误页），疑似触发限流/风控；"
                    "已停止继续请求，稍后会自动降频重试"
                )

        if last_error:
            raise last_error if isinstance(last_error, InterError) else InterError(str(last_error))
        return []

    def query_ticket_price(
        self, train_no: str, from_no: str, to_no: str, seat_codes: list[str], train_date: str
    ) -> dict[str, float]:
        """查询票价（QY-07 结构化的一部分）。失败不抛错，返回空字典。"""
        params = {
            "train_no": train_no,
            "from_station_no": from_no,
            "to_station_no": to_no,
            "seat_types": ",".join(seat_codes),
            "train_date": train_date,
        }
        try:
            response = self.client.get(urls.LEFT_TICKET_PRICE, params=params, allow_retry=False, check_risk=False)
        except InterError:
            return {}
        payload = response.json or {}
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return {}
        prices: dict[str, float] = {}
        for key, value in data.items():
            if key.endswith("_price") and value:
                prices[key[:-6]] = _safe_float(value)
            elif key in ("A9", "M", "O", "1", "2", "3", "4", "6", "9"):
                prices[key] = _safe_float(value)
        return prices

    def query_stop_stations(self, train_no: str, from_code: str, to_code: str, train_date: str) -> list[dict[str, Any]]:
        """经停站信息（QY-07）。失败返回空列表，不影响抢票主流程。"""
        params = {"train_no": train_no, "from_station_telecode": from_code,
                  "to_station_telecode": to_code, "depart_date": train_date}
        try:
            response = self.client.get(urls.TRAIN_STOP_STATIONS, params=params, allow_retry=False, check_risk=False)
        except InterError:
            return []
        payload = response.json or {}
        data = payload.get("data") if isinstance(payload, dict) else None
        return data.get("data", []) if isinstance(data, dict) else []

    # ================================================================== #
    # 下单
    # ================================================================== #
    def submit_order_request(self, ticket: TrainTicket, *, from_name: str, to_name: str, tour_flag: str = "dc") -> bool:
        """提交订单请求（锁票），返回是否成功。

        表单**由规格决定**（``self._order_spec``）：本项目与上游在
        「``back_train_date`` 传什么」「要不要多传 ``undefined``」「带不带 Referer」
        上有差异 —— 切后端只换数据。**判定（下面那段）保持本项目这一份**：
        它是从真实响应里逐条踩出来的（``data`` 可能是 ``"N"``/``"1"``/dict），
        上游那份只有 ``result.get('data') == '0'``，换过来只会更容易判错。
        """
        form = self._order_form(STEP_SUBMIT)
        data = build_form(
            form,
            self._order_context(
                secretStr=ticket.secret_str,
                train_date=ticket.train_date,
                tour_flag=tour_flag,
                query_from_station_name=from_name,
                query_to_station_name=to_name,
            ),
        )
        response = self._order_request(form, data)
        payload = expect_json(response, allow_empty=True) or {}
        if is_login_required(payload):
            raise LoginError("提交订单时发现会话已失效")

        status = payload.get("status")
        body = payload.get("data")
        messages = payload.get("messages") or payload.get("validateMessages")
        reason = _extract_message(payload)

        if status is False:
            if not reason:
                reason = "提交订单失败"
            if _is_sold_out(reason):
                raise SoldOutError(reason)
            if _is_risk(reason):
                raise RiskControlError(reason)
            raise InterError(reason)

        # status 为 True 时，失败原因可能藏在 messages / data 里
        if reason and _is_sold_out(reason):
            raise SoldOutError(reason)
        if reason and _is_risk(reason):
            raise RiskControlError(reason)

        if isinstance(body, str) and body not in ("N", "Y", ""):
            if _is_sold_out(body):
                raise SoldOutError(body)
            if _is_risk(body):
                raise RiskControlError(body)
            # 其它非 N/Y 值在不同官方版本里语义不一致。此处**保守视为成功**：
            # 真正的失败会在随后的 initDc / checkOrderInfo 阶段明确暴露出来，
            # 而在没有明确依据时把成功判成失败，会直接丢掉一次抢票机会。
            log.debug("submitOrderRequest 返回了未识别的 data 值(%s)，继续后续流程", body[:40])

        if isinstance(body, dict):
            messages = body.get("messages") or messages
            if body.get("submitStatus") is False:
                detail = _extract_message(body) or "提交订单被拒绝"
                if _is_sold_out(detail):
                    raise SoldOutError(detail)
                raise InterError(detail)
        if isinstance(messages, list) and messages:
            text = " ".join(str(m) for m in messages)
            if _is_sold_out(text):
                raise SoldOutError(text)
        return True

    def init_dc(self) -> dict[str, Any]:
        """进入确认订单页，获取 REPEAT_SUBMIT_TOKEN 与车票表单信息。

        顺便判**这一单要不要过滑块风控**（``var if_check_slide_passcode``）——
        要过就**在返回之前**过掉：滑块的产物（``sessionId``/``sig``）
        必须赶在 ``checkOrderInfo`` 之前拿到，顺序反过来官方只会回「系统忙」。
        """
        form = self._order_form(STEP_INIT_DC)
        response = self._order_request(form, build_form(form, self._order_context()))
        html = response.text or ""
        if "login" in response.url and "confirmPassenger" not in response.url:
            raise LoginError("进入确认订单页时被重定向到登录页")
        parsed = parse_init_dc_html(html)
        self.repeat_submit_token = parsed["repeat_submit_token"]
        self.ticket_info = parsed.get("ticket_info") or {}
        #: 滑块结果**每单都要重置**：上一单的 sessionId/sig 拿去下一单必被拒，
        #: 而拒的原因仍是一句「系统忙，请稍后重试」。
        self._slide_values = {}
        if needs_slide(html, self._slide_spec):
            self._slide_values = self._solve_slide(html)
        parsed["needs_slide"] = bool(needs_slide(html, self._slide_spec))
        return parsed

    def _solve_slide(self, html: str) -> dict[str, str]:
        """过滑块，返回要填进 ``checkOrderInfo`` 的 ``{sessionId, sig}``。

        三种结局，分得很清：

        1. **没有注入后端**（= 改造前的状态）：留 WARNING 后返回空 ——
           流程照旧往下走。回退不允许引入新的失败点。
        2. **后端说自己不可用**：同样留 WARNING（带上「为什么、怎么办」）后返回空。
        3. **后端可用却失败**：**让它抛**。这时我们声称能做到的事没做到，
           继续往下走只会让使用者看到官方那句「系统忙」，无从知道是滑块没过。
        """
        solver = self._slide_solver
        if solver is None:
            log.warning(
                "本单需要过滑块风控，但当前没有可用的滑块后端（回退状态）："
                "这一单很可能提交失败；请在 App/浏览器里手动完成，"
                "或把 %s 的 slide 域切到 pjialin",
                "TICKET_HELPER_KERNEL_BACKENDS",
            )
            return {}
        ok, why = solver.available()
        if not ok:
            log.warning("本单需要过滑块风控，但滑块后端不可用：%s", why)
            return {}
        result = solver.solve(html=html, cookies=self.client.dump_cookies())
        if result is None:
            log.warning("滑块后端没有给出结果（这一单很可能被风控拦下）")
            return {}
        log.info("滑块已通过：已取到 sessionId/sig")
        return result.as_form_values()

    def get_passengers(self, token: str | None = None) -> list[dict[str, Any]]:
        """获取乘车人列表（GR-05 复用乘客信息）。"""
        token = token or self.repeat_submit_token
        response = self.client.post(
            urls.GET_PASSENGER_DTOS,
            data={"REPEAT_SUBMIT_TOKEN": token, "_json_att": ""},
            allow_retry=False,
            referer=self.client.url("/otn/confirmPassenger/initDc"),
        )
        payload = expect_json(response, allow_empty=True) or {}
        data = payload.get("data") or {}
        passengers = data.get("normal_passengers") or []
        if not passengers and payload.get("status") is False:
            raise InterError(_extract_message(payload) or "获取乘车人失败")
        return passengers

    def get_order_captcha(self, *, module: str = "passenger", rand: str = "") -> Captcha:
        """获取下单阶段图形验证码（VC-02）。

        ⚠️ 必须先确认响应**真的是一张图**再交给识别链（回流 #59）：
        12306 对「不被接受的请求」回 200 + HTML 错误页（见 ``_is_html_response``），
        老实现只做了 ``"json" in response.text[:2]`` 这个**恒为假**的判断
        （在前 2 个字符里找 "json"），也没有 HTML 检查 —— 于是 HTML 错误页字节
        被当成验证码图片一路传到人工兜底弹窗，使用者只看到
        「验证码图片无法显示」，白等 2 分钟超时，且得不到任何原因。
        处置与登录路径（:meth:`get_login_captcha`）对齐：拿不到图就抛
        ``CaptchaUnavailableError`` 并说清为什么。
        """
        # 默认 rand **只从协议层取**（``randp``，与登录的 ``sjrand`` 不同）：
        # 这里写死一个字面量就等于在接口层又定义了一遍协议（回流 #60）。
        rand = rand or urls.ORDER_PASS_CODE_RAND
        params = {
            "module": module,
            "rand": rand,
            "_": int(time.time() * 1000),
        }
        response = self.client.get(
            urls.GET_PASS_CODE_NEW,
            params=params,
            allow_retry=False,
            referer=self.client.url("/otn/confirmPassenger/initDc"),
        )

        # a) 官方用 HTML 错误页表达「请求不被接受」，这是会话级不是路径级
        if self._is_html_response(response):
            raise CaptchaUnavailableError(
                "下单验证码获取失败：请求被官方拒绝（返回 HTML 错误页）。"
                "多半是会话或风控问题，建议稍后重试。"
            )

        content_type = ""
        raw = response.raw
        if raw is not None and getattr(raw, "headers", None):
            content_type = str(raw.headers.get("Content-Type", "") or "")

        text = (response.text or "").lstrip("\ufeff").lstrip()

        # b) JSON 信封：官方在 messages 里给原因
        #    （老判断 `"json" in text[:2]` 在前 2 个字符里找 "json"，永远为假）
        if "json" in content_type.lower() or text[:1] in ("{", "["):
            payload: dict[str, Any] = {}
            if isinstance(response.json, dict):
                payload = response.json
            else:
                try:
                    parsed = json.loads(text)
                except Exception:  # noqa: BLE001 - 解析不了就当没有信封
                    parsed = None
                if isinstance(parsed, dict):
                    payload = parsed
            message = _extract_message(payload)
            if not message:
                messages = payload.get("messages")
                if isinstance(messages, (list, tuple)) and messages:
                    message = "; ".join(str(item) for item in messages)
            raise CaptchaUnavailableError(
                "下单验证码获取失败：接口返回 JSON 而非图片"
                + (f"：{message}" if message else "")
            )

        image_bytes = raw.content if raw is not None else b""
        if not image_bytes:
            raise CaptchaUnavailableError("下单验证码获取失败：接口返回空内容")

        # c) Content-Type 不是图片，且字节头也不是已知图片格式 → 不把垃圾当图传下去
        if "image" not in content_type.lower():
            head = image_bytes[:8]
            known = (
                head.startswith(b"\x89PNG")
                or head.startswith(b"\xff\xd8")
                or head.startswith(b"GIF8")
                or head.startswith(b"BM")
            )
            if not known:
                log.warning(
                    "下单验证码响应不是图片（Content-Type=%s, 前8字节=%r），已拦截",
                    content_type or "未知",
                    head,
                )
                raise CaptchaUnavailableError(
                    "下单验证码获取失败：接口返回的不是图片"
                    f"（Content-Type={content_type or '未知'}）"
                )

        return Captcha(rand=rand, image_bytes=image_bytes)


    def check_order_captcha(self, answer: str, token: str | None = None) -> bool:
        response = self.client.post(
            urls.CHECK_RAND_CODE,
            data={
                "answer": answer,
                "randCode": answer,
                "rand": urls.ORDER_PASS_CODE_RAND,
                "_json_att": "",
                "REPEAT_SUBMIT_TOKEN": token or self.repeat_submit_token,
            },
            allow_retry=False,
            referer=self.client.url("/otn/confirmPassenger/initDc"),
        )
        payload = expect_json(response, allow_empty=True) or {}
        if str(payload.get("result_code")) == "4":
            return True
        data = payload.get("data") or {}
        return bool(data.get("result")) if isinstance(data, dict) else False

    def check_order_info(
        self,
        passengers: list[dict[str, Any]],
        seat_code: str,
        *,
        token: str | None = None,
        rand_code: str = "",
        tour_flag: str = "dc",
    ) -> bool:
        form = self._order_form(STEP_CHECK_ORDER_INFO)
        data = build_form(
            form,
            self._order_context(
                passengerTicketStr=build_passenger_ticket_str(passengers, seat_code),
                oldPassengerStr=build_old_passenger_str(passengers),
                tour_flag=tour_flag,
                randCode=rand_code,
                REPEAT_SUBMIT_TOKEN=token or self.repeat_submit_token,
                # 滑块三件套（只有支持它的后端会用到；本项目那套会忽略——
                # 「上下文里多余的键会被忽略」是规格的既定行为）
                **self._slide_values,
            ),
        )
        response = self._order_request(form, data)
        payload = expect_json(response, allow_empty=True) or {}
        body = payload.get("data") or {}
        if payload.get("status") is False:
            message = _extract_message(payload) or "订单信息校验失败"
            if any(k in message for k in ("验证码", "randCode")):
                raise VerifyCodeError(message)
            raise InterError(message)
        submit_status = body.get("submitStatus") if isinstance(body, dict) else None
        if submit_status is False:
            message = body.get("errMsg") or _extract_message(payload) or "订单校验未通过"
            raise InterError(message)
        return True

    def get_queue_count(
        self,
        ticket: TrainTicket,
        seat_code: str,
        *,
        left_ticket: str = "",
        token: str | None = None,
    ) -> dict[str, Any]:
        form = self._order_form(STEP_GET_QUEUE_COUNT)
        data = build_form(
            form,
            self._order_context(
                # ★ 回流 #64：`train_date` 要官方浏览器同款 JS 日期串
                # （真机 2026-10-03：发 `2026-10-13` 被回「系统忙」）。
                train_date=_format_train_date_js(ticket.train_date),
                train_no=ticket.train_no or ticket.train_code,
                stationTrainCode=ticket.train_code,
                seatType=seat_code,
                fromStationTelecode=ticket.from_station_code,
                toStationTelecode=ticket.to_station_code,
                # ★ 回流 #64：`leftTicket` 的**权威来源是 initDc 页面下发的
                # `leftTicketStr`**（加密长串），与余票行的 `yp_info` **不是一个值**
                # （上游 `order/order.py:388` 也从 ticketInfoForPassengerForm 取）。
                # `yp_info` 只是页面没下发时的兜底。
                leftTicket=(
                    left_ticket
                    or (self.ticket_info or {}).get("leftTicketStr")
                    or ticket.yp_info
                ),
                # 本项目把 `purpose_codes` 写死 "00"（规格里的 const 会盖过这里）；
                # 上游是**动态**的（从 initDc 的 ticketInfoForPassengerForm 取），
                # 所以这里必须真的给值 —— 否则切到上游后端会在第 4 步报缺值。
                purpose_codes=(self.ticket_info or {}).get("purpose_codes") or "00",
                train_location=(
                    (self.ticket_info or {}).get("train_location") or ticket.location_code
                ),
                REPEAT_SUBMIT_TOKEN=token or self.repeat_submit_token,
            ),
        )
        response = self._order_request(form, data)
        payload = expect_json(response, allow_empty=True) or {}
        if payload.get("status") is False:
            raise QueueError(_extract_message(payload) or "获取排队人数失败")
        return payload.get("data") or {}

    def confirm_single_for_queue(
        self,
        passengers: list[dict[str, Any]],
        seat_code: str,
        ticket: TrainTicket,
        *,
        key_check_is_change: str = "",
        left_ticket: str = "",
        token: str | None = None,
        choose_seats: str = "",
        rand_code: str = "",
    ) -> bool:
        form = self._order_form(STEP_CONFIRM_SINGLE_FOR_QUEUE)
        data = build_form(
            form,
            self._order_context(
                passengerTicketStr=build_passenger_ticket_str(passengers, seat_code),
                oldPassengerStr=build_old_passenger_str(passengers),
                randCode=rand_code,
                purpose_codes=(self.ticket_info or {}).get("purpose_codes") or "00",
                key_check_isChange=key_check_is_change,
                # ★ 回流 #64：权威来源同 getQueueCount（initDc 的 leftTicketStr）。
                leftTicketStr=(
                    left_ticket
                    or (self.ticket_info or {}).get("leftTicketStr")
                    or ticket.yp_info
                ),
                train_location=(
                    (self.ticket_info or {}).get("train_location") or ticket.location_code
                ),
                choose_seats=choose_seats,
                REPEAT_SUBMIT_TOKEN=token or self.repeat_submit_token,
            ),
        )
        response = self._order_request(form, data)
        payload = expect_json(response, allow_empty=True) or {}
        body = payload.get("data") or {}
        if payload.get("status") is False:
            message = _extract_message(payload) or "确认下单失败"
            if any(k in message for k in ("余票不足", "无票")):
                raise SoldOutError(message)
            raise InterError(message)
        if isinstance(body, dict):
            if body.get("submitStatus") is False:
                raise InterError(_extract_message(payload) or body.get("errMsg") or "排队提交被拒绝")
            result = str(body.get("result", "0"))
            if result not in ("0", ""):
                raise InterError(f"排队提交异常: {body.get('message') or result}")
        return True

    def query_order_wait_time(self, *, token: str | None = None, tour_flag: str = "dc", max_polls: int = QUEUE_MAX_POLLS) -> QueueResult:
        """轮询排队结果（GR-06 排队等待 → 出票）。"""
        result = QueueResult()
        for poll in range(1, max_polls + 1):
            result.polls = poll
            form = self._order_form(STEP_QUERY_WAIT_TIME)
            params = build_form(
                form,
                self._order_context(
                    tourFlag=tour_flag,
                    REPEAT_SUBMIT_TOKEN=token or self.repeat_submit_token,
                ),
            )
            response = self._order_request(form, params)
            payload = expect_json(response, allow_empty=True) or {}
            data = payload.get("data") or {}
            if isinstance(data, dict):
                result.wait_count = int(data.get("waitCount") or 0)
                result.wait_time = int(data.get("waitTime") or 0)
                order_id = str(data.get("orderId") or "")
                if order_id:
                    result.status = "success"
                    result.order_id = order_id
                    result.message = "出票成功"
                    self._result_order_queue(order_id, token)
                    return result
                # -2 = 排队失败、-3 = 订单已撤销（判读逐字照上游 ``order/order.py:549``）。
                # 不处理它们，就会把「订单已撤销」报成「排队超时，请到未完成订单查看」——
                # 与「把锁票成功当成已建单」是同一类错误：把**确定的失败**说成**可能成功**。
                if result.wait_time in (-2, -3):
                    result.status = "failed"
                    result.message = str(
                        data.get("msg") or f"排队被中止（waitTime={result.wait_time}）"
                    )
                    return result
                if data.get("queryOrderWaitTimeStatus") is False:
                    result.status = "failed"
                    result.message = str(data.get("msg") or "排队查询失败")
                    return result
            time.sleep(QUEUE_POLL_INTERVAL)
        # 轮询耗尽：订单可能仍在服务端排队，不能当作「失败」丢弃
        result.status = "timeout"
        result.message = "排队超时，请到未完成订单中查看"
        return result

    def _result_order_queue(self, order_id: str, token: str | None = None) -> None:
        """通知服务端排队完成（官方前端会回调，失败不影响出票）。"""
        try:
            self.client.post(
                urls.RESULT_ORDER_QUEUE,
                data={"orderId": order_id, "tourFlag": "dc", "_json_att": "",
                      "REPEAT_SUBMIT_TOKEN": token or self.repeat_submit_token},
                allow_retry=False,
                check_risk=False,
            )
        except InterError as exc:
            log.debug("排队完成回调失败（忽略）: %s", exc)

    # ================================================================== #
    # 订单
    # ================================================================== #
    def query_my_order_no_complete(self) -> list[dict[str, Any]]:
        """未完成（待支付）订单（OD-01）。"""
        response = self.client.get(
            urls.QUERY_MY_ORDER_NO_COMPLETE,
            params={"_json_att": ""},
            allow_retry=False,
            check_risk=False,
        )
        payload = expect_json(response, allow_empty=True) or {}
        data = payload.get("data")
        if isinstance(data, dict):
            return data.get("orderDBList") or []
        return []

    def pay_url_of(self, order_no: str) -> str:
        """支付跳转地址（OD-03：只做辅助跳转，不做自动扣款）。"""
        return f"{urls.PAY_GATEWAY}?orderId={order_no}"

    # ================================================================== #
    # 候补（HB-01 ~ HB-06）：**afterNate 家族**
    #
    # 原来的实现打在 `/otn/confirmPassenger/confirmHB`，真机回 **HTTP 405**
    # （2026-09-29 实测）。候补全流程在 `/otn/afterNate/` 下，而且是一串调用：
    #
    #   chechFace（登录/人脸核验标志）→ getSuccessRate（成功率）
    #   → submitOrderRequest（提交需求）→ passengerInitApi（乘车人初始化）
    #   → getQueueNum（排队前置）→ confirmHB（确认订单）→ queryQueue（排队轮询）
    #
    # **`chechFace` 是核验的唯一可靠信号来源**：它的 `face_flag` 就是官方
    # 对「这个账号能不能候补」的核验结论。用户信息那两个接口根本不返回核验字段，
    # 在那里猜是永远猜不出来的。
    # ================================================================== #
    @staticmethod
    def _hb_secret(ticket: Any, seat_type: str) -> tuple[str, str]:
        """候补用的两个密钥串。

        官方把它们拼在 secretStr 后面，**必须带上席别编码**：
        ``secretList = "<secretStr>#<席别码>|"``（末尾那个 ``|`` 是官方的原样写法），
        ``successSecret = "<secretStr>#<席别码>"``（不带竖线）。
        少一样都会得到一句「系统忙，请稍后重试」——那是参数无效的伪装。
        """
        code = SEAT_CODE.get(seat_type, seat_type)
        return f"{ticket.secret_str}#{code}|", f"{ticket.secret_str}#{code}"

    def _hb_post(self, path: str, data: dict[str, Any], *, tolerant: bool = False) -> dict[str, Any]:
        """afterNate 家族统一 POST（统一 Referer，不做重试避免把突发做大）。

        ``tolerant=True`` 用于**准备阶段**（如 ``passengerInitApi``）：
        真机上它可能回一个页面而不是 JSON，而这一步是不是必需、失败会不会
        影响最终结果，我们没有可靠依据。**没有依据就不拦**：
        记一条 warning 继续往下走，让后面 ``confirmHB`` 返回官方真实的结论 ——
        这比在无依据的地方提前中止、把一句话说不清的错误抛给使用者要好。
        （登录态已经在第一步 ``chechFace.login_flag`` 验过，所以这里的页面
        不该被理解成「没登录」。）
        """
        response = self.client.post(
            path,
            data=data,
            allow_retry=False,
            check_risk=False,
            referer=self.client.url(urls.HB_REFERER),
        )
        try:
            payload = expect_json(response, allow_empty=True) or {}
        except InterError as exc:
            if not tolerant:
                raise
            log.warning("候补准备步骤 %s 返回的不是 JSON（HTTP %s），按容错继续：%s",
                        path, response.status_code, exc)
            return {}
        if payload.get("status") is False:
            message = _extract_message(payload) or "候补请求被拒绝"
            if tolerant:
                log.warning("候补准备步骤 %s 被拒（继续尝试）：%s", path, message)
                return payload
            raise InterError(message)
        return payload

    def _log_hb_evidence(
        self,
        builder: Any,
        local_train_no: str,
        server_train_no: str,
        jz_param: str,
        init_data: dict[str, Any],
    ) -> None:
        """把「判定 F1/F2 所需的证据」打到 INFO（**不改任何行为**）。

        为什么必须打：候补链路的几处分歧（F1–F4）**离线无法判定谁对**，
        只能靠真机信号。而真机信号要靠日志积累 —— 若只走 ``log.debug``，
        默认 INFO 下等于没有。本项目已经为此吃过一次亏
        （「任务在盯票但日志里一句都没有」），不要再犯。
        """
        if not server_train_no:
            compare = "官方未返回车次号"
        elif server_train_no == local_train_no:
            compare = "与本地一致"
        else:
            compare = "与本地不一致 —— 这就是 F1，需真机证据判定"
        log.info(
            "候补请求体证据：后端=%s | 本地车次号=%s | 官方车次号=%s（%s）| "
            "jzParam=%r | passengerInitApi 返回键=%s",
            builder.name,
            local_train_no,
            server_train_no or "无",
            compare,
            jz_param or "（未拿到，将发空串）",
            sorted(init_data) if init_data else "（空）",
        )

    def check_hb_face(self, ticket: Any, seat_type: str) -> dict[str, Any]:
        """HB-01 前置：检查登录态与人脸核验标志（**核验真信号**）。

        返回 ``{"login_flag": bool, "face_flag": bool, "face_check_code": str,
        "is_show_qrcode": bool}``。``face_flag`` 为假即「未完成人证核验」。
        """
        secret_list, _ = self._hb_secret(ticket, seat_type)
        payload = self._hb_post(urls.HB_CHECK_FACE, {"secretList": secret_list, "_json_att": ""})
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def get_hb_success_rate(self, ticket: Any, seat_type: str) -> dict[str, Any]:
        """HB-02：候补成功率（``level`` 越小越有戏，``info`` 是官方中文说明）。"""
        _, success_secret = self._hb_secret(ticket, seat_type)
        payload = self._hb_post(urls.HB_SUCCESS_RATE, {"successSecret": success_secret, "_json_att": ""})
        flag = (payload.get("data") or {}).get("flag")
        if isinstance(flag, list) and flag and isinstance(flag[0], dict):
            return flag[0]
        return {}

    def submit_hb_request(self, ticket: Any, seat_type: str) -> None:
        """HB-03：提交候补需求（占位，还没确认订单）。"""
        secret_list, _ = self._hb_secret(ticket, seat_type)
        payload = self._hb_post(urls.HB_SUBMIT_REQUEST, {"secretList": secret_list, "_json_att": ""})
        data = payload.get("data")
        # 有的部署不带 data.flag，此时以「没有 status:false」为准
        if isinstance(data, dict) and data.get("flag") is False:
            raise InterError(str(data.get("msg") or "提交候补需求被拒绝"))

    def init_hb_passengers(self) -> dict[str, Any]:
        """HB-03：候补乘车人初始化（真机上排在 submitOrderRequest 之后）。

        **顺序不能反**：单独提前调它，官方会回一个页面（我实测过），
        看起来像「没登录」—— 其实是状态没到。所以这一步放在链路的第 4 位，
        并且按容错处理（见 :meth:`_hb_post`）。

        **返回值必须留着**：官方在这里回 ``jzdhDateE`` / ``jzdhHourE``
        （候补截止日期与时刻），上游 ``testerSunshine/12306`` 正是用它们拼
        ``confirmHB`` 的 ``jzParam``（见 ``docs/内核替换对照报告.md`` F2）。
        我们此前把返回值直接丢掉，于是 ``jzParam`` 只能发空串 ——
        **调了接口却不读返回值**是这里最该记住的一件事。
        """
        payload = self._hb_post(urls.HB_PASSENGER_INIT, {"_json_att": ""}, tolerant=True)
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def check_hb_queue_num(self) -> bool:
        """HB-03：排队前置检查（``data.flag`` 为真才允许确认订单）。"""
        payload = self._hb_post(urls.HB_QUEUE_NUM, {"_json_att": ""})
        data = payload.get("data")
        if isinstance(data, dict) and "flag" in data:
            return bool(data["flag"])
        return True

    def query_hb_queue(self) -> dict[str, Any]:
        """HB-04：排队状态。

        返回 ``{"flag": bool, "isAsync": bool, "status": int, "waitTime": int}``。
        官方语义：``status == 1`` = 排队成功（候补单已生效、待支付）；
        ``status == -1`` = 排队失败；其它值表示还在排队，``waitTime`` 秒后再问。
        """
        payload = self._hb_post(urls.HB_QUERY_QUEUE, {"_json_att": ""})
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def build_hb_passenger_info(passengers: list[dict[str, Any]]) -> str:
        """拼候补的 ``passengerInfo``（**委托给内核门面**，不再自己拼）。

        段数存在两说（上游 5 段 / 本项目 6 段，见对照报告 F3），**离线无法判定谁对**，
        所以这里改成「按当前后端产出」：

        - 默认后端 ``self`` = 改造前的 6 段写法，**逐字节不变**；
        - 后端 ``upstream`` = 上游的 5 段写法。

        切换方式见 :mod:`ticket_helper.kernel`；设计依据见
        ``docs/内核门面架构设计.md``。
        """
        return get_standby_payload().build_passenger_info(passengers)

    def confirm_hb(
        self,
        passengers: list[dict[str, Any]],
        seat_codes: list[str],
        ticket: Any,
        *,
        from_name: str = "",
        to_name: str = "",
        seat_type: str = "",
        if_receive_wseat: str = "N",
        realize_limit_time_diff: int = 1440,
        token: str | None = None,  # noqa: ARG002 - 保留旧签名，候补链路不用它
    ) -> dict[str, Any]:
        """HB-03/04：走完整条候补链路，成功即「候补单已生效」。

        顺序**不能颠倒**，每一步都有意义（真机实测 + 可用实现对照）：

        1. ``chechFace``：登录态 + **人脸核验标志**；``face_flag`` 为假直接拦下
           （这就是「未完成人证核验」的真正来源，比用户信息接口可靠）；
        2. ``getSuccessRate``：官方给的成功率说明，随通知一起发出去；
        3. ``submitOrderRequest``：提交候补需求；
        4. ``passengerInitApi``：乘车人初始化；
        5. ``getQueueNum``：排队前置检查；
        6. ``confirmHB``：确认订单（带 ``passengerInfo``）；
        7. ``queryQueue``：排队，**``status == 1`` 才算候补单生效**。

        返回值把 ``face`` / ``success_rate`` / ``queue`` 一并带出来，供上层展示。
        """
        seat_type = seat_type or (seat_codes[0] if seat_codes else "")
        seat_label = _seat_name_of(seat_type)
        if not ticket.secret_str:
            raise InterError(
                f"{ticket.train_code} 没有拿到下单密钥（secretStr），无法候补；"
                "该车次通常处于「未起售 / 暂停发售」状态，等开售后重试即可"
            )

        # ---- ① 登录态 + 人脸核验 ----
        face = self.check_hb_face(ticket, seat_label)
        if not face:
            raise InterError("人脸核验检查没有返回数据，请稍后重试")
        if not face.get("login_flag"):
            raise LoginError("候补前检查发现登录态已失效，请重新登录后再试")
        if not face.get("face_flag"):
            raise VerifyRequiredError(
                "12306 要求先完成人证核验才能候补（chechFace.face_flag=false）"
            )

        # ---- ② 成功率（失败不影响主流程） ----
        rate: dict[str, Any] = {}
        try:
            rate = self.get_hb_success_rate(ticket, seat_label)
        except InterError as exc:
            log.debug("候补成功率查询失败（不影响提交）: %s", exc)

        # ---- ③④⑤ 提交需求 → 乘车人初始化 → 排队前置 ----
        self.submit_hb_request(ticket, seat_label)
        init_data = self.init_hb_passengers()
        if not self.check_hb_queue_num():
            raise InterError("12306 暂时不接受该候补请求（getQueueNum.flag=false），稍后重试")

        # ---- ⑥ 确认订单（请求体由内核门面产出，见 docs/内核门面架构设计.md） ----
        builder = get_standby_payload()
        passenger_info = self.build_hb_passenger_info(passengers)
        if not passenger_info:
            raise InterError("没有可用的乘车人信息，无法提交候补")
        local_train_no = ticket.train_no or ticket.train_code
        server_train_no = str((rate or {}).get("train_no") or "")
        jz_param = build_jz_param(
            (init_data or {}).get("jzdhDateE", ""), (init_data or {}).get("jzdhHourE", "")
        )
        self._log_hb_evidence(builder, local_train_no, server_train_no, jz_param, init_data)
        payload = self._hb_post(
            urls.HB_CONFIRM,
            builder.confirm_hb_params(
                passenger_info=passenger_info,
                jz_param=jz_param,
                train_no=local_train_no,
                seat_code=SEAT_CODE.get(seat_label, seat_label),
                seat_name=seat_label,
                server_train_no=server_train_no,
                if_receive_wseat=if_receive_wseat,
                realize_limit_time_diff=realize_limit_time_diff,
            ),
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if data.get("flag") is False:
            raise InterError(str(data.get("msg") or "候补订单被拒绝"))

        # ---- ⑦ 排队：status==1 才算候补单生效 ----
        queue = self.query_hb_queue()
        return {
            "hb_order_id": "",  # 官方的候补单号要等兑现后从订单里查，这里不给假值
            "face": face,
            "success_rate": rate,
            "queue": queue,
            "confirmed": True,
        }

    def query_hb_status(self, hb_order_id: str = "") -> dict[str, Any]:
        """HB-04：候补排队状态（``hb_order_id`` 仅用于日志，官方按会话返回）。"""
        return self.query_hb_queue()

    def query_hb_list(self) -> list[dict[str, Any]]:
        """候补/未完成订单列表（候补单生效后落在「未完成订单」里）。"""
        response = self.client.post(
            urls.QUERY_HB_RESULT,
            data={"_json_att": ""},
            allow_retry=False,
            check_risk=False,
        )
        payload = expect_json(response, allow_empty=True) or {}
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("orderDBList", "OrderDTODataList", "hbOrderList"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return rows
            return []
        if isinstance(data, list):
            return data
        return []

    def cancel_hb(self, hb_order_id: str = "") -> bool:
        """取消/终止候补订单。

        ⚠️ 真机上「候补单取消」走的路径**尚未验证**（候补单在未完成订单里，
        取消入口也随版本变），所以：**调用方必须容忍失败**，
        取消失败不许影响主流程（出票了但候补单还在，也只是多一次提醒）。
        """
        try:
            response = self.client.post(
                urls.CANCEL_HB,
                data={"orderId": hb_order_id, "_json_att": ""} if hb_order_id else {"_json_att": ""},
                allow_retry=False,
                check_risk=False,
            )
            payload = expect_json(response, allow_empty=True) or {}
            return payload.get("status") is not False
        except Exception as exc:  # noqa: BLE001 - 取消失败绝不能反过来影响主流程
            log.warning("取消候补订单失败（候补单可能仍挂在 12306）: %s", exc)
            return False


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
#: 「无票」类错误文案特征
SOLD_OUT_MARKERS = ("余票不足", "无票", "已售完", "没有足够的票", "票已售完", "超出余票", "已无票")
#: 「风控」类错误文案特征
RISK_MARKERS = ("频繁", "稍后", "风险", "异常操作", "非本人")


def _is_sold_out(text: str) -> bool:
    return any(marker in text for marker in SOLD_OUT_MARKERS)


def _is_risk(text: str) -> bool:
    return any(marker in text for marker in RISK_MARKERS)


#: 席别码 → 席别名（候补链路的参数要的是**席别码**，但对外一律用中文名）
_SEAT_NAME_BY_CODE: dict[str, str] = {}
for _name, _code in SEAT_CODE.items():
    _SEAT_NAME_BY_CODE.setdefault(_code, _name)


def _seat_name_of(seat: str) -> str:
    """把「二等座」或「O」都归一成席别名（候补内部再转回编码）。"""
    text = str(seat or "").strip()
    if text in SEAT_CODE:
        return text
    return _SEAT_NAME_BY_CODE.get(text.upper(), text)


def _is_over_60(born: Any) -> bool:
    """候补的 passengerInfo 要一个「是否 60 岁以上」的标志位。

    官方给的是出生日期（``1970-01-01``）；算不出来就按「不是」处理 ——
    它只影响票种判定，判错不比阻断提交更糟。
    """
    text = str(born or "").strip()
    if not text:
        return False
    match = re.search(r"(\d{4})", text)
    if not match:
        return False
    try:
        return (date.today().year - int(match.group(1))) >= 60
    except ValueError:
        return False


def _extract_message(payload: dict[str, Any]) -> str:
    """从 12306 的多种错误结构里提取可读文案。"""
    if not isinstance(payload, dict):
        return ""
    for key in ("messages", "message", "msg", "errMsg", "validateMessages"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            return " ".join(str(v) for v in value if v)
        if isinstance(value, dict):
            text = value.get("message") or value.get("errorMessage")
            if text:
                return str(text)
    data = payload.get("data")
    if isinstance(data, dict):
        return _extract_message(data)
    if isinstance(data, str) and len(data) < 200 and any(
        k in data for k in ("不足", "失败", "无票", "频繁", "错误")
    ):
        return data
    return ""


def _format_train_date(value: str) -> str:
    """12306 部分接口要求 ``YYYY-MM-DD``，部分要求 ``YYYYMMDD``；统一为标准格式。"""
    text = (value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _format_train_date_js(value: str) -> str:
    """``getQueueCount`` 的 ``train_date`` 要**官方浏览器同款** JS 日期串。

    真机存盘的 initDc 页面里 ``queryLeftTicketRequestDTO.train_date`` 就是这个
    形状；上游 ``order/order.py:375`` 也拼同款。发 ``2026-10-13`` 会在第 ⑤ 步
    收到那句含糊的「系统忙，请稍后重试」（真机 2026-10-03 实测，回流 #64）。
    星期/月份用固定英文表 —— ``strftime`` 的结果受 locale 影响，不能赌。
    """
    import datetime as _datetime

    text = _format_train_date(value)
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        return text
    year, month, day = (int(part) for part in match.groups())
    week = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    month_names = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    weekday = week[_datetime.date(year, month, day).weekday()]
    return (
        f"{weekday} {month_names[month - 1]} {day:02d} {year}"
        " 00:00:00 GMT+0800 (China Standard Time)"
    )


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
