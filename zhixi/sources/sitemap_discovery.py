from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Callable
from urllib.parse import unquote, urljoin, urlparse

import httpx


def parse_sitemap_document(
    xml_text: str, expected_host: str, canonicalize: Callable[[str], str]
) -> tuple[str, list[str]]:
    root = ET.fromstring(xml_text)
    kind = root.tag.rsplit('}', 1)[-1].lower()
    if kind not in {'urlset', 'sitemapindex'}:
        return 'unknown', []
    urls = []
    for element in root.iter():
        if element.tag.rsplit('}', 1)[-1].lower() != 'loc' or not element.text:
            continue
        url = canonicalize(element.text.strip())
        if url and (urlparse(url).hostname or '').lower() == expected_host:
            urls.append(url)
    return kind, list(dict.fromkeys(urls))


def _fetch_public_text(
    url: str, validate_url: Callable[[str], None], before_fetch: Callable[[str], None]
) -> str:
    current = url
    with httpx.Client(
        headers={'User-Agent': 'Mozilla/5.0 compatible; LocalResearchBot/1.0'},
        timeout=20.0,
        follow_redirects=False,
    ) as client:
        for _ in range(3):
            validate_url(current)
            before_fetch(current)
            response = client.get(current)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get('location')
                if not location:
                    return ''
                current = urljoin(current, location)
                continue
            if response.status_code in {403, 404}:
                return ''
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                return ''
            return response.text
    return ''


def discover_sitemap_links(
    page_url: str,
    validate_url: Callable[[str], None],
    canonicalize: Callable[[str], str],
    before_fetch: Callable[[str], None],
    max_urls: int = 200,
) -> list[dict[str, str]]:
    parsed = urlparse(page_url)
    host = (parsed.hostname or '').lower()
    origin = f'{parsed.scheme}://{parsed.netloc}'
    sitemap_urls = [origin + '/sitemap.xml']
    robots = _fetch_public_text(origin + '/robots.txt', validate_url, before_fetch)
    for match in re.finditer(r'(?im)^\s*Sitemap\s*:\s*(https?://\S+)\s*$', robots):
        candidate = canonicalize(match.group(1))
        if (
            candidate
            and (urlparse(candidate).hostname or '').lower() == host
            and not candidate.lower().endswith('.gz')
        ):
            sitemap_urls.append(candidate)
    pages: list[str] = []
    for sitemap_url in list(dict.fromkeys(sitemap_urls))[:5]:
        xml_text = _fetch_public_text(sitemap_url, validate_url, before_fetch)
        if not xml_text:
            continue
        try:
            kind, urls = parse_sitemap_document(xml_text, host, canonicalize)
        except ET.ParseError:
            continue
        if kind == 'sitemapindex':
            for child_url in urls[:8]:
                if child_url.lower().endswith('.gz'):
                    continue
                child_text = _fetch_public_text(child_url, validate_url, before_fetch)
                if not child_text:
                    continue
                try:
                    child_kind, child_pages = parse_sitemap_document(child_text, host, canonicalize)
                except ET.ParseError:
                    continue
                if child_kind == 'urlset':
                    pages.extend(child_pages)
                if len(pages) >= max_urls:
                    break
        elif kind == 'urlset':
            pages.extend(urls)
        if len(pages) >= max_urls:
            break
    output = []
    for url in list(dict.fromkeys(pages))[:max_urls]:
        slug = unquote(urlparse(url).path.rstrip('/').rsplit('/', 1)[-1])
        output.append({'url': url, 'text': re.sub(r'[_-]+', ' ', slug).strip()[:200], 'origin': 'sitemap'})
    return output
