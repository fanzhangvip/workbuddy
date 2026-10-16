# -*- coding: utf-8 -*-
"""评估链路的**可观测口径**：把"没跑成"和"被跳过"分开、脏值留痕（纯逻辑，不需要 PG）。

    .venv\\Scripts\\python.exe tests\\test_eval_observability.py

## 守的是什么（2026-10-16 的一轮缺陷修复）

`eval_runner.run_sync` 逐条跑样本，单条异常被兜成
`{"ok": False, "reason": "error", "grade": None}`；而 `metrics()` 原来只判
`actual_grade is None` 就 `continue` —— 注释写的是"被跳过（有效条数不足）"，
**但模型/契约异常也是 `grade is None`**，于是两者一起被剔除出指标分母：

  · `case_valid` 缩水 → 门禁容差 `max(2%, 1/有效样本数)` 变宽 → 保护被悄悄放松；
  · 界面只显示 `case_ok/case_total`，差值被读成"跳过的样本"；
  · 没有任何告警 —— 实测 `run#100` 真的发生过 1 条 `reason='error'`，全靠翻 JSON 才发现。

所以这里钉住：`metrics()` 必须把 `skipped` 与 `error` **分别计数**，且
`case_error` 出现在返回值里（供门禁/告警/界面消费）。

另外钉住 `contracts` 对"模型自报置信度越界/非法"的处理：**夹值但不静默** ——
`confidence_clamped` 留痕，且该字段**不进 JSON schema**（提示词一个字都不变）。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t0"

import sys

from _harness import check, summary  # noqa: E402

import contracts          # noqa: E402
import eval_runner as er  # noqa: E402


_OK_PAIR = {"expected_items": [{"rule_id": "r1", "score": 2}],
            "actual_items": [{"rule_id": "r1", "score": 2}],
            "expected_grade": "A", "actual_grade": "A", "reason": "ok"}
_SKIP_PAIR = {"expected_items": [], "actual_items": [], "expected_grade": "A",
              "actual_grade": None, "reason": "skipped"}
_ERR_PAIR = {"expected_items": [], "actual_items": [], "expected_grade": "A",
             "actual_grade": None, "reason": "error"}


def main():
    # ---------------- ① metrics：跳过 vs 未跑成 ----------------
    m = er.metrics([_OK_PAIR, _SKIP_PAIR, _ERR_PAIR, dict(_ERR_PAIR)])
    check("★ 未跑成的样本单列 case_error（不再混进 case_valid）",
          m.get("case_error") == 2, str({k: m.get(k) for k in
                                        ("case_total", "case_valid", "case_skipped", "case_error")}))
    check("★ 被跳过的样本单列 case_skipped", m.get("case_skipped") == 1, str(m.get("case_skipped")))
    check("有效样本只数真正跑成的那条", m.get("case_valid") == 1 and m.get("case_total") == 4,
          f"valid={m.get('case_valid')} total={m.get('case_total')}")
    check("指标仍按有效样本算（F1 不受未跑成样本影响）", m.get("rule_f1") == 1.0, str(m.get("rule_f1")))
    check("老的调用方（pairs 里没有 reason 键）不会被弄坏：默认算 skipped",
          er.metrics([dict(_SKIP_PAIR) | {"reason": None}]).get("case_skipped") == 1)
    check("全跑成时两个失败计数都是 0",
          er.metrics([_OK_PAIR]).get("case_error") == 0
          and er.metrics([_OK_PAIR]).get("case_skipped") == 0)

    # ---------------- ② 模型自报置信度：夹值但留痕 ----------------
    base = ('{"rule_checks":[{"rule_id":"positive_urgency","hit":true,"score":2,'
            '"reason":"x","evidence_ids":[]}],"model_confidence":%s}')
    j_hi = contracts.JudgmentOut.model_validate_json(base % "2.5")
    j_bad = contracts.JudgmentOut.model_validate_json(base % '"脏值"')
    j_ok = contracts.JudgmentOut.model_validate_json(base % "0.8")
    check("★ 越界值被夹到 1.0 且**留痕**", j_hi.model_confidence == 1.0
          and j_hi.confidence_clamped is True, f"{j_hi.model_confidence}/{j_hi.confidence_clamped}")
    check("★ 非法值回退 0.5 且**留痕**", j_bad.model_confidence == 0.5
          and j_bad.confidence_clamped is True, f"{j_bad.model_confidence}/{j_bad.confidence_clamped}")
    check("合法值不留痕（别把正常数据标成脏）",
          j_ok.confidence_clamped is False and j_ok.model_confidence == 0.8)
    props = contracts.JudgmentOut.model_json_schema().get("properties", {})
    check("★ 留痕字段**不进 JSON schema**（模型看不到它，提示词一字不变）",
          "confidence_clamped" not in props, str(sorted(props)[:8]))
    return summary()


if __name__ == "__main__":
    sys.exit(main())
