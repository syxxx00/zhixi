const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const els = {
  composer: $('#composer'),
  loading: $('#loading'),
  workspace: $('#workspace'),
  text: $('#sourceText'),
  url: $('#sourceUrl'),
  count: $('#charCount'),
  error: $('#errorBox'),
  analyze: $('#analyzeBtn'),
  svg: $('#mindMap'),
  viewport: $('#viewport'),
  links: $('#links'),
  nodes: $('#nodes'),
  detail: $('#nodeDetail'),
};
let mode = 'text',
  result = null,
  transform = { x: 70, y: 80, scale: 1 },
  dragging = null;
let currentHistoryId = null,
  editing = false,
  editBackup = null,
  selectedNode = null;
let pendingFiles = [],
  defaultModelPath = '',
  modelRegistry = [],
  editingModelId = null;
let researchFiles = [];
let trackedTasks = Api.storedArray(sessionStorage, 'mindmap_tracked_tasks').filter(
  (task) => task && typeof task.id === 'string',
);
const queuePinStorageKey = 'mindmap_queue_pins_v1';
let queuePins = (() => {
  try {
    const saved = localStorage.getItem(queuePinStorageKey);
    if (saved !== null) {
      const parsed = JSON.parse(saved);
      if (Array.isArray(parsed)) return parsed.filter((pin) => pin && typeof pin.id === 'string');
    }
  } catch { /* Keep the current page usable when storage is unavailable. */ }
  // One-time migration: preserve the queue shown in an already-open browser session.
  return trackedTasks.map((task) => ({ id: task.id, kind: task.kind || 'analysis', label: task.label || task.goal || '' }));
})();
function persistQueuePins() {
  try { localStorage.setItem(queuePinStorageKey, JSON.stringify(queuePins)); } catch { /* In-memory queue still works. */ }
}
persistQueuePins();
trackedTasks = trackedTasks.filter((task) => queuePins.some((pin) => pin.id === task.id));
function isPinned(id) { return queuePins.some((pin) => pin.id === id); }
function pinTask(task) {
  if (!isPinned(task.id)) queuePins.push({ id: task.id, kind: task.kind || 'analysis', label: task.label || task.goal || '' });
  persistQueuePins();
  const existing = trackedTasks.find((item) => item.id === task.id);
  if (existing) Object.assign(existing, task);
  else trackedTasks.unshift(task);
  persistTasks();
  renderTaskDock();
}
function unpinTask(id) {
  queuePins = queuePins.filter((pin) => pin.id !== id);
  trackedTasks = trackedTasks.filter((task) => task.id !== id);
  persistQueuePins();
  persistTasks();
  renderTaskDock();
  if (!$('#historyOverlay').classList.contains('hidden') && $('#historyTrashPanel').classList.contains('hidden'))
    selectHistoryKind(historyKind);
}
let taskTimer = null,
  mapRenderTimer = null,
  currentResearchId = null,
  candidateOffset = 0;
let historyKind = 'analysis',
  currentResearchView = 'sources';
let historyItems = [], historyOffset = 0, historyRequestVersion = 0;
let historyJobItems = [], historyJobOffset = 0, historyJobRequestVersion = 0;
let researchDockWasExpanded = false;
let researchGraphState = {
  mode: 'overview',
  zoom: 1,
  pages: [],
  links: [],
  clusters: new Map(),
  baseWidth: 610,
};
const sample = `人工智能正在改变知识工作，但真正的价值并不只来自生成速度。一个可靠的 AI 工作流首先要明确任务目标，再选择合适的数据与模型，并建立人工复核机制。高质量数据可以提升结果的准确性；结构化提示可以减少歧义；评估指标则帮助团队判断系统是否真的改善了业务。与此同时，团队还需关注隐私、偏见和错误信息。最有效的实践通常不是完全自动化，而是让模型承担重复的信息整理，让人负责判断、沟通与最终决策。`;
const sourceRoleLabels = {
  social: '社交平台',
  community: '社区讨论',
  reference: '资料参考',
  news: '新闻报道',
  official_lead: '官方线索（待核验）',
  primary: '第一方来源',
  general: '一般网页',
  unclassified: '未分类',
};
const sourceRoleLabel = (role) => sourceRoleLabels[role] || role || sourceRoleLabels.unclassified;
const topicLabels = {
  characters: '旧分类：角色',
  enemies: '旧分类：敌人',
  items: '旧分类：道具/资源',
  stages: '旧分类：关卡/活动',
  stories: '旧分类：剧情',
  systems: '旧分类：机制/玩法',
  opinions: '旧分类：观点',
  news: '旧分类：资讯',
  general: '旧分类：综合',
  unclassified: '尚未按问题分类',
};

async function health() {
  try {
    const data = await Api.request('/api/health').then((r) => r.json());
    defaultModelPath = data.model_path || '';
    loadModelRegistry(data);
    renderModelOptions();
    els.analyze.disabled = !modelRegistry.length;
    await syncResearchTasks();
  } catch {
    showError('后台服务未连接，请确认服务已启动。');
    els.analyze.disabled = true;
  }
  trackedTasks = trackedTasks.filter((task) => task?.id);
  renderTaskDock();
  startTaskPolling();
}

async function syncResearchTasks() {
  try {
    const [projects, trash] = await Promise.all([
      Api.json('/api/research'),
      Api.json('/api/trash'),
    ]);
    const archived = new Set(trash.map((item) => item.id));
    const archivedResearch = new Set(trash.filter((item) => item.kind === 'research').map((item) => item.id));
    queuePins = queuePins.filter((pin) => !archived.has(pin.id));
    persistQueuePins();
    trackedTasks = trackedTasks.filter((task) => isPinned(task.id) && !archived.has(task.id)
      && !archivedResearch.has(task.project_id));
    projects.forEach((project) => mergeResearchTask(project));
    const missing = queuePins.filter((pin) => !trackedTasks.some((task) => task.id === pin.id));
    for (let start = 0; start < missing.length; start += 8) {
      await Promise.all(missing.slice(start, start + 8).map(async (pin) => {
        try {
          const data = pin.kind === 'research'
            ? await Api.json(`/api/research/${encodeURIComponent(pin.id)}`)
            : pin.kind === 'history'
              ? { id: pin.id, kind: 'history', label: pin.label, status: 'completed' }
              : await Api.json(`/api/jobs/${encodeURIComponent(pin.id)}?view=summary`);
          if (data.project_id && archivedResearch.has(data.project_id)) {
            queuePins = queuePins.filter((item) => item.id !== pin.id);
            return;
          }
          trackedTasks.push({ ...data, kind: pin.kind === 'research' ? 'research' : data.kind || pin.kind,
            label: pin.label || data.goal || data.stage || pin.id });
        } catch { /* Keep the pin so a temporary read error cannot erase it. */ }
      }));
    }
    persistQueuePins();
    persistTasks();
  } catch {}
}
function mergeResearchTask(project) {
  if (!isPinned(project.id)) return;
  const data = { ...project, kind: 'research', label: project.goal };
  const existing = trackedTasks.find((task) => task.kind === 'research' && task.id === project.id);
  if (existing) Object.assign(existing, data);
  else trackedTasks.push(data);
}

function loadModelRegistry(server = {}) {
  modelRegistry = Api.storedArray(localStorage, 'mindmap_model_registry').filter(
    (model) => model && typeof model === 'object',
  );
  if (server.default_model) {
    const adopted = ModelDefaults.applyDefault(
      modelRegistry,
      localStorage.getItem('mindmap_task_model_id'),
      server.default_model,
      localStorage.getItem('mindmap_default_model_revision'),
    );
    modelRegistry = adopted.models;
    localStorage.setItem('mindmap_task_model_id', adopted.selectedId);
    localStorage.setItem('mindmap_default_model_revision', adopted.revision);
    if ($('#taskModelSelect')) $('#taskModelSelect').value = adopted.selectedId;
    saveModelRegistry();
  }
  if (!Array.isArray(modelRegistry) || !modelRegistry.length) {
    modelRegistry = [
      {
        id: crypto.randomUUID(),
        name: `${server.model || '默认模型'}（本地）`,
        provider: 'local',
        path: defaultModelPath,
      },
    ];
    saveModelRegistry();
  }
}
function saveModelRegistry() {
  localStorage.setItem('mindmap_model_registry', JSON.stringify(modelRegistry));
}
function modelRequest(model) {
  return model.provider === 'api'
    ? {
        provider: 'api',
        api_config: {
          base_url: model.base_url,
          model: model.model,
          api_key: sessionStorage.getItem(`mindmap_api_key_${model.id}`) || '',
        },
      }
    : { provider: 'local', local_config: { path: model.path } };
}
function renderModelOptions() {
  const select = $('#taskModelSelect'),
    selected = select.value || localStorage.getItem('mindmap_task_model_id') || modelRegistry[0]?.id;
  select.innerHTML = modelRegistry
    .map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`)
    .join('');
  select.value = modelRegistry.some((item) => item.id === selected) ? selected : modelRegistry[0]?.id || '';
  localStorage.setItem('mindmap_task_model_id', select.value);
  $('#privacyLead').textContent =
    modelRegistry.find((item) => item.id === select.value)?.provider === 'api'
      ? '当前所选模型会将正文发送到配置的 API，请确认接口隐私政策。'
      : '当前所选模型在本机运行；网页抓取与文本整理均由本机服务完成。';
}
$('#taskModelSelect').addEventListener('change', () => {
  localStorage.setItem('mindmap_task_model_id', $('#taskModelSelect').value);
  renderModelOptions();
});
function renderRegistryList() {
  $('#modelRegistryList').innerHTML = modelRegistry
    .map(
      (item) =>
        `<button type="button" class="registry-item ${item.id === editingModelId ? 'active' : ''}" data-model-id="${escapeHtml(item.id)}"><strong>${escapeHtml(item.name)}</strong><small>${item.provider === 'api' ? `API · ${escapeHtml(item.model)}` : `本地 · ${escapeHtml(item.path)}`}</small></button>`,
    )
    .join('');
  $$('.registry-item').forEach((button) =>
    button.addEventListener('click', () => editModel(button.dataset.modelId)),
  );
}
function editModel(id) {
  editingModelId = id;
  const item = modelRegistry.find((model) => model.id === id);
  $('#modelConfigName').value = item?.name || '';
  $('#modelProvider').value = item?.provider || 'local';
  $('#localModelPath').value = item?.path || defaultModelPath;
  $('#apiBaseUrl').value = item?.base_url || '';
  $('#apiModel').value = item?.model || '';
  $('#apiKey').value = item ? sessionStorage.getItem(`mindmap_api_key_${item.id}`) || '' : '';
  $('#deleteModel').disabled = !item;
  toggleModelFields();
  renderRegistryList();
  $('#modelError').classList.add('hidden');
}
function openModelDialog() {
  editModel(modelRegistry[0]?.id || null);
  $('#modelOverlay').classList.remove('hidden');
}
function toggleModelFields() {
  const api = $('#modelProvider').value === 'api';
  $('#localFields').classList.toggle('hidden', api);
  $('#apiFields').classList.toggle('hidden', !api);
}
$('#settingsButton').addEventListener('click', openModelDialog);
$('#modelProvider').addEventListener('change', toggleModelFields);
$('#closeModel').addEventListener('click', () => $('#modelOverlay').classList.add('hidden'));
$('#modelOverlay').addEventListener('click', (e) => {
  if (e.target === $('#modelOverlay')) $('#modelOverlay').classList.add('hidden');
});
$('#newModel').addEventListener('click', () => editModel(null));
$('#deleteModel').addEventListener('click', () => {
  if (!editingModelId) return;
  modelRegistry = modelRegistry.filter((item) => item.id !== editingModelId);
  saveModelRegistry();
  editModel(modelRegistry[0]?.id || null);
  renderModelOptions();
});
$('#saveModel').addEventListener('click', async () => {
  const provider = $('#modelProvider').value;
  const id = editingModelId || crypto.randomUUID(),
    name = $('#modelConfigName').value.trim();
  const item =
    provider === 'local'
      ? { id, name, provider, path: $('#localModelPath').value.trim() }
      : { id, name, provider, base_url: $('#apiBaseUrl').value.trim(), model: $('#apiModel').value.trim() };
  if (!name) {
    $('#modelError').textContent = '请填写便于识别的配置名称。';
    $('#modelError').classList.remove('hidden');
    return;
  }
  if (provider === 'api') sessionStorage.setItem(`mindmap_api_key_${id}`, $('#apiKey').value);
  const selection = modelRequest(item);
  const error = $('#modelError');
  error.classList.add('hidden');
  try {
    const response = await Api.request('/api/models/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(selection),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '模型配置无效');
    const index = modelRegistry.findIndex((model) => model.id === id);
    if (index >= 0) modelRegistry[index] = item;
    else modelRegistry.push(item);
    editingModelId = id;
    saveModelRegistry();
    renderRegistryList();
    renderModelOptions();
    els.analyze.disabled = false;
  } catch (err) {
    error.textContent = err.message;
    error.classList.remove('hidden');
  }
});

$$('.tab').forEach((tab) =>
  tab.addEventListener('click', () => {
    mode = tab.dataset.mode;
    $$('.tab').forEach((item) => item.classList.toggle('active', item === tab));
    els.text.classList.toggle('hidden', mode !== 'text');
    els.url.classList.toggle('hidden', mode !== 'url');
    $('#researchInput').classList.toggle('hidden', mode !== 'research');
    $('#sampleBtn').classList.toggle('hidden', mode !== 'text');
    $('#fileBtn').classList.toggle('hidden', mode !== 'text');
    $('#analysisSettings').classList.toggle('hidden', mode === 'research');
    $('#taskModelWrap').classList.remove('hidden');
    $('#analyzeLabel').textContent = mode === 'research' ? '开始深度研究' : '生成思维导图';
    updateCount();
  }),
);
function urlItems() {
  return els.url.value
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
}
function researchSeeds() {
  return $('#researchSeeds')
    .value.split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
}
function updateCount() {
  els.count.textContent =
    mode === 'text'
      ? pendingFiles.length
        ? `${pendingFiles.length} 个文件 · ${pendingFiles.reduce((n, f) => n + f.content.length, 0).toLocaleString()} 字`
        : `${els.text.value.length.toLocaleString()} 字`
      : mode === 'url'
        ? `${urlItems().length} 个网页`
        : !$('#researchNetwork').checked
          ? `仅用 ${researchFiles.length + Number(!!$('#researchMaterialText').value.trim())} 份附加材料`
          : researchSeeds().length
            ? `${researchSeeds().length} 个研究入口`
            : '自动搜索入口';
}
els.text.addEventListener('input', () => {
  pendingFiles = [];
  updateCount();
});
els.url.addEventListener('input', updateCount);
$('#researchSeeds').addEventListener('input', updateCount);
$('#sampleBtn').addEventListener('click', () => {
  pendingFiles = [];
  els.text.value = sample;
  updateCount();
});
const structureSelect = $('#structureMode');
structureSelect.value = localStorage.getItem('mindmap_structure_mode') || 'auto';
structureSelect.addEventListener('change', () =>
  localStorage.setItem('mindmap_structure_mode', structureSelect.value),
);
$('#fileBtn').addEventListener('click', () => $('#fileInput').click());
$('#fileInput').addEventListener('change', async (e) => {
  const files = [...e.target.files];
  if (!files.length) return;
  if (files.length > 30) {
    e.target.value = '';
    return showError('一次最多导入 30 个文件，未静默忽略多余文件。');
  }
  try {
    const loaded = await Promise.all(files.map(readInputFile));
    pendingFiles = loaded;
    els.text.value = pendingFiles.map((file) => `【${file.name}】\n${file.content}`).join('\n\n');
    updateCount();
    showError('');
  } catch (error) {
    e.target.value = '';
    showError(error.message);
  }
});

$('#analysisForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (mode === 'research') {
    const goal = $('#researchGoal').value.trim(),
      seeds = $('#researchNetwork').checked ? researchSeeds() : [],
      publicTopic = $('#researchPublicTopic').value.trim();
    const network = $('#researchNetwork').checked,
      materialText = $('#researchMaterialText').value;
    const materials = [
      ...(materialText.trim() ? [{ title: '附加文本', content: materialText }] : []),
      ...researchFiles.map((file) => ({
        title: file.name,
        content: file.content,
        imported_file: file.imported_file,
      })),
    ];
    if (!goal) return showError('请说明希望通过自动探索获得什么信息。');
    if (!network && !materials.length) return showError('请添加文字或导入文件，供本地研究读取。');
    if (materials.length > 30) return showError('每项研究最多附加30份材料，请移除多余文件后提交。');
    if (network && (!seeds.length || $('#researchScope').value === 'any_public') && publicTopic.length < 2)
      return showError('请填写允许发送到搜索引擎的公开主题，不会自动发送完整研究目标。');
    showError('');
    els.analyze.disabled = true;
    try {
      const healthResponse = await Api.request('/api/health');
      if (!healthResponse.ok) throw Error('无法确认后台搜索隐私能力，未提交任务。');
      const capabilities = (await healthResponse.json()).capabilities;
      if (network) validateSearchPrivacy(goal, publicTopic, capabilities);
      if (materials.length && capabilities?.research_material_inputs !== 1)
        throw Error('当前后台尚未支持同项研究的附件，请更新后台后提交；材料仍保留在输入区。');
      if (capabilities?.timed_research_report !== 1)
        throw Error('后台尚未支持计时研究与自动报告，请更新后台后提交；没有退回按页数停止的旧流程。');
      const selected = modelRegistry.find((item) => item.id === $('#taskModelSelect').value);
      if (!selected) throw Error('请先选择报告使用的模型。');
      const payload = {
        goal,
        public_search_topic: network ? publicTopic : '',
        seeds,
        materials,
        search_provider: network ? 'hybrid' : 'none',
        mode: 'automatic',
        scope: $('#researchScope').value,
        max_pages: 500,
        max_depth: 20,
        time_limit_minutes: Number($('#researchTimeMinutes').value),
        auto_report: true,
        report_model: modelRequest(selected),
      };
      const response = await Api.request('/api/research', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        }),
        data = await response.json();
      if (!response.ok) throw new Error(data.detail || '无法创建研究项目');
      pinTask({ ...data, kind: 'research', label: goal });
      setTaskDockExpanded(true);
      startTaskPolling();
      $('#submissionNotice').textContent =
        '研究任务已提交，系统将自动搜集并使用所选模型整理报告；可继续提交下一项任务。';
      $('#submissionNotice').classList.remove('hidden');
      $('#researchGoal').value = '';
      $('#researchSeeds').value = '';
      $('#researchPublicTopic').value = '';
      $('#researchMaterialText').value = '';
      researchFiles = [];
      $('#researchFileInput').value = '';
      renderResearchFiles();
      updateCount();
    } catch (error) {
      showError(error.message);
    } finally {
      els.analyze.disabled = false;
    }
    return;
  }
  const sources =
    mode === 'url'
      ? urlItems().map((content) => ({ source_type: 'url', content, label: content }))
      : pendingFiles.length
        ? pendingFiles.map((file) => ({
            source_type: 'text',
            content: file.content,
            imported_file: file.imported_file,
            label: file.name,
          }))
        : [{ source_type: 'text', content: els.text.value, label: '粘贴文本' }];
  if (!sources.length || !sources[0].content.trim())
    return showError(mode === 'text' ? '请先粘贴文字或导入文本文件。' : '请输入至少一个完整网页地址。');
  if (sources.length > 30) return showError('一次最多提交 30 个任务。');
  const selected = modelRegistry.find((item) => item.id === $('#taskModelSelect').value);
  if (!selected) return showError('请先在设置中添加一个可用模型。');
  const config = modelRequest(selected),
    items = sources.map((source) => ({
      ...source,
      label: undefined,
      ...config,
      structure_mode: structureSelect.value,
    }));
  showError('');
  els.analyze.disabled = true;
  try {
    if (sources.some((source) => source.imported_file)) {
      const health = await Api.request('/api/health');
      if (!health.ok || (await health.json()).capabilities?.input_material_contract !== 1)
        throw Error('后台尚未支持完整文件保存，请更新后台后重试；本次未提交，文件仍保留在输入区。');
    }
    const batch = sources.length > 1,
      response = await Api.request(batch ? '/api/jobs/batch' : '/api/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(batch ? { items } : items[0]),
      });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '无法创建分析任务');
    const jobs = batch ? data.jobs : [data];
    jobs.forEach((job, index) =>
      pinTask({
        id: job.id,
        label: sources[index].label,
        modelName: selected.name,
        status: job.status,
        stage: job.stage,
        created_at: job.created_at,
      }),
    );
    setTaskDockExpanded(true);
    startTaskPolling();
    $('#submissionNotice').textContent =
      `已提交 ${jobs.length} 个任务，可继续输入下一批；进度与结果入口在右侧任务队列。`;
    $('#submissionNotice').classList.remove('hidden');
    if (mode === 'url') els.url.value = '';
    else {
      els.text.value = '';
      pendingFiles = [];
      $('#fileInput').value = '';
    }
    updateCount();
  } catch (error) {
    showError(error.message);
  } finally {
    els.analyze.disabled = false;
  }
});

function persistTasks() {
  // Storage quota/privacy mode must not stop live monitoring. Server records are authoritative.
  try {
    sessionStorage.setItem(
      'mindmap_tracked_tasks',
      JSON.stringify(trackedTasks.map(({ result, ...task }) => task)),
    );
  } catch {
    /* The current page still keeps its in-memory queue. */
  }
}
function renderResearchFiles() {
  const list = $('#researchFileList');
  list.replaceChildren();
  researchFiles.forEach((file, index) => {
    const row = document.createElement('div'),
      name = document.createElement('span'),
      remove = document.createElement('button');
    row.className = 'research-file';
    name.textContent = `${file.name} · ${file.content.length.toLocaleString()} 字`;
    remove.type = 'button';
    remove.className = 'text-button';
    remove.textContent = '移除';
    remove.setAttribute('aria-label', `移除 ${file.name}`);
    remove.addEventListener('click', () => {
      researchFiles.splice(index, 1);
      renderResearchFiles();
    });
    row.append(name, remove);
    list.append(row);
  });
  updateCount();
}
$('#researchMaterialText').addEventListener('input', updateCount);
$('#researchFileButton').addEventListener('click', () => $('#researchFileInput').click());
$('#researchFileInput').addEventListener('change', async (event) => {
  try {
    const selected = [...event.target.files];
    if (researchFiles.length + selected.length > 30)
      throw Error('每项研究最多导入30个文件，未忽略多余文件。');
    const loaded = await Promise.all(selected.map(readInputFile));
    researchFiles.push(...loaded);
    renderResearchFiles();
    showError('');
  } catch (error) {
    showError(error.message);
  } finally {
    event.target.value = '';
  }
});
$('#researchNetwork').addEventListener('change', () => {
  const enabled = $('#researchNetwork').checked;
  ['#researchPublicTopic', '#researchSeeds', '#researchScope', '#researchTimeMinutes'].forEach(
    (selector) => ($(selector).disabled = !enabled),
  );
  $$('[data-network-field]').forEach((element) => element.classList.toggle('hidden', !enabled));
  $('#researchLocalNote').classList.toggle('hidden', enabled);
  updateCount();
});
function elapsedLabel(seconds) {
  const value = Math.max(0, Math.floor(seconds || 0));
  return value < 60 ? `${value} 秒` : `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
}
function validateSearchPrivacy(goal, topic, capabilities) {
  if (!capabilities?.separate_public_search_topic && topic !== goal)
    throw Error('当前后台尚未支持独立公开搜索主题。为避免发送完整研究目标，已阻止提交；请待后台更新后重试。');
}
function taskElapsedSeconds(task) {
  if (typeof task.elapsed_seconds === 'number' && Number.isFinite(task.elapsed_seconds))
    return Math.max(0, task.elapsed_seconds);
  const start = task.started_at || task.model_started_at;
  if (!start) return 0;
  const end = task.finished_at || (task.status === 'paused' ? task.pause_started_at : 0) || Date.now() / 1000;
  return Math.max(0, end - start - (Number(task.paused_seconds) || 0));
}
function generationLabel(task) {
  const g = task.model_generation;
  if (!g || !['running', 'paused'].includes(task.status)) return '';
  const prefix = task.status === 'paused' ? '暂停前观测：' : '最近观测：';
  if (g.phase === 'prefill')
    return `${prefix}正在处理 ${Number(g.input_tokens) || 0} 个输入 token，尚未生成首个 token`;
  if (g.phase === 'generating' || g.phase === 'completed')
    return `${prefix}本次调用已生成 ${Number(g.generated_tokens) || 0} 个 token${g.first_token_seconds != null ? `，首个 token 等待 ${elapsedLabel(g.first_token_seconds)}（含暂停）` : ''}${g.phase === 'completed' ? '；本次调用完成，不代表整个任务完成' : ''}`;
  return '';
}
function taskFinished(task) {
  if (task.kind === 'research' && typeof task.workflow?.runtime?.finished === 'boolean')
    return task.workflow.runtime.finished;
  if (task.kind === 'research' && task.workflow)
    return ['report_ready', 'report_failed', 'collection_ready', 'cancelled'].includes(task.workflow.phase);
  return task.kind === 'research'
    ? ['completed', 'completed_with_errors', 'failed', 'cancelled'].includes(task.status)
    : ['completed', 'failed', 'cancelled'].includes(task.status);
}
function taskPolling(task) {
  if (task.kind === 'research' && typeof task.workflow?.runtime?.polling === 'boolean')
    return task.workflow.runtime.polling;
  if (
    task.kind === 'research' &&
    task.workflow &&
    ['collecting', 'dispatching', 'reporting', 'report_paused'].includes(task.workflow.phase) &&
    !['paused', 'cancelled'].includes(task.status)
  )
    return true;
  return task.kind === 'research'
    ? ['queued', 'crawling'].includes(task.status) || ['queued', 'running'].includes(task.search_status)
    : !['completed', 'failed', 'cancelled'].includes(task.status);
}
function taskClearable(task) {
  return taskFinished(task) && (task.kind !== 'research' || !taskPolling(task));
}
function taskClearButton(task) {
  return `<button type="button" class="task-clear-button" data-clear-task="${escapeHtml(task.id)}" title="只从队列移除；任务继续运行，可从历史记录重新加入">清除</button>`;
}
function clearTaskRecords() {
  const finished = new Set(trackedTasks.filter(taskClearable).map((task) => task.id));
  queuePins = queuePins.filter((pin) => !finished.has(pin.id));
  trackedTasks = trackedTasks.filter((task) => !finished.has(task.id));
  persistQueuePins();
  $('#clearConfirm').classList.add('hidden');
  persistTasks();
  renderTaskDock();
}
function renderTaskDock() {
  const active = trackedTasks.filter(taskPolling).length;
  $('#taskCount').textContent = String(active || trackedTasks.length);
  const hasFinished = trackedTasks.some(taskClearable);
  $('#clearFinished').classList.toggle('hidden', !hasFinished);
  if (!hasFinished) $('#clearConfirm').classList.add('hidden');
  $('#taskList').innerHTML = trackedTasks.length
    ? trackedTasks
        .map((task) => {
          if (task.kind === 'research' && task.workflow) return renderWorkflowTask(task);
          if (task.kind === 'research') {
            const names = {
              queued: '等待抓取',
              crawling: '探索中',
              awaiting_review: '旧任务待继续',
              paused: '已暂停',
              completed: '已完成',
              completed_with_errors: '完成但有失败',
              cancelled: '已取消',
            };
            const discovering = ['queued', 'running'].includes(task.search_status),
              budgetEnded = task.stop_reason === 'page_budget_reached',
              statusName = discovering
                ? '搜索入口'
                : budgetEnded
                  ? '页面额度已用完'
                  : names[task.status] || task.status,
              visual = ['completed', 'completed_with_errors'].includes(task.status)
                ? 'completed'
                : task.status === 'cancelled'
                  ? 'failed'
                  : task.status === 'crawling' || discovering
                    ? 'running'
                    : 'queued';
            const message = discovering
              ? '正在后台搜索并筛选跨来源入口；你可以继续提交其他任务。'
              : task.search_status === 'failed' && task.search_error
                ? `跨来源搜索失败，继续处理已有入口：${escapeHtml(task.search_error)}`
                : task.status === 'awaiting_review'
                  ? '旧流程停在候选网页处，可在详情中继续自动研究。'
                  : task.status === 'paused'
                    ? '项目已持久化，可以随时继续。'
                    : budgetEnded
                      ? `本轮额度已处理完，${task.pages_waiting_budget || 0} 个候选已保留，可在探索工作区增加预算后继续。`
                      : '当前可用页面已处理完，可查看探索路径或生成报告。';
            return `<article class="task-card research ${visual}"><div class="task-card-head"><strong title="${escapeHtml(task.label || task.goal)}">${escapeHtml(task.label || task.goal)}</strong><span>${escapeHtml(statusName)}</span></div><small>深度研究 · 已获取 ${task.pages_fetched || 0} · 失败 ${task.pages_failed || 0} · 链接 ${task.links_found || 0}</small><p>${message}</p><div class="task-actions"><button type="button" data-open-research="${escapeHtml(task.id)}">查看详情</button>${taskClearButton(task)}</div>${taskMonitorNotice(task)}</article>`;
          }
          const waiting = task.status === 'queued' || String(task.stage || '').startsWith('等待本地模型');
          const actual = taskElapsedSeconds(task);
          const quality = task.completion_kind || task.result?.quality_validation?.status,
            failure = task.error_info || {};
          const visualStatus = waiting ? 'queued' : task.status || 'queued';
          const statusText =
            task.status === 'completed'
              ? quality && quality !== 'normal'
                ? '降级完成'
                : '已完成'
              : task.status === 'failed'
                ? task.superseded_by
                  ? '已重试'
                  : '失败'
                : task.status === 'cancelled'
                  ? '已取消'
                  : task.status === 'cancel_requested'
                    ? '正在取消'
                    : task.status === 'paused'
                      ? '已暂停'
                      : waiting
                        ? '排队中'
                        : '运行中';
          const runtime =
            task.status === 'paused'
              ? `已运行 ${elapsedLabel(actual)}（暂停中）`
              : waiting
                ? '尚未开始'
                : task.model_started_at || actual > 0
                  ? `已运行 ${elapsedLabel(actual)}`
                  : '模型未启动';
          const device = task.inference_device_name
            ? ` · ${task.inference_device_name}${task.inference_dtype ? ` / ${task.inference_dtype}` : ''}${task.model_reused ? '（复用驻留模型）' : '（首次加载）'}`
            : '';
          const failureBody =
            task.status === 'failed'
              ? `<p class="task-error">${escapeHtml(failure.message || task.error || '分析失败')}</p>${failure.suggestion ? `<p class="task-suggestion">${escapeHtml(failure.suggestion)}</p>` : ''}${failure.technical && failure.technical !== failure.message ? `<details><summary>技术详情</summary><code>${escapeHtml(failure.technical)}</code></details>` : ''}`
              : `<p>${escapeHtml(task.stage || '等待执行')}</p>`;
          const canPause =
            ['analysis', 'report_question', 'coverage_review', 'research_synthesis'].includes(
              task.kind || 'analysis',
            ) &&
            task.provider === 'local' &&
            ['queued', 'running'].includes(task.status);
          const actions =
            task.status === 'completed' && (task.result == null || task.result?.tree || task.kind === 'report_question')
              ? `<div class="task-actions"><button type="button" data-open-job="${task.id}">查看结果</button></div>`
              : task.status === 'failed' && !task.superseded_by && failure.retryable
                ? `<div class="task-actions"><button type="button" data-retry-job="${task.id}">重试原任务</button></div>`
                : task.status === 'paused'
                  ? `<div class="task-actions"><button type="button" data-resume-job="${task.id}">继续</button><button class="danger" type="button" data-cancel-job="${task.id}">取消任务</button></div>`
                  : ['queued', 'running'].includes(task.status)
                    ? `<div class="task-actions">${canPause ? `<button type="button" data-pause-job="${task.id}">暂停</button>` : ''}<button class="danger" type="button" data-cancel-job="${task.id}">取消任务</button></div>`
                    : '';
          if (task.kind === 'history')
            return `<article class="task-card completed"><div class="task-card-head"><strong>${escapeHtml(task.label || task.id)}</strong><span>已保存</span></div><div class="task-actions"><button type="button" data-open-job="${escapeHtml(task.id)}">查看结果</button>${taskClearButton(task)}</div></article>`;
          return `<article class="task-card ${escapeHtml(visualStatus)}"><div class="task-card-head"><strong title="${escapeHtml(task.label)}">${escapeHtml(task.label)}</strong><span>${statusText}</span></div><small>${escapeHtml(task.modelName || task.model_name || '模型')}${escapeHtml(device)} · ${runtime}${task.attempt > 1 ? ` · 第 ${task.attempt} 次` : ''}</small>${failureBody}${generationLabel(task) ? `<small class="task-generation">${escapeHtml(generationLabel(task))}</small>` : ''}${actions}<div class="task-actions"><button type="button" data-open-job-detail="${escapeHtml(task.id)}">查看详情</button>${taskClearButton(task)}</div>${taskMonitorNotice(task)}</article>`;
        })
        .join('')
    : '<p class="task-empty">还没有本次会话的任务</p>';
  $$('[data-open-job]').forEach((button) =>
    button.addEventListener('click', () => openTaskResult(button.dataset.openJob)),
  );
  $$('[data-open-job-detail]').forEach((button) =>
    button.addEventListener('click', () => openJobDetail(button.dataset.openJobDetail)),
  );
  $$('[data-retry-job]').forEach((button) =>
    button.addEventListener('click', () => retryTask(button.dataset.retryJob, button)),
  );
  $$('[data-cancel-job]').forEach((button) =>
    button.addEventListener('click', () => cancelTask(button.dataset.cancelJob, button)),
  );
  $$('[data-pause-job]').forEach((button) =>
    button.addEventListener('click', () => changeTaskRunState(button.dataset.pauseJob, 'pause', button)),
  );
  $$('[data-resume-job]').forEach((button) =>
    button.addEventListener('click', () => changeTaskRunState(button.dataset.resumeJob, 'resume', button)),
  );
  $$('[data-open-research]').forEach((button) =>
    button.addEventListener('click', () => openResearch(button.dataset.openResearch)),
  );
  $$('[data-clear-task]').forEach((button) =>
    button.addEventListener('click', () => unpinTask(button.dataset.clearTask)),
  );
  $$('[data-workflow-action]').forEach((button) =>
    button.addEventListener('click', () =>
      workflowAction(button.dataset.project, button.dataset.workflowAction, button),
    ),
  );
}

function taskMonitorNotice(task) {
  return task.monitor_error
    ? `<small class="task-suggestion" role="status">${escapeHtml(task.monitor_error)}</small>`
    : '';
}
const workflowLabels = {
  collecting: '搜集资料',
  paused: '已暂停',
  dispatching: '准备整理报告',
  reporting: '整理报告',
  report_paused: '报告已暂停',
  feedback_revision: '修订报告',
  report_ready: '报告已保存',
  collection_ready: '材料收集完成',
  report_failed: '报告未生成',
  cancelled: '已取消',
};
function workflowLabel(workflow) {
  const runtime = workflow.runtime;
  if (runtime?.status === 'unavailable') return '运行记录不完整';
  if (runtime?.status === 'no_evidence') return '未取得有效材料';
  if (runtime?.status === 'queued') return '排队中';
  if (runtime?.status === 'cancel_requested') return '正在取消';
  return workflowLabels[runtime?.phase || workflow.phase] || runtime?.phase || workflow.phase;
}
function researchActions(project, reports = []) {
  if (Array.isArray(project.workflow?.runtime?.actions)) {
    const actions = [...project.workflow.runtime.actions];
    if ((reports.length || project.workflow.runtime.publication?.latest_report_id || project.latest_report_version)
      && !actions.includes('open-report')) actions.unshift('open-report');
    return actions;
  }
  // Read compatibility for old server/session records, never a write authority.
  const w = project.workflow,
    actions = [],
    phase = w?.phase;
  if (reports.length || w?.runtime?.publication?.latest_report_id || phase === 'report_ready')
    actions.push('open-report');
  if (w?.runtime?.status === 'unavailable') return actions;
  if (phase === 'collecting' || (!w && ['queued', 'crawling'].includes(project.status)))
    actions.push('pause');
  if (['collecting', 'paused'].includes(phase)) actions.push('extend-time');
  if (
    ['paused', 'report_ready', 'report_failed', 'collection_ready'].includes(phase) ||
    (!w && ['paused', 'awaiting_review', 'completed', 'completed_with_errors'].includes(project.status))
  )
    actions.push('continue-automatic', 'continue-with-model');
  if (phase === 'report_failed') actions.push('retry-report');
  if (w?.report_provider === 'local' && ['reporting', 'report_paused'].includes(phase))
    actions.push(phase === 'report_paused' ? 'resume-job' : 'pause-job');
  if (!['completed', 'completed_with_errors', 'cancelled'].includes(project.status) && phase !== 'cancelled')
    actions.push('cancel');
  if (
    !['collecting', 'dispatching', 'reporting', 'report_paused', 'feedback_revision', 'cancelled'].includes(
      phase,
    )
  ) {
    if (project.pages_fetched > 0) actions.push('synthesize');
    if (actions.includes('open-report')) actions.push('audit');
  }
  if (!w && project.search_status === 'failed') actions.push('retry-search');
  return actions;
}
function renderWorkflowTask(task) {
  const w = task.workflow;
  const status = workflowLabel(w),
    done = taskFinished(task);
  const completedResearch =
    done && ['report_ready', 'collection_ready'].includes(w.runtime?.phase || w.phase);
  const message =
    w.runtime?.status === 'unavailable'
      ? '暂不能确认执行状态，原材料与已有报告保留。请查看过程记录；不会按已完成清理。'
      : w.runtime?.status === 'no_evidence'
        ? '本轮未取得回答问题所需的材料，已保存阻碍说明；不代表已有研究结论。'
        : w.phase === 'report_ready'
          ? '可阅读报告、核对来源或继续追问。'
          : w.phase === 'report_failed'
            ? w.error || '报告整理失败，已保留原始材料，可重试。'
            : w.phase === 'paused'
              ? task.error ||
                (w.elapsed_seconds >= w.limit_seconds
                  ? '已暂停。搜集时限已到，点击继续后将整理已有材料，不再自动追加搜集时间。'
                  : '保留现场与剩余搜集时间，继续时无需逐页批准。')
              : w.phase === 'collecting'
                ? w.elapsed_seconds >= w.limit_seconds
                  ? '搜集时限已到，正在结束当前读取并准备报告。'
                  : task.intent?.stage || '自动发现和读取公开资料，停止搜集后会整理报告。'
                : w.stage || '正在把已获取材料整理为报告，搜集停止不代表问题已解决。';
  const action = (name, label) =>
    `<button type="button" data-workflow-action="${name}" data-project="${escapeHtml(task.id)}">${label}</button>`;
  const available = researchActions(task);
  const labels = {
    'open-report': w.phase === 'report_ready' ? '阅读报告' : '查看已有报告',
    'retry-report': '重试整理报告',
    'continue-automatic': completedResearch ? null : '继续研究',
    'continue-with-model': completedResearch ? null : '更换模型后继续',
    pause: task.stop_reason === 'search_recovery_wait' ? '停止自动重试' : '暂停',
    'extend-time': '延长时间',
  };
  const actions = available
    .filter((name) => labels[name])
    .map((name) => action(name, labels[name]))
    .join('');
  const jobActions = available.includes('resume-job')
    ? `<button data-resume-job="${escapeHtml(w.report_job_id)}">继续报告</button>`
    : available.includes('pause-job')
      ? `<button data-pause-job="${escapeHtml(w.report_job_id)}">暂停报告</button>`
      : '';
  return `<article class="task-card research ${done ? 'completed' : 'queued'}"><div class="task-card-head"><strong>${escapeHtml(task.label || task.goal)}</strong><span>${escapeHtml(status)}</span></div>${w.model_summary ? `<small>模型：${escapeHtml(w.model_summary.name)} · ${w.model_summary.provider === 'api' ? 'API' : '本地'}</small>` : ''}<small>搜集用时 ${elapsedLabel(w.elapsed_seconds)} / ${elapsedLabel(w.limit_seconds)} · ${task.pages_fetched || 0} 个页面</small><p>${escapeHtml(message)}</p><div class="task-actions">${actions}${jobActions}<button data-open-research="${escapeHtml(task.id)}">查看详情</button>${taskClearButton(task)}</div>${taskMonitorNotice(task)}</article>`;
}

function confirmRecordAction(title, message, permanent = false) {
  return new Promise((resolve) => {
    const dialog = document.createElement('dialog');
    dialog.className = 'record-confirm-dialog';
    dialog.innerHTML = `<form><h2>${escapeHtml(title)}</h2><p>${escapeHtml(message)}</p>${permanent ? '<label>输入“永久删除”以确认<input type="text" autocomplete="off" aria-label="输入永久删除以确认"></label>' : ''}<div><button type="button">取消</button><button type="submit" class="danger-confirm" ${permanent ? 'disabled' : ''}>${permanent ? '永久删除' : '确认移入回收站'}</button></div></form>`;
    document.body.append(dialog);
    let confirmed = false;
    const submit = dialog.querySelector('button[type="submit"]');
    if (permanent) dialog.querySelector('input').addEventListener('input', (event) => {
      submit.disabled = event.target.value !== '永久删除';
    });
    dialog.querySelector('form').addEventListener('submit', (event) => {
      event.preventDefault();
      if (submit.disabled) return;
      confirmed = true;
      dialog.close();
    });
    dialog.querySelector('button[type="button"]').addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => { dialog.remove(); resolve(confirmed); }, { once: true });
    dialog.showModal();
  });
}

async function archiveRecord(kind, id, label, button) {
  const scope = kind === 'research' ? `整个研究项目“${label}”及其报告` : `“${label}”`;
  const message = `${scope}将从任务队列和历史列表移入回收站。正在执行的步骤会停止；已保存的材料和报告可恢复。`;
  if (!await confirmRecordAction('移入回收站？', message)) return false;
  if (button) button.disabled = true;
  try {
    const response = await Api.request(`/api/trash/${kind}/${id}`, { method: 'POST' });
    const data = await readApiJson(response);
    if (!response.ok) throw Error(data.detail || '移入回收站失败');
    unpinTask(id);
    if (data.kind === 'research') {
      const linked = new Set(trackedTasks.filter((task) => task.project_id === data.id).map((task) => task.id));
      if (linked.size) {
        queuePins = queuePins.filter((pin) => !linked.has(pin.id));
        trackedTasks = trackedTasks.filter((task) => !linked.has(task.id));
        persistQueuePins();
        persistTasks();
        renderTaskDock();
      }
    }
    return true;
  } catch (error) {
    if (button?.closest('#historyOverlay')) {
      const target = $('#historyTrashPanel').classList.contains('hidden') ? $('#historyNote') : $('#historyTrashError');
      target.textContent = error.message;
      target.classList.remove('hidden');
    } else showError(error.message);
    return false;
  } finally {
    if (button) button.disabled = false;
  }
}

function chooseWorkflowModel(original, action) {
  if (!modelRegistry.length) throw Error('请先在设置中添加模型，原任务尚未改变。');
  return new Promise((resolve) => {
    const dialog = document.createElement('dialog');
    dialog.className = 'workflow-model-dialog';
    dialog.setAttribute('aria-labelledby', 'workflowModelTitle');
    dialog.innerHTML = `<form><h2 id="workflowModelTitle">${action === 'retry-report' ? '重试报告使用的模型' : '继续研究使用的模型'}</h2><p>${original ? `原任务：${escapeHtml(original.name)}（${original.provider === 'api' ? 'API' : '本地'}）` : '请明确选择本次使用的模型。'}</p><label>模型<select aria-label="本次操作使用的模型">${modelRegistry.map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)} · ${m.provider === 'api' ? 'API' : '本地'}</option>`).join('')}</select></label><p class="workflow-model-privacy"></p><div class="workflow-model-actions"><button type="button">取消</button><button type="submit" class="dialog-primary">确认并继续</button></div></form>`;
    document.body.append(dialog);
    const select = dialog.querySelector('select'),
      privacy = dialog.querySelector('.workflow-model-privacy');
    const matching = modelRegistry.find(
      (m) =>
        m.provider === original?.provider &&
        (m.provider === 'api'
          ? m.model
          : String(m.path || '')
              .replaceAll('\\', '/')
              .split('/')
              .pop()) === original?.name,
    );
    select.value = matching?.id || $('#taskModelSelect').value || modelRegistry[0].id;
    const update = () => {
      const m = modelRegistry.find((m) => m.id === select.value);
      privacy.textContent =
        m?.provider === 'api'
          ? `研究目标及用于分析的材料将发送到 ${m.base_url}。请确认设置中已填写所需凭据；不会改用其他服务。`
          : '使用所选本地模型，原始材料和已完成记录继续保留。';
    };
    update();
    select.addEventListener('change', update);
    let chosen = null;
    dialog.querySelector('form').addEventListener('submit', (event) => {
      event.preventDefault();
      chosen = modelRegistry.find((m) => m.id === select.value) || null;
      if (chosen) dialog.close();
    });
    dialog.querySelector('button[type="button"]').addEventListener('click', () => dialog.close());
    dialog.addEventListener(
      'close',
      () => {
        dialog.remove();
        resolve(chosen);
      },
      { once: true },
    );
    dialog.showModal();
  });
}
function chooseContinuationMinutes(project, extendOnly = false) {
  return new Promise((resolve) => {
    const dialog = document.createElement('dialog');
    dialog.className = 'workflow-model-dialog';
    dialog.setAttribute('aria-labelledby', 'continuationTitle');
    const existing = !!project.workflow,
      previous = Math.round((project.workflow?.limit_seconds || 1800) / 60);
    dialog.innerHTML = `<form><h2 id="continuationTitle">${existing ? '追加搜集时间' : '设置搜集时间'}</h2><p>${existing ? `原累计时限为 ${previous} 分钟，本次填写的是额外增加的时间，不会重新开始已有研究。` : '选择继续研究的搜集时间。'}</p><label>分钟<input type="number" name="minutes" min="1" max="1440" step="1" value="30" required></label><p class="workflow-model-privacy">到时停止搜集并整理已有材料；报告整理时间另计。取消不会修改任务。</p><div class="workflow-model-actions"><button type="button">取消</button><button type="submit" class="dialog-primary">确认并继续</button></div></form>`;
    document.body.append(dialog);
    let chosen = null;
    if (extendOnly) {
      dialog.querySelector('h2').textContent = '延长搜集时间';
      dialog.querySelector('p').textContent =
        `当前累计时限 ${previous} 分钟。只增加时限，不重置已用时间；暂停的任务不会自动继续。`;
      dialog.querySelector('button[type="submit"]').textContent = '确认延长';
    }
    dialog.querySelector('form').addEventListener('submit', (event) => {
      event.preventDefault();
      const input = dialog.querySelector('input');
      if (!input.reportValidity()) return;
      chosen = Number(input.value);
      dialog.close();
    });
    dialog.querySelector('button[type="button"]').addEventListener('click', () => dialog.close());
    dialog.addEventListener(
      'close',
      () => {
        dialog.remove();
        resolve(chosen);
      },
      { once: true },
    );
    dialog.showModal();
    dialog.querySelector('input').select();
  });
}
async function workflowPayload(id, action) {
  if (!['continue-automatic', 'continue-with-model', 'retry-report', 'extend-time'].includes(action))
    return {};
  const response = await Api.request(`/api/research/${id}`),
    project = await response.json();
  if (!response.ok) throw Error(project.detail || '无法读取原任务配置，未继续。');
  if (action === 'extend-time') {
    if (!researchActions(project).includes('extend-time'))
      throw Error('本轮采集已结束，不能直接延长；请在报告结束后继续搜集。');
    const additional_minutes = await chooseContinuationMinutes(project, true);
    return additional_minutes === null ? null : { additional_minutes };
  }
  const original = project.workflow?.model_summary;
  let time_limit_minutes;
  if (
    action !== 'retry-report' &&
    (!project.workflow || ['completed', 'completed_with_errors'].includes(project.status))
  ) {
    time_limit_minutes = await chooseContinuationMinutes(project);
    if (time_limit_minutes === null) return null;
  }
  if (action === 'continue-automatic' && project.workflow && !original?.requires_reauthorization) {
    return time_limit_minutes === undefined ? {} : { time_limit_minutes }; // Resume preserves remaining time and original model.
  }
  const selected = await chooseWorkflowModel(original, action);
  if (!selected) return null;
  return action === 'retry-report'
    ? modelRequest(selected)
    : {
        report_model: modelRequest(selected),
        ...(time_limit_minutes === undefined ? {} : { time_limit_minutes }),
      };
}
async function workflowAction(id, action, button) {
  if (button) button.disabled = true;
  try {
    if (action === 'open-report') {
      const response = await Api.request(`/api/research/${id}/reports/latest`),
        data = await response.json();
      if (!response.ok) throw Error(data.detail || '报告不可用');
      result = data;
      currentHistoryId = data.history_id;
      closeResearchWorkspace();
      showResult();
      return;
    }
    const payload = await workflowPayload(id, action);
    if (payload === null) return;
    const endpoint = action === 'continue-with-model' ? 'continue-automatic' : action;
    const response = await Api.request(`/api/research/${id}/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }),
      data = await response.json();
    if (!response.ok) throw Error(data.detail?.message || data.detail || '任务操作失败');
    mergeResearchTask(data);
    persistTasks();
    renderTaskDock();
    startTaskPolling();
    if (currentResearchId === id && !$('#researchOverlay').classList.contains('hidden'))
      await loadResearchDetail();
  } catch (error) {
    showError(error.message);
  } finally {
    if (button) button.disabled = false;
  }
}
async function changeTaskRunState(id, action, button) {
  button.disabled = true;
  try {
    const response = await Api.request(`/api/jobs/${id}/${action}`, { method: 'POST' }),
      data = await response.json();
    if (!response.ok) throw new Error(data.detail || '无法改变任务状态');
    const task = trackedTasks.find((item) => item.id === id);
    if (task) Object.assign(task, data);
    persistTasks();
    renderTaskDock();
    startTaskPolling();
    if (data.project_id === currentResearchId && !$('#researchOverlay').classList.contains('hidden'))
      await loadResearchDetail();
  } catch (error) {
    showError(error.message);
    button.disabled = false;
  }
}
async function cancelTask(id, button) {
  if (!confirm('取消后不会保存本次模型输出，确定取消？')) return;
  button.disabled = true;
  try {
    const response = await Api.request(`/api/jobs/${id}/cancel`, { method: 'POST' }),
      data = await response.json();
    if (!response.ok) throw new Error(data.detail || '无法取消任务');
    const task = trackedTasks.find((item) => item.id === id);
    if (task) Object.assign(task, data);
    persistTasks();
    renderTaskDock();
    startTaskPolling();
  } catch (error) {
    showError(error.message);
    button.disabled = false;
  }
}
async function readApiJson(response) {
  return Api.readJSON(response);
}
let taskPollInFlight = false;
async function pollTasks() {
  if (taskPollInFlight) return;
  const active = trackedTasks.filter(taskPolling);
  if (!active.length) {
    renderTaskDock();
    return;
  }
  taskPollInFlight = true;
  try {
    await Promise.all(
      active.map(async (task) => {
        try {
          const endpoint = task.kind === 'research' ? `/api/research/${task.id}` : `/api/jobs/${task.id}?view=summary`;
          const data = await Api.json(endpoint);
          Object.assign(task, data);
          delete task.monitor_error;
          if (data.intent?.stage) task.stage = data.intent.stage;
        } catch (error) {
          task.monitor_error = error.message + ' 将继续刷新；显示的是最近记录。';
        }
      }),
    );
    persistTasks();
    renderTaskDock();
  } finally {
    taskPollInFlight = false;
  }
}
function startTaskPolling() {
  if (taskTimer) clearInterval(taskTimer);
  pollTasks();
  taskTimer = setInterval(pollTasks, 1200);
}
async function retryTask(id, button) {
  const previous = trackedTasks.find((task) => task.id === id);
  if (!previous) return;
  button.disabled = true;
  button.textContent = '正在重建任务…';
  try {
    const response = await Api.request(`/api/jobs/${id}/retry`, { method: 'POST' }),
      data = await response.json();
    if (!response.ok) throw new Error(data.detail || '无法重试任务');
    previous.superseded_by = data.id;
    pinTask({
      ...data,
      label: previous.label,
      modelName: previous.modelName || previous.model_name,
    });
    startTaskPolling();
  } catch (error) {
    previous.error_info = {
      ...(previous.error_info || {}),
      message: error.message,
      retryable: false,
      suggestion: '原任务可能已失效，请返回输入页重新提交。',
    };
    persistTasks();
    renderTaskDock();
  }
}
async function openTaskResult(id) {
  let task = trackedTasks.find((item) => item.id === id);
  if (task?.kind === 'history') return loadHistory(id);
  if (!task?.result) {
    const response = await Api.request(`/api/jobs/${id}?view=reader`),
      data = await response.json();
    if (!response.ok) return showError(data.detail || '无法读取结果');
    Object.assign(task, data);
  }
  if (task.status !== 'completed') return;
  if (task.kind === 'report_question') {
    await openQuestionResult(task);
    return;
  }
  if (!task.result?.tree) return showError('该任务没有可阅读的分析结果；请查看任务详情。');
  if (!task.result.research?.project_id || !task.result.report_version)
    return loadHistory(task.result.history_id || task.id);
  // Read the exact saved version, including its edit baseline. A review job's
  // ID is not the history ID of the report it reviewed.
  try {
    const response = await Api.request(
      `/api/research/${encodeURIComponent(task.result.research.project_id)}/reports/${task.result.report_version}?view=reader`,
    );
    result = await response.json();
    showResult();
  } catch (error) {
    showError(error.message);
  }
}
async function openJobDetail(id) {
  try {
    const job = await Api.json(`/api/jobs/${encodeURIComponent(id)}?view=summary`);
    const title = trackedTasks.find((task) => task.id === id)?.label || job.stage || `任务 ${id.slice(0, 8)}`;
    $('#jobDetailDialog').dataset.jobId = id;
    $('#jobDetailDialog').dataset.jobTitle = title;
    $('#jobDetailDialog').dataset.projectId = job.project_id || '';
    $('#jobDetailTitle').textContent = title;
    $('#jobDetailStatus').textContent = `状态：${job.status || '未知'} · ${job.model_name || '模型未启动'} · ID ${id.slice(0, 8)}`;
    $('#jobDetailStage').textContent = job.stage || '尚无运行记录';
    $('#jobDetailError').textContent = job.error_info?.message || job.error || '';
    $('#jobDetailError').classList.toggle('hidden', !$('#jobDetailError').textContent);
    $('#jobDetailResearch').classList.toggle('hidden', !job.project_id);
    $('#jobDetailTrash').classList.toggle('hidden', !!job.project_id);
    $('#jobDetailDialog').showModal();
  } catch (error) { showError(error.message); }
}
$('#jobDetailClose').addEventListener('click', () => $('#jobDetailDialog').close());
$('#jobDetailResearch').addEventListener('click', () => {
  const id = $('#jobDetailDialog').dataset.projectId;
  $('#jobDetailDialog').close();
  if (id) openResearch(id);
});
$('#jobDetailTrash').addEventListener('click', async (event) => {
  const dialog = $('#jobDetailDialog'), id = dialog.dataset.jobId;
  if (id && await archiveRecord('job', id, `${dialog.dataset.jobTitle}（ID ${id.slice(0, 8)}）`, event.currentTarget)) {
    dialog.close();
    if (!$('#historyOverlay').classList.contains('hidden')) await selectHistoryKind(historyKind);
  }
});
function setTaskDockExpanded(expanded) {
  $('#taskDock').classList.toggle('collapsed', !expanded);
  document.body.classList.toggle('task-dock-open', expanded);
  $('#taskDockToggle').setAttribute('aria-expanded', String(expanded));
  $('#taskDockToggle').setAttribute('aria-label', expanded ? '收起任务队列' : '展开任务队列');
}
$('#taskDockToggle').addEventListener('click', () =>
  setTaskDockExpanded($('#taskDock').classList.contains('collapsed')),
);
$('#clearFinished').addEventListener('click', () => $('#clearConfirm').classList.remove('hidden'));
$('#cancelClear').addEventListener('click', () => $('#clearConfirm').classList.add('hidden'));
$('#confirmClear').addEventListener('click', () => {
  try {
    clearTaskRecords();
  } catch {
    showError('无法保存清理状态，请检查浏览器存储空间后重试。');
  }
});

async function openResearch(id) {
  if ($('#researchOverlay').classList.contains('hidden'))
    researchDockWasExpanded = !$('#taskDock').classList.contains('collapsed');
  document.body.classList.add('research-view');
  setTaskDockExpanded(false);
  currentResearchId = id;
  candidateOffset = 0;
  researchGraphState = {
    mode: 'overview',
    zoom: 1,
    pages: [],
    links: [],
    clusters: new Map(),
    baseWidth: 610,
  };
  $('#historyOverlay').classList.add('hidden');
  setResearchView('sources');
  $('#researchOverlay').classList.remove('hidden');
  $('#researchDetailTitle').textContent = '正在读取研究项目…';
  $('#trashResearchProject').disabled = true;
  $('#researchCandidates').innerHTML = '<p class="research-empty">读取候选页面…</p>';
  $('#researchFetched').innerHTML = '';
  await loadResearchDetail();
}
function setResearchView(view) {
  currentResearchView = ['map', 'sources', 'activity'].includes(view) ? view : 'sources';
  $$('[data-research-view]').forEach((button) =>
    button.classList.toggle('active', button.dataset.researchView === currentResearchView),
  );
  $$('[data-research-panel]').forEach((panel) =>
    panel.classList.toggle('active', panel.dataset.researchPanel === currentResearchView),
  );
}
function closeResearchWorkspace() {
  $('#researchOverlay').classList.add('hidden');
  document.body.classList.remove('research-view');
  if (researchDockWasExpanded) setTaskDockExpanded(true);
  researchDockWasExpanded = false;
}
const researchPageStatusLabels = {
  fetching: '获取中',
  fetched: '已获取',
  duplicate: '重复',
  irrelevant: '旧规则排除',
  failed: '失败',
  queued: '等待抓取',
  candidate: '保留候选',
  deferred_goal: '路线检查·暂缓',
  cancelled: '已取消',
  rejected: '人工忽略',
  skipped_budget: '超出旧页面额度',
  skipped_policy: '策略跳过',
};
function researchPageLabel(page) {
  if (page.title || page.link_text) return page.title || page.link_text;
  try {
    return new URL(page.url).pathname || page.url || '未命名页面';
  } catch {
    return page.url || '未命名页面';
  }
}
function researchGraphClusters(pages, links) {
  const grouped = new Map();
  pages.forEach((page) => {
    const key = `${page.depth}|${page.status}`;
    if (!grouped.has(key))
      grouped.set(key, { id: `cluster-${grouped.size}`, depth: page.depth, status: page.status, pages: [] });
    grouped.get(key).pages.push(page);
  });
  const pageCluster = new Map();
  grouped.forEach((cluster) => cluster.pages.forEach((page) => pageCluster.set(page.id, cluster.id)));
  const edges = new Map();
  const addEdge = (sourceId, targetId) => {
    const from = pageCluster.get(sourceId),
      to = pageCluster.get(targetId);
    if (!from || !to || from === to) return;
    const key = `${from}|${to}`;
    edges.set(key, (edges.get(key) || 0) + 1);
  };
  (links || []).forEach((link) => addEdge(link.source_page_id, link.target_page_id));
  pages.forEach((page) => addEdge(page.parent_page_id, page.id));
  return { clusters: [...grouped.values()], edges, pageCluster };
}
function renderResearchGraph(pages, links) {
  researchGraphState.pages = pages;
  researchGraphState.links = links || [];
  if (pages.length <= 80 && researchGraphState.mode === 'overview') researchGraphState.mode = 'all';
  drawResearchGraph();
}
function drawResearchGraph() {
  const root = $('#researchGraph'),
    pages = researchGraphState.pages,
    links = researchGraphState.links;
  if (!pages.length) {
    root.innerHTML = '<p class="research-empty">还没有可显示的探索路径</p>';
    return;
  }
  const clustered = researchGraphClusters(pages, links),
    overview = researchGraphState.mode === 'overview' && pages.length > 80;
  researchGraphState.clusters = new Map(clustered.clusters.map((item) => [item.id, item]));
  const graphItems = overview ? clustered.clusters : pages.map((page) => ({ ...page, pages: [page] }));
  const statusWeight = {
    fetching: 0,
    fetched: 1,
    duplicate: 2,
    failed: 3,
    queued: 4,
    candidate: 5,
    rejected: 6,
    skipped_budget: 7,
    skipped_policy: 8,
    cancelled: 9,
  };
  graphItems.sort(
    (a, b) =>
      a.depth - b.depth ||
      (statusWeight[a.status] ?? 10) - (statusWeight[b.status] ?? 10) ||
      (b.relevance || 0) - (a.relevance || 0),
  );
  const levels = new Map();
  graphItems.forEach((item) => {
    if (!levels.has(item.depth)) levels.set(item.depth, []);
    levels.get(item.depth).push(item);
  });
  const positions = new Map(),
    columnWidth = 190,
    nodeWidth = 162,
    nodeHeight = 38,
    rowHeight = 50;
  let maxRows = 1;
  for (const [depth, items] of levels) {
    maxRows = Math.max(maxRows, items.length);
    items.forEach((item, index) =>
      positions.set(item.id, { x: 18 + depth * columnWidth, y: 20 + index * rowHeight, item }),
    );
  }
  const width = Math.max(610, 36 + (Math.max(...levels.keys()) + 1) * columnWidth),
    height = Math.max(100, 40 + maxRows * rowHeight);
  researchGraphState.baseWidth = width;
  const edgeCounts = new Map();
  const addEdge = (fromId, toId, count = 1) => {
    if (!positions.has(fromId) || !positions.has(toId) || fromId === toId) return;
    const key = `${fromId}|${toId}`;
    edgeCounts.set(key, (edgeCounts.get(key) || 0) + count);
  };
  if (overview)
    clustered.edges.forEach((count, key) => {
      const [from, to] = key.split('|');
      addEdge(from, to, count);
    });
  else {
    (links || []).forEach((link) => addEdge(link.source_page_id, link.target_page_id));
    pages.forEach((page) => addEdge(page.parent_page_id, page.id));
  }
  const edges = [...edgeCounts]
    .map(([pair, count]) => {
      const [fromId, toId] = pair.split('|'),
        from = positions.get(fromId),
        to = positions.get(toId);
      return `<path class="graph-edge" style="stroke-width:${Math.min(4, 1 + Math.log2(count) / 2)}" d="M ${from.x + nodeWidth} ${from.y + nodeHeight / 2} C ${from.x + nodeWidth + 18} ${from.y + nodeHeight / 2}, ${to.x - 18} ${to.y + nodeHeight / 2}, ${to.x} ${to.y + nodeHeight / 2}"><title>${count} 条发现关系</title></path>`;
    })
    .join('');
  const nodes = graphItems
    .map((item) => {
      const p = positions.get(item.id),
        count = item.pages.length,
        label = overview
          ? `第 ${item.depth} 层 · ${researchPageStatusLabels[item.status] || item.status} (${count})`
          : researchPageLabel(item),
        short = label.length > 22 ? `${label.slice(0, 21)}…` : label;
      return `<g class="graph-node ${escapeHtml(item.status)}" data-research-graph-node="${escapeHtml(item.id)}" role="button" tabindex="0" aria-label="${escapeHtml(label)}" transform="translate(${p.x} ${p.y})"><title>${escapeHtml(label)}${overview ? '' : ` · ${researchPageStatusLabels[item.status] || item.status}`}</title><rect width="${nodeWidth}" height="${nodeHeight}"/><text x="9" y="23">${escapeHtml(short)}</text></g>`;
    })
    .join('');
  const zoom = researchGraphState.zoom,
    scaledWidth = Math.round(width * zoom),
    scaledHeight = Math.round(height * zoom),
    modeText = overview
      ? `${graphItems.length} 个聚类代表全部 ${pages.length} 个页面`
      : `显示全部 ${pages.length} 个页面`;
  root.innerHTML = `<div class="research-graph-toolbar"><span>${modeText} · 点击节点查看详情</span><div><button type="button" data-graph-action="overview" ${pages.length <= 80 || overview ? 'disabled' : ''}>聚类概览</button><button type="button" data-graph-action="all" ${!overview ? 'disabled' : ''}>全部节点</button><button type="button" data-graph-action="out" aria-label="缩小">−</button><output>${Math.round(zoom * 100)}%</output><button type="button" data-graph-action="in" aria-label="放大">＋</button><button type="button" data-graph-action="fit">适应宽度</button></div></div><div class="research-graph-viewport"><svg role="img" aria-label="研究页面探索关系图" width="${scaledWidth}" height="${scaledHeight}" viewBox="0 0 ${width} ${height}">${edges}${nodes}</svg></div><div id="researchGraphDetail" class="research-graph-detail"><p>选择页面或聚类后，这里会显示来源和规划依据。</p></div>`;
  $$('[data-graph-action]').forEach((button) =>
    button.addEventListener('click', () => changeResearchGraph(button.dataset.graphAction)),
  );
  $$('[data-research-graph-node]').forEach((node) => {
    const open = () => showResearchGraphDetail(node.dataset.researchGraphNode);
    node.addEventListener('click', open);
    node.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        open();
      }
    });
  });
}
function changeResearchGraph(action) {
  if (action === 'overview') researchGraphState.mode = 'overview';
  else if (action === 'all') researchGraphState.mode = 'all';
  else if (action === 'in') researchGraphState.zoom = Math.min(1.8, researchGraphState.zoom + 0.15);
  else if (action === 'out') researchGraphState.zoom = Math.max(0.35, researchGraphState.zoom - 0.15);
  else if (action === 'fit')
    researchGraphState.zoom = Math.max(
      0.35,
      Math.min(1, ($('#researchGraph').clientWidth - 4) / researchGraphState.baseWidth),
    );
  drawResearchGraph();
}
function showResearchGraphDetail(id) {
  const page = researchGraphState.pages.find((item) => item.id === id),
    cluster = researchGraphState.clusters.get(id),
    detail = $('#researchGraphDetail');
  if (!detail) return;
  if (page) {
    const status = researchPageStatusLabels[page.status] || page.status;
    detail.innerHTML = `<strong>${escapeHtml(researchPageLabel(page))}</strong><a href="${escapeHtml(page.canonical_url || page.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(page.canonical_url || page.url)}</a><span>${escapeHtml(status)} · 第 ${page.depth} 层 · ${escapeHtml(sourceRoleLabel(page.source_role))} · ${escapeHtml(topicLabels[page.topic_bucket] || '综合')} · 相关度 ${Math.round((page.relevance || 0) * 100)}%</span>${page.planning_reason ? `<p>${escapeHtml(page.planning_reason)}</p>` : ''}${page.error ? `<p class="page-error">${escapeHtml(page.error)}</p>` : ''}`;
    return;
  }
  if (cluster) {
    const roles = new Map(),
      topics = new Map();
    cluster.pages.forEach((item) => {
      const role = sourceRoleLabel(item.source_role),
        topic = topicLabels[item.topic_bucket] || '综合';
      roles.set(role, (roles.get(role) || 0) + 1);
      topics.set(topic, (topics.get(topic) || 0) + 1);
    });
    const summarize = (map) =>
      [...map]
        .sort((a, b) => b[1] - a[1])
        .map(([name, count]) => `${name} ${count}`)
        .join('、');
    detail.innerHTML = `<strong>第 ${cluster.depth} 层 · ${escapeHtml(researchPageStatusLabels[cluster.status] || cluster.status)} · ${cluster.pages.length} 个页面</strong><span>来源：${escapeHtml(summarize(roles) || '未分类')}</span><span>主题：${escapeHtml(summarize(topics) || '综合')}</span><p>${cluster.pages
      .slice(0, 20)
      .map((item) => escapeHtml(researchPageLabel(item)))
      .join(
        '；',
      )}${cluster.pages.length > 20 ? `；另有 ${cluster.pages.length - 20} 个，请切换“全部节点”查看。` : ''}</p>`;
  }
}
function renderResearchDuplicateClusters(clusters, pages, overrides) {
  const section = $('#researchDuplicateSection'),
    root = $('#researchDuplicateClusters'),
    eligible = pages.filter((page) => ['fetched', 'duplicate'].includes(page.status));
  section.classList.toggle('hidden', eligible.length < 2 && !clusters.length && !overrides.length);
  if (section.classList.contains('hidden')) {
    root.innerHTML = '';
    return;
  }
  const pageById = new Map(eligible.map((page) => [page.id, page])),
    pageName = (id) => researchPageLabel(pageById.get(id) || { url: id }),
    kindNames = { exact: '精确重复', near: '高相似', manual: '人工合并', mixed: '混合重复链' },
    decisionNames = { same: '同一重复簇', different: '保持独立' };
  const options = eligible
    .map(
      (page) =>
        `<option value="${escapeHtml(page.id)}">${escapeHtml(researchPageLabel(page).slice(0, 55))}</option>`,
    )
    .join('');
  const manager =
    eligible.length >= 2
      ? `<div class="duplicate-manager"><label>页面 A<select id="duplicateLeftPage">${options}</select></label><label>判断<select id="duplicateDecision"><option value="different">保持独立</option><option value="same">合并为同簇</option></select></label><label>页面 B<select id="duplicateRightPage">${options}</select></label><label>理由<input id="duplicateOverrideReason" maxlength="300" placeholder="为什么要校正自动判断"></label><button id="applyDuplicateOverride" type="button">应用校正</button></div>`
      : '';
  const overrideList = overrides.length
    ? `<div class="duplicate-overrides"><strong>人工校正记录</strong>${overrides.map((item) => `<div><span>${escapeHtml(pageName(item.left_page_id))} ↔ ${escapeHtml(pageName(item.right_page_id))}：${escapeHtml(decisionNames[item.decision] || item.decision)}${item.reason ? ` · ${escapeHtml(item.reason)}` : ''}</span><button type="button" data-reset-duplicate="${escapeHtml(item.left_page_id)}|${escapeHtml(item.right_page_id)}">恢复自动</button></div>`).join('')}</div>`
    : '';
  const clusterList = clusters.length
    ? clusters
        .map((cluster) => {
          const members = cluster.members || [],
            memberById = new Map(members.map((item) => [item.id, item])),
            representative = memberById.get(cluster.representative_page_id) || members[0] || {};
          return `<details><summary><span><strong>${escapeHtml(representative.title || representative.url || '未命名来源')}</strong><small>${escapeHtml(kindNames[cluster.kind] || cluster.kind)} · ${cluster.page_count} 页 · ${cluster.host_count} 个站点${cluster.max_similarity != null ? ` · 最高相似度 ${Math.round(cluster.max_similarity * 100)}%` : ''}</small></span><em>查看成员</em></summary><div>${members
            .map((member) => {
              const relation = member.relation,
                target = relation ? memberById.get(relation.target_page_id) : null,
                relationText = !relation
                  ? '代表页面'
                  : relation.kind === 'exact'
                    ? `与 ${target?.title || relation.target_page_id} 完全相同`
                    : relation.kind === 'manual'
                      ? `人工确认与 ${target?.title || relation.target_page_id} 同簇`
                      : `与 ${target?.title || relation.target_page_id} 高度相似 ${Math.round((relation.similarity || 0) * 100)}%`;
              const split =
                relation && relation.kind !== 'manual'
                  ? `<button type="button" data-separate-duplicate="${escapeHtml(member.id)}|${escapeHtml(relation.target_page_id)}">应保持独立</button>`
                  : '';
              return `<div class="duplicate-member"><a href="${escapeHtml(member.url)}" target="_blank" rel="noopener noreferrer"><strong>${escapeHtml(member.title || member.url)}</strong><small>${escapeHtml(relationText)}</small></a>${split}</div>`;
            })
            .join('')}</div></details>`;
        })
        .join('')
    : '<p class="research-empty">自动检查暂未发现重复簇；仍可在上方人工合并遗漏的重复页面。</p>';
  root.innerHTML = `<p>共 ${clusters.length} 个簇、涉及 ${clusters.reduce((sum, item) => sum + (item.page_count || 0), 0)} 个页面。原始判断与页面均保留，人工校正可随时撤销。</p>${manager}${overrideList}${clusterList}`;
  if ($('#duplicateRightPage')?.options.length > 1) $('#duplicateRightPage').selectedIndex = 1;
  $('#applyDuplicateOverride')?.addEventListener('click', () => {
    const left = $('#duplicateLeftPage').value,
      right = $('#duplicateRightPage').value,
      decision = $('#duplicateDecision').value,
      reason = $('#duplicateOverrideReason').value;
    setDuplicateOverride(left, right, decision, reason);
  });
  $$('[data-separate-duplicate]').forEach((button) =>
    button.addEventListener('click', () => {
      const [left, right] = button.dataset.separateDuplicate.split('|');
      setDuplicateOverride(left, right, 'different', '人工判定自动相似关系属于误判');
    }),
  );
  $$('[data-reset-duplicate]').forEach((button) =>
    button.addEventListener('click', () => {
      const [left, right] = button.dataset.resetDuplicate.split('|');
      setDuplicateOverride(left, right, 'automatic', '');
    }),
  );
}
async function setDuplicateOverride(left_page_id, right_page_id, decision, reason) {
  if (!left_page_id || !right_page_id || left_page_id === right_page_id)
    return showError('请选择两个不同页面。');
  if (decision === 'different' && !confirm('确认这两个页面应作为独立来源？原始自动相似度仍会保留。')) return;
  const response = await Api.request(`/api/research/${currentResearchId}/duplicate-overrides`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ left_page_id, right_page_id, decision, reason }),
    }),
    data = await response.json();
  if (!response.ok) return showError(data.detail || '无法保存重复关系校正');
  await loadResearchDetail();
}
function renderEvidenceGraph(research) {
  const root = $('#evidenceGraph'),
    evidenceById = new Map((research.evidence || []).map((item) => [item.id, item]));
  const sourceById = new Map((research.sources || []).map((item) => [item.id, item])),
    clusters = (research.corroboration || []).slice(0, 30);
  const usedSourceIds = [
    ...new Set(
      clusters.flatMap((cluster) =>
        (cluster.evidence_ids || []).map((id) => evidenceById.get(id)?.page_id).filter(Boolean),
      ),
    ),
  ]
    .filter((id) => sourceById.has(id))
    .slice(0, 20);
  if (!clusters.length || !usedSourceIds.length) {
    root.innerHTML = '<p class="research-empty">证据不足，尚不能形成跨来源关系图</p>';
    return;
  }
  const row = 45,
    sourceX = 18,
    claimX = 330,
    sourceW = 250,
    claimW = 270,
    height = Math.max(100, 35 + Math.max(usedSourceIds.length, clusters.length) * row),
    width = 630;
  const sourcePos = new Map(usedSourceIds.map((id, index) => [id, 20 + index * row])),
    clusterPos = new Map(clusters.map((item, index) => [index, 20 + index * row]));
  const edges = [];
  clusters.forEach((cluster, index) => {
    const pages = new Set(
      (cluster.evidence_ids || [])
        .map((id) => evidenceById.get(id)?.page_id)
        .filter((id) => sourcePos.has(id)),
    );
    pages.forEach((pageId) =>
      edges.push(
        `<path d="M ${sourceX + sourceW} ${sourcePos.get(pageId) + 15} C 292 ${sourcePos.get(pageId) + 15}, 305 ${clusterPos.get(index) + 15}, ${claimX} ${clusterPos.get(index) + 15}"/>`,
      ),
    );
  });
  const sources = usedSourceIds
    .map((id) => {
      const source = sourceById.get(id),
        y = sourcePos.get(id),
        label = (source.title || source.url || '未命名来源').slice(0, 30);
      return `<g class="source-node"><title>${escapeHtml(source.url || label)}</title><rect x="${sourceX}" y="${y}" width="${sourceW}" height="30"/><text x="${sourceX + 8}" y="${y + 19}">${escapeHtml(label)}</text></g>`;
    })
    .join('');
  const claims = clusters
    .map((cluster, index) => {
      const y = clusterPos.get(index),
        repeated = [
          'cross_site_repeated_text',
          'same_site_repeated_text',
          'near_duplicate_material',
        ].includes(candidateAssessment(cluster.assessment)),
        kind =
          candidateAssessment(cluster.assessment) === 'cross_site_candidates'
            ? 'cross-site'
            : candidateAssessment(cluster.assessment) === 'multi_page_candidates'
              ? 'same-site'
              : repeated
                ? 'repeated'
                : 'single',
        label = (cluster.representative_claim || '相关证据').slice(0, 34);
      return `<g class="${kind}"><title>${escapeHtml(cluster.representative_claim || '')}${repeated ? '（重复传播，不计作独立印证）' : ''}</title><rect x="${claimX}" y="${y}" width="${claimW}" height="30"/><text x="${claimX + 8}" y="${y + 19}">${escapeHtml(label)}</text></g>`;
    })
    .join('');
  root.innerHTML = `<svg role="img" aria-label="来源与证据主张关系图" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><text class="graph-legend" x="18" y="12">来源</text><text class="graph-legend" x="330" y="12">相近主张（绿色跨站、黄色同站、灰色单一来源）</text>${edges.join('')}${sources}${claims}</svg>`;
}
async function loadResearchDetail() {
  if (!currentResearchId) return;
  try {
    const [
      projectResponse,
      pagesResponse,
      candidatesResponse,
      duplicatesResponse,
      overridesResponse,
      reportsResponse,
      eventsResponse,
      linksResponse,
    ] = await Promise.all([
      Api.request(`/api/research/${currentResearchId}`),
      Api.request(`/api/research/${currentResearchId}/pages?limit=1000`),
      Api.request(`/api/research/${currentResearchId}/candidates?offset=${candidateOffset}&limit=50`),
      Api.request(`/api/research/${currentResearchId}/duplicates`),
      Api.request(`/api/research/${currentResearchId}/duplicate-overrides`),
      Api.request(`/api/research/${currentResearchId}/reports`),
      Api.request(`/api/research/${currentResearchId}/events?limit=50`),
      Api.request(`/api/research/${currentResearchId}/links?limit=1000`),
    ]);
    const [project, pages, candidateCatalog, duplicateClusters, duplicateOverrides, reports, events, links] =
      await Promise.all(
        [
          projectResponse,
          pagesResponse,
          candidatesResponse,
          duplicatesResponse,
          overridesResponse,
          reportsResponse,
          eventsResponse,
          linksResponse,
        ].map(readApiJson),
      );
    candidateOffset = candidateCatalog.offset || 0;
    mergeResearchTask(project);
    persistTasks();
    renderTaskDock();
    const statusNames = {
      queued: '等待抓取',
      crawling: '正在探索',
      awaiting_review: '旧任务待继续',
      paused: '已暂停',
      completed: '已完成',
      completed_with_errors: '完成但有失败',
      cancelled: '已取消',
    };
    const processed =
        (project.page_states?.fetched || 0) +
        (project.page_states?.duplicate || 0) +
        (project.page_states?.irrelevant || 0) +
        (project.page_states?.failed || 0),
      waitingBudget = project.page_states?.skipped_budget || 0;
    const stopNames = {
      time_limit_reached: '资料搜集时限已到，已转入报告整理',
      page_budget_reached: `本轮页面额度已用完：已处理 ${processed}/${project.max_pages}${waitingBudget ? `，另有 ${waitingBudget} 个候选已保留` : ''}`,
      awaiting_candidate_review: '旧流程停止，继续后自动访问候选',
      queue_exhausted: '当前可用队列已处理完',
      queue_exhausted_with_failures: '当前队列已处理完，部分材料获取或阅读未成功',
      search_discovery_failed: '未取得可用搜索入口，具体原因见过程记录',
      search_unavailable: '搜索服务受阻；已保留查询和剩余时间，请查看运行记录后继续',
      search_recovery_wait: '搜索暂不可用；后台低频探测中，恢复后沿原任务继续',
      planning_failed: '调查步骤出错，已保留现场；具体原因见过程记录',
      user_paused: '用户暂停',
      user_cancelled: '用户取消',
      service_restarted: '服务重启后安全暂停',
    };
    const searchState = ['queued', 'running'].includes(project.search_status)
      ? ' · 正在后台搜索跨来源入口'
      : project.search_status === 'failed'
        ? project.status === 'paused'
          ? ' · 搜索未成功，现场已保留'
          : ' · 跨来源搜索失败，继续处理已有材料'
        : project.search_status === 'completed'
          ? ' · 跨来源搜索完成'
          : '';
    const projectStatus = project.workflow
      ? workflowLabel(project.workflow)
      : project.stop_reason === 'page_budget_reached'
        ? '本轮探索结束'
        : statusNames[project.status] || project.status;
    $('#researchDetailTitle').textContent = project.goal;
    $('#trashResearchProject').dataset.pagesFetched = String(project.pages_fetched || 0);
    $('#trashResearchProject').disabled = false;
    $('#researchDetailStatus').textContent =
      `${projectStatus} · ${project.workflow ? '自动研究与报告' : project.mode === 'automatic' ? '旧版自动抓取' : '旧版候选流程'} · ${project.scope === 'same_host' ? '仅起始站点' : '允许公开站外链接'}${searchState}${project.stop_reason ? ` · ${stopNames[project.stop_reason] || project.stop_reason}` : ''}`;
    $('#researchStats').innerHTML =
      `<div><strong>${project.pages_fetched}</strong><span>已获取页面</span></div><div><strong>${project.page_states?.irrelevant || 0}</strong><span>旧规则排除</span></div><div><strong>${project.pages_failed}</strong><span>获取失败</span></div><div><strong>${project.links_found}</strong><span>发现链接</span></div><div><strong>${project.workflow ? Math.round(project.workflow.limit_seconds / 60) : project.max_pages}</strong><span>${project.workflow ? '搜集时限（分钟）' : '旧版页面上限'}</span></div>`;
    renderResearchGraph(pages, links);
    let planView = $('#researchIntentView');
    if (!planView) {
      planView = document.createElement('details');
      planView.id = 'researchIntentView';
      planView.className = 'research-intent';
      $('#researchStats').closest('.research-summary-bar').after(planView);
    }
    const intent = project.intent,
      plan = intent?.plan;
    planView.hidden = !intent;
    planView.innerHTML = intent
      ? `<summary>研究路线 · ${escapeHtml(intent.stage || intent.status)}</summary>${plan?.subject ? `<p>${escapeHtml(plan.subject)}</p><p>${escapeHtml(plan.scope_note || '')}</p><ol>${(plan.questions || []).map((q) => `<li>${escapeHtml(q.question)}<small>所需证据：${escapeHtml(q.evidence)}</small></li>`).join('')}</ol>` : ''}${intent.error ? `<p>${escapeHtml(intent.error)}</p>` : ''}<p class="research-empty">路线判断不是研究结论；暂缓的线索仍保留。${project.page_states?.deferred_goal || 0} 条链接暂缓访问。</p>`
      : '';
    renderResearchDuplicateClusters(duplicateClusters, pages, duplicateOverrides);
    const controls = [];
    const labels = {
      pause: project.stop_reason === 'search_recovery_wait' ? '停止自动重试' : '暂停',
      'continue-automatic': '继续自动研究',
      'continue-with-model': '更换模型后继续',
      'retry-search': '重试跨来源搜索',
      'open-report': reports.length ? `阅读报告 v${reports[0].version}` : '阅读已有报告',
      audit: '独立语义复核',
      synthesize: '生成研究报告',
      cancel: '取消项目',
      'extend-time': '延长时间',
      'retry-report': '重试整理报告',
    };
    const reportJobId = project.workflow?.runtime?.job?.id || project.workflow?.report_job_id;
    for (const name of researchActions(project, reports)) {
      if (reportJobId && ['pause-job', 'resume-job'].includes(name)) {
        controls.push(
          `<button type="button" data-report-job-action="${name === 'pause-job' ? 'pause' : 'resume'}" data-job-id="${escapeHtml(reportJobId)}" title="临时暂停保留处理现场，不会卸载模型或释放显存">${name === 'pause-job' ? '暂停整理' : '继续整理'}</button>`,
        );
      }
      if (labels[name])
        controls.push(
          `<button data-research-action="${name}"${name === 'cancel' ? ' class="danger"' : ''}>${labels[name]}</button>`,
        );
    }
    $('#researchControls').innerHTML = controls.join('');
    $$('[data-research-action]').forEach((button) =>
      button.addEventListener('click', () => controlResearch(button.dataset.researchAction)),
    );
    $$('#researchControls [data-report-job-action]').forEach((button) =>
      button.addEventListener('click', () =>
        changeTaskRunState(button.dataset.jobId, button.dataset.reportJobAction, button),
      ),
    );
    const candidates = candidateCatalog.items || [],
      fetched = pages.filter((page) =>
        ['fetched', 'duplicate', 'irrelevant', 'failed'].includes(page.status),
      );
    const groupSummary = (candidateCatalog.groups || [])
      .slice(0, 12)
      .map(
        (group) =>
          `<span>${escapeHtml(sourceRoleLabel(group.source_role))}/${escapeHtml(topicLabels[group.topic_bucket] || '综合')} ${group.count}</span>`,
      )
      .join('');
    const candidateItems = candidates
      .map((page) => {
        const cache = page.discovery?.search_cache,
          cacheText =
            cache === 'cache_stale_fallback'
              ? ' · 搜索缓存回退（可能过期）'
              : cache === 'cache_fresh'
                ? ' · 搜索缓存'
                : '';
        return `<article class="research-page"><span><strong>${escapeHtml(page.link_text || page.title || '未命名链接')}</strong><small>${escapeHtml(page.canonical_url)}</small>${page.planning_reason ? `<small class="candidate-purpose">${escapeHtml(page.planning_reason)}</small>` : ''}<small>${escapeHtml(sourceRoleLabel(page.source_role))}${cacheText}</small></span><em>候选</em></article>`;
      })
      .join('');
    const candidateNav = candidateCatalog.total
      ? `<div class="candidate-nav"><button type="button" data-candidate-offset="${Math.max(0, candidateOffset - candidateCatalog.limit)}" ${candidateCatalog.has_previous ? '' : 'disabled'}>上一页</button><span>${candidateOffset + 1}–${candidateOffset + candidates.length} / ${candidateCatalog.total}</span><button type="button" data-candidate-offset="${candidateOffset + candidateCatalog.limit}" ${candidateCatalog.has_more ? '' : 'disabled'}>下一页</button></div>`
      : '';
    $('#researchCandidates').innerHTML = candidateCatalog.total
      ? `<div class="candidate-summary"><strong>保留候选 ${candidateCatalog.total} 个，本页最多 50 个</strong><div>${groupSummary}</div></div>${candidateItems}${candidateNav}`
      : '<p class="research-empty">当前没有保留候选链接</p>';
    $('#researchFetched').innerHTML =
      fetched.length ||
      project.page_states?.skipped_budget ||
      project.page_states?.skipped_policy ||
      project.page_states?.rejected
        ? `${fetched.map((page) => `<article class="research-page fetched ${page.status}"><span><strong>${escapeHtml(page.title || page.link_text || '未命名页面')}</strong><small>${escapeHtml(page.canonical_url)}</small><small>${escapeHtml(sourceRoleLabel(page.source_role))} · ${escapeHtml(topicLabels[page.topic_bucket] || '综合')}${page.information_gain != null ? ` · 新词片段 ${Math.round(page.information_gain * 100)}%（非新增事实）` : ''}${page.research_filter?.goal_coverage ? ` · 字词重叠（非回答覆盖） ${Math.round((page.research_filter.goal_coverage.score || 0) * 100)}%` : ''}${page.near_duplicate ? ` · 高相似候选 ${Math.round(page.near_duplicate.similarity * 100)}%` : ''}</small>${page.status === 'irrelevant' ? '<small>旧词汇规则曾排除此页。可重新纳入已存正文；不会重新抓取或改变旧报告。</small>' : ''}${page.near_duplicate ? `<small>与页面 ${escapeHtml(page.near_duplicate.page_id)} 高度相似；正文仍保留</small>` : ''}${page.error ? `<small class="page-error">${escapeHtml(page.error)}</small>` : ''}</span><em>${page.status === 'failed' ? '失败' : page.status === 'duplicate' ? '重复' : page.status === 'irrelevant' ? '旧规则排除' : `第 ${page.depth} 层`}</em>${page.status === 'failed' ? `<button type="button" data-retry-research-page="${page.id}">重试</button>` : ''}${page.status === 'irrelevant' ? `<button type="button" data-retain-research-page="${page.id}">纳入已存正文</button>` : ''}</article>`).join('')}${project.page_states?.skipped_budget ? `<p class="research-empty">另有 ${project.page_states.skipped_budget} 个候选因页面预算停止，仍保存在项目中。</p>` : ''}${project.page_states?.skipped_policy ? `<p class="research-empty">另有 ${project.page_states.skipped_policy} 个全站地图链接因跨来源范围策略停用，发现记录仍保留。</p>` : ''}${project.page_states?.rejected ? `<p class="research-empty">另有 ${project.page_states.rejected} 个候选经人工审核忽略，发现记录仍保留。</p>` : ''}`
        : '<p class="research-empty">尚未获取页面</p>';
    $('#researchEvents').innerHTML = events.length
      ? events
          .map(
            (event) =>
              `<article class="research-event ${escapeHtml(event.level)}"><time>${escapeHtml(new Date(event.created_at).toLocaleString())}</time><span>${escapeHtml(event.message)}</span></article>`,
          )
          .join('')
      : '<p class="research-empty">还没有运行记录</p>';
    $$('[data-retry-research-page]').forEach((button) =>
      button.addEventListener('click', () => retryResearchPage(button.dataset.retryResearchPage)),
    );
    $$('[data-retain-research-page]').forEach((button) =>
      button.addEventListener('click', async () => {
        if (!confirm('将此页已保存正文纳入后续研究？不会重新抓取、启动模型或改变旧报告。')) return;
        button.disabled = true;
        try {
          const response = await Api.request(
            `/api/research/${encodeURIComponent(currentResearchId)}/pages/${encodeURIComponent(button.dataset.retainResearchPage)}/retain-saved`,
            { method: 'POST' },
          );
          const data = await response.json();
          if (!response.ok) throw Error(data.detail || '未能纳入已存正文');
          await openResearch(currentResearchId);
          setResearchView('sources');
        } catch (error) {
          showError(error.message);
          button.disabled = false;
        }
      }),
    );
    $$('[data-candidate-offset]').forEach((button) =>
      button.addEventListener('click', () => {
        candidateOffset = Number(button.dataset.candidateOffset) || 0;
        loadResearchDetail();
      }),
    );
    persistTasks();
    renderTaskDock();
  } catch (error) {
    $('#researchDetailTitle').textContent = '研究项目读取失败';
    $('#researchDetailStatus').textContent = error.message;
  }
}
async function controlResearch(action) {
  if (!currentResearchId) return;
  if (action === 'cancel' && !confirm('取消后将停止未开始的页面，已获取内容仍会保留。确定取消？')) return;
  if (['continue-automatic', 'continue-with-model', 'retry-report', 'extend-time'].includes(action)) {
    await workflowAction(currentResearchId, action);
    return;
  }
  if (action === 'open-report') {
    const response = await Api.request(`/api/research/${currentResearchId}/reports/latest?view=reader`),
      data = await response.json();
    if (!response.ok) return showError(data.detail || '无法读取研究报告');
    result = data;
    currentHistoryId = data.history_id || null;
    closeResearchWorkspace();
    showResult();
    return;
  }
  if (action === 'synthesize') {
    const selected = modelRegistry.find((item) => item.id === $('#taskModelSelect').value);
    if (!selected) return showError('请先在设置中添加一个可用模型。');
    const response = await Api.request(`/api/research/${currentResearchId}/synthesize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...modelRequest(selected), structure_mode: 'exhaustive' }),
      }),
      data = await response.json();
    if (!response.ok) return showError(data.detail || '无法创建研究报告任务');
    const project = trackedTasks.find((item) => item.kind === 'research' && item.id === currentResearchId);
    pinTask({
      ...data,
      label: `研究报告：${project?.label || '未命名项目'}`,
      modelName: selected.name,
    });
    setTaskDockExpanded(true);
    startTaskPolling();
    closeResearchWorkspace();
    return;
  }
  if (action === 'audit') {
    const selected = modelRegistry.find((item) => item.id === $('#taskModelSelect').value);
    if (!selected) return showError('请先在设置中添加一个可用模型。');
    const response = await Api.request(`/api/research/${currentResearchId}/audit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...modelRequest(selected), structure_mode: 'detailed' }),
      }),
      data = await response.json();
    if (!response.ok) return showError(data.detail || '无法创建语义复核任务');
    const project = trackedTasks.find((item) => item.kind === 'research' && item.id === currentResearchId);
    pinTask({
      ...data,
      label: `报告复核：${project?.label || '未命名项目'}`,
      modelName: selected.name,
    });
    setTaskDockExpanded(true);
    startTaskPolling();
    closeResearchWorkspace();
    return;
  }
  if (action === 'extend-budget') {
    const raw = prompt('要增加多少个页面预算？原证据与探索路径会保留。', '10');
    if (raw === null) return;
    const additional = Number(raw);
    if (!Number.isInteger(additional) || additional < 1 || additional > 200)
      return showError('请输入 1–200 的整数。');
    const response = await Api.request(`/api/research/${currentResearchId}/extend`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ additional_pages: additional }),
      }),
      data = await response.json();
    if (!response.ok) return showError(data.detail || '无法增加页面预算');
    mergeResearchTask(data);
    candidateOffset = 0;
    persistTasks();
    renderTaskDock();
    startTaskPolling();
    await loadResearchDetail();
    return;
  }
  const response = await Api.request(`/api/research/${currentResearchId}/${action}`, { method: 'POST' }),
    data = await response.json();
  if (!response.ok) return showError(data.detail || '操作失败');
  mergeResearchTask(data);
  persistTasks();
  startTaskPolling();
  await loadResearchDetail();
}
async function retryResearchPage(pageId) {
  const response = await Api.request(`/api/research/${currentResearchId}/pages/${pageId}/retry`, {
      method: 'POST',
    }),
    data = await response.json();
  if (!response.ok) return showError(data.detail || '无法重试页面');
  mergeResearchTask(data);
  persistTasks();
  startTaskPolling();
  await loadResearchDetail();
}
$$('[data-research-view]').forEach((button) =>
  button.addEventListener('click', () => setResearchView(button.dataset.researchView)),
);
$('#closeResearch').addEventListener('click', closeResearchWorkspace);
$('#trashResearchProject').addEventListener('click', async (event) => {
  if (!currentResearchId) return;
  const button = event.currentTarget;
  const label = `${$('#researchDetailTitle').textContent}（已获取 ${button.dataset.pagesFetched || 0} 页，ID ${currentResearchId.slice(0, 8)}）`;
  if (await archiveRecord('research', currentResearchId, label, button)) {
    closeResearchWorkspace();
    await openHistory('research');
  }
});

function showError(message) {
  els.error.textContent = message;
  els.error.classList.toggle('hidden', !message);
}
function cleanTimelineItem(item) {
  let event = String(item?.event || '').trim(),
    date = String(item?.date || '').trim();
  const combined = `${date} ${event}`.trim();
  const fullDate = combined.match(
    /(?:19|20)\d{2}(?:\s*年\s*(?:\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?|(?:\s*[-/.]\s*|\s+)\d{1,2}\s*[-/.]\s*\d{1,2}|\s*[-/.]\s*\d{1,2})/,
  );
  if (fullDate) {
    const numbers = fullDate[0].match(/\d+/g) || [];
    if (fullDate[0].includes('年'))
      date = `${numbers[0]}年${numbers[1] ? `${Number(numbers[1])}月` : ''}${numbers[2] ? `${Number(numbers[2])}日` : ''}`;
    else if (numbers.length >= 3)
      date = `${numbers[0]}-${String(Number(numbers[1])).padStart(2, '0')}-${String(Number(numbers[2])).padStart(2, '0')}`;
    else if (numbers.length === 2) date = `${numbers[0]}-${String(Number(numbers[1])).padStart(2, '0')}`;
    event =
      `${combined.slice(0, fullDate.index)}${combined.slice(fullDate.index + fullDate[0].length)}`.trim();
  }
  event = event.replace(/^[\s；;，,:：·|—-]*(?:\d{1,2}:\d{2}(?::\d{2})?)?[\s；;，,:：·|—-]*/, '');
  if (!event || /^(?:来源|稿源|编辑|责任编辑|发布时间|更新时间)[：:].{0,100}$/i.test(event)) return null;
  return { date, event };
}
function dateTimeValue(value) {
  const numbers = String(value || '').match(/\d+/g) || [];
  if (!numbers.length) return '';
  return `${numbers[0]}${numbers[1] ? `-${String(Number(numbers[1])).padStart(2, '0')}` : ''}${numbers[2] ? `-${String(Number(numbers[2])).padStart(2, '0')}` : ''}`;
}
function prepareTreeOverview(tree) {
  if (!tree || tree._overviewPrepared) return;
  const size = (node) => 1 + (node.children || []).reduce((sum, child) => sum + size(child), 0);
  if (size(tree) > 28)
    (tree.children || []).forEach((branch) => {
      if ((branch.children || []).length) branch._collapsed = true;
    });
  tree._overviewPrepared = true;
}
function refreshBasicOutline() {
  const host = $('#basicOutline'),
    section = $('#basicOutlineSection');
  if (!host || !section || typeof renderBasicOutline !== 'function') return;
  renderBasicOutline(host, result);
  section.hidden = host.hidden;
  const readingHost = $('#webpageReading'),
    readingSection = $('#webpageReadingSection');
  if (readingHost && readingSection && typeof renderWebpageReading === 'function') {
    renderWebpageReading(readingHost, result);
    readingSection.hidden = readingHost.hidden;
  }
}
function showResult() {
  document.body.classList.add('instant-view-switch');
  document.body.classList.add('result-view');
  els.loading.classList.add('hidden');
  els.composer.classList.add('hidden');
  els.workspace.classList.remove('hidden');
  currentHistoryId = result.history_id || null;
  $('#trashAnalysisResult').classList.toggle('hidden', !currentHistoryId || !!result.research?.project_id);
  prepareTreeOverview(result.tree);
  setEditing(false, false);
  refreshBasicOutline();
  const resultNotices = [result.batch_summary, ...(result.warnings || [])].filter(Boolean);
  $('#batchNotice').textContent = resultNotices.join('；');
  $('#batchNotice').classList.toggle('hidden', !resultNotices.length);
  const quality = result.quality_validation,
    qualityNames = { normal: '正常完成', needs_review: '需要复核', degraded: '降级完成' };
  $('#qualityBadge').textContent = quality
    ? `${qualityNames[quality.status] || quality.status} · ${quality.page_type || 'document'}`
    : '';
  $('#qualityBadge').className = `quality-badge ${quality?.status || ''}${quality ? '' : ' hidden'}`;
  if (result.report_quality) {
    const reportNames = {
      source_grounded: '可回查原文',
      needs_review: '分析待核实',
      evidence_only: '材料摘录',
      partial: '部分结果',
    };
    $('#qualityBadge').textContent = reportNames[result.report_quality.status] || '研究结果';
    $('#qualityBadge').className =
      `quality-badge ${result.report_quality.status === 'source_grounded' ? 'normal' : 'needs_review'}`;
  }
  $('#resultTitle').textContent = result.title;
  $('#summary').textContent = result.summary;
  if (typeof renderReportReader === 'function') renderReportReader(result);
  $('#keywords').innerHTML = (Array.isArray(result.keywords) ? result.keywords : [])
    .map((word) => `<span>${escapeHtml(word)}</span>`)
    .join('');
  const facts = result.key_facts || [],
    insights = result.insights || [],
    timeline = (result.timeline || []).map(cleanTimelineItem).filter(Boolean);
  $('#factsSection').classList.toggle('hidden', !facts.length);
  $('#factCards').innerHTML = facts
    .map(
      (item) =>
        `<div class="fact-card"><strong>${escapeHtml(item.label || '材料要点')}${typeof reportSupport === 'function' ? reportSupport(item.support_status) : ''}</strong>${escapeHtml(item.value)}</div>`,
    )
    .join('');
  $('#insightsSection').classList.toggle('hidden', !insights.length);
  $('#insightCards').innerHTML = insights
    .map(
      (item) =>
        `<div class="insight-card"><strong>${escapeHtml(item.title || '洞察')}${typeof reportSupport === 'function' ? reportSupport(item.support_status) : ''}</strong>${escapeHtml(item.detail)}</div>`,
    )
    .join('');
  $('#timelineSection').classList.toggle('hidden', !timeline.length);
  $('#timeline').innerHTML = timeline
    .map(
      (item) =>
        `<div class="timeline-item"><time${dateTimeValue(item.date) ? ` datetime="${dateTimeValue(item.date)}"` : ''}>${escapeHtml(item.date || '时间未注明')}</time>${escapeHtml(item.event)}</div>`,
    )
    .join('');
  const validation = result.evidence_validation,
    corrections = validation?.corrections || [];
  $('#evidenceSection').classList.toggle('hidden', !validation);
  $('#evidenceCards').innerHTML = renderNumericReview(validation);
  const research = result.research;
  $('#researchResultSection').classList.toggle('hidden', !research);
  if (research) {
    const checks = research.corroboration || [],
      cross = checks.filter(
        (item) => candidateAssessment(item.assessment) === 'cross_site_candidates',
      ).length,
      multi = checks.filter(
        (item) => candidateAssessment(item.assessment) === 'multi_page_candidates',
      ).length,
      repeated = checks.filter((item) =>
        ['cross_site_repeated_text', 'same_site_repeated_text', 'near_duplicate_material'].includes(
          candidateAssessment(item.assessment),
        ),
      ).length,
      single = checks.filter((item) => candidateAssessment(item.assessment) === 'single_source').length;
    const reliability = research.source_reliability || {},
      temporal = reliability.temporal_coverage || {},
      identity = reliability.identity_signals || {},
      social = reliability.social_samples || [],
      duplicateClusters = research.duplicate_clusters || [];
    const socialSummary = social.length
      ? `<span>平台评论样本 ${social.reduce((sum, item) => sum + (item.comments_sampled || 0), 0)} 条 · 采样页 ${social.reduce((sum, item) => sum + (item.pages_succeeded || 0), 0)}/${social.reduce((sum, item) => sum + (item.pages_requested || 0), 0)} 成功</span>`
      : '';
    $('#researchResultMeta').innerHTML =
      `<strong>${research.sources.length} 个页面来源 · ${reliability.independent_source_count ?? research.sources.length} 个去重来源单元 · ${research.evidence.length} 条证据</strong><span>节点引用覆盖率 ${Math.round((research.citation_coverage || 0) * 100)}% · 跨站相近材料 ${cross} · 同站相近材料 ${multi} · 重复传播 ${repeated} · 单一来源 ${single}</span>${duplicateClusters.length ? `<span>重复内容簇 ${duplicateClusters.length} 个 · 涉及 ${duplicateClusters.reduce((sum, item) => sum + (item.page_count || 0), 0)} 个页面</span>` : ''}<span>有日期来源 ${temporal.dated_sources || 0}/${research.sources.length}${temporal.newest_published_at ? ` · 最新发布日期 ${escapeHtml(temporal.newest_published_at)}` : ''} · 身份声明 ${identity.identity_declared || 0} · 已核验第一方 ${identity.verified_first_party || 0}</span>${socialSummary}${(research.limitations || []).map((item) => `<small>${escapeHtml(item)}</small>`).join('')}`;
    const conflictEvidence = new Map(research.evidence.map((item) => [item.id, item])),
      conflictSources = new Map(research.sources.map((item) => [item.id, item]));
    const audit = research.semantic_audit,
      auditNames = {
        passed: '本次未发现来源支持问题',
        needs_review: '需要人工复核',
        incomplete: '复核不完整',
      },
      issueNames = {
        unsupported_claim: '证据不足',
        overstatement: '表述夸大',
        missing_context: '缺少限定',
        conflict_ignored: '忽略冲突',
        ambiguous_attribution: '归因含混',
        uncited_claim: '节点无引用',
        other: '其他问题',
      };
    $('#semanticAudit').classList.toggle('hidden', !audit);
    $('#semanticAudit').innerHTML = audit
      ? `<header><strong>${auditNames[audit.status] || escapeHtml(audit.status)}</strong><span>覆盖 ${Math.round((audit.coverage || 0) * 100)}% · ${audit.audited_claims || 0}/${audit.claim_count || 0} 条主张 · ${escapeHtml(audit.model || '未知模型')}</span></header>${audit.model_summary ? `<p>${escapeHtml(audit.model_summary)}</p>` : ''}<div>${(
          audit.issues || []
        )
          .map((item) => {
            const traces = (item.evidence_ids || [])
              .map((id) => conflictEvidence.get(id))
              .filter(Boolean)
              .map((evidence) => {
                const source = conflictSources.get(evidence.page_id) || {};
                return `<small>“${escapeHtml((evidence.quote || evidence.claim || '').slice(0, 180))}”${source.url ? ` <a href="${escapeHtml(source.url)}" target="_blank" rel="noopener">查看证据</a>` : ''}</small>`;
              })
              .join('');
            return `<article class="${escapeHtml(item.severity || 'medium')}"><strong>${escapeHtml(issueNames[item.kind] || item.kind)} · ${escapeHtml(item.claim_text || item.claim_id)}</strong><span>${escapeHtml(item.explanation || '')}</span>${item.suggested_revision ? `<em>建议：${escapeHtml(item.suggested_revision)}</em>` : ''}${traces}</article>`;
          })
          .join('')}</div>`
      : '';
    $('#researchConflicts').innerHTML = (research.conflicts || [])
      .map((item) => {
        const label =
          item.kind === 'numeric_claim_mismatch'
            ? '待核验数值差异'
            : item.kind === 'factual_polarity_mismatch'
              ? '待核验事实差异'
              : '存在观点分歧';
        const traces = (item.evidence_ids || [])
          .map((id) => conflictEvidence.get(id))
          .filter(Boolean)
          .map((evidence) => {
            const source = conflictSources.get(evidence.page_id) || {};
            return `<small>“${escapeHtml((evidence.quote || evidence.claim || '').slice(0, 180))}”${source.url ? ` <a href="${escapeHtml(source.url)}" target="_blank" rel="noopener">查看来源</a>` : ''}</small>`;
          })
          .join('');
        return `<div><strong>${label}</strong><span>${escapeHtml(item.message)}</span>${traces}</div>`;
      })
      .join('');
    $('#researchSources').innerHTML = research.sources
      .map(
        (source) =>
          `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener"><strong>${escapeHtml(source.title || '未命名来源')}</strong><small>${escapeHtml(source.url)}</small></a>`,
      )
      .join('');
    renderEvidenceGraph(research);
  }
  $('#sourceMeta').textContent =
    result.source.type === 'url'
      ? result.source.title || '网页'
      : result.source.type === 'research'
        ? `${result.source.pages} 个页面 · ${result.source.evidence} 条证据`
        : `${result.source.characters.toLocaleString()} 字文本`;
  $('#modelMeta').textContent =
    `${result.runtime.model} · ${result.runtime.provider === 'api' ? 'API' : '本地'}`;
  $('#timeMeta').textContent = `${result.runtime.seconds} 秒`;
  transform = { x: 70, y: 80, scale: 1 };
  clearTimeout(mapRenderTimer);
  $('.map-panel').classList.add('rendering');
  $('#mapRenderState').classList.remove('hidden');
  requestAnimationFrame(() => {
    document.body.classList.remove('instant-view-switch');
    mapRenderTimer = setTimeout(() => {
      renderMap();
      fitMap();
      $('.map-panel').classList.remove('rendering');
      $('#mapRenderState').classList.add('hidden');
    }, 0);
  });
}
$('#backBtn').addEventListener('click', () => {
  document.body.classList.add('instant-view-switch');
  setEditing(false, false);
  clearTimeout(mapRenderTimer);
  document.body.classList.remove('result-view');
  document.body.classList.remove('reader-view');
  $('.map-panel').classList.remove('rendering');
  $('#mapRenderState').classList.add('hidden');
  els.workspace.classList.add('hidden');
  els.composer.classList.remove('hidden');
  requestAnimationFrame(() => document.body.classList.remove('instant-view-switch'));
});
$('#trashAnalysisResult').addEventListener('click', async (event) => {
  if (!currentHistoryId || result?.research?.project_id) return;
  const id = currentHistoryId;
  if (await archiveRecord('analysis', id, `${result.title || '分析结果'}（ID ${id.slice(0, 8)}）`, event.currentTarget)) {
    $('#backBtn').click();
    await openHistory('analysis');
  }
});

function editStatus(message) {
  $('#editStatus').textContent = message;
  $('#editStatus').classList.toggle('hidden', !message);
}
function setEditing(value, restore = false) {
  editStatus('');
  if (restore && editBackup) result = structuredClone(editBackup);
  editing = value;
  if (value) {
    editBackup = structuredClone(result);
    $('#titleEditor').value = result.title;
    $('#summaryEditor').value = result.summary;
    $('#keywordsEditor').value = (result.keywords || []).join('，');
  }
  $('#resultTitle').classList.toggle('hidden', value);
  $('#summary').classList.toggle('hidden', value);
  $('#keywords').classList.toggle('hidden', value);
  $('#titleEditor').classList.toggle('hidden', !value);
  $('#summaryEditor').classList.toggle('hidden', !value);
  $('#keywordsEditor').classList.toggle('hidden', !value);
  $('#editResult').classList.toggle('hidden', value);
  $('#saveResult').classList.toggle('hidden', !value);
  $('#cancelEdit').classList.toggle('hidden', !value);
  if (!value) {
    editBackup = null;
    els.detail.classList.add('hidden');
  }
  if (restore) showResult();
}
$('#editResult').addEventListener('click', () => setEditing(true));
$('#cancelEdit').addEventListener('click', () => setEditing(false, true));
$('#saveResult').addEventListener('click', async () => {
  result.title = $('#titleEditor').value || result.title;
  result.summary = $('#summaryEditor').value;
  result.keywords = $('#keywordsEditor')
    .value.split(/[,，]/)
    .map((item) => item.trim())
    .filter(Boolean);
  if (!currentHistoryId) return editStatus('这项结果还没有历史记录，无法保存。');
  editStatus('');
  const button = $('#saveResult');
  button.disabled = true;
  try {
    const response = await Api.request(`/api/history/${currentHistoryId}?view=reader`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expected_revision: editBackup?.edit_revision,
        title: result.title,
        summary: result.summary,
        keywords: result.keywords,
        tree: serializeTree(result.tree),
      }),
    });
    const data = await response.json();
    if (!response.ok)
      return editStatus(
        typeof data.detail === 'string' ? data.detail : '保存失败，编辑内容仍保留，请检查后重试。',
      );
    result = data;
    setEditing(false, false);
    showResult();
  } catch (error) {
    if ([409, 428].includes(error.status)) {
      editStatus(`${error.message} 当前编辑内容仍保留。`);
      const link = document.createElement('a');
      link.href = result.research?.project_id
        ? `/?research=${encodeURIComponent(result.research.project_id)}`
        : `/?history=${encodeURIComponent(currentHistoryId)}`;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = ' 在新窗口核对已保存版本';
      $('#editStatus').append(link);
    } else {
      editStatus(`保存未确认，编辑内容仍保留。请检查连接后重试：${error.message}`);
    }
  } finally {
    button.disabled = false;
  }
});

function visibleTree(root) {
  const rows = [],
    rowGap = 24,
    columnGap = 90;
  function build(node, depth, parent = null) {
    const metrics = nodeMetrics(node, depth);
    const item = {
      node,
      depth,
      parent,
      x: depth * (metrics.width + columnGap),
      y: 0,
      children: [],
      ...metrics,
    };
    rows.push(item);
    const kids = node._collapsed ? [] : node.children || [];
    kids.forEach((child) => item.children.push(build(child, depth + 1, item)));
    const childrenHeight =
      item.children.reduce((sum, child) => sum + child.subtreeHeight, 0) +
      Math.max(0, item.children.length - 1) * rowGap;
    item.subtreeHeight = Math.max(item.height, childrenHeight);
    return item;
  }
  function place(item, top) {
    item.y = top + (item.subtreeHeight - item.height) / 2;
    const childrenHeight =
      item.children.reduce((sum, child) => sum + child.subtreeHeight, 0) +
      Math.max(0, item.children.length - 1) * rowGap;
    let childTop = top + (item.subtreeHeight - childrenHeight) / 2;
    item.children.forEach((child) => {
      place(child, childTop);
      childTop += child.subtreeHeight + rowGap;
    });
  }
  const rootItem = build(root, 0);
  place(rootItem, 0);
  return rows;
}
function renderMap() {
  const rows = visibleTree(result.tree);
  els.links.innerHTML = rows
    .filter((d) => d.parent)
    .map((d) => {
      const x1 = d.parent.x + d.parent.width,
        y1 = d.parent.y + d.parent.height / 2,
        x2 = d.x,
        y2 = d.y + d.height / 2,
        mid = (x1 + x2) / 2;
      return `<path class="map-link" d="M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}"/>`;
    })
    .join('');
  els.nodes.innerHTML = '';
  rows.forEach((d) => {
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    g.setAttribute('class', `map-node depth-${d.depth}`);
    g.setAttribute('transform', `translate(${d.x},${d.y})`);
    g.setAttribute('role', 'button');
    g.setAttribute('tabindex', '0');
    g.setAttribute('aria-label', `查看节点：${d.node.label || '未命名'}`);
    const labelY = 20;
    const noteY = labelY + d.labelLines.length * 18 + 7;
    g.innerHTML = `<rect width="${d.width}" height="${d.height}" rx="12"/><text class="node-label">${d.labelLines.map((line, index) => `<tspan x="16" y="${labelY + index * 18}">${escapeHtml(line)}</tspan>`).join('')}</text>${d.noteLines.length ? `<text class="node-note">${d.noteLines.map((line, index) => `<tspan x="16" y="${noteY + index * 16}">${escapeHtml(line)}</tspan>`).join('')}</text>` : ''}${(d.node.children || []).length ? `<g class="collapse-control" transform="translate(${d.width} ${d.height / 2})"><circle r="10"/><text text-anchor="middle" y="4">${d.node._collapsed ? '+' : '−'}</text></g>` : ''}`;
    g.addEventListener('click', (e) => {
      e.stopPropagation();
      showDetail(d.node);
    });
    g.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        showDetail(d.node);
      }
    });
    const control = g.querySelector('.collapse-control');
    if (control) {
      control.setAttribute('role', 'button');
      control.setAttribute('tabindex', '0');
      control.setAttribute('aria-label', d.node._collapsed ? '展开分支' : '折叠分支');
      const toggle = (e) => {
        e.stopPropagation();
        d.node._collapsed = !d.node._collapsed;
        renderMap();
      };
      control.addEventListener('click', toggle);
      control.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          toggle(e);
        }
      });
    }
    els.nodes.appendChild(g);
  });
  applyTransform();
}
function showDetail(node) {
  selectedNode = node;
  els.detail.classList.remove('hidden');
  els.detail.querySelector('strong').textContent = node.label;
  els.detail.querySelector('p').textContent = node.note || '此节点没有补充说明。';
  const evidenceById = new Map((result?.research?.evidence || []).map((item) => [item.id, item])),
    sourceById = new Map((result?.research?.sources || []).map((item) => [item.id, item]));
  const citations = (node.citations || []).map((id) => evidenceById.get(id)).filter(Boolean);
  const candidates = (node.candidate_citations || []).map((id) => evidenceById.get(id)).filter(Boolean);
  const evidenceMarkup = (items) =>
    items
      .map((item) => {
        const source = sourceById.get(item.page_id) || {};
        return `<blockquote>${escapeHtml(item.quote)}<a href="${escapeHtml(source.url || '#')}" target="_blank" rel="noopener">${escapeHtml(source.title || '查看来源')}</a></blockquote>`;
      })
      .join('');
  els.detail.querySelector('.node-citations').innerHTML =
    `${typeof reportSupport === 'function' ? reportSupport(node.support_status) : ''}${citations.length ? `<h4>原文引用</h4>${evidenceMarkup(citations)}` : ''}${candidates.length ? `<h4>候选资料（尚未确认支持）</h4>${evidenceMarkup(candidates)}` : ''}`;
  const editor = els.detail.querySelector('.node-editor');
  editor.classList.toggle('hidden', !editing);
  editor.querySelector('input').value = node.label;
  editor.querySelector('textarea').value = node.note || '';
}
els.detail.querySelector('.detail-close').addEventListener('click', () => els.detail.classList.add('hidden'));
els.detail.querySelector('.node-save').addEventListener('click', () => {
  if (!selectedNode || !editing) return;
  const editor = els.detail.querySelector('.node-editor');
  selectedNode.label = editor.querySelector('input').value || selectedNode.label;
  selectedNode.note = editor.querySelector('textarea').value;
  renderMap();
  showDetail(selectedNode);
  refreshBasicOutline();
});
function applyTransform() {
  els.viewport.setAttribute(
    'transform',
    `translate(${transform.x} ${transform.y}) scale(${transform.scale})`,
  );
}
function zoom(factor, cx = els.svg.clientWidth / 2, cy = els.svg.clientHeight / 2) {
  const old = transform.scale,
    next = Math.max(0.25, Math.min(2.5, old * factor));
  transform.x = cx - ((cx - transform.x) * next) / old;
  transform.y = cy - ((cy - transform.y) * next) / old;
  transform.scale = next;
  applyTransform();
}
els.svg.addEventListener(
  'wheel',
  (e) => {
    e.preventDefault();
    const box = els.svg.getBoundingClientRect();
    zoom(e.deltaY < 0 ? 1.12 : 0.89, e.clientX - box.left, e.clientY - box.top);
  },
  { passive: false },
);
els.svg.addEventListener('pointerdown', (e) => {
  if (e.target.closest && e.target.closest('.map-node')) return;
  dragging = { x: e.clientX, y: e.clientY, tx: transform.x, ty: transform.y };
  els.svg.setPointerCapture(e.pointerId);
  els.svg.classList.add('dragging');
});
els.svg.addEventListener('pointermove', (e) => {
  if (!dragging) return;
  transform.x = dragging.tx + e.clientX - dragging.x;
  transform.y = dragging.ty + e.clientY - dragging.y;
  applyTransform();
});
els.svg.addEventListener('pointerup', () => {
  dragging = null;
  els.svg.classList.remove('dragging');
});
function fitMap() {
  const box = els.viewport.getBBox();
  if (!box.width || !box.height) return;
  const scale = Math.min(
    1.15,
    (els.svg.clientWidth - 100) / box.width,
    (els.svg.clientHeight - 120) / box.height,
  );
  transform = {
    scale,
    x: 50 - box.x * scale,
    y: (els.svg.clientHeight - box.height * scale) / 2 - box.y * scale,
  };
  applyTransform();
}
$('#zoomIn').addEventListener('click', () => zoom(1.2));
$('#zoomOut').addEventListener('click', () => zoom(0.82));
$('#fitMap').addEventListener('click', fitMap);
let mapResizeFrame = 0;
new ResizeObserver((entries) => {
  if (!entries[0].contentRect.width || !entries[0].contentRect.height) return;
  cancelAnimationFrame(mapResizeFrame);
  mapResizeFrame = requestAnimationFrame(() => {
    if (
      result &&
      !els.workspace.classList.contains('hidden') &&
      !els.workspace.classList.contains('reading-report')
    )
      fitMap();
  });
}).observe(els.svg);

function toMarkdown(node, depth = 0) {
  return `${'#'.repeat(Math.min(depth + 1, 6))} ${node.label}\n${node.note ? `\n${node.note}\n` : ''}\n${(node.children || []).map((child) => toMarkdown(child, depth + 1)).join('')}`;
}
$$('[data-export]').forEach((btn) =>
  btn.addEventListener('click', async () => {
    const type = btn.dataset.export;
    if (type === 'json') {
      const exporting = result;
      btn.disabled = true;
      try {
        const saved = exporting.saved_result_view === 'reader' ? await readSavedResult(exporting) : exporting;
        download(
          `${safeName(exporting.title)}.json`,
          JSON.stringify({ ...saved, title: exporting.title, summary: exporting.summary,
            keywords: exporting.keywords, tree: serializeTree(exporting.tree) }, null, 2),
          'application/json',
        );
      } catch (error) { showError(error.message); }
      finally { btn.disabled = false; }
    }
    if (type === 'markdown')
      download(
        `${safeName(result.title)}.md`,
        result.research && typeof reportToMarkdown === 'function'
          ? reportToMarkdown(result)
          : `# ${result.title}\n\n${result.summary}\n\n${toMarkdown(result.tree, 1)}${typeof webpageReadingMarkdown === 'function' ? webpageReadingMarkdown(result) : ''}`,
        'text/markdown',
      );
    if (type === 'svg') {
      const clone = els.svg.cloneNode(true),
        box = els.viewport.getBBox();
      clone.setAttribute('viewBox', `${box.x - 30} ${box.y - 30} ${box.width + 60} ${box.height + 60}`);
      clone.setAttribute('width', Math.ceil(box.width + 60));
      clone.setAttribute('height', Math.ceil(box.height + 60));
      clone.querySelector('#viewport').removeAttribute('transform');
      const style = document.createElementNS('http://www.w3.org/2000/svg', 'style');
      style.textContent = `.map-link{fill:none;stroke:#aebbb3;stroke-width:1.7}.map-node rect{fill:#fffef9;stroke:#cbd3cd;stroke-width:1.2}.map-node.depth-0 rect{fill:#176b52;stroke:#176b52}.map-node.depth-0 text{fill:#fff}.map-node.depth-0 .node-note{fill:#d7e9e1}.map-node.depth-1 rect{fill:#e3eee7;stroke:#a5c5b5}.map-node text{fill:#1d2723;font-family:"Microsoft YaHei",sans-serif}.map-node .node-label{font-size:13px;font-weight:700}.map-node .node-note{fill:#6d7872;font-size:10.5px}.map-node circle{fill:#d39a42;stroke:#fff;stroke-width:2}`;
      clone.insertBefore(style, clone.firstChild);
      download(
        `${safeName(result.title)}.svg`,
        new XMLSerializer().serializeToString(clone),
        'image/svg+xml',
      );
    }
  }),
);
function download(name, content, type) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([content], { type }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
function safeName(name) {
  return name.replace(/[\\/:*?"<>|]/g, '_').slice(0, 50) || 'mind-map';
}
const textMeasureCanvas = document.createElement('canvas');
const textMeasureContext = textMeasureCanvas.getContext('2d');
function wrapMeasured(value, maxWidth, font) {
  const clean = String(value || '').trim();
  if (!clean) return [];
  textMeasureContext.font = font;
  const lines = [];
  clean.split(/\r?\n/).forEach((paragraph) => {
    if (!paragraph) {
      lines.push('');
      return;
    }
    let current = '';
    for (const character of Array.from(paragraph)) {
      const candidate = current + character;
      if (current && textMeasureContext.measureText(candidate).width > maxWidth) {
        lines.push(current);
        current = character.trimStart();
      } else current = candidate;
    }
    if (current) lines.push(current);
  });
  return lines;
}
function nodeMetrics(node, depth) {
  const width = depth === 0 ? 290 : 270,
    contentWidth = width - 32;
  // The graph is a preview, not storage: the detail/editor retains full text.
  const preview = (lines, count) =>
    lines.length > count ? [...lines.slice(0, count - 1), `${lines[count - 1]}…`] : lines;
  const labelLines = preview(
    wrapMeasured(node.label || '未命名', contentWidth, '700 13px "Microsoft YaHei"'),
    4,
  );
  const noteLines = preview(wrapMeasured(node.note || '', contentWidth, '400 10.5px "Microsoft YaHei"'), 6);
  const labelHeight = Math.max(1, labelLines.length) * 18;
  const noteHeight = noteLines.length ? 7 + noteLines.length * 16 : 0;
  return {
    width,
    height: Math.max(58, 16 + labelHeight + noteHeight + 14),
    labelLines: labelLines.length ? labelLines : ['未命名'],
    noteLines,
  };
}
function serializeTree(node) {
  return {
    id: String(node?.id || ''),
    label: String(node?.label || '未命名'),
    note: String(node?.note || ''),
    citations: Array.isArray(node?.citations) ? node.citations : [],
    children: (node?.children || []).map(serializeTree),
  };
}
function clearUiState(node) {
  if (!node || typeof node !== 'object') return node;
  delete node._collapsed;
  delete node._overviewPrepared;
  (node.children || []).forEach(clearUiState);
  return node;
}
function escapeHtml(value) {
  return String(value).replace(
    /[&<>'"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[c],
  );
}

async function openHistory(kind = historyKind) {
  $('#historyOverlay').classList.remove('hidden');
  showTrashPanel(false);
  await selectHistoryKind(kind);
}
function showTrashPanel(open) {
  $('#historyTrashPanel').classList.toggle('hidden', !open);
  $('#historyList').classList.toggle('hidden', open);
  $('#historyNote').classList.toggle('hidden', open);
  $('.history-tabs').classList.toggle('hidden', open);
  $('#openTrash').classList.toggle('hidden', open);
  $('#historyLoadMore').classList.toggle('hidden', open || !$('#historyLoadMore').dataset.hasMore);
  $('#historyJobSection').classList.toggle('hidden', open || historyKind !== 'analysis' || !historyJobItems.length);
  $('#historyResultHeading').classList.toggle('hidden', open || historyKind !== 'analysis');
}
function historyQueueButton(id) {
  return `<button type="button" class="history-record-queue" data-queue-history="${escapeHtml(id)}" ${isPinned(id) ? 'disabled' : ''}>${isPinned(id) ? '已在队列' : '加入队列'}</button>`;
}
async function loadHistoryJobs(reset = false) {
  if (historyKind !== 'analysis') return;
  const requestVersion = ++historyJobRequestVersion;
  if (reset) {
    historyJobItems = [];
    historyJobOffset = 0;
    $('#historyJobList').innerHTML = '<div class="history-empty">读取任务记录…</div>';
  }
  $('#historyJobLoadMore').disabled = true;
  try {
    const page = await Api.json(`/api/history/jobs?limit=50&offset=${historyJobOffset}`);
    if (requestVersion !== historyJobRequestVersion || historyKind !== 'analysis') return;
    const existing = new Set(historyJobItems.map((item) => item.id));
    const added = page.filter((item) => !existing.has(item.id));
    historyJobItems.push(...added);
    historyJobOffset += page.length;
    $('#historyJobSummary').textContent = `尚未形成结果的任务 · ${historyJobItems.length}${page.length === 50 ? '+' : ''}`;
    $('#historyJobSection').classList.toggle('hidden', !historyJobItems.length);
    $('#historyJobLoadMore').dataset.hasMore = page.length === 50 && added.length ? 'yes' : '';
    $('#historyJobLoadMore').classList.toggle('hidden', !$('#historyJobLoadMore').dataset.hasMore);
    $('#historyJobLoadMore').disabled = false;
    $('#historyJobList').innerHTML = historyJobItems.map((item) =>
      `<div class="history-record"><button type="button" class="history-item" data-history-job="${escapeHtml(item.id)}"><strong>${escapeHtml(item.stage || `任务 ${item.id.slice(0, 8)}`)}</strong><p>${escapeHtml(item.kind || '分析')} · ${escapeHtml(item.model_name || '模型未启动')} · ${escapeHtml(item.status)}</p><time>${escapeHtml(item.updated_at || '')}</time></button><button type="button" class="history-record-queue" data-queue-job="${escapeHtml(item.id)}" ${isPinned(item.id) ? 'disabled' : ''}>${isPinned(item.id) ? '已在队列' : '加入队列'}</button></div>`,
    ).join('');
    $$('#historyJobList [data-history-job]').forEach((button) =>
      button.addEventListener('click', () => openJobDetail(button.dataset.historyJob)),
    );
    $$('#historyJobList [data-queue-job]').forEach((button) => button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const job = await Api.json(`/api/jobs/${encodeURIComponent(button.dataset.queueJob)}?view=summary`);
        const listed = historyJobItems.find((item) => item.id === job.id);
        pinTask({ ...job, label: listed?.stage || job.stage || `任务 ${job.id.slice(0, 8)}` });
        button.textContent = '已在队列';
      } catch (error) { button.disabled = false; showError(error.message); }
    }));
  } catch (error) {
    if (requestVersion !== historyJobRequestVersion) return;
    $('#historyJobSection').classList.remove('hidden');
    if (reset) $('#historyJobList').innerHTML = `<div class="history-empty">${escapeHtml(error.message)}</div>`;
    else showError(error.message);
    $('#historyJobLoadMore').disabled = false;
  }
}
async function selectHistoryKind(kind, append = false) {
  if (append && kind !== historyKind) return;
  historyKind = kind === 'research' ? 'research' : 'analysis';
  const requestVersion = ++historyRequestVersion;
  if (historyKind === 'research') historyJobRequestVersion++;
  showTrashPanel(false);
  $$('[data-history-kind]').forEach((button) =>
    button.classList.toggle('active', button.dataset.historyKind === historyKind),
  );
  $('#historyNote').textContent =
    historyKind === 'research'
      ? '探索路径、候选、失败记录和报告都保存在本机，可随时继续。'
      : '文本与网页分析结果保存在本机，不会上传到云端。';
  if (!append) {
    historyItems = [];
    historyOffset = 0;
    $('#historyList').innerHTML = '<div class="history-empty">读取历史记录…</div>';
  }
  $('#historyLoadMore').disabled = true;
  try {
    const endpoint = historyKind === 'research' ? '/api/research' : '/api/history?view=analysis';
    const response = await Api.request(`${endpoint}${endpoint.includes('?') ? '&' : '?'}limit=50&offset=${historyOffset}`);
    const page = await response.json();
    if (requestVersion !== historyRequestVersion) return;
    if (!response.ok) throw new Error(page.detail || '读取历史失败');
    const existing = new Set(historyItems.map((item) => item.id));
    const added = page.filter((item) => !existing.has(item.id));
    historyItems.push(...added);
    historyOffset += page.length;
    const items = historyItems;
    $('#historyLoadMore').dataset.hasMore = page.length === 50 && added.length > 0 ? 'yes' : '';
    $('#historyLoadMore').classList.toggle('hidden', !$('#historyLoadMore').dataset.hasMore);
    $('#historyLoadMore').disabled = false;
    if (historyKind === 'research') {
      const statusNames = {
        queued: '等待抓取',
        crawling: '探索中',
        awaiting_review: '旧任务待继续',
        paused: '已暂停',
        completed: '本轮结束',
        completed_with_errors: '结束但有失败',
        cancelled: '已取消',
      };
      const statusClass = (status) =>
        ['cancelled', 'completed_with_errors'].includes(status)
          ? 'failed'
          : ['awaiting_review', 'paused'].includes(status)
            ? 'warning'
            : '';
      $('#historyList').innerHTML = items.length
        ? items
            .map((item) => {
              const state = item.workflow
                ? workflowLabel(item.workflow)
                : item.stop_reason === 'page_budget_reached'
                  ? '页面额度已用完'
                  : statusNames[item.status] || item.status;
              const pending = (item.candidates_pending || 0) + (item.pages_waiting_budget || 0),
                report = item.latest_report_version ? ` · 报告 v${item.latest_report_version}` : '';
              const badgeClass =
                item.stop_reason === 'page_budget_reached' ? 'warning' : statusClass(item.status);
              return `<div class="history-record"><button class="history-item research-history-item" data-research-id="${escapeHtml(item.id)}"><strong>${escapeHtml(item.goal || '未命名研究')}</strong><p>已获取 ${item.pages_fetched || 0} 页 · 失败 ${item.pages_failed || 0} 页 · 发现 ${item.links_found || 0} 条链接${pending ? ` · 待处理 ${pending}` : ''}${report}</p><span class="history-item-meta"><time datetime="${escapeHtml(item.updated_at)}">${new Date(item.updated_at).toLocaleString('zh-CN')}</time><span class="history-item-status ${badgeClass}">${escapeHtml(state)}</span></span></button>${historyQueueButton(item.id)}</div>`;
            })
            .join('')
        : '<div class="history-empty">还没有探索研究项目</div>';
      $$('[data-research-id]').forEach((item) =>
        item.addEventListener('click', () => openResearch(item.dataset.researchId)),
      );
    } else {
      const analysisItems = items.filter((item) => !item.project_id);
      $('#historyList').innerHTML = analysisItems.length
        ? analysisItems
            .map(
              (item) =>
                `<div class="history-record"><button class="history-item" data-id="${escapeHtml(item.id)}"><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.summary || '暂无摘要')}</p><time datetime="${escapeHtml(item.updated_at)}">${new Date(item.updated_at).toLocaleString('zh-CN')} · ${item.source_type === 'url' ? '网页' : '文本'}</time></button>${historyQueueButton(item.id)}</div>`,
            )
            .join('')
        : '<div class="history-empty">还没有已完成的分析</div>';
      $$('.history-item').forEach((item) =>
        item.addEventListener('click', () => loadHistory(item.dataset.id)),
      );
    }
    $$('[data-queue-history]').forEach((button) => button.addEventListener('click', () => {
      const id = button.dataset.queueHistory;
      const item = items.find((record) => record.id === id);
      if (!item) return;
      pinTask(historyKind === 'research'
        ? { ...item, kind: 'research', label: item.goal }
        : { id, kind: 'history', label: item.title, status: 'completed' });
      button.textContent = '已在队列';
      button.disabled = true;
    }));
    if (historyKind === 'analysis' && !append) await loadHistoryJobs(true);
  } catch (error) {
    if (requestVersion !== historyRequestVersion) return;
    if (!append) $('#historyList').innerHTML = `<div class="history-empty">${escapeHtml(error.message)}</div>`;
    else showError(error.message);
    $('#historyLoadMore').disabled = false;
  }
}
async function openTrashPanel() {
  showTrashPanel(true);
  $('#historyTrashError').classList.add('hidden');
  $('#historyTrashList').innerHTML = '<div class="history-empty">读取回收站…</div>';
  try {
    const entries = await Api.json('/api/trash');
    $('#historyTrashList').innerHTML = entries.length ? entries.map((item) =>
      `<div class="history-record trash-record"><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.kind === 'research' ? '研究项目' : item.kind === 'analysis' ? '历史分析' : '任务')} · ID ${escapeHtml(item.id.slice(0, 8))} · ${escapeHtml(item.trashed_at)}</small><div><button type="button" data-trash-restore="${escapeHtml(item.kind)}:${escapeHtml(item.id)}">恢复</button><button type="button" class="danger" data-trash-delete="${escapeHtml(item.kind)}:${escapeHtml(item.id)}">永久删除</button></div></div>`
    ).join('') : '<div class="history-empty">回收站为空</div>';
    $$('[data-trash-restore]').forEach((button) => button.addEventListener('click', async () => {
      const [kind, id] = button.dataset.trashRestore.split(':');
      button.disabled = true;
      try {
        const response = await Api.request(`/api/trash/${kind}/${id}/restore`, { method: 'POST' });
        const data = await readApiJson(response);
        if (!response.ok) throw Error(data.detail || '恢复失败');
        await openTrashPanel();
      } catch (error) {
        $('#historyTrashError').textContent = error.message;
        $('#historyTrashError').classList.remove('hidden');
        button.disabled = false;
      }
    }));
    $$('[data-trash-delete]').forEach((button) => button.addEventListener('click', async () => {
      const [kind, id] = button.dataset.trashDelete.split(':');
      const label = button.closest('.trash-record')?.querySelector('strong')?.textContent || id;
      if (!await confirmRecordAction('永久删除？', `“${label}”及其在本项目数据库中的原文、报告和检查点将不可恢复。`, true)) return;
      button.disabled = true;
      try {
        const response = await Api.request(`/api/trash/${kind}/${id}`, { method: 'DELETE' });
        const data = await readApiJson(response);
        if (!response.ok) throw Error(data.detail || '永久删除失败');
        await openTrashPanel();
      } catch (error) {
        $('#historyTrashError').textContent = error.message;
        $('#historyTrashError').classList.remove('hidden');
        button.disabled = false;
      }
    }));
  } catch (error) {
    $('#historyTrashList').innerHTML = `<div class="history-empty">${escapeHtml(error.message)}</div>`;
  }
}
async function loadHistory(id) {
  const response = await Api.request(`/api/history/${id}?view=reader`);
  const data = await response.json();
  if (!response.ok) return showError(data.detail || '读取历史失败');
  result = data;
  clearUiState(result.tree);
  currentHistoryId = id;
  $('#historyOverlay').classList.add('hidden');
  els.composer.classList.add('hidden');
  els.loading.classList.add('hidden');
  showResult();
}
$('#historyBtn').addEventListener('click', () => openHistory());
$('#historyLoadMore').addEventListener('click', () => selectHistoryKind(historyKind, true));
$('#historyJobLoadMore').addEventListener('click', () => loadHistoryJobs());
$('#openTrash').addEventListener('click', openTrashPanel);
$('#closeTrash').addEventListener('click', async () => { showTrashPanel(false); await selectHistoryKind(historyKind); });
$('#closeHistory').addEventListener('click', () => $('#historyOverlay').classList.add('hidden'));
$$('[data-history-kind]').forEach((button) =>
  button.addEventListener('click', () => selectHistoryKind(button.dataset.historyKind)),
);
$('#historyOverlay').addEventListener('click', (event) => {
  if (event.target === $('#historyOverlay')) $('#historyOverlay').classList.add('hidden');
});
health();

// OPS opens the existing reader/editor, preserving its selection and questions.
window.addEventListener('load', () => {
  const opsEntry = new URLSearchParams(location.search);
  if (opsEntry.get('research'))
    openResearch(opsEntry.get('research')).catch((error) => showError(error.message));
  else if (opsEntry.get('history'))
    loadHistory(opsEntry.get('history')).catch((error) => showError(error.message));
});

function candidateAssessment(value) {
  return (
    {
      cross_site_corroborated: 'cross_site_candidates',
      multi_page_supported: 'multi_page_candidates',
      near_duplicate_support: 'near_duplicate_material',
    }[value] || value
  );
}

function renderNumericReview(validation) {
  if (!validation) return '';
  if (validation.schema_version !== 2) {
    const changes = validation.corrections || [];
    return changes.length
      ? `<p>历史版本的自动替换记录（旧规则，并非人工确认）：</p>${changes.map((item) => `<div class="evidence-card"><strong>旧规则曾改写此处</strong><del>${escapeHtml(item.original)}</del><span>${escapeHtml(item.replacement)}</span></div>`).join('')}`
      : '<p>此历史版本只做过数字字面检查，不代表原文支持或事实正确。</p>';
  }
  const issues = validation.issues || [];
  if (!issues.length)
    return `<p>${validation.status === 'not_applicable' ? '没有需要比对的显著数字。' : '数字字面检查未发现差异。'} 这不确认主体、语境或结论正确。</p>`;
  return `<p>以下表达需结合原文核对，程序没有改写正文；数字或单位写法不同本身不等于事实错误。</p>${issues.map((item) => `<details class="report-evidence pending"><summary>${escapeHtml((item.unmatched_numeric_tokens || []).join('、'))} · ${escapeHtml(item.original)}</summary><p>${escapeHtml(item.explanation)}</p>${(item.source_value_candidates || []).length ? `<details><summary>含相同数值的原文候选（不代表支持）</summary>${item.source_value_candidates.map((quote) => `<blockquote><p>${escapeHtml(quote)}</p></blockquote>`).join('')}</details>` : '<p>没有找到含相同数值的原文候选。</p>'}</details>`).join('')}`;
}
