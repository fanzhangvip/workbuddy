# -*- coding: utf-8 -*-
"""增量取数窗口的验收（需求 4.3：`start_time` / `end_time`）。

    .venv/Scripts/python.exe tests/test_sync_window.py

需求原文：

    start_time = 上次成功水位 - 15 分钟
    end_time   = 本轮任务开始时间 - 1 分钟
    · 每轮任务开始时固定 end_time，分页中不得改变，避免新增消息造成分页漂移
    · 成功写入该会话的全部页面后才推进水位；失败时保留原水位
    · 消息使用 UNIQUE(staff_id, session_id, message_id) 约束幂等写入

改造前实测：**只传了 `start_time`，没有 `end_time`** —— 接口按消息时间**逆序** +
offset 分页，翻页途中新到的消息会把整页往后挤，边界那条（最新的）会被跳过。
本文件把窗口规则与「分页全程同一个 end_time」钉住；幂等写入与水位规则由
`test_backfill.py` / `test_batch234.py` 等既有用例覆盖（真跑同步会打真实接口）。
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t0"

import sys

from _harness import ROOT, check, summary      # noqa: E402

import sync_service                            # noqa: E402
import weiban                                  # noqa: E402


def test_pure_window():
    print("\n--- 1. 窗口是纯函数算出来的（需求 4.3 的两条公式）---")
    # 水位 = 1_700_000_000；轮次开始 = 1_700_100_000
    start, end = sync_service.sync_window(1_700_000_000, 1_700_100_000, 15 * 60)
    check("start_time = 水位 − 15 分钟", start == 1_700_000_000 - 900, start)
    check("end_time = 轮次开始 − 60 秒", end == 1_700_100_000 - 60, end)

    s0, e0 = sync_service.sync_window(0, 1_700_100_000, 900)
    check("首次同步（水位=0）→ start_time 不限（None）", s0 is None, s0)
    check("首次同步也固定 end_time", e0 == 1_700_100_000 - 60, e0)

    # 水位比重叠窗口还小（新会话）→ 不该出现负数时间戳
    s1, _ = sync_service.sync_window(300, 1_700_100_000, 900)
    check("水位 < 重叠窗口 → 下界夹到 0（不产生负数）", s1 == 0, s1)


def test_params_are_passed():
    print("\n--- 2. 两个时间参数真的传给了微伴接口 ---")
    calls = []
    orig = weiban.list_messages

    def fake(staff_id, session_id, limit, offset, start_time=None, end_time=None):
        calls.append((start_time, end_time))
        return {"message_list": []}

    weiban.list_messages = fake
    try:
        svc = sync_service.SyncService()
        svc._messages_of("S1", "SESS1", 1_700_000_000, 0, end=1_700_099_940)
        check("start_time 透传", calls and calls[0][0] == 1_700_000_000, calls[:2])
        check("★ end_time 透传（改造前这里没有传 → 分页漂移）",
              calls and calls[0][1] == 1_700_099_940, calls[:2])
    finally:
        weiban.list_messages = orig


def test_end_fixed_across_pages():
    print("\n--- 3. 同一会话翻页时 end_time 必须**逐页相同** ---")
    seen = []
    orig = weiban.list_messages

    def fake(staff_id, session_id, limit, offset, start_time=None, end_time=None):
        seen.append(end_time)
        # 第一页给满（触发翻页），第二页给不满（结束）
        n = limit if offset == 0 else 1
        return {"message_list": [{"id": f"m{offset}-{i}", "time": 1_700_000_000 + i}
                                 for i in range(n)]}

    weiban.list_messages = fake
    try:
        svc = sync_service.SyncService()
        msgs = svc._messages_of("S1", "SESS1", 1_699_999_100, 0, end=1_700_099_940)
        check("确实翻了页（两次调用）", len(seen) == 2, seen)
        check("★ 两页的 end_time 完全一致（否则边界消息会被挤掉）",
              len(set(seen)) == 1 and seen[0] == 1_700_099_940, seen)
        check("返回按时间升序", [m["time"] for m in msgs] == sorted(m["time"] for m in msgs))
    finally:
        weiban.list_messages = orig


def test_overlap_setting_is_wired():
    print("\n--- 4. 重叠窗口是可配项（app_setting > .env > 默认 15 分钟）---")
    import settings_registry as reg
    spec = next((s for s in reg.SETTINGS if s.key == "sync_overlap_minutes"), None)
    check("注册表里有 sync_overlap_minutes", spec is not None)
    check("默认 15 分钟（与需求 4.3 一致）", spec and spec.default == 15,
          spec.default if spec else None)
    # ⚠️ WRITABLE 是 `(api_key, app_setting 键名)` 的**二元组**集合（写侧与读侧同源）
    check("在白名单里（可从前端改）",
          ("sync_overlap_minutes", "sync_overlap_minutes") in set(reg.WRITABLE),
          [w for w in reg.WRITABLE if "overlap" in str(w)])


def test_skip_rule_covers_overlap():
    """跳过条件必须是「连重叠窗口都没覆盖到」，而不是「没超过水位」。

    反例（改造前）：会话最后一条正好 = 水位 → 被跳过 → 重叠窗口形同虚设，
    迟到消息（msg_time 早于水位）永远取不到。
    """
    import inspect
    src = inspect.getsource(sync_service.SyncService._run)
    check("★ 跳过判据含重叠窗口（`watermark - overlap_seconds`）",
          "watermark - overlap_seconds" in src, "代码里没看到该判据")
    # 精确匹配**代码行**（注释里会引用旧判据作为反面说明，不能被算进来）
    check("旧判据（<= 水位就跳过）已从代码里消失",
          "elif last and last <= watermark" not in src
          and 'if s.get("last_msg_time") and s["last_msg_time"] <= watermark' not in src)


def main():
    test_pure_window()
    test_params_are_passed()
    test_end_fixed_across_pages()
    test_overlap_setting_is_wired()
    test_skip_rule_covers_overlap()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
