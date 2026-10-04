/* 12306 抢票助手 · 管理后台前端（原生 JS，无外部依赖）
 *
 * 与桌面客户端共用同一套 REST + WebSocket 接口，因此两端状态天然一致。
 */

'use strict';

// --------------------------------------------------------------------------- //
// 基础工具
// --------------------------------------------------------------------------- //
const TOKEN_KEY = 'th_token';
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function getToken() {
  // 放 sessionStorage 而非 localStorage：关闭标签页即失效，降低 XSS 长尾风险
  return sessionStorage.getItem(TOKEN_KEY) || '';
}

function setToken(token) {
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

async function api(path, { method = 'GET', body = null, raw = false } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  const token = getToken();
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const res = await fetch(path, {
    method,
    headers,
    body: body === null ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) {
    setToken('');
    showLogin('登录已过期，请重新登录');
    throw new Error('unauthorized');
  }
  if (raw) return res;
  let data = null;
  try { data = await res.json(); } catch (e) { data = {}; }
  if (!res.ok) {
    const msg = data && data.message ? data.message : ('请求失败 HTTP ' + res.status);
    throw new Error(msg);
  }
  return data;
}

let toastTimer = null;
function toast(message, kind = '') {
  const el = $('#toast');
  el.innerHTML = '<button type="button" class="toast-hide" aria-label="关闭">×</button>' + escapeHtml(message);
  el.className = 'toast ' + kind;
  clearTimeout(toastTimer);
  // 出错的提示**不自动消失**：一闪而过的错误等于没报，
  // 使用者还得把刚才那步再复现一遍才知道说了什么。
  if (kind !== 'err') toastTimer = setTimeout(() => el.classList.add('hidden'), 4200);
}

$('#toast').addEventListener('click', (ev) => {
  if (!ev.target.closest('.toast-hide')) return;
  clearTimeout(toastTimer);
  $('#toast').classList.add('hidden');
});

// 按钮忙碌态：拉车次 / 保存这类要等几秒的操作，必须让人看出「在跑」，
// 否则会被连点好几次（拉车次连点就是自找风控）。
async function withBusy(button, busyText, task) {
  const el = typeof button === 'string' ? $(button) : button;
  if (!el || el.dataset.busy === '1') return undefined;
  const original = el.textContent;
  el.dataset.busy = '1';
  el.disabled = true;
  if (busyText) el.textContent = busyText;
  try {
    return await task();
  } finally {
    delete el.dataset.busy;
    el.disabled = false;
    el.textContent = original;
  }
}

function escapeHtml(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function badge(text, kind = 'gray') {
  return '<span class="badge ' + kind + '">' + escapeHtml(text) + '</span>';
}

function fmtTime(ts) {
  if (!ts) return '-';
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts);
  if (isNaN(d.getTime())) return String(ts);
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
    p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

// 出票提示音（用 WebAudio 合成，避免引入外部音频文件）
function beep(times = 3) {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    for (let i = 0; i < times; i++) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = 880;
      gain.gain.value = 0.12;
      osc.connect(gain); gain.connect(ctx.destination);
      const start = ctx.currentTime + i * 0.35;
      osc.start(start); osc.stop(start + 0.22);
    }
    setTimeout(() => ctx.close && ctx.close(), times * 400 + 400);
  } catch (e) { /* 浏览器策略限制时静默失败 */ }
}

// --------------------------------------------------------------------------- //
// 登录
// --------------------------------------------------------------------------- //
function showLogin(tip = '') {
  $('#login-mask').classList.remove('hidden');
  $('#app').classList.add('hidden');
  $('#login-tip').textContent = tip;
  // 掉登录态后必须**停掉所有轮询**。
  // 以前这里只切界面，定时器继续跑：看板每 3 秒打一次 /remain，
  // 每次都 401，日志里堆成一串「未登录」而界面上什么都看不出来 ——
  // 使用者只会觉得「余票页面什么都没有」。
  stopPolling();
}

//: 所有「登录后才能跑」的轮询句柄，集中一处便于统一停掉
let captchaTimer = null;
let dashboardTimer = null;
let boardTimer = null;
function stopPolling() {
  if (boardTimer) { clearInterval(boardTimer); boardTimer = null; }
  if (dashboardTimer) { clearInterval(dashboardTimer); dashboardTimer = null; }
  if (captchaTimer) { clearInterval(captchaTimer); captchaTimer = null; }
}

function startPolling() {
  stopPolling();
  captchaTimer = setInterval(pollCaptcha, 5000);
  dashboardTimer = setInterval(() => {
    if ($('#page-dashboard').classList.contains('active')) loadDashboard().catch(() => {});
  }, 5000);
  setupBoardTimer();
}

function showApp() {
  $('#login-mask').classList.add('hidden');
  $('#app').classList.remove('hidden');
}

$('#login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('#login-tip').textContent = '';
  try {
    const data = await api('/api/auth/login', {
      method: 'POST',
      body: { user: $('#login-user').value, pass: $('#login-pass').value },
    });
    setToken(data.token);
    showApp();
    await bootData();
    connectLogs();
    connectEvents();
  } catch (err) {
    $('#login-tip').textContent = err.message || '登录失败';
  }
});

$('#logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
  setToken('');
  location.reload();
});

// --------------------------------------------------------------------------- //
// 导航
// --------------------------------------------------------------------------- //
function showPage(page) {
  const btn = $('.nav-item[data-page="' + page + '"]');
  if (!btn) return;
  $$('.nav-item').forEach((b) => b.classList.remove('active'));
  btn.classList.add('active');
  $$('.page').forEach((p) => p.classList.remove('active'));
  const section = $('#page-' + page);
  if (section) section.classList.add('active');
  $('#topbar-title').textContent = btn.textContent.trim();
  if (page === 'board') loadBoard();
  if (page === 'logs') loadLogBacklog();
  if (page === 'notify') loadNotify();
  if (page === 'orders') loadOrders();
  if (page === 'settings') loadSettings();
  // 进入个人中心时自动把账号列出来并拉当前账号的数据
  if (page === 'profile') loadProfileAccounts().catch(() => {});
}

$$('.nav-item').forEach((btn) => {
  btn.addEventListener('click', () => showPage(btn.dataset.page));
});

// 顶栏状态胶囊：点一下就跳到能处理它的那个页面。
// 使用者不该为了「账号掉线了要重新扫码」而在 9 个菜单里自己找。
$$('.ts-item').forEach((btn) => {
  btn.addEventListener('click', () => showPage(btn.dataset.goto));
});

function setTopStatus(id, text, kind = '') {
  const value = $(id);
  if (!value) return;
  value.textContent = text;
  const item = value.closest('.ts-item');
  if (!item) return;
  item.classList.remove('is-ok', 'is-warn', 'is-err');
  if (kind) item.classList.add('is-' + kind);
}

// --------------------------------------------------------------------------- //
// 仪表盘
// --------------------------------------------------------------------------- //
async function loadDashboard() {
  try {
    const stats = await api('/api/stats');
    const runtime = stats.runtime || {};
    const accounts = runtime.accounts || { total: 0, online: 0, risk: 0 };
    const scheduler = runtime.scheduler || { total: 0, running: 0, success: 0 };
    const storage = stats.storage || {};
    const rate = runtime.rate_limit || {};

    // ---- 顶栏：把「要不要现在动手」的四个数摆在最显眼的地方 ----
    setTopStatus('#ts-hit-v', String(scheduler.success || 0), scheduler.success ? 'ok' : '');
    setTopStatus('#ts-running-v', (scheduler.running || 0) + ' / ' + (scheduler.total || 0),
      scheduler.running ? 'ok' : 'warn');
    setTopStatus('#ts-accounts-v', (accounts.online || 0) + ' / ' + (accounts.total || 0),
      accounts.online ? 'ok' : (accounts.total ? 'warn' : 'err'));
    setTopStatus('#ts-risk-v', String(rate.risk_count || 0), rate.risk_count ? 'err' : 'ok');

    // ---- 主卡片：只放使用者真正关心的三件事 ----
    const primary = [
      ['已抢到', scheduler.success || 0, scheduler.success ? 'ok' : ''],
      ['运行中任务', (scheduler.running || 0) + ' / ' + (scheduler.total || 0), scheduler.running ? 'ok' : ''],
      ['账号在线', (accounts.online || 0) + ' / ' + (accounts.total || 0),
        accounts.online ? 'ok' : (accounts.total ? 'warn' : 'err')],
    ];
    $('#dash-cards').innerHTML = primary.map(([k, v, cls]) =>
      '<div class="card"><div class="k">' + k + '</div><div class="v ' + cls + '">' + escapeHtml(v) + '</div></div>'
    ).join('');

    // ---- 运行指标：留痕用，日常不用盯，收进折叠区 ----
    const metrics = [
      ['累计查询', storage.queries_total || 0, ''],
      ['今日查询', storage.queries_today || 0, ''],
      ['平均延迟', (storage.latency_avg_ms || 0) + ' ms', ''],
      ['当前查询间隔', (rate.interval || 0) + ' s', rate.risk_count ? 'warn' : ''],
      ['风控次数', rate.risk_count || 0, rate.risk_count ? 'err' : 'ok'],
      ['风控中的账号', accounts.risk || 0, accounts.risk ? 'err' : 'ok'],
    ];
    $('#dash-metrics').innerHTML = metrics.map(([k, v, cls]) =>
      '<div class="card"><div class="k">' + k + '</div><div class="v ' + cls + '">' + escapeHtml(v) + '</div></div>'
    ).join('');

    renderHero({ online: accounts.online || 0, total: accounts.total || 0, running: scheduler.running || 0, success: scheduler.success || 0 });
    await renderTaskCards();
  } catch (err) {
    toast(err.message, 'err');
  }
}

// 总览页顶部的「下一步」。空荡荡的仪表盘只会让人不知道从哪开始，
// 所以这里始终给出**一句当前处境 + 一个能点的按钮**。
function renderHero({ online, total, running, success }) {
  const el = $('#dash-hero');
  if (!el) return;
  let html = '';
  if (total === 0) {
    html = '<h3>第 1 步：登录一个 12306 账号</h3>' +
      '<p>抢票要用你自己的 12306 账号。密码登录目前会被官方风控挡住，' +
      '请用手机 12306 App <strong>扫码登录</strong>（登录成功后会记住会话，通常不用重复扫）。</p>' +
      '<div class="hero-actions"><button type="button" class="primary" data-goto="accounts">去扫码登录</button></div>';
  } else if (online === 0) {
    html = '<h3>账号都不在线，先恢复登录</h3>' +
      '<p>共 ' + total + ' 个账号，当前 0 个在线。账号掉线时任务跑起来也查不到票（查询本身不需要登录，但下单必须登录）。</p>' +
      '<div class="hero-actions"><button type="button" class="primary" data-goto="accounts">去账号管理</button></div>';
  } else if (running === 0) {
    html = '<h3>账号已就绪，下一步是把任务跑起来</h3>' +
      '<p>还没有任务在运行。先去「任务配置」说清楚要坐哪天的哪趟车，然后启动它。</p>' +
      '<div class="hero-actions"><button type="button" class="primary" data-goto="tasks">去配置任务</button>' +
      '<button type="button" id="hero-start-all">一键启动已启用的任务</button></div>';
  } else if (success > 0) {
    html = '<h3>已经抢到 ' + success + ' 张票</h3>' +
      '<p>本工具<strong>不代扣款</strong>：请尽快到 12306 完成支付，超时未支付的订单会被官方自动取消。</p>' +
      '<div class="hero-actions"><button type="button" class="primary" data-goto="orders">去订单记录</button></div>';
  } else {
    html = '<h3>' + running + ' 个任务正在盯票</h3>' +
      '<p>可以到「余票看板」看实时余票变化，或在「实时日志」里看它每一轮查到了什么。抢到票会有提示音和通知。</p>' +
      '<div class="hero-actions"><button type="button" data-goto="board">看余票看板</button>' +
      '<button type="button" data-goto="logs">看实时日志</button></div>';
  }
  el.innerHTML = html;
  $$('[data-goto]', el).forEach((btn) => btn.addEventListener('click', () => showPage(btn.dataset.goto)));
  const startAll = $('#hero-start-all');
  if (startAll) startAll.addEventListener('click', () => $('#btn-start-all').click());
}

async function renderTaskCards() {
  const data = await api('/api/tasks/status/all');
  const tasks = data.tasks || [];
  if (!tasks.length) {
    $('#dash-tasks').innerHTML = '<div class="board-empty">还没有运行中的任务，先去「任务配置」创建一个。</div>';
    return;
  }
  $('#dash-tasks').innerHTML = tasks.map((t) => {
    const st = t.stats || {};
    const hit = t.state === 'success';
    const color = t.state === 'success' ? 'ok' : (t.state === 'error' ? 'err' : (t.running ? 'info' : 'gray'));
    return '<div class="task-card ' + (hit ? 'hit' : '') + '">' +
      '<h4>' + escapeHtml(t.task_name) + ' ' + badge(t.state_text || t.state, color) + '</h4>' +
      '<div class="meta">' +
        '账号：' + escapeHtml(t.account || '-') + '<br>' +
        '策略：' + escapeHtml(t.strategy || '-') + ' · 间隔 ' + (t.interval || '-') + 's<br>' +
        '轮次：' + (st.rounds || 0) + ' · 命中候选：' + (st.candidates_seen || 0) + '<br>' +
        '下单尝试：' + (st.order_attempts || 0) + ' · 失败：' + (st.order_failures || 0) +
        (t.error ? '<br><span style="color:#b91c1c">错误：' + escapeHtml(t.error) + '</span>' : '') +
      '</div>' +
      '<div class="actions">' +
        '<button class="tiny" data-act="stop-task" data-id="' + t.task_id + '">停止</button>' +
      '</div></div>';
  }).join('');
}

$('#btn-start-all').addEventListener('click', async () => {
  const data = await api('/api/tasks');
  const tasks = (data.tasks || []).filter((t) => t.enabled);
  if (!tasks.length) return toast('没有已启用的任务', 'err');
  let ok = 0;
  for (const t of tasks) {
    try { await api('/api/tasks/' + t.id + '/start', { method: 'POST' }); ok++; } catch (e) { /* continue */ }
  }
  toast('已启动 ' + ok + ' 个任务', 'ok');
  loadDashboard();
});

$('#btn-stop-all').addEventListener('click', async () => {
  await api('/api/system/stop', { method: 'POST' });
  toast('已发出停止指令', 'ok');
  setTimeout(loadDashboard, 800);
});

$('#btn-refresh-dash').addEventListener('click', loadDashboard);

document.addEventListener('click', async (ev) => {
  const stopBtn = ev.target.closest('[data-act="stop-task"]');
  if (stopBtn) {
    await api('/api/tasks/' + stopBtn.dataset.id + '/stop', { method: 'POST' });
    loadDashboard();
  }
});

// --------------------------------------------------------------------------- //
// 账号
// --------------------------------------------------------------------------- //
const STATUS_TEXT = { online: '在线', offline: '离线', risk: '风控', logining: '登录中', error: '异常', disabled: '已禁用' };

// 人证核验四态：已核验 / 未核验 / 无法判定 / 未检测
// 「未核验」会让人以为账号被 12306 限制了，所以只在**拿到明确字段确认未通过**时才这么显示；
// 接口没返回核验字段时如实说「无法判定」，而不是用姓名去猜（扫码账号会猜错）。
// 真机实测官方两个用户信息接口**都不返回核验字段**，所以「无法判定」是常态而不是异常。
// 状态名与「下一步做什么」由后端统一下发（verify_label / verify_hint），
// 前端不再自己拼一套判断，避免两端口径不一致。
function verifiedBadge(u) {
  const state = u.verify_state || (u.is_checked ? 'verified'
    : (!u.verified_checked ? 'unchecked' : (u.verify_known ? 'unverified' : 'unknown')));
  const kind = state === 'verified' ? 'ok' : (state === 'unverified' ? 'warn' : 'gray');
  const text = u.verify_label || ({ verified: '已核验', unverified: '未核验', unknown: '无法判定', unchecked: '未检测' }[state] || '未检测');
  const hint = u.verify_hint ? ' title="' + escapeHtml(u.verify_hint) + '"' : '';
  return '<span' + hint + '>' + badge(text, kind) + '</span>';
}

async function loadAccounts() {
  const data = await api('/api/users');
  const users = data.users || [];
  // 一个账号都没有时，光有张空表格没有用 —— 直接说清楚下一步该干什么
  const empty = $('#acct-empty');
  if (empty) empty.classList.toggle('hidden', users.length > 0);
  // 「详情」按钮的点击处理器只能拿到 id，这里把整份用户列表挂起来供它查，
  // 免得为了一个 online 判断再打一次接口。
  window.__usersCache = users;
  // 有账号被核验挡住时，把说明自动摊开并给出提示 ——
  // 否则「候补一直不提交」这个现象使用者根本无从归因。
  const blocked = users.filter((u) => u.verify_state === 'unverified');
  const guide = $('#verify-guide');
  const alertBox = $('#verify-alert');
  if (alertBox) {
    if (blocked.length) {
      const names = blocked.map((u) => u.username).slice(0, 3).join('、');
      alertBox.innerHTML = '<strong>' + escapeHtml(names) + (blocked.length > 3 ? ' 等' : '') +
        ' 未完成人证核验，无法提交候补。</strong> ' +
        escapeHtml(blocked[0].verify_hint || '');
      alertBox.classList.remove('hidden');
      if (guide) guide.open = true;
    } else {
      alertBox.classList.add('hidden');
    }
  }
  $('#users-table tbody').innerHTML = users.map((u) => {
    const kind = u.status === 'online' ? 'ok' : (u.status === 'risk' ? 'err' : (u.status === 'offline' ? 'gray' : 'warn'));
    const loginType = u.has_password ? '<span class="badge gray">密码</span>' : '<span class="badge warn">扫码</span>';
    // 扫码账号没有密码，触发密码登录必然失败，所以禁用「测试登录」并说明原因
    const loginBtn = u.has_password
      ? '<button class="tiny" data-act="login-user" data-id="' + u.id + '">测试登录</button> '
      : '<button class="tiny" disabled title="扫码登录账号没有保存密码，请重新扫码">测试登录</button> ';
    // 在 12306 App 里做完人证核验后要能刷新 —— 扫码账号没有密码，
    // 「测试登录」是禁用的，只靠它这类账号永远刷新不了核验状态。
    const verifyBtn = u.online
      ? '<button class="tiny" data-act="verify-user" data-id="' + u.id + '">重测核验</button> '
      : '<button class="tiny" disabled title="账号不在线，先扫码登录再检测">重测核验</button> ';
    return '<tr>' +
      '<td>' + u.id + '</td>' +
      '<td>' + escapeHtml(u.username) + '</td>' +
      '<td>' + badge(STATUS_TEXT[u.status] || u.status, kind) + '</td>' +
      '<td>' + loginType + '</td>' +
      '<td>' + verifiedBadge(u) + '</td>' +
      '<td>' + escapeHtml(u.last_active || '-') + '</td>' +
      '<td>' +
        '<button class="tiny" data-act="detail-user" data-id="' + u.id + '">详情</button> ' +
        loginBtn +
        verifyBtn +
        '<button class="tiny" data-act="toggle-user" data-id="' + u.id + '" data-enabled="' + (!u.enabled) + '">' + (u.enabled ? '禁用' : '启用') + '</button> ' +
        '<button class="tiny danger" data-act="del-user" data-id="' + u.id + '">删除</button>' +
      '</td></tr>';
  }).join('');
}

// --------------------------------------------------------------------------- //
// 个人中心（账号信息 / 乘车人 / 我的订单 / 待支付）
// --------------------------------------------------------------------------- //
function acctVerifiedText(profile) {
  // verified 为 null = 接口没给明确字段。显示「无法判定」而不是「未核验」，
  // 后者会让人以为账号被 12306 限制了。
  if (profile.verified === true) return badge('已核验', 'ok');
  if (profile.verified === false) return badge('未核验（候补会被拦）', 'warn');
  return badge('无法判定', 'gray');
}

function acctSection(section) {
  // 统一把后端的 {ok,error,data} 转成 (数据, 错误文本)
  if (!section || !section.ok) return [null, (section && section.error) || '后台没有返回这块数据'];
  return [section.data, ''];
}

// --------------------------------------------------------------------------- //
// 个人中心（左侧导航一级入口：账号信息 / 乘车人 / 我的订单 / 待支付）
// --------------------------------------------------------------------------- //
const ACCT_FIELDS = [
  ['username', '账号'], ['real_name', '真实姓名'], ['user_type', '用户类型'],
  ['sex', '性别'], ['mobile', '手机号'], ['email', '邮箱'],
  ['id_type', '证件类型'], ['id_no', '证件号'], ['points', '积分'],
  ['register_time', '注册时间'],
];

let acctUserId = 0;
let acctUser = null;
let acctOrderPage = 1;
let acctOrderPages = 1;
//: 从「个人中心」里发起的扫码登录：成功后要顺手把本页刷新一遍
let acctReloadAfterLogin = false;

function switchAcctTab(name) {
  $$('#acct-tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $$('#acct-body .acct-pane').forEach((p) => p.classList.toggle('active', p.id === 'acct-pane-' + name));
}

function acctStatus(id, text, isError) {
  const el = $(id);
  if (!el) return;
  el.textContent = text || '';
  el.className = 'acct-status' + (isError ? ' err' : '');
}

// 账号下拉：一进来就把所有账号列出来，默认选第一个在线的（没人愿意先点两下）
async function loadProfileAccounts() {
  const select = $('#acct-account');
  if (!select) return;
  let users = [];
  try {
    users = (await api('/api/users')).users || [];
  } catch (err) {
    $('#acct-account-hint').textContent = '读取账号列表失败：' + err.message;
    return;
  }
  const previous = Number(select.value || 0);
  select.innerHTML = users.map((u) => {
    const mark = u.online ? '在线' : (STATUS_TEXT[u.status] || u.status || '离线');
    const extra = u.session_saved ? '' : ' · 需扫码';
    return '<option value="' + u.id + '">' + escapeHtml(u.username + '（' + mark + '）' + extra) + '</option>';
  }).join('');
  window.__usersCache = users;

  if (!users.length) {
    $('#acct-account-hint').textContent =
      '还没有账号。请先到「账号管理」添加，或点下方「扫码登录」用手机扫一次。';
    showProfileFor({ id: 0, username: '', online: false });
    return;
  }
  if (previous && users.some((u) => Number(u.id) === previous)) {
    select.value = String(previous);
  } else {
    const online = users.find((u) => u.online);
    select.value = String((online || users[0]).id);
  }
  $('#acct-account-hint').textContent = '共 ' + users.length + ' 个账号';
  onProfileAccountChanged();
}

function onProfileAccountChanged() {
  const select = $('#acct-account');
  const user = (window.__usersCache || []).find((u) => String(u.id) === String(select.value));
  if (user) showProfileFor(user);
}

function showProfileFor(user) {
  acctUser = user || null;
  acctUserId = user ? Number(user.id) || 0 : 0;
  acctOrderPage = 1;
  acctOrderPages = 1;
  switchAcctTab('profile');
  $('#acct-order-kw').value = '';
  $('#acct-order-where').value = 'G';

  if (!acctUserId) {
    $('#acct-banner-text').innerHTML = '⚠ 还没有可查看的账号，请先到「账号管理」添加，或用手机扫码登录一次。';
    $('#acct-banner').classList.remove('hidden');
    ['#acct-profile-status', '#acct-pass-status', '#acct-orders-status', '#acct-pending-status']
      .forEach((id) => acctStatus(id, '', false));
    $('#acct-profile').innerHTML = '<p class="muted">未选择账号</p>';
    $('#acct-pass-table tbody').innerHTML = '<tr><td colspan="7" class="muted">未选择账号</td></tr>';
    $('#acct-orders-table tbody').innerHTML = '<tr><td colspan="10" class="muted">未选择账号</td></tr>';
    $('#acct-pending-table tbody').innerHTML = '<tr><td colspan="7" class="muted">未选择账号</td></tr>';
    return;
  }

  // 未登录是**常态**（扫码账号重启后必然如此），所以页面照常显示，
  // 在这里说清楚原因和下一步，而不是把人挡在门外。
  if (user.online) {
    $('#acct-banner').classList.add('hidden');
  } else {
    $('#acct-banner-text').innerHTML =
      '⚠ 账号「' + escapeHtml(user.username) + '」当前<b>未登录</b>，以下信息需要有效登录态才能拉取。'
      + '点右侧按钮扫码即可；本次扫码成功后会<b>把会话加密记住</b>，之后重启后台不必再扫。';
    $('#acct-banner').classList.remove('hidden');
  }
  loadAccountProfile(false);
  loadAccountOrders(1);
}

// 兼容旧入口：账号行上的「详情」按钮 → 切到个人中心页并选中该账号
function openAccountDetail(user) {
  const navBtn = $$('.nav-item').find((b) => b.dataset.page === 'profile');
  if (navBtn) navBtn.click();
  loadProfileAccounts().then(() => {
    const select = $('#acct-account');
    if (select && user && user.id != null) {
      select.value = String(user.id);
    }
    showProfileFor(user);
  }).catch(() => showProfileFor(user));
}

async function loadAccountProfile(forcePassengers) {
  const uid = acctUserId;
  if (!uid) return;
  acctStatus('#acct-profile-status', '正在拉取…', false);
  acctStatus('#acct-pass-status', '正在拉取…', false);
  acctStatus('#acct-pending-status', '正在拉取…', false);
  try {
    const data = await api('/api/users/' + uid + '/profile?with_passengers=1&with_pending=1'
      + (forcePassengers ? '&refresh=1' : ''));
    if (uid !== acctUserId) return;   // 弹窗已经切到别的账号，丢弃本轮结果
    renderAccountProfile(data.profile);
    renderAccountPassengers(data.passengers);
    renderAccountPending(data.pending);
  } catch (err) {
    if (uid !== acctUserId) return;
    acctStatus('#acct-profile-status', '拉取失败：' + err.message, true);
    acctStatus('#acct-pass-status', '拉取失败：' + err.message, true);
    acctStatus('#acct-pending-status', '拉取失败：' + err.message, true);
  }
}

function renderAccountProfile(section) {
  const [data, error] = acctSection(section);
  const box = $('#acct-profile');
  if (!data) {
    box.innerHTML = '<p class="muted">未取到账号信息</p>';
    acctStatus('#acct-profile-status', error, true);
    return;
  }
  const rows = ACCT_FIELDS.map(([key, label]) =>
    '<div class="acct-field"><span class="k">' + label + '</span><span class="v">'
    + escapeHtml(data[key] || '-') + '</span></div>').join('');
  const extra = '<div class="acct-field"><span class="k">人证核验</span><span class="v">'
    + acctVerifiedText(data) + '</span></div>';
  // 下面两项是本工具侧的状态（不在 12306 返回里），取自账号列表
  const local = acctUser
    ? '<div class="acct-field"><span class="k">免扫码登录</span><span class="v">'
      + (acctUser.session_saved ? '已保存（重启后无需重新扫码）' : '未保存（重启后需重新扫码登录）')
      + '</span></div>'
      + '<div class="acct-field"><span class="k">登录方式</span><span class="v">'
      + (acctUser.has_password ? '账号密码' : '扫码登录') + '</span></div>'
    : '';
  box.innerHTML = rows + extra + local;
  acctStatus('#acct-profile-status', '', false);
}

function renderAccountPassengers(section) {
  const [rows, error] = acctSection(section);
  const tbody = $('#acct-pass-table tbody');
  if (!rows) {
    tbody.innerHTML = '<tr><td colspan="7" class="muted">未取到乘车人</td></tr>';
    acctStatus('#acct-pass-status', error, true);
    return;
  }
  tbody.innerHTML = rows.map((p) => '<tr>' +
    '<td>' + escapeHtml(p.passenger_name) + '</td>' +
    '<td>' + escapeHtml(p.passenger_id_type_name || p.passenger_id_type_code) + '</td>' +
    '<td>' + escapeHtml(p.passenger_id_no) + '</td>' +
    '<td>' + escapeHtml(p.passenger_type_name || p.passenger_type) + '</td>' +
    '<td>' + escapeHtml(p.mobile_no) + '</td>' +
    '<td>' + escapeHtml(p.verify_status_name || '-') + '</td>' +
    '<td>' + (p.is_self ? '是' : '') + '</td>' +
    '</tr>').join('') || '<tr><td colspan="7" class="muted">该账号下没有乘车人</td></tr>';
  acctStatus('#acct-pass-status', '共 ' + rows.length + ' 位乘车人' + (section.cached ? '（缓存）' : ''), false);
}

function renderAccountPending(section) {
  const [rows, error] = acctSection(section);
  const tbody = $('#acct-pending-table tbody');
  if (!rows) {
    tbody.innerHTML = '<tr><td colspan="7" class="muted">未取到待支付订单</td></tr>';
    acctStatus('#acct-pending-status', error, true);
    return;
  }
  tbody.innerHTML = rows.map((o) => {
    const passengers = (o.passengerDTOs || o.passengers || [])
      .map((p) => p.passenger_name || p.name || '').filter(Boolean).join(',');
    const payUrl = o.pay_url || '';
    const action = payUrl
      ? '<a class="btn tiny" href="' + escapeHtml(payUrl) + '" target="_blank" rel="noreferrer">去官方支付</a>'
      : '';
    return '<tr>' +
      '<td>' + escapeHtml(o.sequence_no || o.order_no || '') + '</td>' +
      '<td>' + escapeHtml(o.station_train_code || o.train_code || '') + '</td>' +
      '<td>' + escapeHtml(o.train_date || '') + '</td>' +
      '<td>' + escapeHtml(passengers) + '</td>' +
      '<td>' + escapeHtml(o.ticket_total_price || o.total_price || '') + '</td>' +
      '<td>' + escapeHtml(o.order_status_name || o.status || '') + '</td>' +
      '<td>' + action + '</td></tr>';
  }).join('') || '<tr><td colspan="7" class="muted">没有待支付订单</td></tr>';
  acctStatus('#acct-pending-status', '待支付订单 ' + rows.length + ' 个', false);
}

async function loadAccountOrders(page) {
  const uid = acctUserId;
  if (!uid) return;
  page = Math.max(1, page);
  if (page > acctOrderPages) return;
  acctOrderPage = page;
  acctStatus('#acct-orders-status', '正在查询订单…', false);
  const where = $('#acct-order-where').value;
  const kw = $('#acct-order-kw').value.trim();
  try {
    const section = await api('/api/users/' + uid + '/orders?where=' + where
      + '&page=' + page + '&size=10&kw=' + encodeURIComponent(kw));
    if (uid !== acctUserId) return;
    renderAccountOrders(section);
  } catch (err) {
    if (uid !== acctUserId) return;
    $('#acct-orders-table tbody').innerHTML = '<tr><td colspan="10" class="muted">查询失败</td></tr>';
    acctStatus('#acct-orders-status', '订单未取到：' + err.message, true);
  }
}

function renderAccountOrders(section) {
  const [data, error] = acctSection(section);
  const tbody = $('#acct-orders-table tbody');
  if (!data) {
    tbody.innerHTML = '<tr><td colspan="10" class="muted">未取到订单</td></tr>';
    acctStatus('#acct-orders-status', error, true);
    acctOrderPages = 1;
    updateAcctPageButtons();
    return;
  }
  const orders = data.orders || [];
  tbody.innerHTML = orders.map((o) => {
    const first = (o.passengers || [])[0] || {};
    const seat = first.coach_no ? (first.coach_no + '车' + (first.seat_no || '')) : '';
    return '<tr>' +
      '<td>' + escapeHtml(o.order_no) + '</td>' +
      '<td>' + escapeHtml(o.order_date) + '</td>' +
      '<td>' + escapeHtml(o.train_code) + '</td>' +
      '<td>' + escapeHtml(o.train_date) + '</td>' +
      '<td>' + escapeHtml(o.from_station) + '→' + escapeHtml(o.to_station) + '</td>' +
      '<td>' + escapeHtml(o.seat_type) + '</td>' +
      '<td>' + escapeHtml(o.passenger_names) + '</td>' +
      '<td>' + escapeHtml(o.price) + '</td>' +
      '<td>' + escapeHtml(o.status) + '</td>' +
      '<td>' + escapeHtml(seat) + '</td></tr>';
  }).join('') || '<tr><td colspan="10" class="muted">没有符合条件的订单</td></tr>';

  acctOrderPage = Number(data.page_index || 1);
  acctOrderPages = Math.max(1, Number(data.page_count || 1));
  const scope = data.where === 'H' ? '历史' : '未出行';
  // 把「实际查了什么」写出来：订单为空时，最需要知道的是
  // 「查的是近 N 天 / 按哪个日期口径」，否则只能怀疑是程序坏了。
  const detail = data.window_days
    ? '（近 ' + data.window_days + ' 天·按' + (Number(data.query_type) === 2 ? '乘车' : '订票') + '日期）'
    : '';
  let acctOrderText = scope + '订单 ' + orders.length + ' 条' + detail +
    ' · 第 ' + acctOrderPage + '/' + acctOrderPages + ' 页';
  if (!orders.length) {
    // 一条都没有时把官方的说法摆出来：「真没订单」和「官方有单但我们没取到」
    // 是两件事，不写清楚就会一直当成 bug 查。
    const total = data.total_number;
    if (total === 0) acctOrderText += '　官方返回：名下没有订单';
    else if (typeof total === 'number' && total > 0) {
      acctOrderText += '　注意：官方称名下共 ' + total + ' 单，但本窗口没取到 —— 换个范围或口径再试';
    }
  }
  acctStatus('#acct-orders-status', acctOrderText, false);
  updateAcctPageButtons();
}

function updateAcctPageButtons() {
  $('#acct-order-prev').disabled = acctOrderPage <= 1;
  $('#acct-order-next').disabled = acctOrderPage >= acctOrderPages;
}

$('#acct-tabs').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-tab]');
  if (btn) switchAcctTab(btn.dataset.tab);
});
$('#acct-account').addEventListener('change', onProfileAccountChanged);
$('#acct-reload-accounts').addEventListener('click', () => loadProfileAccounts());
// 就地扫码登录：复用账号页那套二维码流程，扫成功后刷新账号列表与本页
$('#acct-banner-login').addEventListener('click', () => {
  acctReloadAfterLogin = true;
  startQrLogin();
});
$('#acct-pass-refresh').addEventListener('click', () => loadAccountProfile(true));
$('#acct-order-query').addEventListener('click', () => loadAccountOrders(1));
$('#acct-order-where').addEventListener('change', () => loadAccountOrders(1));
$('#acct-order-kw').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') loadAccountOrders(1); });
$('#acct-order-prev').addEventListener('click', () => loadAccountOrders(acctOrderPage - 1));
$('#acct-order-next').addEventListener('click', () => loadAccountOrders(acctOrderPage + 1));

// --------------------------------------------------------------------------- //
// 扫码登录（AC-08）
// --------------------------------------------------------------------------- //
let qrTimer = null;
let qrUuid = '';        // 会话标识
let qrImageUuid = '';   // 当前显示的那张二维码的 uuid（用于识别后台自动换码）

const QR_STATE_TEXT = {
  waiting: '请使用 12306 App 扫描二维码',
  scanned: '已扫描，请在手机上点击确认',
  confirmed: '登录成功',
  expired: '二维码已失效，请刷新',
  failed: '扫码登录失败',
  // 非终态：服务端偶发返回异常信封，只是这一轮没拿到状态，二维码通常仍有效
  unknown: '暂时无法获取扫码状态，正在重试…',
};

function closeQrModal() {
  qrImageUuid = '';
  if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
  // 主动取消：让后台立刻停止这个会话，避免前端关掉后仍在轮询接口
  if (qrUuid) {
    api('/api/users/qr/cancel', { method: 'POST', body: { uuid: qrUuid } }).catch(() => {});
    qrUuid = '';
  }
  $('#qr-modal').classList.add('hidden');
}

function setQrStatus(text, kind = '') {
  const el = $('#qr-status');
  el.textContent = text;
  el.className = 'qr-status' + (kind ? ' ' + kind : '');
}

async function startQrLogin() {
  $('#qr-modal').classList.remove('hidden');
  $('#qr-overlay').classList.add('hidden');
  $('#qr-img').removeAttribute('src');
  $('#qr-remaining').textContent = '';
  setQrStatus('正在获取二维码…');
  try {
    const data = await api('/api/users/qr/start', { method: 'POST', body: {} });
    qrUuid = data.uuid;                          // 会话标识：轮询的键，不会变
    qrImageUuid = data.qr_uuid || data.uuid;     // 当前这张二维码
    if (data.image) {
      $('#qr-img').src = 'data:image/png;base64,' + data.image;
    } else {
      setQrStatus('服务端未返回二维码图片，请稍后重试', 'err');
      return;
    }
    setQrStatus(QR_STATE_TEXT[data.state] || data.message);
    pollQrStatus();
    if (qrTimer) clearInterval(qrTimer);
    qrTimer = setInterval(pollQrStatus, 1500);
  } catch (err) {
    setQrStatus('获取二维码失败：' + err.message, 'err');
  }
}

async function pollQrStatus() {
  if (!qrUuid) return;
  try {
    // seen 传当前显示的那张码的 uuid：后台换了新码才会把图片带回来
    const data = await api('/api/users/qr/status?uuid=' + encodeURIComponent(qrUuid)
      + '&seen=' + encodeURIComponent(qrImageUuid || ''));
    const currentQr = data.qr_uuid || data.uuid;
    if (data.image && currentQr && currentQr !== qrImageUuid) {
      // 官方二维码只有约 20 秒寿命，后台会自动换码，这里跟着换图
      qrImageUuid = currentQr;
      $('#qr-img').src = 'data:image/png;base64,' + data.image;
      $('#qr-overlay').classList.add('hidden');
      if (data.refresh_count > 1) {
        toast('二维码已自动刷新，请扫描当前这张', '');
      }
    }
    if (data.remaining > 0) {
      $('#qr-remaining').textContent = '有效期剩余 ' + data.remaining + ' 秒';
    }
    if (data.state === 'confirmed') {
      setQrStatus('登录成功：' + data.username, 'ok');
      closeQrTimerOnly();
      $('#qr-overlay').classList.remove('hidden');
      toast('账号 ' + data.username + ' 扫码登录成功', 'ok');
      loadAccounts();
      setTimeout(closeQrModal, 1200);
      // 如果这次扫码是从「个人中心」里点起来的，扫完把本页刷新一遍，
      // 否则使用者会盯着一个仍然写着「未登录」的页面，以为扫码没生效。
      if (acctReloadAfterLogin) {
        acctReloadAfterLogin = false;
        setTimeout(() => {
          // 重新拉账号列表：扫码可能会新建账号（账号名扫之前是不知道的），
          // 下拉里必须出现这一项，否则「扫成功了却看不到」。
          loadProfileAccounts().catch(() => {});
        }, 1500);
      }
      return;
    }
    if (data.state === 'expired' || data.state === 'failed') {
      setQrStatus(data.message || QR_STATE_TEXT[data.state], 'err');
      $('#qr-overlay').classList.remove('hidden');
      closeQrTimerOnly();
      return;
    }
    if (data.state === 'scanned') {
      setQrStatus(QR_STATE_TEXT.scanned);
      return;
    }
    setQrStatus(data.message || QR_STATE_TEXT[data.state]);
  } catch (err) {
    // 会话不存在（后台重启过）也要停止轮询，否则会一直空转报错
    setQrStatus('状态查询失败：' + err.message, 'err');
    closeQrTimerOnly();
  }
}

function closeQrTimerOnly() {
  if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
}

$('#qr-login-btn').addEventListener('click', startQrLogin);
$('#acct-empty-qr').addEventListener('click', startQrLogin);
$('#qr-refresh').addEventListener('click', () => {
  closeQrTimerOnly();
  if (qrUuid) { api('/api/users/qr/cancel', { method: 'POST', body: { uuid: qrUuid } }).catch(() => {}); qrUuid = ''; }
  startQrLogin();
});
$('#qr-close').addEventListener('click', closeQrModal);

$('#user-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  try {
    const data = await api('/api/users', {
      method: 'POST',
      body: {
        username: $('#user-name').value.trim(),
        password: $('#user-pass').value,
        heartbeat: parseInt($('#user-heartbeat').value || '120', 10),
      },
    });
    $('#user-pass').value = '';
    toast(data.message || '账号已保存', 'ok');
    loadAccounts();
    // 登录是后台异步做的，隔一小会儿再刷两次，状态立刻可见
    if (data.login_queued) {
      setTimeout(loadAccounts, 1500);
      setTimeout(() => { loadAccounts(); loadDashboard(); }, 5000);
    }
  } catch (err) { toast(err.message, 'err'); }
});

document.addEventListener('click', async (ev) => {
  const el = ev.target.closest('[data-act]');
  if (!el) return;
  const act = el.dataset.act, id = el.dataset.id;
  try {
    if (act === 'detail-user') {
      // **无论账号是否在线都要打开弹窗**：扫码账号重启后就是未登录（常态），
      // 直接把人挡在外面只会让人以为功能没做出来。
      const user = (window.__usersCache || []).find((u) => String(u.id) === String(id));
      if (user) openAccountDetail(user);
      else toast('账号列表已刷新，请重新点击「详情」', 'err');
    }
    if (act === 'login-user') { await api('/api/users/' + id + '/login', { method: 'POST' }); toast('已发起登录，稍后刷新查看', 'ok'); }
    if (act === 'toggle-user') { await api('/api/users/' + id + '/state', { method: 'PUT', body: { enabled: el.dataset.enabled === 'true' } }); loadAccounts(); }
    if (act === 'verify-user') {
      const res = await api('/api/users/' + id + '/verify/refresh', { method: 'POST' });
      // 如实播报四态：verify_known=false 时是「无法判定」，不能说成「未核验」
      toast('人证核验：' + (res.verify_label || '未知') + (res.verify_hint ? '　' + res.verify_hint : ''),
            res.can_standby ? 'ok' : 'err');
      loadAccounts();
    }
    if (act === 'del-user') { if (confirm('确认删除该账号？')) { await api('/api/users/' + id, { method: 'DELETE' }); loadAccounts(); } }
    if (act === 'start-task') { await api('/api/tasks/' + id + '/start', { method: 'POST' }); toast('任务已启动', 'ok'); refreshTasks(); }
    if (act === 'stop-task2') { await api('/api/tasks/' + id + '/stop', { method: 'POST' }); refreshTasks(); }
    if (act === 'edit-task') { await fillTaskForm(id); }
    if (act === 'del-task') { if (confirm('确认删除该任务？')) { await api('/api/tasks/' + id, { method: 'DELETE' }); refreshTasks(); } }
  } catch (err) { toast(err.message, 'err'); }
});

// --------------------------------------------------------------------------- //
// 任务
// --------------------------------------------------------------------------- //
// --------------------------------------------------------------------------- //
// 席别优先级：必须是「有顺序」的控件
//
// 之前这里是一个 <select multiple> —— 它有两个致命问题：
//   1. 表达不了顺序（要按住 Ctrl 点，而且钩选先后跟列表顺序毫无关系），
//      可席别的顺序恰恰就是「有票时先要哪个」的抢票优先级；
//   2. 使用者得知道「要按住 Ctrl 才能多选」这一个隐藏知识。
// 所以换成左右两栏：左边是「会抢的席别」并带 ↑↓ 调整，右边点一下加进去。
// --------------------------------------------------------------------------- //
const SEAT_TYPES = ['商务座', '特等座', '一等座', '二等座', '高级软卧', '软卧', '硬卧', '软座', '硬座', '无座'];
const TRAIN_TYPE_OPTIONS = ['高铁', '动车', '直达', '特快', '快速', '普快', '其他'];
const DEFAULT_SEATS = ['二等座'];

const seatValues = [];   //: 有序：下标 0 就是最高优先级

function renderSeats() {
  $('#t-seats').value = seatValues.join(',');
  const picked = $('#seat-picked');
  picked.innerHTML = seatValues.length
    ? seatValues.map((name, i) => {
        const last = seatValues.length - 1;
        return '<div class="prio-row">' +
          '<span class="rank">' + (i + 1) + '</span>' +
          '<span class="name">' + escapeHtml(name) + '</span>' +
          '<button type="button" class="mv" data-seat-up="' + i + '"' +
            (i === 0 ? ' disabled' : '') + ' title="提高优先级">↑</button>' +
          '<button type="button" class="mv" data-seat-down="' + i + '"' +
            (i === last ? ' disabled' : '') + ' title="降低优先级">↓</button>' +
          '<button type="button" class="mv" data-seat-del="' + i + '" title="移出">×</button>' +
          '</div>';
      }).join('')
    : '<div class="prio-empty">还没有选席别 —— 至少加一个</div>';

  const rest = SEAT_TYPES.filter((s) => !seatValues.includes(s));
  $('#seat-available').innerHTML = rest.length
    ? rest.map((name) =>
        '<button type="button" class="prio-opt" data-seat-add="' + escapeHtml(name) + '">' + escapeHtml(name) + '</button>'
      ).join('')
    : '<div class="prio-empty">都已加入</div>';
}

$('#seat-picked').addEventListener('click', (ev) => {
  const up = ev.target.closest('[data-seat-up]');
  const down = ev.target.closest('[data-seat-down]');
  const del = ev.target.closest('[data-seat-del]');
  if (up) {
    const i = Number(up.dataset.seatUp);
    [seatValues[i - 1], seatValues[i]] = [seatValues[i], seatValues[i - 1]];
  } else if (down) {
    const i = Number(down.dataset.seatDown);
    [seatValues[i + 1], seatValues[i]] = [seatValues[i], seatValues[i + 1]];
  } else if (del) {
    seatValues.splice(Number(del.dataset.seatDel), 1);
  } else {
    return;
  }
  renderSeats();
});

$('#seat-available').addEventListener('click', (ev) => {
  const add = ev.target.closest('[data-seat-add]');
  if (!add) return;
  seatValues.push(add.dataset.seatAdd);
  renderSeats();
});

function setSeats(values) {
  seatValues.length = 0;
  splitCsv(values).forEach((v) => { if (SEAT_TYPES.includes(v) && !seatValues.includes(v)) seatValues.push(v); });
  renderSeats();
}

// --------------------------------------------------------------------------- //
// 车次类型：也改成勾选
// 原来的 <select multiple> 同样要求「按住 Ctrl 点」，而这里本来就是随手勾几个的语义。
// --------------------------------------------------------------------------- //
function renderTrainTypes(selected) {
  const wanted = new Set(selected || []);
  $('#train-types-box').innerHTML = TRAIN_TYPE_OPTIONS.map((name) =>
    '<label><input type="checkbox" value="' + escapeHtml(name) + '"' +
    (wanted.has(name) ? ' checked' : '') + '><span>' + escapeHtml(name) + '</span></label>'
  ).join('');
  $('#t-types').value = TRAIN_TYPE_OPTIONS.filter((n) => wanted.has(n)).join(',');
}

$('#train-types-box').addEventListener('change', () => {
  $('#t-types').value = $$('#train-types-box input:checked').map((i) => i.value).join(',');
});

// --------------------------------------------------------------------------- //
// 表单里的「值列表」：日期 / 站点都用「标签 + 隐藏逗号串」表达
//
// 为什么不让用户直接敲：任务允许配多个日期、多个起止站，而**站名必须与 12306
// 一字不差** —— 手敲「广州南」很容易写成「广州」或「广州南站」，
// 结果是任务跑起来一条也查不到，还很难看出是站名写错了。
// 所以日期给日期控件、站名从站点表里搜出来点选。
// --------------------------------------------------------------------------- //
function createTokenField({ hiddenId, chipsId, emptyText = '还没有添加' }) {
  const hidden = $(hiddenId);
  const chipsBox = $(chipsId);
  const values = [];

  function render() {
    hidden.value = values.join(',');
    chipsBox.innerHTML = values.length
      ? values.map((v) =>
          '<span class="chip">' + escapeHtml(v) +
          '<button type="button" class="chip-x" data-value="' + escapeHtml(v) + '" title="移除">×</button></span>'
        ).join('')
      : '<span class="chip-empty">' + escapeHtml(emptyText) + '</span>';
  }

  const field = {
    values,
    render,
    add(raw) {
      const value = String(raw || '').trim();
      if (!value) return false;
      if (values.includes(value)) { toast('「' + value + '」已经在列表里了'); return false; }
      values.push(value);
      render();
      return true;
    },
    remove(value) {
      const index = values.indexOf(value);
      if (index >= 0) { values.splice(index, 1); render(); }
    },
    text() { return values.join(','); },
    setText(text) {
      values.length = 0;
      splitCsv(text).forEach((v) => { if (!values.includes(v)) values.push(v); });
      render();
    },
    clear() { values.length = 0; render(); },
  };
  render();
  return field;
}

const datesField = createTokenField({ hiddenId: '#t-dates', chipsId: '#dates-chips', emptyText: '还没有添加日期' });
const fromField = createTokenField({ hiddenId: '#t-from', chipsId: '#from-chips', emptyText: '还没有添加出发站' });
const toField = createTokenField({ hiddenId: '#t-to', chipsId: '#to-chips', emptyText: '还没有添加到达站' });

function bindChipRemove(chipsId, field) {
  $(chipsId).addEventListener('click', (ev) => {
    const button = ev.target.closest('.chip-x');
    if (button) field.remove(button.dataset.value);
  });
}
bindChipRemove('#dates-chips', datesField);
bindChipRemove('#from-chips', fromField);
bindChipRemove('#to-chips', toField);

// ---- 车次白/黑名单：也用标签 ----
// 原来是一个逗号分隔的文本框：想删掉中间某一趟车，得在字符串里自己找那一段再删干净。
// 换成标签后每个车次是一条，点 × 就没了。
const whiteField = createTokenField({ hiddenId: '#t-white', chipsId: '#white-chips', emptyText: '不限车次（留空即可）' });
const blackField = createTokenField({ hiddenId: '#t-black', chipsId: '#black-chips', emptyText: '没有排除任何车次' });
bindChipRemove('#white-chips', whiteField);
bindChipRemove('#black-chips', blackField);

// ---- 白/黑名单分段切换 ----
// 两个名单本来就互斥（同一趟车不能同时在两边），并排铺开只是让页面更长，
// 还要使用者自己比对两串车次号。改成一次看一个，**数量写在按钮上**，
// 另一边的状态一眼可知（「只抢这些（2）」「不抢这些 · 无」）。
function refreshTrainSegments() {
  const white = whiteField.values().length;
  const black = blackField.values().length;
  const whiteBtn = $('#train-segment .seg-btn[data-pane="white"]');
  const blackBtn = $('#train-segment .seg-btn[data-pane="black"]');
  if (whiteBtn) whiteBtn.textContent = white ? '只抢这些（' + white + '）' : '只抢这些 · 不限';
  if (blackBtn) blackBtn.textContent = black ? '不抢这些（' + black + '）' : '不抢这些 · 无';
}

function showTrainPane(which) {
  $$('#train-segment .seg-btn').forEach((btn) => btn.classList.toggle('active', btn.dataset.pane === which));
  $$('.seg-pane').forEach((pane) => {
    if (pane.dataset.pane) pane.classList.toggle('hidden', pane.dataset.pane !== which);
  });
  // 切过去的那个名单，输入框要重新拿一次宽度（隐藏时它没被布局过）
  const input = which === 'black' ? $('#t-black-search') : $('#t-white-search');
  if (input) input.focus({ preventScroll: true });
}

$$('#train-segment .seg-btn').forEach((btn) => {
  btn.addEventListener('click', () => showTrainPane(btn.dataset.pane));
});

// 计数跟着标签变化走 —— 用 MutationObserver 盯着标签容器，
// 这样「拉取后合并」「载入任务回填」「清空表单」这些路径都不用各自记得刷新计数
['#white-chips', '#black-chips'].forEach((id) => {
  const host = $(id);
  if (host) new MutationObserver(refreshTrainSegments).observe(host, { childList: true });
});
refreshTrainSegments();

// 也允许手动录车次号（老用户习惯抄车次，不能因为有了拉取就把这条路堵死）
function bindManualTrainInput(inputId, field) {
  $(inputId).addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter') return;
    ev.preventDefault();   // 表单里的回车默认是提交，这里改成「加车次」
    const raw = $(inputId).value.trim();
    if (!raw) return;
    let added = 0;
    splitCsv(raw).forEach((code) => { if (field.add(code.toUpperCase())) added += 1; });
    if (added) {
      $(inputId).value = '';
      toast('已加入 ' + added + ' 个车次', 'ok');
    }
  });
}
bindManualTrainInput('#t-white-search', whiteField);
bindManualTrainInput('#t-black-search', blackField);

// ---- 运行参数：默认收起 ----
// 这一组全是「有默认值、不知道就别动」的项。默认摊开的结果是
// 使用者要在一屏看不完的表单里找「乘车日期」在哪。
function setAdvOpen(open) {
  $('#adv-body').classList.toggle('hidden', !open);
  $('#adv-toggle').textContent = open ? '收起设置' : '展开设置';
  $('#adv-toggle').setAttribute('aria-expanded', String(open));
}

$('#adv-toggle').addEventListener('click', () => {
  setAdvOpen($('#adv-body').classList.contains('hidden'));
});

// ---- 乘车日期：日期控件 + 添加（支持连加多天） ---- //
function isoDate(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate());
}

function resetDatePicker() {
  const picker = $('#date-pick');
  // 已经过去的日期 12306 一定查不到 —— 在控件上直接禁掉，从源头选不出来
  picker.min = isoDate(new Date());
  if (!picker.value) picker.value = isoDate(new Date());
}

// 日期能不能用：能用返回空串，不能用返回**原因**（给人看的话）。
// 与桌面端 DateField、后端 TaskConfig.validate 判的是同一件事。
// ⚠️ 只拦「已经过去」，**不拦「未开售」**：未开售会随时间变成可查。
function dateProblem(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(value || ''))) return '日期格式要 YYYY-MM-DD';
  const picked = new Date(value + 'T00:00:00');
  if (Number.isNaN(picked.getTime()) || isoDate(picked) !== value) {
    return '「' + value + '」不是一个真实存在的日期';
  }
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  if (picked < today) return value + ' 已经过去了 —— 12306 查不到，请选今天及以后';
  return '';
}

$('#date-add').addEventListener('click', () => {
  const value = $('#date-pick').value;
  if (!value) { toast('请先在日期控件里选一个日期', 'err'); return; }
  const problem = dateProblem(value);
  if (problem) { toast(problem, 'err'); return; }
  if (datesField.add(value)) toast('已添加日期 ' + value, 'ok');
});

$('#date-add-3').addEventListener('click', () => {
  const base = $('#date-pick').value;
  if (!base) { toast('请先在日期控件里选一个日期', 'err'); return; }
  const problem = dateProblem(base);
  if (problem) { toast(problem, 'err'); return; }
  const start = new Date(base + 'T00:00:00');
  let added = 0;
  for (let i = 0; i < 3; i++) {
    if (datesField.add(isoDate(new Date(start.getTime() + i * 86400000)))) added += 1;
  }
  toast(added ? '已连加 ' + added + ' 天：' + datesField.text() : '这三天都已经在列表里了', added ? 'ok' : '');
});

// ---- 出发站 / 到达站：从 12306 站点表搜索后点选 ---- //
async function searchStations(keyword, optionsEl) {
  optionsEl.classList.remove('hidden');
  optionsEl.innerHTML = '<div class="muted small" style="padding:6px 8px">正在查询站点表…</div>';
  try {
    const data = await api('/api/stations?kw=' + encodeURIComponent(keyword));
    const items = data.items || [];
    if (!items.length) {
      optionsEl.innerHTML = '<div class="muted small" style="padding:6px 8px">' +
        (keyword ? '没有匹配「' + escapeHtml(keyword) + '」的站点，换个关键字试试' : '站点表里没有可用的常用站') +
        '</div>';
      return;
    }
    optionsEl.innerHTML = items.map((s) =>
      '<button type="button" class="station-opt" data-name="' + escapeHtml(s.name) + '">' +
      '<span>' + escapeHtml(s.name) + '</span>' +
      '<span class="muted small">' + escapeHtml(s.code) + '</span></button>'
    ).join('');
  } catch (err) {
    optionsEl.innerHTML = '<div class="muted small" style="padding:6px 8px">' + escapeHtml(err.message) + '</div>';
  }
}

function bindStationSearch(inputId, buttonId, optionsId, field) {
  const input = $(inputId);
  const options = $(optionsId);
  const run = () => searchStations(input.value.trim(), options);
  $(buttonId).addEventListener('click', run);
  input.addEventListener('keydown', (ev) => {
    // 表单里的回车默认会提交，这里改成「搜站点」
    if (ev.key === 'Enter') { ev.preventDefault(); run(); }
  });
  options.addEventListener('click', (ev) => {
    const button = ev.target.closest('.station-opt');
    if (!button || button.disabled) return;
    if (field.add(button.dataset.name)) {
      // 加入后置灰而不是收起面板：同一趟车往往要连着加「广州」「广州南」几个站
      button.disabled = true;
      button.classList.add('added');
      toast('已加入：' + button.dataset.name, 'ok');
    }
  });
}
bindStationSearch('#from-search', '#from-search-btn', '#from-options', fromField);
bindStationSearch('#to-search', '#to-search-btn', '#to-options', toField);

function hideStationOptions() {
  ['#from-options', '#to-options'].forEach((selector) => $(selector).classList.add('hidden'));
}

function taskFormToPayload() {
  return {
    name: $('#t-name').value.trim(),
    left_dates: $('#t-dates').value.trim(),
    from_stations: $('#t-from').value.trim(),
    to_stations: $('#t-to').value.trim(),
    trains_whitelist: $('#t-white').value.trim(),
    trains_blacklist: $('#t-black').value.trim(),
    train_types: splitCsv($('#t-types').value),
    seat_types: splitCsv($('#t-seats').value),
    depart_time_range: $('#t-time').value.trim(),
    passengers: $('#t-passengers').value.trim(),
    accounts: $('#t-accounts').value.trim(),
    strategy: $('#t-strategy').value,
    query_interval: parseFloat($('#t-interval').value || '1'),
    sale_open_time: $('#t-sale').value.trim(),
    sale_prepare_seconds: parseInt($('#t-prepare').value || '30', 10),
    enabled: $('#t-enabled').checked,
  };
}

// --------------------------------------------------------------------------- //
// 表单校验：标红到具体那一格，并把视野带过去
//
// 之前只有一句 toast「请至少添加一个乘车日期」——而表单要滚两屏才看得完，
// 人根本不知道说的是哪一格。现在把出问题的地方标红 + 滚到眼前。
// --------------------------------------------------------------------------- //
function clearFieldErrors() {
  $$('.field-block.invalid').forEach((el) => {
    el.classList.remove('invalid');
    const err = el.querySelector('.field-error');
    if (err) err.textContent = '';
  });
  $$('label.invalid').forEach((el) => el.classList.remove('invalid'));
  $$('.field-error').forEach((el) => { el.textContent = ''; });
}

function markField(name, message) {
  const block = $('.field-block[data-field="' + name + '"]');
  if (!block) return;
  block.classList.add('invalid');
  const err = block.querySelector('.field-error');
  if (err) err.textContent = message;
  scrollToField(block);
}

function markInput(selector, message) {
  const input = $(selector);
  if (!input) return;
  const label = input.closest('label') || input;
  label.classList.add('invalid');
  let err = label.querySelector('.field-error');
  if (!err) {
    err = document.createElement('p');
    err.className = 'field-error';
    label.appendChild(err);
  }
  err.textContent = message;
  scrollToField(label);
}

let scrolledField = null;
function scrollToField(el) {
  // 只把**第一个**出错的字段滚到眼前：一次滚三处，人会以为页面在乱跳
  if (scrolledField) return;
  scrolledField = el;
  setTimeout(() => {
    el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    scrolledField = null;
  }, 0);
}

$('#task-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  clearFieldErrors();
  // 日期与站点是标签控件（底层 hidden input），浏览器自带的 required 管不到它们，
  // 所以在这里统一校验一遍。
  const missing = [];
  if (!$('#t-name').value.trim()) { markInput('#t-name', '任务名称不能为空'); missing.push('任务名称'); }
  if (!datesField.values.length) { markField('dates', '至少添加一个乘车日期'); missing.push('乘车日期'); }
  if (!fromField.values.length) { markField('from', '至少添加一个出发站'); missing.push('出发站'); }
  if (!toField.values.length) { markField('to', '至少添加一个到达站'); missing.push('到达站'); }
  if (!seatValues.length) { markField('seats', '至少选一个席别'); missing.push('席别'); }
  if (missing.length) {
    toast('还差这些没填：' + missing.join('、') + '（已标红，请照着改）', 'err');
    return;
  }
  const id = $('#task-id').value;
  const payload = taskFormToPayload();
  try {
    await withBusy('#task-submit', '保存中…', async () => {
      if (id) {
        await api('/api/tasks/' + id, { method: 'PUT', body: payload });
        toast('任务已更新（运行中任务配置热生效）', 'ok');
      } else {
        await api('/api/tasks', { method: 'POST', body: payload });
        toast('任务已创建。别停在总览页 —— 点任务卡片上的「启动」它才会开始抢票', 'ok');
      }
    });
    resetTaskForm();
    refreshTasks();
    loadDashboard();
  } catch (err) { toast(err.message, 'err'); }
});

$('#task-reset').addEventListener('click', resetTaskForm);

function resetTaskForm() {
  $('#task-form').reset();
  $('#task-id').value = '';
  $('#t-interval').value = '1.0';
  $('#t-prepare').value = '30';
  $('#t-enabled').checked = true;
  $('#task-submit').textContent = '保存任务';
  clearFieldErrors();
  // form.reset() 只清得掉 hidden input 的 value，标签本身要重新渲染
  datesField.clear();
  fromField.clear();
  toField.clear();
  whiteField.clear();
  blackField.clear();
  setSeats(DEFAULT_SEATS.join(','));
  renderTrainTypes([]);
  setAdvOpen(false);
  resetDatePicker();
  hideStationOptions();
  hideTrainPicker();
}

async function fillTaskForm(id) {
  const data = await api('/api/tasks/' + id);
  const t = data.task || {};
  $('#task-id').value = t.id;
  $('#t-name').value = t.name || '';
  datesField.setText((t.left_dates || []).join(','));
  fromField.setText((t.from_stations || []).join(','));
  toField.setText((t.to_stations || []).join(','));
  whiteField.setText((t.trains_whitelist || []).join(','));
  blackField.setText((t.trains_blacklist || []).join(','));
  renderTrainTypes(t.train_types || []);
  setSeats((t.seat_types || []).join(',') || DEFAULT_SEATS.join(','));
  $('#t-time').value = (t.depart_time_range || []).join(',');
  $('#t-passengers').value = (t.passengers || []).join(',');
  $('#t-accounts').value = (t.accounts || []).join(',');
  $('#t-strategy').value = t.strategy || 'direct';
  $('#t-interval').value = t.query_interval || 1.0;
  $('#t-sale').value = t.sale_open_time || '';
  $('#t-prepare').value = t.sale_prepare_seconds || 30;
  $('#t-enabled').checked = !!t.enabled;
  $('#task-submit').textContent = '更新任务';
  // 编辑已有任务时把运行参数摊开：这一组里很可能有他上次特意改过的值，
  // 收起来会让人以为「我设的东西丢了」。
  setAdvOpen(true);
  clearFieldErrors();
  hideStationOptions();
  hideTrainPicker();
  toast('已载入任务到表单');
}

// --------------------------------------------------------------------------- //
// 车次拉取：白名单 / 黑名单直接从 12306 勾选，不用手敲车次号
//
// 为什么要有：用户根本不知道这条线路当天有哪些车次，只能跑去 12306 网页上
// 一个一个抄，抄错一个字符就变成「任务跑了一整天、一票没抢到」。
// 这里把「填基础信息」和「选车次」连起来：先填日期与起止站，再点「拉取车次」，
// 勾完应用即可，车次号由接口给。
// --------------------------------------------------------------------------- //
let trainOptions = [];           // 最近一次拉取到的车次
const pickedTrains = new Set();  // 当前勾选的车次号

// 与后端 as_list 保持一致的拆分规则（中英文逗号、分号、换行都算分隔符）
function splitCsv(text) {
  return String(text || '').split(/[,，;；\n]/).map((s) => s.trim()).filter(Boolean);
}

function hideTrainPicker() {
  $('#train-picker').classList.add('hidden');
  $('#tp-body').innerHTML = '';
  $('#tp-status').textContent = '';
  $('#tp-count').textContent = '';
  trainOptions = [];
  pickedTrains.clear();
}

function updatePickedCount() {
  const total = trainOptions.length;
  const picked = pickedTrains.size;
  $('#tp-count').textContent = total ? ('已勾选 ' + picked + ' / ' + total + ' 趟') : '';
}

async function pickTrainsFrom12306(refresh) {
  const dates = $('#t-dates').value.trim();
  const from = $('#t-from').value.trim();
  const to = $('#t-to').value.trim();
  if (!dates || !from || !to) {
    // 车次是「按你填的日期和起止站」拉的，缺哪个就直接把他带到哪一格
    clearFieldErrors();
    if (!dates) markField('dates', '先选乘车日期，才知道要拉哪天的车次');
    else if (!from) markField('from', '先选出发站');
    else markField('to', '先选到达站');
    toast('拉取车次需要先填好「乘车日期 / 出发站 / 到达站」', 'err');
    return;
  }
  $('#train-picker').classList.remove('hidden');
  $('#tp-body').innerHTML = '';
  $('#tp-count').textContent = '';
  $('#tp-status').textContent = '正在从 12306 拉取车次…（第一次查询要几秒）';
  try {
    const query = '?dates=' + encodeURIComponent(dates) +
      '&from=' + encodeURIComponent(from) +
      '&to=' + encodeURIComponent(to) +
      (refresh ? '&refresh=1' : '');
    const data = await api('/api/trains/options' + query);
    trainOptions = data.trains || [];
    pickedTrains.clear();
    renderTrainPicker(data);
    updatePickedCount();
  } catch (err) {
    $('#tp-status').textContent = '拉取失败：' + err.message;
    toast(err.message, 'err');
  }
}

function renderTrainPicker(data) {
  const trains = data.trains || [];
  // 拉车次不需要登录：没在线账号时后端会用匿名会话查，这里如实说明。
  const notes = [data.anonymous ? '匿名查询（未登录也能拉，抢票才需要登录）' : ('账号 ' + (data.account || '-')),
    '已查 ' + (data.queried || 0) + '/' + (data.combos || 0) + ' 个组合'];
  if (data.fallback_from) notes.push('账号 ' + data.fallback_from + ' 的会话不可用，已改用匿名查询');
  if (data.cached) notes.push('60 秒内缓存（点「重新拉取」可强制刷新）');
  if (data.truncated) notes.push('组合数超上限，只查了前 ' + (data.combos || 0) + ' 个');
  $('#tp-status').textContent = notes.join(' · ');

  const parts = [];
  if (data.errors && data.errors.length) {
    parts.push('<div class="tp-errors small">部分组合没查到：' + escapeHtml(data.errors.join('；')) + '</div>');
  }
  if (!trains.length) {
    parts.push('<div class="board-empty">没拉到车次。<br>' +
      (data.risk_limited
        ? '当前可能被 12306 限流了 —— 稍等一两分钟再点「重新拉取」，不要连着点。'
        : '请确认上面的乘车日期与起止站没填错（站名要与 12306 一致，用「搜索站点」选最稳），' +
          '再点「重新拉取」。') +
      '</div>');
    $('#tp-body').innerHTML = parts.join('');
    $('#tp-count').textContent = '';
    return;
  }
  const rows = trains.map((t) => {
    const hasTicket = (t.available_seats || []).length > 0;
    return '<tr class="tp-row" data-code="' + escapeHtml(t.train_code) + '">' +
      '<td><input type="checkbox" tabindex="-1"></td>' +
      '<td><strong>' + escapeHtml(t.train_code) + '</strong></td>' +
      '<td>' + escapeHtml(t.train_type) + '</td>' +
      '<td>' + escapeHtml(t.start_time) + '</td>' +
      '<td>' + escapeHtml(t.arrive_time) + '</td>' +
      '<td>' + escapeHtml(t.duration) + '</td>' +
      '<td class="tp-remain' + (hasTicket ? '' : ' none') + '">' + escapeHtml(t.remain_text) + '</td>' +
      '<td class="muted small">' + escapeHtml((t.dates || []).join(' / ')) + '</td>' +
      '</tr>';
  }).join('');
  parts.push('<table class="data-table tp-table"><thead><tr>' +
    '<th></th><th>车次</th><th>类型</th><th>发车</th><th>到达</th><th>历时</th><th>余票</th><th>日期</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table>');
  $('#tp-body').innerHTML = parts.join('');
}

function syncPickedRow(row, checked) {
  const code = row.dataset.code;
  if (checked) pickedTrains.add(code); else pickedTrains.delete(code);
  row.classList.toggle('selected', checked);
  updatePickedCount();
}

function setAllPicked(checked) {
  $$('.tp-row').forEach((row) => {
    const box = row.querySelector('input[type="checkbox"]');
    if (!box) return;
    box.checked = checked;
    syncPickedRow(row, checked);
  });
  updatePickedCount();
}

function applyPickedTrains() {
  if (!pickedTrains.size) {
    toast('还没有勾选任何车次 —— 先点左侧的方框选中要的趟次', 'err');
    return;
  }
  const target = $('input[name="tp-target"]:checked');
  const toBlack = !!target && target.value === 'black';
  const label = toBlack ? '黑名单' : '白名单';
  const otherLabel = toBlack ? '白名单' : '黑名单';
  const field = toBlack ? blackField : whiteField;
  const other = toBlack ? whiteField : blackField;

  const merged = field.values.slice();
  // 按表格顺序（即发车时间）追加：勾选顺序不可靠，但列表顺序就是白名单的优先级
  const picked = trainOptions.filter((t) => pickedTrains.has(t.train_code)).map((t) => t.train_code);
  const added = picked.filter((code) => !merged.includes(code));
  field.setText(merged.concat(added).join(','));

  // 同一车次不能同时出现在两个名单里 —— 后端会直接判配置非法，
  // 与其让用户保存时报错，不如在这里移出并说清楚动了什么。
  const conflict = picked.filter((code) => other.values.includes(code));
  if (conflict.length) {
    other.setText(other.values.filter((code) => !picked.includes(code)).join(','));
  }
  const parts = ['已加入' + label + '：' + (added.join(',') || '（均已存在，无新增）')];
  if (conflict.length) parts.push('已从' + otherLabel + '移出：' + conflict.join(','));
  parts.push('记得点「保存任务」');
  toast(parts.join('；'), conflict.length ? '' : 'ok');
  hideTrainPicker();
}

$('#btn-pick-trains').addEventListener('click', () => pickTrainsFrom12306(false));
$('#btn-pick-trains-black').addEventListener('click', () => {
  // 从黑名单入口进来的人，八成就是想把勾的车次放进黑名单 —— 直接切好默认项
  const black = $('input[name="tp-target"][value="black"]');
  if (black) black.checked = true;
  pickTrainsFrom12306(false);
});
$('#tp-refresh').addEventListener('click', () => pickTrainsFrom12306(true));
$('#tp-apply').addEventListener('click', applyPickedTrains);
$('#tp-close').addEventListener('click', hideTrainPicker);
$('#tp-select-all').addEventListener('click', () => setAllPicked(true));
$('#tp-clear').addEventListener('click', () => setAllPicked(false));
$('#tp-body').addEventListener('click', (ev) => {
  const row = ev.target.closest('.tp-row');
  if (!row) return;
  const box = row.querySelector('input[type="checkbox"]');
  // 点复选框本身时浏览器已切换过一次，其余位置（整行）由这里统一切换
  if (ev.target !== box) box.checked = !box.checked;
  syncPickedRow(row, box.checked);
});

async function refreshTasks() {
  const data = await api('/api/tasks');
  const tasks = data.tasks || [];
  if (!tasks.length) {
    $('#task-items').innerHTML = '<div class="board-empty">还没有任务</div>';
    fillBoardSelect([]);
    return;
  }
  $('#task-items').innerHTML = tasks.map((t) => {
    const rt = t.runtime || {};
    const running = !!rt.running;
    // 乘车日期不合法的，**在列表里就标出来**，不要等使用者启动后发现没反应。
    // 判定规则由后端给（date_hints），前端只负责把它显示出来 —— 规则不能有第二份。
    const hints = t.date_hints || [];
    const hintHtml = hints.map((h) =>
      '<div class="' + (h.verdict === 'past' ? 'alert alert-warn' : 'alert') + '" style="margin:6px 0 0">' +
      escapeHtml(h.text) + '</div>').join('');
    return '<div class="task-item">' +
      '<div class="row"><strong>' + escapeHtml(t.name) + '</strong>' +
      (running ? badge('运行中', 'ok') : badge(rt.state || '已停止', 'gray')) +
      (hints.some((h) => h.verdict === 'past') ? badge('日期已过去', 'err') : '') +
      '</div>' +
      '<div class="meta small muted" style="line-height:1.8">' +
        escapeHtml((t.from_stations || []).join('/')) + ' → ' + escapeHtml((t.to_stations || []).join('/')) +
        ' ｜ ' + (t.left_dates || []).map((d) =>
          (hints.some((h) => h.date === d) ? '<s title="该日期 12306 查不到余票">' + escapeHtml(d) + '</s>' : escapeHtml(d))
        ).join(', ') + '<br>' +
        '席别：' + escapeHtml((t.seat_types || []).join('/')) + ' ｜ 策略：' + escapeHtml(t.strategy) +
        ' ｜ 间隔 ' + t.query_interval + 's' +
        (t.sale_open_time ? '<br>准点开售：' + escapeHtml(t.sale_open_time) : '') +
      '</div>' +
      hintHtml +
      '<div class="actions">' +
        '<button class="tiny primary" data-act="start-task" data-id="' + t.id + '">启动</button>' +
        '<button class="tiny" data-act="stop-task2" data-id="' + t.id + '">停止</button>' +
        '<button class="tiny" data-act="edit-task" data-id="' + t.id + '">编辑</button>' +
        '<button class="tiny danger" data-act="del-task" data-id="' + t.id + '">删除</button>' +
      '</div></div>';
  }).join('');
  fillBoardSelect(tasks);
}

function fillBoardSelect(tasks) {
  const sel = $('#board-task');
  const current = sel.value;
  sel.innerHTML = tasks.map((t) => '<option value="' + t.id + '">' + escapeHtml(t.name) + '</option>').join('');
  if (current && tasks.some((t) => String(t.id) === current)) sel.value = current;
}

// --------------------------------------------------------------------------- //
// 余票看板
// --------------------------------------------------------------------------- //
// boardTimer 在上面统一声明（showLogin 里要能停掉它）

async function loadBoard() {
  const taskId = $('#board-task').value;
  if (!taskId) {
    $('#board-body').innerHTML = '<div class="board-empty">请先创建一个任务</div>';
    return;
  }
  try {
    const data = await api('/api/tasks/' + taskId + '/remain');
    renderBoard(data);
  } catch (err) {
    // 未登录时不把「unauthorized」这种内部字符串糊到界面上
    const msg = err.message === 'unauthorized' ? '登录已过期，请重新登录' : err.message;
    $('#board-body').innerHTML = '<div class="board-empty">' + escapeHtml(msg) + '</div>';
  }
}

function renderBoard(data) {
  const candidates = data.candidates || [];
  const tickets = data.tickets || [];
  const last = data.last_query || null;
  const window_ = data.date_window || {};
  const reason = data.empty_reason || '';

  const heading = '<div class="board-head">' + badge(data.state_text || data.state || '-',
      data.running ? 'info' : 'gray') +
    ' <span class="muted small">账号：' + escapeHtml(data.account || '-') +
    (window_.today ? ' ｜ 12306 可查 ' + escapeHtml(window_.today) + ' ~ ' + escapeHtml(window_.last) : '') +
    '</span></div>';

  if (candidates.length) {
    const rows = candidates.map((c) =>
      '<tr class="hit">' +
        '<td>' + escapeHtml(c.train_code) + '</td>' +
        '<td>' + escapeHtml(c.train_date) + '</td>' +
        '<td>' + escapeHtml(c.from_station) + ' → ' + escapeHtml(c.to_station) + '</td>' +
        '<td>' + escapeHtml(c.start_time) + ' - ' + escapeHtml(c.arrive_time) + '</td>' +
        '<td class="seat-cell has">' + escapeHtml(c.seat_type) + '</td>' +
        '<td>' + escapeHtml(c.remain) + '</td>' +
      '</tr>').join('');
    $('#board-body').innerHTML = heading +
      '<div class="board-note ok">' + candidates.length + ' 个可购席位（会按车次/席别优先级依次下单）</div>' +
      '<table class="data-table"><thead><tr><th>车次</th><th>日期</th><th>区间</th><th>时刻</th><th>席别</th><th>余票</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>';
    return;
  }

  // 没有可购席位时，**必须说清是哪一步空的**，否则「什么都没有」等于没说。
  // 这一屏的四种成因与对应动作见 runtime/context.py::task_remain 的注释。
  let summary = '<div class="board-note warn">' + escapeHtml(reason || '暂未命中可购席位') + '</div>';
  if (last) {
    summary += '<div class="board-note muted small">最近一轮：' +
      '查到 ' + (last.train_count || 0) + ' 个车次 ｜ ' +
      (last.available_seat_count || 0) + ' 个席别有票 ｜ ' +
      '查询 ' + (last.combos || 0) + ' 个组合 ｜ ' +
      (last.latency_ms || 0) + 'ms' +
      '（' + escapeHtml(new Date((last.queried_at || 0) * 1000).toLocaleTimeString()) + '）' +
      '</div>';
  }

  if (tickets.length) {
    // 把「查到了什么」摊开：使用者一眼就能看出是席别不对、还是车次不对
    const rows = tickets.map((t) => {
      const names = Object.keys(t.seats || {});
      const seats = names.length
        ? names.map((name) => {
            const s = t.seats[name] || {};
            const mark = s.available ? 'has' : (s.standby_only ? 'warn' : 'none');
            return '<span class="seat-cell ' + mark + '">' + escapeHtml(name) + ' '
              + escapeHtml(s.remain || '-') + '</span>';
          }).join(' ')
        : '<span class="muted small">-</span>';
      const flag = t.not_yet_on_sale
        ? ' <span class="muted small">（未到起售时间）</span>'
        : (t.buyable ? '' : ' <span class="muted small">（不可网订）</span>');
      return '<tr>' +
        '<td>' + escapeHtml(t.train_code) + flag + '</td>' +
        '<td>' + escapeHtml(t.train_date) + '</td>' +
        '<td>' + escapeHtml(t.from_station) + ' → ' + escapeHtml(t.to_station) + '</td>' +
        '<td>' + escapeHtml(t.start_time) + ' - ' + escapeHtml(t.arrive_time) + '</td>' +
        '<td>' + seats + '</td>' +
        '</tr>';
    }).join('');
    summary += '<div class="board-note muted small">下面是这一轮查到的全部车次（含无票席别），可用来核对是不是车次/席别配错了：</div>' +
      '<table class="data-table"><thead><tr><th>车次</th><th>日期</th><th>区间</th>' +
      '<th>时刻</th><th>各席别余票</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  $('#board-body').innerHTML = heading + summary;
}

$('#btn-board-refresh').addEventListener('click', loadBoard);
$('#board-task').addEventListener('change', loadBoard);
$('#board-auto').addEventListener('change', setupBoardTimer);

function setupBoardTimer() {
  clearInterval(boardTimer);
  if ($('#board-auto').checked) boardTimer = setInterval(loadBoard, 3000);
}

// --------------------------------------------------------------------------- //
// 日志
// --------------------------------------------------------------------------- //
let logWs = null;
let logCount = 0;

function connectLogs() {
  if (logWs) { try { logWs.close(); } catch (e) {} }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  logWs = new WebSocket(proto + '://' + location.host + '/api/logs?token=' + encodeURIComponent(getToken()));
  logWs.onopen = () => setConn(true);
  logWs.onclose = () => { setConn(false); setTimeout(connectLogs, 4000); };
  logWs.onmessage = (ev) => {
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === 'log') appendLog(msg.data);
  };
}

function setConn(on) {
  const el = $('#conn-status');
  el.textContent = on ? '实时已连接' : '连接中断，重连中';
  el.className = 'conn ' + (on ? 'on' : 'off');
}

function levelOf(entry) {
  return (entry && entry.level) || 'INFO';
}

function appendLog(entry) {
  if (!entry) return;
  const filter = $('#log-level').value;
  if (filter && levelOf(entry) !== filter) return;
  const view = $('#log-view');
  const line = document.createElement('div');
  line.className = 'log-line ' + levelOf(entry);
  line.innerHTML = '<span class="ts">' + escapeHtml(entry.ts || '') + '</span> ' +
    '<span class="lv">' + escapeHtml((levelOf(entry) + '     ').slice(0, 7)) + '</span> ' +
    '<span class="src">[' + escapeHtml(entry.logger || '') + ']</span> ' +
    escapeHtml(entry.message || '');
  view.appendChild(line);
  logCount++;
  if (logCount > 1200) { view.removeChild(view.firstChild); logCount--; }
  if ($('#log-auto').checked) view.scrollTop = view.scrollHeight;
}

async function loadLogBacklog() {
  const data = await api('/api/logs?limit=300');
  const view = $('#log-view');
  view.innerHTML = '';
  logCount = 0;
  (data.logs || []).forEach(appendLog);
}

$('#log-clear').addEventListener('click', () => { $('#log-view').innerHTML = ''; logCount = 0; });
$('#log-level').addEventListener('change', loadLogBacklog);
$('#log-export').addEventListener('click', (ev) => {
  ev.preventDefault();
  // 用 fetch 带鉴权头下载，避免把 token 放进 URL 查询串
  fetch('/api/logs/export', { headers: { Authorization: 'Bearer ' + getToken() } })
    .then((r) => r.blob())
    .then((blob) => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'ticket-helper-12306.log';
      a.click();
      URL.revokeObjectURL(a.href);
    })
    .catch(() => toast('导出失败', 'err'));
});

// --------------------------------------------------------------------------- //
// 事件（出票弹窗 / 验证码）
// --------------------------------------------------------------------------- //
let eventWs = null;

function connectEvents() {
  if (eventWs) { try { eventWs.close(); } catch (e) {} }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  eventWs = new WebSocket(proto + '://' + location.host + '/api/events?token=' + encodeURIComponent(getToken()));
  eventWs.onclose = () => setTimeout(connectEvents, 5000);
  eventWs.onmessage = (ev) => {
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === 'notify') {
      const d = msg.data || {};
      toast((d.title || '通知') + '\n' + (d.body || ''), d.critical ? 'ok' : '');
      if (d.critical) { beep(4); loadDashboard(); }
    } else if (msg.type === 'user_event') {
      const d = msg.data || {};
      const who = d.user || d.username || '';
      if (msg.event === 'login_success') { toast('账号 ' + who + ' 登录成功', 'ok'); }
      else if (msg.event === 'login_failed') { toast('账号 ' + who + ' 登录失败：' + (d.error || '请点「测试登录」查看原因'), 'err'); }
      else if (msg.event === 'login_skipped') { /* 扫码账号跳过密码登录，界面上已用按钮提示 */ }
      else if (msg.event === 'risk') { toast('账号 ' + who + ' 触发风控：' + (d.reason || ''), 'err'); }
      // 状态变了就刷新列表与概览，用户不需要手动刷新
      if ($('#page-accounts').classList.contains('active')) loadAccounts().catch(() => {});
      if ($('#page-dashboard').classList.contains('active')) loadDashboard().catch(() => {});
    } else if (msg.type === 'task_status') {
      if ($('#page-dashboard').classList.contains('active')) renderTaskCards().catch(() => {});
    }
  };
}

async function pollCaptcha() {
  try {
    const data = await api('/api/captcha/pending');
    const tasks = data.tasks || [];
    if (tasks.length) {
      const t = tasks[0];
      $('#captcha-img').src = 'data:image/png;base64,' + t.image;
      $('#captcha-modal').classList.remove('hidden');
      $('#captcha-modal').dataset.taskId = t.id;
    } else {
      $('#captcha-modal').classList.add('hidden');
    }
  } catch (e) { /* 静默 */ }
}

$('#captcha-ok').addEventListener('click', async () => {
  const id = $('#captcha-modal').dataset.taskId;
  const answer = $('#captcha-answer').value.trim();
  if (!answer) return toast('请输入验证码坐标', 'err');
  await api('/api/captcha/submit', { method: 'POST', body: { id, answer } });
  $('#captcha-answer').value = '';
  $('#captcha-modal').classList.add('hidden');
});

$('#captcha-skip').addEventListener('click', () => $('#captcha-modal').classList.add('hidden'));

// --------------------------------------------------------------------------- //
// 通知 / 订单 / 设置
// --------------------------------------------------------------------------- //
async function loadNotify() {
  const data = await api('/api/notifications');
  const channels = (data.channels && data.channels.channels) || [];
  // 接口给的是渠道 id（email / serverchan ...），直接显示就是让用户看英文标识符
  const NAMES = {
    web: '本机弹窗', email: '邮件', serverchan: 'Server酱',
    dingtalk: '钉钉机器人', wecom: '企业微信群机器人', telegram: 'Telegram', voice: '电话语音',
  };
  $('#notify-cards').innerHTML = channels.map((c) => {
    const label = NAMES[c.name] || c.name;
    const off = !c.enabled;
    return '<div class="channel-card' + (off ? ' off' : '') + '">' +
      '<div class="channel-head">' +
        '<span class="channel-name">' + escapeHtml(label) + '</span>' +
        badge(c.enabled ? (c.critical_only ? '已启用（仅重大事件）' : '已启用') : '未启用',
              c.enabled ? 'ok' : 'gray') +
      '</div>' +
      '<div class="channel-stats">' +
        '<span>成功 <b>' + (c.sent || 0) + '</b></span>' +
        '<span>失败 <b>' + (c.failed || 0) + '</b></span>' +
      '</div>' +
      (c.last_error ? '<div class="channel-error">最近错误：' + escapeHtml(c.last_error) + '</div>' : '') +
      '<div class="channel-foot">' +
        '<button class="tiny" data-act="test-channel" data-name="' + escapeHtml(c.name) + '">测试这个渠道</button>' +
      '</div>' +
      '</div>';
  }).join('') || '<div class="board-empty">后台没有报告任何通知渠道</div>';

  $('#notify-table tbody').innerHTML = (data.notifications || []).map((n) =>
    '<tr><td>' + escapeHtml(n.created_at) + '</td><td>' + escapeHtml(n.event_type) + '</td>' +
    '<td>' + escapeHtml(n.title) + '</td><td>' + escapeHtml(NAMES[n.channel] || n.channel) + '</td>' +
    '<td>' + (n.ok ? badge('成功', 'ok') : badge('失败', 'err')) + '</td></tr>').join('');

  $$('[data-act="test-channel"]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = '测试中…';
      try {
        const res = await api('/api/notify/test', { method: 'POST', body: { channel: btn.dataset.name } });
        const label = NAMES[btn.dataset.name] || btn.dataset.name;
        toast(label + (res.ok ? ' 测试成功' : ' 测试失败 —— 失败原因见下方「通知记录」'), res.ok ? 'ok' : 'err');
      } catch (err) {
        toast(err.message, 'err');
      } finally {
        btn.disabled = false;
        btn.textContent = original;
        loadNotify().catch(() => {});
      }
    });
  });
}

async function loadOrders() {
  const data = await api('/api/orders?limit=100');
  $('#orders-table tbody').innerHTML = (data.orders || []).map((o) => {
    const kind = o.status === 'success' ? 'ok' : (o.status === 'failed' ? 'err' : 'warn');
    const pay = o.pay_url ? '<a class="btn tiny" target="_blank" rel="noopener" href="' + escapeHtml(o.pay_url) + '">去支付</a>' : '-';
    return '<tr><td>' + escapeHtml(o.created_at) + '</td><td>' + escapeHtml(o.task_id) + '</td>' +
      '<td>' + escapeHtml(o.train_no) + '</td><td>' + escapeHtml(o.depart_date) + '</td>' +
      '<td>' + escapeHtml(o.seat_type) + '</td><td>' + escapeHtml(o.passenger) + '</td>' +
      '<td>' + badge(o.status, kind) + '</td><td>' + escapeHtml(o.order_no) + '</td><td>' + pay + '</td></tr>';
  }).join('') || '<tr><td colspan="9" class="muted">暂无订单</td></tr>';
}

$('#btn-orders-refresh').addEventListener('click', loadOrders);
$('#btn-orders-unfinished').addEventListener('click', async () => {
  try {
    const data = await api('/api/orders/unfinished');
    toast('拉取到 ' + (data.orders || []).length + ' 个待支付订单');
  } catch (err) { toast(err.message, 'err'); }
});

async function loadSettings() {
  const data = await api('/api/config');
  const cfg = data.config || {};
  const rl = cfg.ratelimit || {};
  $('#rl-adaptive').checked = !!rl.adaptive;
  $('#rl-min').value = rl.min_interval;
  $('#rl-max').value = rl.max_interval;
  $('#rl-proxy').value = rl.proxy_pool || '';

  const cd = cfg.codec || {};
  // 配置里若存着已移除的模式（例如 2026-09-29 删掉的 cloud），
  // select 没有对应选项时 value 会变成空串 —— 会让「保存」提交一个非法值。
  // 这里回落到 free，避免出现"界面看着正常、一保存就报错"的状态。
  const modeSel = $('#cd-mode');
  modeSel.value = cd.mode || 'free';
  if (!modeSel.value) modeSel.value = 'free';
  $('#cd-model').value = cd.local_model_path || '';
  $('#cd-endpoint').value = cd.user_endpoint || '';
  $('#cd-retry').value = cd.retry_limit || 3;
  $('#cd-fallback').checked = !!cd.fallback_enabled;

  const snap = await api('/api/system/snapshot');
  $('#settings-cards').innerHTML = [
    ['版本', data.config && data.config.log_level ? 'v1.0.0' : 'v1.0.0', ''],
    ['数据目录', snap.home, ''],
    ['接口基址', snap.api_base, ''],
    ['运行时长', Math.floor((snap.uptime_seconds || 0) / 60) + ' 分钟', ''],
    ['后台运行', snap.running ? '运行中' : '未启动', snap.running ? 'ok' : 'warn'],
    ['配置版本', snap.config_version, ''],
    ['识别链', (snap.codec && snap.codec.chain || []).map((c) => c.name + (c.available ? '' : '(不可用)')).join(', '), ''],
    ['代理池', snap.rate_limit && snap.rate_limit.proxy && snap.rate_limit.proxy.enabled ? '已启用 ' + snap.rate_limit.proxy.size + ' 个' : '未启用', ''],
  ].map(([k, v, cls]) =>
    '<div class="card"><div class="k">' + k + '</div><div class="v ' + cls + '" style="font-size:15px;word-break:break-all">' + escapeHtml(v) + '</div></div>'
  ).join('');

  const hist = await api('/api/config/history');
  $('#history-table tbody').innerHTML = (hist.history || []).map((h) =>
    '<tr><td>' + escapeHtml(h.changed_at) + '</td><td>' + escapeHtml(h.key) + '</td>' +
    '<td class="small">' + escapeHtml((h.new_value || '').slice(0, 160)) + '</td></tr>').join('') ||
    '<tr><td colspan="3" class="muted">暂无变更记录</td></tr>';
}

$('#ratelimit-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  try {
    await api('/api/config/ratelimit', {
      method: 'PUT',
      body: {
        adaptive: $('#rl-adaptive').checked,
        min_interval: parseFloat($('#rl-min').value || '1'),
        max_interval: parseFloat($('#rl-max').value || '8'),
        proxy_pool: $('#rl-proxy').value.trim(),
      },
    });
    toast('限速配置已热更新生效', 'ok');
  } catch (err) { toast(err.message, 'err'); }
});

$('#codec-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const body = {
    mode: $('#cd-mode').value,
    local_model_path: $('#cd-model').value.trim(),
    user_endpoint: $('#cd-endpoint').value.trim(),
    retry_limit: parseInt($('#cd-retry').value || '3', 10),
    fallback_enabled: $('#cd-fallback').checked,
  };
  try {
    await api('/api/config/codec', { method: 'PUT', body });
    toast('验证码配置已热更新生效', 'ok');
  } catch (err) { toast(err.message, 'err'); }
});

// --------------------------------------------------------------------------- //
// 启动
// --------------------------------------------------------------------------- //
async function bootData() {
  // 先把「选」类控件渲染出来，否则打开任务页会看到一片空白，以为坏了
  setSeats(DEFAULT_SEATS.join(','));
  renderTrainTypes([]);
  setAdvOpen(false);
  resetDatePicker();
  await loadDashboard();
  await loadAccounts();
  await refreshTasks();
  // 看板轮询由 startPolling() 统一负责，这里不重复起定时器
}

(async function main() {
  // 若已有有效令牌，直接进入；否则等登录
  if (!getToken()) { showLogin(); return; }
  try {
    await api('/api/auth/me');
    showApp();
    await bootData();
    connectLogs();
    connectEvents();
  } catch (e) {
    showLogin();
  }
  // 轮询只在「真的登录上了」之后才起（并在掉线时统一停掉，见 showLogin）
  if (getToken()) startPolling();
})();
