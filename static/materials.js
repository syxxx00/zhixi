/* Read only the selected report's frozen snapshots; no network or HTML rendering. */
(() => {
  const names = {
    text_input: '原始粘贴文本',
    http_decoded_html: '抓取时的 HTML（解码文本）',
    http_decoded_text: '抓取时的文本',
    imported_file_decoded_text: '导入文件的解码文本',
    adapter_or_extracted_text_only: '适配器或抽取文本（并非原始响应）',
    extracted_text: '抽取后的文本',
    cleaned_text: '清洗后的文本',
    model_input: '实际模型输入',
    rendered_html: '动态渲染 DOM',
  };
  const reasonNames = {
    publication_metadata_line: '移出正文的发布时间/来源栏',
    stray_backspace_control: '移除退格控制符',
  };
  const dialog = document.createElement('dialog');
  dialog.id = 'materialInspector';
  dialog.className = 'material-inspector';
  dialog.setAttribute('aria-labelledby', 'materialInspectorTitle');
  dialog.innerHTML = `<header><div><h2 id="materialInspectorTitle">原始材料与处理记录</h2><p id="materialVersion"></p></div><button type="button" id="closeMaterials" aria-label="关闭材料查看器">关闭</button></header>
    <p class="material-boundary">这里只显示当前结果保存的材料，不会重新抓取网页。保留、字符核对和模型调用完成，都不代表已经验证内容真实性。</p>
    <p id="materialUnavailable" role="status"></p><div id="materialControls"><label>材料来源<select id="materialSource"></select></label><label>处理阶段<select id="materialStage"></select></label><button type="button" id="downloadMaterial">下载当前文本</button><button type="button" id="downloadOriginalFile">下载原文件字节</button></div>
    <p id="materialCounts"></p><details id="materialIdentity"><summary>校验信息</summary><code id="materialHash"></code></details><div class="material-body"><section><h3>保存的完整文本</h3><pre id="materialText" tabindex="0"></pre></section><aside><h3>哪些内容发生了变化</h3><p id="materialAccounting"></p><div id="materialChanges"></div><h3>处理批次</h3><p id="materialBatches"></p></aside></div>`;
  document.body.append(dialog);
  let entries = [];
  let selectionRevision = 0;
  const get = (id) => dialog.querySelector('#' + id);
  const save = (name, data, type = 'text/plain;charset=utf-8') => {
    const a = document.createElement('a'),
      url = URL.createObjectURL(new Blob([data], { type }));
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const stages = (entry) => [
    ...(entry?.material?.stages || []),
    ...(entry?.material?.rendered_dom ? [entry.material.rendered_dom] : []),
  ];
  const selectedStage = () =>
    stages(entries[Number(get('materialSource').value)])[Number(get('materialStage').value)];
  function drawStage() {
    const entry = entries[Number(get('materialSource').value)],
      stage = selectedStage();
    if (!stage) return;
    get('materialText').textContent = stage.text;
    get('materialCounts').textContent = `保存的完整文本：${Number(stage.characters).toLocaleString()} 字符`;
    get('materialHash').textContent = `SHA-256（UTF-8 文本）：${stage.sha256_utf8 || '历史版本未记录'}`;
    get('downloadOriginalFile').hidden = !entry.material?.file?.data_base64;
  }
  async function drawSource() {
    const revision = ++selectionRevision;
    const entry = entries[Number(get('materialSource').value)];
    if (!entry) return;
    get('materialStage').replaceChildren();
    get('materialText').textContent = '';
    get('materialCounts').textContent = '';
    get('materialChanges').replaceChildren();
    get('materialAccounting').textContent = '';
    get('materialBatches').textContent = '';
    get('materialHash').textContent = '';
    get('downloadMaterial').disabled = true;
    get('downloadOriginalFile').hidden = true;
    if (!entry.material) {
      get('materialUnavailable').textContent = '正在读取本版本的所选材料…';
      try {
        const saved = await readSavedResult(entry.report, 'material', entry.sourceId);
        if (revision !== selectionRevision || !dialog.open) return;
        entry.material = saved.material;
        entry.manifest = saved.manifest;
      } catch (error) {
        if (revision === selectionRevision && dialog.open)
          get('materialUnavailable').textContent = `材料读取失败：${error.message}；重新选择材料可重试。`;
        return;
      }
    }
    if (!entry.material?.stages?.length) {
      get('materialUnavailable').textContent = '此版本没有保存这份材料的原始快照。';
      return;
    }
    get('materialUnavailable').textContent = '';
    get('downloadMaterial').disabled = false;
    get('materialStage').replaceChildren(
      ...stages(entry).map((stage, index) => new Option(names[stage.kind] || stage.kind, index)),
    );
    const records = entry.material.transforms || [],
      unknown = records.filter((row) => row.changed && row.span_mapping !== 'verified_character_accounting');
    get('materialAccounting').textContent = unknown.length
      ? '部分步骤只保存了前后快照，尚无逐字符映射；不能据此断言抽取完整。'
      : '已有变换记录可定位到保存的原文；这只验证字符去向，不验证删改是否符合研究目的。';
    const container = get('materialChanges');
    container.replaceChildren();
    for (const record of records) {
      const before = entry.material.stages[record.from_stage];
      const originalPoints = Array.from(before.text);
      for (const span of record.transform?.spans || []) {
        if (span.action === 'keep') continue;
        const detail = document.createElement('details'),
          summary = document.createElement('summary'),
          pre = document.createElement('pre');
        summary.textContent = `${reasonNames[span.reason] || span.reason} · 原字符 ${span.source_start}–${span.source_end}`;
        pre.textContent = originalPoints.slice(span.source_start, span.source_end).join('');
        detail.append(summary, pre);
        container.append(detail);
      }
    }
    if (!container.childElementCount)
      container.textContent = '已记录的步骤没有移出正文或替换的字符；未映射步骤请对照前后快照。';
    const manifest = entry.manifest;
    get('materialBatches').textContent = manifest
      ? `${manifest.chunks?.length || 0} 个处理批次；${manifest.partition === 'exact_character_partition' ? '原字符连续分批' : '按结构记录重打包'}。${manifest.semantic_coverage === 'not_verified' ? '模型是否完整理解：尚未验证。' : ''}`
      : '这个结果没有保存模型批次清单，不能补称已经全文处理。';
    drawStage();
  }
  window.openMaterialInspector = (data) => {
    ++selectionRevision;
    entries = [];
    if (data.source?.material?.stages?.length || data.source?.has_saved_material)
      entries.push({
        title: data.source.title || data.title || '输入材料',
        material: data.source.material,
        manifest: data.processing_manifest,
        report: data,
        sourceId: null,
      });
    for (const source of data.research?.sources || []) {
      const material = source.extraction?.source_material;
      if (material?.stages?.length || source.has_saved_material)
        entries.push({ title: source.title || source.url || '研究来源', material,
          report: data, sourceId: source.id });
    }
    get('materialVersion').textContent = data.report_version
      ? `当前打开的报告 V${data.report_version} · 保存时的来源快照`
      : '当前分析保存的来源快照';
    get('materialUnavailable').textContent = entries.length
      ? ''
      : '此历史版本没有保存原始材料快照。不会用当前网站或报告正文冒充当时原文。';
    get('materialControls').hidden = !entries.length;
    dialog.querySelector('.material-body').hidden = !entries.length;
    get('materialIdentity').hidden = !entries.length;
    get('materialIdentity').open = false;
    get('materialCounts').textContent = '';
    get('materialSource').replaceChildren(...entries.map((entry, index) => new Option(entry.title, index)));
    dialog.showModal();
    if (entries.length) drawSource();
    get('closeMaterials').focus();
  };
  get('materialSource').addEventListener('change', drawSource);
  get('materialStage').addEventListener('change', drawStage);
  dialog.addEventListener('close', () => { ++selectionRevision; });
  get('closeMaterials').addEventListener('click', () => dialog.close());
  get('downloadMaterial').addEventListener('click', () => {
    const stage = selectedStage();
    save('保存的材料-' + stage.kind + '.txt', stage.text);
  });
  get('downloadOriginalFile').addEventListener('click', () => {
    const file = entries[Number(get('materialSource').value)].material.file;
    save(
      file.name,
      Uint8Array.from(atob(file.data_base64), (char) => char.charCodeAt(0)),
      'application/octet-stream',
    );
  });
  document
    .getElementById('viewMaterials')
    .addEventListener('click', () => window.openMaterialInspector(result));
})();
