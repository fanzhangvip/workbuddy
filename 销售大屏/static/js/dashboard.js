/* =========================================================
   大屏主逻辑 —— 数据编排与交互
   包含：舞台等比缩放、数字滚动、列表自动轮播、进度条动画、
         卡片高亮轮换、地图加载、定时数据刷新、悬停暂停滚动
   ========================================================= */
(function () {
  'use strict';

  var REFRESH_MS = 60000;   // 定时数据刷新间隔（60 秒）
  var SPOTLIGHT_MS = 2600;  // 指标卡高亮轮换间隔

  var dashboardData = null; // 最近一次拉取的数据
  var mapReady = false;     // 地图数据是否已注册
  var emptyTimer = null;    // 空态下的快速轮询定时器

  /* ---------------- 通用工具 ---------------- */

  /** 千分位 + 指定小数位 */
  function fmt(value, decimals) {
    var n = Number(value) || 0;
    return n.toLocaleString('zh-CN', {
      minimumFractionDigits: decimals || 0,
      maximumFractionDigits: decimals || 0
    });
  }

  /** 金额缩写：万 / 亿 */
  function money(value) {
    var n = Number(value) || 0;
    if (n >= 1e8) return (n / 1e8).toFixed(2) + '亿';
    if (n >= 1e4) return (n / 1e4).toFixed(1) + '万';
    return Math.round(n).toString();
  }

  /* ---------------- 1. 舞台等比缩放（适配 1920x1080） ---------------- */

  function fitStage() {
    var scale = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
    var stage = document.getElementById('stage');
    stage.style.transform = 'scale(' + scale + ')';
    // 水平垂直居中，避免留边不齐
    stage.style.left = Math.max(0, (window.innerWidth - 1920 * scale) / 2) + 'px';
    stage.style.top = Math.max(0, (window.innerHeight - 1080 * scale) / 2) + 'px';
  }

  /* ---------------- 2. 数字滚动 ---------------- */

  function countUp(el, target, from, decimals, duration) {
    duration = duration || 1300;
    var startTs = null;

    function frame(ts) {
      if (startTs === null) startTs = ts;
      var p = Math.min((ts - startTs) / duration, 1);
      var eased = 1 - Math.pow(1 - p, 3);           // easeOutCubic
      el.textContent = fmt(from + (target - from) * eased, decimals);
      if (p < 1) requestAnimationFrame(frame);
      else el.textContent = fmt(target, decimals);  // 收尾对齐精确值
    }
    requestAnimationFrame(frame);
  }

  /* ---------------- 3. 顶部核心指标 ---------------- */

  var KPI_CONFIG = [
    { key: 'revenue',  label: '总销售额', icon: '💰', unit: '元', decimals: 0 },
    { key: 'profit',   label: '总利润',   icon: '📈', unit: '元', decimals: 0 },
    { key: 'quantity', label: '总销量',   icon: '📦', unit: '件', decimals: 0 },
    { key: 'orders',   label: '订单数',   icon: '🧾', unit: '笔', decimals: 0 },
    { key: 'margin',   label: '毛利率',   icon: '⚖️', unit: '%',  decimals: 1 },
    { key: 'avgPrice', label: '客单价',   icon: '🏷️', unit: '元', decimals: 0 }
  ];

  var prevKpi = {};   // 记录上一次的值，让刷新时从旧值滚动到新值

  function renderKpi(kpi) {
    var row = document.getElementById('kpiRow');

    // 只在首次构建 DOM，刷新时仅更新数字
    if (!row.children.length) {
      row.innerHTML = KPI_CONFIG.map(function (c, i) {
        return '<div class="kpi" data-idx="' + i + '">' +
                 '<div class="kpi-icon">' + c.icon + '</div>' +
                 '<div class="kpi-body">' +
                   '<div class="kpi-label">' + c.label + '</div>' +
                   '<div class="kpi-value">' +
                     '<span id="kpi-' + c.key + '">0</span>' +
                     '<span class="kpi-unit">' + c.unit + '</span>' +
                   '</div>' +
                 '</div>' +
               '</div>';
      }).join('');
      // DOM 构建完成后才能启动高亮轮换
      startSpotlight();
    }

    KPI_CONFIG.forEach(function (c) {
      var el = document.getElementById('kpi-' + c.key);
      if (!el) return;
      var target = Number(kpi[c.key]) || 0;
      countUp(el, target, prevKpi[c.key] || 0, c.decimals);
      prevKpi[c.key] = target;
    });
  }

  /* ---------------- 4. 指标卡高亮轮换（图标动态切换） ---------------- */

  var spotlightTimer = null;

  function startSpotlight() {
    var cards = document.querySelectorAll('#kpiRow .kpi');
    if (!cards.length) return;

    var idx = 0;
    cards[0].classList.add('active');
    idx = 1;

    if (spotlightTimer) clearInterval(spotlightTimer);
    spotlightTimer = setInterval(function () {
      cards.forEach(function (c) { c.classList.remove('active'); });
      cards[idx % cards.length].classList.add('active');
      idx++;
    }, SPOTLIGHT_MS);
  }

  /* ---------------- 5. 自动轮播滚动器 ---------------- */

  /**
   * @param {HTMLElement} viewport 可视区
   * @param {HTMLElement} track    内容轨道（内容需复制一份以实现无缝循环）
   * @param {number} speed         每秒滚动像素
   */
  function AutoScroller(viewport, track, speed) {
    this.viewport = viewport;
    this.track = track;
    this.speed = speed;
    this.offset = 0;
    this.paused = false;
    this.lastTs = 0;
    this.raf = null;

    var self = this;
    // 鼠标悬停时暂停滚动
    viewport.addEventListener('mouseenter', function () { self.paused = true; });
    viewport.addEventListener('mouseleave', function () { self.paused = false; });
  }

  /** 内容复制了一份，滚过一半即完成一个循环 */
  AutoScroller.prototype.halfHeight = function () {
    return this.track.scrollHeight / 2 || 1;
  };

  AutoScroller.prototype.tick = function (ts) {
    if (!this.lastTs) this.lastTs = ts;
    var dt = (ts - this.lastTs) / 1000;
    this.lastTs = ts;

    if (!this.paused) {
      this.offset += this.speed * dt;
      var half = this.halfHeight();
      if (this.offset >= half) this.offset -= half;   // 无缝回绕
      this.track.style.transform = 'translateY(' + (-this.offset) + 'px)';
    }

    var self = this;
    this.raf = requestAnimationFrame(function (t) { self.tick(t); });
  };

  AutoScroller.prototype.reset = function () {
    this.offset = 0;
    this.track.style.transform = 'translateY(0px)';
  };

  AutoScroller.prototype.start = function () {
    var self = this;
    this.lastTs = 0;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = requestAnimationFrame(function (t) { self.tick(t); });
  };

  var detailScroller = null;
  var hotScroller = null;

  /* ---------------- 6. 销售明细列表 ---------------- */

  function renderDetail(rows) {
    var track = document.getElementById('detailTrack');

    var html = rows.map(function (r) {
      return '<div class="detail-row detail-grid">' +
        '<span class="date">' + r.date + '</span>' +
        '<span class="prov">' + r.province + '</span>' +
        '<span class="prod" title="' + r.product + '">' + r.product + '</span>' +
        '<span class="chan" title="' + r.channel + '">' + r.channel + '</span>' +
        '<span class="qty">' + r.quantity + '</span>' +
        '<span class="num">' + fmt(r.revenue, 0) + '</span>' +
      '</div>';
    }).join('');

    track.innerHTML = html + html;   // 复制一份用于无缝滚动

    if (!detailScroller) {
      detailScroller = new AutoScroller(
        document.getElementById('detailViewport'), track, 20
      );
      detailScroller.start();
    }
    detailScroller.reset();
  }

  /* ---------------- 7. 地区热榜 ---------------- */

  function renderHot(rows) {
    var track = document.getElementById('hotTrack');
    var max = rows.length ? rows[0].value : 1;

    var html = rows.map(function (r, i) {
      var cls = i < 3 ? 'top' + (i + 1) : '';
      var percent = (r.value / max * 100).toFixed(1);
      return '<div class="hot-row ' + cls + '">' +
        '<div class="hot-rank">' + (i + 1) + '</div>' +
        '<div class="hot-name">' + r.name + '</div>' +
        '<div class="hot-value">' + money(r.value) + '</div>' +
        '<div class="hot-bar"><i data-w="' + percent + '"></i></div>' +
      '</div>';
    }).join('');

    track.innerHTML = html + html;

    // 下一帧再赋宽度，触发进度条生长动画
    requestAnimationFrame(function () {
      track.querySelectorAll('.hot-bar > i').forEach(function (el) {
        el.style.width = el.getAttribute('data-w') + '%';
      });
    });

    if (!hotScroller) {
      hotScroller = new AutoScroller(
        document.getElementById('hotViewport'), track, 16
      );
      hotScroller.start();
    }
    hotScroller.reset();
  }

  /* ---------------- 8. 利润进度 ---------------- */

  function renderProfit(rows) {
    var box = document.getElementById('profitList');

    box.innerHTML = rows.map(function (r) {
      return '<div class="profit-item">' +
        '<div class="profit-head">' +
          '<span class="profit-name">' + r.name + '</span>' +
          '<span class="profit-margin">利润率 ' + r.margin + '%</span>' +
        '</div>' +
        '<div class="profit-bar"><i data-w="' + r.percent + '"></i></div>' +
      '</div>';
    }).join('');

    requestAnimationFrame(function () {
      box.querySelectorAll('.profit-bar > i').forEach(function (el) {
        el.style.width = el.getAttribute('data-w') + '%';
      });
    });
  }

  /* ---------------- 9. 数据加载 ---------------- */

  /** 加载中国地图数据并注册（合规：含台湾、南海诸岛九段线） */
  function loadMap() {
    return fetch('/static/lib/china.json')
      .then(function (r) { return r.json(); })
      .then(function (geo) {
        Charts.registerMap(geo);
        mapReady = true;
        if (dashboardData) Charts.renderMap(dashboardData.map);
      });
  }

  /** 左上角时间显示（取 updatedAt 的时分秒） */
  function updateClock(updatedAt) {
    var t = (updatedAt || '').split(' ')[1] || '--:--:--';
    document.getElementById('updatedAt').textContent = t;
  }

  /** 无数据：清空图表与列表，显示「暂无数据」遮罩 */
  function showEmpty(reason) {
    Charts.clear();
    document.getElementById('detailTrack').innerHTML = '';
    document.getElementById('hotTrack').innerHTML = '';
    document.getElementById('profitList').innerHTML = '';

    KPI_CONFIG.forEach(function (c) {
      var node = document.getElementById('kpi-' + c.key);
      if (node) node.textContent = '0';
      prevKpi[c.key] = 0;
    });

    document.getElementById('emptyReason').textContent = reason || '尚未上传数据文件';
    document.getElementById('emptyMask').classList.add('show');

    // 空态下加快轮询，上传数据后能较快自动恢复
    if (!emptyTimer) emptyTimer = setInterval(loadDashboard, 10000);
  }

  /** 有数据：隐藏遮罩 */
  function hideEmpty() {
    document.getElementById('emptyMask').classList.remove('show');
    if (emptyTimer) { clearInterval(emptyTimer); emptyTimer = null; }
  }

  /** 拉取大屏全部数据 */
  function loadDashboard() {
    return fetch('/api/dashboard')
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.code !== 0) {
          console.error('数据接口返回异常：', res.msg);
          return;
        }
        var data = res.data;
        dashboardData = data;
        updateClock(data.updatedAt);

        // 无数据（文件被删 / 字段不全）时走空态，等下次刷新自动恢复
        if (data.empty) {
          showEmpty(data.reason);
          return;
        }

        hideEmpty();
        renderKpi(data.kpi);
        renderDetail(data.detail);
        renderHot(data.regionHot);
        renderProfit(data.profitProgress);
        Charts.render(data);                 // 四个饼/柱/折线图
        if (mapReady) Charts.renderMap(data.map);
      })
      .catch(function (err) {
        console.error('加载大屏数据失败：', err);
      });
  }

  /* ---------------- 10. 初始化 ---------------- */

  function init() {
    fitStage();

    // 窗口变化：重新缩放舞台并重绘图表
    window.addEventListener('resize', function () {
      fitStage();
      Charts.resize();
    });

    loadMap().catch(function (err) { console.error('地图数据加载失败：', err); });
    loadDashboard();

    // 定时数据刷新
    setInterval(loadDashboard, REFRESH_MS);

    // 从管理后台标签页切回来时立即刷新，不必等定时器
    window.addEventListener('focus', loadDashboard);
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) loadDashboard();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
