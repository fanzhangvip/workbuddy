/* =========================================================
   AI 数据分析师页面逻辑
   负责：发送提问、渲染回答（Markdown）、展示工具调用轨迹
   ========================================================= */
(function () {
  'use strict';

  var chat = document.getElementById('chat');
  var input = document.getElementById('input');
  var sendBtn = document.getElementById('send');
  var welcome = document.getElementById('welcome');

  var busy = false;                  // 是否正在等待回答
  var history = [];                  // 对话历史 [{role, content}]
  var MAX_HISTORY = 6;               // 最多携带的历史消息条数

  // 示例问题，点击即发送
  var SAMPLES = [
    '各省份销售额排名前五是谁？',
    '哪个产品的利润最高？',
    '各渠道的销售额占比是多少？',
    '按月看销售额趋势，哪个月最低？',
    '抖音直播间卖了多少？利润率是多少？',
    '浙江和广东的销售额对比一下'
  ];

  // marked 配置：单行换行当作 <br>，启用 GFM（表格支持）
  marked.setOptions({ breaks: true, gfm: true });

  /** 转义 < 号，避免模型输出里的原始 HTML 注入页面（保留 markdown 的 > 引用语法） */
  function escapeTags(text) {
    return String(text == null ? '' : text).replace(/</g, '&lt;');
  }

  /** 渲染 Markdown 为安全 HTML */
  function renderMarkdown(text) {
    try {
      return marked.parse(escapeTags(text));
    } catch (err) {
      return '<p>' + escapeTags(text) + '</p>';
    }
  }

  function scrollToBottom() {
    chat.scrollTop = chat.scrollHeight;
  }

  function hideWelcome() {
    if (welcome && welcome.parentNode) {
      welcome.parentNode.removeChild(welcome);
    }
    welcome = null;
  }

  /**
   * 追加一条消息。
   * @param {string} role      'user' | 'ai'
   * @param {string} html      已渲染的 HTML
   * @param {Array}  toolCalls 工具调用轨迹（仅 AI 消息有）
   */
  function addMessage(role, html, toolCalls) {
    var msg = document.createElement('div');
    msg.className = 'msg ' + (role === 'user' ? 'user' : 'ai');

    var avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = role === 'user' ? '🙋' : '🤖';

    var bubble = document.createElement('div');
    bubble.className = 'bubble';

    // 工具调用轨迹：让结论来源可核对
    if (toolCalls && toolCalls.length) {
      var trace = document.createElement('div');
      trace.className = 'tools';
      trace.innerHTML = '<span class="tools-title">🔧 已查询数据（' + toolCalls.length + ' 次）</span><br>' +
        toolCalls.map(function (call) {
          return '· ' + escapeTags(call.tool) + '(' +
                 escapeTags(JSON.stringify(call.args)) + ')';
        }).join('<br>');
      bubble.appendChild(trace);
    }

    var body = document.createElement('div');
    body.innerHTML = html;
    bubble.appendChild(body);

    msg.appendChild(avatar);
    msg.appendChild(bubble);
    chat.appendChild(msg);
    scrollToBottom();
    return msg;
  }

  /** 等待中的占位气泡 */
  function addLoading() {
    var msg = document.createElement('div');
    msg.className = 'msg ai';
    msg.innerHTML =
      '<div class="avatar">🤖</div>' +
      '<div class="bubble">' +
        '<span class="typing"><i></i><i></i><i></i></span>' +
        '<span class="typing-text">正在查询数据并分析…</span>' +
      '</div>';
    chat.appendChild(msg);
    scrollToBottom();
    return msg;
  }

  /** 发送提问 */
  function send(question) {
    question = (question || '').trim();
    if (!question || busy) return;

    busy = true;
    sendBtn.disabled = true;
    input.value = '';
    input.style.height = 'auto';
    hideWelcome();

    addMessage('user', renderMarkdown(question));

    // 注意：当前问题单独传，历史里不要再包含它，否则会重复
    var payloadHistory = history.slice(-MAX_HISTORY);
    history.push({ role: 'user', content: question });

    var loading = addLoading();

    fetch('/api/ai/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: question, history: payloadHistory })
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        loading.remove();
        if (res.code !== 0) {
          addMessage('ai', '<p>⚠️ ' + escapeTags(res.msg || '请求失败') + '</p>');
          return;
        }
        addMessage('ai', renderMarkdown(res.data.answer), res.data.toolCalls);
        history.push({ role: 'assistant', content: res.data.answer });
      })
      .catch(function (err) {
        loading.remove();
        addMessage('ai', '<p>⚠️ 请求失败：' + escapeTags(err.message || err) + '</p>');
      })
      .finally(function () {
        busy = false;
        sendBtn.disabled = false;
        input.focus();
      });
  }

  /* ---------------- 示例问题 ---------------- */

  (function renderSamples() {
    var box = document.getElementById('samples');
    if (!box) return;
    box.innerHTML = SAMPLES.map(function (text) {
      return '<span class="sample">' + escapeTags(text) + '</span>';
    }).join('');
    box.addEventListener('click', function (e) {
      if (e.target.classList && e.target.classList.contains('sample')) {
        send(e.target.textContent);
      }
    });
  })();

  /* ---------------- 输入交互 ---------------- */

  // 输入框高度自适应
  input.addEventListener('input', function () {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 150) + 'px';
  });

  // Enter 发送，Shift+Enter 换行
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send(input.value);
    }
  });

  sendBtn.addEventListener('click', function () { send(input.value); });

  input.focus();
})();
