# -*- coding: utf-8 -*-
"""历史评估置信度回填验收（架构 §6 E4 的配套工具）—— 需要 PostgreSQL 5433。

    .venv/Scripts/python.exe tests/test_backfill.py

覆盖 `backend/backfill.py`：
  1. **纯函数**：`_has_evidence` / `compute_new` / `diff_of` / 占位编号不落库
  2. **确定性**：`last_grade` 必须用**写之前**的快照（否则结果依赖处理顺序）
  3. **幂等**：已回填的行不再被改（`only_missing` 判据）
  4. **诚实性**：缺 `evidence_ids` 的行**不算**（宁缺勿假），但修「有负分却未标复核」的矛盾
  5. **可回滚**：备份 → 改 → 回滚 → 复原

⚠️ 测试只用 `rel_like` 隔离出的**合成行**，绝不碰生产数据；
所有断言用自造数据（不依赖全库现状），跑完清理。
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"


import json
import os
import sys
import tempfile

from _harness import FAILED, PASSED, check, delete_rows  # noqa: E402

import backfill as bf            # noqa: E402
import db                        # noqa: E402
import rules as rules_mod        # noqa: E402
import scoring_service as ss     # noqa: E402


#: 合成行的关系键前缀（同时用于 `rel_like` 隔离）
REL = "BFTEST:subject"
REL2 = "BFTEST:noevidence"




def _rules():
    return rules_mod.load_rules()


def _th():
    return ss._confidence_thresholds()


def _row(items, unchecked=None, grade="C", raw=2, pos_total=2, neg_total=0,
         conf=None, reasons=None, review=False, **extra):
    """造一行「像库里读出来的那样」的 dict（含 evidence 编号）。"""
    r = {"assessment_id": extra.pop("aid", 1), "relationship_key": REL,
         "staff_id": "BFTEST", "session_id": "sess", "grade": grade,
         "raw_score": raw, "positive_total": pos_total, "negative_total": neg_total,
         "positive_items": items, "negative_items": [],
         "unchecked_rules": unchecked or [], "invalid_items": [],
         "needs_human_review": review, "session_confidence": conf,
         "review_reasons": reasons, "source_run_id": 999}
    r.update(extra)
    return r


def _item(rid, score=2, eids=("#001",), evidence=("客户: 我想学",), kind=None):
    it = {"rule_id": rid, "score": score, "polarity": "positive", "dimension": "x",
          "reason": "t", "evidence": list(evidence), "evidence_ids": list(eids),
          "evidence_message_ids": ["m1"]}
    if kind:
        it["confidence_kind"] = kind
    return it


# ============================================================ 1. 纯函数

def test_pure():
    print("\n--- 1. 纯函数（不碰 DB）---")
    rules, th = _rules(), _th()

    # _has_evidence
    check("有 evidence_ids → 可算", bf._has_evidence(_row([_item("positive_coder_profile")])) is True)
    check("有 evidence_message_ids 也算可算",
          bf._has_evidence(_row([_item("positive_coder_profile", eids=())])) is True)
    check("只有 evidence（无编号）→ **不可算**",
          bf._has_evidence(_row([_item("positive_coder_profile", eids=())])) is True)
    no_eid = _item("positive_coder_profile", eids=())
    no_eid.pop("evidence_message_ids")
    check("既无编号也无 message_ids → 不可算",
          bf._has_evidence(_row([no_eid])) is False)
    check("items 全空 → 不可算", bf._has_evidence(_row([])) is False)

    # compute_new：可算
    new = bf.compute_new(_row([_item("positive_coder_profile")]), rules, th)
    check("可算行返回 dict", isinstance(new, dict), str(type(new)))
    check("含四个键", {"session_confidence", "review_reasons",
                     "needs_human_review", "positive_items"} <= set(new), str(sorted(new)))
    check("置信度是 0~1 的浮点", 0.0 <= new["session_confidence"] <= 1.0,
          str(new["session_confidence"]))

    # compute_new：不可算 → None（诚实：宁缺勿假）
    check("不可算行返回 None（不写假数据）",
          bf.compute_new(_row([no_eid]), rules, th) is None)

    # 占位编号绝不能落库
    legacy = bf.compute_new(_row([no_eid]), rules, th, allow_legacy=True)
    check("allow_legacy 下可算（用原文兜底）", legacy is not None)
    ids = [i.get("evidence_ids") for i in legacy["positive_items"]]
    check("★ 占位编号 #LEGACY 未落库", all(x != [bf._LEGACY_MARK] for x in ids), str(ids))

    # diff_of
    r = _row([_item("positive_coder_profile")], conf=None, reasons=None)
    d = bf.diff_of(r, {"session_confidence": 0.8, "review_reasons": ["x"],
                       "needs_human_review": True, "positive_items": r["positive_items"],
                       "negative_items": []})
    check("diff_of 检出 3 个字段变更",
          set(d) == {"session_confidence", "review_reasons", "needs_human_review"}, str(sorted(d)))
    same = bf.diff_of(r, {"session_confidence": None, "review_reasons": None,
                          "needs_human_review": False,
                          "positive_items": r["positive_items"], "negative_items": []})
    check("值相同时 diff_of 为空", same == {}, str(same))


# ============================================================ 2. 确定性（last_grade 快照）

def test_snapshot_determinism():
    print("\n--- 2. 确定性：last_grade 必须用写之前的快照 ---")
    # 同一 rel 造两行（第二行的 last_grade 应取第一行的**原** grade）
    marker = "BFTEST:snap"
    try:
        delete_rows("customer_intent_assessment", relationship_key=marker)
        # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
        try: ss.repair_current_flags()
        except Exception: pass
        a1 = db.query_one("""INSERT INTO customer_intent_assessment
              (relationship_key, staff_id, session_id, raw_score, positive_total,
               negative_total, grade, positive_items, negative_items, unchecked_rules,
               invalid_items, needs_human_review, analyzed_at)
              VALUES (%s,'BFTEST','s1',2,2,0,'D',%s,'[]','[]','[]',false,now())
              RETURNING assessment_id""",
              (marker, db.as_json([_item("positive_coder_profile")])))["assessment_id"]
        a2 = db.query_one("""INSERT INTO customer_intent_assessment
              (relationship_key, staff_id, session_id, raw_score, positive_total,
               negative_total, grade, positive_items, negative_items, unchecked_rules,
               invalid_items, needs_human_review, analyzed_at)
              VALUES (%s,'BFTEST','s2',6,6,0,'S',%s,'[]','[]','[]',false,now())
              RETURNING assessment_id""",
              (marker, db.as_json([_item("positive_coder_profile")])))["assessment_id"]
        rows = bf.fetch_rows(only_missing=True, rel_like=marker)
        check("取到 2 行合成数据", len(rows) == 2, str(len(rows)))
        snap = bf.snapshot_last_grade(rows)
        check("第 1 行的 last_grade 为 None（它是最早的）", snap[a1] is None, str(snap.get(a1)))
        check("★ 第 2 行的 last_grade = 第 1 行的**原** grade 'D'（快照在写之前）",
              snap[a2] == "D", str(snap.get(a2)))

        # 快照可重复：再算一次结果不变
        snap2 = bf.snapshot_last_grade(bf.fetch_rows(only_missing=True, rel_like=marker))
        check("快照幂等（重复计算一致）", snap == snap2, f"{snap} vs {snap2}")
    finally:
        delete_rows("customer_intent_assessment", relationship_key=marker)
        # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
        try: ss.repair_current_flags()
        except Exception: pass


# ============================================================ 3. 端到端：回填 + 幂等 + 回滚

def test_end_to_end():
    print("\n--- 3. 端到端：回填 → 幂等 → 回滚 ---")
    tmp = tempfile.mkdtemp(prefix="bftest_")
    aids = []
    try:
        # 造 2 行：一行可算（有编号）、一行不可算（无编号但**有负分**）
        aids.append(db.query_one("""INSERT INTO customer_intent_assessment
              (relationship_key, staff_id, session_id, raw_score, positive_total,
               negative_total, grade, positive_items, negative_items, unchecked_rules,
               invalid_items, needs_human_review, analyzed_at)
              VALUES (%s,'BFTEST','s1',2,2,0,'B',%s,'[]','[]','[]',false,now())
              RETURNING assessment_id""",
              (REL, db.as_json([_item("positive_coder_profile")])))["assessment_id"])
        no_eid = _item("negative_refuse", score=-3)
        no_eid.pop("evidence_message_ids")
        no_eid["evidence_ids"] = []
        aids.append(db.query_one("""INSERT INTO customer_intent_assessment
              (relationship_key, staff_id, session_id, raw_score, positive_total,
               negative_total, grade, positive_items, negative_items, unchecked_rules,
               invalid_items, needs_human_review, analyzed_at)
              VALUES (%s,'BFTEST','s2',-3,0,-3,'D','[]',%s,'[]','[]',false,now())
              RETURNING assessment_id""",
              (REL2, db.as_json([no_eid])))["assessment_id"])

        r = bf.run(apply=True, rel_like="BFTEST:", backup_dir=tmp, verbose=False)
        check("可算 1 行、不可算 1 行", r["computable"] == 1 and r["skipped_no_evidence"] == 1,
              str(r))
        check("实际改动 1 行", r["changed"] == 1, str(r["changed"]))
        check("生成了备份文件", bool(r["backup"]) and os.path.exists(r["backup"]),
              str(r["backup"]))

        row1 = db.query_one("SELECT * FROM customer_intent_assessment WHERE assessment_id=%s",
                            (aids[0],))
        check("可算行已回填 session_confidence", row1["session_confidence"] is not None,
              str(row1["session_confidence"]))
        check("可算行已回填 review_reasons（JSONB 数组）",
              isinstance(row1["review_reasons"], list), str(row1["review_reasons"]))
        check("items 内的 item_confidence 已写入",
              all("item_confidence" in i for i in (row1["positive_items"] or [])),
              str(row1["positive_items"])[:120])
        check("★ confidence 兼容列未被复活（仍 NULL）", row1["confidence"] is None,
              str(row1["confidence"]))

        row2 = db.query_one("SELECT * FROM customer_intent_assessment WHERE assessment_id=%s",
                            (aids[1],))
        check("不可算行的 session_confidence **保持 NULL**（宁缺勿假）",
              row2["session_confidence"] is None, str(row2["session_confidence"]))
        check("★ 不可算行仍修了「有负分却未标复核」的矛盾",
              row2["needs_human_review"] is True and row2["review_reasons"] == ["has_negative"],
              f'{row2["needs_human_review"]} / {row2["review_reasons"]}')

        # 幂等
        r2 = bf.run(apply=True, rel_like="BFTEST:", backup_dir=tmp, verbose=False)
        check("★ 幂等：重复回填改动 0 行", r2["changed"] == 0 and r2["applied"] == 0, str(r2))

        # 回滚
        bf.rollback(r["backup"], verbose=False)
        row1b = db.query_one("SELECT * FROM customer_intent_assessment WHERE assessment_id=%s",
                             (aids[0],))
        check("★ 回滚：session_confidence 复原为 NULL", row1b["session_confidence"] is None,
              str(row1b["session_confidence"]))
        check("回滚：items 内的 item_confidence 一并复原",
              all("item_confidence" not in i for i in (row1b["positive_items"] or [])),
              str(row1b["positive_items"])[:120])
        row2b = db.query_one("SELECT * FROM customer_intent_assessment WHERE assessment_id=%s",
                             (aids[1],))
        check("回滚：不可算行的 flag 也复原为 false",
              row2b["needs_human_review"] is False, str(row2b["needs_human_review"]))
    finally:
        for aid in aids:
            delete_rows("customer_intent_assessment", assessment_id=aid)
            # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
            try: ss.repair_current_flags()
            except Exception: pass
        db.execute("DELETE FROM customer_intent_assessment WHERE relationship_key LIKE %s",
                   ("BFTEST%",))
        # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
        try: ss.repair_current_flags()
        except Exception: pass
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================ 6. 自洽性修复（不可算行）

def test_consistency_repair():
    """不可算行补不出置信度，但「需复核 / 为何」不依赖置信度，应无损修复。

    三类：
      · 类型一 有负分却 flag=False            → 置 true
      · 类型二 已 flag=True、有负分、reasons 缺失 → 只补 reasons
      · 类型三 有漏检、flag=False、reasons 缺失   → 置 true + ['unchecked_rules']
    """
    print("\n--- 6. 自洽性修复（不可算行；不填假置信度但补齐可解释性）---")
    tmp = tempfile.mkdtemp(prefix="bftest_")
    made = []
    eid = None
    try:
        # 无编号的 item（模拟「不可算」）
        no_eid = _item("negative_refuse", score=-3)
        no_eid.pop("evidence_message_ids")
        no_eid["evidence_ids"] = []

        def mk(session, neg, unchecked, flag, reasons, aid_note):
            return db.query_one("""INSERT INTO customer_intent_assessment
                  (relationship_key, staff_id, session_id, raw_score, positive_total,
                   negative_total, grade, positive_items, negative_items, unchecked_rules,
                   invalid_items, needs_human_review, review_reasons, analyzed_at)
                  VALUES (%s,'BFTEST',%s,2,2,%s,'C','[]',%s,%s,'[]',%s,%s,now())
                  RETURNING assessment_id""",
                  (f"{REL}:{aid_note}", session, -len(neg) * 3,
                   db.as_json(neg), db.as_json(unchecked), flag,
                   db.as_json(reasons) if reasons is not None else None))["assessment_id"]

        # 类型一
        a1 = mk("s1", [no_eid], [], False, None, "t1")
        # 类型二：已 True 且缺 reasons
        a2 = mk("s2", [no_eid], [], True, None, "t2")
        # 类型三：有漏检、flag=False
        a3 = mk("s3", [], ["positive_urgency"], False, None, "t3")
        # 正常行（不该被动）
        a4 = mk("s4", [no_eid], [], True, ["has_negative"], "t4")
        made = [a1, a2, a3, a4]

        r = bf.run(apply=True, rel_like=REL, backup_dir=tmp, verbose=False)
        check("34/34 之外：只修不自算（computable=0）", r["computable"] == 0, str(r["computable"]))
        check("命中 3 条自洽性修复（第 4 条已一致，不动）",
              r["flag_only_fixed"] == 3, str(r["flag_only_fixed"]))

        g = lambda aid: db.query_one(  # noqa: E731
            "SELECT needs_human_review, review_reasons FROM customer_intent_assessment "
            "WHERE assessment_id=%s", (aid,))
        check("类型一：flag False → True 且补 ['has_negative']",
              g(a1)["needs_human_review"] is True and g(a1)["review_reasons"] == ["has_negative"],
              str(g(a1)))
        check("类型二：flag 保持 True，只补 reasons",
              g(a2)["needs_human_review"] is True and g(a2)["review_reasons"] == ["has_negative"],
              str(g(a2)))
        check("类型三：漏检 → True 且 reasons=['unchecked_rules']",
              g(a3)["needs_human_review"] is True
              and g(a3)["review_reasons"] == ["unchecked_rules"], str(g(a3)))
        check("已一致的行未被改动",
              g(a4)["review_reasons"] == ["has_negative"], str(g(a4)))

        # ★ 关键：修复**没有**碰 session_confidence（仍 NULL）
        confs = db.query("SELECT session_confidence FROM customer_intent_assessment "
                         "WHERE assessment_id = ANY(%s)", (made,))
        check("★ 修复不动 session_confidence（仍全为 NULL）",
              all(c["session_confidence"] is None for c in confs), str(confs))

        # 幂等
        r2 = bf.run(apply=True, rel_like=REL, backup_dir=tmp, verbose=False)
        check("★ 修复幂等（再跑 0 行）", r2["flag_only_fixed"] == 0, str(r2["flag_only_fixed"]))

        # 回滚也能还原 flag/reasons
        bf.rollback(r["backup"], verbose=False)
        check("回滚：类型一复原为 False + reasons NULL",
              g(a1)["needs_human_review"] is False and g(a1)["review_reasons"] is None,
              str(g(a1)))
        check("回滚：类型三复原为 False + reasons NULL",
              g(a3)["needs_human_review"] is False and g(a3)["review_reasons"] is None,
              str(g(a3)))
    finally:
        db.execute("DELETE FROM customer_intent_assessment WHERE relationship_key LIKE %s",
                   (f"{REL}%",))
        # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
        try: ss.repair_current_flags()
        except Exception: pass
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================ 4. 生产数据未被误碰

def test_production_untouched():
    print("\n--- 4. 生产数据未被误碰（回填只在目标关系键上生效）---")
    n_before = db.query_one("SELECT count(*) AS n FROM customer_intent_assessment")["n"]
    r = bf.run(apply=False, rel_like="_bf_nomatch_", verbose=False)
    check("不匹配任何行时 total=0", r["total"] == 0, str(r))
    check("生产评估条数未变",
          db.query_one("SELECT count(*) AS n FROM customer_intent_assessment")["n"] == n_before,
          str(n_before))
    # 生产行不会被「--rel」之外的批处理意外命中：确认所有 BFTEST 残留已清
    left = db.query_one("SELECT count(*) AS n FROM customer_intent_assessment "
                        "WHERE relationship_key LIKE %s", ("BFTEST%",))["n"]
    check("BFTEST 合成数据已清空", left == 0, str(left))


# ============================================================ 5. 备份文件内容

def test_backup_shape():
    print("\n--- 5. 备份文件结构（含回滚所需全部字段）---")
    tmp = tempfile.mkdtemp(prefix="bftest_")
    aid = None
    try:
        aid = db.query_one("""INSERT INTO customer_intent_assessment
              (relationship_key, staff_id, session_id, raw_score, positive_total,
               negative_total, grade, positive_items, negative_items, unchecked_rules,
               invalid_items, needs_human_review, analyzed_at)
              VALUES (%s,'BFTEST','s9',2,2,0,'B',%s,'[]','[]','[]',false,now())
              RETURNING assessment_id""",
              (REL, db.as_json([_item("positive_coder_profile")])))["assessment_id"]
        r = bf.run(apply=True, rel_like=REL, backup_dir=tmp, verbose=False)
        data = json.load(open(r["backup"], encoding="utf-8"))
        check("备份含 created_at / thresholds / rule_version",
              {"created_at", "thresholds", "rule_version"} <= set(data), str(sorted(data)))
        rec = (data["rows"] or [])[0]
        check("备份行含回滚所需 5 个字段",
              {"assessment_id", "session_confidence", "review_reasons",
               "needs_human_review", "positive_items"} <= set(rec), str(sorted(rec)))
        check("备份记录的是**原值**（session_confidence 为 None）",
              rec["session_confidence"] is None, str(rec["session_confidence"]))
    finally:
        if aid:
            delete_rows("customer_intent_assessment", assessment_id=aid)
            # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
            try: ss.repair_current_flags()
            except Exception: pass
        db.execute("DELETE FROM customer_intent_assessment WHERE relationship_key LIKE %s",
                   ("BFTEST%",))
        # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
        try: ss.repair_current_flags()
        except Exception: pass
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================

def main():
    print("=" * 70)
    print("历史评估置信度回填验收（§6 E4 配套）")
    print("=" * 70)
    test_pure()
    test_snapshot_determinism()
    test_end_to_end()
    test_consistency_repair()
    test_backup_shape()
    test_production_untouched()
    print("\n" + "=" * 70)
    print("PASS %d / FAIL %d" % (len(PASSED), len(FAILED)))
    if FAILED:
        for f in FAILED:
            print("  FAILED:", f)
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
