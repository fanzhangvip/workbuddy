# -*- coding: utf-8 -*-
"""C-C 验收：①② 记忆段 + ④ 分组收缩 + 提示词说明（架构 §4.8 第三步 · §4.7 的 V2 / V4 / V5）。

对应架构条款
    §4.5.1  ① 客户档案（**降级**读最新 assessment）· ② 上次评分明细
    §4.5.3  ①② 是「记忆块」不是「消息行」：不带 ``#编号``、不进 ``index``（决议 M-C）
    §4.5.4  裁剪顺序第 2 步：④ 中「每组 >1 条」收缩为每组 1 条；①② 永不裁剪
    §4.5.6  SYSTEM_PROMPT 必须说明「①② 是上次结论、只有 #编号 行是本次证据」
    §4.7    V2 记忆真的被读进去 · V4 预算 · V5 证据回链不被破坏

跑法：``.venv\\Scripts\\python.exe tests/test_cc_memory_segments.py``
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

import analyze                   # noqa: E402
import db                        # noqa: E402
import scoring_service as ss     # noqa: E402

_TOTAL, _FAILED = 0, 0
_REL = "CCTEST:cc-probe"          # 合成关系键：只用于提供「上次评估」，不碰真实客户
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
    """挑一个「可读消息数 > 120」的会话当样本（长会话才能压出裁剪路径）。"""
    return db.query_one(
        """SELECT s.staff_id, s.session_id, s.target_id, s.msg_count
             FROM chat_session s
            WHERE s.session_type = 'ext_user' AND s.target_id IS NOT NULL
              AND (SELECT count(*) FROM chat_message m
                    WHERE m.staff_id = s.staff_id AND m.session_id = s.session_id
                      AND (m.plain IS NOT NULL OR (m.content IS NOT NULL AND m.content <> ''))) > 120
            ORDER BY s.msg_count DESC, s.session_id LIMIT 1""")


def _readable_ids(S):
    rows = db.query(
        """SELECT message_id FROM chat_message
            WHERE staff_id = %s AND session_id = %s
              AND (plain IS NOT NULL OR (content IS NOT NULL AND content <> ''))
            ORDER BY msg_time ASC, message_id ASC""",
        (S["staff_id"], S["session_id"]))
    return [r["message_id"] for r in rows]


def _mk_last(S, *, grade="B", raw=3, pos=None, neg=None,
             prof="客户是一名 Java 后端工程师，想转大模型方向。", strat="先约体验课。"):
    """造一条「上次评估」，只为让 ①② 有内容可读。"""
    db.execute("DELETE FROM customer_intent_assessment WHERE relationship_key = %s", (_REL,))
    # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
    try: ss.repair_current_flags()
    except Exception: pass
    db.execute(
        """INSERT INTO customer_intent_assessment
             (relationship_key, staff_id, session_id, raw_score, positive_total, negative_total,
              grade, positive_items, negative_items, profile_summary, sales_strategy,
              msg_count, last_msg_time, rule_version, prompt_version)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (_REL, S["staff_id"], S["session_id"], raw,
         sum(i["score"] for i in (pos or [])), sum(i["score"] for i in (neg or [])),
         grade, db.as_json(pos or []), db.as_json(neg or []), prof, strat,
         S["msg_count"], int(__import__("time").time()) - 86400, "v1.0", "v3"))


def _clear_settings(*keys):
    """只清设置项 —— 千万别顺手清评估，否则后续用例会「没有记忆可读」。"""
    for k in keys:
        db.execute("DELETE FROM app_setting WHERE key = %s", (k,))


def _clear_last():
    db.execute("DELETE FROM customer_intent_assessment WHERE relationship_key = %s", (_REL,))
    # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
    try: ss.repair_current_flags()
    except Exception: pass


def main():
    S = _pick_session()
    if not S:
        print("!!! 找不到样本会话（需要可读消息 > 120 的 ext_user 会话）")
        return 1
    ids = _readable_ids(S)
    meta = {"staff_id": S["staff_id"], "session_id": S["session_id"]}
    S = dict(S)
    S["target_id"] = S["target_id"]
    print(f"样本会话：{S['staff_id'][:12]}… / 可读 {len(ids)} 条")

    _clear_settings("ctx_memory", "ctx_tokens_max", "ctx_fallback_min")

    try:
        # ── 1. V2：记忆真的被读进去 ────────────────────────────────────────
        print("\n--- 1. V2 记忆真的被读进去 ---")
        _mk_last(S, pos=[{"rule_id": "positive_coder_profile", "score": 2, "dimension": "画像"},
                         {"rule_id": "positive_ask_course", "score": 2, "dimension": "问课"}],
                 neg=[{"rule_id": "negative_price_haggling", "score": -1, "dimension": "比价"}])
        ctx = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
        st = ctx["stats"]
        check("① 段非空（V2）", st["seg_profile"] == 1, str(st["seg_profile"]))
        check("② 段非空", st["seg_last"] == 1, str(st["seg_last"]))
        head = ctx["text"].split("\n\n")[0]
        check("① 段标形如【客户状态档案】", head.startswith("【客户状态档案】"), head[:30])
        check("① 段带上次等级与分值", "上次等级=B" in head and "上次总分=+3" in head, head[:120])
        check("① 段带画像摘要", "Java 后端工程师" in head)
        check("① 段明示「非本次证据」", "非本次证据" in head)
        second = ctx["text"].split("\n\n")[1]
        check("② 段标形如【上次评分明细】", second.startswith("【上次评分明细】"), second[:30])
        check("② 段列出上次加分 rule_id 与分值",
              "positive_coder_profile(+2)" in second and "positive_ask_course(+2)" in second, second[:200])
        check("② 段列出上次扣分", "negative_price_haggling(-1)" in second, second[:200])
        check("② 段给合计", "合计 +4" in second and "合计 -1" in second, second[:200])

        # ── 2. V5：①② 不进 index，也不能当证据 ────────────────────────────
        print("\n--- 2. V5 证据回链不被破坏 ---")
        check("index 只含消息行（无 [M1]/[M2]）",
              all(k.startswith("#") for k in ctx["index"]), str(list(ctx["index"])[:3]))
        check("index 条数与消息行一致", len(ctx["index"]) == len(ctx["lines"]),
              f"{len(ctx['index'])}/{len(ctx['lines'])}")
        check("①② 段内不含 #编号",
              not any("#" in l for seg in ctx["text"].split("\n\n")[:2] for l in seg.split("\n")))
        kept, dropped = analyze._attach_evidence(
            [{"rule_id": "positive_coder_profile", "score": 2, "evidence_ids": ["[M1]"]},
             {"rule_id": "positive_ask_course", "score": 2, "evidence_ids": [ctx["lines"][0].split(" ")[0]]}],
            ctx["index"], ctx["labels"])
        check("引用 [M1] 的项被丢弃（V5）", len(dropped) == 1 and not dropped[0].get("codes"),
              str([d.get("invalid_evidence_ids") for d in dropped]))
        check("丢弃项写明原因", "无法回链" in (dropped[0].get("reason") or ""), str(dropped[0].get("reason")))
        check("合法 #编号 的项被保留", len(kept) == 1 and bool(kept[0].get("evidence")), str(len(kept)))

        # ── 3. ①② 永不裁剪（V4 的配套约束）─────────────────────────────
        print("\n--- 3. 预算压到 300：①② 仍在，只砍 ③⑤ ---")
        # 把 ⑤ 阈值设回 60，让 ⑤ 存在，才验证得了「优先砍 ⑤」这一步
        db.set_setting("ctx_fallback_min", "60")
        db.set_setting("ctx_tokens_max", "300")
        try:
            c2 = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
            s2 = c2["stats"]
            check("① ② 段仍保留", s2["seg_profile"] == 1 and s2["seg_last"] == 1,
                  f"{s2['seg_profile']}/{s2['seg_last']}")
            check("确实触发了裁剪", bool(s2["trim"]), str(s2["trim"]))
            check("裁剪只动 ③⑤（键在 d5/d3/shrunk4 里）",
                  set(s2["trim"]) <= {"d5", "d3", "shrunk4"}, str(s2["trim"]))
            check("超限如实上报（①② 不砍 → 允许 exceeded）",
                  s2["budget_exceeded"] is True, str(s2["budget_exceeded"]))
            check("①② 文本确实还在 text 里", c2["text"].startswith("【客户状态档案】"))
        finally:
            _clear_settings("ctx_tokens_max", "ctx_fallback_min")

        # ── 4. ④ 分组收缩（§4.5.4 第 2 步）────────────────────────────────
        print("\n--- 4. ④ 分组收缩（同 rule 每组留最新 1 条）---")
        # 给同一个规则撒 30 条（④ 的配额上限），再压预算逼出收缩
        ss.service._tag_evidence_messages(
            S["staff_id"], S["session_id"],
            [{"rule_id": "positive_cooperative", "evidence_message_ids": ids[:30]}], [])
        try:
            wide = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
            n_before = wide["stats"]["key"]
            check("④ 段先有 >1 条（可收缩的前提）", n_before > 1, str(n_before))
            print(f"       (诊断) 未压预算时 tokens={wide['stats']['tokens']} ④={n_before}")
            # 预算压到很低：先砍 ⑤ 不够，必须继续做 ④ 收缩才可能达标
            db.set_setting("ctx_tokens_max", "600")
            try:
                c3 = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
                s3 = c3["stats"]
                print(f"       (诊断) 压预算后 tokens={s3['tokens']} trim={s3['trim']} ④={s3['key']}")
                check("触发 ④ 收缩（trim 出现 shrunk4）", s3["trim"].get("shrunk4", 0) > 0, str(s3["trim"]))
                check("收缩后 ④ 段条数下降", s3["key"] < n_before, f"{s3['key']} vs {n_before}")
                check("收缩不砍 ①②", s3["seg_profile"] == 1 and s3["seg_last"] == 1)
            finally:
                _clear_settings("ctx_tokens_max")
        finally:
            db.execute("""UPDATE chat_message SET rule_tags = '{}'
                           WHERE staff_id = %s AND session_id = %s""",
                       (S["staff_id"], S["session_id"]))

        # ── 5. V3 确定性 + V4 默认预算 ────────────────────────────────────
        print("\n--- 5. V3 确定性 / V4 默认预算 ---")
        a = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
        b = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
        check("两次调用（去时间标签后）行序列一致",
              _strip_time(a["lines"]) == _strip_time(b["lines"]))
        check("两次调用 text 完全一致（含①②）", a["text"] == b["text"])
        check("默认预算下 tokens ≤ 12000（V4）", a["stats"]["tokens"] <= a["stats"]["tokens_max"],
              f"{a['stats']['tokens']} > {a['stats']['tokens_max']}")

        # ── 6. 回滚旋钮 ───────────────────────────────────────────────────
        print("\n--- 6. ctx_memory=false（可回滚）---")
        db.set_setting("ctx_memory", "false")
        try:
            off = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
            check("关掉 → ①② 均空", off["stats"]["seg_profile"] == 0 and off["stats"]["seg_last"] == 0)
            check("关掉 → text 以 #001 开头", off["text"].startswith("#001 "), off["text"][:20])
        finally:
            _clear_settings("ctx_memory")

        # ── 7. 无上次评估 → ①② 自然为空（不编造）────────────────────────
        print("\n--- 7. 无评估的会话不渲染记忆段 ---")
        _clear_last()
        none_ctx = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
        check("无评估 → seg_profile/seg_last = 0",
              none_ctx["stats"]["seg_profile"] == 0 and none_ctx["stats"]["seg_last"] == 0)
        check("无评估 → text 不含【客户状态档案】", "【客户状态档案】" not in none_ctx["text"])

        # ── 8. 提示词说明（§4.5.6）───────────────────────────────────────
        print("\n--- 8. 提示词说明 ---")
        check("PROMPT_VERSION 已递增到 v3", analyze.PROMPT_VERSION == "v3", analyze.PROMPT_VERSION)
        check("SYSTEM_PROMPT 说明「只有 #编号 行是证据」",
              "只有带 #编号 的消息行才是本次判断的证据" in analyze.SYSTEM_PROMPT)
        check("SYSTEM_PROMPT 说明「不要重复计入上次加减分」",
              "不要因此把上次的加减分项重复计入本次命中项" in analyze.SYSTEM_PROMPT)
        up = analyze.build_user_prompt(ss.rules_mod.load_rules(), "x", "y")
        check("user prompt 标注了记忆段性质", "上次评估的结论" in up and "只有带 #编号 的行是本次证据" in up)

        # ── 9. 观测字段齐备 ───────────────────────────────────────────────
        print("\n--- 9. 观测字段（§4.6）---")
        _mk_last(S)
        ctx9 = ss.build_context(S["staff_id"], S["session_id"], rel_key=_REL, session_meta=meta)
        for k in ("seg_profile", "seg_last", "seg_delta", "seg_fallback", "key",
                  "tokens", "tokens_max", "budget_exceeded", "trim"):
            check(f"stats['{k}'] 存在", k in ctx9["stats"])
        prog = {}
        ss.service._progress = prog
        ss.service._bump_ctx(ctx9["stats"])
        check("_bump_ctx 累计 ctx_seg_profile", prog.get("ctx_seg_profile") == 1, str(prog.get("ctx_seg_profile")))
        check("_bump_ctx 累计 ctx_seg_last", prog.get("ctx_seg_last") == 1, str(prog.get("ctx_seg_last")))

        # ── 10. 图路径与旧路径读同一份上下文 ───────────────────────────────
        print("\n--- 10. 图路径同源取数 ---")
        import graph_session         # noqa: E402
        orig = graph_session.ss.build_context
        seen = {}

        def spy(*a, **kw):
            seen.update(kw)
            return orig(*a, **kw)

        graph_session.ss.build_context = spy
        try:
            stub = lambda *a, **kw: {          # noqa: E731
                "positive_items": [], "negative_items": [], "invalid_items": [],
                "unchecked_rules": [], "profile_summary": "", "sales_strategy": "",
                "model_confidence": 0.0, "rule_version": "v1.0", "prompt_version": "v3",
                "schema_version": "1", "contract_attempts": 1}
            o = analyze.analyze
            analyze.analyze = stub
            try:
                r = graph_session.run_session(ss.service, 997000001, S, ss.rules_mod.load_rules(),
                                              8, "off", False)
                check("图路径返回 scored", r == "scored", r)
                check("图路径经 build_context 取数并传 rel_key",
                      seen.get("rel_key") == f"{S['staff_id']}:{S['target_id']}", str(seen.get("rel_key")))
            finally:
                analyze.analyze = o
                for tbl in ("writeback_outbox", "negative_review", "score_writeback"):
                    db.execute(f"""DELETE FROM {tbl} WHERE assessment_id IN
                                   (SELECT assessment_id FROM customer_intent_assessment
                                     WHERE source_run_id = 997000001)""")
                db.execute("DELETE FROM customer_intent_assessment WHERE source_run_id = 997000001")
                # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
                try: ss.repair_current_flags()
                except Exception: pass
        finally:
            graph_session.ss.build_context = orig

    finally:
        _clear_settings("ctx_memory", "ctx_tokens_max", "ctx_fallback_min")
        _clear_last()
        db.execute("""UPDATE chat_message SET rule_tags = '{}'
                       WHERE staff_id = %s AND session_id = %s""",
                   (S["staff_id"], S["session_id"]))
        left = db.query_one("""SELECT count(*) AS n FROM customer_intent_assessment
                                WHERE relationship_key LIKE %s""", ("CCTEST%",))["n"]
        print(f"\n清理完成，CCTEST 合成评估残留 = {left}")

    print(f"\n===== {_TOTAL - _FAILED}/{_TOTAL} 通过 =====")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
