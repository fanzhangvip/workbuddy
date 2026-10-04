"""扫一遍 Clash 节点，找出哪些能过 YouTube 的机器人校验。

原理：YouTube 的「确认你不是机器人」是**按出口 IP 的声誉**判的，跟客户端参数无关
（实测 13 个播放器接口、三种 JS 运行时、浏览器 UA 全部被拒）。所以你订阅里不同的
节点，结论可能完全不同——这个脚本就是帮你把能用的挑出来。

做法：通过 Clash Verge 的命名管道控制接口，把承载 YouTube 流量的那个分组逐个切到
候选节点，实测「出口 IP 归属」+「YouTube 是否放行」，最后**一定会还原原选择**。

用法：
    ./.venv/Scripts/python.exe tools/yt_node_scan.py                # 扫全部节点
    ./.venv/Scripts/python.exe tools/yt_node_scan.py --limit 12     # 只扫前 12 个
    ./.venv/Scripts/python.exe tools/yt_node_scan.py --group 顶级机场
    ./.venv/Scripts/python.exe tools/yt_node_scan.py --only "洛杉矶|日本"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests

# ---------------------------------------------------------------- Clash 控制接口
#: Clash Verge Rev 的配置目录（Windows）
_VERGE_DIRS = (
    Path(os.environ.get("APPDATA", "")) / "io.github.clash-verge-rev.clash-verge-rev",
    Path(os.environ.get("APPDATA", "")) / "clash-verge",
    Path(os.environ.get("LOCALAPPDATA", "")) / "io.github.clash-verge-rev.clash-verge-rev",
)


def _verge_config() -> Path | None:
    for d in _VERGE_DIRS:
        if d and d.is_dir():
            return d
    return None


def find_pipe() -> str:
    """Clash Verge 把 external-controller 置空，只留命名管道。"""
    d = _verge_config()
    if not d:
        return ""
    for f in ("clash-verge.yaml", "config.yaml"):
        p = d / f
        if not p.exists():
            continue
        m = re.search(r"(?m)^\s*external-controller-pipe:\s*(\S.*?)\s*$",
                      p.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1).strip()
    return ""


def find_mixed_port() -> str:
    d = _verge_config()
    if d:
        for f in ("clash-verge.yaml", "config.yaml"):
            p = d / f
            if not p.exists():
                continue
            m = re.search(r"(?m)^\s*mixed-port:\s*(\d+)",
                          p.read_text(encoding="utf-8", errors="replace"))
            if m:
                return f"http://127.0.0.1:{m.group(1)}"
    return "http://127.0.0.1:7897"


def _dechunk(body: bytes) -> bytes:
    out = b""
    while body:
        line, _, rest = body.partition(b"\r\n")
        try:
            size = int(line.split(b";")[0], 16)
        except ValueError:
            return out + body
        if size == 0:
            return out
        out += rest[:size]
        body = rest[size + 2:]
    return out


def _api(path: str, method: str = "GET", payload: dict | None = None,
         pipe: str = "") -> tuple[int, bytes]:
    pipe = pipe or find_pipe()
    data = b""
    headers = [f"{method} {path} HTTP/1.1", "Host: localhost",
               "Authorization: Bearer set-your-secret", "Connection: close"]
    if payload is not None:
        data = json.dumps(payload).encode()
        headers += ["Content-Type: application/json", f"Content-Length: {len(data)}"]
    with open(pipe, "r+b", buffering=0) as h:
        h.write(("\r\n".join(headers) + "\r\n\r\n").encode() + data)
        raw = b""
        while True:
            chunk = h.read(65536)
            if not chunk:
                break
            raw += chunk
    head, _, body = raw.partition(b"\r\n\r\n")
    text = head.decode("latin-1", errors="replace")
    status = int(text.split(" ", 2)[1]) if " " in text else 0
    if "transfer-encoding: chunked" in text.lower():
        body = _dechunk(body)
    return status, body


def get_json(path: str) -> dict:
    status, body = _api(path)
    if status != 200:
        raise RuntimeError(f"{path} -> HTTP {status}")
    return json.loads(body.decode("utf-8", errors="replace"))


# ---------------------------------------------------------------- 扫描
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
TEST_VIDEO = "https://www.youtube.com/watch?v=n1x1wSyVBB8"

#: 订阅里夹带的信息条（不是真节点）
_FAKE = ("剩余流量", "距离下次重置", "套餐到期", "官网", "订阅", "到期时间", "重置")
_GROUP_TYPES = ("Selector", "URLTest", "Fallback", "LoadBalance", "Relay")


def choose_group(preferred: str = "") -> tuple[str, str, list[str]]:
    proxies = get_json("/proxies")["proxies"]
    group = preferred
    if not group:
        for rule in get_json("/rules")["rules"]:
            if "youtube" in str(rule.get("payload", "")).lower():
                group = str(rule["proxy"])
                break
    group = group or "GLOBAL"
    info = proxies.get(group)
    if not info:
        raise SystemExit(f"找不到分组 {group!r}")
    return group, info.get("now", ""), list(info.get("all", []))


def set_node(group: str, node: str) -> bool:
    path = "/proxies/" + urllib.parse.quote(group, safe="")
    status, _ = _api(path, method="PUT", payload={"name": node})
    return status in (200, 204)


def main() -> int:
    ap = argparse.ArgumentParser(description="找出能过 YouTube 反爬的 Clash 节点")
    ap.add_argument("--group", default="", help="指定承载 YouTube 的分组名")
    ap.add_argument("--limit", type=int, default=0, help="最多测几个节点（0=全部）")
    ap.add_argument("--only", default="", help="只测名称匹配这个正则的节点")
    ap.add_argument("--delay", type=float, default=0.7, help="切换节点后的等待秒数")
    ap.add_argument("--keep", type=int, default=3, help="找到几个就停（0=不提前停）")
    args = ap.parse_args()

    if not find_pipe():
        print("没找到 Clash Verge 的控制管道（或没在用 Clash Verge），无法自动切换节点。")
        print("可以自己在 Clash 里换节点，然后跑 tools/net_check.py 验证。")
        return 2

    mixed = find_mixed_port()
    group, original, members = choose_group(args.group)
    proxies = get_json("/proxies")["proxies"]
    groups = {k for k, v in proxies.items() if v.get("type") in _GROUP_TYPES}

    candidates = [
        n for n in members
        if n not in groups and n.upper() not in ("DIRECT", "REJECT", "PASS")
        and not any(f in n for f in _FAKE)
        and (not args.only or re.search(args.only, n))
    ]
    if args.limit:
        candidates = candidates[:args.limit]

    print(f"代理入口  : {mixed}")
    print(f"分组      : {group}")
    print(f"当前选择  : {original}")
    print(f"候选节点  : {len(candidates)} 个")
    print()

    px = {"http": mixed, "https": mixed}
    good: list[tuple[str, str]] = []
    try:
        for i, node in enumerate(candidates, 1):
            if not set_node(group, node):
                print(f"  [{i:2d}/{len(candidates)}] {node:34s} 切换失败")
                continue
            time.sleep(args.delay)
            try:
                ip = requests.get(
                    "http://ip-api.com/json/?fields=country,isp,hosting,proxy",
                    proxies=px, timeout=12).json()
            except Exception as exc:  # noqa: BLE001
                print(f"  [{i:2d}/{len(candidates)}] {node:34s} 出口探测失败: "
                      f"{type(exc).__name__}")
                continue
            where = f"{ip.get('country','?')} {str(ip.get('isp',''))[:16]}"
            kind = "机房" if ip.get("hosting") else "住宅"
            try:
                r = requests.get(TEST_VIDEO, headers={"User-Agent": UA,
                                                      "Accept-Language": "en-US,en;q=0.9"},
                                 proxies=px, timeout=20)
                text = r.text
                if "not a bot" in text or "confirm you" in text.lower():
                    ok, why = False, "机器人校验"
                elif '"status":"LOGIN_REQUIRED"' in text:
                    ok, why = False, "要求登录"
                else:
                    m = re.search(r'"videoDetails":\{"videoId":"[^"]*","title":"([^"]{0,50})', text)
                    ok, why = (bool(m), f"放行 · {m.group(1)}" if m else "无播放数据")
            except Exception as exc:  # noqa: BLE001
                ok, why = False, f"连接失败 {type(exc).__name__}"
            print(f"  [{i:2d}/{len(candidates)}] {'✅' if ok else '❌'} {node:34s} "
                  f"{where:26s} [{kind}] {why}")
            if ok:
                good.append((node, f"{where} · {kind}"))
            if args.keep and len(good) >= args.keep:
                print("\n已找到足够的可用节点，提前结束。")
                break
    finally:
        set_node(group, original)
        print(f"\n已还原选择 -> {original}")

    print()
    print("=" * 70)
    if good:
        print("能过 YouTube 的节点：")
        for node, where in good:
            print(f"  · {node}   （{where}）")
        print()
        print(f"在 Clash Verge 里把「{group}」切到上面任意一个节点即可，然后重试解析。")
        print("建议固定选一个节点、别留在「自动选择」——自动测速挑的是最快的，")
        print("而最快的那台往往正是被 YouTube 标记过的机房机。")
    else:
        print("这批节点没有一个能过 YouTube。可能的原因：")
        print("  · 订阅里全是机房 IP —— YouTube 对机房 IP 一律要求验证（换客户端参数没用）；")
        print("  · 或者需要登录态：在浏览器登录 YouTube 后，导出 cookies.txt 让程序带上。")
    print("=" * 70)
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
