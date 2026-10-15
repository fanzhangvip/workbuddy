# -*- coding: utf-8 -*-
"""C-A 验收：关键历史检索（架构 §4.8 第一步 · §4.7 的 V1 / V3 / V6）。

对应架构条款
    §4.4.1  载体 ``chat_message.rule_tags`` + GIN，只增不减、幂等
    §4.4.2  「本次相关规则集」= K2
    §4.4.3  两阶段取数（每 rule 保底 1 条，总量 ≤ ctx_key_history_max）
    §4.4.4  ④′ 冷启动早期锚点
    §4.5.3  ③④⑤ 合并后**只编号一次** → ``#编号`` 全文唯一（证据回链 C2）
    §4.7    V1 调用点唯一 · V3 确定性 · V6 ④ 段有效性

跑法：``.venv\\Scripts\\python.exe tests/test_ca_key_history.py``
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


def check(name, cond, extra=""):
    global _TOTAL, _FAILED
    _TOTAL += 1
    if cond:
        print(f"  ok   {name}")
    else:
        _FAILED += 1
        print(f"  FAIL {name}  {extra}")


def _strip_time(lines):
    """去掉时间标签后的行序列（V3 要求可复现）。"""
    return [re.sub(r" \[[^\]]*\] ", " ", l) for l in lines]


def _pick_session():
    """挑一个「可读消息数最多」的 ext_user 会话当样本。"""
    return db.query_one(
        """SELECT s.staff_id, s.session_id, s.target_id, s.msg_count
             FROM chat_session s
            WHERE s.session_type = 'ext_user' AND s.target_id IS NOT NULL
              AND (SELECT count(*) FROM chat_message m
                    WHERE m.staff_id = s.staff_id AND m.session_id = s.session_id
                      AND (m.plain IS NOT NULL OR (m.content IS NOT NULL AND m.content <> ''))) > 200
            ORDER BY s.msg_count DESC, s.session_id LIMIT 1""")


def _readable(sid):
    return db.query(
        """SELECT message_id, msg_time FROM chat_message
            WHERE staff_id = %s AND session_id = %s
              AND (plain IS NOT NULL OR (content IS NOT NULL AND content <> ''))
            ORDER BY msg_time ASC, message_id ASC""",
        (sid["staff_id"], sid["session_id"]))


def _clear_tags(sid):
    db.execute("""UPDATE chat_message SET rule_tags = '{}'
                   WHERE staff_id = %s AND session_id = %s""",
               (sid["staff_id"], sid["session_id"]))


def main():
    S = _pick_session()
    if not S:
        print("!!! 找不到样本会话（需要可读消息 > 200 的 ext_user 会话）")
        return 1
    # ⚠️ 用**合成 rel_key** 而不是真实的 `staff:target`：
    #   本测试要验的是「④ 关键历史取数」的机制，前提是「③ 增量 = 全量」。
    #   但真实 rel 会被生产批量评分写入水位 → ③ 变成「水位后几条」→
    #   断言「带 ④ 后 kept 增加」会反转（实测：批量跑完后 5 vs 59 判失败）。
    #   合成 key 让 `_watermark()` 返回 None（③ = 全量），测试从此**不受生产数据影响**。
    #   注意：`rule_tags` 是按 message 落的，所以 ④ 取数路径照常覆盖。
    REL = f"_catest_:{S['session_id']}"
    rows = _readable(S)
    ids = [r["message_id"] for r in rows]
    print(f"样本会话：{S['staff_id'][:12]}… / 可读 {len(ids)} 条")

    _clear_tags(S)
    meta = {"staff_id": S["staff_id"], "session_id": S["session_id"]}

    try:
        # ── 1. V1 调用点唯一性：评分链路不得再调用 build_conversation ──────────
        print("\n--- 1. V1 调用点唯一性 ---")
        hits = []
        for rel in ("backend/scoring_service.py", "backend/graph_session.py",
                    "backend/api_scoring.py"):
            with open(os.path.join(BASE, rel), encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    s = line.strip()
                    if s.startswith("#") or s.startswith('"'):
                        continue
                    if "build_conversation(" in s and "def build_conversation" not in s:
                        hits.append(f"{rel}:{n}")
        check("评分链路无 build_conversation 调用点（V1）", hits == [], str(hits))
        with open(os.path.join(BASE, "backend/graph_session.py"), encoding="utf-8") as fh:
            check("图路径经 build_context 取数（V1）", "build_context(" in fh.read())

        # ── 2. ④′ 冷启动：无标签时取最早 N 条 ───────────────────────────────
        print("\n--- 2. ④′ 冷启动早期锚点（§4.4.4）---")
        ctx = ss.build_context(S["staff_id"], S["session_id"], rel_key=REL, session_meta=meta)
        check("无标签 → key_source = cold_start", ctx["stats"].get("key_source") == "cold_start",
              str(ctx["stats"].get("key_source")))
        check("无标签 → key_rules 为空（V6 对无标签会话不适用）",
              ctx["stats"].get("key_rules") == [], str(ctx["stats"].get("key_rules")))
        check("无标签 → 早期锚点已进入 index", ctx["stats"]["key"] > 0, str(ctx["stats"]["key"]))
        check("早期锚点即会话最早的若干条",
              ids[0] in ctx["index"].values(), "首条未出现在上下文")

        # ── 3. 标签写入：幂等 + 只增不减（§4.4.1）──────────────────────────
        print("\n--- 3. rule_tags 写入（幂等 / 只增不减）---")
        anchor = ids[:5]
        n1 = ss.service._tag_evidence_messages(
            S["staff_id"], S["session_id"],
            [{"rule_id": "positive_coder_profile", "evidence_message_ids": anchor}], [])
        check("首次写入命中 5 条", n1 == 5, str(n1))
        tags1 = [r["rule_tags"] for r in db.query(
            """SELECT rule_tags FROM chat_message
                WHERE staff_id=%s AND session_id=%s AND message_id = ANY(%s)""",
            (S["staff_id"], S["session_id"], anchor))]
        check("标签已落库", all(t == ["positive_coder_profile"] for t in tags1), str(tags1))

        ss.service._tag_evidence_messages(
            S["staff_id"], S["session_id"],
            [{"rule_id": "positive_coder_profile", "evidence_message_ids": anchor}], [])
        tags2 = [r["rule_tags"] for r in db.query(
            """SELECT rule_tags FROM chat_message
                WHERE staff_id=%s AND session_id=%s AND message_id = ANY(%s)""",
            (S["staff_id"], S["session_id"], anchor))]
        check("重复写入幂等（结果不变）", tags1 == tags2, str(tags2))

        # 同一批消息再叠一个规则 → 两条并存（只增不减）
        ss.service._tag_evidence_messages(
            S["staff_id"], S["session_id"],
            [{"rule_id": "positive_clear_work_need", "evidence_message_ids": anchor[:2]}], [])
        tags3 = db.query_one(
            """SELECT rule_tags FROM chat_message
                WHERE staff_id=%s AND session_id=%s AND message_id=%s""",
            (S["staff_id"], S["session_id"], anchor[0]))["rule_tags"]
        check("同一条消息可同时承载多条规则（只增不减）",
              set(tags3) == {"positive_coder_profile", "positive_clear_work_need"}, str(tags3))
        check("标签按字典序稳定排序", tags3 == sorted(tags3), str(tags3))

        # ── 4. ④ 取数 + V6 有效性 ──────────────────────────────────────────
        print("\n--- 4. ④ 关键历史取数（§4.4.3 / V6）---")
        ctx = ss.build_context(S["staff_id"], S["session_id"], rel_key=REL, session_meta=meta)
        st = ctx["stats"]
        check("key_source = tags", st.get("key_source") == "tags", str(st.get("key_source")))
        check("ctx_key_rules 非空（V6）", bool(st.get("key_rules")), str(st.get("key_rules")))
        check("命中的维度与写入一致（V6）",
              set(st["key_rules"]) == {"positive_coder_profile", "positive_clear_work_need"},
              str(st["key_rules"]))
        check("④ 段条数落在 1~30（V6 上限 = ctx_key_history_max）",
              1 <= st["key"] <= 30, str(st["key"]))
        check("锚点全部在 index 里（证据可回链）",
              all(m in ctx["index"].values() for m in anchor),
              str([m for m in anchor if m not in ctx["index"].values()]))

        # ── 5. pinned 抗窗口：关键历史不会被 60 条窗口切掉 ──────────────────
        print("\n--- 5. pinned 抗窗口（C2 证据回链的前提）---")
        plain = ss.build_context(S["staff_id"], S["session_id"], rel_key=REL,
                                 session_meta=meta, key_history=False)
        check("不带关键历史时，最早的锚点不在上下文（证明它们确被窗口挤掉）",
              ids[0] not in plain["index"].values(),
              f"kept={plain['stats']['kept']}")
        check("带关键历史后，kept 增加（= 窗口 + 锚点）",
              st["kept"] > plain["stats"]["kept"],
              f"{st['kept']} vs {plain['stats']['kept']}")
        check("锚点编号为 #001 起（时间升序 + 统一编号）",
              ctx["index"]["#001"] == ids[0], str(ctx["index"].get("#001")))

        # ── 6. 统一编号：唯一且连续（§4.5.3 C2）───────────────────────────
        print("\n--- 6. 统一编号唯一连续 ---")
        codes = [l.split(" ")[0] for l in ctx["lines"]]
        check("编号与 index 数量一致", len(codes) == len(ctx["index"]), f"{len(codes)}/{len(ctx['index'])}")
        check("编号无重复", len(set(codes)) == len(codes))
        check("编号从 #001 连续到 #N",
              codes == ["#%03d" % i for i in range(1, len(codes) + 1)], str(codes[:3] + codes[-2:]))

        # ── 7. V3 确定性 ───────────────────────────────────────────────────
        print("\n--- 7. V3 确定性 ---")
        again = ss.build_context(S["staff_id"], S["session_id"], rel_key=REL, session_meta=meta)
        check("两次调用（去时间标签后）行序列一致",
              _strip_time(ctx["lines"]) == _strip_time(again["lines"]))
        check("两次调用 index 一致", ctx["index"] == again["index"])

        # ── 8. 两阶段取数：低频规则不会被高频规则挤掉 ───────────────────────
        print("\n--- 8. 两阶段取数（每 rule 保底 1 条）---")
        _clear_tags(S)
        # 规则 A 撒 25 条（高频），规则 B 只 1 条（低频，最早）
        ss.service._tag_evidence_messages(
            S["staff_id"], S["session_id"],
            [{"rule_id": "positive_cooperative", "evidence_message_ids": ids[10:35]}], [])
        ss.service._tag_evidence_messages(
            S["staff_id"], S["session_id"],
            [{"rule_id": "positive_ask_course", "evidence_message_ids": ids[:1]}], [])
        ctx2 = ss.build_context(S["staff_id"], S["session_id"], rel_key=REL, session_meta=meta)
        kr = set(ctx2["stats"].get("key_rules") or [])
        check("低频规则也进了 ④ 段（保底 1 条）", "positive_ask_course" in kr, str(sorted(kr)))
        check("高频规则同样在场", "positive_cooperative" in kr, str(sorted(kr)))
        check("总量仍不超上限", ctx2["stats"]["key"] <= 30, str(ctx2["stats"]["key"]))
        check("同一条消息命中多规则时合并成一行（条数 ≤ 消息数）",
              ctx2["stats"]["key"] <= 26, str(ctx2["stats"]["key"]))

        # ── 9. 标签写入失败不阻断评分 ───────────────────────────────────────
        print("\n--- 9. 标签失败不阻断（§4.4.1(3)）---")
        orig_tx = ss.db.tx
        ss.service._progress = {"tool_failures": 0}
        try:
            def boom():
                raise RuntimeError("模拟 DB 故障")
            ss.db.tx = boom
            n = ss.service._tag_evidence_messages(
                S["staff_id"], S["session_id"],
                [{"rule_id": "positive_coder_profile", "evidence_message_ids": ids[:2]}], [])
            check("返回 0 而不是抛异常", n == 0, str(n))
            check("已记 tool_failures 留痕", ss.service._progress.get("tool_failures") == 1,
                  str(ss.service._progress.get("tool_failures")))
        except Exception as e:
            check("标签失败不阻断（异常穿透）", False, repr(e))
        finally:
            ss.db.tx = orig_tx

        # ── 10. 关掉开关即回到旧行为（可回滚）────────────────────────────
        print("\n--- 10. ctx_key_history 开关（可回滚）---")
        db.set_setting("ctx_key_history", "false")
        try:
            off = ss.build_context(S["staff_id"], S["session_id"], rel_key=REL, session_meta=meta)
            check("开关=false → 无关键历史", off["stats"]["key"] == 0, str(off["stats"]["key"]))
            check("开关=false → key_source 为空", off["stats"].get("key_source") is None,
                  str(off["stats"].get("key_source")))
        finally:
            db.execute("DELETE FROM app_setting WHERE key = 'ctx_key_history'")

        # ── 11. 图路径同样写标签（n_tag 节点闭环）───────────────────────────
        print("\n--- 11. 图路径写标签（graph_session.n_tag）---")
        import analyze                 # noqa: E402
        import graph_session           # noqa: E402

        _clear_tags(S)
        run_id = 994000001
        ev = ids[:3]

        def stub(conversation, facts, rules, **kw):
            return {"positive_items": [{
                        "rule_id": "positive_coder_profile",
                        "dimension": "客户画像", "polarity": "positive", "score": 2,
                        "reason": "stub", "evidence": [], "evidence_ids": ["#001"],
                        "evidence_message_ids": ev}],
                    "negative_items": [], "invalid_items": [], "unchecked_rules": [],
                    "profile_summary": "", "sales_strategy": "", "model_confidence": 0.5,
                    "rule_version": rules.get("rule_version"), "prompt_version": "t",
                    "schema_version": "1", "contract_attempts": 1}

        orig = analyze.analyze
        analyze.analyze = stub
        try:
            r = graph_session.run_session(ss.service, run_id, S, ss.rules_mod.load_rules(),
                                          8, "off", False)
            check("图路径返回 scored", r == "scored", r)
            tagged = db.query(
                """SELECT rule_tags FROM chat_message
                    WHERE staff_id=%s AND session_id=%s AND message_id = ANY(%s)""",
                (S["staff_id"], S["session_id"], ev))
            check("图路径已把证据写进 rule_tags",
                  len(tagged) == len(ev) and all(t["rule_tags"] == ["positive_coder_profile"]
                                                 for t in tagged),
                  str([t["rule_tags"] for t in tagged]))
        finally:
            analyze.analyze = orig
            for tbl in ("writeback_outbox", "negative_review", "score_writeback"):
                db.execute(f"""DELETE FROM {tbl} WHERE assessment_id IN
                               (SELECT assessment_id FROM customer_intent_assessment
                                 WHERE source_run_id = %s)""", (run_id,))
            db.execute("DELETE FROM customer_intent_assessment WHERE source_run_id = %s",
                       (run_id,))
            # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
            try: ss.repair_current_flags()
            except Exception: pass
            _clear_tags(S)

    finally:
        _clear_tags(S)
        left = db.query_one("SELECT count(*) AS n FROM chat_message WHERE cardinality(rule_tags) > 0")
        print(f"\n清理完成，全库带标签行 = {left['n']}")

    print(f"\n===== {_TOTAL - _FAILED}/{_TOTAL} 通过 =====")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
