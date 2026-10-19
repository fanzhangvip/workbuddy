# -*- coding: utf-8 -*-
"""记忆治理层（`customer_memory`）：归并规则 / 人工改判 / 过期 / 上下文渲染（t1）。

    .venv\\Scripts\\python.exe tests\\test_memory.py

## 守的是什么

记忆会影响**之后每一次判定**的上下文，所以它的错法都很贵：把销售话术记成客户事实、
把"这次没提"当成"这事不成立"、模型把人工改过的值覆盖回去、过期预算继续进提示词。
本文件把这几条钉住；另外顺带钉住"记忆不是证据"（不带编号、不进 `index`）。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import sys

from _harness import check, summary  # noqa: E402

import db              # noqa: E402
import memory_repo     # noqa: E402
import task_runtime    # noqa: E402

STAFF = "ZMEM:staff"
TARGET = "ZMEM:u1"
REL = f"{STAFF}:{TARGET}"


def _wipe():
    db.execute("DELETE FROM customer_memory WHERE relationship_key = %s", (REL,))
    db.execute("DELETE FROM customer_intent_assessment WHERE relationship_key = %s", (REL,))
    db.execute("DELETE FROM task WHERE kind = 'memory-test'")


def _slot(slot, value, ev=("m-1",)):
    return {"slot": slot, "slot_cn": slot, "value": value,
            "evidence_message_ids": list(ev), "evidence": [f"#001 客户: {value}"]}


def _apply(struct, **kw):
    with db.tx() as conn:
        return memory_repo.apply_in(conn, rel=REL, profile_struct=struct,
                                    assessment_id=kw.pop("assessment_id", 1), **kw)


def main():
    _wipe()
    saved_ttl = db.query_one("SELECT value FROM app_setting WHERE key='memory_ttl_days'")
    saved_src = db.query_one("SELECT value FROM app_setting WHERE key='ctx_memory_source'")
    try:
        # ---------------- ① 归一：只用来判"是不是同一件事" ----------------
        check("全角/空白/尾标点归一后相等",
              memory_repo.norm("  Ｊａｖａ 后端。") == memory_repo.norm("Java 后端"),
              memory_repo.norm("  Ｊａｖａ 后端。"))
        check("不同值不会被误判成同一件事",
              memory_repo.norm("杭州") != memory_repo.norm("上海"))

        # ---------------- ② 首次归并：写入 active，跳过"未提及" ----------------
        r = _apply([_slot("city", "成都"), _slot("position", "Java 后端"),
                    _slot("budget", "未提及")])
        rows = memory_repo.active_rows(REL)
        by_slot = {x["slot"]: x for x in rows}
        check("首次写入：只写有值的槽位（未提及不落库）",
              set(by_slot) == {"city", "position"} and r["written"] == 2, f"{r} {list(by_slot)}")
        check("记忆带证据原话与 message_id（不是 #编号）",
              by_slot["city"]["evidence_message_ids"] == ["m-1"]
              and "客户: 成都" in (by_slot["city"]["evidence_text"] or ""),
              str(by_slot["city"]))
        check("按固定槽位顺序返回（岗位在前、城市在后）",
              [x["slot"] for x in rows] == ["position", "city"], str([x["slot"] for x in rows]))

        # ---------------- ③ 同一值再次出现：只刷新，不新增 ----------------
        r2 = _apply([_slot("city", "成都", ev=("m-2",))], assessment_id=2)
        rows = memory_repo.active_rows(REL)
        city = next(x for x in rows if x["slot"] == "city")
        check("同值重复 → 不新增行（updated）", r2["updated"] == 1 and len(rows) == 2, str(r2))
        check("同值重复 → 证据合并、去重",
              city["evidence_message_ids"] == ["m-1", "m-2"],
              str(city["evidence_message_ids"]))

        # ---------------- ④ 值变了：旧行 superseded，新行 version+1 ----------------
        r3 = _apply([_slot("city", "上海")], assessment_id=3)
        rows = memory_repo.active_rows(REL)
        city = next(x for x in rows if x["slot"] == "city")
        hist = memory_repo.list_rows(REL, include_history=True)
        old = [x for x in hist if x["slot"] == "city" and x["status"] == "superseded"]
        check("新值覆盖：active 变成上海、version=2",
              city["value"] == "上海" and city["version"] == 2, str(city))
        check("旧值留痕（superseded，可追溯客户搬过家）",
              len(old) == 1 and old[0]["value"] == "成都", str(old))
        check("r3 记了一次写入", r3["written"] == 1, str(r3))

        # ---------------- ⑤ "未提及"不删记忆 ----------------
        _apply([_slot("city", "未提及")], assessment_id=4)
        city = next(x for x in memory_repo.active_rows(REL) if x["slot"] == "city")
        check("★ 本次未提及 ≠ 事实不成立（记忆保留）", city["value"] == "上海", str(city))

        # ---------------- ⑥ 人工值不被模型覆盖 ----------------
        m = memory_repo.set_manual(rel=REL, slot="city", value="杭州", note="客户自己确认过")
        check("人工改判成功且 source=human", m.get("ok") and m.get("value") == "杭州", str(m))
        _apply([_slot("city", "苏州")], assessment_id=5)
        city = next(x for x in memory_repo.active_rows(REL) if x["slot"] == "city")
        check("★ 模型不能覆盖人工确认的值（只刷新 last_seen）",
              city["value"] == "杭州" and city["source"] == "human", str(city))
        check("人工值的前一条（上海）保留为历史",
              any(x["slot"] == "city" and x["value"] == "上海" and x["status"] == "superseded"
                  for x in memory_repo.list_rows(REL, include_history=True)))

        # ---------------- ⑦ 拒绝：回退到上一条历史值 ----------------
        mid = city["memory_id"]
        rej = memory_repo.reject(memory_id=mid)
        city2 = next(x for x in memory_repo.active_rows(REL) if x["slot"] == "city")
        check("★ 拒绝一条记忆 → 回退到上一条（不是直接删掉）",
              rej.get("ok") and rej.get("restored") and city2["value"] == "上海",
              f"{rej} -> {city2['value']}")

        # ---------------- ⑧ 过期：不进上下文 ----------------
        db.execute("""UPDATE customer_memory SET expires_at = now() - interval '1 day'
                       WHERE relationship_key=%s AND slot='city'""", (REL,))
        check("过期记忆被惰性过滤（读取侧不返回）",
              all(x["slot"] != "city" for x in memory_repo.active_rows(REL)),
              str([x["slot"] for x in memory_repo.active_rows(REL)]))
        check("expire_due 把到期行标成 expired（清账）",
              memory_repo.expire_due() >= 1
              and all(x["status"] != "active" for x in
                      memory_repo.list_rows(REL, include_history=True) if x["slot"] == "city"))

        # ---------------- ⑨ 上下文渲染：记忆不是证据 ----------------
        # 先把 city 恢复成有效（上一段刚把它过期掉），否则渲染不出来
        db.execute("""UPDATE customer_memory SET status='active',
                        expires_at = now() + interval '180 days'
                       WHERE relationship_key=%s AND slot='city' AND value='上海'""", (REL,))
        db.execute("""INSERT INTO customer_intent_assessment
                        (relationship_key, staff_id, raw_score, positive_total, negative_total,
                         grade, positive_items, negative_items, profile_summary, profile_struct)
                      VALUES (%s,%s,5,5,0,'B','[]'::jsonb,'[]'::jsonb,%s,%s::jsonb)""",
                   (REL, STAFF, "客户在成都，做 Java。",
                    db.as_json([{"slot": "city", "value": "未提及"}])))
        db.set_setting("ctx_memory_source", "table")
        from scoring_context import build_context
        # ⚠️ 必须走 `delta=True`（评分链）：`delta=False` 是"只给最近 N 条"的展示壳，
        # 它在装配记忆之前就返回了（兴趣链用它，且**不该**看到记忆）。
        ctx = build_context(STAFF, "ZMEM:s1", limit=30, delta=True,
                            session_meta={"staff_id": STAFF, "session_id": "ZMEM:s1"},
                            rel_key=REL)
        text = ctx.get("text") or ""
        check("① 段出现「已知事实」且带槽位", "已知事实" in text and "城市" in text
              and "上海" in text,
              text[:300])
        check("标注格式干净（不出现「（；」这种拼接残渣）", "（；" not in text, text[:300])
        check("★ 最近一次评估说「未提及」→ 标注出来（事实仍在）",
              "上次会话未提及" in text, text[:400])
        check("★ 记忆不带 #编号、不进 index（模型不能把它当证据）",
              "#001" not in text.split("【上次评分明细】")[0]
              and (ctx.get("index") or {}) == {},
              f"{text[:160]} | {ctx.get('index')}")
        check("画像是补充、不是替代（摘要仍在）", "上次画像摘要" in text, "")

        # ---------------- ⑩ 任务成本计数 ----------------
        tid = task_runtime.create(kind="memory-test", trigger="test")["task_id"]
        task_runtime.record_cost(tid, calls=1, tokens=100)
        task_runtime.record_cost(tid, calls=1, tokens=50)
        row = db.query_one("SELECT cost_calls, cost_tokens FROM task WHERE task_id=%s", (tid,))
        check("★ 任务成本真实累加（此前这两列永远是 0）",
              row["cost_calls"] == 2 and row["cost_tokens"] == 150, str(row))
    finally:
        for key, saved in (("memory_ttl_days", saved_ttl), ("ctx_memory_source", saved_src)):
            if saved:
                db.set_setting(key, saved["value"])
            else:
                db.execute("DELETE FROM app_setting WHERE key=%s", (key,))
        _wipe()
        print(f"\n[清理] 残留：{db.query_one('SELECT count(*) AS n FROM customer_memory WHERE relationship_key=%s', (REL,))['n']}")
    return summary()


if __name__ == "__main__":
    sys.exit(main())
