<script setup>
import { ref, computed, onMounted } from 'vue'
// ⚠️ `fmtTime` 必须在这里引入：模板里「已完成于 …」一直在用它（`fmtTime(...)`），
//    但改造前没进 import —— 那条分支一渲染就是 `fmtTime is not a function`，
//    也就是"复核完成的记录反而打不开详情"。机械化扫描（用到的导出函数 vs import）
//    才把它揪出来：**只有 done 状态的记录会走到那一行**，平时看不见。
import { api, fmtTime, fmtRelative, gradeTag, toEpoch } from '../api'
import ChatBubbles from '../components/ChatBubbles.vue'
import Pager from '../components/Pager.vue'
import { useAsyncAction } from '../composables/useAsyncAction'
import { usePolling } from '../composables/usePolling'
import { useLoadFailures } from '../composables/loadFailures'
import PartialLoadBanner from '../components/PartialLoadBanner.vue'

// 局部加载失败统一收口（见 composables/loadFailures.js）
const { failures, note: noteFail, clear: clearFail } = useLoadFailures()

const TABS = [
  { key: '', label: '全部' },
  { key: 'pending', label: '未打分' },
  { key: 'done', label: '已完成' },
  { key: 'ignored', label: '已忽略' },
]

const stats = ref({ total: 0, pending: 0, done: 0, ignored: 0 })
const tab = ref('pending')
const staffs = ref([])
const filters = ref({ staff_id: '', q: '', date_from: '', date_to: '', page: 1, page_size: 20 })
const list = ref({ items: [], total: 0 })
const current = ref(null)          // { review, assessment, conversation, status_cn }
const options = ref([])            // 可选扣分项
const rows = ref([])               // 人工打分行
const note = ref('')
const reviewer = ref(localStorage.getItem('reviewer') || '工作人员')
// busy / error / msg 由 composable 提供（模板里的 `busy` 用法不变）
const { busy, error, run } = useAsyncAction()
async function refresh() {
  try {
    const [s, l] = await Promise.all([
      api.reviewStats(),
      api.reviewList({ status: tab.value, ...filters.value }),
    ])
    stats.value = s; list.value = { items: l.items || [], total: l.total || 0 }
  } catch (e) { error.value = '后端未就绪：' + e.message }
}

async function loadStaffs() {
  try {
    staffs.value = (await api.staffs({ only_with_session: 1, limit: 800 })).items || []
    clearFail('staffs')
  } catch (e) { noteFail('staffs', '员工列表', e) }
}
async function loadOptions() {
  try {
    options.value = (await api.reviewOptions()).items || []
    clearFail('options')
  } catch (e) { noteFail('options', '复核规则选项', e) }
}

// ---------------- 批量改状态 ----------------
// 后端一直有 `/review/batch-status`（批量标忽略 / 退回），前端没有入口 ——
// 339 条积压时只能一条一条点，等于逼着人放弃复核。
const picked = ref([])
const auditLog = ref([])

function togglePick(r, ev) {
  ev.stopPropagation()              // 行点击是"打开详情"，勾选不该顺带打开
  const i = picked.value.indexOf(r.id)
  if (i >= 0) picked.value.splice(i, 1)
  else picked.value.push(r.id)
}

/** 当前页全选 / 取消全选（只作用于**本页**，避免误伤没看见的记录）。 */
function togglePickAll(ev) {
  const ids = list.value.items.map(r => r.id)
  picked.value = ids.every(i => picked.value.includes(i)) ? [] : ids
}

async function batchStatus(status) {
  const ids = picked.value.slice()
  if (!ids.length) { error.value = '先勾选要处理的记录（表头可全选本页）'; return }
  const cn = ST_CN[status] || status
  if (!window.confirm(`把勾选的 ${ids.length} 条记录批量标记为「${cn}」？\n\n`
    + '· 只改复核队列的状态，不动客户分数、不动评估结论；\n'
    + '· 批量动作会写审计（谁、多少条、改成什么）。\n\n确定吗？')) return
  await run(async () => {
    const r = await api.reviewBatchStatus({ review_ids: ids, status })
    if (!r.ok) { error.value = r.reason || '批量操作失败'; return }
    picked.value = []
    await Promise.all([refresh(), loadAudit()])
  })
}

/** 最近业务动作审计（谁改了什么）—— 原来只有 `/review/audit` 端点，页面看不到。 */
async function loadAudit() {
  try { auditLog.value = (await api.reviewAudit(20)).items || []; clearFail('audit') }
  catch (e) { noteFail('audit', '操作审计', e) }
}

function switchTab(k) { tab.value = k; filters.value.page = 1; refresh() }
function search() { filters.value.page = 1; refresh() }
function goto(p) { filters.value.page = p; refresh() }

function buildRows(aiItems) {
  const aiMap = {}
  for (const it of (aiItems || [])) aiMap[it.rule_id] = it
  rows.value = options.value.map(o => {
    const opt = (o.options || [])[0] || { score: 0, criteria: '' }
    const ai = aiMap[o.rule_id]
    return {
      rule_id: o.rule_id, dimension: o.dimension, score: ai?.score ?? opt.score,
      criteria: opt.criteria, checked: !!ai, fromAI: !!ai, reason: ai?.reason || '',
    }
  })
  note.value = ''
}

async function open(r) {
  try {
    const d = await api.reviewDetail(r.id)
    current.value = d
    buildRows(d.review?.ai_negative_items)
  } catch (e) { error.value = e.message }
}

const humanTotal = computed(() => rows.value.filter(r => r.checked).reduce((s, r) => s + (r.score || 0), 0))

async function submit() {
  if (!current.value) return
  await run(async () => {
    localStorage.setItem('reviewer', reviewer.value)
    const items = rows.value.filter(r => r.checked)
      .map(r => ({ rule_id: r.rule_id, score: r.score, reason: r.reason || r.dimension }))
    const res = await api.reviewSubmit({
      review_id: current.value.review.id, items, note: note.value, reviewer: reviewer.value,
    })
    if (!res.ok) { error.value = res.reason || '提交失败'; return }
    await refresh(); await open({ id: current.value.review.id })
  })
}

async function setStatus(st) {
  if (!current.value) return
  await run(async () => {
    localStorage.setItem('reviewer', reviewer.value)
    const res = await api.reviewSetStatus({
      review_id: current.value.review.id, status: st, note: note.value, reviewer: reviewer.value,
    })
    if (!res.ok) { error.value = res.reason || '操作失败'; return }
    await refresh(); await open({ id: current.value.review.id })
  })
}

const ST_TAG = { pending: 'orange', done: 'green', ignored: '' }
const ST_CN = { pending: '未打分', done: '已完成', ignored: '已忽略' }

function reset() { current.value = null; error.value = '' }

onMounted(() => { loadStaffs(); loadOptions(); loadAudit() })
usePolling(refresh, 5000)
</script>

<template>
  <div class="page">
    <!-- 局部加载失败 → 明说 + 可重试（见 components/PartialLoadBanner.vue） -->
    <PartialLoadBanner :failures="failures" @retry="refresh" />
    <div class="split-2col">
      <!-- 左：待办列表 -->
      <div class="card">
        <div class="card-head">负分复核</div>
        <div class="card-body" style="display:grid;gap:12px">
          <div class="tabs">
            <button v-for="t in TABS" :key="t.key" :class="{ on: tab === t.key }" @click="switchTab(t.key)">
              {{ t.label }}
              <span class="cnt">{{ t.key === '' ? stats.total : (stats[t.key] || 0) }}</span>
            </button>
          </div>
          <div style="display:grid;gap:8px">
            <select v-model="filters.staff_id" class="select" @change="search">
              <option value="">全部员工</option>
              <option v-for="s in staffs" :key="s.staff_id" :value="s.staff_id">{{ s.name }}</option>
            </select>
            <input v-model="filters.q" class="input" placeholder="客户 / 员工名称" @keyup.enter="search" />
            <div class="grid-2">
              <input v-model="filters.date_from" class="input" type="date" @change="search" />
              <input v-model="filters.date_to" class="input" type="date" @change="search" />
            </div>
            <button class="btn" @click="search">查询</button>
          </div>
          <div style="overflow:auto;max-height:calc(100vh - 300px)">
            <table class="tbl">
              <thead>
                <tr>
                  <th style="width:28px">
                    <input type="checkbox" :checked="list.items.length && list.items.every(r => picked.includes(r.id))"
                           title="全选/取消本页" @click="togglePickAll($event)" />
                  </th>
                  <th>客户</th><th>AI扣分</th><th>状态</th><th>时间</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in list.items" :key="r.id" :class="{ sel: current?.review?.id === r.id }"
                    style="cursor:pointer" @click="open(r)">
                  <td>
                    <input type="checkbox" :checked="picked.includes(r.id)" @click="togglePick(r, $event)" />
                  </td>
                  <td>
                    <div>{{ r.target_name || r.target_id }}</div>
                    <div class="muted" style="font-size:11.5px">{{ (r.staff_name || '').slice(0, 14) }}</div>
                  </td>
                  <td class="mono" style="color:var(--danger)">{{ r.ai_negative_total }}</td>
                  <td><span class="tag" :class="ST_TAG[r.status]">{{ ST_CN[r.status] }}</span></td>
                  <td class="muted" style="font-size:12px">{{ fmtRelative(r.last_msg_time) }}</td>
                </tr>
                <tr v-if="!list.items.length"><td colspan="5" class="muted" style="text-align:center;padding:24px">暂无待复核记录</td></tr>
              </tbody>
            </table>
          </div>
          <!-- 批量改状态：勾选若干条后一次处理（后端 `/review/batch-status`）。
               339 条积压时逐条点会把复核这件事本身劝退。 -->
          <div v-if="picked.length" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <span class="muted" style="font-size:12.5px">已选 {{ picked.length }} 条</span>
            <button class="btn sm" :disabled="busy" @click="batchStatus('ignored')">批量标记已忽略</button>
            <button class="btn sm" :disabled="busy" @click="batchStatus('pending')">批量退回未打分</button>
            <button class="btn sm ghost" :disabled="busy" @click="picked = []">取消选择</button>
          </div>
          <Pager :page="filters.page" :page-size="filters.page_size"
                 :total="list.total" @go="goto" />
        </div>
      </div>

      <!-- 右：详情 + 打分 -->
      <div v-if="current" style="display:grid;gap:16px">
        <div class="card">
          <div class="card-head">
            复核详情 · {{ current.review.target_name }}
            <span class="tag" :class="ST_TAG[current.review.status]" style="margin-left:8px">{{ ST_CN[current.review.status] }}</span>
            <span v-if="current.assessment" class="tag" :class="gradeTag(current.assessment.grade)" style="margin-left:6px">
              {{ current.assessment.grade }} · {{ current.assessment.raw_score }} 分
            </span>
            <button class="btn sm" style="margin-left:auto" @click="reset">收起</button>
          </div>
          <div class="card-body" style="display:grid;gap:14px">
            <div class="alert warn">
              AI/规则引擎识别到扣分合计 <b>{{ current.review.ai_negative_total }}</b> 分，
              请核对下方聊天记录后确认是否成立。
              <span v-if="current.review.reviewer" class="muted">最近处理人：{{ current.review.reviewer }}</span>
            </div>

            <div>
              <div class="lbl">AI 扣分依据</div>
              <div v-for="(i, k) in (current.review.ai_negative_items || [])" :key="k" class="item">
                <span class="tag red">{{ i.score }}</span>
                <div><b>{{ i.dimension }}</b>
                  <div class="muted" style="font-size:12px;white-space:pre-wrap">{{ i.reason }}</div>
                <!-- 证据行 = 后端回填的整行文本（`#012 [时间] 角色: 内容`），
                     交给同一个组件渲染：去编号噪音、左右分栏、多行保留 -->
                <ChatBubbles compact :text="(i.evidence || []).join('\n')" />
                </div>
              </div>
            </div>

            <div>
              <div class="lbl">聊天记录（近期）</div>
              <div class="conv">
                <!-- 气泡渲染统一走公共组件（AGENTS.md: Reuse Before Rewrite） -->
                <ChatBubbles :messages="current.messages || []" :text="current.conversation"
                             empty="无聊天记录" />
              </div>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head">人工打分</div>
          <div class="card-body" style="display:grid;gap:14px">
            <div>
              <label class="lbl">确认成立的扣分项（勾选即计入，分值可微调）</label>
              <div v-for="r in rows" :key="r.rule_id" class="kv-row">
                <label class="ck">
                  <input type="checkbox" v-model="r.checked" />
                  <span>{{ r.dimension }}</span>
                  <span v-if="r.fromAI" class="tag red" style="margin-left:6px">AI</span>
                </label>
                <input v-model.number="r.score" class="input" type="number" style="width:80px" :disabled="!r.checked" />
              </div>
            </div>
            <div>
              <label class="lbl">备注</label>
              <textarea v-model="note" class="input" rows="2" placeholder="如：已电话确认客户确实暂不考虑"></textarea>
            </div>
            <div>
              <label class="lbl">处理人</label>
              <input v-model="reviewer" class="input" style="max-width:220px" />
            </div>

            <div class="alert" :class="humanTotal < 0 ? 'warn' : 'ok'">
              人工确认扣分合计：<b>{{ humanTotal }}</b>
              （AI 判定 {{ current.review.ai_negative_total }}，一致性：
              {{ humanTotal === current.review.ai_negative_total ? '一致' : '不一致' }}）
            </div>

            <div style="display:flex;gap:10px;flex-wrap:wrap">
              <button class="btn primary" :disabled="busy" @click="submit">
                <i v-if="busy" class="spin"></i>提交打分（标记已完成）
              </button>
              <button class="btn" :disabled="busy" @click="setStatus('ignored')">标记已忽略</button>
              <button class="btn" :disabled="busy" @click="setStatus('pending')">退回未打分</button>
            </div>
            <div v-if="error" class="alert err">{{ error }}</div>
            <div v-if="current.review.status === 'done'" class="alert ok" style="font-size:12px">
              已完成于 {{ fmtTime(toEpoch(current.review.reviewed_at)) }}
              · 人工扣分 {{ current.review.human_negative_total }}
              <span v-if="current.review.human_note">· 备注：{{ current.review.human_note }}</span>
            </div>
          </div>
        </div>
      </div>

      <div v-else class="card empty" style="min-height:320px">
        <div>
          <div class="big">✅</div>
          左侧选择一条记录开始复核<br />
          <span class="muted" style="font-size:12px">确认扣分项后提交，即可完成人工打分</span>
        </div>
      </div>
    </div>

    <!-- 最近业务动作审计（谁在什么时候改了什么）—— 后端 `/review/audit` 一直有，
         页面上却看不到：复核是"人对 AI 的否决"，这类动作比别的更需要可追溯。 -->
    <div class="card" style="margin-top:16px">
      <div class="card-head">
        最近操作审计
        <span class="muted" style="margin-left:auto;font-size:11.5px">最近 20 条业务动作</span>
      </div>
      <div class="card-body" style="max-height:220px;overflow:auto">
        <table class="tbl">
          <thead><tr><th>时间</th><th>操作人</th><th>动作</th><th>对象</th></tr></thead>
          <tbody>
            <tr v-for="a in auditLog" :key="a.id">
              <td class="muted" style="font-size:11.5px">{{ fmtTime(toEpoch(a.created_at)) }}</td>
              <td>{{ a.actor }}</td>
              <td class="mono" style="font-size:11.5px">{{ a.action }}</td>
              <td class="muted" style="font-size:11.5px">{{ a.target }}</td>
            </tr>
            <tr v-if="!auditLog.length">
              <td colspan="4" class="muted" style="text-align:center;padding:16px">暂无审计记录</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<style scoped>
tr.sel { background: var(--brand-weak) !important; }
.ck { display: flex; align-items: center; gap: 7px; font-size: 13px; cursor: pointer; }
.conv { max-height: 320px; overflow: auto; background: var(--panel-2); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px; display: grid; gap: 8px; }
/* 气泡 / 引用块 / 媒体样式已移入 components/ChatBubbles.vue
   （AGENTS.md: Reuse Before Rewrite —— 这里不再重复定义） */
</style>
