# -*- coding: utf-8 -*-
"""测试公共骨架 —— 路径引导 / 输出编码 / `check()` / 汇总。

## 为什么要有它（`AGENTS.md` 的 Reuse Before Rewrite）

改造前 31 个测试文件里：`sys.path` 注入 **34 处**、`ROOT` 推导 **32 份**、
stdout 编码 **28 份**、`check()` 定义 **30 份**（归一化后其实只有 3 种写法）。
一处样板要改就得动 30 个文件 —— 那等于「没人会去改它」。

## 用法

    from _harness import ROOT, check, summary

    check("失败不阻断主流程", got is None, got)      # 断言 + 打印

    def main():
        ...
        return summary()                              # 打印汇总并给出退出码

    if __name__ == "__main__":
        sys.exit(main())

## 约定（与改造前一致，别改）

  · `check(name, cond, detail)` —— 通过打印 ` PASS  name`，失败打印 ` FAIL  name   detail`
    并把用例名记进 `FAILED`；
  · `PASSED` / `FAILED` 是**模块级列表**：测试文件 `import` 走的是同一对象，
    所以老的汇总写法（`len(PASSED)`、`if FAILED:`）不用改也照常工作；
  · 判定只信**退出码**（`run_all.py` 同样只看退出码，不看输出字样）。
"""
import os
import sys

#: 仓库根目录（`tests/` 的上一层）。测试里读文件请用它，不要各自再推导一遍。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 统一输出编码（原 P2-2）：默认 GBK 控制台下，断言名里的 ↔ → ★ 会触发
# UnicodeEncodeError —— 看起来像测试失败，其实是环境问题。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8")

import db                                          # noqa: E402  夹具清理要用（见下）

#: 通过 / 失败的用例名。名字刻意保留 `PASSED`/`FAILED` —— 改造前 19 个文件
#: 的汇总代码就引用它们，这样那些文件**除了样板以外一个字都不用动**。
PASSED: list = []
FAILED: list = []


def check(name, cond, detail="", extra=None):
    """断言并打印一行结果。`detail` / `extra` 只在失败时打印（两个名字都收，
    因为改造前两种写法都有）。返回布尔值，便于 `if check(...):` 这种用法。"""
    text = detail if detail else (extra or "")
    if cond:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"   {text}" if text else ""))
    return bool(cond)


def section(title: str) -> None:
    """分段标题（原先各文件自己写 `print("\\n--- xx ---")`）。"""
    print(f"\n--- {title} ---")


def summary() -> int:
    """打印汇总（含失败清单）并返回**退出码**：0 = 全通过。"""
    total = len(PASSED) + len(FAILED)
    print(f"\n{'=' * 66}")
    print(f"通过 {len(PASSED)} / 失败 {len(FAILED)}（共 {total}）")
    if FAILED:
        print("失败清单：" + "、".join(FAILED))
    print("=" * 66)
    return 1 if FAILED else 0


# ---------------- 测试夹具：清理 ----------------

#: 允许被 :func:`delete_rows` 删行的表（表名要拼进 SQL，故用白名单）。
#: 新增表时加到这里 —— 这是**测试**的清理闸，不是生产逻辑。
CLEANABLE_TABLES = frozenset({
    "app_setting", "audit_log", "chat_message", "customer_intent_assessment",
    "eval_case", "eval_run", "grade_tag_ledger", "grade_tag_outbox",
    "negative_review", "policy_decision", "request_guard", "score_writeback",
    "scoring_run", "session_failure", "sync_job", "task", "task_step", "login_session",
    "tool_audit", "trace_span", "writeback_ledger", "writeback_outbox",
})


def delete_rows(table: str, **conds) -> int:
    """按**等值条件**删行，返回删除行数。测试清理专用。

    为什么要有它：测试里 `db.execute("DELETE FROM t WHERE c = %s", (v,))` 这种样板
    有上百处（`rg -c "DELETE FROM" tests`）。手写 SQL 的三个坑一次避开：

      · 单元素元组写成 `(v)` 就变成「传了个裸值」，SQL 里少占位符的写法很隐蔽；
      · `WHERE` 条件写漏一个就是**整表清空**（这些测试跑在真实库上）；
      · 表名拼错要到运行期才发现。

    用法::

        delete_rows("session_failure", staff_id="B234TEST")
        delete_rows("customer_intent_assessment", relationship_key=_REL)

    :raises ValueError: 表名不在白名单，或**一个条件都没给**（防整表清空）。
    """
    if table not in CLEANABLE_TABLES:
        raise ValueError(f"不在可清理白名单里的表：{table}")
    if not conds:
        raise ValueError("delete_rows 必须至少给一个条件（避免清空整表）")
    where = " AND ".join(f"{k} = %s" for k in conds)
    return db.execute(f"DELETE FROM {table} WHERE {where}", tuple(conds.values()))


def pick_session_for_test(*, min_messages: int = 8, require_unscored: bool = True,
                          ensure_pool: int = 0) -> dict:
    """挑一个「测试前提一定成立」的会话：消息数够，且**客户至少说过 1 句**。

    为什么要有它（同一个坑踩过两次）：`scoring_service._candidates` 按优先级取前 N，
    而其中不少会话**客户一句话都没说**（销售单方面刷屏）→ 被 B4 闸门
    （`ctx_min_customer_msgs`）拦下 → `_score_one` 返回 `skipped` →
    断言「确实评了会话」失败，**看起来像功能坏了**，实际是挑样本碰运气。

    所以：① 用 `session_id` 作次序键（结果确定，`msg_count` 并列时不再抖动）；
    ② `EXISTS(... direction='receive')` **让前提主动成立**。

    :param require_unscored: 是否还要求该关系键**从未评过分**（G6 那条链路需要；
        只做「批量路径对比」的测试不需要，传 False 可选面更大）。
    :param ensure_pool: >0 时改为先挑**名下至少有这么多条合规会话**的员工，再取该员工
        的一条会话。给「同一批候选要跑两遍」的测试用（第一遍评完后，第二遍仍要有
        未评分的候补，否则第二遍全 `skipped`，计数对不上会看着像路径不一致）。
    """
    unscored = "" if not require_unscored else (
        " AND NOT EXISTS (SELECT 1 FROM customer_intent_assessment a"
        "                  WHERE a.relationship_key = c.staff_id || ':' || c.target_id)")
    params = [int(min_messages)]
    staff_clause = ""
    if ensure_pool:
        pool = db.query_one(
            """SELECT c.staff_id, count(*) AS n
                 FROM chat_session c
                WHERE c.session_type = 'ext_user' AND c.target_id IS NOT NULL
                  AND c.msg_count >= %s
                  AND EXISTS (SELECT 1 FROM chat_message m
                               WHERE m.session_id = c.session_id AND m.direction = 'receive')
                GROUP BY c.staff_id HAVING count(*) >= %s
                ORDER BY count(*) DESC, c.staff_id LIMIT 1""",
            (int(min_messages), int(ensure_pool)))
        if not pool:
            return None
        staff_clause = " AND c.staff_id = %s"
        params.append(pool["staff_id"])
    return db.query_one(
        f"""SELECT c.staff_id, s.name AS staff_name, c.session_id, c.target_id,
                   c.name AS target_name, c.msg_count, c.last_msg_time
              FROM chat_session c LEFT JOIN staff s ON s.staff_id = c.staff_id
             WHERE c.session_type = 'ext_user' AND c.target_id IS NOT NULL
               AND c.msg_count >= %s
               AND EXISTS (SELECT 1 FROM chat_message m
                            WHERE m.session_id = c.session_id AND m.direction = 'receive')
               {staff_clause}
               {unscored}
             ORDER BY c.msg_count DESC, c.session_id LIMIT 1""",
        tuple(params))


def purge_run(run_id: int) -> None:
    """删掉某个 run 的**公共产物**：回写 outbox / 回写记录 / 复核 / 评估 / run 行。

    改造前这段「按 `source_run_id` 级联清理 + `repair_current_flags()` 自愈」在
    5 个测试文件里各写一份（相似度 0.84~0.95），差别只是「要不要顺手删
    `session_failure` / `trace_span` / `audit_log` / `writeback_ledger`」——
    按 `AGENTS.md` 的口径：**保留共用部分，差异留给调用方**自己补一两行。

    顺序要紧：先删引用方（outbox / 回写 / 复核），再删 `customer_intent_assessment`，
    最后删 `scoring_run` —— 反过来会留下悬空引用。
    """
    import scoring_service as ss                      # 延迟导入：避免收集期就建连接池

    for tbl in ("writeback_outbox", "score_writeback", "negative_review"):
        db.execute(
            f"DELETE FROM {tbl} WHERE assessment_id IN "
            "(SELECT assessment_id FROM customer_intent_assessment WHERE source_run_id=%s)",
            (run_id,))
    db.execute("DELETE FROM customer_intent_assessment WHERE source_run_id=%s", (run_id,))
    # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
    try:
        ss.repair_current_flags()
    except Exception:                                  # noqa: BLE001
        pass
    db.execute("DELETE FROM scoring_run WHERE id=%s", (run_id,))
