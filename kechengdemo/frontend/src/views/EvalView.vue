<script setup>
import { ref, computed, onMounted } from 'vue'
import { api, fmtRelative, gradeTag } from '../api'
import ChatBubbles from '../components/ChatBubbles.vue'
import Pager from '../components/Pager.vue'
import { useAsyncAction } from '../composables/useAsyncAction'
import { usePolling } from '../composables/usePolling'

// 评估集：回答「判得准不准」。标注口径 = 只标「命中了哪些 rule_id + 档位」，不标总分。
const TABS = [
  { key: '', label: '全部' },
  { key: 'unlabeled', label: '待标注' },
  { key: 'prefilled', label: 'AI 草稿' },
  { key: 'confirmed', label: '已确认' },
  { key: 'frozen', label: '已锁定' },
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
// busy / error / msg 由 composable 提供（模板里的 `busy` / `error` / `msg` 用法不变）
const { busy, error, msg, run } = useAsyncAction()
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
  markClean()          // 刚载入 / 刚保存完 → 视为「干净」
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
// 会话消息数超过本次展示上限 → 更早的消息没显示。必须显式告知：否则「引用回复」
// 里引用的原话看不到出处，会被误判成归属错误（实测被反馈过）。
const truncated = computed(() =>
  (current.value?.case?.msg_count || 0) > (current.value?.msg_limit || 0))

// ---------------- 未保存改动检测 + 状态机 ----------------
// 坑：「锁定」只翻 frozen 标志、**不落标注**。用户改完勾选直接点锁定，改动静默丢失、
// 锁上的是旧内容。所以先能判断"脏没脏"，再据此**禁用锁定**。
const _snap = ref('')

/** 把「会被保存的东西」压成签名：勾选项 + 分值 + 备注。 */
function signature() {
  const parts = rows.value.filter((r) => r.checked)
    .map((r) => [r.rule_id, r.score]).sort()
  parts.push(['__note__', note.value || ''])
  return JSON.stringify(parts)
}

const dirty = computed(() => signature() !== _snap.value)
function markClean() { _snap.value = signature() }

/** 当前样本状态：草稿 / 已确认 / 有争议 / 已锁定。四个标志独立，
 *  但用户只需要看懂「我在哪一档、下一步点哪个」。 */
const caseState = computed(() => {
  const c = current.value?.case
  if (!c) return null
  if (c.frozen) {
    return { key: 'frozen', text: '已锁定', cls: 'st-frozen',
             hint: '金标准已定稿，除仲裁外不可修改。要改请先解锁。' }
  }
  if (c.disputed) {
    return { key: 'disputed', text: '有争议', cls: 'st-disputed',
             hint: '已标记「AI 判定有误」。请修正命中项后点「保存修改」。' }
  }
  if (c.prefilled) {
    return { key: 'draft', text: 'AI 草稿', cls: 'st-draft',
             hint: '尚未人工确认，不计入指标 —— 点「判对」或「保存修改」后才算数。' }
  }
  return { key: 'confirmed', text: '已确认', cls: 'st-ok',
           hint: '已计入指标。核对无误后可「锁定」防止以后被改动。' }
})

/** 「我只判对错」的入口：
 *  判对 → save()：把 AI 草稿落成金标准（prefilled 转 false → 才计入指标）。
 *  判错 → dispute()：**只打争议标记、不动标注内容**，该条继续被排除在指标外。
 *        不直接 save 一个"错标注"：错误内容一旦以 prefilled=false 落库就会被当成
 *        金标准计入指标，比不标更糟（指标被污染且看不出来）。 */
async function judge(ok) {
  if (!current.value || busy.value) return
  if (ok) return save(false)
  await run(async () => {
    const res = await api.evalDispute({ case_id: current.value.case.case_id, note: note.value })
    msg.value = res.ok
      ? '已标记「AI 判定有误」——标注保持草稿态，不计入指标；改正命中项后再保存'
      : (res.reason || '标记失败')
    await refresh(); await open({ case_id: current.value.case.case_id })
  })
}
const posRows = computed(() => rows.value.filter(r => r.polarity === 'positive'))
const negRows = computed(() => rows.value.filter(r => r.polarity === 'negative'))

async function save(alsoFreeze = false) {
  if (!current.value) return
  await run(async () => {
    localStorage.setItem('annotator', annotator.value)
    const items = rows.value.filter(r => r.checked)
      .map(r => ({ rule_id: r.rule_id, score: r.score, reason: r.reason || r.dimension }))
    const res = await api.evalSave({
      case_id: current.value.case.case_id, items, note: note.value, annotator: annotator.value,
    })
    if (!res.ok) { error.value = res.reason || '保存失败'; return }
    if (alsoFreeze) await api.evalFreeze({ case_id: current.value.case.case_id, frozen: true, annotator: annotator.value })
    msg.value = `已保存：${res.expected_raw_score} 分 · ${res.expected_grade} 级`
      + (res.invalid_items?.length ? `（${res.invalid_items.length} 项被丢弃）` : '')
    await refresh(); await open({ case_id: current.value.case.case_id })
  })
}

async function toggleFreeze() {
  if (!current.value) return
  // 防御性：即使按钮被绕过，也不允许「带着未保存改动去锁定」——
  // 那会把旧内容锁住、用户的改动静默消失（最容易被踩的坑）。
  if (!current.value.case.frozen && dirty.value) {
    error.value = '有未保存的改动 —— 请先点「保存修改」，否则锁定的是旧内容'
    return
  }
  await run(async () => {
    const res = await api.evalFreeze({ case_id: current.value.case.case_id, frozen: !current.value.case.frozen })
    await refresh(); await open({ case_id: current.value.case.case_id })
    msg.value = res.frozen ? '已冻结（除仲裁外不可修改）' : '已解冻'
  })
}

async function doSample() {
  await run(async () => {
    const res = await api.evalSample({ kind: kind.value, limit: sampleN.value, min_messages: 5 })
    msg.value = `抽样完成：候选 ${res.candidates} → 新增 ${res.created} 条`
      + (res.distribution ? `｜分布 ${Object.keys(res.distribution).length} 层` : '')
    const dist = res.distribution || {}
    msg.value += '｜' + Object.entries(dist).map(([k, v]) => `${k}×${v}`).join(' ')
    await refresh()
  })
}

async function doRun() {
  await run(async () => {
    const res = await api.evalStart({ kind: kind.value })
    if (!res.ok) { error.value = res.reason || '启动失败'; return }
    msg.value = '评估已在后台开跑（每条一次模型调用，请稍候）'
  })
}

async function setBaseline() {
  await run(async () => {
    const res = await api.evalSetBaseline({})
    msg.value = res.ok ? '已把最近一次跑分设为基线' : (res.reason || '失败')
    await refresh()
  })
}

function reset() { current.value = null; error.value = ''; msg.value = '' }

// AI 预标注：把 AI 当时的判定填成草稿 → 人工从「改草稿」开始，而不是「从零列清单」
async function doPrefill() {
  await run(async () => {
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
  })
}

const pct = (v) => (v === null || v === undefined) ? '-' : (v * 100).toFixed(1) + '%'
const fmtMetric = (k, v) => (v === null || v === undefined) ? '-' : (k === 'rule_f1' || k === 'precision' || k === 'recall' || k === 'grade_acc' || k === 'fp_case_rate' ? pct(v) : v)

async function loadMetaLoop() {
  try { metaLoop.value = await api.evalMetaLoop() } catch (e) { /* 未就绪时忽略 */ }
}

async function runBackflow() {
  await run(async () => {
    const r = await api.evalBackflow({ limit: 100 })
    msg.value = `回流完成：新增 ${r.created} 条，跳过 ${r.skipped} 条（人工判断已进入回归集）`
    loadMetaLoop()
  })
}

onMounted(() => { loadRules(); loadMetaLoop() })
usePolling(refresh, 8000)
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

    <div class="split-2col">
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
            <div class="grid-2">
              <select v-model="kind" class="select" @change="switchKind">
                <option v-for="k in KINDS" :key="k.key" :value="k.key">{{ k.label }}</option>
              </select>
              <div class="inline-row">
                <input v-model.number="sampleN" class="input" type="number" min="1" max="200" style="width:70px" />
                <button class="btn sm" :disabled="busy" @click="doSample">抽样</button>
                <button class="btn sm" :disabled="busy" title="把 AI 当时的判定填成草稿，人工只复核" @click="doPrefill">
                  AI 预标注
                </button>
              </div>
            </div>
            <input v-model="filters.q" class="input" placeholder="客户 / 员工 / 会话 id" @keyup.enter="search" />
            <div class="inline-row">
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
                    <span v-if="r.expected_grade" class="tag" :class="gradeTag(r.expected_grade)">{{ r.expected_grade }}</span>
                    <span v-else class="muted" style="font-size:12px">—</span>
                    <span v-if="r.prefilled" class="tag orange" style="margin-left:4px"
                          title="AI 预标注草稿 —— 复核并保存后才算「已确认」">预</span>
                  </td>
                  <td>
                    <span v-if="r.baseline_grade" class="tag" :class="gradeTag(r.baseline_grade)">{{ r.baseline_grade }}</span>
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
        <Pager :page="filters.page" :page-size="filters.page_size"
               :total="list.total" @go="goto" />
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
              <div v-if="truncated" class="trunc-note">
                会话共 <b>{{ current.case.msg_count }}</b> 条，这里只显示最近
                <b>{{ current.msg_limit }}</b> 条 —— 更早的
                {{ current.case.msg_count - current.msg_limit }} 条未显示。
                <span class="muted">（所以「引用回复」里引用的原话可能找不到原消息，属正常）</span>
                <button class="btn sm" style="margin-left:8px" :disabled="busy"
                        @click="open({ case_id: current.case.case_id }, 5000)">加载全部</button>
              </div>
              <div class="conv">
                <!-- 气泡渲染统一走公共组件（AGENTS.md: Reuse Before Rewrite） -->
                <ChatBubbles :messages="current.messages || []" :text="current.conversation"
                             empty="无聊天记录" />
              </div>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head">命中项标注（正 {{ posRows.filter(r => r.checked).length }} / 负 {{ negRows.filter(r => r.checked).length }}）</div>
          <div class="card-body" style="display:grid;gap:16px">
            <div class="judge-bar" :class="caseState.cls">
              <span class="st-chip">{{ caseState.text }}</span>
              <span class="st-hint">{{ caseState.hint }}</span>
              <span v-if="dirty" class="st-dirty">⚠️ 有未保存的改动</span>
            </div>
            <div>
              <div class="lbl">加分项</div>
              <div v-for="r in posRows" :key="r.rule_id" class="kv-row">
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
              <div v-for="r in negRows" :key="r.rule_id" class="kv-row">
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
              <!-- 按钮按**状态**出现，而不是永远摆同一排 —— 少一半的犹豫 -->
              <template v-if="!current.case.frozen">
                <button class="btn primary" :disabled="busy" @click="judge(true)"
                        title="把 AI 的这份判定直接存为金标准（从此计入指标）">✓ 判对</button>
                <button class="btn" :disabled="busy" @click="judge(false)"
                        title="只打争议标记，不动标注内容；仍不计入指标，等修正后再保存">✗ 判错</button>
                <span class="vsep"></span>
                <button class="btn sm" :disabled="busy || !dirty" @click="save(false)"
                        title="保存你改动过的命中项">{{ dirty ? '保存修改 ●' : '保存修改' }}</button>
                <button class="btn sm" :disabled="busy || dirty" @click="save(true)"
                        :title="dirty ? '有未保存的改动 —— 先点「保存修改」才能锁定'
                                      : '存为金标准并锁定：此后除仲裁外不可修改'">🔒 锁定</button>
              </template>
              <template v-else>
                <button class="btn sm" :disabled="busy" @click="toggleFreeze"
                        title="解锁后才能修改标注">🔓 解锁后修改</button>
              </template>
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
tr.sel { background: var(--brand-weak) !important; }
.ck { display: flex; align-items: center; gap: 7px; font-size: 13px; cursor: pointer; flex-wrap: wrap; }
.conv { max-height: 380px; overflow: auto; background: var(--panel-2); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px; display: grid; gap: 8px; }
/* 气泡样式已移入 components/ChatBubbles.vue（AGENTS.md: Reuse Before Rewrite）——
   这里不再重复定义，避免改一处忘另一处。 */
.trunc-note { font-size: 12px; line-height: 1.7; color: #8a5a00;
              background: #fff8e8; border: 1px solid #f0d79a;
              border-radius: 7px; padding: 7px 10px; margin-bottom: 8px; }
.judge-bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
             border-radius: 8px; padding: 9px 12px;
             background: var(--brand-weak); border: 1px solid #d3e1ff; }
.judge-bar.st-draft { background: #fff8e8; border-color: #f0d79a; }
.judge-bar.st-disputed { background: #fff1ec; border-color: #f3c3b3; }
.judge-bar.st-ok { background: #eef9f1; border-color: #b7e0c4; }
.judge-bar.st-frozen { background: #eef2f7; border-color: #c8d5e4; }
.st-chip { font-size: 12px; font-weight: 600; padding: 1px 8px; border-radius: 10px;
           background: rgba(255, 255, 255, .75); border: 1px solid rgba(0, 0, 0, .08); }
.st-hint { font-size: 12.5px; color: var(--text-2); }
.st-dirty { margin-left: auto; font-size: 12px; color: #b45309; font-weight: 600; }
.vsep { width: 1px; align-self: stretch; background: var(--border); margin: 0 2px; }
.metrics { display: flex; gap: 18px; flex-wrap: wrap; align-items: flex-end; }
.metrics .m { text-align: center; min-width: 74px; }
.metrics .mv { font-size: 21px; font-weight: 700; line-height: 1.15; }
.metrics .mv.bad { color: var(--danger); }
.metrics .mv .sub { font-size: 12px; font-weight: 500; color: var(--text-3); }
.metrics .ml { font-size: 11.5px; color: var(--text-3); margin-top: 2px; }
.metrics .sep { width: 1px; align-self: stretch; background: var(--border); }
</style>
