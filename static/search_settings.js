/* Search credentials never enter browser storage, model requests or task payloads. */
(() => {
  const el = (id) => document.getElementById(id);
  let saved = null;
  const controls = ['searchProvider', 'searchApiKey', 'saveSearchSettings', 'clearSearchKey', 'testSearchSettings'];
  const status = (message, error = false) => {
    el('searchSettingsStatus').textContent = message;
    el('searchSettingsStatus').classList.toggle('field-error', error);
  };
  function render() {
    const provider = el('searchProvider').value;
    const row = saved?.providers.find((item) => item.id === provider);
    el('searchKeyField').classList.toggle('hidden', provider === 'hybrid');
    el('clearSearchKey').disabled = !row?.has_key || row.key_source !== 'server_memory';
    el('testSearchSettings').disabled = provider === 'hybrid';
    el('searchKeyStatus').textContent = provider === 'hybrid'
      ? '匿名搜索无需密钥，但可能遇到验证码或限流。'
      : row?.has_key
        ? `密钥已配置（${row.key_source === 'environment' ? '启动环境变量' : '后台内存'}）；尚不代表接口已实测可用。`
        : '尚未配置密钥。保存不发送请求，单次检索测试才会调用 API。';
    const selected = saved?.providers.find((item) => item.id === saved.default_provider);
    if (selected) el('researchSearchService').textContent =
      `默认搜索：${selected.name}${selected.requires_key && !selected.has_key ? ' · 缺少密钥，请先在设置中填写' : ''}。与分析模型独立。`;
  }
  async function action(work) {
    controls.forEach((id) => { el(id).disabled = true; });
    try { await work(); } catch (error) { status(error.message, true); }
    finally { controls.forEach((id) => { el(id).disabled = false; }); render(); }
  }
  async function request(path, options) {
    const response = await Api.request(path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '搜索设置请求失败');
    return data;
  }
  async function load() {
    await action(async () => {
      saved = await request('/api/search/settings');
      el('searchProvider').value = saved.default_provider;
      status('');
    });
  }
  function tab(search) {
    el('modelSettingsPanel').classList.toggle('hidden', search);
    el('searchSettingsPanel').classList.toggle('hidden', !search);
    el('modelSettingsTab').setAttribute('aria-pressed', String(!search));
    el('searchSettingsTab').setAttribute('aria-pressed', String(search));
  }
  el('modelSettingsTab').addEventListener('click', () => tab(false));
  el('searchSettingsTab').addEventListener('click', () => tab(true));
  el('settingsButton').addEventListener('click', () => { tab(false); load(); });
  el('searchProvider').addEventListener('change', () => {
    el('searchApiKey').value = '';
    el('searchTestResults').replaceChildren();
    status('');
    render();
  });
  async function save(clear = false) {
    await action(async () => {
      const body = { provider: el('searchProvider').value, clear_key: clear };
      if (!clear && el('searchApiKey').value.trim()) body.api_key = el('searchApiKey').value.trim();
      saved = await request('/api/search/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      el('searchApiKey').value = '';
      el('searchTestResults').replaceChildren();
      status(clear ? '内存密钥已清除；若配置了环境变量，仍会使用环境变量。' : '已保存；未发出搜索请求或启动研究。');
    });
  }
  el('saveSearchSettings').addEventListener('click', () => save());
  el('clearSearchKey').addEventListener('click', () => save(true));
  el('testSearchSettings').addEventListener('click', () => {
    if (!saved || saved.default_provider !== el('searchProvider').value || el('searchApiKey').value.trim()) {
      status('请先保存当前服务和密钥，再测试。', true);
      return;
    }
    const query = el('searchTestQuery').value.trim();
    if (!query) { status('请填写公开检索词。', true); return; }
    action(async () => {
      el('searchTestResults').replaceChildren();
      status('正在执行一次真实检索…');
      const data = await request('/api/search/test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query }), timeoutMs: 60000,
      });
      status(`已返回 ${data.results.length} 个候选；请核对是否对应查询，尚未读取原文。`);
      for (const row of data.results) {
        const item = document.createElement('article');
        const link = document.createElement('a');
        let url;
        try { url = new URL(row.url); } catch { continue; }
        if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) continue;
        link.href = url.href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = row.title;
        const snippet = document.createElement('p');
        snippet.textContent = row.description;
        item.append(link, snippet);
        el('searchTestResults').append(item);
      }
    });
  });
  el('closeModel').addEventListener('click', () => { el('searchApiKey').value = ''; });
  el('modelOverlay').addEventListener('click', (event) => {
    if (event.target === el('modelOverlay')) el('searchApiKey').value = '';
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') el('searchApiKey').value = '';
  });
  load();
})();
