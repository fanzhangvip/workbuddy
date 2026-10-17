<script setup>
/**
 * 兴趣点分布图 —— 总览页首屏中心的**单卡三视图**（echarts 绘制，注册点见 `src/echarts.js`）。
 *
 * ## 为什么是「树图 + 条形 + 气泡云」三件套（而不是一张词云）
 *
 * 人的感知精度是有层级的：**位置/长度 > 角度 > 面积 > 颜色**（Cleveland & McGill）。
 * 而词云的问题恰恰是"用面积编码数值、读者读不出频次"（data-to-viz 对词云的结论：
 * 适合快速感知最突出的词，但被公认为**不精确**）。所以三种视图各司其职：
 *
 *   · **树图**（面积 ∝ 客户数）—— 一屏看清"格局"：哪几类占了大部分客户。这是"分布地图"本尊。
 *   · **横向条形**（长度 ∝ 客户数）—— 做**精确排序**与比较；面积类图形做不到这件事。
 *   · **气泡云**（半径 ∝ √客户数）—— 二级标签的"发现层"：找选题灵感，不承担读数职责。
 *
 * 点击任一块 → 抽屉里列出这一类**具体是谁 + 二级标签 + 证据原话**（可核对，不是黑箱）。
 *
 * ## 画法（2026-10-16 改为 echarts）
 *
 * 三张图分别用 echarts 的 `treemap` / `bar` / `graph(force)`，**按需注册**（唯一注册点
 * `src/echarts.js`）。原先手写的 squarified 布局已删除：那是"没有图表库时的替代品"，
 * 有库之后留着就是两套各自要维护的布局实现，还得自己补提示框/自适应/无障碍。
 * 保留的只有业务差异 —— 颜色按一级 key 固定（切片变化不换色，视觉可记忆）、悬浮显示
 * "客户数 · 提及次数"、点击开抽屉。
 *
 * ## 口径
 * 计数一律**按客户去重**（同一客户反复问同一件事只算一次）；`mentions`（提及次数）
 * 作为第二指标展示 —— 用来区分"很多人偶尔问"与"少数人反复问"。
 *
 * ⚠️ 抽屉里会出现**客户原话**：仅限内部选题参考，不得直接对外发布（卡内常驻提示）。
 */
import { ref, computed, nextTick, onMounted, onUnmounted, watch } from 'vue'
import { api, fmtRelative, toEpoch, gradeTag } from '../api'
import { useLoadFailures } from '../composables/loadFailures'
import { usePolling } from '../composables/usePolling'
import echarts from '../echarts'
import PartialLoadBanner from './PartialLoadBanner.vue'

const GRADES = ['all', 'S', 'A', 'B', 'C', 'D']
const GRADE_CN = { all: '全部', S: 'S', A: 'A', B: 'B', C: 'C', D: 'D' }

const dist = ref(null)
const staffs = ref([])
const scope = ref('all')
const staffId = ref('')
const running = ref(false)
const prog = ref({})
const busy = ref(false)
const msg = ref('')
const drawer = ref(null)          // {title, items}
const { failures, note: noteFail, clear: clearFail } = useLoadFailures()

// 一级分类的固定配色（**按 key 固定**，不按排序位置 —— 否则切片一变颜色就换，视觉不可记忆）
const PALETTE = ['#3b82f6', '#0ea5e9', '#14b8a6', '#22c55e', '#84cc16',
                 '#eab308', '#f97316', '#ef4444', '#a855f7', '#94a3b8']
function colorOf(key) {
  const order = dist.value?.taxonomy?.order || []
  const i = Math.max(0, order.indexOf(key))
  return PALETTE[i % PALETTE.length]
}
function nameOf(key) {
  return (dist.value?.taxonomy?.names || {})[key] || key
}

const primaries = computed(() => (dist.value?.primaries || []).filter(p => p.customers > 0))
const totalCustomers = computed(() => primaries.value.reduce((s, p) => s + p.customers, 0))

/** 这一类占了多少客户（条形右端的百分比文案用）。 */
function pct(p) {
  const t = totalCustomers.value || 1
  return Math.round((p.customers / t) * 100)
}

// ============================================================ 三张图（echarts）

const mapEl = ref(null)
const barEl = ref(null)
const cloudEl = ref(null)
let mapChart = null
let barChart = null
let cloudChart = null

//: 悬浮提示统一形状：**客户数在前、提及次数在后**（口径是"按客户去重"，数字顺序不能反）
const TIP = { backgroundColor: 'rgba(17,24,39,.92)', borderWidth: 0, padding: [6, 9],
              textStyle: { color: '#fff', fontSize: 12 } }
const tipText = (name, p) =>
  `${name}<br/>${p?.customers ?? 0} 个客户 · ${p?.mentions ?? 0} 次提及`

const BAR_H = computed(() => Math.max(120, primaries.value.length * 30 + 12))
const CLOUD_H = 240

function treemapOption() {
  return {
    tooltip: { ...TIP, formatter: p => tipText(p.name, p.data?.raw) },
    labelLayout: { hideOverlap: true },
    series: [{
      type: 'treemap',
      roam: false,
      nodeClick: false,                 // 单击交给抽屉（钻取由抽屉做，见 open()）
      breadcrumb: { show: false },
      animationDuration: 260,
      left: 0, top: 2, right: 0, bottom: 0,
      itemStyle: { borderColor: 'rgba(255,255,255,.85)', borderWidth: 2, gapWidth: 2,
                   borderRadius: 4 },
      // 只一级（不挂二级子节点）：子节点的客户数之和 ≥ 父节点（同一客户可以有多条二级标签），
      // 挂上去会让"面积 ∝ 客户数"不再成立 —— 宁可少一层，也不能画一张读不出数的图
      label: { show: true, color: '#fff', fontWeight: 600, fontSize: 13, lineHeight: 17,
               formatter: p => `${p.name}\n${p.value} 客户 · ${pct(p.data?.raw || {})}%` },
      data: primaries.value.map(p => ({
        name: nameOf(p.primary), key: p.primary, value: p.customers, raw: p,
        itemStyle: { color: colorOf(p.primary) },
      })),
    }],
  }
}

function barOption() {
  const list = [...primaries.value].reverse()      // 横向条形自下而上画，反转后最大者在顶
  return {
    tooltip: { ...TIP, formatter: p => tipText(p.name, list[p.dataIndex]) },
    grid: { left: 4, right: 104, top: 4, bottom: 4, containLabel: true },
    xAxis: { type: 'value', show: false, max: list[0]?.customers || 1 },
    yAxis: { type: 'category', data: list.map(p => nameOf(p.primary)),
             axisTick: { show: false }, axisLine: { show: false },
             axisLabel: { fontSize: 12, color: '#4b5563' } },
    series: [{
      type: 'bar',
      barMaxWidth: 14,
      animationDuration: 260,
      itemStyle: { borderRadius: [0, 7, 7, 0] },
      label: { show: true, position: 'right', fontSize: 11.5, color: '#6b7280',
               formatter: p => `${p.value} 客户 · ${list[p.dataIndex]?.mentions ?? 0} 次` },
      data: list.map(p => ({ value: p.customers, itemStyle: { color: colorOf(p.primary) } })),
    }],
  }
}

function cloudOption() {
  const list = (dist.value?.secondaries || []).filter(s => s.customers > 0).slice(0, 40)
  const max = Math.max(1, ...list.map(s => s.customers))
  return {
    tooltip: { ...TIP, formatter: p => tipText(p.name, p.data?.raw) },
    series: [{
      type: 'graph',
      layout: 'force',
      roam: false,
      animation: false,                 // 力导向只跑一次（见下），不做入场动画
      // `layoutAnimation:false` → 位置在初始化时一次算完，同输入同结果（不会每帧抖动）
      force: { repulsion: 140, gravity: 0.09, edgeLength: 0, friction: 0.7,
               layoutAnimation: false },
      label: { show: true, position: 'inside', color: '#fff', fontSize: 11,
               formatter: p => (p.name.length > 6 ? p.name.slice(0, 6) + '…' : p.name) },
      emphasis: { focus: 'self', label: { fontSize: 11 } },
      data: list.map(s => ({
        name: s.secondary, key: s.primary, raw: s,
        // 半径 ∝ √客户数（面积才 ∝ 客户数）；最小 14px 保证还能点得中
        symbolSize: 14 + 42 * Math.sqrt(s.customers / max),
        itemStyle: { color: colorOf(s.primary), opacity: 0.92 },
      })),
      links: [],
    }],
  }
}

/** 取（或重建）某个容器上的实例 —— 容器被 `v-else` 卸载过就必须重建。 */
function instanceFor(el, inst, onPick) {
  const usable = inst && !inst.isDisposed() && inst.getDom() === el && el.isConnected
  if (usable) return inst
  if (inst && !inst.isDisposed()) inst.dispose()
  const chart = echarts.init(el)
  chart.on('click', onPick)             // 只在**新建**时绑定，避免重复注册（会触发多次）
  return chart
}

function draw() {
  nextTick(() => {
    if (!primaries.value.length) return          // 空态：容器不在 DOM 里
    if (mapEl.value) {
      mapChart = instanceFor(mapEl.value, mapChart, p => p.data?.key && open(p.data.key))
      mapChart.setOption(treemapOption(), true)
    }
    if (barEl.value) {
      barChart = instanceFor(barEl.value, barChart, p => {
        const item = [...primaries.value].reverse()[p.dataIndex]
        if (item) open(item.primary)
      })
      barChart.setOption(barOption(), true)
    }
    if (cloudEl.value) {
      cloudChart = instanceFor(cloudEl.value, cloudChart, p => {
        const s = p.data?.raw
        if (s) open(s.primary, s.secondary)
      })
      cloudChart.setOption(cloudOption(), true)
    }
  })
}

function onResize() {
  for (const c of [mapChart, barChart, cloudChart]) {
    if (c && !c.isDisposed()) c.resize()
  }
}

watch(dist, () => draw())

// ============================================================ 取数与操作

async function load() {
  try {
    dist.value = await api.interestDistribution({
      scope: scope.value, staff_id: staffId.value || '', top: 8, secondary_top: 40,
    })
    clearFail('dist')
  } catch (e) { noteFail('dist', '兴趣点分布', e) }
}

async function loadStatus() {
  try {
    const st = await api.interestStatus()
    running.value = !!st.running
    prog.value = st.progress || {}
    clearFail('status')
  } catch (e) { noteFail('status', '分析状态', e) }
}

async function loadStaffs() {
  try {
    staffs.value = (await api.staffs({ only_with_session: 1, limit: 800 })).items || []
    clearFail('staffs')
  } catch (e) { noteFail('staffs', '员工列表', e) }
}

function refresh() { load(); loadStatus() }

async function start(mode = 'incremental') {
  const cov = dist.value?.coverage || {}
  const text = mode === 'full'
    ? `全量重跑：会重新分析**全部**有正文的会话（当前 ${cov.sessions_available || 0} 个）。\n\n`
      + `· 每条一次大模型调用，可能数小时；\n· 同一条会话的内容没变的话，结果本来也不会变。\n\n确定吗？`
    : `增量分析：只跑"没分析过"或"有新消息"的会话（当前待分析 ${cov.sessions_pending || 0} 个）。\n\n`
      + `· 每条一次大模型调用；\n· 结果落在库里，随时可以重跑。\n\n确定开始吗？`
  if (!window.confirm(text)) return
  busy.value = true; msg.value = ''
  try {
    const r = await api.interestStart({ max_sessions: 0 })
    if (!r.ok) { msg.value = r.reason || '启动失败'; return }
    msg.value = `已启动兴趣点分析（run #${r.run_id}）`
    await loadStatus()
  } catch (e) { msg.value = '启动失败：' + (e?.message || e) } finally { busy.value = false }
}

async function stop() {
  busy.value = true; msg.value = ''
  try {
    const r = await api.interestStop()
    msg.value = r.ok ? (r.message || '已发出停止信号') : (r.reason || '没有正在运行的分析')
    await loadStatus()
  } catch (e) { msg.value = '停止失败：' + (e?.message || e) } finally { busy.value = false }
}

/** 点任意块/标签 → 抽屉（谁 + 二级标签 + 证据原话）。 */
async function open(primary, secondary = '') {
  const title = secondary ? `「${secondary}」的客户（${nameOf(primary)}）` : nameOf(primary)
  drawer.value = { title, items: [], loading: true }
  try {
    const r = await api.interestCustomers({
      primary, secondary, scope: scope.value, staff_id: staffId.value || '', limit: 50,
    })
    drawer.value = { title, items: r.items || [], loading: false }
  } catch (e) {
    drawer.value = { title, items: [], loading: false, error: e?.message || String(e) }
  }
}

const progressPct = computed(() => {
  const t = prog.value.session_total || 0
  return t ? Math.min(100, Math.round(((prog.value.session_done || 0) / t) * 100)) : 0
})

// 只在**分析中**轮询（空闲时 30 秒一次也纯属浪费；页面隐藏时 usePolling 自己会停）
usePolling(loadStatus, 4000, { idle: 30000, idleWhen: () => !running.value })
onMounted(() => {
  loadStaffs()
  refresh()
  window.addEventListener('resize', onResize)
})
onUnmounted(() => {
  drawer.value = null
  window.removeEventListener('resize', onResize)
  for (const c of [mapChart, barChart, cloudChart]) {
    if (c && !c.isDisposed()) c.dispose()          // 不 dispose 会漏 canvas 与全局事件
  }
})
defineExpose({ refresh })
</script>

<template>
  <div class="card interest">
    <div class="card-head">
      兴趣点分布图
      <span class="muted" style="font-weight:400;font-size:12px;margin-left:8px">
        按<b>客户</b>去重（同一客户反复问只算一次）· 只看<b>客户发言</b>（员工话术不算）·
        悬浮看提及次数
      </span>
      <span v-if="dist?.coverage" class="muted" style="margin-left:auto;font-size:12px">
        已分析 <b>{{ dist.coverage.sessions_analyzed }}</b>/{{ dist.coverage.sessions_available }} 会话 ·
        {{ dist.coverage.customers }} 客户 ·
        <span v-if="dist.analyzed_at">最近 {{ fmtRelative(toEpoch(dist.analyzed_at)) }}</span>
        <span v-else>尚无数据</span>
      </span>
    </div>

    <div class="card-body" style="display:grid;gap:12px">
      <PartialLoadBanner :failures="failures" @retry="refresh" />

      <!-- 切片 + 操作 -->
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <div class="seg compact">
          <button v-for="g in GRADES" :key="g" :class="{ on: scope === g }"
                  @click="scope = g; load()">{{ GRADE_CN[g] }}</button>
        </div>
        <select v-model="staffId" class="select" style="width:190px" @change="load()">
          <option value="">全部员工</option>
          <option v-for="s in staffs" :key="s.staff_id" :value="s.staff_id">{{ s.name }}</option>
        </select>
        <button class="btn sm" :disabled="busy" @click="refresh">刷新</button>
        <span class="muted" style="font-size:11.5px">等级切片按客户<b>当前有效</b>的评估结论</span>
        <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
          <button class="btn sm primary" :disabled="busy || running" @click="start('incremental')">
            <i v-if="busy" class="spin"></i>{{ running ? '分析中…' : '开始分析（增量）' }}
          </button>
          <button class="btn sm danger" v-if="running" :disabled="busy" @click="stop">停止</button>
        </div>
      </div>

      <div v-if="running" style="display:grid;gap:4px">
        <div style="display:flex;justify-content:space-between;font-size:12px">
          <span class="muted">{{ prog.stage || '分析中' }}</span>
          <span class="muted">{{ prog.session_done || 0 }}/{{ prog.session_total || 0 }} 会话 ·
            已落 {{ prog.interest_points || 0 }} 条兴趣点</span>
        </div>
        <div class="progress"><i :style="{ width: progressPct + '%' }"></i></div>
      </div>

      <div v-if="msg" class="alert ok" style="font-size:12.5px">{{ msg }}</div>

      <!-- 空态 -->
      <div v-if="!primaries.length" class="empty" style="padding:26px">
        <div class="big">🧭</div>
        还没有兴趣点数据
        <div class="muted" style="font-size:12px;margin-top:6px">
          点右上「开始分析（增量）」跑一轮（每会话一次模型调用，
          当前有 {{ dist?.coverage?.sessions_pending ?? 0 }} 个会话待分析）
        </div>
      </div>

      <!-- 三件套（echarts；容器尺寸交给 CSS，图表内部自适应） -->
      <div v-else class="triple">
        <div class="map">
          <div class="block-title" style="padding:2px 2px 6px">
            一级分类 · 面积 ∝ 客户数（点任一块看"是谁"）
          </div>
          <div ref="mapEl" class="chart" style="height:286px"></div>
        </div>

        <div class="side">
          <div class="block-title">一级分类（按客户数 · 长度才读得准）</div>
          <div ref="barEl" class="chart" :style="{ height: BAR_H + 'px' }"></div>
          <div class="block-title">二级标签（话题发现层，前 40 · 半径 ∝ √客户数）</div>
          <div ref="cloudEl" class="chart" :style="{ height: CLOUD_H + 'px' }"></div>
        </div>
      </div>

      <div class="muted" style="font-size:11.5px;line-height:1.7;border-top:1px dashed var(--border);padding-top:8px">
        ⚠️ 证据原话来自客户真实聊天记录，**仅限内部选题参考，不得直接对外发布**；
        图形口径：树图面积 / 条形长度 ∝ 客户数，气泡半径 ∝ √客户数（面积读不准，需要精确比较看条形）。
      </div>
    </div>

    <!-- 抽屉：谁 + 二级标签 + 证据原话 -->
    <div v-if="drawer" class="drawer-mask" @click.self="drawer = null">
      <div class="drawer">
        <div class="card-head">
          {{ drawer.title }}
          <span class="muted" style="font-weight:400;font-size:12px;margin-left:6px">
            {{ drawer.items.length }} 个客户（最多 50）
          </span>
          <button class="btn sm" style="margin-left:auto" @click="drawer = null">关闭</button>
        </div>
        <div style="padding:12px;display:grid;gap:10px;max-height:70vh;overflow:auto">
          <div v-if="drawer.loading" class="muted">读取中…</div>
          <div v-if="drawer.error" class="alert err" style="font-size:12.5px">{{ drawer.error }}</div>
          <div v-for="c in drawer.items" :key="c.session_id" class="cust">
            <div style="display:flex;gap:6px;align-items:baseline;flex-wrap:wrap">
              <b>{{ c.target_name || c.target_id }}</b>
              <span class="tag" v-if="c.grade" :class="gradeTag(c.grade)">{{ c.grade }}</span>
              <span class="muted" style="font-size:11.5px">{{ c.staff_name }} · {{ c.msg_count }} 条消息</span>
              <a class="btn sm" style="margin-left:auto"
                 :href="`#/chat?staff=${encodeURIComponent(c.staff_id)}&session=${encodeURIComponent(c.session_id)}`">
                打开会话
              </a>
            </div>
            <div class="muted" style="font-size:11.5px;margin-top:2px">
              {{ (c.secondaries || []).join('、') }}
            </div>
            <ul class="ev">
              <li v-for="(m, i) in (c.matched || [])" :key="i">
                <span class="tag" style="margin-right:4px">{{ nameOf(m.primary) }}</span>
                <b>{{ m.secondary }}</b>
                <div v-for="(e, j) in (m.evidence || [])" :key="j" class="quote">{{ e }}</div>
              </li>
            </ul>
          </div>
          <div v-if="!drawer.loading && !drawer.items.length" class="muted">
            这一类下暂时没有客户（换切片或先跑一轮分析）
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.interest { margin-bottom: 16px; }
.triple { display: grid; grid-template-columns: 55% 1fr; gap: 14px; align-items: start; }
@media (max-width: 1180px) { .triple { grid-template-columns: 1fr; } }
/* `min-width:0` 是必须的：grid 子项默认 min-width:auto，echarts 初始化时按 0 宽度
   量不到尺寸 → 图会画成空白（前端经典坑） */
.map, .side { min-width: 0; }
.map { background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px; padding: 6px; }
.chart { width: 100%; }
.side { display: grid; gap: 6px; align-content: start; }
.block-title { font-size: 12px; color: var(--text-2); font-weight: 600; }
.cust { border-bottom: 1px solid var(--border); padding-bottom: 8px; }
.ev { margin: 4px 0 0 0; padding-left: 14px; font-size: 11.5px; color: var(--text-2); }
.quote { background: var(--panel-2); border-left: 2px solid var(--border); padding: 2px 6px;
  margin: 3px 0; border-radius: 3px; white-space: pre-wrap; }
.drawer-mask { position: fixed; inset: 0; background: rgba(16, 24, 40, .35); display: flex;
  justify-content: flex-end; z-index: 60; }
.drawer { width: 620px; max-width: 94vw; height: 100%; background: var(--panel); overflow: auto;
  box-shadow: -8px 0 30px rgba(16, 24, 40, .14); }
</style>
