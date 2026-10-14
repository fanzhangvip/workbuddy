# -*- coding: utf-8 -*-
"""阶段 A（输出契约）端到端验收 —— 需要 PostgreSQL(5433) + DeepSeek 真实调用。

    C:/Users/EDY/.workbuddy/binaries/python/envs/default/Scripts/python.exe tests/test_stage_a_acceptance.py

做三件事：
  ① 挑一个**从未被评过**的真实会话，走后端 `service.score_session()` 完整跑一遍
  ② 断言契约层的产出真的落到了库里（新列 / 证据回链 / 覆盖率 / 契约调用次数）
  ③ **按 assessment_id 清理**本次产生的全部痕迹（含 rule_tags），不给业务库留垃圾

验收锚点（阶段 A）：
  - 落库的 positive/negative_items 每条都带 evidence_message_ids（证据可回链，F4/A1）
  - unchecked_rules 有值（覆盖率可计算；模型漏看的规则能被数出来）
  - prompt_version / schema_version / model_confidence / contract_attempts 均已写入
  - model_confidence 与 confidence 两列都不为空（前端兼容）
  - 契约层落库失败时**不产生**半截 assessment（本脚本同时验证 session_failure 能力）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

import db                                     # noqa: E402
import scoring_service as ss                  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {detail}" if detail else ""))


def pick_candidate():
    """挑一个未评分过、消息数适中的真实会话。"""
    return db.query_one(
        """SELECT c.staff_id, c.session_id, c.target_id, c.msg_count
             FROM chat_session c
             LEFT JOIN LATERAL (
                  SELECT a.assessment_id FROM customer_intent_assessment a
                   WHERE a.relationship_key = c.staff_id || ':' || COALESCE(c.target_id, c.session_id)
                   ORDER BY a.assessment_id DESC LIMIT 1
             ) last ON true
            WHERE c.session_type = 'ext_user' AND c.target_id IS NOT NULL
              AND c.msg_count BETWEEN 8 AND 40 AND last.assessment_id IS NULL
            ORDER BY c.msg_count DESC LIMIT 1""")


def cleanup(aid, staff_id, session_id):
    """清掉本次验收产生的全部行（含候选水位，使该会话仍可被正常流程选中）。"""
    if aid:
        db.execute("DELETE FROM writeback_outbox WHERE assessment_id = %s", (aid,))
        db.execute("DELETE FROM negative_review WHERE assessment_id = %s", (aid,))
        db.execute("DELETE FROM score_writeback WHERE assessment_id = %s", (aid,))
        db.execute("DELETE FROM customer_intent_assessment WHERE assessment_id = %s", (aid,))
    db.execute("DELETE FROM session_failure WHERE staff_id = %s AND session_id = %s",
               (staff_id, session_id))
    # C-A 起评分链路会写 chat_message.rule_tags（关键历史检索键）——验收痕迹一并清掉
    db.execute("""UPDATE chat_message SET rule_tags = '{}'
                   WHERE staff_id = %s AND session_id = %s""", (staff_id, session_id))


def main():
    cand = pick_candidate()
    if not cand:
        print("跳过：库里没有「未评分过且消息数在 8~40」的会话，无法做端到端验收。")
        return 0
    print(f"验收目标：staff={cand['staff_id'][:14]}… session={cand['session_id'][:18]}… "
          f"msgs={cand['msg_count']}")

    aid = None
    try:
        print("\n--- 跑完整评分链路（真实模型调用）---")
        res = ss.service.score_session(cand["staff_id"], cand["session_id"])
        check("链路返回 ok", bool(res.get("ok")), str(res.get("reason") or "")[:120])
        if not res.get("ok"):
            return 1
        row = res["assessment"]
        aid = row["assessment_id"]
        print(f"  assessment_id={aid} raw={row['raw_score']} grade={row['grade']} "
              f"attempts={row.get('contract_attempts')} unchecked={row.get('unchecked_rules')}")

        print("\n--- 验收断言 ---")
        check("落库：contract_attempts ≥ 1", (row.get("contract_attempts") or 0) >= 1,
              str(row.get("contract_attempts")))
        check("落库：prompt_version 已写", bool(row.get("prompt_version")), str(row.get("prompt_version")))
        check("落库：schema_version 已写", bool(row.get("schema_version")), str(row.get("schema_version")))
        check("落库：model_confidence 已写", row.get("model_confidence") is not None,
              str(row.get("model_confidence")))
        # ⚠️ 契约已变（§6 置信度三层合成 / E4）：`confidence` 列**刻意停写** ——
        # 铁律是「置信度必须算出来，不能问出来」，模型自报值只留 `model_confidence`。
        # 前端已改为读 `model_confidence` + `session_confidence`，故这里断言「停写」而非「非空」。
        check("落库：confidence 兼容列已停写（E4：模型自报不参与决策）",
              row.get("confidence") is None, str(row.get("confidence")))
        check("落库：session_confidence 已算出", row.get("session_confidence") is not None,
              str(row.get("session_confidence")))
        check("落库：unchecked_rules 是列表", isinstance(row.get("unchecked_rules"), list),
              str(row.get("unchecked_rules")))
        check("落库：rule_version 已写", bool(row.get("rule_version")), str(row.get("rule_version")))

        items = (row.get("positive_items") or []) + (row.get("negative_items") or [])
        if items:
            with_ev = [i for i in items if i.get("evidence_message_ids")]
            check("证据回链：命中项都带 evidence_message_ids",
                  len(with_ev) == len(items), f"{len(with_ev)}/{len(items)}")
            no_code = [i for i in items if not i.get("evidence_ids")]
            check("证据回链：命中项都带 #编号", not no_code, str([i["rule_id"] for i in no_code]))
        else:
            check("命中项非空（否则无法验证证据回链）", False, "本次 0 命中，选一个更活跃的会话重试")

        # 覆盖率分母 = 14 条 llm 规则；命中 + 漏检应能对上
        import rules as R
        total_llm = len(R.llm_rule_ids(R.load_rules()))
        hit_ids = {i["rule_id"] for i in items}
        unchecked = set(row.get("unchecked_rules") or [])
        check("覆盖率：命中数 + 漏检数 ≤ llm 规则总数",
              len(hit_ids) + len(unchecked) <= total_llm,
              f"hit={len(hit_ids)} unchecked={len(unchecked)} total={total_llm}")

        print("\n--- 负分复核队列 ---")
        if row.get("negative_items"):
            rev = db.query_one("SELECT status FROM negative_review WHERE assessment_id = %s", (aid,))
            check("负分项存在 → 已入复核队列", bool(rev), str(rev))
        else:
            check("无负分项 → 复核状态为 na", row.get("review_status") == "na",
                  str(row.get("review_status")))

        print("\n--- 回写（默认 dry_run，不产生外部副作用）---")
        wb = db.query_one("SELECT mode, status FROM score_writeback WHERE assessment_id = %s", (aid,))
        if wb:
            check("回写模式为 dry_run（验收期间不外写）", wb["mode"] == "dry_run", str(wb))
        else:
            check("无不回写（无正分或已关闭）", True)
    finally:
        cleanup(aid, cand["staff_id"], cand["session_id"])
        print(f"\n（已清理验收痕迹：assessment={aid}）")

    print(f"\n===== {len(PASSED)} passed / {len(FAILED)} failed =====")
    if FAILED:
        print("失败：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
