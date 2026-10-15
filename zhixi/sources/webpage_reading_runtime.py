"""Adapters for normal analysis; does not create/resume research projects."""

import json
from urllib.parse import urljoin, urlsplit

from zhixi.sources.webpage_reading import contextual_links, read_links


def fetch_public_page(ctx, url):
    """Anonymous static HTML only. Validate every hop; no renderer/adapters/files."""
    current = url
    try:
        with ctx.httpx.Client(
            timeout=25,
            follow_redirects=False,
            headers={
                'User-Agent': 'WordProcessingReader/1.0',
                'Accept': 'text/html,application/xhtml+xml,text/plain',
            },
        ) as client:
            for _ in range(6):
                parts = urlsplit(current)
                if parts.scheme not in {'http', 'https'} or parts.username or parts.password:
                    raise ValueError('补充阅读不接受非网页协议或带凭据的地址')
                ctx._validate_public_url(current)
                client.cookies.clear()
                with client.stream('GET', current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = response.headers.get('location')
                        if not target:
                            raise ValueError('重定向缺少地址')
                        current = urljoin(current, target)
                        continue
                    response.raise_for_status()
                    mime = response.headers.get('content-type', '').split(';')[0].strip().lower()
                    if (
                        mime not in {'text/html', 'application/xhtml+xml', 'text/plain'}
                        or 'attachment' in response.headers.get('content-disposition', '').lower()
                    ):
                        raise ValueError('此入口不是可直接阅读的页面；未下载文件')
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        body.extend(chunk)
                        if len(body) > 2_000_000:
                            raise ValueError('页面传输超过安全容量，未继续下载')
                    raw = bytes(body).decode(response.encoding or 'utf-8', errors='replace')
                if mime == 'text/plain':
                    title, text = current, raw
                    material = {'stages': []}
                    links = []
                else:
                    parser = ctx.ArticleParser()
                    parser.feed(raw)
                    title, text = parser.result()
                    material = {'stages': [{'kind': 'http_decoded_html', 'text': raw}]}
                    links = contextual_links(material, current, ctx.canonicalize_url)
                return {
                    'title': title,
                    'url': current,
                    'content': text,
                    'links': links,
                    'retrieval': 'anonymous_static_http',
                    'original': raw,
                }
        raise ValueError('页面重定向次数过多，未继续访问')
    except ctx.httpx.HTTPError as exc:
        raise ValueError(f'补充网页读取失败：{type(exc).__name__}') from exc


def supplement(ctx, request, root, progress, runtime_update, job_id):
    def checkpoint(stage):
        if ctx.job_cancelled(job_id):
            raise ctx.JobCancelled('网页补充阅读已取消')
        ctx.wait_if_job_paused(job_id)
        if progress:
            progress(14, stage)

    def decide(system, packet):
        checkpoint('判断网页内容与后续链接')
        catalog = json.dumps(packet, ensure_ascii=False)
        if request.provider == 'local':
            # Use the existing per-job model lock, cancellation and selected model.
            return ctx.run_qwen(
                '',
                root['title'],
                request,
                progress,
                runtime_update,
                job_id,
                task_payload={
                    'task': 'research_reasoning',
                    'phase': 'webpage:read',
                    'system': system,
                    'catalog': catalog,
                },
            )
        config = request.api_config
        if not config:
            raise ValueError('请配置 API 模型')
        headers = {'Content-Type': 'application/json'}
        if config.api_key:
            headers['Authorization'] = 'Bearer ' + config.api_key
        base = config.base_url.rstrip('/')
        endpoint = base if base.endswith('/chat/completions') else base + '/chat/completions'
        if runtime_update:
            runtime_update(model_started_at=ctx.time.time(), model_kind='api', model_name=config.model)
        try:
            response = ctx.httpx.post(
                endpoint,
                headers=headers,
                json={
                    'model': config.model,
                    'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': catalog}],
                    'temperature': 0.1,
                    'max_tokens': 2000,
                },
                timeout=1800,
            )
            response.raise_for_status()
            choice = response.json()['choices'][0]
            checkpoint('校验网页阅读判断')
            if choice.get('finish_reason') != 'stop':
                raise ValueError('网页阅读判断输出未完整结束')
            return ctx.extract_json_object(choice['message']['content'])
        except (ctx.httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
            raise ValueError('网页阅读判断请求失败：' + type(exc).__name__) from exc

    return read_links(
        root,
        decide=decide,
        fetch=lambda url: fetch_public_page(ctx, url),
        canonicalize=ctx.canonicalize_url,
        validate=ctx._validate_public_url,
        checkpoint=checkpoint,
        cancellation_errors=(ctx.JobCancelled,),
    )
