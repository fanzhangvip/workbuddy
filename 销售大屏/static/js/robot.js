/* =========================================================
   右下角 AI 悬浮机器人
   功能：打开 / 最小化 / 输入提问 / 发送 / 加载提示 / 消息滚动
   回答展示：简短结论 + 关键数据依据 + 实际执行的 Pandas 操作
   ========================================================= */
(function () {
  'use strict';

  var robot = document.getElementById('robot');
  var fab = document.getElementById('robotFab');
  var minBtn = document.getElementById('robotMin');
  var msgs = document.getElementById('robotMsgs');
  var form = document.getElementById('robotForm');
  var textInput = document.getElementById('robotText');
  var sendBtn = document.getElementById('robotSend');
  var stateEl = document.getElementById('robotState');

  if (!robot) return;

  var busy = false;              // 是否正在等待回答
  var history = [];              // 对话历史
  var MAX_HISTORY = 4;           // 最多携带的历史条数（控制 token 消耗）
  var loadingTimer = null;

  // 加载提示的轮换文案，让用户知道进行到哪一步
  var LOADING_STEPS = ['正在解析问题…', '正在查询数据…', '正在整理解读…'];

  /** 转义，防止内容注入页面 */
  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function scrollDown() {
    msgs.scrollTop = msgs.scrollHeight;
  }

  function setState(text) {
    if (stateEl) stateEl.textContent = text;
  }

  /* ---------------- 打开 / 最小化 ---------------- */

  function openPanel() {
    robot.classList.add('open');
    textInput.focus();
    scrollDown();
  }

  function closePanel() {
    robot.classList.remove('open');
  }

  fab.addEventListener('click', openPanel);
  minBtn.addEventListener('click', closePanel);

  /* ---------------- 消息渲染 ---------------- */

  function addUser(question) {
    var node = document.createElement('div');
    node.className = 'rb rb-user';
    node.textContent = question;          // 用 textContent，天然防注入
    msgs.appendChild(node);
    scrollDown();
  }

  function addError(message) {
    var node = document.createElement('div');
    node.className = 'rb rb-error';
    node.textContent = '⚠️ ' + message;
    msgs.appendChild(node);
    scrollDown();
  }

  /**
   * 渲染 AI 回答。
   * 数据类问题展示「结论 + 依据 + Pandas」三段；闲聊类只展示一段话。
   */
  function addAi(data) {
    var node = document.createElement('div');

    // 没有调用工具 = 普通对话，直接用文本气泡，不套「结论」标签
    if (!data.usedTool) {
      node.className = 'rb rb-ai rb-chat';
      node.textContent = data.conclusion || '';
      msgs.appendChild(node);
      scrollDown();
      return;
    }

    node.className = 'rb rb-ai';

    var html = '<div class="rb-conclusion">' + esc(data.conclusion) + '</div>';

    // 关键数据依据
    if (data.evidence && data.evidence.length) {
      html += '<div class="rb-evidence-label">关键数据依据</div><ul class="rb-evidence">' +
        data.evidence.map(function (item) {
          return '<li>' + esc(item) + '</li>';
        }).join('') +
        '</ul>';
    }

    // 实际执行的 Pandas 操作（来自工具执行回传，不是模型自述）
    if (data.pandasOps && data.pandasOps.length) {
      html += '<div class="rb-pandas">' +
        '<span class="rb-pandas-label">实际执行的 Pandas 操作</span>' +
        data.pandasOps.map(function (expr) {
          return '<code>' + esc(expr) + '</code>';
        }).join('') +
        '</div>';
    }

    node.innerHTML = html;
    msgs.appendChild(node);
    scrollDown();
  }

  /** 加载提示气泡 */
  function addLoading() {
    var node = document.createElement('div');
    node.className = 'rb rb-ai';
    node.innerHTML = '<span class="rb-loading"><i></i><i></i><i></i></span>' +
                     '<span class="rb-loading-text">' + LOADING_STEPS[0] + '</span>';
    msgs.appendChild(node);
    scrollDown();

    // 轮换文案
    var index = 0;
    var label = node.querySelector('.rb-loading-text');
    loadingTimer = setInterval(function () {
      index = (index + 1) % LOADING_STEPS.length;
      label.textContent = LOADING_STEPS[index];
    }, 4500);

    return node;
  }

  function stopLoading() {
    if (loadingTimer) {
      clearInterval(loadingTimer);
      loadingTimer = null;
    }
  }

  /* ---------------- 发送提问 ---------------- */

  function send(question) {
    question = (question || '').trim();
    if (!question || busy) return;

    busy = true;
    sendBtn.disabled = true;
    textInput.value = '';
    setState('思考中…');

    // 首条消息带的操作提示，发出后就不需要了
    var hint = msgs.querySelector('.robot-hint');
    if (hint) hint.remove();

    addUser(question);

    // 当前问题单独传，历史中不再包含它，避免重复
    var payloadHistory = history.slice(-MAX_HISTORY);
    history.push({ role: 'user', content: question });

    var loading = addLoading();

    fetch('/api/ai/brief', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: question, history: payloadHistory })
    })
      .then(function (response) { return response.json(); })
      .then(function (res) {
        stopLoading();
        loading.remove();

        if (res.code !== 0) {
          addError(res.msg || '请求失败');
          return;
        }
        addAi(res.data);
        history.push({ role: 'assistant', content: res.data.conclusion });
      })
      .catch(function (err) {
        stopLoading();
        loading.remove();
        addError('请求失败：' + (err.message || err));
      })
      .finally(function () {
        busy = false;
        sendBtn.disabled = false;
        setState('已就绪');
        textInput.focus();
      });
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    send(textInput.value);
  });
})();
