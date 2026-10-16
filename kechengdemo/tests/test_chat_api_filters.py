# -*- coding: utf-8 -*-
"""会话管理页（`#/chat`）三个接口的行为 —— 需要 PostgreSQL 5433。

    .venv\\Scripts\\python.exe tests\\test_chat_api_filters.py

## 守的都是"实测踩到过、但没人测"的地方（2026-10-16）

1. **`/api/sessions` 的 `total` 漏了关键词条件**：搜索时"共 N 个会话"显示的是全量
   （实测 `has_msg=0&q=zzz` → items=19、total=**20058**）。
2. **`total_all`**：页面默认勾着「只看已同步」，某员工有 2 万个会话但一条消息都没同步时，
   `items`/`total` 都是 0 —— 旧文案据此说「该员工暂无会话」，与事实相反。有了 `total_all`
   才能说清"有会话、只是没同步"。
3. **`/api/messages` 的向上翻页必须是复合游标**：`msg_time` 是秒级，同一秒多条消息很常见
   （实测 804 个有正文的会话里 **636 个**存在，共 3203 条），只按秒翻页会**跳过**边界那一秒
   里更早的消息。
4. **`/api/staffs` 的 `session_type` 只影响计数、不影响谁在列表里**：否则切到"群聊"后，
   没有群聊会话的员工会从下拉里消失，用户切不回来。

⚠️ 全部用合成行（`ZZAPI:` 前缀），跑完删净 —— 不借真实会话（借真实数据的测试会随数据漂移）。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import sys

from _harness import check, summary  # noqa: E402

import api_chat               # noqa: E402
import db                     # noqa: E402

STAFF = "ZZAPI:staff"
T = 1_792_100_000


def _wipe():
    db.execute("DELETE FROM chat_message WHERE staff_id = %s", (STAFF,))
    db.execute("DELETE FROM chat_session WHERE staff_id = %s", (STAFF,))
    db.execute("DELETE FROM staff WHERE staff_id = %s", (STAFF,))


def _setup():
    _wipe()
    db.execute("""INSERT INTO staff (staff_id, name, alias) VALUES (%s,%s,%s)""",
               (STAFF, "ZZAPI 测试员工", "zzapi"))
    # 三个会话：两个有消息、一个没有（用于区分 total 与 total_all）
    for i, (sid, nmsg) in enumerate((("ZZAPI:s1", 5), ("ZZAPI:s2", 2), ("ZZAPI:s3", 0))):
        db.execute("""INSERT INTO chat_session
                        (staff_id, session_id, session_type, name, target_id,
                         last_msg_time, msg_count)
                      VALUES (%s,%s,'ext_user',%s,%s,%s,%s)""",
                   (STAFF, sid, f"ZZAPI 客户{i + 1}", f"ZZAPI:t{i + 1}",
                    T - i * 1000, nmsg))
    # s1 的 5 条消息**全在同一秒**（复合游标的用例）；s2 两条不同秒
    for j in range(5):
        db.execute("""INSERT INTO chat_message
                        (staff_id, session_id, message_id, direction, msg_time,
                         content, content_state, msgtype, msg_type)
                      VALUES (%s,%s,%s,'recv',%s,%s,'ok',1,'text')""",
                   (STAFF, "ZZAPI:s1", f"ZZAPI:m{j}", T, f"同秒消息 {j}"))
    for j in range(2):
        db.execute("""INSERT INTO chat_message
                        (staff_id, session_id, message_id, direction, msg_time,
                         content, content_state, msgtype, msg_type)
                      VALUES (%s,%s,%s,'recv',%s,%s,'ok',1,'text')""",
                   (STAFF, "ZZAPI:s2", f"ZZAPI:n{j}", T - 5000 - j, f"普通消息 {j}"))


def main():
    _setup()
    try:
        # ---------------- ① total 必须跟着关键词走 ----------------
        r = api_chat.sessions(staff_id=STAFF, session_type="ext_user", q="ZZAPI", has_msg=0)
        check("关键词命中 3 个会话 → total=3", r["total"] == 3, str(r["total"]))
        r2 = api_chat.sessions(staff_id=STAFF, session_type="ext_user", q="客户1", has_msg=0)
        check("关键词只命中 1 个 → total=1（旧代码这里仍是 3）",
              r2["total"] == 1 and len(r2["items"]) == 1, f"total={r2['total']}")
        r3 = api_chat.sessions(staff_id=STAFF, session_type="ext_user", q="不存在xyz", has_msg=0)
        check("关键词无命中 → total=0", r3["total"] == 0, str(r3["total"]))

        # ---------------- ② total_all 忽略 has_msg（供前端区分空状态） ----------------
        r4 = api_chat.sessions(staff_id=STAFF, session_type="ext_user", q="ZZAPI", has_msg=1)
        check("has_msg=1 → 只有 2 个已同步会话", r4["total"] == 2, str(r4["total"]))
        check("total_all 忽略 has_msg → 3（「有会话但都没同步」才说得清）",
              r4["total_all"] == 3, str(r4["total_all"]))

        # ---------------- ③ 消息翻页：复合游标不跳过同秒消息 ----------------
        first = api_chat.messages(staff_id=STAFF, session_id="ZZAPI:s1", limit=3)
        check("最新 3 条（同一秒）", [m["message_id"] for m in first["items"]]
              == ["ZZAPI:m2", "ZZAPI:m3", "ZZAPI:m4"], str(len(first["items"])))
        oldest = first["items"][0]
        more = api_chat.messages(staff_id=STAFF, session_id="ZZAPI:s1", limit=3,
                                 before_time=oldest["msg_time"],
                                 before_id=oldest["message_id"])
        check("★ 复合游标能取到同一秒里更早的 2 条（不丢）",
              [m["message_id"] for m in more["items"]] == ["ZZAPI:m0", "ZZAPI:m1"],
              str([m["message_id"] for m in more["items"]]))
        legacy = api_chat.messages(staff_id=STAFF, session_id="ZZAPI:s1", limit=3,
                                   before_time=oldest["msg_time"])
        check("旧语义（只传秒）确实会跳过同秒消息 —— 这就是要带 message_id 的原因",
              legacy["items"] == [], str([m["message_id"] for m in legacy["items"]]))

        # ---------------- ④ staffs：类型只影响计数 ----------------
        s_ext = api_chat.staffs(q="ZZAPI", only_with_session=1, session_type="ext_user")
        s_room = api_chat.staffs(q="ZZAPI", only_with_session=1, session_type="room")
        check("session_type=ext_user → 计数 3",
              s_ext["items"] and s_ext["items"][0]["session_count"] == 3,
              str(s_ext["items"][:1]))
        check("★ session_type=room → 计数 0，但**员工仍在下拉里**（否则切不回来）",
              s_room["items"] and s_room["items"][0]["session_count"] == 0,
              str(s_room["items"][:1]))
        check("不传 session_type → 仍是全部类型（向后兼容）",
              api_chat.staffs(q="ZZAPI", only_with_session=1)["items"][0]["session_count"] == 3)

        # ---------------- ⑤ /session 与 /sessions 返回同形 ----------------
        # 深链兜底（ChatView 的 openFromQuery）把 /session 拿到的这一条**直接插进列表**，
        # 页面各处的其它代码拿到的字段就必须和列表项一样。曾经 /session 是 `SELECT *`：
        # 少了子查询算出来的 decoded_count → 同一个会话，在列表里"已解码 100%"，
        # 单独打开却是"0%"。这里钉住两份字段集**完全相同**。
        one = api_chat.session_detail(staff_id=STAFF, session_id="ZZAPI:s1")
        listed = [x for x in api_chat.sessions(staff_id=STAFF, session_type="ext_user",
                                              has_msg=0)["items"]
                  if x["session_id"] == "ZZAPI:s1"]
        check("查得到这个会话", bool(listed), str(one)[:120])
        check("★ /session 的字段集与 /sessions 列表项完全一致（含 decoded_count）",
              bool(listed) and set(one) == set(listed[0]),
              "只在一侧：" + str(set(one) ^ set(listed[0])) if listed else "列表里没有")
        check("/session 查不到时返回空 dict（前端据此说「打不开」）",
              api_chat.session_detail(staff_id=STAFF, session_id="ZZAPI:nope") == {})
    finally:
        _wipe()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
