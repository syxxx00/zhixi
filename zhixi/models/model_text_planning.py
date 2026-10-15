from __future__ import annotations

import json
import re
from typing import Any


def _text_units(text: str, limit: int) -> list[str]:
    paragraphs = [item.strip() for item in re.split(r'\n+', text) if item.strip()]
    units: list[str] = []
    for paragraph in paragraphs or [text.strip()]:
        if len(paragraph) <= limit:
            units.append(paragraph)
            continue
        sentences = [
            item.strip() for item in re.split(r'(?<=[。！？!?；;])\s*|(?<=\.)\s+', paragraph) if item.strip()
        ]
        for sentence in sentences or [paragraph]:
            if len(sentence) <= limit:
                units.append(sentence)
            else:
                units.extend(sentence[index : index + limit] for index in range(0, len(sentence), limit))
    return units


def _pack_units(units: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for unit in units:
        extra = len(unit) + (1 if current else 0)
        if current and used + extra > limit:
            chunks.append('\n'.join(current))
            current, used = [], 0
        current.append(unit)
        used += len(unit) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append('\n'.join(current))
    return chunks


def _structured_chunks(source: dict[str, Any], limit: int) -> list[str]:
    sections = source.get('sections')
    if not isinstance(sections, list) or not sections:
        return []
    metadata = {key: value for key, value in source.items() if key != 'sections'}
    expanded: list[dict[str, Any]] = []
    for raw in sections:
        if not isinstance(raw, dict):
            expanded.append({'heading': '未命名区块', 'items': [raw]})
            continue
        if len(json.dumps(raw, ensure_ascii=False)) <= limit:
            expanded.append(raw)
            continue
        items = raw.get('items') if isinstance(raw.get('items'), list) else []
        if not items:
            return []
        group: list[Any] = []
        for item in items:
            candidate = {**raw, 'items': [*group, item]}
            if group and len(json.dumps(candidate, ensure_ascii=False)) > limit:
                expanded.append({**raw, 'items': group})
                group = []
            group.append(item)
        if group:
            expanded.append({**raw, 'items': group})
    output: list[str] = []
    group = []
    for section in expanded:
        candidate = {**metadata, 'sections': [*group, section]}
        if group and len(json.dumps(candidate, ensure_ascii=False)) > limit:
            output.append(json.dumps({**metadata, 'sections': group}, ensure_ascii=False))
            group = []
        group.append(section)
    if group:
        output.append(json.dumps({**metadata, 'sections': group}, ensure_ascii=False))
    return output


def split_source_chunks(text: str, limit: int = 4_200, preserve_evidence: bool = False) -> list[str]:
    """Split a model source without dropping text or breaking structured page sections when possible."""
    if limit < 1:
        raise ValueError('分批长度必须大于零')
    source = text
    if not source.strip():
        return []
    if len(source) <= limit:
        return [source]
    if preserve_evidence:
        starts = list(re.finditer(r'(?m)^\[证据 [^\]\r\n]+\]\r?\n来源标题：', source))
        if starts:
            prefix = source[: starts[0].start()].strip()
            blocks = [
                source[
                    match.start() : starts[index + 1].start() if index + 1 < len(starts) else len(source)
                ].strip()
                for index, match in enumerate(starts)
            ]
            # The target size is soft for one complete record. Never split a
            # source ID away from its original text, parent reply or context JSON.
            return [prefix + '\n\n' + chunk for chunk in _pack_units(blocks, max(1, limit - len(prefix) - 2))]
    try:
        structured = json.loads(source)
    except (json.JSONDecodeError, TypeError):
        structured = None
    if isinstance(structured, dict):
        chunks = _structured_chunks(structured, limit)
        if chunks:
            return chunks
    # Keep an exact partition, including indentation, blank lines and CRLF.
    # Prefer line/sentence boundaries without splitting decimal dots or adding
    # separators that did not exist in the input. Long atomic text is split at
    # the explicit size boundary, but remains reconstructible in source order.
    chunks = []
    start = 0
    while start < len(source):
        end = min(start + limit, len(source))
        if end < len(source):
            boundaries = list(re.finditer(r'\r?\n|[。！？!?；;]', source[start:end]))
            if boundaries:
                end = start + boundaries[-1].end()
        chunks.append(source[start:end])
        start = end
    return chunks
