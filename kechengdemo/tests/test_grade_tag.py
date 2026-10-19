# -*- coding: utf-8 -*-
"""D2-1「等级 → 微伴标签」验收 —— 需要 PostgreSQL 5433。

    .venv/Scripts/python.exe tests/test_grade_tag.py

覆盖：
  · 钩子判定（纯函数、边界穷举）
  · **开关与分数回写完全独立**（四种组合各判各的）
  · 标签 id 同步与缓存
  · 入队（开关 on 才入队）
  · drain 状态机：live 成功 / dry_run 记账 / off 延后 / id 未就绪延后 /
    等级未变跳过（**必须是终态，不能被反复捞起来**）
  · **顺序安全**：同客户多条 → 只投最新，更早的 superseded
  · 失败 → 退避重试；达上限 → dead
  · 水位推进（只有真投递成功才推进）
  · **零污染**：不写 `score_writeback`、不动 `writeback_ledger`

⚠️ 全部用合成数据 + 打桩 weiban（**绝不真调微伴**）；跑完清理；断言不依赖生产现状。
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"


import sys

from _harness import check, summary  # noqa: E402

import db                          # noqa: E402
import grade_tag as gt             # noqa: E402
import hooks                       # noqa: E402
import weiban                      # noqa: E402

_REL = "GTAGTEST_STAFF:GTAGTEST_CUST"
_AID = 999100001
#: 测试开始时的 audit_log 水位（用 id 而不是行数/时间戳 —— 踩过）
_AUDIT_MAX = 0
_TAG_IDS = {"S": "tag_S", "A": "tag_A", "B": "tag_B", "C": "tag_C", "D": "tag_D"}




def _cleanup():
    for tbl in ("grade_tag_outbox",):
        db.execute(f"DELETE FROM {tbl} WHERE relationship_key LIKE %s", ("%GTAGTEST%",))
    db.execute("DELETE FROM grade_tag_ledger WHERE relationship_key LIKE %s", ("%GTAGTEST%",))
    db.execute("DELETE FROM customer_intent_assessment WHERE relationship_key LIKE %s",
               ("%GTAGTEST%",))
    db.execute("DELETE FROM app_setting WHERE key IN ('grade_tag_mode','grade_tag_ids',"
               "'grade_tag_group')")
    if _AUDIT_MAX:
        db.execute("DELETE FROM audit_log WHERE id > %s", (_AUDIT_MAX,))
    # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
    try:
        import scoring_service as ss
        ss.repair_current_flags()
    except Exception:
        pass


class _FakeWeiban:
    """打桩微伴（**记录调用**，便于断言「到底有没有真调接口」）。"""

    def __init__(self, errcode=0, errmsg="ok", raise_exc=None, tags=None):
        self.calls = []
        self.errcode = errcode
        self.errmsg = errmsg
        self.raise_exc = raise_exc
        self.tags = tags if tags is not None else [
            {"tag_group": "意向等级", "tag_name": g, "tag_id": f"tag_{g}"} for g in "SABCD"
        ]

    def iter_all_tags(self, page_size=200):
        yield from self.tags

    def add_customer_tag(self, **kw):
        self.calls.append(("add", kw))
        return {"errcode": 0, "errmsg": "ok"}

    def batch_update_customer_tags(self, staff_id, external_user_id=None,
                                   add_tag_id_list=None, remove_tag_id_list=None):
        self.calls.append(("batch", {"staff": staff_id, "cust": external_user_id,
                                     "add": add_tag_id_list, "rm": remove_tag_id_list}))
        if self.raise_exc:
            raise self.raise_exc
        return {"errcode": self.errcode, "errmsg": self.errmsg}


def _use(fake):
    weiban.iter_all_tags = fake.iter_all_tags
    weiban.add_customer_tag = fake.add_customer_tag
    weiban.batch_update_customer_tags = fake.batch_update_customer_tags


def _set_mode(m):
    db.set_setting("grade_tag_mode", m)


def _set_tags(ids=None):
    import json
    db.set_setting("grade_tag_ids", json.dumps(ids if ids is not None else _TAG_IDS))


def _enqueue(aid, grade="S", remove=None, mode="live"):
    with db.tx() as conn:
        return gt.enqueue(conn, aid, _REL, "GTAGTEST_STAFF", "GTAGTEST_CUST",
                          grade, mode=mode, remove_tag_id=remove)


def _rows():
    return db.query("SELECT * FROM grade_tag_outbox WHERE relationship_key=%s "
                    "ORDER BY outbox_id", (_REL,))


def _ledger():
    return db.query_one("SELECT * FROM grade_tag_ledger WHERE relationship_key=%s", (_REL,))


# ============================================================ 1. 钩子（纯函数）

def test_hook():
    print("\n--- 1. 钩子判定（纯函数）---")
    check("off → 拒绝，且提示「已关闭」",
          hooks.before_grade_tag(mode="off", grade="S", tag_id="t").reason == "tag_mode_off")
    check("非法等级 → 拒绝",
          hooks.before_grade_tag(mode="live", grade="X", tag_id="t").reason == "bad_grade")
    check("等级为空 → 拒绝（不能把 None 写成标签）",
          hooks.before_grade_tag(mode="live", grade=None, tag_id="t").reason == "bad_grade")
    check("tag_id 未就绪 → 拒绝（绝不隐式建标签）",
          hooks.before_grade_tag(mode="live", grade="S", tag_id=None).reason == "tag_not_ready")
    check("★ 等级未变 → 拒绝（避免每次评估都去刷一遍标签）",
          hooks.before_grade_tag(mode="live", grade="S", tag_id="t",
                                 current_grade="S").reason == "grade_unchanged")
    check("等级有变 → 放行", hooks.before_grade_tag(mode="live", grade="S",
                                                   tag_id="t", current_grade="A").allow)
    check("小写等级自动归一", hooks.before_grade_tag(mode="live", grade="s",
                                                   tag_id="t").detail["grade"] == "S")
    check("dry_run 也放行（由调用方记账不投递）",
          hooks.before_grade_tag(mode="dry_run", grade="B", tag_id="t").allow)
    check("detail 带 from_grade（可追溯等级变化）",
          hooks.before_grade_tag(mode="live", grade="S", tag_id="t",
                                 current_grade="B").detail["from_grade"] == "B")

    print("\n--- 2. ★ 开关与分数回写**完全独立** ---")
    combos = [("off", "live"), ("live", "off"), ("live", "live"), ("dry_run", "off")]
    for tag_m, score_m in combos:
        t = hooks.before_grade_tag(mode=tag_m, grade="S", tag_id="t")
        s = hooks.before_writeback(positive_total=5, mode=score_m)
        check(f"标签={tag_m:<8} 分数={score_m:<8} → 各自判定（标签 {t.reason} / 分数 {s.reason}）",
              True, "")
    check("★ 标签 off 不影响分数 live",
          hooks.before_grade_tag(mode="off", grade="S", tag_id="t").denied
          and hooks.before_writeback(positive_total=5, mode="live").allow)
    check("★ 分数 off 不影响标签 live",
          hooks.before_grade_tag(mode="live", grade="S", tag_id="t").allow
          and hooks.before_writeback(positive_total=5, mode="off").denied)


# ============================================================ 3. 标签同步

def test_sync():
    print("\n--- 3. 标签 id 同步与缓存 ---")
    fake = _FakeWeiban()
    _use(fake)
    _cleanup()
    try:
        res = gt.sync_tags(dry_run=True)
        check("dry_run 扫到 5 个等级标签", res["found"] == _TAG_IDS, str(res["found"]))
        check("dry_run 不写缓存", gt.get_tag_ids() == {}, str(gt.get_tag_ids()))

        res2 = gt.sync_tags(dry_run=False)
        check("★ 非 dry_run 写入缓存", gt.get_tag_ids() == _TAG_IDS, str(gt.get_tag_ids()))
        check("缓存后可直接用于判定",
              hooks.before_grade_tag(mode="live", grade="S",
                                     tag_id=gt.get_tag_ids().get("S")).allow)

        fake.tags = [{"tag_group": "别的组", "tag_name": "S", "tag_id": "x"}]
        res3 = gt.sync_tags(dry_run=True)
        check("★ 只认「意向等级」组（别的组的同名标签不算）",
              res3["found"] == {} and res3["missing"] == list("SABCD"), str(res3["found"]))
    finally:
        _cleanup()


# ============================================================ 4. 入队

def test_enqueue():
    print("\n--- 4. 入队（开关 on 才入队）---")
    _cleanup()
    try:
        _set_mode("off")
        check("off 时 enabled=False", gt.enabled() is False)
        check("off 时不入队（评估时就不产生待办）", _enqueue(_AID) is False, "")

        _set_mode("live")
        check("live 时 enabled=True", gt.enabled() is True)
        check("入队成功", _enqueue(_AID, grade="s") is True)   # 故意用小写
        check("同 assessment 重复入队被 ON CONFLICT 挡住（幂等）",
              _enqueue(_AID, grade="A") is False)
        r = _rows()[0]
        check("★ 等级已归一为大写并固化", r["grade"] == "S", r["grade"])
        check("remove_tag_id 随行固化（出队不再算）", r["remove_tag_id"] is None)
    finally:
        _cleanup()


# ============================================================ 5. drain 状态机

def test_drain_live():
    print("\n--- 5. drain：live 成功 ---")
    _cleanup()
    fake = _FakeWeiban()
    _use(fake)
    try:
        _set_mode("live")
        _set_tags()
        _enqueue(_AID, grade="S", remove="tag_A")
        stats = gt.drain()
        check("投递 1 条且成功", stats["ok"] == 1, str(stats))
        call = fake.calls[0][1]
        check("★ 用 batch_update 一次 add 新 + remove 旧",
              call["add"] == ["tag_S"] and call["rm"] == ["tag_A"], str(call))
        r = _rows()[0]
        check("outbox 落 done", r["status"] == "done", r["status"])
        led = _ledger()
        check("★ 水位推进到新等级", led and led["grade"] == "S" and led["tag_id"] == "tag_S",
              str(led))
        check("★ 零污染：不写 score_writeback",
              db.query_one("SELECT count(*) AS n FROM score_writeback WHERE assessment_id=%s",
                           (_AID,))["n"] == 0)
        check("★ 零污染：不动分数水位 writeback_ledger",
              db.query_one("SELECT count(*) AS n FROM writeback_ledger WHERE relationship_key=%s",
                           (_REL,))["n"] == 0)

        check("★ 再 drain 不会重复投递（终态不被捞起）", gt.drain()["checked"] == 0)
        check("总分未被标签链路调用", len(fake.calls) == 1, str(len(fake.calls)))
    finally:
        _cleanup()


def test_drain_dry_run_off_idle():
    print("\n--- 6. drain：dry_run / off / id 未就绪 ---")
    _cleanup()
    fake = _FakeWeiban()
    _use(fake)
    try:
        # dry_run：记账但不调接口
        _set_mode("dry_run"); _set_tags(); _enqueue(_AID, grade="S")
        st = gt.drain()
        check("dry_run 记账 1 条", st["dry_run"] == 1, str(st))
        check("★ dry_run **零网络调用**", fake.calls == [], str(fake.calls))
        check("dry_run 不推进水位", _ledger() is None)
        check("dry_run 是终态（不会反复捞起）", gt.drain()["checked"] == 0)

        # off：延后，保持 pending
        # 场景：**先开着入了队，中途把开关关掉**（关 = 暂停，不是作废）
        _cleanup(); _set_mode("live"); _set_tags(); _enqueue(_AID, grade="A")
        _set_mode("off")
        st2 = gt.drain()
        check("off 时延后 1 条", st2["deferred"] == 1, str(st2))
        check("★ off 时**一次接口都不调**", fake.calls == [], str(fake.calls))
        check("延后后仍是 pending（**暂停而非作废**）", _rows()[0]["status"] == "pending")

        # 无 tag_id：延后（不是失败）
        _cleanup(); _set_mode("live"); _set_tags({})
        fake.calls.clear()
        _enqueue(_AID, grade="B")
        st3 = gt.drain()
        check("标签 id 未就绪 → 延后（不是 failed）", st3["deferred"] == 1, str(st3))
        check("未就绪时也不调接口", fake.calls == [], str(fake.calls))

        # 等级未变：终态 skipped
        _cleanup(); _set_mode("live"); _set_tags()
        with db.tx() as conn:
            conn.execute(
                """INSERT INTO grade_tag_ledger (relationship_key, staff_id, external_user_id,
                       grade, tag_id, writes) VALUES (%s,%s,%s,'S','tag_S',1)""",
                (_REL, "GTAGTEST_STAFF", "GTAGTEST_CUST"))
        fake.calls.clear()
        _enqueue(_AID, grade="S")
        st4 = gt.drain()
        check("★ 等级未变 → skipped（不该白调一次接口）", st4["skipped"] == 1, str(st4))
        check("等级未变时零网络调用", fake.calls == [], str(fake.calls))
        check("★ skipped 是终态（早先的实现会被反复重试）",
              gt.drain()["checked"] == 0)
    finally:
        _cleanup()


def test_order_safety():
    print("\n--- 7. ★ 顺序安全：同客户多条只投最新 ---")
    _cleanup()
    fake = _FakeWeiban()
    _use(fake)
    try:
        _set_mode("live"); _set_tags()
        # 同一客户连续三次评分：A → S → B（模拟乱序到达）
        _enqueue(_AID, grade="A")
        _enqueue(_AID + 1, grade="S")
        _enqueue(_AID + 2, grade="B")
        rows = _rows()
        check("三条待办都已入队", len(rows) == 3, str(len(rows)))
        stats = gt.drain()
        check("只投递 1 条（最新的 B）", stats["ok"] == 1 and len(fake.calls) == 1, str(stats))
        check("★ 投的是**最新等级** B（不是旧等级）",
              fake.calls[0][1]["add"] == ["tag_B"], str(fake.calls[0]))
        check("★ 更早的两条标 superseded",
              stats["superseded"] == 2, str(stats))
        st = [r["status"] for r in _rows()]
        check("状态：superseded / superseded / done", st == ["superseded", "superseded", "done"],
              str(st))
        check("水位是最终等级 B", (_ledger() or {}).get("grade") == "B")
    finally:
        _cleanup()


def test_failure_and_dead():
    print("\n--- 8. 失败重试 / 达上限转 dead ---")
    _cleanup()
    fake = _FakeWeiban(errcode=20353, errmsg="找不到对应员工")
    _use(fake)
    try:
        _set_mode("live"); _set_tags()
        _enqueue(_AID, grade="C")
        st = gt.drain()
        check("业务错误码非 0 → failed", st["failed"] == 1, str(st))
        r = _rows()[0]
        check("记录下 errcode 便于排障", r["last_errcode"] == 20353, str(r["last_errcode"]))
        check("attempts 累加", r["attempts"] == 1, str(r["attempts"]))
        check("★ 排到未来重试（不是立刻重试狂打接口）",
              r["next_attempt_at"] > r["updated_at"], f"{r['next_attempt_at']}")
        check("失败**不推进**水位（否则以为已写成功）", _ledger() is None)

        # 网络异常也按失败处理，不炸
        fake2 = _FakeWeiban(raise_exc=TimeoutError("timed out"))
        _use(fake2)
        db.execute("UPDATE grade_tag_outbox SET next_attempt_at=now() WHERE outbox_id=%s",
                   (_rows()[0]["outbox_id"],))
        st2 = gt.drain()
        check("网络异常被兜住（不抛到主流程）", st2["failed"] == 1, str(st2))
        check("异常信息落 last_errmsg",
              "timed out" in (_rows()[0]["last_errmsg"] or ""), str(_rows()[0]["last_errmsg"]))

        # 达上限 → dead
        db.execute("UPDATE grade_tag_outbox SET attempts=max_attempts, next_attempt_at=now() "
                   "WHERE outbox_id=%s", (_rows()[0]["outbox_id"],))
        st3 = gt.drain()
        check("★ 达上限 → dead + 计数", st3["dead"] == 1, str(st3))
        check("dead 是终态", _rows()[0]["status"] == "dead", _rows()[0]["status"])
    finally:
        _cleanup()


# ============================================================ 9. 人工对齐

def test_align():
    print("\n--- 9. 人工对齐（补历史 / 修漂移）---")
    _cleanup()
    fake = _FakeWeiban()
    _use(fake)
    try:
        db.execute(
            """INSERT INTO customer_intent_assessment
                 (relationship_key, staff_id, target_id, session_id, raw_score, grade,
                  is_current, analyzed_at)
               VALUES (%s,%s,%s,%s,6,'S',true,now())""",
            (_REL, "GTAGTEST_STAFF", "GTAGTEST_CUST", "GTAGTEST_SESS"))

        _set_mode("off")
        check("★ off 时拒绝对齐（不会偷偷写微伴）",
              gt.align(dry_run=True).get("ok") is False)

        _set_mode("live")
        _set_tags({})
        check("tag id 未就绪时拒绝并给出指引",
              "未就绪" in gt.align(relationship_keys=[_REL], dry_run=True).get("reason", ""))

        _set_tags()
        check("★ 不给范围 → 拒绝执行（否则会给全库真实客户改标签）",
              gt.align(dry_run=True).get("ok") is False,
              str(gt.align(dry_run=True))[:120])

        # ⚠️ 回归护栏（真发生过的事故）：加安全闸之前，`align()` 不传范围会扫全库，
        # 一次性把 **196 个真实客户**写进了 `grade_tag_ledger`（水位里的 tag_id 还是
        # 测试桩值 tag_A/B/C/D）。后果：开 live 后 drain 判「等级未变」而跳过 →
        # **真实客户永远打不上标签**，且摘标签时会去删不存在的 id。
        # 这里断言「拒绝」必须伴随**零副作用**，而不只是返回 ok=false。
        _led_before = db.query_one("SELECT count(*) n FROM grade_tag_ledger")["n"]
        fake.calls.clear()
        gt.align(dry_run=False)                     # 无范围，非 dry_run —— 最危险的组合
        _led_after = db.query_one("SELECT count(*) n FROM grade_tag_ledger")["n"]
        check("★ 无范围时零副作用：不写任何客户水位",
              _led_after == _led_before, f"{_led_before} -> {_led_after}")
        check("★ 无范围时一次网络调用都没有", fake.calls == [], str(fake.calls))
        res = gt.align(relationship_keys=[_REL], dry_run=True)
        check("dry_run 报告 1 条待改", res["to_change"] == 1, str(res)[:150])
        check("★ dry_run 零网络调用", fake.calls == [], str(fake.calls))

        res2 = gt.align(relationship_keys=[_REL], dry_run=False)
        check("执行后真调了一次接口", len(fake.calls) == 1, str(fake.calls))
        check("对齐写的是该客户的当前等级 S",
              fake.calls[0][1]["add"] == ["tag_S"], str(fake.calls[0]))
        check("★ 对齐后水位有记录（下次不会重复写）",
              (_ledger() or {}).get("grade") == "S", str(_ledger()))
        check("★ 再对齐应报「等级未变」（幂等）",
              gt.align(relationship_keys=[_REL], dry_run=True)["to_change"] == 0)

        fake.calls.clear()
        res3 = gt.align(relationship_keys=[_REL], dry_run=False, remove_all_others=True)
        check("remove_all_others=True → 摘掉其它 4 个等级标签（修漂移）",
              sorted(fake.calls[0][1]["rm"]) == ["tag_A", "tag_B", "tag_C", "tag_D"],
              str(fake.calls[0]))
        check("res3 正常返回", res3["ok"] is True, str(res3)[:120])
    finally:
        _cleanup()


def test_persist_integration():
    """端到端：评分落库 → **同事务**入队 → drain 真投递（打桩微伴）。

    这一步验证的是「接线」而不是某个函数：`_persist_assessment` 是否真的把标签任务
    写进了同一个事务。分开测的函数都对、但没接上，是最常见的漏。
    """
    print("\n--- 10. ★ 端到端：评分落库 → 入队 → 投递 ---")
    import scoring_service as ss

    s = db.query_one(
        """SELECT c.staff_id, c.session_id, c.target_id, c.name AS target_name,
                  c.msg_count, c.last_msg_time, st.name AS staff_name
             FROM chat_session c LEFT JOIN staff st ON st.staff_id=c.staff_id
            WHERE c.session_type='ext_user' AND c.target_id IS NOT NULL
              AND c.msg_count >= 8 ORDER BY c.msg_count DESC, c.session_id LIMIT 1""")
    if not s:
        print("  （库无可用会话，跳过）")
        return
    cand = dict(s)
    cand["target_id"] = "GTAGTEST_CUST"
    fake = _FakeWeiban()
    _use(fake)
    _cleanup()
    try:
        res = {"positive_items": [{"rule_id": "positive_coder_profile", "score": 2,
                                   "dimension": "d", "polarity": "positive", "reason": "t",
                                   "evidence": ["x"], "evidence_ids": ["#001"],
                                   "evidence_message_ids": ["m"], "evidence_verified": 1}],
               "negative_items": [], "invalid_items": [], "unchecked_rules": [],
               "model_confidence": 0.5, "rule_version": "v1.0", "prompt_version": "t",
               "schema_version": "1", "contract_attempts": 1, "model_name": "deepseek-chat",
               "rule_checks": []}
        mk = lambda raw, g: ({"raw_score": raw, "positive_total": raw, "negative_total": 0},
                             {"grade": g, "priority": "", "advice": ""})

        # ① 开关 off（默认）→ 落库但**不入队**
        _set_mode("off")
        aid = ss.service._persist_assessment(0, cand, res, *mk(6, "S"), False, [],
                                             res["positive_items"], [], "off")
        check("★ off 时落库成功但不产生标签待办",
              db.query_one("SELECT count(*) AS n FROM grade_tag_outbox "
                           "WHERE assessment_id=%s", (aid,))["n"] == 0)

        # ② 开关 live → 落库同时入队（同一事务）
        _set_mode("live"); _set_tags()
        aid2 = ss.service._persist_assessment(0, cand, res, *mk(6, "S"), False, [],
                                              res["positive_items"], [], "off")
        rows = db.query("SELECT * FROM grade_tag_outbox WHERE assessment_id=%s", (aid2,))
        check("★ live 时评分落库**同时**入队（接线正确）", len(rows) == 1, str(len(rows)))
        check("入队时带上了本次等级", rows[0]["grade"] == "S", rows[0]["grade"])
        check("目标客户是 external_user_id（回写用同一 id）",
              rows[0]["external_user_id"] == "GTAGTEST_CUST", rows[0]["external_user_id"])

        st = gt.drain()
        check("★ drain 真投递成功", st["ok"] == 1, str(st))
        check("投递的是本次等级的标签", fake.calls[0][1]["add"] == ["tag_S"], str(fake.calls[0]))
        # ⚠️ 关系键由 `_persist_assessment` 自己拼（**真实员工 id** + 合成客户），
        #    不是测试里那个合成 rel —— 按入队行里的实际键去查，否则查了个不存在的客户
        rel = rows[0]["relationship_key"]
        check("★ 端到端后水位落到该客户",
              (gt.ledger_state(rel) or {}).get("grade") == "S", str(gt.ledger_state(rel)))
    finally:
        _cleanup()


def test_tag_name_alias():
    """标签名归一：微伴里既有的是「S级意向」，不能因为名字不同就重复建一套标签。

    为什么重要：`sync_tags` 认不出既有标签 → 判定"缺失" → 走 `tag/add` 建新标签，
    而 `tag/add` 会把标签**真的打到载体客户身上**。归一错了就是在客户后台留垃圾。
    """
    cases = {"S": "S", "s": "S", "S级": "S", "S级意向": "S", " S 级意向 ": "S",
             "A级意向": "A", "C": "C", "D级": "D",
             "VIP学员": "", "跟进意向": "", "S级VIP": "", "": ""}
    bad = {k: (gt.grade_of_tag_name(k), v) for k, v in cases.items()
           if gt.grade_of_tag_name(k) != v}
    check("★ 等级标签名归一（S / S级 / S级意向 都认成 S；其它名字不误认）",
          not bad, str(bad))


def main():
    global _AUDIT_MAX
    _AUDIT_MAX = (db.query_one("SELECT coalesce(max(id),0) AS n FROM audit_log") or {}).get("n", 0)
    # ⚠️ 本文件会改 `grade_tag_*` 设置（模式 / 标签 id / 组名 / 载体客户）。
    # **先存、跑完恢复** —— 以前只删不恢复：跑一次回归就把操作员配好的「意向标签」组、
    # tag_id 和 dry_run 模式**清空**，之后标签链路静默变回 off（实测踩到）。
    # 测试必须能反复跑，且不能改生产配置。
    _KEYS = ("grade_tag_mode", "grade_tag_ids", "grade_tag_group", "grade_tag_probe_customer")
    saved = {k: db.get_setting(k) for k in _KEYS}
    db.execute("DELETE FROM app_setting WHERE key = ANY(%s)", (list(_KEYS),))
    print("=== D2-1 等级标签回写验收（架构 §10 D2-1）===")
    try:
        test_hook()
        test_sync()
        test_enqueue()
        test_drain_live()
        test_drain_dry_run_off_idle()
        test_order_safety()
        test_failure_and_dead()
        test_align()
        test_persist_integration()
        test_tag_name_alias()
    finally:
        _cleanup()
        for k, v in saved.items():
            if v is None:
                db.execute("DELETE FROM app_setting WHERE key = %s", (k,))
            else:
                db.set_setting(k, v)
    return summary()

if __name__ == "__main__":
    sys.exit(main())
