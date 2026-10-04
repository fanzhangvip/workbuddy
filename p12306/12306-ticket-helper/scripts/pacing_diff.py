"""节奏/参数差分表（A15 心跳 + A17 限速）：**两套实现的数字差在哪**。

为什么这张表值得存在
--------------------
这一期的差异**全是数字**，而且**都不会报错**：

- 上游查询节奏 ``0.5~1.0 秒``，本项目 ``1~8 秒 + 自适应`` —— 切过去只是**更容易被限流**；
- 上游心跳 ``120 秒``**没有地板**，本项目有 ``30 秒``地板 —— 切过去是**去掉一层防护**；
- 上游请求超时 ``5 秒``，本项目 ``12 秒`` —— 切过去只是**更容易超时重试**。

三件事在日志里都不可见，只能靠一张表把它们摆出来。

用法::

    python scripts/pacing_diff.py

断言侧：``tests/test_kernel_pacing.py``（装配层）与 ``tests/test_pacing_baseline.py``（对上游出处）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from ticket_helper.kernel import registry  # noqa: E402

#: 期望的差异（**逐项写死**；多一处少一处都报错）
EXPECTED = {
    "pacing": {
        "interval_min": (1.0, 0.5),
        "interval_max": (8.0, 1.0),
        "timeout": (12.0, 5.0),
    },
    # 心跳拿「某个账号配了 600 秒」当样本：配 120（默认值）时两边数字恰好一样，
    # 看不出差异 —— 而差异恰恰只在「账号配的不是 120」时才显形。
    "heartbeat": {
        "interval": (600.0, 120.0),
        "min_interval": (30.0, 0.0),
    },
}


def _spec_with(domain: str, backend: str, **kwargs):
    previous = os.environ.get(registry.ENV_VAR)
    os.environ[registry.ENV_VAR] = f"{domain}={backend}"
    registry.reset_cache()
    try:
        return registry.get(domain, **kwargs).spec()
    finally:
        if previous is None:
            os.environ.pop(registry.ENV_VAR, None)
        else:
            os.environ[registry.ENV_VAR] = previous
        registry.reset_cache()


def main() -> int:
    problems: list[str] = []
    print("=" * 74)
    print("节奏/参数：两套实现对比（判据 = 真正喂给限速器/心跳/传输层的数字）")
    print("=" * 74)

    for domain, fields in EXPECTED.items():
        kwargs = {"interval": 600} if domain == "heartbeat" else {}
        mine = _spec_with(domain, "self", **kwargs)
        theirs = _spec_with(domain, "pjialin", **kwargs)
        print(f"\n── {domain}")
        for field, (expect_self, expect_up) in fields.items():
            got_self = float(getattr(mine, field))
            got_up = float(getattr(theirs, field))
            mark = "✔" if (got_self, got_up) == (expect_self, expect_up) else "✘"
            print(f"    {field:<16} self={got_self:<8} pjialin={got_up:<8} {mark}")
            if (got_self, got_up) != (expect_self, expect_up):
                problems.append(
                    f"{domain}.{field} 与预期不符：self={got_self} pjialin={got_up}"
                    f"（预期 {(expect_self, expect_up)}）"
                )

    # 语义性判断（不只是数字不同）：上游那套**更激进**，且心跳**没有地板**
    pacing_self = _spec_with("pacing", "self")
    pacing_up = _spec_with("pacing", "pjialin")
    if not pacing_up.interval_max <= pacing_self.interval_min:
        problems.append("预期「上游的**上限**不高于本项目的**下限**」（上游明显更激进）")
    span_self = pacing_self.interval_max - pacing_self.interval_min
    span_up = pacing_up.interval_max - pacing_up.interval_min
    if not span_up < span_self:
        problems.append(f"预期上游区间更窄：self={span_self} pjialin={span_up}")
    hb_self = _spec_with("heartbeat", "self", interval=600)
    hb_up = _spec_with("heartbeat", "pjialin", interval=600)
    if not (hb_self.min_interval > 0 and hb_up.min_interval == 0):
        problems.append("预期「只有本项目有心跳地板」")

    print()
    print("-" * 74)
    if problems:
        print(f"✘ 与预期不符（{len(problems)} 处）：")
        for item in problems:
            print(f"    - {item}")
        return 1
    print("✔ 差异与文档一致。**注意方向**：")
    print("   · pacing：上游区间 0.5~1.0s 整段不高于本项目下限 1.0s —— 切过去 = 请求更密 = 更容易被限流；")
    print("   · heartbeat：上游**没有地板** —— 切过去 = 去掉一层防护，不是「更接近上游」；")
    print("   · timeout：上游 5s vs 本项目 12s —— 切过去更容易超时重试。")
    print("")
    print("⚠️ 这张表**只说明差在哪**，不说明该不该切默认（默认保持 self，要真机证据）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
