#!/usr/bin/env python
"""把 12306 账号写入加密存储（一次性初始化脚本）。

用途
----
首次使用时把账号导入系统，避免手输。脚本会：
1. 把账号交给 AppContext，密码用本机密钥文件加密后写入 SQLite；
2. 不在任何输出中回显密码（只显示掩码）。

用法
----
交互式（推荐，密码不回显到 shell 历史）::

    python scripts/seed_account.py

命令行（注意 shell 历史会留痕，仅本机自用场景可接受）::

    python scripts/seed_account.py --username 13800000000 --password 'xxxx'
    python scripts/seed_account.py --from-file C:/path/to/account.txt

``--from-file`` 支持两种格式（自动识别）：
- 每行一条 ``用户名|密码``；
- 形如需求文档里的写法：``12306账号：<user>`` / ``password: <pass>``。

安全说明：账号密码**只写入本机用户目录**（``%USERPROFILE%\\.ticket-helper``），
不会上传、不会打印明文、也不会进版本库。
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ticket_helper.config.settings import UserConfig  # noqa: E402


def _make_console_safe() -> None:
    """中文 Windows 控制台是 GBK 编码，让输出容错，避免特殊符号把脚本打断。"""
    import sys as _sys

    for stream in (_sys.stdout, _sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


_make_console_safe()


def parse_account_file(path: Path) -> list[tuple[str, str]]:
    """从文本文件里解析出 (用户名, 密码) 列表。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    accounts: list[tuple[str, str]] = []

    # 格式一：用户名|密码（每行一条）
    for line in text.splitlines():
        line = line.strip()
        if line and "|" in line and not line.startswith("#"):
            user, _, password = line.partition("|")
            if user.strip() and password.strip():
                accounts.append((user.strip(), password.strip()))
    if accounts:
        return accounts

    # 格式二：文档式（账号：xxx / password: yyy）
    users = re.findall(r"(?:账号|用户名|username)\s*[:：]\s*(\S+)", text, flags=re.IGNORECASE)
    passwords = re.findall(r"(?:password|密码)\s*[:：]\s*(\S+)", text, flags=re.IGNORECASE)
    for user, password in zip(users, passwords):
        accounts.append((user, password))
    return accounts


def mask(value: str) -> str:
    if len(value) <= 2:
        return "*" * len(value)
    return value[0] + "*" * (len(value) - 2) + value[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入 12306 账号（加密存储）")
    parser.add_argument("--username", help="12306 用户名 / 手机号")
    parser.add_argument("--password", help="密码（命令行传入会留在 shell 历史中，注意取舍）")
    parser.add_argument("--from-file", help="从一个文本文件批量导入")
    parser.add_argument("--heartbeat", type=int, default=120, help="心跳间隔秒数，默认 120")
    args = parser.parse_args(argv)

    pairs: list[tuple[str, str]] = []
    if args.from_file:
        path = Path(args.from_file).expanduser()
        if not path.exists():
            print(f"文件不存在：{path}", file=sys.stderr)
            return 1
        pairs = parse_account_file(path)
        if not pairs:
            print("未能从文件中解析出账号，请检查格式（用户名|密码 或 账号：xxx / password: yyy）", file=sys.stderr)
            return 1
    elif args.username:
        password = args.password or getpass.getpass(f"请输入 {args.username} 的 12306 密码（输入不回显）：")
        pairs = [(args.username, password)]
    else:
        username = input("12306 用户名 / 手机号：").strip()
        if not username:
            print("用户名为空，已取消", file=sys.stderr)
            return 1
        password = getpass.getpass("12306 密码（输入不回显）：")
        pairs = [(username, password)]

    from ticket_helper.runtime.context import AppContext

    context = AppContext(start_watchdog=False)
    print(f"数据目录：{context.paths.home}")

    ok = 0
    for username, password in pairs:
        if not username or not password:
            print(f"跳过不完整的一条：{mask(username or '')}")
            continue
        try:
            user = context.upsert_user(UserConfig(username=username, password=password, heartbeat=args.heartbeat))
            stored = context.db.get_user_by_name(username, with_secret=True)
            encrypted = bool(stored and context.cipher.is_encrypted(stored.get("password_enc")))
            print(f"√ 已保存账号 {mask(username)}（id={user.id}，加密存储={'是' if encrypted else '否'})")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"× 保存失败 {mask(username)}：{exc}", file=sys.stderr)

    print(f"\n完成：成功 {ok} / 共 {len(pairs)} 条")
    if ok:
        print("下一步：")
        print("  python main.py doctor        # 自检")
        print("  python main.py web           # 启动后台与网页管理界面")
        print("  python main.py desktop       # 启动桌面客户端")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
