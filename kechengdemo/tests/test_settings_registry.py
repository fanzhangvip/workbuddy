# -*- coding: utf-8 -*-
"""配置注册表验收（代码评审 P2-10）—— 需要 PostgreSQL 5433。

    .venv/Scripts/python.exe tests/test_settings_registry.py

背景
    改造前配置清单**存在两处**：读侧 62 行手工 `db.setting_int(...)`、写侧 46 项白名单元组。
    新增配置只改一侧 → 「改了不生效」或「改完看不见」。

覆盖
  · 注册表自洽（键唯一、env 唯一、类型合法、risk 只用合法值）
  · **读侧等值**：注册表派生值 == 改造前手写的逐键默认值（含 7 个 API 别名）
  · **写侧等价**：`WRITABLE` ⊇ 改造前白名单元组，且 `(api_key, key)` 形态一致
  · **无漂移**：读侧返回的键 ⊇ 写侧可写键（否则配置「能写读不到」）
  · 写入生效 + 审计留痕（含 old→new）
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"


import sys

from _harness import check, summary  # noqa: E402

import db                        # noqa: E402
import settings_registry as reg  # noqa: E402

_AUDIT_MAX = 0
#: 改造前写在 `api_scoring._apply_settings` 里的白名单元组（**原文照抄**）
_OLD_WHITELIST = (
    ("max_sessions", "scoring_max_sessions"),
    ("min_messages", "scoring_min_messages"),
    ("fetch_tags", "scoring_fetch_tags"),
    ("deadline_minutes", "scoring_deadline_minutes"),
    ("max_llm_calls", "scoring_max_llm_calls"),
    ("priority_order", "scoring_priority_order"),
    ("llm_timeout_sec", "llm_timeout_sec"),
    ("data_retention_days", "data_retention_days"),
    ("eval_gate_enabled", "eval_gate_enabled"),
    ("eval_baseline_path", "eval_baseline_path"),
    ("morning_cron", "scoring_morning_cron"),
    ("public_class_grade_scope", "public_class_grade_scope"),
    ("writeback_delta_mode", "writeback_delta_mode"),
    ("grade_tag_mode", "grade_tag_mode"),
    ("grade_tag_group", "grade_tag_group"),
    ("grade_tag_probe_customer", "grade_tag_probe_customer"),
    ("writeback_quota_per_run", "writeback_quota_per_run"),
    ("writeback_quota_per_customer_per_day", "writeback_quota_per_customer_per_day"),
    ("ctx_window_size", "ctx_window_size"),
    ("ctx_drop_msgtypes", "ctx_drop_msgtypes"),
    ("ctx_drop_pure_url", "ctx_drop_pure_url"),
    ("ctx_drop_system", "ctx_drop_system"),
    ("ctx_display_relative", "ctx_display_relative"),
    ("ctx_min_kept", "ctx_min_kept"),
    ("ctx_min_customer_msgs", "ctx_min_customer_msgs"),
    ("ctx_min_effective_chars", "ctx_min_effective_chars"),
    ("ctx_key_history", "ctx_key_history"),
    ("ctx_key_history_max", "ctx_key_history_max"),
    ("ctx_cold_start_early", "ctx_cold_start_early"),
    ("ctx_delta_max", "ctx_delta_max"),
    ("ctx_tokens_max", "ctx_tokens_max"),
    ("ctx_memory", "ctx_memory"),
    ("ctx_fallback_min", "ctx_fallback_min"),
    ("llm_temperature", "llm_temperature"),
    ("llm_max_tokens", "llm_max_tokens"),
    ("llm_retry_attempts", "llm_retry_attempts"),
    ("rejudge_retries", "rejudge_retries"),
    ("session_max_failures", "session_max_failures"),
    ("use_graph_session", "use_graph_session"),
    ("use_outbox", "use_outbox"),
    ("trace_enabled", "trace_enabled"),
    ("confidence_review_min", "confidence_review_min"),
    ("confidence_boundary_delta", "confidence_boundary_delta"),
    ("confidence_flip_steps", "confidence_flip_steps"),
    ("confidence_weak_weight", "confidence_weak_weight"),
    ("review_engine_negative", "review_engine_negative"),
)
#: 改造前 `get_settings()` 里逐键写死的**默认值**（键 = API 名）
_OLD_DEFAULTS = {
    "max_sessions": 200, "min_messages": 4, "fetch_tags": False,
    "deadline_minutes": 120, "max_llm_calls": 500, "priority_order": True,
    "llm_timeout_sec": 0, "data_retention_days": 0, "eval_gate_enabled": True,
    "eval_baseline_path": "", "morning_cron": "", "public_class_grade_scope": "S,A",
    "writeback_delta_mode": True, "grade_tag_mode": "off",
    "writeback_quota_per_run": 0, "writeback_quota_per_customer_per_day": 0,
    "ctx_window_size": 60, "ctx_drop_msgtypes": "31,9", "ctx_drop_pure_url": True,
    "ctx_drop_system": True, "ctx_display_relative": True, "ctx_min_kept": 4,
    "ctx_min_customer_msgs": 1, "ctx_min_effective_chars": 0, "ctx_key_history": True,
    "ctx_key_history_max": 30, "ctx_cold_start_early": 10, "ctx_delta_max": 200,
    "ctx_tokens_max": 12000, "ctx_memory": True, "ctx_fallback_min": 4,
    "llm_temperature": 0.0, "llm_max_tokens": 0, "llm_retry_attempts": 3,
    "session_max_failures": 3, "use_graph_session": False, "use_outbox": True,
    "trace_enabled": False,
}




def _cleanup():
    db.execute("DELETE FROM app_setting WHERE key IN "
               "('ctx_min_kept','trace_enabled','public_class_grade_scope')")
    if _AUDIT_MAX:
        db.execute("DELETE FROM audit_log WHERE id > %s", (_AUDIT_MAX,))


# ============================================================ 自洽性

def test_registry_shape():
    print("\n--- 1. 注册表自洽 ---")
    keys = [s.key for s in reg.SETTINGS]
    check("app_setting 键无重复", len(keys) == len(set(keys)),
          str([k for k in keys if keys.count(k) > 1]))
    envs = [s.env for s in reg.SETTINGS if s.env]
    check(".env 键无重复", len(envs) == len(set(envs)),
          str([e for e in envs if envs.count(e) > 1]))
    bad_type = [s.key for s in reg.SETTINGS if s.type not in (reg.INT, reg.BOOL, reg.FLOAT, reg.STR)]
    check("类型只取四种", not bad_type, str(bad_type))
    bad_risk = [s.key for s in reg.SETTINGS if s.risk not in ("", "high")]
    check("risk 只用合法值", not bad_risk, str(bad_risk))
    check("每项都有中文说明", all(s.desc for s in reg.SETTINGS),
          str([s.key for s in reg.SETTINGS if not s.desc]))
    risky = [s.key for s in reg.SETTINGS if s.risk == "high"]
    check("写外部系统/高风险项已标记", len(risky) >= 4, str(risky))
    check("分组可用", set(reg.by_group()) >= {"scoring", "ctx", "llm", "grade_tag"},
          str(sorted(reg.by_group())))


# ============================================================ 写侧等价

def test_writable_equivalence():
    print("\n--- 2. 写侧与改造前白名单等价 ---")
    old = set(_OLD_WHITELIST)
    new = set(reg.WRITABLE)
    check("★ 改造前的每一项都仍在白名单里（无功能回退）", old <= new,
          "缺失：" + str(sorted(old - new)))
    # ⚠️ 不能断言「与改造前**完全**相等」—— 后续会有**有意新增**的配置项。
    # 正确语义是：新增项必须来自**显式清单**（防止「无意多出」的静默漂移）。
    # 新增配置时把键加到这里 + 写清理由，是一次有意识的动作。
    _INTENTIONAL_ADDITIONS = {
        # 长任务心跳（修「进程被杀 → 卡死的锁把系统阻塞 4 小时」）
        ("job_heartbeat_stale_minutes", "job_heartbeat_stale_minutes"),
        ("scoring_stale_minutes", "scoring_stale_minutes"),
        # 日常任务（需求 4.2）：什么时间同步、什么时候接着分析 —— 必须能在后台配，
        # 否则「改成 20:00 / 03:30」就得改代码重启（见 ARCHITECTURE ADR-014）。
        ("sync_main_cron", "sync_main_cron"),
        ("sync_main_window_minutes", "sync_main_window_minutes"),
        ("sync_compensate_cron", "sync_compensate_cron"),
        ("sync_compensate_lookback_hours", "sync_compensate_lookback_hours"),
        ("daily_run_analysis", "daily_run_analysis"),
        # 增量同步的重叠回看窗口（需求 4.3：start_time = 水位 − 15 分钟）
        ("sync_overlap_minutes", "sync_overlap_minutes"),
        # 评估侧告警阈值（修 P8）：复核积压与凭据新鲜度 —— 这两个信号以前
        # 只写日志/审计、没有出口，2026-10-13 正文通道断了 3 天没人发现。
        ("eval_alert_review_backlog", "eval_alert_review_backlog"),
        ("eval_alert_credential_hours", "eval_alert_credential_hours"),
        # 「48 小时未回复」的上窗（P2/D2）：业务口径，必须能在设置页调，
        # 否则「14 天改成 7 天」要改代码重启。
        ("no_reply_window_days", "no_reply_window_days"),
        # 门禁可用化（P4）与定期回归（P5）：发布两步化开关、跑分新鲜度、
        # 覆盖率提示阈值、回归时刻与条数 —— 都是运维需要现场调的。
        ("eval_publish_requires_run", "eval_publish_requires_run"),
        ("eval_gate_fresh_minutes", "eval_gate_fresh_minutes"),
        ("eval_min_cases_per_rule", "eval_min_cases_per_rule"),
        ("eval_regression_cron", "eval_regression_cron"),
        ("eval_regression_limit", "eval_regression_limit"),
        # 会话列表翻页上限（2026-10-16）：以前是写死的 20000 + 静默截断，
        # 现在可配且会被记进任务详情 —— 大员工（实测 2 万会话）需要现场调。
        ("sync_session_page_cap", "sync_session_page_cap"),
        # 回写审批有效期（2026-10-16 接线）：策略判定 confirm/human 的客户回写会进
        # 审批队列等人工签字；有效期过后拒绝再签（分数/会话/阈值都可能已变）。
        # 0 = 不过期（默认），需要"隔夜就作废"的口径时在这里调。
        ("writeback_approval_expires_minutes", "writeback_approval_expires_minutes"),
    }
    added = new - old
    check("白名单只比改造前多出**显式声明**的新增项",
          added <= _INTENTIONAL_ADDITIONS, f"未声明的新增：{sorted(added - _INTENTIONAL_ADDITIONS)}")
    check("声明的拟新增项确实已在白名单里（清单不能过期）",
          _INTENTIONAL_ADDITIONS <= new, str(sorted(_INTENTIONAL_ADDITIONS - new)))
    check("形态是 (API 键名, app_setting 键名)",
          all(isinstance(a, str) and isinstance(b, str) for a, b in reg.WRITABLE))
    check("API 名无重复（否则同一 payload 键会写两个设置）",
          len({a for a, _ in reg.WRITABLE}) == len(reg.WRITABLE))
    check("app_setting 键无重复", len({b for _, b in reg.WRITABLE}) == len(reg.WRITABLE))


# ============================================================ 读侧等值

def test_read_equivalence():
    print("\n--- 3. 读侧与改造前默认值等价 ---")
    fn = {reg.INT: db.setting_int, reg.BOOL: db.setting_bool,
          reg.FLOAT: db.setting_float, reg.STR: db.setting}
    got = {s.api_key: reg.typed_get(s, fn[s.type]) for s in reg.SETTINGS}

    # 只对比「默认值」本身：本用例不写 app_setting，故 setting_* 会自然回落到默认
    mism = []
    for k, want in _OLD_DEFAULTS.items():
        if k not in got:
            mism.append(f"{k} 缺失"); continue
        g = got[k]
        if type(g) is not type(want) or g != want:
            mism.append(f"{k}: got={g!r}({type(g).__name__}) want={want!r}")
    check("★ 逐键默认值与改造前**完全一致**（含类型）", not mism, "; ".join(mism))

    print("    核对键数 =", len(_OLD_DEFAULTS), "｜注册表键数 =", len(reg.SETTINGS))


def test_api_alias_preserved():
    print("\n--- 4. 对外键名保持历史形态（前端零改动）---")
    aliases = {s.key: s.api_key for s in reg.SETTINGS if s.api != ""}
    expect = {"scoring_max_sessions": "max_sessions", "scoring_min_messages": "min_messages",
              "scoring_fetch_tags": "fetch_tags", "scoring_deadline_minutes": "deadline_minutes",
              "scoring_max_llm_calls": "max_llm_calls",
              "scoring_priority_order": "priority_order",
              "scoring_morning_cron": "morning_cron"}
    check("★ 7 个历史短键名全部保留", aliases == expect,
          f"差异={ {k: (aliases.get(k), v) for k, v in expect.items() if aliases.get(k) != v} }")
    check("其余键 API 名 == app_setting 键名",
          all(s.api_key == s.key for s in reg.SETTINGS if not s.api))


# ============================================================ 无漂移

def test_no_drift():
    print("\n--- 5. 读侧 ⊆ 写侧无漂移 ---")
    import api_scoring
    s = api_scoring.get_settings()
    read_keys = set(s.keys())
    write_keys = {a for a, _ in reg.WRITABLE}
    missing = write_keys - read_keys          # 能写但读不到 → 「改完看不见」
    check("★ 每个可写键都能被读到（无「能写读不到」）", not missing, str(sorted(missing)))
    # 反向不算错：读侧还有计算值（writeback_mode / runtime / prompt_version …）
    extra = read_keys - write_keys
    check("读侧额外的键都是「计算值/只读常量」",
          extra <= {"writeback_mode", "env_writeback_mode", "runtime", "grade_tag_ids",
                    "prompt_version", "schema_version"},
          str(sorted(extra)))
    check("get_settings 仍返回原有短键名（未破坏前端）",
          "max_sessions" in s and "min_messages" in s and "ctx_min_kept" in s,
          str(sorted(k for k in s if k.startswith("scoring_"))[:6]))


# ============================================================ 写入生效

def test_write_path():
    print("\n--- 6. 写入生效 + 审计留痕（走真实 API 函数）---")
    import api_scoring
    orig = db.get_setting("ctx_min_kept")
    try:
        r = api_scoring.set_settings({"ctx_min_kept": 7})
        check("写入返回 ok", r.get("ok") is True, str(r)[:150])
        check("值已落库", str(db.get_setting("ctx_min_kept")) == "7",
              str(db.get_setting("ctx_min_kept")))
        check("读侧能立即看到新值", api_scoring.get_settings().get("ctx_min_kept") == 7,
              str(api_scoring.get_settings().get("ctx_min_kept")))
        row = db.query_one("SELECT actor, action, target, detail FROM audit_log "
                           "WHERE target='ctx_min_kept' ORDER BY id DESC LIMIT 1")
        check("审计已留痕", bool(row), str(row))
        check("审计记录了 old→new（否则答不出「原本是多少」）",
              row and row["detail"].get("old") == orig
              and row["detail"].get("new") == "7", str(row and row["detail"]))
        check("审计主体来自服务端（未开鉴权时为 local）",
              row and row["actor"] in ("local", "admin"), str(row and row["actor"]))
        # 值未变不重复留痕
        n_before = db.query_one("SELECT count(*) n FROM audit_log WHERE target='ctx_min_kept'")["n"]
        api_scoring.set_settings({"ctx_min_kept": 7})
        n_after = db.query_one("SELECT count(*) n FROM audit_log WHERE target='ctx_min_kept'")["n"]
        check("同值重复保存不留痕（避免刷记录）", n_after == n_before, f"{n_before}->{n_after}")
    finally:
        if orig is None:
            db.execute("DELETE FROM app_setting WHERE key='ctx_min_kept'")
        else:
            db.set_setting("ctx_min_kept", orig)


def main():
    global _AUDIT_MAX
    _AUDIT_MAX = (db.query_one("SELECT coalesce(max(id),0) AS n FROM audit_log") or {}).get("n", 0)
    print("=== 配置注册表验收（评审 P2-10）===")
    try:
        test_registry_shape()
        test_writable_equivalence()
        test_read_equivalence()
        test_api_alias_preserved()
        test_no_drift()
        test_write_path()
    finally:
        _cleanup()
    return summary()

if __name__ == "__main__":
    sys.exit(main())
