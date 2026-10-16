# -*- coding: utf-8 -*-
"""结构指纹（sha256 前 N 位）—— **全项目唯一实现**。

用途：比对「两次运行的输入 / 两次响应的结果是否一致」，而**不落原文**
（PII + 体积控制）。改造前 `audit.py` 与 `trace.py` 各有一份逐字相同的
`digest()` —— 两处「同一个东西的指纹」一旦漂移，审计对不上的时候根本无从查起。

⚠️ **为什么不放进 `trace.py`**：`trace` 既是本项目模块名，**也是标准库模块名**
（`backend/db.py` 里就专门用 `sys.modules.get("trace")` 绕开这个重名）。
让 `audit` 去 `import trace`，等于把「可能 import 到标准库那个 trace」的风险
扩散到更多模块。指纹是中性的东西，单独放一个小模块最安全。
"""
import hashlib
import json

#: 默认截断长度。16 位十六进制 = 64 bit，用于「比对是否一致」足够，
#: 又短到不会让人误以为能当唯一 ID 用。
DIGEST_LEN = 16


def digest(value, n: int = DIGEST_LEN) -> str:
    """结构化摘要：`json.dumps(排序键, 不转义非 ASCII)` → `sha256` → 前 `n` 位。

    无法序列化的对象退化成 `repr(value)`：宁可指纹不稳，也不要因为一个奇怪对象
    把审计 / 追踪写崩（这两条路径都是**旁路观测**，不能反过来影响主流程）。
    """
    try:
        s = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:                              # noqa: BLE001
        s = repr(value)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:int(n)]
