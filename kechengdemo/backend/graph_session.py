# -*- coding: utf-8 -*-
"""单会话评分流程的 LangGraph 图化（阶段B）。

设计原则
    **节点是旧 ``_score_one`` 的逐字搬运，不夹带任何"顺手优化"。**
    口径漂移的防线是 ``tests/test_graph_session_parity.py``：新旧路径跑同一批
    会话，``raw_score / grade / positive_total / negative_total`` 差异必须为 0。

图结构
    ``prepare_context`` ──(kept < ctx_min_kept)──► END        （早退，不计 LLM 调用）
            │
            └─► emit_ctx_stats → facts → judge → items → local_score
                 → persist(assessment + outbox 同事务) → tag_evidence(rule_tags)
                 → writeback → review → progress → END

为什么用「闭包注入 service」而不是把它放进 state
    ``ScoringService`` 持有 ``threading.Lock``；若放进 state，一旦框架对 state 做
    ``deepcopy``（不同版本行为不同）就会直接 ``TypeError``。用闭包彻底规避。

为什么所有节点都是同步的、且不配 RetryPolicy
    ``llm.chat`` 用 ``urllib``、``db`` 用 psycopg 同步池，天然同步；换成 async
    只会徒增一层 ``to_thread``。重试有三层且职责已清晰（契约修复内循环 /
    ``_llm_analyze`` 的 ``RetryableLLMError`` 退避 / outbox 退避），再加节点级
    RetryPolicy 会造成重复计费与有副作用节点的危险重放。
"""
from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

import assessment_repo          # ② 段记忆要读「上次评估」（该表唯一读入口）
import confidence               # 阈值 + 三层合成（不再经由 scoring_service 转发）
import db
import hooks
import llm
import rules as rules_mod
import scoring_context          # 事实 / 上下文（评分输入的中立模块）
import trace

NODE_PREPARE = "prepare_context"
NODE_CTX_STATS = "emit_ctx_stats"
NODE_FACTS = "facts"
NODE_JUDGE = "judge"
NODE_ITEMS = "items"
NODE_LOCAL = "local_score"
NODE_PERSIST = "persist"
NODE_TAG = "tag_evidence"
NODE_WRITEBACK = "writeback"
NODE_REVIEW = "review"
NODE_PROGRESS = "progress"


class SessionState(TypedDict, total=False):
    """图状态：只放**普通可序列化值**（不放 service 实例，见模块 docstring）。"""
    # 外层注入
    run_id: int
    cand: dict
    rules: dict
    min_messages: int
    mode: str
    fetch_tags: bool

    # 节点间传递
    ctx: dict
    facts: dict
    res: dict
    pos_items: list
    neg_items: list
    unchecked: list
    score: dict
    grade: dict
    needs_review: bool
    session_confidence: float
    review_reasons: list
    aid: int
    wb_status: str
    review_enqueued: bool

    # 返回语义
    outcome: str            # "scored" / "skipped"


# ============================================================ 节点实现
# 统一签名 (svc, state) -> dict；svc 由闭包注入。

def n_prepare(svc, state: SessionState) -> dict:
    """取消息 → 预处理层规范化 → 判定「有效条数是否够」。对应旧 L513-520。"""
    cand = state["cand"]
    staff_id, session_id = cand["staff_id"], cand["session_id"]
    ctx = scoring_context.build_context(
        staff_id, session_id,
        session_meta={"staff_id": staff_id, "session_id": session_id},
        rel_key=f"{staff_id}:{cand['target_id']}")
    # ① before_llm_call（架构 §2.4 H3）：与旧路径同一判定入口，保证口径一致。
    # B4 三口径（保留条数 / 客户开口数 / 文本长度）与旧路径**必须完全一致**，
    # 否则 `test_graph_session_parity` 会立刻暴露差异。
    _st = ctx["stats"]
    gate = hooks.before_llm_call(
        kept=_st["kept"],
        min_kept=db.setting_int("ctx_min_kept", "CTX_MIN_KEPT", 4),
        customer_msgs=_st.get("customer_msgs"),
        min_customer_msgs=db.setting_int("ctx_min_customer_msgs", "CTX_MIN_CUSTOMER_MSGS", 1),
        text_len=_st.get("text_len"),
        min_effective_chars=db.setting_int("ctx_min_effective_chars",
                                           "CTX_MIN_EFFECTIVE_CHARS", 0))
    if gate.denied:
        svc._note_hook(gate)
        return {"ctx": ctx, "outcome": "skipped"}
    return {"ctx": ctx}


def route_after_prepare(state: SessionState) -> str:
    return "skip" if state.get("outcome") == "skipped" else "go"


def n_ctx_stats(svc, state: SessionState) -> dict:
    """累计预处理观测指标（仅内存）。对应旧 L521。"""
    svc._bump_ctx(state["ctx"]["stats"])
    return {}


def n_facts(svc, state: SessionState) -> dict:
    """规则引擎事实（48h 未回复等）。对应旧 L523。"""
    cand = state["cand"]
    facts = scoring_context.compute_facts(cand["staff_id"], cand["session_id"],
                                          fetch_tags=state["fetch_tags"])
    svc._note_facts(facts)          # 修 G1：取标签失败留痕（与旧路径同口径）
    return {"facts": facts}


def n_judge(svc, state: SessionState) -> dict:
    """大模型判定。对应旧 L524-525（llm_calls 在调用前 +1，失败也算一次调用）。"""
    svc._bump(state["run_id"], llm_calls=1)
    res = svc._llm_analyze(state["ctx"], state["facts"]["text"], state["rules"])
    # 与串行路径 `_score_one` 同口径：模型自报置信度被夹取也要计数（否则两条路径
    # 的观测量会漂 —— `test_graph_session_parity` 正是在盯这类不一致）
    if res.get("confidence_clamped"):
        svc._inc(confidence_clamped=1)
    return {"res": res}


def n_items(svc, state: SessionState) -> dict:
    """拆分正/负项 + 叠加规则引擎硬指标。对应旧 L527-532。"""
    res, rules, facts = state["res"], state["rules"], state["facts"]
    # engine=rule 的规则已在 rules.derive_items 里排除，这里再兜一层（防御契约回退）
    engine_ids = rules_mod.engine_rule_ids(rules)
    neg = [i for i in (res.get("negative_items") or []) if i.get("rule_id") not in engine_ids]
    neg += rules_mod.engine_negative_items(facts, rules)  # negative_no_reply_2d 等硬指标
    # P2：锚点随 `res` 带去落库（与串行路径 `_score_one` 同口径 —— 两条路径必须一致，
    # 否则 `test_graph_session_parity.py` 会漂）
    res["data_as_of"] = facts.get("as_of")
    return {"pos_items": res.get("positive_items") or [], "neg_items": neg}


def n_local_score(svc, state: SessionState) -> dict:
    """本地计分 + **代码算出的置信度与复核触发**。对应旧 L534-540。"""
    score = rules_mod.compute_score(state["pos_items"], state["neg_items"], state["rules"])
    grade = rules_mod.grade_of(score["raw_score"], state["rules"])
    unchecked = list(state["res"].get("unchecked_rules") or [])
    # ② after_llm_output（架构 §2.4 H3）：与旧路径同一判定入口（体检 + 极性终检）
    gate = hooks.after_llm_output(pos_items=state["pos_items"], neg_items=state["neg_items"],
                                  unchecked=unchecked,
                                  invalid_items=state["res"].get("invalid_items"),
                                  rules=state["rules"],
                                  total_llm=len(rules_mod.llm_rule_ids(state["rules"])))
    svc._note_hook(gate)
    if gate.denied:
        raise llm.FatalLLMError(
            f"命中项极性异常（规则表与输出不符）：{gate.detail.get('polarity_violations')}")
    # 置信度三层合成（架构 第六章 E4）：与旧路径同一函数、同一阈值，口径必然一致
    cand = state["cand"]
    last_grade = (assessment_repo.last_assessment(
        f"{cand['staff_id']}:{cand['target_id']}") or {}).get("grade")
    # 传**合并后**的 pos/neg_items（含引擎硬指标）；理由见 scoring_service._score_one 的注释
    conf = confidence.compute(state["pos_items"], state["neg_items"], state["res"],
                              score, grade, state["rules"], last_grade=last_grade,
                              thresholds=confidence.thresholds())
    n_pos = len(state["pos_items"])
    conf_items = conf.get("items") or []
    return {"score": score, "grade": grade, "unchecked": unchecked,
            "needs_review": conf["needs_human_review"],
            # 命中项带上 item_confidence / confidence_kind（落库留痕，供复核参考）
            "pos_items": conf_items[:n_pos], "neg_items": conf_items[n_pos:],
            "session_confidence": conf["session_confidence"],
            "review_reasons": conf["review_reasons"]}


def n_persist(svc, state: SessionState) -> dict:
    """落库（assessment + outbox 同事务）。对应旧 L542-566。"""
    aid = svc._persist_assessment(state["run_id"], state["cand"], state["res"],
                                  state["score"], state["grade"], state["needs_review"],
                                  state["unchecked"], state["pos_items"], state["neg_items"],
                                  state["mode"],
                                  session_confidence=state.get("session_confidence"),
                                  review_reasons=state.get("review_reasons"))
    return {"aid": aid}


def n_tag(svc, state: SessionState) -> dict:
    """写关键历史标签（架构 §4.4.1）。对应旧路径 persist 之后、回写之前。"""
    cand = state["cand"]
    svc._tag_evidence_messages(cand["staff_id"], cand["session_id"],
                               state["pos_items"], state["neg_items"])
    return {}


def n_writeback(svc, state: SessionState) -> dict:
    """回写微伴。对应旧 L568-570。"""
    cand = state["cand"]
    wb = svc._writeback(state["run_id"], f"{cand['staff_id']}:{cand['target_id']}",
                        state["aid"], cand["staff_id"], cand["target_id"],
                        state["score"]["positive_total"], state["mode"],
                        bool(state["neg_items"]))
    return {"wb_status": wb}


def n_review(svc, state: SessionState) -> dict:
    """负分进人工复核队列。对应旧 L572-574。"""
    done = svc._enqueue_review(state["aid"], state["cand"], state["neg_items"],
                               state["score"]["raw_score"], state["grade"]["grade"],
                               state["wb_status"])
    return {"review_enqueued": bool(done)}


def n_progress(svc, state: SessionState) -> dict:
    """进度自增（仅内存）。对应旧 L576-584。"""
    wb, need = state["wb_status"], state["review_enqueued"]
    svc._inc(assessment_new=1,
             writeback_ok=1 if wb in ("ok", "dry_run") else 0,
             writeback_fail=1 if wb == "failed" else 0,
             review_new=1 if need else 0)
    return {"outcome": "scored"}


# ============================================================ 装配

def _node(name: str, svc, fn):
    """把 (svc, state) 适配成 LangGraph 的 (state)->dict，并套上追踪包装。"""
    def bound(state, _fn=fn, _svc=svc):
        return _fn(_svc, state)
    return trace.traced(name)(bound)


def build_graph(svc):
    """编译单会话图（无 checkpointer：本阶段不做节点级断点续跑，见方案风险 7）。"""
    g = StateGraph(SessionState)
    g.add_node(NODE_PREPARE, _node(NODE_PREPARE, svc, n_prepare))
    g.add_node(NODE_CTX_STATS, _node(NODE_CTX_STATS, svc, n_ctx_stats))
    g.add_node(NODE_FACTS, _node(NODE_FACTS, svc, n_facts))
    g.add_node(NODE_JUDGE, _node(NODE_JUDGE, svc, n_judge))
    g.add_node(NODE_ITEMS, _node(NODE_ITEMS, svc, n_items))
    g.add_node(NODE_LOCAL, _node(NODE_LOCAL, svc, n_local_score))
    g.add_node(NODE_PERSIST, _node(NODE_PERSIST, svc, n_persist))
    g.add_node(NODE_TAG, _node(NODE_TAG, svc, n_tag))
    g.add_node(NODE_WRITEBACK, _node(NODE_WRITEBACK, svc, n_writeback))
    g.add_node(NODE_REVIEW, _node(NODE_REVIEW, svc, n_review))
    g.add_node(NODE_PROGRESS, _node(NODE_PROGRESS, svc, n_progress))

    g.add_edge(START, NODE_PREPARE)
    g.add_conditional_edges(NODE_PREPARE, route_after_prepare,
                            {"skip": END, "go": NODE_CTX_STATS})
    g.add_edge(NODE_CTX_STATS, NODE_FACTS)
    g.add_edge(NODE_FACTS, NODE_JUDGE)
    g.add_edge(NODE_JUDGE, NODE_ITEMS)
    g.add_edge(NODE_ITEMS, NODE_LOCAL)
    g.add_edge(NODE_LOCAL, NODE_PERSIST)
    g.add_edge(NODE_PERSIST, NODE_TAG)
    g.add_edge(NODE_TAG, NODE_WRITEBACK)
    g.add_edge(NODE_WRITEBACK, NODE_REVIEW)
    g.add_edge(NODE_REVIEW, NODE_PROGRESS)
    g.add_edge(NODE_PROGRESS, END)
    return g.compile()


_GRAPHS = {}


def graph_for(svc):
    """按 service 实例缓存编译结果（单例场景下只会编译一次）。"""
    key = id(svc)
    g = _GRAPHS.get(key)
    if g is None:
        g = build_graph(svc)
        _GRAPHS[key] = g
    return g


def run_session(svc, run_id, cand, rules, min_messages, mode, fetch_tags) -> str:
    """同步执行单会话图。

    返回 ``"scored"`` / ``"skipped"``（与旧 ``_score_one`` 的返回值逐字一致）；
    异常（``FatalLLMError`` / ``ContractViolationError`` / 重试耗尽后的
    ``RuntimeError`` / DB 异常）**原样穿透** ``invoke()``，交由 ``_run`` 的
    ``except Exception`` 走既有失败分支（``_record_failure``）。
    """
    init = {
        "run_id": run_id,
        "cand": cand,
        "rules": rules,
        "min_messages": min_messages,
        "mode": mode,
        "fetch_tags": fetch_tags,
    }
    final = graph_for(svc).invoke(init, config={"recursion_limit": 25})
    return final.get("outcome", "scored")
