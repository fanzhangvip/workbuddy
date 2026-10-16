# -*- coding: utf-8 -*-
"""引擎规则的时间窗口 + 评估集时间锚点 —— 需要 PostgreSQL 5433。

    .venv\\Scripts\\python.exe tests\\test_engine_window_and_anchor.py

（方案里原本列了 `test_engine_no_reply_window` 与 `test_eval_anchor_immutable` 两个文件，
合并成一个：两者守的是**同一条缝** —— "48h 未回复"这个引擎项必须由**锚点**决定，
既不随日历自己翻转，也不无限期成立。）

## 守的四件事

1. **窗口边界**：47.9h 不命中、48h 命中、14 天命中、14 天+1h 不命中；
   `no_reply_window_days=0` = 不设上限（退回改造前语义）。
2. **锚点固定则结论固定**：同一 `as_of` 跑两次完全一致。
3. **`run_case` 真的用了样本的锚点**（不是 `now()`）：造一条"锚点时刻 gap 只有 10 天、
   今天 gap 已 200 天"的会话 —— 如果代码错用 `now()`，引擎项就会消失，测试立刻红。
4. **锚点落库**：`assessment.data_as_of` 与 `compute_facts` 返回的 `as_of` 是同一个值
   （不是落库时又取了一次 `time.time()`）。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import sys
import time

from _harness import check, summary  # noqa: E402

import analyze           # noqa: E402
import db                # noqa: E402
import eval_case_repo as repo   # noqa: E402
import eval_runner       # noqa: E402
import scoring_context as sctx  # noqa: E402

STAFF, SESS, TARGET = "ANCHOR:staff", "ANCHOR:sess", "ANCHOR:target"
KIND = "__anchor_test__"
DAY = 86400


def _wipe():
    db.execute("DELETE FROM eval_case WHERE kind=%s", (KIND,))
    db.execute("DELETE FROM chat_message WHERE staff_id=%s", (STAFF,))
    db.execute("DELETE FROM chat_session WHERE staff_id=%s", (STAFF,))


def _mk_session(last_offset_days: int, n=6):
    """造一条合成会话：最后一条消息在 `last_offset_days` 天前（相对 now()）。"""
    _wipe()
    db.execute("""INSERT INTO chat_session (staff_id, session_id, session_type, target_id,
                                            name, msg_count, last_msg_time)
                  VALUES (%s,%s,'ext_user',%s,'anchor-test',%s,%s)""",
               (STAFF, SESS, TARGET, n, int(time.time())))
    base = int(time.time()) - last_offset_days * DAY - 3600
    for i in range(n):
        db.execute("""INSERT INTO chat_message (staff_id, session_id, message_id, msgid,
                                                direction, msg_time, msg_type, content,
                                                content_state)
                      VALUES (%s,%s,%s,%s,%s,%s,'text',%s,'ok')""",
                   (STAFF, SESS, f"m{i}", f"mw{i}", "send" if i % 2 else "recv",
                    base + i * 600, f"第 {i} 条消息"))


def main():
    rules = eval_runner.rules_mod.load_rules()
    orig_analyze = analyze.analyze
    try:
        # ---------------- ① 窗口边界（受 14 天窗口约束） ----------------
        _mk_session(0)                       # 最后一条消息 = 刚刚
        last = db.query_one("""SELECT msg_time FROM chat_message
                                WHERE staff_id=%s ORDER BY msg_time DESC LIMIT 1""", (STAFF,))
        t = int(last["msg_time"])
        cases = [
            ("47.9 小时 → 不命中", t + int(47.9 * 3600), False),
            ("48.0 小时 → 命中", t + 48 * 3600, True),
            ("7 天 → 命中", t + 7 * DAY, True),
            ("14 天 → 命中", t + 14 * DAY, True),
            ("14 天 + 1 小时 → 不命中（超窗）", t + 14 * DAY + 3600, False),
            ("200 天 → 不命中（超窗）", t + 200 * DAY, False),
        ]
        for name, as_of, want in cases:
            f = sctx.compute_facts(STAFF, SESS, as_of=as_of)
            check(f"窗口边界：{name}", f["no_reply_48h"] is want, str(f["no_reply_48h"]))
        f0 = sctx.compute_facts(STAFF, SESS, as_of=t + 200 * DAY, window_days=0)
        check("窗口边界：window_days=0 → 不设上限（退回改造前语义）",
              f0["no_reply_48h"] is True, str(f0["no_reply_48h"]))

        # ---------------- ② 锚点固定 → 结论固定 ----------------
        a = sctx.compute_facts(STAFF, SESS, as_of=t + 5 * DAY)
        b = sctx.compute_facts(STAFF, SESS, as_of=t + 5 * DAY)
        check("同一锚点跑两次，结论完全一致",
              a["no_reply_48h"] == b["no_reply_48h"] and a["hours"] == b["hours"],
              f"{a['no_reply_48h']}/{a['hours']} vs {b['no_reply_48h']}/{b['hours']}")
        check("返回值里带锚点（供落库与追溯）", a.get("as_of") == t + 5 * DAY,
              str(a.get("as_of")))

        # ---------------- ③ run_case 真的用样本锚点，而不是 now() ----------------
        # 造「锚点时刻 gap = 10 天（窗内）」，而**今天** gap 已 ~200 天：
        _mk_session(200)
        last2 = db.query_one("""SELECT msg_time FROM chat_message
                                 WHERE staff_id=%s ORDER BY msg_time DESC LIMIT 1""", (STAFF,))
        anchor = int(last2["msg_time"]) + 10 * DAY          # 锚点：窗内
        cid = repo.insert_sample(
            kind=KIND, staff_id=STAFF, session_id=SESS, target_id=TARGET,
            staff_name="t", target_name="t", session_name="t", messages_hash="h",
            msg_count=6, last_msg_time=int(last2["msg_time"]),
            baseline_items=db.as_json([]), anchor_ts=anchor)
        case = db.query_one("SELECT * FROM eval_case WHERE case_id=%s", (cid,))
        analyze.analyze = lambda *a, **kw: {
            "positive_items": [], "negative_items": [], "invalid_items": [],
            "unchecked_rules": [], "profile_summary": "", "sales_strategy": "",
            "model_confidence": 0.5, "rule_version": rules.get("rule_version"),
            "prompt_version": "anchor-test", "schema_version": "1", "contract_attempts": 1}
        got = eval_runner.run_case(case, rules)
        eng = [i for i in got["items"] if i["rule_id"] == "negative_no_reply_2d"]
        check("run_case 用样本锚点判定引擎项（锚点内 → 命中；若错用 now() 会消失）",
              len(eng) == 1 and eng[0]["score"] == -2, str(got["items"])[:200])
        check("锚点确实写进返回（后续由落库侧使用）",
              case.get("anchor_ts") == anchor, str(case.get("anchor_ts")))

        # 反向：把锚点挪到窗外（200 天）→ 同一个会话不再命中
        db.execute("UPDATE eval_case SET anchor_ts=%s WHERE case_id=%s",
                   (int(last2["msg_time"]) + 200 * DAY, cid))
        case2 = db.query_one("SELECT * FROM eval_case WHERE case_id=%s", (cid,))
        got2 = eval_runner.run_case(case2, rules)
        check("锚点挪到窗外 → 同一会话不再命中（证明判定跟着锚点走）",
              not [i for i in got2["items"] if i["rule_id"] == "negative_no_reply_2d"],
              str(got2["items"])[:200])
    finally:
        analyze.analyze = orig_analyze
        _wipe()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
