"""人证核验状态诊断（**真实 12306**）：把原始响应打出来。

为什么需要它
------------
候补购票要求账号**已完成人证一致性核验**（官方口径：铁路 12306 App →
我的 → 人证核验）。核验没过时，官方会在提交候补时拒绝。

问题在于：**「未核验」和「判不出来」是两件事**。
我们靠 ``initQueryUserInfoApi`` / ``initMy12306Api`` 返回里的字段判断核验状态，
而这两个接口的字段名随版本变化 —— 一旦官方没给明确字段，
正确做法是说「无法判定」，而**不是**武断地说「未核验」。
后者会让人以为账号被 12306 限制了，属于有害的误导。

本脚本把这两个接口的**原始 JSON**打出来，用来回答一个问题：
界面上显示的那个核验状态，到底是官方说的，还是我们猜的？

    python scripts/diagnose_verify.py --restore          # 用磁盘会话恢复后探测
    python scripts/diagnose_verify.py --user 樊章        # 指定账号

只读接口、不改任何东西。输出写到用户目录下的 ``diagnose-verify-*.json``。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_console_safe() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


_make_console_safe()

#: 我们认为「可能与核验有关」的字段名。真机上如果换名，这里能第一眼看出来。
VERIFY_KEYS = (
    "isUserVerified",
    "userVerified",
    "isChecked",
    "verifyStatus",
    "face_flag",
    "verified_flag",
    "checkFlag",
    "realNameCheck",
)


def _keys_of(payload, limit: int = 40) -> str:
    if not isinstance(payload, dict):
        return f"<{type(payload).__name__}>"
    keys = list(payload)
    return ", ".join(keys[:limit]) + (" …" if len(keys) > limit else "")


def _scan_verify_fields(payload, prefix: str = "") -> list[tuple[str, object]]:
    """递归找出所有**名字像核验**的字段，连值一起列出来。"""
    found: list[tuple[str, object]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if key in VERIFY_KEYS or "verif" in str(key).lower() or "checked" in str(key).lower():
                found.append((path, value))
            found.extend(_scan_verify_fields(value, path))
    elif isinstance(payload, list):
        for index, item in enumerate(payload[:3]):
            found.extend(_scan_verify_fields(item, f"{prefix}[{index}]"))
    return found


def _probe(user, path: str, *, method: str, referer: str) -> dict:
    """按指定方法打一次接口，返回原始 JSON（不经过解析层）。"""
    from ticket_helper.inter.base import expect_json

    url = user.client.url(referer)
    if method == "post":
        response = user.client.post(
            path, data={"_json_att": ""}, allow_retry=False, referer=url
        )
    else:
        response = user.client.get(
            path, params={"_json_att": ""}, allow_retry=False, referer=url
        )
    return expect_json(response, allow_empty=True) or {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="人证核验状态诊断（真实 12306）")
    parser.add_argument("--user", default="", help="指定账号名（默认取第一个可用账号）")
    parser.add_argument(
        "--restore", action="store_true", help="先用磁盘上留存的会话恢复登录（不必重新扫码）"
    )
    args = parser.parse_args(argv)

    from ticket_helper.runtime.context import AppContext

    print("正在初始化（读取配置、站点表与会话）…")
    ctx = AppContext()
    try:
        user = ctx.accounts.get(args.user) if args.user else next(iter(ctx.accounts.online_users()), None)
        # 「指定了账号但它当前不在线」是最常见的情况（重启后会话只是存在磁盘上），
        # 所以这里不能只在 user is None 时才恢复 —— 否则 --restore 形同虚设。
        if args.restore and (user is None or not user.is_login):
            candidates = [user] if user is not None else list(ctx.accounts.all())
            for candidate in candidates:
                try:
                    if candidate.restore_session():
                        user = candidate
                        print(f"已用磁盘会话恢复账号「{candidate.username}」的登录态")
                        break
                except Exception as exc:  # noqa: BLE001
                    print(f"  恢复 {candidate.username} 的会话失败：{exc}")
            else:
                if user is not None:
                    print(f"  账号「{user.username}」的磁盘会话已失效，需要重新扫码登录")
        if user is None or not user.is_login:
            print("× 没有可用的已登录账号。先扫码登录，或加 --restore。")
            return 1

        print(f"服务端：{ctx.config.api_base}")
        print(f"账号：{user.username}\n")

        report: dict[str, object] = {"account": user.username, "api_base": ctx.config.api_base}

        # ---- 1) 两个「用户信息」接口的原始响应 ---- #
        endpoints = (
            ("GET", "/otn/modifyUser/initQueryUserInfoApi", "/otn/modifyUser/initQueryUserInfo"),
            ("POST", "/otn/modifyUser/initQueryUserInfoApi", "/otn/modifyUser/initQueryUserInfo"),
            ("POST", "/otn/index/initMy12306Api", "/otn/index/initMy12306"),
        )
        hits: list[tuple[str, object]] = []
        for method, path, referer in endpoints:
            label = f"{method} {path}"
            print("=" * 72)
            print(label)
            try:
                raw = _probe(user, path, method=method.lower(), referer=referer)
            except Exception as exc:  # noqa: BLE001
                print(f"  × 请求失败：{exc}")
                report[label] = {"error": str(exc)}
                continue
            report[label] = raw
            data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
            print(f"  顶层键：{_keys_of(raw)}")
            print(f"  data 键：{_keys_of(data)}")
            found = _scan_verify_fields(raw)
            if found:
                for key_path, value in found:
                    print(f"  ★ 疑似核验字段 {key_path} = {value!r}")
                hits.extend(found)
            else:
                print("  ⚠ 响应里**没有任何**疑似核验的字段")
            # 慢一点，别把突发做大
            time.sleep(0.6)

        # ---- 2) 我们自己的判定 ---- #
        print("=" * 72)
        print("本地判定（detect_verified）")
        user.detect_verified()
        print(f"  is_checked      = {user.is_checked}    （是否已核验）")
        print(f"  verify_known    = {user.verify_known}  （本次判不判得出来）")
        print(f"  verified_checked= {user.verified_checked}（是否真的检测过）")
        if not user.verify_known:
            print("  → 判不出来。界面该显示「无法判定」，**不能**显示「未核验」。")
        elif not user.is_checked:
            print("  → 官方明确说未核验：需要在铁路 12306 App 里完成人证核验。")
        else:
            print("  → 已核验。")
        report["local"] = {
            "is_checked": user.is_checked,
            "verify_known": user.verify_known,
            "verified_checked": user.verified_checked,
        }

        # ---- 3) 接口里到底有没有明确字段 ---- #
        print("=" * 72)
        print("结论")
        if hits:
            print("  接口返回里存在疑似核验字段，判定依据成立：")
            for key_path, value in hits:
                print(f"    {key_path} = {value!r}")
        else:
            print("  接口返回里**没有**核验字段 → 我们**无法**确认核验状态。")
            print("  这种情况下候补引擎不应该拦，应该让官方去判（提交失败时它会给出真实原因）。")
        report["verify_field_hits"] = [{"path": k, "value": v} for k, v in hits]

        target = ctx.paths.home / f"diagnose-verify-{int(time.time())}.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("=" * 72)
        print(f"原始响应已写入：{target}")
        return 0
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    finally:
        try:
            ctx.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
