from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from zhixi.content.content_pipeline import structure_for_model
from zhixi.sources.platform_adapters import PlatformAdapter
from zhixi.content.text_cleaning import clean_source_text


CONFIG_PREFIX = 'configured:'
MAX_RESPONSE_BYTES = 5_000_000


def value_at(data: Any, path: str) -> Any:
    value = data
    for part in (item for item in path.split('.') if item):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def scalar_text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ''


def _validate_definition(raw: Any, position: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f'第 {position} 个适配器必须是对象')
    adapter_id = str(raw.get('id') or '').strip()
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', adapter_id):
        raise ValueError(f'第 {position} 个适配器 id 无效')
    hosts = raw.get('hosts')
    if not isinstance(hosts, list) or not hosts or len(hosts) > 20:
        raise ValueError(f'适配器 {adapter_id} 需要 1-20 个明确 hosts')
    normalized_hosts = []
    for host in hosts:
        value = str(host).strip().lower().rstrip('.')
        if not re.fullmatch(
            r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', value
        ):
            raise ValueError(f'适配器 {adapter_id} 包含无效主机名')
        normalized_hosts.append(value)
    prefixes = raw.get('path_prefixes') or ['/']
    if not isinstance(prefixes, list) or not prefixes or len(prefixes) > 20:
        raise ValueError(f'适配器 {adapter_id} 的 path_prefixes 无效')
    prefixes = [str(item) for item in prefixes]
    if any(not item.startswith('/') for item in prefixes):
        raise ValueError(f'适配器 {adapter_id} 的路径前缀必须以 / 开头')
    text_paths = raw.get('text_paths') or []
    if not isinstance(text_paths, list) or len(text_paths) > 30:
        raise ValueError(f'适配器 {adapter_id} 的 text_paths 无效')
    text_paths = [str(item).strip() for item in text_paths]
    if any(not item for item in text_paths):
        raise ValueError(f'适配器 {adapter_id} 的 text_paths 不能包含空路径')
    if not text_paths and not (raw.get('items_path') and raw.get('item_text_path')):
        raise ValueError(f'适配器 {adapter_id} 至少需要正文路径或列表文本路径')
    item_limit = int(raw.get('item_limit') or 100)
    if item_limit < 1 or item_limit > 500:
        raise ValueError(f'适配器 {adapter_id} 的 item_limit 必须在 1-500 之间')
    return {
        **raw,
        'id': adapter_id,
        'hosts': list(dict.fromkeys(normalized_hosts)),
        'path_prefixes': prefixes,
        'text_paths': text_paths,
        'item_limit': item_limit,
    }


def _adapter_from_definition(definition: dict[str, Any]) -> PlatformAdapter:
    name = CONFIG_PREFIX + definition['id']
    hosts = set(definition['hosts'])
    prefixes = tuple(definition['path_prefixes'])

    def match(url: str) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower().rstrip('.')
        return definition['id'] if host in hosts and parsed.path.startswith(prefixes) else ''

    def fetch(url: str, _identity: str):
        with httpx.Client(
            headers={'User-Agent': 'WordProcessingResearchBot/1.0', 'Accept': 'application/json'},
            timeout=25.0,
            follow_redirects=False,
        ) as client:
            response = client.get(url)
            if response.status_code in {301, 302, 303, 307, 308}:
                raise ValueError('声明式适配器不跟随重定向，请把最终公开 JSON 地址作为输入')
            response.raise_for_status()
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise ValueError('平台 JSON 响应超过 5 MB 限制')
            try:
                payload = response.json()
            except ValueError as exc:
                raise ValueError('声明式适配器预期 JSON，但接口返回了其他内容') from exc

        title = scalar_text(value_at(payload, str(definition.get('title_path') or ''))) or definition['id']
        lines = []
        for path in definition['text_paths']:
            text = scalar_text(value_at(payload, path))
            if text:
                lines.append(text)
        items = value_at(payload, str(definition.get('items_path') or ''))
        item_text_path = str(definition.get('item_text_path') or '')
        item_url_path = str(definition.get('item_url_path') or '')
        discovered_links = []
        sampled = 0
        if isinstance(items, list):
            for item in items[: definition['item_limit']]:
                text = scalar_text(value_at(item, item_text_path)) if item_text_path else scalar_text(item)
                if text:
                    lines.append(text)
                    sampled += 1
                link = scalar_text(value_at(item, item_url_path)) if item_url_path else ''
                if link:
                    absolute = urljoin(url, link)
                    if urlparse(absolute).scheme in {'http', 'https'}:
                        discovered_links.append({'url': absolute, 'text': text[:200], 'origin': name})
        content, base_cleaning = clean_source_text('\n'.join(lines), web=True, title=title)
        if len(content) < 20:
            raise ValueError('声明式适配器没有从配置路径中提取到足够内容')
        profile = {
            'kind': 'platform_feed',
            'confidence': 'high',
            'reasons': [f'声明式适配器 {definition["id"]}'],
            'signals': {'items_sampled': sampled},
        }
        model_text, structure = structure_for_model(content, profile, [])
        publisher = scalar_text(value_at(payload, str(definition.get('publisher_path') or '')))
        cleaning = {
            **base_cleaning,
            'page': profile,
            'structure': structure,
            'extraction': f'adapter:{name}',
            'discovered_links': discovered_links,
            'source_identity': {
                'publisher': publisher,
                'identity_signals_available': bool(publisher),
                'fetched_host': (urlparse(url).hostname or '').lower(),
                'first_party_status': 'unverified',
                'reason': '发布者字段来自配置的公开 JSON 路径，尚未进行外部身份核验。',
            },
            'platform': {'name': definition['id'], 'items_sampled': sampled, 'declarative': True},
            'warnings': ['声明式适配器按配置字段采样；未配置的字段不会被自动推断。'],
        }
        return title, content, model_text, url, cleaning

    return PlatformAdapter(name, match, fetch)


def load_declarative_adapters(path: Path) -> tuple[list[PlatformAdapter], dict[str, Any]]:
    if not path.exists():
        return [], {'path': str(path), 'loaded': 0, 'errors': [], 'exists': False}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        return [], {'path': str(path), 'loaded': 0, 'errors': [str(exc)], 'exists': True}
    if (
        not isinstance(payload, dict)
        or payload.get('version') != 1
        or not isinstance(payload.get('adapters'), list)
    ):
        return [], {
            'path': str(path),
            'loaded': 0,
            'errors': ['配置需要 version=1 和 adapters 数组'],
            'exists': True,
        }
    adapters = []
    errors = []
    for position, raw in enumerate(payload['adapters'], 1):
        if isinstance(raw, dict) and raw.get('enabled') is False:
            continue
        try:
            adapters.append(_adapter_from_definition(_validate_definition(raw, position)))
        except (TypeError, ValueError) as exc:
            errors.append(str(exc))
    return adapters, {'path': str(path), 'loaded': len(adapters), 'errors': errors, 'exists': True}
