from __future__ import annotations

import html
import json
import re
from datetime import date
from typing import Any
from urllib.parse import urlparse


JSONLD_PATTERN = re.compile(
    r"<script\b[^>]*type\s*=\s*['\"]application/ld\+json['\"][^>]*>(.*?)</script>", re.I | re.S
)


def _name(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()[:300]
    if isinstance(value, dict):
        return str(value.get('name') or '').strip()[:300]
    if isinstance(value, list):
        return '；'.join(filter(None, (_name(item) for item in value)))[:300]
    return ''


def extract_jsonld_identity(raw_html: str) -> dict[str, Any]:
    publishers: list[str] = []
    authors: list[str] = []
    same_as: list[str] = []
    declared_urls: list[str] = []
    dates: dict[str, list[str]] = {'published_at': [], 'modified_at': []}

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        types = value.get('@type', [])
        types = [types] if isinstance(types, str) else types
        types = types if isinstance(types, list) else []
        if any(t in {'Article', 'NewsArticle', 'BlogPosting', 'ScholarlyArticle', 'WebPage', 'Report'} for t in types):
            for source, target in (('datePublished', 'published_at'), ('dateModified', 'modified_at')):
                declared_date = valid_declared_date(value.get(source))
                if declared_date:
                    dates[target].append(declared_date)
        publisher = _name(value.get('publisher'))
        author = _name(value.get('author'))
        if publisher:
            publishers.append(publisher)
        if author:
            authors.append(author)
        urls = value.get('sameAs')
        for item in urls if isinstance(urls, list) else [urls]:
            if isinstance(item, str) and item.startswith(('http://', 'https://')):
                same_as.append(item[:2000])
        declared = value.get('url')
        if isinstance(declared, str) and declared.startswith(('http://', 'https://')):
            declared_urls.append(declared[:2000])
        for key in ('@graph', 'mainEntity', 'mainEntityOfPage', 'isPartOf', 'publisher', 'author'):
            if key in value:
                walk(value[key])

    for match in JSONLD_PATTERN.finditer(raw_html[:5_000_000]):
        try:
            walk(json.loads(html.unescape(match.group(1)).strip()))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return {
        'jsonld_publishers': list(dict.fromkeys(publishers))[:10],
        'jsonld_authors': list(dict.fromkeys(authors))[:10],
        'same_as': list(dict.fromkeys(same_as))[:30],
        'jsonld_urls': list(dict.fromkeys(declared_urls))[:10],
        'jsonld_dates': {key: list(dict.fromkeys(values))[:10] for key, values in dates.items() if values},
    }


def valid_declared_date(value):
    """Accept explicit calendar dates only; never infer from a title/year range."""
    if not isinstance(value, str) or not re.match(r'^\d{4}-\d{2}-\d{2}(?:$|[T ])', value.strip()):
        return ''
    try:
        date.fromisoformat(value.strip()[:10])
    except ValueError:
        return ''
    return value.strip()[:100]


def build_source_identity(
    fetched_url: str, metadata: dict[str, str], canonical_url: str, raw_html: str
) -> dict[str, Any]:
    structured = extract_jsonld_identity(raw_html)
    metadata = dict(metadata)
    for field in ('published_at', 'modified_at'):
        explicit = valid_declared_date(metadata.get(field))
        values = structured.get('jsonld_dates', {}).get(field, [])
        # Conflicting article dates stay as declarations, not an invented date.
        if explicit:
            metadata[field] = explicit
        elif values and len({v[:10] for v in values}) == 1:
            metadata[field] = values[0]
        else:
            metadata.pop(field, None)
    fetched_host = (urlparse(fetched_url).hostname or '').lower()
    canonical_host = (urlparse(canonical_url).hostname or '').lower()
    declared = bool(
        metadata.get('publisher')
        or metadata.get('site_name')
        or structured['jsonld_publishers']
        or structured['same_as']
    )
    return {
        **metadata,
        **structured,
        'fetched_host': fetched_host,
        'declared_canonical_url': canonical_url,
        'canonical_same_host': bool(canonical_url and fetched_host == canonical_host),
        'first_party_status': 'identity_declared' if declared else 'unverified',
        'reason': (
            '页面提供了发布者、站点或 sameAs 身份声明；这些是可追踪线索，仍需外部交叉验证。'
            if declared
            else '页面未提供可识别的发布者或账号关联，不能自动证明其为第一方来源。'
        ),
    }


def source_context(page):
    """Small shared view of captured identity, not a trust score or body text.

    Extraction metadata is preserved in the page snapshot. No network, model
    inference, historical-row migration or raw-HTML reparse occurs here.
    """
    extraction = page.get('extraction') or page.get('extraction_json') or {}
    if isinstance(extraction, str):
        try:
            extraction = json.loads(extraction)
        except ValueError:
            return {}  # Optional legacy metadata must not prevent reading originals.
    if not isinstance(extraction, dict):
        return {}
    identity = extraction.get('source_identity') if isinstance(extraction.get('source_identity'), dict) else {}
    metadata = extraction.get('metadata') if isinstance(extraction.get('metadata'), dict) else {}
    context = {}
    for key in ('published_at', 'modified_at'):
        value = valid_declared_date(identity.get(key)) or valid_declared_date(metadata.get(key))
        if value:
            context[key] = value
        declarations = identity.get('jsonld_dates', {}).get(key, [])
        dates = sorted({v[:10] for v in [value, *declarations] if valid_declared_date(v)})
        if len(dates) > 1:
            context.pop(key, None)
            context[f'conflicting_{key}'] = dates
    for key in ('publisher', 'author', 'site_name'):
        value = identity.get(key) or metadata.get(key)
        if not value and key in {'publisher', 'author'}:
            value = '；'.join(identity.get(f'jsonld_{key}s') or [])
        if value:
            context[key] = str(value)[:300]
    # Captured layout is a reading constraint, not a semantic trust verdict.
    # In particular, a portal can contain real prose as well as article links.
    captured = extraction.get('page') or {}
    if isinstance(captured, dict) and captured.get('kind'):
        signals = captured.get('signals') if isinstance(captured.get('signals'), dict) else {}
        context['captured_form'] = {
            'kind': captured['kind'],
            'selection': captured.get('selection_reason', ''),
            'signals': {key: signals[key] for key in
                        ('prose_ratio', 'link_ratio', 'paragraphs', 'headings', 'list_items')
                        if key in signals},
            'scope': 'layout_hint_not_content_verification; link_labels_are_not_target_bodies',
        }
    if context:
        context['meaning'] = 'page_declared_metadata_and_captured_layout_not_verified; publication_date_is_not_data_period'
    return context
