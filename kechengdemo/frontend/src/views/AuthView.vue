<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api'

const status = ref(null)
const verifying = ref(false)
const verifyResult = ref(null)
const starting = ref(false)
const localError = ref('')
let timer = null

const STAGES = [
  { key: 'launching', text: '启动浏览器' },
  { key: 'waiting_login', text: '等待扫码登录' },
  { key: 'capturing', text: '抓取凭据' },
  { key: 'done', text: '完成' },
]

async function load() {
  try { status.value = await api.authStatus() } catch (e) { localError.value = '后端未就绪：' + e.message }
}

const login = computed(() => status.value?.login || {})
const stageIndex = computed(() => {
  const i = STAGES.findIndex(s => s.key === login.value.stage)
  return login.value.stage === 'failed' ? -1 : i
})

const health = computed(() => {
  const s = status.value
  if (!s?.has_file) return { cls: 'err', title: '尚未登录', desc: '还没有抓取到凭据，点击下方按钮弹出浏览器扫码登录。' }
  if (s.expired) return { cls: 'warn', title: '登录态可能已过期', desc: `凭据获取于 ${s.age_hours} 小时前，微伴登录态约 7 天有效，建议重新登录。` }
  return { cls: 'ok', title: '凭据正常', desc: `获取于 ${s.age_hours} 小时前（open_sid …${s.open_sid_tail}）` }
})

async function startLogin() {
  starting.value = true
  localError.value = ''
  verifyResult.value = null
  try {
    const r = await api.authLogin({ wait_sec: 300 })
    if (!r.ok) localError.value = r.reason || '启动失败'
  } catch (e) { localError.value = e.message }
  starting.value = false
  load()
}

async function doVerify() {
  verifying.value = true
  verifyResult.value = null
  try { verifyResult.value = await api.authVerify() } catch (e) { verifyResult.value = { ok: false, error: e.message } }
  verifying.value = false
  load()
}

onMounted(() => { load(); timer = setInterval(load, 3000) })
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="page">
    <div style="max-width:860px;margin:0 auto;display:grid;gap:16px">

      <div class="card">
        <div class="card-head">凭据状态</div>
        <div class="card-body">
          <div class="alert" :class="health.cls" style="margin-bottom:16px">
            <strong>{{ health.title }}</strong><br />{{ health.desc }}
          </div>

          <table class="tbl">
            <tbody>
              <tr><th style="width:180px">凭据文件</th>
                  <td class="mono">{{ status?.has_file ? '.cache/ww_bootstrap.json' : '不存在' }}</td></tr>
              <tr><th>open_sid</th><td class="mono">{{ status?.open_sid_tail ? '…' + status.open_sid_tail : '-' }}</td></tr>
              <tr><th>获取时间</th>
                  <td>{{ status?.captured_at ? new Date(status.captured_at * 1000).toLocaleString() : '-' }}</td></tr>
              <tr><th>已过时长</th><td>{{ status?.age_hours ?? '-' }} 小时</td></tr>
              <tr><th>数据库登记</th>
                  <td><span class="tag" :class="status?.db_status === 'ok' ? 'green' : 'orange'">{{ status?.db_status }}</span></td></tr>
            </tbody>
          </table>

          <div style="display:flex;gap:10px;margin-top:18px;flex-wrap:wrap">
            <button class="btn primary lg" :disabled="starting || login.running" @click="startLogin">
              <i v-if="starting || login.running" class="spin"></i>
              {{ login.running ? '登录流程进行中…' : '弹出浏览器扫码登录' }}
            </button>
            <button class="btn lg" :disabled="verifying || !status?.has_file" @click="doVerify">
              <i v-if="verifying" class="spin dark"></i>
              校验凭据是否有效
            </button>
          </div>

          <div v-if="localError" class="alert err" style="margin-top:14px">{{ localError }}</div>

          <div v-if="verifyResult" class="alert" :class="verifyResult.ok ? 'ok' : 'err'" style="margin-top:14px">
            <template v-if="verifyResult.ok">
              通道正常：签名 → 开会话（sid …{{ verifyResult.sid_tail }}）→ 解密
              {{ verifyResult.decrypted }}/{{ verifyResult.sample_msgs }} 条样例消息正文。
            </template>
            <template v-else>校验失败：{{ verifyResult.error }}</template>
          </div>
        </div>
      </div>

      <div class="card" v-if="login.running || login.stage !== 'idle'">
        <div class="card-head">登录进度</div>
        <div class="card-body">
          <div class="steps">
            <div v-for="(s, i) in STAGES" :key="s.key" class="step"
                 :class="{ done: stageIndex > i || login.stage === 'done', active: stageIndex === i }">
              <span class="step-dot"></span>
              <span>{{ s.text }}</span>
            </div>
          </div>
          <div class="alert" :class="login.stage === 'failed' ? 'err' : login.stage === 'done' ? 'ok' : 'info'"
               style="margin-top:14px">
            {{ login.message }}
            <div v-if="login.error" class="mono" style="margin-top:6px">{{ login.error }}</div>
          </div>
          <div v-if="login.running" class="alert warn" style="margin-top:12px">
            浏览器窗口已弹出，请在窗口内完成登录（企业微信扫码）。<br />
            若长时间停在此步骤，请确认弹出的窗口未被最小化，并已完成扫码。
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">为什么需要「扫码登录」这一步？</div>
        <div class="card-body" style="line-height:1.9;color:var(--text-2)">
          <p style="margin:0 0 10px">
            抓取聊天明文需要两把凭据，它们<b>只能从浏览器里拿到</b>，所以需要你登录一次：
          </p>
          <ol style="margin:0 0 12px;padding-left:20px">
            <li><b>微伴登录态 cookie</b> —— 用来实时生成 <span class="mono">jsapi_sign</span> 签名；</li>
            <li><b>企微 <span class="mono">wwopen.open.sid</span> cookie</b> —— 即组件的 <span class="mono">open_sid</span>，
                由企业微信下发。</li>
          </ol>
          <p style="margin:0">
            抓到的凭据会缓存在 <span class="mono">.cache/ww_bootstrap.json</span>，之后<b>所有同步都是纯 HTTP</b>，
            不再需要浏览器。微伴登录态约 7 天有效，过期后回到本页重新登录一次即可。
          </p>
        </div>
      </div>

    </div>
  </div>
</template>

<style scoped>
.steps { display: flex; gap: 8px; flex-wrap: wrap; }
.step {
  display: flex; align-items: center; gap: 7px; padding: 7px 13px;
  border: 1px solid var(--border); border-radius: 99px; font-size: 12.5px; color: var(--text-3);
}
.step-dot { width: 8px; height: 8px; border-radius: 50%; background: #d3d8e3; }
.step.active { border-color: var(--brand); color: var(--brand); background: var(--brand-weak); }
.step.active .step-dot { background: var(--brand); }
.step.done { color: #0b8a4f; border-color: #c6efdb; background: #f2fdf7; }
.step.done .step-dot { background: var(--ok); }
</style>
