# -*- coding: utf-8 -*-
"""评估集写入门禁（W1–W5 / 修 P13–P15）—— 需要 PostgreSQL 5433。

    .venv\\Scripts\\python.exe tests\\test_eval_write_guard.py

## 守的是什么

2026-10-15 实测的一串事故：30 条人工标注保存后**门禁认为没有标注**（保存路径没清
`prefilled`）；21 行随后被 AI 输出**整体替换**且**审计里查不到**（`prefill` 的审计带
`if total <= 30` 的门槛）；而 `prefill(overwrite=True)` 在设计上**没有任何保护**
已确认标注的机制。这个文件把三件事各钉一条：

1. **写了必留痕，且与写入同事务** —— 审计写不进去 → 整体回滚（"写不进去就不许改"）；
2. **覆盖分三级**：默认只填空白 / `overwrite` 只覆盖草稿 / 覆盖已确认需显式 + admin；
3. **冻结的样本任何情况下不碰**，`set_annotation` 稳定把 `prefilled` 置 false。

## 夹具纪律

**不碰真实评估集的 30 条**：全部用合成的 `eval_case` 行（`kind='__guard_test__'`、
`staff_id='GUARD:...'`），跑完删净（含审计行）。这也是本项目测试的通用要求 ——
借真实数据会让断言随数据形态漂移（`test_cb_delta_and_budget.py` 刚踩过）。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import sys

from _harness import check, summary  # noqa: E402

import audit                # noqa: E402
import db                   # noqa: E402
import eval_case_repo as repo   # noqa: E402
import eval_runner          # noqa: E402

KIND = "__guard_test__"
ITEM = {"rule_id": "positive_cooperative", "score": 1, "polarity": "positive",
        "dimension": "配合度、认可度高"}
_n = [0]


def _mk() -> int:
    """造一条合成样本（带 baseline_items，才能走 prefill 路径）。"""
    _n[0] += 1
    return repo.insert_sample(
        kind=KIND, staff_id="GUARD:staff", session_id=f"GUARD:sess-{_n[0]}",
        target_id="GUARD:target", staff_name="t", target_name="t", session_name="t",
        messages_hash="h", msg_count=5, last_msg_time=1,
        baseline_items=db.as_json([ITEM]), baseline_raw_score=1, baseline_grade="C",
        baseline_rule_version="v-test", baseline_prompt_version="t", baseline_at=None)


def _row(cid):
    return db.query_one("SELECT * FROM eval_case WHERE case_id=%s", (cid,))


def _audit(cid, action):
    return db.query("""SELECT * FROM audit_log
                        WHERE target=%s AND action=%s ORDER BY id""", (str(cid), action))


def main():
    audit_max = db.query_one("SELECT coalesce(max(id),0) AS n FROM audit_log")["n"]
    made = []
    try:
        # ---------------- W5：保存必须清 prefilled（P13 的根因） ----------------
        c = _mk(); made.append(c)
        repo.set_prefill(c, items=[ITEM], raw_score=1, grade="C", annotator="AI",
                         actor="tester")
        check("预标注后 prefilled=true（草稿态）", _row(c)["prefilled"] is True)
        repo.set_annotation(c, items=[ITEM], raw_score=1, grade="C",
                            annotator="tester", note="", disputed=False)
        r = _row(c)
        check("保存后 prefilled=false（否则门禁永远认为没标注）",
              r["prefilled"] is False, f"prefilled={r['prefilled']}")
        check("保存后 annotator = 服务端推导的主体", r["annotator"] == "tester",
              str(r["annotator"]))

        # ---------------- W1：写了必留痕，且 before→after ----------------
        rows = _audit(c, "eval_annotate")
        check("保存写了一条 eval_annotate 审计", len(rows) == 1, f"n={len(rows)}")
        if rows:
            d = rows[0]["detail"] if isinstance(rows[0]["detail"], dict) else {}
            check("审计含 before → after（可追溯「从什么改成什么」）",
                  "before" in d and "after" in d, str(list(d))[:120])
            check("审计 before 记下了原草稿态（prefilled=True）",
                  (d.get("before") or {}).get("prefilled") is True, str(d.get("before"))[:120])
            check("审计 after 记下新态（prefilled=False）",
                  (d.get("after") or {}).get("prefilled") is False,
                  str(d.get("after"))[:120])
        rows = _audit(c, "eval_prefill")
        check("预标注也留痕（改造前只在 ≤30 条时留一条汇总）", len(rows) == 1, f"n={len(rows)}")

        # ---------------- W1：审计写不进去 → 整体回滚（不许静默改） ----------------
        c2 = _mk(); made.append(c2)
        repo.set_prefill(c2, items=[ITEM], raw_score=1, grade="C", annotator="AI", actor="t")
        before = _row(c2)["expected_raw_score"]
        orig = audit.record_action_in
        audit.record_action_in = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        repo_audit_orig = repo.audit.record_action_in
        repo.audit.record_action_in = audit.record_action_in
        raised = False
        try:
            repo.set_annotation(c2, items=[{**ITEM, "score": 2}], raw_score=2,
                               grade="B", annotator="t", note="", disputed=False)
        except Exception:                      # noqa: BLE001
            raised = True
        finally:
            repo.audit.record_action_in = repo_audit_orig
            audit.record_action_in = orig
        check("审计写失败 → 保存抛错", raised)
        check("审计写失败 → 行**没有**被改动（整体回滚）",
              _row(c2)["expected_raw_score"] == before,
              f"{before} → {_row(c2)['expected_raw_score']}")

        # ---------------- W3：覆盖分三级 ----------------
        draft, confirmed, frozen = _mk(), _mk(), _mk()
        made += [draft, confirmed, frozen]
        for cid in (draft, confirmed, frozen):
            repo.set_prefill(cid, items=[ITEM], raw_score=1, grade="C", annotator="AI", actor="t")
        repo.set_annotation(confirmed, items=[ITEM], raw_score=1, grade="C",
                            annotator="human", note="人工确认过", disputed=False)
        repo.set_frozen(frozen, True, actor="t")
        repo.set_prefill(frozen, items=[{**ITEM, "score": 2}], raw_score=2, grade="B",
                         annotator="AI", actor="t")
        check("冻结样本：连 overwrite_annotated 也覆盖不了",
              _row(frozen)["expected_raw_score"] == 1,
              str(_row(frozen)["expected_raw_score"]))

        res = eval_runner.prefill(kind=KIND, overwrite=True, actor="t")
        check("overwrite=True 覆盖了草稿行（score 仍来自 baseline）",
              _row(draft)["prefilled"] is True, str(_row(draft)["prefilled"]))
        check("overwrite=True **不动**已确认行", _row(confirmed)["annotator"] == "human",
              str(_row(confirmed)["annotator"]))
        # ≥1 而不是 ==1：本次夹具里前面还有已确认的行（`c`），也会被如实计入
        check("报告里如实给出被跳过的已确认条数", (res.get("skipped_annotated") or 0) >= 1,
              str(res.get("skipped_annotated")))
        check("报告里如实给出被跳过的冻结条数", res.get("skipped_frozen") == 1,
              str(res.get("skipped_frozen")))

        res2 = eval_runner.prefill(kind=KIND, overwrite_annotated=True, actor="t")
        check("overwrite_annotated=True 才覆盖已确认行（且转回草稿态）",
              _row(confirmed)["prefilled"] is True and
              _row(confirmed)["annotator"] == "AI预标注",
              f"prefilled={_row(confirmed)['prefilled']}")
        check("覆盖人工内容留下 overwrote_annotated 标记（审计可查）",
              any((r["detail"] or {}).get("overwrote_annotated") is True
                  for r in _audit(confirmed, "eval_prefill")
                  if isinstance(r["detail"], dict)),
              str([r["detail"] for r in _audit(confirmed, "eval_prefill")][:2])[:150])

        # ---------------- W6：草稿态可见（门禁的理由里要点出来） ----------------
        st = db.query_one("""SELECT count(*) AS n FROM eval_case
                              WHERE active AND prefilled AND expected_items IS NOT NULL""")
        check("stats 口径：草稿带内容的条数可数", (st or {}).get("n") is not None, str(st))
    finally:
        # 清理必须**按本次造出来的 case_id** 删审计（不能用 actor 白名单：
        # 测试里会用 "human" 之类的名字模拟人工，白名单一定会漏 —— 实测漏了 3 行）。
        if made:
            db.execute("DELETE FROM audit_log WHERE target = ANY(%s)",
                       ([str(c) for c in made],))
        db.execute("DELETE FROM eval_case WHERE kind=%s", (KIND,))
        db.execute("DELETE FROM audit_log WHERE id > %s AND target LIKE %s",
                   (audit_max, "GUARD%"))
    return summary()


if __name__ == "__main__":
    sys.exit(main())
