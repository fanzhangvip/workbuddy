# -*- coding: utf-8 -*-
"""改动 → 该跑哪些测试（`run_all.py --changed` 用）。

## 为什么要它

改一行 `scoring_context.py` 却要跑 36 个测试，是"改一次跑一次"最大的阻力。
这张表把"改哪里 → 跑哪些"固化下来，**改一次只跑相关的几个文件**。

## 两条纪律

1. **宁可多跑，不可漏跑**：文件不在这张表里 → 退回跑全量 t0+t1（见 `run_all.py`）。
2. **表里的每个测试名都必须真实存在**，否则 `run_all.py --changed` 直接报错 ——
   表会随着测试改名而失效，失效必须吵，不能静默少跑。

按**文件名**（basename）索引，所以 `backend/scoring_context.py` 与
`scoring_context.py` 是同一个键。
"""

#: 键 = 被改文件的名字；值 = 需要跑的测试文件（不带 .py）
_BY_NAME = {
    # ---------------- 规则与判定 ----------------
    "rules.py": ["test_rules_validation", "test_contracts", "test_confidence"],
    "rules.json": ["test_rules_validation", "test_contracts", "test_eval_runner",
                   "test_engine_window_and_anchor"],
    "contracts.py": ["test_contracts", "test_stage_a_acceptance"],
    "analyze.py": ["test_contracts", "test_prompt_trust_boundary", "test_llm_client",
                   "test_stage_a_acceptance"],
    "confidence.py": ["test_confidence"],
    "policy.py": ["test_production_hardening", "test_grade_tag"],

    # ---------------- 上下文与评分 ----------------
    "scoring_context.py": ["test_cb_delta_and_budget", "test_ca_key_history",
                           "test_cc_memory_segments", "test_ctx_preprocess",
                           "test_graph_session_parity", "test_ctx_integration",
                           "test_engine_window_and_anchor"],
    "ctx_preprocess.py": ["test_ctx_preprocess", "test_ctx_integration"],
    "scoring_service.py": ["test_loop_guard", "test_run_with_graph", "test_hooks",
                           "test_g0_hardening", "test_graph_session_parity",
                           "test_observability", "test_scoring_start_gate_blocked"],
    "graph_session.py": ["test_graph_session_parity", "test_run_with_graph",
                         "test_graph_skip_and_failure", "test_hooks",
                         "test_engine_window_and_anchor"],
    "hooks.py": ["test_hooks", "test_loop_guard"],

    # ---------------- 同步与外部链路 ----------------
    "sync_service.py": ["test_sync_window", "test_dedup_semantics", "test_g0_hardening",
                        "test_bugfix_p0p1"],
    "weiban.py": ["test_sync_window", "test_dedup_semantics"],
    "ww_chatdata.py": ["test_channel_check", "test_dedup_semantics"],
    "wb_keepalive.py": ["test_channel_check"],
    "wb_bootstrap.py": ["test_channel_check"],

    # ---------------- 评估集与门禁 ----------------
    "eval_runner.py": ["test_eval_runner", "test_cb_delta_and_budget",
                       "test_scoring_start_gate_blocked", "test_engine_window_and_anchor"],
    "eval_case_repo.py": ["test_eval_runner", "test_eval_write_guard"],
    "meta_loop.py": ["test_eval_runner"],
    "review_repo.py": ["test_eval_runner"],
    "assessment_repo.py": ["test_eval_runner", "test_backfill"],
    "backfill.py": ["test_backfill"],

    # ---------------- 回写与标签 ----------------
    "outbox.py": ["test_outbox_drain", "test_g6_outbox_survives_watermark"],
    "writeback_repo.py": ["test_outbox_drain", "test_g6_outbox_survives_watermark"],
    "grade_tag.py": ["test_grade_tag"],
    "failure_worker.py": ["test_batch234"],

    # ---------------- 运行时与调度 ----------------
    "job_service.py": ["test_bugfix_p0p1", "test_production_hardening", "test_loop_guard"],
    "scheduler.py": ["test_bugfix_p0p1", "test_production_hardening", "test_sync_window"],
    "task_runtime.py": ["test_production_hardening"],
    "retention.py": ["test_batch234"],
    "trace.py": ["test_trace_span"],

    # ---------------- 基础设施与接口层 ----------------
    "db.py": ["test_schema_and_defaults", "test_settings_registry", "test_hooks",
              "test_eval_runner"],
    "schema.sql": ["test_schema_and_defaults", "test_hooks", "test_eval_runner"],
    "settings_registry.py": ["test_settings_registry"],
    "security.py": ["test_security", "test_rbac", "test_login_session"],
    "auth_service.py": ["test_login_session", "test_browser_capability"],
    "audit.py": ["test_observability", "test_trace_span", "test_eval_write_guard"],
    "http_client.py": ["test_llm_client", "test_channel_check"],
    "llm.py": ["test_llm_client", "test_stage_a_acceptance"],
    "idempotency.py": ["test_batch234"],
    "api_common.py": ["test_production_hardening"],
    "api_eval.py": ["test_eval_runner"],
    "api_review.py": ["test_eval_runner"],
    "api_scoring.py": ["test_eval_runner", "test_production_hardening"],
    "api_sync.py": ["test_sync_window", "test_bugfix_p0p1"],
}


def tests_for(changed_names) -> list | None:
    """给定一批被改文件名，返回该跑的测试名；**任一文件不在表里 → 返回 None**（退回全量）。"""
    out, unknown = [], []
    for n in changed_names:
        hit = _BY_NAME.get(n)
        if hit is None:
            unknown.append(n)
        else:
            out.extend(hit)
    if unknown:
        return None
    seen, uniq = set(), []
    for n in out:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def unknown_files(changed_names) -> list:
    return [n for n in changed_names if n not in _BY_NAME]
