"""智能候补引擎（4.4 / HB-01 ~ HB-06）。

Py12306 没有候补能力，本模块为二次开发新增，严格遵循官方硬约束：

| 约束 | 值 | 实现位置 |
|---|---|---|
| 每账号同时存在的待兑现候补订单 | ≤ 6 | :meth:`StandbyEngine.check_quota` |
| 每订单可选的乘车日期 | ≤ 3 | :meth:`StandbyEngine.plan_combos` |
| 每订单「日期+车次」组合 | ≤ 60 | :meth:`StandbyEngine.plan_combos` |

**双轨策略（HB-05）**：候补提交成功后，抢票引擎继续跑捡漏循环；
任一轨成功即停另一轨（并默认取消候补订单，避免重复占票/重复支付）。

**人证核验前置（AC-07）**：候补要求账号完成人证一致性核验。但**只有官方明确说
未核验时才拦**；接口没返回核验字段（真机就是如此）属于「判不出来」，
必须放行并让官方去判 —— 否则会把正常账号永久挡在候补之外。
「怎么核验」的出路见 :mod:`ticket_helper.account.verify`。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ticket_helper.account import verify
from ticket_helper.config.settings import (
    STANDBY_MAX_COMBOS_PER_ORDER,
    STANDBY_MAX_DATES_PER_ORDER,
    STANDBY_MAX_ORDERS,
    TaskConfig,
)
from ticket_helper.errors import (
    InterError,
    LoginError,
    RiskControlError,
    StandbyLimitError,
    VerifyRequiredError,
)

log = logging.getLogger(__name__)


def _int_or(value: Any, default: int) -> int:
    """官方有时把数字包成字符串（``"12"`` / ``12.0``）；判不出来就用默认值。"""
    try:
        return int(float(str(value).split(".")[0]))
    except (TypeError, ValueError):
        return default

#: 未提交（候补单尚未创建）。
#: 人类验收层把这一个态命名为 PENDING；原先这里是裸字符串 ``"idle"``，
#: 属于「引擎自创字符串」，与「状态值必须来自固定词汇表」的契约冲突。
STANDBY_PENDING = "pending"
STANDBY_QUEUING = "queuing"
STANDBY_SUCCESS = "success"
STANDBY_FAILED = "failed"
STANDBY_CANCELLED = "cancelled"
STANDBY_STOPPED = "stopped"

#: 候补轨的**全部**状态值（中间态 + 终态）。
#:
#: 存储层用它做「未知状态不得入库」的校验 —— 那边**不复制**这些字面量，
#: 只按名字来这里取（见 `storage/database.py::_STATUS_SOURCES`），
#: 所以这里改名或删值，入库校验会**立刻**跟着变，不会静默漂移。
#:
#: ⚠️ 这里**没有** "submitted"：它曾经存在过，但全仓从不赋值
#: （原 `stop()` 里那处 `in (SUBMITTED, QUEUING)` 是死比较），
#: 而建表默认值 `status TEXT DEFAULT 'submitted'` 又把它伪装成「有产生点」，
#: 于是僵尸状态守卫一直放行它。删掉那个默认值之后守卫立刻抓到了它 ——
#: 「默认值算产生点」这个判据本身就是错的。
STANDBY_STATES: tuple[str, ...] = (
    STANDBY_PENDING,
    STANDBY_QUEUING,
    STANDBY_SUCCESS,
    STANDBY_FAILED,
    STANDBY_CANCELLED,
    STANDBY_STOPPED,
)

#: 候补轨的**终态**集合：值落在其中就表示这条轨不会再变了。
#:
#: 这是**唯一的一份定义**。其它模块（例如 ``grab.engine`` 的「仅候补」等待逻辑）
#: 必须 import 它，不允许再抄一份字符串 —— 抄一份的后果是静默的：
#: 改了这里的常量名，那边会留着旧字面量，于是任务永远等不到终态而挂死，
#: 且全程没有任何报错。``tests/test_contracts.py`` 盯着这条。
STANDBY_TERMINAL_STATES: tuple[str, ...] = (
    STANDBY_SUCCESS,
    STANDBY_FAILED,
    STANDBY_CANCELLED,
    STANDBY_STOPPED,
)

#: 候补排队轮询的**最小间隔**（秒）。
#: 官方对连续请求敏感（订单接口连打 4 次就会被 WAF 拦），所以生产环境必须低频，
#: 这条下限就是那个策略。测试可以临时改小它来缩短等待 ——
#: 只影响「多久问一次」，不影响兑现判据与轮询次数，语义不变。
MIN_POLL_INTERVAL = 15.0


@dataclass
class StandbyPlan:
    """候补计划（已按官方上限裁剪）。"""

    dates: list[str] = field(default_factory=list)
    combos: list[dict[str, Any]] = field(default_factory=list)
    seat_types: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def combo_count(self) -> int:
        return len(self.combos)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dates": self.dates,
            "combos": self.combos,
            "seat_types": self.seat_types,
            "combo_count": self.combo_count,
            "warnings": self.warnings,
        }


class StandbyEngine:
    """单个任务的候补引擎。"""

    def __init__(
        self,
        task: TaskConfig,
        *,
        account_provider: Any,
        notifier: Any = None,
        db: Any = None,
        poll_interval: float = 60.0,
        cancel_on_stop: bool = True,
        max_orders: int = STANDBY_MAX_ORDERS,
        order_check_interval: float = 300.0,
    ) -> None:
        self.task = task
        self.account_provider = account_provider
        self.notifier = notifier
        self.db = db
        self.poll_interval = max(MIN_POLL_INTERVAL, poll_interval)
        self.cancel_on_stop = cancel_on_stop
        self.max_orders = max_orders
        #: 查「我的订单」确认候补是否兑现的间隔（秒）。
        #: 订单接口对连续请求敏感（连打 4 次就被 WAF 拦），所以必须低频，
        #: 而且要与排队轮询分开计时 —— 用「第 N 轮查一次」那种写法，
        #: 一旦 poll_interval 变了就会跟着变形，迟早变成高频。
        self.order_check_interval = max(15.0, order_check_interval)

        self.status = STANDBY_PENDING
        self.hb_order_id = ""
        self.plan = StandbyPlan()
        #: 候补链路第一步 chechFace 的结果 —— **核验的唯一权威判据**（face_flag）
        self.face: dict[str, Any] = {}
        #: 官方给的成功率说明（level 越小越有戏，info 是中文文案）
        self.success_rate: dict[str, Any] = {}
        #: 最近一次排队状态（status==1 表示候补单已生效、待支付）
        self.queue: dict[str, Any] = {}
        #: 是否已经把「30 分钟内去支付」的提醒发出去过（同一单只提醒一次）
        self._pay_reminded = False
        #: 下一次查订单的时刻（monotonic）。初始 0 = 进循环就先查一次，
        #: 这样「第一次兑现检查」不必等满一个 order_check_interval。
        self._next_order_check = 0.0
        self.error = ""
        self.stats = {"attempts": 0, "polls": 0, "success": 0, "failures": 0}

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._grab_engine: Any = None
        self._lock = threading.RLock()

    # ================================================================== #
    # 上限校验（HB-02 / HB-06）—— 纯逻辑，便于单测（HB-T1）
    # ================================================================== #
    def plan_combos(self, train_codes: list[str], *, seats: list[str] | None = None) -> StandbyPlan:
        """生成候补组合并裁剪到官方上限。"""
        plan = StandbyPlan(seat_types=list(seats or self.task.seat_types))

        dates = list(dict.fromkeys(self.task.left_dates))
        if len(dates) > STANDBY_MAX_DATES_PER_ORDER:
            plan.warnings.append(
                f"乘车日期 {len(dates)} 个超过官方上限 {STANDBY_MAX_DATES_PER_ORDER} 个，已截取前 {STANDBY_MAX_DATES_PER_ORDER} 个"
            )
            dates = dates[:STANDBY_MAX_DATES_PER_ORDER]
        plan.dates = dates

        trains = [t.strip().upper() for t in train_codes if t.strip()] or [""]
        pairs = [(f, t) for f in self.task.from_stations for t in self.task.to_stations]

        combos: list[dict[str, Any]] = []
        for date in dates:
            for from_station in self.task.from_stations:
                for to_station in self.task.to_stations:
                    for train_code in trains:
                        combos.append(
                            {
                                "date": date,
                                "from_station": from_station,
                                "to_station": to_station,
                                "train_code": train_code,
                            }
                        )
        if not pairs:
            plan.warnings.append("未配置起止站，无法生成候补组合")

        if len(combos) > STANDBY_MAX_COMBOS_PER_ORDER:
            plan.warnings.append(
                f"「日期+车次」组合 {len(combos)} 个超过官方上限 {STANDBY_MAX_COMBOS_PER_ORDER} 个，已截取前 {STANDBY_MAX_COMBOS_PER_ORDER} 个"
            )
            combos = combos[:STANDBY_MAX_COMBOS_PER_ORDER]
        plan.combos = combos

        if not combos:
            raise StandbyLimitError("候补组合为空：请检查任务的日期、起止站与车次配置")
        return plan

    def check_quota(self) -> int:
        """检查账号候补订单配额（HB-06）；返回剩余可用订单数。"""
        active = 0
        if self.db is not None:
            active = self.db.count_active_standby()
        remaining = self.max_orders - active
        if remaining <= 0:
            raise StandbyLimitError(
                f"候补订单已达官方上限（{self.max_orders} 单），请先等待已有候补兑现或取消后再试"
            )
        return remaining

    # ================================================================== #
    # 生命周期
    # ================================================================== #
    def start(self, grab_engine: Any) -> bool:
        """跟随抢票引擎启动候补轨。"""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            if self.task.strategy not in ("standby", "dual"):
                return False
            self._grab_engine = grab_engine
            plan = self.plan_combos(self.task.trains_whitelist)
            if plan.warnings:
                for warning in plan.warnings:
                    log.warning("任务[%s]候补计划: %s", self.task.name, warning)
            self.plan = plan
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name=f"standby-{self.task.id or self.task.name}", daemon=True
            )
            self._thread.start()
        log.info("任务[%s]候补轨已启动（计划组合 %d 个）", self.task.name, self.plan.combo_count)
        return True

    def _fail(self, reason: str) -> None:
        """进入终态 ``FAILED``。**这是唯一写 FAILED 的地方。**

        为什么必须收成一处：状态机契约要求「终态不可转出」，
        而终态若散落在十几处 ``self.status = STANDBY_FAILED``，
        既没人能一眼看全有哪些失败出口，也无法保证每处都带上失败原因
        （原先「排队失败」那条路径的 ``self.error`` 就是空的）。
        """
        self.error = reason
        self.status = STANDBY_FAILED
        log.error("任务[%s]候补失败: %s", self.task.name, reason)

    def stop(self, *, reason: str = "手动停止", cancel: bool | None = None) -> None:
        """停止候补轨；默认按配置决定是否取消已提交的候补订单。"""
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=5.0)
        self._thread = None

        should_cancel = self.cancel_on_stop if cancel is None else cancel
        # 只认「排队中」：`submitted` 这个中间态并不存在（见 `STANDBY_STATES` 的说明），
        # 留着它会让读代码的人以为「刚提交、还没排队」也是一条真实路径。
        if should_cancel and self.hb_order_id and self.status == STANDBY_QUEUING:
            self._cancel_order(self.hb_order_id)
        if self.status not in (STANDBY_SUCCESS,):
            self.status = STANDBY_CANCELLED if should_cancel else STANDBY_STOPPED
        log.info("任务[%s]候补轨已停止: %s", self.task.name, reason)

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    # ================================================================== #
    def _run(self) -> None:
        try:
            if not self._create_order():
                return
            self._monitor_loop()
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            log.exception("任务[%s]候补引擎异常: %s", self.task.name, exc)
            if self.notifier is not None:
                self.notifier.notify_error(self.task.name, f"候补引擎异常: {exc}")

    def _resolve_user(self, *, ignore_stop: bool = False, attempts: int = 3) -> Any:
        """获取一个已登录账号。

        ``ignore_stop=True`` 供**停止流程**使用：``stop()`` 会先置位停止事件，
        若此处仍检查停止标志，取消候补订单的请求就永远发不出去，
        结果是「捡漏出票了但候补单还挂着」——可能导致重复占票/重复支付。
        因此停止流程下改为固定尝试若干次后放弃，既不被标志挡住，也不会卡住 UI。
        """
        remaining = 20 if ignore_stop else attempts
        deadline = time.time() + 60
        while remaining > 0 and time.time() < deadline:
            if self._stop_event.is_set() and not ignore_stop:
                return None
            user = self.account_provider(self.task)
            if user is not None and user.is_login:
                return user
            remaining -= 1
            if remaining <= 0:
                break
            self._stop_event.wait(1.0 if ignore_stop else 3.0)
        return None

    def _create_order(self) -> bool:
        """HB-01：自动创建候补订单。"""
        user = self._resolve_user()
        if user is None or user.api is None:
            self._fail("无可用账号")
            return False

        # AC-07：只有**官方明确说未核验**才拦。
        #
        # 这里曾经写成「if not user.is_checked: 拦」，把「判不出来」也拦了 ——
        # 而真机实测（scripts/diagnose_verify.py）官方这两个用户信息接口
        # （initQueryUserInfoApi / initMy12306Api）**都不返回核验字段**，
        # 于是 verified_known 永远是 False，「判不出来」必然发生：
        # 一个完全正常的账号会被永久拦在候补之外，且提示说的是「未完成人证核验」，
        # 让人以为是账号被限制了 —— 属于有害的误导。
        #
        # 正确做法：判不出来就**放行**，让官方去判。官方真拒了，
        # 会在提交响应里给出原因，我们再把它翻成人话（见下面的 except 分支）。
        if not user.is_checked:
            user.detect_verified()
        if verify.should_block(user):
            self._fail(f"账号未完成人证核验，无法使用候补（{verify.hint_of(user)}）")
            log.error("任务[%s]账号 %s 明确未核验，跳过候补", self.task.name, user.username)
            if self.notifier is not None:
                self.notifier.notify_standby(
                    self.task.name, verify.standby_block_message(user.username)
                )
            return False
        # 说明：上面这一道只是「零成本前置」。**真正权威的核验判据在候补链路第一步**
        # `afterNate/chechFace` 的 face_flag（见下面 except VerifyRequiredError），
        # 因为用户信息接口根本不返回核验字段，在那里永远判不出结论。
        if not user.verify_known:
            # 不拦，但要留痕：以后有人问「它到底判没判」，日志里要看得见
            log.info(
                "任务[%s]账号 %s 的核验状态无法判定（官方未返回该字段），照常尝试候补",
                self.task.name,
                user.username,
            )

        try:
            remaining = self.check_quota()
        except StandbyLimitError as exc:
            self._fail(exc.message)
            log.error("任务[%s]候补配额不足: %s", self.task.name, exc.message)
            if self.notifier is not None:
                self.notifier.notify_standby(self.task.name, exc.message)
            return False

        self.stats["attempts"] += 1
        try:
            train_codes = self._discover_candidate_trains(user)
            self.plan = self.plan_combos(train_codes)
            passengers = self._resolve_passengers(user)
            if not passengers:
                self._fail("未获取到乘车人")
                return False

            target = self._find_standby_target(user, train_codes)
            if target is None:
                self._fail("未找到可候补的车次（可能全部有票或已停售）")
                log.info("任务[%s]当前没有可候补的车次", self.task.name)
                return False

            # 这条链路会依次走：chechFace（登录/核验）→ getSuccessRate →
            # submitOrderRequest → passengerInitApi → getQueueNum → confirmHB → queryQueue。
            # 官方**不再返回候补单号**，只以「排队 status==1 = 候补单已生效」为准，
            # 所以 hb_order_id 留空、由订单页去认领，不要造一个假单号出来。
            result = user.api.confirm_hb(
                passengers,
                self.plan.seat_types,
                target,
                from_name=target.from_station,
                to_name=target.to_station,
            )
            queue = result.get("queue") or {}
            rate = result.get("success_rate") or {}
            self.hb_order_id = str(result.get("hb_order_id") or "")
            self.face = result.get("face") or {}
            self.success_rate = rate
            self.queue = queue
            self.status = STANDBY_QUEUING
            log.info(
                "任务[%s]候补订单已提交并生效: 车次=%s 日期=%s 席别=%s 排队=%s 成功率=%s（账号剩余可用候补单 %d）",
                self.task.name,
                target.train_code,
                target.train_date,
                "/".join(self.plan.seat_types),
                queue.get("status"),
                rate.get("info") or rate.get("level"),
                remaining - 1,
            )
            if self.db is not None:
                self.db.save_standby_order(
                    {
                        "task_id": self.task.id,
                        "account": user.username,
                        "order_no": self.hb_order_id,
                        "combos": self.plan.combos,
                        "seat_types": self.plan.seat_types,
                        "status": STANDBY_QUEUING,
                        "result": f"候补单已生效（排队 {queue.get('status')}）",
                    }
                )
            if self.notifier is not None:
                # 候补单生效后**必须在 30 分钟内到 12306 支付**，否则订单会被取消 ——
                # 这一点比「已提交」重要得多，必须写进通知正文。
                rate_text = f"官方预估：{rate.get('info')}" if rate.get("info") else ""
                self.notifier.notify_standby(
                    self.task.name,
                    f"已自动提交候补订单并生效（{self.plan.combo_count} 个「日期+车次」组合，"
                    f"席别 {'/'.join(self.plan.seat_types)}）。\n"
                    f"⚠️ 请到 12306 的「候补订单」在 **30 分钟内完成支付**，"
                    f"本程序不代扣款、也不代付；超时未付订单会被自动取消。\n"
                    f"同时继续捡漏（任一出票即会提醒）。"
                    + (f"\n{rate_text}" if rate_text else ""),
                    {
                        "train_no": target.train_code,
                        "depart_date": target.train_date,
                        "from_station": target.from_station,
                        "to_station": target.to_station,
                        "seat_type": "/".join(self.plan.seat_types),
                    },
                )
            return True
        except StandbyLimitError as exc:
            self._fail(exc.message)
            log.error("任务[%s]候补受限: %s", self.task.name, exc.message)
            return False
        except RiskControlError as exc:
            self._fail(str(exc))
            log.error("任务[%s]候补触发风控: %s", self.task.name, exc)
            if self.notifier is not None:
                self.notifier.notify_risk(self.task.name, f"候补提交触发风控：{exc.message}")
            return False
        except VerifyRequiredError as exc:
            # **真机上唯一能证明「确实需要核验」的信号**（chechFace.face_flag=false）。
            # 这一类失败重试无用，必须把「去 12306 App 做核验」的出路给到使用者。
            self._fail(f"12306 要求先完成人证核验才能候补：{exc}")
            self.stats["failures"] += 1
            log.error("任务[%s]账号 %s 的人证核验未通过（chechFace.face_flag=false）", self.task.name, user.username)
            if self.notifier is not None:
                self.notifier.notify_standby(
                    self.task.name, verify.standby_block_message(user.username)
                )
            return False
        except (InterError, LoginError) as exc:
            self._fail(str(exc))
            self.stats["failures"] += 1
            log.warning("任务[%s]候补提交失败: %s", self.task.name, exc)
            if self.notifier is not None:
                # 官方真拒了：如果是核验问题，把它翻成「去哪儿点两下」，
                # 而不是把官方那句生硬的原话丢给使用者。
                if verify.is_unverified_error(str(exc)):
                    self.error = f"官方拒绝候补提交：未完成人证核验。{verify.HINT[verify.STATE_UNVERIFIED]}"
                    self.notifier.notify_standby(
                        self.task.name, verify.standby_block_message(user.username)
                    )
                else:
                    self.notifier.notify_error(self.task.name, f"候补提交失败：{exc}")
            return False

    def _discover_candidate_trains(self, user: Any) -> list[str]:
        """从当前余票中挑出「仅候补」的车次（HB-01）。"""
        assert user.api is not None
        codes: list[str] = []
        for from_station, to_station, train_date in self.task.combos()[:3]:
            try:
                tickets = user.api.query_left_ticket(
                    train_date,
                    user.api.stations.code_of(from_station),
                    user.api.stations.code_of(to_station),
                )
            except (InterError, Exception) as exc:  # noqa: BLE001
                log.debug("候补探测查询失败: %s", exc)
                continue
            for ticket in tickets:
                if self.task.trains_whitelist and ticket.train_code.upper() not in {
                    t.upper() for t in self.task.trains_whitelist
                }:
                    continue
                if ticket.train_code and ticket.train_code not in codes:
                    codes.append(ticket.train_code)
        return codes

    def _find_standby_target(self, user: Any, train_codes: list[str]) -> Any:
        """找到一条可用于候补的车次记录（取其 secretStr/位置码等参数）。"""
        assert user.api is not None
        for from_station, to_station, train_date in self.task.combos():
            try:
                tickets = user.api.query_left_ticket(
                    train_date,
                    user.api.stations.code_of(from_station),
                    user.api.stations.code_of(to_station),
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("候补目标查询失败: %s", exc)
                continue
            for ticket in tickets:
                if train_codes and ticket.train_code not in train_codes:
                    continue
                # **必须当前可网订**：真机实测「列车运行图调整,暂停发售」那类车次的
                # secretStr 是空的，拿去候补只会得到一句「系统忙」——白跑一整条链路。
                if not ticket.buyable:
                    log.debug("跳过 %s：当前不可网订（%s）", ticket.train_code, ticket.can_web_buy or "-")
                    continue
                if ticket.standby_seats(self.task.seat_types) or not ticket.has_any_ticket:
                    return ticket
        return None

    def _resolve_passengers(self, user: Any) -> list[dict[str, Any]]:
        wanted = [p.strip() for p in self.task.passengers if p.strip()]
        try:
            all_passengers = user.get_passengers()
        except Exception as exc:  # noqa: BLE001
            log.warning("候补获取乘车人失败: %s", exc)
            return []
        if not all_passengers:
            return []
        if not wanted:
            return all_passengers[:1]
        by_name = {str(p.get("passenger_name", "")).strip(): p for p in all_passengers}
        selected = [by_name[n] for n in wanted if n in by_name]
        return selected or all_passengers[:1]

    # ------------------------------------------------------------------ #
    def _monitor_loop(self) -> None:
        """HB-04：轮询候补排队状态，并定期查订单确认是否兑现。

        **真机的 ``afterNate/queryQueue`` 语义**（与旧实现完全不同）：

        - ``status == 1``  → 排队成功：候补单已生效、**待支付**；
        - ``status == -1`` → 排队失败；
        - 其它值           → 还在排队，``waitTime`` 秒后再问。

        旧实现把这几个数当成 ``"success"/"failed"`` 这样的英文状态字符串去比，
        永远匹配不上；而且候补**兑现**根本不在这个接口里体现 ——
        兑现结果要落到「我的订单」里去看，所以这里要**低频**查一次订单
        （订单接口对连续请求敏感，连打 4 次就会被 WAF 拦，绝不能放进内层循环）。
        """
        user = self._resolve_user()
        if user is None or user.api is None:
            return
        while not self._stop_event.is_set():
            if self._grab_engine is not None and getattr(self._grab_engine, "state", "") == "success":
                log.info("任务[%s]捡漏已成功，停止候补监控", self.task.name)
                return
            self.stats["polls"] += 1
            delay = self.poll_interval
            try:
                queue = user.api.query_hb_queue()
                self.queue = queue
                status = queue.get("status")
                wait_time = _int_or(queue.get("waitTime"), 0)
                if status == -1:
                    self._fail("候补排队失败（12306 未接受这笔候补需求）")
                    log.warning("任务[%s]候补排队失败", self.task.name)
                    if self.db is not None:
                        self.db.save_standby_order(
                            {
                                "task_id": self.task.id,
                                "order_no": self.hb_order_id,
                                "status": STANDBY_FAILED,
                                "result": "排队失败",
                            }
                        )
                    if self.notifier is not None:
                        self.notifier.notify_standby(
                            self.task.name, "候补排队失败：12306 未接受这笔候补需求，稍后会自动重试。"
                        )
                    return
                if status == 1 and not self._pay_reminded:
                    # 这一刻起，订单真的存在了，而且**只有 30 分钟可以付** ——
                    # 不提醒的话，「抢到了但忘了付」是必然会发生的事。
                    self._pay_reminded = True
                    log.info("任务[%s]候补单已生效，等待支付", self.task.name)
                    if self.notifier is not None:
                        self.notifier.notify_standby(
                            self.task.name,
                            "候补订单已生效（排队成功）。\n"
                            "⚠️ 请尽快到 12306 的「候补订单」完成支付 —— "
                            "官方给的支付窗口是 **30 分钟**，超时会自动取消；本程序不代扣款。",
                            {
                                "train_no": self.plan.combos[0].get("train_code", "-") if self.plan.combos else "-",
                                "depart_date": self.plan.dates[0] if self.plan.dates else "-",
                                "from_station": self.task.from_stations[0] if self.task.from_stations else "-",
                                "to_station": self.task.to_stations[0] if self.task.to_stations else "-",
                                "seat_type": "/".join(self.plan.seat_types),
                            },
                        )
                # 官方建议等 waitTime/3；取一个既不过密也不至于太慢的值
                if wait_time > 0:
                    delay = max(self.poll_interval, min(wait_time / 3.0, 120.0))
                log.debug("任务[%s]候补排队中: %s", self.task.name, queue)
            except Exception as exc:  # noqa: BLE001
                log.debug("候补进度查询异常: %s", exc)

            # 按**时间**查一次订单，确认有没有真的出票（默认 5 分钟一次）
            if time.monotonic() >= self._next_order_check:
                if self._check_fulfilled_by_order(user):
                    return
                self._next_order_check = time.monotonic() + self.order_check_interval
            self._stop_event.wait(delay)

    def _check_fulfilled_by_order(self, user: Any) -> bool:
        """从「我的订单（未出行）」里确认候补是否已兑现。

        候补兑现是**异步**的：系统匹配到票就直接生成已支付订单。
        所以判断依据不该是排队接口，而是订单里**有没有出现这一趟**。
        返回 True 表示已兑现（调用方随即结束监控）。
        """
        assert user.api is not None
        codes = {(c or "").upper() for c in (self.task.trains_whitelist or [])}
        dates = set(self.plan.dates or self.task.left_dates or [])
        try:
            payload = user.api.query_order_history(where="G", page_size=5)
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 这里**不能**只打 debug。2026-09-30 真发生过：`query_order_history`
            # 里一个属性没初始化 → 每次调用都抛 AttributeError → 被这行吞掉 →
            # 监控循环**永远等不到兑现、也不会报错**，测试直接卡死 20 分钟才被发现。
            # 「查订单失败」意味着「兑现判定失效」，必须让人看见。
            log.warning("候补兑现检查（订单查询）失败，本轮无法判定是否已出票: %s", exc)
            return False
        for order in payload.get("orders") or []:
            code = str(order.get("train_code") or "").upper()
            date = str(order.get("train_date") or "")
            if codes and code not in codes:
                continue
            if dates and date and date not in dates:
                continue
            if not code:
                continue
            log.info("任务[%s]订单里发现已兑现的候补车票: %s %s %s", self.task.name, code, date, order.get("order_no"))
            self._on_fulfilled({**order, "stationTrainCode": code, "train_date": date})
            return True
        return False

    def _on_fulfilled(self, info: dict[str, Any]) -> None:
        """HB-04：候补成功 → 多渠道通知 + 引导支付 + 停止捡漏轨。"""
        self.status = STANDBY_SUCCESS
        self.stats["success"] += 1
        log.info("🎫 任务[%s]候补兑现成功！订单 %s", self.task.name, self.hb_order_id)
        if self.db is not None:
            self.db.save_standby_order(
                {"task_id": self.task.id, "order_no": self.hb_order_id, "status": STANDBY_SUCCESS,
                 "result": "候补兑现成功"}
            )
        if self.notifier is not None:
            self.notifier.notify_success(
                self.task.name,
                {
                    "train_no": info.get("stationTrainCode", "-"),
                    "depart_date": info.get("train_date", "-"),
                    "from_station": self.task.from_stations[0] if self.task.from_stations else "-",
                    "to_station": self.task.to_stations[0] if self.task.to_stations else "-",
                    "seat_type": info.get("seatTypeName", "/".join(self.plan.seat_types)),
                    "passenger": ",".join(self.task.passengers) or "-",
                    "order_no": self.hb_order_id,
                },
            )
        if self._grab_engine is not None:
            self._grab_engine.stop(reason="候补已兑现成功")

    def _cancel_order(self, order_id: str) -> bool:
        # 注意：这里必须允许在「已置位停止标志」的情况下取账号，否则取消请求发不出去
        user = self._resolve_user(ignore_stop=True)
        if user is None or user.api is None:
            return False
        try:
            ok = user.api.cancel_hb(order_id)
            if ok:
                self.status = STANDBY_CANCELLED
                log.info("任务[%s]已取消候补订单 %s（避免与已出票订单重复占票）", self.task.name, order_id)
                if self.db is not None:
                    self.db.save_standby_order(
                        {"task_id": self.task.id, "order_no": order_id, "status": STANDBY_CANCELLED,
                         "result": "另一轨已出票，自动取消"}
                    )
            return ok
        except Exception as exc:  # noqa: BLE001
            log.warning("取消候补订单失败（请手动到 12306 检查）: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    def status_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task.id,
            "task_name": self.task.name,
            "status": self.status,
            "running": self.running,
            "hb_order_id": self.hb_order_id,
            "face": dict(self.face),
            "success_rate": dict(self.success_rate),
            "queue": dict(self.queue),
            "error": self.error,
            "plan": self.plan.to_dict(),
            "stats": dict(self.stats),
            "poll_interval": self.poll_interval,
        }

    def apply_config(self, task: TaskConfig) -> None:
        self.task = task
