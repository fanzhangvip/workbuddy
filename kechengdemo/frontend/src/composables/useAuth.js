import { computed, ref } from 'vue'

import { api, onUnauthorized } from '../api'

/**
 * 登录状态（本系统账号，**不是**微伴/企微的数据源凭据 —— 那个在「数据源凭据」页）。
 *
 * 为什么抽出来：路由守卫、顶栏显示当前用户、退出按钮、以及 401 自动踢回登录页，
 * 四处都要读同一份状态。分散成 `ref` 各自维护的结果是「退出后顶栏还显示着用户名」
 * 这类不一致 —— 一个模块级单例（下面这几个 ref）就没有这个问题。
 *
 * 会话本身在服务端（HttpOnly Cookie），前端**不存口令、也读不到 sid**：
 * 「记住我」是 Cookie 的 `Max-Age`，由后端决定。
 */
const user = ref('')
const role = ref('')
const authEnabled = ref(true)
const checked = ref(false)          // 是否已问过服务端「我是谁」

async function refresh() {
  try {
    const me = await api.authMe()
    user.value = me.user || ''
    role.value = me.role || ''
    authEnabled.value = me.auth_enabled !== false
  } catch (e) {
    user.value = ''
  } finally {
    checked.value = true
  }
  return user.value
}

async function signIn(username, password, remember) {
  const r = await api.authSignIn({ username, password, remember })
  user.value = r.user || username
  role.value = r.role || ''
  return r
}

async function signOut() {
  try { await api.authSignOut() } catch (e) { /* 幂等：失败也照样清本地状态 */ }
  user.value = ''
  role.value = ''
}

/** 会话失效（任一接口 401）时由 api.js 通知 → 统一清状态（路由守卫随后会踢到登录页）。 */
onUnauthorized(() => { user.value = '' })

export function useAuth() {
  return {
    user, role, authEnabled, checked,
    isAuthed: computed(() => !!user.value),
    /** 角色中文名（顶栏展示「我能做什么」）。 */
    roleText: computed(() => ({ admin: '管理员', operator: '运营', reviewer: '复核员',
                                viewer: '只读' }[role.value] || role.value || '')),
    refresh, signIn, signOut,
  }
}
