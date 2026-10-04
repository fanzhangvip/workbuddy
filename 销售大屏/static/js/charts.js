/* =========================================================
   图表模块 —— 封装所有 ECharts 实例
   对外暴露 window.Charts：
     Charts.init()            初始化非地图图表
     Charts.render(data)      渲染除地图外的图表
     Charts.renderMap(data)   渲染中国地图（需先注册地图数据）
     Charts.resize()          窗口变化时重绘
   ========================================================= */
(function (global) {
  'use strict';

  // ---------------- 主题常量 ----------------
  var TEXT = '#dbe7fb';
  var SUB  = '#8ea3c2';

  // 饼图/玫瑰图配色（蓝青为主，金色点缀）
  var PALETTE = ['#38e0ff', '#4ea8ff', '#2f7fe0', '#7f8cff', '#ffd777',
                 '#35d0a8', '#f08fb0', '#9fb6d6', '#ff9d5c', '#5ec8f0'];

  // 省份中心经纬度（地图涟漪点用）
  var PROVINCE_COORD = {
    '北京': [116.41, 39.90], '天津': [117.20, 39.13], '河北': [114.50, 38.05],
    '山西': [112.55, 37.87], '内蒙古': [111.75, 40.84], '辽宁': [123.43, 41.80],
    '吉林': [125.32, 43.90], '黑龙江': [126.64, 45.76], '上海': [121.47, 31.23],
    '江苏': [118.78, 32.06], '浙江': [120.15, 30.27], '安徽': [117.28, 31.86],
    '福建': [119.30, 26.08], '江西': [115.89, 28.68], '山东': [117.00, 36.65],
    '河南': [113.65, 34.76], '湖北': [114.30, 30.60], '湖南': [112.98, 28.19],
    '广东': [113.27, 23.13], '广西': [108.32, 22.82], '海南': [110.35, 20.02],
    '重庆': [106.55, 29.56], '四川': [104.07, 30.67], '贵州': [106.71, 26.57],
    '云南': [102.71, 25.05], '西藏': [91.13, 29.65], '陕西': [108.95, 34.27],
    '甘肃': [103.82, 36.06], '青海': [101.78, 36.62], '宁夏': [106.27, 38.47],
    '新疆': [87.62, 43.79], '台湾': [121.52, 25.03], '香港': [114.17, 22.32],
    '澳门': [113.55, 22.20]
  };

  var charts = {};        // id -> echarts 实例
  var geoFullNames = [];  // 地图要素全名列表（如「广东省」）

  // ---------------- 工具函数 ----------------

  /** 格式化金额：万 / 亿 */
  function money(value) {
    if (value >= 1e8) return (value / 1e8).toFixed(2) + '亿';
    if (value >= 1e4) return (value / 1e4).toFixed(1) + '万';
    return Math.round(value).toString();
  }

  /** 短省名转地图全名：短名一定是全名前缀，如「广东」->「广东省」 */
  function toFullName(shortName) {
    for (var i = 0; i < geoFullNames.length; i++) {
      if (geoFullNames[i].indexOf(shortName) === 0) return geoFullNames[i];
    }
    return shortName;
  }

  /** 统一的 tooltip 样式 */
  function tooltipStyle() {
    return {
      backgroundColor: 'rgba(10, 16, 28, .94)',
      borderColor: 'rgba(78, 168, 255, .38)',
      borderWidth: 1,
      padding: [8, 12],
      textStyle: { color: TEXT, fontSize: 12 },
      extraCssText: 'backdrop-filter: blur(6px); border-radius: 4px; box-shadow: 0 6px 18px rgba(0,0,0,.5);'
    };
  }

  /** 取得（或懒创建）图表实例 */
  function getChart(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    if (!charts[id]) charts[id] = echarts.init(el);
    return charts[id];
  }

  /** 注册地图数据并记录要素全名 */
  function registerMap(geoJson) {
    echarts.registerMap('china', geoJson);
    geoFullNames = geoJson.features
      .map(function (f) { return f.properties && f.properties.name; })
      .filter(Boolean);
  }

  // ---------------- 各图表渲染 ----------------

  /** 地区占比（环形图） */
  function renderProvince(data) {
    var chart = getChart('chartProvince');
    if (!chart) return;

    chart.setOption({
      tooltip: Object.assign({
        trigger: 'item',
        formatter: function (p) { return p.name + '<br/>销售额：' + money(p.value) + '（' + p.percent + '%）'; }
      }, tooltipStyle()),
      legend: {
        type: 'scroll', orient: 'vertical', right: 6, top: 'center',
        itemWidth: 8, itemHeight: 8, itemGap: 7,
        textStyle: { color: SUB, fontSize: 11 },
        pageIconColor: '#38e0ff', pageIconInactiveColor: '#3d4a60',
        pageTextStyle: { color: SUB, fontSize: 10 }
      },
      color: PALETTE,
      series: [{
        type: 'pie',
        radius: ['46%', '72%'],
        center: ['31%', '52%'],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: 'rgba(8, 12, 20, .9)', borderWidth: 2 },
        label: { show: false },
        labelLine: { show: false },
        emphasis: {
          scale: true, scaleSize: 7,
          itemStyle: { shadowBlur: 16, shadowColor: 'rgba(56, 224, 255, .55)' }
        },
        data: data,
        animationDuration: 1000,
        animationEasing: 'cubicOut'
      }]
    }, true);
  }

  /** 产品排行（横向条形） */
  function renderProduct(data) {
    var chart = getChart('chartProduct');
    if (!chart) return;

    // 倒序，让最大值显示在最上方
    var names = data.map(function (d) { return d.name; }).reverse();
    var values = data.map(function (d) { return d.value; }).reverse();

    chart.setOption({
      grid: { left: 4, right: 62, top: 10, bottom: 2, containLabel: true },
      tooltip: Object.assign({
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: function (ps) { return ps[0].name + '<br/>销售额：' + money(ps[0].value); }
      }, tooltipStyle()),
      xAxis: { type: 'value', show: false },
      yAxis: {
        type: 'category',
        data: names,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: SUB, fontSize: 11 }
      },
      series: [{
        type: 'bar',
        data: values,
        barWidth: 9,
        showBackground: true,
        backgroundStyle: { color: 'rgba(255, 255, 255, .04)', borderRadius: 5 },
        itemStyle: {
          borderRadius: [0, 5, 5, 0],
          color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
            { offset: 0, color: '#2b6fd4' },
            { offset: 1, color: '#38e0ff' }
          ])
        },
        label: {
          show: true, position: 'right', color: TEXT, fontSize: 11,
          formatter: function (p) { return money(p.value); }
        },
        animationDuration: 1200,
        animationDelay: function (i) { return i * 55; }
      }]
    }, true);
  }

  /** 渠道占比（玫瑰图） */
  function renderChannel(data) {
    var chart = getChart('chartChannel');
    if (!chart) return;

    chart.setOption({
      tooltip: Object.assign({
        trigger: 'item',
        formatter: function (p) { return p.name + '<br/>销售额：' + money(p.value) + '（' + p.percent + '%）'; }
      }, tooltipStyle()),
      legend: {
        type: 'scroll', orient: 'vertical', right: 4, top: 'center',
        itemWidth: 8, itemHeight: 8, itemGap: 6,
        textStyle: { color: SUB, fontSize: 11 },
        pageIconColor: '#38e0ff', pageIconInactiveColor: '#3d4a60',
        pageTextStyle: { color: SUB, fontSize: 10 }
      },
      color: PALETTE,
      series: [{
        type: 'pie',
        roseType: 'radius',
        radius: ['24%', '74%'],
        center: ['30%', '52%'],
        itemStyle: { borderColor: 'rgba(8, 12, 20, .9)', borderWidth: 2, borderRadius: 3 },
        label: { show: false },
        labelLine: { show: false },
        emphasis: {
          scale: true, scaleSize: 6,
          itemStyle: { shadowBlur: 16, shadowColor: 'rgba(56, 224, 255, .55)' }
        },
        data: data,
        animationDuration: 1000,
        animationEasing: 'cubicOut'
      }]
    }, true);
  }

  /** 月度趋势（柱 + 折线） */
  function renderTrend(trend) {
    var chart = getChart('chartTrend');
    if (!chart) return;

    var months = trend.months.map(function (m) { return m.slice(2).replace('-', '/'); });

    chart.setOption({
      grid: { left: 6, right: 12, top: 34, bottom: 2, containLabel: true },
      tooltip: Object.assign({
        trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: 'rgba(56, 224, 255, .45)' } },
        formatter: function (ps) {
          var s = ps[0].name + '<br/>';
          ps.forEach(function (p) { s += p.marker + p.seriesName + '：' + money(p.value) + '<br/>'; });
          return s;
        }
      }, tooltipStyle()),
      legend: {
        right: 8, top: 2, itemWidth: 12, itemHeight: 8, itemGap: 14,
        textStyle: { color: SUB, fontSize: 11 }
      },
      xAxis: {
        type: 'category',
        data: months,
        boundaryGap: true,
        axisLine: { lineStyle: { color: 'rgba(120, 155, 205, .25)' } },
        axisTick: { show: false },
        axisLabel: { color: SUB, fontSize: 11 }
      },
      yAxis: {
        type: 'value',
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: SUB, fontSize: 11, formatter: function (v) { return money(v); } },
        splitLine: { lineStyle: { color: 'rgba(120, 155, 205, .10)' } }
      },
      series: [
        {
          name: '销售额',
          type: 'bar',
          barWidth: 14,
          data: trend.revenue,
          itemStyle: {
            borderRadius: [3, 3, 0, 0],
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: '#38e0ff' },
              { offset: 1, color: 'rgba(47, 111, 208, .25)' }
            ])
          }
        },
        {
          name: '利润',
          type: 'line',
          smooth: true,
          symbol: 'circle',
          symbolSize: 6,
          data: trend.profit,
          lineStyle: { width: 2, color: '#ffd777', shadowBlur: 10, shadowColor: 'rgba(255, 215, 119, .5)' },
          itemStyle: { color: '#ffd777', borderColor: '#fff3d0', borderWidth: 1 },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(255, 215, 119, .22)' },
              { offset: 1, color: 'rgba(255, 215, 119, 0)' }
            ])
          }
        }
      ],
      animationDuration: 1100,
      animationEasing: 'cubicOut'
    }, true);
  }

  /** 中国销售地图（分级设色 + 涟漪点） */
  function renderMap(data) {
    var chart = getChart('chartMap');
    if (!chart) return;

    // 无数据时直接清空，避免 Math.max 空数组得到 -Infinity
    if (!data || !data.length) { chart.clear(); return; }

    // 短省名 -> 地图全名
    var mapData = data.map(function (d) {
      return { name: toFullName(d.name), value: d.value };
    });
    var maxValue = Math.max.apply(null, data.map(function (d) { return d.value; }).concat([1]));

    // 涟漪点：销售额前 8 的省份
    var rippleData = data.slice()
      .sort(function (a, b) { return b.value - a.value; })
      .slice(0, 8)
      .filter(function (d) { return PROVINCE_COORD[d.name]; })
      .map(function (d) {
        return { name: toFullName(d.name), value: PROVINCE_COORD[d.name].concat([d.value]) };
      });

    chart.setOption({
      tooltip: Object.assign({
        trigger: 'item',
        formatter: function (p) {
          var value = Array.isArray(p.value) ? p.value[2] : p.value;
          if (value === undefined || isNaN(value)) return p.name;
          return p.name + '<br/>销售额：' + money(value);
        }
      }, tooltipStyle()),

      visualMap: {
        type: 'continuous',
        min: 0,
        max: maxValue,
        left: 10,
        bottom: 14,
        itemWidth: 9,
        itemHeight: 76,
        calculable: false,
        textStyle: { color: SUB, fontSize: 10 },
        formatter: function (v) { return money(v); },
        inRange: { color: ['#10314f', '#1a5f9c', '#2f9fd8', '#38e0ff'] }
      },

      // geo 提供坐标系 + 基础底图（即使分级设色异常，地图轮廓依然可见）
      geo: {
        map: 'china',
        roam: false,
        zoom: 1.16,
        center: [104, 36],
        itemStyle: {
          areaColor: '#0d1a2b',
          borderColor: 'rgba(96, 150, 210, .48)',
          borderWidth: 0.6
        },
        emphasis: {
          itemStyle: { areaColor: '#1d4a75' },
          label: { show: true, color: '#fff', fontSize: 11 }
        },
        label: { show: false },
        // 南海诸岛（九段线）单独描边并标注，符合国标要求
        regions: [{
          name: '南海诸岛',
          itemStyle: { areaColor: 'transparent', borderColor: '#5b8fc7', borderWidth: 1 },
          label: { show: true, color: '#7fa8d4', fontSize: 9 }
        }]
      },

      series: [
        {
          name: '销售额',
          type: 'map',
          geoIndex: 0,
          data: mapData,
          emphasis: {
            itemStyle: { areaColor: '#2a6ea8' },
            label: { color: '#ffffff', fontSize: 11 }
          }
        },
        {
          name: '热点',
          type: 'effectScatter',
          coordinateSystem: 'geo',
          data: rippleData,
          symbolSize: function (val) { return 7 + (val[2] / maxValue) * 11; },
          rippleEffect: { brushType: 'stroke', scale: 3.2, period: 4 },
          itemStyle: {
            color: '#ffd777',
            shadowBlur: 12,
            shadowColor: 'rgba(255, 180, 0, .9)'
          },
          label: { show: false },
          zlevel: 2,
          animationDuration: 1000
        }
      ]
    }, true);
  }

  // ---------------- 对外接口 ----------------
  global.Charts = {
    registerMap: registerMap,
    renderMap: renderMap,

    /** 渲染除地图外的全部图表 */
    render: function (data) {
      renderProvince(data.provinceShare);
      renderProduct(data.productRank);
      renderChannel(data.channelShare);
      renderTrend(data.trend);
    },

    /** 窗口尺寸变化后重绘（缩放由 dashboard.js 处理） */
    resize: function () {
      Object.keys(charts).forEach(function (id) { charts[id].resize(); });
    },

    /** 清空全部图表（无数据时使用） */
    clear: function () {
      Object.keys(charts).forEach(function (id) { charts[id].clear(); });
    }
  };
})(window);
