# -*- coding: utf-8 -*-
"""续跑长会话批量评分（修正版监控：**监控代码自身崩溃不得终止评分**）。跑完即删。

上一版教训：监控循环里写错一个列名（`session_failed` 在 detail 而非列）→ 主进程 traceback
→ **daemon 线程随主进程退出被杀死** → 评分中断在 1/170。所以监控必须整体 try/except。
"""
import sys
import time

# ⚠️ 这行的作用是**导入即生效的副作用**：`_harness` 在导入时把仓库根与 `backend/`
#    注入 `sys.path`、并把 stdout/stderr 统一成 UTF-8。名字一个都用不到，
#    但**不能删**（删了之后 `import scoring_service` 之类会直接 ImportError）。
import _harness  # noqa: F401,E402

import db                       # noqa: E402
import scoring_service as ss    # noqa: E402

MIN_MSGS = 60
r = ss.service.start(min_messages=MIN_MSGS, max_sessions=300, triggered_by="manual")
print("start:", r, flush=True)
if not r.get("ok"):
    sys.exit(1)
run_id = r["run_id"]
print("run_id = %s | 只跑 msg_count >= %d 的会话" % (run_id, MIN_MSGS), flush=True)

t0 = time.time()
while ss.service._thread and ss.service._thread.is_alive():
    time.sleep(20)
    el = int(time.time() - t0)
    try:
        row = db.query_one(
            """SELECT session_done, session_total, llm_calls, llm_failed,
                      detail->>'stage' AS stage
                 FROM scoring_run WHERE id=%s""", (run_id,)) or {}
        print("  [%3dm%02ds] %s | 完成 %s/%s | llm=%s 失败=%s"
              % (el // 60, el % 60, row.get("stage"), row.get("session_done"),
                 row.get("session_total"), row.get("llm_calls"), row.get("llm_failed")), flush=True)
    except Exception as e:                 # 监控失败绝不影响评分
        print("  [监控警告] %s: %s" % (type(e).__name__, e), flush=True)

try:
    row = db.query_one("SELECT * FROM scoring_run WHERE id=%s", (run_id,)) or {}
    print("\n=== 完成 ===", flush=True)
    print("status=%s elapsed=%s 秒" % (row.get("status"), row.get("elapsed_sec")), flush=True)
    print("候选=%s 完成=%s 跳过=%s 新评估=%s llm=%s 失败=%s 回写ok=%s 复核入队=%s"
          % (row.get("session_total"), row.get("session_done"), row.get("session_skipped"),
             row.get("assessment_new"), row.get("llm_calls"), row.get("llm_failed"),
             row.get("writeback_ok"), row.get("review_new")), flush=True)
    print("\n=== assessment 的 source_run_id 分布 ===", flush=True)
    for x in db.query("""SELECT COALESCE(source_run_id::text,'NULL') AS src, count(*) AS n
                           FROM customer_intent_assessment GROUP BY 1 ORDER BY 2 DESC"""):
        print("  source_run_id=%s → %s 条" % (x["src"], x["n"]), flush=True)
except Exception as e:
    print("[最终统计失败]", e, flush=True)
