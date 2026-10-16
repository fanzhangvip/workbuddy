import { createRouter, createWebHashHistory } from 'vue-router'
import DashboardView from './views/DashboardView.vue'
import ChatView from './views/ChatView.vue'
import SyncView from './views/SyncView.vue'
import AuthView from './views/AuthView.vue'
import ScoreView from './views/ScoreView.vue'
import ReviewView from './views/ReviewView.vue'
import EvalView from './views/EvalView.vue'
import LoginView from './views/LoginView.vue'
import { useAuth } from './composables/useAuth'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    // 登录页：未登录时必须能进（`public`），且不显示顶栏（见 App.vue）
    { path: '/login', name: 'login', component: LoginView, meta: { title: '登录', public: true } },
    { path: '/dashboard', name: 'dashboard', component: DashboardView, meta: { title: '总览' } },
    { path: '/chat', name: 'chat', component: ChatView, meta: { title: '会话管理' } },
    { path: '/score', name: 'score', component: ScoreView, meta: { title: '评分中心' } },
    { path: '/review', name: 'review', component: ReviewView, meta: { title: '负分复核' } },
    { path: '/eval', name: 'eval', component: EvalView, meta: { title: '评估集' } },
    { path: '/sync', name: 'sync', component: SyncView, meta: { title: '同步中心' } },
    // ⚠️ 这一页是**数据源凭据**（微伴扫码），不是登录本系统 —— 名字改清楚，
    //    否则「登录」两个字会在两个地方指两件事。
    { path: '/auth', name: 'auth', component: AuthView, meta: { title: '数据源凭据' } },
  ],
})

// 路由守卫：没登录就送去登录页（且带上原本要去的地址，登录完直接回去）。
// 判据以 `/api/auth/me` 为准（服务端会话）—— 而不是「本地存过什么」：
// 会话可能在服务端已过期，本地却还留着痕迹。
router.beforeEach(async (to) => {
  if (to.meta.public) return true
  const { refresh, isAuthed, checked, authEnabled } = useAuth()
  if (!checked.value) await refresh()
  if (authEnabled.value && !isAuthed.value) {
    return { name: 'login', query: to.fullPath ? { to: to.fullPath } : {} }
  }
  return true
})

export default router
