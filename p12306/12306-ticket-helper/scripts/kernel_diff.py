"""打印两条内核后端的**请求体差异表** —— 这是判定「换内核到底换了什么」的 A 级外部信号。

为什么需要它
------------
候补链路有 4 处分歧（F1–F4，见 ``docs/内核替换对照报告.md``），
**离线无法判定谁对**。既然判不了对错，就至少要让「差在哪」变成一件
**可复跑、可断言、写在纸上**的事 —— 否则「我切到上游内核了」
只是一句无法查证的声明。

用它
----
::

    python scripts/kernel_diff.py            # 打印差异表；差异与文档不符则退出码非 0

脚本与 ``tests/test_kernel_facade.py`` **共用** :data:`DOCUMENTED_DIVERGENCE` ——
只有一处定义，不允许两边各写一遍（否则文档漂移了也没人报错）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from ticket_helper.kernel.base import build_jz_param  # noqa: E402
from ticket_helper.kernel.self_builder import (  # noqa: E402
    SelfStandbyPayloadBuilder,
)
from ticket_helper.kernel.testerSunshine_builder import (  # noqa: E402
    TesterSunshineStandbyPayloadBuilder,
)

#: **文档里写明的**分歧集合。实际差异若多出或少掉任何一项，本脚本退出码非 0。
#: 键是「差异的方面」，值是对照报告里的编号。
DOCUMENTED_DIVERGENCE: dict[str, str] = {
    "passengerInfo": "F3（段数 6 → 5）",
    "jzParam": "F2（空串 → ④ 返回的日期）",
    "hbTrain": "F1（本地车次号 → ② 返回的官方车次号）",
    "confirmHB 字段集合": "F4（10 个 → 4 个）",
}

#: 差分用的固定输入。**故意让两个车次号不相等**，否则 F1 会被掩盖成「看不出来」。
SAMPLE_PASSENGERS: list[dict[str, Any]] = [
    {
        "passenger_name": "张三",
        "passenger_id_no": "44010119900101****",
        "passenger_type": "1",
        "passenger_id_type_code": "1",
        "allEncStr": "bf6ae40d3655ae7eff005ee21d95876b38ab97a8031b464bc2f74a067e3ec957",
        "born_date": "1990-01-01",
    }
]
SAMPLE_LOCAL_TRAIN_NO = "G6102"          # 本地推算（明文车次）
SAMPLE_SERVER_TRAIN_NO = "5l000G61020A"  # 官方返回（内部号）—— 两者不等，F1 才看得见
SAMPLE_SEAT_CODE = "O"           # 官方席别**编码**
SAMPLE_SEAT_NAME = "二等座"       # 官方席别**名**（上游按名字查它自己的码表）
#: 后端名常量（唯一下发点，别处不许硬写字符串）
SELF = SelfStandbyPayloadBuilder.name
UP = TesterSunshineStandbyPayloadBuilder.name

SAMPLE_JZ_DATE = "2026-10-01"
SAMPLE_JZ_HOUR = "19:00"


def build_both() -> dict[str, dict[str, Any]]:
    """用同一组输入，跑两条后端，返回 ``{后端名: {"passenger_info":…, "params":…}}``。"""
    jz_param = build_jz_param(SAMPLE_JZ_DATE, SAMPLE_JZ_HOUR)
    out: dict[str, dict[str, Any]] = {}
    for builder in (SelfStandbyPayloadBuilder(), TesterSunshineStandbyPayloadBuilder()):
        info = builder.build_passenger_info(SAMPLE_PASSENGERS)
        params = builder.confirm_hb_params(
            passenger_info=info,
            jz_param=jz_param,
            train_no=SAMPLE_LOCAL_TRAIN_NO,
            seat_code=SAMPLE_SEAT_CODE,
            seat_name=SAMPLE_SEAT_NAME,
            server_train_no=SAMPLE_SERVER_TRAIN_NO,
        )
        out[builder.name] = {"passenger_info": info, "params": params}
    return out


def diff_payloads(both: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """算出实际差异，按「方面」返回。

    只比较**同名键的值**与**键集合**；键顺序不在比较范围
    （表单是 key=value，顺序不影响语义，比它只会制造噪声）。
    """
    mine, theirs = both[SELF], both[UP]
    mp, tp = mine["params"], theirs["params"]
    diff: dict[str, dict[str, Any]] = {}

    if mine["passenger_info"] != theirs["passenger_info"]:
        diff["passengerInfo"] = {
            SELF: f"{mine['passenger_info'].count('#') // len(SAMPLE_PASSENGERS) + 1} 段/人",
            UP: f"{theirs['passenger_info'].count('#') // len(SAMPLE_PASSENGERS) + 1} 段/人",
            "detail_self": mine["passenger_info"],
            "detail_upstream": theirs["passenger_info"],
        }

    for key in ("jzParam", "hbTrain"):
        if mp.get(key) != tp.get(key):
            diff[key] = {SELF: mp.get(key), UP: tp.get(key)}

    only_self = sorted(set(mp) - set(tp))
    only_up = sorted(set(tp) - set(mp))
    if only_self or only_up:
        diff["confirmHB 字段集合"] = {
            SELF: f"{len(mp)} 个：{', '.join(sorted(mp))}",
            UP: f"{len(tp)} 个：{', '.join(sorted(tp))}",
            f"只在 {SELF}": only_self,
            f"只在上游有": only_up,
        }
    return diff


def main() -> int:
    both = build_both()
    diff = diff_payloads(both)

    print("=" * 78)
    print("内核后端请求体差异表（候补 confirmHB / passengerInfo）")
    print("=" * 78)
    for name, why in DOCUMENTED_DIVERGENCE.items():
        got = diff.get(name)
        mark = "✔ 有差异" if got else "✘ 无差异（文档说有，实现里没有）"
        print(f"\n[{name}]  {why}\n  {mark}")
        if got:
            print(f"    {SELF:<12}: {got[SELF]}")
            print(f"    {UP:<12}: {got[UP]}")
            if f"只在 {SELF}" in got:
                print(f"    只在 {SELF} 有: {got[f'只在 {SELF}']}")
                print(f"    只在上游有  : {got['只在上游有']}")

    actual = set(diff)
    documented = set(DOCUMENTED_DIVERGENCE)
    print("\n" + "-" * 78)
    if actual == documented:
        print(f"✔ 实际差异与文档一致，共 {len(actual)} 项：{', '.join(sorted(actual))}")
        print("\n⚠️  提醒：本表只说明「差在哪」，**不说明谁对**。")
        print("   谁对只能由真机信号判定（对照报告 §4.3 的 B 级信号）。")
        return 0

    print("✘ 实际差异与文档不符：")
    print(f"    文档有但实现没有: {sorted(documented - actual)}")
    print(f"    实现有但文档没写: {sorted(actual - documented)}")
    print("    → 要么改实现，要么改 DOCUMENTED_DIVERGENCE（并同步对照报告）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
