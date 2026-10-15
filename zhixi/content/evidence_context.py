"""Preserve source spans and attribution without pretending keyword matches resolve entities."""

from __future__ import annotations

import re
import json
from typing import Any, Iterator


def source_ordered_evidence(evidence: list[dict]) -> list[dict]:
    """Read each page in source order, not extraction's keyword ranking.

    This full-material path eventually reads every record. Ranking disconnected
    lines scatters code/conditions and duplicates context across model calls.
    Page order stays first-seen; unlocated records retain their relative order
    after located records of that page. No evidence, metadata or ID is changed.
    Locations are ordering hints, not attribution or semantic verification.
    """
    page_order = {}
    ordered = []
    for index, row in enumerate(evidence):
        page = row['page_id']
        if page not in page_order:
            page_order[page] = len(page_order)
        span = (row.get('metadata') or {}).get('source_span') or {}
        start, end = span.get('start'), span.get('end')
        located = type(start) is int and type(end) is int and 0 <= start < end
        key = (page_order[page], 0 if located else 1, start if located else index, index)
        ordered.append((key, row))
    return [row for _, row in sorted(ordered, key=lambda entry: entry[0])]


def source_units(text: str) -> Iterator[dict[str, Any]]:
    """Keep short facts and long paragraphs; split only long prose at actual sentence boundaries."""
    for match in re.finditer(r'[^\n]+', text):
        raw = match.group()
        leading = len(raw) - len(raw.lstrip())
        value = raw.strip()
        start = match.start() + leading
        if not value:
            continue
        social = bool(re.match(r'(?:热门评论|时间序列评论)(?:回复)?[（：:]', value))
        code = bool(re.search(r'<[^>]+>|```', value))
        pieces = [value] if social or code or len(value) <= 1000 else re.split(r'(?<=[。！？!?；;])', value)
        offset = 0
        for piece in pieces:
            stripped = piece.strip()
            actual = start + offset + len(piece) - len(piece.lstrip())
            offset += len(piece)
            if stripped:
                yield {'text': stripped, 'start': actual, 'end': actual + len(stripped), 'social': social}


def research_original_quotes(corpus: str) -> list[str]:
    """Read complete quoted fields after a full envelope, not arbitrary keyword lines.

    Preserve quote paragraphs, including real source text beginning with words
    such as '研究目标'. Corpus envelopes remain untrusted model input.
    """
    try:
        bundle = json.loads(corpus)
    except (ValueError, TypeError):
        bundle = None
    if isinstance(bundle, dict) and bundle.get('format') in {
        'evidence_transport_v1',
        'evidence_transport_v2',
    }:
        from zhixi.content.evidence_transport import expand_bundle

        return [
            source['quote']
            for source in expand_bundle(bundle)['sources']
            if source.get('origin') != 'model_generated_not_original'
        ]
    header = re.compile(
        r'^\[证据 [^\]\r\n]+\]\r?\n来源标题：[^\r\n]*\r?\n来源网址：[^\r\n]*\r?\n'
        r'类型：[^\r\n]*(?:\r?\n采样：[^\r\n]*)?(?:\r?\n上下文线索：[^\r\n]*)?\r?\n原文：',
        re.M,
    )
    matches = list(header.finditer(corpus))
    return [
        corpus[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(corpus)].rstrip(
            '\r\n'
        )
        for index, match in enumerate(matches)
    ]


def research_original_units(corpus: str) -> list[str]:
    """Remove envelopes before segmentation so JSON context cannot leak as facts."""
    return [unit['text'] for quote in research_original_quotes(corpus) for unit in source_units(quote)]


def alignment(goal_groups: list[str], quote: str, title: str, *, parent_quote: str = '') -> dict[str, Any]:
    subjects = goal_groups
    named = [subject for subject in subjects if subject.lower() in quote.lower()]
    in_title = [subject for subject in subjects if subject.lower() in title.lower()]
    in_parent = [subject for subject in subjects if subject.lower() in parent_quote.lower()]
    # A subject switch is a signal to retain context for review, not an entity
    # recognition result. Never infer the new subject's identity from this rule.
    shifted = re.split(r'其中|相较之下|至于|另一方面|相比之下', quote)
    switch = (
        len(shifted) > 1
        and named
        and any(
            part.strip() and not any(subject.lower() in part.lower() for subject in subjects)
            for part in shifted[1:]
        )
    )
    if switch:
        state, reason = 'ambiguous', '片段内存在话题转向，不能把整段观点都归给研究对象。'
    elif named:
        state, reason = 'mentions_target', '原文出现目标中的字词；文字重叠不能确定主体或主张归属。'
    elif in_parent:
        state, reason = 'parent_context', '父评论与目标有字词重叠；保留父评论作为上下文，不推定回复主体。'
    elif in_title:
        state, reason = 'page_context', '页面标题与目标有字词重叠；这不是逐句归因结论。'
    else:
        state, reason = 'unresolved', '现有名称与上下文不足以确定片段指向，保留材料待判断。'
    return {
        'status': state,
        'reason': reason,
        'basis': 'lexical_hints_not_entity_resolution',
        'matched_subjects': named,
        'title_subjects': in_title,
        'parent_subjects': in_parent,
    }
