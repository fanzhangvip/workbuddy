import axios from 'axios'

const http = axios.create({ baseURL: '/api', timeout: 120000 })

export const api = {
  // 鉴权
  authStatus: () => http.get('/auth/status').then(r => r.data),
  authLogin: (payload = {}) => http.post('/auth/login', payload).then(r => r.data),
  authVerify: () => http.post('/auth/verify').then(r => r.data),

  // 同步
  syncStart: (payload) => http.post('/sync/start', payload).then(r => r.data),
  syncStop: () => http.post('/sync/stop').then(r => r.data),
  syncStatus: () => http.get('/sync/status').then(r => r.data),
  syncJobs: (limit = 20) => http.get('/sync/jobs', { params: { limit } }).then(r => r.data),
  syncSchedule: () => http.get('/sync/schedule').then(r => r.data),
  syncSetSchedule: (minutes) => http.post('/sync/schedule', { interval_minutes: minutes }).then(r => r.data),
  syncSetKeepalive: (minutes) => http.post('/sync/schedule', { keepalive_interval_minutes: minutes }).then(r => r.data),

  // 凭据保活（微伴登录态滑动过期，定期访问即可续期）
  keepaliveStatus: () => http.get('/sync/keepalive').then(r => r.data),
  keepaliveRun: (wait = 45) => http.post('/sync/keepalive', { wait }).then(r => r.data),

  // 查询
  stats: () => http.get('/stats').then(r => r.data),
  staffs: (params) => http.get('/staffs', { params }).then(r => r.data),
  sessions: (params) => http.get('/sessions', { params }).then(r => r.data),
  sessionDetail: (params) => http.get('/session', { params }).then(r => r.data),
  messages: (params) => http.get('/messages', { params }).then(r => r.data),
  search: (params) => http.get('/search', { params }).then(r => r.data),
  recentSessions: (params) => http.get('/sessions/recent', { params }).then(r => r.data),

  // ---------------- 客户意向评分（评分中心）----------------
  scoringStart: (payload = {}) => http.post('/scoring/start', payload).then(r => r.data),
  scoringStop: () => http.post('/scoring/stop').then(r => r.data),
  scoringStatus: () => http.get('/scoring/status').then(r => r.data),
  scoringRuns: (limit = 20) => http.get('/scoring/runs', { params: { limit } }).then(r => r.data),
  scoringMetrics: () => http.get('/scoring/metrics').then(r => r.data),
  scoringAssessments: (params) => http.get('/scoring/assessments', { params }).then(r => r.data),
  scoringAssessment: (assessment_id) => http.get('/scoring/assessment', { params: { assessment_id } }).then(r => r.data),
  scoringRules: (rule_version = '') => http.get('/scoring/rules', { params: { rule_version } }).then(r => r.data),
  scoringSession: (staff_id, session_id) => http.post('/scoring/session', { staff_id, session_id }).then(r => r.data),
  scoringSchedule: () => http.get('/scoring/schedule').then(r => r.data),
  scoringSetSchedule: (minutes) => http.post('/scoring/schedule', { interval_minutes: minutes }).then(r => r.data),
  scoringSettings: () => http.get('/scoring/settings').then(r => r.data),
  scoringSetSettings: (payload) => http.post('/scoring/settings', payload).then(r => r.data),

  // ---------------- 负分人工复核 ----------------
  reviewStats: () => http.get('/review/stats').then(r => r.data),
  reviewList: (params) => http.get('/review/list', { params }).then(r => r.data),
  reviewDetail: (review_id, msg_limit = 60) =>
    http.get('/review/detail', { params: { review_id, msg_limit } }).then(r => r.data),
  reviewOptions: () => http.get('/review/options').then(r => r.data),
  reviewSubmit: (payload) => http.post('/review/submit', payload).then(r => r.data),
  reviewSetStatus: (payload) => http.post('/review/status', payload).then(r => r.data),
  reviewAudit: (limit = 30) => http.get('/review/audit', { params: { limit } }).then(r => r.data),
}

// ---------------- 展示用工具 ----------------

export function fmtTime(ts, withSeconds = false) {
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  const p = (n) => String(n).padStart(2, '0')
  const base = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
  return withSeconds ? `${base}:${p(d.getSeconds())}` : base
}

export function fmtClock(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}`
}

export function fmtRelative(ts) {
  if (!ts) return '-'
  const diff = Date.now() / 1000 - ts
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`
  return fmtTime(ts).slice(0, 10)
}

export function dayLabel(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const now = new Date()
  const same = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
  if (same(d, now)) return '今天'
  const y = new Date(now.getTime() - 86400000)
  if (same(d, y)) return '昨天'
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}
