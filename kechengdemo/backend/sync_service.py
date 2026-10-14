# -*- coding: utf-8 -*-
"""同步服务：把「微伴元数据 + 企微明文正文」拉下来落进 PostgreSQL。

链路（全部纯 HTTP）
    ① 微伴 GET  /open-api/staff/list                      → 员工
    ② 微伴 GET  /open-api/chat/session/list               → 会话
    ③ 微伴 GET  /open-api/chat/message/list/v2            → 消息元数据（正文为 null）
    ④ 企微 POST /chatdata/getchatdata                     → 该会话消息的明文正文
    ⑤ 按 msgid(=original_id) 合并 upsert 进 chat_message

增量与断点续传
    ``sync_cursor`` 记录每个会话已同步到的 ``last_msg_time``：
      - 会话的 ``last_msg_time`` 不高于水位 → 直接跳过（一次请求都不发）
      - 拉消息时带 ``start_time`` 只取水位之后的部分
"""
import threading
import time
import traceback

import db

import weiban                # noqa: E402  根目录既有模块
import ww_chatdata           # noqa: E402

PAGE = 100                   # 微伴接口单页条数
SESSION_TYPES = ("ext_user", "room", "other_staff")


# ---------------------------------------------------------------- SQL

SQL_UPSERT_STAFF = """
INSERT INTO staff (staff_id, name, alias, avatar, mobile, position, status,
                   ia_authorized, ma_authorized, oa_authorized, raw,
                   created_at, updated_at, synced_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
ON CONFLICT (staff_id) DO UPDATE SET
  name = EXCLUDED.name, alias = EXCLUDED.alias, avatar = EXCLUDED.avatar,
  mobile = EXCLUDED.mobile, position = EXCLUDED.position, status = EXCLUDED.status,
  ia_authorized = EXCLUDED.ia_authorized, ma_authorized = EXCLUDED.ma_authorized,
  oa_authorized = EXCLUDED.oa_authorized, raw = EXCLUDED.raw,
  updated_at = EXCLUDED.updated_at, synced_at = now()
"""

SQL_UPSERT_SESSION = """
INSERT INTO chat_session (staff_id, session_id, session_type, name, remark, avatar,
                          target_id, agree_msgaudit, last_msg_time, last_msg_thumb,
                          created_at, synced_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
ON CONFLICT (staff_id, session_id) DO UPDATE SET
  session_type = EXCLUDED.session_type, name = EXCLUDED.name, remark = EXCLUDED.remark,
  avatar = EXCLUDED.avatar, target_id = EXCLUDED.target_id,
  agree_msgaudit = EXCLUDED.agree_msgaudit,
  last_msg_time = GREATEST(chat_session.last_msg_time, EXCLUDED.last_msg_time),
  last_msg_thumb = EXCLUDED.last_msg_thumb,
  created_at = COALESCE(chat_session.created_at, EXCLUDED.created_at),
  synced_at = now()
"""

SQL_UPSERT_MESSAGE = """
INSERT INTO chat_message (staff_id, session_id, message_id, msgid, direction, msg_time,
                          msg_type, from_id, to_id, from_name, from_avatar, room_id,
                          file_url, secret_key, content, msgtype, plain, content_state,
                          created_at, updated_at, fetched_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
ON CONFLICT (staff_id, session_id, message_id) DO UPDATE SET
  content = COALESCE(NULLIF(EXCLUDED.content, ''), chat_message.content),
  msgtype = COALESCE(EXCLUDED.msgtype, chat_message.msgtype),
  plain   = COALESCE(EXCLUDED.plain, chat_message.plain),
  secret_key = COALESCE(EXCLUDED.secret_key, chat_message.secret_key),
  content_state = CASE
      WHEN COALESCE(NULLIF(EXCLUDED.content, ''), chat_message.content) IS NOT NULL
      THEN 'ok' ELSE EXCLUDED.content_state END,
  fetched_at = now()
RETURNING (xmax = 0) AS inserted
"""


# ---------------------------------------------------------------- 服务

class SyncService:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = False
        self._job_id = None
        self._progress = {}

    # ---------------- 对外接口 ----------------

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            job_id = self._job_id
        job = db.query_one("SELECT * FROM sync_job WHERE id = %s", (job_id,)) if job_id else None
        if not job:
            job = db.query_one("SELECT * FROM sync_job ORDER BY id DESC LIMIT 1")
        return {"running": running, "stopping": self._stop, "job": job,
                "progress": dict(self._progress)}

    def stop(self) -> dict:
        if not self.is_running():
            return {"ok": False, "reason": "当前没有正在运行的同步任务"}
        self._stop = True
        return {"ok": True, "message": "已发出停止信号，将在当前会话处理完后退出"}

    def start(self, kind: str = "incremental", staff_ids: list = None,
              session_types: list = None, max_sessions_per_staff: int = 200,
              max_msgs_per_session: int = 0, triggered_by: str = "manual",
              ignore_cursor: bool = False) -> dict:
        if self.is_running():
            return {"ok": False, "reason": "已有同步任务在运行，请先停止或等待完成"}
        # 互斥（修 H1）：评分正在跑时不得启动同步 —— 否则评分读到的是「半截会话」
        import scoring_service
        if scoring_service.service.is_running():
            return {"ok": False, "reason": "评分任务进行中，请先等待评分完成再同步（避免评分读到半截数据）"}

        types = [t for t in (session_types or ["ext_user"]) if t in SESSION_TYPES]
        if not types:
            types = ["ext_user"]

        detail = db.as_json({"staff_ids": staff_ids, "session_types": types,
                             "max_sessions_per_staff": max_sessions_per_staff})
        job_id = db.query_one(
            """INSERT INTO sync_job (kind, status, triggered_by, detail)
               VALUES (%s, 'running', %s, %s) RETURNING id""",
            (kind, triggered_by, detail)
        )["id"]

        self._stop = False
        self._job_id = job_id
        self._progress = {"stage": "启动中", "current_staff": None, "current_session": None,
                          "session_total": 0, "session_done": 0, "msg_total": 0,
                          "msg_decrypted": 0, "msg_new": 0}
        self._thread = threading.Thread(
            target=self._run,
            args=(job_id, kind, staff_ids, types, max_sessions_per_staff,
                  max_msgs_per_session, ignore_cursor),
            daemon=True, name=f"sync-{job_id}")
        self._thread.start()
        return {"ok": True, "job_id": job_id}

    # ---------------- 内部实现 ----------------

    def _patch_job(self, job_id, **fields):
        if not fields:
            return
        keys = ", ".join(f"{k} = %s" for k in fields)
        db.execute(f"UPDATE sync_job SET {keys} WHERE id = %s",
                   (*fields.values(), job_id))

    def _finish_job(self, job_id, status: str, error: str = None):
        db.execute(
            "UPDATE sync_job SET status = %s, error = %s, finished_at = now() WHERE id = %s",
            (status, error, job_id))

    def _bump(self, job_id, *, sessions=None, msgs=None, decrypted=None, new=None,
              detail=None):
        with self._lock:
            for k, v in (("session_done", sessions), ("msg_total", msgs),
                         ("msg_decrypted", decrypted), ("msg_new", new)):
                if v:
                    self._progress[k] = self._progress.get(k, 0) + v
            if detail:
                self._progress.update(detail)
            snapshot = dict(self._progress)
        self._patch_job(
            job_id,
            session_done=snapshot.get("session_done", 0),
            msg_total=snapshot.get("msg_total", 0),
            msg_decrypted=snapshot.get("msg_decrypted", 0),
            msg_new=snapshot.get("msg_new", 0),
            detail=db.as_json(snapshot),
        )
        return snapshot

    # ---- 员工 ----
    def _sync_staff(self, job_id) -> dict:
        n = 0
        rows = []
        for st in weiban.iter_all_staff():
            n += 1
            rows.append((
                st.get("id"), st.get("name"), st.get("alias"), st.get("avatar"),
                st.get("mobile"), st.get("position"), st.get("status"),
                st.get("ia_authorized"), st.get("ma_authorized"), st.get("oa_authorized"),
                db.as_json(st), st.get("created_at"), st.get("updated_at"),
            ))
            if len(rows) >= 100:
                db.execute_many(SQL_UPSERT_STAFF, rows)
                rows.clear()
        if rows:
            db.execute_many(SQL_UPSERT_STAFF, rows)
        self._patch_job(job_id, staff_count=n)
        return {"count": n}

    def _staff_id_list(self) -> list:
        return [r["staff_id"] for r in
                db.query("SELECT staff_id FROM staff ORDER BY name NULLS LAST")]

    # ---- 会话 ----
    def _sync_sessions_of(self, staff_id: str, session_types: list,
                          stop_before: int = 0) -> list:
        """拉某员工的会话并落库。

        ``stop_before``：增量模式下传该员工已有的最大水位。会话列表按最近活跃
        降序返回，一旦整页都早于水位即可停止翻页，省掉大量无效请求。
        """
        out = []
        for stype in session_types:
            offset = 0
            while True:
                res = weiban.list_sessions(staff_id, stype, limit=PAGE, offset=offset,
                                           order_by="last_updated_desc")
                batch = res.get("session_list") or []
                if not batch:
                    break
                out.extend(batch)
                offset += PAGE
                if len(batch) < PAGE or offset >= 20000:
                    break
                if stop_before and all((s.get("last_msg_time") or 0) <= stop_before
                                       for s in batch):
                    break
        if out:
            db.execute_many(SQL_UPSERT_SESSION, [(
                staff_id, s["id"], s.get("session_type") or "ext_user",
                s.get("name"), s.get("remark"), s.get("avatar"), s.get("target_id"),
                s.get("agree_msgaudit"), s.get("last_msg_time"), s.get("last_msg_thumb"),
                s.get("created_at"),
            ) for s in out])
        return out

    # ---- 单会话消息 ----
    def _messages_of(self, staff_id, session_id, since: int, cap: int) -> list:
        msgs, offset = [], 0
        while True:
            res = weiban.list_messages(staff_id, session_id, limit=PAGE, offset=offset,
                                       start_time=since or None)
            batch = res.get("message_list") or []
            msgs.extend(batch)
            offset += PAGE
            if len(batch) < PAGE:
                break
            if cap and len(msgs) >= cap:
                break
        if cap:
            msgs = msgs[:cap]
        msgs.sort(key=lambda m: m.get("time") or 0)
        return msgs

    def _persist(self, staff_id, session_id, msgs, plain: dict) -> int:
        """把一批消息 upsert 进库，返回新增条数。"""
        if not msgs:
            return 0
        inserted = 0
        with db.tx() as conn:
            with conn.cursor() as cur:
                for m in msgs:
                    item = plain.get(m.get("original_id"))
                    content = ww_chatdata.plain_text(item) if item else ""
                    if item is None:
                        state = "pending" if m.get("secret_key") else "fail"
                    else:
                        state = "ok" if content else "empty"
                    cur.execute(SQL_UPSERT_MESSAGE, (
                        staff_id, session_id, m.get("id"), m.get("original_id"),
                        m.get("direction"), m.get("time"), m.get("type"),
                        m.get("from_id"), m.get("to_id"), m.get("from_name"),
                        m.get("from_avatar"), m.get("room_id"), m.get("file_url"),
                        m.get("secret_key"), content or None,
                        (item or {}).get("msgtype"),
                        db.as_json(item) if item else None, state,
                        m.get("created_at"), m.get("updated_at"),
                    ))
                    row = cur.fetchone()
                    if row and row.get("inserted"):
                        inserted += 1
        return inserted

    def _refresh_session_stat(self, staff_id, session_id):
        db.execute(
            """UPDATE chat_session SET msg_count = (
                   SELECT count(*) FROM chat_message
                   WHERE staff_id = %s AND session_id = %s)
               WHERE staff_id = %s AND session_id = %s""",
            (staff_id, session_id, staff_id, session_id))

    # ---- 凭据体检 ----
    def _credential_precheck(self) -> dict:
        """同步前凭据体检：有效且够新鲜就跳过，否则自动保活。

        微伴登录态是滑动过期的（每次访问重置为 now+7 天），所以定期保活
        就不会失效。保活本身出问题时不阻塞同步 —— 真正的凭据失效会在
        解密环节抛出明确错误。
        """
        try:
            import wb_keepalive
        except Exception as e:
            return {"ok": True, "code": 0,
                    "message": f"保活模块不可用，跳过预检（{e}）"}
        try:
            res = wb_keepalive.ensure_credentials(source="sync")
            print(f"[sync] 凭据体检：{res.get('message')}")
            return res
        except Exception as e:
            return {"ok": False, "code": 1,
                    "message": f"保活异常：{type(e).__name__}: {e}"}

    # ---- 主流程 ----
    def _run(self, job_id, kind, staff_ids, session_types, max_sessions, max_msgs,
             ignore_cursor):
        t0 = time.time()
        try:
            db.init_db()

            # ---------- 凭据体检：滑动过期的登录态，快过期就自动保活 ----------
            self._bump(job_id, detail={"stage": "凭据体检"})
            pre = self._credential_precheck()
            if pre.get("code") == 3:      # 连凭据文件都没有 → 只能人工扫码
                raise RuntimeError(f"缺少聊天记录凭据，同步中止：{pre.get('message')}")
            if not pre.get("ok"):
                print(f"[sync] 凭据预检未通过：{pre.get('message')}（继续尝试同步）")

            self._bump(job_id, detail={"stage": "同步员工列表"})
            info = self._sync_staff(job_id)

            targets = staff_ids or self._staff_id_list()
            ww = ww_chatdata.WecomChatData(verbose=False)

            # ---------- 第一遍：列出所有待处理会话，便于进度条 ----------
            cur_map = {(r["staff_id"], r["session_id"]): r["last_msg_time"]
                       for r in db.query("SELECT staff_id, session_id, last_msg_time FROM sync_cursor")}
            staff_watermark = {}
            for (st, _se), t in cur_map.items():
                if t > staff_watermark.get(st, 0):
                    staff_watermark[st] = t

            todo = []      # (staff_id, session_dict)
            for i, sid in enumerate(targets, 1):
                if self._stop:
                    break
                self._bump(job_id, detail={"stage": f"读取会话列表 ({i}/{len(targets)})",
                                           "current_staff": sid})
                try:
                    sess = self._sync_sessions_of(
                        sid, session_types, stop_before=staff_watermark.get(sid, 0))
                except Exception as e:
                    print(f"[sync] 员工 {sid} 会话列表失败：{e}")
                    continue
                sess = [s for s in sess if s.get("agree_msgaudit") is not False]
                if max_sessions:
                    sess = sess[:max_sessions]
                todo.extend((sid, s) for s in sess)

            self._patch_job(job_id, session_total=len(todo))
            self._bump(job_id, detail={"stage": "拉取消息与正文",
                                       "session_total": len(todo)})

            # ---------- 第二遍：逐会话拉消息 + 解密 + 落库 ----------
            for idx, (sid, s) in enumerate(todo, 1):
                if self._stop:
                    break
                sess_id = s["id"]
                sname = s.get("name") or "无备注"
                watermark = 0 if ignore_cursor else cur_map.get((sid, sess_id), 0)

                # 增量快路径：会话最后消息没超过水位 → 跳过
                if s.get("last_msg_time") and s["last_msg_time"] <= watermark:
                    self._bump(job_id, sessions=1,
                               detail={"stage": "拉取消息与正文", "skipped": True,
                                       "current_session": sname})
                    continue

                self._bump(job_id, detail={"stage": "拉取消息与正文",
                                           "current_session": sname,
                                           "current_staff": sid,
                                           "session_total": len(todo)})
                try:
                    msgs = self._messages_of(sid, sess_id, watermark, max_msgs)
                except Exception as e:
                    print(f"[sync] 会话 {sess_id} 消息拉取失败：{e}")
                    self._bump(job_id, sessions=1)
                    continue

                if not msgs:
                    self._bump(job_id, sessions=1)
                    continue

                add = self._persist(sid, sess_id, msgs, {})     # 先落元数据
                self._bump(job_id, msgs=len(msgs), new=add)

                # 解密正文
                try:
                    plain = ww.decrypt(msgs)
                except ww_chatdata.WecomChatDataError as e:
                    raise RuntimeError(f"正文解密通道失效：{e}") from None

                self._persist(sid, sess_id, msgs, plain)
                self._bump(job_id, decrypted=len(plain))

                top = max((m.get("time") or 0) for m in msgs)
                db.execute(
                    """INSERT INTO sync_cursor (staff_id, session_id, last_msg_time, last_synced_at)
                       VALUES (%s,%s,%s, now())
                       ON CONFLICT (staff_id, session_id) DO UPDATE
                         SET last_msg_time = GREATEST(sync_cursor.last_msg_time, EXCLUDED.last_msg_time),
                             last_synced_at = now()""",
                    (sid, sess_id, top))
                self._refresh_session_stat(sid, sess_id)
                self._bump(job_id, sessions=1)

            status = "stopped" if self._stop else "success"
            elapsed = time.time() - t0
            self._bump(job_id, detail={"stage": "已完成", "elapsed_sec": round(elapsed, 1)})
            self._finish_job(job_id, status)
            print(f"[sync] job={job_id} {status} 用时 {elapsed:.1f}s")
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[sync] job={job_id} 失败：{e}\n{tb}")
            self._finish_job(job_id, "failed", error=str(e)[:2000])
            self._bump(job_id, detail={"stage": "失败", "error": str(e)})
        finally:
            self._stop = False


service = SyncService()
