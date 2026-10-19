# -*- coding: utf-8 -*-
"""同步第一遍「读取会话列表」的成本控制 —— 需要 PostgreSQL 5433。

    .venv\\Scripts\\python.exe tests\\test_sync_session_scan.py

## 守的是什么（2026-10-16 实测出来的慢根因）

一次「增量同步」卡在第一遍 20 分钟只推进 127 个员工。查下来：`stop_before` 原来
只能来自 `sync_cursor`（**会话级**水位），而 **308 个员工里只有 11 个有游标** ——
其余 297 个 `stop_before=0`，于是每轮都要把他们的会话列表**一路翻到 20000 条上限**
（单个员工 20050 条会话 = 200 页请求）。而这些会话**上一轮就已经 upsert 进
`chat_session` 了**，库里的痕迹本身就是"扫到哪儿了"。

本文件钉住三件事：

1. **有水位的员工提前收工**（`stopped_early`），没水位的才全翻；
2. **`_staff_scan_watermark` 能把"扫过"变成水位**（扫完一轮 → 下一轮就有水位）；
3. **撞上翻页上限时不再是静默截断**：`stats.truncated` 为真，且那次打印告警
   （以前写死 `offset >= 20000` 直接 break，用户以为"同步成功了"）。

⚠️ 不打真实微伴接口：`sync_service.weiban.list_sessions` 被替身成合成分页。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import sys
from pathlib import Path

from _harness import ROOT, check, summary  # noqa: E402

import db                     # noqa: E402
import sync_service as ss     # noqa: E402

STAFF = "SCAN:staff"
DAY = 86400
T = 1_792_000_000                     # 固定时间基准，便于断言


def _page(n, base):
    """造一页会话（按 last_msg_time 降序）。"""
    return [{"id": f"SCAN:s{n - i}", "session_type": "ext_user", "name": f"客户{n - i}",
             "target_id": f"SCAN:t{n - i}", "last_msg_time": base - i * 600,
             "agree_msgaudit": True} for i in range(n)]


def _wipe():
    db.execute("DELETE FROM chat_session WHERE staff_id = %s", (STAFF,))


def _with_pages(pages):
    """把 `weiban.list_sessions` 替身成"按 offset 返回给定页"。"""
    def fake(staff_id, stype, limit=100, offset=0, order_by=None):
        idx = offset // limit
        return {"session_list": pages[idx] if idx < len(pages) else []}
    return fake


def main():
    _wipe()
    orig = ss.weiban.list_sessions
    saved_cap = db.query_one("SELECT value FROM app_setting WHERE key='sync_session_page_cap'")
    try:
        # ---------------- ① 无水位 → 全翻；有水位的页 → 提前收工 ----------------
        p1, p2, p3 = _page(100, T), _page(100, T - 100_000), _page(40, T - 200_000)
        ss.weiban.list_sessions = _with_pages([p1, p2, p3])
        out, st = ss.service._sync_sessions_of(STAFF, ["ext_user"], stop_before=0)
        check("无水位 → 一直翻到最后一页（240 条 / 3 页）",
              len(out) == 240 and st["pages"] == 3, f"{len(out)}/{st['pages']}")
        check("无水位 → 没有提前收工", st["stopped_early"] == 0, str(st))
        check("无水位 → 未截断", st["truncated"] is False, str(st))

        # ② 水位 = 第 2 页的最新时间 → 第 2 页整页都更旧 → 第 2 页就收工
        out2, st2 = ss.service._sync_sessions_of(STAFF, ["ext_user"],
                                                 stop_before=p2[0]["last_msg_time"])
        check("有水位 → 第 2 页后提前收工（pages=2、stopped_early=1）",
              st2["pages"] == 2 and st2["stopped_early"] == 1, str(st2))
        check("提前收工时**已经落库**的会话数正确（200 条）", len(out2) == 200, str(len(out2)))

        # ---------------- ③ 「扫过」能变成水位（下一轮才可能提前收工） ----------------
        wm = ss.service._staff_scan_watermark(STAFF, ["ext_user"])
        expect = max(s["last_msg_time"] for s in p1)
        check("扫完一轮后，员工水位 = 已知会话里最新的 last_msg_time", wm == expect,
              f"{wm} vs {expect}")
        check("换个类型查 → 没有该类型的会话，水位为 0",
              ss.service._staff_scan_watermark(STAFF, ["room"]) == 0)

        # ---------------- ④ 撞上限 → **报告**截断（不再静默） ----------------
        db.set_setting("sync_session_page_cap", "200")
        many = [_page(100, T - i * 100_000) for i in range(5)]
        ss.weiban.list_sessions = _with_pages(many)
        out3, st3 = ss.service._sync_sessions_of(STAFF, ["ext_user"], stop_before=0)
        check("撞上翻页上限 → stats.truncated 为真", st3["truncated"] is True, str(st3))
        check("撞上限即停（200 条 / 2 页，不会悄悄继续翻）",
              st3["pages"] == 2 and len(out3) == 200, f"{st3['pages']}/{len(out3)}")

        # ---------------- ⑥ 够数就不再翻（`max_sessions_per_staff` 的翻页闸）----------------
        # 为什么必须钉住：调用方随后会 `sess[:200]` 丢掉多余的，而"翻完再丢"的代价是
        # 每员工十几二十页外部请求 —— 实测单轮 2779 页、27 分钟，把日常同步窗口
        # （20 分钟）整个吃光，第二遍拉消息一次都没跑到（连续 3 天 session_done=0）。
        db.execute("DELETE FROM app_setting WHERE key='sync_session_page_cap'")
        many2 = [_page(100, T - i * 100_000) for i in range(6)]
        ss.weiban.list_sessions = _with_pages(many2)
        out4, st4 = ss.service._sync_sessions_of(STAFF, ["ext_user"], stop_before=0,
                                                 limit=200)
        check("★ 给了 limit=200 → 翻 2 页就停（不再为「翻完再丢」付外部请求）",
              st4["pages"] == 2 and len(out4) == 200, f"{st4['pages']}/{len(out4)}")
        check("limit 不改变结果：拿到的就是调用方要的前 200 条",
              out4[0]["last_msg_time"] == many2[0][0]["last_msg_time"]
              and out4[-1]["last_msg_time"] == many2[1][-1]["last_msg_time"],
              f"{out4[0]['last_msg_time']} … {out4[-1]['last_msg_time']}")
        check("没有 limit 的老行为不变（仍然翻到底）",
              ss.service._sync_sessions_of(STAFF, ["ext_user"], stop_before=0)[1]["pages"] == 6,
              "limit 缺省时不该改变语义")
    finally:
        ss.weiban.list_sessions = orig
        if saved_cap:
            db.set_setting("sync_session_page_cap", saved_cap["value"])
        else:
            db.execute("DELETE FROM app_setting WHERE key='sync_session_page_cap'")
        _wipe()

    # ---------------- ⑤ 接线与「旧行为已消失」（源码级） ----------------
    src = (Path(ROOT) / "backend" / "sync_service.py").read_text(encoding="utf-8")
    check("`_run` 用了员工扫描水位（否则优化等于没接）",
          "_staff_scan_watermark(_sid, session_types)" in src)
    check("水位带安全余量（老会话不会被永久跳过）",
          "SCAN_STOP_MARGIN_SECONDS" in src and "kw - SCAN_STOP_MARGIN_SECONDS" in src)
    check("翻页上限来自配置而不是写死 20000",
          'db.setting_int("sync_session_page_cap"' in src)
    check("旧的静默截断判据已消失",
          "offset >= 20000" not in src, "仍是写死的静默截断")
    check("第一遍的可观测字段写进任务详情",
          '"session_pages": pages_total' in src and '"truncated_count"' in src)
    return summary()


if __name__ == "__main__":
    sys.exit(main())
