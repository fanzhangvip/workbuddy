# -*- coding: utf-8 -*-
"""C-B 验收：③ 增量段 + ⑤ 兜底条件化 + 预算（架构 §4.8 第二步 · §4.7 的 V3 / V4）。

对应架构条款
    §4.5.1  ③ 增量 = ``chat_message WHERE msg_time > watermark``；⑤ 兜底 = 最近 60 条
    §4.5.2  装配算法：水位取 ``customer_intent_assessment.last_msg_time``（不需要新表）
    §4.5.4  预算 ≈12k token；裁剪顺序「先砍 ⑤ → 再砍 ③ 最早」
    §4.7    V3 确定性 · V4 预算（p99 长会话 ctx_tokens ≤ 12k；超限 ctx_trim 非空）
    §4.8    C-B 依赖 C-A；本轮 ⑤ 的触发阈值取「窗口」（等 C-C 的 ①② 落地后再收紧）

跑法：``.venv\\Scripts\\python.exe tests/test_cb_delta_and_budget.py``
"""
# 统一输出编码（修 P2-2）：默认 GBK 控制台下，断言名里的 ↔ → 等字符会触发
# UnicodeEncodeError —— 看起来像测试失败，其实是环境问题。
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.stderr.reconfigure(encoding="utf-8")

import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "backend"))
sys.path.insert(0, BASE)

import db                        # noqa: E402
import scoring_service as ss     # noqa: E402

_TOTAL, _FAILED = 0, 0
_REL = "CBTEST:delta-probe"      # 合成关系键：水位只认它，不碰任何真实客户
_CODE = re.compile(r"^#\d{3,} ")


def check(name, cond, extra=""):
    global _TOTAL, _FAILED
    _TOTAL += 1
    if cond:
        print(f"  ok   {name}")
    else:
        _FAILED += 1
        print(f"  FAIL {name}  {extra}")


def _strip_time(lines):
    return [re.sub(r" \[[^\]]*\] ", " ", l) for l in lines]


def _pick_session():
    return db.query_one(
        """SELECT s.staff_id, s.session_id, s.target_id, s.msg_count
             FROM chat_session s
            WHERE s.session_type = 'ext_user' AND s.target_id IS NOT NULL
              AND (SELECT count(*) FROM chat_message m
                    WHERE m.staff_id = s.staff_id AND m.session_id = s.session_id
                      AND (m.plain IS NOT NULL OR (m.content IS NOT NULL AND m.content <> ''))) > 200
            ORDER BY s.msg_count DESC LIMIT 1""")


def _readable(sid):
    return db.query(
        """SELECT message_id, msg_time FROM chat_message
            WHERE staff_id = %s AND session_id = %s
              AND (plain IS NOT NULL OR (content IS NOT NULL AND content <> ''))
            ORDER BY msg_time ASC, message_id ASC""",
        (sid["staff_id"], sid["session_id"]))


def _mk_watermark(staff_id, wm):
    """给合成关系键插一条评估，只为了提供水位（不产生任何真实业务痕迹）。"""
    db.execute("DELETE FROM customer_intent_assessment WHERE relationship_key = %s", (_REL,))
    # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
    try: ss.repair_current_flags()
    except Exception: pass
    db.execute(
        """INSERT INTO customer_intent_assessment (relationship_key, staff_id, last_msg_time)
           VALUES (%s, %s, %s)""",
        (_REL, staff_id, int(wm)))


def _clear_watermark():
    db.execute("DELETE FROM customer_intent_assessment WHERE relationship_key = %s", (_REL,))
    # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
    try: ss.repair_current_flags()
    except Exception: pass


def _set(key, val):
    db.set_setting(key, str(val))


def _clear(*keys):
    for k in keys:
        db.execute("DELETE FROM app_setting WHERE key = %s", (k,))


def main():
    S = _pick_session()
    if not S:
        print("!!! 找不到样本会话（需要可读消息 > 200 的 ext_user 会话）")
        return 1
    rows = _readable(S)
    ids = [r["message_id"] for r in rows]
    times = [r["msg_time"] for r in rows]
    meta = {"staff_id": S["staff_id"], "session_id": S["session_id"]}
    print(f"样本会话：{S['staff_id'][:12]}… / 可读 {len(ids)} 条")

    _clear_watermark()
    _clear("ctx_delta_max", "ctx_tokens_max", "ctx_fallback_min")

    try:
        # ── 1. ③ 的取数单元：水位过滤 + 软上限 ─────────────────────────────
        print("\n--- 1. ③ 增量取数（水位过滤 / 软上限）---")
        mid = times[len(times) // 2 - 1]
        _mk_watermark(S["staff_id"], mid)
        wm = ss._watermark(_REL)
        check("水位读回（来自 last_msg_time）", wm == int(mid), f"{wm} vs {mid}")
        d = ss._delta_rows(S["staff_id"], S["session_id"], wm, 200)
        check("③ 只含水位之后的消息",
              all(int(r["msg_time"]) > int(mid) for r in d), f"n={len(d)}")
        check("③ 恰好等于水位之后的总数",
              len(d) == sum(1 for t in times if t > int(mid)), f"{len(d)}")

        d_all = ss._delta_rows(S["staff_id"], S["session_id"], None, 200)
        check("水位为空 → 增量 = 全量（受软上限截断到 200）", len(d_all) == 200, str(len(d_all)))
        check("超上限时保留的是**最新的**那批",
              [r["message_id"] for r in d_all] == ids[-200:], "取的不是最新 200 条")

        _clear_watermark()
        _set("ctx_delta_max", 50)
        d50 = ss._delta_rows(S["staff_id"], S["session_id"], None,
                             ss.setting_int("ctx_delta_max", "CTX_DELTA_MAX", 200))
        check("ctx_delta_max 生效（软上限 50）", len(d50) == 50, str(len(d50)))
        _clear("ctx_delta_max")

        # ── 2. 无水位：③ = 全量，⑤ 不启用（③ 已铺满窗口）───────────────────
        print("\n--- 2. 无水位 → ③ 铺满窗口，不启用 ⑤ ---")
        ctx = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
        st = ctx["stats"]
        check("seg_delta > 60（③ 确实铺满窗口）", st["seg_delta"] > 60, str(st["seg_delta"]))
        check("seg_fallback = 0（⑤ 未被启用）", st["seg_fallback"] == 0, str(st["seg_fallback"]))
        check("④ 锚点仍在（最早 10 条）", st["key"] == 10, str(st["key"]))
        inv = set(ctx["index"].values())
        check("③ 覆盖了最新消息", all(m in inv for m in ids[-10:]), "最新 10 条缺失")

        # ── 3. 水位靠尾：由 ctx_fallback_min 决定要不要补 ⑤ ──────────────
        print("\n--- 3. 水位靠尾 → ctx_fallback_min 决定是否补 ⑤ ---")
        _mk_watermark(S["staff_id"], times[-5])
        try:
            # 3a. 默认阈值（=4）：③④ 已有 14 条 → **不补 ⑤**，③ 保持「增量」语义
            c2 = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
            s2 = c2["stats"]
            check("seg_delta 很小（只 4 条新消息）", 0 < s2["seg_delta"] <= 5, str(s2["seg_delta"]))
            check("默认阈值(4) → 不补 ⑤（③ 真正是「增量」）",
                  s2["seg_fallback"] == 0, str(s2["seg_fallback"]))
            check("总条数 = ③ + ④（不再被 ⑤ 吞并）",
                  s2["kept"] == s2["seg_delta"] + s2["key"],
                  f"kept={s2['kept']} ③={s2['seg_delta']} ④={s2['key']}")

            # 3b. 阈值设 60（退回 C-B 的保守行为）：补到窗口规模
            _set("ctx_fallback_min", 60)
            try:
                c2b = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
                s2b = c2b["stats"]
                check("ctx_fallback_min=60 → 补 ⑤", s2b["seg_fallback"] > 0, str(s2b["seg_fallback"]))
                inv2 = set(c2b["index"].values())
                cover = sum(1 for m in ids[-60:] if m in inv2)
                check("最近 60 条覆盖率 ≥ 55（对话连续性保住了）", cover >= 55, str(cover))
                check("总条数 ≥ 窗口 60", s2b["kept"] >= 60, str(s2b["kept"]))
            finally:
                _clear("ctx_fallback_min")

            # ── 4. V4 预算：⑤ 第一个被砍，③ 次之 ─────────────────────────
            # 把阈值设回 60，让 ⑤ 存在，才验证得了「先砍 ⑤」这一步
            print("\n--- 4. V4 预算与裁剪顺序 ---")
            _set("ctx_fallback_min", 60)
            try:
                _set("ctx_tokens_max", 2500)
                try:
                    c3 = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
                    s3 = c3["stats"]
                    check("超限时 trim 非空（V4）", bool(s3["trim"]), str(s3["trim"]))
                    check("第一个砍的是 ⑤（d5 非空）", s3["trim"].get("d5", 0) > 0, str(s3["trim"]))
                    check("⑤ 砍完 seg_fallback 归零", s3["seg_fallback"] == 0, str(s3["seg_fallback"]))
                    check("③ 未被砍（seg_delta 仍在）", s3["seg_delta"] > 0, str(s3["seg_delta"]))
                    check("砍完即达标 → budget_exceeded = False",
                          s3["budget_exceeded"] is False, str(s3["budget_exceeded"]))
                    check("tokens ≤ tokens_max（V4）", s3["tokens"] <= s3["tokens_max"],
                          f"{s3['tokens']} > {s3['tokens_max']}")
                finally:
                    _clear("ctx_tokens_max")

                _set("ctx_tokens_max", 300)      # 连 ④ 都装不下 → 砍完 ③ 仍超限
                try:
                    c4 = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
                    s4 = c4["stats"]
                    check("⑤ 砍完还超 → 继续砍 ③（d3 非空）", s4["trim"].get("d3", 0) > 0, str(s4["trim"]))
                    check("③ 被砍到 0", s4["seg_delta"] == 0, str(s4["seg_delta"]))
                    check("④ 仍保留（维度不再被砍 —— D4 拍板）", s4["key"] == 10, str(s4["key"]))
                    check("仍超限 → budget_exceeded = True（照原样发送 + 告警）",
                          s4["budget_exceeded"] is True, str(s4["budget_exceeded"]))
                finally:
                    _clear("ctx_tokens_max")
            finally:
                _clear("ctx_fallback_min")

            # ── 5. V3 确定性（③⑤ 合并后仍可复现）─────────────────────────
            print("\n--- 5. V3 确定性 ---")
            a = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
            b = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
            check("两次调用（去时间标签后）行序列一致",
                  _strip_time(a["lines"]) == _strip_time(b["lines"]))
            check("两次调用 index 一致", a["index"] == b["index"])
        finally:
            _clear_watermark()

        # ── 6. 观测字段齐备（架构 §4.6）───────────────────────────────────
        print("\n--- 6. 观测字段（§4.6）---")
        ctx = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
        st = ctx["stats"]
        for k in ("watermark", "seg_delta", "seg_fallback", "tokens", "tokens_max",
                  "budget_exceeded", "trim", "key", "key_rules", "key_source"):
            check(f"stats['{k}'] 存在", k in st, str(sorted(st.keys())))
        check("p99 长会话 tokens ≤ 12k（V4 默认预算）",
              st["tokens"] <= st["tokens_max"], f"{st['tokens']} > {st['tokens_max']}")

        # ── 7. 回滚旋钮：ctx_delta_max=0 → 退回「最近窗口」──────────────────
        print("\n--- 7. ctx_delta_max = 0（可回滚）---")
        # 关增量后 ③=0，若要"退回最近窗口"需同时把 ⑤ 阈值设回窗口规模（旧行为）
        _set("ctx_delta_max", 0)
        _set("ctx_fallback_min", 60)
        try:
            off = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
            so = off["stats"]
            check("关掉增量 → seg_delta = 0", so["seg_delta"] == 0, str(so["seg_delta"]))
            check("阈值=60 → ⑤ 兜底接管", so["seg_fallback"] > 0, str(so["seg_fallback"]))
            check("仍不低于窗口条数", so["kept"] >= 60, str(so["kept"]))
        finally:
            _clear("ctx_delta_max", "ctx_fallback_min")

        # ── 8. ④ 开关与 ③ 互不干扰 ───────────────────────────────────────
        print("\n--- 8. ④ 开关不影响 ③ ---")
        _set("ctx_key_history", "false")
        try:
            no_key = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
            check("关 ④ → key = 0", no_key["stats"]["key"] == 0, str(no_key["stats"]["key"]))
            check("关 ④ 不影响 ③（seg_delta 仍 > 60）",
                  no_key["stats"]["seg_delta"] > 60, str(no_key["stats"]["seg_delta"]))
        finally:
            _clear("ctx_key_history")

        # ── 9. 展示壳 build_conversation 仍是「最近 N 条」───────────────────
        print("\n--- 9. build_conversation 语义不变 ---")
        conv = ss.build_conversation(S["staff_id"], S["session_id"], limit=60)
        n_msg = len([l for l in conv.split("\n") if _CODE.match(l)])
        check("展示壳消息行 ≤ 60", n_msg <= 60, str(n_msg))
        check("展示壳不含 ④ 的早期锚点（不掺关键历史）",
              ids[0] not in conv, "最早那条不该出现")

        # ── 10. 评分链路两条路径装配口径一致（图 / 旧）────────────────────────
        print("\n--- 10. 图路径与旧路径同源取数 ---")
        import graph_session           # noqa: E402
        orig_pin = graph_session.ss.build_context
        seen = {}

        def spy(*a, **kw):
            seen.update(kw)
            return orig_pin(*a, **kw)

        graph_session.ss.build_context = spy
        try:
            import analyze             # noqa: E402

            def stub(*a, **kw):
                return {"positive_items": [], "negative_items": [], "invalid_items": [],
                        "unchecked_rules": [], "profile_summary": "", "sales_strategy": "",
                        "model_confidence": 0.0, "rule_version": "v1.0", "prompt_version": "t",
                        "schema_version": "1", "contract_attempts": 1}

            orig = analyze.analyze
            analyze.analyze = stub
            cand = {"staff_id": S["staff_id"], "session_id": S["session_id"],
                    "target_id": S["target_id"], "msg_count": S["msg_count"]}
            try:
                graph_session.run_session(ss.service, 996000001, cand, ss.rules_mod.load_rules(),
                                          8, "off", False)
                check("图路径经 build_context 取数", bool(seen), "未调用")
                check("图路径传了 rel_key（水位口径与旧路径一致）",
                      seen.get("rel_key") == f"{S['staff_id']}:{S['target_id']}", str(seen.get("rel_key")))
            finally:
                analyze.analyze = orig
                for tbl in ("writeback_outbox", "negative_review", "score_writeback"):
                    db.execute(f"""DELETE FROM {tbl} WHERE assessment_id IN
                                   (SELECT assessment_id FROM customer_intent_assessment
                                     WHERE source_run_id = 996000001)""")
                db.execute("DELETE FROM customer_intent_assessment WHERE source_run_id = 996000001")
                # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
                try: ss.repair_current_flags()
                except Exception: pass
        finally:
            graph_session.ss.build_context = orig_pin

    finally:
        _clear_watermark()
        _clear("ctx_delta_max", "ctx_tokens_max", "ctx_key_history", "ctx_fallback_min")

    print(f"\n===== {_TOTAL - _FAILED}/{_TOTAL} 通过 =====")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
