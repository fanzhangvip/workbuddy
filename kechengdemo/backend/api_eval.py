# -*- coding: utf-8 -*-
"""评估集接口（架构 第五章 E5 / 闸6）。

定位：**回答「判得准不准」**。在这之前全库没有任何东西能回答这个问题，
改规则/调提示词只能凭感觉。本模块提供：抽样 → 人工标注 → 跑分对比 → 门禁 → 导出。

标注口径（**务必保持**）：只标「命中了哪些 rule_id + 哪个档位」，**不标总分** ——
总分由 `rules.py` 算。若让人标总分，等于让人工重新实现一遍规则引擎（架构 §5.1 原文）。
"""
import json

from fastapi import APIRouter, Body, Depends
from fastapi.responses import Response

import api_common
import api_guard
import audit
import db
import eval_case_repo
import eval_runner
import meta_loop
import rules as rules_mod
import scoring_service as ss
import security

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
          -- 草稿**带内容**（= 保存过或预标注过，但都不是金标准）。
          -- 与 `confirmed` 的区别是本项目踩过的坑：保存路径曾不清 `prefilled`，
          -- 于是"人工确认过"在门禁眼里等于 0 条；这个计数让那种状态可见。
          (SELECT count(*) FROM eval_case
             WHERE prefilled AND expected_items IS NOT NULL)                 AS prefilled_with_content,
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

    res = api_common.paginate(
        """
        SELECT case_id, kind, staff_id, session_id, target_id, staff_name, target_name,
               msg_count, last_msg_time, expected_items, expected_raw_score, expected_grade,
               baseline_raw_score, baseline_grade, baseline_rule_version, baseline_at,
               source, disputed, annotator, prefilled, frozen, note, created_at, updated_at
          FROM eval_case""",
        "SELECT count(*) AS n FROM eval_case",
        " AND ".join(cond), params,
        order_by="prefilled DESC, (expected_items IS NULL) DESC, case_id DESC",
        page=page, page_size=page_size)
    # 派生：标注与基线的差异（一眼看出"人标 vs 系统判"差在哪）
    for it in res["items"]:
        it["diff_from_baseline"] = _diff_label(it)
    return res


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
            # `messages` = 给人看的结构化消息（带媒体地址）；`conversation` 保留兼容
            "messages": ss.build_display_messages(c["staff_id"], c["session_id"],
                                                  limit=msg_limit),
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

@router.post("/save", dependencies=[Depends(api_guard.require("reviewer", "operator", "admin"))])
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
    # 审计主体由服务端推导（评审 P0-2）：客户端自报值只进 detail.reported_actor
    reported = payload.get("annotator")
    annotator = security.actor()

    eval_case_repo.set_annotation(cid, items=valid, raw_score=score["raw_score"],
                                  grade=grade["grade"], annotator=annotator,
                                  note=(payload.get("note") or "").strip(),
                                  disputed=bool(payload.get("disputed")),
                                  # 审计写在 repo 内（与 UPDATE 同事务）：
                                  # 这里只补「服务端推导不出来的那一半」——
                                  # 客户端自报的 annotator 与非法项。
                                  detail=security.actor_detail(reported, invalid=bad))
    return {"ok": True, "expected_raw_score": score["raw_score"], "expected_grade": grade["grade"],
            "positive_total": score["positive_total"], "negative_total": score["negative_total"],
            "invalid_items": bad}


# ---------------- 冻结 / 解冻 ----------------

@router.post("/dispute", dependencies=[Depends(api_guard.require("reviewer", "operator", "admin"))])
def dispute(payload: dict = Body(default={})):
    """标记「AI 判定有误」：**只打争议标记，不动标注内容**。

    为什么不复用 `/save`：`/save` 会把 `prefilled` 清成 false → 那份**错误内容**
    立刻变成"人工金标准"计入指标 —— 那比不标更糟（指标被污染且看不出来）。
    所以处置是：留在草稿态（继续被排除在指标外）+ 打争议标记进「争议」筛选，
    等人工把命中项改对再保存。
    """
    cid = payload.get("case_id")
    if not cid:
        return {"ok": False, "reason": "缺少 case_id"}
    note = (payload.get("note") or "").strip()
    n = eval_case_repo.mark_dispute(cid, note, actor=security.actor())
    return {"ok": bool(n), "case_id": cid, "disputed": True,
            "note": "已标记争议；标注内容保持不变（仍排除在指标外），改正后再保存"}


@router.post("/freeze", dependencies=[Depends(api_guard.require("reviewer", "operator", "admin"))])
def freeze(payload: dict = Body(default={})):
    """金标准一经冻结，除仲裁外不得修改（架构 §5.3 第 4 条）。"""
    cid, frozen = payload.get("case_id"), bool(payload.get("frozen", True))
    n = eval_case_repo.set_frozen(cid, frozen, actor=security.actor())
    return {"ok": bool(n), "frozen": frozen}


@router.post("/active", dependencies=[Depends(api_guard.require("reviewer", "operator", "admin"))])
def set_active(payload: dict = Body(default={})):
    cid, active = payload.get("case_id"), bool(payload.get("active", True))
    n = eval_case_repo.set_active(cid, active, actor=security.actor())
    return {"ok": bool(n), "active": active}


@router.post("/delete", dependencies=[Depends(api_guard.require("reviewer", "operator", "admin"))])
def delete_case(payload: dict = Body(default={})):
    cid = payload.get("case_id")
    n = eval_case_repo.delete_case(cid, actor=security.actor())
    return {"ok": bool(n), "deleted": n}


# ---------------- 分层抽样 ----------------

@router.post("/sample", dependencies=[Depends(api_guard.require("reviewer", "operator", "admin"))])
def sample(payload: dict = Body(default={})):
    """按「等级 × 是否有负分 × 消息数分档」分层抽样，覆盖 S/A/B/C/D。"""
    return eval_runner.sample(kind=(payload.get("kind") or "golden"),
                              limit=int(payload.get("limit") or 30),
                              min_messages=int(payload.get("min_messages") or 5),
                              staff_ids=payload.get("staff_ids"))


# ---------------- AI 预标注（把草稿填好，人工只复核）----------------

@router.post("/prefill", dependencies=[Depends(api_guard.require("reviewer", "operator", "admin"))])
def prefill(payload: dict = Body(default={})):
    """把 AI 当时的判定预填成标注初稿（`prefilled=true`），人工只需复核修正。

    ⚠️ **预标注不是金标准**：内容来自 AI 自己 → 跑分与门禁**默认排除** `prefilled`。
    人工在标注页保存后自动转为「已确认」。`dry_run=true` 可先看会填哪些。
    """
    # ⚠️ `annotator` 是**系统身份**（「AI 预标注」），刻意**不接受客户端指定** ——
    # 否则可以伪造「这条是 AI 预标注/这条是人工标注」的溯源信息。
    # 操作者身份由鉴权中间件从凭据推导，只进审计 detail。
    #
    # W3（修 P14）：覆盖分三级 —— 默认只填空白；`overwrite` 只覆盖仍是草稿的；
    # 要冲掉**已人工确认**的内容必须显式 `force_overwrite_annotated`，且**仅 admin**，
    # 审计里会带 `overwrote_annotated`。改造前一次 `overwrite:true` 就能把人工标注全冲掉。
    if payload.get("force_overwrite_annotated") and not security.has_role("admin"):
        return {"ok": False,
                "reason": "覆盖已人工确认的标注需要 admin 角色（普通复核请用 overwrite=true，"
                          "它只覆盖仍是草稿的样本）"}
    return eval_runner.prefill(kind=(payload.get("kind") or ""),
                               limit=int(payload.get("limit") or 0),
                               overwrite=bool(payload.get("overwrite")),
                               dry_run=bool(payload.get("dry_run")),
                               overwrite_annotated=bool(payload.get("force_overwrite_annotated")),
                               actor=security.actor())

# ---------------- 跑分（后台线程 + 进度）----------------

@router.post("/start", dependencies=[Depends(api_guard.require("reviewer", "operator", "admin"))])
def start_run(payload: dict = Body(default={})):
    return eval_runner.start(kind=(payload.get("kind") or ""),
                             limit=int(payload.get("limit") or 0),
                             include_prefilled=bool(payload.get("include_prefilled")))


@router.post("/run", dependencies=[Depends(api_guard.require("reviewer", "operator", "admin"))])
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
                              started_at, finished_at, baseline_id
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


@router.get("/coverage")
def coverage():
    """评估集对每条规则的覆盖度（修 P9）：哪条规则在评估集里**一条样本都没有**。

    用途：新加规则时先看这里 —— 0 样本覆盖 = 改了也测不出来，门禁对它天然免疫。
    """
    return eval_runner.coverage()


@router.get("/alerts")
def alerts():
    """评估侧的告警汇总（修 P8）：门禁越界 / 单规则误报率 / 复核积压 / 凭据与同步新鲜度。

    `items` 为空 = 一切正常。前端总览页据此显示徽标。
    """
    return eval_runner.alerts()


@router.post("/backflow", dependencies=[Depends(api_guard.require("reviewer", "operator", "admin"))])
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


@router.post("/baseline", dependencies=[Depends(api_guard.require("reviewer", "operator", "admin"))])
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
    audit.record_action(security.actor(), "eval_set_baseline", str(row["id"]), {"metrics": row.get("metrics")})
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
