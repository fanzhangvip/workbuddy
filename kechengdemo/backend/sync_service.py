# -*- coding: utf-8 -*-
"""同步服务：把「微伴元数据 + 企微明文正文」拉下来落进 PostgreSQL。

链路（全部纯 HTTP）
    ① 微伴 GET  /open-api/staff/list                      → 员工
    ② 微伴 GET  /open-api/chat/session/list               → 会话
    ③ 微伴 GET  /open-api/chat/message/list/v2            → 消息元数据（正文为 null）
    ④ 企微 POST /chatdata/getchatdata                     → 该会话消息的明文正文
    ⑤ 按 msgid(=original_id) 合并 upsert 进 chat_message

增量与断点续传
    ``sync_cursor`` 记录每个会话已同步到的 ``last_msg_time``：
      - 会话的 ``last_msg_time`` 不高于水位 → 直接跳过（一次请求都不发）
      - 拉消息时带 ``start_time`` 只取水位之后的部分
"""
import time
import traceback

import db
import job_service            # noqa: E402  批量任务骨架（与 ScoringService 共用）

import weiban                # noqa: E402  根目录既有模块
import ww_chatdata           # noqa: E402

PAGE = 100                   # 微伴接口单页条数
SESSION_TYPES = ("ext_user", "room", "other_staff")

#: 每个员工最多同步多少个会话（**硬上限，不可放开**）。
#: 为什么必须封顶：这个数字直接决定外部调用量（每个会话还要再拉消息 + 调企微解密）。
#: 不封顶时，一次「全量 + 不选员工」就是 308 个员工 × 上千会话的全量重拉 ——
#: 文档自己写着单员工上千会话的全量要 1 小时量级，乘上员工数就是失控。
#: 语义（**与旧文档不同，旧文档写的 `0 = 不限` 是错的**）：
#:   · 缺省 / 传 0 / 传负数 → 用 `DEFAULT_MAX_SESSIONS_PER_STAFF`
#:   · 超过 `MAX_SESSIONS_PER_STAFF_CAP` → 收敛到该上限
DEFAULT_MAX_SESSIONS_PER_STAFF = 200
MAX_SESSIONS_PER_STAFF_CAP = 200

#: 会话列表**翻页条数上限**（每员工每类型）。撞到它就停，以前是**静默截断**
#: （实测 top3 员工恰好停在 20050 条左右 —— 更旧的会话永远看不到，也不报警）。
#: 现在它可配（`sync_session_page_cap`），撞到时写进任务 detail 并打日志。
SESSION_PAGE_CAP_DEFAULT = 20000

#: 「会话列表已扫到哪」的**安全余量**（秒）。用员工已知的最新时间当水位提前收工，
#: 等于假设"列表按最近活跃降序 ⇒ 整页都更旧 ⇒ 后面更旧"。这条假设对**从未入库**的
#: 老会话有风险（它排在后面、时间又旧，会被一并跳过），所以水位先往回让 7 天 ——
#: 代价是极少数情况下多翻一页，收益是不要把"沉默的老会话"永久漏掉。
SCAN_STOP_MARGIN_SECONDS = 7 * 86400


def resolve_max_sessions_per_staff(value) -> tuple:
    """把调用方给的"每员工最多同步多少会话"收敛成生效值。返回 ``(生效值, 是否被收敛)``。

    **这是唯一口径**（API / 调度器 / 脚本三条路都走它），因而也是唯一需要单测的地方
    —— 不必启动批量就能验（`tests/test_sync_window.py`）。

    规则（**旧文档写的 `0 = 不限` 是错的**，已按此修正）：
      · 缺省 / None / 空串 / 0 / 负数 → `DEFAULT_MAX_SESSIONS_PER_STAFF`
      · 超过 `MAX_SESSIONS_PER_STAFF_CAP` → 收敛到该上限
      · 中间的合法值 → 原样使用
    """
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return DEFAULT_MAX_SESSIONS_PER_STAFF, True        # 缺省也算"被收敛"（调用方不知道上限）
    if n > MAX_SESSIONS_PER_STAFF_CAP:
        return MAX_SESSIONS_PER_STAFF_CAP, True
    return n, False


def sync_window(watermark: int, round_start: int, overlap_seconds: int) -> tuple:
    """需求 4.3 的取数窗口 → `(start_time, end_time)`。

    ```
    start_time = 上次成功水位 - 15 分钟
    end_time   = 本轮任务开始时间 - 1 分钟
    ```

    **两条的原因都不一样，别只改一条**：

    · `start_time` 往回重叠：微伴的消息可能**乱序/迟到**入库（或上一次同步正好在
      某条消息落库前失败）—— 不重叠就会把它永久跳过（水位已经越过它了）。
      重叠窗口内重复取到的消息由 `PRIMARY KEY(staff_id, session_id, message_id)`
      的 upsert 幂等吸收，代价只是少量重复请求。
    · `end_time`：本轮**固定上界**。接口是「按消息时间逆序 + offset 分页」，
      不固定上界时翻页途中新到的消息会把整页往后挤 → 边界那条被跳过。

    水位为 0（首次同步）→ `start_time` 返回 None（不限，取全量）。
    """
    start = max(0, int(watermark) - int(overlap_seconds)) if watermark else None
    return start, int(round_start) - 60


# ---------------------------------------------------------------- SQL

SQL_UPSERT_STAFF = """
INSERT INTO staff (staff_id, name, alias, avatar, mobile, position, status,
                   ia_authorized, ma_authorized, oa_authorized, raw,
                   created_at, updated_at, synced_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
ON CONFLICT (staff_id) DO UPDATE SET
  name = EXCLUDED.name, alias = EXCLUDED.alias, avatar = EXCLUDED.avatar,
  mobile = EXCLUDED.mobile, position = EXCLUDED.position, status = EXCLUDED.status,
  ia_authorized = EXCLUDED.ia_authorized, ma_authorized = EXCLUDED.ma_authorized,
  oa_authorized = EXCLUDED.oa_authorized, raw = EXCLUDED.raw,
  updated_at = EXCLUDED.updated_at, synced_at = now()
"""

SQL_UPSERT_SESSION = """
INSERT INTO chat_session (staff_id, session_id, session_type, name, remark, avatar,
                          target_id, agree_msgaudit, last_msg_time, last_msg_thumb,
                          created_at, synced_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
ON CONFLICT (staff_id, session_id) DO UPDATE SET
  session_type = EXCLUDED.session_type, name = EXCLUDED.name, remark = EXCLUDED.remark,
  avatar = EXCLUDED.avatar, target_id = EXCLUDED.target_id,
  agree_msgaudit = EXCLUDED.agree_msgaudit,
  last_msg_time = GREATEST(chat_session.last_msg_time, EXCLUDED.last_msg_time),
  last_msg_thumb = EXCLUDED.last_msg_thumb,
  created_at = COALESCE(chat_session.created_at, EXCLUDED.created_at),
  synced_at = now()
"""

SQL_UPSERT_MESSAGE = """
INSERT INTO chat_message (staff_id, session_id, message_id, msgid, direction, msg_time,
                          msg_type, from_id, to_id, from_name, from_avatar, room_id,
                          file_url, secret_key, content, msgtype, plain, content_state,
                          created_at, updated_at, fetched_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
ON CONFLICT (staff_id, session_id, message_id) DO UPDATE SET
  content = COALESCE(NULLIF(EXCLUDED.content, ''), chat_message.content),
  msgtype = COALESCE(EXCLUDED.msgtype, chat_message.msgtype),
  plain   = COALESCE(EXCLUDED.plain, chat_message.plain),
  secret_key = COALESCE(EXCLUDED.secret_key, chat_message.secret_key),
  content_state = CASE
      WHEN COALESCE(NULLIF(EXCLUDED.content, ''), chat_message.content) IS NOT NULL
      THEN 'ok' ELSE EXCLUDED.content_state END,
  fetched_at = now()
RETURNING (xmax = 0) AS inserted
"""


# ---------------------------------------------------------------- 服务

class SyncService(job_service.JobService):
    """同步批量任务。

    运行状态（本进程线程 / 跨进程 DB 行 / 心跳僵尸 / 绝对超时）与进度累加
    都在基类 `job_service.JobService` —— 与 `ScoringService` **共用一份实现**。
    改造前两边各写一遍，且已经漂移（本服务在锁内读线程状态，评分那边没加锁）；
    而这段逻辑决定「会不会两个批量同时跑」—— 评分读到半截会话就不可复现了。
    """

    TABLE = "sync_job"

    def __init__(self):
        super().__init__()
        self._job_id = None

    # ---------------- 对外接口 ----------------

    # `is_running()` 继承自 `job_service.JobService`（跨进程 + 僵尸两级判定）

    def status(self) -> dict:
        with self._lock:
            job_id = self._job_id
        running = self.is_running()      # 跨进程可见（后端重启后也能看到别的进程在跑）
        job = db.query_one("SELECT * FROM sync_job WHERE id = %s", (job_id,)) if job_id else None
        if not job:
            job = db.query_one("SELECT * FROM sync_job ORDER BY id DESC LIMIT 1")
        return {"running": running, "stopping": self._stop, "job": job,
                "progress": dict(self._progress)}

    def stop(self) -> dict:
        """发出停止信号。**含跨进程**：同时写 `sync_job.stop_requested`。

        改造前这里**只设进程内标志** `self._stop` —— 而同步绝大多数时候跑在
        调度器或另一个进程里（`triggered_by=schedule`），那个进程读不到这个标志，
        于是 UI 点「停止」会得到「已发出停止信号」但**什么都不会停**；而同步在跑时
        评分被互斥闸挡住（用户侧表现就是「同步任务进行中，无法评分」）。
        `scoring_service.stop()` 的注释恰好警告过这个坑（「只设进程内标志的话，
        后端点『停止』停不掉脚本/调度器里的批量」），这里补齐成同一套做法。
        """
        if not self.is_running():
            return {"ok": False, "reason": "当前没有正在运行的同步任务"}
        self._stop = True
        with self._lock:
            jid = self._job_id
        if jid:
            db.execute("UPDATE sync_job SET stop_requested=true WHERE id=%s", (jid,))
        else:
            db.execute("""UPDATE sync_job SET stop_requested=true
                           WHERE id = (SELECT id FROM sync_job WHERE status='running'
                                        ORDER BY id DESC LIMIT 1)""")
        return {"ok": True, "message": "已发出停止信号，将在当前会话处理完后退出"}

    def start(self, kind: str = "incremental", staff_ids: list = None,
              session_types: list = None, max_sessions_per_staff: int = 200,
              max_msgs_per_session: int = 0, triggered_by: str = "manual",
              ignore_cursor: bool = False, lookback_hours: int = None) -> dict:
        """启动一次同步。

        :param lookback_hours: **回看窗口**（小时）。给了它就**不看水位**，
        改为「最近 N 小时内的消息全部重拉一遍」—— 这是需求 4.2 的
        「48 小时补偿同步」要解决的问题：

            水位是按 `last_msg_time` 往前推的，而微伴的消息有时**迟到入库**
            （接口延迟、网络失败、任务中断）→ 到的时候 `msg_time` 已经**小于水位**，
            增量同步永远不会再取它 → 这条消息**永久丢失**。

        重拉是**幂等**的：`_persist` 对 `chat_message` 是 upsert，
        已存在的行不会重复插入；水位也只前进不后退（`GREATEST`）。
        """
        if self.is_running():
            return {"ok": False, "reason": "已有同步任务在运行，请先停止或等待完成"}
        # 互斥（修 H1）：评分正在跑时不得启动同步 —— 否则评分读到的是「半截会话」
        # 互斥（修 H1）：评分正在跑时不得启动同步（同一不变量、同一判定入口）。
        # 见 scoring_service.start() 的说明：判定查库，服务之间不再互相 import。
        if "scoring" in job_service.running_kinds(exclude="sync"):
            return {"ok": False, "reason": "评分任务进行中，请先等待评分完成再同步（避免评分读到半截数据）"}

        types = [t for t in (session_types or ["ext_user"]) if t in SESSION_TYPES]
        if not types:
            types = ["ext_user"]
        # 每员工会话数**封顶**（规则与理由见 `resolve_max_sessions_per_staff`）。
        # 收敛放在服务层这个唯一入口，API / 调度器 / 脚本三条路都绕不过：
        # 改造前 API 层用 `or 200` 隐式吃掉 0，而文档写的是"0 = 不限" —— 三者互相矛盾。
        max_sessions_per_staff, capped = resolve_max_sessions_per_staff(max_sessions_per_staff)

        detail = db.as_json({"staff_ids": staff_ids, "session_types": types,
                             "max_sessions_per_staff": max_sessions_per_staff,
                             "sessions_capped": capped,
                             "lookback_hours": lookback_hours})
        job_id = db.query_one(
            """INSERT INTO sync_job (kind, status, triggered_by, detail)
               VALUES (%s, 'running', %s, %s) RETURNING id""",
            (kind, triggered_by, detail)
        )["id"]

        self._stop = False
        self._job_id = job_id
        self._progress = {"stage": "启动中", "current_staff": None, "current_session": None,
                          "session_total": 0, "session_done": 0, "msg_total": 0,
                          "msg_decrypted": 0, "msg_new": 0,
                          "staff_total": 0, "staff_done": 0}
        self._spawn(self._run,
                    (job_id, kind, staff_ids, types, max_sessions_per_staff,
                     max_msgs_per_session, ignore_cursor, lookback_hours),
                    name=f"sync-{job_id}")
        # 把**生效值**回给调用方（修"文档说 0=不限、实现却是 200"那处矛盾）：
        # 界面据此提示"已按上限 200 收敛"，而不是让人以为"不限"生效了。
        return {"ok": True, "job_id": job_id,
                "max_sessions_per_staff": max_sessions_per_staff,
                "sessions_capped": capped}

    # ---------------- 内部实现 ----------------

    def _patch_job(self, job_id, **fields):
        if not fields:
            return
        # 每次进度更新顺带刷新心跳：心跳停了 = 进程没了（见 schema.sql 的说明）。
        # ⚠️ 心跳用字面量 `now()` 内联，其余字段一律参数化 —— 不用「值等于某个魔法字符串」
        # 当哨兵（真实取值可能与哨兵撞车，那会把用户数据拼进 SQL）。
        keys = ", ".join(f"{k} = %s" for k in fields) + ", heartbeat_at = now()"
        db.execute(f"UPDATE sync_job SET {keys} WHERE id = %s",
                   (*fields.values(), job_id))

    def _should_stop(self, job_id) -> bool:
        """是否该停：**进程内标志 + 跨进程 DB 标志**（与评分同一套语义）。

        每轮查一次库，和 `scoring_service._run` 一样（那边每轮取
        `stop_requested`，顺带取护栏字段）。命中后把进程内标志也置上，
        后续轮次不再查库；**查库失败按「不停」处理**（宁可多跑一轮，
        也不要因为一次查询抖动把用户的同步掐掉）。
        """
        if self._stop:
            return True
        try:
            row = db.query_one("SELECT stop_requested FROM sync_job WHERE id=%s", (job_id,))
        except Exception:                                  # noqa: BLE001
            return False
        if row and row.get("stop_requested"):
            self._stop = True
            return True
        return False

    def _finish_job(self, job_id, status: str, error: str = None):
        db.execute(
            "UPDATE sync_job SET status = %s, error = %s, finished_at = now() WHERE id = %s",
            (status, error, job_id))

    def _bump(self, job_id, *, sessions=None, msgs=None, decrypted=None, new=None,
              detail=None):
        # 累加部分与 ScoringService 共用基类实现；这里只保留「本表有哪些列」。
        snapshot = self._accumulate({"session_done": sessions, "msg_total": msgs,
                                     "msg_decrypted": decrypted, "msg_new": new},
                                    detail)
        self._patch_job(
            job_id,
            session_done=snapshot.get("session_done", 0),
            msg_total=snapshot.get("msg_total", 0),
            msg_decrypted=snapshot.get("msg_decrypted", 0),
            msg_new=snapshot.get("msg_new", 0),
            detail=db.as_json(snapshot),
        )
        return snapshot

    # ---- 员工 ----
    def _sync_staff(self, job_id) -> dict:
        n = 0
        rows = []
        for st in weiban.iter_all_staff():
            n += 1
            rows.append((
                st.get("id"), st.get("name"), st.get("alias"), st.get("avatar"),
                st.get("mobile"), st.get("position"), st.get("status"),
                st.get("ia_authorized"), st.get("ma_authorized"), st.get("oa_authorized"),
                db.as_json(st), st.get("created_at"), st.get("updated_at"),
            ))
            if len(rows) >= 100:
                db.execute_many(SQL_UPSERT_STAFF, rows)
                rows.clear()
        if rows:
            db.execute_many(SQL_UPSERT_STAFF, rows)
        self._patch_job(job_id, staff_count=n)
        return {"count": n}

    def _staff_id_list(self) -> list:
        return [r["staff_id"] for r in
                db.query("SELECT staff_id FROM staff ORDER BY name NULLS LAST")]

    # ---- 会话 ----
    def _staff_scan_watermark(self, staff_id: str, session_types: list) -> int:
        """该员工**已知会话里最新的 `last_msg_time`** —— 当作"会话列表已扫到哪"的水位。

        ## 为什么需要它（2026-10-16 实测）

        `stop_before` 原来只有一个来源：`sync_cursor`（**会话级**水位）。而实测
        **308 个员工里只有 11 个有游标** —— 其余 297 个 `stop_before=0`，
        于是每次同步都要把他们的会话列表**一路翻到 20000 条上限**
        （单员工 20050 条会话 = 200 页请求；实测一次增量同步卡在第一遍 20 分钟只推进 127 个员工）。

        但这些会话**上一轮就已经 upsert 进 `chat_session` 了** —— 库里的痕迹本身就是
        "扫到哪儿了"的证据。用它当水位，才能把「第一次发现」和「之后每轮都全翻」区分开：
        没有会话记录的员工仍然全量扫（该扫），扫过的员工只翻最前面一两页。

        ⚠️ 假设与 `stop_before` 原有语义同类（列表按最近活跃降序 ⇒ 整页更旧 ⇒ 后面更旧），
        所以调用方会**先减去 `SCAN_STOP_MARGIN_SECONDS`** 再当水位用。
        """
        row = db.query_one(
            """SELECT max(last_msg_time) AS mx FROM chat_session
                WHERE staff_id = %s AND session_type = ANY(%s)""",
            (staff_id, list(session_types)))
        return int((row or {}).get("mx") or 0)

    def _sync_sessions_of(self, staff_id: str, session_types: list,
                          stop_before: int = 0, limit: int = 0) -> tuple:
        """拉某员工的会话并落库。返回 ``(会话列表, stats)``。

        ``stop_before``：增量模式下传该员工已有的最大水位。会话列表按最近活跃
        降序返回，一旦整页都早于水位即可停止翻页，省掉大量无效请求。

        ``limit``：每个员工最多要多少个会话（= `max_sessions_per_staff`）。
        **翻到这么多就不再翻** —— 反正调用方随后会 `sess[:limit]` 丢掉多余的。
        不加这道闸的实测后果（2026-10-19 复盘）：297/308 个员工没有会话级游标，
        `stop_before` 接近 0，每轮都要翻十几二十页（单轮 2779 页、约 27 分钟），
        而**日常同步窗口只有 20 分钟** —— 第一遍就把窗口吃光，第二遍（真正拉消息）
        一次都没跑到：连续 3 天 `sync_job.session_done` 都是 0。

        ``stats`` = ``{pages, stopped_early, truncated}``：
        · `pages` —— 实际翻了几页（这个数直接决定第一遍的耗时，必须看得见）
        · `stopped_early` —— 靠水位提前收工的次数（证明优化真的生效）
        · `truncated` —— 撞上 `sync_session_page_cap` 被截断（**以前是静默的**：
          超过上限的旧会话永远看不到，用户却以为"同步成功了"）
        """
        cap = db.setting_int("sync_session_page_cap", "SYNC_SESSION_PAGE_CAP",
                             SESSION_PAGE_CAP_DEFAULT)
        out, pages, early, truncated = [], 0, 0, False
        for stype in session_types:
            offset = 0
            while True:
                res = weiban.list_sessions(staff_id, stype, limit=PAGE, offset=offset,
                                           order_by="last_updated_desc")
                batch = res.get("session_list") or []
                if not batch:
                    break
                out.extend(batch)
                pages += 1
                offset += PAGE
                if len(batch) < PAGE:
                    break
                if limit and len(out) >= limit:
                    break                 # 够数了：再翻下去的会话调用方本来也不要
                if stop_before and all((s.get("last_msg_time") or 0) <= stop_before
                                       for s in batch):
                    early += 1
                    break
                # ⚠️ 顺序有意：**先看水位再撞上限** —— 能靠水位收工就别去撞截断。
                if cap and offset >= cap:
                    truncated = True
                    print(f"[sync] ⚠️ 员工 {staff_id[:12]}… 的会话列表在 {cap} 条处截断"
                          f"（`sync_session_page_cap`）：更旧的会话本次拉不到")
                    break
            if limit and len(out) >= limit:
                break
        if out:
            db.execute_many(SQL_UPSERT_SESSION, [(
                staff_id, s["id"], s.get("session_type") or "ext_user",
                s.get("name"), s.get("remark"), s.get("avatar"), s.get("target_id"),
                s.get("agree_msgaudit"), s.get("last_msg_time"), s.get("last_msg_thumb"),
                s.get("created_at"),
            ) for s in out])
        return out, {"pages": pages, "stopped_early": early, "truncated": truncated}

    # ---- 单会话消息 ----
    def _messages_of(self, staff_id, session_id, since: int, cap: int,
                     end: int = None) -> list:
        """逐页取消息（返回按时间升序的完整列表）。

        :param since: `start_time` —— 取数下界（**已含重叠回看**，见 `sync_window`）
        :param end:   `end_time` —— 取数上界。**本轮全程传同一个值**：
            需求 4.3 要求「每轮任务开始时固定 `end_time`，分页中不得改变，避免新增消息
            造成分页漂移」。微伴这个接口是**按消息时间逆序 + offset 分页**，
            不固定上界时，翻页途中新到的消息会把整页往后挤 —— 边界上的那条会被**跳过**
            （而它恰恰是最新、最该入库的一条）。
        """
        msgs, offset = [], 0
        while True:
            res = weiban.list_messages(staff_id, session_id, limit=PAGE, offset=offset,
                                       start_time=since or None, end_time=end)
            batch = res.get("message_list") or []
            msgs.extend(batch)
            offset += PAGE
            if len(batch) < PAGE:
                break
            if cap and len(msgs) >= cap:
                break
        if cap:
            msgs = msgs[:cap]
        msgs.sort(key=lambda m: m.get("time") or 0)
        return msgs

    def _persist(self, staff_id, session_id, msgs, plain: dict) -> int:
        """把一批消息 upsert 进库，返回新增条数。"""
        if not msgs:
            return 0
        inserted = 0
        with db.tx() as conn:
            with conn.cursor() as cur:
                for m in msgs:
                    item = plain.get(m.get("original_id"))
                    content = ww_chatdata.plain_text(item) if item else ""
                    if item is None:
                        state = "pending" if m.get("secret_key") else "fail"
                    else:
                        state = "ok" if content else "empty"
                    cur.execute(SQL_UPSERT_MESSAGE, (
                        staff_id, session_id, m.get("id"), m.get("original_id"),
                        m.get("direction"), m.get("time"), m.get("type"),
                        m.get("from_id"), m.get("to_id"), m.get("from_name"),
                        m.get("from_avatar"), m.get("room_id"), m.get("file_url"),
                        m.get("secret_key"), content or None,
                        (item or {}).get("msgtype"),
                        db.as_json(item) if item else None, state,
                        m.get("created_at"), m.get("updated_at"),
                    ))
                    row = cur.fetchone()
                    if row and row.get("inserted"):
                        inserted += 1
        return inserted

    def _refresh_session_stat(self, staff_id, session_id):
        db.execute(
            """UPDATE chat_session SET msg_count = (
                   SELECT count(*) FROM chat_message
                   WHERE staff_id = %s AND session_id = %s)
               WHERE staff_id = %s AND session_id = %s""",
            (staff_id, session_id, staff_id, session_id))

    # ---- 凭据体检 ----
    def _credential_precheck(self) -> dict:
        """同步前凭据体检：有效且够新鲜就跳过，否则自动保活。

        微伴登录态是滑动过期的（每次访问重置为 now+7 天），所以定期保活
        就不会失效。保活本身出问题时不阻塞同步 —— 真正的凭据失效会在
        解密环节抛出明确错误。
        """
        try:
            import wb_keepalive
        except Exception as e:
            return {"ok": True, "code": 0,
                    "message": f"保活模块不可用，跳过预检（{e}）"}
        try:
            res = wb_keepalive.ensure_credentials(source="sync")
            print(f"[sync] 凭据体检：{res.get('message')}")
            return res
        except Exception as e:
            return {"ok": False, "code": 1,
                    "message": f"保活异常：{type(e).__name__}: {e}"}

    # ---- 主流程 ----
    def _run(self, job_id, kind, staff_ids, session_types, max_sessions, max_msgs,
             ignore_cursor, lookback_hours=None):
        t0 = time.time()
        # 补偿窗口（需求 4.2）：给了回看小时数就**抛开水位**，改为「最近 N 小时全重拉」，
        # 用来兜住「消息迟到入库 → msg_time 小于水位 → 增量永远取不到」的漏处理。
        since = (int(time.time()) - int(lookback_hours) * 3600) if lookback_hours else None
        # 需求 4.3：取数窗口在本轮开始时**一次性定死**（分页过程中绝不改变）。
        #   · 上界 = 本轮开始 - 60 秒 → 防「翻页途中新消息把整页往后挤」造成漏取；
        #   · 下界 = 水位 - 15 分钟（可配 `sync_overlap_minutes`）→ 兜住乱序/迟到的消息。
        round_start = int(time.time())
        overlap_seconds = db.setting_int("sync_overlap_minutes", "SYNC_OVERLAP_MINUTES",
                                         15) * 60
        # ⚠️ 上界**不要在这里自己算**：`sync_window()` 返回 `(start, end)`，
        #    end 就是 `round_start - 60`。此处曾留一个同名局部变量，从未被使用
        #    （复制同一公式 = 两份真相，改一处必然忘另一处）。
        try:
            db.init_db()

            # ---------- 凭据体检：滑动过期的登录态，快过期就自动保活 ----------
            self._bump(job_id, detail={"stage": "凭据体检"})
            pre = self._credential_precheck()
            if pre.get("code") == 3:      # 连凭据文件都没有 → 只能人工扫码
                raise RuntimeError(f"缺少聊天记录凭据，同步中止：{pre.get('message')}")
            if not pre.get("ok"):
                print(f"[sync] 凭据预检未通过：{pre.get('message')}（继续尝试同步）")

            self._bump(job_id, detail={"stage": "同步员工列表"})
            self._sync_staff(job_id)          # 进度与结果由 _bump 落库，返回值无需使用

            targets = staff_ids or self._staff_id_list()
            ww = ww_chatdata.WecomChatData(verbose=False)

            # ---------- 第一遍：列出所有待处理会话，便于进度条 ----------
            cur_map = {(r["staff_id"], r["session_id"]): r["last_msg_time"]
                       for r in db.query("SELECT staff_id, session_id, last_msg_time FROM sync_cursor")}
            staff_watermark = {}
            for (st, _se), t in cur_map.items():
                if t > staff_watermark.get(st, 0):
                    staff_watermark[st] = t
            # 再叠一层「该员工**已扫到哪**」（修 2026-10-16 实测的慢根因）：
            # 只有 11/308 个员工有会话级游标，其余 `stop_before=0` → 每次都把会话列表
            # 翻到 20000 条上限（单员工 200 页）。用「库里已知会话的最新时间 − 安全余量」
            # 当水位，就能把"首次发现"和"之后每轮都全翻"分开。
            for _sid in targets:
                kw = self._staff_scan_watermark(_sid, session_types)
                if kw:
                    kw = max(0, kw - SCAN_STOP_MARGIN_SECONDS)
                    if kw > staff_watermark.get(_sid, 0):
                        staff_watermark[_sid] = kw

            todo = []      # (staff_id, session_dict)
            pages_total = early_total = 0
            truncated_staff = []
            for i, sid in enumerate(targets, 1):
                if self._should_stop(job_id):
                    break
                self._bump(job_id, detail={"stage": f"读取会话列表 ({i}/{len(targets)})",
                                           "current_staff": sid,
                                           # 第一遍的进度（修 UX 缺陷）：这一段要遍历**全部员工**、
                                           # 每员工一次会话列表调用（实测 ~5 秒/员工 → 308 员工
                                           # ≈27 分钟），而 `session_total` 要到第一遍结束才写 ——
                                           # 不把这一段单独报出来，界面上的进度条会**半小时恒为 0%**，
                                           # 看起来像卡死（实测踩到：6.2 分钟时 stage 是 60/308、
                                           # 进度条仍是 0/0）。
                                           "staff_done": i, "staff_total": len(targets),
                                           "sessions_found": len(todo)})
                try:
                    sess, sc = self._sync_sessions_of(
                        sid, session_types, stop_before=staff_watermark.get(sid, 0),
                        limit=max_sessions)
                    pages_total += sc["pages"]
                    early_total += sc["stopped_early"]
                    if sc["truncated"]:
                        truncated_staff.append(sid[:12])
                        self._bump(job_id, detail={"truncated_count": len(truncated_staff)})
                except Exception as e:
                    print(f"[sync] 员工 {sid} 会话列表失败：{e}")
                    continue
                sess = [s for s in sess if s.get("agree_msgaudit") is not False]
                if max_sessions:
                    sess = sess[:max_sessions]
                todo.extend((sid, s) for s in sess)

            self._patch_job(job_id, session_total=len(todo))
            # 第一遍的**可观测性**（这一遍就是同步的成本大头）：
            #   · session_pages   —— 一共翻了几页（整页 100 条）
            #   · scan_early_stops—— 靠水位提前收工了几次（证明"已扫到哪"生效）
            #   · truncated_staff —— 撞上 `sync_session_page_cap` 被截断的员工（**不再是静默的**）
            self._bump(job_id, detail={"session_pages": pages_total,
                                       "scan_early_stops": early_total,
                                       "truncated_staff": truncated_staff[:10],
                                       "truncated_count": len(truncated_staff)})
            if truncated_staff:
                print(f"[sync] ⚠️ {len(truncated_staff)} 个员工的会话列表被截断"
                      f"（`sync_session_page_cap`），更旧的会话本次看不到")
            self._bump(job_id, detail={"stage": "拉取消息与正文",
                                       "session_total": len(todo)})

            # ---------- 第二遍：逐会话拉消息 + 解密 + 落库 ----------
            for _idx, (sid, s) in enumerate(todo, 1):
                if self._should_stop(job_id):
                    break
                sess_id = s["id"]
                sname = s.get("name") or "无备注"
                watermark = 0 if ignore_cursor else cur_map.get((sid, sess_id), 0)

                # 取数下界：
                #   · 补偿模式 → 回看窗口起点（抛开水位，重拉最近 N 小时）
                #   · 常规模式 → 水位 - 重叠回看（需求 4.3：`start_time` 往回 15 分钟）
                since_ts = since if since is not None else watermark
                last = s.get("last_msg_time") or 0
                if since is not None:
                    # 补偿模式只扫「窗口内还有动静」的会话，避免把 78 万会话全重拉一遍
                    if last and last < since:
                        self._bump(job_id, sessions=1,
                                   detail={"stage": "补偿同步", "skipped": True,
                                           "current_session": sname})
                        continue
                # 增量快路径：会话在**重叠窗口内**确实没有动静 → 跳过。
                # ⚠️ 判据不能用 `last <= watermark`：最后一条正好等于水位时会被跳过，
                # 而重叠窗口要救的恰恰是那种会话（迟到消息的 msg_time 早于水位）。
                # 正确判据是「连重叠窗口都没覆盖到」= `last < 水位 - 重叠`。
                elif last and last < (watermark - overlap_seconds):
                    self._bump(job_id, sessions=1,
                               detail={"stage": "拉取消息与正文", "skipped": True,
                                       "current_session": sname})
                    continue

                self._bump(job_id, detail={"stage": "拉取消息与正文",
                                           "current_session": sname,
                                           "current_staff": sid,
                                           "session_total": len(todo)})
                try:
                    start_ts, end_ts = sync_window(since_ts, round_start, overlap_seconds)
                    msgs = self._messages_of(sid, sess_id, start_ts, max_msgs, end=end_ts)
                except Exception as e:
                    print(f"[sync] 会话 {sess_id} 消息拉取失败：{e}")
                    self._bump(job_id, sessions=1)
                    continue

                if not msgs:
                    self._bump(job_id, sessions=1)
                    continue

                add = self._persist(sid, sess_id, msgs, {})     # 先落元数据
                self._bump(job_id, msgs=len(msgs), new=add)

                # 解密正文
                try:
                    plain = ww.decrypt(msgs)
                except ww_chatdata.WecomChatDataError as e:
                    raise RuntimeError(f"正文解密通道失效：{e}") from None

                self._persist(sid, sess_id, msgs, plain)
                self._bump(job_id, decrypted=len(plain))

                top = max((m.get("time") or 0) for m in msgs)
                db.execute(
                    """INSERT INTO sync_cursor (staff_id, session_id, last_msg_time, last_synced_at)
                       VALUES (%s,%s,%s, now())
                       ON CONFLICT (staff_id, session_id) DO UPDATE
                         SET last_msg_time = GREATEST(sync_cursor.last_msg_time, EXCLUDED.last_msg_time),
                             last_synced_at = now()""",
                    (sid, sess_id, top))
                self._refresh_session_stat(sid, sess_id)
                self._bump(job_id, sessions=1)

            status = "stopped" if self._stop else "success"
            elapsed = time.time() - t0
            self._bump(job_id, detail={"stage": "已完成", "elapsed_sec": round(elapsed, 1)})
            self._finish_job(job_id, status)
            print(f"[sync] job={job_id} {status} 用时 {elapsed:.1f}s")
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[sync] job={job_id} 失败：{e}\n{tb}")
            self._finish_job(job_id, "failed", error=str(e)[:2000])
            self._bump(job_id, detail={"stage": "失败", "error": str(e)})
        finally:
            self._stop = False


service = SyncService()
