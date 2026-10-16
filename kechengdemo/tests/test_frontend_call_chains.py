# -*- coding: utf-8 -*-
"""前端各页「次要数据源」的两种退化：**静默失败** 与 **深链失效**（源码级断言）。

    .venv\\Scripts\\python.exe tests\\test_frontend_call_chains.py

纯逻辑（只读 `frontend/src`），不需要 PG、不需要浏览器。

## 守的是这两类 bug

### F3 静默失败（"没数据" ≠ "加载失败"）

页面上有一批**次要数据源**：闸6 门禁状态、数据保留期、等级标签、员工下拉、
复核规则选项、meta-loop 报告…… 它们清一色写成 `catch (e) { /* 忽略 */ }`
（改造前共 7 处）。接口一失败，页面**只剩一块空白**，用户读到的是「这里本来就没有数据」。
三次实测排查（会话管理、评分中心、评估集）都栽在这个模式上。

修法：统一收口到 `composables/loadFailures.js`（`note` 记录 / `clear` 撤销）
+ 共用组件 `components/PartialLoadBanner.vue`。**主数据**失败仍然走
`useAsyncAction` 的 `error`（它该挡住视线，不该降级成一条小横幅）。

### F1 深链失效（点了一条搜索结果，跳过去什么都没发生）

`/chat?staff=xxx&session=yyy` 进来时（改造前）要求「该员工必须出现在**前 800 名**员工里」，
且目标会话必须落在**首屏 300 条**里，否则 `find()` 返回 undefined → **什么都不做**，
页面跟没点一样。实测样本：某员工 20058 个会话，目标排第 320。

修法：`staff_id` 直接用深链给的；会话找不到就按 `(staff_id, session_id)` 单查
（`api.sessionDetail`）并插进列表头部；确实打不开时给**明确错误**而不是静默。

## 为什么用"读源码"的方式测

这些行为只在浏览器里可见，本项目没有前端测试基建。退一步的做法是**钉住接线方式**：
护栏被删/被绕过 → 本文件立刻红。与 `tests/test_scoring_ui_guardrails.py`、
`tests/test_sync_window.py` 同一套思路。

⚠️ 断言的粒度是"关键语句存在"，不是逐字比对 —— 改文案不该弄红它，改掉护栏才会。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t0"

import re
import sys
from pathlib import Path

from _harness import ROOT, check, summary  # noqa: E402

SRC = Path(ROOT) / "frontend" / "src"
VIEWS = SRC / "views"

#: 需要挂「部分加载失败」横幅的页面 → 该页的重试入口函数名。
#: 值必须与模板里 `@retry="…"` 一致（重试按钮点了没反应也算坏）。
BANNER_ON = {
    "ChatView.vue": "retryFailed",
    "DashboardView.vue": "load",
    "SyncView.vue": "refresh",
    "ScoreView.vue": "refresh",
    "ReviewView.vue": "refresh",
    "EvalView.vue": "refresh",
}


def _src(name: str) -> str:
    return (VIEWS / name).read_text(encoding="utf-8")


def main():
    # ---------------- F3：统一收口的存在性 ----------------
    comp = (SRC / "components" / "PartialLoadBanner.vue").read_text(encoding="utf-8")
    hook = (SRC / "composables" / "loadFailures.js").read_text(encoding="utf-8")
    check("共用组件 PartialLoadBanner 存在且能重试（emit('retry')）",
          "defineEmits(['retry'])" in comp and 'emit(\'retry\')' in comp)
    check("收口 hook 有 note / clear（失败记录 + 恢复后撤销）",
          "function note(" in hook and "function clear(" in hook)

    # 横幅文案**只能有一份**（AGENTS.md 的 Reuse Before Rewrite：改一次需求只动一个文件）
    dup = [p.name for p in SRC.rglob("*.vue")
           if p.name != "PartialLoadBanner.vue" and "部分数据加载失败" in p.read_text(encoding="utf-8")]
    check("「部分数据加载失败」文案只存在于组件里（没有各页抄一份）",
          not dup, "、".join(dup))

    # ---------------- F3：六个页面都接上了 ----------------
    for name, retry in BANNER_ON.items():
        s = _src(name)
        check(f"{name} 引入了 PartialLoadBanner",
              "import PartialLoadBanner from '../components/PartialLoadBanner.vue'" in s, name)
        check(f"{name} 模板里挂了横幅且能重试（@retry=\"{retry}\"）",
              '<PartialLoadBanner :failures="failures"' in s and f'@retry="{retry}"' in s, name)
        check(f"{name} 用了 useLoadFailures 收口", "useLoadFailures()" in s, name)

    # ---------------- F3：静默 catch 不许回来 ----------------
    def _silent_one_liners(text: str) -> list:
        """挑出「单行就把异常吞掉」的 catch。

        只看**单行**的：`} catch (e) { /* 忽略 */ }`、`catch (_) {}`。
        多行体的 catch（`} catch (e) {` 换行后写处理逻辑）一律放过 —— 判不了，
        这里守的是"改造前那 7 处"的形状。
        """
        out = []
        for i, line in enumerate(text.splitlines(), 1):
            if "catch" not in line or not line.rstrip().endswith("}"):
                continue          # 以 `{` 结尾 = 多行体，交给人工看
            body = line.split("{", 1)[1].rstrip() if "{" in line else ""
            body = body[:-1].strip()               # 去掉行尾那个 `}`
            if body == "" or (body.startswith("/*") and body.endswith("*/")):
                out.append(i)
        return out

    # 自检：检测器本身要抓得住"改造前的形状"，否则它只是在静默通过
    check("静默 catch 检测器有效（拿改造前的写法试一下）",
          _silent_one_liners("  } catch (e) { /* 忽略 */ }") == [1]
          and _silent_one_liners("  } catch (e) {}") == [1]
          and _silent_one_liners("  } catch (e) { noteFail('x', 'X', e) }") == []
          and _silent_one_liners("  } catch (e) {") == [])
    silent = [f"{p.name}:{i}" for p in sorted(VIEWS.glob("*.vue"))
              for i in _silent_one_liners(p.read_text(encoding="utf-8"))]
    check("次要数据源不再有「catch 里只写注释」的静默吞异常",
          not silent, "、".join(silent))

    # ---------------- F1：深链兜底 ----------------
    chat = _src("ChatView.vue")
    check("深链不再要求「员工在前 800 名里」（直接用 query 的 staff_id）",
          "if (qStaff) staffId.value = qStaff" in chat
          and "staffs.value.some(" not in chat, "深链仍被员工列表门禁卡住")
    check("会话不在首屏里时按 (staff_id, session_id) 单查兜底",
          "api.sessionDetail(" in chat and "不在最近 300 条里" in chat)
    check("单查失败/确实打不开 → 给明确错误（不再静默什么都不发生）",
          "打不开这个会话" in chat)
    check("兜底成功后给可关闭的提示（msg toast）",
          'v-if="msg"' in chat and "class=\"toast alert ok\"" in chat, "缺用户可见反馈")

    # ---------------- 复用：单个会话与列表项同形（后端）----------------
    api_chat_src = (Path(ROOT) / "backend" / "api_chat.py").read_text(encoding="utf-8")
    check("`/session` 与 `/sessions` 共用同一份列清单（同一对象不该两种长相）",
          "_SESSION_COLS" in api_chat_src
          and api_chat_src.count("{_SESSION_COLS}") >= 2
          and "SELECT * FROM chat_session" not in api_chat_src,
          "单体查询又写成 SELECT * 了")

    # ---------------- 不再有「定义了却没人用」----------------
    # 这一条是 2026-10-16 那次全量审计的护栏：**前端封装里不允许出现没人调的函数**。
    # 改造前有 10 个（`scoringRules`/`scoringSession`/`evalRuns`… 全是"后端有、界面没入口"）。
    # 真要留纯运维用的函数，得先在下面这份白名单里写清理由 —— 别让死代码悄悄堆回去。
    _OPS_ONLY = {
        # 键 = api.js 里的函数名；值 = 为什么允许它没有调用方
        "evalExportUrl": "返回的是下载 URL（给 <a href> 用，不是函数调用）",
    }
    api_src = (SRC / "api.js").read_text(encoding="utf-8")
    all_views = ""
    # ⚠️ 必须连 `composables/*.js` 一起扫：`useAuth.js` 就用了 authMe/authSignIn/authSignOut，
    #    漏掉它会把这三个报成"没人调"（假红比漏红更烦，会把这条护栏逼成"被注释掉"）。
    for p in (list(VIEWS.glob("*.vue")) + [SRC / "App.vue"]
              + list((SRC / "components").glob("*.vue"))
              + list((SRC / "composables").glob("*.js"))):
        all_views += p.read_text(encoding="utf-8")
    fns = re.findall(r"^\s{2}(\w+):\s*\(", api_src, re.M)
    dead = [n for n in fns
            if n not in _OPS_ONLY and f"api.{n}(" not in all_views
            and f"api.{n} " not in all_views and f"api.{n}\n" not in all_views]
    check("★ 没有「后端有端点、前端封装了但没人调」的死函数",
          not dead, "、".join("api." + n for n in dead))
    check("同步跑分的同步封装已删除（浏览器 120 秒必断，文档明确说别用）",
          "evalRunNow" not in api_src)

    # ---------------- 空头字段不许回潮 ----------------
    # `last_notified_grade`（"上次通知过的等级"）从建表起就没有写方，也没有通知出口 ——
    # 2026-10-16 删列。空列比缺列更坏：读 schema 的人会以为通知功能已经存在。
    schema_src = (Path(ROOT) / "backend" / "schema.sql").read_text(encoding="utf-8")
    check("★ schema.sql 不再**新增** last_notified_grade（只留 DROP 迁移语句）",
          "ADD COLUMN IF NOT EXISTS last_notified_grade" not in schema_src
          and "DROP COLUMN IF EXISTS last_notified_grade" in schema_src)
    code_files = [p for p in (Path(ROOT) / "backend").glob("*.py")]
    offenders = [p.name for p in code_files
                 if "last_notified_grade" in p.read_text(encoding="utf-8")]
    check("后端代码里也没有 last_notified_grade 的引用", not offenders, "、".join(offenders))

    # ---------------- 「有接口没入口」的三个典型已接线 ----------------
    score = _src("ScoreView.vue")
    check("评分中心有回写审批队列（批准/驳回）",
          "scoringApprovals(" in score and "scoringApprovalDecide(" in score)
    check("评分中心有批次任务面板（详情/取消/续跑，`/scoring/tasks*` 五个端点都接上了）",
          "scoringTasks(" in score and "scoringTaskCancel(" in score
          and "scoringTaskResume(" in score and "scoringTaskDetail(" in score)
    check("评分中心有审计留痕面板（策略分布 + 工具调用）",
          "scoringPolicyStats(" in score and "scoringAuditTool(" in score)
    check("评分中心能发布规则（含强制发布）",
          "scoringRulesPublish(" in score and "force: true" in score)
    check("评分中心能执行保留期清理 / 投递标签队列",
          "scoringRetentionPurge(" in score and "scoringGradeTagDrain(" in score)
    review = _src("ReviewView.vue")
    check("复核页有批量改状态（`/review/batch-status`）",
          "reviewBatchStatus(" in review)
    check("复核页有操作审计（`/review/audit`）", "reviewAudit(" in review)
    ev = _src("EvalView.vue")
    check("评估集有运行历史 / 基线 / 告警 / 覆盖度",
          all(k in ev for k in ("evalRuns(", "evalBaseline(", "evalAlerts(", "evalCoverage(")))
    check("评估集能停用 / 删除样本", "evalActive(" in ev and "evalDelete(" in ev)
    check("评估集不再裸 fetch（统一走 api.js）", "fetch(" not in ev)
    chat = _src("ChatView.vue")
    check("会话页能对单个会话重新评分（`/scoring/session`）",
          "scoringSession(" in chat and "rescoreSession" in chat)
    sync = _src("SyncView.vue")
    check("同步页显示第一遍的翻页/截断统计（`sync_job.detail` 不再是死数据）",
          "scanStats" in sync and "truncated_count" in sync)

    # ---------------- 「用了但没引入」不许出现 ----------------
    # 这一条抓到过真 bug：`ReviewView.vue` 的模板一直在用 `fmtTime(...)`，但那个文件
    # 没 import 它 → 复核**已完成**的记录打开详情就是 `fmtTime is not a function`。
    # 只有 done 状态那一条分支会走到，平时看不见（典型"只在某条数据上炸"）。
    api_exports = set(re.findall(r"^export (?:function|const) (\w+)", api_src, re.M))
    api_exports |= set(re.findall(r"^export \{([^}]*)\}", api_src, re.M)[0].split(",")) \
        if re.search(r"^export \{", api_src, re.M) else set()
    api_exports = {x.strip() for x in api_exports if x.strip()}
    missing_import = []
    for p in (list(VIEWS.glob("*.vue")) + list((SRC / "components").glob("*.vue"))
              + list((SRC / "composables").glob("*.js"))):
        text = p.read_text(encoding="utf-8")
        imported = set()
        for m in re.finditer(r"import \{([^}]+)\} from ['\"][^'\"]*api['\"]", text):
            imported |= {x.strip() for x in m.group(1).split(",")}
        for name in api_exports:
            if re.search(r"\b" + name + r"\s*\(", text) and name not in imported:
                missing_import.append(f"{p.name}:{name}")
    check("★ 没有「用了 api.js 的导出函数但忘了 import」的文件（模板里会直接报错）",
          not missing_import, "、".join(missing_import))

    return summary()


if __name__ == "__main__":
    sys.exit(main())
