<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api, fmtRelative } from '../api'

// 评估集：回答「判得准不准」。标注口径 = 只标「命中了哪些 rule_id + 档位」，不标总分。
const TABS = [
  { key: '', label: '全部' },
  { key: 'unlabeled', label: '待标注' },
  { key: 'prefilled', label: '待复核' },
  { key: 'confirmed', label: '已确认' },
  { key: 'frozen', label: '已冻结' },
  { key: 'disputed', label: '有争议' },
]
const KINDS = [
  { key: 'golden', label: '金标准' },
  { key: 'regression', label: '回归集' },
  { key: 'edge', label: '边界集' },
]

const stats = ref({})
// E5 meta-loop：按 rule 的误报率排行 + 复核回流进度
const metaLoop = ref(null)
const gate = ref(null)
const evStatus = ref({})
const tab = ref('')
const kind = ref('golden')
const sampleN = ref(30)
const filters = ref({ page: 1, page_size: 20, q: '' })
const list = ref({ items: [], total: 0 })
const current = ref(null)
const rows = ref([])
const note = ref('')
const annotator = ref(localStorage.getItem('annotator') || '复核员')
const busy = ref(false)
const error = ref('')
const msg = ref('')
let timer = null

const RULE_INFO = ref({ items: [], rule_version: '' })
const THRESHOLDS = ref([])

async function refresh() {
  try {
    const [s, l, g, st] = await Promise.all([
      api.evalStats(), api.evalList({ ...filters.value, kind: kind.value, status: tab.value }),
      api.evalGate(), api.evalStatus(),
    ])
    stats.value = s
    list.value = { items: l.items || [], total: l.total || 0 }
    gate.value = g
    evStatus.value = st
  } catch (e) { error.value = '后端未就绪：' + e.message }
}

async function loadRules() {
  try {
    const r = await api.reviewOptions().catch(() => null)   // 复用同一后端（负分）→ 下面补正分
    const all = await (await fetch('/api/eval/options')).json()
    RULE_INFO.value = all
    THRESHOLDS.value = (await (await fetch('/api/scoring/rules')).json())?.grade_thresholds || []
  } catch (e) { /* 规则加载失败不阻塞列表 */ }
}

function switchTab(k) { tab.value = k; filters.value.page = 1; refresh() }
function switchKind() { filters.value.page = 1; current.value = null; refresh() }
function search() { filters.value.page = 1; refresh() }
function goto(p) { filters.value.page = p; refresh() }

function buildRows(caseObj, ruleItems) {
  const expMap = {}, baseMap = {}
  for (const it of (caseObj.expected_items || [])) expMap[it.rule_id] = it
  for (const it of (caseObj.baseline_items || [])) baseMap[it.rule_id] = it
  rows.value = (ruleItems || []).map(o => {
    const hit = expMap[o.rule_id]
    const opts = o.options || []
    const opt = hit ? (opts.find(x => x.score === hit.score) || opts[0]) : opts[0]
    return {
      rule_id: o.rule_id, dimension: o.dimension, polarity: o.polarity,
      engine: o.engine, hint: o.hint, options: opts,
      score: hit ? hit.score : (opt ? opt.score : 0),
      checked: !!hit, reason: hit?.reason || '',
      fromSystem: !!baseMap[o.rule_id], systemScore: baseMap[o.rule_id]?.score ?? null,
    }
  })
  note.value = caseObj.note || ''
}

async function open(r) {
  error.value = ''; msg.value = ''
  try {
    const d = await api.evalDetail(r.case_id)
    current.value = d
    const items = d.rules?.items || RULE_INFO.value.items || []
    buildRows(d.case || {}, items)
  } catch (e) { error.value = e.message }
}

const expectedScore = computed(() =>
  rows.value.filter(r => r.checked).reduce((s, r) => s + (r.score || 0), 0))
const expectedGrade = computed(() => {
  const ts = THRESHOLDS.value || []
  for (const t of ts) if (expectedScore.value >= t.min) return t.grade
  return '-'
})
const convLines = computed(() => {
  const txt = current.value?.conversation || ''
  return txt.split('\n').filter(Boolean).map(l => {
    const i = l.indexOf(':')
    return { who: l.slice(0, i), text: l.slice(i + 1).trim(), side: l.startsWith('销售') ? 'right' : 'left' }
  })
})
const posRows = computed(() => rows.value.filter(r => r.polarity === 'positive'))
const negRows = computed(() => rows.value.filter(r => r.polarity === 'negative'))

async function save(alsoFreeze = false) {
  if (!current.value) return
  busy.value = true; error.value = ''; msg.value = ''
  try {
    localStorage.setItem('annotator', annotator.value)
    const items = rows.value.filter(r => r.checked)
      .map(r => ({ rule_id: r.rule_id, score: r.score, reason: r.reason || r.dimension }))
    const res = await api.evalSave({
      case_id: current.value.case.case_id, items, note: note.value, annotator: annotator.value,
    })
    if (!res.ok) { error.value = res.reason || '保存失败'; busy.value = false; return }
    if (alsoFreeze) await api.evalFreeze({ case_id: current.value.case.case_id, frozen: true, annotator: annotator.value })
    msg.value = `已保存：${res.expected_raw_score} 分 · ${res.expected_grade} 级`
      + (res.invalid_items?.length ? `（${res.invalid_items.length} 项被丢弃）` : '')
    await refresh(); await open({ case_id: current.value.case.case_id })
  } catch (e) { error.value = e.message }
  busy.value = false
}

async function toggleFreeze() {
  if (!current.value) return
  busy.value = true
  try {
    const res = await api.evalFreeze({ case_id: current.value.case.case_id, frozen: !current.value.case.frozen })
    await refresh(); await open({ case_id: current.value.case.case_id })
    msg.value = res.frozen ? '已冻结（除仲裁外不可修改）' : '已解冻'
  } catch (e) { error.value = e.message }
  busy.value = false
}

async function doSample() {
  busy.value = true; error.value = ''; msg.value = ''
  try {
    const res = await api.evalSample({ kind: kind.value, limit: sampleN.value, min_messages: 5 })
    msg.value = `抽样完成：候选 ${res.candidates} → 新增 ${res.created} 条`
      + (res.distribution ? `｜分布 ${Object.keys(res.distribution).length} 层` : '')
    const dist = res.distribution || {}
    msg.value += '｜' + Object.entries(dist).map(([k, v]) => `${k}×${v}`).join(' ')
    await refresh()
  } catch (e) { error.value = e.message }
  busy.value = false
}

async function doRun() {
  busy.value = true; error.value = ''; msg.value = ''
  try {
    const res = await api.evalStart({ kind: kind.value })
    if (!res.ok) { error.value = res.reason || '启动失败'; busy.value = false; return }
    msg.value = '评估已在后台开跑（每条一次模型调用，请稍候）'
  } catch (e) { error.value = e.message }
  busy.value = false
}

async function setBaseline() {
  busy.value = true
  try {
    const res = await api.evalSetBaseline({})
    msg.value = res.ok ? '已把最近一次跑分设为基线' : (res.reason || '失败')
    await refresh()
  } catch (e) { error.value = e.message }
  busy.value = false
}

function reset() { current.value = null; error.value = ''; msg.value = '' }

// AI 预标注：把 AI 当时的判定填成草稿 → 人工从「改草稿」开始，而不是「从零列清单」
async function doPrefill() {
  busy.value = true; error.value = ''; msg.value = ''
  try {
    const res = await api.evalPrefill({ kind: kind.value })
    msg.value = `已预填 ${res.filled} 条草稿`
      + (res.skipped_no_baseline ? `｜${res.skipped_no_baseline} 条无 AI 判定可参考，需全手标` : '')
      + '。⚠️ 预标注来自 AI 自己，请逐条复核后保存（保存后才算「已确认」并计入指标）'
    if (res.plan_preview?.length) {
      msg.value += '｜预览 ' + res.plan_preview.slice(0, 5)
        .map(p => `#${p.case_id}:${p.grade}`).join(' ')
    }
    tab.value = 'prefilled'
    await refresh()
  } catch (e) { error.value = e.message }
  busy.value = false
}

const GTAG = { S: 'red', A: 'orange', B: 'blue', C: 'green', D: '' }
const pct = (v) => (v === null || v === undefined) ? '-' : (v * 100).toFixed(1) + '%'
const fmtMetric = (k, v) => (v === null || v === undefined) ? '-' : (k === 'rule_f1' || k === 'precision' || k === 'recall' || k === 'grade_acc' || k === 'fp_case_rate' ? pct(v) : v)

async function loadMetaLoop() {
  try { metaLoop.value = await api.evalMetaLoop() } catch (e) { /* 未就绪时忽略 */ }
}

async function runBackflow() {
  busy.value = true; msg.value = ''
  try {
    const r = await api.evalBackflow({ limit: 100 })
    msg.value = `回流完成：新增 ${r.created} 条，跳过 ${r.skipped} 条（人工判断已进入回归集）`
    loadMetaLoop()
  } catch (e) { error.value = e.message }
  busy.value = false
}

onMounted(() => { refresh(); loadRules(); loadMetaLoop(); timer = setInterval(() => { refresh() }, 8000) })
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="page">
    <!-- E5 meta-loop：把人工判断变回系统改进 -->
    <div class="card" style="margin-bottom:16px" v-if="metaLoop">
      <div class="card-head">meta-loop · 误报率排行与回流（E5/G8）
        <span class="tag" style="margin-left:8px">{{ metaLoop.ranking?.reviews_with_human || 0 }} 条人工结论</span>
        <span class="tag" style="margin-left:6px">
          回流 {{ metaLoop.review?.backflowed || 0 }} / {{ metaLoop.review?.done || 0 }} 已复核
        </span>
        <button class="btn sm" style="margin-left:auto" :disabled="busy" @click="runBackflow">
          回流改判到评估集
        </button>
      </div>
      <div class="card-body" style="display:grid;gap:12px">
        <div class="muted" style="font-size:11.5px;line-height:1.7">
          只统计<b>人工已完成</b>的复核（否则等于拿 AI 自己的判断当标准答案）。
          <b>误报率</b>=人工否掉的命中项 / AI 命中项总数；越高说明该规则越爱乱报 →
          据此改 <code>rules.json</code> 的 criteria/examples。
          <b>回流</b>只收改判过的（与 AI 一致的不回流，那只是确认、无新信息）。
        </div>
        <div v-if="metaLoop.ranking?.alerts?.length" class="alert warn" style="font-size:12px">
          有 <b>{{ metaLoop.ranking.alerts.length }}</b> 条规则误报率超过
          {{ Math.round((metaLoop.ranking.threshold || 0) * 100) }}%，建议优先复核其 criteria/examples。
        </div>
        <div v-if="metaLoop.ranking?.rules?.length" style="overflow:auto">
          <table style="width:100%;font-size:12px">
            <thead><tr class="muted">
              <th style="text-align:left">规则</th><th style="text-align:right">AI命中</th>
              <th style="text-align:right">人工保留</th><th style="text-align:right">误报</th>
              <th style="text-align:right">漏报</th><th style="text-align:right">误报率</th>
            </tr></thead>
            <tbody>
              <tr v-for="r in metaLoop.ranking.rules" :key="r.rule_id">
                <td>{{ r.rule_id }} <span v-if="r.alert" class="tag red">超阈值</span></td>
                <td style="text-align:right">{{ r.ai_hits }}</td>
                <td style="text-align:right">{{ r.human_keeps }}</td>
                <td style="text-align:right" :style="{ color: r.false_positives ? 'var(--danger)' : '' }">{{ r.false_positives }}</td>
                <td style="text-align:right">{{ r.misses }}</td>
                <td style="text-align:right">{{ Math.round((r.fp_rate || 0) * 100) }}%</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="muted" style="font-size:12px">
          暂无人工作出无效结论的复核记录（改判过的复核会出现在这里）。
        </div>
      </div>
    </div>

    <!-- 顶部：指标 + 门禁 -->
    <div class="card" style="margin-bottom:16px">
      <div class="card-head">评估集 · 判得准不准
        <span class="tag" style="margin-left:8px">规则 {{ stats.rule_version || '-' }}</span>
        <span v-if="gate?.has_baseline" class="tag" :class="gate.gate_passed ? 'green' : 'red'" style="margin-left:6px">
          门禁：{{ gate.gate_passed ? '通过' : '阻断' }}
        </span>
        <span v-else class="tag" style="margin-left:6px">尚无基线</span>
        <button class="btn sm" style="margin-left:auto" :disabled="busy" @click="setBaseline">设为基线</button>
      </div>
      <div class="card-body">
        <div class="metrics">
          <div class="m"><div class="mv">{{ stats.total || 0 }}</div><div class="ml">样本总数</div></div>
          <div class="m"><div class="mv">{{ stats.confirmed || 0 }}</div><div class="ml">已确认</div></div>
          <div class="m">
            <div class="mv" :class="stats.prefilled ? 'bad' : ''">{{ stats.prefilled || 0 }}</div>
            <div class="ml">待复核（AI 预标注）</div>
          </div>
          <div class="m"><div class="mv">{{ stats.unlabeled || 0 }}</div><div class="ml">待标注</div></div>
          <div class="m"><div class="mv">{{ stats.frozen || 0 }}</div><div class="ml">已冻结</div></div>
          <div class="m"><div class="mv">{{ stats.golden || 0 }}<span class="sub">/{{ stats.regression || 0 }}/{{ stats.edge || 0 }}</span></div><div class="ml">金/回归/边界</div></div>
          <div class="sep"></div>
          <div class="m"><div class="mv" :class="gate?.gate_passed === false ? 'bad' : ''">{{ fmtMetric('rule_f1', gate?.metrics?.rule_f1) }}</div><div class="ml">命中项 F1</div></div>
          <div class="m"><div class="mv">{{ fmtMetric('grade_acc', gate?.metrics?.grade_acc) }}</div><div class="ml">等级一致率</div></div>
          <div class="m"><div class="mv">{{ fmtMetric('fp_case_rate', gate?.metrics?.fp_case_rate) }}</div><div class="ml">误报率</div></div>
          <div class="m"><div class="mv">{{ gate?.metrics?.precision ? pct(gate.metrics.precision) : '-' }}<span class="sub">/{{ gate?.metrics?.recall ? pct(gate.metrics.recall) : '-' }}</span></div><div class="ml">精确/召回</div></div>
        </div>
        <div v-if="gate?.gate_diffs?.length" class="alert" :class="gate.gate_passed ? 'ok' : 'err'" style="margin-top:12px;font-size:12px">
          <span v-for="d in gate.gate_diffs" :key="d.metric" style="margin-right:14px">
            {{ d.metric }}：基线 {{ fmtMetric(d.metric, d.base) }} → 当前 {{ fmtMetric(d.metric, d.now) }}
            （{{ d.delta >= 0 ? '+' : '' }}{{ d.delta }}）{{ d.ok ? '✓' : '✗' }}
          </span>
          <span v-if="gate.baseline_set_at" class="muted">基线设于 {{ gate.baseline_set_at }}</span>
        </div>
        <div v-if="evStatus.running" class="alert warn" style="margin-top:12px">
          评估进行中：{{ evStatus.done }}/{{ evStatus.total }}
          <span v-if="evStatus.current">· 当前样本 #{{ evStatus.current }}</span>
        </div>
        <div v-if="msg" class="alert ok" style="margin-top:12px;font-size:12.5px">{{ msg }}</div>
        <div v-if="error" class="alert err" style="margin-top:12px;font-size:12.5px">{{ error }}</div>
      </div>
    </div>

    <div class="layout">
      <!-- 左：样本列表 -->
      <div class="card">
        <div class="card-head">样本</div>
        <div class="card-body" style="display:grid;gap:12px">
          <div class="tabs">
            <button v-for="t in TABS" :key="t.key" :class="{ on: tab === t.key }" @click="switchTab(t.key)">
              {{ t.label }}
              <span class="cnt">{{ t.key === '' ? (stats.total || 0) : (stats[t.key === 'unlabeled' ? 'unlabeled' : t.key] || 0) }}</span>
            </button>
          </div>
          <div style="display:grid;gap:8px">
            <div class="two">
              <select v-model="kind" class="select" @change="switchKind">
                <option v-for="k in KINDS" :key="k.key" :value="k.key">{{ k.label }}</option>
              </select>
              <div class="row-inline">
                <input v-model.number="sampleN" class="input" type="number" min="1" max="200" style="width:70px" />
                <button class="btn sm" :disabled="busy" @click="doSample">抽样</button>
                <button class="btn sm" :disabled="busy" title="把 AI 当时的判定填成草稿，人工只复核" @click="doPrefill">
                  AI 预标注
                </button>
              </div>
            </div>
            <input v-model="filters.q" class="input" placeholder="客户 / 员工 / 会话 id" @keyup.enter="search" />
            <div class="row-inline">
              <button class="btn" @click="search">查询</button>
              <button class="btn primary" :disabled="busy || evStatus.running" @click="doRun">
                <i v-if="busy" class="spin"></i>跑评估
              </button>
              <a class="btn" :href="api.evalExportUrl(kind)" target="_blank">导出</a>
            </div>
          </div>
          <div style="overflow:auto;max-height:calc(100vh - 420px)">
            <table class="tbl">
              <thead><tr><th>客户</th><th>消息</th><th>人标</th><th>基线</th></tr></thead>
              <tbody>
                <tr v-for="r in list.items" :key="r.case_id" :class="{ sel: current?.case?.case_id === r.case_id }"
                    style="cursor:pointer" @click="open(r)">
                  <td>
                    <div>{{ r.target_name || r.target_id }}</div>
                    <div class="muted" style="font-size:11.5px">{{ (r.staff_name || '').slice(0, 14) }}</div>
                  </td>
                  <td class="mono">{{ r.msg_count }}</td>
                  <td>
                    <span v-if="r.expected_grade" class="tag" :class="GTAG[r.expected_grade]">{{ r.expected_grade }}</span>
                    <span v-else class="muted" style="font-size:12px">—</span>
                    <span v-if="r.prefilled" class="tag orange" style="margin-left:4px"
                          title="AI 预标注草稿 —— 复核并保存后才算「已确认」">预</span>
                  </td>
                  <td>
                    <span v-if="r.baseline_grade" class="tag" :class="GTAG[r.baseline_grade]">{{ r.baseline_grade }}</span>
                    <span v-else class="muted" style="font-size:12px">—</span>
                    <span v-if="r.diff_from_baseline?.grade_changed" class="tag red" style="margin-left:4px">≠</span>
                  </td>
                </tr>
                <tr v-if="!list.items.length">
                  <td colspan="4" class="muted" style="text-align:center;padding:24px">
                    还没有样本 —— 点上方「抽样」从达标会话里分层抽取
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="pager" v-if="list.total > filters.page_size">
            <button class="btn sm" :disabled="filters.page <= 1" @click="goto(filters.page - 1)">上一页</button>
            <span class="muted" style="font-size:12px">{{ filters.page }} / {{ Math.ceil(list.total / filters.page_size) }}</span>
            <button class="btn sm" :disabled="filters.page >= Math.ceil(list.total / filters.page_size)" @click="goto(filters.page + 1)">下一页</button>
          </div>
        </div>
      </div>

      <!-- 右：标注 -->
      <div v-if="current" style="display:grid;gap:16px">
        <div class="card">
          <div class="card-head">
            标注 · {{ current.case.target_name || current.case.target_id }}
            <span class="tag" style="margin-left:8px">{{ current.case.kind }}</span>
            <span v-if="current.case.frozen" class="tag orange" style="margin-left:6px">已冻结</span>
            <span v-if="current.drift" class="tag red" style="margin-left:6px">内容已变（指纹不符）</span>
            <button class="btn sm" style="margin-left:auto" @click="reset">收起</button>
          </div>
          <div class="card-body" style="display:grid;gap:14px">
            <div class="alert info" style="font-size:12.5px">
              <b>只勾选「命中了哪些规则 + 档位」，不要填总分</b> —— 总分与等级由规则引擎自动算。
              <span class="muted">灰色「系统」标记 = 抽样时系统判定的同一项（供对照）。</span>
            </div>
            <div>
              <div class="lbl">聊天记录（最多 {{ current.msg_limit }} 条）</div>
              <div class="conv">
                <div v-for="(l, k) in convLines" :key="k" class="bubble" :class="l.side">
                  <span class="who">{{ l.who }}</span>{{ l.text }}
                </div>
                <div v-if="!convLines.length" class="muted" style="font-size:12px">无聊天记录</div>
              </div>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head">命中项标注（正 {{ posRows.filter(r => r.checked).length }} / 负 {{ negRows.filter(r => r.checked).length }}）</div>
          <div class="card-body" style="display:grid;gap:16px">
            <div>
              <div class="lbl">加分项</div>
              <div v-for="r in posRows" :key="r.rule_id" class="row">
                <label class="ck">
                  <input type="checkbox" v-model="r.checked" :disabled="current.case.frozen && !current.case.disputed" />
                  <span>{{ r.dimension }}</span>
                  <span v-if="r.fromSystem" class="tag" style="margin-left:6px">系统 {{ r.systemScore > 0 ? '+' : '' }}{{ r.systemScore }}</span>
                  <span v-if="r.hint" class="muted" style="font-size:11px;margin-left:6px">{{ r.hint.slice(0, 16) }}</span>
                </label>
                <select v-model.number="r.score" class="select" style="width:110px" :disabled="!r.checked">
                  <option v-for="o in r.options" :key="o.score" :value="o.score">{{ o.score > 0 ? '+' : '' }}{{ o.score }}</option>
                </select>
              </div>
            </div>
            <div>
              <div class="lbl">扣分项</div>
              <div v-for="r in negRows" :key="r.rule_id" class="row">
                <label class="ck">
                  <input type="checkbox" v-model="r.checked" :disabled="current.case.frozen && !current.case.disputed" />
                  <span>{{ r.dimension }}</span>
                  <span v-if="r.engine === 'rule'" class="tag orange" style="margin-left:6px">引擎</span>
                  <span v-else-if="r.fromSystem" class="tag" style="margin-left:6px">系统 {{ r.systemScore }}</span>
                </label>
                <select v-model.number="r.score" class="select" style="width:110px" :disabled="!r.checked">
                  <option v-for="o in r.options" :key="o.score" :value="o.score">{{ o.score }}</option>
                </select>
              </div>
            </div>
            <div>
              <label class="lbl">备注 / 争议说明</label>
              <textarea v-model="note" class="input" rows="2" placeholder="如：客户已电话确认本月不报，故 negative_refuse 成立"></textarea>
            </div>
            <div>
              <label class="lbl">标注人</label>
              <input v-model="annotator" class="input" style="max-width:220px" />
            </div>
            <div class="alert" :class="expectedScore >= 0 ? 'ok' : 'warn'">
              由规则引擎算得：<b>{{ expectedScore }}</b> 分 · 等级 <b>{{ expectedGrade }}</b>
              <span v-if="current.case.baseline_raw_score !== null" class="muted" style="margin-left:10px">
                （系统基线：{{ current.case.baseline_raw_score }} 分 · {{ current.case.baseline_grade }} 级
                <span v-if="current.case.baseline_rule_version">/ 规则 {{ current.case.baseline_rule_version }}</span>）
              </span>
            </div>
            <div style="display:flex;gap:10px;flex-wrap:wrap">
              <button class="btn primary" :disabled="busy" @click="save(false)"><i v-if="busy" class="spin"></i>保存标注</button>
              <button class="btn" :disabled="busy" @click="save(true)">保存并冻结</button>
              <button class="btn" :disabled="busy" @click="toggleFreeze">{{ current.case.frozen ? '解冻' : '冻结' }}</button>
            </div>
            <div v-if="current.case.annotator" class="muted" style="font-size:12px">
              最近标注：{{ current.case.annotator }} · {{ fmtRelative(Date.parse(current.case.updated_at) / 1000) }}
            </div>
          </div>
        </div>
      </div>

      <div v-else class="card empty" style="min-height:320px">
        <div>
          <div class="big">🎯</div>
          左侧选择一条样本开始标注<br />
          <span class="muted" style="font-size:12px">
            标完 30 条后点「跑评估」，就能得到命中项 F1 / 等级一致率 / 误报率
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.layout { display: grid; grid-template-columns: 420px 1fr; gap: 16px; align-items: start; }
@media (max-width: 1200px) { .layout { grid-template-columns: 1fr; } }
.lbl { display: block; font-size: 12px; color: var(--text-2); margin-bottom: 6px; font-weight: 500; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.row-inline { display: flex; gap: 8px; align-items: center; }
.tabs { display: flex; gap: 6px; flex-wrap: wrap; }
.tabs button {
  padding: 6px 12px; border-radius: 8px; border: 1px solid var(--border); background: var(--panel);
  font-size: 12.5px; cursor: pointer; font-family: inherit; color: var(--text-2); display: inline-flex; gap: 6px; align-items: center;
}
.tabs button.on { border-color: var(--brand); background: var(--brand-weak); color: var(--brand); font-weight: 600; }
.cnt { background: var(--panel-2); border-radius: 99px; padding: 0 6px; font-size: 11px; border: 1px solid var(--border); }
tr.sel { background: var(--brand-weak) !important; }
.pager { display: flex; align-items: center; gap: 12px; justify-content: center; }
.row { display: flex; align-items: center; gap: 10px; justify-content: space-between; padding: 5px 0; }
.ck { display: flex; align-items: center; gap: 7px; font-size: 13px; cursor: pointer; flex-wrap: wrap; }
.conv { max-height: 380px; overflow: auto; background: var(--panel-2); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px; display: grid; gap: 8px; }
.bubble { font-size: 12.5px; line-height: 1.6; max-width: 84%; padding: 7px 10px; border-radius: 10px;
  background: var(--panel); border: 1px solid var(--border); word-break: break-all; }
.bubble.right { margin-left: auto; background: var(--brand-weak); border-color: #d3e1ff; }
.bubble .who { display: block; font-size: 10.5px; color: var(--text-3); margin-bottom: 2px; }
.metrics { display: flex; gap: 18px; flex-wrap: wrap; align-items: flex-end; }
.metrics .m { text-align: center; min-width: 74px; }
.metrics .mv { font-size: 21px; font-weight: 700; line-height: 1.15; }
.metrics .mv.bad { color: var(--danger); }
.metrics .mv .sub { font-size: 12px; font-weight: 500; color: var(--text-3); }
.metrics .ml { font-size: 11.5px; color: var(--text-3); margin-top: 2px; }
.metrics .sep { width: 1px; align-self: stretch; background: var(--border); }
</style>
