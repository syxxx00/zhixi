"""Anonymous search transport and result-card parsing, never source evidence.

Only fixed search endpoints are requested here. Destination URLs are returned
as candidates; the existing public-URL/robots checks own subsequent reading.
No browser cookies, CAPTCHA solving, model calls or database access.
"""

from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import math
import re
import socket
import ssl
from threading import Lock
import time
from urllib.parse import unquote, urlencode, urljoin, urlsplit, parse_qs

import httpx

from zhixi.research.research_acquisition import SearchAccessError, SearchResults, check_search_access


ENDPOINTS = {
    'baidu_web': ('https://www.baidu.com/s', 'wd'),
    'yahoo': ('https://search.yahoo.com/search', 'p'),
    'so360': ('https://www.so.com/s', 'q'),
}
CACHE_VERSION = 'anonymous-cards-v2'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.7',
}
REQUEST_INTERVAL = 2.0
REQUEST_BUDGET = 40.0
RETRY_DELAY = 2.0
MAX_RESPONSE_BYTES = 3_000_000
_locks = {engine: Lock() for engine in ENDPOINTS}
_next_request = {engine: 0.0 for engine in ENDPOINTS}
_cooldown = {engine: 0.0 for engine in ENDPOINTS}
_cooldown_reason = {engine: '' for engine in ENDPOINTS}
_cooldown_errors = {engine: [] for engine in ENDPOINTS}
_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'wbr'}


class _Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.parts = []

    @property
    def classes(self):
        return set((self.attrs.get('class') or '').split())

    def nodes(self, exclude=None):
        for part in self.parts:
            if isinstance(part, _Node) and part.tag not in {'script', 'style', 'svg'}:
                if exclude is None or not exclude(part):
                    yield part
                    yield from part.nodes(exclude)

    def text(self):
        return ' '.join(
            part if isinstance(part, str) else part.text()
            for part in self.parts
            if isinstance(part, str) or part.tag not in {'script', 'style', 'svg'}
        )


class _Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        # HTML permits omitted </li>. Do not swallow the next result card.
        if tag == 'li':
            node = self.current
            while node.parent and node.tag not in {'ul', 'ol'}:
                if node.tag == 'li':
                    self.current = node.parent
                    break
                node = node.parent
        node = _Node(tag, attrs, self.current)
        self.current.parts.append(node)
        if tag not in _VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.parts.append(data)


def _is_card(node, engine):
    if engine == 'baidu_web':
        # result-op includes generated answers, images and recommendations.
        return node.tag == 'div' and {'result', 'c-container'}.issubset(node.classes)
    if engine == 'yahoo':
        return node.tag == 'div' and 'algo' in node.classes
    return node.tag == 'li' and 'res-list' in node.classes


def _destination(anchor, engine, card=None):
    href = anchor.attrs.get('href') or ''
    if 'e-landurl' in anchor.attrs:  # 360 paid result, even with an empty value.
        return ''
    parsed = urlsplit(urljoin(ENDPOINTS[engine][0], href))
    if parsed.hostname in {'e.so.com', 'ads.search.yahoo.com'} or parsed.path.startswith('/aclick'):
        return ''
    if engine == 'yahoo' and parsed.hostname == 'r.search.yahoo.com':
        if '/RU=' not in href:
            return ''
        href = unquote(href.split('/RU=', 1)[1].split('/RK=', 1)[0].split('/RS=', 1)[0])
    elif engine == 'so360':
        href = anchor.attrs.get('data-mdurl') or href
    elif engine == 'baidu_web':
        if card.classes & {'ec-tuiguang', 'ec-container', 'c-ad'} or 'data-tuiguang' in card.attrs:
            return ''
        # Natural cards expose the original target in mu. Never resolve every
        # tracking link at search time; source reading validates each redirect.
        href = card.attrs.get('mu') or urljoin(ENDPOINTS[engine][0], href)
    parsed = urlsplit(href)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        return ''
    if engine == 'baidu_web' and parsed.hostname in {'www.baidu.com', 'baidu.com'}:
        if parsed.path != '/link' or not parse_qs(parsed.query).get('url'):
            return ''
    if engine == 'baidu_web' and parsed.hostname in {
        'image.baidu.com', 'nourl.ubs.baidu.com', 'cpro.baidu.com', 'pos.baidu.com',
        'tuiguang.baidu.com', 'wappass.baidu.com',
    }:
        return ''
    if parsed.hostname in {
        'search.yahoo.com',
        'r.search.yahoo.com',
        'images.search.yahoo.com',
        'video.search.yahoo.com',
        'so.com',
        'www.so.com',
        'e.so.com',
        'image.so.com',
        'ai.so.com',
    }:
        return ''  # Search navigation/opaque redirects are not destination pages.
    return href


def parse_results(raw_html, engine, limit=10):
    """Read each natural-result card independently; unknown markup is an error.

    Search snippets may be incomplete or wrong. Preserve them as discovery
    metadata, without upgrading them to fetched body text or semantic proof.
    """
    document = _Document()
    try:
        document.feed(raw_html)
        nodes = list(document.root.nodes())
        cards = [node for node in nodes if _is_card(node, engine)]
        results, seen = [], set()
        for card in cards:
            owned = list(card.nodes(lambda node: _is_card(node, engine)))
            headings = [node for node in owned if node.tag == 'h3']
            if engine == 'so360':
                headings = [node for node in headings if 'res-title' in node.classes]
            if not headings:
                continue
            heading = headings[0]
            anchor = next((node for node in heading.nodes() if node.tag == 'a'), None)
            if anchor is None:
                node = heading.parent
                while node is not None and node is not card:
                    if node.tag == 'a':
                        anchor = node
                        break
                    node = node.parent
            if anchor is None:
                continue
            url = _destination(anchor, engine, card)
            title = ' '.join(heading.text().split())
            if not url or not title or url in seen:
                continue
            snippet_classes = (
                {'compText'} if engine == 'yahoo' else {'res-desc', 'res-comm-con', 'res-summary'}
            )
            snippets = [node for node in owned if node.classes & snippet_classes]
            if engine == 'baidu_web':
                snippets = [node for node in owned if 'c-abstract' in node.classes or any(
                    name.startswith('summary-text_') for name in node.classes
                )]
            # A malformed outer card must not absorb a nested card's snippet.
            description = ''
            if snippets and not any(_is_card(n, engine) for n in snippets[0].nodes()):
                description = ' '.join(snippets[0].text().split())
            seen.add(url)
            results.append({'url': url, 'title': title[:300], 'description': description[:1000]})
            if len(results) >= limit:
                break
    except (ValueError, RecursionError) as exc:
        raise SearchAccessError(f'{engine} 搜索页解析失败', 'parse_failed') from exc
    if results:
        return results
    empty_nodes = [n for n in nodes if n.classes & {'no-results', 'no-result', 'search-noresult'}]
    if empty_nodes and any(re.search(r'No results|没有找到|未找到', n.text(), re.I) for n in empty_nodes):
        return []
    raise SearchAccessError(f'{engine} 未识别出自然搜索结果；不能据此判断没有资料', 'unexpected_response')


def _request(engine, query, should_continue=None):
    endpoint, key = ENDPOINTS[engine]
    current = endpoint + '?' + urlencode({key: query})
    origin = urlsplit(endpoint)
    allowed_paths = {'/search', '/_bv/v.gif'} if engine == 'yahoo' else {'/s'}
    # Keep only this anonymous request's cookies across normal same-origin
    # redirects. Never follow a login/challenge or arbitrary source URL here.
    with httpx.Client(
        headers=HEADERS,
        timeout=httpx.Timeout(15.0),
        follow_redirects=False,
    ) as client:
        for _ in range(5):
            if should_continue is not None and not should_continue():
                raise SearchAccessError('搜索已停止，未发送请求', 'cancelled')
            with client.stream('GET', current) as response:
                if response.is_redirect:
                    location = response.headers.get('location', '')
                    target = urlsplit(urljoin(current, location))
                    # Recognize the provider's verification destination without
                    # visiting it or persisting its query/token.
                    from zhixi.sources.web_access import verification_destination

                    if verification_destination(target.geturl()) or (
                        target.hostname == origin.hostname
                        and re.search(r'/(captcha|antispider|sorry)(/|$)', target.path, re.I)
                    ):
                        raise SearchAccessError(
                            f'{engine} 搜索需要访问验证，未跟随验证页面', 'challenge_required'
                        )
                    if (
                        not location
                        or target.scheme != 'https'
                        or target.hostname != origin.hostname
                        or target.port not in {None, 443}
                        or target.username
                        or target.password
                        or target.path not in allowed_paths
                    ):
                        raise SearchAccessError(
                            f'{engine} 搜索转向了未允许的入口，未跟随', 'unexpected_redirect'
                        )
                    current = target.geturl()
                    continue
                content = bytearray()
                for chunk in response.iter_bytes():
                    if should_continue is not None and not should_continue():
                        raise SearchAccessError('搜索已停止，未继续读取响应', 'cancelled')
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise SearchAccessError(f'{engine} 搜索响应过大，未解析', 'response_too_large')
                loaded = httpx.Response(
                    response.status_code,
                    # iter_bytes() has already decompressed the response. Do
                    # not ask a second Response to decode gzip/br again.
                    headers={
                        k: v
                        for k, v in response.headers.items()
                        if k.lower() not in {'content-encoding', 'content-length'}
                    },
                    content=bytes(content),
                    request=response.request,
                )
                break
        else:
            raise SearchAccessError(f'{engine} 搜索重定向过多，未取得结果', 'unexpected_redirect')
    try:
        check_search_access(loaded, engine)
    except SearchAccessError as exc:
        exc.retry_after = _retry_after(loaded.headers.get('retry-after', ''))
        raise
    if loaded.status_code != 200:
        exc = SearchAccessError(f'{engine} 搜索返回 HTTP {loaded.status_code}', 'http_error')
        exc.http_status = loaded.status_code
        exc.retry_after = _retry_after(loaded.headers.get('retry-after', ''))
        raise exc
    if 'html' not in loaded.headers.get('content-type', '').lower():
        raise SearchAccessError(f'{engine} 返回的不是 HTML 搜索页', 'unexpected_response')
    return loaded.text


def _retry_after(value):
    try:
        delay = float(value) if value.isdigit() else parsedate_to_datetime(value).timestamp() - time.time()
        return max(0.0, delay) if math.isfinite(delay) else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _transport_failure(exc):
    """Classify causes without logging URLs, proxy credentials or raw messages."""
    chain, current = [], exc
    while current is not None and all(current is not prior for prior in chain):
        chain.append(current)
        current = current.__cause__
    message = ' '.join(str(item).lower() for item in chain)
    if (
        any(isinstance(item, ssl.SSLCertVerificationError) for item in chain)
        or 'certificate_verify_failed' in message
    ):
        kind, label = 'tls_certificate', 'TLS证书校验失败'
    elif any(isinstance(item, socket.gaierror) for item in chain):
        kind, label = 'dns', '域名解析失败'
    elif 'unexpected_eof' in message or 'eof occurred in violation of protocol' in message:
        kind, label = 'tls_eof', 'TLS连接被提前关闭'
    elif isinstance(exc, httpx.ProxyError):
        kind, label = 'proxy_connection', '代理连接失败'
    elif isinstance(exc, httpx.TimeoutException):
        kind, label = type(exc).__name__, '网络请求超时'
    else:
        kind, label = type(exc).__name__, '网络连接或传输失败'
    return kind, label, kind != 'tls_certificate'


def discover(engine, query, limit=10, *, should_continue=None):
    if not query.strip() or limit <= 0:
        return []
    if engine not in ENDPOINTS:
        raise ValueError('Unknown search engine')
    started = time.monotonic()

    def active():
        if should_continue is not None and not should_continue():
            raise SearchAccessError('搜索已停止，未继续请求', 'cancelled')
        if time.monotonic() - started >= REQUEST_BUDGET:
            raise SearchAccessError('搜索请求时间已用完，未无限等待', 'request_deadline')
        return True

    def wait_until(until):
        while time.monotonic() < until:
            active()
            time.sleep(min(0.1, max(0, until - time.monotonic())))

    # Waiting for another query must remain cancellable too.
    while not _locks[engine].acquire(timeout=0.1):
        active()
    try:
        active()
        if time.monotonic() < _cooldown[engine]:
            raise SearchAccessError(
                f'{engine} 暂时不可用，退避中（{_cooldown_reason[engine]}）',
                'access_cooldown',
                [{
                    'retry_after_seconds': round(_cooldown[engine] - time.monotonic(), 1),
                    'cause': _cooldown_reason[engine],
                    'errors': _cooldown_errors[engine],
                }],
            )
        wait_until(_next_request[engine])
        attempts = []
        for attempt in range(2):
            active()
            try:
                rows = parse_results(_request(engine, query, active), engine, limit)
                return SearchResults(rows, [{
                    'attempts': attempt + 1, 'recovered_errors': attempts,
                    'seconds': round(time.monotonic() - started, 2),
                }])
            except httpx.HTTPError as cause:
                kind, label, retryable = _transport_failure(cause)
                error = SearchAccessError(f'{engine}：{label}；不代表没有搜索结果', 'transport_error')
                error.__cause__ = cause
                detail = {'status': error.code, 'cause': kind}
            except SearchAccessError as cause:
                error = cause
                status = getattr(error, 'http_status', None)
                retryable = status in {500, 502, 503, 504}
                detail = {'status': error.code}
                if status is not None:
                    detail['http_status'] = status
            attempts.append(detail)
            if error.code == 'cancelled':
                raise error
            retry_after = getattr(error, 'retry_after', 0)
            if retryable and attempt == 0 and not retry_after:
                wait_until(time.monotonic() + RETRY_DELAY)
                continue
            if error.code not in {'cancelled', 'request_deadline'}:
                _cooldown[engine] = time.monotonic() + max(60, retry_after)
                _cooldown_reason[engine] = detail.get('cause') or error.code
                _cooldown_errors[engine] = attempts
            error.diagnostics = [{
                'attempts': len(attempts), 'errors': attempts,
                'seconds': round(time.monotonic() - started, 2),
                'retry_after_seconds': max(0, round(_cooldown[engine] - time.monotonic(), 1)),
            }]
            raise error
    finally:
        _next_request[engine] = time.monotonic() + REQUEST_INTERVAL
        _locks[engine].release()
