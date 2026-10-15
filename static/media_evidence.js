/* Media cards use saved provenance, never scraped HTML. No third-party request before consent. */
(() => {
  'use strict';
  const active = new WeakMap();
  const displayNames = {
    needs_activation: '待启用外部媒体',
    permission_unknown: '展示权限待核实',
    site_restricted: '站方拒绝内嵌',
    login_required: '需要登录原站',
    load_failed: '加载失败',
    offline: '当前离线',
    external_only: '仅提供原站入口，未内嵌',
    missing: '媒体文件缺失',
    stale: '证据或引用已变化',
    available_local: '本地图片待显示',
  };
  const understandingNames = {
    not_analyzed: '未分析',
    metadata_only: '仅取得标题或地址，未分析媒体内容',
    image_observed: '已有图像观察',
    subtitles_only: '仅字幕分析，未核实画面与听感',
    audiovisual_observed: '已有视听观察',
  };
  const rightsNames = {
    unknown: '权利未核实',
    allowed_embed: '已记录允许内嵌的依据',
    user_provided_research_only: '用户提供，仅供研究',
    provider_embed_candidate: '平台嵌入候选，权利范围待核实',
  };
  function localTime(value) {
    if (!value) return '时间未知';
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? '时间无效'
      : date.toLocaleString('zh-CN', {
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          hour12: false,
        });
  }
  const el = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (cls) node.className = cls;
    return node;
  };
  function safeURL(value) {
    try {
      if (typeof value !== 'string' || /[\s\\\x00-\x1f]/.test(value)) return '';
      const u = new URL(value),
        host = u.hostname.toLowerCase();
      if (
        u.protocol !== 'https:' ||
        u.username ||
        u.password ||
        u.port ||
        !host.includes('.') ||
        /\.(localhost|local|internal|test|invalid)$/.test(host) ||
        /^[\d.]+$/.test(host) ||
        host.includes(':')
      )
        return '';
      return u.href;
    } catch {
      return '';
    }
  }
  function youtubeId(value) {
    const url = safeURL(value);
    if (!url) return '';
    const u = new URL(url);
    let id = '';
    if (u.hostname === 'youtu.be') id = u.pathname.slice(1);
    else if (
      ['youtube.com', 'www.youtube.com', 'm.youtube.com', 'www.youtube-nocookie.com'].includes(u.hostname)
    ) {
      if (u.pathname === '/watch' && u.searchParams.getAll('v').length === 1) id = u.searchParams.get('v');
      else id = u.pathname.match(/^\/(?:embed|shorts)\/([\w-]{11})$/)?.[1] || '';
    }
    return /^[\w-]{11}$/.test(id) ? id : '';
  }
  function link(url, title) {
    const href = safeURL(url);
    if (!href) return el('span', '来源地址无效');
    const a = el('a', title);
    a.href = href;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    return a;
  }
  function isUnexaminedPageCandidate(item, reviews = []) {
    return item.retrieval_path === 'page_html_metadata_not_media_bytes' &&
      item.media_kind === 'image' && item.provider === 'external' &&
      item.understanding_status === 'metadata_only' && !item.observation &&
      !item.binding?.anchor_id && item.binding_status === 'valid' &&
      !item.old_anchor_unverified &&
      !reviews.some((review) => review.evidence_id === item.evidence_id && review.media_version === item.version);
  }
  function renderCandidateSummary(container, candidates, report) {
    if (!candidates.length) return;
    const section = el('div', undefined, 'media-candidate-summary');
    const sourceTitles = new Map((report.research?.sources || []).map((source) => [source.url, source.title]));
    const groups = new Map();
    for (const item of candidates) {
      const url = item.source_url || '';
      groups.set(url, (groups.get(url) || 0) + 1);
    }
    section.append(
      el('h4', `未分析的页面图片线索 · ${candidates.length} 条`),
      el('p', `来自 ${groups.size} 个网页。系统只发现图片地址，未读取或理解图片内容；这些线索不作为报告结论的依据。`, 'media-note'),
    );
    const details = el('details'), list = el('ul');
    details.append(el('summary', '查看涉及的网页'));
    for (const [url, count] of groups) {
      const row = el('li');
      row.append(link(url, sourceTitles.get(url) || url || '来源地址无效'), el('span', ` · ${count} 条地址`));
      list.append(row);
    }
    details.append(list);
    section.append(details, el('p', '完整地址、版本与哈希仍保留；可用“导出报告及处理记录”取得。', 'media-note'));
    container.append(section);
  }
  let youtubeAPI;
  function loadYouTubeAPI() {
    if (window.YT?.Player) return Promise.resolve(window.YT);
    if (youtubeAPI) return youtubeAPI;
    youtubeAPI = new Promise((resolve, reject) => {
      const previous = window.onYouTubeIframeAPIReady;
      const script = el('script');
      script.src = 'https://www.youtube.com/iframe_api';
      const timeout = setTimeout(() => {
        script.remove();
        reject(Error('播放器接口超时；网络或站方限制尚无法区分'));
      }, 20000);
      window.onYouTubeIframeAPIReady = () => {
        clearTimeout(timeout);
        try {
          previous?.();
        } finally {
          resolve(window.YT);
        }
      };
      script.onerror = () => {
        clearTimeout(timeout);
        script.remove();
        reject(Error('播放器接口加载失败；请检查网络或浏览器限制'));
      };
      document.head.append(script);
    }).catch((error) => {
      youtubeAPI = null;
      throw error;
    });
    return youtubeAPI;
  }
  function imageFrame(item, src, setState, cleanups) {
    const frame = el('div', undefined, 'media-image-frame'),
      img = el('img');
    img.alt = item.title || '来源图片';
    img.referrerPolicy = 'no-referrer';
    img.onload = () =>
      setState(
        'image_displayed',
        `图片已显示 · ${img.naturalWidth} × ${img.naturalHeight}；显示不等于内容已分析`,
      );
    img.onerror = () =>
      setState(
        navigator.onLine ? 'load_failed' : 'offline',
        navigator.onLine ? '图片加载失败；原文件缺失、网络或反盗链原因待核实' : '当前离线，图片未能显示',
      );
    img.src = src;
    frame.append(img);
    const b = item.locator?.normalized_bbox;
    if (
      Array.isArray(b) &&
      b.length === 4 &&
      b.every(Number.isFinite) &&
      b.every((v) => v >= 0 && v <= 1) &&
      b[2] > 0 &&
      b[3] > 0 &&
      b[0] + b[2] <= 1 &&
      b[1] + b[3] <= 1
    ) {
      const box = el('span', undefined, 'media-region');
      box.setAttribute('aria-label', '证据定位区域');
      Object.assign(box.style, {
        left: `${b[0] * 100}%`,
        top: `${b[1] * 100}%`,
        width: `${b[2] * 100}%`,
        height: `${b[3] * 100}%`,
      });
      frame.append(box);
    }
    const zoom = el('button', '放大原图');
    zoom.type = 'button';
    zoom.className = 'media-zoom';
    zoom.onclick = () => {
      const dialog = el('dialog', undefined, 'media-image-dialog'),
        close = el('button', '关闭'),
        full = el('img');
      close.type = 'button';
      close.onclick = () => dialog.close();
      full.src = src;
      full.alt = img.alt;
      full.referrerPolicy = 'no-referrer';
      dialog.append(close, full, el('p', item.locator?.capture_scope || '可见范围未记录，不代表全页'));
      dialog.addEventListener('close', () => dialog.remove());
      document.body.append(dialog);
      dialog.showModal();
      cleanups.push(() => {
        if (dialog.open) dialog.close();
        dialog.remove();
      });
    };
    frame.append(zoom);
    return frame;
  }
  async function render(container, report) {
    if (!container) return;
    active.get(container)?.dispose();
    const cleanups = [],
      state = {
        cancelled: false,
        dispose() {
          this.cancelled = true;
          cleanups.forEach((fn) => fn());
        },
      };
    active.set(container, state);
    container.replaceChildren();
    const project = report.research?.project_id,
      version = report.report_version,
      reportId = report.report_id;
    if (!project || !Number.isInteger(version) || version < 1) return;
    const current = () => !state.cancelled && active.get(container) === state && container.isConnected;
    const prefix = `/api/research/${encodeURIComponent(project)}/reports/${version}/media`;
    container.classList.add('report-media');
    container.append(
      el('h3', '相关原资料'),
      el('p', '媒体展示与内容理解分别记录；来源标题、封面和播放器加载均不能证明已观看或听取。', 'media-note'),
    );
    const loading = el('p', '正在读取本报告版本的媒体记录…');
    container.append(loading);
    let data;
    try {
      const response = await Api.request(prefix);
      if (response.status === 404) {
        if (current()) loading.textContent = '当前后台尚未提供此版本的媒体记录；文字报告仍可阅读';
        return;
      }
      data = await response.json();
      if (!response.ok) throw Error(data.detail || '媒体记录读取失败');
    } catch (error) {
      if (current()) loading.textContent = `媒体记录未能恢复：${error.message}`;
      return;
    }
    if (!current()) return;
    loading.remove();
    if (data.report_id && reportId && data.report_id !== reportId) {
      container.append(el('p', '报告 ID 与媒体绑定不一致，停止展示旧引用。'));
      return;
    }
    if (!data.items?.length) {
      container.append(el('p', '本报告版本尚未绑定媒体；这不表示已完成媒体研究。'));
      return;
    }
    const candidates = data.items.filter((item) => isUnexaminedPageCandidate(item, data.reviews || []));
    const candidateIds = new Set(candidates.map((item) => `${item.evidence_id}:${item.version}`));
    for (const item of data.items.filter((entry) => !candidateIds.has(`${entry.evidence_id}:${entry.version}`))) {
      if (!current()) break;
      const card = el('figure', undefined, 'media-evidence-card');
      card.dataset.mediaId = item.evidence_id;
      card.dataset.mediaVersion = item.version;
      card.id = `media-${String(item.evidence_id).replace(/[^\w-]/g, '')}-${Number(item.version)}`;
      const title = el('figcaption', item.title || '媒体资料');
      card.append(title);
      const understanding = understandingNames[item.understanding_status] || '理解状态未知';
      card.append(el('p', understanding, 'media-understanding'));
      const status = el('p', undefined, 'media-display-status');
      status.setAttribute('role', 'status');
      card.append(status);
      const setState = (code, message) => {
        if (current()) {
          card.dataset.displayState = code;
          status.textContent = message;
        }
      };
      const stale = item.binding_status !== 'valid';
      setState(
        stale ? 'stale' : item.display_status,
        stale
          ? '引用已过期：不能把本媒体当作当前段落依据'
          : `${displayNames[item.display_status] || '未知展示状态'}${item.display_reason ? '：' + item.display_reason : ''}`,
      );
      const sourceDetails = el('details', undefined, 'media-source-details');
      sourceDetails.append(el('summary', '出处与使用限制'));
      sourceDetails.append(link(item.source_url, '原始发布页 ↗'));
      if (item.canonical_media_url !== item.source_url)
        sourceDetails.append(link(item.canonical_media_url, '媒体原站入口 ↗'));
      sourceDetails.append(
        el(
          'p',
          `作者：${item.author || '未核实'} · 来源类别：${item.source_role || '未核实'} · ${rightsNames[item.rights_status] || '其他权利记录，需核实范围'}${item.rights_basis ? '；' + item.rights_basis : ''}`,
          'media-note',
        ),
      );
      const scope = item.locator?.capture_scope || '可见范围未记录';
      sourceDetails.append(
        el(
          'p',
          `取得/观测：${localTime(item.captured_at || item.observed_at)}（本地时间） · ${scope}${item.width ? ` · 原图 ${item.width} × ${item.height}` : ''}`,
          'media-note',
        ),
      );
      if (item.capture_status)
        sourceDetails.append(el('p', `页面内容状态：${item.capture_status}`, 'media-note'));
      if (item.duplicate_of)
        card.append(el('p', `与 ${item.duplicate_of} 重复或转载关联，不算独立印证`, 'media-note'));
      if (item.observation)
        card.append(
          el('p', `记录的观察：${item.observation}`),
          el('p', `观察方法：${item.understanding_method || '未记录'}`, 'media-note'),
        );
      const host = el('div', undefined, 'media-display');
      card.append(host);
      if (
        !stale &&
        item.provider === 'local' &&
        item.display_status === 'available_local' &&
        /^[a-f0-9]{64}$/.test(item.content_hash) &&
        item.asset_url === `/api/media-assets/${item.content_hash}`
      ) {
        host.append(imageFrame(item, item.asset_url, setState, cleanups));
      } else if (
        !stale &&
        item.display_status === 'needs_activation' &&
        ((item.provider === 'youtube' &&
          youtubeId(item.canonical_media_url) === item.media_id &&
          /^[\w-]{11}$/.test(item.media_id)) ||
          (item.provider === 'external' &&
            item.media_kind === 'image' &&
            item.rights_status === 'allowed_embed' &&
            item.rights_basis &&
            safeURL(item.canonical_media_url)))
      ) {
        host.append(
          el('p', '启用后会向媒体提供方发送请求（包含 IP 等连接信息）；不会自动播放或发声。', 'media-note'),
        );
        const enable = el('button', item.media_kind === 'video' ? '启用内嵌播放器' : '启用外部图片');
        enable.type = 'button';
        host.append(enable);
        enable.onclick = async () => {
          if (!navigator.onLine) {
            setState('offline', '当前离线，未启用第三方媒体');
            return;
          }
          enable.disabled = true;
          if (item.media_kind === 'image') {
            host.replaceChildren(imageFrame(item, safeURL(item.canonical_media_url), setState, cleanups));
            return;
          }
          setState('loading', '正在请求播放器；尚未验证可播放');
          try {
            const YT = await loadYouTubeAPI();
            if (!current()) return;
            const frame = el('iframe');
            frame.title = item.title || '外部视频播放器';
            frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-presentation');
            frame.allow = 'encrypted-media; fullscreen';
            frame.referrerPolicy = 'strict-origin-when-cross-origin';
            frame.allowFullscreen = true;
            const url = new URL(`https://www.youtube-nocookie.com/embed/${item.media_id}`);
            url.searchParams.set('enablejsapi', '1');
            url.searchParams.set('origin', location.origin);
            url.searchParams.set('autoplay', '0');
            url.searchParams.set('playsinline', '1');
            if (Number.isInteger(item.locator?.start_ms) && item.locator.start_ms >= 0)
              url.searchParams.set('start', String(Math.floor(item.locator.start_ms / 1000)));
            if (Number.isInteger(item.locator?.end_ms) && item.locator.end_ms > item.locator.start_ms)
              url.searchParams.set('end', String(Math.ceil(item.locator.end_ms / 1000)));
            frame.src = url.href;
            host.replaceChildren(frame);
            // iframe.load is intentionally NOT a success signal.
            const timer = setTimeout(
              () =>
                setState(
                  navigator.onLine ? 'load_failed' : 'offline',
                  '播放器未回报就绪，未验证可播放；可从原站核查限制',
                ),
              25000,
            );
            cleanups.push(() => clearTimeout(timer));
            let player = new YT.Player(frame, {
              events: {
                onReady: (event) => {
                  clearTimeout(timer);
                  if (!current()) return;
                  event.target.mute();
                  setState('player_ready', '播放器已就绪，尚未验证播放；点击播放器播放（初始静音）');
                },
                onStateChange: (event) => {
                  if (!current()) return;
                  if (event.data === 1) {
                    const actual = event.target.getVideoData()?.video_id;
                    if (actual !== item.media_id) {
                      setState('stale', '播放器媒体 ID 不匹配，停止播放');
                      event.target.stopVideo();
                      return;
                    }
                    setState(
                      'playing_observed',
                      '播放器回报正在播放指定视频；本工具未据此声称已看过或听过内容',
                    );
                  }
                },
                onError: (event) => {
                  clearTimeout(timer);
                  const names = {
                    2: ['load_failed', '播放器参数无效'],
                    5: ['load_failed', '播放器解码失败'],
                    100: ['missing', '视频已移除、私有或不存在'],
                    101: ['site_restricted', '发布方不允许内嵌'],
                    150: ['site_restricted', '发布方不允许内嵌'],
                    153: ['load_failed', '播放器请求缺少有效身份/来源信息'],
                  };
                  const info = names[event.data] || [
                    'load_failed',
                    `播放器报错 ${event.data}，具体原因待核实`,
                  ];
                  setState(...info);
                },
              },
            });
            cleanups.push(() => {
              try {
                player.destroy();
              } catch {}
            });
          } catch (error) {
            if (current()) {
              setState(navigator.onLine ? 'load_failed' : 'offline', error.message);
              enable.disabled = false;
            }
          }
        };
      }
      card.append(sourceDetails);
      const provenance = el('details'),
        summary = el('summary', '证据版本与定位');
      provenance.append(
        summary,
        el(
          'pre',
          JSON.stringify(
            {
              evidence_id: item.evidence_id,
              version: item.version,
              record_hash: item.record_hash,
              content_hash: item.content_hash || '未取得媒体字节',
              rights_status: item.rights_status,
              locator: item.locator,
              binding: item.binding,
            },
            null,
            2,
          ),
        ),
      );
      card.append(provenance);
      const reviewDetails = el('details', undefined, 'media-review-details'),
        reviewSummary = el('summary', '异议与修订说明');
      reviewDetails.append(reviewSummary);
      card.append(reviewDetails);
      const reviewList = el('div', undefined, 'media-reviews');
      reviewDetails.append(reviewList);
      const appendReview = (review) =>
        reviewList.append(
          el('p', `${review.kind === 'revision_note' ? '修订说明' : '异议'}（尚未自动裁决）：${review.text}`),
        );
      (data.reviews || [])
        .filter((r) => r.evidence_id === item.evidence_id && r.media_version === item.version)
        .forEach(appendReview);
      if (!stale) {
        const form = el('form'),
          label = el('label', '对此区域/片段的异议或修订说明'),
          input = el('textarea'),
          kind = el('select'),
          button = el('button', '保存意见'),
          reply = el('p');
        input.required = true;
        input.maxLength = 4000;
        label.append(input);
        button.type = 'submit';
        reply.setAttribute('role', 'status');
        for (const [value, text] of [
          ['objection', '提出异议'],
          ['revision_note', '记录修订说明'],
        ]) {
          const option = el('option', text);
          option.value = value;
          kind.append(option);
        }
        kind.setAttribute('aria-label', '意见类型');
        form.append(label, kind, button, reply);
        reviewDetails.append(form);
        form.onsubmit = async (event) => {
          event.preventDefault();
          button.disabled = true;
          try {
            const response = await Api.request(prefix + '/reviews', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                evidence_id: item.evidence_id,
                media_version: item.version,
                record_hash: item.record_hash,
                text: input.value,
                kind: kind.value,
              }),
            });
            const review = await response.json();
            if (!response.ok) throw Error(review.detail || '保存失败');
            if (current()) {
              appendReview(review);
              input.value = '';
              reply.textContent = '已保存到此报告/媒体版本；正文修订仍需另存新版。';
            }
          } catch (error) {
            if (current()) reply.textContent = error.message;
          } finally {
            button.disabled = false;
          }
        };
      }
      container.append(card);
      // Explicit, hash-checked server binding locates media beside the corresponding passage.
      if (!stale && item.binding?.anchor_id) {
        const reader = container.closest('#reportReader') || container.parentElement;
        const target = [...reader.querySelectorAll('[data-anchor-id]')].find(
          (n) => n.dataset.anchorId === item.binding.anchor_id,
        );
        if (target) {
          target.after(card);
          cleanups.push(() => card.remove());
        }
      }
    }
    if (current()) renderCandidateSummary(container, candidates, report);
  }
  window.MediaEvidence = {
    render,
    dispose(container) {
      active.get(container)?.dispose();
      active.delete(container);
    },
    safeURL,
    youtubeId,
  };
})();
