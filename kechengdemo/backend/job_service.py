# -*- coding: utf-8 -*-
"""「一次批量任务」的公共骨架：进程内线程 + 跨进程 DB 行 + 心跳僵尸判定。

## 为什么要有它

`ScoringService`（评分）与 `SyncService`（同步）各自把同样这件事写了一遍：

  · `__init__`：锁 / 线程 / 停止标志 / 进度字典
  · `is_running()`：本进程线程 → 查 DB 里的 running 行 → 心跳僵尸 → 绝对超时僵尸
  · `_bump()`：锁内累加进度计数，锁外写库（`sync_service._bump` 的进度累加
    与 `scoring_service._bump` 是同一段代码）

两份实现**已经开始漂移**（sync 版在锁内读线程状态、scoring 版没加锁），
而这段逻辑决定的是「会不会有两个批量同时在跑」—— 一旦分歧，评分就会读到
正在写入的 `chat_message`（半截会话），结论不可复现。同一不变量只该有一处实现。

## 僵尸判定的两级（两者都必须有）

① **心跳**：长任务每次进度更新（`_bump`）刷新 `heartbeat_at`；心跳停超过
   `job_heartbeat_stale_minutes`（默认 15 分钟）→ 进程已死 → 标 failed 并放行。
   为什么必须有：进程被 kill 时来不及改状态，记录会**永久停在 running**。
② **绝对超时**：仍超过 `scoring_stale_minutes`（默认 240 分钟）的一律视为僵尸
   （兼容没有心跳的老行）。

DB 不可用时**宁可放行也不误拦**：拦错了会把系统锁死，放行了最多是多跑一轮。
"""
import threading

import db

#: **已知的跑批任务 → 运行记录表**。这里是**唯一登记处**：新增一个跑批任务时
#: 在表里加一行即可，其余模块不需要认识彼此。
#:
#: 为什么要有这张表（而不是让服务互相 `is_running()`）：改造前
#: `scoring_service.start()` 要 `import sync_service` 判互斥、
#: `sync_service.start()` 又要 `import scoring_service` —— 两个模块**互相 import**
#: （靠函数内延迟 import 才没炸），而「互斥」这个不变量本该由第三方中立处判定。
#: 委托方与被委托方互相认识，就是典型的循环依赖。
JOB_TABLES = {"scoring": "scoring_run", "sync": "sync_job"}

#: 绝对超时兜底（分钟）。必须**大于最长批量耗时**（170 个长会话 × ~58 秒 ≈ 2.7 小时）。
STALE_MINUTES = 240

#: 心跳判死阈值（分钟）。必须**大于单会话的最坏耗时**（3 次重试 × 120s 超时 ≈ 6 分钟），
#: 否则会把活着的任务误判为死 → 两个批量并行 → 反而破坏了互斥。取 15 分钟留足余量。
HEARTBEAT_STALE_MINUTES = 15


class JobService:
    """批量任务服务的公共骨架。子类指定 `TABLE`（运行记录表）即可。"""

    #: 记录运行状态的表（`scoring_run` / `sync_job`）。子类必须给。
    TABLE = ""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = False
        self._progress = {}

    # ---------------- 运行状态 ----------------

    def is_running(self) -> bool:
        """是否有任务在跑。**含跨进程检测 + 心跳僵尸回收**（见模块文档）。"""
        if not self.TABLE:
            raise NotImplementedError("子类必须指定 TABLE")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
        return table_running(self.TABLE)

    # ---------------- 进度 ----------------

    def _accumulate(self, deltas: dict = None, detail: dict = None) -> dict:
        """在锁内累加进度计数（只累加**非零**增量）并返回快照。

        `detail` 是「直接覆盖」的字段（如 `stage` / `current_session`），
        与累加的计数分开 —— 两者语义不同，混在一起就会出现「stage 变成数字」。
        """
        with self._lock:
            for k, v in (deltas or {}).items():
                if v:
                    self._progress[k] = self._progress.get(k, 0) + v
            if detail:
                self._progress.update(detail)
            return dict(self._progress)

    def _spawn(self, target, args=(), name: str = "") -> None:
        """统一的线程启动入口（daemon：进程退出时不该被它拖住）。"""
        self._stop = False
        self._thread = threading.Thread(target=target, args=args, daemon=True,
                                        name=name or f"{self.TABLE or 'job'}")
        self._thread.start()


# ---------------- 跨进程运行状态（无需 import 任何服务） ----------------

def mark_zombie(table: str, row_id: int, reason: str) -> None:
    """把僵尸行标成 failed（只在仍是 running 时改）。"""
    db.execute(
        f"""UPDATE {table} SET status='failed', finished_at=now(),
                  error=COALESCE(error, %s) WHERE id=%s AND status='running'""",
        (reason, row_id))


def table_running(table: str) -> bool:
    """该表是否有 `status='running'` 的行。**含两级僵尸回收**：

    ① 心跳：`heartbeat_at` 停超过 `job_heartbeat_stale_minutes`（默认 15 分钟）
       → 进程已死 → 标 failed 并放行（进程被 kill 时来不及改状态）；
    ② 绝对超时：`started_at` 超过 `scoring_stale_minutes`（默认 240 分钟）的一律视为
       僵尸（兼容没有心跳的老行）。

    DB 不可用时**宁可放行也不误拦**：拦错了会把系统锁死，放行了最多多跑一轮。
    """
    try:
        row = db.query_one(
            f"""SELECT id,
                       EXTRACT(EPOCH FROM (now() - started_at))   AS el,
                       EXTRACT(EPOCH FROM (now() - heartbeat_at)) AS hb
                  FROM {table} WHERE status='running' ORDER BY id DESC LIMIT 1""")
    except Exception:                                  # noqa: BLE001
        return False
    if not row:
        return False
    stale = db.setting_int("scoring_stale_minutes", "SCORING_STALE_MINUTES",
                           STALE_MINUTES) * 60
    hb_stale = db.setting_int("job_heartbeat_stale_minutes",
                              "JOB_HEARTBEAT_STALE_MINUTES",
                              HEARTBEAT_STALE_MINUTES) * 60
    hb = row.get("hb")
    if hb is not None and float(hb) > hb_stale:
        mark_zombie(table, row["id"],
                    f"进程异常退出：心跳已停 {int(float(hb) // 60)} 分钟"
                    f"（阈值 {hb_stale // 60} 分钟），自动标记为失败")
        return False
    if row.get("el") is not None and float(row["el"]) > stale:
        mark_zombie(table, row["id"],
                    f"进程异常退出：超过 {stale // 60} 分钟仍为 running，自动标记为失败")
        return False
    return True


def running_kinds(*, exclude: str = "") -> list:
    """哪些跑批任务正在运行（**查库，跨进程可见**），按 `JOB_TABLES` 的顺序返回。

    「同步与评分不得同时跑」这个不变量只在这里实现一次：
    改造前它散在 `scoring_service.start()`（函数内 `import sync_service`）、
    `sync_service.start()`（函数内 `import scoring_service`）与
    `scheduler.guard_can_start()` 三处，口径还不完全一致。
    """
    return [k for k, table in JOB_TABLES.items()
            if k != exclude and table_running(table)]
