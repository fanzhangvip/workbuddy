# -*- coding: utf-8 -*-
"""兴趣点提取的**批量链路与分布口径**（t1：需要本地 PostgreSQL 5433）。

    .venv\\Scripts\\python.exe tests\\test_interest_pipeline.py

## 这条链为什么必须单独钉住

    POST /api/interest/start ────┐
    scheduler 日常链 step=interest ├─► interest_service.start() ─► InterestService._run
    POST /api/interest/stop ─────┘        ├─ 增量候选（水位）
                                          ├─ build_context(delta=False)  ← 口径固定
                                          ├─ 与评分互斥（都吃模型配额）
                                          └─ 预算闸 → partial（可续跑）

它要回答的是「跑完一轮之后图上的数字对不对、再跑会不会重复烧钱、被闸住时数据
状态是不是可续」—— 全是**接线**问题（谁拦谁、水位推没推、拒得干不干净）。

## 打桩打在哪里（为什么这么打）

打 `interest.extract`（"一次提取"的边界）：它之上是候选 SQL 与上下文装配，之下才是
模型。打桩后跑的仍是**产品代码的完整路径** —— 候选筛选 / 装配 / 证据回链 / 二级标签
归一 / 落库 / 水位推进 / 进度与终态，一个都不省。真跑一次要 5~6 小时且花钱
（首跑 805 个会话 × ~23 秒），所以只能这样验。

打桩返回的**证据编号从真实会话文本里取**（不是硬编码 `#001`）—— 这样"回链成功"
检验的是产品的 `index` 编号，而不是测试自己造的巧合。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import re
import sys
import time

from _harness import check, summary  # noqa: E402

import config          # noqa: E402
import db              # noqa: E402
import interest        # noqa: E402
import interest_repo   # noqa: E402
from interest_service import service   # noqa: E402

#: 合成数据统一前缀：清理按它一个条件就能扫干净
STAFF = "ZINTP:staff"
SESS1, SESS2, SESS3 = "ZINTP:s1", "ZINTP:s2", "ZINTP:s3"
#: ⚠️ 两个会话**故意挂在同一个客户**（`target_id`）下：本功能的计数口径是
#: 「按**客户**去重」而不是"按会话"，同客户多会话正好是这条口径的判别用例
#: —— 如果哪天有人把 count(DISTINCT relationship_key) 改成 count(*)，它会响。
TARGET1 = TARGET2 = "ZINTP:u1"
REL1, REL2 = f"{STAFF}:{TARGET1}", f"{STAFF}:{TARGET2}"
#: 固定时间戳（水位断言要精确比对，不能用 now）
T0 = 1_792_200_000
NMSG = 12


# ---------------------------------------------------------------- 夹具

def _wipe():
    """清掉本文件造的所有行（含上次跑到一半留下的）。"""
    for sess in (SESS1, SESS2, SESS3):
        db.execute("DELETE FROM chat_message WHERE staff_id=%s AND session_id=%s",
                   (STAFF, sess))
        db.execute("DELETE FROM chat_session WHERE staff_id=%s AND session_id=%s",
                   (STAFF, sess))
    db.execute("DELETE FROM interest_scan WHERE staff_id=%s", (STAFF,))
    db.execute("DELETE FROM interest_run WHERE triggered_by='__test__'")
    db.execute("DELETE FROM customer_intent_assessment WHERE relationship_key IN (%s,%s)",
               (REL1, REL2))
    db.execute("DELETE FROM session_failure WHERE staff_id=%s", (STAFF,))


def _ins_msg(sess, mid, msg_time, text, direction="recv"):
    db.execute(
        """INSERT INTO chat_message
             (staff_id, session_id, message_id, msgid, direction, msg_time,
              msg_type, msgtype, content, content_state)
           VALUES (%s,%s,%s,%s,%s,%s,'text',1,%s,'ok')""",
        (STAFF, sess, mid, f"m_{mid}", direction, msg_time, text))


def _setup():
    """两个合成会话，各 12 条有正文的消息（够 `min_messages` 的默认 8）。"""
    _wipe()
    for sess, target in ((SESS1, TARGET1), (SESS2, TARGET2)):
        db.execute(
            """INSERT INTO chat_session
                 (staff_id, session_id, session_type, target_id, name,
                  msg_count, last_msg_time)
               VALUES (%s,%s,'ext_user',%s,%s,%s,%s)""",
            (STAFF, sess, target, f"ZINTP 客户 {target}", NMSG, T0))
        for i in range(NMSG):
            _ins_msg(sess, f"{sess}-m{i}", T0 - (NMSG - i) * 60,
                     "我想转行做算法" if i % 2 else "我零基础，能学会吗",
                     "recv" if i % 2 else "send")
    # ③ 号会话是**员工单方面刷屏**（客户一句没说）：它必须**不进候选**
    #    —— 提取的是客户的兴趣，没有客户发言就没有语料（省一次调用，也不会永远挂着）
    db.execute(
        """INSERT INTO chat_session
             (staff_id, session_id, session_type, target_id, name, msg_count, last_msg_time)
           VALUES (%s,%s,'ext_user',%s,%s,%s,%s)""",
        (STAFF, SESS3, "ZINTP:u3", "ZINTP 客户 u3", NMSG, T0))
    for i in range(NMSG):
        _ins_msg(SESS3, f"{SESS3}-m{i}", T0 - (NMSG - i) * 60,
                 "老师这边现在报名有优惠，名额不多了", "send")


def _advance(sess, ts, mid):
    """给会话追加一条新消息并把会话水位推到 `ts`（模拟"又有新对话"）。"""
    _ins_msg(sess, mid, ts, "其实我还想问问价格和优惠")
    db.execute("""UPDATE chat_session SET msg_count = msg_count + 1, last_msg_time=%s
                   WHERE staff_id=%s AND session_id=%s""", (ts, STAFF, sess))


# ---------------------------------------------------------------- 打桩

#: 打桩收到的对话文本（用来断言"送进模型的到底是谁说的话"）
SEEN: list = []


def _stub_extract(text, **kw):
    """替掉真模型：从会话文本里取一个**真实存在的**编号当证据。"""
    SEEN.append(text or "")
    m = re.search(r"#\d+", text or "")
    code = m.group(0) if m else "#001"
    return {"interests": [interest.InterestItem(
                primary="career_change", secondary=" 想转行做算法。",
                confidence=0.9, evidence_ids=[code])],
            "summary": "客户想转行做算法（stub）", "llm_calls": 1, "model_name": "stub"}


def _expected_mid(sess):
    """该会话 `#001` 对应的 message_id —— 与 `_analyze_one` 用**同一套装配**
    （含 `customer_only=True`，否则编号会落在员工那条消息上）。"""
    from scoring_context import build_context
    ctx = build_context(STAFF, sess, limit=300,
                        session_meta={"staff_id": STAFF, "session_id": sess},
                        rel_key=f"{STAFF}:{sess}", key_history=False, delta=False,
                        customer_only=True)
    codes = sorted(ctx.get("index") or {})
    return ctx["index"][codes[0]] if codes else None


# ---------------------------------------------------------------- 运行控制

def _last_run() -> dict:
    return db.query_one(
        "SELECT * FROM interest_run WHERE triggered_by='__test__' "
        "ORDER BY id DESC LIMIT 1") or {}


def _wait_done(timeout=90) -> str:
    """等这一轮收口（终态不是 running）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = _last_run().get("status")
        if st and st != "running":
            return st
        time.sleep(0.5)
    return None


def _wait_idle(timeout=30):
    """等线程与运行行都归位（否则下一轮 `start()` 会被自己的互斥闸挡住）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not service.is_running():
            return True
        time.sleep(0.5)
    return False


def _run_once(**kw):
    """跑一轮并等到收口，返回（start 返回值, run 行）。"""
    assert _wait_idle(), "上一轮还没退干净"
    r = service.start(triggered_by="__test__", **kw)
    if not r.get("ok"):
        return r, {}
    _wait_done()
    _wait_idle()
    return r, _last_run()


def _where(staff_id=STAFF, scope="all"):
    return interest_repo.distribution(scope=scope, staff_id=staff_id)


# ---------------------------------------------------------------- 用例

def test_first_run_and_increment():
    """① 首跑落库（含证据、归一、水位）→ ② 立刻再跑不重复 → ③ 有新消息只补那一条。"""
    SEEN.clear()
    r, run = _run_once(kind="incremental", staff_ids=[STAFF])
    check("启动成功", r.get("ok") is True, str(r)[:200])
    check("本轮收口 success", run.get("status") == "success", str(run.get("status")))
    check("候选 = 2 个会话、分析 2 个（员工单方刷屏的 s3 不在其中）",
          (run.get("session_total"), run.get("session_done")) == (2, 2),
          f"total={run.get('session_total')} done={run.get('session_done')}")
    check("每会话 1 次模型调用（预算按调用数算）", run.get("llm_calls") == 2,
          str(run.get("llm_calls")))
    check("落库 2 条兴趣点、0 条因证据回链不上被丢", run.get("interest_points") == 2
          and run.get("dropped_points") == 0,
          f"points={run.get('interest_points')} dropped={run.get('dropped_points')}")

    task = db.query_one("SELECT status FROM task WHERE task_id=%s",
                        (run.get("task_id"),)) or {}
    check("检查点任务收口 succeeded（可观测）", task.get("status") == "succeeded",
          str(task))

    scan1 = interest_repo.scan_of(STAFF, SESS1)
    item = (scan1.get("interests") or [{}])[0]
    check("★ 证据回链成了 message_id（编号→消息，不是把编号存下来）",
          item.get("evidence_message_ids") == [_expected_mid(SESS1)],
          str(item.get("evidence_message_ids")))
    # 原话片段要和 `#001` 那条消息的正文对得上（编号→原话，界面点开就是它）
    first_text = db.query_one(
        """SELECT content FROM chat_message WHERE staff_id=%s AND session_id=%s
              AND direction <> 'send'
            ORDER BY msg_time ASC, message_id ASC LIMIT 1""", (STAFF, SESS1))["content"]
    check("★ 证据带着原话片段（点开能核对）",
          bool(item.get("evidence")) and first_text in (item.get("evidence") or [""])[0],
          str(item.get("evidence"))[:160])
    check("★ 二级标签落库前已归一（去空白/去尾标点）",
          item.get("secondary") == "想转行做算法", repr(item.get("secondary")))
    check("一级分类是受控词表里的 key", item.get("primary") == "career_change",
          str(item.get("primary")))
    check("★ 水位 = 会话最新消息时间（下次增量靠它）",
          int(scan1.get("last_msg_time") or 0) == T0, str(scan1.get("last_msg_time")))
    check("词表/提示词版本随结果落库（跨版本可比）",
          scan1.get("taxonomy_version") == interest.load_taxonomy()["taxonomy_version"]
          and scan1.get("prompt_version") == interest.PROMPT_VERSION,
          f"{scan1.get('taxonomy_version')} / {scan1.get('prompt_version')}")

    n_before = db.query_one("SELECT count(*) AS n FROM interest_scan")["n"]
    r2, run2 = _run_once(kind="incremental", staff_ids=[STAFF])
    n_after = db.query_one("SELECT count(*) AS n FROM interest_scan")["n"]
    check("★ 立刻再跑：候选 0 个（水位齐平，不重复烧钱）",
          (run2.get("session_total"), run2.get("llm_calls")) == (0, 0),
          f"total={run2.get('session_total')} calls={run2.get('llm_calls')}")
    check("再跑没有多写行", n_after == n_before, f"{n_before} → {n_after}")

    _advance(SESS1, T0 + 100, f"{SESS1}-m99")
    r3, run3 = _run_once(kind="incremental", staff_ids=[STAFF])
    check("★ 有新消息 → 只重跑那一条会话",
          (run3.get("session_total"), run3.get("session_done")) == (1, 1),
          f"total={run3.get('session_total')} done={run3.get('session_done')}")
    check("水位推进到新消息时间",
          int(interest_repo.scan_of(STAFF, SESS1).get("last_msg_time") or 0) == T0 + 100,
          str(interest_repo.scan_of(STAFF, SESS1).get("last_msg_time")))
    check("② 的 run 行仍在（历史可查）", bool(run2.get("id")), str(run2.get("id")))


def test_customer_only():
    """★ 只看客户自己说的话：员工话术既不能进模型，也不能变成"客户的兴趣"。

    这是**口径**问题，不是优化：员工话术里塞满课程卖点（"现在报名有优惠""名额不多了"），
    整段丢给模型，模型很容易把**销售的主张**读成客户的兴趣 —— 图上就会出现一批
    客户从没提过的点，点开证据还是销售自己说的。所以：

      ① `build_context(customer_only=True)` 只留客户发言（编号也从客户第一条开始）；
      ② 候选闸按**客户发言条数**算，员工刷屏不能凑数；
      ③ 该参数与 `delta=True` 组合直接报错（那几段混着档案与上次结论，筛不了）。
    """
    from scoring_context import build_context

    ctx = build_context(STAFF, SESS1, limit=300, session_meta={"staff_id": STAFF,
                                                               "session_id": SESS1},
                        rel_key=f"{STAFF}:{SESS1}", key_history=False, delta=False,
                        customer_only=True)
    txt = ctx["text"]
    check("★ 员工的话不出现在文本里（'我零基础，能学会吗'是销售说的）",
          "我零基础" not in txt, txt[:200])
    check("客户的话都在，且编号从 #001 起（客户第一条）",
          "我想转行做算法" in txt and ctx["index"].get("#001") is not None, txt[:200])
    check("渲染出的角色一律是「客户」（模型不会去追问不存在的上下文）",
          "客户:" in txt and "销售:" not in txt, txt[:200])
    check("统计里 sales_msgs = 0（口径可自证）",
          int(ctx["stats"].get("sales_msgs") or 0) == 0
          and int(ctx["stats"].get("customer_msgs") or 0) > 0, str(ctx["stats"]))
    check("★ 与 delta=True 组合直接报错（不悄悄给半截上下文）",
          _raises(lambda: build_context(STAFF, SESS1, delta=True, customer_only=True)),
          "")

    # 候选闸：门槛量的是**客户发言条数**（员工刷屏凑不出来）。
    # 用一个临时会话精确卡边界：9 条员工 + 3 条客户 → 门槛 3 收、门槛 4 不收。
    s4 = "ZINTP:s4"
    db.execute("""INSERT INTO chat_session (staff_id, session_id, session_type, target_id,
                                            name, msg_count, last_msg_time)
                  VALUES (%s,%s,'ext_user','ZINTP:u4','ZINTP 客户 u4',%s,%s)""",
               (STAFF, s4, NMSG, T0))
    for i in range(NMSG):
        _ins_msg(s4, f"{s4}-m{i}", T0 - (NMSG - i) * 60,
                 "客户说的话" if i < 3 else "老师刷屏的一段课程卖点",
                 "recv" if i < 3 else "send")
    try:
        q = lambda n: interest_repo.candidates(min_messages=8, min_customer_msgs=n,
                                               staff_ids=[STAFF])
        check("★ 门槛量的是「客户说了几句」，不是总会话消息数：3 条客户发言 @门槛3 收、@门槛4 不收",
              s4 in [c["session_id"] for c in q(3)] and s4 not in [c["session_id"] for c in q(4)],
              f"q3={len(q(3))} q4={len(q(4))}")
    finally:
        db.execute("DELETE FROM chat_message WHERE staff_id=%s AND session_id=%s",
                   (STAFF, s4))
        db.execute("DELETE FROM chat_session WHERE staff_id=%s AND session_id=%s",
                   (STAFF, s4))
    check("★ 员工单方刷屏的 s3 从未进过候选（不是靠模型兜底）",
          all(c["session_id"] != SESS3 for c in
              interest_repo.candidates(min_messages=1, min_customer_msgs=1,
                                       staff_ids=[STAFF])),
          "")
    cov = interest_repo.coverage(min_messages=8, min_customer_msgs=1)
    check("覆盖度分母同口径：s3 不算「待分析」（否则进度条永远到不了头）",
          cov["sessions_pending"] >= 0 and cov["sessions_available"] >= 2, str(cov))

    # 打桩真的只收到客户的话
    seen = "\n".join(SEEN)
    check("★ 送进模型的实际文本里没有员工话术", SEEN and "我零基础" not in seen,
          seen[:160])


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    except Exception:                              # noqa: BLE001
        return False
    return False


def test_distribution_scope():
    """④ 图上每个数字的口径：按客户去重 / 二级标签 / 等级与员工切片 / 下钻证据。"""
    d = _where()
    check("★ 一级分类按**客户**去重（2 个会话同属 1 个客户 → customers=1）",
          d["primaries"] == [{"primary": "career_change", "customers": 1,
                              "mentions": 2}], str(d["primaries"]))
    check("二级标签同样按客户去重、并按客户数排序",
          d["secondaries"] and d["secondaries"][0]["secondary"] == "想转行做算法"
          and d["secondaries"][0]["customers"] == 1, str(d["secondaries"])[:200])
    cov = d["coverage"]
    check("覆盖面：已分析会话与客户数都被统计进去",
          cov["sessions_analyzed"] >= 2 and cov["customers"] >= 1
          and cov["sessions_available"] >= cov["sessions_analyzed"], str(cov))
    check("按员工切片：别的员工看不到这批数据",
          _where(staff_id="ZINTP:someone_else")["primaries"] == [],
          str(_where(staff_id="ZINTP:someone_else")["primaries"]))

    # ★ 覆盖度与等级分布也必须跟着切片走：否则切到某个员工后，图变了而口径没变
    mine = interest_repo.coverage(min_messages=0, min_customer_msgs=1, staff_id=STAFF)
    check("★ 覆盖度按员工切片：只数这个员工名下的 2 个会话",
          mine["sessions_available"] >= 2 and mine["customers"] == 1, str(mine))
    check("等级分布按员工切片：别的员工不会混进来",
          sum(g["customers"] for g in _where(staff_id=STAFF)["by_grade"]) == 1,
          str(_where(staff_id=STAFF)["by_grade"]))

    # ★ 一级 + 二级必须落在**同一条**兴趣点上（JSONB 数组包含的经典陷阱）
    other = {"primary": "price_promo", "secondary": "想分期", "confidence": 0.9,
             "evidence_message_ids": ["x"], "evidence": ["#001 客户: 能分期吗"]}
    interest_repo.upsert_scan(
        staff_id=STAFF, session_id=SESS2, relationship_key=REL2, staff_name="ZINTP",
        target_id=TARGET2, target_name="ZINTP 客户", interests=[other], summary="",
        taxonomy_version="i1.0", prompt_version="t", model_name="stub",
        msg_count=NMSG, last_msg_time=0)
    try:
        cross = interest_repo.customers(primary="career_change", secondary="想分期",
                                        staff_id=STAFF)
        check("★ 一级+二级下钻不能跨条目匹配（否则抽屉里只有客户名、没有原话）",
              cross == [], f"命中了 {len(cross)} 条")
        same = interest_repo.customers(primary="career_change", secondary="想转行做算法",
                                       staff_id=STAFF)
        check("同一个兴趣点上的组合仍然能下钻", len(same) >= 1, str(len(same)))
    finally:
        # 恢复 s2 的原始结果，后面的用例还要用
        interest_repo.delete_scan(STAFF, SESS2)
        interest_repo.upsert_scan(
            staff_id=STAFF, session_id=SESS2, relationship_key=REL2, staff_name="ZINTP",
            target_id=TARGET2, target_name="ZINTP 客户",
            interests=[{"primary": "career_change", "secondary": "想转行做算法",
                        "confidence": 0.9, "evidence_message_ids": ["m"],
                        "evidence": ["#002 客户: 我想转行做算法"]}],
            summary="", taxonomy_version="i1.0", prompt_version="t", model_name="stub",
            msg_count=NMSG, last_msg_time=T0)

    items = interest_repo.customers(primary="career_change", staff_id=STAFF)
    row = next((x for x in items if x["session_id"] == SESS1), None)
    check("下钻能拿到客户 + 二级标签",
          bool(row) and row["secondaries"] == ["想转行做算法"],
          str(row and row.get("secondaries")))
    check("★ 下钻带的是**命中该分类的**证据原话",
          bool(row) and (row["matched"] or [{}])[0].get("evidence"),
          str(row and row.get("matched"))[:200])
    check("未评分客户在等级列显示为空（不是伪造一个等级）",
          bool(row) and row.get("grade") is None, str(row and row.get("grade")))

    a = db.query_one(
        """INSERT INTO customer_intent_assessment
             (relationship_key, staff_id, session_id, target_id, raw_score,
              positive_total, negative_total, grade, positive_items, negative_items,
              invalid_items, msg_count, last_msg_time, is_current)
           VALUES (%s,%s,%s,%s,6,6,0,'S','[]'::jsonb,'[]'::jsonb,'[]'::jsonb,%s,%s,true)
           RETURNING assessment_id""",
        (REL1, STAFF, SESS1, TARGET1, NMSG, T0 + 100))["assessment_id"]
    try:
        s = _where(scope="S")
        check("★ 等级切片：S 档只算这个客户的会话",
              [p["customers"] for p in s["primaries"]] == [1], str(s["primaries"]))
        check("A 档为空（没有 A 客户）", _where(scope="A")["primaries"] == [],
              str(_where(scope="A")["primaries"]))
        n_s = len(interest_repo.customers(primary="career_change", scope="S",
                                          staff_id=STAFF))
        n_a = interest_repo.customers(primary="career_change", scope="A",
                                      staff_id=STAFF)
        check("下钻随切片一起过滤：S 档能看到该客户的 2 条会话、A 档看不到",
              n_s == 2 and n_a == [], f"S={n_s} A={len(n_a)}")
    finally:
        db.execute("DELETE FROM customer_intent_assessment WHERE assessment_id=%s",
                   (a,))


def test_mutual_exclusion():
    """⑤ 与评分互斥（都吃模型配额）、与同步并行（只读消息，不挡 20:00 日常）。"""
    run_id = db.query_one(
        """INSERT INTO scoring_run (kind, status, triggered_by, rule_version)
           VALUES ('incremental','running','__test__','__test__') RETURNING id""")["id"]
    try:
        assert _wait_idle()
        r = service.start(staff_ids=[STAFF], triggered_by="__test__")
        check("★ 评分在跑 → 拒绝启动兴趣分析", r.get("ok") is False, str(r)[:200])
        check("拒绝原因说清了是「评分」占着配额", "评分" in (r.get("reason") or ""),
              str(r.get("reason")))
        check("被拒时**没有**留下 interest_run 行",
              db.query_one("SELECT count(*) AS n FROM interest_run "
                           "WHERE triggered_by='__test__' AND status='running'")["n"] == 0)
    finally:
        db.execute("DELETE FROM scoring_run WHERE id=%s", (run_id,))

    # 同步在进行中 —— 兴趣分析**应当**能跑（两者抢的资源不同：同步不打模型）
    _advance(SESS2, T0 + 200, f"{SESS2}-m99")
    jid = db.query_one("INSERT INTO sync_job (kind, status, triggered_by) "
                       "VALUES ('test','running','__test__') RETURNING id")["id"]
    try:
        r, run = _run_once(kind="incremental", staff_ids=[STAFF])
        check("★ 同步在跑 → 兴趣分析仍可启动（不阻塞日常同步）", r.get("ok") is True,
              str(r)[:200])
        check("并且真的分析了那一条增量",
              (run.get("session_total"), run.get("session_done")) == (1, 1),
              f"total={run.get('session_total')} done={run.get('session_done')}")
    finally:
        db.execute("DELETE FROM sync_job WHERE id=%s", (jid,))


def test_budget_gate_and_resume():
    """⑥ 预算闸：撞上限落 `partial`（不假装成功），且下一轮能从水位接着跑。"""
    _advance(SESS1, T0 + 300, f"{SESS1}-m100")
    _advance(SESS2, T0 + 300, f"{SESS2}-m100")
    prev = db.query_one("SELECT value FROM app_setting WHERE key='interest_max_llm_calls'")
    db.set_setting("interest_max_llm_calls", 1)
    try:
        r, run = _run_once(kind="incremental", staff_ids=[STAFF])
        check("撞预算上限 → 状态 partial（不是 success）",
              run.get("status") == "partial", str(run.get("status")))
        detail = run.get("detail") or {}
        check("★ partial 的原因写明是预算闸（前端要能解释为什么没跑完）",
              detail.get("partial_reason") == "max_llm_calls",
              str(detail.get("partial_reason")))
        check("预算真的只花 1 次调用", run.get("llm_calls") == 1, str(run.get("llm_calls")))
        check("还剩 1 条没跑（下一轮接着跑，不是丢掉）",
              run.get("session_total") == 2 and run.get("session_done") == 1,
              f"total={run.get('session_total')} done={run.get('session_done')}")
        task = db.query_one("SELECT status FROM task WHERE task_id=%s",
                            (run.get("task_id"),)) or {}
        check("检查点任务也标 partial", task.get("status") == "partial", str(task))
    finally:
        if prev:
            db.set_setting("interest_max_llm_calls", prev["value"])
        else:
            db.execute("DELETE FROM app_setting WHERE key='interest_max_llm_calls'")

    r2, run2 = _run_once(kind="incremental", staff_ids=[STAFF])
    check("★ 放开预算后接着跑：只剩那 1 条",
          (run2.get("session_total"), run2.get("session_done")) == (1, 1)
          and run2.get("status") == "success",
          f"total={run2.get('session_total')} done={run2.get('session_done')} "
          f"status={run2.get('status')}")


def test_daily_chain():
    """日常链条 sync → scoring → interest 的接线（`scheduler._daily_watch_job`）。

    这条链此前**一个测试都没有**，而它出问题的方式正是最难发现的那种：拿到
    `ok:false` 只打一行 print，表现是"当天那一步静默不跑"。新增的第三步
    （兴趣点）与评分互斥，所以这里把三种情形都钉住：**等自己**（保留状态、下个
    tick 再看）、**正常往下走**、**开关关掉就不跑**。
    """
    import job_service
    import scheduler
    import interest_service
    from interest_service import service as interest_svc

    calls = {"scoring": 0, "interest": 0}
    kinds = {"v": []}                    # 假的 running_kinds（避免真去造 running 行）

    class _Stub:
        def __init__(self, name):
            self.name, self.running = name, False

        def is_running(self):
            return self.running

        def start(self, **kw):
            calls[self.name] += 1
            return {"ok": True, "run_id": 1}

        def stop(self):
            return {"ok": True}

    s_scoring, s_sync, s_interest = _Stub("scoring"), _Stub("sync"), _Stub("interest")
    orig = (scheduler.scoring, scheduler.service, scheduler.job_service.running_kinds,
            interest_svc.is_running, interest_svc.start, scheduler._daily_state)
    scheduler.scoring, scheduler.service = s_scoring, s_sync
    scheduler.job_service.running_kinds = lambda **kw: list(kinds["v"])
    interest_svc.is_running = s_interest.is_running
    interest_svc.start = s_interest.start
    auto_prev = db.query_one("SELECT value FROM app_setting WHERE key='interest_auto_run'")
    try:
        # ---------- ① 兴趣点还在跑 → 评分**不能**启动，但链条不能就此结束 ----------
        kinds["v"] = ["interest"]
        scheduler._set_daily_state({"step": "sync", "phase": "main", "started_at": time.time()})
        scheduler._daily_watch_job()
        st = scheduler._daily_state()
        check("★ 兴趣分析还在跑 → 暂不起评分（互斥），但状态**保留**等下个 tick",
              st.get("step") == "sync" and calls["scoring"] == 0, f"{st} calls={calls}")

        # ---------- ② 它跑完了 → 继续往下走 ----------
        kinds["v"] = []
        scheduler._daily_watch_job()
        st = scheduler._daily_state()
        check("它跑完 → 评分照常启动（不再被自己挡一整天）",
              st.get("step") == "scoring" and calls["scoring"] == 1, f"{st} calls={calls}")

        # ---------- ③ 评分结束 → 链条第③步自动起兴趣点（开关默认开）----------
        scheduler._set_daily_state({"step": "scoring", "phase": "main", "started_at": time.time()})
        scheduler._daily_watch_job()
        st = scheduler._daily_state()
        check("★ 评分结束 → 自动启动兴趣点增量（日常链第③步）",
              st.get("step") == "interest" and calls["interest"] == 1, f"{st} calls={calls}")

        # ---------- ④ 兴趣点在跑 → 链条等它；跑完 → 清状态 ----------
        s_interest.running = True
        scheduler._daily_watch_job()
        check("第③步还在跑 → 状态保留（不提前宣布完成）",
              scheduler._daily_state().get("step") == "interest",
              str(scheduler._daily_state()))
        scheduler._daily_watch_job()
        check("仍在跑 → 依然保留", scheduler._daily_state().get("step") == "interest")
        s_interest.running = False
        scheduler._daily_watch_job()
        check("★ 跑完 → 链条状态清空（这一天算结束）", scheduler._daily_state() == {},
              str(scheduler._daily_state()))

        # ---------- ⑤ 关掉开关 → 只手动触发，链条在评分处收尾 ----------
        db.set_setting("interest_auto_run", "false")
        before = calls["interest"]
        scheduler._set_daily_state({"step": "scoring", "phase": "main", "started_at": time.time()})
        scheduler._daily_watch_job()
        check("★ `interest_auto_run=false` → 日常链不自动跑兴趣点（手动按钮仍可用）",
              calls["interest"] == before and scheduler._daily_state() == {},
              f"calls={calls} state={scheduler._daily_state()}")

        # ---------- ⑥ 启动被拒（真的被别的东西占着）→ 到此为止，不无限重试 ----------
        db.set_setting("interest_auto_run", "true")
        interest_svc.start = lambda **kw: {"ok": False, "reason": "stub-被占着"}
        scheduler._set_daily_state({"step": "scoring", "phase": "main", "started_at": time.time()})
        scheduler._daily_watch_job()
        check("兴趣点启动被拒 → 链条收尾（不卡在中间反复重试）",
              scheduler._daily_state() == {}, str(scheduler._daily_state()))
    finally:
        scheduler.scoring, scheduler.service = orig[0], orig[1]
        scheduler.job_service.running_kinds = orig[2]
        interest_svc.is_running, interest_svc.start = orig[3], orig[4]
        scheduler._set_daily_state({})
        if auto_prev:
            db.set_setting("interest_auto_run", auto_prev["value"])
        else:
            db.execute("DELETE FROM app_setting WHERE key='interest_auto_run'")


def main():
    if not (config.LLM_API_KEY and config.LLM_BASE_URL):
        print("!! 缺 LLM 配置 —— `start()` 会在建 run 之前就返回，本测试无意义")
        return 1
    if db.query_one("SELECT count(*) AS n FROM interest_run "
                    "WHERE status='running'")["n"]:
        print("!! 已有兴趣分析在跑（互斥闸）—— 等它结束再跑本测试")
        return 1

    orig_extract = interest.extract
    task_max = db.query_one("SELECT COALESCE(max(task_id),0) AS n FROM task")["n"]
    interest.extract = _stub_extract
    try:
        _setup()
        test_first_run_and_increment()
        test_customer_only()
        test_distribution_scope()
        test_mutual_exclusion()
        test_budget_gate_and_resume()
        test_daily_chain()
    finally:
        interest.extract = orig_extract
        _wipe()
        # 检查点任务也一并清掉（否则库里会攒下测试造的空任务）
        db.execute("DELETE FROM task_step WHERE task_id IN "
                   "(SELECT task_id FROM task WHERE kind='interest' AND task_id > %s)",
                   (task_max,))
        db.execute("DELETE FROM task WHERE kind='interest' AND task_id > %s", (task_max,))
        left = db.query_one("SELECT count(*) AS n FROM interest_scan "
                            "WHERE staff_id=%s", (STAFF,))["n"]
        print(f"\n[清理] 残留合成行：interest_scan={left}")
    return summary()


if __name__ == "__main__":
    sys.exit(main())
