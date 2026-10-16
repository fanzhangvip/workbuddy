<script setup>
/**
 * 登录页 —— 取代浏览器的**原生 Basic 弹框**（那个样式不可控、不能记住、看不出是谁）。
 *
 * 与「数据源凭据」页的区别（别混）：这一页登的是**本系统账号**；
 * 那一页是让微伴/企微的抓数凭据（扫码）保持新鲜。
 */
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuth } from '../composables/useAuth'

const router = useRouter()
const route = useRoute()
const { refresh, signIn, isAuthed } = useAuth()

// 「记住我」= 记住**用户名** + 30 天会话（口令不落前端存储，浏览器只留 HttpOnly 的会话）
const SAVED_USER_KEY = 'wb_login_user'
const username = ref(localStorage.getItem(SAVED_USER_KEY) || '')
const password = ref('')
const remember = ref(!!localStorage.getItem(SAVED_USER_KEY))
const busy = ref(false)
const error = ref('')

onMounted(async () => {
  // 已经登录（比如手敲了 #/login）→ 直接进应用，不要让人再登一遍
  await refresh()
  if (isAuthed.value) router.replace(redirectTarget())
})

function redirectTarget() {
  const to = String(route.query.to || '')
  return to && to !== '/login' ? to : '/chat'
}

async function submit(ev) {
  if (busy.value) return
  error.value = ''
  // ⚠️ **以表单 DOM 的实际值为准**，不能只用 `username.value`：
  // 浏览器的密码管理器（Chrome 自动填充 / 1Password 等）会直接设 `input.value`，
  // 只发 `change` **不发 `input`** —— 而 `v-model` 监听的是 `input`。
  // 结果就是「输入框里明明有 admin，提交却说『请输入用户名』」（用户实测踩到）。
  // DOM 才是用户看得见的东西，提交时读它，顺手把 ref 同步回来（记住我要存它）。
  const form = ev?.target
  const u = String(form?.elements?.username?.value ?? username.value ?? '').trim()
  const p = String(form?.elements?.password?.value ?? password.value ?? '')
  username.value = u
  password.value = p
  if (!u) { error.value = '请输入用户名'; return }
  if (!p) { error.value = '请输入口令'; return }
  busy.value = true
  try {
    await signIn(u, p, remember.value)
    if (remember.value) localStorage.setItem(SAVED_USER_KEY, u)
    else localStorage.removeItem(SAVED_USER_KEY)
    password.value = ''                       // 用完立刻从内存里清掉
    router.replace(redirectTarget())
  } catch (e) {
    error.value = e?.response?.status === 401
      ? '用户名或口令不正确'
      : (e?.response?.data?.reason || '登录失败：' + (e?.message || '网络异常'))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <form class="card login-card" @submit.prevent="submit">
      <div class="brand">
        <span class="logo-mark">会</span>
        <div>
          <div class="brand-name">会话存档管理台</div>
          <div class="muted" style="font-size:12px">企业微信客户意向评分系统</div>
        </div>
      </div>

      <label class="lbl">用户名</label>
      <!-- `name` 不是摆设：密码管理器靠它识别字段，缺失时自动填充往往不生效/不提示保存 -->
      <input v-model="username" class="input" name="username" autocomplete="username"
             placeholder="admin" autofocus />

      <label class="lbl" style="margin-top:12px">口令</label>
      <input v-model="password" class="input" name="password" type="password"
             autocomplete="current-password" placeholder="部署时 .env 里的 API_PASSWORD" />

      <label class="remember">
        <input type="checkbox" v-model="remember" />
        <span class="rm-text">记住我</span>
        <span class="muted rm-hint">（这台机器 30 天内免登录；口令不会保存在浏览器里）</span>
      </label>

      <div v-if="error" class="alert err" style="margin-top:12px">{{ error }}</div>

      <button type="submit" class="btn primary lg" style="width:100%;margin-top:14px"
              :disabled="busy">
        <i v-if="busy" class="spin"></i>{{ busy ? '登录中…' : '登录' }}
      </button>

      <div class="muted" style="font-size:11.5px;line-height:1.7;margin-top:12px">
        口令见部署机 <span class="mono">.env</span> 的
        <span class="mono">API_PASSWORD</span>（或后端启动日志打印的那一串）；
        多账号用 <span class="mono">API_USERS=用户:口令:角色,…</span>。
        <br />脚本 / curl 仍可直接用 HTTP Basic，不必先登录。
      </div>
    </form>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh; display: grid; place-items: center;
  background: radial-gradient(1200px 600px at 50% -10%, #eaf1ff 0%, var(--bg) 60%);
  padding: 24px;
}
.login-card { width: 380px; max-width: 100%; padding: 26px 24px 20px; }
.brand { display: flex; align-items: center; gap: 10px; margin-bottom: 20px; }
.brand-name { font-weight: 650; font-size: 15px; }
.remember {
  display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 14px;
  font-size: 12.5px; color: var(--text-2); cursor: pointer; user-select: none;
}
.remember .rm-text { white-space: nowrap; }
.remember .rm-hint { font-size: 11.5px; flex: 1 1 100%; margin-left: 22px; }
</style>
