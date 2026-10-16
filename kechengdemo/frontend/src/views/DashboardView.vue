<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api, fmtTime, fmtRelative } from '../api'

const router = useRouter()
const stats = ref({})
const recent = ref([])
const topStaffs = ref([])
const q = ref('')
const results = ref([])
const searching = ref(false)

async function load() {
  try {
    const [s, r, st] = await Promise.all([
      api.stats(),
      api.recentSessions({ limit: 20, session_type: 'ext_user' }),
      api.staffs({ only_with_session: 1, limit: 10 }),
    ])
    stats.value = s
    recent.value = r.items || []
    topStaffs.value = st.items || []
  } catch (e) { /* 后端未就绪 */ }
}

async function doSearch() {
  if (!q.value.trim()) { results.value = []; return }
  searching.value = true
  try {
    const r = await api.search({ q: q.value.trim(), limit: 40 })
    results.value = r.items || []
  } catch (e) { results.value = [] }
  searching.value = false
}

function go(staffId, sessionId) {
  router.push({ path: '/chat', query: { staff: staffId, session: sessionId } })
}

const rate = () => {
  const t = stats.value.msg_count || 0
  return t ? Math.round(((stats.value.decrypted_count || 0) / t) * 100) : 0
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div style="display:grid;gap:16px">

      <div class="grid-stats">
        <div class="card stat">
          <div class="label">员工总数</div>
          <div class="value">{{ stats.staff_count ?? '-' }}</div>
          <div class="sub">{{ stats.staff_with_session ?? 0 }} 名已有会话数据</div>
        </div>
        <div class="card stat">
          <div class="label">会话总数</div>
          <div class="value">{{ (stats.session_count ?? 0).toLocaleString() }}</div>
          <div class="sub">含客户 / 群聊 / 同事</div>
        </div>
        <div class="card stat">
          <div class="label">消息总数</div>
          <div class="value">{{ (stats.msg_count ?? 0).toLocaleString() }}</div>
          <div class="sub">已解密 {{ (stats.decrypted_count ?? 0).toLocaleString() }} 条（{{ rate() }}%）</div>
        </div>
        <div class="card stat">
          <div class="label">最新消息</div>
          <div class="value" style="font-size:17px">{{ stats.latest_msg_time ? fmtTime(stats.latest_msg_time) : '-' }}</div>
          <div class="sub">{{ stats.latest_msg_time ? fmtRelative(stats.latest_msg_time) : '尚未同步' }}</div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">全文检索<span class="muted" style="font-weight:400;font-size:12px">按消息正文模糊匹配</span></div>
        <div class="card-body">
          <div style="display:flex;gap:10px">
            <input v-model="q" class="input" placeholder="输入关键词，例如：报名 / 价格 / 试听 …" @keyup.enter="doSearch" />
            <button class="btn primary" :disabled="searching" @click="doSearch">
              <i v-if="searching" class="spin"></i>搜索
            </button>
          </div>

          <div v-if="results.length" class="results">
            <div v-for="r in results" :key="r.message_id" class="hit" @click="go(r.staff_id, r.session_id)">
              <div class="hit-top">
                <span class="tag blue">{{ r.staff_name || r.staff_id }}</span>
                <span class="tag">{{ r.session_name || r.session_id }}</span>
                <span class="muted" style="margin-left:auto;font-size:12px">{{ fmtTime(r.msg_time) }}</span>
              </div>
              <div class="hit-body">{{ r.content }}</div>
            </div>
          </div>
          <div v-else-if="q && !searching" class="muted" style="margin-top:12px;font-size:13px">没有匹配的消息</div>
        </div>
      </div>

      <div class="two-col">
        <div class="card">
          <div class="card-head">最近活跃会话</div>
          <div style="max-height:420px;overflow:auto">
            <table class="tbl">
              <thead><tr><th>客户</th><th>所属员工</th><th>消息</th><th>最后活跃</th></tr></thead>
              <tbody>
                <tr v-for="s in recent" :key="s.session_id" style="cursor:pointer" @click="go(s.staff_id, s.session_id)">
                  <td><b>{{ s.remark || s.name }}</b><div class="muted" style="font-size:11.5px">{{ s.name }}</div></td>
                  <td class="muted" style="font-size:12px">{{ s.staff_name }}</td>
                  <td class="mono">{{ s.msg_count }}</td>
                  <td class="muted" style="font-size:12px">{{ fmtRelative(s.last_msg_time) }}</td>
                </tr>
                <tr v-if="!recent.length"><td colspan="4" class="muted" style="text-align:center;padding:24px">
                  暂无数据，请先到「同步中心」拉取</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="card">
          <div class="card-head">会话最多的员工</div>
          <div style="max-height:420px;overflow:auto">
            <table class="tbl">
              <thead><tr><th>员工</th><th>会话</th><th>消息</th><th>最近活跃</th></tr></thead>
              <tbody>
                <tr v-for="s in topStaffs" :key="s.staff_id" style="cursor:pointer"
                    @click="router.push({ path: '/chat', query: { staff: s.staff_id } })">
                  <td><b>{{ s.name }}</b></td>
                  <td class="mono">{{ s.session_count }}</td>
                  <td class="mono">{{ s.msg_count || 0 }}</td>
                  <td class="muted" style="font-size:12px">{{ fmtRelative(s.last_msg_time) }}</td>
                </tr>
                <tr v-if="!topStaffs.length"><td colspan="4" class="muted" style="text-align:center;padding:24px">暂无数据</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

    </div>
  </div>
</template>

<style scoped>
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 1100px) { .two-col { grid-template-columns: 1fr; } }

.results { margin-top: 14px; display: grid; gap: 8px; max-height: 380px; overflow: auto; }
.hit {
  padding: 10px 12px; border: 1px solid var(--border); border-radius: 9px;
  cursor: pointer; transition: .15s; background: var(--panel);
}
.hit:hover { border-color: #c9d2e3; background: var(--panel-2); }
.hit-top { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
/* 消息正文可能自带换行（如多行的直播宣传文案）→ 必须保留换行，
   否则会糊成一整段、看不出原本的分行结构 */
.hit-body { font-size: 13px; line-height: 1.65; color: var(--text-2); white-space: pre-wrap; }
</style>
