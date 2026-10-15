# -*- coding: utf-8 -*-
"""评估集接口（架构 第五章 E5 / 闸6）。

定位：**回答「判得准不准」**。在这之前全库没有任何东西能回答这个问题，
改规则/调提示词只能凭感觉。本模块提供：抽样 → 人工标注 → 跑分对比 → 门禁 → 导出。

标注口径（**务必保持**）：只标「命中了哪些 rule_id + 哪个档位」，**不标总分** ——
总分由 `rules.py` 算。若让人标总分，等于让人工重新实现一遍规则引擎（架构 §5.1 原文）。
"""
import json

from fastapi import APIRouter, Body
from fastapi.responses import Response

import db
import eval_runner
import meta_loop
import rules as rules_mod
import scoring_service as ss

router = APIRouter(prefix="/api/eval", tags=["eval"])


# ---------------- 统计 ----------------

@router.get("/stats")
def stats():
    row = db.query_one("""
        SELECT
          (SELECT count(*) FROM eval_case)                                   AS total,
          (SELECT count(*) FROM eval_case WHERE expected_items IS NOT NULL)  AS labeled,
          (SELECT count(*) FROM eval_case WHERE expected_items IS NULL)      AS unlabeled,
          (SELECT count(*) FROM eval_case WHERE prefilled)                   AS prefilled,
          (SELECT count(*) FROM eval_case
             WHERE expected_items IS NOT NULL AND NOT prefilled)             AS confirmed,
          (SELECT count(*) FROM eval_case WHERE frozen)                      AS frozen,
          (SELECT count(*) FROM eval_case WHERE disputed)                    AS disputed,
          (SELECT count(*) FROM eval_case WHERE kind='golden')               AS golden,
          (SELECT count(*) FROM eval_case WHERE kind='regression')           AS regression,
          (SELECT count(*) FROM eval_case WHERE kind='edge')                 AS edge
    """)
    row["baseline"] = eval_runner.load_baseline()
    row["last_run"] = db.query_one("SELECT * FROM eval_run ORDER BY id DESC LIMIT 1")
    row["running"] = db.query_one(
        "SELECT count(*) AS n FROM eval_run WHERE status='running'")["n"]
    row["rule_version"] = rules_mod.load_rules().get("rule_version")
    return row


# ---------------- 列表 ----------------

@router.get("/list")
def list_cases(kind: str = "", status: str = "", q: str = "",
               page: int = 1, page_size: int = 20):
    """status: all / labeled / unlabeled / frozen / disputed / drift / prefilled / confirmed"""
    cond, params = ["1=1"], {}
    if kind:
        cond.append("kind = %(kind)s"); params["kind"] = kind
    if status == "labeled":
        cond.append("expected_items IS NOT NULL")
    elif status == "unlabeled":
        cond.append("expected_items IS NULL")
    elif status == "prefilled":
        # AI 预标注 → 还没人工复核过（这是复核队列真正要清的）
        cond.append("prefilled = true")
    elif status == "confirmed":
        # 人工已确认（预标注复核后保存会转 false）
        cond.append("expected_items IS NOT NULL AND NOT prefilled")
    elif status == "frozen":
        cond.append("frozen = true")
    elif status == "disputed":
        cond.append("disputed = true")
    if q:
        cond.append("(target_name ILIKE %(q)s OR staff_name ILIKE %(q)s OR session_id ILIKE %(q)s)")
        params["q"] = f"%{q}%"

    where = " AND ".join(cond)
    params["limit"], params["offset"] = page_size, (max(1, page) - 1) * page_size
    items = db.query(f"""
        SELECT case_id, kind, staff_id, session_id, target_id, staff_name, target_name,
               msg_count, last_msg_time, expected_items, expected_raw_score, expected_grade,
               baseline_raw_score, baseline_grade, baseline_rule_version, baseline_at,
               source, disputed, annotator, prefilled, frozen, note, created_at, updated_at
          FROM eval_case WHERE {where}
         ORDER BY prefilled DESC, (expected_items IS NULL) DESC, case_id DESC
         LIMIT %(limit)s OFFSET %(offset)s""", params)
    total = db.query_one(f"SELECT count(*) AS n FROM eval_case WHERE {where}",
                         {k: v for k, v in params.items() if k not in ("limit", "offset")})
    # 派生：标注与基线的差异（一眼看出"人标 vs 系统判"差在哪）
    for it in items:
        it["diff_from_baseline"] = _diff_label(it)
    return {"items": items, "total": (total or {}).get("n", 0), "page": page, "page_size": page_size}


def _diff_label(it: dict):
    if it.get("baseline_grade") is None and it.get("expected_grade") is None:
        return None
    return {"grade_changed": it.get("baseline_grade") != it.get("expected_grade"),
            "score_delta": ((it.get("expected_raw_score") or 0) - (it.get("baseline_raw_score") or 0))
            if it.get("baseline_raw_score") is not None else None}


# ---------------- 详情（含对话与规则清单）----------------

@router.get("/detail")
def detail(case_id: int, msg_limit: int = 100):
    c = db.query_one("SELECT * FROM eval_case WHERE case_id=%s", (case_id,))
    if not c:
        return {}
    conv = ""
    if c.get("staff_id") and c.get("session_id"):
        conv = ss.build_conversation(c["staff_id"], c["session_id"], limit=msg_limit)
    drift = None
    if c.get("messages_hash"):
        drift = eval_runner.messages_hash(c["staff_id"], c["session_id"]) != c["messages_hash"]
    return {"case": c, "conversation": conv, "drift": drift,
            "msg_limit": msg_limit, "rules": _rule_options()}


def _rule_options():
    """正 + 负**全部**规则（评估集要标两侧命中项，与复核页只列负分不同）。

    ⚠️ `rules.json` 里的规则**没有 `polarity` 字段** —— 极性是由「在
    `positive_rules` 还是 `negative_rules` 列表里」隐含表达的（`rules.validate_items`
    也是靠这个推导）。所以这里必须**按分组赋 polarity**，不能读 `r["polarity"]`。
    """
    rules = rules_mod.load_rules()
    out = []
    for grp, polarity in (("positive_rules", "positive"), ("negative_rules", "negative")):
        for r in rules.get(grp, []):
            out.append({"rule_id": r["rule_id"], "dimension": r["dimension"],
                        "polarity": polarity, "engine": r.get("engine"),
                        "hint": r.get("hint"),
                        "options": [{"score": o["score"], "criteria": o["criteria"]}
                                    for o in r.get("options", [])]})
    return {"rule_version": rules.get("rule_version"), "items": out}


@router.get("/options")
def options():
    return _rule_options()


# ---------------- 保存标注（金标准）----------------

@router.post("/save")
def save(payload: dict = Body(default={})):
    """保存人工标注。payload: {case_id, items:[{rule_id, score, reason}], note, annotator, disputed}"""
    cid = payload.get("case_id")
    c = db.query_one("SELECT * FROM eval_case WHERE case_id=%s", (cid,))
    if not c:
        return {"ok": False, "reason": "样本不存在"}
    if c.get("frozen") and not payload.get("arbitrate"):
        return {"ok": False, "reason": "该样本已冻结，除仲裁外不得修改"}

    rules = rules_mod.load_rules()
    valid, bad = rules_mod.validate_items(payload.get("items") or [], rules)
    pos = [i for i in valid if i["polarity"] == "positive"]
    neg = [i for i in valid if i["polarity"] == "negative"]
    score = rules_mod.compute_score(pos, neg, rules)
    grade = rules_mod.grade_of(score["raw_score"], rules)
    annotator = (payload.get("annotator") or "复核员").strip()

    db.execute(
        """UPDATE eval_case SET expected_items=%s, expected_raw_score=%s, expected_grade=%s,
               annotator=%s, note=%s, disputed=%s, prefilled=false, updated_at=now()
             WHERE case_id=%s""",
        (db.as_json(valid), score["raw_score"], grade["grade"], annotator,
         (payload.get("note") or "").strip(), bool(payload.get("disputed")), cid))
    db.execute("INSERT INTO audit_log (actor, action, target, detail) VALUES (%s,%s,%s,%s)",
               (annotator, "eval_annotate", str(cid),
                db.as_json({"items": valid, "raw_score": score["raw_score"],
                            "grade": grade["grade"], "invalid": bad})))
    return {"ok": True, "expected_raw_score": score["raw_score"], "expected_grade": grade["grade"],
            "positive_total": score["positive_total"], "negative_total": score["negative_total"],
            "invalid_items": bad}


# ---------------- 冻结 / 解冻 ----------------

@router.post("/freeze")
def freeze(payload: dict = Body(default={})):
    """金标准一经冻结，除仲裁外不得修改（架构 §5.3 第 4 条）。"""
    cid, frozen = payload.get("case_id"), bool(payload.get("frozen", True))
    n = db.execute("UPDATE eval_case SET frozen=%s, updated_at=now() WHERE case_id=%s",
                   (frozen, cid))
    db.execute("INSERT INTO audit_log (actor, action, target, detail) VALUES (%s,%s,%s,%s)",
               ((payload.get("annotator") or "复核员"), "eval_freeze", str(cid),
                db.as_json({"frozen": frozen})))
    return {"ok": bool(n), "frozen": frozen}


@router.post("/active")
def set_active(payload: dict = Body(default={})):
    cid, active = payload.get("case_id"), bool(payload.get("active", True))
    n = db.execute("UPDATE eval_case SET active=%s, updated_at=now() WHERE case_id=%s",
                   (active, cid))
    return {"ok": bool(n), "active": active}


@router.post("/delete")
def delete_case(payload: dict = Body(default={})):
    cid = payload.get("case_id")
    n = db.execute("DELETE FROM eval_case WHERE case_id=%s AND frozen=false", (cid,))
    return {"ok": bool(n), "deleted": n}


# ---------------- 分层抽样 ----------------

@router.post("/sample")
def sample(payload: dict = Body(default={})):
    """按「等级 × 是否有负分 × 消息数分档」分层抽样，覆盖 S/A/B/C/D。"""
    return eval_runner.sample(kind=(payload.get("kind") or "golden"),
                              limit=int(payload.get("limit") or 30),
                              min_messages=int(payload.get("min_messages") or 5),
                              staff_ids=payload.get("staff_ids"))


# ---------------- AI 预标注（把草稿填好，人工只复核）----------------

@router.post("/prefill")
def prefill(payload: dict = Body(default={})):
    """把 AI 当时的判定预填成标注初稿（`prefilled=true`），人工只需复核修正。

    ⚠️ **预标注不是金标准**：内容来自 AI 自己 → 跑分与门禁**默认排除** `prefilled`。
    人工在标注页保存后自动转为「已确认」。`dry_run=true` 可先看会填哪些。
    """
    return eval_runner.prefill(kind=(payload.get("kind") or ""),
                               limit=int(payload.get("limit") or 0),
                               overwrite=bool(payload.get("overwrite")),
                               dry_run=bool(payload.get("dry_run")),
                               annotator=(payload.get("annotator") or "AI预标注"))


# ---------------- 跑分（后台线程 + 进度）----------------

@router.post("/start")
def start_run(payload: dict = Body(default={})):
    return eval_runner.start(kind=(payload.get("kind") or ""),
                             limit=int(payload.get("limit") or 0))


@router.post("/run")
def run_now(payload: dict = Body(default={})):
    """**同步**跑一遍评估集并返回指标（后台版是 `/start`）。

    同步版的意义：门禁判定与「改完规则立刻看指标」都要**当场拿到结果**，
    而后台版要轮询 `/status`。样本少（30 条）时同步跑完全可接受。

    ⚠️ `include_prefilled=true` 会把 AI 预标注也计入 —— **仅供对照「AI 自评一致性」，
    不能作为门禁依据**（那是 AI 批改自己）。
    """
    return eval_runner.run_sync(kind=(payload.get("kind") or ""),
                                limit=int(payload.get("limit") or 0),
                                check_drift=bool(payload.get("check_drift", True)),
                                include_prefilled=bool(payload.get("include_prefilled")))


@router.get("/status")
def run_status():
    return eval_runner.status()


@router.get("/runs")
def runs(limit: int = 20):
    rows = db.query("""SELECT id, kind, rule_version, prompt_version, case_total, case_ok,
                              metrics, gate_passed, gate_diffs, status, elapsed_sec,
                              started_at, finished_at
                         FROM eval_run ORDER BY id DESC LIMIT %s""", (limit,))
    return {"items": rows}


@router.get("/run-detail")
def run_detail(run_id: int):
    r = db.query_one("SELECT * FROM eval_run WHERE id=%s", (run_id,))
    return r or {}


# ---------------- 基线 ----------------

@router.get("/meta-loop")
def meta_loop_report():
    """E5/G8 meta-loop 报告：按 rule 的误报率排行 + 复核回流进度 + 验证状态分布。

    这是「把人工判断变回系统改进」的唯一出口 —— 没有它，人工复核的结论
    只是躺在表里的一条记录，下次模型照样犯同样的错。
    """
    return meta_loop.report()


@router.post("/backflow")
def meta_loop_backflow(payload: dict = Body(default={})):
    """把**人工改判过**的复核记录回流成 `eval_case`（E5）。

    只回流改判过的（`corrected` / `rejected`）：与 AI 一致的复核不回流，
    那只是确认、不带来新信息，回流只会稀释评估集。
    """
    return meta_loop.backflow_reviews(limit=int(payload.get("limit") or 100),
                                      kind=payload.get("kind") or "regression")


@router.get("/baseline")
def get_baseline():
    return eval_runner.load_baseline()


@router.post("/baseline")
def set_baseline(payload: dict = Body(default={})):
    """把某次跑分冻结为基线（门禁的对比参照）；不传 run_id 则取最近一次成功记录。"""
    rid = payload.get("run_id")
    row = (db.query_one("SELECT * FROM eval_run WHERE id=%s", (rid,)) if rid else
           db.query_one("SELECT * FROM eval_run WHERE status='success' ORDER BY id DESC LIMIT 1"))
    if not row:
        return {"ok": False, "reason": "没有可用的跑分记录"}
    out = eval_runner.save_baseline(row.get("metrics") or {}, kind=row.get("kind") or "",
                                    rule_version=row.get("rule_version") or "",
                                    prompt_version=row.get("prompt_version") or "",
                                    case_total=row.get("case_total") or 0,
                                    note=(payload.get("note") or "").strip())
    db.execute("INSERT INTO audit_log (actor, action, target, detail) VALUES (%s,%s,%s,%s)",
               ("manual", "eval_set_baseline", str(row["id"]),
                db.as_json({"metrics": row.get("metrics")})))
    return {"ok": True, "baseline": out}


# ---------------- 门禁预检（不跑模型，只比 history）----------------

@router.get("/gate")
def gate_check():
    """当前最新跑分 vs 基线是否过门禁（等价于 run 时的判定，用于随时查看）。"""
    last = db.query_one("SELECT * FROM eval_run WHERE status='success' ORDER BY id DESC LIMIT 1")
    base = eval_runner.load_baseline()
    if not last:
        return {"ok": False, "reason": "还没有跑分记录"}
    passed, diffs = eval_runner.gate(last.get("metrics") or {}, base.get("metrics") or {})
    return {"ok": True, "run_id": last["id"], "has_baseline": bool(base.get("metrics")),
            "gate_passed": passed, "gate_diffs": diffs,
            "metrics": last.get("metrics"), "baseline_metrics": base.get("metrics"),
            "baseline_set_at": base.get("set_at")}


# ---------------- 导出 jsonl ----------------

@router.get("/export")
def export(kind: str = "", only_labeled: bool = True):
    cond, params = ["active = true"], {}
    if kind:
        cond.append("kind = %(kind)s"); params["kind"] = kind
    if only_labeled:
        cond.append("expected_items IS NOT NULL")
    rows = db.query(f"SELECT * FROM eval_case WHERE {' AND '.join(cond)} ORDER BY case_id", params)
    lines = []
    for r in rows:
        lines.append(json.dumps({
            "case_id": r["case_id"], "kind": r["kind"],
            "staff_id": r["staff_id"], "session_id": r["session_id"],
            "messages_hash": r["messages_hash"],
            "expected_items": r["expected_items"],
            "expected_raw_score": r["expected_raw_score"], "expected_grade": r["expected_grade"],
            "disputed": r["disputed"], "annotator": r["annotator"], "note": r["note"],
        }, ensure_ascii=False))
    body = "\n".join(lines)
    return Response(body, media_type="application/x-ndjson",
                    headers={"Content-Disposition": 'attachment; filename="eval_cases.jsonl"'})
