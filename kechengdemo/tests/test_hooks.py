# -*- coding: utf-8 -*-
"""显式钩子验收（架构 §2.4 H3）—— 需要 PostgreSQL 5433。

    .venv/Scripts/python.exe tests/test_hooks.py

覆盖四条钩子与其**接入点**：
  · ① `before_llm_call`   G3 超时 / G4 预算 / B4 有效信息量
  · ② `after_llm_output`  闸4 结构 / B2 极性 / A1 证据 / 闸2 覆盖率（体检 + 极性终检）
  · ③ `before_writeback`  D2 开关 / 幂等 / 配额（边界3）
  · ④ `before_persist`    H3 落库前完整性

两条最关键的设计断言：
  · **纯函数**：四条 hook 都不碰 DB（把 db 打桩成会抛异常，hook 仍须正常返回）
  · **接入后行为逐字不变**：把改造前 `_writeback` 的 if-else 复刻成参考实现，
    与 hook 的 reason→status 映射**逐组合**对比（9 组全等）
"""
# 统一输出编码（修 P2-2）：默认 GBK 控制台下，断言名里的 ↔ → 等字符会触发
# UnicodeEncodeError —— 看起来像测试失败，其实是环境问题。
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.stderr.reconfigure(encoding="utf-8")

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "backend"))

import db                        # noqa: E402
import hooks                     # noqa: E402
import rules as rules_mod        # noqa: E402
import scoring_service as ss     # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {detail}" if detail else ""))


# ============================================================ 0. 纯函数（不碰 DB）

class _Boom:
    """任何属性访问都抛异常的替身：用来证明 hook 不读 DB / 不读配置。"""

    def __getattr__(self, name):
        raise AssertionError(f"hook 不应访问 db.{name}（必须是纯函数）")


def test_pure_functions():
    print("\n--- 0. 四条 hook 都是纯函数（不碰 DB / 不读配置）---")
    real_db = hooks.__dict__.get("db", None)
    injected = False
    try:
        hooks.db = _Boom()                       # 若 hook 内部用到 db，会立刻炸
        injected = True
    except Exception:
        pass
    try:
        r1 = hooks.before_llm_call(kept=1, min_kept=4)
        r2 = hooks.after_llm_output(pos_items=[{"rule_id": "x"}], rules={"positive_rules": [{"rule_id": "x"}]}, total_llm=14)
        r3 = hooks.before_writeback(positive_total=3, mode="dry_run")
        r4 = hooks.before_persist(rel_key="a:b", staff_id="a", target_id="b", session_id="s",
                                  score={"raw_score": 1, "positive_total": 1, "negative_total": 0},
                                  grade={"grade": "C"}, rule_version="v1")
        check("① before_llm_call 不碰 DB", r1.reason == "insufficient_content", str(r1))
        check("② after_llm_output 不碰 DB", r2.allow is True, str(r2))
        check("③ before_writeback 不碰 DB", r3.reason == "mode_dry_run", str(r3))
        check("④ before_persist 不碰 DB", r4.allow is True, str(r4))
    finally:
        if injected:
            if real_db is None:
                del hooks.db
            else:
                hooks.db = real_db

    # 静态检查（防将来有人不小心引入依赖）：hooks 只应 import 标准库
    src = open(os.path.join(_ROOT, "backend", "hooks.py"), encoding="utf-8").read()
    imports = [ln.strip() for ln in src.split("\n")
               if ln.lstrip().startswith(("import ", "from "))]
    forbidden = [ln for ln in imports
                 if not ln.startswith(("import time", "from typing"))]
    check("hooks.py 的 import 只含标准库", forbidden == [], str(forbidden))
    # 注意：只扫 import 行 —— 文档字符串里会**提到** scoring_service（说明为何不 import 它），
    # 用整文件字符串匹配会误报（踩到过）。
    check("hooks.py 未 import scoring_service（无循环导入风险）",
          not any("scoring_service" in ln for ln in imports), str(imports))


# ============================================================ 1. before_llm_call

def test_before_llm_call():
    print("\n--- 1. ① before_llm_call（G3 / G4 / B4）---")
    NOW = 1_000_000.0

    check("无任何约束 → 放行",
          hooks.before_llm_call(kept=10, min_kept=4).allow is True)
    check("kept < min_kept → insufficient_content（B4）",
          hooks.before_llm_call(kept=3, min_kept=4).reason == "insufficient_content")
    check("kept == min_kept → 放行（< 而非 <=）",
          hooks.before_llm_call(kept=4, min_kept=4).allow is True)
    check("不传 kept → 跳过内容量检查（轮次级用法）",
          hooks.before_llm_call(deadline=None).allow is True)
    check("deadline 已到 → deadline（G3）",
          hooks.before_llm_call(kept=10, min_kept=4, deadline=NOW - 1, now=NOW).reason == "deadline")
    check("预算用满 → max_llm_calls（G4）",
          hooks.before_llm_call(kept=10, min_kept=4, max_llm_calls=5,
                                llm_calls=5).reason == "max_llm_calls")
    check("预算 0 = 不限 → 放行",
          hooks.before_llm_call(kept=10, min_kept=4, max_llm_calls=0, llm_calls=999).allow is True)
    check("整轮闸优先于会话闸（同时越界时返回 deadline）",
          hooks.before_llm_call(kept=1, min_kept=4, deadline=NOW - 1, now=NOW).reason == "deadline")
    check("与 ss.guard_reason 是同一实现（re-export 未复制）",
          ss.guard_reason is hooks.guard_reason)
    check("detail 携带观测值（kept/min_kept/failed_on）",
          {"kept", "min_kept", "failed_on"} <= set(hooks.before_llm_call(
              kept=2, min_kept=4).detail))
    # B4 三口径（架构 §2.3）
    check("B4：客户一句没说 → 拒绝（即使条数够）",
          hooks.before_llm_call(kept=10, min_kept=4, customer_msgs=0,
                                min_customer_msgs=1).reason == "insufficient_content"
          and hooks.before_llm_call(kept=10, min_kept=4, customer_msgs=0,
                                    min_customer_msgs=1).detail["failed_on"] == "customer_msgs")
    check("B4：文本太短 → 拒绝",
          hooks.before_llm_call(kept=10, min_kept=4, customer_msgs=3, min_customer_msgs=1,
                                text_len=20, min_effective_chars=100).detail["failed_on"]
          == "text_len")
    check("B4：三口径全过 → 放行",
          hooks.before_llm_call(kept=10, min_kept=4, customer_msgs=3, min_customer_msgs=1,
                                text_len=500, min_effective_chars=100).allow is True)
    check("B4：阈值 0 = 不限（不影响既有行为）",
          hooks.before_llm_call(kept=10, min_kept=4, customer_msgs=0, min_customer_msgs=0,
                                text_len=1, min_effective_chars=0).allow is True)


# ============================================================ 2. after_llm_output

def test_after_llm_output():
    print("\n--- 2. ② after_llm_output（闸4 / B2 / A1 / 闸2）---")
    rules = rules_mod.load_rules()
    # 取一条真实的正分规则与一条负分规则
    pos_rid = rules["positive_rules"][0]["rule_id"]
    neg_rid = [r for r in rules["negative_rules"] if r.get("engine") != "rule"][0]["rule_id"]

    r = hooks.after_llm_output(
        pos_items=[{"rule_id": pos_rid}], neg_items=[{"rule_id": neg_rid}],
        unchecked=["x"], invalid_items=[], rules=rules,
        total_llm=len(rules_mod.llm_rule_ids(rules)))
    check("正常输出 → 放行", r.allow is True, str(r))
    check("detail 含覆盖率", r.detail.get("coverage") is not None, str(r.detail))
    check("detail 含命中数 / 漏检数 / 无效项数",
          {"hits", "unchecked", "invalid"} <= set(r.detail), str(sorted(r.detail)))
    check("命中 2 项 → hits=2", r.detail["hits"] == 2, str(r.detail["hits"]))

    # ★ B2 极性终检：负分规则出现在加分项里 → 必须拒绝（这是流程错误）
    bad = hooks.after_llm_output(pos_items=[{"rule_id": neg_rid}], neg_items=[],
                                 unchecked=[], rules=rules)
    check("★ 负分规则混进加分项 → 拒绝（B2 极性终检）",
          bad.denied and bad.reason == "polarity_violation", str(bad))
    check("极性违规时 detail 列出违规 rule_id",
          bad.detail.get("polarity_violations") == [neg_rid], str(bad.detail))

    bad2 = hooks.after_llm_output(pos_items=[], neg_items=[{"rule_id": pos_rid}],
                                  unchecked=[], rules=rules)
    check("★ 加分规则混进负分项 → 同样拒绝", bad2.reason == "polarity_violation", str(bad2))

    check("不传 rules → 不做极性检查（但仍给体检报告）",
          hooks.after_llm_output(pos_items=[{"rule_id": neg_rid}], rules=None).allow is True)
    check("无命中项 → invalid_ratio = 0（不除零）",
          hooks.after_llm_output(pos_items=[], neg_items=[], rules=rules).detail["invalid_ratio"] == 0.0)


# ============================================================ 3. before_writeback

def test_before_writeback():
    print("\n--- 3. ③ before_writeback（D2 / 幂等 / 配额）---")
    check("无正分 → no_positive", hooks.before_writeback(positive_total=0).reason == "no_positive")
    check("mode=off → mode_off", hooks.before_writeback(positive_total=3, mode="off").reason == "mode_off")
    check("mode=dry_run → mode_dry_run",
          hooks.before_writeback(positive_total=3, mode="dry_run").reason == "mode_dry_run")
    check("mode=live 且有正分 → 放行",
          hooks.before_writeback(positive_total=3, mode="live").allow is True)
    check("已写过 → already_written（幂等，先于 dry_run 判定）",
          hooks.before_writeback(positive_total=3, mode="live",
                                 already_written=True).reason == "already_written")
    check("本轮配额用尽 → quota_run",
          hooks.before_writeback(positive_total=3, mode="live", quota_run=5,
                                 quota_run_limit=5).reason == "quota_run")
    check("单客户日配额用尽 → quota_customer",
          hooks.before_writeback(positive_total=3, mode="live", quota_customer=3,
                                 quota_customer_limit=3).reason == "quota_customer")
    check("配额上限 = 0 → 不限（放行）",
          hooks.before_writeback(positive_total=3, mode="live", quota_run=999,
                                 quota_run_limit=0).allow is True)
    check("off 优先于配额（关闭时不看配额）",
          hooks.before_writeback(positive_total=3, mode="off", quota_run=99,
                                 quota_run_limit=1).reason == "mode_off")
    check("reason 均有中文文案", all(r in hooks.REASON_CN for r in
                                    ("no_positive", "mode_off", "mode_dry_run", "already_written",
                                     "quota_run", "quota_customer")))


def test_writeback_parity_with_legacy():
    """★ 关键：接入 hook 后，回写 status 与**改造前的 if-else 完全一致**。

    改造前的 `_writeback` 逻辑（复刻为参考实现）:
        status = "skipped"
        if score >= 1:
            if mode == "off":     status = "skipped"
            elif mode == "dry_run": status = "dry_run"
            else:                 status = <真投递>   # live
    这里把它写成函数，与 hook 的 reason→status 映射**逐组合**对比。
    """
    print("\n--- 3b. ★ 与改造前逐字一致（表驱动，9 组全对比）---")

    def legacy(score, mode):
        if score >= 1:
            if mode == "off":
                return "skipped"
            if mode == "dry_run":
                return "dry_run"
            return "LIVE"                      # 真投递
        return "skipped"

    def via_hook(score, mode):
        r = hooks.before_writeback(positive_total=score, mode=mode)
        return "LIVE" if r.allow else hooks.REASON_TO_WB_STATUS.get(r.reason, "skipped")

    rows = [(s, m) for s in (0, 1, 3) for m in ("off", "dry_run", "live")]
    bad = [(s, m, legacy(s, m), via_hook(s, m)) for s, m in rows if legacy(s, m) != via_hook(s, m)]
    check("9 组 (score × mode) 的 status 判定全等", not bad, str(bad))
    check("对照组：确实覆盖了 3 种结果",
          {legacy(s, m) for s, m in rows} == {"skipped", "dry_run", "LIVE"},
          str({legacy(s, m) for s, m in rows}))


# ============================================================ 4. before_persist

def test_before_persist():
    print("\n--- 4. ④ before_persist（H3 落库前完整性）---")
    ok_score = {"raw_score": 1, "positive_total": 1, "negative_total": 0}
    ok_kw = dict(rel_key="s:t", staff_id="s", target_id="t", session_id="sess",
                 score=ok_score, grade={"grade": "C"}, rule_version="v1.0")
    check("齐全 → 放行", hooks.before_persist(**ok_kw).allow is True)
    check("关系键缺 ':' → bad_relation_key",
          hooks.before_persist(**{**ok_kw, "rel_key": "st"}).reason == "bad_relation_key")
    check("关系键为空 → bad_relation_key",
          hooks.before_persist(**{**ok_kw, "rel_key": None}).reason == "bad_relation_key")
    check("target_id 缺失 → missing_field",
          hooks.before_persist(**{**ok_kw, "target_id": None}).reason == "missing_field")
    check("session_id 缺失 → missing_field",
          hooks.before_persist(**{**ok_kw, "session_id": None}).reason == "missing_field")
    check("score 缺 raw_score → missing_field",
          hooks.before_persist(**{**ok_kw, "score": {"positive_total": 1,
                                                     "negative_total": 0}}).reason == "missing_field")
    check("grade 无等级值 → missing_field",
          hooks.before_persist(**{**ok_kw, "grade": {}}).reason == "missing_field")
    check("rule_version 为空 → missing_field",
          hooks.before_persist(**{**ok_kw, "rule_version": ""}).reason == "missing_field")


# ============================================================ 5. 接入点：拒绝留痕

def test_note_hook():
    print("\n--- 5. 接入点：`_note_hook` 只对拒绝计数 ---")
    svc = ss.service
    svc._progress = {}
    svc._note_hook(hooks.before_llm_call(kept=10, min_kept=4))                 # 放行 → 不计
    svc._note_hook(hooks.before_llm_call(kept=1, min_kept=4))                  # 拒绝 → 计 1
    svc._note_hook(hooks.before_llm_call(kept=2, min_kept=4))                  # 拒绝 → 计 2
    svc._note_hook(hooks.before_writeback(positive_total=0))                   # 拒绝 → 另一键
    check("放行不计数", "hook_denials" in svc._progress, str(svc._progress.get("hook_denials")))
    check("同一原因累加（insufficient_content = 2）",
          svc._progress["hook_denials"].get("insufficient_content") == 2,
          str(svc._progress.get("hook_denials")))
    check("不同原因分开计（no_positive = 1）",
          svc._progress["hook_denials"].get("no_positive") == 1,
          str(svc._progress.get("hook_denials")))
    check("只统计拒绝原因（键数 = 2）",
          len(svc._progress["hook_denials"]) == 2, str(sorted(svc._progress["hook_denials"])))


# ============================================================ 6. 接入点：回写 status 不变（真跑）

def test_writeback_integration():
    print("\n--- 6. 接入点：`_writeback` 三种模式的落库 status ---")
    rid = 999777001
    try:
        for mode, expect in (("off", "skipped"), ("dry_run", "dry_run")):
            for aid in (rid, rid + 1):
                db.execute("DELETE FROM score_writeback WHERE assessment_id=%s", (aid,))
            svc = ss.service
            svc._progress = {}
            st = svc._writeback(0, "t:rel", rid, "t_staff", "t_target", 3, mode, False)
            row = db.query_one("SELECT status, mode, errmsg FROM score_writeback WHERE assessment_id=%s", (rid,))
            check(f"mode={mode} → 返回 {expect}", st == expect, f"got={st}")
            check(f"mode={mode} → 落库 status={expect}", row and row["status"] == expect,
                  str(row))
        # live 模式但已写过 → 被幂等闸拦下（不再投递）
        db.execute("DELETE FROM score_writeback WHERE assessment_id=%s", (rid + 2,))
        db.execute("""INSERT INTO score_writeback (relationship_key, assessment_id, staff_id,
                          external_user_id, score, mode, status)
                      VALUES ('t:rel', %s, 't_staff', 't_target', 3, 'live', 'ok')""", (rid + 2,))
        ss.service._progress = {}
        st = ss.service._writeback(0, "t:rel", rid + 2, "t_staff", "t_target", 3, "live", False)
        check("live + 已写过 → 被幂等闸拦下（skipped）", st == "skipped", str(st))
        check("幂等闸未覆盖原记录（仍只有 1 行 ok）",
              db.query_one("SELECT count(*) AS n FROM score_writeback WHERE assessment_id=%s",
                           (rid + 2,))["n"] == 1)
    finally:
        for aid in (rid, rid + 1, rid + 2):
            db.execute("DELETE FROM score_writeback WHERE assessment_id=%s", (aid,))


# ============================================================ 7. 接入点：before_persist 生效

def test_persist_gate():
    print("\n--- 7. 接入点：`_persist_assessment` 的落库前校验 ---")
    cand_bad = {"staff_id": "s", "session_id": "sess", "target_id": None, "msg_count": 5}
    try:
        ss.service._persist_assessment(
            999777002, cand_bad, {"rule_version": "v1"}, {"raw_score": 0, "positive_total": 0,
                                                          "negative_total": 0},
            {"grade": "C"}, False, [], [], [], "off")
        check("缺 target_id → 应当抛错", False, "没抛错")
    except ValueError as e:
        check("缺 target_id → 抛 ValueError（而不是走到 SQL 才炸）",
              "missing_field" in str(e) or "bad_relation_key" in str(e), str(e)[:80])
    except Exception as e:
        check("缺 target_id → 抛 ValueError", False, f"抛了 {type(e).__name__}: {e}")


# ============================================================ 8. 设置项

def test_settings():
    print("\n--- 8. 新设置项已登记（配额，边界3）---")
    # ⚠️ 原实现是 grep `api_scoring.py` 的**源码文本**找键名 —— 这是脆弱断言：
    # 配置项一旦搬到别处（本轮搬到 `settings_registry.py` 完成「读写两侧同源」），
    # 断言就失效，但它证明的是「源码里有这个字符串」而不是「配置真的可用」。
    # 改为**行为断言**：从注册表查（可写）+ 实际取值（可读）→ 搬到哪里都不会误报。
    import settings_registry as reg
    writable = {a for a, _ in reg.WRITABLE}
    check("writeback_quota_per_run 已登记（可写）",
          "writeback_quota_per_run" in writable)
    check("writeback_quota_per_customer_per_day 已登记（可写）",
          "writeback_quota_per_customer_per_day" in writable)
    check("两项都在注册表里有类型与默认值声明",
          all(s.type == reg.INT for s in reg.SETTINGS
              if s.key.startswith("writeback_quota_")),
          str([(s.key, s.type) for s in reg.SETTINGS if s.key.startswith("writeback_quota_")]))
    check("默认 0 = 不限（不改变既有行为）",
          ss.setting_int("writeback_quota_per_run", "WRITEBACK_QUOTA_PER_RUN", 0) == 0)


# ============================================================

def main():
    print("=" * 70)
    print("显式钩子验收（架构 §2.4 H3）")
    print("=" * 70)
    test_pure_functions()
    test_before_llm_call()
    test_after_llm_output()
    test_before_writeback()
    test_writeback_parity_with_legacy()
    test_before_persist()
    test_note_hook()
    test_writeback_integration()
    test_persist_gate()
    test_settings()
    print("\n" + "=" * 70)
    print("PASS %d / FAIL %d" % (len(PASSED), len(FAILED)))
    if FAILED:
        for f in FAILED:
            print("  FAILED:", f)
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
