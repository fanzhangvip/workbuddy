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
import eval_case_repo
import rules as rules_mod
import scoring_context as sctx

KINDS = ("golden", "regression", "edge")

#: 门禁主指标与方向（+1 = 越高越好，-1 = 越低越好）
#: ⚠️ 用 `rule_f1_llm`（**排除引擎硬指标**，修 P9）而不是 `rule_f1`：
#:   `negative_no_reply_2d` 这类引擎项由后台按锚点算出来，会随锚点/时间自己出现消失
#:   （实测同一批样本两个时刻跑，含该项的样本数 17 → 21）。把它算进门禁，
#:   "时间流逝"就会被读成"判得准不准"的变化。两者都在 `metrics()` 里有数，
#:   但**只有 `rule_f1_llm` 面向门禁**。
#: ⚠️ `gate()` 对基线里**缺失的键是静默跳过**的 → 所以基线必须在本次改动**之后**
#:   冻结（新的 `eval_run` 里才有这个键）；否则这一项会安静地失去保护。
GATE_METRICS = {"rule_f1_llm": +1, "grade_acc": +1, "fp_case_rate": -1}
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
        f"""SELECT {sctx.MSG_COLS} FROM chat_message
             WHERE staff_id = %s AND session_id = %s AND {sctx.READABLE}
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
        cid = eval_case_repo.insert_sample(
            kind=kind, staff_id=c["staff_id"], session_id=c["session_id"],
            target_id=c["target_id"], staff_name=c.get("staff_name"),
            target_name=c.get("target_name"), session_name=c.get("target_name"),
            messages_hash=h, msg_count=c.get("msg_count"),
            last_msg_time=c.get("last_msg_time"),
            baseline_items=db.as_json(b_items) if b_items is not None else None,
            baseline_raw_score=c.get("b_raw"), baseline_grade=c.get("grade"),
            baseline_rule_version=c.get("b_rv"), baseline_prompt_version=c.get("b_pv"),
            baseline_at=c.get("b_at"),
            # P2：锚点取**基线那次评估的时刻**（`b_at`），抽样时刻只是退化兜底。
            # 为什么不用抽样时刻：样本是"改成什么样"的对照物，必须复现**当时**的输入，
            # 而不是"我抽样那一刻"的时间（差一天，48h 那类规则就可能翻转）。
            anchor_ts=(int(c["b_at"].timestamp()) if c.get("b_at") else int(time.time())))
        if cid:
            created.append(cid)

    dist = defaultdict(int)
    for c in picked:
        dist[f"{c.get('grade') or '未评级'}/{'有负分' if int(c.get('negative_total') or 0) else '无负分'}"
             f"/{_msg_bucket(c.get('msg_count'))}"] += 1
    return {"ok": True, "kind": kind, "candidates": len(cands), "picked": len(picked),
            "created": len(created), "case_ids": created, "distribution": dict(dist)}


# ============================================================ 2.5 AI 预标注

def prefill(kind: str = "", limit: int = 0, overwrite: bool = False,
            annotator: str = "AI预标注", dry_run: bool = False,
            overwrite_annotated: bool = False, actor: str = "system") -> dict:
    """把「AI 当时的判定」预填成人工标注的**初稿**，人工只需复核修正。

    为什么需要它：30 条从空白开始标注很慢（每条要约 110 条消息 + 15 条规则）；
    而从一份草稿开始改很快 —— 人擅长「判断对不对」，不擅长「从零列清单」。

    数据来源 = `eval_case.baseline_items`（抽样时从该会话最近一次评估直接取的快照）：
    · **零 LLM 调用、零成本、瞬时**（重跑模型要 ~10 分钟且要花钱）
    · 它同时是「改动前基线」，作为初稿恰好让人一眼看出「哪里该改」

    ⚠️ 四条纪律（否则预标注会毁掉评估集）：
    1. **必须打 `prefilled=true`** —— 预填内容来自 AI 自己，直接当金标准就是
       「AI 批改自己」，F1 必然虚高。跑分与门禁**默认排除** `prefilled`。
    2. **人工保存后自动转 false**（`api_eval.save` 里已处理）—— 「复核过」才叫金标准。
    3. **覆盖分三级**（修 P14）：默认只填空白（`expected_items IS NULL`）；
       `overwrite=True` 只覆盖**仍是草稿**的（`prefilled=true`）；
       要覆盖**已人工确认**的内容（`prefilled=false`）必须显式 `overwrite_annotated=True`，
       且会在审计里标 `overwrote_annotated`。
       —— 改造前只有"覆盖 / 不覆盖"两级，一次 `/prefill {overwrite:true}` 就能把人工标注
       全冲掉（实测：2026-10-15 有 21 行被整体替换成 AI 输出）。
    4. **冻结的样本一律不碰**（`frozen=true` 是"除仲裁外不得修改"的那道闸；改造前
       `prefill` 完全不看 `frozen`，只有 `/save` 拦）。

    审计：每行一条 `eval_prefill`（写在 `eval_case_repo` 内、与 UPDATE 同事务），
    取代了改造前"条数 ≤30 才留一条汇总"的写法（那个条件让**样本越多越不留痕**）。

    返回 `{total, filled, skipped_no_baseline, skipped_annotated, skipped_frozen, case_ids}`。
    """
    params, cond = {}, ["active = true", "baseline_items IS NOT NULL", "frozen = false"]
    if kind:
        cond.append("kind = %(kind)s")
        params["kind"] = kind
    if overwrite_annotated:
        pass                                   # 显式允许覆盖人工确认过的（除冻结外全量）
    elif overwrite:
        cond.append("prefilled = true")        # 只覆盖仍是草稿的
    else:
        cond.append("expected_items IS NULL")
    sql = (f"SELECT case_id, staff_id, session_id, target_id, msg_count, baseline_items, "
           f"expected_items FROM eval_case WHERE {' AND '.join(cond)} ORDER BY case_id ASC")
    if limit:
        sql += " LIMIT %(limit)s"
        params["limit"] = int(limit)
    rows = db.query(sql, params)

    # 无基线可预填的（有 active + 无 baseline）——单独计数，好在报告里如实说明
    nog = db.query_one(
        "SELECT count(*) AS n FROM eval_case "
        " WHERE active AND baseline_items IS NULL" + (" AND kind = %(kind)s" if kind else ""),
        {"kind": kind} if kind else {})
    # 被**有意跳过**的两类，如实报告（否则调用方会以为"都填好了"）
    kcond = " AND kind = %(kind)s" if kind else ""
    kparams = {"kind": kind} if kind else {}
    base_where = "active AND baseline_items IS NOT NULL" + kcond
    skipped_frozen = db.query_one(
        f"SELECT count(*) AS n FROM eval_case WHERE {base_where} AND frozen", kparams)["n"]
    skipped_annotated = db.query_one(
        f"SELECT count(*) AS n FROM eval_case WHERE {base_where} AND NOT frozen "
        f"AND expected_items IS NOT NULL AND NOT prefilled", kparams)["n"]

    rules = rules_mod.load_rules()
    plan, filled, invalid_total = [], [], 0
    for r in rows:
        raw = r.get("baseline_items") or []
        # 走与生产同一套校验：分值不在档位 / 未知规则的项会被剔除，
        # 避免把非法项预填进去（人工看到的是"可保存的合法草稿"）
        valid, bad = rules_mod.validate_items(raw, rules)
        invalid_total += len(bad)
        pos = [i for i in valid if i.get("polarity") == "positive"]
        neg = [i for i in valid if i.get("polarity") == "negative"]
        score = rules_mod.compute_score(pos, neg, rules)
        grade = rules_mod.grade_of(score["raw_score"], rules)
        filled.append(r["case_id"])
        plan.append({"case_id": r["case_id"], "items": valid,
                     "raw_score": score["raw_score"], "grade": grade["grade"],
                     "invalid": len(bad)})

    if not dry_run and plan:
        for p in plan:
            eval_case_repo.set_prefill(p["case_id"], items=p["items"],
                                       raw_score=p["raw_score"], grade=p["grade"],
                                       annotator=annotator, actor=actor)

    return {"ok": True, "dry_run": bool(dry_run),
            "total": len(rows),
            "filled": len(plan), "case_ids": filled,
            "invalid_items_dropped": invalid_total,
            "skipped_no_baseline": int((nog or {}).get("n") or 0),
            "skipped_annotated": int(skipped_annotated or 0),
            "skipped_frozen": int(skipped_frozen or 0),
            "plan_preview": [{k: p[k] for k in ("case_id", "grade", "raw_score", "invalid")}
                             for p in plan[:10]]}


# ============================================================ 3. 跑单条（只算不落库）

def run_case(case: dict, rules: dict = None, fetch_tags: bool = None) -> dict:
    """对一条样本跑一次「当前规则 + 当前提示词」的判定，**只返回结果，不落任何业务表**。

    与 `ScoringService._score_one` 的前半段逐条对齐（含 engine 硬指标、极性过滤），
    唯一差别是**不做 `_persist_assessment` / `_tag_evidence_messages` / `_writeback`**。
    """
    rules = rules or rules_mod.load_rules()
    if fetch_tags is None:
        fetch_tags = db.setting_bool("scoring_fetch_tags", "SCORING_FETCH_TAGS", False)
    staff_id, session_id = case["staff_id"], case["session_id"]
    rel = f"{staff_id}:{case.get('target_id')}"

    # ⚠️ 评估集必须用**固定、可复现**的上下文，不能用生产那套"按水位取增量"。
    #
    # 踩过的坑（实测）：`build_context(delta=True)` 的 ③ 段 = 上次评估水位之后的新消息。
    # 而评估样本都是从**已评过的会话**里抽的 → 水位很新 → ③ 几乎为空，
    # ④ 关键历史只拉 K2 规则集命中的少数几条，⑤ 兜底要 `kept < 4` 才触发。
    # 结果：v4 在 160 条消息的会话上**只看到 5 条**就去打分 → 命中项几乎全丢，
    # 指标变成"少看 155 条消息的 v4" vs "看全的旧版" → 测什么都不是。
    #
    # 所以评估走 `delta=False`：语义是「最近 N 条」，**只取决于会话内容、与何时跑无关**。
    # 代价是它不模拟生产的水位+记忆路径 —— 但那本来就不是评估集要回答的问题：
    # 评估集回答的是「给定这段对话，规则/提示词判得对不对」，必须可复现。
    _ctx_limit = db.setting_int("eval_ctx_limit", "EVAL_CTX_LIMIT", 300)
    ctx = sctx.build_context(staff_id, session_id, limit=_ctx_limit,
                             session_meta={"staff_id": staff_id, "session_id": session_id},
                             rel_key=rel, key_history=False, delta=False)
    min_kept = db.setting_int("ctx_min_kept", "CTX_MIN_KEPT", 4)
    if ctx["stats"]["kept"] < min_kept:
        return {"ok": False, "reason": "skipped", "detail": "有效条数不足，生产链路会跳过该会话",
                "items": [], "raw_score": None, "grade": None}

    # P2：评估集必须用**固定锚点**跑（样本的 `anchor_ts` = 该样本 baseline 那次评估的时刻），
    # 否则 `negative_no_reply_2d` 会随日历自己出现/消失 —— 实测同一批样本两个时刻跑，
    # 含该项的样本数 17 → 21，指标因此不可比、门禁跟着抖。
    facts = sctx.compute_facts(staff_id, session_id, fetch_tags=fetch_tags,
                               as_of=case.get("anchor_ts"))
    res = analyze.analyze(
        ctx.get("text", ""), facts["text"], rules,
        evidence_index=ctx.get("index"), labels=ctx.get("labels"),
        current_date=ctx.get("current_date"),
        temperature=db.setting_float("llm_temperature", "LLM_TEMPERATURE", analyze.DEFAULT_TEMPERATURE),
        max_tokens=db.setting_int("llm_max_tokens", "LLM_MAX_TOKENS", 0) or None,
        max_repairs=db.setting_int("rejudge_retries", "REJUDGE_RETRIES", analyze.DEFAULT_MAX_REPAIRS))

    engine_ids = rules_mod.engine_rule_ids(rules)
    neg = [i for i in (res.get("negative_items") or []) if i.get("rule_id") not in engine_ids]
    neg += rules_mod.engine_negative_items(facts, rules)
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
    """[{rule_id, score}] → {rule_id: score}（同 rule 多条时取分值绝对值最大者）。

    入参先过 `db.as_dict_list()`：它可能是 list、也可能是 json 字符串（原先这里
    假设是 list，别的模块又各自处理字符串 —— 同一个知识 5 份实现）。
    """
    out = {}
    for i in db.as_dict_list(items):
        rid = i.get("rule_id")
        if not rid:
            continue
        sc = int(i.get("score") or 0)
        if rid not in out or abs(sc) > abs(out[rid]):
            out[rid] = sc
    return out


#: JSONB 读回来可能是 list 或 json 字符串 → 统一成 list（全项目一个口径：`db.as_dict_list`）
_as_items = db.as_dict_list


def metrics(pairs: list, engine_ids=None) -> dict:
    """汇总指标。`pairs` = [{expected_items, actual_items, expected_grade, actual_grade}]。

    命中项级 F1 按 **rule_id 集合**算（这是文档说的「命中项 F1」）；另外单独给
    `item_exact_rate`（命中项里**档位分值也对**的比例）—— 只命中 rule 但给错档位，
    业务上同样是错判，F1 看不出来。

    传了 ``engine_ids``（`rules.engine_rule_ids()`）时**额外**给两个数，**不动 `rule_f1`**：

    · ``rule_f1_llm`` —— 排除引擎硬指标后的 F1，回答「模型判得准不准」。
      为什么要有它：``negative_no_reply_2d``（48h 未回复）这类引擎项由后台按**当前时间**
      算出来，会随日历自己出现/消失（实测：同一批样本两个时刻跑，含该项的样本数
      17 → 21）。把它算进主指标，等于让「时间流逝」污染「判得准不准」。
      刻意**不覆盖** ``rule_f1``：历史 `eval_run` 里的数字要能与新的可比。

    · ``engine_drift_rate`` —— 引擎项与人工标注不一致的样本占比，回答「这条硬指标稳不稳」。
      它高 = 该规则在随数据/时间漂移，而不是模型退化。
    """
    TP = FP = FN = EXACT = 0
    TP2 = FP2 = FN2 = 0                 # 排除引擎项后的 TP/FP/FN
    eng_drift = eng_seen = 0
    grade_ok = cases_with_fp = valid = 0
    # ⚠️ 「被跳过」与「没跑成」必须分开（2026-10-16）：
    #   · skipped —— 有效条数不足，生产链路本来也会跳过 → 合理排除
    #   · error   —— 模型/契约异常（限流、连续不合契约、网络）→ **必须单独计数**
    # 此前两者都是 `actual_grade is None`，一句 `continue` 一起被剔除出指标分母，
    # 于是"模型抽风"静默变成"这轮样本少了几条"：`case_valid` 缩水 → 门禁容差
    # （`max(2%, 1/有效样本数)`）变宽 → 保护被悄悄削弱，且没有任何告警。
    skipped = errors = 0
    miss_detail = defaultdict(int)

    for p in pairs:
        if p.get("actual_grade") is None:
            if (p.get("reason") or "") == "error":
                errors += 1
            else:
                skipped += 1
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
        if engine_ids:
            e2 = {k: v for k, v in e.items() if k not in engine_ids}
            a2 = {k: v for k, v in a.items() if k not in engine_ids}
            TP2 += len(set(e2) & set(a2))
            FP2 += len(set(a2) - set(e2))
            FN2 += len(set(e2) - set(a2))
            ee = {k for k in e if k in engine_ids}
            aa = {k for k in a if k in engine_ids}
            if ee or aa:                       # 只统计「样本里有引擎项」的样本
                eng_seen += 1
                if ee != aa:
                    eng_drift += 1

    precision = TP / (TP + FP) if (TP + FP) else 1.0
    recall = TP / (TP + FN) if (TP + FN) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    n = valid or 1
    out = {
        "case_total": len(pairs), "case_valid": valid,
        "case_skipped": skipped, "case_error": errors,
        "tp": TP, "fp": FP, "fn": FN,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "rule_f1": round(f1, 4),
        "item_exact_rate": round(EXACT / TP, 4) if TP else 1.0,
        "grade_acc": round(grade_ok / n, 4),
        "fp_case_rate": round(cases_with_fp / n, 4),
        "miss_top": dict(sorted(miss_detail.items(), key=lambda kv: -kv[1])[:10]),
    }
    if engine_ids:
        p2 = TP2 / (TP2 + FP2) if (TP2 + FP2) else 1.0
        r2 = TP2 / (TP2 + FN2) if (TP2 + FN2) else 1.0
        out["rule_f1_llm"] = round(2 * p2 * r2 / (p2 + r2), 4) if (p2 + r2) else 0.0
        out["llm_tp"] = TP2
        out["llm_fp"] = FP2
        out["llm_fn"] = FN2
        out["engine_drift_rate"] = round(eng_drift / eng_seen, 4) if eng_seen else 0.0
        out["engine_cases"] = eng_seen
    return out


# ============================================================ 5. 回归门禁

def coverage() -> dict:
    """评估集对**每条需要模型判定的规则**的覆盖度（修 P9）。

    为什么需要它：评估集是按会话抽样的，规则却是按维度加的 —— 新加一条规则时，
    **很可能一条样本都没覆盖到它**，于是"改了但测不出来"，门禁对它天然免疫。
    这个报告把「哪条规则 0 样本」摆到台面上（发布时的覆盖提示也复用它的结果）。

    口径：只看 `kind='llm'`（需要模型表态）的规则；`engine=rule` 的硬指标不需要样本，
    它的稳定性由 `metrics()["engine_drift_rate"]` 回答 —— 两者分工不重叠。
    """
    rules = rules_mod.load_rules()
    need = set(rules_mod.llm_rule_ids(rules))
    rows = db.query("SELECT expected_items FROM eval_case "
                    " WHERE active AND expected_items IS NOT NULL")
    hit = defaultdict(int)
    neg = defaultdict(int)
    for r in rows:
        for it in db.as_dict_list(r.get("expected_items")):
            rid = it.get("rule_id")
            if not rid:
                continue
            hit[rid] += 1
            if str(it.get("polarity")) == "negative" or int(it.get("score") or 0) < 0:
                neg[rid] += 1
    dim = {}
    for key in ("positive_rules", "negative_rules"):
        for r in rules.get(key, []):
            dim[r["rule_id"]] = r.get("dimension") or ""
    by_rule = [{"rule_id": rid, "dimension": dim.get(rid, ""),
                "cases": hit.get(rid, 0), "negative_cases": neg.get(rid, 0)}
               for rid in sorted(need)]
    by_rule.sort(key=lambda x: (x["cases"], x["rule_id"]))
    return {"cases": len(rows), "rule_total": len(need),
            "covered": sum(1 for x in by_rule if x["cases"] > 0),
            "uncovered": [x["rule_id"] for x in by_rule if x["cases"] == 0],
            "by_rule": by_rule}


def alerts() -> dict:
    """评估侧的告警汇总（修 P8）：**一处看全「现在有什么不对」**。

    为什么需要它：误报率排行榜只有人打开 `/api/eval/meta-loop` 才看得到；
    门禁越界只写一行审计；凭据失效只写日志。三个信号都"存在但没有出口"，
    于是 2026-10-13 的正文通道断了 3 天、10-15 的同步整天空转，都没人发现。

    本函数只做**聚合**（不新增判定口径）：能复用的都复用 —— 误报率来自
    `meta_loop.rule_fp_ranking()`、越界来自最近一次 `eval_run`、凭据新鲜度来自
    `wb_keepalive.load_bootstrap()` 的 `captured_at` + 最近一次成功的 `sync_job`。

    返回 `{"items": [...], "counts": {...}}`；`items` 为空 = 一切正常。
    """
    items = []

    # ① 最近一次跑分：门禁越界（逐指标 diff）
    last = db.query_one(
        """SELECT id, gate_passed, gate_diffs, metrics, finished_at
             FROM eval_run ORDER BY id DESC LIMIT 1""") or {}
    for d in db.as_dict_list(last.get("gate_diffs")):
        if d.get("ok") is False:
            items.append({"level": "warn", "kind": "gate_regression",
                          "message": f"最近一次跑分（#{last.get('id')}）指标回退："
                                     f"{d.get('metric')} {d.get('base')} → {d.get('now')}",
                          "detail": d})

    # ② 单规则误报率超阈（复用 meta_loop 的既有口径，不重算）
    # ②′ 最近一次跑分里有多少样本**根本没跑成**（模型/契约异常被排除在指标外）
    m_last = last.get("metrics") or {}          # psycopg 直接把 JSONB 返成 dict
    ce = int(m_last.get("case_error") or 0)
    if ce:
        total = int(m_last.get("case_total") or 0)
        ratio = (ce / total) if total else 1.0
        items.append({
            "level": "err" if ratio >= 0.2 else "warn", "kind": "eval_case_errors",
            "message": (f"最近一次跑分（#{last.get('id')}）有 {ce}/{total} 条样本**未跑成**"
                        f"（模型/契约异常）—— 这些样本不在指标分母里，指标代表性下降"),
            "detail": {"run_id": last.get("id"), "case_error": ce, "case_total": total,
                       "case_valid": m_last.get("case_valid"),
                       "case_skipped": m_last.get("case_skipped")}})

    try:
        import meta_loop
        for a in (meta_loop.rule_fp_ranking().get("alerts") or []):
            items.append({"level": "warn", "kind": "rule_false_positive",
                          "message": f"规则 {a.get('rule_id')} 误报率 {a.get('fp_rate')}"
                                     f"（AI 命中 {a.get('ai_hits')} 次）",
                          "detail": a})
    except Exception as e:                      # noqa: BLE001  聚合失败不该让接口 500
        items.append({"level": "info", "kind": "meta_loop_unavailable",
                      "message": f"误报率排行榜读取失败：{type(e).__name__}: {e}"})

    # ③ 复核积压（人工是最稀缺的资源；队列堵住 = 学习闭环停摆）
    th = db.setting_int("eval_alert_review_backlog", "EVAL_ALERT_REVIEW_BACKLOG", 50)
    pend = db.query_one("SELECT count(*) AS n FROM negative_review WHERE status='pending'") or {}
    if th and int(pend.get("n") or 0) > th:
        items.append({"level": "warn", "kind": "review_backlog",
                      "message": f"待复核 {pend['n']} 条，超过阈值 {th}",
                      "detail": {"pending": pend["n"], "threshold": th}})

    # ④ 凭据 / 同步新鲜度（正文通道断过一次的先例）
    age_h = max_h = None
    try:
        import wb_keepalive
        boot = wb_keepalive.load_bootstrap() or {}
        if boot.get("captured_at"):
            age_h = round((time.time() - float(boot["captured_at"])) / 3600, 1)
    except Exception:                           # noqa: BLE001
        age_h = None
    max_h = db.setting_int("eval_alert_credential_hours", "EVAL_ALERT_CREDENTIAL_HOURS", 36)
    if age_h is None:
        items.append({"level": "warn", "kind": "credential_missing",
                      "message": "读不到凭据文件（.cache/ww_bootstrap.json）—— 同步/解密通道不可用"})
    elif max_h and age_h > max_h:
        items.append({"level": "warn", "kind": "credential_stale",
                      "message": f"凭据 {age_h} 小时未刷新（阈值 {max_h} 小时）"
                                 f"—— 宿主机需跑 wb_keepalive.py",
                      "detail": {"age_hours": age_h, "threshold": max_h}})
    ok_sync = db.query_one(
        """SELECT max(finished_at) AS last_ok FROM sync_job WHERE status='success'""") or {}
    if ok_sync.get("last_ok"):
        idle_h = round((time.time() - ok_sync["last_ok"].timestamp()) / 3600, 1)
        if idle_h > 26:                          # 日常任务每天一轮，超过 26 小时没成功就是异常
            items.append({"level": "warn", "kind": "sync_stalled",
                          "message": f"最近一次成功同步在 {idle_h} 小时前 —— 检查日常任务/凭据",
                          "detail": {"idle_hours": idle_h}})

    counts = {}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    return {"items": items, "counts": counts, "count": len(items),
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


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
                  baseline_path: str = None, eval_run_id: int = None) -> dict:
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

    ``eval_run_id``（两步发布，修 P4）：调用方先 `POST /api/eval/start` 跑一轮（后台，
    十几分钟），再把那次 `run_id` 传进来。传了它就用**那一次**的指标，并且**必须**
    通过两道校验：① 是当前规则版本 + 当前提示词版本跑的；② 在 `eval_gate_fresh_minutes`
    分钟内。过不了就当"无法判定"（而不是拿它当通过）。
    """
    enabled = db.setting_bool("eval_gate_enabled", "EVAL_GATE_ENABLED", True)
    path = baseline_path or db.setting("eval_baseline_path", "EVAL_BASELINE_PATH", "")
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
           "blocked": False, "gate_passed": None, "diffs": [], "reason": "",
           "metrics": None, "stale": False, "baseline_run_id": base.get("run_id"),
           "baseline_set_at": base.get("set_at"), "baseline_source": base.get("source")}

    if not enabled:
        out["reason"] = "回归门禁已关闭（eval_gate_enabled=false）"
        return out
    if not out["has_baseline"]:
        out["reason"] = ("尚无基线（`app_setting.eval_baseline_run_id` 与 "
                         "`eval/baseline.json` 都没有）→ **无法判定**，放行但不代表通过。"
                         "先把一次跑分结果冻结为基线：POST /api/eval/baseline")
        return out
    # ⚠️ **缺键必须吵**（否则安静地失去保护）：`gate()` 对基线里没有的指标是 `continue`，
    #    所以"基线是旧版跑分（缺 `rule_f1_llm`）"这种情形会让那项**完全不设防**却毫无提示。
    missing = [k for k in GATE_METRICS if k not in (base.get("metrics") or {})]
    if missing:
        out["missing_metrics"] = missing

    # 显式指定的跑分（两步发布）：校验"对版 + 新鲜"，过了就直接用它
    if eval_run_id:
        row = db.query_one("""SELECT id, metrics, rule_version, prompt_version, finished_at
                                FROM eval_run WHERE id = %s""", (int(eval_run_id),))
        if not row or not row.get("metrics"):
            out["reason"] = f"指定的跑分记录 #{eval_run_id} 不存在或没有指标 → 无法判定"
            return out
        cur = rules_mod.load_rules()
        age_min = ((time.time() - row["finished_at"].timestamp())
                   if row.get("finished_at") else None)
        fresh_min = db.setting_int("eval_gate_fresh_minutes", "EVAL_GATE_FRESH_MINUTES", 30)
        bad = []
        if row.get("rule_version") != cur.get("rule_version"):
            bad.append(f"规则版本 {row.get('rule_version')} ≠ 当前 {cur.get('rule_version')}")
        if row.get("prompt_version") != analyze.PROMPT_VERSION:
            bad.append(f"提示词版本 {row.get('prompt_version')} ≠ 当前 {analyze.PROMPT_VERSION}")
        if age_min is not None and age_min > fresh_min * 60:
            bad.append(f"该跑分已在 {age_min / 60:.0f} 分钟前（阈值 {fresh_min} 分钟）")
        if bad:
            out["reason"] = ("指定的跑分不能作为门禁依据：" + "；".join(bad)
                             + " → 无法判定，放行但不代表通过。请重新跑一轮再发布。")
            out["stale"] = True
            return out
        out["metrics"] = row["metrics"]

    # 有基线 → 需要本次指标。**先用进程内上一次跑分结果，但必须验新鲜度**（修 P5）：
    # 改造前只要 `_STATE["result"]` 里有 metrics 就直接拿来比 —— 不看它是**哪一版规则**
    # 跑的，也不看是不是几小时前的。后果：改完 rules.json 在同一进程里发布，门禁会拿
    # "改动之前"的指标去比基线 → 虚假通过（或虚假拦截）。
    now_metrics = out.get("metrics")          # 已由 `eval_run_id` 指定 → 直接用
    stale = False
    cached = {} if now_metrics else (status().get("result") or {})
    if cached.get("metrics"):
        cur = rules_mod.load_rules()
        age = time.time() - float(cached.get("finished_at") or 0)
        fresh_min = db.setting_int("eval_gate_fresh_minutes", "EVAL_GATE_FRESH_MINUTES", 30)
        same_rv = cached.get("rule_version") == cur.get("rule_version")
        same_pv = cached.get("prompt_version") == analyze.PROMPT_VERSION
        if same_rv and same_pv and age <= fresh_min * 60:
            now_metrics = cached["metrics"]
        else:
            stale = True
            why = []
            if not same_rv:
                why.append(f"规则版本 {cached.get('rule_version')} ≠ 当前 {cur.get('rule_version')}")
            if not same_pv:
                why.append(f"提示词版本 {cached.get('prompt_version')} ≠ 当前 {analyze.PROMPT_VERSION}")
            if age > fresh_min * 60:
                why.append(f"距今 {age / 60:.0f} 分钟 > {fresh_min} 分钟")
            out["stale_reason"] = "；".join(why)
    if now_metrics is None and run_if_possible:
        # ⚠️ 只认**人工确认过**的标注（`NOT prefilled`）：AI 预标注是自己批自己，
        #    计入指标会得到虚高的 F1 与「虚假通过」，比没有门禁更危险。
        labeled = db.query_one(
            "SELECT count(*) AS n FROM eval_case "
            " WHERE active AND expected_items IS NOT NULL AND NOT prefilled")
        if not (labeled or {}).get("n"):
            pf = db.query_one(
                "SELECT count(*) AS n FROM eval_case "
                " WHERE active AND expected_items IS NOT NULL AND prefilled")
            # ⚠️ 这 N 条**有内容但仍是草稿态**（`prefilled=true`）。行本身分不出
            #    "AI 草稿没人碰过" 与 "人工保存过、但当时的保存路径没清这个标记"
            #    —— 2026-10-15 就踩到了后者（`set_annotation` 的清标记是当天 13:06
            #    才落盘的，而 30 条是 07:30 保存的）。所以文案要给出**动作**：
            #    重存一次即可生效，而不是让人以为"评估集没人碰过"又白标一遍。
            extra = (f"（另有 {pf['n']} 条仍是**草稿态**：它们有内容、但不是金标准。"
                     f"若其中包含人工确认过的内容，在评估页重存一次即转为「已确认」）"
                     ) if (pf or {}).get("n") else ""
            out["reason"] = ("有基线，但**没有已人工确认的标注样本** → 无法计算指标，放行但不代表通过。"
                             "请在评估页（`#/eval`）复核标注后用 POST /api/eval/run 复算。" + extra)
            return out
        res = run_sync(kind=kind, limit=limit, check_drift=False)
        now_metrics = res.get("metrics")
    if now_metrics is None:
        if stale:
            out["reason"] = (f"上一次跑分结果**已过期/不对版**（{out.get('stale_reason')}）"
                             f"→ 不复用，且本次不现跑（run_if_possible=false）"
                             f"→ 无法判定，放行但不代表通过。"
                             f"请先跑一轮评估：POST /api/eval/gate/start（后台）或 /api/eval/run。")
        else:
            out["reason"] = "拿不到本次指标（没有跑分结果）→ 放行但不代表通过"
        out["stale"] = stale
        return out

    # 容差随样本量（修 P6）：30 条样本时 1 条翻转就是 3.3pp，比 `GATE_TOL=0.02` 还大，
    # 于是"容差"形同虚设、单条抖动就能阻断；样本多时才回到百分比口径。
    tol = max(GATE_TOL, 1.0 / max(int(now_metrics.get("case_valid") or 1), 1))
    passed, diffs = gate(now_metrics, base.get("metrics") or {}, tol=tol)
    out.update({"decidable": True, "gate_passed": passed, "diffs": diffs,
                "metrics": now_metrics, "blocked": not passed,
                "stale": stale, "tol": round(tol, 4)})
    # 覆盖率提示（修 P9，**不硬拦**）：某条规则样本太少 = 这次改动很可能测不到它。
    try:
        min_cases = db.setting_int("eval_min_cases_per_rule", "EVAL_MIN_CASES_PER_RULE", 3)
        if min_cases:
            weak = [x["rule_id"] for x in coverage()["by_rule"]
                    if 0 < x["cases"] < min_cases]
            thin = coverage()["uncovered"]
            if thin or weak:
                out["coverage_warning"] = {"no_sample": thin, "below_min": weak,
                                           "min_cases": min_cases}
    except Exception:                           # noqa: BLE001  提示失败不该影响门禁判定
        pass
    out["reason"] = ("指标持平或变好 → 允许发布" if passed
                     else "**指标下降 → 阻断发布**（逐指标 diff 见 diffs）")
    if out.get("missing_metrics"):
        out["reason"] += (f"；⚠️ 基线里没有这些指标：{out['missing_metrics']}"
                          f" —— 它们**本次不受保护**（基线是旧版跑分所致）。"
                          f"用新跑分重新冻结基线即可补上")
    if out.get("coverage_warning"):
        cw = out["coverage_warning"]
        out["reason"] += (f"；⚠️ 覆盖提示：{len(cw['no_sample'])} 条规则**无样本**、"
                          f"{len(cw['below_min'])} 条不足 {cw['min_cases']} 条 —— "
                          f"这些规则改坏了也测不出来")

    # ⚠️ 「有多少样本根本没跑成」必须出现在门禁结论里（2026-10-16）：
    #    模型/契约异常既不进 F1、也不进 case_valid —— 那等于**悄悄把保护放松**：
    #    有效样本越少，容差 `max(2%, 1/有效样本数)` 越宽。此前这个数字在系统里
    #    没有任何出口（只在 `eval_run.detail.cases[].reason` 里躺着）。
    m_now = out.get("metrics") or {}
    case_error = int(m_now.get("case_error") or 0)
    case_total = int(m_now.get("case_total") or 0)
    if case_error:
        out["case_error"] = case_error
        out["case_skipped"] = int(m_now.get("case_skipped") or 0)
        out["reason"] += (f"；⚠️ 本轮有 **{case_error} 条样本未跑成**（模型/契约异常，"
                          f"分母只剩 {int(m_now.get('case_valid') or 0)}/{case_total} 条）"
                          f" —— 指标不完整，不代表质量没退步")
        # 大量失败时连"判定"都不该做：拿残缺指标下结论比不判更危险
        if case_total and case_error >= max(2, int(case_total * CASE_ERROR_UNDECIDABLE_RATIO)):
            out["decidable"] = False
            out["blocked"] = False
            out["reason"] += (f"；未跑成比例 ≥ {int(CASE_ERROR_UNDECIDABLE_RATIO * 100)}% "
                              f"→ **无法判定**（放行但不代表通过），请重跑一轮")
    return out


#: 未跑成的样本占比达到多少就**不予判定**（而不是拿残缺指标下结论）。
#: 20% 是刻意保守：样本 27 条时约等于 5 条失败 —— 到那个程度，
#: F1/grade_acc 已经不反映真实水平，此时"放行"必须明说是"没判定"，不能伪装成"通过"。
CASE_ERROR_UNDECIDABLE_RATIO = 0.20


#: 基线指向哪次跑分（`app_setting` 键）。**刻意不注册进 `settings_registry`**：
#: 注册即出现在设置页、并可由前端写入 → 谁都能把基线指到任意一次跑分上。
#: 只允许经 `POST /api/eval/baseline` 变更（那里有角色校验 + 审计）。
BASELINE_RUN_KEY = "eval_baseline_run_id"


def load_baseline() -> dict:
    """当前基线：**先库、后文件**（修 P7）。

    为什么从"只读文件"改成"库优先"：文件基线丢过一次 —— 实测 2026-10-15 07:35:16
    用 `POST /api/eval/baseline` 设过（审计留痕、`gate_diffs` 里有 base 值），
    几小时后文件就不在了，门禁静默退回"无法判定、放行"。而 `retention.py` 明确
    **不删** `eval_run` —— 落库比落文件持久，而且天然带 `rule_version` / `prompt_version`
    / `finished_at`（门禁判断"基线是哪一版"本来就需要它们）。

    文件保留为**兜底 + 人工留档**，读不到库时仍然生效。
    """
    rid = db.setting(BASELINE_RUN_KEY, "EVAL_BASELINE_RUN_ID", "")
    if rid:
        try:
            row = db.query_one(
                """SELECT id, kind, metrics, rule_version, prompt_version,
                          case_total, finished_at
                     FROM eval_run WHERE id = %s""", (int(rid),))
        except (TypeError, ValueError):
            row = None
        if row and row.get("metrics"):
            return {"set_at": (row["finished_at"].strftime("%Y-%m-%dT%H:%M:%S")
                               if row.get("finished_at") else None),
                    "run_id": row["id"], "kind": row.get("kind"),
                    "rule_version": row.get("rule_version"),
                    "prompt_version": row.get("prompt_version"),
                    "case_total": row.get("case_total"),
                    "metrics": row["metrics"], "source": "db"}
    if not BASELINE_PATH.exists():
        return {}
    try:
        return {**json.loads(BASELINE_PATH.read_text(encoding="utf-8")), "source": "file"}
    except Exception:                           # noqa: BLE001
        return {}


def save_baseline(metrics_dict: dict, *, kind: str = "", rule_version: str = "",
                  prompt_version: str = "", case_total: int = 0, note: str = "",
                  run_id: int = None) -> dict:
    """把一次跑分结果冻结为基线（门禁的对比参照）。

    落**两处**：① `app_setting['eval_baseline_run_id']` → `eval_run` 行（主要，随库持久）；
    ② `eval/baseline.json`（兼容旧读取方 + 便于人工留档）。
    """
    if run_id:
        db.set_setting(BASELINE_RUN_KEY, str(int(run_id)))
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"set_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind,
               "run_id": int(run_id) if run_id else None,
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


def _cases(kind: str, limit: int, case_ids: list = None,
           include_prefilled: bool = False) -> list:
    """取「可跑分」的样本。

    ⚠️ 默认**排除 AI 预标注**（`prefilled`）：预填内容来自 AI 自己，若计入指标
    就成了「AI 批改自己」→ F1 虚高、门禁失效。要对照「AI 自评一致性」时才开
    ``include_prefilled=True``（结果不能当门禁依据）。
    """
    params, cond = {}, ["active = true", "expected_items IS NOT NULL"]
    if not include_prefilled:
        cond.append("NOT prefilled")
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
             case_ids: list = None, include_prefilled: bool = False) -> dict:
    """同步跑一遍评估集：逐条 `run_case` → 汇总 → 落 `eval_run` → 门禁。

    ``include_prefilled=True`` 会把 AI 预标注也计入（**仅供对照，不能当门禁依据**）。
    """
    t0 = time.time()
    cases = _cases(kind, limit, case_ids, include_prefilled=include_prefilled)
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
                      "actual_grade": got.get("grade"),
                      # 带上原因：`metrics()` 要用它区分「跳过」与「没跑成」
                      "reason": got.get("reason")})
        details.append({"case_id": c["case_id"], "staff_id": c["staff_id"],
                        "session_id": c["session_id"], "expected_grade": c.get("expected_grade"),
                        "actual_grade": got.get("grade"), "expected_raw": c.get("expected_raw_score"),
                        "actual_raw": got.get("raw_score"), "ok": got.get("ok"),
                        "reason": got.get("reason"), "items": got.get("items"),
                        "ctx_stats": got.get("ctx_stats")})
        if on_progress:
            on_progress(idx, len(cases), c["case_id"])

    # 传 engine_ids → 额外产出 `rule_f1_llm`（排除 48h 这类引擎硬指标）与
    # `engine_drift_rate`（该指标与人标的不一致率）。**`rule_f1` 语义不变**，
    # 历史 eval_run 仍可比；门禁切到新键是后续步骤（P4）。
    m = metrics(pairs, engine_ids=rules_mod.engine_rule_ids(rules))
    base = load_baseline()
    # 容差随样本量（修 P6）：30 条时 1 条翻转 = 3.3pp > `GATE_TOL=0.02`，
    # 单条抖动就能阻断；样本多时才回到百分比口径。
    tol = max(GATE_TOL, 1.0 / max(int(m.get("case_valid") or 1), 1))
    passed, diffs = gate(m, base.get("metrics") or {}, tol=tol)
    elapsed = round(time.time() - t0, 1)

    row = db.query_one(
        """INSERT INTO eval_run (kind, rule_version, prompt_version, case_total, case_ok,
                                 metrics, gate_passed, gate_diffs, status, elapsed_sec, detail,
                                 finished_at, baseline_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'success',%s,%s,now(),%s) RETURNING id""",
        (kind or None, rules.get("rule_version"), analyze.PROMPT_VERSION,
         len(cases), m["case_valid"], db.as_json(m),
         passed if base.get("metrics") else None, db.as_json(diffs) if diffs else None,
         elapsed, db.as_json({"cases": details, "drift": drift, "tol": round(tol, 4),
                              "baseline_at": base.get("set_at"),
                              "baseline_run_id": base.get("run_id")}),
         # `baseline_id`（修 2026-10-16）：这一列**从建表起就没人写过**，
         # 于是"这轮跑分是拿哪条基线比出来的"只能去 `detail` 这个 JSON 里翻。
         # 现在按列填上（同一份事实只保留一处真相；detail 里的仍留作历史兼容）。
         base.get("run_id")))
    # `rule_version` / `prompt_version` / `finished_at` / `case_ids` 是**门禁验新鲜度**
    # 必需的字段（修 P5）：没有它们，`gate_decision` 无法判断进程内缓存的跑分结果
    # 是不是"当前这版规则 + 当前提示词 + 30 分钟内"跑的。
    out = {"ok": True, "run_id": row["id"], "case_total": len(cases), "case_ok": m["case_valid"],
           "drift": drift, "metrics": m, "gate_passed": passed if base.get("metrics") else None,
           "gate_diffs": diffs, "has_baseline": bool(base.get("metrics")), "tol": round(tol, 4),
           "elapsed_sec": elapsed,
           "baseline_id": base.get("run_id"),
           "rule_version": rules.get("rule_version"), "prompt_version": analyze.PROMPT_VERSION,
           "case_ids": [c["case_id"] for c in cases],
           "finished_at": int(time.time())}
    with _LOCK:
        _STATE.update({"result": out, "elapsed_sec": elapsed})
    return out


def _record_failed_run(kind: str, err: str) -> None:
    """整轮评估异常也要落一条 `eval_run(status='failed')` + 审计（2026-10-16）。

    为什么：后台跑评估时若 `run_sync` 抛异常（DB/样本读取、线程内的意外），
    此前只在内存里留了 `_STATE["error"]` —— **重启即失、库里零记录**，
    连"这轮跑过并失败了"都查不到。项目对批量的既定纪律是"失败要有载体"
    （`sync_job`/`scoring_run`/`session_failure` 都落库），评估不该例外。
    """
    try:
        rv = pv = None
        try:
            rv = rules_mod.load_rules().get("rule_version")
            import analyze as _an
            pv = getattr(_an, "PROMPT_VERSION", None)
        except Exception:                             # noqa: BLE001  版本取不到也要留痕
            pass
        row = db.query_one(
            """INSERT INTO eval_run (kind, rule_version, prompt_version, case_total, case_ok,
                                     metrics, status, error, elapsed_sec, finished_at)
               VALUES (%s,%s,%s,0,0,NULL,'failed',%s,0,now()) RETURNING id""",
            (kind or None, rv, pv, (err or "")[:2000]))
        try:
            import audit
            audit.record_action("system", "eval_run_failed", f"run:{row['id']}",
                                {"kind": kind, "error": (err or "")[:500]})
        except Exception as e2:                       # noqa: BLE001
            print(f"[eval] 落审计失败（跑分失败行已记）：{e2}")
    except Exception as e:                            # noqa: BLE001
        print(f"[eval] ⚠️ 连失败记录都写不进去：{type(e).__name__}: {e}｜原始错误：{err}")


def _no_cases_reason(kind: str, include_prefilled: bool) -> str:
    """「一条可评样本都没有」时说清**为什么**、以及**该做什么**（修 2026-10-16 的实测误判）。

    背景：评估集默认排除 AI 草稿（`prefilled`）—— 这是对的，草稿计入门禁等于
    「AI 批改自己」。但库里 30 条全是草稿时，「跑评估」会**跑 0 条、瞬间结束**、
    `eval_run` 落一条 `case_total=0`，界面上看起来"什么都没发生"
    （实测就有人据此判断"评测没生效"，其实评测集是好的，只是没有可用样本）。
    """
    tot = (db.query_one("SELECT count(*) AS n FROM eval_case WHERE active") or {}).get("n") or 0
    draft = (db.query_one("SELECT count(*) AS n FROM eval_case "
                          "WHERE active AND prefilled") or {}).get("n") or 0
    if draft and not include_prefilled:
        return (f"当前 0 条可评样本：{tot} 条样本里有 {draft} 条仍是**AI 草稿**"
                f"（草稿不计入指标，否则就是「AI 批改自己」）。"
                f"请在评估页逐条点「✓ 判对」或「保存修改」把它们转成金标准后再跑；"
                f"只想看方向性对照的话，可以显式带 `include_prefilled=true`"
                f"（**结果不能当门禁依据**）。")
    return (f"当前 0 条可评样本（kind={kind or '全部'}）：先「抽样」建样本并完成标注，"
            f"或检查样本是否都被停用 / 被冻结。")


def start(kind: str = "", limit: int = 0, include_prefilled: bool = False) -> dict:
    """起后台线程跑评估集（条数多 + 每条一次真实模型调用，HTTP 同步会超时）。

    ``include_prefilled``：**仅供方向性对照**（对比"改动前基线 vs 现在"），
    不能当门禁依据 —— 预填内容来自 AI 自己，计入指标就是"AI 批改自己"。
    之所以要有这个参数：同步路径 `/api/eval/run` 早就支持它，后台路径却没有，
    于是"想跑一次方向性对照"只能走会超时的同步接口 —— 能力不对齐本身就是缺陷。
    """
    with _LOCK:
        if _STATE["running"]:
            return {"ok": False, "reason": "已有评估在跑，请等待完成"}
        # ⚠️ 0 条可评样本就**不启动**（修 2026-10-16）：跑 0 条会瞬间结束、
        #    只落一条 case_total=0 的记录，用户看到的是"点了没反应"。
        #    这道检查必须在**拿到锁之后**（修 2026-10-16 复审）：放在锁外时，
        #    "已有评估在跑" 与 "0 条样本" 会竞争，用户可能得到后者 —— 而那会把人
        #    引去补样本，真正该做的是等这一轮跑完。
        if not _cases(kind, limit, include_prefilled=include_prefilled):
            return {"ok": False, "reason": _no_cases_reason(kind, include_prefilled)}
        _STATE["include_prefilled"] = bool(include_prefilled)
        _STATE.update({"running": True, "total": 0, "done": 0, "current": None,
                       "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "elapsed_sec": 0, "error": None, "result": None})

    def _prog(i, n, cid):
        with _LOCK:
            _STATE.update({"total": n, "done": i, "current": cid})

    def _body():
        try:
            run_sync(kind, limit, on_progress=_prog,
                     include_prefilled=include_prefilled)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            with _LOCK:
                _STATE["error"] = msg
            # ⚠️ 整轮异常也必须留痕（2026-10-16）：此前只写进程内 `_STATE["error"]`
            #    —— 重启即失、不落 `eval_run`、不写审计。若异常发生在插 `eval_run`
            #    行之前（DB/样本读取出错），这一轮在**库里完全没有记录**，
            #    与项目纪律"失败要有载体"（sync_job / scoring_run / session_failure）不一致。
            _record_failed_run(kind, msg)
        finally:
            with _LOCK:
                _STATE["running"] = False

    threading.Thread(target=_body, daemon=True, name="eval-run").start()
    return {"ok": True, "triggered_by": "manual"}
