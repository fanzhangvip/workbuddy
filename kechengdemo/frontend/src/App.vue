<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from './api'

const cred = ref(null)
let timer = null

async function load() {
  try { cred.value = await api.authStatus() } catch (e) { /* 后端未启动时静默 */ }
}

onMounted(() => { load(); timer = setInterval(load, 15000) })
onUnmounted(() => clearInterval(timer))

const credState = computed(() => {
  const c = cred.value
  if (!c || !c.has_file) return { cls: 'bad', text: '未登录' }
  if (c.expired) return { cls: 'warn', text: '登录态可能已过期' }
  return { cls: 'ok', text: `凭据正常 · ${c.age_hours}h 前获取` }
})
</script>

<template>
  <div class="app">
    <header class="topbar">
      <div class="logo">
        <span class="logo-mark">会</span>
        <span>会话存档管理台</span>
      </div>
      <nav class="nav">
        <router-link to="/dashboard">总览</router-link>
        <router-link to="/chat">会话管理</router-link>
        <router-link to="/score">评分中心</router-link>
        <router-link to="/review">负分复核</router-link>
        <router-link to="/sync">同步中心</router-link>
        <router-link to="/auth">登录与凭据</router-link>
      </nav>
      <div class="topbar-right">
        <span class="muted" style="font-size:12px;display:flex;align-items:center;gap:6px">
          <i class="dot" :class="credState.cls"></i>{{ credState.text }}
        </span>
      </div>
    </header>
    <router-view />
  </div>
</template>
