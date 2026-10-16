# -*- coding: utf-8 -*-
"""评分中心那几条**安全护栏**（源码级断言）—— 纯逻辑，不需要 PG。

    .venv\\Scripts\\python.exe tests\\test_scoring_ui_guardrails.py

## 为什么用"读源码"的方式测

它们守的是**界面上的安全动作**（切"真实写入"要二次确认、停止要看返回值、对齐要有真执行
入口）——这些行为只在浏览器里可见，而在本项目里没有前端测试基建。
退一步的做法是**钉住接线方式**：这些护栏一旦被删/被绕过，本文件立刻红。

这与 `tests/test_sync_window.py` 里那几条源码级断言是同一套思路：
**行为测试抓不到"接线退化"，只能靠把接线本身钉住。**

⚠️ 断言的粒度是"关键语句存在"，不是逐字比对 —— 改文案不该弄红它，改掉护栏才会。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t0"

import sys
from pathlib import Path

from _harness import ROOT, check, summary  # noqa: E402

VIEW = Path(ROOT) / "frontend" / "src" / "views" / "ScoreView.vue"
POLL = Path(ROOT) / "frontend" / "src" / "composables" / "usePolling.js"


def _fn_src(text: str, name: str) -> str:
    """粗切出 `async function <name>(...)` 的函数体（到下一个顶层 function 为止）。"""
    i = text.find(f"function {name}(")
    if i < 0:
        return ""
    j = text.find("\nasync function ", i + 10)
    k = text.find("\nfunction ", i + 10)
    ends = [x for x in (j, k) if x > 0]
    return text[i:min(ends)] if ends else text[i:]


def main():
    src = VIEW.read_text(encoding="utf-8")

    # ---- P1：写外部系统的开关必须二次确认，且要校验返回值 ----
    for fn, label in (("setMode", "分数回写"), ("setTagMode", "等级标签")):
        body = _fn_src(src, fn)
        check(f"{label}切 live 有二次确认（window.confirm）",
              "window.confirm" in body and "'live'" in body, body[:80])
        check(f"{label}切模式校验了返回值（r.ok）", "if (!r.ok)" in body, body[:80])
    check("切 live 的确认文案说清了「累加不可逆 + 水位为空会全额累加」",
          "不可撤销" in src and "正分全额" in src)

    # ---- P3：停止必须看返回值 ----
    stop_body = _fn_src(src, "stop")
    check("「停止」检查了返回值（没任务在跑时会有提示）", "if (!r.ok)" in stop_body, stop_body[:80])

    # ---- P4：对齐既要有预演、也要有**真执行**入口 ----
    align_body = _fn_src(src, "alignGradeTags")
    check("对齐支持执行路径（dry_run 不再写死 true）",
          "dry_run: !apply" in align_body, align_body[:120])
    check("对齐执行前有二次确认", "window.confirm" in align_body)
    check("模板里有「执行对齐（写微伴）」按钮",
          "执行对齐" in src and "alignGradeTags(true)" in src)

    # ---- P8：在微伴创建标签要确认 ----
    sync_body = _fn_src(src, "syncGradeTags")
    check("「创建缺失标签」前有二次确认",
          "window.confirm" in sync_body and "createMissing" in sync_body, sync_body[:120])

    # ---- P7：全量重评要确认，且界面上说明"不勾也会因规则版本变化重评" ----
    start_body = _fn_src(src, "start")
    check("「忽略水位强制重评」有二次确认",
          "window.confirm" in start_body and "form.value.force" in start_body, start_body[:120])
    check("界面上说明了「规则版本变化」也算候选",
          "规则版本变化" in src)

    # ---- P5：当前任务与最近一次运行必须分开表述 ----
    check("卡片按是否在跑切换标题（当前任务 / 最近一次运行）",
          "running ? '当前任务' : '最近一次运行'" in src)
    check("不再把进程内 stage 为空写成「暂无任务」",
          "『暂无任务』" not in src and "'暂无任务'" not in src)

    # ---- P6：轮询要能按可见性/空闲降频 ----
    poll = POLL.read_text(encoding="utf-8")
    check("usePolling 支持隐藏时停轮询",
          "visibilityState" in poll and "visibilitychange" in poll)
    check("usePolling 支持 idle 降频（setTimeout 链）",
          "idleWhen" in poll and "setTimeout" in poll)
    check("评分中心用上了空闲降频",
          "usePolling(refresh, 3000, { idle" in src, "未接 idle 参数")
    return summary()


if __name__ == "__main__":
    sys.exit(main())
