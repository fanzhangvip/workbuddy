# -*- coding: utf-8 -*-
"""阶段二集成测试（需数据库）：预处理层 + 接口层 + 配置登记。

运行（用后端 venv python，连宿主 5433）：
    "C:/Users/EDY/.workbuddy/binaries/python/envs/default/Scripts/python.exe" tests/test_ctx_integration.py

覆盖：
  1) DB 取数 → build_context → 编号回链 message_id → 证据闭环
  2) /api/messages 增补字段 is_media / type_name / role / media_url
  3) /api/scoring/settings 登记 6 个 ctx_* 且可读写

测试数据用唯一 session_id（__itest_*），finally 清理，不污染库。
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import db                      # noqa: E402
import ctx_preprocess          # noqa: E402
import scoring_service         # noqa: E402
import api_chat                # noqa: E402
import api_scoring             # noqa: E402

STAFF = "__itest_staff"
SID = "__itest_%d" % int(time.time())

CTX_KEYS = ("ctx_window_size", "ctx_drop_msgtypes", "ctx_drop_pure_url",
            "ctx_drop_system", "ctx_display_relative", "ctx_min_kept")


def ins(mid, direction, mt, content, ts, plain=None):
    db.execute(
        """INSERT INTO chat_message
             (staff_id, session_id, message_id, msgid, direction, msg_time,
              content, msgtype, plain, content_state, created_at, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'ok',%s,%s)""",
        (STAFF, SID, mid, "m_" + mid, direction, ts, content, mt,
         json.dumps(plain) if plain is not None else None, ts, ts))


def cleanup():
    db.execute("DELETE FROM chat_message WHERE staff_id = %s AND session_id = %s",
               (STAFF, SID))


def test_ctx_pipeline():
    now = int(time.time())
    ins("itest-1", "send", 1, "你好，我是做 Java 后端的", now - 3600)
    ins("itest-2", "recv", 1, "我们公司下季度要上 AI 客服项目", now - 1800)
    # 图片：正文是 CDN 链接，应被占位成 [图片]
    ins("itest-3", "recv", 2, "http://cdn.example.com/a.png", now - 600)
    # 撤回消息：整行应被丢弃（revoke）
    ins("itest-4", "recv", 31, "撤回了一条消息", now - 300)
    # 空正文（有 plain 但文本为空）：应被丢弃（empty）
    ins("itest-5", "recv", 1, "", now - 60,
        plain={"msgtype": 1, "text": {"content": ""}})

    ctx = scoring_service.build_context(STAFF, SID)

    assert ctx["index"].get("#001") == "itest-1", ctx["index"]
    assert ctx["index"].get("#002") == "itest-2", ctx["index"]
    print("[OK] index 回链：", ctx["index"])

    dr = ctx["stats"]["drop_reasons"]
    assert dr.get("revoke", 0) == 1, dr
    assert dr.get("empty", 0) == 1, dr
    print("[OK] 丢弃原因：", dict(dr), "| kept =", ctx["stats"]["kept"])

    assert "[图片]" in ctx["text"], ctx["text"]
    assert "cdn.example.com" not in ctx["text"], ctx["text"]
    assert "撤回" not in ctx["text"], ctx["text"]
    print("[OK] 媒体 URL 已占位、撤回已剔除")

    r = ctx_preprocess.resolve_evidence(["#002", "#1", "#999"], ctx["index"])
    assert r["message_ids"] == ["itest-2", "itest-1"], r
    assert r["invalid"] == ["#999"], r
    print("[OK] resolve_evidence 闭环：", r)


def test_api_chat_enrichment():
    t = int(time.time())
    db.execute(
        """INSERT INTO chat_message
             (staff_id, session_id, message_id, msgid, direction, msg_time,
              content, msgtype, plain, content_state, created_at, updated_at)
           VALUES (%s,%s,%s,%s,'recv',%s,%s,%s,%s::jsonb,'ok',%s,%s)""",
        (STAFF, SID, "api-1", "m_api-1", t, "http://cdn.example.com/x.png", 2,
         json.dumps({"msgtype": 2, "image": {"media": {"url": "http://cdn.example.com/x.png"}}}),
         t, t))
    res = api_chat.messages(staff_id=STAFF, session_id=SID, limit=20)
    it = next(x for x in res["items"] if x["message_id"] == "api-1")
    assert it["is_media"] is True, it
    assert it["type_name"] == "图片", it
    assert it["role"] == "客户", it
    assert it["media_url"] == "http://cdn.example.com/x.png", it
    print("[OK] /api/messages 增补字段：",
          {k: it[k] for k in ("is_media", "type_name", "role", "media_url", "media_name")})


def test_settings_registration():
    s = api_scoring.get_settings()
    for k in CTX_KEYS:
        assert k in s, (k, s)
    print("[OK] get_settings ctx_*：", {k: s[k] for k in CTX_KEYS})

    old = s["ctx_min_kept"]
    api_scoring.set_settings({"ctx_min_kept": 7})
    got = api_scoring.get_settings()["ctx_min_kept"]
    assert int(got) == 7, got
    api_scoring.set_settings({"ctx_min_kept": old})            # 还原，避免副作用
    assert int(api_scoring.get_settings()["ctx_min_kept"]) == int(old)
    print("[OK] set/get ctx_min_kept 往返正常（已还原为 %s）" % old)


def main():
    cleanup()
    try:
        test_ctx_pipeline()
        test_api_chat_enrichment()
        test_settings_registration()
        print("\n=== 阶段二集成测试全部通过 ===")
        return 0
    finally:
        cleanup()
        try:
            db.pool().close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
