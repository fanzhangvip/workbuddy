"""账号与会话管理（4.2 / AC-01 ~ AC-07）。

一个 :class:`User` 对应一个 12306 账号 + 一条独立会话（独立 HttpClient /
Cookie 容器），因此多账号天然隔离、可并行（DS-01）。

心跳保活（AC-03）：独立守护线程按 ``heartbeat`` 周期调用 ``checkUser``；
掉线立即 ``restore()`` 重登；连续失败进入 ``risk`` 状态并上报，交由限速器降频。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Sequence

from ticket_helper.account import verify
from ticket_helper.account.qrlogin import DEFAULT_QR_TTL, QrLoginSession
from ticket_helper.config.settings import CodecConfig, UserConfig
from ticket_helper.errors import (
    CaptchaUnavailableError,
    InterError,
    LoginError,
    RiskControlError,
    ValidationError,
    VerifyCodeError,
)
from ticket_helper.inter.api import TicketApi
from ticket_helper.inter.base import HttpClient
from ticket_helper.inter.station import StationMap
from ticket_helper.verify.service import VerifyCodeService

log = logging.getLogger(__name__)

#: 账号状态机
STATUS_OFFLINE = "offline"
STATUS_LOGINING = "logining"
STATUS_ONLINE = "online"
STATUS_RISK = "risk"
STATUS_DISABLED = "disabled"

#: 账号状态机的**全部**合法取值。
#: 存储层用它做「未知状态不得入库」的校验（见 `storage/database.py`），
#: 那边只记常量名、不复制字面量。
USER_STATUSES: tuple[str, ...] = (
    STATUS_OFFLINE,
    STATUS_LOGINING,
    STATUS_ONLINE,
    STATUS_RISK,
    STATUS_DISABLED,
)
STATUS_ERROR = "error"

#: 连续登录失败多少次后判定为风控
RISK_FAILURE_THRESHOLD = 3


@dataclass
class LoginStats:
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    last_login_at: float = 0.0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "successes": self.successes,
            "failures": self.failures,
            "last_login_at": self.last_login_at,
            "last_error": self.last_error,
        }


class User:
    """单个 12306 账号及其会话。"""

    def __init__(
        self,
        config: UserConfig,
        *,
        user_id: int | None = None,
        base_url: str = "https://kyfw.12306.cn",
        station_map: StationMap | None = None,
        verify_service: VerifyCodeService | None = None,
        codec_config: CodecConfig | None = None,
        http_factory: Callable[..., HttpClient] | None = None,
        max_login_retry: int = 3,
        risk_handler: Callable[[Any], None] | None = None,
        session_store: Any | None = None,
        route_resolver: Callable[[str], Sequence[str]] | None = None,
        login_spec: Any | None = None,
        qr_spec: Any | None = None,
        order_spec: Any | None = None,
        slide_solver: Any | None = None,
        pacing_spec: Any | None = None,
        heartbeat_spec: Any | None = None,
    ) -> None:
        self.config = config
        self.id = user_id
        self.username = config.username
        self._password = config.password
        #: 心跳间隔：规格注入时以规格为准（唯一一处「地板」实现在 HeartbeatSpec 里）；
        #: 未注入 = 改造前行为（`max(30, config.heartbeat)`）。
        self._heartbeat_spec = heartbeat_spec
        self.heartbeat_interval = self._resolve_heartbeat(config.heartbeat)
        self.enabled = config.enabled
        self.base_url = base_url
        #: 会话持久化（可为 None：不传就退化成「重启需重新登录」的旧行为）
        self.session_store = session_store

        factory = http_factory or HttpClient
        self.client = factory(base_url=base_url)
        if pacing_spec is not None:
            # 超时来自节奏规格（上游 5s / 本项目 12s）。**必须显式赋值**：
            # 不改的话「切了 pacing」只影响间隔、不影响超时 —— 一半生效最难看。
            self.client.timeout = float(pacing_spec.timeout)
        # 把接口层探测到的风控信号（429/403/风控文案）转给限速器，实现自适应降频（RC-01）
        if risk_handler is not None:
            self.client.on_risk(risk_handler)
        self.station_map = station_map
        #: 余票查询路径的取法（由装配中心注入；不注入 = 固定候选列表）
        self.route_resolver = route_resolver
        #: 登录态探测的规格（由装配中心注入；不注入 = 协议层默认规格）
        self.login_spec = login_spec
        #: 扫码轮询请求体的规格（由装配中心注入；不注入 = 协议层默认规格）
        self.qr_spec = qr_spec
        #: 下单六步的请求规格（由装配中心注入；不注入 = 协议层默认规格）
        self.order_spec = order_spec
        #: 过滑块的后端（对象；未注入 = 没有程序化解法，照旧往下走）
        self.slide_solver = slide_solver
        self.api = (
            TicketApi(
                self.client,
                station_map,
                route_resolver,
                login_spec,
                qr_spec,
                order_spec,
                slide_solver,
            )
            if station_map
            else None
        )  # type: ignore[arg-type]
        self.verify = verify_service or VerifyCodeService(codec_config)
        self.max_login_retry = max(1, max_login_retry)

        self.is_login = False
        self.is_checked = False
        #: 是否**真的执行过**核验检测。
        #: 只登录成功后才检测，所以「未登录」时它是 False ——
        #: 界面据此区分「还没检测」与「检测过、确实未核验」，
        #: 否则两者都显示「未核验」，使用者会误以为账号被限制了。
        self.verified_checked = False
        #: 本次检测**是否判得出来**（有没有拿到明确的核验字段）。
        #: 官方不返回该字段时为 False —— 界面据此显示「无法判定」，
        #: 而不是给一个看起来确切、实际可能错的「未核验」。
        self.verify_known = False
        self.status = STATUS_OFFLINE
        self.last_active: float = 0.0
        self.stats = LoginStats()
        self._consecutive_failures = 0
        self._stop_event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._login_lock = threading.RLock()
        self._event_listeners: list[Callable[[str, dict[str, Any]], None]] = []

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def on_event(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        self._event_listeners.append(callback)

    def _emit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        data = {"user": self.username, **(payload or {})}
        for callback in list(self._event_listeners):
            try:
                callback(event, data)
            except Exception as exc:  # noqa: BLE001
                log.error("账号事件回调失败: %s", exc)

    def _resolve_heartbeat(self, configured: object) -> int:
        """心跳间隔的**唯一**解析口（构造与热更新两条路径共用）。

        地板（30 秒）由 `HeartbeatSpec.effective_interval` 实现，这里不再 `max(30, ...)`：
        写两处必然漂，而漂的表现是**心跳比预期更频繁**（更招风控），不报错。
        """
        if self._heartbeat_spec is not None:
            return int(float(self._heartbeat_spec.effective_interval()))
        try:
            return max(30, int(float(configured)))
        except (TypeError, ValueError):
            return 300

    # ------------------------------------------------------------------ #
    # 登录
    # ------------------------------------------------------------------ #
    def attach_api(self, station_map: StationMap) -> None:
        self.station_map = station_map
        # ⚠️ 必须**带齐所有策略**（别在这里写数量 —— 数字会腐烂，
        # 这句原来说「三个策略」，加到第五个时它就成了假话）。
        # 漏了任何一个，都会让**这条路径上的账号**悄悄退回默认行为，
        # 而其他账号已用上新策略 —— 两种行为并存，且只在特定入口暴露。
        self.api = TicketApi(
            self.client,
            station_map,
            self.route_resolver,
            self.login_spec,
            self.qr_spec,
            self.order_spec,
            self.slide_solver,
        )

    def login(self, *, force: bool = False) -> bool:
        """自动登录（含验证码识别与重试）。"""
        with self._login_lock:
            # 先判启用状态：已禁用的账号即使残留着旧会话也不允许再被使用
            if not self.enabled:
                self.status = STATUS_DISABLED
                return False
            if self.is_login and not force:
                return True
            if self.api is None:
                raise LoginError("未绑定站点表，无法登录（请先 attach_api）")

            # 先试「上次登录留下的会话」。
            # 这条路径对**扫码账号是唯一出路**：它没有密码，会话一没就只能重新扫码，
            # 而扫码必须有人拿手机配合。force=True 表示「明确要求重新走一遍登录」，
            # 此时不复用会话。
            if not force and self.session_store is not None and self.restore_session():
                return True

            if not self._password:
                # 扫码登录出来的账号本来就没有密码，走密码登录必然失败。
                # 这里给出明确结论而不是「反复重试后报密码错误」——
                # 后者的提示会把人引向「是不是密码填错了」这个错误方向。
                message = "该账号没有保存密码（扫码登录账号），需要用手机扫码登录"
                self.stats.last_error = message
                if self.is_login:
                    # 会话其实还在线，只是没法「用密码重新登录」。
                    # 此时不要把状态打成异常：界面显示「异常」但账号实际可用，
                    # 只会让人更困惑，排查方向也会被带偏。
                    log.warning("账号 %s 已在线，跳过密码重新登录: %s", self.username, message)
                    self._emit("login_skipped", {"reason": message, "online": True})
                    return False
                self.status = STATUS_ERROR
                log.warning("账号 %s 跳过密码登录: %s", self.username, message)
                self._emit("login_skipped", {"reason": message})
                return False

            self.status = STATUS_LOGINING
            self._stop_event.clear()
            last_error = ""

            for attempt in range(1, self.max_login_retry + 1):
                self.stats.attempts += 1
                try:
                    answer, rand = self._fetch_login_captcha()
                    result = self.api.login(self.username, self._password, captcha_answer=answer, rand=rand)
                    if result.ok:
                        self.is_login = True
                        self.status = STATUS_ONLINE
                        self.last_active = time.time()
                        self._consecutive_failures = 0
                        self.stats.successes += 1
                        self.stats.last_login_at = time.time()
                        log.info("账号登录成功: %s", self.username)
                        self._emit("login_success", {"attempt": attempt})
                        self.save_session()
                        self.detect_verified()
                        return True

                    last_error = result.message or "登录失败"
                    log.warning("账号 %s 登录失败(第%d次): %s", self.username, attempt, last_error)
                    if result.need_captcha:
                        continue
                    if "密码" in last_error or "用户名" in last_error:
                        break  # 凭据错误，重试无意义
                except RiskControlError as exc:
                    last_error = str(exc)
                    self._enter_risk(last_error)
                    break
                except CaptchaUnavailableError as exc:
                    # 接口侧就拿不到图（例如被风控挡掉）：重试只是把同一个接口再打一遍，
                    # 不会变好，还会加重风控。直接给结论，并保留有价值的排查细节。
                    last_error = exc.message
                    log.warning("账号 %s 无法获取验证码，停止重试: %s", self.username, last_error)
                    break
                except VerifyCodeError as exc:
                    last_error = f"验证码识别失败: {exc.message}"
                    log.warning("账号 %s 验证码识别失败(第%d次)", self.username, attempt)
                    if "人工录入已超时" in exc.message:
                        # 人工兜底都超时了，再重试只会把验证码弹窗再推一遍，
                        # 使用者等来的还是「超时」。直接给出结论更清楚。
                        break
                except InterError as exc:
                    last_error = str(exc)
                    log.warning("账号 %s 登录异常(第%d次): %s", self.username, attempt, last_error)
                time.sleep(min(5.0, 1.0 * attempt))

            self.stats.failures += 1
            self.stats.last_error = last_error
            self.is_login = False
            self._consecutive_failures += 1
            if self._consecutive_failures >= RISK_FAILURE_THRESHOLD:
                self._enter_risk(last_error or "连续登录失败")
            elif self.status != STATUS_RISK:
                # 不要覆盖已经判定的风控状态：风控是「需要人工介入」的终态信息，
                # 被降级成泛化的 error 会让使用者失去「该去检查账号是否被限制」的线索。
                self.status = STATUS_ERROR
            self._emit("login_failed", {"error": last_error})
            return False

    def _fetch_login_captcha(self) -> tuple[str, str]:
        """取验证码图片并识别（VC-01 / VC-03）。"""
        assert self.api is not None
        captcha = self.api.get_login_captcha()
        image = b""
        if captcha.image_base64:
            import base64

            try:
                image = base64.b64decode(captcha.image_base64)
            except Exception:  # noqa: BLE001
                image = b""
        answer = self.verify.recognize(image, hint=captcha.answer_hint, kind="login")
        return answer, captcha.rand

    def _enter_risk(self, reason: str) -> None:
        self.status = STATUS_RISK
        self.is_login = False
        log.error("账号 %s 进入风控状态: %s", self.username, reason)
        self._emit("risk", {"reason": reason})

    # ------------------------------------------------------------------ #
    # 会话持久化（AC-03：重启后不必重新扫码）
    # ------------------------------------------------------------------ #
    def save_session(self) -> bool:
        """把当前会话 Cookie 加密留在本地。"""
        if self.session_store is None or self.api is None:
            return False
        return self.session_store.save(self.username, self.client.export_cookies())

    def restore_session(self) -> bool:
        """尝试用本地留存的会话恢复登录态。成功返回 True。

        三个要点：
        1. **磁盘上的会话不可信**，恢复完必须真调一次 ``checkUser`` 验证 ——
           否则会给界面一个「在线」的假象，而所有业务请求都会被官方拒掉；
        2. 验证不过就**立刻清除**文件，避免每次启动都拿同一个死会话去试；
        3. 恢复成功后照常检测人证核验，让界面上的核验状态也是新的。
        """
        store = self.session_store
        if store is None or self.api is None:
            return False
        cookies = store.load(self.username)
        if not cookies:
            return False
        self.client.import_cookies(cookies)
        try:
            alive = self.api.check_user()
        except RiskControlError:
            raise
        except InterError as exc:
            log.info("账号 %s 的会话恢复失败（%s），将正常登录", self.username, exc)
            self.client.clear_cookies()
            store.clear(self.username)
            return False
        if not alive:
            log.info("账号 %s 的本地会话已失效，将正常登录", self.username)
            self.client.clear_cookies()
            store.clear(self.username)
            return False

        self.is_login = True
        self.status = STATUS_ONLINE
        self.last_active = time.time()
        self._consecutive_failures = 0
        self.stats.successes += 1
        self.stats.last_login_at = time.time()
        log.info("账号 %s 已用本地会话恢复登录（无需重新扫码）", self.username)
        self._emit("session_restored")
        try:
            self.detect_verified()
        except Exception as exc:  # noqa: BLE001 - 核验检测失败不该推翻已恢复的会话
            log.debug("恢复会话后检测人证核验失败: %s", exc)
        return True

    # ------------------------------------------------------------------ #
    # 会话保活
    # ------------------------------------------------------------------ #
    def check_alive(self) -> bool:
        """探测会话是否仍有效。"""
        if not self.is_login or self.api is None:
            return False
        try:
            alive = self.api.check_user()
        except InterError as exc:
            log.warning("账号 %s 会话检查异常: %s", self.username, exc)
            return False
        if alive:
            self.last_active = time.time()
            self.status = STATUS_ONLINE
            return True
        return False

    def keep_alive_once(self) -> bool:
        """一次心跳：正常返回 True；掉线则自动恢复（AC-03）。"""
        if not self.enabled:
            return False
        if self.check_alive():
            return True
        log.warning("账号 %s 会话已失效，尝试恢复", self.username)
        return self.restore()

    def restore(self) -> bool:
        """状态恢复：重新登录并拉取用户信息。"""
        self._emit("restore_start")
        self.is_login = False
        ok = self.login(force=True)
        if ok:
            self._emit("restore_success")
        else:
            self._emit("restore_failed")
        return ok

    def start_heartbeat(self) -> None:
        """启动心跳守护线程（幂等）。"""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._stop_event.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name=f"heartbeat-{self.username}", daemon=True
        )
        self._heartbeat_thread.start()
        log.info("账号 %s 心跳线程已启动（间隔 %ds）", self.username, self.heartbeat_interval)

    def stop_heartbeat(self) -> None:
        self._stop_event.set()
        thread = self._heartbeat_thread
        if thread and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=2.0)
        self._heartbeat_thread = None

    def _heartbeat_loop(self) -> None:
        # 启动前先随机抖动，避免多账号在同一秒集中打请求
        import random

        self._stop_event.wait(random.uniform(0.5, 3.0))
        while not self._stop_event.is_set():
            try:
                self.keep_alive_once()
            except Exception as exc:  # noqa: BLE001 - 心跳线程绝不能死
                log.error("账号 %s 心跳循环异常: %s", self.username, exc)
            self._stop_event.wait(self.heartbeat_interval)

    # ------------------------------------------------------------------ #
    # 信息
    # ------------------------------------------------------------------ #
    def detect_verified(self) -> bool:
        """检测人证核验状态（AC-07）；未核验账号无法使用候补。

        三元状态，别用「猜」：``is_checked`` 只在**拿到明确字段**时才置位，
        同时用 ``verify_known`` 表示「本次到底判没判出来」。

        为什么不再用「姓名 ≠ 账号名」来猜
        ----------------------------------
        真机踩到过：扫码登录时官方 ``uamauthclient`` 返回的 ``username``
        本身就是真实姓名（例如「樊章」），于是
        ``user_name != self.username`` 恒为 False → 一个已实名的账号被判成
        「未核验」。而「未核验」会让人以为账号被 12306 限制了，属于**有害的误导**。

        宁可如实说「无法判定」，也不要给一个看起来确切、实际是错的结论。
        """
        self.verified_checked = True
        self.verify_known = False
        if self.api is None:
            return False
        try:
            info = self.api.get_user_info()
        except InterError as exc:
            log.debug("获取用户信息失败: %s", exc)
            return self.is_checked
        if not isinstance(info, dict):
            return self.is_checked

        # 官方字段随版本变化，做多种兼容判断
        flag = (
            info.get("isUserVerified")
            or info.get("userVerified")
            or info.get("isChecked")
            or info.get("verifyStatus")
        )
        if flag is None:
            # 没有明确字段 → 不猜。之前的姓名启发式在扫码登录场景下必然误判。
            log.debug("用户信息里没有核验字段，判定为「无法判定」: keys=%s", list(info.keys())[:8])
            return self.is_checked

        self.verify_known = True
        self.is_checked = str(flag).upper() in ("Y", "1", "TRUE", "YES", "2")
        return self.is_checked

    def get_passengers(self) -> list[dict[str, Any]]:
        """乘车人列表（GR-05）。"""
        if self.api is None:
            return []
        self.api.init_dc()
        return self.api.get_passengers()

    # ------------------------------------------------------------------ #
    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if not self.enabled:
            self.status = STATUS_DISABLED
            self.stop_heartbeat()
        else:
            self.status = STATUS_OFFLINE if not self.is_login else STATUS_ONLINE

    @property
    def password(self) -> str:
        return self._password

    def set_password(self, password: str) -> None:
        self._password = password

    @property
    def masked(self) -> str:
        return self.username[:2] + "***" if len(self.username) > 2 else "***"

    def to_dict(self, *, include_stats: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "username": self.username,
            "status": self.status,
            "online": self.is_login,
            "is_checked": self.is_checked,
            "verified_checked": self.verified_checked,
            "verify_known": self.verify_known,
            "enabled": self.enabled,
            "heartbeat": self.heartbeat_interval,
            # 只暴露「有没有密码」这个布尔量，绝不回显密码本身。
            # 前端据此展示登录方式，并决定「测试登录」是否可用。
            "has_password": bool(self._password),
            "login_type": "password" if self._password else "qr",
            "last_active": (
                datetime.fromtimestamp(self.last_active).strftime("%Y-%m-%d %H:%M:%S") if self.last_active else ""
            ),
        }
        # 核验状态的中文名与「下一步做什么」由 account.verify 统一下发：
        # 前端不必自己再拼一套四态判断，也就不会出现
        # 「这里显示无法判定、那里判成未核验」这种自相矛盾。
        payload["verify_state"] = verify.state_of(payload)
        payload["verify_label"] = verify.LABEL[payload["verify_state"]]
        payload["verify_hint"] = verify.HINT[payload["verify_state"]]
        if self.session_store is not None:
            # 让界面能告诉使用者「重启后免扫码已生效」，而不是让他猜
            try:
                payload["session_saved"] = bool(self.session_store.exists(self.username))
            except Exception:  # noqa: BLE001 - 权限/IO 异常不该让账号列表整个挂掉
                payload["session_saved"] = False
        if include_stats:
            payload["stats"] = self.stats.to_dict()
        return payload

    def close(self) -> None:
        self.stop_heartbeat()
        self.client.close()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username} status={self.status}>"


class AccountManager:
    """多账号管理器（4.2 / DS-01）。"""

    def __init__(
        self,
        *,
        base_url: str = "https://kyfw.12306.cn",
        station_map: StationMap | None = None,
        verify_service: VerifyCodeService | None = None,
        codec_config: CodecConfig | None = None,
        auto_heartbeat: bool = True,
        risk_handler: Callable[[Any], None] | None = None,
        session_store: Any | None = None,
        route_resolver: Callable[[str], Sequence[str]] | None = None,
        login_spec: Any | None = None,
        qr_spec: Any | None = None,
        order_spec: Any | None = None,
        slide_solver: Any | None = None,
        pacing_spec: Any | None = None,
        heartbeat_spec_for: Callable[[object], Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self.station_map = station_map
        self.route_resolver = route_resolver
        self.login_spec = login_spec
        self.qr_spec = qr_spec
        self.order_spec = order_spec
        self.slide_solver = slide_solver
        self.pacing_spec = pacing_spec
        #: 心跳规格**按账号配置**解析（每个账号可以有自己的 heartbeat），
        #: 所以装配中心给的是**可调用对象**而不是一个规格对象。
        self.heartbeat_spec_for = heartbeat_spec_for
        self.verify_service = verify_service
        self.codec_config = codec_config
        self.auto_heartbeat = auto_heartbeat
        self.risk_handler = risk_handler
        self.session_store = session_store
        self._users: dict[str, User] = {}
        self._lock = threading.RLock()
        self._event_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._rr_index = 0
        #: 进行中的扫码登录会话：uuid → QrLoginSession（AC-08）
        self._qr_sessions: dict[str, QrLoginSession] = {}

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def on_event(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        self._event_listeners.append(callback)

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        for callback in list(self._event_listeners):
            try:
                callback(event, payload)
            except Exception as exc:  # noqa: BLE001
                log.error("账号管理器事件回调失败: %s", exc)

    # ------------------------------------------------------------------ #
    # 增删改
    # ------------------------------------------------------------------ #
    def build_user(self, config: UserConfig, *, user_id: int | None = None) -> User:
        """按管理器配置构造一个 User，但**不注册**到账号表。

        扫码登录需要先拿一个「还没登录成功的临时会话」去申请二维码，
        扫成功之后才谈得上账号是谁；所以必须有一个不登记进账号列表的构造入口。
        """
        user = User(
            config,
            user_id=user_id,
            base_url=self.base_url,
            station_map=self.station_map,
            route_resolver=self.route_resolver,
            login_spec=self.login_spec,
            qr_spec=self.qr_spec,
            order_spec=self.order_spec,
            slide_solver=self.slide_solver,
            pacing_spec=self.pacing_spec,
            heartbeat_spec=(
                self.heartbeat_spec_for(config.heartbeat) if self.heartbeat_spec_for else None
            ),
            verify_service=self.verify_service,
            codec_config=self.codec_config,
            risk_handler=self.risk_handler,
            session_store=self.session_store,
        )
        user.on_event(lambda event, payload: self._emit(event, payload))
        return user

    def add_user(self, config: UserConfig, *, user_id: int | None = None) -> User:
        with self._lock:
            existing = self._users.get(config.username)
            if existing is not None:
                # 只在新密码非空时覆盖：扫码登录出来的账号没有密码，
                # 若直接用空串覆盖，会把「同一账号原本可用的密码」清掉。
                if config.password:
                    existing.set_password(config.password)
                existing._heartbeat_spec = self.heartbeat_spec_for(
                    config.heartbeat
                ) if self.heartbeat_spec_for else existing._heartbeat_spec
                existing.heartbeat_interval = existing._resolve_heartbeat(config.heartbeat)
                existing.set_enabled(config.enabled)
                return existing

            user = self.build_user(config, user_id=user_id)
            self._users[config.username] = user
            log.info("已添加账号: %s (enabled=%s)", config.username, config.enabled)
            return user

    def remove_user(self, username: str) -> bool:
        with self._lock:
            user = self._users.pop(username, None)
        if user is None:
            return False
        user.close()
        # 一并清掉本地留存的会话：账号都删了，还留着一份登录凭据在磁盘上
        # 既没用也不合适。
        if self.session_store is not None:
            self.session_store.clear(username)
        log.info("已移除账号: %s", username)
        return True

    def get(self, username: str) -> User | None:
        with self._lock:
            return self._users.get(username)

    def all(self) -> list[User]:
        with self._lock:
            return list(self._users.values())

    def enabled_users(self) -> list[User]:
        return [u for u in self.all() if u.enabled]

    def online_users(self) -> list[User]:
        return [u for u in self.all() if u.is_login]

    def enable(self, username: str, enabled: bool = True) -> bool:
        user = self.get(username)
        if user is None:
            return False
        user.set_enabled(enabled)
        if enabled and self.auto_heartbeat:
            user.start_heartbeat()
        return True

    def count(self) -> int:
        with self._lock:
            return len(self._users)

    # ------------------------------------------------------------------ #
    # 登录编排
    # ------------------------------------------------------------------ #
    def login_all(self, *, parallel: bool = True) -> dict[str, bool]:
        """批量登录（多账号并行，互不阻塞）。"""
        targets = self.enabled_users()
        results: dict[str, bool] = {}
        if not parallel or len(targets) <= 1:
            for user in targets:
                results[user.username] = user.login()
        else:
            threads: list[threading.Thread] = []

            def _worker(u: User) -> None:
                try:
                    results[u.username] = u.login()
                except Exception as exc:  # noqa: BLE001
                    log.error("账号 %s 登录线程异常: %s", u.username, exc)
                    results[u.username] = False

            for user in targets:
                thread = threading.Thread(target=_worker, args=(user,), name=f"login-{user.username}", daemon=True)
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join(timeout=120)

        if self.auto_heartbeat:
            for user in targets:
                if user.is_login:
                    user.start_heartbeat()
        return results

    def allocate(self, task: Any) -> User | None:
        """为任务分配账号（4.2）。

        规则：
        1. 任务显式指定的账号优先，且必须在「在线」集合中；
        2. 否则在全部在线账号中轮询（round-robin），保证多任务负载均衡；
        3. 没有在线账号时返回 None，由调度器稍后重试（不阻塞任务）。
        """
        accounts = list(getattr(task, "accounts", None) or [])
        if accounts:
            for name in accounts:
                user = self.get(name)
                if user and user.enabled and user.is_login:
                    return user
            return None

        candidates = self.online_users()
        if not candidates:
            return None
        with self._lock:
            user = candidates[self._rr_index % len(candidates)]
            self._rr_index += 1
        return user

    # ------------------------------------------------------------------ #
    def broadcast_status(self) -> list[dict[str, Any]]:
        return [u.to_dict() for u in self.all()]

    def snapshot(self) -> dict[str, Any]:
        users = self.all()
        return {
            "total": len(users),
            "online": sum(1 for u in users if u.is_login),
            "risk": sum(1 for u in users if u.status == STATUS_RISK),
            "offline": sum(1 for u in users if u.status == STATUS_OFFLINE),
            "users": [u.to_dict() for u in users],
        }

    def stop_all(self) -> None:
        for user in self.all():
            user.stop_heartbeat()

    def close(self) -> None:
        for user in self.all():
            user.close()
        with self._lock:
            self._users.clear()
            self._qr_sessions.clear()


# --------------------------------------------------------------------------- #
# 扫码登录（AC-08）
# --------------------------------------------------------------------------- #
def _install_qr_login_methods() -> None:
    """把扫码登录相关方法挂到 AccountManager 上。

    集中放在文件尾部而不是塞进类体：扫码登录是一块相对独立的能力，
    收在一起更好维护，也不打断主类里「增删改 / 分配 / 保活」的阅读节奏。
    """

    def start_qr_login(
        self: "AccountManager",
        *,
        username_hint: str = "",
        ttl: float = DEFAULT_QR_TTL,
        on_success: Any = None,
    ) -> "QrLoginSession":
        session = QrLoginSession(self, username_hint=username_hint, ttl=ttl, on_success=on_success)
        with self._lock:
            self.gc_qr_sessions()
            # 按**稳定的会话标识**登记：二维码约 20 秒就会自动换一张（uuid 变化），
            # 若按二维码 uuid 登记，换码后前端拿原 key 轮询就会「会话不存在」。
            self._qr_sessions[session.session_id] = session
        session.start()
        return session

    def get_qr_session(self: "AccountManager", session_id: str) -> "QrLoginSession | None":
        with self._lock:
            session = self._qr_sessions.get(session_id)
            if session is not None:
                return session
            # 兼容：也允许用「当前二维码 uuid」查（旧前端/手工调试）
            for item in self._qr_sessions.values():
                if item.status.uuid == session_id:
                    return item
            return None

    def cancel_qr_login(self: "AccountManager", session_id: str) -> bool:
        session = self.get_qr_session(session_id)
        if session is None:
            return False
        session.cancel()
        return True

    def gc_qr_sessions(self: "AccountManager", *, keep: int = 20) -> int:
        """清理已结束的会话，避免长时间运行后无限堆积。

        终态会话保留最近 ``keep`` 个：前端可能还会最后查一次结果拿用户名，
        全部立刻删掉会导致「明明扫成功了却查不到」。
        """
        removed = 0
        with self._lock:
            finished = [(key, sess) for key, sess in self._qr_sessions.items() if sess.finished]
            for key, _ in finished[:-keep] if len(finished) > keep else []:
                self._qr_sessions.pop(key, None)
                removed += 1
        return removed

    def adopt_qr_user(self: "AccountManager", temp_user: Any, username: str) -> Any:
        """把扫码用的临时 User 原地采纳为正式账号。

        必须复用同一个 User 对象：登录态（Cookie/tk）就在它的 HttpClient 上，
        换成新建的 User 会把会话丢掉，表现为「扫码提示成功、账号却仍是离线」。
        """
        username = (username or "").strip()
        if not username:
            raise ValidationError("账号用户名不能为空")

        with self._lock:
            replaced = self._users.pop(username, None)
            previous_password = ""
            if replaced is not None and replaced is not temp_user:
                previous_password = replaced.password
                replaced.stop_heartbeat()

            temp_user.username = username
            temp_user.config.username = username
            # 扫码账号通常没有密码；若该账号原本有密码，保留下来以便之后仍可密码登录
            if previous_password and not temp_user.password:
                temp_user.set_password(previous_password)
            temp_user.heartbeat_interval = 120
            temp_user.is_login = True
            temp_user.status = STATUS_ONLINE
            temp_user.last_active = time.time()
            temp_user.stats.successes += 1
            temp_user.stats.last_login_at = time.time()
            self._users[username] = temp_user

        if self.auto_heartbeat:
            temp_user.start_heartbeat()
        # 扫码成功是「会话刚刚建立」的时刻，立刻把 Cookie 加密留存 ——
        # 否则程序一关就得重新扫码（扫码必须有人拿手机配合，成本很高）。
        save = getattr(temp_user, "save_session", None)
        if callable(save):
            save()
        log.info("扫码登录账号已采纳: %s", username)
        self._emit("qr_login_success", {"user": username})
        return temp_user

    AccountManager.start_qr_login = start_qr_login          # type: ignore[attr-defined]
    AccountManager.get_qr_session = get_qr_session          # type: ignore[attr-defined]
    AccountManager.cancel_qr_login = cancel_qr_login        # type: ignore[attr-defined]
    AccountManager.gc_qr_sessions = gc_qr_sessions          # type: ignore[attr-defined]
    AccountManager.adopt_qr_user = adopt_qr_user            # type: ignore[attr-defined]


_install_qr_login_methods()
