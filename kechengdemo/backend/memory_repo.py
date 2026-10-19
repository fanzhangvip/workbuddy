# -*- coding: utf-8 -*-
"""`customer_memory`（客户级稳定事实）的**唯一写入口** + 供上下文/界面读取的查询。

## 它解决什么问题

`profile_struct` 是**每次评估一份**的快照：这次聊到"在成都"，下次没聊到，快照里就是
"未提及" —— 模型每轮都要重新归纳，而"客户从北京搬到上海"这种变化也没人记得。
本表把 8 个固定槽位归并成**每客户一条 active 事实**，历史值留成 `superseded`。

## 三条铁律（改代码前先读）

1. **记忆不是证据**：装配时用【】段标、**不带 `#编号`**，不进 `index` ——
   模型无法把记忆当 `evidence_ids` 回链（与决议 M-C 一致）。
2. **人工值不被模型覆盖**：`source='human'` 的行只刷新 `last_seen`；
   要改它必须再走人工接口。
3. **"未提及"不是"没有"**：本次评估某槽位为"未提及"时**什么都不做**（不删、不改），
   由读取侧标注"上次会话未提及" —— 记忆只会在 TTL 到期或人工拒绝时失效。

## 与 `assessment_repo` 的分工

`assessment_repo` 管结论（每次一份、`is_current` 唯一）；本模块管**跨次事实**
（每客户每槽位一条 active）。写入发生在 `_persist_assessment()` 的**同一事务**里
（传 `conn`），保证"结论有了、记忆没写"不会发生。
"""
import unicodedata

import contracts
import db

#: 固定槽位与中文名（与模型契约共用一份定义，避免两处漂移）
SLOTS = contracts.PROFILE_SLOTS
SLOT_CN = contracts.PROFILE_SLOT_CN

#: 模型用这个词表示"本次没提到" —— 它不是事实，绝不能覆盖已有记忆
UNKNOWN = "未提及"

#: 每条事实保留多少条证据 id（再多也没用，展示只取第一条原话）
MAX_EVIDENCE = 5


def norm(value) -> str:
    """归一化：NFKC（全角→半角）+ 折叠空白 + 去首尾标点。

    只用来判"是不是同一件事"。**不**改写入库的原值 —— 客户的原话要保留原样。
    """
    s = unicodedata.normalize("NFKC", str(value or "")).strip()
    s = " ".join(s.split())
    return s.strip(" \t\r\n。.,，；;：:！!？?、\"'“”‘’()（）[]【】")


def _clean(v) -> str:
    """入库前的清洗：只折叠空白，**不做 NFKC** —— 全角括号/引号是中文原文的一部分，
    归一化只用于比较（`norm`），不该改写客户原话的排版。"""
    return " ".join(str(v or "").split()).strip()


def ttl_days() -> int:
    return max(1, db.setting_int("memory_ttl_days", "MEMORY_TTL_DAYS", 180))


# ============================================================ 写（结论同事务）

def apply_in(conn, *, rel: str, profile_struct: list, assessment_id: int,
             actor: str = "system", ttl: int = None) -> dict:
    """把一次评估的结构化画像**归并**进记忆（必须在写结论的同一事务里调用）。

    返回 ``{"written": n, "kept": n, "updated": n}``（观测/测试用）。

    规则（顺序即优先级）：
      ① 槽位值空 / "未提及" -> **跳过**（不动记忆）；
      ② 已有 active 且 ``source='human'`` -> 只刷新 ``last_seen``；
      ③ 归一后相同 -> 刷新 ``last_seen``、提高 confidence、补证据；
      ④ 归一后不同 -> 旧行 ``superseded``，插入新行（version+1）；
      ⑤ 没有 -> 插入 version=1。
    """
    ttl = int(ttl or ttl_days())
    out = {"written": 0, "kept": 0, "updated": 0}
    for rec in (profile_struct or []):
        slot = str(rec.get("slot") or "").strip()
        if slot not in SLOTS:
            continue
        value = _clean(rec.get("value"))
        if not value or value == UNKNOWN:
            continue                      # 铁律 3：本次没提 != 删除
        vnorm = norm(value)
        ev_ids = [str(x) for x in (rec.get("evidence_message_ids") or [])][:MAX_EVIDENCE]
        ev_text = (rec.get("evidence") or [""])[0]
        ev_text = (str(ev_text) or "")[:200] or None
        conf = float(rec.get("confidence") or 0.0) or (0.8 if ev_ids else 0.4)

        cur = conn.execute(
            """SELECT memory_id, value, value_norm, source, confidence, evidence_message_ids
                 FROM customer_memory
                WHERE relationship_key=%s AND slot=%s AND status='active'
                FOR UPDATE""", (rel, slot)).fetchone()

        if cur:
            if cur["source"] == "human":
                conn.execute(
                    """UPDATE customer_memory SET last_seen=now(), updated_at=now()
                        WHERE memory_id=%s""", (cur["memory_id"],))
                out["kept"] += 1
                continue
            if (cur["value_norm"] or norm(cur["value"])) == vnorm:
                ids = list(cur["evidence_message_ids"] or [])
                ids += [x for x in ev_ids if x not in ids]
                conn.execute(
                    """UPDATE customer_memory
                          SET last_seen=now(), updated_at=now(),
                              confidence=GREATEST(confidence, %s),
                              evidence_message_ids=%s,
                              evidence_text=COALESCE(evidence_text, %s)
                        WHERE memory_id=%s""",
                    (conf, ids[:MAX_EVIDENCE], ev_text, cur["memory_id"]))
                out["updated"] += 1
                continue
            conn.execute(
                """UPDATE customer_memory SET status='superseded', updated_at=now()
                    WHERE memory_id=%s""", (cur["memory_id"],))

        conn.execute(
            """INSERT INTO customer_memory
                 (relationship_key, slot, value, value_norm, confidence, source,
                  evidence_message_ids, evidence_text, status, expires_at,
                  version, updated_by)
               VALUES (%s,%s,%s,%s,%s,'llm',%s,%s,'active',
                       now() + make_interval(days => %s),
                       COALESCE((SELECT max(version)+1 FROM customer_memory
                                  WHERE relationship_key=%s AND slot=%s), 1), %s)""",
            (rel, slot, value, vnorm, conf, ev_ids, ev_text, ttl, rel, slot, actor))
        out["written"] += 1

    if any(out.values()):
        _audit_in(conn, actor, rel, "memory_apply",
                  {"assessment_id": assessment_id, **out})
    return out


def set_manual(*, rel: str, slot: str, value: str, note: str = "",
               actor: str = "manual", ttl: int = None) -> dict:
    """人工改判：写一条 ``source='human'`` 的 active 记忆（旧值转 superseded）。"""
    slot = str(slot or "").strip()
    if slot not in SLOTS:
        return {"ok": False, "reason": f"slot 必须是 {'/'.join(SLOTS)} 之一"}
    val = _clean(value)
    if not val:
        return {"ok": False, "reason": "value 不能为空"}
    if len(val) > 200:
        return {"ok": False, "reason": "value 太长（≤200 字）"}
    ttl = int(ttl or ttl_days())
    with db.tx() as conn:
        old = conn.execute(
            """SELECT memory_id, value FROM customer_memory
                WHERE relationship_key=%s AND slot=%s AND status='active' FOR UPDATE""",
            (rel, slot)).fetchone()
        if old:
            conn.execute(
                """UPDATE customer_memory SET status='superseded', updated_at=now()
                    WHERE memory_id=%s""", (old["memory_id"],))
        row = conn.execute(
            """INSERT INTO customer_memory
                 (relationship_key, slot, value, value_norm, confidence, source,
                  status, expires_at, version, updated_by)
               VALUES (%s,%s,%s,%s,1.0,'human','active',
                       now() + make_interval(days => %s),
                       COALESCE((SELECT max(version)+1 FROM customer_memory
                                  WHERE relationship_key=%s AND slot=%s), 1), %s)
               RETURNING memory_id""",
            (rel, slot, val, norm(val), ttl, rel, slot, actor)).fetchone()
        _audit_in(conn, actor, rel, "memory_manual",
                  {"slot": slot, "old": (old or {}).get("value"), "new": val,
                   "note": (note or "")[:200]})
    return {"ok": True, "memory_id": row["memory_id"], "slot": slot, "value": val}


def reject(*, memory_id: int, actor: str = "manual") -> dict:
    """拒绝一条记忆：置 ``rejected``，并**回退到上一条 superseded**（如果有）。"""
    with db.tx() as conn:
        row = conn.execute(
            """SELECT memory_id, relationship_key, slot, value, source
                 FROM customer_memory WHERE memory_id=%s FOR UPDATE""",
            (memory_id,)).fetchone()
        if not row:
            return {"ok": False, "reason": "记忆不存在"}
        conn.execute(
            """UPDATE customer_memory SET status='rejected', updated_at=now()
                WHERE memory_id=%s""", (memory_id,))
        prev = conn.execute(
            """SELECT memory_id FROM customer_memory
                WHERE relationship_key=%s AND slot=%s AND status='superseded'
                ORDER BY version DESC, memory_id DESC LIMIT 1""",
            (row["relationship_key"], row["slot"])).fetchone()
        restored = None
        if prev:
            conn.execute(
                """UPDATE customer_memory
                      SET status='active', last_seen=now(), updated_at=now()
                    WHERE memory_id=%s""", (prev["memory_id"],))
            restored = prev["memory_id"]
        _audit_in(conn, actor, row["relationship_key"], "memory_reject",
                  {"slot": row["slot"], "value": row["value"], "restored": restored})
    return {"ok": True, "rejected": memory_id, "restored": restored}


def _audit_in(conn, actor, rel, action, detail) -> None:
    """审计与写入**同事务**：记忆改了却查不到谁改的，比没改更糟。"""
    import audit
    audit.record_action_in(conn, actor, action, rel, detail)


# ============================================================ 读

def active_rows(rel: str) -> list:
    """该客户当前有效（未过期）的记忆，按固定槽位顺序返回。"""
    rows = db.query(
        """SELECT memory_id, slot, value, confidence, source, evidence_message_ids,
                  evidence_text, last_seen, expires_at, version
             FROM customer_memory
            WHERE relationship_key=%s AND status='active'
              AND (expires_at IS NULL OR expires_at > now())""", (rel,))
    order = {s: i for i, s in enumerate(SLOTS)}
    for r in rows:
        r["slot_cn"] = SLOT_CN.get(r["slot"], r["slot"])
    return sorted(rows, key=lambda r: order.get(r["slot"], 99))


def list_rows(rel: str, include_history: bool = False) -> list:
    """界面用：默认只给 active；``include_history=True`` 时连历史一起给。"""
    cond = "" if include_history else "AND status='active'"
    rows = db.query(
        f"""SELECT memory_id, relationship_key, slot, value, confidence, source,
                   evidence_message_ids, evidence_text, status, first_seen, last_seen,
                   expires_at, version, updated_by, updated_at
              FROM customer_memory
             WHERE relationship_key=%s {cond}
             ORDER BY slot, version DESC, memory_id DESC""", (rel,))
    order = {s: i for i, s in enumerate(SLOTS)}
    for r in rows:
        r["slot_cn"] = SLOT_CN.get(r["slot"], r["slot"])
    return sorted(rows, key=lambda r: (order.get(r["slot"], 99),
                                       -int(r.get("version") or 0)))


def stats() -> dict:
    return db.query_one(
        """SELECT count(*) FILTER (WHERE status='active') AS active,
                  count(DISTINCT relationship_key) FILTER (WHERE status='active') AS customers,
                  count(*) AS total
             FROM customer_memory""") or {}


# ============================================================ 维护

def expire_due() -> int:
    """把到期但仍标 active 的行标成 expired（读取侧已惰性过滤，这里只为清账）。"""
    return db.execute(
        """UPDATE customer_memory SET status='expired', updated_at=now()
            WHERE status='active' AND expires_at IS NOT NULL AND expires_at <= now()""")


def backfill(*, dry_run: bool = True, ttl: int = None) -> dict:
    """从每个客户**最新一条**评估的 ``profile_struct`` 建初始记忆（一次性迁移）。

    只处理"还没有任何 active 记忆"的客户：重复执行不会覆盖人工改过的值。
    """
    rows = db.query(
        """SELECT DISTINCT ON (a.relationship_key)
                  a.relationship_key, a.assessment_id, a.profile_struct
             FROM customer_intent_assessment a
            WHERE a.profile_struct IS NOT NULL AND a.profile_struct <> '[]'::jsonb
              AND NOT EXISTS (SELECT 1 FROM customer_memory m
                               WHERE m.relationship_key = a.relationship_key
                                 AND m.status = 'active')
            ORDER BY a.relationship_key, a.assessment_id DESC""")
    out = {"customers": len(rows), "written": 0, "dry_run": bool(dry_run)}
    if dry_run:
        return out
    ttl = int(ttl or ttl_days())
    for r in rows:
        with db.tx() as conn:
            res = apply_in(conn, rel=r["relationship_key"],
                           profile_struct=db.as_list(r["profile_struct"]),
                           assessment_id=r["assessment_id"], actor="backfill", ttl=ttl)
        out["written"] += res["written"]
    return out
