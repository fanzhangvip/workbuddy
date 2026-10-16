# -*- coding: utf-8 -*-
"""运行时配置的**声明式注册表** —— 「键 / 类型 / 默认值 / 说明」的单一来源。

## 解决什么问题（代码评审 P2-10）

改造前同一份配置清单**存在两处**：
  · **读侧**：`api_scoring.get_settings()` 里逐个人工地 `db.setting_int("ctx_window_size",
    "CTX_WINDOW_SIZE", 60)` —— 62 行手写；
  · **写侧**：`api_scoring._apply_settings()` 里的白名单元组 —— 46 项手写 `(key, setting_key)`。
两侧靠「约定」保持一致，**新增一个配置极容易只改一侧**：
只改读侧 → 能读到但不接受写入（表现为「改了不生效」）；
只改写侧 → 能写入但读不到（表现为「改完看不见」）。

现在两边都从本表派生 → **物理上不可能漂移**。

## 与既有 API 的关系

`db.setting_int/setting_bool/setting_float/setting` 的语义是
**「app_setting 优先，其次 .env，最后默认值」**。本表只是把「默认值」与「.env 键名」
声明出来，取值仍走同一套函数 → **行为逐字不变**（`tests/test_settings_registry.py` 对
逐键做「改造前 vs 改造后」等值对比）。

## 分组

`group` 只用于前端/文档的组织与排障检索，不参与逻辑。
"""
from dataclasses import dataclass, field

import db

#: 类型标识（与 scoring_service 的四个取值函数一一对应）
INT, BOOL, FLOAT, STR = "int", "bool", "float", "str"


@dataclass(frozen=True)
class Setting:
    """一项运行时配置。"""
    key: str                       #: app_setting / 前端用的键名
    env: str                       #: .env / 环境变量键名
    type: str                      #: INT / BOOL / FLOAT / STR
    default: object                #: 默认值（app_setting 与 .env 都缺失时）
    group: str = "other"           #: 归类（仅用于组织）
    desc: str = ""                 #: 中文说明
    risk: str = ""                 #: 非空表示「高风险/写外部系统」相关，前端可据此加提醒
    api: str = ""                  #: `GET /settings` 响应里的键名；空 = 同 `key`

    @property
    def api_key(self) -> str:
        """对外的键名。

        ⚠️ 少数键的历史 API 名与 app_setting 键名**不同**（如 `max_sessions`
        ← `scoring_max_sessions`）。前端与既有脚本按旧名读取，改名＝破坏性变更 →
        这里显式保留别名，注册表只用来去重、不改接口。
        """
        return self.api or self.key


# ============================================================ 注册表

SETTINGS: tuple = (
    # ---------------- 评分范围与行为 ----------------
    Setting("scoring_max_sessions", "SCORING_MAX_SESSIONS", INT, 200, "scoring",
            "单轮评分最多处理多少会话（0 = 不限）", api="max_sessions"),
    Setting("scoring_min_messages", "SCORING_MIN_MESSAGES", INT, 4, "scoring",
            "参与评分的最少消息数", api="min_messages"),
    Setting("scoring_fetch_tags", "SCORING_FETCH_TAGS", BOOL, False, "scoring",
            "评分时是否拉取客户自定义字段/标签（用于到课、完课判定）", api="fetch_tags"),
    Setting("scoring_deadline_minutes", "SCORING_DEADLINE_MINUTES", INT, 120, "scoring",
            "单轮评分的墙钟上限，超时优雅退出（架构 G3/G4）", api="deadline_minutes"),
    Setting("scoring_max_llm_calls", "SCORING_MAX_LLM_CALLS", INT, 500, "scoring",
            "单轮评分最多调用多少次模型（0 = 不限，护栏 G3）", api="max_llm_calls"),
    Setting("scoring_priority_order", "SCORING_PRIORITY_ORDER", BOOL, True, "scoring",
            "候选会话是否按优先级排序（先评高分/活跃客户）", api="priority_order"),
    Setting("scoring_morning_cron", "SCORING_MORNING_CRON", STR, "", "scoring",
            "早会复核触发时刻 HH:MM（**空 = 关闭**，决策 D3）", api="morning_cron"),
    # ---------------- 日常任务（需求 4.2：什么时间同步、什么时候接着分析） ----------------
    Setting("sync_main_cron", "SYNC_MAIN_CRON", STR, "20:00", "schedule",
            "主增量同步 + 分析：每日触发时刻 HH:MM（**空 = 关闭**）。"
            "需求 4.2 要求覆盖 19:30 的销售主动发送及其后的客户互动"),
    Setting("sync_main_window_minutes", "SYNC_MAIN_WINDOW_MINUTES", INT, 20, "schedule",
            "主任务的执行窗口（分钟）：窗口内跑同步，到点优雅收尾（0 = 不限）"),
    Setting("sync_compensate_cron", "SYNC_COMPENSATE_CRON", STR, "03:30", "schedule",
            "48 小时补偿同步 + 分析：每日触发时刻 HH:MM（**空 = 关闭**）。"
            "修复接口入库延迟 / 网络失败 / 任务中断导致的漏处理"),
    Setting("sync_compensate_lookback_hours", "SYNC_COMPENSATE_LOOKBACK_HOURS", INT, 48,
            "schedule",
            "补偿任务的回看窗口（小时）：**抛开水位**重拉最近 N 小时，"
            "兜住「消息迟到入库 → msg_time 小于水位 → 增量永远取不到」"),
    Setting("daily_run_analysis", "DAILY_RUN_ANALYSIS", BOOL, True, "schedule",
            "日常任务：同步跑完后**自动接着跑分析**（关掉则只同步）"),
    Setting("sync_overlap_minutes", "SYNC_OVERLAP_MINUTES", INT, 15, "schedule",
            "增量同步的重叠回看窗口（分钟，需求 4.3）："
            "`start_time = 水位 − 该值`，兜住乱序/迟到入库的消息（重复由唯一键幂等吸收）"),
    Setting("public_class_grade_scope", "PUBLIC_CLASS_GRADE_SCOPE", STR, "S,A", "scoring",
            "公开课复核覆盖的等级范围（逗号分隔）"),

    # ---------------- 模型调用 ----------------
    Setting("llm_timeout_sec", "LLM_TIMEOUT_SEC", INT, 0, "llm",
            "单次 LLM 调用超时秒数（0 = 用 llm 模块默认 120s）"),
    Setting("llm_temperature", "LLM_TEMPERATURE", FLOAT, 0.0, "llm",
            "采样温度（评分判定场景应为 0，保证可复现）"),
    Setting("llm_max_tokens", "LLM_MAX_TOKENS", INT, 0, "llm",
            "单次输出上限（0 = 用代码默认 8192；**勿调小**，真实会话会被截断）"),
    Setting("llm_retry_attempts", "LLM_RETRY_ATTEMPTS", INT, 3, "llm",
            "临时故障重试次数（不含契约修复）"),
    Setting("rejudge_retries", "REJUDGE_RETRIES", INT, None, "llm",
            "契约校验失败后的修复重试次数（默认取 analyze.DEFAULT_MAX_REPAIRS）"),
    Setting("session_max_failures", "SESSION_MAX_FAILURES", INT, 3, "llm",
            "单会话连续失败多少次后转人工（修 G5）"),

    # ---------------- 长任务互斥（进程被杀后不锁死系统） ----------------
    Setting("job_heartbeat_stale_minutes", "JOB_HEARTBEAT_STALE_MINUTES", INT, 15, "llm",
            "长任务心跳判死阈值（分钟）：心跳停这么久 = 进程已死 → 判僵尸并放行。"
            "**必须大于单会话最坏耗时**，否则会把活任务误判为死"),
    Setting("scoring_stale_minutes", "SCORING_STALE_MINUTES", INT, 240, "llm",
            "二级兜底（分钟）：running 超过这么久一律判僵尸（兼容无心跳的老行）"),

    # ---------------- 回写（写外部系统） ----------------
    Setting("writeback_delta_mode", "WRITEBACK_DELTA_MODE", BOOL, True, "writeback",
            "微伴评分为累计语义 → 只写差值。**开 live 前不要关**", risk="high"),
    Setting("writeback_quota_per_run", "WRITEBACK_QUOTA_PER_RUN", INT, 0, "writeback",
            "单轮回写次数上限（0 = 不限；架构 边界3）", risk="high"),
    Setting("writeback_quota_per_customer_per_day", "WRITEBACK_QUOTA_PER_CUSTOMER_PER_DAY",
            INT, 0, "writeback", "单客户单日回写上限（0 = 不限；文档建议 3）", risk="high"),

    # ---------------- 等级标签（D2-1，独立链路） ----------------
    Setting("grade_tag_mode", "GRADE_TAG_MODE", STR, "off", "grade_tag",
            "等级→微伴标签三档开关 off/dry_run/live。**与分数回写独立**，默认 off",
            risk="high"),
    Setting("grade_tag_group", "GRADE_TAG_GROUP", STR, None, "grade_tag",
            "等级标签所在的微伴标签组名（默认取 grade_tag.DEFAULT_TAG_GROUP）"),
    Setting("grade_tag_probe_customer", "", STR, "", "grade_tag",
            "建标签载体 `staff_id:external_user_id`（专用测试客户）。"
            "**空 = 禁止自动创建标签**（防把等级标签打到无关真实客户身上）", risk="high"),

    # ---------------- 上下文预处理 ----------------
    Setting("ctx_window_size", "CTX_WINDOW_SIZE", INT, 60, "ctx",
            "兜底窗口条数上限"),
    Setting("ctx_drop_msgtypes", "CTX_DROP_MSGTYPES", STR, "31,9", "ctx",
            "丢弃的消息类型（31=撤回，9=名片）"),
    Setting("ctx_drop_pure_url", "CTX_DROP_PURE_URL", BOOL, True, "ctx",
            "是否丢弃纯链接消息"),
    Setting("ctx_drop_system", "CTX_DROP_SYSTEM", BOOL, True, "ctx",
            "是否丢弃系统消息"),
    Setting("ctx_display_relative", "CTX_DISPLAY_RELATIVE", BOOL, True, "ctx",
            "行首时间是否附「N 天前」相对时间"),
    Setting("ctx_min_kept", "CTX_MIN_KEPT", INT, 4, "ctx",
            "有效信息量闸：保留条数下限（B4 三口径之一）"),
    Setting("ctx_min_customer_msgs", "CTX_MIN_CUSTOMER_MSGS", INT, 1, "ctx",
            "有效信息量闸：客户至少开口多少句（防销售单方面刷屏）"),
    Setting("ctx_min_effective_chars", "CTX_MIN_EFFECTIVE_CHARS", INT, 0, "ctx",
            "有效信息量闸：文本长度下限（0 = 不限）"),
    Setting("ctx_key_history", "CTX_KEY_HISTORY", BOOL, True, "ctx",
            "④ 关键历史段总开关（回滚开关）"),
    Setting("ctx_key_history_max", "CTX_KEY_HISTORY_MAX", INT, 30, "ctx",
            "④ 段总量上限（条）"),
    Setting("ctx_cold_start_early", "CTX_COLD_START_EARLY", INT, 10, "ctx",
            "④′ 冷启动：无 rule_tags 时取最早的 N 条作锚点"),
    Setting("ctx_delta_max", "CTX_DELTA_MAX", INT, 200, "ctx",
            "③ 增量段软上限（**0 = 关闭增量**，退回「最近窗口」）"),
    Setting("ctx_tokens_max", "CTX_TOKENS_MAX", INT, 12000, "ctx",
            "整段文本的 token 硬预算（超限先砍 ⑤ 再砍 ③）"),
    Setting("ctx_memory", "CTX_MEMORY", BOOL, True, "ctx",
            "①② 记忆段总开关（读回上次结论；**不改分**）"),
    Setting("ctx_fallback_min", "CTX_FALLBACK_MIN", INT, 4, "ctx",
            "⑤ 兜底窗口触发阈值（③④ 有效条数低于它就补窗口）"),

    # ---------------- 置信度与复核（架构 第六章 E4） ----------------
    Setting("confidence_review_min", "CONFIDENCE_REVIEW_MIN", FLOAT, None, "confidence",
            "会话置信度低于它 → 转人工（默认按真实分布标定）"),
    Setting("confidence_boundary_delta", "CONFIDENCE_BOUNDARY_DELTA", INT, None,
            "confidence", "距等级边界多少分以内视为「边界样本」"),
    Setting("confidence_flip_steps", "CONFIDENCE_FLIP_STEPS", INT, None, "confidence",
            "结论翻转所需的档位数"),
    Setting("confidence_weak_weight", "CONFIDENCE_WEAK_WEIGHT", INT, None, "confidence",
            "弱证据（模糊匹配）的降权系数"),
    Setting("review_engine_negative", "REVIEW_ENGINE_NEGATIVE", BOOL, None, "confidence",
            "引擎规则负分是否也进人工复核（**默认 true=不放松**；关掉可把复核率 90%→60%）"),

    # ---------------- 评估集与门禁 ----------------
    Setting("eval_gate_enabled", "EVAL_GATE_ENABLED", BOOL, True, "eval",
            "改规则前是否强制跑评估集门禁（闸6）"),
    Setting("eval_baseline_path", "EVAL_BASELINE_PATH", STR, "", "eval",
            "评估基线文件路径（空 = 用评估集库内基线）"),
    Setting("eval_alert_review_backlog", "EVAL_ALERT_REVIEW_BACKLOG", INT, 50, "eval",
            "待复核超过这个条数时在 /api/eval/alerts 告警（**0 = 不告警**）"),
    Setting("eval_alert_credential_hours", "EVAL_ALERT_CREDENTIAL_HOURS", INT, 36, "eval",
            "凭据超过这个小时数未刷新时告警（**0 = 不告警**；正文通道断过 3 天的先例）"),
    Setting("no_reply_window_days", "NO_REPLY_WINDOW_DAYS", INT, 14, "eval",
            "「连续 48 小时未回复」的上窗（天）：只判「48 小时 ~ 该窗口」之间的中断，"
            "超过视为线索已冷；**0 = 不设上限**（退回改造前语义）"),
    Setting("eval_publish_requires_run", "EVAL_PUBLISH_REQUIRES_RUN", BOOL, False, "eval",
            "发布规则前必须先跑一轮评估集（两步发布）。**默认关** = 与改造前一致："
            "发布时同步现跑（十几分钟的长请求）"),
    Setting("eval_gate_fresh_minutes", "EVAL_GATE_FRESH_MINUTES", INT, 30, "eval",
            "跑分结果超过这个分钟数就不复用于门禁（防「拿改规则之前的指标比基线」）"),
    Setting("eval_min_cases_per_rule", "EVAL_MIN_CASES_PER_RULE", INT, 3, "eval",
            "每条规则在评估集里至少应有几条样本（低于它就提示「改坏了也测不出来」；0 = 不提示）"),
    Setting("eval_regression_cron", "EVAL_REGRESSION_CRON", STR, "09:00", "eval",
            "定期回归的时刻（**每周一**执行，格式 HH:MM；空 = 关闭）"),
    Setting("eval_regression_limit", "EVAL_REGRESSION_LIMIT", INT, 0, "eval",
            "定期回归每轮跑多少条样本（0 = 全部有效样本）"),

    # ---------------- 阶段B 灰度与观测 ----------------
    Setting("use_graph_session", "USE_GRAPH_SESSION", BOOL, False, "pipeline",
            "LangGraph 图化路径灰度开关（false = 走原串行路径）"),
    Setting("use_outbox", "USE_OUTBOX", BOOL, True, "pipeline",
            "回写 outbox 总开关（修 G6：回写与评分解耦）"),
    Setting("trace_enabled", "TRACE_ENABLED", BOOL, False, "observability",
            "本地链路追踪（**默认关**：属观测能力，非保护措施）"),

    # ---------------- 数据保留（边界4） ----------------
    Setting("data_retention_days", "DATA_RETENTION_DAYS", INT, 0, "retention",
            "运行观测类数据的保留天数（**0 = 不清理**；保留多久是业务决策）"),
)

#: 键 → Setting 的索引
BY_KEY: dict = {s.key: s for s in SETTINGS}
#: .env 键 → Setting 的索引
BY_ENV: dict = {s.env: s for s in SETTINGS if s.env}
#: 写侧白名单的**唯一来源**：`(API 键名, app_setting 键名)`。
#: 写侧不再手写这份清单 —— 与读侧同源，物理上不可能漂移。
WRITABLE: tuple = tuple((s.api_key, s.key) for s in SETTINGS)


def typed_get(s: Setting, get_fn):
    """按声明类型取值。

    :param get_fn: `scoring_service` 的取值函数之一（`setting_int` 等）。
        由调用方注入 —— 本模块**不 import scoring_service**，避免循环依赖
        （`scoring_service` → `api_scoring` → 本模块）。
    """
    if s.type == INT:
        return get_fn(s.key, s.env, int(s.default or 0))
    if s.type == BOOL:
        return get_fn(s.key, s.env, bool(s.default))
    if s.type == FLOAT:
        return get_fn(s.key, s.env, float(s.default) if s.default is not None else 0.0)
    d = s.default
    return get_fn(s.key, s.env, d) if d is not None else get_fn(s.key, s.env)


def by_group() -> dict:
    """按 group 归类（供前端设置页 / 文档生成）。"""
    out: dict = {}
    for s in SETTINGS:
        out.setdefault(s.group, []).append(s)
    return out


def describe() -> list:
    """导出为纯 dict 列表（供 API / 文档）。"""
    return [{"key": s.key, "env": s.env, "type": s.type, "group": s.group,
             "default": s.default, "desc": s.desc, "risk": s.risk} for s in SETTINGS]
