# -*- coding: utf-8 -*-
"""指定员工的聊天记录全量导出（微伴 OpenAPI 取元数据 + 企微接口取明文正文）。

数据链路
--------------------------------
  ① 员工 / 会话 / 消息元数据  ← 微伴 OpenAPI（快，纯 HTTP）
  ② 消息明文正文              ← 企微 getchatdata（见 ww_chatdata.py）
  ③ 按 ``original_id == msgid`` 精确合并 → Markdown + JSONL

用法示例
--------------------------------
    # 先看一眼某员工有哪些会话
    python wb_staff_export.py --staff "智泊AI大模型-lucky老师" --list-sessions

    # 导出最近 20 个客户会话（每个会话最多 200 条）
    python wb_staff_export.py --staff "智泊AI-俏俏班主任老师" --sessions 20 --msgs 200

    # 只导出 2026-09-01 之后的消息
    python wb_staff_export.py --staff 俏俏 --since 2026-09-01

    # 指定输出目录
    python wb_staff_export.py --staff 俏俏 --out "D:/导出"

输出结构
--------------------------------
    <out>/<员工名>/
        ├── 汇总.md            # 索引 + 统计
        ├── 消息明细.jsonl     # 每条消息一行 JSON（供后续打分/落库）
        └── sessions/
            ├── 01_客户昵称.md
            └── ...
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

import weiban                                     # noqa: E402
import ww_chatdata                                # noqa: E402
import ctx_preprocess                             # noqa: E402  归一化入口（统一「消息 → 文本」）

MSG_TYPE_CN = {
    "text": "文本", "image": "图片", "emotion": "表情", "voice": "语音",
    "video": "视频", "file": "文件", "link": "链接", "miniprogram": "小程序",
    "location": "位置", "card": "名片", "merge_forward": "合并转发",
    "chatrecord": "聊天记录", "docmsg": "文档", "markdown": "markdown",
    "news": "图文", "meeting": "会议", "calendar": "日程", "mixed": "混合",
}

BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_name(s: str, fallback: str = "未命名", limit: int = 40) -> str:
    s = BAD_CHARS.sub("_", (s or "").strip()) or fallback
    s = re.sub(r"\s+", " ", s)
    return s[:limit]


def resolve_staff(key: str):
    """按 staff_ext_id 或姓名（精确/包含）定位员工。"""
    key = (key or "").strip()
    if key.startswith("wo") and len(key) > 20:
        for st in weiban.iter_all_staff():
            if st.get("id") == key:
                return st
        return {"id": key, "name": key}

    exact, fuzzy = [], []
    for st in weiban.iter_all_staff():
        name = (st.get("name") or "").strip()
        if name == key:
            exact.append(st)
        elif key and key in name:
            fuzzy.append(st)
    if exact:
        return exact[0]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        print(f"⚠️ 匹配到 {len(fuzzy)} 名员工，请用更精确的名字或直接给 staff_ext_id：")
        for st in fuzzy[:20]:
            print(f"   {st['id']}  {st.get('name')}")
        sys.exit(2)
    raise SystemExit(f"❌ 未找到员工：{key}")


def fmt_ts(ts) -> str:
    if not ts:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except Exception:
        return "-"


def to_date_arg(s: str):
    """'2026-09-01' -> 秒级时间戳"""
    if not s:
        return None
    for f in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(time.mktime(time.strptime(s, f)))
        except ValueError:
            continue
    raise SystemExit(f"❌ 时间格式无法识别：{s}（示例 2026-09-01）")


def main():
    ap = argparse.ArgumentParser(description="指定员工聊天记录全量导出")
    ap.add_argument("--staff", required=True, help="员工姓名或 staff_ext_id")
    ap.add_argument("--sessions", type=int, default=0,
                    help="最多导出多少个会话（0=全部）")
    ap.add_argument("--msgs", type=int, default=0,
                    help="每个会话最多导出多少条消息（0=全部）")
    ap.add_argument("--since", help="只导出该日期之后的消息，如 2026-09-01")
    ap.add_argument("--session-type", default="ext_user",
                    choices=["ext_user", "room", "other_staff"],
                    help="会话类型：客户/群聊/同事")
    ap.add_argument("--out", default=str(ROOT / "导出"), help="输出根目录")
    ap.add_argument("--list-sessions", action="store_true", help="只列出会话不导出")
    ap.add_argument("--no-resume", action="store_true", help="忽略断点，重新导出")
    args = ap.parse_args()

    since = to_date_arg(args.since)

    # ---------- 员工 ----------
    staff = resolve_staff(args.staff)
    staff_id, staff_name = staff["id"], staff.get("name") or staff["id"]
    print(f"员工：{staff_name}\n      {staff_id}")

    # ---------- 会话 ----------
    print("拉取会话列表…")
    sessions = list(weiban.list_sessions(
        staff_id, args.session_type, limit=100, offset=0,
        order_by="last_updated_desc").get("session_list") or [])

    # 继续翻页
    offset = 100
    while True:
        batch = weiban.list_sessions(staff_id, args.session_type, limit=100,
                                     offset=offset,
                                     order_by="last_updated_desc").get("session_list") or []
        if not batch:
            break
        sessions.extend(batch)
        offset += 100
        if args.sessions and len(sessions) >= args.sessions:
            break
        if len(batch) < 100:
            break

    if args.sessions:
        sessions = sessions[:args.sessions]
    print(f"会话数：{len(sessions)}")

    if args.list_sessions:
        print()
        for i, s in enumerate(sessions, 1):
            print(f"{i:>4}  {fmt_ts(s.get('last_msg_time'))}  "
                  f"{safe_name(s.get('name'), '无备注')}  {s.get('id')}")
        return 0

    if not sessions:
        print("没有可导出的会话。")
        return 0

    # ---------- 输出目录 ----------
    out_root = Path(args.out) / safe_name(staff_name, staff_id[:12], 60)
    sess_dir = out_root / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    state_file = out_root / ".state.json"
    done = {}
    if state_file.exists() and not args.no_resume:
        try:
            done = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            done = {}

    # ---------- 解密客户端 ----------
    print("初始化企微正文解密通道…")
    ww = ww_chatdata.WecomChatData()

    jsonl_f = (out_root / "消息明细.jsonl").open("a", encoding="utf-8")
    index, total_msgs, total_hit = [], 0, 0

    try:
        for idx, sess in enumerate(sessions, 1):
            sid = sess.get("id")
            sname = safe_name(sess.get("name"), "无备注")
            tag = f"{idx:02d}_{sname}"

            if str(sid) in done and not args.no_resume:
                print(f"[{idx}/{len(sessions)}] {sname} —— 已完成，跳过")
                index.append(done[str(sid)])
                continue

            print(f"[{idx}/{len(sessions)}] {sname} …", end="", flush=True)

            # 1) 元数据
            msgs = []
            off = 0
            while True:
                res = weiban.list_messages(staff_id, sid, limit=100, offset=off,
                                           start_time=since)
                batch = res.get("message_list") or []
                msgs.extend(batch)
                off += 100
                if not batch or len(batch) < 100:
                    break
                if args.msgs and len(msgs) >= args.msgs:
                    break
            if args.msgs:
                msgs = msgs[:args.msgs]
            msgs.sort(key=lambda m: m.get("time") or 0)   # 时间正序

            # 2) 正文
            plain = {}
            if msgs:
                try:
                    plain = ww.decrypt(msgs)
                except ww_chatdata.WecomChatDataError as e:
                    print(f"\n❌ 正文解密失败：{e}")
                    break

            # 3) 合并写出
            lines = [f"# {staff_name} ↔ {sess.get('name') or '（无备注）'}", "",
                     f"> 会话 ID：`{sid}`　消息 {len(msgs)} 条，"
                     f"正文命中 {len(plain)} 条。", ""]
            for m in msgs:
                ts = fmt_ts(m.get("time"))
                who = "员工" if m.get("direction") == "send" else "客户"
                item = plain.get(m.get("original_id"))
                # 归一化走预处理层：raw_text 保留原文（含 URL），与旧逻辑一致
                c = ctx_preprocess.classify({
                    "plain": item, "direction": m.get("direction"), "msg_time": m.get("time")})
                body = c["raw_text"]
                mt = c["type_name"] if item else \
                    MSG_TYPE_CN.get(m.get("type"), m.get("type") or "?")
                if not body:
                    body = "〔正文未取到〕"
                lines.append(f"**{ts} {who}**（{mt}）：{body}")
                lines.append("")

                jsonl_f.write(json.dumps({
                    "staff_id": staff_id,
                    "staff_name": staff_name,
                    "session_id": sid,
                    "session_name": sess.get("name"),
                    "msg_id": m.get("original_id"),
                    "direction": m.get("direction"),
                    "time": m.get("time"),
                    "time_str": ts,
                    "type": m.get("type"),
                    "msgtype": (item or {}).get("msgtype"),
                    "from_name": m.get("from_name"),
                    "content": body,
                }, ensure_ascii=False) + "\n")

            (sess_dir / f"{tag}.md").write_text("\n".join(lines), encoding="utf-8")
            total_msgs += len(msgs)
            total_hit += len(plain)
            print(f" {len(msgs)} 条 / 正文 {len(plain)} 条")

            entry = {"idx": idx, "session_id": sid, "session_name": sess.get("name"),
                     "last_msg_time": sess.get("last_msg_time"),
                     "msgs": len(msgs), "hits": len(plain), "file": f"{tag}.md"}
            index.append(entry)
            done[str(sid)] = entry
            state_file.write_text(json.dumps(done, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    finally:
        jsonl_f.close()

    # ---------- 汇总 ----------
    summary = [f"# {staff_name} —— 聊天记录导出汇总", "",
               f"- 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
               f"- 会话数：{len(index)}",
               f"- 消息总数：{total_msgs}",
               f"- 正文命中：{total_hit}",
               f"- 会话类型：{args.session_type}",
               ""]
    summary += ["| # | 最后消息时间 | 会话 | 消息数 | 正文 |", "|---|---|---|---|---|"]
    for e in index:
        summary.append(f"| {e['idx']} | {fmt_ts(e.get('last_msg_time'))} | "
                       f"{safe_name(e.get('session_name'), '无备注')} | "
                       f"{e['msgs']} | {e['hits']} |")
    (out_root / "汇总.md").write_text("\n".join(summary), encoding="utf-8")

    print(f"\n✅ 完成：{out_root}")
    print(f"   会话 {len(index)} 个，消息 {total_msgs} 条，正文命中 {total_hit} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
