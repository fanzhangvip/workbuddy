<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api, fmtTime, fmtRelative } from '../api'

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
const busy = ref(false)
const error = ref('')
// 架构新增项（第 2–4 批）：运行时参数 + F2 早会/公开课复核名单
const arch = ref({ llm_timeout_sec: 0, data_retention_days: 0, eval_gate_enabled: true,
                   morning_cron: '', public_class_grade_scope: 'S,A',
                   writeback_delta_mode: true, ctx_min_customer_msgs: 1,
                   ctx_min_effective_chars: 0 })
const saList = ref({ count: 0, items: [], scope: ['S', 'A'] })
const gate = ref(null)
const retention = ref(null)
let timer = null

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
  try { staffs.value = (await api.staffs({ only_with_session: 1, limit: 800 })).items || [] }
  catch (e) { /* ignore */ }
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

const WB_CN = { live: '真实写入', dry_run: '演练模式（不写入）', off: '已关闭' }

async function start() {
  busy.value = true; error.value = ''
  try {
    const r = await api.scoringStart({
      staff_ids: form.value.staff_id ? [form.value.staff_id] : null,
      max_sessions: Number(form.value.max_sessions) || 0,
      min_messages: Number(form.value.min_messages) || 0,
      force: form.value.force,
    })
    if (!r.ok) error.value = r.reason || '启动失败'
  } catch (e) { error.value = e.message }
  busy.value = false; refresh()
}

async function stop() {
  busy.value = true
  try { await api.scoringStop() } catch (e) { error.value = e.message }
  busy.value = false; refresh()
}

async function saveSchedule() {
  try { schedule.value = await api.scoringSetSchedule(Number(scInterval.value) || 0) }
  catch (e) { error.value = e.message }
}

// ---- 架构新增参数（第 2–4 批）----
async function saveArch() {
  busy.value = true; error.value = ''
  try {
    const r = await api.scoringSetSettings({ ...arch.value })
    if (!r.ok) error.value = r.reason || '保存失败'
    else refresh()
  } catch (e) { error.value = e.message }
  busy.value = false
}

// F2：早会/公开课复核 —— 点一下「增量刷新」S/A 名单，并展示刷新后的名单
async function publicClassReview(mode) {
  busy.value = true; error.value = ''
  try {
    const r = await api.scoringPublicClassReview({ mode })
    if (!r.ok) error.value = r.reason || '启动失败'
    if (r.name_list) saList.value = r.name_list
    refresh()
  } catch (e) { error.value = e.message }
  busy.value = false
}

async function loadSaReview() {
  try { saList.value = await api.scoringSaReview() } catch (e) { /* 未就绪时忽略 */ }
}

async function loadGate() {
  try { gate.value = await api.scoringRulesGate() } catch (e) { /* ignore */ }
}

async function loadRetention() {
  try { retention.value = await api.scoringRetention() } catch (e) { /* ignore */ }
}

async function setMode(mode) {
  try { settings.value = await api.scoringSetSettings({ writeback_mode: mode }); refresh() }
  catch (e) { error.value = e.message }
}

function search() { filters.value.page = 1; refresh() }
function goto(p) { filters.value.page = p; refresh() }

async function openDetail(id) { try { detail.value = await api.scoringAssessment(id) } catch (e) { error.value = e.message } }

const STATUS_TAG = { success: 'green', running: 'blue', failed: 'red', stopped: 'orange', partial: 'purple' }
const STATUS_CN = { success: '成功', running: '运行中', failed: '失败', stopped: '已停止', partial: '部分完成' }
// 部分完成的原因（架构 G3/G4）：让「为什么没跑完」一眼可见，而不是看到状态就慌
const PARTIAL_CN = { deadline: '达到整轮超时上限', max_llm_calls: '达到大模型调用次数上限' }
const GRADE_TAG = { S: 'red', A: 'orange', B: 'blue', C: 'green', D: '' }
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
  refresh(); loadStaffs(); loadSaReview(); loadGate(); loadRetention()
  timer = setInterval(refresh, 3000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="page">
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
            <div class="two">
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
            <div style="display:flex;gap:10px">
              <button class="btn primary" style="flex:1" :disabled="running || busy" @click="start">
                <i v-if="busy" class="spin"></i>{{ running ? '评分中…' : '开始评分' }}
              </button>
              <button class="btn danger" :disabled="!running || busy" @click="stop">停止</button>
            </div>
            <div v-if="error" class="alert err">{{ error }}</div>
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
              当前：{{ WB_CN[settings.writeback_mode] }}
              <template v-if="settings.writeback_mode === 'live'">
                —— 会真实修改微伴客户评分。微伴为<b>累加</b>语义，系统已按
                <b>差值累加</b>回写（D2-1），不会重复累加；水位见 writeback_ledger。
              </template>
            </div>
            <div class="muted" style="font-size:11.5px;line-height:1.7">
              微伴「添加客户评分」要求 score &gt; 0，且接口是覆盖还是累加尚未确认，
              因此默认演练模式。可在微伴小号验证后再切换。
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
                    <td><span class="tag" :class="GRADE_TAG[x.grade]">{{ x.grade }}</span></td>
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
      </div>

      <!-- 右：指标 + 运行 + 结果 -->
      <div style="display:grid;gap:16px">
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
            当前任务
            <span class="tag" :class="STATUS_TAG[run.status] || 'blue'">{{ STATUS_CN[run.status] || '空闲' }}</span>
            <span class="muted" style="margin-left:auto;font-size:12px">规则 {{ status?.rule_version }}</span>
          </div>
          <div class="card-body" style="display:grid;gap:12px">
            <div>
              <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:6px">
                <span class="muted">{{ status?.progress?.stage || '暂无任务' }}</span>
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
              <span class="tag" :class="GRADE_TAG[g[0]]" style="width:30px;justify-content:center">{{ g[0] }}</span>
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
                    <td><span class="tag" :class="GRADE_TAG[a.grade]">{{ a.grade }}</span></td>
                    <td class="mono">+{{ a.positive_total }} / {{ a.negative_total }}</td>
                    <td><span class="tag" :class="a.writeback_status === 'ok' ? 'green' : a.writeback_status === 'failed' ? 'red' : 'blue'">{{ a.writeback_status }}</span></td>
                    <td><span class="tag" :class="a.review_status === 'pending' ? 'orange' : a.review_status === 'done' ? 'green' : ''">{{ a.review_status === 'na' ? '无需' : a.review_status === 'pending' ? '未打分' : a.review_status === 'done' ? '已完成' : '已忽略' }}</span></td>
                    <td class="muted" style="font-size:12px">{{ fmtRelative(a.last_msg_time) }}</td>
                  </tr>
                  <tr v-if="!list.items.length"><td colspan="8" class="muted" style="text-align:center;padding:24px">暂无评分结果，点击左上「开始评分」</td></tr>
                </tbody>
              </table>
            </div>
            <div class="pager" v-if="list.total > filters.page_size">
              <button class="btn sm" :disabled="filters.page <= 1" @click="goto(filters.page - 1)">上一页</button>
              <span class="muted" style="font-size:12px">{{ filters.page }} / {{ Math.ceil(list.total / filters.page_size) }}（共 {{ list.total }}）</span>
              <button class="btn sm" :disabled="filters.page >= Math.ceil(list.total / filters.page_size)" @click="goto(filters.page + 1)">下一页</button>
            </div>
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
                  <td class="muted" style="font-size:12px">{{ fmtTime(Date.parse(r.started_at) / 1000) }}</td>
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
          <span class="tag" :class="GRADE_TAG[detail.grade]" style="margin-left:8px">{{ detail.grade }}</span>
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
            <span class="muted">· {{ fmtTime(Date.parse(detail.analyzed_at) / 1000) }}</span>
          </div>
          <div v-if="detail.review_reasons && detail.review_reasons.length" class="alert warn" style="font-size:12.5px">
            触发人工复核：<b>{{ detail.review_reasons.map(r => REASON_CN[r] || r).join('、') }}</b>
            <span class="muted">（由规则判定，非模型自报）</span>
          </div>
          <div>
            <div class="lbl">加分项</div>
            <div v-for="(i, k) in (detail.positive_items || [])" :key="k" class="item">
              <span class="tag green">+{{ i.score }}</span>
              <div><b>{{ i.dimension }}</b><div class="muted" style="font-size:12px">{{ i.reason }}</div>
                <div v-for="(ev, e) in (i.evidence || [])" :key="e" class="ev">证据：{{ ev }}</div>
              </div>
            </div>
            <div v-if="!(detail.positive_items || []).length" class="muted" style="font-size:12px">无</div>
          </div>
          <div>
            <div class="lbl">扣分项（需人工复核）</div>
            <div v-for="(i, k) in (detail.negative_items || [])" :key="k" class="item">
              <span class="tag red">{{ i.score }}</span>
              <div><b>{{ i.dimension }}</b><div class="muted" style="font-size:12px">{{ i.reason }}</div>
                <div v-for="(ev, e) in (i.evidence || [])" :key="e" class="ev">证据：{{ ev }}</div>
              </div>
            </div>
            <div v-if="!(detail.negative_items || []).length" class="muted" style="font-size:12px">无</div>
          </div>
          <div>
            <div class="lbl">客户画像</div>
            <div class="alert info">{{ detail.profile_summary || '-' }}</div>
          </div>
          <div>
            <div class="lbl">销售策略</div>
            <div class="alert ok">{{ detail.sales_strategy || '-' }}</div>
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
@media (max-width: 1200px) { .layout { grid-template-columns: 1fr; } }
.lbl { display: block; font-size: 12px; color: var(--text-2); margin-bottom: 6px; font-weight: 500; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.ck { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text-2); }
.seg { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.seg button { text-align: left; padding: 10px 12px; border-radius: 9px; border: 1px solid var(--border);
  background: var(--panel); cursor: pointer; font-family: inherit; font-size: 13px; font-weight: 600; color: var(--text); }
.seg button small { display: block; font-weight: 400; font-size: 11px; color: var(--text-3); margin-top: 3px; }
.seg button.on { border-color: var(--brand); background: var(--brand-weak); color: var(--brand); }
.filters { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.pager { display: flex; align-items: center; gap: 12px; justify-content: center; }
.item { display: flex; gap: 10px; padding: 8px 0; border-bottom: 1px dashed var(--border); }
.item:last-child { border-bottom: none; }
.ev { font-size: 12px; color: #1e4fa8; background: var(--brand-weak); border-radius: 6px; padding: 4px 8px; margin-top: 5px; }
.mask { position: fixed; inset: 0; background: rgba(16,24,40,.35); display: flex; justify-content: flex-end; z-index: 50; }
.drawer { width: 560px; max-width: 92vw; height: 100%; background: var(--panel); overflow: auto; box-shadow: -8px 0 30px rgba(16,24,40,.14); }
</style>
