# -*- coding: utf-8 -*-
"""写回身份自检（t1）：库里的 `target_id` 是否就是微伴的 `external_user_id`。

    .venv\\Scripts\\python.exe tests\\test_writeback_identity.py

## 为什么这条必须单独测

评分与标签回写都只靠 `staff_id + external_user_id` 定位客户。如果库里的
`target_id` 不是微伴的 `external_user_id`，**回写就会打到错误的客户身上**
（或者报"找不到对应员工"）——而这是外部不可撤销的副作用。自检接口就是用来
把这条假设变成"每次都能量出来的事实"，所以它自己的判定逻辑不能错。

不打真实微伴接口：`weiban.list_external_users` 被替身成合成客户列表。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import sys

from _harness import check, summary  # noqa: E402

import api_scoring     # noqa: E402
import db              # noqa: E402
import weiban          # noqa: E402

STAFF = "ZID:staff"
SESS_A, TARGET_A = "ZID:s1", "ZID:u1"
SESS_B, TARGET_B = "ZID:s2", "ZID:u2"
T0 = 1_792_300_000


def _wipe():
    db.execute("DELETE FROM chat_session WHERE staff_id = %s", (STAFF,))


def _setup():
    _wipe()
    for sess, target, n in ((SESS_A, TARGET_A, 10), (SESS_B, TARGET_B, 5)):
        db.execute(
            """INSERT INTO chat_session
                 (staff_id, session_id, session_type, target_id, name, msg_count, last_msg_time)
               VALUES (%s,%s,'ext_user',%s,%s,%s,%s)""",
            (STAFF, sess, target, f"客户 {target}", n, T0))


def _fake_list(ids):
    def fake(staff_id="", limit=100, offset=0, **kw):
        return {"errcode": 0, "errmsg": "ok",
                "external_user_list": [{"id": i} for i in ids[offset:offset + limit]]}
    return fake


def main():
    _setup()
    orig = weiban.list_external_users
    try:
        # ---------- ① 一个客户在微伴侧已不存在（关系解除/换员工）----------
        weiban.list_external_users = _fake_list([TARGET_A])
        r = api_scoring.writeback_identity_check(staff_id=STAFF, limit=10)
        check("自检跑通", r.get("ok") is True, str(r)[:200])
        check("核对了库里的 2 个客户", r.get("checked") == 2, str(r.get("checked")))
        check("★ 命中 1 个、缺失 1 个（缺失的写回必然失败，应先处理关系）",
              r.get("matched") == 1 and r.get("missing_count") == 1, str(r)[:300])
        check("缺失项带 target_id 与来源会话时间（便于人工定位）",
              r["missing"] and r["missing"][0]["target_id"] == TARGET_B
              and r["missing"][0].get("last_at") is not None, str(r.get("missing")))
        check("结论说清是「部分不一致」", "不一致" in (r.get("conclusion") or ""), r.get("conclusion"))

        # ---------- ② 两边完全一致 ----------
        weiban.list_external_users = _fake_list([TARGET_A, TARGET_B])
        r2 = api_scoring.writeback_identity_check(staff_id=STAFF, limit=10)
        check("★ 全部命中 → 结论为「一致」（target_id 就是 external_user_id）",
              r2.get("matched") == 2 and r2.get("missing_count") == 0
              and "一致" in (r2.get("conclusion") or ""), str(r2)[:300])

        # ---------- ③ 接口失败要如实报，不能假装通过 ----------
        def boom(**kw):
            raise RuntimeError("access_token 失效")
        weiban.list_external_users = boom
        r3 = api_scoring.writeback_identity_check(staff_id=STAFF, limit=10)
        check("★ 微伴接口失败 → ok=false 并带原因（不静默通过）",
              r3.get("ok") is False and "access_token" in (r3.get("reason") or ""), str(r3))
    finally:
        weiban.list_external_users = orig
        _wipe()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
