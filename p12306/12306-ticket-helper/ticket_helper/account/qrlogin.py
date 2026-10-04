"""扫码登录会话（AC-08）。

设计要点
--------
1. **非阻塞**：``start()`` 拿到二维码，``poll()`` 只查一次状态。
   轮询节奏由前端（Web 定时器 / 桌面 QTimer）决定，这样用户关掉弹窗时
   请求立刻停止，不会留下一个后台循环继续打 12306 接口。

2. **会话 Cookie 必须沿用**：扫码登录的登录态是写在**发起扫码的那个 HttpClient**
   上的。所以这里先分配一个临时 ``User``（无密码），扫成功后把这个对象
   「原地采纳」为正式账号，而不是新建一个 —— 新建会导致 Cookie 丢失、
   表现为「扫码显示成功但账号仍是离线」。

3. **扫码前不知道是谁**：账号是在确认成功后才按服务端返回的用户名创建的。
   因此不存在「先填用户名再扫码」的强绑定，``username_hint`` 仅用于
   「给已存在的账号刷新登录态」这种已知场景。

4. **状态机是封闭的**：进入 confirmed/expired/failed 后即为终态，
   重复 poll 返回缓存结果（幂等），避免两个页面同时轮询时重复完成登录。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from ticket_helper.config.settings import UserConfig
from ticket_helper.errors import InterError, RiskControlError
from ticket_helper.inter.parser import (
    QR_STATE_CONFIRMED,
    QR_STATE_EXPIRED,
    QR_STATE_FAILED,
    QR_STATE_SCANNED,
    QR_STATE_TEXT,
    QR_STATE_UNKNOWN,
    QR_STATE_WAITING,
)

log = logging.getLogger(__name__)

#: 二维码有效期（秒）。
#:
#: 这个值是**真机实测**出来的，不是猜的：对 kyfw.12306.cn 连续试验，
#: 无论每 1 秒还是每 3 秒轮询、无论是否携带设备指纹 Cookie，
#: 二维码都在 **约 21 秒** 后由服务端返回 ``result_code=3（已过期）``。
#: 网上流传的「2 分钟」并不符合实际。20 秒意味着用户很难从容扫码，
#: 所以必须靠自动刷新来保证「界面上永远有一张可扫的码」。
DEFAULT_QR_TTL = 25.0

#: 终态：进入后不再发起任何请求
TERMINAL_STATES = (QR_STATE_CONFIRMED, QR_STATE_EXPIRED, QR_STATE_FAILED)

#: 连续多少次「无法识别」才判定为真的坏了。
#: 用 2 秒轮询间隔算，约 12 秒 —— 足够跨过接口偶发的异常信封，
#: 又不至于让用户面对一个永远不动的二维码干等。
MAX_UNKNOWN_RESPONSES = 6

#: 自动刷新二维码的次数上限。
#: 每张码约 20 秒，30 次约 10 分钟；再久说明用户并不在电脑前，
#: 继续刷新只是白白打接口，不如停下来给出明确结论。
MAX_QR_REFRESHES = 30


@dataclass
class QrLoginStatus:
    """一次扫码登录的状态快照（可直接序列化给前端）。"""

    state: str = QR_STATE_WAITING
    message: str = ""
    #: **当前二维码**的 uuid（每次自动换码都会变，前端据此判断要不要重画图片）
    uuid: str = ""
    #: **会话标识**（从建会话到结束都不变，前端用它做轮询的键）
    session_id: str = ""
    username: str = ""
    user_id: int | None = None
    image_base64: str = ""
    expires_at: float = 0.0
    #: 已自动刷新次数（前端可提示「二维码已自动刷新」）
    refresh_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def succeeded(self) -> bool:
        return self.state == QR_STATE_CONFIRMED

    @property
    def remaining(self) -> int:
        return max(0, int(self.expires_at - time.time())) if self.expires_at else 0

    def copy(self) -> "QrLoginStatus":
        """返回一份状态快照。

        会话在轮询中会**原地改写**自己的状态对象（自动换码会改 uuid、图片、有效期），
        所以对外必须给快照：否则调用方手里的对象会「自己变」——
        拿它做前后对比的代码会得到莫名其妙的结果。
        """
        return QrLoginStatus(
            state=self.state,
            message=self.message,
            uuid=self.uuid,
            session_id=self.session_id,
            username=self.username,
            user_id=self.user_id,
            image_base64=self.image_base64,
            expires_at=self.expires_at,
            refresh_count=self.refresh_count,
            extra=dict(self.extra),
        )

    def to_dict(self, *, include_image: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "state": self.state,
            "message": self.message or QR_STATE_TEXT.get(self.state, ""),
            # uuid 是「当前二维码」的（换码会变），session_id 才是轮询用的稳定键
            "uuid": self.uuid,
            "session_id": self.session_id or self.uuid,
            "username": self.username,
            "user_id": self.user_id,
            "remaining": self.remaining,
            "done": self.done,
            "succeeded": self.succeeded,
            "refresh_count": self.refresh_count,
        }
        if include_image:
            # 裸 base64；前端自行拼 data:image/png;base64,
            data["image"] = self.image_base64
        return data


class QrLoginSession:
    """一次扫码登录的生命周期管理。"""

    def __init__(
        self,
        manager: Any,
        *,
        username_hint: str = "",
        ttl: float = DEFAULT_QR_TTL,
        on_success: Callable[[Any], None] | None = None,
        auto_refresh: bool = True,
    ) -> None:
        self.manager = manager
        self.username_hint = username_hint.strip()
        self.ttl = max(10.0, float(ttl))
        self.on_success = on_success
        #: 过期后是否自动换一张新码。
        #: 官方二维码只有约 20 秒寿命，不自动刷新的话用户几乎不可能从容扫上。
        self.auto_refresh = auto_refresh
        self.max_refreshes = MAX_QR_REFRESHES

        #: 稳定的会话标识。**不能**复用二维码 uuid：二维码约 20 秒就会自动换一张，
        #: 若注册表挂在二维码 uuid 上，换码后前端再拿原 key 轮询就会「会话不存在」。
        self.session_id = f"qr_{uuid.uuid4().hex[:16]}"
        self.status = QrLoginStatus(session_id=self.session_id)
        self.user: Any = None            # 临时 User（登录态就挂在它的 client 上）
        self.created_at = 0.0
        self._unknown_responses = 0
        #: 是否出现过「已扫码」状态。出现过就不再自动换码 ——
        #: 此时用户手机上已经在确认，换掉二维码会把这一步作废。
        self._scanned_once = False
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    @property
    def uuid(self) -> str:
        return self.status.uuid

    def _new_user(self) -> Any:
        """分配一个无密码的临时 User，仅用于承载扫码会话。"""
        config = UserConfig(
            username=self.username_hint or f"qr_pending_{int(time.time() * 1000)}",
            password="",
            enabled=True,
        )
        user = self.manager.build_user(config)
        # 扫码会话不需要心跳（此时还没登录成功），避免无意义的保活请求
        user.heartbeat_interval = 0
        return user

    # ------------------------------------------------------------------ #
    def start(self) -> QrLoginStatus:
        """申请二维码。失败时返回 ``failed`` 状态而不是抛异常，方便前端统一处理。"""
        with self._lock:
            if self.status.uuid:
                return self.status
            try:
                self.user = self._new_user()
                if self.user.api is None:
                    raise InterError("接口未就绪，无法发起扫码登录")
                ticket = self.user.api.create_qr_login()
            except RiskControlError as exc:
                return self._fail(f"发起扫码登录被风控拦截：{exc}")
            except InterError as exc:
                return self._fail(f"获取登录二维码失败：{exc}")

            self.created_at = time.time()
            self.status = QrLoginStatus(
                state=QR_STATE_WAITING,
                message=QR_STATE_TEXT[QR_STATE_WAITING],
                uuid=ticket.uuid,
                session_id=self.session_id,
                image_base64=ticket.image_base64,
                expires_at=self.created_at + self.ttl,
            )
            log.info("扫码登录会话已创建: uuid=%s ttl=%.0fs", ticket.uuid, self.ttl)
            return self.status

    # ------------------------------------------------------------------ #
    def refresh(self) -> QrLoginStatus:
        """换一张新二维码（沿用同一个会话，Cookie 不丢）。

        官方二维码只有约 20 秒寿命，所以「过期即自动换码」是必须的：
        否则用户打开弹窗、拿起手机、解开锁屏，码早就失效了，
        体验就是「怎么扫都没反应」。
        """
        with self._lock:
            if self.status.done and self.status.state != QR_STATE_EXPIRED:
                return self.status
            if self.user is None or self.user.api is None:
                self.user = self.user or self._new_user()
                if self.user.api is None:
                    return self._fail("接口未就绪，无法刷新二维码")
            if self.status.refresh_count >= self.max_refreshes:
                return self._expire(
                    f"二维码已连续刷新 {self.status.refresh_count} 次仍未扫码，"
                    "已停止刷新。请重新发起扫码登录"
                )

            previous = self.status.state
            try:
                ticket = self.user.api.create_qr_login()
            except RiskControlError as exc:
                self.status.message = f"刷新二维码被风控拦截：{exc}"
                return self.status
            except InterError as exc:
                # 刷新失败不是终态：旧码也许还能用，先保留会话让用户重试
                self.status.message = f"刷新二维码失败：{exc}（可点「刷新二维码」重试）"
                log.warning("刷新登录二维码失败: %s", exc)
                return self.status

            self.created_at = time.time()
            self._unknown_responses = 0
            self.status.state = QR_STATE_WAITING
            self.status.session_id = self.session_id
            self.status.uuid = ticket.uuid
            self.status.image_base64 = ticket.image_base64
            self.status.expires_at = self.created_at + self.ttl
            self.status.refresh_count += 1
            self.status.message = "二维码已自动刷新，请扫描当前这张"
            log.info(
                "登录二维码已刷新（第%d次）: uuid=%s（原状态=%s）",
                self.status.refresh_count,
                ticket.uuid[:16],
                previous,
            )
            return self.status

    # ------------------------------------------------------------------ #
    def _handle_expired(self, message: str = "") -> QrLoginStatus:
        """二维码失效：能自动换码就换，否则才收尾成终态。"""
        if not (self.auto_refresh and not self._scanned_once):
            return self._expire(message)
        before = self.status.uuid
        refreshed = self.refresh()
        if refreshed.done:
            # 刷新本身已经给出结论（例如达到自动刷新上限）
            return refreshed
        if refreshed.uuid != before:
            log.info("二维码已过期并自动换成新码（累计刷新 %d 次）", self.status.refresh_count)
            return refreshed
        # 刷新没能拿到新码，只能按过期收尾
        return self._expire(message)

    # ------------------------------------------------------------------ #
    def poll(self) -> QrLoginStatus:
        """查询一次状态；已确认则顺手完成登录并采纳账号。"""
        with self._lock:
            if self.status.done:
                return self.status           # 终态幂等，避免重复完成登录
            if not self.status.uuid:
                return self._fail("扫码会话尚未开始")
            if time.time() > self.status.expires_at:
                # 本地兜底：服务端偶尔不会及时报过期。
                # 同样是「换码」而不是「判死」。
                return self._handle_expired()

            try:
                state, uamtk, message = self.user.api.check_qr_login(self.status.uuid)
            except RiskControlError as exc:
                # 风控属于「暂时不通」而非终态：保留二维码，让用户稍后继续刷
                self.status.message = f"轮询被风控拦截：{exc}"
                log.warning("扫码状态轮询被风控: %s", exc)
                return self.status
            except InterError as exc:
                self.status.message = f"查询扫码状态失败：{exc}"
                log.warning("查询扫码状态失败: %s", exc)
                return self.status

            if state == QR_STATE_WAITING:
                self._unknown_responses = 0
                self.status.state = QR_STATE_WAITING
                self.status.message = message or QR_STATE_TEXT[QR_STATE_WAITING]
                return self.status

            if state == QR_STATE_UNKNOWN:
                # 关键：**不要**在第一次遇到异常响应就把会话判死。
                # 官方偶发会返回不带状态字段的错误信封，此时二维码其实还有效；
                # 一判失败，用户就会「还没扫就被告知失败」，扫完也不会再有任何变化。
                self._unknown_responses += 1
                self.status.state = QR_STATE_UNKNOWN
                self.status.message = message or QR_STATE_TEXT[QR_STATE_UNKNOWN]
                self.status.extra["unknown_count"] = self._unknown_responses
                if self._unknown_responses == 1:
                    log.warning(
                        "扫码状态暂时无法识别（已忽略，继续轮询）: uuid=%s %s",
                        self.status.uuid,
                        message,
                    )
                if self._unknown_responses >= MAX_UNKNOWN_RESPONSES:
                    # 服务端原始说明放到 extra，避免和这里的说明互相嵌套成一长串括号
                    self.status.extra["server_message"] = message
                    return self._fail(
                        f"连续 {self._unknown_responses} 次无法获取扫码状态，"
                        "请点「刷新二维码」重试（也可能是网络或风控导致，稍后再试）"
                    )
                return self.status

            if state == QR_STATE_SCANNED:
                self._unknown_responses = 0
                self._scanned_once = True
                self.status.state = QR_STATE_SCANNED
                self.status.message = message or QR_STATE_TEXT[QR_STATE_SCANNED]
                log.info("二维码已被扫描，等待手机确认: uuid=%s", self.status.uuid)
                return self.status

            if state == QR_STATE_EXPIRED:
                return self._handle_expired(message)

            if state == QR_STATE_CONFIRMED:
                return self._complete(uamtk)

            return self._fail(message or f"未知的扫码状态: {state}")

    # ------------------------------------------------------------------ #
    def _complete(self, uamtk: str) -> QrLoginStatus:
        """扫码确认后完成登录并采纳账号。"""
        try:
            result = self.user.api.finish_qr_login(uamtk=uamtk)
        except RiskControlError as exc:
            return self._fail(f"扫码登录被风控拦截：{exc}")
        except InterError as exc:
            return self._fail(f"扫码登录失败：{exc}")

        if not result.ok:
            return self._fail(result.message or "扫码登录失败")

        username = result.username or self.username_hint
        if not username:
            return self._fail("扫码登录成功但服务端未返回用户名")

        try:
            self.user = self.manager.adopt_qr_user(self.user, username)
        except Exception as exc:  # noqa: BLE001 - 采纳失败要给出明确结论
            log.error("采纳扫码账号失败: %s", exc)
            return self._fail(f"扫码登录成功但保存账号失败：{exc}")

        self.status.state = QR_STATE_CONFIRMED
        self.status.username = username
        self.status.user_id = getattr(self.user, "id", None)
        self.status.message = QR_STATE_TEXT[QR_STATE_CONFIRMED]
        log.info("扫码登录完成: %s", username)

        if self.on_success is not None:
            try:
                self.on_success(self.user)
            except Exception as exc:  # noqa: BLE001 - 落库失败不应让登录结论变失败
                log.error("扫码登录成功回调失败: %s", exc)
        return self.status

    # ------------------------------------------------------------------ #
    def _expire(self, message: str = "") -> QrLoginStatus:
        self.status.state = QR_STATE_EXPIRED
        self.status.message = message or QR_STATE_TEXT[QR_STATE_EXPIRED]
        log.info("扫码二维码已失效: uuid=%s", self.status.uuid)
        return self.status

    def _fail(self, message: str) -> QrLoginStatus:
        self.status.state = QR_STATE_FAILED
        self.status.message = message or QR_STATE_TEXT[QR_STATE_FAILED]
        log.warning("扫码登录失败: %s", self.status.message)
        return self.status

    @property
    def unknown_responses(self) -> int:
        """连续无法识别的响应次数（诊断用）。"""
        return self._unknown_responses

    # ------------------------------------------------------------------ #
    def cancel(self) -> None:
        """用户主动关闭弹窗：标记为已失效，停止后续轮询。"""
        with self._lock:
            if not self.status.done:
                self.status.state = QR_STATE_EXPIRED
                self.status.message = "已取消扫码登录"
            self.user = None if not self.status.succeeded else self.user

    @property
    def finished(self) -> bool:
        return self.status.done
