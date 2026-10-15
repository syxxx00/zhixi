"""Bounded original reading with independently citable visible occurrences.

Selected records carry their full stored context. Other complete quotes already
inside those windows get their own IDs without recursively opening new windows.
The original ledger is never edited; this is a reading view, not data cleaning.
"""

from copy import deepcopy
from hashlib import sha256
import json

from zhixi.content.evidence_transport import source_bundle, compact_bundle, source_origin


def reading_bundle(evidence, selected_ids, pages, aliases):
    by_id = {str(row['id']): row for row in evidence}
    selected = set(selected_ids)
    if len(by_id) != len(evidence) or not selected or not selected <= by_id.keys():
        raise ValueError('原文阅读需要唯一且存在的记录编号')
    rows = [row for identity, row in by_id.items() if identity in selected]
    from zhixi.content.evidence_context import source_ordered_evidence

    rows = source_ordered_evidence(rows)
    if any(source_origin(row) == 'model_generated_not_original' for row in rows):
        raise ValueError('模型草稿不是可读取原文')
    bundle = source_bundle(rows, pages, aliases)
    visible = list(rows)
    additions = []
    for identity, row in by_id.items():
        if identity in selected or source_origin(row) == 'model_generated_not_original':
            continue
        metadata = row.get('metadata') or {}
        span = metadata.get('source_span') or {}
        start, end = span.get('start'), span.get('end')
        if not (
            type(start) is int and type(end) is int and 0 <= start < end and end - start == len(row['quote'])
        ):
            continue
        window = next(
            (
                window
                for window in bundle['context_windows']
                if window['page_id'] == row['page_id']
                and window.get('comment_id') == metadata.get('comment_id')
                and window['start'] <= start < end <= window['end']
                and window['text'][start - window['start'] : end - window['start']] == row['quote']
            ),
            None,
        )
        if window is None:
            continue
        # Keep provenance, author/parent context and quote exactly; only opening
        # of this neighbour's own before/after window is deferred explicitly.
        source = source_bundle([row], pages, aliases)['sources'][0]
        context = source['context_not_quote']
        context.pop('before', None)
        context.pop('after', None)
        context['window'] = [window['window_id'], start, end, start, end]
        context['reading_scope'] = 'complete_quote_in_visible_window_not_full_adjacent_context'
        context['full_context_record_id'] = aliases[identity]
        bundle['sources'].append(source)
        visible.append(row)
        additions.append(
            {
                'evidence_id': identity,
                'source_id': aliases[identity],
                'page_id': row['page_id'],
                'comment_id': metadata.get('comment_id'),
                'source_span': deepcopy(span),
                'window_id': window['window_id'],
                'full_context_opened': False,
            }
        )
    bundle['reading_scope'] = {
        'selected_full_context_ids': [aliases[str(row['id'])] for row in rows],
        'neighbour_quote_only_ids': [item['source_id'] for item in additions],
        'all_quotes_visible_in_this_call': len(visible) == len(evidence),
        'warning': '邻句拥有独立引用编号，但未因此递归打开它的全部前后文。不能将此阅读范围以外当作无信息；如判断依赖更远条件，须读取full_context_record_id。',
    }
    compressed = compact_bundle(bundle)

    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

    transport = compressed if len(encode(compressed)) < len(encode(bundle)) else bundle
    return (
        transport,
        visible,
        {
            'method': 'explicit_read_window_with_exact_neighbour_ids_v1',
            'selected_evidence_ids': [str(row['id']) for row in rows],
            'visible_evidence_ids': [str(row['id']) for row in visible],
            'quote_only_records': additions,
            'semantic_support': 'not_assessed',
            'windows': [
                {key: value for key, value in window.items() if key != 'text'}
                | {'text_sha256': sha256(window['text'].encode('utf-8')).hexdigest()}
                for window in bundle['context_windows']
            ],
        },
    )
