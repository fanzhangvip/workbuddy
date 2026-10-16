-- ============================================================
-- 企微聊天记录归档系统 —— 表结构
-- 幂等约定：chat_message 主键 = (staff_id, session_id, message_id)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------- 员工 ----------------
CREATE TABLE IF NOT EXISTS staff (
    staff_id     TEXT PRIMARY KEY,           -- 微伴 staff_ext_id（wogiz...）
    name         TEXT,
    alias        TEXT,                       -- 企业微信号
    avatar       TEXT,
    mobile       TEXT,
    position     TEXT,
    status       SMALLINT,                   -- 1=已激活
    ia_authorized BOOLEAN,                   -- 会话内容存档：内部/应用授权
    ma_authorized BOOLEAN,
    oa_authorized BOOLEAN,
    raw          JSONB,                      -- 原始返回，防字段遗漏
    created_at   BIGINT,
    updated_at   BIGINT,
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_staff_name ON staff (name);

-- ---------------- 会话 ----------------
CREATE TABLE IF NOT EXISTS chat_session (
    staff_id       TEXT NOT NULL,
    session_id     TEXT NOT NULL,            -- 微伴 session_ext_id（wmgiz...）
    session_type   TEXT NOT NULL DEFAULT 'ext_user',  -- ext_user/room/other_staff
    name           TEXT,                     -- 客户昵称
    remark         TEXT,                     -- 员工备注
    avatar         TEXT,
    target_id      TEXT,                     -- 对方 id
    agree_msgaudit BOOLEAN,                  -- 对方是否同意存档（false 则拉不到消息）
    last_msg_time  BIGINT,
    last_msg_thumb TEXT,
    created_at     BIGINT,
    msg_count      INT NOT NULL DEFAULT 0,   -- 已入库消息数（维护字段）
    synced_at      TIMESTAMPTZ,
    PRIMARY KEY (staff_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_session_last_time ON chat_session (last_msg_time DESC);
CREATE INDEX IF NOT EXISTS idx_session_type ON chat_session (session_type);
CREATE INDEX IF NOT EXISTS idx_session_name ON chat_session USING gin (name gin_trgm_ops);

-- ---------------- 消息 ----------------
CREATE TABLE IF NOT EXISTS chat_message (
    staff_id     TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    message_id   TEXT NOT NULL,              -- 微伴消息 id
    msgid        TEXT,                       -- 企微 msgid（= original_id），解密用
    direction    TEXT,                       -- send=员工发出 / recv=客户发来
    msg_time     BIGINT,                     -- 消息时间（秒）
    msg_type     TEXT,                       -- 微伴消息类型：text/image/...
    from_id      TEXT,
    to_id        TEXT,
    from_name    TEXT,
    from_avatar  TEXT,
    room_id      TEXT,
    file_url     TEXT,
    secret_key   TEXT,                       -- 解密密钥（企微下发）
    -- ↓ 解密后回填
    content      TEXT,                       -- 可读正文
    msgtype      SMALLINT,                   -- 企微 msgtype（1=文本 2=图片 ...）
    plain        JSONB,                      -- 企微完整明文条目
    content_state TEXT NOT NULL DEFAULT 'pending',  -- pending/ok/empty/fail
    created_at   BIGINT,
    updated_at   BIGINT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (staff_id, session_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_msg_session_time ON chat_message (session_id, msg_time);
CREATE INDEX IF NOT EXISTS idx_msg_staff_time   ON chat_message (staff_id, msg_time DESC);
CREATE INDEX IF NOT EXISTS idx_msg_state        ON chat_message (content_state);
CREATE INDEX IF NOT EXISTS idx_msg_content_trgm ON chat_message USING gin (content gin_trgm_ops);

-- ---------------- 关键历史检索键（架构 §4.4.1）----------------
-- 一条消息的 rule_tags = 「它曾经作为哪些规则的证据被采纳过」，**只增不减**。
-- 为什么用「列 + GIN」而不是新表 message_rule_tag：
--   查询是「整行 + 数组包含」，正是 GIN 的强项；且天然与 message_id 同行，零 join。
--   来源追溯已由 customer_intent_assessment.positive_items[].evidence_message_ids 承载。
ALTER TABLE chat_message
  ADD COLUMN IF NOT EXISTS rule_tags TEXT[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_msg_rule_tags ON chat_message USING gin (rule_tags);

-- ---------------- 同步水位（增量 + 断点续传）----------------
CREATE TABLE IF NOT EXISTS sync_cursor (
    staff_id      TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    last_msg_time BIGINT NOT NULL DEFAULT 0,  -- 该会话已同步到的时间水位（秒）
    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (staff_id, session_id)
);

-- ---------------- 同步任务 ----------------
CREATE TABLE IF NOT EXISTS sync_job (
    id            BIGSERIAL PRIMARY KEY,
    kind          TEXT NOT NULL DEFAULT 'full',   -- full / incremental / single_staff
    status        TEXT NOT NULL DEFAULT 'running',-- running/success/failed/stopped
    triggered_by  TEXT NOT NULL DEFAULT 'manual', -- manual / schedule
    staff_count   INT  NOT NULL DEFAULT 0,
    session_total INT  NOT NULL DEFAULT 0,
    session_done  INT  NOT NULL DEFAULT 0,
    msg_total     BIGINT NOT NULL DEFAULT 0,
    msg_decrypted BIGINT NOT NULL DEFAULT 0,
    msg_new       BIGINT NOT NULL DEFAULT 0,
    detail        JSONB,                          -- {current_staff, current_session, ...}
    error         TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_job_started ON sync_job (started_at DESC);

-- ---------------- 凭据（扫码登录结果）----------------
CREATE TABLE IF NOT EXISTS credential (
    id         SMALLINT PRIMARY KEY DEFAULT 1,
    open_sid   TEXT,
    payload    JSONB,                        -- 完整 bootstrap（含 cookie）
    captured_at BIGINT,
    status     TEXT NOT NULL DEFAULT 'missing', -- missing/ok/expired
    message    TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT credential_single_row CHECK (id = 1)
);

-- ============================================================
--               客户意向评分（规则引擎 + 大模型）
-- ============================================================

-- ---------------- 评分规则（版本化，由 rules.json 初始化，可人工修改）----------------
-- 一条规则的一个档位一行；同一 rule_id 的多档位拆成多行（UNIQUE 含 score）。
CREATE TABLE IF NOT EXISTS scoring_rule (
    id           BIGSERIAL PRIMARY KEY,
    rule_version TEXT NOT NULL,
    rule_id      TEXT NOT NULL,
    polarity     TEXT NOT NULL,               -- positive / negative
    dimension    TEXT NOT NULL,
    score        INT  NOT NULL DEFAULT 0,     -- 该档位分值
    criteria     TEXT,
    engine       TEXT,                        -- ai（语义判断）/ rule（规则引擎硬算）
    source_type  TEXT NOT NULL DEFAULT 'json',-- json/excel/manual
    status       TEXT NOT NULL DEFAULT 'active',
    effective_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (rule_version, rule_id, score)
);
CREATE INDEX IF NOT EXISTS idx_rule_version ON scoring_rule (rule_version, polarity);

-- ---------------- 客户意向评分结果（每次分析一条，可追溯）----------------
CREATE TABLE IF NOT EXISTS customer_intent_assessment (
    assessment_id   BIGSERIAL PRIMARY KEY,
    relationship_key TEXT NOT NULL,           -- staff_id:target_id（需求 §3.2）
    staff_id        TEXT NOT NULL,
    staff_name      TEXT,
    target_id       TEXT,                     -- 即微伴 external_user_id
    target_name     TEXT,
    session_id      TEXT,
    raw_score       INT NOT NULL DEFAULT 0,   -- -12 ~ +13
    positive_total  INT NOT NULL DEFAULT 0,
    negative_total  INT NOT NULL DEFAULT 0,
    grade           TEXT,                     -- S/A/B/C/D
    positive_items  JSONB,                    -- [{rule_id,dimension,score,evidence,reason}]
    negative_items  JSONB,
    invalid_items   JSONB,                    -- 模型越权/非法命中（被忽略，留痕）
    profile_summary TEXT,
    sales_strategy  TEXT,
    confidence      REAL,
    needs_human_review BOOLEAN DEFAULT false,
    rule_version    TEXT,
    msg_count       INT,
    last_msg_time   BIGINT,                   -- 参与本次评分的最后消息时间（水位）
    -- 分流结果（便于前端快速筛选）
    writeback_status TEXT NOT NULL DEFAULT 'na', -- na/skipped/dry_run/ok/failed/pending
    review_status    TEXT NOT NULL DEFAULT 'na', -- na/pending/done/ignored
    analyzed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_assess_rel    ON customer_intent_assessment (relationship_key, assessment_id DESC);
CREATE INDEX IF NOT EXISTS idx_assess_time   ON customer_intent_assessment (analyzed_at DESC);
CREATE INDEX IF NOT EXISTS idx_assess_staff  ON customer_intent_assessment (staff_id);
CREATE INDEX IF NOT EXISTS idx_assess_grade  ON customer_intent_assessment (grade);
CREATE INDEX IF NOT EXISTS idx_assess_review ON customer_intent_assessment (review_status);

-- ---------------- 评分回写微伴（正分自动回写；幂等 + 审计）----------------
-- 微伴「添加客户评分」要求 score 为大于 0 的整数（错误码 20215），
-- 且接口覆盖/累加语义未知 → 默认 dry_run（只记录不真写），可配置切换 live。
CREATE TABLE IF NOT EXISTS score_writeback (
    id            BIGSERIAL PRIMARY KEY,
    relationship_key TEXT NOT NULL,
    assessment_id BIGINT NOT NULL,
    staff_id      TEXT NOT NULL,
    external_user_id TEXT,
    score         INT,                        -- 本次回写的分值（=加分合计）
    mode          TEXT NOT NULL DEFAULT 'dry_run', -- dry_run / live
    status        TEXT NOT NULL DEFAULT 'pending', -- pending/ok/failed/skipped
    attempts      INT NOT NULL DEFAULT 0,
    errcode       INT,
    errmsg        TEXT,
    response      JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (assessment_id)                    -- 一次评估只回写一次（幂等）
);
CREATE INDEX IF NOT EXISTS idx_wb_status ON score_writeback (status);
CREATE INDEX IF NOT EXISTS idx_wb_rel    ON score_writeback (relationship_key);

-- ---------------- 负分人工复核队列（扣分项需人工确认/打分）----------------
CREATE TABLE IF NOT EXISTS negative_review (
    id            BIGSERIAL PRIMARY KEY,
    relationship_key TEXT NOT NULL,
    assessment_id BIGINT NOT NULL,
    staff_id      TEXT NOT NULL,
    staff_name    TEXT,
    target_id     TEXT,
    target_name   TEXT,
    session_id    TEXT,
    ai_negative_total INT NOT NULL DEFAULT 0,
    ai_negative_items JSONB,                  -- AI/规则引擎给出的扣分项（证据）
    human_negative_total INT,                 -- 人工最终扣分合计
    human_negative_items JSONB,               -- 人工确认/修正后的扣分项
    human_note    TEXT,
    reviewer      TEXT,
    status        TEXT NOT NULL DEFAULT 'pending', -- pending(未打分)/done(已完成)/ignored(已忽略)
    raw_score     INT,                        -- 该次评估的总分（参考）
    grade         TEXT,
    last_msg_time BIGINT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at   TIMESTAMPTZ,
    UNIQUE (assessment_id)
);
CREATE INDEX IF NOT EXISTS idx_review_status ON negative_review (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_staff  ON negative_review (staff_id);

-- ---------------- 评分任务运行记录（可观测）----------------
CREATE TABLE IF NOT EXISTS scoring_run (
    id             BIGSERIAL PRIMARY KEY,
    kind           TEXT NOT NULL DEFAULT 'incremental',
    status         TEXT NOT NULL DEFAULT 'running', -- running/success/failed/stopped
    triggered_by   TEXT NOT NULL DEFAULT 'manual',  -- manual/schedule
    rule_version   TEXT,
    staff_count    INT NOT NULL DEFAULT 0,
    session_total  INT NOT NULL DEFAULT 0,
    session_done   INT NOT NULL DEFAULT 0,
    session_skipped INT NOT NULL DEFAULT 0,
    assessment_new INT NOT NULL DEFAULT 0,
    writeback_ok   INT NOT NULL DEFAULT 0,
    writeback_fail INT NOT NULL DEFAULT 0,
    review_new     INT NOT NULL DEFAULT 0,
    llm_calls      INT NOT NULL DEFAULT 0,
    llm_failed     INT NOT NULL DEFAULT 0,
    elapsed_sec    REAL,
    detail         JSONB,
    error          TEXT,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_scoring_run_time ON scoring_run (started_at DESC);

-- ---------------- 审计日志（评分/复核/回写/导出等操作留痕）----------------
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    actor      TEXT,                          -- 操作人（暂无登录体系时可为 system/manual）
    action     TEXT NOT NULL,                 -- review_submit / review_status / writeback / ...
    target     TEXT,                          -- 目标对象标识
    detail     JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log (created_at DESC);

-- ---------------- 运行时设置（可从前端调整，优先于 .env）----------------
CREATE TABLE IF NOT EXISTS app_setting (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- 输出契约（阶段 A）：把「模型输出」从自由 JSON 收敛成可校验的结构化契约
--   · 新增列全部 IF NOT EXISTS，init_db() 整文件幂等执行，无需迁移框架
-- ============================================================================

-- 逐维度表态的漏检集合（engine != rule 的规则里，模型没表态的那些）
ALTER TABLE customer_intent_assessment ADD COLUMN IF NOT EXISTS unchecked_rules   JSONB DEFAULT '[]'::jsonb;
-- 归因用：为什么这次的结论与上次不同（提示词版本 / 契约版本）
ALTER TABLE customer_intent_assessment ADD COLUMN IF NOT EXISTS prompt_version    TEXT;
ALTER TABLE customer_intent_assessment ADD COLUMN IF NOT EXISTS schema_version    TEXT;
-- 模型自报置信度（仅留档，不参与任何决策；代码侧的置信度另算）
ALTER TABLE customer_intent_assessment ADD COLUMN IF NOT EXISTS model_confidence  REAL;
-- 产生这条评估的评分任务（幂等键的一部分，防重复落库）
ALTER TABLE customer_intent_assessment ADD COLUMN IF NOT EXISTS source_run_id     BIGINT;
-- 契约校验实际用了几次 LLM 调用（1 = 一次通过；>1 = 触发了修复重试）
ALTER TABLE customer_intent_assessment ADD COLUMN IF NOT EXISTS contract_attempts INT DEFAULT 1;

-- 幂等键：同一轮评分里同一会话只落一条（source_run_id 为空的历史数据不受约束）
CREATE UNIQUE INDEX IF NOT EXISTS uq_assessment_run_session
    ON customer_intent_assessment (source_run_id, session_id)
    WHERE source_run_id IS NOT NULL;

-- ---------------- 会话级失败明细（修 G5：失败会话说得清、数得准）----------------
CREATE TABLE IF NOT EXISTS session_failure (
    failure_id    BIGSERIAL PRIMARY KEY,
    run_id        BIGINT,
    staff_id      TEXT,
    session_id    TEXT,
    stage         TEXT,                       -- prepare_context / llm_judge / validate_contract / ...
    error_class   TEXT,                       -- EmptyCompletionError / ContractViolation / ...
    error_message TEXT,
    payload       JSONB,                      -- 契约错误的可读清单等
    attempts      INT DEFAULT 0,              -- 连续失败次数（超阈值转 dead）
    status        TEXT DEFAULT 'pending',     -- pending / retrying / dead
    next_retry_at TIMESTAMPTZ,
    -- 以下 4 列原先只由 failure_worker.ensure_columns() 用 ALTER 补（DDL 存在两个来源），
    -- 这里补进权威 schema；旧库仍由 ensure_columns() 幂等补齐
    manual_required BOOLEAN NOT NULL DEFAULT false,  -- 达阈值转人工
    resolved_at     TIMESTAMPTZ,                     -- 处理完成时间
    last_attempt_at TIMESTAMPTZ,                     -- 最近一次重投时间
    resolution      TEXT,                            -- auto / manual
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_session_failure_status ON session_failure (status, next_retry_at);
CREATE INDEX IF NOT EXISTS ix_session_failure_session ON session_failure (staff_id, session_id);
CREATE INDEX IF NOT EXISTS ix_session_failure_manual ON session_failure (manual_required) WHERE manual_required;

-- ---------------- 长任务心跳（修「进程被杀 → 卡死的锁阻塞数小时」）----------------
-- 为什么需要：`is_running()` 靠 DB 里的 status='running' 做**跨进程**互斥，
-- 但进程被 kill（或崩溃）时来不及改状态 → 记录永久停在 running →
-- 后续所有评分/同步都被拦住，直到「按 started_at 算的 240 分钟超时」才放行。
-- 实测：一次调试中 `sync_job` 留下一个 running 行，直接把评分阻塞到超时。
-- 现在每次进度更新（`_bump`）顺带刷新 `heartbeat_at`：心跳停了 = 进程没了 →
-- 分钟级判定僵尸并放行；`started_at` 的超时判定保留为二级兜底（兼容没有心跳的老行）。
ALTER TABLE sync_job    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
ALTER TABLE scoring_run ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;

-- ---------------- 跨进程停止（修「点停止没反应」）----------------
-- 为什么需要：`scoring_service.stop()` 会把停止请求写进 `scoring_run.stop_requested`，
-- 于是**任何进程**（后端 / 调度器 / 脚本）里的那一轮都能看到并退出；而同步原先只设
-- 进程内标志 `self._stop` —— 同步跑在调度器/别的进程时，UI 点「停止」会返回
-- 「已发出停止信号」，但那个进程根本读不到，同步继续跑（而它在跑时评分被互斥挡住）。
-- 实测踩到：一条 44 分钟前启动、进程已被重启杀掉的同步行，让「开始评分」被拒 15 分钟。
ALTER TABLE sync_job ADD COLUMN IF NOT EXISTS stop_requested BOOLEAN NOT NULL DEFAULT false;

-- ============================================================================
-- 阶段 B：回写解耦（修 G6）+ 本地链路追踪
--   · 全部 IF NOT EXISTS，init_db() 整文件幂等执行，无需迁移框架
-- ============================================================================

-- ---------------- 回写 outbox（修 G6：回写失败不再永久丢失）----------------
-- 与 assessment 同事务入队：即使进程被 kill，任务仍在，独立于增量水位向前推进。
--   幂等分工：outbox UNIQUE(assessment_id) 保证「一定会尝试」；
--             score_writeback UNIQUE(assessment_id) 保证「一定只落一行」。
CREATE TABLE IF NOT EXISTS writeback_outbox (
    outbox_id       BIGSERIAL PRIMARY KEY,
    assessment_id   BIGINT NOT NULL,
    relationship_key TEXT NOT NULL,
    staff_id        TEXT NOT NULL,
    target_id       TEXT NOT NULL,
    score           INT  NOT NULL,                     -- 正分合计（>=1），语义同 score_writeback.score
    mode            TEXT NOT NULL DEFAULT 'live',      -- 入队时刻的意图（审计用）
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending/failed/done/skipped/dead
    attempts        INT  NOT NULL DEFAULT 0,
    max_attempts    INT  NOT NULL DEFAULT 8,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_errcode    INT,
    last_errmsg     TEXT,
    last_response   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (assessment_id)                             -- 一次评估只入队一条
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON writeback_outbox (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_outbox_rel ON writeback_outbox (relationship_key);
-- 结果未知、禁止自动重投（2026-10-16）：微伴评分为**累加语义、不可撤销**，
-- 所以「HTTP 已发出但没拿到结果」（超时 / 断连 / 进程被杀）**不能重投** ——
-- 重投就是再多加一次分。这类行置 needs_reconcile=true 并停投，等人工用
-- `weiban.list_external_users` 读回客户当前累计分后判定「到底写没写」。
ALTER TABLE writeback_outbox ADD COLUMN IF NOT EXISTS needs_reconcile BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS idx_outbox_reconcile ON writeback_outbox (needs_reconcile)
    WHERE needs_reconcile;

-- ---------------- 本地链路追踪（阶段 B，默认关闭，由 trace_enabled 打开）----------------
-- 只存「结构指纹」与计数，绝不落聊天正文（PII 与体积控制）。
CREATE TABLE IF NOT EXISTS trace_span (
    span_id      BIGSERIAL PRIMARY KEY,
    run_id       BIGINT,
    session_id   TEXT,
    node         TEXT NOT NULL,
    attempt      INT NOT NULL DEFAULT 1,
    ok           BOOLEAN NOT NULL DEFAULT true,
    error_class  TEXT,
    elapsed_ms   INT,
    input_digest  TEXT,                                -- sha256[:16] 结构投影
    output_digest TEXT,
    prompt_tokens     INT,
    completion_tokens INT,
    total_tokens      INT,
    detail       JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trace_run  ON trace_span (run_id, span_id);
CREATE INDEX IF NOT EXISTS idx_trace_node ON trace_span (node, created_at DESC);

-- ---------------- 评估集（第五章 E5 / 闸6）----------------
-- 目的：回答「判得准不准」。**只标"命中了哪些 rule_id（含档位分值）"，不标总分** ——
-- 总分由 rules.py 算；让人工标总分等于让人实现一遍规则引擎（架构 §4.1 原文）。
-- messages_hash 是对话内容指纹：跑评估前校验，不一致说明会话内容已变，该样本失效。
CREATE TABLE IF NOT EXISTS eval_case (
    case_id       BIGSERIAL PRIMARY KEY,
    kind          TEXT NOT NULL DEFAULT 'golden',   -- golden / regression / edge
    staff_id      TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    target_id     TEXT,
    staff_name    TEXT,
    target_name   TEXT,
    session_name  TEXT,
    messages_hash TEXT,                             -- 对话内容指纹（防漂移）
    msg_count     INT,
    last_msg_time BIGINT,
    -- ---- 人工标注（金标准）----
    expected_items     JSONB,                       -- [{rule_id, polarity, score, reason}]
    expected_raw_score INT,                         -- 由 rules.compute_score 算出（非人工标）
    expected_grade     TEXT,
    -- ---- 基线快照（记录"改动前"系统怎么判，用于前后对比）----
    baseline_items        JSONB,
    baseline_raw_score    INT,
    baseline_grade        TEXT,
    baseline_rule_version TEXT,
    baseline_prompt_version TEXT,
    baseline_at           TIMESTAMPTZ,
    -- ---- 元数据 ----
    source    TEXT NOT NULL DEFAULT 'manual',       -- manual / review / sample
    disputed  BOOLEAN NOT NULL DEFAULT false,       -- 存在标注争议
    annotator TEXT,
    note      TEXT,
    active    BOOLEAN NOT NULL DEFAULT true,
    frozen    BOOLEAN NOT NULL DEFAULT false,       -- 冻结后除仲裁外不得修改
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, staff_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_eval_kind   ON eval_case (kind, active, case_id DESC);
CREATE INDEX IF NOT EXISTS idx_eval_seen   ON eval_case (staff_id, session_id);

-- 每次跑评估集的结果（回答"这轮改动把指标抬了还是压了"，也留门禁判据）
CREATE TABLE IF NOT EXISTS eval_run (
    id            BIGSERIAL PRIMARY KEY,
    kind          TEXT,                             -- 跑的是哪一层（可空=全部）
    rule_version  TEXT,
    prompt_version TEXT,
    case_total    INT,
    case_ok       INT,
    metrics       JSONB,                            -- {rule_f1, precision, recall, grade_acc, fp_rate, ...}
    baseline_id   INT,                              -- 对比的基线（引用 eval_run.id）
    gate_passed   BOOLEAN,
    gate_diffs    JSONB,                            -- [{metric, base, now, delta}]
    status        TEXT NOT NULL DEFAULT 'running',  -- running / success / failed
    error         TEXT,
    elapsed_sec   DOUBLE PRECISION,
    detail        JSONB,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_eval_run_time ON eval_run (started_at DESC);

-- 复核改判可回流成评估样本（架构 §4.2 的 negative_review.eval_case_id）
ALTER TABLE negative_review ADD COLUMN IF NOT EXISTS eval_case_id BIGINT;

-- 跨进程停止请求（停止是「另一个进程发的请求」，不能被本进程标志表达）。
-- 为什么要单独一列而不是塞进 detail：`_bump` 每轮都会整体覆盖 detail，标记会被冲掉。
ALTER TABLE scoring_run ADD COLUMN IF NOT EXISTS stop_requested BOOLEAN NOT NULL DEFAULT false;

-- ---------------- 置信度三层合成（第六章 E4）----------------
-- 铁律：置信度必须「算出来」，不能「问出来」。
--   · model_confidence  = 模型自报值，**仅留档观测，不参与任何决策**（已有列）
--   · session_confidence = 系统算出（命中项置信均值 × 覆盖率 − 无效项惩罚 + 一致性加成），参与复核触发
--   · verification_status = 人工验证状态，与上面两者分离 —— 避免复核员误以为
--     「模型说 0.9」就等于「验证过了」
ALTER TABLE customer_intent_assessment
  ADD COLUMN IF NOT EXISTS session_confidence REAL;
ALTER TABLE customer_intent_assessment
  ADD COLUMN IF NOT EXISTS verification_status TEXT NOT NULL DEFAULT 'na';
-- 为什么触发人工复核（可追溯）：["has_negative","low_confidence","grade_boundary",...]
ALTER TABLE customer_intent_assessment
  ADD COLUMN IF NOT EXISTS review_reasons JSONB;
CREATE INDEX IF NOT EXISTS ix_assess_session_conf
  ON customer_intent_assessment (session_confidence);

-- ---------------- Loop 护栏（第二章 G3/G4/B3、§7.2）----------------
-- deadline：整轮总超时。改造前「超时」只能靠单会话内部重试耗时（最多 3×120 秒）
--   自然累积，卡住一整轮也无法中断 —— 实测一次批量跑了 2.7 小时才发现没有总闸。
ALTER TABLE scoring_run ADD COLUMN IF NOT EXISTS deadline TIMESTAMPTZ;
-- max_llm_calls：成本预算。LLM 调用是真花钱的，必须能设上限（G4）。
ALTER TABLE scoring_run ADD COLUMN IF NOT EXISTS max_llm_calls INT;
-- session_failed：失败会话数。改造前只累加在内存 detail 里、**没落列**，
--   导致「这轮挂了几个会话」在 SQL 层面查不到（G5 的配套）。
ALTER TABLE scoring_run ADD COLUMN IF NOT EXISTS session_failed INT NOT NULL DEFAULT 0;
-- partial_reason：为何以 partial 结束（deadline / max_llm_calls / stop）。
ALTER TABLE scoring_run ADD COLUMN IF NOT EXISTS partial_reason TEXT;

-- ---------------- 归因与留痕字段（架构 §7.2 / B1 / 闸2 / F6）----------------
-- model_name：实际返回的模型名（非配置值）。与 prompt_version / schema_version
--   一起构成「结果变更可归因」—— 换模型后分数变了，能立刻看出是谁换的。
ALTER TABLE customer_intent_assessment ADD COLUMN IF NOT EXISTS model_name TEXT;

-- P2（时间锚点）：这次判定用的是哪个"现在"（epoch 秒）。
--   为什么必须落库：`negative_no_reply_2d` 这类引擎项按 `as_of - 最后消息时间` 算，
--   不记录锚点就无法解释"当时为什么这么判"，也无法复现（时间一过结论就换）。
--   与其同时：评估集用**固定锚点**（`eval_case.anchor_ts`）跑，指标才可比。
ALTER TABLE customer_intent_assessment ADD COLUMN IF NOT EXISTS data_as_of BIGINT;

-- P2：评估样本的时间锚点（取该样本 baseline 那次评估的时刻）。
ALTER TABLE eval_case ADD COLUMN IF NOT EXISTS anchor_ts BIGINT;
-- rule_checks：模型**逐维度原始表态**落库（此前只在契约内校验、不落库）。
--   与 unchecked_rules 互补：后者只说「漏了哪些」，前者说「表了什么态、给了多少分」。
ALTER TABLE customer_intent_assessment ADD COLUMN IF NOT EXISTS rule_checks JSONB;
-- old_raw_score：上一次有效分数（对比「这次涨了还是跌了」）。F6，①② 记忆段也会用到
ALTER TABLE customer_intent_assessment ADD COLUMN IF NOT EXISTS old_raw_score INT;
-- last_notified_grade：**已删除**（2026-10-16 审计）。
--   当初按 F6 加列「上次通知过的等级」，但本系统**没有任何通知出口**（架构说明书 §7.2
--   自己也标注「待通知流程，本系统暂无通知出口」）→ 列加了、永远为 NULL、也没有读方。
--   空列比缺列更坏：读 schema 的人会以为通知功能已经存在。要做通知时**连带建列**。
ALTER TABLE customer_intent_assessment DROP COLUMN IF EXISTS last_notified_grade;

-- ---------------- E2：当前有效标记（列表去重）----------------
-- 语义：同一 relationship_key 下**只有最新一条** is_current=true。
-- 历史行置 false 并记 superseded_by=新行 id —— 这样「某客户当前的结论」是 O(1) 查询，
-- 同时保留完整历史（可回答「这个客户是怎么一路变过来的」）。
-- ⚠️ 与 `uq_assessment_run_session` 互补：那个约束保证「同一轮同一会话只落一条」，
--    本列保证「同一客户在列表里只出现一条」。两者解决的是不同层面的重复。
ALTER TABLE customer_intent_assessment ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE customer_intent_assessment ADD COLUMN IF NOT EXISTS superseded_by BIGINT;
CREATE INDEX IF NOT EXISTS idx_assess_current ON customer_intent_assessment (relationship_key) WHERE is_current;
-- 历史数据回填：每个客户只保留最新一条为 current（幂等，可重复执行）
UPDATE customer_intent_assessment a SET is_current = false
 WHERE EXISTS (SELECT 1 FROM customer_intent_assessment b
                WHERE b.relationship_key = a.relationship_key
                  AND b.assessment_id > a.assessment_id);

-- ---------------- E3：结构化画像（带逐槽位证据）----------------
-- 固定 8 槽位（记忆方案 M-B）：position/company/city/need/budget/timeline/concerns/competitor。
-- profile_summary 保留为「自由文本补充」，本列才是可查询、可跨次对比的那一份。
ALTER TABLE customer_intent_assessment ADD COLUMN IF NOT EXISTS profile_struct JSONB;

-- ---------------- 闸1/F5：规则可机读化 ----------------
-- examples：每个分值档的 positive / negative / ambiguous 三档示例（钉住边界）
-- conflict_priority：两条规则同时像命中时先算哪条（替代提示词里硬编码的冲突说明）
ALTER TABLE scoring_rule ADD COLUMN IF NOT EXISTS examples JSONB;
ALTER TABLE scoring_rule ADD COLUMN IF NOT EXISTS conflict_priority INT;

-- ---------------- D2-1：客户级回写水位（差值累加）----------------
-- 背景（架构 §10 D2）：微伴「客户评分」= **累计行为分，无覆盖接口**。
-- 所以「整值回写」会把同一个分数反复累加 → 客户分数虚高。
-- 正确做法：每次只写**增量** = 本次 positive_total − 该客户已回写累计。
-- 本表就是那个「已回写累计」的水位（一客户一行）。
-- ⚠️ 与 `score_writeback` 的分工：
--    · `score_writeback` = 每次回写的**流水**（幂等记账，UNIQUE(assessment_id)）
--    · `writeback_ledger` = 客户的**累计水位**（差值计算的依据）
--    只有 live 成功投递才推进水位 —— dry_run 不推进，因为「没真写就不算已写」。
CREATE TABLE IF NOT EXISTS writeback_ledger (
    relationship_key   TEXT PRIMARY KEY,
    staff_id           TEXT,
    external_user_id   TEXT,
    written_total      INT NOT NULL DEFAULT 0,   -- 已回写累计分
    last_assessment_id BIGINT,
    last_write_at      TIMESTAMPTZ,
    writes             INT NOT NULL DEFAULT 0,   -- 成功回写次数
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------- H2：请求幂等（关键操作防重复提交）----------------
-- 互斥守卫管「不能同时跑两个」，本表管「同一个请求不要跑两遍」——两者互补。
-- 调用方给 request_key 时：首次执行并落结果，重复到达直接复用首次结果。
CREATE TABLE IF NOT EXISTS request_guard (
    request_key TEXT PRIMARY KEY,
    endpoint    TEXT NOT NULL,
    response    JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_request_guard_time ON request_guard (created_at);

-- ---------------- 评估集「AI 预标注」标记 ----------------
-- 用途：把 AI 当时的判定（baseline_items）预填成 expected_items 初稿，人工只需**复核修正**
--   → 从空白开始标 30 条很慢，从草稿改很快。
-- ⚠️ 为什么必须单独打标（而不是直接当已标注）：预填内容来自 AI 自己，
--   若计入指标就成了「AI 批改自己」→ 指标虚高、门禁形同虚设。
--   所以口径：**默认只统计 `NOT prefilled`（人工确认过的）**，prefilled 仅供对照。
ALTER TABLE eval_case ADD COLUMN IF NOT EXISTS prefilled BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS idx_eval_prefilled ON eval_case (kind) WHERE prefilled;

-- ---------------- D2-1：等级改用微伴标签（与分数回写完全独立）----------------
-- 背景（架构 §10 D2）：微伴「客户评分」只能累加、无覆盖接口 → 分数必须差值回写；
-- 而**标签有增有删**，用 `tag/batch_update` 一次 add 新等级 + remove 旧等级
-- 天然表达「当前等级」，不存在「加了新的、旧的没删掉」的中间态。
--
-- ⚠️ 为什么**不复用** `writeback_outbox` 与 `writeback_ledger`：
--    · `outbox._record()` 会写 `score_writeback` 表并**推进分数水位** `writeback_ledger`
--    · 标签任务若混进去，会被当成一次「分数回写」→ **差值累加被污染**（客户分数虚高）
--    · 且两者的幂等键与重试语义不同（分数：一个评估一次；标签：同一客户只认最新等级）
CREATE TABLE IF NOT EXISTS grade_tag_ledger (
    relationship_key   TEXT PRIMARY KEY,
    staff_id           TEXT,
    external_user_id   TEXT,
    grade              TEXT,                    -- 已写入的等级（S/A/B/C/D）
    tag_id             TEXT,                    -- 已写入的标签 id
    last_assessment_id BIGINT,
    last_write_at      TIMESTAMPTZ,
    writes             INT NOT NULL DEFAULT 0,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS grade_tag_outbox (
    outbox_id        BIGSERIAL PRIMARY KEY,
    assessment_id    BIGINT NOT NULL,
    relationship_key TEXT NOT NULL,
    staff_id         TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    grade            TEXT NOT NULL,             -- 目标等级
    remove_tag_id    TEXT,                      -- 要摘掉的旧标签（入队时**固化**，出队不再算）
    mode             TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    attempts         INT NOT NULL DEFAULT 0,
    max_attempts     INT NOT NULL DEFAULT 8,
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_errcode     INT,
    last_errmsg      TEXT,
    last_response    JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (assessment_id)
);
CREATE INDEX IF NOT EXISTS idx_grade_tag_due ON grade_tag_outbox (next_attempt_at) WHERE status IN ('pending','failed');

-- ============================================================================
-- 生产化加固（见《客户意向评分_架构设计说明书.md》第十章 数据模型）
--   · 工具调用审计（五问）      → tool_audit
--   · 策略判定留痕              → policy_decision
--   · 任务运行时（外循环）      → task / task_step / approval_request
--   · 全部 IF NOT EXISTS，init_db() 整文件幂等执行，无需迁移框架（ADR-012）
-- ============================================================================

-- ---------------- 工具调用审计（架构 §6.2，硬伤 5）----------------
-- 与 audit_log 的分工：
--   · audit_log      = **业务动作**审计（提交复核、发布规则、改配置）
--   · tool_audit     = **工具调用**审计（调了微伴/企微/大模型，参数是什么、谁批的、结果如何）
-- 为什么必须落参数：文章 B 的五问里「为什么」只能靠参数回答；
-- 但参数里常带 token / 密钥 → 写库前**必须脱敏**（见 audit.redact()）。
CREATE TABLE IF NOT EXISTS tool_audit (
    call_id          BIGSERIAL PRIMARY KEY,
    task_id          BIGINT,                       -- 关联任务（可空：历史/单次调用）
    step_id          BIGINT,
    actor_id         TEXT NOT NULL,                -- 谁（服务端推导，非请求体自报）
    tool_name        TEXT NOT NULL,                -- 什么（如 weiban.add_customer_score）
    params_redacted  JSONB NOT NULL DEFAULT '{}'::jsonb,   -- 参数（已脱敏）
    decision         TEXT NOT NULL DEFAULT 'allow'
                     CHECK (decision IN ('allow','confirm','deny')),
    decision_reason  TEXT,                         -- 为何（allow/deny 的依据）
    approval_source  TEXT,                         -- policy_auto | user_approved
    idempotency_key  TEXT UNIQUE,                  -- 工具级幂等（三层里的第一层）
    result_status    TEXT,                         -- ok|failed|denied|skipped
    errcode          INT,
    errmsg           TEXT,
    response_digest  TEXT,                         -- 响应只存指纹，不存正文（PII/体积）
    elapsed_ms       INT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()    -- 何时
);
CREATE INDEX IF NOT EXISTS idx_tool_audit_time  ON tool_audit (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_audit_tool  ON tool_audit (tool_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_audit_actor ON tool_audit (actor_id, created_at DESC);
-- 被拒绝 / 需人工的调用是排障与合规最该看的信号（文章 B：被拒绝的请求尤其要记）
CREATE INDEX IF NOT EXISTS idx_tool_audit_deny  ON tool_audit (decision) WHERE decision <> 'allow';

-- ---------------- 策略判定留痕（架构 §6.5，硬伤 4）----------------
-- 为什么要把阈值一起存：置信度阈值会随业务调整，**只存结论不存判据**的话，
-- 三个月后没人能解释"这条为什么当时自动执行了"。存快照 → 结论可复现。
CREATE TABLE IF NOT EXISTS policy_decision (
    decision_id        BIGSERIAL PRIMARY KEY,
    task_id            BIGINT,
    relationship_key   TEXT NOT NULL,
    assessment_id      BIGINT,
    session_confidence REAL,
    review_reasons     JSONB,
    action             TEXT NOT NULL CHECK (action IN ('auto','confirm','human')),
    action_reasons     JSONB,
    thresholds         JSONB,                      -- 当时的阈值快照
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_policy_action ON policy_decision (action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_policy_rel    ON policy_decision (relationship_key, decision_id DESC);

-- ---------------- 任务运行时（架构 §6.3/§6.4，硬伤 3）----------------
-- 收敛四处分散状态机为一个 `task`；`task_step` 是**检查点**：
-- 不只记"跑到第几步"，还记 next_step（下一步该做什么）与 output_ref（已产出的结果在哪）。
CREATE TABLE IF NOT EXISTS task (
    task_id         BIGSERIAL PRIMARY KEY,
    kind            TEXT NOT NULL,                 -- scoring|sync|eval|grade_tag|retention
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','running','waiting_approval','paused',
                                      'succeeded','partial','failed','cancelled')),
    actor_id        TEXT NOT NULL,                 -- 发起人（服务端推导）
    trigger_src     TEXT NOT NULL DEFAULT 'manual',-- manual|schedule|public_class|system
    scope           JSONB,
    priority        INT NOT NULL DEFAULT 5,
    budget_calls    INT,
    budget_tokens   BIGINT,
    cost_calls      INT NOT NULL DEFAULT 0,
    cost_tokens     BIGINT NOT NULL DEFAULT 0,
    deadline_at     TIMESTAMPTZ,
    idempotency_key TEXT UNIQUE,
    last_step_seq   INT NOT NULL DEFAULT 0,        -- **恢复点**
    progress        JSONB NOT NULL DEFAULT '{}'::jsonb,
    partial_reason  TEXT,
    error_class     TEXT,
    error_message   TEXT,
    heartbeat_at    TIMESTAMPTZ,                   -- 僵尸判定（INV-16）
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_task_status ON task (status, priority, task_id);
CREATE INDEX IF NOT EXISTS idx_task_beat   ON task (heartbeat_at) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS task_step (
    step_id       BIGSERIAL PRIMARY KEY,
    task_id       BIGINT NOT NULL REFERENCES task(task_id) ON DELETE CASCADE,
    seq           INT NOT NULL,
    name          TEXT NOT NULL,                   -- 如 session:score / writeback:drain
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','running','succeeded','failed','skipped')),
    input_ref     JSONB,                           -- {session_id, watermark, ...} 不放正文
    output_ref    JSONB,                           -- {assessment_id, outbox_id, ...}
    next_step     TEXT,                            -- **检查点的核心**：下一步做什么
    attempts      INT NOT NULL DEFAULT 0,
    error_class   TEXT,
    error_message TEXT,
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    UNIQUE (task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_step_task ON task_step (task_id, seq);

-- 高风险操作的审批门：把"先计划后执行"从软约束变成硬约束
-- （文章 H：teammate 提交计划后被**物理阻塞**，而不是靠提示词自觉）
CREATE TABLE IF NOT EXISTS approval_request (
    request_id      BIGSERIAL PRIMARY KEY,
    task_id         BIGINT NOT NULL REFERENCES task(task_id),
    step_id         BIGINT,
    tool_name       TEXT NOT NULL,
    params_redacted JSONB NOT NULL DEFAULT '{}'::jsonb,
    requested_by    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected','expired')),
    decided_by      TEXT,
    decided_at      TIMESTAMPTZ,
    note            TEXT,
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_approval_pending ON approval_request (status, created_at DESC);

-- 回写审批（2026-10-16 接线）：`policy` 判定为 confirm / human 的**回写**要人工签字才放行。
--   两处放宽/新增的理由：
--   · `task_id` 原来 NOT NULL —— 但回写审批**不应该阻塞整个评分批次**（`request_approval`
--     会把任务推到 `waiting_approval`）；它只卡住那一条客户回写，所以 task_id 允许为空。
--   · `assessment_id` —— 审批对象是"哪一条评估的回写"，没有它就无法在放行时找回要投递的分数。
ALTER TABLE approval_request ALTER COLUMN task_id DROP NOT NULL;
ALTER TABLE approval_request ADD COLUMN IF NOT EXISTS assessment_id BIGINT;
CREATE INDEX IF NOT EXISTS idx_approval_assessment ON approval_request (assessment_id);

-- 关联：业务运行记录 → 统一任务（历史数据为 NULL，不破坏兼容）
ALTER TABLE scoring_run ADD COLUMN IF NOT EXISTS task_id BIGINT;
ALTER TABLE sync_job    ADD COLUMN IF NOT EXISTS task_id BIGINT;
-- 结论可按策略判定追溯（硬伤 4 的落库侧）
ALTER TABLE customer_intent_assessment ADD COLUMN IF NOT EXISTS task_id BIGINT;
ALTER TABLE customer_intent_assessment ADD COLUMN IF NOT EXISTS policy_action TEXT;
-- 幂等记录的 TTL 可在 SQL 层判断（原先只能靠应用层时间差）
ALTER TABLE request_guard ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
-- 续跑：新任务指向它从哪个任务恢复（检查点来源）。历史数据为 NULL。
ALTER TABLE task ADD COLUMN IF NOT EXISTS resumed_from BIGINT;

-- ---------------- 登录会话（Cookie 会话，取代浏览器 Basic 弹框）----------------
-- 为什么需要它：原先全站走 HTTP Basic，**首屏就弹浏览器原生登录框**（样式不可控、
-- 无法「记住我」、无法显示当前用户、退出还得关浏览器）。改法不是废掉 Basic
-- （脚本 / curl / 测试仍用它），而是在其之上加一层**服务端会话**：
--   · 浏览器只存**不透明会话 id**（HttpOnly Cookie），口令不再落到前端存储；
--   · 会话可撤销（退出 = 删行 + 清 Cookie）、可过期（勾选记住我 → 30 天；
--     不勾 → 12 小时，且浏览器关闭即失效）；
--   · `last_seen_at` 让运维能看出「谁在用、最后一次什么时候」。
CREATE TABLE IF NOT EXISTS login_session (
    sid          TEXT PRIMARY KEY,              -- 随机 token（32 字节 base64url）
    username     TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    user_agent   TEXT,
    client_ip    TEXT
);
CREATE INDEX IF NOT EXISTS idx_login_session_exp ON login_session (expires_at);
