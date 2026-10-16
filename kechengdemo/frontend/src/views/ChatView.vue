<script setup>
import { ref, computed, watch, onMounted, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import {
  api, fmtClock, fmtRelative, dayLabel,
  isInlineImage, mediaCard, mediaKindOf, mediaText,
} from '../api'
import { useLoadFailures } from '../composables/loadFailures'
import PartialLoadBanner from '../components/PartialLoadBanner.vue'

const route = useRoute()

// 「局部加载失败」统一收口（见 composables/loadFailures.js）：
// 次要数据源（员工列表/深链单查）失败不再静默成"空数据"。
const { failures, note: noteFail, clear: clearFail } = useLoadFailures()

const PAGE_SIZE = 60

const staffs = ref([])
const staffId = ref('')
const staffQ = ref('')
const staffOpen = ref(false)

const sessions = ref([])
const sessionTotal = ref(0)
// 同类型 + 同关键词、但**忽略「只看已同步」**的总数（接口的 `total_all`）。
// 用来区分"真没有会话"与"有会话但一条都没同步消息"—— 后者若还说"暂无会话"，
// 就会把"没同步"误报成"没数据"（实测就是这么被误判的）。
const sessionTotalAll = ref(0)
const sessionOffset = ref(0)
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
const msg = ref('')
const rescoring = ref(false)          // 「重新评分这个会话」按钮的忙态

// ---------------- 员工 ----------------
async function loadStaffs() {
  try {
    // `session_type` 只影响下拉里的**会话计数**（与右侧列表口径一致），不影响谁在列表里
    const r = await api.staffs({ only_with_session: 1, limit: 800,
                                 session_type: sessionType.value })
    staffs.value = r.items || []
    if (!staffId.value && staffs.value.length) staffId.value = staffs.value[0].staff_id
    clearFail('staffs')
  } catch (e) {
    // 不再静默：员工下拉拉不到时，页面顶部会出现一条可重试的横幅
    noteFail('staffs', '员工列表', e)
  }
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
      q: sessionQ.value.trim(), has_msg: onlyMsg.value ? 1 : 0,
      limit: 300, offset: 0,
    })
    sessions.value = r.items || []
    sessionTotal.value = r.total || 0
    sessionTotalAll.value = r.total_all ?? r.total ?? 0
    sessionOffset.value = (r.items || []).length
  } catch (e) { error.value = '加载会话失败：' + e.message }
  loadingSessions.value = false
}

// 「加载更多」：会话列表原来写死 limit=300 且没有分页 —— 对实测那位 2 万会话的员工，
// 第 301 条以后**永远看不到**，只能靠搜索碰运气。接口本来就支持 offset。
async function loadMoreSessions() {
  if (!staffId.value || loadingSessions.value) return
  loadingSessions.value = true
  try {
    const r = await api.sessions({
      staff_id: staffId.value, session_type: sessionType.value,
      q: sessionQ.value.trim(), has_msg: onlyMsg.value ? 1 : 0,
      limit: 300, offset: sessionOffset.value,
    })
    sessions.value = [...sessions.value, ...(r.items || [])]
    sessionOffset.value = sessions.value.length
    sessionTotal.value = r.total || 0
  } catch (e) { error.value = '加载更多会话失败：' + e.message }
  loadingSessions.value = false
}

//: 空列表的三种成因（修"没数据显示成'暂无会话'"）
const emptyKind = computed(() => {
  if (sessions.value.length || loadingSessions.value) return ''
  if (sessionTotalAll.value === 0) {
    return sessionQ.value.trim() ? 'no_match' : 'no_session'
  }
  return onlyMsg.value ? 'none_synced' : 'no_match'   // 有会话但被"只看已同步"滤掉了
})

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
// 会话类型变了 → 员工下拉里的"会话数"也要跟着换成同类型的口径
watch(sessionType, () => { if (ready) loadStaffs() })

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
    if (!reset && messages.value.length) {
      // 复合游标（修"加载更早会丢消息"）：`msg_time` 是**秒级**的，同一秒多条消息很常见
      // （实测 804 个有正文的会话里 636 个存在同秒多条），只传秒会让边界那一秒里更早的
      // 几条被跳过。带上 message_id 后与后端的 `(msg_time, message_id) <` 对上。
      params.before_time = messages.value[0].msg_time
      params.before_id = messages.value[0].message_id
    }
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
// 前端只负责展示 —— 「哪个类型出图、哪个出卡片」的分类规则**不在这里**，
// 统一在 `api.js` 的 mediaKindOf / isInlineImage / mediaCard（两个渲染处共用一个知识）。
function mediaKind(m) {
  return m.is_media && m.media_url && !m._imgFail ? 'kind-' + mediaKindOf(m) : ''
}
function msgText(m) {
  // ⚠️ 中文名一律用接口下发的 `type_name`（唯一事实源 `ww_chatdata.TYPE_NAME`），
  // 前端不要再抄一份 msgtype → 中文名映射。
  if (m.is_media) return mediaText(m, !!m.media_url)
  return m.content || '〔正文未取到〕'
}
function imgFail(m) { m._imgFail = true }

function sessionLabel(s) {
  return s.remark || s.name || '（无备注）'
}

/**
 * 深链兜底：`/chat?staff=xxx&session=yyy`（总览页点搜索结果 / 外部分享的链接）。
 *
 * 抽成函数是为了能**重复调用** —— 顶部横幅上的「重试」会再走一遍这一整段；
 * 写死在 `onMounted` 里的话，深链失败后唯一的出路就是刷新整个页面。
 */
async function openFromQuery() {
  // ⚠️ 不再要求「该员工必须出现在前 800 个员工里」—— 深链的目标是明确的 staff_id，
  //    员工下拉只是展示层，缺了它不该让整条深链失效。
  const qStaff = route.query.staff
  if (qStaff) staffId.value = qStaff
  await loadSessions()
  const qSession = route.query.session
  if (!qSession) return
  let found = sessions.value.find(s => s.session_id === qSession)
  if (!found) {
    // ⚠️ 深链目标**不一定在首屏 300 条里**（实测：某员工 20058 个会话，目标排第 320）
    //    → 改造前这里只 `find()` 一次，找不到就**什么都不发生** ——
    //    用户在总览页点一条搜索结果，跳到本页却看不到任何反应。
    //    现在按 (staff_id, session_id) 单查一次，并把它插进列表（否则左侧不会高亮）。
    try {
      const one = await api.sessionDetail({ staff_id: staffId.value, session_id: qSession })
      if (one && one.session_id) {
        sessions.value = [one, ...sessions.value]
        msg.value = '该会话不在最近 300 条里，已按会话 id 单独打开'
        clearFail('deeplink')
      }
    } catch (e) {
      // 单查失败：技术原因（HTTP 状态）留在横幅上给运维看，用户真正的结论是下面那句 error
      noteFail('deeplink', '会话单查', e)
    }
    found = sessions.value.find(s => s.session_id === qSession)
  }
  if (found) await openSession(found)
  else error.value = '打不开这个会话：它不在当前员工的历史里，或还没同步到本地'
}

/** 横幅上的「重试」：本页两个**局部**数据源都再走一遍。
 *
 * 只重试其中一个的话，另一个的失败记录会一直挂在横幅上（而横幅写的就是它）。
 * 主数据（会话/消息列表）不走这里 —— 它们失败时是 `error`，见 `loadSessions`。
 */
async function retryFailed() {
  await loadStaffs()
  if (route.query.session) await openFromQuery()
}

/** 对**这一个会话**立即重评（`POST /api/scoring/session`）。
 *
 * 为什么在这里：看到一段对话觉得"判得不对"时，最想要的是"就按现在这些消息重新判一次"，
 * 而不是回评分中心跑一整批。后端这个端点一直存在（`api.scoringSession` 也早就有封装），
 * 但**没有任何界面入口** —— 想用只能 curl。
 */
async function rescoreSession() {
  const s = current.value
  if (!s) return
  if (!window.confirm(
    `重新评分「${sessionLabel(s)}」？\n\n`
    + '· 只评这一个会话（不跑整批），会消耗一次模型调用；\n'
    + '· 结论会覆盖该客户当前有效的那一条评估（历史保留）；\n'
    + '· 若新的分数触发了「需人工确认」，回写会进评分中心的审批队列。\n\n确定吗？')) return
  rescoring.value = true
  msg.value = ''
  try {
    const r = await api.scoringSession(s.staff_id, s.session_id)
    if (!r.ok) { error.value = r.reason || '重新评分失败'; return }
    // 返回形状是 `{ok, assessment}`（见 scoring_service.score_session）
    const a = r.assessment || {}
    msg.value = `已重评：${a.grade || '-'}｜原始分 ${a.raw_score ?? '-'}`
      + (a.writeback_status ? `｜回写 ${a.writeback_status}` : '')
      + (a.review_status && a.review_status !== 'na' ? '｜已进复核队列' : '')
    await loadMessages(true)
  } catch (e) {
    // 403 = 当前账号角色不够（viewer 只读）—— 说清楚，别让人以为是"坏了"
    error.value = (e?.response?.status === 403 ? '当前账号没有执行评分的权限（需要 operator/admin）：' : '')
      + (e?.message || '重新评分失败')
  } finally {
    rescoring.value = false
  }
}

onMounted(async () => {
  await loadStaffs()
  await openFromQuery()
  ready = true
})
</script>

<template>
  <div class="page flush chat-page">
    <!-- ============ 左侧：员工 + 会话列表 ============ -->
    <aside class="sidebar">
      <!-- 局部加载失败（员工列表 / 深链单查）→ 明说，而不是让页面空着 -->
      <PartialLoadBanner :failures="failures" @retry="retryFailed" />
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
        <div class="inline-row">
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
              <span v-if="s.msg_count && !s.decoded_count" class="tag red"
                    style="margin-left:4px" title="有消息行但一条正文都没解出来（企微解密通道）">
                正文未解密
              </span>
            </div>
          </div>
        </div>
        <!-- 空状态**三态**（修 2026-10-16）：以前列表一空就写「该员工暂无会话」，
             而实测那位员工有 20058 个会话、只是**一条消息都没同步** ——
             把"没同步"说成"没数据"，正是这次"看不到数据"被误判的原因。 -->
        <div v-if="emptyKind" class="empty">
          <div v-if="emptyKind === 'no_session'">
            <div class="big">💬</div>
            该员工没有这类会话<br /><span style="font-size:12px">可换会话类型，或到「同步中心」拉取数据</span>
          </div>
          <div v-else-if="emptyKind === 'none_synced'">
            <div class="big">📥</div>
            有 <b>{{ sessionTotalAll }}</b> 个会话，但<b>一条消息都还没同步</b><br />
            <span style="font-size:12px">右侧为空是因为库里还没有聊天记录 —— 不是页面坏了</span>
            <div style="margin-top:10px">
              <button class="btn sm" @click="onlyMsg = false">
                显示全部 {{ sessionTotalAll }} 个会话
              </button>
            </div>
          </div>
          <div v-else>
            <div class="big">🔍</div>
            没有匹配「{{ sessionQ.trim() }}」的会话<br />
            <span style="font-size:12px">换个关键词，或清空搜索框</span>
          </div>
        </div>
        <!-- 会话列表原来是写死 300 条且没有分页：2 万会话的员工第 301 条以后永远看不到 -->
        <div v-if="sessions.length && sessions.length < sessionTotal" class="load-more">
          <button class="btn sm" :disabled="loadingSessions" @click="loadMoreSessions">
            <i v-if="loadingSessions" class="spin"></i>
            加载更多（已显示 {{ sessions.length }} / {{ sessionTotal }}）
          </button>
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
            <!-- 单会话重评：`POST /api/scoring/session` 一直存在，此前只能 curl -->
            <button class="btn sm" :disabled="rescoring" title="只按当前消息重评这一个会话"
                    @click="rescoreSession">
              <i v-if="rescoring" class="spin"></i>{{ rescoring ? '重评中…' : '重新评分' }}
            </button>
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
                  <!-- 图片 / 表情：直接出图 -->
                  <template v-if="isInlineImage(m) && m.media_url && !m._imgFail">
                    <a :href="m.media_url" target="_blank" rel="noreferrer">
                      <img :class="mediaKindOf(m) === 'emoji' ? 'msg-emoji' : 'msg-img'"
                           :src="m.media_url" :alt="m.type_name || '媒体'" @error="imgFail(m)" />
                    </a>
                  </template>
                  <!-- 语音 / 视频 / 文件：可点卡片（图标与动作词见 api.js 的 mediaCard） -->
                  <template v-else-if="m.is_media && m.media_url">
                    <a class="media-card" :href="m.media_url" target="_blank" rel="noreferrer">
                      {{ mediaCard(m).icon }} {{ mediaCard(m).name }}
                      <span class="muted">{{ mediaCard(m).action }}</span>
                    </a>
                  </template>
                  <!-- 文本 / 其它 / 媒体无链接的回退 -->
                  <template v-else>
                    <span style="white-space:pre-wrap">{{ msgText(m) }}</span>
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
    <div v-if="msg" class="toast alert ok" @click="msg = ''">{{ msg }}</div>
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
/* ⚠️ 类名必须与模板绑定的**同一个值**：`<div class="bubble" :class="[m.direction, …]">`，
   而 `direction` 的取值是 `send` / `receive`（后端下发）。这里原来写 `.recv` ——
   永远匹配不上，于是「接收气泡左上角收个小圆角」这条样式**一直没生效**。 */
.bubble.receive { border-top-left-radius: 3px; }
.bubble.send { background: var(--brand); color: #fff; border-top-right-radius: 3px; }
.bubble.send a, .bubble.send .muted { color: rgba(255, 255, 255, .82); }
.bubble.kind-image, .bubble.kind-emoji { background: transparent; box-shadow: none; padding: 0; }
.bubble.kind-image.send, .bubble.kind-emoji.send { background: transparent; }

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
