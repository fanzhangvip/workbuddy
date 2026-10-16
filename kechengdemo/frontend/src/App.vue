<script setup>
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, onUnauthorized } from './api'
import { useAuth } from './composables/useAuth'
import { usePolling } from './composables/usePolling'

const cred = ref(null)
const route = useRoute()
const router = useRouter()
const { user, roleText, authEnabled, signOut } = useAuth()

// 会话失效（任一接口 401）→ **就地**送回登录页。
// 为什么必须在这里挂：路由守卫只在导航时跑，会话过期不会触发导航 ——
// 用户停在某个页面上会看到"接口全 401、页面全空"，但没有任何跳转，
// 观感就是「服务挂了」（实测发生过）。带上原地址，登录完直接回去。
onUnauthorized(() => {
  if (route.path === '/login') return
  router.replace({ name: 'login', query: route.fullPath ? { to: route.fullPath } : {} })
})

// 登录页不显示顶栏（那一页自己就是一屏）
const showChrome = computed(() => route.path !== '/login')

async function doSignOut() {
  await signOut()
  router.replace({ name: 'login' })
}

async function load() {
  try { cred.value = await api.authStatus() } catch (e) { /* 后端未启动时静默 */ }
}

usePolling(load, 15000)

const credState = computed(() => {
  const c = cred.value
  // ⚠️ 这里说的是**微伴/企微数据源凭据**，不是「你有没有登录本系统」——
  //    两件事都在页面上出现过一次「登录」，措辞必须明确，否则会被当成没登录。
  if (!c || !c.has_file) return { cls: 'bad', text: '数据源凭据未建立' }
  if (c.expired) return { cls: 'warn', text: '数据源凭据可能已过期' }
  return { cls: 'ok', text: `数据源凭据正常 · ${c.age_hours}h 前获取` }
})
</script>

<template>
  <div class="app">
    <header class="topbar" v-if="showChrome">
      <div class="logo">
        <span class="logo-mark">会</span>
        <span>会话存档管理台</span>
      </div>
      <nav class="nav">
        <router-link to="/dashboard">总览</router-link>
        <router-link to="/chat">会话管理</router-link>
        <router-link to="/score">评分中心</router-link>
        <router-link to="/review">负分复核</router-link>
        <router-link to="/eval">评估集</router-link>
        <router-link to="/sync">同步中心</router-link>
        <router-link to="/auth">数据源凭据</router-link>
      </nav>
      <div class="topbar-right">
        <span class="muted" style="font-size:12px;display:flex;align-items:center;gap:6px">
          <i class="dot" :class="credState.cls"></i>{{ credState.text }}
        </span>
        <!-- 当前登录用户 + 退出（未启用鉴权时不显示，免得让人以为需要登录） -->
        <span v-if="authEnabled && user" class="muted"
              style="font-size:12px;display:flex;align-items:center;gap:8px">
          <span class="tag">{{ user }}<template v-if="roleText"> · {{ roleText }}</template></span>
          <button class="btn sm" @click="doSignOut">退出</button>
        </span>
      </div>
    </header>
    <router-view />
  </div>
</template>
