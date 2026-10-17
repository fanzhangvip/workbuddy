# -*- coding: utf-8 -*-
"""兴趣点提取的批量服务（独立于评分：`JobService` 子类 + 增量水位 + 检查点）。

## 为什么独立成一条链（而不是挂在评分批量里）

兴趣点回答「用户关心什么」（选题），评分回答「这个客户值不值得跟」（转化）。
两者共用一次上下文装配看起来省事，但代价是：想重跑兴趣就得重评分、评分契约会被
喂进一份不相干的输出、任一侧失败都拖累另一侧。分开之后，各自增量、各自重跑、
各自可观测 —— 代价只是多一次模型调用（每会话 1 次）。

## 增量与可复现

· 候选 = 「有正文 且 (没分析过 OR 分析水位 < 会话最新时间)」（`interest_repo.candidates`）
  → 天然增量、重跑幂等、中断后接着跑；
· 上下文固定用 `delta=False`（"最近 N 条"，与评估集同口径）→ **同一会话任何时候跑
  都得到同样的兴趣点**，不受"上次评到哪"影响；
· 词表版本 / 提示词版本随结果落库，跨版本差异可识别。

## 护栏（与评分同一套）

· 与评分**互斥**（都吃模型配额）：`"scoring" in running_kinds()` → 拒绝启动；
· 与同步**不互斥**：本服务只读消息，不阻塞 20:00 的日常同步（链条上本来也是串行）；
· 整轮预算 `interest_max_llm_calls` + 墙钟 `deadline`（复用 `hooks.guard_reason`）；
· 单会话失败**不拖垮整轮**，但要落 `session_failure`（DB 可见），不静默。
"""
import time

import db
import job_service
import interest
import interest_repo


class InterestService(job_service.JobService):
    """兴趣点提取批量。运行状态：进程内线程 + `interest_run` 行 + 心跳僵尸判定。"""

    TABLE = "interest_run"

    def __init__(self):
        super().__init__()
        self._run_id = None
        self._task_id = None
        self._stop = False
        self._partial = None

    # ---------------- 运行控制 ----------------

    def start(self, *, kind: str = "incremental", staff_ids: list = None,
              max_sessions: int = 0, min_messages: int = 0,
              min_customer_msgs: int = 0,
              triggered_by: str = "manual") -> dict:
        import job_service as js

        if self.is_running():
            return {"ok": False, "reason": "已有兴趣点分析在运行，请先等待完成"}
        running = js.running_kinds(exclude="interest")
        if "scoring" in running:
            return {"ok": False, "reason": "评分任务进行中（两者都要调用大模型），"
                                          "请等评分跑完再分析兴趣点"}
        if not _llm_ready():
            return {"ok": False, "reason": "缺少大模型配置（LLM_BASE_URL / LLM_API_KEY）"}

        t = interest.load_taxonomy()
        errs = interest.validate_taxonomy(t)
        if errs:
            return {"ok": False, "reason": "兴趣词表不合法：" + "；".join(errs)}

        max_sessions = int(max_sessions or db.setting_int(
            "interest_max_sessions", "INTEREST_MAX_SESSIONS", 0))
        min_messages = int(min_messages or db.setting_int(
            "interest_min_messages", "INTEREST_MIN_MESSAGES", 8))
        min_customer_msgs = int(min_customer_msgs or db.setting_int(
            "interest_min_customer_msgs", "INTEREST_MIN_CUSTOMER_MSGS", 2))
        max_llm_calls = db.setting_int("interest_max_llm_calls",
                                       "INTEREST_MAX_LLM_CALLS", 300)
        deadline_min = int(db.setting_int("scoring_deadline_minutes",
                                          "SCORING_DEADLINE_MINUTES", 120))

        detail = db.as_json({"staff_ids": staff_ids, "max_sessions": max_sessions,
                             "min_messages": min_messages,
                             "min_customer_msgs": min_customer_msgs,
                             "taxonomy_version": t.get("taxonomy_version"),
                             "prompt_version": interest.PROMPT_VERSION})
        run_id = db.query_one(
            """INSERT INTO interest_run (kind, status, triggered_by, detail,
                                         deadline, max_llm_calls)
               VALUES (%s, 'running', %s, %s,
                       CASE WHEN %s > 0 THEN now() + make_interval(mins => %s) END,
                       NULLIF(%s, 0)) RETURNING id""",
            (kind, triggered_by, detail, deadline_min, deadline_min,
             max_llm_calls))["id"]

        self._task_id = None
        try:
            import task_runtime
            task = task_runtime.create(
                kind="interest", trigger=triggered_by,
                scope={"staff_ids": staff_ids, "max_sessions": max_sessions,
                       "min_messages": min_messages,
                       "min_customer_msgs": min_customer_msgs,
                       "taxonomy_version": t.get("taxonomy_version")},
                budget_calls=max_llm_calls or None, deadline_minutes=deadline_min)
            self._task_id = task.get("task_id")
            db.execute("UPDATE interest_run SET task_id=%s WHERE id=%s",
                       (self._task_id, run_id))
        except Exception as e:                      # noqa: BLE001
            print(f"[interest] 建任务失败（不影响分析，仅失去检查点）：{e}")

        self._stop = False
        self._partial = None
        self._run_id = run_id
        self._progress = {"stage": "启动中", "current_target": None,
                          "session_total": 0, "session_done": 0, "session_skipped": 0,
                          "session_failed": 0, "llm_calls": 0, "llm_failed": 0,
                          "interest_points": 0, "dropped_points": 0,
                          "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self._spawn(self._run, (run_id, staff_ids, max_sessions, min_messages,
                                min_customer_msgs), name=f"interest-{run_id}")
        return {"ok": True, "run_id": run_id, "taxonomy_version": t.get("taxonomy_version"),
                "prompt_version": interest.PROMPT_VERSION}

    def stop(self) -> dict:
        """请求停止：置位 + 落库，让批在当前会话处理完后优雅退出。"""
        if not self.is_running():
            return {"ok": False, "reason": "当前没有正在运行的兴趣点分析", "stopped": False}
        self._stop = True
        if self._run_id:
            db.execute("UPDATE interest_run SET stop_requested=true WHERE id=%s",
                       (self._run_id,))
        return {"ok": True, "stopped": True,
                "message": "已发出停止信号，将在当前会话处理完后退出"}

    def status(self) -> dict:
        job = db.query_one("SELECT * FROM interest_run ORDER BY id DESC LIMIT 1") or {}
        with self._lock:
            prog = dict(self._progress)
        return {"running": self.is_running(), "stopping": self._stop,
                "job": job, "progress": prog,
                "coverage": interest_repo.coverage(
                    min_messages=db.setting_int("interest_min_messages",
                                                "INTEREST_MIN_MESSAGES", 8),
                    min_customer_msgs=db.setting_int(
                        "interest_min_customer_msgs", "INTEREST_MIN_CUSTOMER_MSGS", 2)),
                "taxonomy_version": interest.load_taxonomy().get("taxonomy_version"),
                "prompt_version": interest.PROMPT_VERSION}

    # ---------------- 内部 ----------------

    def _bump(self, run_id, *, detail=None, **deltas):
        snap = self._accumulate(deltas, detail)
        # ⚠️ `session_total` 必须写回：它是"本轮候选总数"，落库后才是**刷新页面/换进程
        # 时唯一能看到的进度来源**（内存进度只活在跑批的那个进程里）。漏写会让
        # `interest_run.session_total` 恒为 0 —— 进度条显示 0/0，像"没开始跑"。
        db.execute(
            """UPDATE interest_run
                  SET session_total=%s, session_done=%s, session_skipped=%s, session_failed=%s,
                      llm_calls=%s, llm_failed=%s, interest_points=%s, dropped_points=%s,
                      detail=%s, heartbeat_at=now()
                WHERE id=%s""",
            (snap.get("session_total", 0), snap.get("session_done", 0),
             snap.get("session_skipped", 0), snap.get("session_failed", 0),
             snap.get("llm_calls", 0),
             snap.get("llm_failed", 0), snap.get("interest_points", 0),
             snap.get("dropped_points", 0), db.as_json(snap), run_id))
        return snap

    def _inc(self, **deltas):
        self._accumulate(deltas)

    def _record_failure(self, run_id, cand, err):
        """单会话失败落 `session_failure`（与评分链路同一张表、同一口径）。

        ⚠️ `run_id` 刻意留 NULL 且 `status='dead'`：这张表的 `run_id` 语义是
        **评分批次**，兴趣分析不属于它；`failure_worker` 只重投 `pending`，
        而兴趣分析的重试由**下一轮增量**天然完成（水位没推进就还会被选中）——
        标 dead 是"终态可见"，不是"放弃"。
        """
        try:
            prev = db.query_one(
                """SELECT COALESCE(MAX(attempts),0) AS n FROM session_failure
                    WHERE staff_id=%s AND session_id=%s AND stage='interest_extract'""",
                (cand["staff_id"], cand["session_id"])) or {}
            attempts = int(prev.get("n") or 0) + 1
            db.execute(
                """INSERT INTO session_failure
                     (run_id, staff_id, session_id, stage, error_class, error_message,
                      attempts, status)
                   VALUES (NULL,%s,%s,'interest_extract',%s,%s,%s,'dead')""",
                (cand["staff_id"], cand["session_id"], type(err).__name__,
                 str(err)[:2000], attempts))
        except Exception as e:                      # noqa: BLE001
            print(f"[interest] 写失败明细时出错：{e}")
            self._inc(failure_record_errors=1)

    def _run(self, run_id, staff_ids, max_sessions, min_messages, min_customer_msgs):
        import hooks
        import task_runtime

        t0 = time.time()
        try:
            db.init_db()
            if self._task_id:
                try:
                    task_runtime.transition(self._task_id, "running")
                except Exception as e:              # noqa: BLE001
                    print(f"[interest] 任务转 running 失败（继续分析）：{e}")
            taxonomy = interest.load_taxonomy()
            deadline = db.setting_int("scoring_deadline_minutes",
                                      "SCORING_DEADLINE_MINUTES", 120)
            deadline_ts = (time.time() + deadline * 60) if deadline else None
            row = db.query_one("SELECT deadline, max_llm_calls FROM interest_run WHERE id=%s",
                               (run_id,)) or {}
            if row.get("deadline"):
                deadline_ts = row["deadline"].timestamp()
            max_llm_calls = int(row.get("max_llm_calls") or 0)
            temperature = db.setting_float("llm_temperature", "LLM_TEMPERATURE",
                                           _default_temperature())
            max_tokens = db.setting_int("llm_max_tokens", "LLM_MAX_TOKENS", 0) or None
            max_repairs = db.setting_int("rejudge_retries", "REJUDGE_RETRIES", 2)
            timeout = db.setting_int("llm_timeout_sec", "LLM_TIMEOUT_SEC", 0) or None
            ctx_limit = db.setting_int("interest_ctx_limit", "INTEREST_CTX_LIMIT", 300)

            self._bump(run_id, detail={"stage": "筛选候选会话"})
            cands = interest_repo.candidates(min_messages=min_messages,
                                             min_customer_msgs=min_customer_msgs,
                                             staff_ids=staff_ids, limit=max_sessions)
            self._bump(run_id, session_total=len(cands),
                       detail={"stage": f"开始分析（共 {len(cands)} 个会话）"})

            for i, cand in enumerate(cands, 1):
                if self._stop:
                    break
                guard = hooks.guard_reason(deadline_ts, max_llm_calls,
                                           self._progress.get("llm_calls", 0))
                if guard:
                    self._partial = guard
                    break
                self._bump(run_id, detail={"stage": f"分析 ({i}/{len(cands)})",
                                           "current_target": cand.get("target_name"),
                                           "current_staff": cand.get("staff_name")})
                step_id = None
                if self._task_id:
                    try:
                        step_id = task_runtime.step_begin(
                            self._task_id, str(cand["session_id"]),
                            input_ref={"kind": "interest"})
                    except Exception:               # noqa: BLE001
                        step_id = None
                try:
                    r = self._analyze_one(run_id, cand, taxonomy, ctx_limit,
                                          temperature, max_tokens, max_repairs, timeout)
                    if r == "skipped":
                        self._bump(run_id, session_skipped=1)
                    else:
                        self._bump(run_id, session_done=1)
                    if step_id:
                        task_runtime.step_end(step_id, True, output_ref={"result": r})
                except Exception as e:              # noqa: BLE001
                    self._bump(run_id, llm_failed=1, session_failed=1)
                    self._record_failure(run_id, cand, e)
                    if step_id:
                        try:
                            task_runtime.step_end(step_id, False, error=str(e)[:500])
                        except Exception:           # noqa: BLE001
                            pass

            if self._task_id:
                try:
                    if self._stop:
                        task_runtime.transition(self._task_id, "cancelled",
                                                reason="人工停止")
                    elif self._partial:
                        task_runtime.transition(self._task_id, "partial",
                                                reason=self._partial)
                    else:
                        task_runtime.transition(self._task_id, "succeeded")
                except Exception as e:              # noqa: BLE001
                    print(f"[interest] 任务终态写入失败：{e}")

            elapsed = round(time.time() - t0, 1)
            status = "stopped" if self._stop else ("partial" if self._partial else "success")
            db.execute(
                """UPDATE interest_run SET status=%s, elapsed_sec=%s, finished_at=now(),
                       detail = COALESCE(detail,'{}'::jsonb) || %s::jsonb
                 WHERE id=%s""",
                (status, elapsed, db.as_json({"partial_reason": self._partial,
                                              "elapsed_sec": elapsed}), run_id))
        except Exception as e:                      # noqa: BLE001
            import traceback
            traceback.print_exc()
            db.execute("""UPDATE interest_run SET status='failed', error=%s,
                                 finished_at=now(), elapsed_sec=%s WHERE id=%s""",
                       (str(e)[:2000], round(time.time() - t0, 1), run_id))
            if self._task_id:
                try:
                    import task_runtime as tr
                    tr.transition(self._task_id, "failed", reason=str(e)[:500])
                except Exception:                   # noqa: BLE001
                    pass

    def _analyze_one(self, run_id, cand, taxonomy, ctx_limit, temperature, max_tokens,
                     max_repairs, timeout) -> str:
        """分析一个会话：装配上下文 → 调模型 → 回链归一 → 落库。

        ⚠️ `customer_only=True`：**只把客户自己说的话**交给模型。员工的话术
        （"我们这门课…""现在报名有优惠""名额不多了"）里塞满了课程卖点，模型很容易
        把**销售的主张**读成"客户的兴趣" —— 那图上就会出现一批客户从没提过的点，
        而且点开证据还是销售自己说的。这条口子必须在这里关掉（不是靠提示词叮嘱）。
        """
        from scoring_context import build_context

        staff_id, session_id = cand["staff_id"], cand["session_id"]
        rel = cand.get("relationship_key") or f"{staff_id}:{cand.get('target_id')}"
        ctx = build_context(staff_id, session_id, limit=ctx_limit,
                            session_meta={"staff_id": staff_id, "session_id": session_id},
                            rel_key=rel, key_history=False, delta=False,
                            customer_only=True)
        text = (ctx.get("text") or "").strip()
        if not text:
            # 客户没说过可读的话（只有图片/被撤回/null），白跑一次调用不值得。
            # 但仍要**落一行空结果把水位推过去**：否则这条会话永远是候选，
            # 每轮增量都被重新选中、每轮都跳过 —— 界面上的"待分析"永远归不了零。
            interest_repo.upsert_scan(
                staff_id=staff_id, session_id=session_id, relationship_key=rel,
                staff_name=cand.get("staff_name"), target_id=cand.get("target_id"),
                target_name=cand.get("target_name"), interests=[],
                summary="（客户没有可提取的发言）",
                taxonomy_version=str(taxonomy.get("taxonomy_version") or ""),
                prompt_version=interest.PROMPT_VERSION, model_name="",
                msg_count=cand.get("msg_count") or 0,
                last_msg_time=cand.get("last_msg_time") or 0)
            return "skipped"

        def on_attempt(_n, meta):
            usage = (meta or {}).get("usage") or {}
            with self._lock:
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    self._progress[k] = self._progress.get(k, 0) + int(usage.get(k) or 0)

        self._bump(run_id, llm_calls=1)
        out = interest.extract(text, temperature=temperature, max_tokens=max_tokens,
                               max_repairs=max_repairs, timeout=timeout,
                               on_attempt=on_attempt)
        rows = interest.to_rows(out["interests"], taxonomy,
                                ctx.get("index"), ctx.get("labels"))
        items = rows["items"]
        interest_repo.upsert_scan(
            staff_id=staff_id, session_id=session_id, relationship_key=rel,
            staff_name=cand.get("staff_name"), target_id=cand.get("target_id"),
            target_name=cand.get("target_name"), interests=items, summary=out["summary"],
            taxonomy_version=str(taxonomy.get("taxonomy_version") or ""),
            prompt_version=interest.PROMPT_VERSION, model_name=out.get("model_name") or "",
            msg_count=cand.get("msg_count") or 0, last_msg_time=cand.get("last_msg_time") or 0)
        self._bump(run_id, interest_points=len(items), dropped_points=rows["dropped"])
        return "analyzed"


def _llm_ready() -> bool:
    import config
    return bool(config.LLM_API_KEY and config.LLM_BASE_URL)


def _default_temperature() -> float:
    import analyze
    return analyze.DEFAULT_TEMPERATURE


service = InterestService()
