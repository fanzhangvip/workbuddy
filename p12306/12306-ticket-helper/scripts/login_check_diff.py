"""登录态检测差分表：**两套实现到底问了什么、凭什么下结论**。

为什么这张表值得单独存在
------------------------
`scripts/kernel_diff.py` 比的是候补请求体、`query_route_diff.py` 比的是
「几次请求打到对的那条」。这一层比的是**判据本身**：

=================  ==============================  =====================  =========
后端               请求                            判活字段               取值要求
=================  ==============================  =====================  =========
``self``           ``POST /otn/login/checkUser``   ``data.flag``          真值
``pjialin``        ``GET  /otn/login/conf``        ``data.is_login``      ``'Y'``
=================  ==============================  =====================  =========

三处差异都**不是风格问题**：

1. **端点不同** —— 两条都自称「检测登录态」，但一个是 `checkUser`、
   一个是 `conf`。切过去等于换了一条官方接口。
2. **判活字段不同** → 而且取值语义不同。这条最危险：上游是
   ``== 'Y'`` 的**字符串比较**；若照抄字段却按 Python 真值判，
   ``is_login = "N"`` 会被判成「活着」（非空字符串为真）。
   本项目曾把「判不出来」当成「还活着」，结果是界面显示在线、之后每个请求都被拒。
3. **Referer 有无不同** —— 上游那条不带。本项目在真机上验证过：
   没逛过页面的会话调这类接口会被 WAF 拒（回 HTML 错误页而不是 4xx）。

用法::

    python scripts/login_check_diff.py

输出是给人看的；它断言的东西在
``tests/test_user_baseline.py`` 与 ``tests/test_kernel_user.py`` 里被钉住。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from ticket_helper.kernel import registry  # noqa: E402
from ticket_helper.kernel.domains.login_check import (  # noqa: E402
    ALIVE,
    DEAD,
    UNKNOWN,
    alive_of,
)

DOMAIN = "login_check"

#: 场景 → (标签, 响应样例, self 期望, pjialin 期望)。
#:
#: **判据是「各自对自己认得的字段给不给得出结论」**，不是「谁对」——
#: 后者只能靠真机。这里要钉住的是两条：
#:
#: 1. 两套**互不认得对方的字段**（各自看对方的响应都是 UNKNOWN）；这意味着
#:    切后端时**必须连响应结构一起换**，光换端点会全部判成「判不出来」；
#: 2. 上游那条在 ``is_login='N'`` 时必须说 DEAD —— 这是它显式给
#:    ``alive_values`` 的理由（见 PROBES 里那条 ★）。
PROBES: tuple[tuple[str, dict, str, str], ...] = (
    ("明确在线（本项目字段）", {"data": {"flag": True}}, ALIVE, UNKNOWN),
    ("明确离线（本项目字段）", {"data": {"flag": False}}, DEAD, UNKNOWN),
    ("明确在线（上游字段）", {"data": {"is_login": "Y"}}, UNKNOWN, ALIVE),
    ("★ 上游字段给了 'N'", {"data": {"is_login": "N"}}, UNKNOWN, DEAD),
    ("字段都不在", {"data": {}}, UNKNOWN, UNKNOWN),
)


def _spec_with(backend: str):
    """指定后端取规格。"""
    previous = os.environ.get(registry.ENV_VAR)
    os.environ[registry.ENV_VAR] = f"{DOMAIN}={backend}"
    registry.reset_cache()
    try:
        return registry.get(DOMAIN).spec()
    finally:
        if previous is None:
            os.environ.pop(registry.ENV_VAR, None)
        else:
            os.environ[registry.ENV_VAR] = previous
        registry.reset_cache()


def _describe(spec) -> None:
    values = "（按 Python 真值）" if spec.alive_values is None else str(list(spec.alive_values))
    print(f"    {spec.name:<10}: {spec.method.upper():4s} {spec.path}")
    print(f"    {'':<10}  请求体={dict(spec.data) or '{}'}  Referer={spec.referer or '（不带）'}")
    print(f"    {'':<10}  判活字段={list(spec.alive_paths)}  取值要求={values}")


def main() -> int:
    self_spec = _spec_with("self")
    up_spec = _spec_with("pjialin")

    print("=" * 78)
    print("登录态检测：两套实现对比")
    print("=" * 78)
    _describe(self_spec)
    print()
    _describe(up_spec)

    diffs: list[str] = []
    problems: list[str] = []
    if self_spec.path != up_spec.path:
        diffs.append(f"端点：{self_spec.path}  vs  {up_spec.path}")
    else:
        problems.append(f"端点居然相同（{self_spec.path}）—— 与切出的上游事实不符")
    if self_spec.method != up_spec.method:
        diffs.append(f"方法：{self_spec.method.upper()}  vs  {up_spec.method.upper()}")
    else:
        problems.append(f"方法居然相同（{self_spec.method}）—— 与切出的上游事实不符")
    if bool(self_spec.referer) != bool(up_spec.referer):
        diffs.append(
            f"Referer：{'带' if self_spec.referer else '不带'}  vs  "
            f"{'带' if up_spec.referer else '不带'}"
        )
    else:
        problems.append("Referer 的有无居然相同 —— 与切出的上游事实不符")
    if self_spec.alive_paths != up_spec.alive_paths:
        diffs.append(f"判活字段：{list(self_spec.alive_paths)}  vs  {list(up_spec.alive_paths)}")
    else:
        problems.append("判活字段居然相同 —— 与切出的上游事实不符")
    if up_spec.alive_values is None:
        problems.append(
            "★ 上游规格没给 alive_values —— 会把 is_login='N' 判成「活着」（PY 真值语义）"
        )

    print("\n" + "-" * 78)
    print("同一份响应喂给两套规格，各自下什么结论：")
    print(f"    {'场景':<22} {'self':<10} {'pjialin':<10} 含义")
    verdicts: dict[str, tuple[str, str]] = {}
    for label, payload, want_self, want_up in PROBES:
        mine = alive_of(payload, self_spec)
        theirs = alive_of(payload, up_spec)
        verdicts[label] = (mine, theirs)
        ok = mine == want_self and theirs == want_up
        note = "" if ok else f"期望 self={want_self} pjialin={want_up}"
        print(f"    {'✔' if ok else '✘'} {label:<20} {mine:<10} {theirs:<10} {note}")
        if not ok:
            problems.append(f"{label}: 实际 self={mine} / pjialin={theirs}")

    # 「互不认得对方字段」是**结构性事实**，单独断言：
    mine_on_up, theirs_on_self = verdicts["明确在线（上游字段）"][0], verdicts["明确在线（本项目字段）"][1]
    if (mine_on_up, theirs_on_self) != (UNKNOWN, UNKNOWN):
        problems.append("两套实现的字段应当互不认得 —— 否则说明有一边偷偷认了对方的字段")

    print("\n" + "-" * 78)
    if diffs:
        print(f"✔ 差异共 {len(diffs)} 处，与文档一致：")
        for item in diffs:
            print(f"    - {item}")
        print("    ⚠️ 判活字段**互不认得**：切后端必须连响应结构一起换，")
        print("       只换端点会让两边都判成「判不出来（UNKNOWN）」。")
    else:
        print("✘ 没发现任何差异 —— 这本身就是问题（说明有一边被改成了另一边）")
        problems.append("没有差异")

    if problems:
        print("\n✘ 与预期不符：")
        for item in problems:
            print(f"    - {item}")
        return 1

    print("")
    print("⚠️  这张表**只说明差在哪**，不说明谁对：")
    print("   - 「哪条端点在真机上真的可用」需要真机证据；")
    print("   - 上游那条不带 Referer，真机上很可能被 WAF 拒（回 HTML 错误页而不是 4xx），")
    print("     而那正是本项目当初把判定改成「回 HTML 一律判失效」的原因。")
    print("   ⇒ 默认保持 self，见 tests/test_kernel_user.py 的最后一条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
