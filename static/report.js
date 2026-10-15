/* Reading view shares the saved report with the existing map and editor. */
let reportNavigationController;

async function readSavedResult(data, view = 'full', sourceId = null) {
  const project = data.research?.project_id;
  const path = project && data.report_version
    ? `/api/research/${encodeURIComponent(project)}/reports/${Number(data.report_version)}`
    : data.history_id ? `/api/history/${encodeURIComponent(data.history_id)}` : null;
  if (!path) throw Error('此结果没有保存的版本，无法读取补充记录。');
  const params = new URLSearchParams({ view });
  if (sourceId !== null) params.set('source_id', sourceId);
  const response = await Api.request(`${path}?${params}`);
  return response.json();
}

function bindReportNavigation() {
  const controller = (reportNavigationController = new AbortController());
  const options = { signal: controller.signal };
  const narrow = matchMedia('(max-width: 850px)');
  const toc = document.querySelector('.report-toc');
  const toggle = document.querySelector('#readerNavigationToggle');
  const panel = document.querySelector('#readerNavigationPanel');
  const close = (restoreFocus = false) => {
    if (!narrow.matches) return;
    panel.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    toggle.textContent = '目录与工具';
    if (restoreFocus) toggle.focus({ preventScroll: true });
  };
  const adapt = () => {
    // One set of controls and version IDs, even when resizing an open menu.
    const focusInPanel = panel.contains(document.activeElement);
    panel.hidden = narrow.matches;
    toggle.setAttribute('aria-expanded', String(!narrow.matches));
    toggle.textContent = '目录与工具';
    if (narrow.matches && focusInPanel) toggle.focus({ preventScroll: true });
  };
  toggle.addEventListener(
    'click',
    () => {
      if (!panel.hidden) return close();
      panel.hidden = false;
      toggle.setAttribute('aria-expanded', 'true');
      toggle.textContent = '收起目录';
    },
    options,
  );
  document.addEventListener(
    'pointerdown',
    (event) => {
      if (!toc.contains(event.target)) close();
    },
    options,
  );
  document.addEventListener(
    'keydown',
    (event) => {
      if (event.key === 'Escape' && narrow.matches && !panel.hidden) {
        close(true);
        event.preventDefault();
      }
    },
    options,
  );
  toc.addEventListener(
    'focusout',
    (event) => {
      if (event.relatedTarget && !toc.contains(event.relatedTarget)) close();
    },
    options,
  );
  panel.addEventListener(
    'click',
    (event) => {
      const link = event.target.closest('a[href^="#"]');
      if (link && narrow.matches) {
        close();
        const target = document.getElementById(link.hash.slice(1));
        if (target) {
          target.tabIndex = -1;
          target.focus({ preventScroll: true });
        }
      } else if (event.target.closest('button')) close(true);
    },
    { ...options, capture: true },
  );
  narrow.addEventListener('change', adapt, options);
  adapt();
}

function setReportView(view) {
  const reading = view === 'report' && Boolean(result?.research);
  els.workspace.classList.toggle('reading-report', reading);
  document.body.classList.toggle('reader-view', reading);
  $('#reportReader').classList.toggle('hidden', !reading);
  $$('[data-result-view]').forEach((button) =>
    button.setAttribute('aria-pressed', String(button.dataset.resultView === view)),
  );
  if (reading) setTaskDockExpanded(false);
  else
    requestAnimationFrame(() => {
      renderMap();
      fitMap();
    });
}

function reportSupport(status) {
  const labels = { source_excerpt: '原文摘录', needs_review: '', heading: '', coverage_notice: '材料范围' };
  return status && labels[status] ? `<span class="report-support ${status}">${labels[status]}</span>` : '';
}

function reportReadingNote(data) {
  const pageCount = new Set((data.research?.evidence || []).map((item) => item.page_id).filter(Boolean)).size;
  const parts = [pageCount === 1 ? '本篇所用材料来自一个页面，覆盖范围有限。' : '本篇根据本轮取得的材料整理。'];
  if (data.report_quality?.user_edited) parts.push('当前为用户编辑版本。');
  if (data.report_quality?.status === 'evidence_only') parts.push('当前仅为材料摘录。');
  if (data.generation_validation?.status === 'degraded') parts.push('本版包含未完成的草稿，不能视为完整报告。');
  else if (data.report_quality?.status === 'partial') parts.push('本版为部分结果。');
  const check = data.overall_report_check?.status;
  const delivery = data.report_quality?.delivery_status;
  if (check === 'incomplete' || delivery === 'incomplete') parts.push('部分交付检查未完成，具体范围见处理记录。');
  if (delivery === 'unresolved_repairs') parts.push('修订仍有未解决意见，已有正文和原文保留。');
  else if (check === 'issues_found' || delivery === 'issues_found') parts.push('检查仍有具体意见，可在处理记录中查看；意见本身不等于事实错误。');
  if (data.question_coverage?.status === 'incomplete') parts.push('问题覆盖核对未完成。');
  return parts.join(' ');
}

function openCoverageReview(data, supplement = null) {
  const dialog = document.createElement('dialog');
  dialog.className = 'question-dialog';
  dialog.setAttribute('aria-labelledby', 'coverageDialogTitle');
  dialog.innerHTML = `<header><h2 id="coverageDialogTitle">${supplement ? '纳入补查并更新报告' : '重新核对研究问题'} · V${Number(data.report_version || 1)}</h2><button type="button" aria-label="关闭">×</button></header>
    <div class="question-scroll"><p>${supplement ? '将原报告和所选补查版本中的完整原文、来源、上下文一起综合，仍回答原研究问题。不把补查的模型总结当原始证据。生成新版，旧报告和问答不变。' : '只核对此版本已存的原文，不重新抓取或生成正文。完成后另存新版本，原报告和选文问答保留。'}</p>
    <form>${supplement ? `<label>补查：${escapeHtml(supplement.goal)}<select name="supplementVersion">${supplement.reports.map((r) => `<option value="${Number(r.version)}">V${Number(r.version)} · ${escapeHtml(formatReportTime(r.created_at))}</option>`).join('')}</select></label>` : ''}<label>${supplement ? '综合模型' : '核对模型'}<select name="model">${modelRegistry.map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`).join('')}</select></label>
    <p data-coverage-privacy class="report-secondary"></p><button type="submit" class="question-submit">${supplement ? '纳入原文并生成新版' : '加入核对队列'}</button><p role="status"></p></form></div>`;
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  dialog.querySelector('header button').onclick = () => dialog.close();
  const select = dialog.querySelector('[name=model]');
  select.value = modelRegistry.find((m) => m.provider === 'local')?.id || modelRegistry[0]?.id || '';
  const privacy = () => {
    dialog.querySelector('[data-coverage-privacy]').textContent =
      modelRegistry.find((m) => m.id === select.value)?.provider === 'api'
        ? supplement
          ? '将把原研究问题、原报告与所选补查版本的全部原文发送至所选API。'
          : '将把此版本研究问题与全部已存原文发送至所选API。'
        : '研究问题与原文均在本机模型中处理，不发起网页搜索。';
  };
  select.onchange = privacy;
  privacy();
  dialog.querySelector('form').onsubmit = async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('[type=submit]');
    button.disabled = true;
    try {
      const model = modelRegistry.find((m) => m.id === select.value);
      if (!model) throw Error('请先添加模型。');
      const suffix = supplement
        ? `/supplements/${encodeURIComponent(supplement.id)}/reports/${Number(dialog.querySelector('[name=supplementVersion]').value)}/incorporate`
        : '/coverage';
      const response = await Api.request(
        `/api/research/${data.research.project_id}/reports/${data.report_version || 1}${suffix}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(modelRequest(model)),
        },
      );
      const task = await response.json();
      if (!response.ok) throw Error(typeof task.detail === 'string' ? task.detail : '核对提交失败');
      trackedTasks.push({ ...task, label: (supplement ? '纳入补查：' : '重新核对：') + data.title });
      persistTasks();
      renderTaskDock();
      startTaskPolling();
      dialog.close();
    } catch (error) {
      dialog.querySelector('[role=status]').textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
  dialog.showModal();
  select.focus();
}

function renderQuestionCoverage(ledger, sourceLinks) {
  if (!ledger) return '';
  const names = {
    answered: '模型认为可回答',
    partial: '部分回答',
    not_found: '当前材料未找到回答',
    conflicting: '存在分歧',
    incomplete: '核对未完成',
  };
  const inventory = ledger.basis === 'delivery_inventory_not_answer_coverage';
  return `<section id="report-question-coverage" class="report-section"><h3>${inventory ? '研究问题与材料范围' : '原文能回答哪些问题？'}</h3>
    ${ledger.model ? `<p class="report-secondary">核对模型：${escapeHtml(ledger.model)}</p>` : ''}
    <p class="report-secondary">${inventory ? '沿用本轮研究问题，正文已进行原文核对；这里不重复全文扫描，也不自动宣称问题已经充分回答。' : ledger.schema_version >= 2 ? '你的完整问题始终单独核对；模型提出的子问题只作补充，原文片段匹配不代表拆解正确。' : '以下问题由模型从研究目标拆解，再对已存原文逐批核对。问题拆解可能遗漏。'}回答判断不是事实保证；未搜索全网。</p>
    ${ledger.planning?.status === 'unavailable' ? '<p>模型拆解不可用，已保留并直接核对你的完整问题。</p>' : ''}
    ${ledger.status === 'incomplete' ? '<p>部分核对未完成，不能据此判断研究问题已经充分回答或资料确实缺失。</p>' : ''}
    ${ledger.error ? `<p>${escapeHtml(ledger.error)}</p>` : ''}
    ${(ledger.questions || [])
      .map(
        (
          q,
        ) => `<details class="report-evidence ${q.status === 'answered' ? '' : 'pending'}" ${q.origin === 'user_goal' && q.answer ? 'open' : ''}><summary>${q.origin === 'user_goal' ? '你的完整问题 · ' : q.origin === 'model_proposal' ? '模型建议子问题 · ' : ''}${escapeHtml(q.question)} · ${escapeHtml(names[q.status] || '未核对')}</summary>
      ${q.origin === 'model_proposal' ? `<p class="report-secondary">对应你的原话：${escapeHtml(q.goal_quote)}（仅标明出处，未确认拆解语义）</p>` : ''}
      <p class="report-secondary">已完成有效核对 ${Number(q.checked_batches || 0)}/${Number(q.total_batches || 0)} 批</p>
      ${q.error ? `<p>${escapeHtml(q.error)}</p>` : ''}${q.answer ? `<div class="report-prose" data-report-text data-anchor-id="coverage:${escapeHtml(q.id)}"><p>${escapeHtml(q.answer)}</p></div><button type="button" class="report-ask-section" data-ask-anchor="coverage:${escapeHtml(q.id)}">就此回答提问</button>` : ''}
      ${(q.missing || []).map((value) => `<p>仍需了解：${escapeHtml(value)}</p>`).join('')}
      ${(q.excerpts || []).length ? `<details><summary>核对原文和上下文</summary>${sourceLinks([...new Set(q.excerpts.map((e) => e.evidence_id))])}</details>` : ''}
      ${
        q.status !== 'incomplete' && (q.queries || []).length
          ? `<form data-gap-form="${escapeHtml(q.id)}"><label>补充研究的公开查询<textarea name="query" rows="2" maxlength="1000" required>${escapeHtml(q.queries[0])}</textarea></label>
        <label>搜集时限（分钟）<input name="budget" type="number" min="1" max="1440" value="30" required></label>
        <label>报告模型<select name="model">${modelRegistry.map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`).join('')}</select></label>
        <p class="report-secondary">仅发送上方可编辑查询，不附带报告或选文；点击确认后才会联网并新建关联任务。</p><button type="submit" class="report-ask-section">确认查询并补充研究</button><p role="status"></p></form>`
          : ''
      }
      <div data-gap-links="${escapeHtml(q.id)}"></div>
      ${q.status === 'incomplete' ? `<details><summary>查看逐批记录与未完成原因</summary>${(q.batches || []).map((b, i) => `<p>第 ${i + 1} 批：${escapeHtml(b.error || '摘录格式校验通过；只代表本批范围')}</p>${b.decision?.answer ? `<p>${escapeHtml(b.decision.answer)}</p>` : ''}`).join('')}</details>` : ''}
    </details>`,
      )
      .join('')}</section>`;
}

async function bindCoverageResearch(data) {
  if (!data.question_coverage || !data.report_version) return;
  const prefix = `/api/research/${data.research.project_id}/reports/${data.report_version}`;
  const container = document.querySelector('#report-question-coverage');
  async function links() {
    try {
      const response = await Api.request(prefix + '/gap-research');
      if (!response.ok || !container?.isConnected) return;
      const rows = await response.json();
      container.querySelectorAll('[data-gap-links]').forEach((el) => {
        el.replaceChildren();
        rows
          .filter((row) => row.gap_id === el.dataset.gapLinks)
          .forEach((row) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'report-ask-section';
            button.textContent = '查看补充研究：' + row.goal;
            button.addEventListener('click', () => openResearch(row.id));
            el.append(button);
            if (row.reports?.length) {
              const incorporate = document.createElement('button');
              incorporate.type = 'button';
              incorporate.className = 'report-ask-section';
              incorporate.textContent = '纳入补查，更新原报告';
              incorporate.addEventListener('click', () => openCoverageReview(data, row));
              el.append(incorporate);
            } else {
              const note = document.createElement('p');
              note.className = 'report-secondary';
              note.textContent = '补查尚无报告版本；在补充研究中生成报告后，可选取其原文回到本研究。';
              el.append(note);
            }
          });
      });
    } catch {}
  }
  container?.querySelectorAll('[data-gap-form]').forEach((form) =>
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = form.querySelector('button'),
        status = form.querySelector('[role=status]');
      button.disabled = true;
      try {
        const values = new FormData(form),
          model = modelRegistry.find((m) => m.id === values.get('model'));
        if (!model) throw Error('请选择报告模型。');
        const response = await Api.request(
          prefix + `/gaps/${encodeURIComponent(form.dataset.gapForm)}/research`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              query: values.get('query'),
              time_limit_minutes: Number(values.get('budget')),
              mode: 'automatic',
              auto_report: true,
              report_model: modelRequest(model),
            }),
          },
        );
        const task = await response.json();
        if (!response.ok) throw Error(typeof task.detail === 'string' ? task.detail : '补查任务创建失败');
        trackedTasks.push({ ...task, kind: 'research', label: task.goal });
        persistTasks();
        renderTaskDock();
        startTaskPolling();
        status.textContent = '已创建关联补充研究。';
        await links();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    }),
  );
  await links();
}

function reportFieldReferences(data) {
  const output = [];
  const names = { title: '标题', label: '节点名称', date: '日期', event: '事件' };
  const add = (owner, location) => {
    for (const [field, ids] of Object.entries(owner?.field_citations || {})) {
      if (!names[field] || !Array.isArray(ids) || !ids.length) continue;
      output.push({
        location: `${location} · ${names[field]}`,
        text: String(owner[field] || ''),
        ids: [...new Set(ids)],
      });
    }
  };
  add(data, '报告');
  const walk = (node) => {
    if (!node) return;
    add(node, node.label || '章节');
    (node.children || []).forEach(walk);
  };
  walk(data.tree);
  (data.insights || []).forEach((item, index) => add(item, `解读 ${index + 1}`));
  (data.timeline || []).forEach((item, index) => add(item, `时间线 ${index + 1}`));
  return output;
}

function renderReportProcess(data, sourceLinks) {
  const audit = data.research?.semantic_audit;
  const grounding = data.source_grounding;
  const reviewIssues = audit?.issues || [];
  const reviewNames = {
    unsupported_claim: '依据不足',
    overstatement: '表述过强',
    missing_context: '缺少条件',
    conflict_ignored: '遗漏分歧',
    ambiguous_attribution: '主体不清',
    uncited_claim: '尚无引用',
    other: '需要核对',
  };
  const decisionNames = {
    supported: '原文支持',
    contradicted: '与原文相反',
    insufficient: '所给证据不足',
    conflicting: '来源相互冲突',
    invalid: '未完成复核',
  };
  const decisions = audit?.decisions || [];
  const decisionView = (
    item,
  ) => `<details class="report-evidence ${item.verdict === 'supported' ? '' : 'pending'}"><summary>${escapeHtml(decisionNames[item.verdict] || '未完成复核')} · ${escapeHtml(item.claim_text || '')}</summary>
    <p>${escapeHtml(item.reason || '模型没有给出可用判断。')}</p>
    ${item.validation_error ? `<p class="report-secondary">未计入复核：${escapeHtml(item.validation_error)}</p>` : ''}
    ${(item.excerpts || []).length ? `<p class="report-secondary">模型用于判断的原文摘录：</p>${item.excerpts.map((excerpt) => `<blockquote><p>${escapeHtml(excerpt.quote)}</p></blockquote>`).join('')}` : ''}
    ${item.suggested_revision ? `<p>修改建议（尚未采用）：${escapeHtml(item.suggested_revision)}</p>` : ''}
    <details><summary>查看对应材料与上下文</summary>${sourceLinks(item.evidence_ids) || '<p>未提供有效引用。</p>'}</details></details>`;
  const decisionDetails = decisions.length
    ? `${decisions
        .filter((item) => item.verdict !== 'supported')
        .map(decisionView)
        .join('')}
    ${
      decisions.some((item) => item.verdict === 'supported')
        ? `<details class="report-evidence"><summary>模型判定为原文支持 · ${decisions.filter((item) => item.verdict === 'supported').length} 项</summary>${decisions
            .filter((item) => item.verdict === 'supported')
            .map(decisionView)
            .join('')}</details>`
        : ''
    }`
    : '';
  const groundingView = grounding
    ? `<section id="report-source-grounding" class="report-section"><h3>段落核对记录</h3>
    <p class="report-secondary">逐段重读全部已存材料，再从原始记录重写；模型选择和解释仍可能错误。${grounding.schema_version >= 2 ? '原文由程序按模型选取的编号直接取回，并非模型逐字摘录；编号存在不证明支持关系。' : '原文摘录匹配不证明整段真实。'}这不证明研究完成。</p>
    ${grounding.status === 'incomplete' ? '<p>部分段落仍是待核实草稿，未用缺失或无效检查冒充核实结果。</p>' : ''}
    ${(grounding.units || [])
      .map(
        (
          unit,
        ) => `<details class="report-evidence ${unit.status === 'rewritten' ? '' : 'pending'}"><summary>${escapeHtml(unit.heading || unit.path)} · ${unit.status === 'rewritten' ? '已重写' : unit.status === 'insufficient' ? '依据不足' : '重核未完成'}</summary>
      <p class="report-secondary">有效重核 ${Number(unit.checked_batches || 0)}/${Number(unit.total_batches || 0)} 批；这只是已存材料范围。</p>
      ${unit.reading_batches?.length ? `<details><summary>跨批读取与写作 · ${unit.reading_batches.length} 次尝试</summary>${unit.reading_batches.map((read) => `<p>${escapeHtml({ 'grounding:batch_write': '分批撰写', 'grounding:reconcile': '回读原文综合' }[read.phase] || '读取原文')} ${Number(read.index)}/${Number(read.total)}：${escapeHtml(read.error || { written_not_semantically_verified: '已生成，内容仍需核实', requested_more_context: '已请求补读相邻原文' }[read.status] || '尚未完成')}；完整展开 ${read.reading?.selected_evidence_ids?.length || 0} 条原文。</p>`).join('')}</details>` : ''}
      ${unit.error ? `<p>${escapeHtml(unit.error)}</p>` : ''}${(unit.unresolved || []).map((v) => `<p>尚不能确定：${escapeHtml(v)}</p>`).join('')}
      <details><summary>查看修改前的草稿</summary><blockquote><p>${escapeHtml(unit.original_candidate || '')}</p></blockquote></details>
      ${unit.text ? `<details><summary>查看原文重写结果</summary><blockquote><p>${escapeHtml(unit.text)}</p></blockquote></details>` : ''}
      ${unit.attribution?.statements?.length ? `<details><summary>逐条查看表述和出处</summary><p class="report-secondary">观察、观点、推断和建议由模型区分，仍可能分错；引用只对应所列这一条。</p>${unit.attribution.statements.map((statement) => `<div class="report-statement-evidence"><p>${escapeHtml(statement.text)}</p>${sourceLinks((statement.excerpts || []).map((e) => e.evidence_id)) || '<p class="report-secondary">本条未列原文引用，不作为已取得的事实。</p>'}</div>`).join('')}</details>` : ''}
      ${sourceLinks(unit.rewrite_evidence_ids || [])}
      <details><summary>查看逐批尝试与原始输出</summary><pre class="report-grounding-record">${escapeHtml(JSON.stringify(unit, null, 2))}</pre></details>
    </details>`,
      )
      .join('')}</section>`
    : '';
  const auditView = audit
    ? `<section id="report-review" class="report-section"><h3>模型复核意见</h3>
    <p class="report-secondary">${escapeHtml(audit.model || '本地模型')} 已复核 ${Number(audit.audited_claims || 0)}/${Number(audit.claim_count || 0)} 项表述。模型复核是第二次判断，不是正确性保证。</p>
    <p class="report-secondary">这里只检查表述与所选材料是否一致，不证明来源本身真实，也不代表已搜索全部资料。</p>
    ${decisionDetails}
    ${reviewIssues
      .filter((issue) => !decisions.length || issue.kind === 'uncited_claim')
      .map(
        (issue) =>
          `<details class="report-evidence pending"><summary>${escapeHtml(reviewNames[issue.kind] || '需要核对')} · ${escapeHtml(issue.claim_text || '')}</summary><blockquote><p>${escapeHtml(issue.explanation || '')}</p>${issue.suggested_revision ? `<p>修改建议（尚未采用）：${escapeHtml(issue.suggested_revision)}</p>` : ''}</blockquote>${sourceLinks(issue.evidence_ids)}</details>`,
      )
      .join('')}
    ${!reviewIssues.length && !decisions.length ? '<p>本次复核没有提出具体问题；未检查的部分仍不能视为通过。</p>' : ''}
    ${(audit.unreviewed_claim_ids || []).length ? `<p>还有 ${audit.unreviewed_claim_ids.length} 项表述未得到模型判断。</p>` : ''}
    </section>`
    : '';
  const check = data.overall_report_check;
  const delivery = data.delivery_repairs;
  const checkView = check
    ? `<section id="report-delivery-check" class="report-section"><h3>交付检查记录</h3>
    <p>${escapeHtml({ incomplete: '部分检查未完成，不能把缺失判断当作通过。', issues_found: '本轮检查仍有具体意见；模型意见本身不等于事实错误。', no_issues_reported: '本轮检查未提出具体问题，不等于事实已核实。' }[check.status] || '未取得完整检查状态。')}</p>
    ${check.error ? `<p>${escapeHtml(check.error)}</p>` : ''}
    ${check.uncompleted_paragraph_ids?.length ? `<p class="report-secondary">未完成范围：${escapeHtml(check.uncompleted_paragraph_ids.join('、'))}</p>` : ''}
    ${(check.issues || []).map((issue) => `<p>${escapeHtml(issue)}</p>`).join('')}
    ${(check.prior_scope_findings || []).map((item) => `<details class="report-evidence"><summary>首轮其他范围的意见 · ${escapeHtml(item.paragraph_ids.join('、'))}</summary>${item.issues.map((issue) => `<p>${escapeHtml(issue)}</p>`).join('')}</details>`).join('')}
    ${delivery?.status === 'unresolved' ? '<p>针对性修订仍有未解决意见，未要求正文反复改到检查通过。</p>' : ''}
    <details><summary>查看检查与修订记录</summary><pre class="report-grounding-record">${escapeHtml(JSON.stringify({ check, delivery }, null, 2))}</pre></details></section>`
    : '';
  return `${renderQuestionCoverage(data.question_coverage, sourceLinks)}${auditView}${groundingView}${checkView}${data.evidence_validation?.schema_version === 2 && data.evidence_validation.issues?.length ? `<details id="report-numeric-review" class="report-evidence pending"><summary>数字表达待核对 · ${data.evidence_validation.issues.length} 项</summary>${renderNumericReview(data.evidence_validation)}</details>` : ''}`;
}

function renderReportReader(data) {
  reportNavigationController?.abort();
  const oldMedia = document.querySelector('#reportMedia');
  if (oldMedia && window.MediaEvidence) window.MediaEvidence.dispose(oldMedia);
  const research = data.research;
  $('#reportViewTabs').classList.toggle('hidden', !research);
  if (!research) {
    $('#reportReader').replaceChildren();
    setReportView('map');
    return;
  }
  const quality = data.report_quality;
  const evidence = new Map((research.evidence || []).map((item) => [item.id, item]));
  const sources = new Map((research.sources || []).map((item) => [item.id, item]));
  const paragraphs = (value) => (value ? `<p>${escapeHtml(String(value))}</p>` : '');
  const renderSourceQuotes = (ids) =>
    (ids || [])
      .map((id) => evidence.get(id))
      .filter(Boolean)
      .map((item) => {
        const source = sources.get(item.page_id) || {};
        let href = '';
        try {
          const url = new URL(source.url);
          if (['http:', 'https:'].includes(url.protocol)) href = url.href;
        } catch {}
        const context = item.metadata?.context || {},
          alignment = item.metadata?.alignment;
        return `<blockquote><p>${escapeHtml(item.quote || item.claim || '')}</p>
      ${alignment ? `<p class="report-secondary">归属线索：${escapeHtml(alignment.reason)} 字词匹配只是线索，不代表观点已归属于研究对象。</p>` : ''}
      ${context.parent_quote ? `<details><summary>查看父评论</summary><p>${escapeHtml(context.parent_quote)}</p></details>` : ''}
      ${context.before || context.after ? `<details><summary>查看相邻原文</summary><p>${escapeHtml(context.before || '')}</p><p>${escapeHtml(item.quote || '')}</p><p>${escapeHtml(context.after || '')}</p></details>` : ''}
      <footer>${href ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.title || '查看原网页')} ↗</a>` : escapeHtml(source.title || '来源未注明')}</footer></blockquote>`;
      })
      .join('');
  const references = [];
  const sourceLinks = (ids) => {
    const present = (ids || []).filter((id) => evidence.has(id));
    if (!present.length) return '';
    const index = references.push(present) - 1;
    return `<div data-source-refs="${index}"><p class="report-secondary">展开时显示保存的原文。</p></div>`;
  };
  const hydrateQuotes = (root) => {
    root.querySelectorAll('[data-source-refs]').forEach((host) => {
      let parent = host.parentElement;
      while (parent && parent !== $('#reportReader')) {
        if (parent.tagName === 'DETAILS' && !parent.open) return;
        parent = parent.parentElement;
      }
      host.innerHTML = renderSourceQuotes(references[Number(host.dataset.sourceRefs)]);
      delete host.dataset.sourceRefs;
    });
  };
  const toc = [];
  function section(node, path, depth = 0) {
    const anchor = `report-section-${path}`;
    if (depth === 0) toc.push({ anchor, title: node.label });
    const heading = Math.min(3 + depth, 6);
    const citations = sourceLinks(node.citations),
      candidates = sourceLinks(node.candidate_citations);
    return `<section class="report-section" id="${anchor}" data-section-id="${escapeHtml(node.id || path)}">
      <h${heading}>${escapeHtml(node.label || '分析')}${reportSupport(node.support_status)}</h${heading}>
      <div class="report-prose" data-report-text data-anchor-id="${escapeHtml(node.id || path)}">${paragraphs(node.note)}</div>
      ${node.note ? `<button type="button" class="report-ask-section" data-ask-anchor="${escapeHtml(node.id || path)}">就此段提问</button>` : ''}
      ${citations ? `<details class="report-evidence"><summary>来源与原文 · ${(node.citations || []).length} 条引用</summary>${citations}</details>` : ''}
      ${candidates ? `<details class="report-evidence pending"><summary>候选资料 · 尚未确认支持此分析</summary>${candidates}</details>` : ''}
      ${node.reader_details ? `<details class="report-detail-material"><summary>展开详细材料 · ${(node.children || []).length} 段</summary>${(node.children || []).map((child, i) => section(child, `${path}-${i}`, depth + 1)).join('')}</details>` : (node.children || []).map((child, i) => section(child, `${path}-${i}`, depth + 1)).join('')}
    </section>`;
  }
  const sections = (data.tree?.children || []).map((node, i) => section(node, String(i))).join('');
  const fieldReferences = reportFieldReferences(data);
  const fieldReferenceView = fieldReferences.length
    ? `<details class="report-evidence report-field-references"><summary>标题与时间线的修订依据</summary>${fieldReferences.map((item) => `<h4>${escapeHtml(item.location)}</h4><p>${escapeHtml(item.text)}</p>${sourceLinks(item.ids) || '<p>本版本未找到对应原文，请查看修订记录。</p>'}`).join('')}</details>`
    : '';
  const unused = quality?.unrepresented_evidence_ids || [];
  $('#reportReader').innerHTML = `
    <aside class="report-toc" aria-label="报告导航与工具"><div class="report-navigation-bar"><button type="button" id="readerBack">← 新建研究</button><button type="button" id="readerNavigationToggle" aria-expanded="false" aria-controls="readerNavigationPanel">目录与工具</button></div><div id="readerNavigationPanel"><span class="report-toc-label">本篇目录</span>
      <label class="report-version-label" for="readerVersion">报告版本<select id="readerVersion"><option value="${Number(data.report_version || 1)}">V${Number(data.report_version || 1)}</option></select></label>
      <nav aria-label="报告章节"><a href="#report-overview">概览</a>${toc.map((item) => `<a href="#${item.anchor}">${escapeHtml(item.title)}</a>`).join('')}<a href="#report-limitations">资料范围</a><a href="#report-source-list">来源资料</a><a href="#reportMedia">媒体资料</a></nav>
      <button type="button" id="readerExport">导出 Markdown ↓</button>
      <button type="button" id="readerMaterials">原始材料与处理记录</button>
      <button type="button" id="readerQuestions">问答记录</button>
      <button type="button" id="readerCoverage">重新核对问题</button>
    </div></aside>
    <article class="report-document">
      <header id="report-overview" class="report-heading"><div class="eyebrow">RESEARCH REPORT${data.report_version ? ` · V${Number(data.report_version)}` : ''}</div>
        <h2>${escapeHtml(data.title)}</h2><p class="report-byline">${new Set([...evidence.values()].map((item) => item.page_id).filter(Boolean)).size} 个材料来源 · ${evidence.size} 条材料 · ${escapeHtml(data.runtime?.model || '模型未注明')}</p>
      </header>
      <div class="report-reading-note" aria-label="阅读说明">${escapeHtml(reportReadingNote(data))}</div>
      <section class="report-overview"><h3>报告概览 ${reportSupport(quality?.summary_status)}</h3><div class="report-prose" data-report-text data-anchor-id="summary">${paragraphs(data.summary)}</div><button type="button" class="report-ask-section" data-ask-anchor="summary">就概览提问</button></section>
      ${research.revision ? `<details id="report-revision-inputs" class="report-evidence"><summary>本版纳入了补查材料</summary><p>仍按原研究问题综合保存的原文；没有把补查总结当作证据，也未重新抓取网页。</p>${research.revision.input_snapshots.map((r, i) => `<p>${i ? '补查' : '原报告'}：${escapeHtml(r.title || '研究报告')} · V${Number(r.report_version)} · ${Number(r.evidence_count)} 条材料，新增纳入 ${Number(r.added_evidence_count)} 条</p><button type="button" class="report-ask-section" data-input-project="${escapeHtml(r.project_id)}" data-input-version="${Number(r.report_version)}">查看这个报告版本</button>`).join('')}</details>` : ''}
      ${sections || '<p class="report-empty">当前还没有可供阅读的报告章节。</p>'}
      ${(data.insights || []).length ? `<section class="report-section"><h3>分析与解释</h3>${data.insights.map((item) => `<h4>${escapeHtml(item.title)} ${reportSupport(item.support_status)}</h4><div class="report-prose">${paragraphs(item.detail)}</div>`).join('')}</section>` : ''}
      ${fieldReferenceView}
      <section id="report-limitations" class="report-section"><h3>资料范围</h3>
        <p class="report-secondary">由模型根据本轮材料整理；观点归属和具体条件以正文及引用为准。</p>
        <details class="report-evidence"><summary>查看完整范围说明</summary>${(research.limitations || []).map((item) => `<p>${escapeHtml(item)}</p>`).join('') || '<p>当前报告尚未记录详细的覆盖范围。</p>'}</details>
        ${unused.length ? `<details class="report-evidence"><summary>${unused.length} 条材料尚未进入报告正文</summary>${sourceLinks(unused)}</details>` : ''}
      </section>
      <section id="report-source-list" class="report-section"><h3>来源资料</h3><p class="report-secondary">展开材料可核对原文与网页出处。</p>${[...sources.values()].map((source) => `<details class="report-evidence"><summary>${escapeHtml(source.title || '未命名来源')}</summary>${sourceLinks([...evidence.values()].filter((item) => item.page_id === source.id).map((item) => item.id)) || '<p>该来源暂无纳入报告的证据。</p>'}</details>`).join('')}</section>
      <section id="reportMedia" class="report-section" aria-label="报告媒体证据"></section>
      <details id="report-process-records" class="report-evidence report-process-records"><summary>处理与核对记录</summary>
        <p class="report-secondary">保留生成过程供排查，不属于报告正文。</p><button type="button" id="readerExportProcess">导出报告及处理记录</button>
<div data-report-records>${data.saved_result_view === 'reader' ? '<p>展开时读取本版本的处理记录。</p>' : renderReportProcess(data, sourceLinks)}</div>
      </details>
    </article>`;
  bindReportNavigation();
  const reader = $('#reportReader');
  let processData = null;
  let loadingProcess = null;
  const readProcess = async () => {
    if (!processData) {
      loadingProcess ||= readSavedResult(data, 'process').catch((error) => {
        loadingProcess = null;
        throw error;
      });
      processData = await loadingProcess;
    }
    return {
      ...data, ...processData,
      research: { ...data.research, ...processData.research },
    };
  };
  reader.addEventListener('toggle', async (event) => {
    const detail = event.target;
    if (detail.tagName !== 'DETAILS' || !detail.open) return;
    if (detail.id === 'report-process-records' && data.saved_result_view === 'reader') {
      const records = detail.querySelector('[data-report-records]');
      if (!records.dataset.loaded && !records.dataset.loading) {
        records.dataset.loading = 'true';
        records.textContent = '正在读取本版本的处理记录…';
        try {
          const loaded = await readProcess();
          if (!detail.isConnected || reportNavigationController.signal.aborted) return;
          records.innerHTML = renderReportProcess(loaded, sourceLinks);
          records.dataset.loaded = 'true';
          if (typeof bindReportQuestions === 'function') bindReportQuestions(loaded, records);
          bindCoverageResearch(loaded);
        } catch (error) {
          if (detail.isConnected) records.textContent = `读取记录失败：${error.message}；收起后重新展开可重试。`;
        } finally {
          delete records.dataset.loading;
        }
      }
    }
    if (detail.isConnected) hydrateQuotes(detail);
  }, { capture: true, signal: reportNavigationController.signal });
  hydrateQuotes(reader);
  if (window.MediaEvidence) window.MediaEvidence.render($('#reportMedia'), data);
  if (data.research?.media_snapshot?.unverified_anchor_count) {
    const warning = document.createElement('p');
    warning.className = 'report-notice';
    warning.textContent = `${data.research.media_snapshot.unverified_anchor_count} 项旧媒体的对应段落已变化，现仅保留为原资料，不再作为新段落的依据。`;
    $('#reportMedia').before(warning);
  }
  $('#readerBack').addEventListener('click', () => $('#backBtn').click());
  $('#readerExport').addEventListener('click', () => $('[data-export="markdown"]').click());
  $('#readerExportProcess').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const exported = data.saved_result_view === 'reader' ? await readProcess() : data;
      const url = URL.createObjectURL(new Blob([reportToMarkdown(exported, { includeProcess: true })], { type: 'text/markdown;charset=utf-8' }));
      const link = document.createElement('a');
      link.href = url; link.download = '报告及处理记录.md'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { showError(error.message); }
    finally { button.disabled = false; }
  });
  $('#readerMaterials').addEventListener('click', () => openMaterialInspector(data));
  $('#readerCoverage').addEventListener('click', () => openCoverageReview(data));
  document.querySelectorAll('[data-input-project]').forEach((button) =>
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const response = await Api.request(
            `/api/research/${encodeURIComponent(button.dataset.inputProject)}/reports/${Number(button.dataset.inputVersion)}?view=reader`,
          ),
          report = await response.json();
        if (!response.ok) throw Error(report.detail || '来源报告版本无法读取');
        result = report;
        currentHistoryId = report.history_id || null;
        showResult();
        window.scrollTo(0, 0);
      } catch (error) {
        showError(error.message);
        button.disabled = false;
      }
    }),
  );
  if (typeof bindReportQuestions === 'function') bindReportQuestions(data);
  loadReportVersions(data);
  bindCoverageResearch(data);
  setReportView('report');
}

function formatReportTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? '时间未知'
    : date.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      });
}

async function loadReportVersions(data) {
  const picker = $('#readerVersion');
  try {
    const response = await Api.request(`/api/research/${data.research.project_id}/reports`);
    if (!response.ok) return;
    const versions = await response.json();
    if (!picker.isConnected) return;
    picker.title = '时间按浏览器本地时区显示';
    picker.innerHTML = versions
      .map(
        (item) =>
          `<option value="${Number(item.version)}">V${Number(item.version)} · ${escapeHtml(formatReportTime(item.created_at))}</option>`,
      )
      .join('');
    picker.value = String(data.report_version || 1);
    picker.addEventListener('change', async () => {
      picker.disabled = true;
      try {
        const selected = await Api.request(
            `/api/research/${data.research.project_id}/reports/${picker.value}?view=reader`,
          ),
          report = await selected.json();
        if (!selected.ok) throw Error(report.detail || '报告版本无法读取');
        result = report;
        currentHistoryId = report.history_id || null;
        showResult();
        window.scrollTo(0, 0);
      } catch (error) {
        showError(error.message);
        picker.disabled = false;
      }
    });
  } catch {}
}

$$('[data-result-view]').forEach((button) =>
  button.addEventListener('click', () => setReportView(button.dataset.resultView)),
);

function mediaSnapshotMarkdown(snapshot, { includeProcess = false } = {}) {
  if (!snapshot) return '';
  const plain = (value) =>
    String(value || '').replace(/[\\`*_{}\[\]()#+.!<>]/g, (character) => '\\' + character);
  const rows = [
    '## 媒体记录',
    '',
    '以下来自保存时的媒体版本，不代表已观看、听取或证明正文结论。导出不携带原媒体文件，也不会自动内嵌第三方资源。',
    '',
  ];
  const names = {
    not_analyzed: '未分析',
    metadata_only: '仅取得标题或地址',
    image_observed: '已有图像观察',
    subtitles_only: '仅字幕分析',
    audiovisual_observed: '已有视听观察',
  };
  const candidates = (snapshot.items || []).filter((item) =>
    item.retrieval_path === 'page_html_metadata_not_media_bytes' &&
    item.media_kind === 'image' && item.provider === 'external' &&
    item.understanding_status === 'metadata_only' && !item.observation &&
    !item.binding?.anchor_id && !item.old_anchor_unverified,
  );
  const candidateIds = new Set(candidates.map((item) => `${item.evidence_id}:${item.version}`));
  if (candidates.length) {
    const sources = new Set(candidates.map((item) => item.source_url));
    rows.push(`发现 ${candidates.length} 条未分析的页面图片地址，来自 ${sources.size} 个网页；未读取图片内容，不作为报告结论依据。`, '');
  }
  for (const item of snapshot.items || []) {
    if (!includeProcess && candidateIds.has(`${item.evidence_id}:${item.version}`)) continue;
    rows.push(
      `### ${plain(item.title || '媒体资料')}`,
      '',
      `理解状态：${names[item.understanding_status] || '未知'}；展示记录：${plain(item.display_status)}`,
      '',
      `原始发布页：${plain(item.source_url)}`,
      `媒体地址：${plain(item.canonical_media_url)}`,
      '',
      `版本：${plain(item.evidence_id)} / ${Number(item.version)}；记录哈希：${plain(item.record_hash)}`,
      '',
      `文件哈希：${plain(item.content_hash || '未取得媒体字节')}`,
      '',
      `定位：${plain(JSON.stringify(item.locator || {}))}`,
      '',
      `观察记录：${plain(item.observation || '未记录内容观察')}`,
      '',
    );
    if (item.old_anchor_unverified)
      rows.push('旧段落已变化，媒体仅保留为原资料，不能继续当作新段落依据。', '');
  }
  if (!(snapshot.items || []).length) rows.push('本版未绑定媒体；未取得材料不代表完成媒体研究。', '');
  return rows.join('\n') + '\n';
}

function reportToMarkdown(data, { includeProcess = false } = {}) {
  const rows = data.research?.evidence || [],
    sources = new Map((data.research?.sources || []).map((item) => [item.id, item]));
  const numbers = new Map(rows.map((item, index) => [item.id, index + 1]));
  const literal = (value) =>
    String(value || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  const walk = (node, depth) =>
    `${'#'.repeat(Math.min(depth, 6))} ${literal(node.label)}\n\n${literal(node.note)}${(node.citations || [])
      .filter((id) => numbers.has(id))
      .map((id) => ` [^${numbers.get(id)}]`)
      .join('')}\n\n${(node.children || []).map((child) => walk(child, depth + 1)).join('')}`;
  const coverage = includeProcess && data.question_coverage
    ? `## 原文能回答哪些问题\n\n> 模型拆解和覆盖判断不是事实保证，未搜索全网。\n\n${(data.question_coverage.questions || []).map((q) => `### ${literal(q.question)}\n\n状态：${literal(q.status)}；有效核对 ${q.checked_batches || 0}/${q.total_batches || 0} 批\n\n${literal(q.answer || q.error || '未完成核对')} ${(q.excerpts || []).map((e) => (numbers.has(e.evidence_id) ? `[^${numbers.get(e.evidence_id)}]` : '')).join('')}\n\n${(q.missing || []).map((m) => `- 仍缺：${literal(m)}`).join('\n')}\n\n`).join('')}`
    : '';
  const revision =
    (includeProcess && data.source_grounding
      ? `## 原文重核与修改\n\n状态：${literal(data.source_grounding.status)}。匹配原文不证明解释正确；未完成段落仍为待核实草稿。\n\n${(data.source_grounding.units || []).map((u) => `### ${literal(u.heading || u.path)}\n\n状态：${literal(u.status)}；有效重核 ${u.checked_batches || 0}/${u.total_batches || 0} 批\n\n原稿：${literal(u.original_candidate)}\n\n${literal(u.error || u.text || '未形成可采用的重写')}\n\n${(u.unresolved || []).map((v) => `- 尚不能确定：${literal(v)}`).join('\n')}\n\n${(u.excerpts || []).map((e) => `- 模型标注 ${literal(e.relation)}：${literal(e.quote)} ${numbers.has(e.evidence_id) ? `[^${numbers.get(e.evidence_id)}]` : ''}`).join('\n')}\n\n`).join('')}`
      : '') +
    (includeProcess && data.research?.revision
      ? `## 补查纳入记录\n\n仍按原问题综合保存的原文，不把补查总结当证据。\n\n${data.research.revision.input_snapshots.map((r) => `- ${literal(r.title)} · V${Number(r.report_version)} · ${Number(r.evidence_count)} 条材料；来源项目 ${literal(r.project_id)}，报告 ${literal(r.report_id)}`).join('\n')}\n\n`
      : '');
  const fields = reportFieldReferences(data);
  const fieldReferences = fields.length
    ? `## 标题与时间线的修订依据\n\n${fields
        .map(
          (item) =>
            `- ${literal(item.location)}：${literal(item.text)} ${item.ids
              .filter((id) => numbers.has(id))
              .map((id) => `[^${numbers.get(id)}]`)
              .join(' ')}`,
        )
        .join('\n')}\n\n`
    : '';
  const statementRecords = (includeProcess ? data.source_grounding?.units || [] : []).filter(
    (u) => u.attribution?.statements?.length,
  );
  const attribution = statementRecords.length
    ? `## 逐条表述与出处\n\n性质分类与引用关系仍是模型判断，不是事实验证。\n\n${statementRecords
        .map(
          (u) =>
            `### ${literal(u.heading || u.path)}\n\n${u.attribution.statements
              .map(
                (s) =>
                  `${literal(s.text)} ${(s.excerpts || [])
                    .map((e) => (numbers.has(e.evidence_id) ? `[^${numbers.get(e.evidence_id)}]` : ''))
                    .filter(Boolean)
                    .join(' ')}\n\n`,
              )
              .join('')}`,
        )
        .join('')}`
    : '';
  return `# ${literal(data.title)}\n\n> ${data.report_quality?.user_edited ? '用户编辑版本。' : '根据本轮取得的材料整理，来源见文末。'}\n\n${literal(data.summary)}\n\n${(data.tree?.children || []).map((node) => walk(node, 2)).join('')}\n${mediaSnapshotMarkdown(data.research?.media_snapshot, { includeProcess })}${fieldReferences}## 资料范围\n\n<details><summary>完整范围说明</summary>\n\n${(data.research?.limitations || []).map((item) => `- ${literal(item)}`).join('\n')}\n\n</details>\n\n## 来源与原文\n\n${rows
    .map((item, index) => {
      const source = sources.get(item.page_id) || {};
      return `[^${index + 1}]: ${literal(item.quote || item.claim)} — ${literal(source.title)} ${source.url || ''}`;
    })
    .join('\n\n')}\n${includeProcess ? `\n# 处理与核对记录\n\n${revision}${attribution}${coverage}` : ''}`;
}
