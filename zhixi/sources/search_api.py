"""Credentialed public-query discovery, with no models, settings or storage.

The caller supplies a public search query (never a document or conversation).
Results are discovery metadata, including Baidu's ``content`` excerpt; none of
these fields establishes that a destination's original body has been fetched.

Contracts checked against the official documentation:
https://cloud.baidu.com/doc/qianfan-api/s/Wmbq4z7e5
https://cloud.baidu.com/doc/qianfan/s/Cmh4suws6
https://api-dashboard.search.brave.com/api-reference/web/search/get
"""

from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from threading import Lock
import time
from urllib.parse import urlsplit

import httpx

from zhixi.research.research_acquisition import SearchAccessError, SearchResults


ENDPOINTS = {
    'baidu': 'https://qianfan.baidubce.com/v2/ai_search/web_search',
    'brave': 'https://api.search.brave.com/res/v1/web/search',
}
CACHE_VERSION = 'credentialed-web-metadata-v1'
REQUEST_INTERVAL = 1.0
REQUEST_TIMEOUT = 15.0
REQUEST_BUDGET = 20.0
MAX_RESPONSE_BYTES = 3_000_000
FAILURE_COOLDOWN = 60.0
MAX_COOLDOWN = 3600.0
_locks = {engine: Lock() for engine in ENDPOINTS}
_next_request = dict.fromkeys(ENDPOINTS, 0.0)
_cooldowns = {engine: {} for engine in ENDPOINTS}
_MAX_RESULTS = {'baidu': 50, 'brave': 20}
_MESSAGES = {
    'access_blocked': '搜索密钥缺失、无效或权限不足，请检查密钥与服务权限。',
    'rate_limited': '搜索额度不足或请求限流，请检查额度并稍后重试。',
    'access_cooldown': '搜索服务暂处于鉴权或额度冷却期，请检查密钥或稍后重试。',
    'http_error': '搜索服务返回错误，请稍后重试。',
    'parse_failed': '搜索响应格式异常，不能据此判断没有结果。',
    'transport_error': '搜索连接失败，请检查网络配置后重试。',
    'unexpected_response': '搜索参数或响应格式不受支持，请检查搜索设置。',
    'unexpected_redirect': '搜索服务发生重定向，已停止请求以保护密钥。',
    'response_too_large': '搜索响应超过大小限制，未继续读取。',
    'request_deadline': '搜索等待超时，请稍后重试。',
    'cancelled': '搜索已停止。',
    'query_too_long': '查询过长，未发送或截断：百度最多72单位（汉字占2）；Brave最多600字符、75词。',
}
_AUTH_CODES = {
    'baidu': {
        '216003', 'InvalidHTTPAuthHeader', 'InvalidAccessKeyId', 'AccessDenied',
        'OptInRequired', 'PermissionDeniedError', 'TokenStatusInvalidError',
        'SdkTokenNotExistError', 'QianfanPermissionDeniedError', 'QianfanApiExpired',
        'invalid_iam_token', 'no_parameter_permission',
    },
    'brave': {
        'SUBSCRIPTION_TOKEN_INVALID', 'SUBSCRIPTION_NOT_FOUND',
        'OPTION_NOT_IN_PLAN', 'RESOURCE_NOT_ALLOWED',
    },
}
_QUOTA_CODES = {
    'baidu': {
        'QuotaLimitExceeded', 'QuotaLimitExpired', 'ChargeNotEnough',
        'rpm_rate_limit_exceeded', 'tpm_rate_limit_exceeded', 'user_rate_limit_exceeded',
        'cluster_rate_limit_exceeded', 'cluster_rpm_rate_limit_exceeded',
        'cluster_tpm_rate_limit_exceeded', 'preemptible_rate_limit_exceeded',
    },
    'brave': {'QUOTA_LIMITED', 'RATE_LIMITED', 'CREDIT_EXHAUSTED', 'USAGE_LIMIT_EXCEEDED'},
}


class _Failure(Exception):
    """Internal control flow; only fixed labels and an integer may escape."""

    def __init__(self, code, *, http_status=None, retry_after=0.0, cause=None):
        self.code = code
        self.http_status = http_status
        self.retry_after = retry_after
        self.cause = cause


def _retry_after(value):
    """Only a bounded duration escapes; never retain the raw response header."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return FAILURE_COOLDOWN
    if not math.isfinite(seconds):
        return FAILURE_COOLDOWN
    return min(MAX_COOLDOWN, max(FAILURE_COOLDOWN, seconds))


def _active(should_continue, deadline):
    if should_continue is not None:
        try:
            keep_going = bool(should_continue())
        except Exception:
            # A broken cancellation callback must not authorize a paid request.
            keep_going = False
        if not keep_going:
            raise _Failure('cancelled')
    if time.monotonic() >= deadline:
        raise _Failure('request_deadline')


def _validate(engine, api_key, query, limit, should_continue):
    if not isinstance(engine, str) or engine not in ENDPOINTS:
        raise _Failure('unexpected_response')
    if not isinstance(api_key, str) or not api_key or any(not 33 <= ord(c) <= 126 for c in api_key):
        raise _Failure('access_blocked')
    if not isinstance(query, str) or not query.strip():
        raise _Failure('unexpected_response')
    if type(limit) is not int or limit <= 0:
        raise _Failure('unexpected_response')
    if should_continue is not None and not callable(should_continue):
        raise _Failure('unexpected_response')
    # Baidu documents 72 units with Han characters counting twice. Conservatively
    # count every non-ASCII character twice; never let the provider truncate it.
    too_long = (
        sum(1 if c.isascii() else 2 for c in query) > 72
        if engine == 'baidu'
        else len(query) > 600 or len(query.split()) > 75
    )
    if too_long:
        raise _Failure('query_too_long')
    # Invalid Unicode must also fail before paying for a request.
    try:
        query.encode('utf-8')
    except UnicodeError:
        raise _Failure('unexpected_response') from None
    return min(limit, _MAX_RESULTS[engine])


def _reject_constant(value):
    raise ValueError('Invalid JSON constant')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON member')
        result[key] = value
    return result


def _decode(raw, status):
    try:
        return json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise _Failure('parse_failed' if 200 <= status < 300 else 'http_error', http_status=status) from None


def _provider_error(data, engine, status):
    if not isinstance(data, dict):
        return
    if 'error' in data:
        detail = data['error']
        if not isinstance(detail, dict):
            raise _Failure('http_error', http_status=status)
        code, provider_status = detail.get('code'), detail.get('status')
    elif 'code' in data:  # Baidu documents this field only for errors.
        code, provider_status = data['code'], None
    elif data.get('type') == 'ErrorResponse':
        raise _Failure('http_error', http_status=status)
    else:
        return
    # Do not stringify arbitrary provider objects or include error details/IDs.
    token = str(code) if type(code) in (str, int) else ''
    if token in _AUTH_CODES[engine] or provider_status in (401, 403):
        category = 'access_blocked'
    elif token in _QUOTA_CODES[engine] or provider_status in (402, 429):
        category = 'rate_limited'
    else:
        category = 'http_error'
    raise _Failure(category, http_status=status)


def _safe_url(url):
    if not url or '\\' in url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return False
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme in {'http', 'https'}
            and bool(parsed.hostname)
            and '%' not in parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and (parsed.port is None or 0 < parsed.port <= 65535)
        )
    except ValueError:
        return False


def _rows(data, engine, limit):
    if not isinstance(data, dict):
        raise _Failure('parse_failed')
    if engine == 'baidu':
        entries = data.get('references')
    else:
        web = data.get('web')
        entries = web.get('results') if isinstance(web, dict) else None
    # Missing/null results do not prove that a search completed with zero hits.
    if not isinstance(entries, list):
        raise _Failure('parse_failed')
    rows, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise _Failure('parse_failed')
        url, title = entry.get('url'), entry.get('title')
        if not isinstance(url, str) or not isinstance(title, str) or not title.strip():
            raise _Failure('parse_failed')
        if engine == 'baidu':
            if entry.get('type', 'web') != 'web':
                raise _Failure('parse_failed')
            description = entry.get('snippet')
            if description is None:
                # The official example has content but no snippet. It is still
                # search metadata, never a fetched original body.
                description = entry.get('content')
        else:
            description = entry.get('description')
        if description is None:
            description = ''
        if not isinstance(description, str):
            raise _Failure('parse_failed')
        if not _safe_url(url) or url in seen:
            continue
        seen.add(url)
        rows.append({'url': url, 'title': title, 'description': description})
    if entries and not rows:
        raise _Failure('parse_failed')
    return rows[:limit]


def _request(engine, api_key, query, limit, active, remaining):
    headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity'}
    if engine == 'baidu':
        method = 'POST'
        headers['Authorization'] = 'Bearer ' + api_key
        options = {'json': {
            'edition': 'standard',
            'search_source': 'baidu_search_v2',
            'messages': [{'role': 'user', 'content': query}],
            'resource_type_filter': [{'type': 'web', 'top_k': limit}],
        }}
    else:
        method = 'GET'
        headers['X-Subscription-Token'] = api_key
        options = {'params': {
            'q': query, 'count': limit, 'search_lang': 'zh-hans', 'country': 'CN',
            'ui_lang': 'zh-CN', 'text_decorations': 'false', 'spellcheck': 'false',
        }}
    # A fresh client cannot reuse cookies/auth from another provider. Preserve
    # HTTPX's default environment/proxy policy and TLS certificate validation.
    # HTTPX's default transport has zero retries; there is no retry loop here.
    with httpx.Client(
        timeout=httpx.Timeout(min(REQUEST_TIMEOUT, remaining())),
        follow_redirects=False,
    ) as client:
        active()
        with client.stream(method, ENDPOINTS[engine], headers=headers, **options) as response:
            active()
            status = response.status_code
            if 300 <= status < 400:
                raise _Failure('unexpected_redirect', http_status=status)
            if status in (401, 403):
                raise _Failure('access_blocked', http_status=status)
            if status in (402, 429):
                raise _Failure('rate_limited', http_status=status,
                               retry_after=_retry_after(response.headers.get('retry-after')))
            content_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
            if content_type != 'application/json' and not (
                content_type.startswith('application/') and content_type.endswith('+json')
            ):
                raise _Failure(
                    'unexpected_response' if 200 <= status < 300 else 'http_error', http_status=status,
                )
            # HTTPX decodes Content-Encoding exactly once. Bound the decoded
            # body as well as any declared wire size, including compressed JSON.
            length = response.headers.get('content-length')
            if length is not None:
                try:
                    size = int(length)
                except ValueError:
                    raise _Failure('unexpected_response', http_status=status) from None
                if size < 0:
                    raise _Failure('unexpected_response', http_status=status)
                if size > MAX_RESPONSE_BYTES:
                    raise _Failure('response_too_large', http_status=status)
            raw = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                active()
                if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise _Failure('response_too_large', http_status=status)
                raw.extend(chunk)
            active()
            data = _decode(raw, status)
            try:
                _provider_error(data, engine, status)
            except _Failure as failure:
                if failure.code == 'rate_limited':
                    failure.retry_after = _retry_after(response.headers.get('retry-after'))
                raise
            if not 200 <= status < 300:
                raise _Failure('http_error', http_status=status)
            rows = _rows(data, engine, limit)
            active()
    active()  # Cancellation during response/client cleanup must win too.
    return rows


def discover(engine, api_key, query, limit=10, *, should_continue=None) -> SearchResults:
    """Send at most one paid search request, returning ranked URL metadata.

    Limits are capped to one provider page (Baidu 50, Brave 20). Queries are
    never rewritten, split or truncated. Calls for each provider are serialized
    and paced across credentials; lock/pacing waits are cancellable. Socket
    waits have a timeout <=15s and deadline/cancellation checks occur between
    stream chunks (a blocked synchronous read ends at its socket timeout).
    Only explicit result arrays, including [], count as successful responses.
    """
    started = time.monotonic()
    deadline = started + REQUEST_BUDGET
    attempts = 0
    http_status = None
    retry_after = 0.0
    cause = None
    try:
        count = _validate(engine, api_key, query, limit, should_continue)
        credential = hashlib.sha256(api_key.encode('ascii')).digest()

        def active():
            _active(should_continue, deadline)

        active()
        lock = _locks[engine]
        while not lock.acquire(timeout=0.05):
            active()
        try:
            active()
            cooldowns = _cooldowns[engine]
            for fingerprint, (until, _) in list(cooldowns.items()):
                if until <= time.monotonic():
                    del cooldowns[fingerprint]
            if credential in cooldowns:
                until, reason = cooldowns[credential]
                raise _Failure('access_cooldown', retry_after=max(0, until - time.monotonic()), cause=reason)
            while time.monotonic() < _next_request[engine]:
                active()
                time.sleep(min(0.05, max(0, _next_request[engine] - time.monotonic())))
            active()
            attempts = 1
            # Record dispatch time even if transport fails: failures must not
            # produce a burst of subsequent paid attempts.
            _next_request[engine] = time.monotonic() + REQUEST_INTERVAL
            try:
                rows = _request(engine, api_key, query, count, active,
                                lambda: max(0.001, deadline - time.monotonic()))
            except _Failure as failure:
                if failure.code in {'access_blocked', 'rate_limited'}:
                    delay = max(FAILURE_COOLDOWN, failure.retry_after)
                    cooldowns[credential] = (time.monotonic() + delay, failure.code)
                    failure.retry_after = delay
                raise
            active()
        finally:
            lock.release()
        active()
    except _Failure as failure:
        code, http_status = failure.code, failure.http_status
        retry_after, cause = failure.retry_after, failure.cause
    except Exception:
        # Raw httpx/config/JSON exceptions may embed headers, URLs or bodies.
        # Raise outside the except block so neither __cause__ nor __context__
        # retains that exception, even when callers inspect repr/diagnostics.
        code = 'transport_error'
    else:
        return SearchResults(rows, [{
            'engine': engine, 'status': 'returned' if rows else 'empty',
            'attempts': attempts, 'seconds': round(time.monotonic() - started, 3),
        }])
    detail = {'status': code, 'attempts': attempts}
    if isinstance(engine, str) and engine in ENDPOINTS:
        detail['engine'] = engine
    if http_status is not None:
        detail['http_status'] = http_status
    if retry_after:
        detail['retry_after_seconds'] = round(retry_after, 3)
    if cause is not None:
        detail['cause'] = cause
    error = SearchAccessError(_MESSAGES[code], code, [detail])
    if http_status is not None:
        error.http_status = http_status
    raise error from None
