<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api, fmtTime } from '../api'

const form = ref({
  kind: 'incremental',
  staff_id: '',
  session_types: ['ext_user'],
  max_sessions_per_staff: 200,
  max_msgs_per_session: 0,
})

const staffs = ref([])
const status = ref(null)
const jobs = ref([])
const schedule = ref({ enabled: false, interval_minutes: 0, next_run_time: null })
const keepalive = ref(null)
const kaInterval = ref(null)
const kaBusy = ref(false)
const busy = ref(false)
const error = ref('')

let timer = null

async function refresh() {
  try {
    const [s, j, sc, ka] = await Promise.all([
      api.syncStatus(), api.syncJobs(15), api.syncSchedule(), api.keepaliveStatus(),
    ])
    status.value = s
    jobs.value = j.items || []
    schedule.value = sc
    keepalive.value = ka
    // 只在首次（或用户未改动时）回填保活间隔
    if (kaInterval.value === null && sc?.keepalive) {
      kaInterval.value = sc.keepalive.interval_minutes ?? 0
    }
  } catch (e) { error.value = '后端未就绪：' + e.message }
}

async function loadStaffs() {
  try {
    const r = await api.staffs({ only_with_session: 1, limit: 800 })
    staffs.value = r.items || []
  } catch (e) { /* ignore */ }
}

const job = computed(() => status.value?.job || {})
const progress = computed(() => {
  const total = job.value.session_total || 0
  const done = job.value.session_done || 0
  return total ? Math.min(100, Math.round((done / total) * 100)) : 0
})
const running = computed(() => !!status.value?.running)

// ---------------- 凭据保活 ----------------
// 微伴后台没有账号密码登录（只能企微扫码），但登录态是「滑动过期」的：
// 每次访问后台，session_id 过期时间被重置为「当前时间 + 7 天」。
// 所以定期保活即可让凭据永不过期，无需人工重新扫码。
const kaCred = computed(() => keepalive.value?.credential || {})
const kaLast = computed(() => keepalive.value?.last_run || {})

const kaAgeText = computed(() => {
  const h = kaCred.value.age_hours
  if (h === null || h === undefined) return '暂无凭据文件'
  if (h < 1) return `更新于 ${Math.max(1, Math.round(h * 60))} 分钟前`
  if (h < 48) return `更新于 ${h.toFixed(1)} 小时前`
  return `已 ${(h / 24).toFixed(1)} 天未刷新`
})

const kaFresh = computed(() => !!kaCred.value.exists && (kaCred.value.age_hours ?? 999) < 48)
const kaTagText = computed(() => (!kaCred.value.exists ? '无凭据' : kaFresh.value ? '正常' : '待刷新'))
const kaTagClass = computed(() => (kaFresh.value ? 'green' : 'orange'))

const kaLastText = computed(() => {
  const l = kaLast.value
  if (!l.at) return '本次启动后尚未保活'
  return `${new Date(l.at * 1000).toLocaleString()}｜${l.ok ? '成功' : '失败'}`
})

async function start() {
  busy.value = true; error.value = ''
  try {
    const payload = {
      kind: form.value.kind,
      staff_ids: form.value.staff_id ? [form.value.staff_id] : null,
      session_types: form.value.session_types.length ? form.value.session_types : ['ext_user'],
      max_sessions_per_staff: Number(form.value.max_sessions_per_staff) || 0,
      max_msgs_per_session: Number(form.value.max_msgs_per_session) || 0,
    }
    const r = await api.syncStart(payload)
    if (!r.ok) error.value = r.reason || '启动失败'
  } catch (e) { error.value = e.message }
  busy.value = false
  refresh()
}

async function stop() {
  busy.value = true
  try { await api.syncStop() } catch (e) { error.value = e.message }
  busy.value = false
  refresh()
}

async function saveSchedule() {
  try { schedule.value = await api.syncSetSchedule(Number(schedule.value.interval_minutes) || 0) }
  catch (e) { error.value = e.message }
}

async function saveKa() {
  try { schedule.value = await api.syncSetKeepalive(Number(kaInterval.value) || 0) }
  catch (e) { error.value = e.message }
}

async function runKa() {
  kaBusy.value = true; error.value = ''
  try {
    const r = await api.keepaliveRun(45)
    if (!r.ok) error.value = '保活失败：' + (r.message || '未知原因')
    await refresh()
  } catch (e) { error.value = '保活失败：' + e.message }
  kaBusy.value = false
}

function toggleType(t) {
  const arr = form.value.session_types
  const i = arr.indexOf(t)
  i >= 0 ? arr.splice(i, 1) : arr.push(t)
}

const STATUS_TAG = {
  success: 'green', running: 'blue', failed: 'red', stopped: 'orange',
}
const STATUS_CN = {
  success: '成功', running: '运行中', failed: '失败', stopped: '已停止',
}

onMounted(() => { refresh(); loadStaffs(); timer = setInterval(refresh, 2500) })
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="page">
    <div class="layout">

      <!-- 左：启动同步 -->
      <div style="display:grid;gap:16px">
        <div class="card">
          <div class="card-head">启动同步</div>
          <div class="card-body" style="display:grid;gap:14px">
            <div>
              <label class="lbl">同步模式</label>
              <div class="seg">
                <button :class="{ on: form.kind === 'incremental' }" @click="form.kind = 'incremental'">
                  增量同步
                  <small>只拉水位之后的新消息，快</small>
                </button>
                <button :class="{ on: form.kind === 'full' }" @click="form.kind = 'full'">
                  全量同步
                  <small>忽略水位重新拉，慢</small>
                </button>
              </div>
            </div>

            <div>
              <label class="lbl">员工范围</label>
              <select v-model="form.staff_id" class="select">
                <option value="">全部员工（{{ staffs.length }} 名有会话的）</option>
                <option v-for="s in staffs" :key="s.staff_id" :value="s.staff_id">
                  {{ s.name }}（{{ s.session_count }} 会话）
                </option>
              </select>
            </div>

            <div>
              <label class="lbl">会话类型</label>
              <div class="chips">
                <button v-for="t in [['ext_user','客户会话'],['room','群聊'],['other_staff','同事']]"
                        :key="t[0]" class="chip" :class="{ on: form.session_types.includes(t[0]) }"
                        @click="toggleType(t[0])">{{ t[1] }}</button>
              </div>
            </div>

            <div class="two">
              <div>
                <label class="lbl">每员工最多会话数<span class="muted">（0=不限）</span></label>
                <input v-model="form.max_sessions_per_staff" class="input" type="number" min="0" />
              </div>
              <div>
                <label class="lbl">每会话最多消息<span class="muted">（0=不限）</span></label>
                <input v-model="form.max_msgs_per_session" class="input" type="number" min="0" />
              </div>
            </div>

            <div style="display:flex;gap:10px">
              <button class="btn primary" style="flex:1" :disabled="running || busy" @click="start">
                <i v-if="busy" class="spin"></i>{{ running ? '同步中…' : '开始同步' }}
              </button>
              <button class="btn danger" :disabled="!running || busy" @click="stop">停止</button>
            </div>

            <div v-if="error" class="alert err">{{ error }}</div>
          </div>
        </div>

        <div class="card">
          <div class="card-head">定时同步</div>
          <div class="card-body" style="display:grid;gap:12px">
            <div style="display:flex;gap:10px;align-items:flex-end">
              <div style="flex:1">
                <label class="lbl">间隔（分钟，0 = 关闭）</label>
                <input v-model="schedule.interval_minutes" class="input" type="number" min="0" />
              </div>
              <button class="btn" @click="saveSchedule">保存</button>
            </div>
            <div class="alert" :class="schedule?.enabled ? 'ok' : 'info'">
              {{ schedule?.enabled
                  ? `已开启：每 ${schedule.interval_minutes} 分钟自动增量同步，下次运行 ${schedule.next_run_time ? new Date(schedule.next_run_time).toLocaleString() : '-'}`
                  : '定时同步已关闭，仅支持手动触发' }}
            </div>
          </div>
        </div>

        <!-- 凭据保活 -->
        <div class="card">
          <div class="card-head">
            凭据保活
            <span class="tag" :class="kaTagClass" style="margin-left:auto">{{ kaTagText }}</span>
          </div>
          <div class="card-body" style="display:grid;gap:12px">
            <div class="alert" :class="kaFresh ? 'ok' : 'info'">
              微伴登录态：{{ kaAgeText }}
              <span v-if="kaCred.open_sid_tail" class="mono" style="margin-left:6px">{{ kaCred.open_sid_tail }}</span>
            </div>

            <div style="display:flex;gap:10px;align-items:flex-end">
              <div style="flex:1">
                <label class="lbl">保活间隔（分钟，0 = 关闭）</label>
                <input v-model="kaInterval" class="input" type="number" min="0" />
              </div>
              <button class="btn" @click="saveKa">保存</button>
            </div>

            <div class="alert info" style="font-size:12px">
              {{ schedule?.keepalive?.enabled
                  ? `每 ${schedule.keepalive.interval_minutes} 分钟自动保活，下次 ${schedule.keepalive.next_run_time ? new Date(schedule.keepalive.next_run_time).toLocaleString() : '-'}`
                  : '自动保活已关闭' }}
            </div>

            <div class="muted" style="font-size:12px">最近一次：{{ kaLastText }}</div>

            <button class="btn primary" :disabled="kaBusy || running" @click="runKa">
              <i v-if="kaBusy" class="spin"></i>{{ kaBusy ? '保活中…' : '立即保活' }}
            </button>

            <div class="muted" style="font-size:11.5px;line-height:1.6">
              微伴后台无账号密码登录（只能企微扫码），但登录态是滑动过期的 ——
              每次访问自动续期 7 天，定期保活即可免去人工重新扫码。
            </div>
          </div>
        </div>
      </div>

      <!-- 右：进度 + 历史 -->
      <div style="display:grid;gap:16px">
        <div class="card">
          <div class="card-head">
            当前任务
            <span class="tag" :class="STATUS_TAG[job.status] || 'blue'">{{ STATUS_CN[job.status] || '空闲' }}</span>
            <span v-if="running" style="margin-left:auto"><i class="spin dark" style="display:inline-block"></i></span>
          </div>
          <div class="card-body" style="display:grid;gap:14px">
            <div>
              <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:6px">
                <span class="muted">{{ status?.progress?.stage || '暂无任务' }}</span>
                <span class="muted">{{ job.session_done || 0 }} / {{ job.session_total || 0 }} 会话</span>
              </div>
              <div class="progress"><i :style="{ width: progress + '%' }"></i></div>
            </div>

            <div class="grid-stats">
              <div class="stat card">
                <div class="label">拉取消息</div>
                <div class="value">{{ (job.msg_total || 0).toLocaleString() }}</div>
              </div>
              <div class="stat card">
                <div class="label">解密命中</div>
                <div class="value" style="color:var(--ok)">{{ (job.msg_decrypted || 0).toLocaleString() }}</div>
              </div>
              <div class="stat card">
                <div class="label">新增入库</div>
                <div class="value" style="color:var(--brand)">{{ (job.msg_new || 0).toLocaleString() }}</div>
              </div>
            </div>

            <div class="alert info" v-if="status?.progress?.current_session">
              正在处理：{{ status.progress.current_session }}
              <span class="mono" style="margin-left:6px">{{ (status.progress.current_staff || '').slice(0, 20) }}…</span>
            </div>
            <div class="alert err" v-if="job.error">{{ job.error }}</div>
          </div>
        </div>

        <div class="card">
          <div class="card-head">任务历史</div>
          <div style="max-height:380px;overflow:auto">
            <table class="tbl">
              <thead>
                <tr><th>#</th><th>模式</th><th>状态</th><th>会话</th><th>消息</th><th>解密</th><th>开始</th></tr>
              </thead>
              <tbody>
                <tr v-for="j in jobs" :key="j.id">
                  <td class="mono">{{ j.id }}</td>
                  <td>
                    {{ j.kind === 'full' ? '全量' : '增量' }}
                    <span class="tag" :class="j.triggered_by === 'schedule' ? 'orange' : 'blue'">
                      {{ j.triggered_by === 'schedule' ? '定时' : '手动' }}
                    </span>
                  </td>
                  <td><span class="tag" :class="STATUS_TAG[j.status] || ''">{{ STATUS_CN[j.status] || j.status }}</span></td>
                  <td class="mono">{{ j.session_done }}/{{ j.session_total }}</td>
                  <td class="mono">{{ (j.msg_total || 0).toLocaleString() }}</td>
                  <td class="mono" style="color:var(--ok)">{{ (j.msg_decrypted || 0).toLocaleString() }}</td>
                  <td class="muted" style="font-size:12px">{{ fmtTime(Date.parse(j.started_at) / 1000) }}</td>
                </tr>
                <tr v-if="!jobs.length"><td colspan="7" class="muted" style="text-align:center;padding:24px">暂无任务记录</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

    </div>
  </div>
</template>

<style scoped>
.layout { display: grid; grid-template-columns: 380px 1fr; gap: 16px; align-items: start; }
@media (max-width: 1100px) { .layout { grid-template-columns: 1fr; } }

.lbl { display: block; font-size: 12px; color: var(--text-2); margin-bottom: 6px; font-weight: 500; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }

.seg { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.seg button {
  text-align: left; padding: 10px 12px; border-radius: 9px; border: 1px solid var(--border);
  background: var(--panel); cursor: pointer; font-family: inherit; font-size: 13px;
  font-weight: 600; color: var(--text); transition: .15s; line-height: 1.4;
}
.seg button small { display: block; font-weight: 400; font-size: 11px; color: var(--text-3); margin-top: 3px; }
.seg button.on { border-color: var(--brand); background: var(--brand-weak); color: var(--brand); }
.seg button.on small { color: #5b7fbf; }

.chips { display: flex; gap: 8px; flex-wrap: wrap; }
.chip {
  padding: 6px 14px; border-radius: 99px; border: 1px solid var(--border); background: var(--panel);
  font-size: 12.5px; cursor: pointer; font-family: inherit; color: var(--text-2); transition: .15s;
}
.chip.on { border-color: var(--brand); background: var(--brand-weak); color: var(--brand); font-weight: 600; }
</style>
