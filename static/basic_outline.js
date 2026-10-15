/* A read-only view of the same editable tree, not a second model summary. */
(function (root) {
  function renderBasicOutline(host, result) {
    if (!host) return;
    const expanded = new Set(
      Array.from(host.querySelectorAll('details[data-path][open]'), (node) => node.dataset.path),
    );
    host.replaceChildren();
    host.hidden = !result?.tree || Boolean(result.research) || result.source?.type === 'research';
    if (host.hidden) return;
    const doc = host.ownerDocument;
    const text = (tag, value) => {
      const node = doc.createElement(tag);
      node.textContent = typeof value === 'string' ? value : '';
      return node;
    };
    const pending = [{ node: result.tree, parent: host, path: '0' }];
    while (pending.length) {
      const { node, parent, path } = pending.pop();
      if (!node || typeof node !== 'object') continue;
      const children = Array.isArray(node.children) ? node.children : [];
      const section = doc.createElement(children.length ? 'details' : 'article');
      section.className = 'basic-outline-node';
      if (children.length) {
        section.dataset.path = path;
        section.open = path === '0' || expanded.has(path);
      }
      section.append(text(children.length ? 'summary' : 'h4', node.label || '未命名节点'));
      if (node.note) section.append(text('p', node.note));
      parent.append(section);
      for (let i = children.length - 1; i >= 0; i--)
        pending.push({ node: children[i], parent: section, path: `${path}.${i}` });
    }
  }
  root.renderBasicOutline = renderBasicOutline;
  root.webpageReadingMarkdown = function (result) {
    if (!result?.webpage_reading) return '';
    const lines = ['\n\n## 网页来源\n'];
    for (const page of result.webpage_reading.pages || []) {
      if (page.status !== 'included' || !/^W\d+$/.test(page.id)) continue;
      try {
        const url = new URL(page.url);
        if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) continue;
        lines.push(`[${page.id}]: <${url.href.replace(/>/g, '%3E').replace(/</g, '%3C')}>`);
        lines.push(
          `- ${page.id}：${String(page.title || '').replace(/[\r\n]/g, ' ')}（已读取，不等于已核实）`,
        );
      } catch {}
    }
    for (const note of result.webpage_reading.limitations || []) lines.push(`\n阅读限制：${note}`);
    return lines.join('\n');
  };
  root.renderWebpageReading = function (host, result) {
    if (!host) return;
    host.replaceChildren();
    const reading = result?.webpage_reading;
    host.hidden = !reading;
    if (!reading) return;
    const doc = host.ownerDocument;
    const appendText = (parent, tag, value) => {
      const element = doc.createElement(tag);
      element.textContent = value || '';
      parent.append(element);
      return element;
    };
    const labels = {
      included: '已纳入分析',
      not_relevant: '已读，未采用',
      duplicate: '重复内容',
      empty: '未取得正文',
      judgment_failed: '用途判断未完成',
      unread_capacity: '容量不足，未分析',
      failed: '读取失败',
      not_selected: '未选择访问',
      not_followed: '仅识别入口',
      not_read: '未读取',
    };
    appendText(
      host,
      'p',
      '补充阅读围绕输入网页展开；已读不等于已核实。图片、视频及文件链接不代表已理解其内容。',
    );
    for (const page of reading.pages || []) {
      const item = doc.createElement('details');
      item.className = 'basic-outline-node';
      appendText(
        item,
        'summary',
        `${page.id} · ${page.title || page.url} · ${labels[page.status] || page.status}`,
      );
      const link = appendText(item, 'a', page.url);
      try {
        const url = new URL(page.url);
        if (['http:', 'https:'].includes(url.protocol) && !url.username && !url.password) {
          link.href = url.href;
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
        }
      } catch {}
      appendText(item, 'p', (page.observations || []).join('\n'));
      const original = doc.createElement('details');
      appendText(original, 'summary', '查看本次取得的文字');
      appendText(original, 'p', page.content);
      item.append(original);
      host.append(item);
    }
    const other = (reading.links || []).filter((link) => !link.page_id);
    if (other.length) {
      const list = doc.createElement('details');
      appendText(list, 'summary', `未读入口与读取情况（${other.length}）`);
      for (const link of other)
        appendText(
          list,
          'p',
          `${link.occurrences?.[0]?.text || link.url}\n${link.url}\n${labels[link.status] || link.status}：${link.error || link.reason || '未选择继续阅读'}`,
        );
      host.append(list);
    }
  };
})(globalThis);
