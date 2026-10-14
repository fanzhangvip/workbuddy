# -*- coding: utf-8 -*-
"""评估集运行器（架构 第五章 E5 / 闸6）：抽样 · 跑分 · 指标 · 回归门禁。

为什么需要它
    在这之前，全库**没有任何东西能回答"判得准不准"** —— 复核侧只有一个「一致率」计数，
    而且只在极少数人工改判过的样本上有值。改一次规则、调一次提示词，只能凭感觉说"应该更好了"。
    评估集把这个判断变成**数字**（命中项 F1 / 等级一致率 / 误报率）。

三条铁律（来自架构 §5）
    1. **只标「命中了哪些 rule_id（含档位分值）」，不标总分**。
       总分由 `rules.py` 算 —— 让人工标总分，等于让人实现一遍规则引擎。
    2. **跑分只算不落库**：`run_case()` 复用评分链路的**前半段**
       （`build_context` → `compute_facts` → `analyze` → `compute_score`），
       **不写** `customer_intent_assessment`、不碰 `rule_tags`、不触发回写。
       否则跑一次评估会污染业务数据，还会推进增量水位、把"增量段"变成假数据。
    3. **门禁看方向**：F1 / 等级一致率要**不降**，误报率要**不升**；留容差避免噪声抖动误阻断。
"""
import hashlib
import json
import threading
import time
from collections import defaultdict
from pathlib import Path

import analyze
import db
import rules as rules_mod
import scoring_service as ss

KINDS = ("golden", "regression", "edge")

#: 门禁主指标与方向（+1 = 越高越好，-1 = 越低越好）
GATE_METRICS = {"rule_f1": +1, "grade_acc": +1, "fp_case_rate": -1}
#: 容差：小于 2 个百分点视为「没变」（模型有随机性，0 容差会让门禁天天误报）
GATE_TOL = 0.02

BASELINE_PATH = Path(__file__).resolve().parent.parent / "eval" / "baseline.json"


# ============================================================ 1. 对话指纹

def messages_hash(staff_id: str, session_id: str) -> str:
    """算该会话的「对话内容指纹」（架构 §4.2 的 `messages_hash`，防样本漂移）。

    **刻意用 `raw_text` 而不是渲染后的行**：渲染会带「6天前」这类相对时间，
    隔一天再跑 hash 就变了 → 会误报"样本失效"。用原始正文 + message_id + direction，
    只有**内容真的变了**（消息增删、正文改写）才变。
    """
    rows = db.query(
        f"""SELECT {ss._MSG_COLS} FROM chat_message
             WHERE staff_id = %s AND session_id = %s AND {ss._READABLE}
             ORDER BY msg_time ASC, message_id ASC""",
        (staff_id, session_id))
    import ctx_preprocess
    h = hashlib.sha256()
    for r in rows:
        it = ctx_preprocess.classify(r)
        h.update(f"{it['message_id']}|{it['direction']}|{it['raw_text']}\n".encode("utf-8"))
    return h.hexdigest()[:32]


# ============================================================ 2. 分层抽样

def _msg_bucket(n) -> str:
    n = int(n or 0)
    if n < 20:
        return "短"
    if n <= 60:
        return "中"
    return "长"


def sample(kind: str = "golden", limit: int = 30, min_messages: int = 5,
           staff_ids: list = None) -> dict:
    """分层抽样：按「等级 × 是否有负分」分组、组内按消息数分档轮转抽取。

    分层维度直接决定样本能覆盖什么：
    · **等级**（S/A/B/C/D/未评级）—— 保证高低意向都有，否则全是 A 的样本集测不出 D 的漏判
    · **是否有负分** —— 负分项是人工复核的主战场，错判代价最高
    · **消息数分档**（短 <20 / 中 20~60 / 长 >60）—— 长会话才走 ④⑤ 段与裁剪路径，
      短会话验证不了那部分改动（本轮上下文装配的收益**只能靠长会话样本量出来**）
    """
    kind = kind if kind in KINDS else "golden"
    limit = max(1, int(limit or 30))
    params = {"kind": kind, "min_msgs": int(min_messages or 5)}
    staff_filter = ""
    if staff_ids:
        staff_filter = "AND c.staff_id = ANY(%(staff_ids)s)"
        params["staff_ids"] = list(staff_ids)

    cands = db.query(f"""
        SELECT c.staff_id, s.name AS staff_name, c.session_id, c.target_id,
               c.name AS target_name, c.msg_count, c.last_msg_time,
               last.grade, last.negative_total,
               last.assessment_id AS b_aid, last.raw_score AS b_raw, last.positive_items AS b_pos,
               last.negative_items AS b_neg, last.rule_version AS b_rv,
               last.prompt_version AS b_pv, last.analyzed_at AS b_at
          FROM chat_session c
          LEFT JOIN staff s ON s.staff_id = c.staff_id
          LEFT JOIN LATERAL (
               SELECT a.assessment_id, a.grade, a.negative_total, a.raw_score,
                      a.positive_items, a.negative_items, a.rule_version,
                      a.prompt_version, a.analyzed_at
                FROM customer_intent_assessment a
                WHERE a.relationship_key = c.staff_id || ':' || COALESCE(c.target_id, c.session_id)
                ORDER BY a.assessment_id DESC LIMIT 1
          ) last ON true
         WHERE c.session_type = 'ext_user' AND c.target_id IS NOT NULL
           AND c.msg_count >= %(min_msgs)s
           {staff_filter}
           AND NOT EXISTS (
               SELECT 1 FROM eval_case e
                WHERE e.kind = %(kind)s AND e.staff_id = c.staff_id AND e.session_id = c.session_id)
         ORDER BY c.msg_count DESC""", params)

    # 分组 → 组内按消息数「短→中→长」排序 → round-robin 抽取（保证跨组覆盖）
    groups = defaultdict(list)
    for c in cands:
        key = (c.get("grade") or "未评级",
               "有负分" if int(c.get("negative_total") or 0) != 0 else "无负分")
        groups[key].append(c)
    for k in groups:
        groups[k].sort(key=lambda x: (int(x.get("msg_count") or 0)))
    grp_keys = sorted(groups.keys(), key=lambda k: (k[0] not in ("S", "A"), k[0], k[1]))

    picked, cursor = [], {k: 0 for k in grp_keys}
    while len(picked) < limit:
        progressed = False
        for k in grp_keys:
            if len(picked) >= limit:
                break
            i = cursor[k]
            if i < len(groups[k]):
                picked.append(groups[k][i])
                cursor[k] = i + 1
                progressed = True
        if not progressed:                     # 所有组都抽完了
            break

    created = []
    for c in picked:
        h = messages_hash(c["staff_id"], c["session_id"])
        # 基线快照：从「该会话已有的最近一次评估」直接取，**不额外调 LLM**
        # （抽样时批量评分往往刚跑完，这批结果就是天然的"改动前基线"）。
        b_items = (_as_items(c.get("b_pos")) + _as_items(c.get("b_neg"))) if c.get("b_aid") else None
        row = db.query_one(
            """INSERT INTO eval_case
                 (kind, staff_id, session_id, target_id, staff_name, target_name,
                  session_name, messages_hash, msg_count, last_msg_time, source,
                  baseline_items, baseline_raw_score, baseline_grade,
                  baseline_rule_version, baseline_prompt_version, baseline_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'sample',%s,%s,%s,%s,%s,%s)
               ON CONFLICT (kind, staff_id, session_id) DO NOTHING
               RETURNING case_id""",
            (kind, c["staff_id"], c["session_id"], c["target_id"], c.get("staff_name"),
             c.get("target_name"), c.get("target_name"), h, c.get("msg_count"),
             c.get("last_msg_time"),
             db.as_json(b_items) if b_items is not None else None, c.get("b_raw"), c.get("grade"),
             c.get("b_rv"), c.get("b_pv"), c.get("b_at")))
        if row:
            created.append(row["case_id"])

    dist = defaultdict(int)
    for c in picked:
        dist[f"{c.get('grade') or '未评级'}/{'有负分' if int(c.get('negative_total') or 0) else '无负分'}"
             f"/{_msg_bucket(c.get('msg_count'))}"] += 1
    return {"ok": True, "kind": kind, "candidates": len(cands), "picked": len(picked),
            "created": len(created), "case_ids": created, "distribution": dict(dist)}


# ============================================================ 3. 跑单条（只算不落库）

def run_case(case: dict, rules: dict = None, fetch_tags: bool = None) -> dict:
    """对一条样本跑一次「当前规则 + 当前提示词」的判定，**只返回结果，不落任何业务表**。

    与 `ScoringService._score_one` 的前半段逐条对齐（含 engine 硬指标、极性过滤），
    唯一差别是**不做 `_persist_assessment` / `_tag_evidence_messages` / `_writeback`**。
    """
    rules = rules or rules_mod.load_rules()
    if fetch_tags is None:
        fetch_tags = ss.setting_bool("scoring_fetch_tags", "SCORING_FETCH_TAGS", False)
    staff_id, session_id = case["staff_id"], case["session_id"]
    rel = f"{staff_id}:{case.get('target_id')}"

    ctx = ss.build_context(staff_id, session_id,
                           session_meta={"staff_id": staff_id, "session_id": session_id},
                           rel_key=rel)
    min_kept = ss.setting_int("ctx_min_kept", "CTX_MIN_KEPT", 4)
    if ctx["stats"]["kept"] < min_kept:
        return {"ok": False, "reason": "skipped", "detail": "有效条数不足，生产链路会跳过该会话",
                "items": [], "raw_score": None, "grade": None}

    facts = ss.compute_facts(staff_id, session_id, fetch_tags=fetch_tags)
    res = analyze.analyze(
        ctx.get("text", ""), facts["text"], rules,
        evidence_index=ctx.get("index"), labels=ctx.get("labels"),
        current_date=ctx.get("current_date"),
        temperature=ss.setting_float("llm_temperature", "LLM_TEMPERATURE", analyze.DEFAULT_TEMPERATURE),
        max_tokens=ss.setting_int("llm_max_tokens", "LLM_MAX_TOKENS", 0) or None,
        max_repairs=ss.setting_int("rejudge_retries", "REJUDGE_RETRIES", analyze.DEFAULT_MAX_REPAIRS))

    engine_ids = ss.service._engine_rule_ids(rules)
    neg = [i for i in (res.get("negative_items") or []) if i.get("rule_id") not in engine_ids]
    neg += ss.service._engine_negative_items(facts, rules)
    pos = res.get("positive_items") or []
    items = pos + neg
    score = rules_mod.compute_score(pos, neg, rules)
    grade = rules_mod.grade_of(score["raw_score"], rules)

    return {"ok": True, "items": items, "raw_score": score["raw_score"],
            "positive_total": score["positive_total"], "negative_total": score["negative_total"],
            "grade": grade["grade"], "unchecked_rules": list(res.get("unchecked_rules") or []),
            "invalid_items": list(res.get("invalid_items") or []),
            "ctx_stats": ctx["stats"],
            "rule_version": res.get("rule_version"), "prompt_version": res.get("prompt_version"),
            "contract_attempts": res.get("contract_attempts")}


# ============================================================ 4. 指标

def _item_map(items) -> dict:
    """[{rule_id, score}] → {rule_id: score}（同 rule 多条时取分值绝对值最大者）。"""
    out = {}
    for i in (items or []):
        rid = i.get("rule_id")
        if not rid:
            continue
        sc = int(i.get("score") or 0)
        if rid not in out or abs(sc) > abs(out[rid]):
            out[rid] = sc
    return out


#: JSONB 读回来可能是 list 或 json 字符串 → 统一成 list（复用 scoring_service 的口径）
_as_items = ss._as_items


def metrics(pairs: list) -> dict:
    """汇总指标。`pairs` = [{expected_items, actual_items, expected_grade, actual_grade}]。

    命中项级 F1 按 **rule_id 集合**算（这是文档说的「命中项 F1」）；
    另外单独给 `item_exact_rate`（命中项里**档位分值也对**的比例）——
    只命中 rule 但给错档位，业务上同样是错判，F1 看不出来。
    """
    TP = FP = FN = EXACT = 0
    grade_ok = cases_with_fp = valid = 0
    miss_detail = defaultdict(int)

    for p in pairs:
        if p.get("actual_grade") is None:      # 该样本被跳过（有效条数不足）
            continue
        valid += 1
        e, a = _item_map(p.get("expected_items")), _item_map(p.get("actual_items"))
        tp, fp, fn = set(e) & set(a), set(a) - set(e), set(e) - set(a)
        TP, FP, FN = TP + len(tp), FP + len(fp), FN + len(fn)
        EXACT += sum(1 for k in tp if e[k] == a[k])
        if fp:
            cases_with_fp += 1
        for k in fn:
            miss_detail[k] += 1                # 漏判了哪个维度 → 诊断用
        if p.get("expected_grade") == p.get("actual_grade"):
            grade_ok += 1

    precision = TP / (TP + FP) if (TP + FP) else 1.0
    recall = TP / (TP + FN) if (TP + FN) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    n = valid or 1
    return {
        "case_total": len(pairs), "case_valid": valid,
        "tp": TP, "fp": FP, "fn": FN,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "rule_f1": round(f1, 4),
        "item_exact_rate": round(EXACT / TP, 4) if TP else 1.0,
        "grade_acc": round(grade_ok / n, 4),
        "fp_case_rate": round(cases_with_fp / n, 4),
        "miss_top": dict(sorted(miss_detail.items(), key=lambda kv: -kv[1])[:10]),
    }


# ============================================================ 5. 回归门禁

def gate(now: dict, baseline: dict, tol: float = GATE_TOL) -> tuple:
    """对比基线判门禁。返回 `(passed, diffs)`。

    方向敏感：`rule_f1` / `grade_acc` 越高越好；`fp_case_rate` 越低越好。
    容差 `tol` 是为了不让模型随机性引发的 1~2 个百分点抖动天天误阻断。
    """
    diffs, passed = [], True
    for k, direction in GATE_METRICS.items():
        base, cur = (baseline or {}).get(k), (now or {}).get(k)
        if base is None or cur is None:
            continue
        delta = (cur - base) * direction       # > 0 = 变好
        ok = delta >= -tol
        passed = passed and ok
        diffs.append({"metric": k, "base": round(base, 4), "now": round(cur, 4),
                      "delta": round(cur - base, 4), "direction": "up" if direction > 0 else "down",
                      "ok": ok})
    return passed, diffs


def gate_decision(kind: str = "", limit: int = 0, run_if_possible: bool = True,
                  baseline_path: str = None) -> dict:
    """闸6「发布前硬拦截」的判定（架构 §2.7 / §5.4）。

    改造前只有「跑分时算一个 `gate_passed`」，**没有任何地方用它拦截发布** ——
    改了 `rules.json` 直接生效，回归门禁形同装饰。

    本函数把「要不要拦住这次发布」变成一次明确判定，三种结果泾渭分明：

    | 情况 | blocked | 说明 |
    |---|---|---|
    | 门禁关闭（`eval_gate_enabled=false`） | False | 显式关闭，尊重配置 |
    | **没有基线** | False | 从没冻结过基线 → 无可比对象。**放行但如实说明**（不能假装通过） |
    | **没有已标注样本** | False | 没标注就没法算指标 → 同上，如实说明 |
    | 有基线且指标不降 | False | 正常通过 |
    | 有基线且指标下降 | **True** | 拦截，附逐指标 diff |

    ⚠️ 关键设计：**「无法判定」不等于「通过」**。返回里 ``decidable`` 明确区分二者，
    前端与日志都要能看到「这次发布其实是没门禁保护的」——否则会给人虚假的安全感。
    """
    import scoring_service as ss
    enabled = ss.setting_bool("eval_gate_enabled", "EVAL_GATE_ENABLED", True)
    path = baseline_path or ss.setting("eval_baseline_path", "EVAL_BASELINE_PATH", "")
    base = {}
    if path:
        try:
            p = Path(path)
            base = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception:                       # noqa: BLE001
            base = {}
    if not base:
        base = load_baseline()

    out = {"enabled": enabled, "baseline_path": str(path or BASELINE_PATH),
           "has_baseline": bool(base.get("metrics")), "decidable": False,
           "blocked": False, "gate_passed": None, "diffs": [], "reason": "", "metrics": None}

    if not enabled:
        out["reason"] = "回归门禁已关闭（eval_gate_enabled=false）"
        return out
    if not out["has_baseline"]:
        out["reason"] = ("尚无基线（eval/baseline.json 不存在）→ **无法判定**，放行但不代表通过。"
                         "先把一次跑分结果冻结为基线：POST /api/eval/baseline")
        return out

    # 有基线 → 需要本次指标。已有跑分结果就直接用，否则现跑一轮。
    now_metrics = None
    st = status()
    if st.get("result") and (st["result"].get("metrics")):
        now_metrics = st["result"]["metrics"]
    if now_metrics is None and run_if_possible:
        labeled = db.query_one(
            "SELECT count(*) AS n FROM eval_case WHERE active AND expected_items IS NOT NULL")
        if not (labeled or {}).get("n"):
            out["reason"] = ("有基线，但**没有已人工标注的样本** → 无法计算指标，放行但不代表通过。"
                             "标注后可用 POST /api/eval/run 复算")
            return out
        res = run_sync(kind=kind, limit=limit, check_drift=False)
        now_metrics = res.get("metrics")
    if now_metrics is None:
        out["reason"] = "拿不到本次指标（没有跑分结果）→ 放行但不代表通过"
        return out

    passed, diffs = gate(now_metrics, base.get("metrics") or {})
    out.update({"decidable": True, "gate_passed": passed, "diffs": diffs,
                "metrics": now_metrics, "blocked": not passed})
    out["reason"] = ("指标持平或变好 → 允许发布" if passed
                     else "**指标下降 → 阻断发布**（逐指标 diff 见 diffs）")
    return out


def load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {}
    try:
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_baseline(metrics_dict: dict, *, kind: str = "", rule_version: str = "",
                  prompt_version: str = "", case_total: int = 0, note: str = "") -> dict:
    """把一次跑分结果冻结为基线（门禁的对比参照）。"""
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"set_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind,
               "rule_version": rule_version, "prompt_version": prompt_version,
               "case_total": case_total, "note": note, "metrics": metrics_dict}
    BASELINE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


# ============================================================ 6. 编排（可后台跑，带进度）

_LOCK = threading.Lock()
_STATE = {"running": False, "total": 0, "done": 0, "current": None,
          "started_at": None, "elapsed_sec": 0, "error": None,
          "result": None, "case_ids": []}


def status() -> dict:
    with _LOCK:
        out = dict(_STATE)
    out["baseline"] = load_baseline().get("metrics")
    return out


def _cases(kind: str, limit: int, case_ids: list = None) -> list:
    params, cond = {}, ["active = true", "expected_items IS NOT NULL"]
    if kind:
        cond.append("kind = %(kind)s")
        params["kind"] = kind
    if case_ids:
        cond.append("case_id = ANY(%(case_ids)s)")
        params["case_ids"] = list(case_ids)
    sql = f"""SELECT * FROM eval_case WHERE {' AND '.join(cond)}
               ORDER BY case_id ASC""" + (" LIMIT %(limit)s" if limit else "")
    if limit:
        params["limit"] = int(limit)
    return db.query(sql, params)


def run_sync(kind: str = "", limit: int = 0, on_progress=None, check_drift: bool = True,
             case_ids: list = None) -> dict:
    """同步跑一遍评估集：逐条 `run_case` → 汇总 → 落 `eval_run` → 门禁。"""
    t0 = time.time()
    cases = _cases(kind, limit, case_ids)
    rules = rules_mod.load_rules()
    pairs, details, drift = [], [], []

    for idx, c in enumerate(cases, 1):
        if check_drift and c.get("messages_hash"):
            cur = messages_hash(c["staff_id"], c["session_id"])
            if cur != c["messages_hash"]:
                drift.append(c["case_id"])     # 会话内容已变 → 该样本失效，跳过
                if on_progress:
                    on_progress(idx, len(cases), c["case_id"])
                continue
        try:
            got = run_case(c, rules)
        except Exception as e:                 # 单条失败不影响整轮（与生产口径一致）
            got = {"ok": False, "reason": "error", "detail": f"{type(e).__name__}: {e}",
                   "items": [], "raw_score": None, "grade": None}
        pairs.append({"expected_items": c.get("expected_items"),
                      "actual_items": got.get("items"),
                      "expected_grade": c.get("expected_grade"),
                      "actual_grade": got.get("grade")})
        details.append({"case_id": c["case_id"], "staff_id": c["staff_id"],
                        "session_id": c["session_id"], "expected_grade": c.get("expected_grade"),
                        "actual_grade": got.get("grade"), "expected_raw": c.get("expected_raw_score"),
                        "actual_raw": got.get("raw_score"), "ok": got.get("ok"),
                        "reason": got.get("reason"), "items": got.get("items"),
                        "ctx_stats": got.get("ctx_stats")})
        if on_progress:
            on_progress(idx, len(cases), c["case_id"])

    m = metrics(pairs)
    base = load_baseline()
    passed, diffs = gate(m, base.get("metrics") or {})
    elapsed = round(time.time() - t0, 1)

    row = db.query_one(
        """INSERT INTO eval_run (kind, rule_version, prompt_version, case_total, case_ok,
                                 metrics, gate_passed, gate_diffs, status, elapsed_sec, detail,
                                 finished_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'success',%s,%s,now()) RETURNING id""",
        (kind or None, rules.get("rule_version"), analyze.PROMPT_VERSION,
         len(cases), m["case_valid"], db.as_json(m),
         passed if base.get("metrics") else None, db.as_json(diffs) if diffs else None,
         elapsed, db.as_json({"cases": details, "drift": drift,
                              "baseline_at": base.get("set_at")})))
    out = {"ok": True, "run_id": row["id"], "case_total": len(cases), "case_ok": m["case_valid"],
           "drift": drift, "metrics": m, "gate_passed": passed if base.get("metrics") else None,
           "gate_diffs": diffs, "has_baseline": bool(base.get("metrics")),
           "elapsed_sec": elapsed}
    with _LOCK:
        _STATE.update({"result": out, "elapsed_sec": elapsed})
    return out


def start(kind: str = "", limit: int = 0) -> dict:
    """起后台线程跑评估集（条数多 + 每条一次真实模型调用，HTTP 同步会超时）。"""
    with _LOCK:
        if _STATE["running"]:
            return {"ok": False, "reason": "已有评估在跑，请等待完成"}
        _STATE.update({"running": True, "total": 0, "done": 0, "current": None,
                       "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "elapsed_sec": 0, "error": None, "result": None})

    def _prog(i, n, cid):
        with _LOCK:
            _STATE.update({"total": n, "done": i, "current": cid})

    def _body():
        try:
            run_sync(kind, limit, on_progress=_prog)
        except Exception as e:
            with _LOCK:
                _STATE["error"] = f"{type(e).__name__}: {e}"
        finally:
            with _LOCK:
                _STATE["running"] = False

    threading.Thread(target=_body, daemon=True, name="eval-run").start()
    return {"ok": True, "triggered_by": "manual"}
