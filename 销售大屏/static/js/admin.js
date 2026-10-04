/* =========================================================
   数据管理后台逻辑
   文件信息 / 字段校验 / 数据预览 / 上传覆盖 / 删除
   ========================================================= */
(function () {
  'use strict';

  var selectedFile = null;   // 当前选中的待上传文件

  var el = {
    toast:      document.getElementById('toast'),
    infoGrid:   document.getElementById('infoGrid'),
    fieldChips: document.getElementById('fieldChips'),
    fieldHint:  document.getElementById('fieldHint'),
    dropZone:   document.getElementById('dropZone'),
    fileInput:  document.getElementById('fileInput'),
    dropFile:   document.getElementById('dropFile'),
    pickBtn:    document.getElementById('pickBtn'),
    uploadBtn:  document.getElementById('uploadBtn'),
    deleteBtn:  document.getElementById('deleteBtn'),
    previewWrap: document.getElementById('previewWrap'),
    previewMeta: document.getElementById('previewMeta')
  };

  /* ---------------- 工具 ---------------- */

  function toast(message, type) {
    el.toast.textContent = message;
    el.toast.className = 'toast show ' + (type === 'ok' ? 'ok' : 'err');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () {
      el.toast.className = 'toast';
    }, 6000);
  }

  function infoItem(label, value, cls) {
    return '<div class="info-item">' +
             '<div class="info-label">' + label + '</div>' +
             '<div class="info-value ' + (cls || '') + '">' + value + '</div>' +
           '</div>';
  }

  /** 统一请求封装，返回 data 部分 */
  function api(url, options) {
    return fetch(url, options).then(function (r) {
      return r.json().then(function (body) {
        return { httpOk: r.ok, body: body };
      });
    });
  }

  /* ---------------- 文件信息 ---------------- */

  function loadInfo() {
    return api('/api/data/info').then(function (res) {
      var d = res.body.data || {};

      if (!d.exists) {
        el.infoGrid.innerHTML = infoItem('数据状态', '暂无数据文件', 'muted');
      } else {
        el.infoGrid.innerHTML =
          infoItem('文件名', d.fileName, 'accent') +
          infoItem('文件大小', d.sizeText) +
          infoItem('数据行数', Number(d.rows).toLocaleString('zh-CN')) +
          infoItem('字段数量', d.cols + ' 列') +
          infoItem('修改时间', d.modifiedAt) +
          infoItem('上传时间', d.uploadedAt || '—');
      }

      // 字段完整性：逐项打勾或打叉
      var fields = d.fields || [];
      if (!fields.length) {
        el.fieldChips.innerHTML = '<span class="chip bad"><span class="mark">✕</span>无数据文件</span>';
        el.fieldHint.textContent = '上传数据文件后会自动校验这 7 个必需字段。';
        return;
      }

      el.fieldChips.innerHTML = fields.map(function (f) {
        return '<span class="chip ' + (f.present ? 'ok' : 'bad') + '">' +
                 '<span class="mark">' + (f.present ? '✓' : '✕') + '</span>' + f.name +
               '</span>';
      }).join('');

      el.fieldHint.textContent = d.valid
        ? '✓ 字段完整，共 ' + d.rows + ' 条记录，可正常用于大屏。'
        : '✕ 缺少字段：' + d.missing.join('、') + '，请补齐后重新上传。';
      el.fieldHint.style.color = d.valid ? '#7fd8a8' : '#ff9aa6';

      el.deleteBtn.disabled = false;
    }).catch(function (err) {
      toast('读取文件信息失败：' + err, 'err');
    });
  }

  /* ---------------- 数据预览 ---------------- */

  function loadPreview() {
    return api('/api/data/preview?limit=15').then(function (res) {
      var d = res.body.data || {};

      if (!d.columns || !d.columns.length) {
        el.previewWrap.innerHTML = '<div class="empty">暂无数据，请先上传数据文件</div>';
        el.previewMeta.textContent = '';
        return;
      }

      // 用 textContent 填充，避免表格内容触发 XSS
      var table = document.createElement('table');
      var thead = document.createElement('thead');
      var htrow = document.createElement('tr');
      d.columns.forEach(function (c) {
        var th = document.createElement('th');
        th.textContent = c;
        htrow.appendChild(th);
      });
      thead.appendChild(htrow);
      table.appendChild(thead);

      var tbody = document.createElement('tbody');
      (d.rows || []).forEach(function (row) {
        var tr = document.createElement('tr');
        row.forEach(function (cell) {
          var td = document.createElement('td');
          td.textContent = cell;
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);

      el.previewWrap.innerHTML = '';
      el.previewWrap.appendChild(table);
      el.previewMeta.textContent = '显示前 ' + d.shown + ' 行 / 共 ' + d.total + ' 行';
    }).catch(function (err) {
      toast('读取预览失败：' + err, 'err');
    });
  }

  /** 重新加载信息与预览 */
  function refreshAll() {
    loadInfo();
    loadPreview();
  }

  /* ---------------- 文件选择 ---------------- */

  function setFile(file) {
    selectedFile = file || null;
    if (!selectedFile) {
      el.dropFile.textContent = '未选择文件';
      el.uploadBtn.disabled = true;
      return;
    }
    var kb = (selectedFile.size / 1024).toFixed(1);
    el.dropFile.textContent = '已选择：' + selectedFile.name + '（' + kb + ' KB）';
    el.uploadBtn.disabled = false;
  }

  el.pickBtn.addEventListener('click', function () { el.fileInput.click(); });
  el.dropZone.addEventListener('click', function (e) {
    if (e.target === el.dropZone || e.target.classList.contains('drop-icon') ||
        e.target.classList.contains('drop-text') || e.target.classList.contains('drop-sub')) {
      el.fileInput.click();
    }
  });

  el.fileInput.addEventListener('change', function () {
    setFile(el.fileInput.files[0]);
  });

  // 拖拽上传
  ['dragenter', 'dragover'].forEach(function (name) {
    el.dropZone.addEventListener(name, function (e) {
      e.preventDefault();
      el.dropZone.classList.add('over');
    });
  });
  ['dragleave', 'drop'].forEach(function (name) {
    el.dropZone.addEventListener(name, function (e) {
      e.preventDefault();
      el.dropZone.classList.remove('over');
    });
  });
  el.dropZone.addEventListener('drop', function (e) {
    if (e.dataTransfer && e.dataTransfer.files.length) {
      setFile(e.dataTransfer.files[0]);
    }
  });

  /* ---------------- 上传 ---------------- */

  el.uploadBtn.addEventListener('click', function () {
    if (!selectedFile) return;

    var form = new FormData();
    form.append('file', selectedFile);

    el.uploadBtn.disabled = true;
    el.uploadBtn.textContent = '上传中…';

    api('/api/data/upload', { method: 'POST', body: form })
      .then(function (res) {
        var body = res.body;
        if (body.code === 0) {
          toast(body.msg, 'ok');
          setFile(null);
          el.fileInput.value = '';
          refreshAll();
        } else {
          var extra = (body.data && body.data.found)
            ? '（文件实际字段：' + body.data.found.join('、') + '）' : '';
          toast(body.msg + extra, 'err');
        }
      })
      .catch(function (err) { toast('上传失败：' + err, 'err'); })
      .finally(function () {
        el.uploadBtn.disabled = !selectedFile;
        el.uploadBtn.textContent = '上传并覆盖';
      });
  });

  /* ---------------- 删除 ---------------- */

  el.deleteBtn.addEventListener('click', function () {
    if (!window.confirm('确定要删除当前数据文件吗？\n删除后大屏将显示「暂无数据」。')) return;

    api('/api/data', { method: 'DELETE' })
      .then(function (res) {
        if (res.body.code === 0) {
          toast(res.body.msg, 'ok');
          refreshAll();
        } else {
          toast(res.body.msg, 'err');
        }
      })
      .catch(function (err) { toast('删除失败：' + err, 'err'); });
  });

  /* ---------------- 初始化 ---------------- */

  refreshAll();
})();
