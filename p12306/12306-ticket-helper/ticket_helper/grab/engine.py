"""抢票引擎（4.4 / GR-01 ~ GR-08）。

状态机：``IDLE → READY → GRABBING → SUCCESS | STOPPED``（GR-T1）。

两种模式在一套主循环里统一实现：
- **准点秒杀（GR-01）**：``sale_open_time`` 未到则进入 READY 待命，
  到点前一瞬发起首次查询，做到「开售即下单」；待命期间**不空转查询**，
  避免无意义的请求触发风控（长时间挂机验收项）；
- **余票捡漏（GR-02）**：无票时按 ``query_interval`` 循环，
  发现余票立即进入下单流程。

所有探测结果都会回喂给限速器：风控 → 降频，平稳 → 逐步恢复。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from ticket_helper.account.manager import User
from ticket_helper.config.settings import TaskConfig
from ticket_helper.errors import (
    InterError,
    LoginError,
    RiskControlError,
    TrainDateUnavailableError,
    VerifyCodeError,
)
from ticket_helper.grab.strategy import PickStrategy
from ticket_helper.logger import metrics
from ticket_helper.order.service import ORDER_SUCCESS, OrderResult, OrderService
from ticket_helper.query.engine import Candidate, QueryEngine
from ticket_helper.ratelimit.limiter import RateLimiter
from ticket_helper.standby.engine import STANDBY_TERMINAL_STATES
from ticket_helper import train_date as train_date_util

log = logging.getLogger(__name__)

#: 引擎状态
STATE_IDLE = "idle"
STATE_READY = "ready"
STATE_GRABBING = "grabbing"
STATE_SUCCESS = "success"
STATE_STOPPED = "stopped"
STATE_ERROR = "error"

#: 运行状态 → 对外展示文案
STATE_TEXT = {
    STATE_IDLE: "空闲",
    STATE_READY: "待命（等待开售）",
    STATE_GRABBING: "抢票中",
    STATE_SUCCESS: "已抢到",
    STATE_STOPPED: "已停止",
    STATE_ERROR: "异常",
}

#: 候选「暂时跳过」的冷却时间（秒）。
#: 必须是**有时限**的：风控、瞬间无票、排队失败都可能是暂时性的，
#: 如果把它们当成永久黑名单，那么被拉黑的候选（尤其是任务只配了一个车次时）
#: 将永远不再被尝试 —— 任务会继续空转查询，却再也不会下单，用户只会觉得「它明明有票却不抢」。
FAILED_CANDIDATE_COOLDOWN = 30.0

#: 候补轨的终态集合。**引用 ``standby.engine`` 的那一份，不在这里重新抄。**
#:
#: 原先这里是 ``("success", "failed", "cancelled", "stopped")``，理由写的是
#: 「两边互相 import 会形成环」—— 那个理由**是错的**（已核）：
#: ``standby.engine`` 只 import ``account.verify`` / ``config.settings`` /
#: ``errors`` / ``inter.parser``，不 import ``grab.engine``，不存在环。
#: 抄一份的后果很具体且**静默**：``STANDBY_SUCCESS`` 一旦改名，
#: 这里会留着旧字面量，「仅候补」的任务就永远等不到终态而挂死，全程无报错。
#: ``tests/test_contracts.py`` 盯着这条（改名会立刻红）。

#: 需要走捡漏（直购）循环的策略。``standby`` **不在其中** ——
#: 界面文案是「仅候补」，语义上不允许产生直购订单。
DIRECT_PURCHASE_STRATEGIES = ("direct", "dual")

#: 「日期暂不可查」（还没开售）时的重试间隔。
#:
#: 为什么不能沿用 ``query_interval``（默认 1 秒）：官方对这类日期的请求**照样会拒**，
#: 一秒一次猛冲的结果是先把会话真的打成限流 —— 症状就从「日期还没到」
#: 变成了「真的被限频」，排查难度直接上一个数量级。
#: 而这个日期一天最多变一次状态（开售），所以每分钟问一次已经远远够用。
DATE_UNAVAILABLE_RETRY_INTERVAL = 60.0


@dataclass
class GrabStats:
    rounds: int = 0
    queries: int = 0
    candidates_seen: int = 0
    order_attempts: int = 0
    order_failures: int = 0
    risk_events: int = 0
    started_at: float = 0.0
    last_query_at: float = 0.0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "queries": self.queries,
            "candidates_seen": self.candidates_seen,
            "order_attempts": self.order_attempts,
            "order_failures": self.order_failures,
            "risk_events": self.risk_events,
            "started_at": self.started_at,
            "last_query_at": self.last_query_at,
            "last_error": self.last_error,
        }


class GrabEngine:
    """单个任务的抢票引擎（一个实例 = 一条抢票线程）。"""

    def __init__(
        self,
        task: TaskConfig,
        *,
        account_provider: Callable[[TaskConfig], User | None],
        order_service: OrderService,
        notifier: Any = None,
        db: Any = None,
        rate_limiter: RateLimiter | None = None,
        station_map: Any = None,
        standby_engine: Any = None,
        on_state_change: Callable[[str, str, dict[str, Any]], None] | None = None,
        prepare_timeout: float = 60.0,
    ) -> None:
        self.task = task
        self.account_provider = account_provider
        self.orders = order_service
        self.notifier = notifier
        self.db = db
        self.rate_limiter = rate_limiter
        self.station_map = station_map
        self.standby_engine = standby_engine
        self.on_state_change = on_state_change
        #: 启动准备阶段等待可用账号的最长时间（可在测试中调小）
        self.prepare_timeout = max(0.0, float(prepare_timeout))

        self.state = STATE_IDLE
        self.stats = GrabStats()
        self.strategy = PickStrategy(task)
        self.query: QueryEngine | None = None
        self.user: User | None = None
        self.error: str = ""
        self.result: OrderResult | None = None

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        #: 候选 key → 上次失败时间（带冷却，不是永久黑名单）
        self._failed_keys: dict[str, float] = {}
        self._last_candidates: list[Candidate] = []
        self._pause_until: float = 0.0

    # ================================================================== #
    # 生命周期
    # ================================================================== #
    def start(self) -> bool:
        """启动抢票线程（幂等）。"""
        with self._lock:
            if self._thread and self._thread.is_alive():
                log.info("任务[%s]已在运行中", self.task.name)
                return False
            self._stop_event.clear()
            self._failed_keys.clear()
            self.orders.reset_dedup()
            self.stats = GrabStats(started_at=time.time())
            self.error = ""
            self.result = None
            self._thread = threading.Thread(
                target=self._run, name=f"grab-{self.task.id or self.task.name}", daemon=True
            )
            self._thread.start()
        log.info("任务[%s]抢票引擎已启动", self.task.name)
        return True

    def stop(self, *, reason: str = "手动停止") -> None:
        """停止抢票线程。"""
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=5.0)
        self._thread = None
        if self.state not in (STATE_SUCCESS,):
            self._set_state(STATE_STOPPED, reason)

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    # ================================================================== #
    # 主循环
    # ================================================================== #
    def _run(self) -> None:
        try:
            if not self._prepare():
                return
            self._main_loop()
        except Exception as exc:  # noqa: BLE001 - 引擎线程绝不能崩出去
            self.error = str(exc)
            log.exception("任务[%s]抢票引擎异常退出: %s", self.task.name, exc)
            self._set_state(STATE_ERROR, f"引擎异常: {exc}")
            self._notify_error(f"抢票引擎异常: {exc}")
        finally:
            if self.state == STATE_GRABBING:
                self._set_state(STATE_STOPPED, "循环结束")

    def _prepare(self) -> bool:
        """准备阶段：校验日期 → 分配账号 → 确保登录 → 初始化查询引擎。"""
        self._set_state(STATE_IDLE, "正在准备")

        # 日期先于账号校验：日期填错了，等多久账号都没用。
        # 真机实测：12306 对「已过去 / 还没开售」的日期回的是与风控**完全相同**的
        # HTML 错误页，所以不在这里拦住，症状就会伪装成「🚨 风控」，
        # 使用者去降频、换账号、干等 —— 全都无效（详见 ticket_helper.train_date）。
        past_dates = [d for d in self.task.left_dates if train_date_util.classify(d) == train_date_util.DATE_PAST]
        if past_dates and len(past_dates) == len(self.task.left_dates):
            self.error = train_date_util.explain(past_dates[0])
            log.error("任务[%s]全部乘车日期都已过去，拒绝启动: %s", self.task.name, self.error)
            self._set_state(STATE_ERROR, self.error)
            self._notify_error(self.error)
            return False
        if past_dates:
            skipped = "、".join(past_dates)
            log.warning("任务[%s]跳过已过去的乘车日期: %s", self.task.name, skipped)
            self._notify_info(f"已跳过过去的乘车日期：{skipped}（12306 不再提供这些日期的余票）")

        deadline = time.time() + self.prepare_timeout
        while time.time() < deadline and not self._stop_event.is_set():
            user = self.account_provider(self.task)
            if user is None:
                log.warning("任务[%s]暂无可用账号（等待其他账号上线）", self.task.name)
                self._sleep(3.0)
                continue
            self.user = user
            if not user.is_login and not user.login():
                log.warning("任务[%s]账号 %s 登录失败", self.task.name, user.username)
                self._sleep(3.0)
                continue
            break
        else:
            if self._stop_event.is_set():
                self._set_state(STATE_STOPPED, "启动前被停止")
            else:
                self._notify_error("无可用账号：请检查账号配置与登录状态")
                self._set_state(STATE_ERROR, "无可用账号")
            return False

        if self.user is None or self.user.api is None:
            self._set_state(STATE_ERROR, "账号未就绪")
            return False

        self.query = QueryEngine(self.user.api, self.task, db=self.db)
        log.info(
            "任务[%s]已就绪: 账号=%s 日期=%s 起止=%s 席别=%s 策略=%s",
            self.task.name,
            self.user.username,
            ",".join(self.task.left_dates),
            f"{'/'.join(self.task.from_stations)}→{'/'.join(self.task.to_stations)}",
            "/".join(self.task.seat_types),
            self.task.strategy,
        )
        return True

    def _main_loop(self) -> None:
        assert self.query is not None

        # 准点秒杀：先待命到开售时刻（GR-01）
        if self.task.sale_open_time:
            self._set_state(STATE_READY, f"等待开售 {self.task.sale_open_time}")
            if not self._wait_for_sale_open():
                return

        self._set_state(STATE_GRABBING, "开始抢票")
        self._notify_info(
            f"任务启动，开始监控 {'/'.join(self.task.left_dates)} "
            f"{'/'.join(self.task.from_stations)}→{'/'.join(self.task.to_stations)}"
        )

        # 候补+捡漏双轨（HB-05）：先起候补轨，再进捡漏循环
        if self.task.strategy in ("standby", "dual") and self.standby_engine is not None:
            self.standby_engine.start(self)

        # 策略语义必须与两端 UI 的文案严格对应（desktop_ui/pages/tasks.py 与
        # web/static/index.html 都写着 ``standby · 仅候补``）：
        #   direct  → 只走直购捡漏
        #   standby → **仅候补**，不产生任何直购订单
        #   dual    → 候补 + 捡漏双轨
        #
        # 以前这里无所谓策略、无条件进入下面的捡漏循环，于是
        # ``strategy="standby"`` 时程序照样会去下直购单 —— 与界面上写的
        # 「仅候补」正面冲突。这不是文案问题：选它的人往往正是不想产生
        # 直购订单（例如怕同一趟车重复占票），结果却真的下了单。
        if self.task.strategy not in DIRECT_PURCHASE_STRATEGIES:
            self._run_standby_only()
            return

        consecutive_errors = 0
        while not self._stop_event.is_set():
            if self.state == STATE_SUCCESS:
                break
            self.stats.rounds += 1
            try:
                self._tick()
                consecutive_errors = 0
            except LoginError as exc:
                consecutive_errors += 1
                log.warning("任务[%s]会话失效，尝试恢复: %s", self.task.name, exc)
                if self.user is not None:
                    self.user.restore()
                self._handle_error_streak(consecutive_errors)
            except RiskControlError as exc:
                self.stats.risk_events += 1
                self.stats.last_error = str(exc)
                log.error("任务[%s]触发风控: %s", self.task.name, exc)
                if self.rate_limiter is not None:
                    self.rate_limiter.report_risk()
                self._notify_risk(f"检测到风控：{exc.message}，已自动降低查询频率")
                self._sleep(min(60.0, 10.0 * self.stats.risk_events))
            except TrainDateUnavailableError as exc:
                # 日期不在可售范围（已过去 / 还没开售）—— **这不是风控**。
                #
                # 三条都不能做：
                #   1) 不记 risk_events、不动限速器 —— 那会把「改一个日期就好」
                #      伪装成「被官方限流了」，使用者怎么调都调不好；
                #   2) 不按 query_interval 猛冲 —— 官方对这类日期一样会拒，
                #      猛冲只会把会话真的打成限流（症状升级，更难查）；
                #   3) 不刷屏通知 —— 这是**状态**不是**事件**，一天最多变一次。
                self.stats.last_error = exc.message
                log.warning("任务[%s]乘车日期暂不可查: %s", self.task.name, exc.message)
                self._notify_date_problem(exc.message)
                self._sleep(DATE_UNAVAILABLE_RETRY_INTERVAL)
            except InterError as exc:
                consecutive_errors += 1
                self.stats.last_error = str(exc)
                log.warning("任务[%s]查询异常: %s", self.task.name, exc)
                if self.rate_limiter is not None:
                    self.rate_limiter.report_error()
                self._handle_error_streak(consecutive_errors)
            except Exception as exc:  # noqa: BLE001
                consecutive_errors += 1
                self.stats.last_error = str(exc)
                log.exception("任务[%s]主循环未预期异常: %s", self.task.name, exc)
                self._handle_error_streak(consecutive_errors)

            self._sleep(self._current_interval())

        if self.state == STATE_SUCCESS and self.standby_engine is not None:
            # 捡漏成功 → 停掉候补轨（HB-05 成其一即停）
            self.standby_engine.stop(reason="捡漏已成功出票")

    def _run_standby_only(self) -> None:
        """仅候补：只等候补轨出结果，**绝不**进入直购循环。

        为什么要有这个分支：候补轨（``StandbyEngine._monitor_loop``）本来就会
        自己在后台提交候补单、轮询兑现；本引擎在这个策略下唯一要做的事就是
        **别去下直购单**，并把候补轨的终态同步到自己的状态上，
        这样「任务状态」这一个出口就能完整反映结果，界面不必分两条轨来看。
        """
        engine = self.standby_engine
        if engine is None:
            # 配置成仅候补却没有候补轨，等于这个任务什么都不会做。
            # 必须报错而不是静默空转 —— 静默空转会被当成「程序卡住了」。
            self.error = "策略为仅候补，但候补引擎未就绪（请检查配置）"
            self._set_state(STATE_ERROR, self.error)
            self._notify_error("策略为「仅候补」，但候补引擎未就绪，任务未执行")
            return

        self._set_state(STATE_GRABBING, "候补监控中（仅候补，不下直购单）")
        log.info("任务[%s]策略=standby：只跑候补轨，不进入直购捡漏循环", self.task.name)

        while not self._stop_event.is_set():
            if getattr(engine, "status", "") in STANDBY_TERMINAL_STATES and not engine.running:
                break
            self._sleep(0.5)

        if self._stop_event.is_set():
            engine.stop(reason="任务被停止")
            self._set_state(STATE_STOPPED, "已停止")
            return

        status = getattr(engine, "status", "")
        detail = getattr(engine, "error", "") or ""
        if status == "success":
            self._set_state(STATE_SUCCESS, "候补已兑现，请尽快完成支付")
        elif status == "failed":
            self.error = detail or "候补未成功"
            self._set_state(STATE_ERROR, self.error)
        else:
            self._set_state(STATE_STOPPED, detail or f"候补轨结束（{status}）")

    def _handle_error_streak(self, streak: int) -> None:
        """连续失败时指数退避并告警（LG-03）。"""
        if streak in (5, 10, 20):
            self._notify_error(f"连续 {streak} 次查询/下单失败，最近错误：{self.stats.last_error}")
        self._sleep(min(30.0, 2.0 * streak))

    def _current_interval(self) -> float:
        base = max(0.3, float(self.task.query_interval))
        if self.rate_limiter is not None:
            return max(base, self.rate_limiter.interval)
        return base

    # ------------------------------------------------------------------ #
    def _tick(self) -> None:
        """一轮：查询 → 选候选 → 下单。"""
        assert self.query is not None and self.user is not None

        if self._pause_until and time.time() < self._pause_until:
            self._sleep(min(2.0, self._pause_until - time.time()))
            return

        # 计数放在请求**之前**：查询抛异常（风控 / 日期不可查 / 会话失效）时它同样是
        # 一次已经发出去的查询。放在之后的话，被拒的任务会一直显示「累计查询 0」，
        # 与真相相反 —— 使用者看到「0 次查询」就会以为是没在跑。
        self.stats.queries += 1
        result = self.query.query_once()
        self.stats.last_query_at = time.time()

        if result.errors:
            self.stats.last_error = result.errors[-1]

        if not result.tickets:
            problem = result.errors[-1] if result.errors else ""
            self._log_round(
                f"empty|{problem}",
                f"任务[{self.task.name}]第{self.stats.rounds}轮: 这一轮没有可用车次"
                + (f"；{problem}" if problem else "（官方返回 0 条）"),
            )
            if self.rate_limiter is not None:
                self.rate_limiter.report_ok()
            return

        candidates = self.query.build_candidates(result.tickets, seat_scope="available")
        self._last_candidates = candidates
        self.stats.candidates_seen += len(candidates)
        metrics.incr("grab.candidates", len(candidates))

        total_available = sum(
            1 for t in result.tickets for s in t.seats.values() if s.available
        )
        if candidates:
            self._log_round(
                "hit",
                f"任务[{self.task.name}]第{self.stats.rounds}轮: 命中 {len(candidates)} 个可购席位"
                f"（{len({c.ticket.train_code for c in candidates})} 个车次）",
            )
        else:
            # 这一条以前是 DEBUG —— 默认级别下**完全不出现**。
            # 后果是「有票但一个候选都没有」在日志里连一行都没有，
            # 排查时连"到底查到几趟车、卡在哪一步"都看不见（本项目真出过）。
            self._log_round(
                f"nofit|{len(result.tickets)}|{total_available}|{','.join(self.task.seat_types)}",
                f"任务[{self.task.name}]第{self.stats.rounds}轮: 查到 {len(result.tickets)} 个车次，"
                f"其中 {total_available} 个席别有票，但都不在席别优先级 "
                f"[{'/'.join(self.task.seat_types)}] 内（或车次未到起售时间）",
            )

        candidate = self.strategy.next_candidate(candidates, exclude=self._active_failed_keys())
        if candidate is None:
            if self.rate_limiter is not None:
                self.rate_limiter.report_ok()
            return

        self._try_order(candidate)

    def _try_order(self, candidate: Candidate) -> None:
        """对候选执行下单（含乘客准备与失败重试策略）。"""
        assert self.user is not None
        key = self.strategy.key_of(candidate)

        passengers = self._resolve_passengers()
        if not passengers:
            self.stats.last_error = "未获取到有效乘车人"
            self._pause_until = time.time() + 30
            self._notify_error("任务无人可下单：未获取到乘车人信息，请检查任务的乘客配置")
            return

        self.stats.order_attempts += 1
        result = self.orders.submit(
            self.user,
            candidate.ticket,
            candidate.seat,
            passengers,
            task_id=self.task.id,
            task_name=self.task.name,
            max_retry=max(1, min(3, self.task.max_retry)),
        )

        if result.ok:
            self.result = result
            self._set_state(STATE_SUCCESS, "出票成功")
            self._on_success(result, candidate)
            return

        # 无论哪种失败，都让该候选冷却一段时间 —— 在一张刚刚失败的票上立刻重试
        # 基本是浪费请求（无票/被抢完/风控都不会瞬间恢复），冷却期后它仍会重新参与。
        with self._lock:
            self._failed_keys[key] = time.time()
        self.stats.order_failures += 1

        if result.retryable:
            log.warning(
                "任务[%s]下单未成功(%s)，本轮换候选: %s",
                self.task.name,
                candidate.ticket.train_code,
                result.message,
            )
            if "风控" in result.message or "频繁" in result.message:
                # 下单阶段也会撞上风控（锁票/排队接口同样受限制）。
                # 这里必须同样计数并降频，否则会出现「查询降频了、下单还在猛冲」的漏洞。
                self.stats.risk_events += 1
                self.stats.last_error = result.message
                if self.rate_limiter is not None:
                    self.rate_limiter.report_risk(kind="order_risk")
                self._notify_risk(f"下单阶段触发风控：{result.message}，已自动降低频率并暂停 15 秒")
                self._pause_until = time.time() + 15
        else:
            log.info("任务[%s]下单终止: %s", self.task.name, result.message)

    def _log_round(self, key: str, message: str) -> None:
        """按「结论」去重地记录每轮查询的结果，并带上重复轮数。

        为什么不能每轮都打：``query_interval`` 默认 1 秒，每轮一行会把日志刷成噪声，
        真正的问题反而被淹掉。

        为什么也不能像以前那样用 ``log.debug``：默认日志级别是 INFO，
        于是「任务在盯票、但一个候选都没有」这件事在日志里**一句话都没有** ——
        使用者只能看到「正在盯票」，完全不知道为什么没买到（本项目真出过，
        而且排查时连"这一轮到底查到几趟车"都看不到）。
        """
        with self._lock:
            repeats = getattr(self, "_round_log_repeats", 0)
            if key == getattr(self, "_round_log_key", ""):
                self._round_log_repeats = repeats + 1
                return
            self._round_log_key = key
            self._round_log_repeats = 0
        note = f"（上一条同样的结论重复了 {repeats} 轮）" if repeats else ""
        log.info("%s%s", message, note)

    def _active_failed_keys(self) -> set[str]:
        """当前仍处于冷却期内的候选 key（冷却过期的会被清理）。"""
        now = time.time()
        with self._lock:
            expired = [k for k, ts in self._failed_keys.items() if now - ts >= FAILED_CANDIDATE_COOLDOWN]
            for key in expired:
                self._failed_keys.pop(key, None)
            return set(self._failed_keys)

    def _resolve_passengers(self) -> list[dict[str, Any]]:
        """把任务里的乘客姓名映射为 12306 乘车人对象（GR-05）。"""
        assert self.user is not None
        with self._lock:
            cached = getattr(self, "_passenger_cache", None)
            if cached is not None:
                return cached

        wanted = [p.strip() for p in self.task.passengers if p.strip()]
        try:
            all_passengers = self.user.get_passengers()
        except (InterError, VerifyCodeError, LoginError) as exc:
            log.warning("任务[%s]获取乘车人失败: %s", self.task.name, exc)
            return []

        if not all_passengers:
            return []

        selected: list[dict[str, Any]] = []
        if wanted:
            by_name = {str(p.get("passenger_name", "")).strip(): p for p in all_passengers}
            for name in wanted:
                if name in by_name:
                    selected.append(by_name[name])
                else:
                    log.warning("任务[%s]乘车人 %s 不在账号 %s 的乘车人列表中", self.task.name, name, self.user.username)
            if not selected:
                # 名字对不上时退化为「用第一个可用乘车人」，避免任务彻底空转
                log.warning("任务[%s]配置的乘客都未匹配到，改为使用账号下第一个乘车人", self.task.name)
                selected = all_passengers[:1]
        else:
            selected = all_passengers[:1]

        with self._lock:
            self._passenger_cache = selected
        return selected

    # ------------------------------------------------------------------ #
    # 准点开售
    # ------------------------------------------------------------------ #
    def _wait_for_sale_open(self) -> bool:
        """等待开售时刻（GR-01 / GR-T2）。"""
        target = self._parse_sale_time()
        if target is None:
            log.warning("任务[%s]开售时间解析失败(%s)，直接进入捡漏模式", self.task.name, self.task.sale_open_time)
            return True

        prepare = max(0, int(self.task.sale_prepare_seconds))
        now = datetime.now()
        if target <= now:
            log.info("任务[%s]开售时间已过（%s），直接进入捡漏模式", self.task.name, self.task.sale_open_time)
            return True

        wait_total = (target - now).total_seconds()
        wait_until_prepare = max(0.0, wait_total - prepare)
        log.info(
            "任务[%s]进入准点待命：开售 %s，还有 %.0f 秒（提前 %d 秒进入秒杀）",
            self.task.name,
            target.strftime("%Y-%m-%d %H:%M:%S"),
            wait_total,
            prepare,
        )
        self._notify_info(f"已进入准点待命，开售时间 {target.strftime('%m-%d %H:%M:%S')}")

        # 长等待切成短片段，便于响应停止指令（挂机体验）
        while wait_until_prepare > 0 and not self._stop_event.is_set():
            chunk = min(5.0, wait_until_prepare)
            self._sleep(chunk)
            wait_until_prepare -= chunk

        if self._stop_event.is_set():
            self._set_state(STATE_STOPPED, "待命期间被停止")
            return False

        # 最后阶段：精确到毫秒的等待，确保「到点即下首单」
        self._set_state(STATE_READY, "开售倒计时中")
        while not self._stop_event.is_set():
            remaining = (target - datetime.now()).total_seconds()
            if remaining <= 0:
                break
            if remaining > 1.0:
                self._sleep(min(0.5, remaining - 0.9))
            else:
                # 最后 1 秒密集轮询，误差控制在毫秒级
                time.sleep(min(0.02, max(0.001, remaining / 4)))

        log.info("任务[%s]开售时刻已到，立即发起首次查询与下单", self.task.name)
        return not self._stop_event.is_set()

    def _parse_sale_time(self) -> datetime | None:
        raw = (self.task.sale_open_time or "").strip()
        if not raw:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None

    # ------------------------------------------------------------------ #
    def _on_success(self, result: OrderResult, candidate: Candidate) -> None:
        """出票成功后的收尾：通知 + 停止 + 期待支付。"""
        log.info(
            "🎫 任务[%s]抢票成功: %s %s %s 订单号=%s",
            self.task.name,
            result.train_code,
            result.depart_date,
            result.seat_type,
            result.order_no,
        )
        if self.notifier is not None:
            self.notifier.notify_success(
                self.task.name,
                {
                    "train_no": result.train_code,
                    "depart_date": result.depart_date,
                    "from_station": result.from_station,
                    "to_station": result.to_station,
                    "seat_type": result.seat_type,
                    "passenger": result.passenger,
                    "order_no": result.order_no,
                },
            )
        self._stop_event.set()
        if self.standby_engine is not None:
            self.standby_engine.stop(reason="捡漏已成功出票")

    def _notify_info(self, detail: str) -> None:
        if self.notifier is not None:
            self.notifier.notify_info(self.task.name, detail)

    def _notify_error(self, detail: str) -> None:
        if self.notifier is not None:
            self.notifier.notify_error(self.task.name, detail)

    def _notify_risk(self, detail: str) -> None:
        if self.notifier is not None:
            self.notifier.notify_risk(self.task.name, detail)

    def _notify_date_problem(self, detail: str) -> None:
        """「日期不可查」只通知一次。

        这是一个**状态**（今天查不到，明天可能就开售了），不是一个**事件**。
        每次查询都推一条通知的话，界面上会堆满同一条消息，
        真正的错误（比如账号掉线）反而被淹掉。
        消息内容变化时会重新通知一次 —— 例如从「还没开售」变成别的日子。
        """
        if not detail:
            return
        with self._lock:
            if getattr(self, "_last_date_notice", "") == detail:
                return
            self._last_date_notice = detail  # type: ignore[attr-defined]
        self._notify_error(detail)

    # ------------------------------------------------------------------ #
    def _sleep(self, seconds: float) -> None:
        """可中断睡眠：停止指令最长 0.2s 内生效。

        用 ``Event.wait`` 而非 ``time.sleep``，保证「停止任务」的响应是即时的，
        不会因为一个长睡眠让 UI 卡住或重启慢半拍。
        """
        remaining = float(seconds)
        while remaining > 0:
            if self._stop_event.is_set():
                return
            chunk = 0.2 if remaining > 0.2 else remaining
            self._stop_event.wait(chunk)
            remaining -= chunk

    def _set_state(self, state: str, message: str = "") -> None:
        with self._lock:
            if self.state == state and not message:
                return
            previous, self.state = self.state, state
        log.debug("任务[%s]状态 %s → %s (%s)", self.task.name, previous, state, message)
        if self.on_state_change is not None:
            try:
                self.on_state_change(self.task.name, state, self.status())
            except Exception as exc:  # noqa: BLE001
                log.error("状态变更回调失败: %s", exc)

    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        return {
            "task_id": self.task.id,
            "task_name": self.task.name,
            "state": self.state,
            "state_text": STATE_TEXT.get(self.state, self.state),
            "running": self.running,
            "error": self.error,
            "account": self.user.username if self.user else "",
            "strategy": self.task.strategy,
            "interval": round(self._current_interval(), 2),
            "stats": self.stats.to_dict(),
            "result": self.result.to_dict() if self.result else None,
        }

    def latest_candidates(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self._last_candidates]

    def apply_config(self, task: TaskConfig) -> None:
        """热更新任务配置（CF-04）。"""
        self.task = task
        self.strategy.apply_config(task)
        if self.query is not None:
            self.query.apply_config(task)
        with self._lock:
            self._passenger_cache = None  # type: ignore[attr-defined]
