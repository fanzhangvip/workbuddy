# -*- coding: utf-8 -*-
"""两处「设计了但从来没被写/读」的收尾（2026-10-16）。

    .venv\\Scripts\\python.exe tests\\test_eval_baseline_written.py

## ① `eval_run.baseline_id` —— 从建表起没人写过

它记的是「这一轮跑分是拿哪条基线比出来的」。改造前基线只写进 `detail` 这个 JSON，
于是"这轮和谁比的"只能翻 JSON；而列一直空着，读 schema 的人会以为它被填了。
（《客户意向评分_架构设计说明书.md》第八章 8.6 门禁三结果与两步发布把「这一轮拿哪条基线比」列为可追溯项。）

## ② `scoring_rule.effective_at` —— 从建表起没人读过

需求文档 §9.3 要求这张表带"生效时间"，schema 里也有（`NOT NULL DEFAULT now()`），
但代码从不读它 → "这版规则什么时候开始生效的"在系统里没有答案。现在
`GET /api/scoring/rules` 一并返回。

⚠️ 本轮跑分用 `kind="__none__"`（**0 条样本、不调模型**）—— 只验落库那一行；
基线键与建出来的行在 `finally` 里全部还原/删除。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import sys

from _harness import check, summary  # noqa: E402

import db                     # noqa: E402
import eval_runner as er      # noqa: E402
import api_scoring            # noqa: E402
import audit                  # noqa: E402

KEY = er.BASELINE_RUN_KEY
KIND = "__test_none__"


def main():
    old = db.get_setting(KEY)
    made = []
    try:
        # ---------------- ① baseline_id ----------------
        base_run = db.query_one(
            """INSERT INTO eval_run (kind, rule_version, prompt_version, case_total, case_ok,
                                     metrics, status, elapsed_sec, finished_at)
               VALUES ('__test_base__','test','test',3,3,%s,'success',1,now())
               RETURNING id""", (db.as_json({"rule_f1": 0.5}),))["id"]
        made.append(base_run)
        db.set_setting(KEY, str(base_run))
        check("基线已指向测试用跑分（前置条件）",
              er.load_baseline().get("run_id") == base_run)

        out = er.run_sync(kind=KIND)          # 0 条样本：不调模型
        made.append(out["run_id"])
        check("跑分确实落了一行（0 条样本也落，便于对照）", bool(out["run_id"]))
        check("★ 返回值带 baseline_id", out.get("baseline_id") == base_run,
              str(out.get("baseline_id")))
        row = db.query_one("SELECT baseline_id FROM eval_run WHERE id=%s", (out["run_id"],))
        check("★ `eval_run.baseline_id` 落库（这列此前从未被写过）",
              row["baseline_id"] == base_run, str(row["baseline_id"]))

        # ---------------- ② effective_at ----------------
        rv = db.query_one("SELECT rule_version AS v FROM scoring_rule "
                          "ORDER BY rule_version DESC LIMIT 1")
        r = api_scoring.rules_list()
        check("`/api/scoring/rules` 返回生效时间",
              r.get("effective_at") is not None, str(r.get("effective_at")))
        check("生效时间与库里这一版规则的 max(effective_at) 一致",
              r.get("rule_version") and rv and r["rule_version"] == rv["v"]
              and r.get("rule_count", 0) > 0, f"{r.get('rule_version')} / {r.get('rule_count')}")

        # ---------------- ③ 门禁必须披露"有多少样本没跑成"（2026-10-16） ----------------
        # 场景：一轮跑分里模型/契约异常导致部分样本被排除在指标外（`case_error`）。
        # 此前这个数字在系统里**没有任何出口** —— 有效样本变少会让门禁容差变宽，
        # 保护被悄悄放松却不报警。现在：门禁 reason 里明说，占比 ≥20% 时不予判定。
        import analyze as _an
        import rules as _rules
        _rv = _rules.load_rules().get("rule_version")
        _pv = _an.PROMPT_VERSION
        base_run2 = db.query_one(
            """INSERT INTO eval_run (kind, rule_version, prompt_version, case_total, case_ok,
                                     metrics, status, elapsed_sec, finished_at)
               VALUES ('__test_base__',%s,%s,10,10,%s,'success',1,now())
               RETURNING id""",
            (_rv, _pv, db.as_json({"rule_f1": 0.5}),))["id"]
        made.append(base_run2)
        db.set_setting(KEY, str(base_run2))
        now_run = db.query_one(
            """INSERT INTO eval_run (kind, rule_version, prompt_version, case_total, case_ok,
                                     metrics, status, elapsed_sec, finished_at)
               VALUES ('__test_now__',%s,%s,10,6,%s,'success',1,now())
               RETURNING id""",
            (_rv, _pv, db.as_json({"rule_f1": 0.5, "case_total": 10, "case_valid": 6,
                                   "case_skipped": 1, "case_error": 3}),))["id"]
        made.append(now_run)
        dec = er.gate_decision(eval_run_id=now_run, run_if_possible=False)
        check("★ 门禁结论里说明「N 条样本未跑成、指标不完整」",
              "未跑成" in (dec.get("reason") or "") and dec.get("case_error") == 3,
              str(dec.get("reason"))[:120])
        check("未跑成占比 ≥20% → 不予判定（不拿残缺指标下结论）",
              dec.get("decidable") is False, f"decidable={dec.get('decidable')}")
        # 只有 1 条失败（10%）时：仍然判定，但必须披露
        now_run2 = db.query_one(
            """INSERT INTO eval_run (kind, rule_version, prompt_version, case_total, case_ok,
                                     metrics, status, elapsed_sec, finished_at)
               VALUES ('__test_now__',%s,%s,10,9,%s,'success',1,now())
               RETURNING id""",
            (_rv, _pv, db.as_json({"rule_f1": 0.5, "case_total": 10, "case_valid": 9,
                                   "case_error": 1}),))["id"]
        made.append(now_run2)
        dec2 = er.gate_decision(eval_run_id=now_run2, run_if_possible=False)
        check("★ 少量失败仍判定，但 reason 里照样披露",
              dec2.get("decidable") is True and "未跑成" in (dec2.get("reason") or ""),
              f"decidable={dec2.get('decidable')}｜{str(dec2.get('reason'))[:90]}")

        # ---------------- ④ 整轮评估异常要落库 + 审计（此前只写进程内状态） ----------------
        er._record_failed_run("__test_failed__", "RuntimeError: 模拟整轮异常")
        fr = db.query_one("""SELECT id, status, error FROM eval_run
                              WHERE kind='__test_failed__' ORDER BY id DESC LIMIT 1""")
        if fr:
            made.append(fr["id"])
        check("★ 整轮异常也落一条 eval_run(status='failed')",
              bool(fr) and fr["status"] == "failed" and "模拟整轮异常" in (fr["error"] or ""),
              str(fr))
        au = db.query_one("""SELECT count(*) AS n FROM audit_log
                              WHERE action='eval_run_failed' AND target=%s""",
                          (f"run:{fr['id']}" if fr else "run:0",))
        check("并有审计留痕（谁/何时/为什么）", bool(au) and au["n"] >= 1, str(au))
    finally:
        if old is None:
            db.execute("DELETE FROM app_setting WHERE key=%s", (KEY,))
        else:
            db.set_setting(KEY, old)
        for rid in made:
            db.execute("DELETE FROM eval_run WHERE id=%s", (rid,))
        print(f"（已还原基线键 → {old!r}，删掉 {len(made)} 条测试跑分行）")
    return summary()


if __name__ == "__main__":
    sys.exit(main())
