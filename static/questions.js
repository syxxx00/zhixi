/* Version-bound report Q&A; modal lives in the top layer, above the task dock. */
const questionDialog = document.createElement('dialog');
questionDialog.className = 'question-dialog';
questionDialog.setAttribute('aria-labelledby', 'questionDialogTitle');
document.body.append(questionDialog);
const selectionAction = document.createElement('button');
selectionAction.type = 'button';
selectionAction.className = 'selection-question hidden';
selectionAction.textContent = '询问选中文字';
document.body.append(selectionAction);
let questionReport = null,
  questionAnchor = null,
  questionParent = null,
  questionPoll = null;
let pendingSelection = null;
let questionHistoryStamp = '';
let pendingQuestionPlan = null;
const questionAnswerCache = new Map();

function reportNode(data, id) {
  if (id === 'summary') return { id, note: data.summary };
  if (id.startsWith('coverage:')) {
    const q = data.question_coverage?.questions?.find((item) => item.id === id.slice(9));
    return q?.answer ? { id, note: q.answer } : null;
  }
  function walk(node) {
    if (node?.id === id) return node;
    for (const child of node?.children || []) {
      const found = walk(child);
      if (found) return found;
    }
    return null;
  }
  return walk(data.tree);
}
async function textDigest(text) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}
function bindReportQuestions(data, root = document) {
  if (root === document) $('#readerQuestions').addEventListener('click', () => openQuestionDialog(data));
  root.querySelectorAll('[data-ask-anchor]').forEach((button) =>
    button.addEventListener('click', () => {
      const node = reportNode(data, button.dataset.askAnchor);
      if (node)
        openQuestionDialog(data, {
          anchor_id: node.id,
          selected_text: node.note,
          start: 0,
          end: [...node.note].length,
        });
    }),
  );
}
document.addEventListener('selectionchange', () => {
  if (questionDialog.open) return;
  const selection = getSelection();
  selectionAction.classList.add('hidden');
  pendingSelection = null;
  if (!selection?.rangeCount || selection.isCollapsed || !result?.research) return;
  const range = selection.getRangeAt(0),
    root = range.startContainer.parentElement?.closest('[data-report-text]');
  if (!root || !root.contains(range.endContainer)) return;
  const node = reportNode(result, root.dataset.anchorId);
  if (!node) return;
  const preceding = range.cloneRange();
  preceding.selectNodeContents(root);
  preceding.setEnd(range.startContainer, range.startOffset);
  const selected = selection.toString(),
    start = [...preceding.toString()].length;
  if (!selected.trim() || [...node.note].slice(start, start + [...selected].length).join('') !== selected)
    return;
  pendingSelection = {
    anchor_id: node.id,
    selected_text: selected,
    start,
    end: start + [...selected].length,
  };
  const rect = range.getBoundingClientRect();
  selectionAction.style.left = `${Math.max(12, Math.min(innerWidth - 180, rect.left))}px`;
  selectionAction.style.top = `${Math.max(12, Math.min(innerHeight - 48, rect.bottom + 8))}px`;
  selectionAction.classList.remove('hidden');
});
selectionAction.addEventListener('pointerdown', (event) => event.preventDefault());
selectionAction.addEventListener('click', () => {
  if (pendingSelection) openQuestionDialog(result, pendingSelection);
});
questionDialog.addEventListener('close', () => {
  clearInterval(questionPoll);
  questionPoll = null;
});

async function openQuestionDialog(data, anchor = null, parent = null) {
  questionReport = structuredClone(data);
  questionAnchor = anchor;
  questionParent = parent;
  questionHistoryStamp = '';
  pendingQuestionPlan = null;
  questionAnswerCache.clear();
  selectionAction.classList.add('hidden');
  questionDialog.innerHTML = `<header><div><span class="eyebrow">REPORT CONVERSATION · V${Number(data.report_version || 1)}</span><h2 id="questionDialogTitle">${anchor ? '就这段内容提问' : '报告问答记录'}</h2></div><button type="button" id="closeQuestions" aria-label="关闭问答">×</button></header>
    <div class="question-scroll"><p class="question-report-title">${escapeHtml(data.title || '研究报告')}</p>
    ${
      anchor
        ? `<blockquote class="question-selection">${escapeHtml(anchor.selected_text)}</blockquote><form id="questionForm">
      <label for="reportQuestionText">你的问题</label><textarea id="reportQuestionText" rows="3" maxlength="3000" required placeholder="例如：这句话有哪些依据？适用条件是什么？"></textarea>
      <div class="question-controls"><label>回答方式<select id="questionMode"><option value="quick">快速问 · 已有报告与相关原文</option><option value="deep">深入问 · 扩大相关原文检索</option></select></label><label>使用模型<select id="questionModel">${modelRegistry.map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.name)}</option>`).join('')}</select></label></div>
      <label><input type="checkbox" id="questionApplyChanges"> 根据回答执行修订（保留原版，先查看处理范围）</label>
      <label>确认修订后，材料不足时允许补查<select id="questionResearchAllowance"><option value="0">不新增联网</option><option value="10">最多 10 分钟</option><option value="30">最多 30 分钟</option><option value="60">最多 60 分钟</option></select></label>
      <p id="questionSearchScope" class="report-secondary">正在读取原研究的公开搜索范围；不会把选文或报告作为搜索词发送。</p>
      <p id="questionPrivacy" class="report-secondary"></p><p id="questionBehavior" class="report-secondary">默认只回答问题：复用已保存的报告和相关原文，不重做报告、不联网。后续建议不会自动执行。</p>
      <div id="questionPlan" class="report-notice hidden" aria-live="polite"></div>
      <button type="submit" class="question-submit">查看处理范围</button><p id="questionError" role="status"></p></form>`
        : '<p class="report-secondary">在报告中选中文字，或使用段落下的提问按钮。历史问题保留原报告版本，不会随编辑自动迁移。</p>'
    }
      <section id="questionHistory" aria-live="polite"></section></div>`;
  $('#closeQuestions').addEventListener('click', () => questionDialog.close());
  if (!questionDialog.open) questionDialog.showModal();
  if (anchor) {
    const openingReport = questionReport,
      allowance = $('#questionResearchAllowance');
    allowance.disabled = true;
    allowance.value = '0';
    Promise.all([
      Api.request(`/api/research/${encodeURIComponent(data.research.project_id)}`),
      Api.request('/api/health'),
    ])
      .then(async ([response, healthResponse]) => {
        if (!response.ok || !healthResponse.ok)
          throw Error('无法读取公开搜索授权和服务能力，本次只用已存材料。');
        const [project, health] = await Promise.all([response.json(), healthResponse.json()]);
        if (!questionDialog.open || questionReport !== openingReport) return;
        if (!health.capabilities?.feedback_collection) {
          $('#questionSearchScope').textContent = '当前后台尚未启用同项目自动补查，本次只用已存材料。';
          return;
        }
        const topic = project.public_search_topic ?? project.goal;
        const allowed =
          project.search_provider !== 'none' &&
          Boolean(topic?.trim()) &&
          !['paused', 'cancelled'].includes(project.status);
        allowance.dataset.allowed = String(allowed);
        allowance.disabled = !allowed || !$('#questionApplyChanges').checked;
        $('#questionSearchScope').textContent = allowed
          ? `必要补查仅沿已授权公开主题：${topic}。私有选文和完整报告不作为搜索词外发。`
          : '原研究未授权公开搜索，或已暂停/取消；本次只用已存材料，不自动恢复研究。';
      })
      .catch((error) => {
        if (questionDialog.open && questionReport === openingReport)
          $('#questionSearchScope').textContent = error.message;
      });
    $('#questionModel').value = $('#taskModelSelect').value || modelRegistry[0]?.id || '';
    const privacy = () => {
      $('#questionPrivacy').textContent =
        modelRegistry.find((m) => m.id === $('#questionModel').value)?.provider === 'api'
          ? '问题、报告正文及用于核对的原材料会发送到所选API，用于回答和检查关联表述；发生修订冲突时会重新读取新版。'
          : '所选文字、材料、回答与修订均在本机处理。';
    };
    privacy();
    $('#questionModel').addEventListener('change', privacy);
    $('#questionApplyChanges').addEventListener('change', () => {
      allowance.disabled = allowance.dataset.allowed !== 'true' || !$('#questionApplyChanges').checked;
      if (allowance.disabled) allowance.value = '0';
    });
    $('#questionForm').addEventListener('input', () => {
      pendingQuestionPlan = null;
      $('#questionPlan').classList.add('hidden');
      $('#questionForm [type="submit"]').textContent = '查看处理范围';
    });
    $('#questionForm').addEventListener('submit', submitReportQuestion);
    $('#reportQuestionText').focus();
  }
  await loadQuestionHistory();
  clearInterval(questionPoll);
  questionPoll = setInterval(() => {
    if (questionDialog.open) loadQuestionHistory();
  }, 2500);
}
async function submitReportQuestion(event) {
  event.preventDefault();
  const button = event.submitter || event.currentTarget.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    const model = modelRegistry.find((item) => item.id === $('#questionModel').value);
    if (!model) throw Error('请先在设置中添加模型。');
    const node = reportNode(questionReport, questionAnchor.anchor_id);
    const payload = {
      ...modelRequest(model),
      ...questionAnchor,
      text_hash: await textDigest(node.note),
      report_version: questionReport.report_version || 1,
      question: $('#reportQuestionText').value.trim(),
      mode: $('#questionMode').value,
      parent_question_id: questionParent,
      apply_changes: $('#questionApplyChanges').checked,
      feedback_research_minutes: $('#questionApplyChanges').checked ? Number($('#questionResearchAllowance').value) || null : null,
    };
    const fingerprint = JSON.stringify(payload);
    if (!pendingQuestionPlan || pendingQuestionPlan.fingerprint !== fingerprint) {
      const preview = await Api.request(`/api/research/${questionReport.research.project_id}/questions/preview`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: fingerprint,
      });
      const plan = await preview.json();
      if (!preview.ok) throw Error(typeof plan.detail === 'string' ? plan.detail : '无法确认本次处理范围');
      pendingQuestionPlan = { fingerprint, hash: plan.hash };
      $('#questionPlan').innerHTML = `<strong>本次将执行</strong><ol>${plan.steps.map((step) => `<li>${escapeHtml(step)}</li>`).join('')}</ol><p>尚未调用模型。确认后才加入任务队列。</p>`;
      $('#questionPlan').classList.remove('hidden');
      button.textContent = '确认并开始';
      return;
    }
    payload.execution_plan_hash = pendingQuestionPlan.hash;
    const response = await Api.request(`/api/research/${questionReport.research.project_id}/questions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }),
      data = await response.json();
    if (!response.ok)
      throw Error(typeof data.detail === 'string' ? data.detail : '问题提交失败，请检查选文与模型设置。');
    trackedTasks.push({ ...data, label: payload.question });
    persistTasks();
    renderTaskDock();
    startTaskPolling();
    $('#reportQuestionText').value = '';
    pendingQuestionPlan = null;
    $('#questionPlan').classList.add('hidden');
    button.textContent = '查看处理范围';
    $('#questionError').textContent = '已加入队列。可以继续阅读或提交下一个问题。';
    await loadQuestionHistory();
  } catch (error) {
    $('#questionError').textContent = error.message;
  } finally {
    button.disabled = false;
  }
}
function renderQuestionAnswer(answer) {
  const evidence = new Map((answer.research?.evidence || []).map((item) => [item.id, item]));
  const sources = new Map((answer.research?.sources || []).map((item) => [item.id, item]));
  const quotes = (ids) =>
    (ids || [])
      .map((id) => evidence.get(id))
      .filter(Boolean)
      .map((item) => {
        const source = sources.get(item.page_id) || {};
        let url = '';
        try {
          const parsed = new URL(source.url);
          if (['http:', 'https:'].includes(parsed.protocol)) url = parsed.href;
        } catch {}
        return `<blockquote>${escapeHtml(item.quote)}${url ? `<footer><a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.title || '原网页')} ↗</a></footer>` : ''}</blockquote>`;
      })
      .join('');
  function walk(node) {
    const details = (node.children || []).map(walk).join('');
    return `<section><h4>${escapeHtml(node.label || '说明')} ${reportSupport(node.support_status)}</h4><p>${escapeHtml(node.note || '')}</p>${node.citations?.length ? `<details><summary>核对原文</summary>${quotes(node.citations)}</details>` : ''}${node.candidate_citations?.length ? `<details><summary>候选依据 · 支持关系待核实</summary>${quotes(node.candidate_citations)}</details>` : ''}${node.reader_details ? `<details><summary>展开详细材料 · ${(node.children || []).length} 段</summary>${details}</details>` : details}</section>`;
  }
  const scope = answer.question_scope || {};
  const capacityNote = scope.capacity_reduced ? ' 上下文按模型容量选取，未提供不代表原报告没有。' : '';
  return `<p>${escapeHtml(answer.summary || '')}</p>${answer.source_grounding?.status === 'incomplete' ? '<p class="report-notice">部分回答未完成原文重核与重写，仍含待核实草稿。</p>' : ''}<p class="report-secondary">本次使用 ${Number(scope.selected_count || 0)}/${Number(scope.available_count || 0)} 条版本内材料。回答中的改写仍需核对依据。${capacityNote}</p><details><summary>展开解释与证据</summary>${(answer.tree?.children || []).map(walk).join('')}</details>${answer.source_grounding ? `<details><summary>查看原文重核与修改记录</summary><pre class="report-grounding-record">${escapeHtml(JSON.stringify(answer.source_grounding, null, 2))}</pre></details>` : ''}`;
}
async function loadQuestionHistory() {
  const data = questionReport;
  if (!data || !questionDialog.open) return;
  try {
    const response = await Api.request(
      `/api/research/${data.research.project_id}/questions?report_version=${data.report_version || 1}&summary_only=true`,
    );
    if (!response.ok) throw Error('问答记录暂时无法读取');
    const rows = await response.json();
    if (questionReport !== data) return;
    const stamp = JSON.stringify(rows);
    if (stamp === questionHistoryStamp) return;
    questionHistoryStamp = stamp;
    const history = $('#questionHistory'),
      open = new Set(
        [...history.querySelectorAll('[data-question-record][open]')].map(
          (item) => item.dataset.questionRecord,
        ),
      );
    history.innerHTML = `<h3>已保存的问题 · ${rows.length}</h3>${
      rows.length
        ? rows
            .map(
              (
                item,
              ) => `<details class="question-record" data-question-record="${escapeHtml(item.id)}" ${open.has(item.id) ? 'open' : ''}><summary>${escapeHtml(item.question)} <span>V${item.report_version} · ${item.mode === 'deep' ? '深入问' : '快速问'} · ${escapeHtml({ completed: '已回答', failed: '失败', cancelled: '已取消', running: '处理中', queued: '排队中' }[item.status] || item.status || '未知')}</span></summary>
      ${item.anchor_state === 'other_version' ? '<p class="question-version-warning">这是其他版本的问答，未自动迁移到当前报告。</p>' : ''}<blockquote class="question-selection">${escapeHtml(item.anchor.selected_text)}</blockquote>
      <div data-answer-detail="${escapeHtml(item.id)}">${item.answer?.summary ? `<p>${escapeHtml(item.answer.summary)}</p><p class="report-secondary">展开记录后读取解释与原文。</p>` : ''}</div>
      ${item.status !== 'completed' ? `<p role="status">${escapeHtml(item.error_info?.message || item.stage || '等待处理')}${item.answer?.summary ? '；已生成的回答已保留。' : ''}</p>` : ''}
      ${item.answer?.feedback?.status === 'revised' ? `<p class="report-secondary">此反馈已自动修订报告，原版仍可查看。</p><button type="button" data-feedback-version="${Number(item.answer.feedback.report_version)}">查看修订版 V${Number(item.answer.feedback.report_version)}</button>` : ''}
      ${item.answer?.feedback?.status === 'needs_research' ? '<p class="report-secondary">现有材料不足，尚未修改报告；本次尚未执行联网补查。</p>' : ''}
      ${item.answer?.feedback?.status === 'limited' ? '<p class="report-secondary">本轮补查已结束，现有材料仍不足以修正这段结论。回答与原报告均已保留，未继续增加搜集时间。</p>' : ''}
      ${item.answer?.feedback?.status === 'needs_confirmation' ? `<section class="report-notice"><h4>后续处理建议 · 尚未执行</h4><p>${escapeHtml(item.answer.feedback.proposal?.reason || '')}</p><ul>${[...(item.answer.feedback.proposal?.changes || []), ...(item.answer.feedback.proposal?.missing || [])].map((text) => `<li>${escapeHtml(text)}</li>`).join('')}</ul>${item.anchor_state === 'current_version' ? `<button type="button" data-plan-question="${item.id}">查看修订与补查范围</button>` : ''}</section>` : ''}
      ${item.status === 'completed' && item.anchor_state === 'current_version' ? `<button type="button" data-follow-question="${item.id}">继续追问</button>` : ''}
      ${(item.supplementary_research || []).map((project) => `<button type="button" data-question-research="${project.id}">查看补充研究：${escapeHtml(project.goal)}</button>`).join('')}
      ${item.status === 'failed' ? `<button type="button" data-retry-question="${item.job_id}">重试此问题</button>` : ''}</details>`,
            )
            .join('')
        : '<p>此报告还没有问答记录。</p>'
    }`;
    history.querySelectorAll('[data-question-record]').forEach((record) => {
      const item = rows.find((row) => row.id === record.dataset.questionRecord);
      const load = async () => {
        if (!record.open || !item.answer?.summary) return;
        const holder = record.querySelector('[data-answer-detail]');
        const key = `${item.id}:${item.job_id}:${item.status}:${item.answer.summary}`;
        try {
          if (!questionAnswerCache.has(key)) {
            const response = await Api.request(`/api/research/${data.research.project_id}/questions/${item.id}`);
            if (!response.ok) throw Error('回答详情暂时无法读取；已保存的回答不受影响。');
            const answer = await response.json();
            if (questionReport !== data) return;
            questionAnswerCache.set(key, answer);
          }
          if (holder.isConnected) holder.innerHTML = renderQuestionAnswer(questionAnswerCache.get(key));
        } catch (error) {
          if (holder.isConnected) holder.textContent = error.message;
        }
      };
      record.addEventListener('toggle', load);
      if (record.open) load();
    });
    history.querySelectorAll('[data-follow-question]').forEach((button) =>
      button.addEventListener('click', () => {
        const item = rows.find((row) => row.id === button.dataset.followQuestion);
        openQuestionDialog(data, item.anchor, item.id);
      }),
    );
    history.querySelectorAll('[data-plan-question]').forEach((button) =>
      button.addEventListener('click', async () => {
        const item = rows.find((row) => row.id === button.dataset.planQuestion);
        await openQuestionDialog(data, item.anchor, item.id);
        $('#reportQuestionText').value = item.question;
        $('#questionApplyChanges').checked = true;
        $('#questionApplyChanges').dispatchEvent(new Event('change'));
      }),
    );
    history.querySelectorAll('[data-feedback-version]').forEach((button) =>
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const response = await Api.request(
            `/api/research/${encodeURIComponent(data.research.project_id)}/reports/${Number(button.dataset.feedbackVersion)}?view=reader`,
          );
          if (!response.ok) throw Error('修订版暂时无法读取，原报告和回答仍保留。');
          const revised = await response.json();
          questionDialog.close();
          result = revised;
          currentHistoryId = revised.history_id || null;
          showResult();
          window.scrollTo(0, 0);
        } catch (error) {
          button.insertAdjacentText('afterend', error.message);
          button.disabled = false;
        }
      }),
    );
    history.querySelectorAll('[data-question-research]').forEach((button) =>
      button.addEventListener('click', () => {
        questionDialog.close();
        openResearch(button.dataset.questionResearch);
      }),
    );
    history.querySelectorAll('[data-supplement-question]').forEach((form) =>
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const button = form.querySelector('[type="submit"]');
        button.disabled = true;
        const status = form.querySelector('[role="status"]');
        try {
          const response = await Api.request(
              `/api/research/${data.research.project_id}/questions/${form.dataset.supplementQuestion}/research`,
              {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                  query: form.elements.query.value.trim(),
                  time_limit_minutes: Number(form.elements.budget.value),
                  mode: 'automatic',
                  auto_report: true,
                  report_model: modelRequest(modelRegistry.find((m) => m.id === form.elements.model.value)),
                }),
              },
            ),
            project = await response.json();
          if (!response.ok)
            throw Error(typeof project.detail === 'string' ? project.detail : '补充研究创建失败');
          trackedTasks.push({ ...project, kind: 'research', label: project.goal });
          persistTasks();
          renderTaskDock();
          startTaskPolling();
          await loadQuestionHistory();
        } catch (error) {
          status.textContent = error.message;
          button.disabled = false;
        }
      }),
    );
    history.querySelectorAll('[data-retry-question]').forEach((button) =>
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const response = await Api.request(`/api/jobs/${button.dataset.retryQuestion}/retry`, {
              method: 'POST',
            }),
            job = await response.json();
          if (!response.ok) throw Error(job.detail || '重试失败');
          trackedTasks.push(job);
          persistTasks();
          renderTaskDock();
          startTaskPolling();
          await loadQuestionHistory();
        } catch (error) {
          button.insertAdjacentText('afterend', error.message);
          button.disabled = false;
        }
      }),
    );
  } catch (error) {
    if ($('#questionHistory')) $('#questionHistory').textContent = error.message;
  }
}
async function openQuestionResult(task) {
  const project = task.project_id || task.result?.project_id;
  const response = await Api.request(`/api/research/${project}/reports/${task.result.report_version}?view=reader`);
  if (!response.ok) return showError('原报告版本暂时无法读取，请从历史报告打开问答。');
  await openQuestionDialog(await response.json());
}
