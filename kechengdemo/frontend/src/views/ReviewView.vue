<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api, fmtTime, fmtRelative } from '../api'

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
const busy = ref(false)
const error = ref('')
let timer = null

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
  try { staffs.value = (await api.staffs({ only_with_session: 1, limit: 800 })).items || [] } catch (e) {}
}
async function loadOptions() {
  try { options.value = (await api.reviewOptions()).items || [] } catch (e) {}
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
const convLines = computed(() => {
  const txt = current.value?.conversation || ''
  return txt.split('\n').filter(Boolean).map(l => {
    const i = l.indexOf(':')
    return { who: l.slice(0, i), text: l.slice(i + 1).trim(), side: l.startsWith('销售') ? 'right' : 'left' }
  })
})

async function submit() {
  if (!current.value) return
  busy.value = true; error.value = ''
  try {
    localStorage.setItem('reviewer', reviewer.value)
    const items = rows.value.filter(r => r.checked)
      .map(r => ({ rule_id: r.rule_id, score: r.score, reason: r.reason || r.dimension }))
    const res = await api.reviewSubmit({
      review_id: current.value.review.id, items, note: note.value, reviewer: reviewer.value,
    })
    if (!res.ok) { error.value = res.reason || '提交失败'; busy.value = false; return }
    await refresh(); await open({ id: current.value.review.id })
  } catch (e) { error.value = e.message }
  busy.value = false
}

async function setStatus(st) {
  if (!current.value) return
  busy.value = true; error.value = ''
  try {
    localStorage.setItem('reviewer', reviewer.value)
    const res = await api.reviewSetStatus({
      review_id: current.value.review.id, status: st, note: note.value, reviewer: reviewer.value,
    })
    if (!res.ok) { error.value = res.reason || '操作失败'; busy.value = false; return }
    await refresh(); await open({ id: current.value.review.id })
  } catch (e) { error.value = e.message }
  busy.value = false
}

const ST_TAG = { pending: 'orange', done: 'green', ignored: '' }
const ST_CN = { pending: '未打分', done: '已完成', ignored: '已忽略' }
const GRADE_TAG = { S: 'red', A: 'orange', B: 'blue', C: 'green', D: '' }

function reset() { current.value = null; error.value = '' }

onMounted(() => { refresh(); loadStaffs(); loadOptions(); timer = setInterval(refresh, 5000) })
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="page">
    <div class="layout">
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
            <div class="two">
              <input v-model="filters.date_from" class="input" type="date" @change="search" />
              <input v-model="filters.date_to" class="input" type="date" @change="search" />
            </div>
            <button class="btn" @click="search">查询</button>
          </div>
          <div style="overflow:auto;max-height:calc(100vh - 300px)">
            <table class="tbl">
              <thead><tr><th>客户</th><th>AI扣分</th><th>状态</th><th>时间</th></tr></thead>
              <tbody>
                <tr v-for="r in list.items" :key="r.id" :class="{ sel: current?.review?.id === r.id }"
                    style="cursor:pointer" @click="open(r)">
                  <td>
                    <div>{{ r.target_name || r.target_id }}</div>
                    <div class="muted" style="font-size:11.5px">{{ (r.staff_name || '').slice(0, 14) }}</div>
                  </td>
                  <td class="mono" style="color:var(--danger)">{{ r.ai_negative_total }}</td>
                  <td><span class="tag" :class="ST_TAG[r.status]">{{ ST_CN[r.status] }}</span></td>
                  <td class="muted" style="font-size:12px">{{ fmtRelative(r.last_msg_time) }}</td>
                </tr>
                <tr v-if="!list.items.length"><td colspan="4" class="muted" style="text-align:center;padding:24px">暂无待复核记录</td></tr>
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

      <!-- 右：详情 + 打分 -->
      <div v-if="current" style="display:grid;gap:16px">
        <div class="card">
          <div class="card-head">
            复核详情 · {{ current.review.target_name }}
            <span class="tag" :class="ST_TAG[current.review.status]" style="margin-left:8px">{{ ST_CN[current.review.status] }}</span>
            <span v-if="current.assessment" class="tag" :class="GRADE_TAG[current.assessment.grade]" style="margin-left:6px">
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
                  <div class="muted" style="font-size:12px">{{ i.reason }}</div>
                  <div v-for="(ev, e) in (i.evidence || [])" :key="e" class="ev">{{ ev }}</div>
                </div>
              </div>
            </div>

            <div>
              <div class="lbl">聊天记录（近期）</div>
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
          <div class="card-head">人工打分</div>
          <div class="card-body" style="display:grid;gap:14px">
            <div>
              <label class="lbl">确认成立的扣分项（勾选即计入，分值可微调）</label>
              <div v-for="r in rows" :key="r.rule_id" class="row">
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
              已完成于 {{ fmtTime(Date.parse(current.review.reviewed_at) / 1000) }}
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
  </div>
</template>

<style scoped>
.layout { display: grid; grid-template-columns: 420px 1fr; gap: 16px; align-items: start; }
@media (max-width: 1200px) { .layout { grid-template-columns: 1fr; } }
.lbl { display: block; font-size: 12px; color: var(--text-2); margin-bottom: 6px; font-weight: 500; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.tabs { display: flex; gap: 6px; flex-wrap: wrap; }
.tabs button {
  padding: 6px 12px; border-radius: 8px; border: 1px solid var(--border); background: var(--panel);
  font-size: 12.5px; cursor: pointer; font-family: inherit; color: var(--text-2); display: inline-flex; gap: 6px; align-items: center;
}
.tabs button.on { border-color: var(--brand); background: var(--brand-weak); color: var(--brand); font-weight: 600; }
.cnt { background: var(--panel-2); border-radius: 99px; padding: 0 6px; font-size: 11px; border: 1px solid var(--border); }
.tabs button.on .cnt { background: #fff; border-color: #d3e1ff; }
tr.sel { background: var(--brand-weak) !important; }
.pager { display: flex; align-items: center; gap: 12px; justify-content: center; }
.item { display: flex; gap: 10px; padding: 8px 0; border-bottom: 1px dashed var(--border); }
.item:last-child { border-bottom: none; }
.ev { font-size: 12px; color: #1e4fa8; background: var(--brand-weak); border-radius: 6px; padding: 4px 8px; margin-top: 5px; }
.row { display: flex; align-items: center; gap: 10px; justify-content: space-between; padding: 5px 0; }
.ck { display: flex; align-items: center; gap: 7px; font-size: 13px; cursor: pointer; }
.conv { max-height: 320px; overflow: auto; background: var(--panel-2); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px; display: grid; gap: 8px; }
.bubble { font-size: 12.5px; line-height: 1.6; max-width: 84%; padding: 7px 10px; border-radius: 10px;
  background: var(--panel); border: 1px solid var(--border); word-break: break-all; }
.bubble.right { margin-left: auto; background: var(--brand-weak); border-color: #d3e1ff; }
.bubble .who { display: block; font-size: 10.5px; color: var(--text-3); margin-bottom: 2px; }
</style>
