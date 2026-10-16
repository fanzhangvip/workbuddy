# -*- coding: utf-8 -*-
"""一次代码评审（P0/P1/P2 批次）的缺陷修复回归 —— 需要 PostgreSQL 5433。

    .venv/Scripts/python.exe tests/test_bugfix_p0p1.py

覆盖：
  · P0-3 浏览器清理**只杀本项目实例**，绝不 `taskkill /F /IM chrome.exe`
  · P0-4 建标签必须有**人工配置的专用载体**，绝不自动挑真实客户
  · P1-4 `sync_service.is_running()` **跨进程可见**（含僵尸回收）
  · P2-8 引擎规则档位**从规则表读**，不再硬编码 -2
  · P2-11 `sync_rules` 不再「先清表再写」；陈旧档位仍会被清掉
  · P2-12 `/api/health` 不返回连接串
  · P2-13 `trace.enabled()` 进程内缓存 + 设置变更后立即失效
  · P2-7 `add_customer_score` 两种 token 模式都走统一 HTTP 层

⚠️ 全部打桩外部依赖（绝不真调微伴 / 绝不起浏览器）；跑完清理；断言不依赖生产现状。
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"


import copy
import sys

from _harness import check, delete_rows, summary  # noqa: E402

import db                          # noqa: E402
import grade_tag as gt             # noqa: E402
import rules as rules_mod          # noqa: E402
import scoring_service as ss       # noqa: E402
import sync_service                # noqa: E402
import trace as trace_mod          # noqa: E402
import weiban                      # noqa: E402
import wb_bootstrap                # noqa: E402

_SYNC_JOB_IDS = []




def _cleanup():
    for jid in _SYNC_JOB_IDS:
        delete_rows("sync_job", id=jid)
    db.execute("DELETE FROM app_setting WHERE key='grade_tag_probe_customer'")
    # E2 自愈（其它测试同款）
    try:
        ss.repair_current_flags()
    except Exception:
        pass


# ============================================================ P0-3

class _SpyRun:
    """打桩 subprocess.run：只记录，不真执行。"""

    def __init__(self):
        self.cmds = []

    def __call__(self, cmd, **kw):
        self.cmds.append([str(c) for c in cmd])

        class _R:
            returncode = 0
        return _R()


class _SpyPopen:
    def __init__(self, pid=424242):
        self.calls = []
        self.pid = pid

    def __call__(self, cmd, **kw):
        self.calls.append([str(c) for c in cmd])

        class _P:
            pass
        p = _P()
        p.pid = self.pid
        return p


class _FakeSubModule:
    """假 subprocess：转发常量，只替换 run / Popen（避免动到全局 subprocess）。"""

    import subprocess as _rs
    DEVNULL = _rs.DEVNULL
    PIPE = _rs.PIPE

    def __init__(self, **attrs):
        self.__dict__.update(attrs)


def test_browser_lifecycle():
    print("\n--- P0-3 浏览器生命周期（不再无差别杀 Chrome）---")
    orig_sub, orig_read, orig_by_profile = (
        wb_bootstrap.subprocess, wb_bootstrap._read_pid, wb_bootstrap._pids_by_profile)
    spy = _SpyRun()
    try:
        wb_bootstrap.subprocess = _FakeSubModule(run=spy)
        wb_bootstrap._read_pid = lambda: 12345
        wb_bootstrap._pids_by_profile = lambda: []
        res = wb_bootstrap.kill_own_chrome(wait_sec=0)
        cmds = spy.cmds
        check("kill_own_chrome 不调用全量 taskkill /IM",
              all("/IM" not in c for c in cmds), str(cmds))
        check("只杀记录在案的 PID（含进程树 /T）",
              any(c[:2] == ["taskkill", "/F"] and "/T" in c and "12345" in c for c in cmds),
              str(cmds))
        check("返回里如实报告杀了哪些", res.get("killed") == [12345] and res.get("count") == 1,
              str(res))

        # 没有自己的实例 → 什么都不做（既不误杀，也不报错）
        spy.cmds.clear()
        wb_bootstrap._read_pid = lambda: None
        res2 = wb_bootstrap.kill_own_chrome(wait_sec=0)
        check("无本项目实例时零动作", res2.get("count") == 0 and not spy.cmds, str(res2))

        # 显式逃生开关：只有 force_all=True 才允许全量
        spy.cmds.clear()
        wb_bootstrap.kill_own_chrome(wait_sec=0, force_all=True)
        check("force_all=True 才执行全量 taskkill（显式逃生）",
              any("/IM" in c and "chrome.exe" in c for c in spy.cmds), str(spy.cmds))

        # launch_chrome 记录 PID（PID 文件写盘）
        popen = _SpyPopen(pid=778899)
        wb_bootstrap.subprocess = _FakeSubModule(Popen=popen)
        wb_bootstrap._read_pid = orig_read        # 还原真实读盘逻辑
        pid_file = wb_bootstrap.PID_FILE
        old_pid_exists = pid_file.exists()
        old_pid_val = pid_file.read_text(encoding="utf-8") if old_pid_exists else None
        try:
            wb_bootstrap.launch_chrome("about:blank")
            check("launch_chrome 把 PID 落盘（供后续精确清理）",
                  wb_bootstrap._read_pid() == 778899,
                  str(wb_bootstrap._read_pid()))
            cmd = popen.calls[0] if popen.calls else []
            check("launch_chrome 用独立 profile + CDP",
                  any(str(x) == f"--user-data-dir={wb_bootstrap.PROFILE}" for x in cmd)
                  and any(str(x).startswith("--remote-debugging-port=") for x in cmd),
                  str(cmd))
        finally:
            if old_pid_exists:
                pid_file.write_text(old_pid_val, encoding="utf-8")
            else:
                try:
                    pid_file.unlink()
                except Exception:
                    pass
    finally:
        wb_bootstrap.subprocess = orig_sub
        wb_bootstrap._read_pid = orig_read
        wb_bootstrap._pids_by_profile = orig_by_profile


# ============================================================ P0-4

class _FakeWeibanSync:
    def __init__(self):
        self.added = []

    def iter_all_tags(self, page_size=200):
        # 一个等级标签都没有 → missing = S/A/B/C/D，必然走「创建」分支
        yield {"tag_group": "其它组", "tag_name": "x", "tag_id": "t_x"}

    def add_customer_tag(self, **kw):
        self.added.append(kw)
        return {"errcode": 0, "errmsg": "ok"}


def test_grade_tag_probe():
    print("\n--- P0-4 建标签载体必须人工配置（不拿真实客户当白手套）---")
    fake = _FakeWeibanSync()
    orig = weiban
    import sys as _s
    _s.modules["weiban"] = fake          # grade_tag 内部 `import weiban` 拿的是这里
    try:
        db.execute("DELETE FROM app_setting WHERE key='grade_tag_probe_customer'")
        res = gt.sync_tags(create_missing=True, dry_run=False)
        check("未配置载体 → 拒绝执行（ok=False）", res.get("ok") is False, str(res))
        check("未配置载体 → 一次接口都没调", fake.added == [], str(fake.added))
        check("拒绝理由说明要人工配置/人工建标签",
              "载体" in str(res.get("reason", "")), str(res.get("reason")))

        # 配置了专用载体 → 用它，且**不是**从库里挑的真实客户
        db.set_setting(gt.PROBE_KEY, "PROBE_STAFF:PROBE_CUST")
        fake.added.clear()
        res2 = gt.sync_tags(create_missing=True, dry_run=False)
        check("配置后按配置的载体创建", res2.get("ok") is True, str(res2)[:200])
        check("载体是配置里的那一对（不是库里的真实客户）",
              fake.added and all(c["staff_id"] == "PROBE_STAFF"
                                 and c["external_user_id"] == "PROBE_CUST"
                                 for c in fake.added), str(fake.added[:1]))
    finally:
        _s.modules["weiban"] = orig
        db.execute("DELETE FROM app_setting WHERE key='grade_tag_probe_customer'")
        db.execute("DELETE FROM app_setting WHERE key='grade_tag_ids'")


# ============================================================ P1-4

def test_sync_cross_process():
    print("\n--- P1-4 同步的跨进程互斥 ---")
    import psycopg  # noqa: F401
    row = db.query_one(
        """INSERT INTO sync_job (kind, status) VALUES ('test','running') RETURNING id""")
    jid = row["id"]
    _SYNC_JOB_IDS.append(jid)
    # 本进程**没有**线程在跑；只有 DB 里一条 running（模拟别的进程/脚本）
    check("别的进程在跑同步 → 本进程也能看到",
          sync_service.service.is_running() is True,
          f"thread={sync_service.service._thread}")

    # 僵尸：超时仍 running → 应被判为失败并放行
    db.execute("UPDATE sync_job SET started_at = now() - interval '999 minutes' WHERE id=%s",
               (jid,))
    check("僵尸 running（超时）→ 不误拦", sync_service.service.is_running() is False)
    st = (db.query_one("SELECT status, error FROM sync_job WHERE id=%s", (jid,)) or {})
    check("僵尸被标为 failed 并写明原因",
          st.get("status") == "failed" and "自动标记" in str(st.get("error")), str(st))


def test_heartbeat_zombie():
    """心跳判死：进程被 kill 后，锁必须在**分钟级**释放，而不是等 4 小时。

    真实场景（实测踩到）：后端带着一个 running 的 `sync_job` 被 kill →
    `is_running()` 一直为真 → 后续所有评分/同步被拦，直到 `scoring_stale_minutes`
    （240 分钟）超时才放行。**4 小时里系统完全不可用**，而没有任何提示。
    """
    print("\n--- P1-4b 心跳判死（不让卡死的锁把系统锁 4 小时）---")
    import scoring_service as ss
    row = db.query_one("INSERT INTO sync_job (kind, status) VALUES ('test','running') "
                       "RETURNING id")
    jid = row["id"]
    _SYNC_JOB_IDS.append(jid)
    try:
        # 刚开始、有心跳 → 视为活着（不能把活任务误判为死，否则会并行两个批量）
        db.execute("UPDATE sync_job SET heartbeat_at = now() WHERE id=%s", (jid,))
        check("★ 心跳新鲜 → 视为在跑（不误判活任务）",
              sync_service.service.is_running() is True)

        # 心跳停在 90 分钟前 → 进程已死
        db.execute("UPDATE sync_job SET heartbeat_at = now() - interval '90 minutes' "
                   "WHERE id=%s", (jid,))
        check("★ 心跳已停 → 立刻放行（不必等 240 分钟）",
              sync_service.service.is_running() is False)
        st = db.query_one("SELECT status, error FROM sync_job WHERE id=%s", (jid,)) or {}
        check("僵尸被标 failed 且原因写明「心跳已停」",
              st.get("status") == "failed" and "心跳" in str(st.get("error")), str(st))

        # 阈值必须大于单会话最坏耗时（否则会误杀活任务 → 破坏互斥）
        hb = db.setting_int("job_heartbeat_stale_minutes", "JOB_HEARTBEAT_STALE_MINUTES",
                            ss.HEARTBEAT_STALE_MINUTES)
        check("★ 心跳阈值 > 单会话最坏耗时（3 次重试 × 120s ≈ 6 分钟，留足余量）",
              hb >= 10, f"{hb} 分钟")
        check("心跳阈值远小于二级兜底（否则等于没缩短）",
              hb < db.setting_int("scoring_stale_minutes", "SCORING_STALE_MINUTES",
                                  ss.STALE_MINUTES), f"{hb} vs {ss.STALE_MINUTES}")

        # 老行（heartbeat_at 为 NULL）不能被永久误判为活 → 退回 started_at 判定
        db.execute("UPDATE sync_job SET heartbeat_at = NULL, status='running', "
                   "started_at = now() WHERE id=%s", (jid,))
        check("无心跳的老行 → 退回 started_at 判定（视为在跑，不误杀）",
              sync_service.service.is_running() is True)
        db.execute("UPDATE sync_job SET started_at = now() - interval '999 minutes' "
                   "WHERE id=%s", (jid,))
        check("无心跳且绝对超时 → 仍能判死（二级兜底生效）",
              sync_service.service.is_running() is False)
    finally:
        delete_rows("sync_job", id=jid)
        if jid in _SYNC_JOB_IDS:
            _SYNC_JOB_IDS.remove(jid)


def test_progress_refreshes_heartbeat():
    """进度更新必须刷新心跳（否则心跳机制形同虚设）。"""
    print("\n--- P1-4c 进度更新刷新心跳 ---")
    import sync_service as _sync
    row = db.query_one("INSERT INTO sync_job (kind, status, heartbeat_at) "
                       "VALUES ('test','running', now() - interval '30 minutes') RETURNING id")
    jid = row["id"]
    _SYNC_JOB_IDS.append(jid)
    try:
        before = db.query_one("SELECT EXTRACT(EPOCH FROM (now()-heartbeat_at)) AS a "
                              "FROM sync_job WHERE id=%s", (jid,))["a"]
        check("前置：心跳已陈旧", before and float(before) > 1500, str(before))
        _sync.service._patch_job(jid, session_done=1)
        after = db.query_one("SELECT EXTRACT(EPOCH FROM (now()-heartbeat_at)) AS a "
                             "FROM sync_job WHERE id=%s", (jid,))["a"]
        check("★ 一次进度更新后心跳被刷新（判死机制才有意义）",
              after is not None and float(after) < 5, str(after))
    finally:
        delete_rows("sync_job", id=jid)
        if jid in _SYNC_JOB_IDS:
            _SYNC_JOB_IDS.remove(jid)


# ============================================================ P2-8

def test_rule_tier_from_table():
    print("\n--- P2-8 引擎规则档位从规则表读 ---")
    base = rules_mod.load_rules()
    orig = None
    for r in base["negative_rules"]:
        if r["rule_id"] == "negative_no_reply_2d":
            orig = r["options"][0]["score"]
    check("规则表里该规则当前档位已知", orig is not None, str(orig))

    mutated = copy.deepcopy(base)
    for r in mutated["negative_rules"]:
        if r["rule_id"] == "negative_no_reply_2d":
            r["options"][0]["score"] = -3
    # 引擎硬指标已下沉到 `rules`（P3：它本就是规则引擎的职责，不是跑批服务的）
    items = rules_mod.engine_negative_items({"no_reply_48h": True, "hours": 50}, mutated)
    check("档位改成 -3 后，产出同步变成 -3（不再硬编码 -2）",
          bool(items) and items[0]["score"] == -3, str(items))
    check("未改动时仍按原档位",
          rules_mod.rule_tier_score(base, "negative_no_reply_2d", -2) == orig, str(orig))


# ============================================================ P2-11

def test_sync_rules_no_wipe():
    print("\n--- P2-11 sync_rules 不清空重写 ---")
    ver = ss.current_rule_version()
    ss.sync_rules(ver)
    before = db.query_one("SELECT count(*) AS n FROM scoring_rule WHERE rule_version=%s",
                          (ver,))["n"]
    check("首次同步后有规则行", before > 0, str(before))

    # 塞一条「本版本里存在、但 rules.json 里没有」的陈旧档位
    db.execute(
        """INSERT INTO scoring_rule (rule_version, rule_id, polarity, dimension, score,
                                     criteria, engine, source_type)
           VALUES (%s,'__STALE_TEST__','negative','x',-1,'stale','rule','json')
           ON CONFLICT DO NOTHING""", (ver,))
    # 再同步一次：过程中不应出现空窗，且陈旧档位被清掉
    ss.sync_rules(ver)
    after = db.query_one("SELECT count(*) AS n FROM scoring_rule WHERE rule_version=%s",
                         (ver,))["n"]
    stale = db.query_one("SELECT count(*) AS n FROM scoring_rule WHERE rule_id='__STALE_TEST__'")["n"]
    check("反复同步后规则行数不减少（说明不是先清后写）", after >= before, f"{before}->{after}")
    check("不在 rules.json 里的陈旧档位被清掉", stale == 0, str(stale))


# ============================================================ P2-13

def test_trace_cache():
    print("\n--- P2-13 trace 开关缓存与失效 ---")
    orig = db.get_setting("trace_enabled")
    try:
        db.set_setting("trace_enabled", "false")
        check("设置为 false 后关闭", trace_mod.enabled() is False)
        # 绕过 set_setting 直改 DB → 缓存应让结果保持旧值（证明缓存生效）
        db.execute("UPDATE app_setting SET value='true' WHERE key='trace_enabled'")
        check("直改 DB 不即时生效（证明有进程内缓存）", trace_mod.enabled() is False)
        # 走 set_setting → 必须立即失效
        db.set_setting("trace_enabled", "true")
        check("经 set_setting 变更后立即生效（缓存被失效）", trace_mod.enabled() is True)
        db.set_setting("trace_enabled", "false")
        check("再改回 false 也立即生效", trace_mod.enabled() is False)
    finally:
        if orig is None:
            db.execute("DELETE FROM app_setting WHERE key='trace_enabled'")
            trace_mod.invalidate_enabled_cache()
        else:
            db.set_setting("trace_enabled", orig)


# ============================================================ P2-12

def test_health_no_dsn():
    print("\n--- P2-12 /api/health 不暴露连接串 ---")
    try:
        import main
        res = main.health()
    except Exception as e:                      # noqa: BLE001
        check("能导入并调用 health", False, f"{type(e).__name__}: {e}")
        return
    check("health 只返回 ok", res.get("ok") is True, str(res))
    check("health 不含 db / 连接串信息",
          "db" not in res and "@" not in str(res), str(res))


# ============================================================ P2-7

def test_unified_http_layer():
    print("\n--- P2-7 add_customer_score 两种模式都走统一 HTTP 层 ---")
    orig_retry, orig_tok = weiban.request_retry, weiban.get_access_token
    calls = []
    try:
        weiban.get_access_token = lambda *a, **k: "TOK"
        weiban.request_retry = lambda *a, **kw: (calls.append((a, kw)) or {"errcode": 0})

        weiban.add_customer_score("S1", external_user_id="C1", score=3,
                                  use_header_token=True)
        weiban.add_customer_score("S1", external_user_id="C1", score=3,
                                  use_header_token=False)
        check("两种模式都经由 request_retry（不再有裸 urlopen 分支）", len(calls) == 2, str(len(calls)))
        hdr = calls[0][1].get("headers") or {}
        check("header 模式把 token 放 header（不进 URL）",
              hdr.get("access_token") == "TOK", str(calls[0][1]))
        check("URL 模式仍用 params 传 token（行为不变）",
              (calls[1][1].get("params") or {}).get("access_token") == "TOK", str(calls[1][1]))
    finally:
        weiban.request_retry = orig_retry
        weiban.get_access_token = orig_tok


def main():
    print("=== 评审缺陷修复回归（P0/P1/P2）===")
    try:
        test_browser_lifecycle()
        test_grade_tag_probe()
        test_sync_cross_process()
        test_heartbeat_zombie()
        test_progress_refreshes_heartbeat()
        test_rule_tier_from_table()
        test_sync_rules_no_wipe()
        test_trace_cache()
        test_health_no_dsn()
        test_unified_http_layer()
    finally:
        _cleanup()
    return summary()

if __name__ == "__main__":
    sys.exit(main())
