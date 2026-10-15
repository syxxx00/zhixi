/* JSON API transport shared by the reader, task queue and OPS.
 * The deadline covers headers AND body. No automatic replay of writes.
 * request() returns a buffered JSON-response adapter, not a streaming Response.
 */
(() => {
  function failure(status, value) {
    const detail = value?.detail;
    const message = typeof detail === 'string' ? detail : detail?.message;
    const error = new Error(message || `后台请求失败（HTTP ${status}），请稍后重试；任务记录仍保留。`);
    error.status = status;
    return error;
  }

  async function readJSON(response) {
    let value;
    try {
      value = JSON.parse(await response.text());
    } catch (error) {
      if (error.name === 'AbortError') throw error;
      if (!response.ok) throw failure(response.status);
      throw new Error('后台返回了无法解析的数据，请重试。');
    }
    if (!response.ok) throw failure(response.status, value);
    return value;
  }

  async function request(url, options = {}) {
    const { timeoutMs = 30000, controller: owner, signal = owner?.signal, ...init } = options;
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    let rejectAbort;
    const aborted = new Promise((_, reject) => {
      rejectAbort = reject;
    });
    const onAbort = () => rejectAbort(new DOMException('请求超时或已取消；未更改任务状态。', 'AbortError'));
    controller.signal.addEventListener('abort', onAbort, { once: true });
    if (controller.signal.aborted) onAbort();
    const timer = setTimeout(abort, timeoutMs);
    try {
      const transfer = (async () => {
        const response = await fetch(url, { cache: 'no-store', ...init, signal: controller.signal });
        const body = await response.text();
        const buffered = {
          ok: response.ok,
          status: response.status,
          headers: response.headers,
          url: response.url,
          text: async () => body,
          json: () => readJSON(buffered),
        };
        return buffered;
      })();
      return await Promise.race([transfer, aborted]);
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      controller.signal.removeEventListener('abort', onAbort);
    }
  }

  function storedArray(storage, key) {
    try {
      const value = JSON.parse(storage.getItem(key) || '[]');
      return Array.isArray(value) ? value : [];
    } catch {
      return [];
    }
  }

  globalThis.Api = Object.freeze({
    request,
    readJSON,
    storedArray,
    json: async (url, options) => (await request(url, options)).json(),
  });
})();
