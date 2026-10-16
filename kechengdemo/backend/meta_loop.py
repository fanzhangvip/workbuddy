# -*- coding: utf-8 -*-
"""meta-loop：复核改判 → 误报率排行 → 回流改规则 / 评估集（架构 §2.7 E5/G8）。

## 这一环为什么重要

前面所有环节产出的都是**单次结论**；meta-loop 是唯一把「人工判断」变回
「系统改进」的通道。没有它，人工复核的结论就只是躺在表里的一条记录 ——
下次遇到同样的会话，模型照样犯同样的错。

## 两个出口

1. **误报率排行榜**（`rule_fp_ranking`）：按 `rule_id` 聚合「AI 认为该扣分 / 人工却
   判定不该扣」的比例 → 直接指出「哪条规则在乱报」。这是**改 `rules.json` 的依据**
   （G8：误报率排行榜 → 回流改 criteria/examples）。
2. **改判回流评估集**（`backflow_reviews`）：把人工改判过的复核记录变成 `eval_case`
   —— 人工判断是**最贵的资源**，不该只用一次。回流后它们自动加入回归门禁，
   以后改规则想「悄悄变差」就过不了门禁了。

## 指标口径（必须说清，否则会误读）

· `fp_rate`：**人工否掉的 AI 命中项 / AI 命中项总数**。越高 = 这条规则越爱乱报
· `miss`：人工**新增**的扣分项（AI 没发现）→ 这条规则**漏报**
· `agreement`：AI 与人工完全一致的记录占比

⚠️ 只统计**人工已完成**（`status='done'`）的复核 —— 没人工结论的记录参与统计
等于把「AI 自己的判断」当成标准答案（自证清白）。
"""
import time

import audit
import eval_case_repo
import db
import review_repo

#: 误报率超过这个值就在报告里标红（业务阈值，可按需调）
FP_ALERT_THRESHOLD = 0.3

#: 至少有多少条人工样本才值得看（样本太少时比例无意义）
MIN_SAMPLES = 3


def _items_of(row, col) -> dict:
    """把 items 列转成 ``{rule_id: |score|}``。

    读 JSONB 的部分已收敛到 `db.as_dict_list()`（原先 5 处各写一遍）。
    """
    out = {}
    for it in db.as_dict_list(row.get(col)):
        if it.get("rule_id"):
            out[str(it["rule_id"])] = abs(int(it.get("score") or 0))
    return out


def rule_fp_ranking(limit: int = 30) -> dict:
    """按规则聚合「AI 命中 vs 人工判定」，产出误报率排行榜。

    只看**已有人工结论**的复核记录（`status='done'` 且有人工项）——
    否则就是拿 AI 的判断当标准答案。
    """
    rows = db.query(
        """SELECT id, assessment_id, ai_negative_items, human_negative_items,
                  ai_negative_total, human_negative_total, reviewer
             FROM negative_review
            WHERE status = 'done' AND human_negative_items IS NOT NULL
            ORDER BY id DESC LIMIT 2000""")

    agg = {}
    matched_records = 0
    for r in rows:
        ai = _items_of(r, "ai_negative_items")
        hu = _items_of(r, "human_negative_items")
        matched_records += 1
        for rid in set(ai) | set(hu):
            a = rid in ai
            h = rid in hu
            if not a and not h:
                continue
            k = agg.setdefault(rid, {"rule_id": rid, "ai_hits": 0, "human_keeps": 0,
                                     "false_positives": 0, "misses": 0, "confirmed": 0})
            if a:
                k["ai_hits"] += 1
            if a and h:
                k["human_keeps"] += 1
                k["confirmed"] += 1
            elif a and not h:
                k["false_positives"] += 1          # AI 报了，人工否掉
            elif h and not a:
                k["misses"] += 1                   # 人工补了，AI 没报

    for k in agg.values():
        k["fp_rate"] = round(k["false_positives"] / k["ai_hits"], 4) if k["ai_hits"] else 0.0
        k["alert"] = (k["ai_hits"] >= MIN_SAMPLES and k["fp_rate"] >= FP_ALERT_THRESHOLD)

    ranking = sorted(agg.values(), key=lambda x: (-x["fp_rate"], -x["false_positives"]))
    return {
        "reviews_with_human": matched_records,
        "rules": ranking[:limit],
        "alerts": [x for x in ranking if x["alert"]],
        "threshold": FP_ALERT_THRESHOLD,
        "min_samples": MIN_SAMPLES,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def backflow_reviews(limit: int = 100, kind: str = "regression",
                     actor: str = "meta_loop") -> dict:
    """把**人工改判过**的复核记录回流成 `eval_case`（E5）。

    造出来的样本带 ``expected_items``（= 人工结论），因此**立刻可用于回归门禁** ——
    人工判断是稀缺资源，不该只用一次。

    只回流 ``verification_status IN ('corrected','rejected')`` 对应的记录：
    人工与 AI 一致的记录**不回流**（那只是确认，不带来新信息，回流只会稀释评估集）。
    """
    import eval_runner as er

    rows = db.query(
        """SELECT nr.id, nr.assessment_id, nr.staff_id, nr.session_id, nr.target_id,
                  nr.staff_name, nr.target_name, nr.human_negative_items,
                  nr.reviewer, a.verification_status
             FROM negative_review nr
             LEFT JOIN customer_intent_assessment a ON a.assessment_id = nr.assessment_id
            WHERE nr.status = 'done'
              AND nr.human_negative_items IS NOT NULL
              AND nr.eval_case_id IS NULL
              AND COALESCE(a.verification_status, 'na') IN ('corrected', 'rejected')
            ORDER BY nr.id
            LIMIT %s""", (int(limit),))

    created, skipped = [], []
    for r in rows:
        try:
            # 样本漂移防护：指纹不符（对话已改）就不回流 —— 否则金标准对不上实际内容
            h = er.messages_hash(r["staff_id"], r["session_id"])
            exists = db.query_one(
                """SELECT case_id FROM eval_case
                    WHERE staff_id=%s AND session_id=%s AND kind=%s""",
                (r["staff_id"], r["session_id"], kind))
            if exists:
                review_repo.link_eval_case(r["id"], exists["case_id"])
                skipped.append({"review_id": r["id"], "reason": "样本已存在",
                                "case_id": exists["case_id"]})
                continue
            # 人工结论里只保留**负分**项（复核页只标负分），与 run_case 的合并口径一致
            items = []
            for it in db.as_dict_list(r["human_negative_items"]):
                if it.get("rule_id"):
                    items.append({"rule_id": it["rule_id"],
                                  "polarity": it.get("polarity") or "negative",
                                  "score": int(it.get("score") or 0),
                                  "reason": it.get("reason") or "人工复核结论"})
            import rules as rules_mod
            rules = rules_mod.load_rules()
            score = rules_mod.compute_score([], items, rules)
            grade = rules_mod.grade_of(score["raw_score"], rules)["grade"]
            # 建样本 + 回填关联**在同一事务**：只建不关联会留下「复核不知道进了哪个样本」
            with db.tx() as conn:
                cid = eval_case_repo.insert_from_review(
                    conn, kind=kind, staff_id=r["staff_id"], session_id=r["session_id"],
                    target_id=r["target_id"], staff_name=r["staff_name"],
                    target_name=r["target_name"], messages_hash=h, msg_count=0,
                    last_msg_time=None, items=items, raw_score=score["raw_score"],
                    grade=grade)
                review_repo.link_eval_case(r["id"], cid)
            created.append({"review_id": r["id"], "case_id": cid,
                            "expected_raw_score": score["raw_score"], "expected_grade": grade})
        except Exception as e:                  # noqa: BLE001
            skipped.append({"review_id": r["id"], "reason": f"{type(e).__name__}: {e}"})

    if created:
        audit.record_action(actor, "meta_backflow", f"eval_case:{kind}",
                            {"created": len(created),
                             "case_ids": [c["case_id"] for c in created]})
    return {"created": len(created), "skipped": len(skipped),
            "cases": created, "skips": skipped[:10], "kind": kind}


def link_review_to_case(review_id: int, kind: str = "regression") -> dict:
    """复核提交后的**自动回流**（单条）。

    为什么放在复核提交里而不是只靠定时任务：人工刚做完判断的**那一刻**
    上下文最完整（知道为什么改），立刻回流不会漏；定时批量是兜底。
    """
    return backflow_reviews(limit=1, kind=kind) if _eligible(review_id) else \
        {"created": 0, "skipped": 1, "reason": "该复核未改判（与 AI 一致），无需回流"}


def _eligible(review_id: int) -> bool:
    row = db.query_one(
        """SELECT COALESCE(a.verification_status,'na') AS v
             FROM negative_review nr
             LEFT JOIN customer_intent_assessment a ON a.assessment_id = nr.assessment_id
            WHERE nr.id = %s""", (int(review_id),))
    return bool(row) and row.get("v") in ("corrected", "rejected")


def report() -> dict:
    """meta-loop 总报告（前端一处看全）。"""
    rank = rule_fp_ranking()
    stat = db.query_one(
        """SELECT count(*) FILTER (WHERE status='done') AS done,
                  count(*) FILTER (WHERE eval_case_id IS NOT NULL) AS backflowed,
                  count(*) AS total
             FROM negative_review""") or {}
    vstat = db.query(
        """SELECT COALESCE(verification_status,'na') AS v, count(*) AS n
             FROM customer_intent_assessment GROUP BY 1 ORDER BY 2 DESC""")
    return {"ranking": rank, "review": stat, "verification": vstat,
            "eval_cases": db.query_one(
                """SELECT count(*) AS total,
                          count(*) FILTER (WHERE expected_items IS NOT NULL) AS labeled,
                          count(*) FILTER (WHERE source='review') AS from_review
                     FROM eval_case""") or {}}
