import { createRouter, createWebHashHistory } from 'vue-router'
import DashboardView from './views/DashboardView.vue'
import ChatView from './views/ChatView.vue'
import SyncView from './views/SyncView.vue'
import AuthView from './views/AuthView.vue'
import ScoreView from './views/ScoreView.vue'
import ReviewView from './views/ReviewView.vue'

export default createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/dashboard', name: 'dashboard', component: DashboardView, meta: { title: '总览' } },
    { path: '/chat', name: 'chat', component: ChatView, meta: { title: '会话管理' } },
    { path: '/score', name: 'score', component: ScoreView, meta: { title: '评分中心' } },
    { path: '/review', name: 'review', component: ReviewView, meta: { title: '负分复核' } },
    { path: '/sync', name: 'sync', component: SyncView, meta: { title: '同步中心' } },
    { path: '/auth', name: 'auth', component: AuthView, meta: { title: '登录与凭据' } },
  ],
})
