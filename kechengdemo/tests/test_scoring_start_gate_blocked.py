# -*- coding: utf-8 -*-
"""闸6 在「评分启动」链上的行为 —— 需要 PostgreSQL 5433。

    .venv\\Scripts\\python.exe tests\\test_scoring_start_gate_blocked.py

## 这条链为什么必须单独钉住

```
POST /api/scoring/start ─┐
scheduler._scoring_job   ├─► scoring_service.start() ──(规则版本变了)──► gate_decision(run_if_possible=False)
scheduler.daily_watch    ┘        scoring_service.py:328-352              └─ blocked → 直接拒绝启动评分
```

它是**闸6 目前唯一真会拦人的地方**（`/rules/publish` 走的是同一条判定，但主线上
每天真正会被挡的是这里）。而截至 2026-10-16，**36 个测试里没有一个覆盖它** ——
也就是说：把这段代码改坏（或把它的判定改松），回归全绿也发现不了。

顺带钉住两件事：

1. **只在规则版本真的变了时才查门禁**（否则每次启动都查纯属浪费）；
2. **被拦住时不得留下任何 scoring_run 行** —— "拒绝启动"必须是拒绝，不是"启动到一半"。

## 为什么用打桩而不是真造一次指标回退

真造回退要冻结基线 + 跑一轮真模型评估（31~48 分钟、还花钱）。这里要验的是**这条链的
接线**（谁调谁、拦不拦、留没留痕），不是指标算法本身 —— 算法已由 `test_eval_runner.py`
的 `test_gate()` 覆盖。所以把 `gate_decision` 打桩成"返回 blocked"，只验接线。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import sys

from _harness import check, summary  # noqa: E402

import config           # noqa: E402
import db               # noqa: E402
import eval_runner      # noqa: E402
import job_service      # noqa: E402
import scoring_service as ss   # noqa: E402


class _Sentinel(RuntimeError):
    """打桩 `sync_rules` 用的哨兵：证明"代码走过了门禁检查这一段"。"""


def _gate(blocked, diffs=None):
    """伪造一次门禁判定。"""
    return {"enabled": True, "has_baseline": True, "decidable": True,
            "gate_passed": not blocked, "blocked": blocked,
            "diffs": diffs or [], "metrics": {}, "baseline_path": "(stub)",
            "reason": "stub"}


def main():
    if not (config.LLM_API_KEY and config.LLM_BASE_URL):
        print("!! 缺 LLM 配置 —— `start()` 会在闸6 **之前**就返回，本测试无意义")
        return 1
    busy = job_service.running_kinds()
    if busy:
        print(f"!! 有批量在跑（互斥闸）：{busy} —— 请等它结束再跑本测试")
        return 1

    rv = ss.current_rule_version()
    # 造「上一轮评分的规则版本 ≠ 当前版本」这个前提（闸6 的触发条件）
    stale = db.query_one(
        """INSERT INTO scoring_run (kind, status, triggered_by, rule_version)
           VALUES ('incremental','success','test-gate','__stale__') RETURNING id""")
    stale_id = stale["id"]
    audit_max = db.query_one("SELECT coalesce(max(id),0) AS n FROM audit_log")["n"]
    orig_gate = eval_runner.gate_decision
    orig_sync = ss.sync_rules
    try:
        # ---------- ① 规则版本变了 + 门禁说 blocked → 必须拒绝启动 ----------
        eval_runner.gate_decision = lambda **kw: _gate(True, [
            {"metric": "rule_f1_llm", "base": 0.90, "now": 0.50, "ok": False}])
        ss.sync_rules = lambda *a, **kw: (_ for _ in ()).throw(_Sentinel("不该走到这里"))
        before = db.query_one("SELECT count(*) AS n FROM scoring_run")["n"]
        r = ss.service.start(triggered_by="test-gate")
        after = db.query_one("SELECT count(*) AS n FROM scoring_run")["n"]
        check("门禁 blocked → start() 返回 ok=False", r.get("ok") is False, str(r)[:200])
        check("拒绝原因指向闸6/回归门禁",
              "回归门禁" in (r.get("reason") or ""), str(r.get("reason"))[:200])
        check("判定结果随返回值透出（便于排障）", isinstance(r.get("gate"), dict),
              str(r.get("gate"))[:120])
        check("被拦住时**没有**新建 scoring_run 行（拒绝就是拒绝）",
              after == before, f"{before} → {after}")
        n_audit = db.query_one(
            "SELECT count(*) AS n FROM audit_log WHERE id > %s AND action='rule_gate_blocked'",
            (audit_max,))["n"]
        check("拦下时写了 rule_gate_blocked 审计", n_audit == 1, f"n={n_audit}")

        # ---------- ② 规则版本变了 + 门禁放行 → 必须真的往下走 ----------
        eval_runner.gate_decision = lambda **kw: _gate(False)
        try:
            ss.service.start(triggered_by="test-gate")
            reached = False
        except _Sentinel:
            reached = True
        check("放行时**确实走过了门禁**（哨兵被触发）", reached,
              "没走到 sync_rules —— 说明门禁之后的流程没被执行")

        # ---------- ③ 规则版本没变 → 不该查门禁（省一次判定） ----------
        db.execute("UPDATE scoring_run SET rule_version=%s WHERE id=%s", (rv, stale_id))
        called = []
        eval_runner.gate_decision = lambda **kw: (called.append(1), _gate(True))[1]
        try:
            ss.service.start(triggered_by="test-gate")
        except _Sentinel:
            pass
        check("规则版本未变时**不查**门禁（打桩未被调用）", not called, f"调用 {len(called)} 次")
    finally:
        eval_runner.gate_decision = orig_gate
        ss.sync_rules = orig_sync
        db.execute("DELETE FROM scoring_run WHERE id=%s", (stale_id,))
        db.execute("DELETE FROM audit_log WHERE id > %s AND action='rule_gate_blocked'",
                   (audit_max,))

    return summary()


if __name__ == "__main__":
    sys.exit(main())
