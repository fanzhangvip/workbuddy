import axios from 'axios'

const http = axios.create({ baseURL: '/api', timeout: 120000 })

// 401（会话过期 / 未登录）→ 通知上层跳登录页。
// 用「注册回调」而不是在这里 import router：api.js 是纯 HTTP 层，
// 让它依赖路由会把「发请求」和「导航」两件事绑死，也不好测。
//
// ⚠️ 必须是**回调列表**，不能只有一个槽位：`useAuth`（清状态）与 `App.vue`
//    （跳登录页）都要挂 —— 单槽位会让后注册的覆盖先注册的，表现为「状态清了但不跳转」。
//    真踩到过：会话过期后页面一直空转、每 3 秒 401，看起来像"服务挂了"，
//    而路由守卫只在**导航时**才跑，停在原页的标签页永远等不到那次导航。
const _unauthorizedHandlers = []
export function onUnauthorized(handler) {
  if (handler && !_unauthorizedHandlers.includes(handler)) _unauthorizedHandlers.push(handler)
}

/**
 * 生成一个**幂等键**，给"会写外部系统 / 不可逆"的动作用。
 *
 * 后端早就有 `request_key` 幂等（`idempotency.run_once`，start / settings /
 * rules-publish / retention-purge / baseline / approval / task-resume 都支持），
 * 但**前端从来没传过** —— 于是那套保护在界面上等于不存在：
 * 网络重试、代理重发、用户手抖双击都可能真执行两次。
 *
 * 用法：**一次动作一个 key，重试复用、成功即弃**
 * （`keyFor('publish')` 失败后仍返回同一个 key → 后端直接回放首次结果）。
 */
export function newRequestKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  return `k-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

http.interceptors.response.use(
  (r) => r,
  (err) => {
    const status = err?.response?.status
    // 登录接口自身 401 是「口令错」的正常返回，不该触发跳转（否则登录页会被自己踢走）
    const url = String(err?.config?.url || '')
    if (status === 401 && !url.includes('/auth/session')) {
      for (const h of _unauthorizedHandlers) {
        try { h(err) } catch (e) { /* 某个回调坏了不该拖累其它回调 */ }
      }
    }
    return Promise.reject(err)
  },
)

export const api = {
  // 鉴权
  authStatus: () => http.get('/auth/status').then(r => r.data),
  authLogin: (payload = {}) => http.post('/auth/login', payload).then(r => r.data),
  authVerify: () => http.post('/auth/verify').then(r => r.data),
  // 登录本系统（与上面的「微伴扫码登录」是两件不同的事）
  authMe: () => http.get('/auth/me').then(r => r.data),
  authSignIn: (payload) => http.post('/auth/session', payload).then(r => r.data),
  authSignOut: () => http.delete('/auth/session').then(r => r.data),

  // 同步
  syncStart: (payload) => http.post('/sync/start', payload).then(r => r.data),
  syncStop: () => http.post('/sync/stop').then(r => r.data),
  syncStatus: () => http.get('/sync/status').then(r => r.data),
  syncJobs: (limit = 20) => http.get('/sync/jobs', { params: { limit } }).then(r => r.data),
  syncSchedule: () => http.get('/sync/schedule').then(r => r.data),
  syncSetSchedule: (minutes) => http.post('/sync/schedule', { interval_minutes: minutes }).then(r => r.data),
  // 日常任务（需求 4.2）：主同步+分析 / 补偿同步+分析 —— 时刻与窗口都可在后台配
  syncSetDaily: (payload) => http.post('/sync/schedule/daily', payload).then(r => r.data),
  syncSetKeepalive: (minutes) => http.post('/sync/schedule', { keepalive_interval_minutes: minutes }).then(r => r.data),

  // 凭据保活（微伴登录态滑动过期，定期访问即可续期）
  keepaliveStatus: () => http.get('/sync/keepalive').then(r => r.data),
  keepaliveRun: (wait = 45) => http.post('/sync/keepalive', { wait }).then(r => r.data),

  // 查询
  stats: () => http.get('/stats').then(r => r.data),
  staffs: (params) => http.get('/staffs', { params }).then(r => r.data),
  sessions: (params) => http.get('/sessions', { params }).then(r => r.data),
  sessionDetail: (params) => http.get('/session', { params }).then(r => r.data),
  messages: (params) => http.get('/messages', { params }).then(r => r.data),
  search: (params) => http.get('/search', { params }).then(r => r.data),
  recentSessions: (params) => http.get('/sessions/recent', { params }).then(r => r.data),

  // ---------------- 客户意向评分（评分中心）----------------
  scoringStart: (payload = {}) => http.post('/scoring/start', payload).then(r => r.data),
  scoringStop: () => http.post('/scoring/stop').then(r => r.data),
  scoringStatus: () => http.get('/scoring/status').then(r => r.data),
  scoringRuns: (limit = 20) => http.get('/scoring/runs', { params: { limit } }).then(r => r.data),
  scoringMetrics: () => http.get('/scoring/metrics').then(r => r.data),
  scoringAssessments: (params) => http.get('/scoring/assessments', { params }).then(r => r.data),
  scoringAssessment: (assessment_id) => http.get('/scoring/assessment', { params: { assessment_id } }).then(r => r.data),
  scoringRules: (rule_version = '') => http.get('/scoring/rules', { params: { rule_version } }).then(r => r.data),
  scoringSession: (staff_id, session_id) => http.post('/scoring/session', { staff_id, session_id }).then(r => r.data),
  scoringSchedule: () => http.get('/scoring/schedule').then(r => r.data),
  scoringSetSchedule: (minutes) => http.post('/scoring/schedule', { interval_minutes: minutes }).then(r => r.data),
  scoringSettings: () => http.get('/scoring/settings').then(r => r.data),
  scoringSetSettings: (payload) => http.post('/scoring/settings', payload).then(r => r.data),
  // 架构第 2–4 批新增：F2 复核入口 / 闸6 门禁 / 边界4 保留期
  scoringSaReview: (scope = '') => http.get('/scoring/sa-review', { params: { scope } }).then(r => r.data),
  scoringPublicClassReview: (payload) => http.post('/scoring/public-class-review', payload).then(r => r.data),
  scoringRulesGate: () => http.get('/scoring/rules/gate').then(r => r.data),
  scoringRulesPublish: (payload) => http.post('/scoring/rules/publish', payload).then(r => r.data),
  scoringRetention: (days) => http.get('/scoring/retention', { params: { days } }).then(r => r.data),
  scoringRetentionPurge: (payload) => http.post('/scoring/retention/purge', payload).then(r => r.data),
  // 回写对账（结果未知的行：请求发出去了但没拿到微伴回话，禁止自动重投）
  scoringWritebackReconcileList: (limit = 50) =>
    http.get('/scoring/writeback/reconcile', { params: { limit } }).then(r => r.data),
  scoringWritebackReconcile: (payload) =>
    http.post('/scoring/writeback/reconcile', payload).then(r => r.data),
  // D2-1 等级标签（独立于分数回写）
  scoringGradeTags: () => http.get('/scoring/grade-tags').then(r => r.data),
  scoringGradeTagSync: (payload) => http.post('/scoring/grade-tags/sync', payload).then(r => r.data),
  scoringGradeTagAlign: (payload) => http.post('/scoring/grade-tags/align', payload).then(r => r.data),
  scoringGradeTagDrain: (payload) => http.post('/scoring/grade-tags/drain', payload || {}).then(r => r.data),
  // E5 meta-loop：误报率排行 + 改判回流
  evalMetaLoop: () => http.get('/eval/meta-loop').then(r => r.data),
  // AI 预标注：把 AI 当时判定填成草稿（人工只复核）；/run 是同步跑分
  evalPrefill: (payload) => http.post('/eval/prefill', payload).then(r => r.data),
  evalBackflow: (payload) => http.post('/eval/backflow', payload).then(r => r.data),
  // 回写审批（策略判定 confirm/human 的客户回写等人工签字）
  scoringApprovals: (status = 'pending', limit = 50) =>
    http.get('/scoring/approvals', { params: { status, limit } }).then(r => r.data),
  scoringApprovalDecide: (request_id, payload = {}) =>
    http.post(`/scoring/approvals/${request_id}`, payload).then(r => r.data),
  // 批次任务（外循环 task/task_step：看走到哪、取消、从检查点续跑）
  scoringTasks: (params) => http.get('/scoring/tasks', { params }).then(r => r.data),
  scoringTaskStats: (days = 30) => http.get('/scoring/tasks/stats', { params: { days } }).then(r => r.data),
  scoringTaskDetail: (task_id, steps = 50) =>
    http.get(`/scoring/tasks/${task_id}`, { params: { steps } }).then(r => r.data),
  scoringTaskCancel: (task_id, payload = {}) =>
    http.post(`/scoring/tasks/${task_id}/cancel`, payload).then(r => r.data),
  scoringTaskResume: (task_id) => http.post(`/scoring/tasks/${task_id}/resume`).then(r => r.data),
  // 审计留痕（策略分布 / 工具调用五问）
  scoringPolicyStats: (days = 30) => http.get('/scoring/policy/stats', { params: { days } }).then(r => r.data),
  scoringAuditStats: (days = 30) => http.get('/scoring/audit/stats', { params: { days } }).then(r => r.data),
  scoringAuditTool: (params) => http.get('/scoring/audit/tool', { params }).then(r => r.data),

  // ---------------- 负分人工复核 ----------------
  reviewStats: () => http.get('/review/stats').then(r => r.data),
  reviewList: (params) => http.get('/review/list', { params }).then(r => r.data),
  reviewDetail: (review_id, msg_limit = 60) =>
    http.get('/review/detail', { params: { review_id, msg_limit } }).then(r => r.data),
  reviewOptions: () => http.get('/review/options').then(r => r.data),
  reviewSubmit: (payload) => http.post('/review/submit', payload).then(r => r.data),
  reviewSetStatus: (payload) => http.post('/review/status', payload).then(r => r.data),
  // 批量改状态（例如一次性把同一批明显误判的负分标成「已忽略」）
  reviewBatchStatus: (payload) => http.post('/review/batch-status', payload).then(r => r.data),
  reviewAudit: (limit = 30) => http.get('/review/audit', { params: { limit } }).then(r => r.data),

  // ---------------- 评估集（回答"判得准不准"）----------------
  evalStats: () => http.get('/eval/stats').then(r => r.data),
  evalList: (params) => http.get('/eval/list', { params }).then(r => r.data),
  evalDetail: (case_id, msg_limit = 100) =>
    http.get('/eval/detail', { params: { case_id, msg_limit } }).then(r => r.data),
  evalSave: (payload) => http.post('/eval/save', payload).then(r => r.data),
  // 「我只判对错」：判错时**只打争议标记**（不改标注内容，仍排除在指标外）
  evalDispute: (payload) => http.post('/eval/dispute', payload).then(r => r.data),
  evalFreeze: (payload) => http.post('/eval/freeze', payload).then(r => r.data),
  evalActive: (payload) => http.post('/eval/active', payload).then(r => r.data),
  evalSample: (payload) => http.post('/eval/sample', payload).then(r => r.data),
  evalStart: (payload = {}) => http.post('/eval/start', payload).then(r => r.data),
  evalStatus: () => http.get('/eval/status').then(r => r.data),
  evalRuns: (limit = 20) => http.get('/eval/runs', { params: { limit } }).then(r => r.data),
  evalRunDetail: (run_id) => http.get('/eval/run-detail', { params: { run_id } }).then(r => r.data),
  evalBaseline: () => http.get('/eval/baseline').then(r => r.data),
  evalSetBaseline: (payload = {}) => http.post('/eval/baseline', payload).then(r => r.data),
  // 规则选项（判对/判错时的 rule_id 与档位来源）—— 原来这里是裸 fetch，且走的是
  // 后端同一个端点：绕过 api.js 会让"统一加超时/统一加 401 处理"漏掉这一处。
  evalOptions: () => http.get('/eval/options').then(r => r.data),
  // 覆盖度（哪些规则一条样本都没有）/ 告警聚合（门禁回退、复核积压、凭据新鲜度）
  evalCoverage: () => http.get('/eval/coverage').then(r => r.data),
  evalAlerts: () => http.get('/eval/alerts').then(r => r.data),
  evalDelete: (payload) => http.post('/eval/delete', payload).then(r => r.data),
  evalGate: () => http.get('/eval/gate').then(r => r.data),
  evalExportUrl: (kind = '') => `/api/eval/export?kind=${encodeURIComponent(kind)}`,
}

// ---------------- 展示用工具 ----------------

// 聊天记录行的格式契约来自后端 `ctx_preprocess.render_lines()`：
//     `#001 [10-08 09:12｜6天前] 客户: 内容`
// ⚠️ 不能用 `line.indexOf(':')` 切分 —— 时间里的 `09:12` 会先被命中，
//    于是 who 变成 "#001 [10-08 09"、正文变成 "12｜6天前] 客户: 内容"（实测踩到）。
// ⚠️ 也不能用 `startsWith('销售')` 判左右 —— 行以 `#001` 开头，永远判不中 → 全部靠左。
const CONV_LINE = /^#(\d{3,})\s*\[([^\]]*)\]\s*([^:：]{1,12})[:：]\s*([\s\S]*)$/

// 企微「引用回复」的正文形态：被引用的原话在上、一行短横线、然后是自己写的内容。
// 例：
//     "你们总共有几个系列的课程呀"
//     ------
//     这边负责的是两个课程，……
// 不拆开的话，读者会看到「销售气泡里写着客户的话」，误以为归属搞错了（实测被反馈过）。
const QUOTE_SEP = /^-{3,}\s*$/

/** 把一条消息正文拆成「引用块 + 正文」。没有引用分隔线时原样返回。 */
export function splitQuote(text) {
  const lines = (text || '').split('\n')
  const i = lines.findIndex((l) => QUOTE_SEP.test(l.trim()))
  if (i <= 0) return { quote: '', body: text }
  const q = lines.slice(0, i).join('\n').trim()
  const b = lines.slice(i + 1).join('\n').trim()
  // 分隔线两侧任一侧为空 → 不是引用形态，别硬拆
  if (!q || !b) return { quote: '', body: text }
  return { quote: q, body: b }
}

/**
 * 把后端返回的**结构化消息**（`/api/eval/detail` · `/api/review/detail` 的 `messages`）
 * 转成气泡渲染用的形状。优先用它，而不是解析文本 —— 文本形态把媒体压成了 `[图片]`
 * 占位、且不带 URL，图/表情根本渲染不出来。
 */
export function toBubbles(messages) {
  return (messages || []).map((m) => {
    const { quote, body } = splitQuote(m.text || '')
    return {
      ...m,
      quote,
      text: body,
      side: (m.role || '') === '销售' ? 'right' : 'left',
    }
  })
}

// ---------------- 媒体消息的展示分类 ----------------

/**
 * 媒体类型 → 展示分类。**前端唯一一份**（`AGENTS.md` 的 Reuse Before Rewrite 规则）。
 *
 * 为什么前端还得有这张表：msgtype 的**数字**是企微协议的一部分
 * （2 图片 / 3 表情 / 6 语音 / 7 视频 / 8 文件），「怎么画」只能按数字分支；
 * 但**中文名一律用后端下发的 `type_name`**（唯一事实源 `ww_chatdata.TYPE_NAME`）。
 * 前端只管画法，不管命名 —— 所以这里没有、也不该有中文名。
 *
 * 改造前 ChatView 与 ChatBubbles 各写一份判断（`2/3 出图、6/7/8 出卡片` vs
 * `2||3 出图、其余出卡片`），同一个知识要改两处。
 */
export const MEDIA_KIND = { 2: 'image', 3: 'emoji', 6: 'voice', 7: 'video', 8: 'file' }

/** 展示分类：`'image' | 'emoji' | 'voice' | 'video' | 'file' | ''（非媒体）`。 */
export function mediaKindOf(m) {
  if (!m || !m.is_media) return ''
  return MEDIA_KIND[Number(m.msgtype)] || 'other'
}

/** 图片、表情**直接出图**；其余（语音 / 视频 / 文件）给可点卡片。 */
export function isInlineImage(m) {
  const k = mediaKindOf(m)
  return k === 'image' || k === 'emoji'
}

/** 媒体卡片的「图标 + 名称 + 动作词」——两处渲染共用，避免同一类型两种说法。 */
export function mediaCard(m) {
  const k = mediaKindOf(m)
  const ICON = { voice: '🎤', video: '🎬', file: '📎' }
  const ACTION = { voice: '点击播放', video: '点击打开', file: '点击下载' }
  return {
    icon: ICON[k] || '📎',
    name: (m && m.media_name) || (m && m.type_name) || '附件',
    action: ACTION[k] || '点击打开',
  }
}

/**
 * 不画图 / 不画卡片时的媒体正文文案。
 * ``url`` 为真 = 还有链接可点；为假 = 链接没取到（企微 CDN 有时效）。
 */
export function mediaText(m, url) {
  const name = (m && m.type_name) || '附件'
  return url ? `${name}（媒体链接有时效，点击尝试打开）`
             : `${name}（链接已过期或未取到）`
}

// ---------------- 等级标签色 ----------------

//: 等级 → `tag` 颜色类。**前端唯一一份**：改造前 ScoreView / ReviewView 各有一份
//: 逐字相同的 `GRADE_TAG`，EvalView 又抄了一份叫 `GTAG` —— 三处同源，
//: 改一次配色要动三个文件（实测就这样飘过一次）。
const TAG_OF_GRADE = { S: 'red', A: 'orange', B: 'blue', C: 'green', D: '' }

/** 等级 → 标签色类（未知等级返回空串，不抛）。 */
export function gradeTag(grade) {
  return TAG_OF_GRADE[grade] || ''
}

// ---------------- 回写模式中文名 ----------------

//: `score_writeback_mode` / `grade_tag_mode` 的取值 → 中文名。
//: 两个开关是**独立的两套流水**（分数回写 / 等级标签），但取值域与说法完全一样，
//: 原先在 ScoreView 里各写了一份同内容的映射。
export const MODE_CN = { live: '真实写入', dry_run: '演练模式（不写入）', off: '已关闭' }

/**
 * 把后端返回的聊天记录文本解析成可渲染的气泡。
 * 解析不出来时**原样保留整行**（宁可显示得朴素，也不要把内容切碎丢掉）。
 */
export function parseConversation(txt) {
  const out = []
  for (const raw of (txt || '').split('\n')) {
    const line = raw.replace(/\s+$/, '')
    if (!line.trim()) continue
    const m = line.match(CONV_LINE)
    if (m) {
      const who = m[3].trim()
      out.push({
        code: '#' + m[1],
        time: m[2],
        who,
        text: m[4].trim(),
        // 员工（销售）靠右、客户靠左 —— 与聊天软件一致
        side: who === '销售' ? 'right' : 'left',
      })
    } else if (out.length) {
      // ★ 续行：它属于**上一条消息**（正文里自带换行，如一段多行的直播宣传文案）。
      //   不能当成新气泡 —— 那会造出一串"没有角色"的气泡、全部走兜底靠左。
      //   实测：case 26 共 35 行，其中 30 行是续行，于是同一条销售消息的后续
      //   30 行全跑到左边，看着像"销售在自问自答"。
      out[out.length - 1].text += '\n' + line.trim()
    } else {
      // 首行就解析不出来：原样保留（宁可显示得朴素，也不要静默丢内容）
      out.push({ code: '', time: '', who: '', text: line, side: 'left' })
    }
  }
  // 引用块要**在整条正文拼好之后**再拆 —— 因为头行只带正文的第一行，
  // 分隔线与后面的内容是以"续行"形式进来的。
  return out.map((b) => {
    const { quote, body } = splitQuote(b.text)
    return { ...b, quote, text: body }
  })
}

export function fmtTime(ts, withSeconds = false) {
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  const p = (n) => String(n).padStart(2, '0')
  const base = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
  return withSeconds ? `${base}:${p(d.getSeconds())}` : base
}

/**
 * 后端返回的 `datetime` 是**字符串**（`"2026-10-16 02:17:03.653598+00:00"`），
 * 而 `fmtTime` / `fmtRelative` 吃的是**秒级数字**时间戳 —— 直接把字符串喂进去，
 * `ts * 1000` 得到 `NaN`，页面会显示 `NaN-NaN-NaN NaN:NaN`（报错都不报，最阴的一类）。
 *
 * 所以：**数字原样返回，字符串先 `Date.parse` 再除以 1000，解析不了返回 null**。
 * 判断收敛在这里一处 —— 各页面不必再各写一遍 `fmtTime(Date.parse(x) / 1000)`。
 */
export function toEpoch(v) {
  if (v === null || v === undefined || v === '') return null
  if (typeof v === 'number') return v
  const t = Date.parse(v)
  return Number.isNaN(t) ? null : t / 1000
}

export function fmtClock(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}`
}

export function fmtRelative(ts) {
  if (!ts) return '-'
  const diff = Date.now() / 1000 - ts
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`
  return fmtTime(ts).slice(0, 10)
}

export function dayLabel(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const now = new Date()
  const same = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
  if (same(d, now)) return '今天'
  const y = new Date(now.getTime() - 86400000)
  if (same(d, y)) return '昨天'
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}
