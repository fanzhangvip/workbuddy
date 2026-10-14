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
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_session_failure_status ON session_failure (status, next_retry_at);
CREATE INDEX IF NOT EXISTS ix_session_failure_session ON session_failure (staff_id, session_id);

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
