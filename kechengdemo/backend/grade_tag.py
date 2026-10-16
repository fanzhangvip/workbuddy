# -*- coding: utf-8 -*-
"""D2-1：把「意向等级」写进微伴标签 —— **与分数回写完全独立的一条链路**。

## 为什么要单独一条链路

| 维度 | 分数回写 | 等级标签（本模块） |
|---|---|---|
| 微伴语义 | **只能累加、无覆盖** | **有增有删 → 天然覆盖** |
| 表达方式 | 差值累加（`writeback_ledger`） | `batch_update` add 新 + remove 旧 |
| 开关 | `score_writeback_mode` | **`grade_tag_mode`**（独立） |
| 水位 | 客户累计已写分数 | 客户当前已写等级 |
| 幂等键 | 一个评估写一次 | **同一客户只认最新等级** |

两条链路**可以分别开关**：可以「只记分、不打标」，也可以「只打标、不记分」。
⚠️ 因此本模块**绝不复用** `outbox.py` / `writeback_ledger` ——
`outbox._record()` 会写 `score_writeback` 并推进分数水位，标签任务混进去
会被当成一次分数回写 → **差值累加被污染（客户分数虚高）**。

## 三档开关（`grade_tag_mode`）

| 值 | 行为 |
|---|---|
| `off`（**默认**） | 评估时**不入队**，drain 遇到待办则**延后保持 pending**（暂停而非作废） |
| `dry_run` | 记流水（`status='dry_run'`）但**一次接口都不调** |
| `live` | 真调 `tag/batch_update` |

**默认 `off` 是刻意的**：标签会真的改客户身上的数据，属于「写外部系统」的副作用，
必须由人显式打开 —— 不能因为「代码写好了」就默认开始改微伴。

## 顺序安全（本模块最容易出错的地方）

同一客户可能被连续评分多次（A → S → A）。若 outbox 按任意顺序投递，
**旧任务后到会把标签写回旧等级**。故 drain 时：
**同一 `relationship_key` 只处理 `outbox_id` 最大的那条**，更早的标 `superseded`。
"""
import time

import db
import http_client                  # 全项目唯一的退避实现（Reuse Before Rewrite）

#: 标签组名（级别标签都挂在这个组下；可用设置覆盖）
DEFAULT_TAG_GROUP = "意向等级"
#: 允许的等级
GRADES = ("S", "A", "B", "C", "D")
#: 标签 id 缓存键（存在 `app_setting`）
TAG_IDS_KEY = "grade_tag_ids"
#: 建标签载体配置键（专用测试客户），格式 `staff_id:external_user_id`
PROBE_KEY = "grade_tag_probe_customer"

BACKOFF_BASE = 30
BACKOFF_CAP = 3600
DEFAULT_MAX_ATTEMPTS = 8
STUCK_SECONDS = 600
OFF_DEFER_SECONDS = 600


# ============================================================ 配置

def tag_group() -> str:
    import scoring_service as ss
    return (db.setting("grade_tag_group", "GRADE_TAG_GROUP", DEFAULT_TAG_GROUP)
            or DEFAULT_TAG_GROUP).strip()


def mode() -> str:
    """实时读开关（`off` / `dry_run` / `live`）。**与分数回写开关无关。**"""
    import scoring_service as ss
    m = str(db.setting("grade_tag_mode", "GRADE_TAG_MODE", "off") or "off").strip().lower()
    return m if m in ("off", "dry_run", "live") else "off"


def enabled() -> bool:
    return mode() != "off"


def get_tag_ids() -> dict:
    """已缓存的「等级 → tag_id」映射（未同步过则为空）。"""
    raw = db.get_setting(TAG_IDS_KEY)
    if not raw:
        return {}
    try:
        import json
        d = json.loads(raw) if isinstance(raw, str) else raw
        return {str(k).upper(): v for k, v in (d or {}).items() if v}
    except Exception:
        return {}


# ============================================================ 标签同步（人工触发）

def sync_tags(create_missing: bool = False, dry_run: bool = True) -> dict:
    """扫描微伴标签库，找出「意向等级」组下的等级标签并缓存其 `tag_id`。

    ``create_missing=True`` 时，对缺失的等级调 `tag/add` **创建标签**
    （⚠️ 真副作用：会在微伴后台新建标签，故默认 `dry_run=True` 只报告不创建）。

    为什么不让评分链路自动创建：`tag/add` 的「不存在则自动创建」意味着
    一次拼写错误就会在客户后台留下一批垃圾标签组。**建标签必须是人工动作。**
    """
    import weiban
    grp = tag_group()
    found, all_groups = {}, set()
    try:
        for t in weiban.iter_all_tags():
            g = str(t.get("tag_group") or "")
            all_groups.add(g)
            if g == grp:
                name = str(t.get("tag_name") or "").strip().upper()
                if name in GRADES and t.get("tag_id"):
                    found[name] = t["tag_id"]
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "reason": f"{type(e).__name__}: {e}", "group": grp}

    missing = [g for g in GRADES if g not in found]
    created, errors = [], []
    if missing and create_missing and not dry_run:
        # ⚠️ `tag/add` 会把标签**真的打到某个客户身上** → 必须由人工指定**专用载体**，
        # 绝不自动挑一个真实客户当「白手套」（旧实现从 chat_session 取 msg_count 最大的客户）。
        probe = _probe_ids()
        if not probe:
            return {"ok": False, "group": grp, "found": found, "missing": missing,
                    "created": [], "errors": [],
                    "reason": ("未配置建标签载体：请先在设置 %s 填入 "
                               "`staff_id:external_user_id`（一个专用测试客户），"
                               "或直接在微伴后台人工建好「%s」标签组后重新同步"
                               % (PROBE_KEY, grp))}
        probe_staff, probe_customer = probe
        # 先确保组里有至少一个标签：`tag/add` 会在组不存在时自动建组
        for g in missing:
            try:
                res = weiban.add_customer_tag(
                    staff_id=probe_staff,
                    external_user_id=probe_customer,
                    tag_group=grp, tag_name=g)
                if res.get("errcode") == 0:
                    created.append(g)
                else:
                    errors.append({"grade": g, "errcode": res.get("errcode"),
                                   "errmsg": res.get("errmsg")})
            except Exception as e:               # noqa: BLE001
                errors.append({"grade": g, "error": f"{type(e).__name__}: {e}"})
        if created:                              # 建完再扫一次，拿回真正的 tag_id
            return sync_tags(create_missing=False, dry_run=False)

    if found and not dry_run:
        db.set_setting(TAG_IDS_KEY, __import__("json").dumps(found, ensure_ascii=False))
    return {"ok": True, "group": grp, "found": found, "missing": missing,
            "created": created, "errors": errors, "dry_run": bool(dry_run),
            "cached": not dry_run and bool(found),
            "groups_seen": sorted(g for g in all_groups if g)[:20]}


def _probe_ids() -> tuple | None:
    """建标签所需的载体 (staff_id, external_user_id) —— **必须人工配置**。

    历史实现从 `chat_session` 里挑「消息最多的真实客户」当载体，
    结果是把等级标签**实际打到无关的真实客户身上**（污染客户数据）。
    现在改为读设置 `grade_tag_probe_customer`（格式 ``staff_id:external_user_id``），
    未配置则**拒绝执行**，提示人工在微伴后台建标签。
    """
    raw = str(db.get_setting(PROBE_KEY) or "").strip()
    if not raw or ":" not in raw:
        return None
    staff_id, ext_id = raw.split(":", 1)
    staff_id, ext_id = staff_id.strip(), ext_id.strip()
    if not staff_id or not ext_id:
        return None
    return staff_id, ext_id


# ============================================================ 客户当前已写等级（水位）

def ledger_state(relationship_key: str) -> dict:
    """该客户**已写入**的等级与标签（无记录 → 空 dict）。"""
    try:
        return db.query_one(
            "SELECT grade, tag_id FROM grade_tag_ledger WHERE relationship_key=%s",
            (relationship_key,)) or {}
    except Exception:
        return {}


def _advance_ledger(cur, row) -> None:
    """投递成功后推进客户级等级水位（**复用调用方 cursor，与记账同事务**）。

    同事务是硬要求：否则出现「流水说写成功了、水位没动」→ 下次重评会以为
    「等级没变」而跳过，标签永久停在旧值。
    """
    cur.execute(
        """INSERT INTO grade_tag_ledger
             (relationship_key, staff_id, external_user_id, grade, tag_id,
              last_assessment_id, last_write_at, writes)
           VALUES (%s,%s,%s,%s,%s,%s,now(),1)
           ON CONFLICT (relationship_key) DO UPDATE
             SET grade = EXCLUDED.grade,
                 tag_id = EXCLUDED.tag_id,
                 last_assessment_id = EXCLUDED.last_assessment_id,
                 last_write_at = now(),
                 writes = grade_tag_ledger.writes + 1,
                 updated_at = now()""",
        (row["relationship_key"], row["staff_id"], row["external_user_id"],
         row["grade"], row.get("add_tag_id"), row["assessment_id"]))


# ============================================================ 入队（调用方事务内）

def enqueue(conn, assessment_id, relationship_key, staff_id, external_user_id,
            grade, mode=None, remove_tag_id=None) -> bool:
    """在**调用方事务**内入队（与 assessment 的 INSERT 同事务 → 不会「评了但没打标」）。

    ``remove_tag_id`` 在**入队时固化**（= 当时水位里的旧标签）：出队重试时不能再算一次
    —— 期间水位可能已被别的任务推进，重算会得到不同的值。

    ⚠️ **开关为 `off` 时直接不入队**（返回 False）。理由：`off` 的语义是
    「**完全不动微伴**」——不仅不调用，连待办都不该产生。把这条不变式放在函数内部
    （而不是只靠调用方检查），是为了让「谁调的都不例外」。
    """
    if not enabled():
        return False
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO grade_tag_outbox
                 (assessment_id, relationship_key, staff_id, external_user_id,
                  grade, remove_tag_id, mode, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,'pending')
               ON CONFLICT (assessment_id) DO NOTHING""",
            (assessment_id, relationship_key, staff_id, external_user_id,
             str(grade).strip().upper(), remove_tag_id, mode or "live"))
        return cur.rowcount > 0


# ============================================================ 排空（drain worker）

def _delay(attempts: int) -> int:
    """第 attempts 次失败后的等待秒数 —— 统一走 `http_client.backoff_seconds()`。

    原先本函数与 `outbox._delay()` 是**逐字相同的两份**；等级标签与分数回写走的是
    **同一条微伴通道**，两边口径必须一致（只抖一边，另一边的重试尖峰照样打到微伴）。
    """
    return max(1, int(http_client.backoff_seconds(
        attempts - 1, BACKOFF_BASE, cap=BACKOFF_CAP)))


def _claim(conn, limit: int) -> tuple:
    """抢占到期任务 → ``(rows, superseded_count)``。

    ① 回收「投递中断」（卡在 sending 超时）的行
    ② **同一客户只保留最新一条**：更早的标 `superseded`
       —— 否则乱序投递会把标签写回旧等级（A→S→A 的经典竞态）
    ③ 标记 sending
    """
    db.reclaim_stuck_sending(conn, "grade_tag_outbox", STUCK_SECONDS,
                             " [投递中断，已回收]")
    cur = conn.execute(
        """UPDATE grade_tag_outbox o
              SET status='superseded', updated_at=now(),
                  last_errmsg='同一客户已有更新的等级任务，本条作废'
            WHERE o.status IN ('pending','failed')
              AND EXISTS (SELECT 1 FROM grade_tag_outbox n
                           WHERE n.relationship_key = o.relationship_key
                             AND n.outbox_id > o.outbox_id
                             AND n.status IN ('pending','failed','sending'))""")
    superseded = int(cur.rowcount or 0)
    with conn.cursor() as c:
        c.execute(
            """SELECT outbox_id, assessment_id, relationship_key, staff_id,
                      external_user_id, grade, remove_tag_id, attempts, max_attempts
                 FROM grade_tag_outbox
                WHERE status IN ('pending','failed') AND next_attempt_at <= now()
                ORDER BY next_attempt_at ASC, outbox_id ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED""",
            (limit,))
        rows = c.fetchall()
        for r in rows:
            c.execute("UPDATE grade_tag_outbox SET status='sending', updated_at=now() "
                      "WHERE outbox_id=%s", (r["outbox_id"],))
    return rows, superseded


def _finish(conn, row, status, *, terminal, ok=False, attempts=0, add_tag_id=None,
            errcode=None, errmsg=None, resp=None) -> None:
    """一次投递结束。

    ``terminal=True`` → 落终态（`done` / `dry_run` / `skipped` / `unchanged`），
    **不再重排**；``terminal=False`` → 落 `failed` / `dead` 并按退避重排。

    ⚠️ 这个 `terminal` 参数是必须的：早先的实现把「非 ok」一律写成 `failed` 并重排，
    于是 `dry_run` / `skipped` / `grade_unchanged` 会被**反复捞起来重试**
    （每次都不该投递、每次都重新入队）—— 既刷日志又让 outbox 永不排空。
    """
    with conn.cursor() as cur:
        j = db.as_json(resp) if resp is not None else None
        if terminal:
            cur.execute(
                """UPDATE grade_tag_outbox
                      SET status=%s, attempts=%s, last_errcode=%s, last_errmsg=%s,
                          last_response=%s, updated_at=now()
                    WHERE outbox_id=%s""",
                (status, attempts, errcode, errmsg, j, row["outbox_id"]))
            if ok:                             # 只有**真投递成功**才推进等级水位
                r = dict(row)
                r["add_tag_id"] = add_tag_id
                _advance_ledger(cur, r)
            return
        cur.execute(
            """UPDATE grade_tag_outbox
                  SET status=%s, attempts=%s, last_errcode=%s, last_errmsg=%s,
                      last_response=%s,
                      next_attempt_at = now() + make_interval(secs => %s),
                      updated_at=now()
                WHERE outbox_id=%s""",
            (status, attempts, errcode, errmsg, j, _delay(attempts), row["outbox_id"]))


def _defer(conn, row, seconds: int, note: str) -> None:
    conn.execute(
        """UPDATE grade_tag_outbox
              SET status='pending', last_errmsg=%s,
                  next_attempt_at = now() + make_interval(secs => %s), updated_at=now()
            WHERE outbox_id=%s""",
        (note, int(seconds), row["outbox_id"]))


def drain(limit: int = 50) -> dict:
    """排空到期任务。**实时**读取开关（关掉即暂停，不丢任务）。"""
    import hooks
    import weiban

    stats = {"checked": 0, "ok": 0, "failed": 0, "dry_run": 0, "skipped": 0,
             "dead": 0, "deferred": 0, "superseded": 0}
    m = mode()
    ids = get_tag_ids()

    with db.tx() as conn:
        rows, superseded = _claim(conn, limit)
    stats["superseded"] = superseded

    for row in rows:
        stats["checked"] += 1
        tag_id = ids.get(str(row["grade"]).upper())

        # --- 闸门：开关与水位在**排空时刻**实时判定（纯函数 hook）---
        decision = hooks.before_grade_tag(
            mode=m, grade=row["grade"], tag_id=tag_id,
            current_grade=(ledger_state(row["relationship_key"]) or {}).get("grade"))
        attempts = int(row["attempts"]) + 1

        if m == "off":
            with db.tx() as conn:
                _defer(conn, row, OFF_DEFER_SECONDS, "等级标签回写已关闭，暂缓投递")
            stats["deferred"] += 1
            continue
        if decision.denied:
            reason = decision.reason
            if reason == "tag_not_ready":
                # 标签 id 还没同步 → **延后**（不是失败：等人工 sync 后自然恢复）
                with db.tx() as conn:
                    _defer(conn, row, OFF_DEFER_SECONDS, "等级标签 id 未就绪，暂缓投递")
                stats["deferred"] += 1
                continue
            # 终态（skipped / unchanged）：**不重排**，否则会被反复捞起来
            with db.tx() as conn:
                _finish(conn, row, hooks.REASON_TO_TAG_STATUS.get(reason, "skipped"),
                        terminal=True, attempts=int(row["attempts"]),
                        errmsg=hooks.REASON_CN.get(reason, reason))
            stats["skipped"] += 1
            continue
        if m == "dry_run":
            with db.tx() as conn:
                _finish(conn, row, "dry_run", terminal=True,
                        attempts=int(row["attempts"]), errmsg="演练模式：仅记录，未写标签")
            stats["dry_run"] += 1
            continue

        # --- live：真实投递（网络调用在事务外）---
        errcode = errmsg = None
        resp = None
        ok = False
        try:
            add = [tag_id]
            rm = [row["remove_tag_id"]] if row["remove_tag_id"] and \
                row["remove_tag_id"] != tag_id else []
            resp = weiban.batch_update_customer_tags(
                row["staff_id"], external_user_id=row["external_user_id"],
                add_tag_id_list=add, remove_tag_id_list=rm)
            errcode = resp.get("errcode")
            errmsg = resp.get("errmsg")
            ok = errcode == 0
        except Exception as e:                   # noqa: BLE001
            errmsg = f"{type(e).__name__}: {e}"
        dead = (not ok) and attempts >= int(row["max_attempts"])

        with db.tx() as conn:
            _finish(conn, row, "done" if ok else ("dead" if dead else "failed"),
                    terminal=ok, ok=ok, attempts=attempts, add_tag_id=tag_id,
                    errcode=errcode, errmsg=errmsg, resp=resp)
        if ok:
            stats["ok"] += 1
        else:
            stats["failed"] += 1
            if dead:
                stats["dead"] += 1

    return stats


# ============================================================ 人工对齐（一次性/批量）

def align(relationship_keys: list = None, limit: int = 0, dry_run: bool = True,
          remove_all_others: bool = False) -> dict:
    """**人工触发**：把「当前等级」对齐到微伴标签（用于补历史 / 修正漂移）。

    ## ⚠️ 必须显式指定范围（安全闸）

    ``relationship_keys`` 与 ``limit`` **至少要给一个**。都不给则**拒绝执行** ——
    否则这个函数会「把库里每个客户的当前等级都写一遍」，而它们**全是真实客户**：
    一次误点就等于给全量客户改标签。**批量写外部系统必须是有意识的动作。**

    其余参数：
    - `dry_run=True`（默认）：只报告会改哪些，**不调接口**
    - `remove_all_others=True`：把「其它 4 个等级标签」一并 remove
      （**修正人工在微伴后台改过标签导致的漂移**），并**强制重写**（即使等级未变）
    """
    import weiban

    m = mode()
    if not enabled():
        return {"ok": False, "reason": f"等级标签回写已关闭（grade_tag_mode={m}）",
                "mode": m}
    if not relationship_keys and not limit:
        return {"ok": False,
                "reason": "拒绝执行：必须显式指定范围（relationship_keys 或 limit>0）。"
                          "不设范围 = 给全库每个真实客户改标签，属高危操作。",
                "hint": "先 dry_run 看影响面，确认后再放开"}
    ids = get_tag_ids()
    if not ids:
        return {"ok": False, "reason": "等级标签 id 未就绪，请先在设置里「同步等级标签」"}

    cond, params = ["a.is_current"], {}
    if relationship_keys:
        cond.append("a.relationship_key = ANY(%(rels)s)")
        params["rels"] = list(relationship_keys)
    sql = (f"""SELECT a.relationship_key, a.staff_id, a.target_id AS external_user_id,
                      a.grade, a.assessment_id
                 FROM customer_intent_assessment a WHERE {' AND '.join(cond)}
                ORDER BY a.assessment_id DESC""")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = db.query(sql, params)

    plan, changed = [], 0
    for r in rows:
        grade = str(r["grade"] or "").strip().upper()
        led = ledger_state(r["relationship_key"])
        add = ids.get(grade)
        if not add:
            plan.append({"rel": r["relationship_key"], "grade": grade,
                         "skip": "无该等级的标签 id"})
            continue
        if led.get("grade") == grade and not remove_all_others:
            plan.append({"rel": r["relationship_key"], "grade": grade,
                         "skip": "等级未变"})
            continue
        if remove_all_others:
            rm = [v for k, v in ids.items() if k != grade]
        else:
            rm = [led["tag_id"]] if led.get("tag_id") and led["tag_id"] != add else []
        plan.append({"rel": r["relationship_key"], "grade": grade,
                     "from": led.get("grade"), "add": [add], "remove": rm})
        changed += 1
        if dry_run:
            continue
        try:
            resp = weiban.batch_update_customer_tags(
                r["staff_id"], external_user_id=r["external_user_id"],
                add_tag_id_list=[add], remove_tag_id_list=rm)
            if resp.get("errcode") == 0:
                with db.tx() as conn:
                    _advance_ledger(conn, {"relationship_key": r["relationship_key"],
                                           "staff_id": r["staff_id"],
                                           "external_user_id": r["external_user_id"],
                                           "grade": grade, "add_tag_id": add,
                                           "assessment_id": r["assessment_id"]})
        except Exception as e:                   # noqa: BLE001
            plan[-1]["error"] = f"{type(e).__name__}: {e}"

    return {"ok": True, "mode": m, "dry_run": bool(dry_run),
            "scanned": len(rows), "to_change": changed,
            "remove_all_others": bool(remove_all_others),
            "plan": plan[:50]}


def stats() -> dict:
    """标签回写观测（供前端/排障）。"""
    return {
        "mode": mode(),
        "tag_group": tag_group(),
        "tag_ids": get_tag_ids(),
        "outbox": db.query(
            "SELECT status, count(*) AS n FROM grade_tag_outbox GROUP BY status ORDER BY status"),
        "ledger_rows": (db.query_one("SELECT count(*) AS n FROM grade_tag_ledger") or {}).get("n", 0),
        "errors": db.query(
            """SELECT outbox_id, relationship_key, grade, attempts, last_errcode, last_errmsg
                 FROM grade_tag_outbox
                WHERE status IN ('failed','dead')
                ORDER BY outbox_id DESC LIMIT 10"""),
    }
