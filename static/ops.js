const $ = (s) => document.querySelector(s);
const esc = (v) =>
  String(v ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
const kinds = {
  job: '模型任务',
  research: '正式研究',
  analysis: '已保存分析',
  report: '研究报告',
  experiment: '独立实验',
};
const states = {
  running: '运行中',
  queued: '排队中',
  crawling: '获取中',
  completed: '已完成',
  saved: '已保存',
  no_evidence: '无有效材料',
  failed: '失败',
  paused: '已暂停',
  cancelled: '已取消',
  cancel_requested: '正在取消',
  pause_requested: '等待批次结束',
  resuming: '正在继续',
  interrupted: '运行失联',
  unavailable: '状态暂不可读',
};
const phases = {
  queued: '等待开始采集',
  no_evidence: '未取得有效材料，已保存阻碍说明',
  report_ready: '报告已保存',
  collection_ready: '材料收集完成',
  evaluation_ready: '测试调用结束',
  reporting: '报告生成中',
  collecting: '理解目标、搜索与读取材料',
  report_failed: '报告生成失败',
  report_paused: '报告已暂停',
  feedback_revision: '报告修订中',
  dispatching: '准备整理报告',
};
const date = (v) => (v ? new Date(typeof v === 'number' ? v * 1000 : v).toLocaleString('zh-CN') : '—');
const duration = (v) =>
  v == null ? '—' : Math.floor(Math.max(0, v) / 60) + ' 分 ' + Math.floor(Math.max(0, v) % 60) + ' 秒';
const badge = (i) =>
  '<span class="badge state-' +
  esc(i.status) +
  '">' +
  esc(
    ['collection_ready', 'evaluation_ready'].includes(i.phase)
      ? phases[i.phase]
      : states[i.status] || i.status,
  ) +
  '</span>';
const key = (i) => i.kind + '/' + i.id;
const safeURL = (v) => {
  try {
    const u = new URL(v);
    return ['https:', 'http:'].includes(u.protocol) ? u.href : null;
  } catch {
    return null;
  }
};
const listState = { q: '', kind: '', status: '', period: '', sort: 'updated', page: 1, page_size: 20 };
let pageData = null,
  selected = null,
  detailData = null,
  detailFresh = false,
  activeTab = 'overview',
  openedHere = false,
  savedScroll = 0;
let listSerial = 0,
  detailSerial = 0,
  recordSerial = 0,
  listBusy = false,
  detailBusy = false,
  actionPending = false;
let listController,
  detailController,
  recordController,
  debounce,
  recordPage = 1,
  reportPage = 1,
  reportId = null;
async function getJSON(url, options = {}) {
  return Api.json(url, options);
}
function connection(ok) {
  $('#connection').textContent = ok ? '监控连接正常' : '监控连接中断';
  $('#connection').classList.toggle('online', ok);
}
function saveURL() {
  const u = new URL(location.href);
  Object.entries(listState).forEach(([k, v]) => u.searchParams.set(k, v));
  history.replaceState(history.state, '', u);
}
function restoreURL() {
  const p = new URLSearchParams(location.search);
  for (const k of ['q', 'kind', 'status', 'period', 'sort']) if (p.has(k)) listState[k] = p.get(k);
  listState.page = Math.max(1, Number(p.get('page')) || 1);
  listState.page_size = [10, 20, 50, 100].includes(Number(p.get('page_size')))
    ? Number(p.get('page_size'))
    : 20;
  for (const [id, k] of [
    ['query', 'q'],
    ['kind', 'kind'],
    ['status', 'status'],
    ['period', 'period'],
    ['sort', 'sort'],
    ['pageSize', 'page_size'],
  ])
    $('#' + id).value = listState[k];
}
function listURL() {
  const p = new URLSearchParams(listState);
  p.delete('period');
  if (listState.period) p.set('since', Date.now() / 1000 - Number(listState.period) * 86400);
  return '/api/ops/tasks?' + p;
}
function renderList(v, background) {
  const c = v.counts || {};
  $('#totalCount').textContent = Object.values(c).reduce((a, b) => a + b, 0);
  $('#activeCount').textContent = [
    'running',
    'queued',
    'crawling',
    'resuming',
    'pause_requested',
    'cancel_requested',
  ].reduce((a, k) => a + (c[k] || 0), 0);
  const same = pageData && pageData.items.map(key).join('|') === v.items.map(key).join('|');
  if (background && !same) {
    $('#applyUpdate').hidden = false;
    return false;
  }
  if (!same)
    $('#taskRows').innerHTML = v.items
      .map(
        (i) =>
          '<tr><td><button class="task-open" data-kind="' +
          esc(i.kind) +
          '" data-id="' +
          esc(i.id) +
          '">' +
          esc(i.title) +
          '</button><span class="task-id">' +
          esc(i.id) +
          '</span></td><td>' +
          esc(kinds[i.kind] || i.kind) +
          '</td><td data-cell="status"></td><td><span class="stage-cell" data-cell="stage"></span></td><td><time data-cell="time"></time></td></tr>',
      )
      .join('');
  const rows = $('#taskRows').querySelectorAll('tr');
  v.items.forEach((i, n) => {
    const r = rows[n];
    r.querySelector('.task-open').textContent = i.title;
    const b = r.querySelector('[data-cell=status]');
    if (b.innerHTML !== badge(i)) b.innerHTML = badge(i);
    r.querySelector('[data-cell=stage]').textContent = phases[i.phase] || i.stage || '—';
    r.querySelector('time').textContent = date(i.updated_at);
  });
  pageData = v;
  listState.page = v.page;
  saveURL();
  $('#emptyList').hidden = !!v.items.length;
  $('#listCount').textContent = '共 ' + v.total + ' 条记录';
  $('#pageLabel').textContent = v.page + ' / ' + Math.max(1, Math.ceil(v.total / v.page_size));
  $('#jumpPage').value = v.page;
  $('#jumpPage').max = Math.max(1, Math.ceil(v.total / v.page_size));
  $('#previousPage').disabled = v.page <= 1;
  $('#nextPage').disabled = v.page * v.page_size >= v.total;
  $('#goPage').disabled = false;
  $('#applyUpdate').hidden = true;
  return true;
}
async function refreshList(background = false) {
  if (background && (listBusy || $('#taskDetail').open)) return;
  const serial = ++listSerial;
  listController?.abort();
  listController = new AbortController();
  listBusy = true;
  if (!background) for (const id of ['previousPage', 'nextPage', 'goPage']) $('#' + id).disabled = true;
  $('#taskRows').setAttribute('aria-busy', 'true');
  try {
    const v = await getJSON(listURL(), { controller: listController });
    if (serial !== listSerial) return;
    if (!Array.isArray(v.items)) throw Error('任务目录格式异常');
    if (!renderList(v, background)) {
      $('#taskRows').classList.add('stale');
      $('#listNotice').hidden = false;
      $('#listNotice').textContent = '目录已变化，当前保留原来的浏览位置。点击“目录有变化”读取最新列表。';
      connection(true);
      return;
    }
    $('#taskRows').classList.remove('stale');
    $('#listNotice').hidden = true;
    connection(true);
    $('#snapshotTime').textContent =
      '目录读取于 ' + new Date().toLocaleTimeString('zh-CN') + ' · 正式任务与独立实验分别保留';
  } catch (e) {
    if (serial !== listSerial) return;
    $('#taskRows').classList.add('stale');
    connection(false);
    $('#listNotice').hidden = false;
    $('#listNotice').textContent =
      '目录刷新失败，保留旧记录；不代表任务失败。' + (e.name === 'AbortError' ? '请求超时。' : e.message);
    if (e.status === 404) {
      $('#connection').textContent = '后台版本待更新';
      $('#listNotice').textContent =
        '页面已更新，但后台尚未加载任务目录接口。请重启枝析后台后刷新页面；原任务和报告不会因此消失。';
    } else if (!pageData)
      $('#listNotice').textContent =
        '尚未读取到任务目录，不代表没有任务。' +
        (e.name === 'AbortError' ? '请求超时，请稍后刷新。' : e.message);
    if (!pageData) $('#listCount').textContent = '任务目录未加载';
    for (const id of ['previousPage', 'nextPage', 'goPage']) $('#' + id).disabled = true;
  } finally {
    if (serial === listSerial) {
      listBusy = false;
      $('#taskRows').setAttribute('aria-busy', 'false');
    }
  }
}
function changeFilters() {
  for (const [id, k] of [
    ['query', 'q'],
    ['kind', 'kind'],
    ['status', 'status'],
    ['period', 'period'],
    ['sort', 'sort'],
    ['pageSize', 'page_size'],
  ])
    listState[k] = $('#' + id).value;
  listState.page_size = Number(listState.page_size);
  listState.page = 1;
  saveURL();
  refreshList();
}
$('#filters').onsubmit = (e) => {
  e.preventDefault();
  clearTimeout(debounce);
  changeFilters();
};
$('#query').oninput = () => {
  clearTimeout(debounce);
  debounce = setTimeout(changeFilters, 300);
};
for (const id of ['kind', 'status', 'period', 'sort', 'pageSize']) $('#' + id).onchange = changeFilters;
$('#resetFilters').onclick = () => {
  for (const id of ['query', 'kind', 'status', 'period']) $('#' + id).value = '';
  $('#sort').value = 'updated';
  changeFilters();
};
for (const id of ['refreshList', 'applyUpdate']) $('#' + id).onclick = () => refreshList();
$('#previousPage').onclick = () => {
  listState.page--;
  refreshList();
};
$('#nextPage').onclick = () => {
  listState.page++;
  refreshList();
};
$('#goPage').onclick = () => {
  listState.page = Math.max(1, Number($('#jumpPage').value) || 1);
  refreshList();
};
document.querySelectorAll('[data-quick]').forEach(
  (b) =>
    (b.onclick = () => {
      $('#status').value = b.dataset.quick;
      changeFilters();
    }),
);
function taskURL() {
  return '/api/ops/tasks/' + encodeURIComponent(selected.kind) + '/' + encodeURIComponent(selected.id);
}
function markFresh(fresh, message = '') {
  detailFresh = fresh;
  $('#detailSummary').classList.toggle('stale', !fresh);
  $('#detailNotice').hidden = fresh;
  $('#detailNotice').textContent = message;
  $('#detailActions')
    .querySelectorAll('button[data-action]')
    .forEach((b) => (b.disabled = !fresh || actionPending));
}
function renderDetail(i, initial) {
  detailData = i;
  $('#detailTitle').textContent = i.title || i.goal || i.id;
  $('#detailKind').textContent = kinds[i.kind] || i.kind;
  $('#detailId').textContent = i.id;
  const summary =
    '<div class="summary-grid"><div><span>任务状态</span>' +
    badge(i) +
    '</div><div><span>累计运行</span><b>' +
    duration(i.elapsed_seconds) +
    '</b></div><div><span>当前阶段</span><b>' +
    esc(phases[i.phase] || i.stage || '—') +
    '</b></div><div><span>记录更新时间</span><b>' +
    esc(date(i.updated_at)) +
    '</b></div></div>';
  if ($('#detailSummary').innerHTML !== summary) $('#detailSummary').innerHTML = summary;
  const actions = [];
  if (i.kind === 'experiment') {
    if (i.status === 'running') actions.push(['pause', '完成当前批次后暂停']);
    if (i.status === 'paused' && i.can_resume) actions.push(['resume', '继续任务']);
    if (i.status === 'failed' && i.can_retry) actions.push(['retry', '重试报告']);
  } else if (i.kind === 'job' && i.provider === 'local') {
    if (i.status === 'running') actions.push(['pause', '暂停任务']);
    if (i.status === 'paused') actions.push(['resume', '继续任务']);
  }
  const signature = JSON.stringify([actions, i.open_url, i.project_url, i.status]);
  if ($('#detailActions').dataset.signature !== signature) {
    $('#detailActions').dataset.signature = signature;
    $('#detailActions').innerHTML =
      actions.map(([a, l]) => '<button data-action="' + a + '">' + l + '</button>').join('') +
      (i.open_url
        ? '<a href="' + esc(i.open_url) + '" target="_blank" rel="noopener">在原界面打开成果 / 任务</a>'
        : '') +
      (i.project_url
        ? '<a href="' + esc(i.project_url) + '" target="_blank" rel="noopener">查看所属研究的当前进度</a>'
        : '') +
      (i.status === 'pause_requested' ? '<small>暂停请求已接收，等待当前批次保存。</small>' : '');
  }
  const facts = [
    ['所属类型', kinds[i.kind]],
    ['状态说明', i.stage],
    ['已完成调用', i.completed_calls == null ? null : i.completed_calls + ' / ' + (i.total_calls ?? '—')],
    ['模型', i.model_name],
    ['来源', i.provider],
    ['关联研究', i.project_id],
    ['重试次数', i.attempt],
    ['已取得页面', i.pages?.fetched ?? i.pages_fetched],
    ['模型进程', i.model_pid],
    ['分析进程', i.analyzer_pid],
    ['推理设备', i.inference_device_name],
    ['推理精度', i.inference_dtype],
    ['显存占用 MB', i.gpu_memory_allocated_mb],
    ['正文字符数', i.source_characters],
    ['排队耗时', i.queue_seconds == null ? null : duration(i.queue_seconds)],
    ['阶段耗时', i.stage_elapsed_seconds == null ? null : duration(i.stage_elapsed_seconds)],
    ['错误', i.error],
  ];
  const factsHTML = facts
    .filter(([, v]) => v != null && v !== '')
    .map(([k, v]) => '<dt>' + esc(k) + '</dt><dd>' + esc(v) + '</dd>')
    .join('');
  if ($('#detailFacts').innerHTML !== factsHTML) $('#detailFacts').innerHTML = factsHTML;
  $('#detailPath').textContent = i.artifact_path ? '原始记录：' + i.artifact_path : '';
  const research = ['experiment', 'research'].includes(i.kind) && !!i.project_id;
  document
    .querySelectorAll('[data-tab]')
    .forEach((b) => (b.hidden = b.dataset.tab !== 'overview' && !research));
  const reports = i.reports || [],
    rs = JSON.stringify(reports);
  if (initial || $('#reportList').dataset.signature !== rs) {
    $('#reportList').dataset.signature = rs;
    $('#reportList').innerHTML = reports.length
      ? reports
          .map(
            (r) =>
              '<div><button class="report-link" data-report="' +
              esc(r.id) +
              '">查看报告 V' +
              r.version +
              ' · ' +
              esc(date(r.created_at)) +
              '</button> <a href="' +
              taskURL() +
              '/reports/' +
              encodeURIComponent(r.id) +
              '/download">下载完整 JSON</a></div>',
          )
          .join('')
      : '<p class="muted">当前没有保存的研究报告。采集完成不等于报告生成完成。</p>';
  }
}
async function refreshDetail(initial = false) {
  if (!selected || (detailBusy && !initial)) return;
  const serial = ++detailSerial;
  detailController?.abort();
  detailController = new AbortController();
  detailBusy = true;
  try {
    const i = await getJSON(taskURL(), { controller: detailController });
    if (serial !== detailSerial || !selected) return;
    renderDetail(i, initial);
    markFresh(true);
    connection(true);
  } catch (e) {
    if (serial !== detailSerial || !selected) return;
    markFresh(
      false,
      '无法确认当前状态，以下为旧记录，操作已停用。' + (e.name === 'AbortError' ? '请求超时。' : e.message),
    );
    connection(false);
  } finally {
    if (serial === detailSerial) detailBusy = false;
  }
}
function showTask(kind, id, push = true) {
  savedScroll = scrollY;
  selected = { kind, id };
  detailData = null;
  detailFresh = false;
  activeTab = 'overview';
  reportId = null;
  recordPage = 1;
  listSerial++;
  listController?.abort();
  listBusy = false;
  detailSerial++;
  detailController?.abort();
  detailBusy = false;
  recordSerial++;
  recordController?.abort();
  $('#detailTitle').textContent = '正在读取任务…';
  for (const id of [
    'detailSummary',
    'detailActions',
    'detailFacts',
    'detailPath',
    'reportPreview',
    'recordRows',
    'reportList',
  ])
    $('#' + id).innerHTML = '';
  $('#detailActions').dataset.signature = '';
  $('#reportPager').hidden = true;
  $('#detailNotice').hidden = true;
  $('#detailId').textContent = id;
  $('#detailKind').textContent = kinds[kind] || kind;
  $('#recordQuery').value = '';
  if (push) {
    const u = new URL(location.href);
    u.searchParams.set('task', kind + '/' + id);
    history.pushState({ opsDetail: true }, '', u);
    openedHere = true;
  }
  if (!$('#taskDetail').open) $('#taskDetail').showModal();
  setTab('overview');
  refreshDetail(true);
}
function hideTask() {
  selected = null;
  detailSerial++;
  detailController?.abort();
  detailBusy = false;
  recordSerial++;
  recordController?.abort();
  $('#taskDetail').close();
  scrollTo(0, savedScroll);
  openedHere = false;
}
function closeTask() {
  if (openedHere) {
    history.back();
    return;
  }
  const u = new URL(location.href);
  u.searchParams.delete('task');
  history.replaceState(null, '', u);
  hideTask();
}
$('#taskRows').onclick = (e) => {
  const b = e.target.closest('.task-open');
  if (b) showTask(b.dataset.kind, b.dataset.id);
};
$('#closeDetail').onclick = closeTask;
$('#taskDetail').addEventListener('cancel', (e) => {
  e.preventDefault();
  closeTask();
});
window.addEventListener('popstate', () => {
  restoreURL();
  const task = new URLSearchParams(location.search).get('task');
  if (task) {
    const p = task.indexOf('/');
    showTask(task.slice(0, p), task.slice(p + 1), false);
  } else {
    hideTask();
    refreshList(true);
  }
});
$('#detailActions').onclick = async (e) => {
  const b = e.target.closest('button[data-action]');
  if (!b || !detailFresh || actionPending || b.disabled) return;
  const target = { ...selected };
  actionPending = true;
  markFresh(true);
  const url =
    target.kind === 'experiment'
      ? '/api/ops/experiments/' + encodeURIComponent(target.id) + '/' + b.dataset.action
      : '/api/jobs/' + encodeURIComponent(target.id) + '/' + b.dataset.action;
  try {
    await getJSON(url, { method: 'POST' });
    if (selected && key(selected) === key(target)) await refreshDetail();
  } catch (error) {
    if (selected && key(selected) === key(target))
      markFresh(false, '操作结果暂不能确认，请等待状态刷新后再操作。' + error.message);
  } finally {
    actionPending = false;
    if (selected && key(selected) === key(target))
      $('#detailActions')
        .querySelectorAll('button[data-action]')
        .forEach((x) => (x.disabled = !detailFresh));
  }
};
function setTab(tab) {
  activeTab = tab;
  recordSerial++;
  recordController?.abort();
  document
    .querySelectorAll('[data-tab]')
    .forEach((b) => b.setAttribute('aria-selected', b.dataset.tab === tab));
  $('#overviewPanel').hidden = tab !== 'overview';
  $('#recordsPanel').hidden = !['pages', 'events'].includes(tab);
  $('#reportsPanel').hidden = tab !== 'reports';
  if (['pages', 'events'].includes(tab)) {
    recordPage = 1;
    $('#recordQuery').value = '';
    loadRecords();
  }
}
document.querySelectorAll('[data-tab]').forEach((b) => (b.onclick = () => setTab(b.dataset.tab)));
async function loadRecords() {
  if (!selected) return;
  const serial = ++recordSerial;
  recordController?.abort();
  recordController = new AbortController();
  $('#recordCount').textContent = '正在读取…';
  try {
    const v = await getJSON(
      taskURL() +
        '/records/' +
        activeTab +
        '?' +
        new URLSearchParams({ page: recordPage, q: $('#recordQuery').value }),
      { controller: recordController },
    );
    if (serial !== recordSerial) return;
    recordPage = v.page;
    $('#recordRows').innerHTML =
      v.items
        .map(
          (r) =>
            '<article class="record">' +
            (r.url
              ? '<strong>' +
                esc(r.title || r.url) +
                '</strong>' +
                (safeURL(r.url)
                  ? '<a href="' +
                    esc(safeURL(r.url)) +
                    '" target="_blank" rel="noopener noreferrer">' +
                    esc(r.url) +
                    '</a>'
                  : '') +
                '<small>' +
                esc(states[r.status] || r.status) +
                '</small>' +
                (r.error ? '<p>' + esc(r.error) + '</p>' : '')
              : '<small>' + esc(r.kind) + ' · ' + esc(r.level) + '</small><p>' + esc(r.message) + '</p>') +
            '<time>' +
            esc(date(r.updated_at || r.created_at)) +
            '</time></article>',
        )
        .join('') || '<p class="empty">没有符合条件的记录。</p>';
    $('#recordCount').textContent = '共 ' + v.total + ' 条';
    $('#recordPage').textContent = v.page + ' / ' + Math.max(1, Math.ceil(v.total / v.page_size));
    $('#recordPrevious').disabled = v.page <= 1;
    $('#recordNext').disabled = v.page * v.page_size >= v.total;
  } catch (e) {
    if (serial === recordSerial) $('#recordCount').textContent = '读取失败：' + e.message;
  }
}
$('#recordSearch').onsubmit = (e) => {
  e.preventDefault();
  recordPage = 1;
  loadRecords();
};
$('#reloadRecords').onclick = () => loadRecords();
$('#recordPrevious').onclick = () => {
  recordPage--;
  loadRecords();
};
$('#recordNext').onclick = () => {
  recordPage++;
  loadRecords();
};
$('#reportList').onclick = (e) => {
  const b = e.target.closest('[data-report]');
  if (b) {
    reportId = b.dataset.report;
    reportPage = 1;
    loadReport();
  }
};
async function loadReport() {
  const serial = ++recordSerial;
  recordController?.abort();
  recordController = new AbortController();
  $('#reportPreview').textContent = '正在读取保存版正文，不启动模型…';
  try {
    const v = await getJSON(taskURL() + '/reports/' + encodeURIComponent(reportId) + '?page=' + reportPage, {
      controller: recordController,
    });
    if (serial !== recordSerial) return;
    $('#reportPreview').innerHTML =
      '<h2>' +
      esc(v.title) +
      '</h2><p class="muted">' +
      esc(v.notice) +
      '</p>' +
      (reportPage === 1
        ? '<p>' +
          esc(v.summary) +
          '</p>' +
          (v.summary_truncated ? '<p class="notice">摘要较长，预览显示前 16000 字；可下载完整报告。</p>' : '')
        : '') +
      v.items
        .map(
          (n) =>
            '<section><h3>' +
            esc(n.label || '正文') +
            '</h3><p>' +
            esc(n.note) +
            '</p>' +
            (n.truncated
              ? '<p class="notice">该段较长，当前仅显示前 16000 字；完整内容仍在原报告中。</p>'
              : '') +
            '</section>',
        )
        .join('');
    $('#reportPager').hidden = false;
    $('#reportPage').textContent = reportPage + ' / ' + Math.max(1, Math.ceil(v.total / v.page_size));
    $('#reportPrevious').disabled = reportPage <= 1;
    $('#reportNext').disabled = reportPage * v.page_size >= v.total;
  } catch (e) {
    if (serial === recordSerial) $('#reportPreview').textContent = '读取报告失败：' + e.message;
  }
}
$('#reportPrevious').onclick = () => {
  reportPage--;
  loadReport();
};
$('#reportNext').onclick = () => {
  reportPage++;
  loadReport();
};
async function refreshHealth() {
  try {
    const h = await getJSON('/api/health');
    $('#modelReady').textContent =
      h.local_model_server?.model_name || h.default_model?.name || h.model || '未设置';
  } catch {
    $('#modelReady').textContent = '服务暂不可读';
  }
}
let deviceBusy = false;
const percent = (v) => (typeof v === 'number' && Number.isFinite(v) ? v.toFixed(1) + '%' : '不可用');
const capacity = (used, total, unit) =>
  typeof used === 'number' && typeof total === 'number' && total > 0
    ? (used / unit).toFixed(1) + ' / ' + (total / unit).toFixed(1) + ' GiB'
    : '不可用';
async function refreshDevice() {
  if (deviceBusy || document.hidden || !$('#devicePanel').open) return;
  deviceBusy = true;
  try {
    const d = await getJSON('/api/ops/device');
    const readings = [
      ['CPU（短时采样）', percent(d.cpu_percent)],
      ['内存', capacity(d.memory?.used_bytes, d.memory?.total_bytes, 1024 ** 3)],
    ];
    if (d.gpus?.length)
      d.gpus.forEach((g) => {
        readings.push(
          [g.name + ' · GPU', percent(g.percent)],
          [g.name + ' · 显存', capacity(g.used_mib, g.total_mib, 1024)],
        );
      });
    else readings.push(['GPU', '不可用'], ['显存', '不可用']);
    $('#deviceReadings').innerHTML = readings
      .map(([label, value]) => '<div><span>' + esc(label) + '</span><b>' + esc(value) + '</b></div>')
      .join('');
    $('#deviceTime').textContent =
      '采样于 ' + date(d.sampled_at) + ' · 当前支持 Windows CPU/内存及 NVIDIA GPU';
  } catch {
    $('#deviceReadings').textContent = '设备状态不可用';
    $('#deviceTime').textContent = '本次读取失败，不代表任务失败；稍后自动重试。';
  } finally {
    deviceBusy = false;
  }
}
$('#devicePanel').ontoggle = () => refreshDevice();
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refreshDevice();
});
setInterval(refreshDevice, 15000);
restoreURL();
refreshList();
refreshHealth();
const initialTask = new URLSearchParams(location.search).get('task');
if (initialTask) {
  const p = initialTask.indexOf('/');
  if (p > 0) showTask(initialTask.slice(0, p), initialTask.slice(p + 1), false);
}
setInterval(() => {
  if (document.hidden) return;
  if (selected) refreshDetail();
  else refreshList(true);
}, 5000);
setInterval(() => {
  if (!document.hidden) refreshHealth();
}, 30000);
