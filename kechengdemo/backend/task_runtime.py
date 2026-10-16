# -*- coding: utf-8 -*-
"""任务运行时（外循环）：统一状态机 + 检查点 + 心跳 + 恢复。修硬伤 3。

## 解决什么

改造前有**四套互不相干的状态机**（`scoring_run` / `sync_job` / `writeback_outbox` /
`session_failure`），但没有统一的「任务」概念，**尤其没有检查点**：
`scoring_run` 只记「已完成 N 个会话」，不记「当前走到哪一步、下一步该做什么」。
进程崩溃后的行为是**只回收、不恢复** —— `is_running()` 把僵尸 run 标记 failed 就放行，
重跑只能靠增量水位，而未完成的中间状态直接归零。

文章 B 的原话：**「没有检查点的长任务，本质上是在拿运气赌进程不会挂。」**

## 三层语义

  L1 内循环 —— 单个会话内部的判定（`analyze` + 契约 + 修复重试）
  L2 任务循环 —— **本模块**：任务怎么开始、每步走到哪、挂了怎么续
  L3 业务闭环 —— 采集 → 判定 → 行动 → 验证 → 学习

## 两条不变量（违反即缺陷，见《客户意向评分_架构设计说明书.md》第十三章 INV-5 / INV-6）

  INV-5  终态（succeeded/partial/failed/cancelled）**不得再迁出**
  INV-6  同一 task 的 `seq` 严格递增；`last_step_seq` = 最后一个 succeeded 的 seq

## 恢复语义（为什么是"新建任务"而不是"改回 running"）

`recover_stale()` 把心跳超时的任务标记为 `failed`（理由可查），并返回**可恢复清单**；
真正的续跑由 `resume()` **新建一个任务**，把 `resumed_from` 指向源任务。
这样做的理由：历史不可变（源任务保留"它当时确实挂了"这个事实），
且新任务有自己干净的幂等键与预算，不会与旧任务的行互相污染。
"""
import json
import time

import db
import security
import audit                       # 审批参数与工具参数同一套脱敏（密钥不得落库）

#: 终态 —— 任何迁出终态的尝试都被拒绝（INV-5）
TERMINAL = ("succeeded", "partial", "failed", "cancelled")

#: 允许的状态迁移表。**唯一语义**，不允许隐式跳转。
ALLOWED = {
    "queued": {"running", "cancelled", "failed"},
    "running": {"waiting_approval", "paused", "succeeded", "partial",
                "failed", "cancelled"},
    "waiting_approval": {"running", "cancelled", "failed"},
    "paused": {"running", "cancelled", "failed"},
    "succeeded": set(),
    "partial": set(),
    "failed": set(),
    "cancelled": set(),
}

#: 心跳超时阈值（秒）。超过它仍为 running → 视为进程异常退出（INV-16）。
HEARTBEAT_TIMEOUT = 60


class TransitionError(Exception):
    """非法状态迁移（如终态迁出、waiting_approval 被自动执行）。"""


def can_transition(frm: str, to: str) -> bool:
    """纯函数：``frm`` 能否迁到 ``to``。**终态不可迁出**（INV-5）。"""
    return to in ALLOWED.get(frm or "", set())


def create(*, kind: str, actor_id: str | None = None, trigger: str = "manual",
           scope=None, priority: int = 5, budget_calls=None, budget_tokens=None,
           deadline_minutes=None, idempotency_key=None, resumed_from=None) -> dict:
    """建任务（状态 `queued`）。同 `idempotency_key` 重复创建 → 复用已有任务。

    返回 ``{"ok", "task_id", "reused"}``。`actor_id` 缺省由服务端推导。
    """
    who = actor_id or security.actor()
    if idempotency_key:
        old = db.query_one(
            "SELECT task_id, status FROM task WHERE idempotency_key=%s",
            (idempotency_key,))
        if old:
            return {"ok": True, "task_id": old["task_id"], "reused": True,
                    "status": old["status"]}
    row = db.query_one(
        """INSERT INTO task (kind, status, actor_id, trigger_src, scope, priority,
                             budget_calls, budget_tokens, deadline_at,
                             idempotency_key, resumed_from)
           VALUES (%s, 'queued', %s, %s, %s::jsonb, %s, %s, %s,
                   CASE WHEN %s > 0 THEN now() + make_interval(mins => %s) END,
                   %s, %s)
           RETURNING task_id""",
        (kind, who, trigger,
         json.dumps(scope, ensure_ascii=False) if scope else None,
         int(priority or 5),
         (int(budget_calls) if budget_calls else None),
         (int(budget_tokens) if budget_tokens else None),
         int(deadline_minutes or 0), int(deadline_minutes or 0),
         idempotency_key, resumed_from))
    return {"ok": True, "task_id": row["task_id"], "reused": False, "status": "queued"}


def get(task_id: int) -> dict:
    return db.query_one("SELECT * FROM task WHERE task_id=%s", (task_id,)) or {}


def transition(task_id: int, to: str, *, reason: str = None, error=None) -> bool:
    """**唯一**的状态迁移入口：先校验再写库。非法迁移抛 :class:`TransitionError`。

    为什么要集中而不是各处直接 UPDATE：状态机只在文档里写一遍不等于实现正确，
    必须有**单一入口**才能保证「终态迁不出」「waiting_approval 不被自动执行」
    这两条在代码层面成立（文章 E：状态定义只是第一步，迁移条件才是关键）。
    """
    cur = get(task_id)
    if not cur:
        raise TransitionError(f"任务 {task_id} 不存在")
    frm = cur.get("status")
    if not can_transition(frm, to):
        raise TransitionError(f"非法状态迁移：{frm} → {to}（task {task_id}）")
    sets = ["status = %s"]
    params = [to]
    if to == "running" and not cur.get("started_at"):
        sets.append("started_at = now()")
        sets.append("heartbeat_at = now()")
    if to in TERMINAL:
        sets.append("finished_at = now()")
    if to == "partial":
        sets.append("partial_reason = %s")
        params.append(reason)
    if to in ("failed", "cancelled"):
        sets.append("partial_reason = COALESCE(%s, partial_reason)")
        params.append(reason)
    if error is not None:
        sets.append("error_class = %s")
        params.append(type(error).__name__ if not isinstance(error, str) else "Error")
        sets.append("error_message = %s")
        params.append(str(error)[:2000])
    params.append(task_id)
    db.execute(f"UPDATE task SET {', '.join(sets)} WHERE task_id = %s", tuple(params))
    audit.record_action(security.actor(), f"task_{to}", f"task:{task_id}",
           {"from": frm, "to": to, "reason": reason})
    return True


def step_begin(task_id: int, name: str, *, input_ref=None, seq: int = None) -> int:
    """开一个新步骤（检查点起点），返回 ``step_id``。

    ``seq`` 缺省自动取 ``max(seq)+1`` —— 调用方不必自己记序号（少一处出错点）。
    """
    if seq is None:
        row = db.query_one(
            "SELECT COALESCE(max(seq), 0) AS m FROM task_step WHERE task_id=%s",
            (task_id,))
        seq = int((row or {}).get("m") or 0) + 1
    got = db.query_one(
        """INSERT INTO task_step (task_id, seq, name, status, input_ref, started_at)
           VALUES (%s,%s,%s,'running',%s::jsonb, now())
           RETURNING step_id""",
        (task_id, int(seq), name,
         json.dumps(input_ref, ensure_ascii=False, default=str) if input_ref else None))
    return got["step_id"]


def step_end(step_id: int, ok: bool, *, output_ref=None, next_step=None,
             error=None) -> None:
    """收尾一个步骤：落**结果引用**与**下一步该做什么**（检查点的核心）。

    ``next_step`` 是恢复时唯一需要的信息 —— 没有它，"续跑"只能靠猜。
    """
    db.execute(
        """UPDATE task_step
              SET status=%s, output_ref=%s::jsonb, next_step=%s,
                  error_class=%s, error_message=%s, finished_at=now(),
                  attempts = attempts + 1
            WHERE step_id=%s""",
        ("succeeded" if ok else "failed",
         json.dumps(output_ref, ensure_ascii=False, default=str) if output_ref else None,
         next_step,
         (type(error).__name__ if (error and not isinstance(error, str)) else
          ("Error" if error else None)),
         (str(error)[:2000] if error else None),
         step_id))
    # INV-6：恢复点 = 最后一个 succeeded 的 seq
    if ok:
        db.execute(
            """UPDATE task t SET last_step_seq = GREATEST(t.last_step_seq, s.seq)
                 FROM task_step s
                WHERE s.step_id = %s AND t.task_id = s.task_id""", (step_id,))


def heartbeat(task_id: int) -> None:
    """刷新心跳（INV-16：running 状态下每 ≤60s 一次）。失败不抛。"""
    try:
        db.execute("UPDATE task SET heartbeat_at = now() WHERE task_id=%s", (task_id,))
    except Exception:                           # noqa: BLE001
        pass


def _progress_snapshot(task_id: int) -> dict:
    """读 `task.progress` 并归一成 dict。

    JSONB 读回来可能是 `dict`、也可能是**字符串**（写入方传了字符串时），
    这个知识原先在 `bump_progress` 与 `set_progress` 里各写了一遍 ——
    于是「进度坏了怎么兜底」有两套实现，改一处必漏另一处。
    """
    row = db.query_one("SELECT progress FROM task WHERE task_id=%s", (task_id,))
    cur = row.get("progress") if row else None
    if isinstance(cur, str):
        try:
            cur = json.loads(cur)
        except Exception:                          # noqa: BLE001
            cur = {}
    return dict(cur or {})


def _write_progress(task_id: int, snap: dict) -> None:
    """写回进度，并**顺手刷新心跳** —— 心跳靠的就是「进度在动」这件事。"""
    db.execute("UPDATE task SET progress=%s::jsonb, heartbeat_at=now() WHERE task_id=%s",
               (json.dumps(snap, ensure_ascii=False), task_id))


def bump_progress(task_id: int, **deltas) -> dict:
    """累加进度计数（存 `task.progress` JSONB）。返回累加后的快照。"""
    snap = _progress_snapshot(task_id)
    for k, v in deltas.items():
        if v:
            snap[k] = snap.get(k, 0) + v
    _write_progress(task_id, snap)
    return snap


def completed_step_names(task_id: int) -> set:
    """该任务里**已成功**的步骤名集合 —— 续跑时用它跳过已完成的工作。

    这是「从检查点续跑而不是从头再来」的落地：名字即幂等键
    （如 ``session:<staff_id>:<session_id>``），已成功的不再重复执行。
    """
    rows = db.query(
        "SELECT DISTINCT name FROM task_step WHERE task_id=%s AND status='succeeded'",
        (task_id,))
    return {r["name"] for r in rows}


def recover_stale(stale_seconds: int = HEARTBEAT_TIMEOUT) -> dict:
    """启动时扫描：把心跳超时的 `running` 任务标记失败，并返回**可恢复清单**。

    ⚠️ 语义边界（必须说清，否则会被误读为"什么都没做"）：
      本函数**不直接续跑**，只做两件事 —— ① 解除阻塞（否则永远不会再被调度）
      ② 交出检查点。真正的续跑由 :func:`resume` 新建任务完成。
      这样源任务保留「它当时确实挂了」这个事实，历史不可变。

    返回 ``{"recovered": n, "resumable": [{task_id, kind, last_step_seq, progress}]}``。
    DB 不可用时**返回空而不再抛** —— 启动路径不该被恢复逻辑拖垮。
    """
    try:
        stale = db.query(
            """SELECT task_id, kind, last_step_seq, progress, scope, actor_id,
                      COALESCE(EXTRACT(EPOCH FROM (now() - heartbeat_at)), 0) AS age
                 FROM task
                WHERE status = 'running'
                  AND COALESCE(heartbeat_at, started_at, created_at)
                      < now() - make_interval(secs => %s)
                ORDER BY task_id""", (int(stale_seconds),))
    except Exception as e:                      # noqa: BLE001
        print(f"[task] 扫描僵尸任务失败（不影响启动）：{type(e).__name__}: {e}")
        return {"recovered": 0, "resumable": []}
    for r in stale:
        try:
            transition(r["task_id"], "failed",
                       reason=f"进程异常退出：心跳超过 {int(stale_seconds)}s 未刷新")
        except TransitionError as e:
            print(f"[task] 僵尸回收跳过 {r['task_id']}：{e}")
    return {"recovered": len(stale),
            "resumable": [{"task_id": r["task_id"], "kind": r["kind"],
                           "last_step_seq": r["last_step_seq"],
                           "stale_seconds": int(r.get("age") or 0),
                           "progress": r.get("progress"), "scope": r.get("scope")}
                          for r in stale]}


def resume(src_task_id: int, *, actor_id: str | None = None,
           reason: str = "manual_resume") -> dict:
    """新建一个任务，从 ``src_task_id`` 的检查点继续。

    返回 ``{"ok", "task_id", "resumed_from", "done_steps", "carry_over"}``：
      · ``done_steps``：源任务里已成功的步骤名（调用方据此**跳过已完成会话**）
      · ``carry_over``：源任务已消耗的预算 —— **必须继承**，
        否则"续跑"会绕过成本上限，等于给预算开了一个后门。
    """
    src = get(src_task_id)
    if not src:
        return {"ok": False, "reason": f"源任务 {src_task_id} 不存在"}
    if src.get("status") not in TERMINAL:
        return {"ok": False,
                "reason": f"源任务状态为 {src.get('status')}，只有终态任务才能续跑"
                          f"（否则会同时有两个任务在跑同一批会话）"}
    who = actor_id or security.actor()
    created = create(
        kind=src["kind"], actor_id=who,
        trigger="resume",
        scope=src.get("scope") if not isinstance(src.get("scope"), str)
        else json.loads(src["scope"]),
        priority=src.get("priority") or 5,
        budget_calls=src.get("budget_calls"), budget_tokens=src.get("budget_tokens"),
        resumed_from=src_task_id)
    tid = created["task_id"]
    # 预算继承（见 docstring：不继承 = 预算后门）
    db.execute(
        "UPDATE task SET cost_calls=%s, cost_tokens=%s, last_step_seq=%s "
        "WHERE task_id=%s",
        (int(src.get("cost_calls") or 0), int(src.get("cost_tokens") or 0),
         0, tid))
    audit.record_action(who, "task_resume", f"task:{tid}",
           {"resumed_from": src_task_id, "reason": reason})
    return {"ok": True, "task_id": tid, "resumed_from": src_task_id,
            "done_steps": sorted(completed_step_names(src_task_id)),
            "carry_over": {"cost_calls": int(src.get("cost_calls") or 0),
                           "cost_tokens": int(src.get("cost_tokens") or 0)}}


# ---------------------------------------------------------------- 审批门控

def request_approval(task_id: int, tool_name: str, *, step_id=None, params=None,
                     reason: str = None, expires_minutes: int = 0,
                     block_task: bool = True, assessment_id: int = None) -> dict:
    """为高风险操作建审批请求，并把任务**物理阻塞**在 `waiting_approval`。

    文章 H 的关键点：把「先计划后执行」从软约束（写在提示词里希望模型遵守）
    变成**硬约束**（代码层面不让它继续往下走）。所以这里不只是插一条记录，
    还要**真的把任务状态推走** —— 之后的调度器看到一个不在 running 的任务，
    自然不会继续推进它。

    ``block_task=False``（2026-10-16 新增）：**只登记审批、不推任务状态**。
    给「一条客户回写需要人工确认」这种场景用 —— 它卡住的是**那一条回写**
    （`outbox._policy_hold` 会一直挂着它），而不是整个评分批次；把批次推到
    `waiting_approval` 等于因为一个客户而停掉整轮评分。``assessment_id`` 记录
    "审批的是哪一条评估"，放行时才找得回要投递的分数。
    """
    who = security.actor()
    row = db.query_one(
        """INSERT INTO approval_request
             (task_id, step_id, tool_name, params_redacted, requested_by,
              expires_at, assessment_id)
           VALUES (%s,%s,%s,%s::jsonb,%s,
                   CASE WHEN %s > 0 THEN now() + make_interval(mins => %s) END,
                   %s)
           RETURNING request_id""",
        (task_id, step_id, tool_name,
         # ⚠️ 必须走同一套脱敏：审批参数里同样会带 token / 密钥
         json.dumps(audit.redact(params or {}), ensure_ascii=False, default=str),
         who, int(expires_minutes or 0), int(expires_minutes or 0), assessment_id))
    if block_task:
        transition(task_id, "waiting_approval",
                   reason=f"需审批：{tool_name}｜{reason or ''}")
    return {"ok": True, "request_id": row["request_id"],
            "task_id": task_id, "blocked": bool(block_task)}


def request_writeback_approval(*, assessment_id: int, relationship_key: str,
                               staff_id: str, external_user_id: str, score: int,
                               reason: str = "") -> dict:
    """「这条回写要人工点头」的审批请求（**不阻塞批次**），**幂等**。

    为什么需要它：`outbox._policy_hold()` 早就把 `policy_action != auto` 的回写挂在
    `pending` 上等人工 —— 但**从来没有任何机制能放行它**（也没人知道要去放行）：
    `approval_request` 只有测试在写，审批队列永远是空的。于是「需人工二次确认后才写入」
    这句承诺（`scoring_service._POLICY_WB_CN`）在代码里是**兑现不了的**。

    幂等口径：同一 `assessment_id` 只要已有 `pending` / `approved` 的请求就直接复用，
    不重复插（同一客户被反复评估时会走到这里多次）。已被**驳回**的不复用 ——
    那是"人工明确说过不写"，再次评估要重新走一遍确认。
    """
    if not assessment_id:
        return {"ok": False, "reason": "缺少 assessment_id"}
    try:
        old = db.query_one(
            """SELECT request_id, status FROM approval_request
                WHERE assessment_id = %s AND status IN ('pending','approved')
                ORDER BY request_id DESC LIMIT 1""", (assessment_id,))
        if old:
            return {"ok": True, "request_id": old["request_id"],
                    "status": old["status"], "reused": True, "blocked": False}
    except Exception as e:                      # noqa: BLE001
        print(f"[approval] 查重失败（照常新建）：{type(e).__name__}: {e}")
    expires = db.setting_int("writeback_approval_expires_minutes",
                             "WRITEBACK_APPROVAL_EXPIRES_MINUTES", 0)
    res = request_approval(
        None, "weiban.add_customer_score",
        params={"assessment_id": assessment_id, "relationship_key": relationship_key,
                "staff_id": staff_id, "external_user_id": external_user_id,
                "score": int(score)},
        reason=reason, block_task=False, assessment_id=assessment_id,
        expires_minutes=expires)
    audit.record_action("system", "writeback_approval_requested",
                        f"assessment:{assessment_id}",
                        {"request_id": res.get("request_id"), "reason": reason,
                         "score": int(score)})
    return res


def decide_approval(request_id: int, *, approve: bool, note: str = "") -> dict:
    """审批：通过则任务回到 `running`，驳回则任务 `cancelled`。

    ⚠️ 只认 `pending` 的请求；**已决的请求不会被二次处理**
    （文章 H：审批响应必须精确匹配，否则会误批准另一个待处理请求）。

    两类请求走同一张表，但**收尾方式不同**（2026-10-16）：
    · 有 `task_id` 的（工具网关）→ 推进任务状态机（原语义，不变）；
    · 没有 `task_id` 的（回写审批）→ 不动任何任务状态，只返回结论；真正的放行
      由调用方按 `assessment_id` 落 `outbox`（见 `api_scoring.decide_approval`）。
      这类请求卡住的是**一条客户回写**，不该去推一个可能早已结束的批次任务。

    `expires_at` 过期后拒绝处理：审批的是"当时那一次判定"，太久之后的签字没有意义
    （分数、会话、策略阈值都可能已经变了）。过期的行会被就地标成 `expired`，
    不再挂在"待审批"里误导人。
    """
    who = security.actor()
    req = db.query_one("SELECT * FROM approval_request WHERE request_id=%s",
                       (request_id,))
    if not req:
        return {"ok": False, "reason": f"审批请求 {request_id} 不存在"}
    if req["status"] != "pending":
        return {"ok": False, "reason": f"该请求已是 {req['status']}，不再处理"}
    exp = req.get("expires_at")
    if exp and exp.timestamp() < time.time():
        db.execute("""UPDATE approval_request SET status='expired'
                       WHERE request_id=%s AND status='pending'""", (request_id,))
        return {"ok": False, "reason": f"审批请求 {request_id} 已过期（{exp:%Y-%m-%d %H:%M}），"
                                       f"已标记为 expired；如仍需写入请重新评估该客户"}
    db.execute(
        """UPDATE approval_request SET status=%s, decided_by=%s, decided_at=now(),
               note=%s WHERE request_id=%s AND status='pending'""",
        ("approved" if approve else "rejected", who, note or None, request_id))
    if req.get("task_id"):
        try:
            transition(req["task_id"], "running" if approve else "cancelled",
                       reason=f"审批 {'通过' if approve else '驳回'}：{note or ''}")
        except TransitionError as e:
            return {"ok": False, "reason": str(e)}
    audit.record_action(who, "approval_decide", f"approval:{request_id}",
           {"approve": bool(approve), "task_id": req.get("task_id"),
            "tool_name": req.get("tool_name"),
            "assessment_id": req.get("assessment_id"), "note": note})
    return {"ok": True, "request_id": request_id, "approved": bool(approve),
            "task_id": req.get("task_id"), "tool_name": req.get("tool_name"),
            "assessment_id": req.get("assessment_id")}


def is_blocked(task_id: int) -> bool:
    """任务是否被审批门卡住 —— 调用方（调度器）**推进前必须查**。"""
    st = (get(task_id) or {}).get("status")
    return st in ("waiting_approval", "paused", "cancelled")


def stats(days: int = 30) -> dict:
    """任务运行时观测：状态分布 + 僵尸回收次数 + 待审批数。"""
    rows = db.query(
        """SELECT status, count(*) AS n FROM task
            WHERE created_at >= now() - (%s || ' days')::interval
            GROUP BY 1 ORDER BY 2 DESC""", (str(int(days)),))
    pending = db.query_one(
        "SELECT count(*) AS n FROM approval_request WHERE status='pending'") or {}
    steps = db.query_one(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE status='failed') AS failed
             FROM task_step""") or {}
    return {"days": days, "by_status": {r["status"]: int(r["n"]) for r in rows},
            "pending_approvals": int(pending.get("n") or 0),
            "steps": steps,
            "heartbeat_timeout_seconds": HEARTBEAT_TIMEOUT,
            "allowed_transitions": {k: sorted(v) for k, v in ALLOWED.items()}}


