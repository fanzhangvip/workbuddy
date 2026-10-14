# -*- coding: utf-8 -*-
"""
微伴助手 OpenAPI —— 拉取客户聊天记录

链路（三步）：
  1) POST /open-api/access_token/get        用 corp_id + secret 换取 access_token
  2) GET  /open-api/chat/session/list       拉取某员工的会话列表（客户会话 type=ext_user）
  3) GET  /open-api/chat/message/list/v2    拉取指定会话的消息（按消息时间逆序）

用法示例：
  # 方式一：用 corp_id + secret（推荐，脚本自动换 token）
  python weiban_chat_fetch.py --corp-id YOUR_CORP_ID --secret YOUR_SECRET --staff-id ZengDeWei

  # 方式二：已有 access_token 直接拉
  python weiban_chat_fetch.py --token YOUR_ACCESS_TOKEN --staff-id ZengDeWei

常用可选参数：
  --session-type ext_user     会话类型：ext_user(客户/默认) | room(群聊) | other_staff(同事)
  --start-time 1700000000     消息起始时间（秒级时间戳，含）
  --end-time   1700086400     消息结束时间（秒级时间戳，含）
  --limit 100                 每页条数（消息接口 30-100，默认 30）
  --max-sessions 5            最多拉取前 N 个会话（调试用，默认不限）
  --out chat_records.json     输出 JSON 文件
  --pretty                    控制台按"时间 发送者: 内容"打印文本

说明：
  - 所有时间字段单位为秒。
  - 消息接口不返回总量，列表长度 < limit 即表示没有更多数据。
  - file_url 为聊天文件下载链接，仅 15 分钟内有效。
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://open.weibanzhushou.com"
TIMEOUT = 30


def http_post_json(path, payload):
    """POST JSON，返回解析后的 dict。"""
    url = BASE + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get(path, params):
    """GET，返回解析后的 dict。"""
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_access_token(corp_id, secret):
    """第一步：换取 access_token。"""
    res = http_post_json("/open-api/access_token/get", {"corp_id": corp_id, "secret": secret})
    if res.get("errcode") != 0:
        raise RuntimeError(f"获取 access_token 失败：{res}")
    print(f"[1/3] access_token 获取成功，有效期 {res.get('expires_in')} 秒")
    return res["access_token"]


def list_sessions(token, staff_id, session_type, limit=100):
    """第二步：分页拉取员工会话列表。"""
    sessions, offset = [], 0
    while True:
        res = http_get(
            "/open-api/chat/session/list",
            {"access_token": token, "staff_id": staff_id,
             "type": session_type, "limit": limit, "offset": offset},
        )
        if res.get("errcode") != 0:
            raise RuntimeError(f"获取会话列表失败：{res}")
        batch = res.get("session_list", [])
        sessions.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return sessions


def list_messages(token, staff_id, session_id, limit=30, start_time=None, end_time=None):
    """第三步：分页拉取单个会话的消息（v2，按消息时间逆序，用 offset 翻页）。"""
    messages, offset = [], 0
    while True:
        params = {"access_token": token, "staff_id": staff_id,
                  "session_id": session_id, "limit": limit, "offset": str(offset)}
        if start_time is not None:
            params["start_time"] = start_time
        if end_time is not None:
            params["end_time"] = end_time
        res = http_get("/open-api/chat/message/list/v2", params)
        if res.get("errcode") != 0:
            raise RuntimeError(f"获取会话消息失败(session={session_id})：{res}")
        batch = res.get("message_list", [])
        messages.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return messages


def main():
    ap = argparse.ArgumentParser(description="微伴助手 OpenAPI 拉取客户聊天记录")
    ap.add_argument("--corp-id", help="企业标识 corp_id")
    ap.add_argument("--secret", help="企业 API 调用凭据 secret")
    ap.add_argument("--token", help="已有 access_token（与 corp-id/secret 二选一）")
    ap.add_argument("--staff-id", required=True, help="员工 id（对应企业微信 userid）")
    ap.add_argument("--session-type", default="ext_user",
                    choices=["ext_user", "room", "other_staff"], help="会话类型，默认 ext_user(客户)")
    ap.add_argument("--limit", type=int, default=30, help="每页条数，默认 30（消息接口 30-100）")
    ap.add_argument("--start-time", type=int, help="消息起始时间（秒级时间戳）")
    ap.add_argument("--end-time", type=int, help="消息结束时间（秒级时间戳）")
    ap.add_argument("--max-sessions", type=int, help="最多处理前 N 个会话（调试用）")
    ap.add_argument("--out", default="chat_records.json", help="输出 JSON 文件")
    ap.add_argument("--pretty", action="store_true", help="控制台打印可读文本")
    args = ap.parse_args()

    # 鉴权
    if args.token:
        token = args.token
        print("[1/3] 使用传入的 access_token")
    elif args.corp_id and args.secret:
        token = get_access_token(args.corp_id, args.secret)
    else:
        ap.error("请提供 --token，或同时提供 --corp-id 与 --secret")

    # 会话列表
    sessions = list_sessions(token, args.staff_id, args.session_type, limit=100)
    if args.max_sessions:
        sessions = sessions[: args.max_sessions]
    print(f"[2/3] 共获取会话 {len(sessions)} 个（type={args.session_type}）")

    # 逐会话拉消息
    result = []
    for i, s in enumerate(sessions, 1):
        sid = s.get("id")
        msgs = list_messages(token, args.staff_id, sid, limit=max(args.limit, 30),
                             start_time=args.start_time, end_time=args.end_time)
        result.append({"session": s, "messages": msgs})
        print(f"      ({i}/{len(sessions)}) 会话 {sid} - {s.get('name')} 拉取 {len(msgs)} 条消息")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[3/3] 已写入 {args.out}")

    if args.pretty:
        print("\n===== 聊天记录预览 =====")
        for item in result:
            s = item["session"]
            print(f"\n--- 会话：{s.get('name')}（{s.get('session_type')}）---")
            for m in sorted(item["messages"], key=lambda x: x.get("time", 0)):
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.get("time", 0)))
                who = m.get("from_name") or m.get("from_id")
                print(f"{ts}  {who}: {m.get('text_content', '')}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)
