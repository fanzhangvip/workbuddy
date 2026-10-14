<script setup>
import { ref, computed, watch, onMounted, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import { api, fmtTime, fmtClock, fmtRelative, dayLabel } from '../api'

const route = useRoute()

const PAGE_SIZE = 60

const staffs = ref([])
const staffId = ref('')
const staffQ = ref('')
const staffOpen = ref(false)

const sessions = ref([])
const sessionTotal = ref(0)
const sessionQ = ref('')
const sessionType = ref('ext_user')
const onlyMsg = ref(true)
const loadingSessions = ref(false)

const current = ref(null)
const messages = ref([])
const loadingMsgs = ref(false)
const hasMore = ref(false)
const msgQ = ref('')
const msgQActive = ref('')

const scrollEl = ref(null)
const error = ref('')

// ---------------- 员工 ----------------
async function loadStaffs() {
  try {
    const r = await api.staffs({ only_with_session: 1, limit: 800 })
    staffs.value = r.items || []
    if (!staffId.value && staffs.value.length) staffId.value = staffs.value[0].staff_id
  } catch (e) { error.value = '加载员工失败：' + e.message }
}

const filteredStaffs = computed(() => {
  const kw = staffQ.value.trim().toLowerCase()
  if (!kw) return staffs.value
  return staffs.value.filter(s =>
    (s.name || '').toLowerCase().includes(kw) || (s.alias || '').toLowerCase().includes(kw))
})

const currentStaff = computed(() => staffs.value.find(s => s.staff_id === staffId.value) || null)

function pickStaff(s) {
  staffId.value = s.staff_id
  staffOpen.value = false
  staffQ.value = ''
}

// ---------------- 会话 ----------------
async function loadSessions() {
  if (!staffId.value) { sessions.value = []; return }
  loadingSessions.value = true
  try {
    const r = await api.sessions({
      staff_id: staffId.value, session_type: sessionType.value,
      q: sessionQ.value.trim(), has_msg: onlyMsg.value ? 1 : 0, limit: 300,
    })
    sessions.value = r.items || []
    sessionTotal.value = r.total || 0
  } catch (e) { error.value = '加载会话失败：' + e.message }
  loadingSessions.value = false
}

let searchTimer = null
let ready = false                      // 初始化期间抑制 watch，避免与首屏加载竞态
watch(sessionQ, () => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(loadSessions, 300)
})
watch([staffId, sessionType, onlyMsg], () => {
  if (!ready) return
  current.value = null; messages.value = []; loadSessions()
})

// ---------------- 消息 ----------------
async function openSession(s, keepQuery = false) {
  current.value = s
  if (!keepQuery) { msgQ.value = ''; msgQActive.value = '' }
  await loadMessages(true)
}

async function loadMessages(reset = false) {
  if (!current.value) return
  loadingMsgs.value = true
  try {
    const params = {
      staff_id: current.value.staff_id,
      session_id: current.value.session_id,
      limit: PAGE_SIZE,
    }
    if (msgQActive.value) params.q = msgQActive.value
    if (!reset && messages.value.length) params.before_time = messages.value[0].msg_time
    const r = await api.messages(params)
    messages.value = reset ? r.items : [...r.items, ...messages.value]
    hasMore.value = r.has_more && !msgQActive.value
    if (reset) await nextTick(() => scrollToBottom())
  } catch (e) { error.value = '加载消息失败：' + e.message }
  loadingMsgs.value = false
}

function searchInSession() {
  msgQActive.value = msgQ.value.trim()
  loadMessages(true)
}
function clearInSessionSearch() {
  msgQ.value = ''; msgQActive.value = ''
  loadMessages(true)
}

function scrollToBottom() {
  const el = scrollEl.value
  if (el) el.scrollTop = el.scrollHeight
}

const grouped = computed(() => {
  const out = []
  for (const m of messages.value) {
    const day = dayLabel(m.msg_time)
    if (!out.length || out[out.length - 1].day !== day) out.push({ day, items: [] })
    out[out.length - 1].items.push(m)
  }
  return out
})

const decodedRate = computed(() => {
  const total = current.value?.msg_count || 0
  const ok = current.value?.decoded_count || 0
  return total ? Math.round((ok / total) * 100) : 0
})

function avatarOf(m) {
  if (m.direction === 'send') return currentStaff.value?.avatar || ''
  return current.value?.avatar || ''
}

// 媒体消息：是否媒体、类型名由后端统一下发（ctx_preprocess），
// 前端只负责展示（emoji 图标与富渲染属纯展示，不重复维护映射）。
function mediaKind(m) {
  return m.is_media && m.media_url && !m._imgFail ? 'kind-' + m.msgtype : ''
}
const MEDIA_LABEL = { 2: '🖼 图片', 3: '😄 表情', 6: '🎤 语音', 7: '🎬 视频', 8: '📎 文件' }
function msgText(m) {
  if (m.is_media) {
    return `${MEDIA_LABEL[m.msgtype] || '附件'}（媒体链接有时效，点击尝试打开）`
  }
  return m.content || '〔正文未取到〕'
}
function imgFail(m) { m._imgFail = true }

function sessionLabel(s) {
  return s.remark || s.name || '（无备注）'
}

onMounted(async () => {
  await loadStaffs()
  // 支持从总览页带参数跳转：/chat?staff=xxx&session=yyy
  const qStaff = route.query.staff
  if (qStaff && staffs.value.some(s => s.staff_id === qStaff)) staffId.value = qStaff
  await loadSessions()
  const qSession = route.query.session
  if (qSession) {
    const found = sessions.value.find(s => s.session_id === qSession)
    if (found) await openSession(found)
  }
  ready = true
})
</script>

<template>
  <div class="page flush chat-page">
    <!-- ============ 左侧：员工 + 会话列表 ============ -->
    <aside class="sidebar">
      <div class="staff-picker">
        <div class="sp-trigger" @click="staffOpen = !staffOpen">
          <img v-if="currentStaff?.avatar" class="avatar round" :src="currentStaff.avatar" width="28" height="28" />
          <div class="sp-text">
            <div class="sp-name">{{ currentStaff?.name || '选择员工' }}</div>
            <div class="sp-sub muted">{{ currentStaff ? currentStaff.session_count + ' 个会话' : '暂无数据，先去同步' }}</div>
          </div>
          <span class="muted">▾</span>
        </div>
        <div v-if="staffOpen" class="sp-panel">
          <input v-model="staffQ" class="input" placeholder="搜索员工…" @click.stop />
          <div class="sp-list">
            <div v-for="s in filteredStaffs" :key="s.staff_id" class="sp-item"
                 :class="{ active: s.staff_id === staffId }" @click="pickStaff(s)">
              <img v-if="s.avatar" class="avatar round" :src="s.avatar" width="26" height="26" />
              <div class="sp-item-text">
                <div class="sp-name">{{ s.name }}</div>
                <div class="sp-sub muted">{{ s.session_count }} 会话 · {{ fmtRelative(s.last_msg_time) }}</div>
              </div>
            </div>
            <div v-if="!filteredStaffs.length" class="empty" style="min-height:80px">无匹配员工</div>
          </div>
        </div>
      </div>

      <div class="filters">
        <input v-model="sessionQ" class="input" placeholder="搜索客户昵称 / 备注…" />
        <div class="filter-row">
          <select v-model="sessionType" class="select">
            <option value="ext_user">客户会话</option>
            <option value="room">群聊</option>
            <option value="other_staff">同事</option>
          </select>
          <label class="ck"><input type="checkbox" v-model="onlyMsg" /> 只看已同步</label>
        </div>
      </div>

      <div class="sess-meta muted">
        <span>共 {{ sessionTotal }} 个会话</span>
        <span v-if="loadingSessions"><i class="spin dark" style="display:inline-block"></i></span>
      </div>

      <div class="sess-list">
        <div v-for="s in sessions" :key="s.session_id" class="sess"
             :class="{ active: current?.session_id === s.session_id }"
             @click="openSession(s)">
          <img class="avatar round" :src="s.avatar" width="38" height="38" />
          <div class="sess-main">
            <div class="sess-top">
              <span class="sess-name">{{ sessionLabel(s) }}</span>
              <span class="sess-time muted">{{ fmtRelative(s.last_msg_time) }}</span>
            </div>
            <div class="sess-sub">
              <span class="muted">{{ s.name }}</span>
              <span class="tag" :class="s.agree_msgaudit ? 'green' : 'red'">
                {{ s.agree_msgaudit ? '已存档' : '未同意' }}
              </span>
            </div>
            <div class="sess-sub muted" style="margin-top:2px">
              {{ s.msg_count }} 条 · 已解 {{ s.decoded_count }} 条
            </div>
          </div>
        </div>
        <div v-if="!sessions.length && !loadingSessions" class="empty">
          <div>
            <div class="big">💬</div>
            该员工暂无会话<br /><span style="font-size:12px">请到「同步中心」拉取数据</span>
          </div>
        </div>
      </div>
    </aside>

    <!-- ============ 右侧：消息区 ============ -->
    <section class="msg-pane">
      <template v-if="current">
        <header class="msg-head">
          <img class="avatar" :src="current.avatar" width="38" height="38" />
          <div class="mh-text">
            <div class="mh-title">
              {{ sessionLabel(current) }}
              <span v-if="current.remark && current.name" class="muted" style="font-weight:400;font-size:12px">
                （昵称：{{ current.name }}）
              </span>
            </div>
            <div class="mh-sub muted">
              {{ current.msg_count }} 条消息 · 正文命中 {{ decodedRate }}%
              · 会话 ID <span class="mono">{{ current.session_id }}</span>
            </div>
          </div>
          <div class="mh-actions">
            <input v-model="msgQ" class="input" style="width:170px" placeholder="会话内搜索…"
                   @keyup.enter="searchInSession" />
            <button class="btn sm" @click="searchInSession">搜索</button>
            <button v-if="msgQActive" class="btn sm" @click="clearInSessionSearch">清除</button>
          </div>
        </header>

        <div ref="scrollEl" class="msg-scroll">
          <div v-if="hasMore" class="load-more">
            <button class="btn sm" :disabled="loadingMsgs" @click="loadMessages(false)">
              加载更早的消息
            </button>
          </div>
          <div v-else-if="messages.length" class="load-more muted">已到最早一条</div>

          <div v-for="(g, gi) in grouped" :key="gi">
            <div class="day-sep"><span>{{ g.day }}</span></div>
            <div v-for="m in g.items" :key="m.message_id" class="row" :class="m.direction">
              <img class="avatar round" :src="avatarOf(m)" width="34" height="34" />
              <div class="bubble-col">
                <div class="bubble-meta">
                  {{ m.from_name || (m.direction === 'send' ? '员工' : '客户') }}
                  <span class="muted">{{ fmtClock(m.msg_time) }}</span>
                </div>

                <div class="bubble" :class="[m.direction, mediaKind(m)]">
                  <!-- 图片 -->
                  <template v-if="m.msgtype === 2 && m.media_url && !m._imgFail">
                    <a :href="m.media_url" target="_blank" rel="noreferrer">
                      <img class="msg-img" :src="m.media_url" alt="图片" @error="imgFail(m)" />
                    </a>
                  </template>
                  <!-- 表情 -->
                  <template v-else-if="m.msgtype === 3 && m.media_url && !m._imgFail">
                    <img class="msg-emoji" :src="m.media_url" alt="表情" @error="imgFail(m)" />
                  </template>
                  <!-- 语音 -->
                  <template v-else-if="m.msgtype === 6 && m.media_url">
                    <a class="media-card" :href="m.media_url" target="_blank" rel="noreferrer">
                      🎤 {{ m.media_name || '语音消息' }} <span class="muted">点击播放</span>
                    </a>
                  </template>
                  <!-- 视频 -->
                  <template v-else-if="m.msgtype === 7 && m.media_url">
                    <a class="media-card" :href="m.media_url" target="_blank" rel="noreferrer">🎬 视频文件 点击打开</a>
                  </template>
                  <!-- 文件 -->
                  <template v-else-if="m.msgtype === 8 && m.media_url">
                    <a class="media-card" :href="m.media_url" target="_blank" rel="noreferrer">
                      📎 {{ m.media_name || '文件' }} <span class="muted">点击下载</span>
                    </a>
                  </template>
                  <!-- 文本 / 其它 / 媒体无链接的回退 -->
                  <template v-else>
                    <a v-if="m.is_media && m.media_url"
                       class="media-card" :href="m.media_url" target="_blank" rel="noreferrer">
                      {{ msgText(m) }}
                    </a>
                    <span v-else style="white-space:pre-wrap">{{ msgText(m) }}</span>
                  </template>
                </div>

                <div class="bubble-foot muted">
                  <span class="tag">{{ m.type_name }}</span>
                  <span v-if="m.content_state !== 'ok'" class="tag orange">未解密</span>
                </div>
              </div>
            </div>
          </div>

          <div v-if="!messages.length && !loadingMsgs" class="empty">
            <div><div class="big">🗂</div>该会话还没有入库消息</div>
          </div>
          <div v-if="loadingMsgs" class="empty" style="min-height:60px">
            <i class="spin dark" style="display:inline-block;width:18px;height:18px"></i>
          </div>
        </div>
      </template>

      <div v-else class="empty" style="height:100%">
        <div>
          <div class="big">👈</div>
          从左侧选择一个会话查看聊天记录
        </div>
      </div>
    </section>

    <div v-if="error" class="toast alert err" @click="error = ''">{{ error }}</div>
  </div>
</template>

<style scoped>
.chat-page { display: flex; height: 100%; }

/* ---- 侧栏 ---- */
.sidebar {
  width: 336px; flex: none; display: flex; flex-direction: column;
  background: var(--panel); border-right: 1px solid var(--border); min-height: 0;
}
.staff-picker { position: relative; padding: 12px; border-bottom: 1px solid var(--border); }
.sp-trigger {
  display: flex; align-items: center; gap: 10px; padding: 8px 10px;
  border: 1px solid var(--border); border-radius: 9px; cursor: pointer; transition: .15s;
}
.sp-trigger:hover { border-color: #c9d2e3; background: var(--panel-2); }
.sp-text { flex: 1; min-width: 0; }
.sp-name { font-weight: 600; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sp-sub { font-size: 11px; }

.sp-panel {
  position: absolute; left: 12px; right: 12px; top: 62px; z-index: 30;
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  box-shadow: 0 12px 32px rgba(16, 24, 40, .14); padding: 8px;
}
.sp-list { max-height: 320px; overflow: auto; margin-top: 8px; }
.sp-item { display: flex; align-items: center; gap: 9px; padding: 7px 9px; border-radius: 8px; cursor: pointer; }
.sp-item:hover { background: var(--panel-2); }
.sp-item.active { background: var(--brand-weak); }
.sp-item-text { min-width: 0; }

.filters { padding: 10px 12px; display: grid; gap: 8px; border-bottom: 1px solid var(--border); }
.filter-row { display: flex; gap: 8px; align-items: center; }
.ck { display: flex; align-items: center; gap: 5px; font-size: 12px; color: var(--text-2); white-space: nowrap; cursor: pointer; }
.ck input { accent-color: var(--brand); }
.sess-meta { padding: 8px 14px; font-size: 12px; display: flex; justify-content: space-between; }

.sess-list { flex: 1; overflow: auto; min-height: 0; }
.sess {
  display: flex; gap: 10px; padding: 11px 13px; cursor: pointer;
  border-bottom: 1px solid #f2f4f8; transition: .12s;
}
.sess:hover { background: var(--panel-2); }
.sess.active { background: var(--brand-weak); box-shadow: inset 3px 0 0 var(--brand); }
.sess-main { flex: 1; min-width: 0; }
.sess-top { display: flex; align-items: baseline; gap: 8px; }
.sess-name { font-weight: 600; font-size: 13px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sess-time { font-size: 11px; flex: none; }
.sess-sub { display: flex; align-items: center; gap: 6px; font-size: 11.5px; margin-top: 3px; }

/* ---- 消息区 ---- */
.msg-pane { flex: 1; min-width: 0; display: flex; flex-direction: column; background: #eff2f7; }
.msg-head {
  flex: none; display: flex; align-items: center; gap: 11px; padding: 12px 18px;
  background: var(--panel); border-bottom: 1px solid var(--border);
}
.mh-text { flex: 1; min-width: 0; }
.mh-title { font-weight: 600; font-size: 14px; }
.mh-sub { font-size: 11.5px; margin-top: 3px; }
.mh-actions { display: flex; gap: 7px; align-items: center; }

.msg-scroll { flex: 1; overflow: auto; padding: 16px 22px 26px; min-height: 0; }

.load-more { text-align: center; padding: 6px 0 14px; font-size: 12px; }
.day-sep { text-align: center; margin: 14px 0; }
.day-sep span {
  display: inline-block; padding: 3px 12px; background: rgba(31, 36, 48, .07);
  color: var(--text-2); border-radius: 99px; font-size: 11.5px;
}

.row { display: flex; gap: 10px; margin: 14px 0; align-items: flex-start; }
.row.send { flex-direction: row-reverse; }
.bubble-col { max-width: min(66%, 720px); display: flex; flex-direction: column; }
.row.send .bubble-col { align-items: flex-end; }

.bubble-meta { font-size: 11.5px; color: var(--text-3); margin-bottom: 4px; display: flex; gap: 7px; }
.row.send .bubble-meta { flex-direction: row-reverse; }

.bubble {
  padding: 9px 13px; border-radius: 10px; font-size: 13.5px; line-height: 1.72;
  word-break: break-word; background: #fff; box-shadow: 0 1px 2px rgba(16, 24, 40, .06);
}
.bubble.recv { border-top-left-radius: 3px; }
.bubble.send { background: var(--brand); color: #fff; border-top-right-radius: 3px; }
.bubble.send a, .bubble.send .muted { color: rgba(255, 255, 255, .82); }
.bubble.kind-2, .bubble.kind-3 { background: transparent; box-shadow: none; padding: 0; }
.bubble.kind-2.send, .bubble.kind-3.send { background: transparent; }

.msg-img { max-width: 260px; max-height: 320px; border-radius: 9px; display: block; box-shadow: var(--shadow); }
.msg-emoji { width: 76px; height: 76px; object-fit: contain; }
.media-card {
  display: inline-flex; align-items: center; gap: 8px; padding: 7px 11px;
  border-radius: 8px; background: rgba(0, 0, 0, .04); font-size: 13px;
}
.bubble.send .media-card { background: rgba(255, 255, 255, .18); color: #fff; }

.bubble-foot { margin-top: 5px; display: flex; gap: 6px; font-size: 11px; }
.row.send .bubble-foot { flex-direction: row-reverse; }

.toast {
  position: fixed; left: 50%; bottom: 26px; transform: translateX(-50%);
  z-index: 99; cursor: pointer; box-shadow: 0 8px 24px rgba(16, 24, 40, .16);
}
</style>
