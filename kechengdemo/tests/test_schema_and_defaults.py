# -*- coding: utf-8 -*-
"""Schema 漂移检测 + 默认值清单验收（代码评审 P1-3 / §5.5）—— 需要 PostgreSQL 5433。

    .venv/Scripts/python.exe tests/test_schema_and_defaults.py

覆盖
  · `schema.sql` 是权威且**完整**：解析出的表/列与实际库一致（零漂移）
  · 解析器同时吃 `CREATE TABLE` 与 `ALTER TABLE ... ADD COLUMN`
    （本项目后续新增列大量走 ALTER；只解析建表会**误报漂移**，实测踩到）
  · 漂移能被检出：模拟「库里少一列」→ 报 missing_columns
  · 默认值清单：保护类默认必开；观测/优化类默认关是**设计意图**不算不符
  · 「已接线」的定时任务清单里含 `idempotency.reap`（评审 P2-1 的原话形态：
    「函数写好了但全仓库没有调用方」）
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"


import os
import sys

from _harness import ROOT, check, delete_rows, summary  # noqa: E402

import defaults_manifest as dm   # noqa: E402
import schema_check as sc        # noqa: E402





# ============================================================ 解析器

def test_parser():
    print("\n--- 1. schema.sql 解析器 ---")
    want, unparsed = sc.expected_columns()
    check("解析出表（>15 张）", len(want) > 15, str(len(want)))
    check("所有 CREATE TABLE 都解析成功（无「拿不准」）", unparsed == 0, str(unparsed))
    # 关键回归：ALTER 补的列必须被算进去，否则「库里有、权威没有」的误报会一片
    check("★ ALTER TABLE ADD COLUMN 的列已计入（chat_message.rule_tags）",
          "rule_tags" in want.get("chat_message", set()),
          str(sorted(want.get("chat_message", set()))[:8]))
    check("★ 多列 ALTER 已计入（customer_intent_assessment.session_confidence）",
          "session_confidence" in want.get("customer_intent_assessment", set()))
    check("ALTER 形式新增的列也计入（scoring_run.deadline）",
          "deadline" in want.get("scoring_run", set()),
          str(sorted(want.get("scoring_run", set()))[:10]))
    check("表级约束没被当成列（无 primary/unique/foreign）",
          not any(k in {"primary", "unique", "foreign", "check", "constraint"}
                  for cols in want.values() for k in cols))
    # 已收敛回 schema.sql 的两处内联 DDL
    check("failure_worker 的 4 个补列已在 schema.sql（P1-3 收敛）",
          {"manual_required", "resolved_at", "last_attempt_at", "resolution"}
          <= want.get("session_failure", set()),
          str(sorted(want.get("session_failure", set()))))
    check("idempotency 的 request_guard 表已在 schema.sql",
          "request_guard" in want)


# ============================================================ 漂移

def test_no_drift():
    print("\n--- 2. 当前库无漂移 ---")
    r = sc.check()
    check("能连上库并给出报告", r.get("ok") is not None, str(r)[:150])
    check("★ 缺表为空", not r.get("missing_tables"), str(r.get("missing_tables")))
    check("★ 缺列为空（权威有、库里没有 = 会真报错）",
          not r.get("missing_columns"), str(r.get("missing_columns")))
    check("库中无「schema.sql 未声明」的多余列",
          not r.get("extra_columns"), str(r.get("extra_columns")))
    check("整体 drift=False", r.get("drift") is False, str(r.get("drift")))
    check("报了表数（可核对）", r.get("tables_in_schema") == r.get("tables_in_db"),
          f"{r.get('tables_in_schema')} vs {r.get('tables_in_db')}")


def test_drift_detected():
    print("\n--- 3. 漂移可被检出（模拟权威多一列）---")
    orig = sc.expected_columns

    def fake():
        want, unp = orig()
        want = {k: set(v) for k, v in want.items()}
        want.setdefault("chat_session", set()).add("__ghost_column__")
        return want, unp

    sc.expected_columns = fake
    try:
        r = sc.check()
        check("模拟后判定为漂移", r.get("drift") is True, str(r.get("drift")))
        hit = any(e["table"] == "chat_session" and "__ghost_column__" in e["columns"]
                  for e in r.get("missing_columns") or [])
        check("★ 精确定位到缺失的列（报告可直接用于补 DDL）", hit,
              str(r.get("missing_columns")))
        check("hint 给出可执行建议", "schema" in str(r.get("hint", "")), str(r.get("hint")))
    finally:
        sc.expected_columns = orig


# ============================================================ 默认值清单

def test_manifest_shape():
    print("\n--- 4. 默认值清单结构 ---")
    kinds = {k for _, _, k, _, _ in dm.MANIFEST}
    check("类别只用四种", kinds <= {"guard", "observe", "optimize", "policy"}, str(kinds))
    check("四类都有代表项",
          {"guard", "observe", "optimize", "policy"} <= kinds, str(kinds))
    check("每项都有中文说明", all(d for *_, d in dm.MANIFEST))
    check("键无重复", len({k for k, *_ in dm.MANIFEST}) == len(dm.MANIFEST))
    # 保护类必须默认开（这是清单的核心不变量）
    guards_off = [k for k, _, kind, d, _ in dm.MANIFEST
                  if kind == "guard" and isinstance(d, bool) and d is False]
    check("★ 保护类里没有「默认关」的布尔项（否则就是「以为防住了」）",
          not guards_off, str(guards_off))


def test_manifest_verify():
    print("\n--- 5. 清单与实际配置一致 ---")
    r = dm.verify()
    check("自检通过（无误报）", r.get("ok") is True, str(r.get("guard_mismatches")))
    check("每项都读到了实际值", all("actual" in i for i in r["items"]))
    check("legend 说明了四类含义", len(r.get("legend") or {}) == 4, str(r.get("legend")))
    # 「已接线」清单必须包含评审点名的那个函数
    callables = {j["callable"] for j in r["wired_jobs"]}
    check("★ idempotency.reap() 已接线（评审 P2-1 的原话形态）",
          any("reap" in c for c in callables), str(sorted(callables)))
    check("★ outbox drain 已接线", any("outbox.drain" in c for c in callables))
    check("★ 失败重投已接线", any("failure_worker" in c for c in callables))
    check("定时任务都标了间隔", all(j["interval_minutes"] > 0 for j in r["wired_jobs"]))


def test_guard_mismatch_detected():
    print("\n--- 6. 「保护被关掉」能被检出 ---")
    import db
    key = next(k for k, _, kind, d, _ in dm.MANIFEST
               if kind == "guard" and isinstance(d, bool) and d is True)
    orig = db.get_setting(key)
    try:
        db.set_setting(key, "false")
        r = dm.verify()
        hit = [m for m in r.get("guard_mismatches") or [] if m["key"] == key]
        check("★ 关掉保护类配置后，自检报出「保护措施被关闭」", bool(hit), str(hit))
        check("整体 ok 转为 False", r.get("ok") is False, str(r.get("ok")))
    finally:
        if orig is None:
            delete_rows("app_setting", key=key)
        else:
            db.set_setting(key, orig)


def main():
    print("=== Schema 漂移 + 默认值清单验收（评审 P1-3 / §5.5）===")
    test_parser()
    test_no_drift()
    test_drift_detected()
    test_manifest_shape()
    test_manifest_verify()
    test_guard_mismatch_detected()
    return summary()

if __name__ == "__main__":
    sys.exit(main())
