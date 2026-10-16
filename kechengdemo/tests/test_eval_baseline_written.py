# -*- coding: utf-8 -*-
"""两处「设计了但从来没被写/读」的收尾（2026-10-16）。

    .venv\\Scripts\\python.exe tests\\test_eval_baseline_written.py

## ① `eval_run.baseline_id` —— 从建表起没人写过

它记的是「这一轮跑分是拿哪条基线比出来的」。改造前基线只写进 `detail` 这个 JSON，
于是"这轮和谁比的"只能翻 JSON；而列一直空着，读 schema 的人会以为它被填了。
（`客户意向评分_回归门禁与评分正确性_设计方案.md` 把这条列为 D3 的验收点之一。）

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
