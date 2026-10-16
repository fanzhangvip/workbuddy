<script setup>
import { ref, computed, onMounted } from 'vue'
// `toEpoch`：后端的 datetime 是字符串，`fmtTime`/`fmtRelative` 只吃秒级数字 ——
// 不转就会渲染成 `NaN-NaN-NaN`（见 api.js 里的说明）。
import { api, fmtTime, fmtRelative, gradeTag, MODE_CN, toEpoch } from '../api'
import ChatBubbles from '../components/ChatBubbles.vue'
import Pager from '../components/Pager.vue'
import { useAsyncAction } from '../composables/useAsyncAction'
import { usePolling } from '../composables/usePolling'
import { useLoadFailures } from '../composables/loadFailures'
import PartialLoadBanner from '../components/PartialLoadBanner.vue'

// 局部加载失败统一收口（门禁 / 保留期 / 标签 / 早会名单 / 员工列表都曾是静默 catch，
// 接口一失败页面就只剩空白 —— 看起来像"没有数据"，见 composables/loadFailures.js）
const { failures, note: noteFail, clear: clearFail } = useLoadFailures()

const form = ref({ staff_id: '', max_sessions: 50, min_messages: 4, force: false })
const staffs = ref([])
const status = ref(null)
const metrics = ref(null)
const runs = ref([])
const list = ref({ items: [], total: 0 })
const schedule = ref({ scoring: { enabled: false, interval_minutes: 0, next_run_time: null } })
const settings = ref({ writeback_mode: 'dry_run' })
const scInterval = ref(null)
const filters = ref({ grade: '', q: '', review_status: '', page: 1, page_size: 15 })
const detail = ref(null)
// busy / error / msg 由 composable 提供（模板里的 `busy` / `error` / `msg` 用法不变）。
// ⚠️ 这里把 `run` 重命名成 `act`：本文件已经有一个 `run`（当前任务的 computed），
//    同名会直接构建失败 —— 变量命名冲突在复用时要显式躲开，别硬碰。
// ⚠️ `msg` 原先**只被用、没被声明**（两个标签按钮里 `msg.value = ...` 会抛
//    ReferenceError）；现在由 composable 提供，顺手修掉。
const { busy, error, msg, run: act } = useAsyncAction()
// 架构新增项（第 2–4 批）：运行时参数 + F2 早会/公开课复核名单
const arch = ref({ llm_timeout_sec: 0, data_retention_days: 0, eval_gate_enabled: true,
                   morning_cron: '', public_class_grade_scope: 'S,A',
                   writeback_delta_mode: true, ctx_min_customer_msgs: 1,
                   ctx_min_effective_chars: 0 })
const saList = ref({ count: 0, items: [], scope: ['S', 'A'] })
const gate = ref(null)
const retention = ref(null)
async function refresh() {
  try {
    const [s, m, r, l, sc, st] = await Promise.all([
      api.scoringStatus(), api.scoringMetrics(), api.scoringRuns(15),
      api.scoringAssessments({ ...filters.value }), api.scoringSchedule(), api.scoringSettings(),
    ])
    status.value = s; metrics.value = m; runs.value = r.items || []
    list.value = { items: l.items || [], total: l.total || 0 }
    schedule.value = sc; settings.value = st
    for (const k of Object.keys(arch.value)) if (k in st) arch.value[k] = st[k]
    if (scInterval.value === null && sc?.scoring) scInterval.value = sc.scoring.interval_minutes ?? 0
  } catch (e) { error.value = '后端未就绪：' + e.message }
}

async function loadStaffs() {
  try {
    staffs.value = (await api.staffs({ only_with_session: 1, limit: 800 })).items || []
    clearFail('staffs')
  } catch (e) { noteFail('staffs', '员工列表', e) }
}

const run = computed(() => status.value?.run || {})
const running = computed(() => !!status.value?.running)
const progress = computed(() => {
  const t = run.value.session_total || 0, d = run.value.session_done || 0
  return t ? Math.min(100, Math.round((d / t) * 100)) : 0
})
const grades = computed(() => {
  const m = metrics.value || {}
  return [['S', m.g_s], ['A', m.g_a], ['B', m.g_b], ['C', m.g_c], ['D', m.g_d]]
})
const gradeMax = computed(() => Math.max(1, ...grades.value.map(g => g[1] || 0)))

// D2-1 等级标签：与上面的分数回写**完全独立**的一套开关与流水
const gt = ref({ mode: 'off', tag_group: '意向等级', tag_ids: {}, outbox: [], ledger_rows: 0, errors: [] })
const gtGroup = ref('意向等级')

async function start() {
  // 忽略水位 = 真全量：`_candidates` 去掉所有增量条件，达标会话全部重评。
  // （当前规则版本变化时，**即使不勾**也会把已评会话全部纳入候选 —— 增量条件里
  //   本来就有「规则版本不同」这一条。）
  if (form.value.force && !window.confirm(
    '「忽略水位强制重评」会跳过所有增量条件：\n\n'
    + '· 所有达标会话（消息数 ≥ ' + (form.value.min_messages || 4) + '）全部重评；\n'
    + '· 每条一次真实模型调用，整轮耗时可能数小时、且会消耗模型额度；\n'
    + '· 期间不能同步（两者互斥）。\n\n确定继续吗？')) return
  await act(async () => {
    const r = await api.scoringStart({
      staff_ids: form.value.staff_id ? [form.value.staff_id] : null,
      max_sessions: Number(form.value.max_sessions) || 0,
      min_messages: Number(form.value.min_messages) || 0,
      force: form.value.force,
    })
    if (!r.ok) { error.value = r.reason || '启动失败'; return }
    msg.value = `已启动评分（${form.value.force ? '真全量' : '增量'}）`
  })
  refresh()
}

async function stop() {
  await act(async () => {
    const r = await api.scoringStop()
    // ⚠️ 必须看返回值：没任务在跑时后端回 `{ok:false, reason:"当前没有正在运行的评分任务"}`，
    //    改造前这里不看 —— 点了停止既没停也没提示，像是按钮坏了。
    if (!r.ok) { error.value = r.reason || '停止失败'; return }
    msg.value = r.message || '已发出停止信号'
  })
  refresh()
}

async function saveSchedule() {
  await act(async () => {
    schedule.value = await api.scoringSetSchedule(Number(scInterval.value) || 0)
  })
}

// ---- 架构新增参数（第 2–4 批）----
async function saveArch() {
  await act(async () => {
    const r = await api.scoringSetSettings({ ...arch.value })
    if (!r.ok) { error.value = r.reason || '保存失败'; return }
    refresh()
  })
}

// F2：早会/公开课复核 —— 点一下「增量刷新」S/A 名单，并展示刷新后的名单
async function publicClassReview(mode) {
  await act(async () => {
    const r = await api.scoringPublicClassReview({ mode })
    if (!r.ok) { error.value = r.reason || '启动失败'; return }
    if (r.name_list) saList.value = r.name_list
    msg.value = mode === 'after' ? '已触发课后复核刷新（增量）' : '已触发课前复核刷新（增量）'
    refresh()
  })
}

async function loadSaReview() {
  try { saList.value = await api.scoringSaReview(); clearFail('sa') }
  catch (e) { noteFail('sa', '早会复核名单', e) }
}

async function loadGate() {
  try { gate.value = await api.scoringRulesGate(); clearFail('gate') }
  catch (e) { noteFail('gate', '闸6 门禁状态', e) }
}

async function loadRetention() {
  try { retention.value = await api.scoringRetention(); clearFail('retention') }
  catch (e) { noteFail('retention', '数据保留期', e) }
}

// ---- D2-1 等级 → 微伴标签（与分数回写分开的开关）----
async function loadGradeTags() {
  try {
    const r = await api.scoringGradeTags()
    gt.value = r
    if (r.tag_group) gtGroup.value = r.tag_group
    clearFail('tags')
  } catch (e) { noteFail('tags', '等级标签状态', e) }
}

// ============================================================ 回写审批
// 为什么有这张卡：策略（`policy`）判定为 confirm / human 的回写**不该静默丢弃** ——
// 改造前它只写一行 reason="policy_confirm" 就完了，而 outbox 里那条待办会一直挂着等
// 人工确认……**却没有任何地方能确认它**（`approval_request` 那时候只有测试在写）。
// 结果就是"需人工二次确认后才写入"这句承诺在系统里兑现不了。现在：
//   策略拦下 → 建审批请求（不阻塞批次）→ 这张卡里批准/驳回 → 批准则放行投递。
const approvals = ref([])
const approvalNote = ref({})

async function loadApprovals() {
  try {
    approvals.value = (await api.scoringApprovals('pending', 50)).items || []
    clearFail('approvals')
  } catch (e) { noteFail('approvals', '回写审批队列', e) }
}

function approvalClient(a) {
  const p = a.params_redacted || {}
  return p.external_user_id || p.relationship_key || '-'
}

async function decideApproval(a, approve) {
  const p = a.params_redacted || {}
  if (approve && !window.confirm(
    '批准这条回写 → 放行投递：\n\n'
    + `· 客户 ${approvalClient(a)}｜将累加 ${p.score ?? '-'} 分\n`
    + '· 微伴「客户评分」只有累加语义，写入**不可撤销**\n'
    + '· 仍需 `writeback_mode=live` 才会真写；dry_run 下只记账\n\n确定批准吗？')) return
  if (!approve && !window.confirm(
    '驳回这条回写？\n\n· 该待办会置为终态（dead），不再投递\n'
    + '· 微伴那边的客户分数**不会有任何变化**\n\n确定驳回吗？')) return
  await act(async () => {
    const r = await api.scoringApprovalDecide(a.request_id, {
      approved: approve, note: approvalNote.value[a.request_id] || '',
    })
    if (!r.ok) { error.value = r.reason || '审批失败'; return }
    msg.value = r.note_text || (approve ? '已批准' : '已驳回')
    approvalNote.value[a.request_id] = ''
    await Promise.all([loadApprovals(), refresh()])
  })
}

// ============================================================ 批次任务（外循环）
// `task` / `task_step` 是「长任务检查点」，库里有数据，但此前**只有 curl 能看**：
// 想回答"这一批走到哪一步、能不能续跑"必须手打接口（`main.py` 启动时提示的
// `/api/scoring/tasks/{id}/resume` 就是这么用的）。这里给它一个界面。
const tasks = ref([])
const taskStats = ref(null)
const taskOpen = ref(null)          // { task, steps, approvals }

async function loadTasks() {
  try {
    const [t, s] = await Promise.all([api.scoringTasks({ limit: 20 }), api.scoringTaskStats(30)])
    tasks.value = t.items || []
    taskStats.value = s
    clearFail('tasks')
  } catch (e) { noteFail('tasks', '批次任务', e) }
}

async function openTask(t) {
  await act(async () => {
    const r = await api.scoringTaskDetail(t.task_id)
    if (r.ok === false) { error.value = r.reason || '打开任务失败'; return }
    taskOpen.value = r
  })
}

async function cancelTask(t) {
  if (!window.confirm(`取消任务 #${t.task_id}？\n\n`
    + '· 取消是**终态**（不可迁出），正在跑的这一批会停下；\n'
    + '· 已评完的会话保留，未跑的不再继续。\n\n确定取消吗？')) return
  await act(async () => {
    const r = await api.scoringTaskCancel(t.task_id, { reason: '界面人工取消' })
    if (!r.ok) { error.value = r.reason || '取消失败'; return }
    msg.value = `任务 #${t.task_id} 已取消`
    await loadTasks()
  })
}

async function resumeTask(t) {
  if (!window.confirm(`从检查点续跑任务 #${t.task_id}？\n\n`
    + '· 会**新建**一个任务（历史不变），已成功的步骤会跳过；\n'
    + '· 续跑不是重跑，"从头再来"请用「忽略水位强制重评」。\n\n确定续跑吗？')) return
  await act(async () => {
    const r = await api.scoringTaskResume(t.task_id)
    if (!r.ok) { error.value = r.reason || '续跑失败'; return }
    msg.value = `已从 #${t.task_id} 新建续跑任务（跳过 ${(r.done_steps || []).length} 个已完成步骤）`
    await loadTasks()
  })
}

// ============================================================ 审计留痕
// `policy_decision`（1,396 行策略判定）与 `tool_audit`（工具调用五问）一直**只写不看**：
// 策略分布、谁在什么情况下写了微伴，此前只有 curl 能查。
const policyStats = ref(null)
const auditStats = ref(null)
const toolCalls = ref([])

async function loadAudit() {
  try {
    const [p, a, t] = await Promise.all([
      api.scoringPolicyStats(30), api.scoringAuditStats(30),
      api.scoringAuditTool({ days: 7, limit: 20 }),
    ])
    policyStats.value = p; auditStats.value = a; toolCalls.value = t.items || []
    clearFail('audit')
  } catch (e) { noteFail('audit', '审计留痕', e) }
}

// ============================================================ 规则发布 / 保留期清理 / 标签队列
const ruleInfo = ref({})

async function loadRulesInfo() {
  try { ruleInfo.value = await api.scoringRules(); clearFail('rulesinfo') }
  catch (e) { noteFail('rulesinfo', '规则版本信息', e) }
}

/** 发布 rules.json（走闸6 门禁）。这是"改完规则让日常评分认它"的**唯一**正确入口。 */
async function publishRules() {
  if (!window.confirm(
    '发布当前 rules.json 到评分链路？\n\n'
    + '· 会走闸6 门禁：有基线且指标下降 → 发布被拒绝（可再点「强制发布」）；\n'
    + '· 规则版本变化会让**已评估会话重新进入候选**（下一轮评分收敛）；\n'
    + '· 不发布的话，日常评分会被门禁挡住且**只打一行日志**。\n\n确定发布吗？')) return
  await act(async () => {
    const r = await api.scoringRulesPublish({})
    if (!r.ok) { error.value = r.reason || '发布失败'; return }
    const dec = r.gate || {}
    msg.value = `已发布` + (r.published?.rule_version ? ` v${r.published.rule_version}` : '')
      + '｜门禁：' + (dec.decidable ? (dec.blocked ? '阻断（已放行本次）' : '通过') : '无法判定（放行但不代表通过）')
      + (r.warning ? `｜${r.warning}` : '')
    await Promise.all([loadRulesInfo(), loadGate(), refresh()])
  })
}

/** 强制发布（`force=true`）：门禁拦住但要立刻生效时用 —— 会写审计。 */
async function publishRulesForce() {
  if (!window.confirm(
    '强制发布（忽略闸6 阻断）？\n\n'
    + '· 门禁说"指标下降"时，强制发布等于**明知有回退也让新规则上线**；\n'
    + '· 这次操作会写进审计（可事后追责），并把原因记在门禁结果里。\n\n确定强制发布吗？')) return
  await act(async () => {
    const r = await api.scoringRulesPublish({ force: true })
    msg.value = r.ok ? '已强制发布（已写审计）' : (r.reason || '发布失败')
    await Promise.all([loadRulesInfo(), loadGate(), refresh()])
  })
}

/** 按保留期清理运行观测表（**不可恢复**，故必须二次确认并先说清删什么）。 */
async function purgeRetention() {
  const days = retention.value?.days || 0
  if (!days) {
    error.value = '保留期未开启（0 天 = 不清理）—— 先在上方把「数据保留」设为天数并保存'
    return
  }
  if (!window.confirm(
    `按 ${days} 天保留期清理运行观测表？\n\n`
    + '· 只删观测副产品：audit_log / trace_span / session_failure / request_guard / scoring_run 等；\n'
    + '· 业务结论表（评估结论 / 复核 / 回写账本 / 评估集）**永不按天数清理**；\n'
    + '· 删除不可恢复 —— 上方的行数是预演结果，可先看清楚再执行。\n\n确定执行吗？')) return
  await act(async () => {
    const r = await api.scoringRetentionPurge({ days })
    if (!r.ok) { error.value = r.reason || '清理失败'; return }
    const n = Object.values(r.deleted || {}).reduce((s, v) => s + (v || 0), 0)
    msg.value = `已清理 ${n} 行观测数据（${Object.keys(r.deleted || {}).length} 张表）`
    await Promise.all([loadRetention(), loadAudit()])
  })
}

/** 立即排空等级标签队列（不等调度器那一拍）。 */
async function drainTags() {
  if (!window.confirm(
    '立即投递等级标签队列？\n\n'
    + '· 只投递**已入队**的标签动作（不会凭空给客户打标签）；\n'
    + '· `grade_tag_mode=off` 时不调用微伴，dry_run 下只记账不写。\n\n确定投递吗？')) return
  await act(async () => {
    const r = await api.scoringGradeTagDrain({})
    msg.value = r.ok === false ? (r.reason || '投递失败')
      : `已投递：成功 ${r.ok_count ?? r.ok ?? 0}、失败 ${r.fail ?? 0}、跳过 ${r.skipped ?? 0}`
    await loadGradeTags()
  })
}

async function setTagMode(mode) {
  // 「真实写入」= 真在微伴后台给客户打/改标签（外部副作用，标签有增有删），必须先确认。
  if (mode === 'live' && !window.confirm(
    '切到「真实写入」后，系统会在微伴后台给客户打「' + gtGroup.value + '」等级标签'
    + '（真实调用微伴标签接口，可被再次修改）。\n\n确定切换吗？')) return
  await act(async () => {
    const r = await api.scoringSetSettings({ grade_tag_mode: mode })
    if (!r.ok) { error.value = r.reason || '保存失败'; return }
    msg.value = `标签模式已切到${MODE_CN[mode] || mode}`
    await loadGradeTags()
  })
}

async function syncGradeTags(createMissing) {
  // 「创建缺失标签」会在微伴后台**真实创建**标签（外部副作用），先确认。
  if (createMissing && !window.confirm(
    '会在微伴后台**真实创建**缺失的等级标签（组：' + gtGroup.value + '）。\n\n确定继续吗？')) return
  await act(async () => {
    const r = await api.scoringGradeTagSync({ create_missing: !!createMissing, dry_run: !createMissing })
    if (!r.ok) { error.value = r.reason || '同步失败'; return }
    const found = Object.keys(r.found || {})
    msg.value = `标签同步：已找到 ${found.length} 个（${found.join('/') || '无'}）`
      + (r.missing?.length ? `；缺失 ${r.missing.join('/')}` : '')
      + (r.created?.length ? `；已创建 ${r.created.join('/')}` : '')
    await loadGradeTags()
  })
}

async function alignGradeTags(apply) {
  // `apply=false` → 预演（只报告会改谁）；`apply=true` → **真写微伴标签**（外部副作用）。
  // 改造前这里把 `dry_run: true` 写死 —— 于是"对齐"永远只预演、系统里没有任何执行入口，
  // 想真对齐只能手工 curl（`grade_tag.align` 本来就支持 dry_run=false）。
  if (apply && !window.confirm(
    '将按当前等级**真实修改微伴后台的等级标签**（最多 200 位客户）。\n\n'
    + '· 等级未变的客户不改；\n'
    + '· 不会删除其它标签（remove_all_others=false）；\n'
    + '· 标签模式必须是 live，否则会被后端拒绝。\n\n确定执行吗？')) return
  await act(async () => {
    const r = await api.scoringGradeTagAlign({ limit: 200, dry_run: !apply,
                                               remove_all_others: false })
    msg.value = r.ok
      ? (apply ? `对齐执行：扫描 ${r.scanned} 位，已改 ${r.changed ?? r.to_change} 位`
               : `对齐预演：扫描 ${r.scanned} 位，待改 ${r.to_change} 位（未写微伴）`)
      : (r.reason || '失败')
    if (r.ok && apply) await loadGradeTags()
  })
}

async function saveTagGroup() {
  await act(async () => {
    const r = await api.scoringSetSettings({ grade_tag_group: gtGroup.value })
    if (!r.ok) { error.value = r.reason || '保存失败'; return }
    await loadGradeTags()
  })
}

async function setMode(mode) {
  // ⚠️ 写外部系统 + **不可撤销**：微伴评分只有累加、没有覆盖。
  // 当前 `writeback_ledger`（客户级回写水位）为空 → 被重评的客户会按其**正分全额**累加；
  // 若这些客户在微伴里本来已有人工打分，就是重复累加（业务已确认接受，但必须显式确认）。
  if (mode === 'live' && !window.confirm(
    '切到「真实写入」后，系统会把评分**差值累加**写进微伴，且**不可撤销**：\n\n'
    + '· 微伴「添加客户评分」只有累加语义，没有覆盖接口；\n'
    + '· 当前客户级回写水位为空 → 被你重评的客户会按其**正分全额**累加一次；\n'
    + '· 若这些客户在微伴里已有人工打分，会出现重复累加。\n\n确定切换吗？')) return
  await act(async () => {
    const r = await api.scoringSetSettings({ writeback_mode: mode })
    if (!r.ok) { error.value = r.reason || '切换失败'; return }
    settings.value = r
    msg.value = `回写模式已切到${MODE_CN[mode] || mode}`
    refresh()
  })
}

function search() { filters.value.page = 1; refresh() }
function goto(p) { filters.value.page = p; refresh() }

async function openDetail(id) { try { detail.value = await api.scoringAssessment(id) } catch (e) { error.value = e.message } }

const STATUS_TAG = { success: 'green', running: 'blue', failed: 'red', stopped: 'orange', partial: 'purple' }
const STATUS_CN = { success: '成功', running: '运行中', failed: '失败', stopped: '已停止', partial: '部分完成' }
// 部分完成的原因（架构 G3/G4）：让「为什么没跑完」一眼可见，而不是看到状态就慌
const PARTIAL_CN = { deadline: '达到整轮超时上限', max_llm_calls: '达到大模型调用次数上限' }
// 人工复核的触发原因（架构 第六章 E4：由规则判定，不再听模型自报）
const REASON_CN = {
  has_negative: '有语义负分项',
  engine_negative_only: '仅引擎硬指标（如客户超 48h 未回复）',
  unchecked_rules: '有规则被漏检',
  low_confidence: '系统置信度偏低',
  grade_boundary: '分数正好卡在等级分界（差 1 分即换级）',
  weak_evidence: '高权重项但证据不硬',
  grade_flip: '与上次等级明显翻转',
}

onMounted(() => {
  loadStaffs(); loadSaReview(); loadGate(); loadRetention(); loadGradeTags()
  // 新接线的四条：回写审批 / 批次任务 / 审计留痕 / 规则版本信息
  loadApprovals(); loadTasks(); loadAudit(); loadRulesInfo()
})
// 有任务在跑 → 3 秒（要看进度）；空闲 → 15 秒（数据几乎不变，省掉大量无谓查询）。
// 页面不可见时 `usePolling` 直接停（切回来立刻补一次）—— 这两条合起来把"挂着不动也
// 每 3 秒打 6 个接口"的负载降掉了（`/metrics` 一次约 15 个子查询）。
usePolling(refresh, 3000, { idle: 15000, idleWhen: () => !running.value })
</script>

<template>
  <div class="page">
    <!-- 局部加载失败 → 明说 + 可重试（见 components/PartialLoadBanner.vue） -->
    <PartialLoadBanner :failures="failures" @retry="refresh" />
    <div class="layout">
      <!-- 左：控制 -->
      <div style="display:grid;gap:16px">
        <div class="card">
          <div class="card-head">
            启动评分
            <span class="tag" :class="running ? 'blue' : ''" style="margin-left:auto">
              {{ running ? '评分中' : '空闲' }}
            </span>
          </div>
          <div class="card-body" style="display:grid;gap:14px">
            <div class="alert info" style="font-size:12px">
              规则引擎 + 大模型：从聊天记录识别加减分信号并给出证据，总分由本地规则计算。
              正分自动回写微伴，负分转人工复核。
            </div>
            <div>
              <label class="lbl">员工范围</label>
              <select v-model="form.staff_id" class="select">
                <option value="">全部员工</option>
                <option v-for="s in staffs" :key="s.staff_id" :value="s.staff_id">{{ s.name }}</option>
              </select>
            </div>
            <div class="grid-2 loose">
              <div>
                <label class="lbl">本次最多会话</label>
                <input v-model="form.max_sessions" class="input" type="number" min="0" />
              </div>
              <div>
                <label class="lbl">最少消息数</label>
                <input v-model="form.min_messages" class="input" type="number" min="0" />
              </div>
            </div>
            <label class="ck"><input type="checkbox" v-model="form.force" /> 忽略水位强制重评</label>
            <!-- P7：不加这句会让人以为"不勾就是只评新消息"。实际上增量条件里还有
                 「规则版本变化」这一条 —— 规则一改，所有已评会话本来就会重新入候选。 -->
            <div class="muted" style="font-size:11.5px;line-height:1.6;margin-top:-6px">
              不勾 = 增量（新消息 / 未评过 / **规则版本变化**都算候选）；勾上 = 无视以上条件全量重评（会二次确认）。
            </div>
            <div style="display:flex;gap:10px">
              <button class="btn primary" style="flex:1" :disabled="running || busy" @click="start">
                <i v-if="busy" class="spin"></i>{{ running ? '评分中…' : '开始评分' }}
              </button>
              <button class="btn danger" :disabled="!running || busy" @click="stop">停止</button>
            </div>
            <div v-if="error" class="alert err">{{ error }}</div>
            <!-- 成功/中性提示的**统一出口**：改造前只有标签卡片里显示 msg，
                 「停止/切模式/公开课复核」这些动作的结果没有任何地方展示。 -->
            <div v-if="msg" class="alert ok">{{ msg }}</div>
          </div>
        </div>

        <div class="card">
          <div class="card-head">微伴评分回写</div>
          <div class="card-body" style="display:grid;gap:12px">
            <div class="seg">
              <button :class="{ on: settings.writeback_mode === 'dry_run' }" @click="setMode('dry_run')">
                演练模式<small>只记录，不写入微伴</small>
              </button>
              <button :class="{ on: settings.writeback_mode === 'live' }" @click="setMode('live')">
                真实写入<small>调用微伴评分接口</small>
              </button>
            </div>
            <div class="alert" :class="settings.writeback_mode === 'live' ? 'warn' : 'info'">
              当前：{{ MODE_CN[settings.writeback_mode] }}
              <template v-if="settings.writeback_mode === 'live'">
                —— 会真实修改微伴客户评分。微伴为<b>累加</b>语义，系统已按
                <b>差值累加</b>回写（D2-1），不会重复累加；水位见 writeback_ledger。
              </template>
            </div>
            <div class="muted" style="font-size:11.5px;line-height:1.7">
              微伴「添加客户评分」要求 score &gt; 0，语义**已确认 = 累加**（无覆盖接口），
              故系统按<b>差值累加</b>回写。等级另走上面的<b>意向等级标签</b>（独立开关）。
            </div>
            <div style="display:flex;gap:10px;align-items:flex-end">
              <div style="flex:1">
                <label class="lbl">定时评分（分钟，0=关闭）</label>
                <input v-model="scInterval" class="input" type="number" min="0" />
              </div>
              <button class="btn" @click="saveSchedule">保存</button>
            </div>
            <div class="muted" style="font-size:12px">
              {{ schedule?.scoring?.enabled
                  ? `下次 ${schedule.scoring.next_run_time ? new Date(schedule.scoring.next_run_time).toLocaleString() : '-'}`
                  : '定时评分已关闭' }}
            </div>
          </div>
        </div>
        <div class="card">
          <div class="card-head">
            意向等级标签 · D2-1
            <span class="tag orange" style="margin-left:8px">与评分回写相互独立</span>
          </div>
          <div class="card-body" style="display:grid;gap:12px">
            <div class="seg">
              <button :class="{ on: gt.mode === 'off' }" @click="setTagMode('off')">
                已关闭<small>不写标签（连待办都不产生）</small>
              </button>
              <button :class="{ on: gt.mode === 'dry_run' }" @click="setTagMode('dry_run')">
                演练模式<small>只记录，不写微伴</small>
              </button>
              <button :class="{ on: gt.mode === 'live' }" @click="setTagMode('live')">
                真实写入<small>调用微伴标签接口</small>
              </button>
            </div>
            <div class="alert" :class="gt.mode === 'live' ? 'warn' : 'info'">
              当前：<b>{{ MODE_CN[gt.mode] }}</b>
              <template v-if="gt.mode === 'off'">
                —— 不会调用微伴标签接口（默认状态）。
              </template>
              <template v-else-if="gt.mode === 'live'">
                —— 会真实给客户打「{{ gt.tag_group }}」标签。
                等级改用标签是因为<b>评分只能累加、表达不了降级</b>，而标签有增有删。
              </template>
            </div>

            <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
              <div style="width:170px">
                <label class="lbl">标签组名</label>
                <input v-model="gtGroup" class="input" />
              </div>
              <button class="btn" @click="saveTagGroup" :disabled="busy">保存</button>
              <button class="btn ghost" @click="syncGradeTags(false)" :disabled="busy">同步标签</button>
              <button class="btn ghost" @click="syncGradeTags(true)" :disabled="busy"
                      title="缺失时会真的在微伴后台创建标签（副作用）">创建缺失标签</button>
              <button class="btn ghost" @click="alignGradeTags(false)" :disabled="busy">对齐预演</button>
              <!-- P4：原来只有预演（`dry_run` 写死），系统里没有任何真执行入口，
                   想真对齐只能手工 curl。补一个带确认的执行按钮（后端本来就支持）。 -->
              <button class="btn ghost" @click="alignGradeTags(true)" :disabled="busy"
                      title="按当前等级真实修改微伴后台的等级标签（最多 200 位，会二次确认）">
                执行对齐（写微伴）
              </button>
              <!-- 队列里的标签动作没必要干等定时 job（默认 30 秒一拍）；但要显式确认，
                   因为它走的是"写微伴"那条链（真正写不写仍由 grade_tag_mode 决定）。 -->
              <button class="btn ghost" @click="drainTags" :disabled="busy"
                      title="立即排空 grade_tag_outbox（不新建标签动作，只投递已入队的）">
                立即投递队列
              </button>
            </div>

            <div class="muted" style="font-size:11.5px;line-height:1.7">
              <b>已缓存标签</b>：
              <span v-if="Object.keys(gt.tag_ids || {}).length">
                <span v-for="(v, k) in gt.tag_ids" :key="k" class="tag" style="margin-right:4px">{{ k }}</span>
              </span>
              <span v-else>尚未同步 —— 需先「同步标签」拿到 tag_id，否则不会写（也不会自动建标签）</span>
              <br />
              已写客户 <b>{{ gt.ledger_rows || 0 }}</b> 位 ·
              <span v-if="(gt.outbox || []).length">
                流水 <span v-for="o in gt.outbox" :key="o.status" style="margin-right:6px">
                  {{ o.status }}={{ o.n }}</span>
              </span>
              <span v-else>暂无流水</span>
            </div>

            <div v-if="(gt.errors || []).length" class="alert err" style="font-size:12px">
              <div v-for="e in gt.errors.slice(0, 3)" :key="e.outbox_id">
                #{{ e.outbox_id }} {{ e.grade }} ×{{ e.attempts }}
                {{ e.last_errcode }} {{ e.last_errmsg }}
              </div>
            </div>

            <div v-if="msg" class="alert ok" style="font-size:12.5px">{{ msg }}</div>
          </div>
        </div>

        <div class="card">
          <div class="card-head">复核入口（早会 / 公开课）· F2</div>
          <div class="card-body" style="display:grid;gap:12px">
            <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
              <div style="width:150px">
                <label class="lbl">早会复核时刻（HH:MM，空=关闭）</label>
                <input v-model="arch.morning_cron" class="input" placeholder="08:30" />
              </div>
              <div style="width:120px">
                <label class="lbl">等级范围</label>
                <input v-model="arch.public_class_grade_scope" class="input" placeholder="S,A" />
              </div>
              <button class="btn" @click="saveArch" :disabled="busy">保存</button>
              <button class="btn ghost" @click="publicClassReview('before')" :disabled="busy">
                课前复核（增量刷新）
              </button>
              <button class="btn ghost" @click="publicClassReview('after')" :disabled="busy">
                课后复核（增量刷新）
              </button>
            </div>
            <div class="muted" style="font-size:11.5px">
              公开课不定期 → 手动按钮；早会时刻不写死在代码，改完<b>立即生效</b>（不必重启后端）。
              两种模式都是「限定等级范围的增量刷新」——只有新消息的客户才会重评（D3 拍板）。
            </div>
            <div style="display:flex;align-items:center;gap:8px">
              <b style="font-size:12.5px">当前 {{ saList.scope.join('/') }} 名单：{{ saList.count }} 人</b>
              <button class="btn ghost" style="padding:2px 8px;font-size:12px" @click="loadSaReview">刷新名单</button>
            </div>
            <div v-if="saList.items && saList.items.length" style="max-height:190px;overflow:auto">
              <table style="width:100%;font-size:12px">
                <thead><tr class="muted"><th style="text-align:left">等级</th><th style="text-align:left">客户</th>
                  <th style="text-align:left">销售</th><th style="text-align:right">分</th>
                  <th style="text-align:left">复核</th></tr></thead>
                <tbody>
                  <tr v-for="x in saList.items.slice(0, 50)" :key="x.assessment_id">
                    <td><span class="tag" :class="gradeTag(x.grade)">{{ x.grade }}</span></td>
                    <td>{{ x.target_name || x.target_id }}</td>
                    <td class="muted">{{ x.staff_name || '-' }}</td>
                    <td style="text-align:right">{{ x.raw_score }}</td>
                    <td class="muted">{{ x.review_status }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head">治理与观测（闸6 门禁 / 边界4 保留期 / B4·D2-1 参数）</div>
          <div class="card-body" style="display:grid;gap:12px">
            <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
              <div style="width:140px">
                <label class="lbl">单次 LLM 超时（秒，0=默认）</label>
                <input v-model.number="arch.llm_timeout_sec" class="input" type="number" min="0" />
              </div>
              <div style="width:110px">
                <label class="lbl">客户最少发言</label>
                <input v-model.number="arch.ctx_min_customer_msgs" class="input" type="number" min="0" />
              </div>
              <div style="width:130px">
                <label class="lbl">文本长度下限（0=不限）</label>
                <input v-model.number="arch.ctx_min_effective_chars" class="input" type="number" min="0" />
              </div>
              <div style="width:150px">
                <label class="lbl">数据保留（天，0=不清理）</label>
                <input v-model.number="arch.data_retention_days" class="input" type="number" min="0" />
              </div>
              <label class="lbl" style="display:flex;align-items:center;gap:6px;padding-bottom:8px">
                <input type="checkbox" v-model="arch.eval_gate_enabled" /> 回归门禁
              </label>
              <label class="lbl" style="display:flex;align-items:center;gap:6px;padding-bottom:8px">
                <input type="checkbox" v-model="arch.writeback_delta_mode" /> 差值累加回写
              </label>
              <button class="btn" @click="saveArch" :disabled="busy">保存</button>
            </div>

            <!-- 规则发布：`rules.json` 改完**必须**走这里，否则日常评分会被闸6 挡住
                 （而且只打一行 print、静默不跑）。此前只能 curl，见 AGENTS.md 的
                 「规则变更与门禁」；这里给界面入口，并把门禁结论原样显示出来。 -->
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
              <span class="muted" style="font-size:11.5px">
                当前规则 <b>{{ ruleInfo.rule_version || '-' }}</b>
                <span v-if="ruleInfo.rule_count">· {{ ruleInfo.rule_count }} 条</span>
                <span v-if="ruleInfo.effective_at">
                  · 自 {{ fmtTime(toEpoch(ruleInfo.effective_at)) }} 起生效
                </span>
              </span>
              <button class="btn primary" @click="publishRules" :disabled="busy"
                      title="把当前 rules.json 发布到评分链路（走闸6 门禁）">发布规则</button>
              <button class="btn ghost" @click="publishRulesForce" :disabled="busy"
                      title="门禁阻断但仍要上线时用（写审计）">强制发布</button>
              <button class="btn ghost" @click="purgeRetention" :disabled="busy"
                      title="按保留期清理运行观测表（不可恢复）">执行保留期清理</button>
            </div>

            <div class="alert info" style="font-size:12px" v-if="gate">
              <b>闸6 门禁</b>：{{ gate.decidable
                  ? (gate.gate_passed ? '有基线且指标未下降 → 允许发布' : '指标下降 → 发布会被阻断')
                  : '无法判定（' + gate.reason + '）' }}
              <span class="muted">— 「无法判定」不等于「通过」</span>
            </div>

            <div class="alert info" style="font-size:12px" v-if="retention && retention.enabled">
              <b>边界4 保留期</b>（{{ retention.days }} 天）：只清运行观测表 ——
              <span v-for="(v, k) in retention.tables" :key="k" style="margin-right:8px">
                {{ k }}={{ v.rows }}
              </span>
              <div class="muted">业务结论表（评估 / 复核 / 回写账本 / 评估集）**永不按天数清理**。</div>
            </div>
            <div class="alert warn" style="font-size:12px" v-else-if="retention">
              保留期未开启（0 天 = 不清理）—— 保留多久是业务决策，代码不替业务拍板。
            </div>
          </div>
        </div>

        <!-- ============ 回写审批队列 ============
             策略判定 confirm / human 的客户回写不自动写微伴，而是排在这里等人工签字。
             `outbox` 里那条待办会一直保持 pending（G6：宁可留着也不静默丢），
             批准即放行、驳回即置终态 —— 这是"需人工二次确认后才写入"的真实兑现处。 -->
        <div class="card">
          <div class="card-head">
            回写审批队列
            <span class="tag" :class="approvals.length ? 'orange' : 'green'" style="margin-left:8px">
              {{ approvals.length }} 条待审
            </span>
            <span class="muted" style="margin-left:auto;font-size:11.5px">
              策略置信度处于中间档 / 偏低时转人工
            </span>
          </div>
          <div class="card-body" style="display:grid;gap:12px">
            <div v-if="!approvals.length" class="muted" style="font-size:12.5px">
              没有待审批的回写 —— 策略放行的回写是自动走的，只有「需人工确认」的会停在这里。
            </div>
            <div v-for="a in approvals" :key="a.request_id" style="border-bottom:1px solid var(--border);padding-bottom:10px">
              <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap">
                <b style="font-size:12.5px">#{{ a.request_id }}</b>
                <span style="font-size:12.5px">{{ approvalClient(a) }}</span>
                <span class="tag" style="margin-left:4px">拟加 {{ (a.params_redacted || {}).score ?? '-' }} 分</span>
                <span class="muted" style="font-size:11.5px">{{ fmtTime(toEpoch(a.created_at)) }}</span>
                <span v-if="a.expires_at" class="muted" style="font-size:11.5px">
                  有效期至 {{ fmtTime(toEpoch(a.expires_at)) }}
                </span>
              </div>
              <div class="muted" style="font-size:11.5px;margin-top:2px">
                销售 {{ (a.params_redacted || {}).staff_id || '-' }}｜请求方 {{ a.requested_by }}
              </div>
              <div style="display:flex;gap:8px;margin-top:6px;align-items:center">
                <input v-model="approvalNote[a.request_id]" class="input" style="flex:1"
                       placeholder="批注（会写进审计）" />
                <button class="btn primary sm" :disabled="busy" @click="decideApproval(a, true)">批准</button>
                <button class="btn danger sm" :disabled="busy" @click="decideApproval(a, false)">驳回</button>
              </div>
            </div>
          </div>
        </div>

      </div>
      <!-- 左栏（控制）到此结束；下面两块是**全宽**运维面板（`.span-2`）——
           它们的表格列多（7 列 / 6 列），塞进 360px 的控制栏会横向溢出。 -->

      <!-- ============ 批次任务（外循环 task/task_step）============
             「这一批走到哪一步、能不能续跑」此前只有 curl 能答；任务检查点库里一直有数据。 -->
      <div class="card span-2">
          <div class="card-head">
            批次任务
            <span v-if="taskStats" class="muted" style="margin-left:8px;font-size:11.5px">
              30 天 {{ Object.entries(taskStats.by_status || {}).map(([k, v]) => k + '×' + v).join(' ') || '无' }}
              ｜步骤 {{ taskStats.steps?.total || 0 }}（失败 {{ taskStats.steps?.failed || 0 }}）
            </span>
            <button class="btn sm" style="margin-left:auto" :disabled="busy" @click="loadTasks">刷新</button>
          </div>
          <div class="card-body" style="display:grid;gap:8px">
            <table class="tbl">
              <thead>
                <tr><th>#</th><th>类型</th><th>状态</th><th>进度</th><th>心跳</th><th>续自</th><th></th></tr>
              </thead>
              <tbody>
                <tr v-for="t in tasks" :key="t.task_id">
                  <td class="mono">{{ t.task_id }}</td>
                  <td class="muted">{{ t.kind }}</td>
                  <td>
                    <span class="tag" :class="['succeeded'].includes(t.status) ? 'green'
                      : ['failed', 'cancelled'].includes(t.status) ? 'red'
                      : t.status === 'running' ? 'blue' : 'orange'">{{ t.status }}</span>
                  </td>
                  <td class="muted" style="font-size:11.5px">
                    {{ t.progress?.stage || '-' }}
                    <span v-if="t.progress?.done != null">（{{ t.progress.done }}/{{ t.progress.total }}）</span>
                  </td>
                  <td class="muted" style="font-size:11.5px">{{ fmtRelative(toEpoch(t.heartbeat_at)) }}</td>
                  <td class="mono">{{ t.resumed_from || '-' }}</td>
                  <td style="white-space:nowrap">
                    <button class="btn sm" :disabled="busy" @click="openTask(t)">详情</button>
                    <button class="btn sm" style="margin-left:4px" :disabled="busy"
                            @click="cancelTask(t)">取消</button>
                    <button class="btn sm" style="margin-left:4px" :disabled="busy"
                            :title="'从检查点新建任务续跑'" @click="resumeTask(t)">续跑</button>
                  </td>
                </tr>
                <tr v-if="!tasks.length">
                  <td colspan="7" class="muted" style="text-align:center;padding:16px">
                    还没有批次任务（跑一次评分 / 同步就会出现）
                  </td>
                </tr>
              </tbody>
            </table>
            <div v-if="taskOpen" style="max-height:260px;overflow:auto;font-size:12px">
              <div class="muted" style="margin-bottom:4px">
                任务 #{{ taskOpen.task?.task_id }} 的步骤（检查点）：
              </div>
              <table class="tbl">
                <thead><tr><th>seq</th><th>步骤</th><th>状态</th><th>尝试</th><th>错误</th></tr></thead>
                <tbody>
                  <tr v-for="s in taskOpen.steps" :key="s.seq">
                    <td class="mono">{{ s.seq }}</td>
                    <td>{{ s.name }}</td>
                    <td class="muted">{{ s.status }}</td>
                    <td class="mono">{{ s.attempts }}</td>
                    <td class="muted" style="font-size:11px">{{ s.error_message || '' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>

      <!-- ============ 审计与策略留痕 ============
             `policy_decision`（策略判定快照）与 `tool_audit`（工具调用五问）此前**只写不看**：
             库里 1,396 行策略判定、39 行工具调用，页面上一个数字都看不到。 -->
      <div class="card span-2">
          <div class="card-head">
            审计与策略留痕
            <span class="muted" style="margin-left:auto;font-size:11.5px">
              近 30 天策略判定 / 近 7 天工具调用
            </span>
          </div>
          <div class="card-body" style="display:grid;gap:10px">
            <div class="muted" style="font-size:12px">
              <b>策略判定</b>：
              <span v-if="policyStats">
                <span v-for="(v, k) in (policyStats.by_action || {})" :key="k" style="margin-right:10px">
                  {{ k }}={{ v }}
                </span>
                <span v-if="!(policyStats.by_action || {})" >暂无</span>
              </span>
              <span v-else>—</span>
              <span v-if="policyStats" class="muted">｜共 {{ policyStats.total || 0 }} 条</span>
            </div>
            <div class="muted" style="font-size:12px">
              <b>审计动作</b>：
              <span v-if="auditStats">
                <span v-for="(v, k) in (auditStats.counts || {})" :key="k" style="margin-right:10px">
                  {{ k }}={{ v }}
                </span>
              </span>
              <span v-else>—</span>
            </div>
            <table class="tbl">
              <thead>
                <tr><th>时间</th><th>工具</th><th>参数</th><th>决策</th><th>结果</th><th>耗时</th></tr>
              </thead>
              <tbody>
                <tr v-for="c in toolCalls" :key="c.call_id">
                  <td class="muted" style="font-size:11.5px">{{ fmtTime(toEpoch(c.created_at)) }}</td>
                  <td class="mono" style="font-size:11.5px">{{ c.tool_name }}</td>
                  <td class="muted" style="font-size:11px">{{ JSON.stringify(c.params_redacted || {}).slice(0, 40) }}</td>
                  <td class="muted">{{ c.decision }}</td>
                  <td>
                    <span class="tag" :class="c.result_status === 'ok' ? 'green' : 'red'">
                      {{ c.result_status }}
                    </span>
                  </td>
                  <td class="muted">{{ c.elapsed_ms != null ? c.elapsed_ms + 'ms' : '-' }}</td>
                </tr>
                <tr v-if="!toolCalls.length">
                  <td colspan="6" class="muted" style="text-align:center;padding:16px">
                    近 7 天没有工具调用记录（只有"真写微伴"这类动作才会落这里）
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

      <!-- 右：指标 + 运行 + 结果 -->
      <!-- `col-right` 把右栏钉在第 1 行第 2 列：不钉的话，两张全宽面板（DOM 在右栏之前）
           会先占掉第 2、3 行，右栏被挤到最后一行去。 -->
      <div class="col-right" style="display:grid;gap:16px">
        <div class="grid-stats">
          <div class="stat card"><div class="label">已评客户</div><div class="value">{{ (metrics?.customers || 0).toLocaleString() }}</div></div>
          <div class="stat card"><div class="label">待复核</div><div class="value" style="color:var(--warn)">{{ metrics?.review_pending || 0 }}</div></div>
          <div class="stat card"><div class="label">已完成复核</div><div class="value" style="color:var(--ok)">{{ metrics?.review_done || 0 }}</div></div>
          <div class="stat card"><div class="label">回写成功 / 演练</div><div class="value">{{ metrics?.wb_ok || 0 }} <span class="muted" style="font-size:14px">/ {{ metrics?.wb_dry || 0 }}</span></div></div>
          <div class="stat card"><div class="label">平均分</div><div class="value">{{ metrics?.avg_score ?? '-' }}</div></div>
          <div class="stat card">
            <div class="label">人工与AI一致率</div>
            <div class="value">{{ metrics?.agree_rate == null ? '-' : Math.round(metrics.agree_rate * 100) + '%' }}</div>
            <div class="sub">{{ metrics?.review_agree || 0 }}/{{ metrics?.review_decided || 0 }} 已决策</div>
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <!-- P5：这一张卡原来把三个口径混在一起 —— 右上角是**当前规则版本**，
                 进度与数字是**最近一次运行**，中间文案又是「暂无任务」，
                 看起来像"任务卡在 75%"或"v1.1 已经跑过了"。现在拆开写清。 -->
            {{ running ? '当前任务' : '最近一次运行' }}
            <span class="tag" :class="STATUS_TAG[run.status] || 'blue'">{{ STATUS_CN[run.status] || '空闲' }}</span>
            <span class="muted" style="margin-left:auto;font-size:12px">
              当前规则 {{ status?.rule_version }}（本行版本：{{ run.rule_version || '—' }}）
            </span>
          </div>
          <div class="card-body" style="display:grid;gap:12px">
            <div>
              <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:6px">
                <span class="muted">{{ running ? (status?.progress?.stage || '启动中') : '当前没有任务在跑' }}</span>
                <span class="muted">{{ run.session_done || 0 }} / {{ run.session_total || 0 }} 会话</span>
              </div>
              <div class="progress"><i :style="{ width: progress + '%' }"></i></div>
            </div>
            <div class="grid-stats">
              <div class="stat card"><div class="label">新增评分</div><div class="value" style="color:var(--brand)">{{ run.assessment_new || 0 }}</div></div>
              <div class="stat card"><div class="label">转入复核</div><div class="value" style="color:var(--warn)">{{ run.review_new || 0 }}</div></div>
              <div class="stat card"><div class="label">大模型调用</div><div class="value">{{ run.llm_calls || 0 }}<span class="muted" style="font-size:13px"> / 失败 {{ run.llm_failed || 0 }}</span></div></div>
            </div>
            <div class="alert warn" v-if="run.status === 'partial'">
              本轮<b>未跑完</b>即退出：{{ PARTIAL_CN[run.partial_reason] || run.partial_reason }}。已处理的会话结论有效，可稍后再跑一轮续上。
            </div>
            <div class="alert info" v-if="status?.progress?.current_target">
              正在评分：{{ status.progress.current_target }}
              <span class="muted" style="margin-left:6px">{{ status.progress.current_staff || '' }}</span>
            </div>
            <div class="alert err" v-if="run.error">{{ run.error }}</div>
          </div>
        </div>

        <div class="card">
          <div class="card-head">等级分布</div>
          <div class="card-body" style="display:grid;gap:8px">
            <div v-for="g in grades" :key="g[0]" style="display:flex;align-items:center;gap:10px">
              <span class="tag" :class="gradeTag(g[0])" style="width:30px;justify-content:center">{{ g[0] }}</span>
              <div class="progress" style="flex:1"><i :style="{ width: ((g[1] || 0) / gradeMax * 100) + '%' }"></i></div>
              <span class="mono" style="width:46px;text-align:right">{{ g[1] || 0 }}</span>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head">评分结果</div>
          <div class="card-body" style="display:grid;gap:12px">
            <div class="filters">
              <select v-model="filters.grade" class="select" style="width:110px" @change="search">
                <option value="">全部等级</option>
                <option v-for="g in ['S','A','B','C','D']" :key="g" :value="g">{{ g }} 级</option>
              </select>
              <select v-model="filters.review_status" class="select" style="width:130px" @change="search">
                <option value="">全部复核状态</option>
                <option value="pending">未打分</option>
                <option value="done">已完成</option>
                <option value="ignored">已忽略</option>
                <option value="na">无需复核</option>
              </select>
              <input v-model="filters.q" class="input" style="flex:1;min-width:140px"
                     placeholder="客户 / 员工名称" @keyup.enter="search" />
              <button class="btn" @click="search">查询</button>
            </div>
            <div style="overflow:auto">
              <table class="tbl">
                <thead><tr><th>客户</th><th>员工</th><th>分数</th><th>等级</th><th>加分/扣分</th><th>回写</th><th>复核</th><th>时间</th></tr></thead>
                <tbody>
                  <tr v-for="a in list.items" :key="a.assessment_id" style="cursor:pointer" @click="openDetail(a.assessment_id)">
                    <td>{{ a.target_name || a.target_id }}</td>
                    <td class="muted">{{ (a.staff_name || '').slice(0, 12) }}</td>
                    <td class="mono">{{ a.raw_score }}</td>
                    <td><span class="tag" :class="gradeTag(a.grade)">{{ a.grade }}</span></td>
                    <td class="mono">+{{ a.positive_total }} / {{ a.negative_total }}</td>
                    <td><span class="tag" :class="a.writeback_status === 'ok' ? 'green' : a.writeback_status === 'failed' ? 'red' : 'blue'">{{ a.writeback_status }}</span></td>
                    <td><span class="tag" :class="a.review_status === 'pending' ? 'orange' : a.review_status === 'done' ? 'green' : ''">{{ a.review_status === 'na' ? '无需' : a.review_status === 'pending' ? '未打分' : a.review_status === 'done' ? '已完成' : '已忽略' }}</span></td>
                    <td class="muted" style="font-size:12px">{{ fmtRelative(a.last_msg_time) }}</td>
                  </tr>
                  <tr v-if="!list.items.length"><td colspan="8" class="muted" style="text-align:center;padding:24px">暂无评分结果，点击左上「开始评分」</td></tr>
                </tbody>
              </table>
            </div>
            <Pager :page="filters.page" :page-size="filters.page_size"
                   :total="list.total" show-total @go="goto" />
          </div>
        </div>

        <div class="card">
          <div class="card-head">运行记录</div>
          <div style="max-height:300px;overflow:auto">
            <table class="tbl">
              <thead><tr><th>#</th><th>状态</th><th>触发</th><th>会话</th><th>评分</th><th>复核</th><th>回写</th><th>用时</th><th>开始</th></tr></thead>
              <tbody>
                <tr v-for="r in runs" :key="r.id">
                  <td class="mono">{{ r.id }}</td>
                  <td><span class="tag" :class="STATUS_TAG[r.status]">{{ STATUS_CN[r.status] || r.status }}</span></td>
                  <td><span class="tag" :class="r.triggered_by === 'schedule' ? 'orange' : 'blue'">{{ r.triggered_by === 'schedule' ? '定时' : '手动' }}</span></td>
                  <td class="mono">{{ r.session_done }}/{{ r.session_total }}</td>
                  <td class="mono" style="color:var(--brand)">{{ r.assessment_new }}</td>
                  <td class="mono" style="color:var(--warn)">{{ r.review_new }}</td>
                  <td class="mono">{{ r.writeback_ok }}/{{ r.writeback_fail }}</td>
                  <td class="mono">{{ r.elapsed_sec ?? '-' }}s</td>
                  <td class="muted" style="font-size:12px">{{ fmtTime(toEpoch(r.started_at)) }}</td>
                </tr>
                <tr v-if="!runs.length"><td colspan="9" class="muted" style="text-align:center;padding:20px">暂无运行记录</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- 评分详情抽屉 -->
    <div v-if="detail" class="mask" @click.self="detail = null">
      <div class="drawer">
        <div class="card-head">
          评分详情 · {{ detail.target_name }}
          <span class="tag" :class="gradeTag(detail.grade)" style="margin-left:8px">{{ detail.grade }}</span>
          <button class="btn sm" style="margin-left:auto" @click="detail = null">关闭</button>
        </div>
        <div class="card-body" style="display:grid;gap:14px">
          <div class="alert info">
            总分 <b>{{ detail.raw_score }}</b>（加分 {{ detail.positive_total }} / 扣分 {{ detail.negative_total }}）
            · 规则 {{ detail.rule_version }}
            <span class="muted">｜</span>
            <span :title="'系统算出（命中项置信 × 覆盖率 − 无效项惩罚 + 一致性加成）'">
              系统置信度 <b>{{ detail.session_confidence == null ? '—' : detail.session_confidence.toFixed(2) }}</b>
            </span>
            <span class="muted" :title="'模型自报值，仅留档，不参与任何决策'">
              （模型自报 {{ detail.model_confidence == null ? '—' : detail.model_confidence }}）
            </span>
            <span class="muted">· {{ fmtTime(toEpoch(detail.analyzed_at)) }}</span>
          </div>
          <div v-if="detail.review_reasons && detail.review_reasons.length" class="alert warn" style="font-size:12.5px">
            触发人工复核：<b>{{ detail.review_reasons.map(r => REASON_CN[r] || r).join('、') }}</b>
            <span class="muted">（由规则判定，非模型自报）</span>
          </div>
          <div>
            <div class="lbl">加分项</div>
            <div v-for="(i, k) in (detail.positive_items || [])" :key="k" class="item">
              <span class="tag green">+{{ i.score }}</span>
              <div><b>{{ i.dimension }}</b><div class="muted" style="font-size:12px;white-space:pre-wrap">{{ i.reason }}</div>
                <!-- 证据行交给公共气泡组件（AGENTS.md: Reuse Before Rewrite） -->
                <ChatBubbles compact :text="(i.evidence || []).join('\n')" />
              </div>
            </div>
            <div v-if="!(detail.positive_items || []).length" class="muted" style="font-size:12px">无</div>
          </div>
          <div>
            <div class="lbl">扣分项（需人工复核）</div>
            <div v-for="(i, k) in (detail.negative_items || [])" :key="k" class="item">
              <span class="tag red">{{ i.score }}</span>
              <div><b>{{ i.dimension }}</b><div class="muted" style="font-size:12px;white-space:pre-wrap">{{ i.reason }}</div>
                <!-- 证据行交给公共气泡组件（AGENTS.md: Reuse Before Rewrite） -->
                <ChatBubbles compact :text="(i.evidence || []).join('\n')" />
              </div>
            </div>
            <div v-if="!(detail.negative_items || []).length" class="muted" style="font-size:12px">无</div>
          </div>
          <div>
            <div class="lbl">客户画像</div>
            <div class="alert info" style="white-space:pre-wrap">{{ detail.profile_summary || '-' }}</div>
          </div>
          <div>
            <div class="lbl">销售策略</div>
            <div class="alert ok" style="white-space:pre-wrap">{{ detail.sales_strategy || '-' }}</div>
          </div>
          <div v-if="detail.writeback" class="muted" style="font-size:12px">
            回写：{{ detail.writeback.status }}（{{ detail.writeback.mode }}，分值 {{ detail.writeback.score }}）
            {{ detail.writeback.errmsg || '' }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.layout { display: grid; grid-template-columns: 360px 1fr; gap: 16px; align-items: start; }
/* 全宽运维面板（批次任务 / 审计留痕）：列多，必须横跨两栏 */
.layout > .span-2 { grid-column: 1 / -1; }
/* 右栏显式钉在第 1 行第 2 列 —— 见模板里那段注释 */
.layout > .col-right { grid-column: 2; grid-row: 1; }
@media (max-width: 1200px) { .layout { grid-template-columns: 1fr; } }
.ck { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text-2); }
.filters { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
/* 证据行气泡样式已移入 components/ChatBubbles.vue（AGENTS.md: Reuse Before Rewrite） */
.mask { position: fixed; inset: 0; background: rgba(16,24,40,.35); display: flex; justify-content: flex-end; z-index: 50; }
.drawer { width: 560px; max-width: 92vw; height: 100%; background: var(--panel); overflow: auto; box-shadow: -8px 0 30px rgba(16,24,40,.14); }
</style>
