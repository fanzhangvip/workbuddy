"""扫码轮询差分表：**两套实现到底往表单里放了什么**。

为什么这张表值得单独存在
------------------------
扫码这个域的差异**特别不显眼**：

- 端点**完全相同**（``/passport/web/checkqr``）—— 所以「切了没切」不会体现在 URL 上；
- 返回值**也相同**（都返回扫码状态）—— 所以也不会体现在结果上；
- 唯一的不同是**表单里多了两个键**（设备指纹 Cookie）。

⇒ 判据只能落在「真实发出去的表单」上。这张表就是把它摆出来，
并顺带对照一遍 ``result_code`` 的语义（那一项两边是**一致**的，也要显示出来）。

用法::

    python scripts/qr_check_diff.py

断言侧：``tests/test_kernel_qr.py``（端到端看 mock 记下的表单）
与 ``tests/test_qr_baseline.py``（对着上游出处）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from ticket_helper.inter import parser as parser_mod  # noqa: E402
from ticket_helper.inter import urls as urls_mod  # noqa: E402
from ticket_helper.kernel import registry  # noqa: E402

DOMAIN = "qr_check"

#: 假装拿到的设备指纹（真机上来自 Cookie）
FAKE_DEVICE = "RAIL-DEVICE-EXAMPLE"
FAKE_EXPIRATION = "9999999999999"


def _spec_with(backend: str):
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


def main() -> int:
    self_spec = _spec_with("self")
    up_spec = _spec_with("pjialin")

    print("=" * 78)
    print("扫码轮询（checkqr）：两套实现对比")
    print("=" * 78)
    for spec in (self_spec, up_spec):
        form = spec.form(uuid="<uuid>", device_id=FAKE_DEVICE, expiration=FAKE_EXPIRATION)
        print(f"    {spec.name:<10}: POST {spec.path}")
        print(f"    {'':<10}  表单键 = {list(spec.form_keys)}")
        print(f"    {'':<10}  实际表单 = {form}")
    print()

    problems: list[str] = []

    # ① 端点必须相同（这是本域的结构事实，不是缺陷）
    if self_spec.path != up_spec.path:
        problems.append(
            f"端点居然不同了（{self_spec.path} vs {up_spec.path}）—— 上游改结构了？"
        )

    # ② 请求体必须不同（差异要留在能被看见的地方）
    self_keys = set(self_spec.form_keys)
    up_keys = set(up_spec.form_keys)
    extra = sorted(up_keys - self_keys)
    if not extra:
        problems.append("两套表单的键相同了 —— 差异被抹平了？")
    else:
        print("-" * 78)
        print(f"✔ 表单差异：上游多带 {extra}")
        print("    它是**设备指纹 Cookie**（真机上官方也会从 Cookie 读，所以两边都能用；")
        print("    上游显式放进表单属于双保险）。差异留档，默认不切。")

    # ③ appid 必须两边都有且一致
    for name, spec in (("self", self_spec), ("pjialin", up_spec)):
        if "appid" not in spec.form_keys:
            problems.append(f"{name} 的表单缺 appid —— 少了它官方只回 result_code=9，不报错")

    print()
    print("-" * 78)
    print("附：result_code 语义（这一项两边**一致**，一起显示以便长期对照）")
    print(f"    {'码':<4} {'本项目状态':<12} 上游分支动作")
    upstream_action = {
        "0": "继续轮询",
        "1": "提示「请确认登录」",
        "2": "跳出循环（成功）",
        "3": "删掉二维码、重新下载（过期）",
    }
    for code in ("0", "1", "2", "3"):
        state = parser_mod.QR_STATE_MAP.get(code)
        print(f"    {code:<4} {state:<12} {upstream_action[code]}")

    print()
    print("-" * 78)
    if problems:
        print("✘ 与预期不符：")
        for item in problems:
            print(f"    - {item}")
        return 1
    print("✔ 端点一致、表单差异与文档一致、appid 两边都有。")
    print("")
    print("⚠️  这张表**只说明差在哪**，不说明该不该切默认：")
    print("   - 本项目不带设备指纹也真机跑通了（扫码是唯一可靠的登录路径）；")
    print("   - 「更接近上游」不是换行为的理由 —— 要真机证据（v3 §10-17）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
