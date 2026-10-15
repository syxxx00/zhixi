"""Compact local model transport, not cleaning or semantic evidence selection.

Every quote and evidence identity remains intact. Overlapping context is shared
only when same-page offsets and every overlapping character agree. Unlocated or
inconsistent context stays inline; stored evidence/diagnostics are never edited.
"""

from copy import deepcopy
import json
from zhixi.content.source_identity import source_context


FORMAT = 'evidence_transport_v1'
COMPACT_FORMAT = 'evidence_transport_v2'
SOURCE_COLUMNS = ['source_id', 'quote', 'page_id', 'window', 'context_ref', 'provenance', 'origin']


def source_origin(row):
    return (
        'model_generated_not_original'
        if row.get('type') in {'model_summary', 'model_draft'}
        else 'source_material_not_independently_verified'
    )


def source_bundle(evidence, pages, aliases=None, *, include_prior_judgments=False):
    """Transport originals; historical interpretations are an explicit opt-in.

    Prior readings stay in the research ledger for exploration and inspection.
    Factual writing/review must not receive a guessed relationship beside the
    original merely because the guess carries a 'not evidence' label.
    """
    aliases = aliases or {str(row['id']): str(row['id']) for row in evidence}
    records, candidates, heading_hints = [], [], {}
    for row in evidence:
        identity = str(row['id'])
        metadata = row.get('metadata') or {}
        context = deepcopy(metadata.get('context') or {})
        # Selection receipts contain other ID namespaces and often entire model
        # exchanges. They belong in the stored ledger, not beside citable IDs.
        use = metadata.get('material_use') or {}
        if use.get('purpose') in {'primary', 'context', 'counterevidence'}:
            context['material_use_not_verified'] = {'purpose': use['purpose']}
        # These are diagnostic/derived labels, not original words. Originals and
        # full diagnostics remain in the frozen evidence ledger, not the prompt.
        context.pop('page_title', None)
        regions = context.pop('page_regions', [])
        headings = list(dict.fromkeys(r.get('heading') for r in regions if r.get('heading')))
        if headings:
            refs = []
            for heading in headings:
                if heading not in heading_hints:
                    heading_hints[heading] = f'H{len(heading_hints) + 1}'
                refs.append(heading_hints[heading])
            context['heading_refs'] = refs
        if context.get('parent_quote') == '':
            context.pop('parent_quote')
        source = {'source_id': aliases[identity], 'page_id': row['page_id'], 'quote': row['quote']}
        source['origin'] = source_origin(row)
        provenance = {
            key: metadata[key]
            for key in (
                'comment_id',
                'created_at_unix',
                'likes',
                'replies',
                'sampling_strategy',
                'is_reply',
                'parent_comment_id',
                'sample_page',
                'sample_cursor',
            )
            if key in metadata
        }
        if provenance:
            source['provenance'] = provenance
        span = metadata.get('source_span') or {}
        start, end = span.get('start'), span.get('end')
        before, after = context.get('before', ''), context.get('after', '')
        if (
            type(start) is int
            and type(end) is int
            and isinstance(before, str)
            and isinstance(after, str)
            and start >= len(before)
            and end - start == len(row['quote'])
        ):
            candidates.append(
                {
                    'page_id': row['page_id'],
                    'comment_id': metadata.get('comment_id'),
                    'start': start - len(before),
                    'end': end + len(after),
                    'text': before + row['quote'] + after,
                    'source': source,
                    'quote_start': start,
                    'quote_end': end,
                }
            )
            context.pop('before', None)
            context.pop('after', None)
        source['context_not_quote'] = context
        records.append(source)
    windows = []
    for candidate in sorted(candidates, key=lambda c: (str(c['page_id']), c['start'], c['end'])):
        window = None
        for existing in reversed(windows):
            if (
                existing['page_id'] != candidate['page_id']
                or existing.get('comment_id') != candidate['comment_id']
                or existing['end'] < candidate['start']
            ):
                continue
            overlap_end = min(existing['end'], candidate['end'])
            offset = candidate['start'] - existing['start']
            overlap = overlap_end - candidate['start']
            if offset >= 0 and existing['text'][offset : offset + overlap] == candidate['text'][:overlap]:
                window = existing
                if candidate['end'] > window['end']:
                    window['text'] += candidate['text'][overlap:]
                    window['end'] = candidate['end']
                break
        if window is None:
            window = {
                'window_id': f'C{len(windows) + 1}',
                'page_id': candidate['page_id'],
                'start': candidate['start'],
                'end': candidate['end'],
                'text': candidate['text'],
            }
            if candidate['comment_id'] is not None:
                window['comment_id'] = candidate['comment_id']
            windows.append(window)
        candidate['source']['context_not_quote']['window'] = [
            window['window_id'],
            candidate['start'],
            candidate['end'],
            candidate['quote_start'],
            candidate['quote_end'],
        ]
    page_ids = {row['page_id'] for row in evidence}
    reading_context = {}
    for page_id in page_ids if include_prior_judgments else ():
        hints = (pages.get(page_id) or {}).get('reading_context_not_evidence', [])
        relevant = []
        for hint in hints:
            start, end = hint['offset'], hint['offset'] + hint['characters']
            if any(row['page_id'] == page_id
                   and (hint.get('coordinate_scope') != 'structured_original'
                        or (row.get('metadata') or {}).get('material_use', {}).get('source_span_id') == hint['span_id'])
                   and type((span := (row.get('metadata') or {}).get('source_span') or {}).get('start')) is int
                   and type(span.get('end')) is int and span['start'] < end and span['end'] > start
                   for row in evidence):
                relevant.append(hint)
        if relevant:
            # Optional prior judgments cannot make a single original record
            # untransportable. Preserve whole hints; explicitly expose omission.
            kept = []
            for hint in relevant:
                if len(json.dumps([*kept, hint], ensure_ascii=False)) <= 1600:
                    kept.append(hint)
            reading_context[page_id] = {'judgments': deepcopy(kept),
                                       'omitted_judgments': len(relevant) - len(kept)}
    return {
        'format': FORMAT,
        'pages': {
            key: {
                'title': (pages.get(key) or {}).get('title', ''),
                'url': (pages.get(key) or {}).get('url', ''),
                **({'source_context': context} if (context := source_context(pages.get(key) or {})) else {}),
            }
            for key in pages
            if key in page_ids
        },
        'sources': records,
        'context_windows': windows,
        'heading_hints_not_verified': {value: key for key, value in heading_hints.items()},
        'window_ref_format': ['window_id', 'context_start', 'context_end', 'quote_start', 'quote_end'],
        **({'prior_reading_not_evidence': reading_context} if reading_context else {}),
        **({'text_integrity': {'replacement_characters_present': True,
                              'meaning': '替换字符保留在原文；不能根据数字或相邻词猜补乱码中的对象和条件。'}}
           if any('\ufffd' in row['quote'] for row in evidence) else {}),
        'scope': 'quote是对应材料的原字符；origin为model_generated_not_original时只是模型草稿，不是原文证据。来源材料也不等于独立核实的事实。共享上下文仅用于理解，不能挂在其他source_id上作为出处。',
    }


def compact_bundle(bundle):
    """Losslessly encode repeated field names/context, not select or clean text."""
    if bundle.get('format') != FORMAT:
        raise ValueError('只能压缩明确版本的材料传输')
    compact = {key: deepcopy(value) for key, value in bundle.items() if key != 'sources'}
    contexts, context_keys, records = [], {}, []
    for source in bundle['sources']:
        context = deepcopy(source['context_not_quote'])
        window = context.pop('window', None)
        key = json.dumps(context, ensure_ascii=False, sort_keys=True)
        if key not in context_keys:
            context_keys[key] = len(contexts)
            contexts.append(context)
        records.append(
            [
                source['source_id'],
                source['quote'],
                source['page_id'],
                window,
                context_keys[key],
                deepcopy(source.get('provenance', {})),
                source['origin'],
            ]
        )
    defaults = {}
    for index in (2, 5, 6):
        if records and all(row[index] == records[0][index] for row in records):
            defaults[SOURCE_COLUMNS[index]] = deepcopy(records[0][index])
    columns = [name for name in SOURCE_COLUMNS if name not in defaults]
    records = [[row[SOURCE_COLUMNS.index(name)] for name in columns] for row in records]
    compact.update(
        format=COMPACT_FORMAT,
        sources=records,
        source_columns=columns,
        source_defaults=defaults,
        contexts=contexts,
        record_format='sources每行按source_columns列名读取，source_defaults为各行共同字段；context_ref是contexts的0起始索引；window仍按window_ref_format定位。quote只属于同一行source_id，不属于上下文的其他行。',
    )
    return compact


def expand_bundle(bundle):
    """Decode both persisted legacy envelopes and current transport exactly."""
    if bundle.get('format') == FORMAT:
        return deepcopy(bundle)
    if bundle.get('format') != COMPACT_FORMAT:
        raise ValueError('未知材料传输版本')
    columns, defaults = bundle['source_columns'], bundle['source_defaults']
    if (
        len(columns) != len(set(columns))
        or set(columns) & set(defaults)
        or set(columns) | set(defaults) != set(SOURCE_COLUMNS)
    ):
        raise ValueError('材料列定义不完整或重复')
    sources = []
    for row in bundle['sources']:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError('材料行与列定义不一致')
        value = {**deepcopy(defaults), **dict(zip(columns, deepcopy(row)))}
        ref = value.pop('context_ref')
        if type(ref) is not int or not 0 <= ref < len(bundle['contexts']):
            raise ValueError('材料上下文引用不存在')
        context = deepcopy(bundle['contexts'][ref])
        window = value.pop('window')
        if window is not None:
            context['window'] = window
        provenance = value.pop('provenance')
        value['context_not_quote'] = context
        if provenance:
            value['provenance'] = provenance
        sources.append(value)
    result = {
        key: deepcopy(value)
        for key, value in bundle.items()
        if key not in {'source_columns', 'source_defaults', 'contexts', 'record_format', 'sources'}
    }
    result.update(format=FORMAT, sources=sources)
    return result


def compact_source_bundle(evidence, pages, aliases=None, *, include_prior_judgments=False):
    expanded = source_bundle(evidence, pages, aliases, include_prior_judgments=include_prior_judgments)
    compact = compact_bundle(expanded)

    # For a few rows, the column dictionary itself costs more than it saves.
    # Both envelopes carry the same content and origin boundary.
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

    return compact if len(encode(compact)) < len(encode(expanded)) else expanded


def encode_bundle(evidence, pages, goal):
    return json.dumps(
        {'goal': goal, **compact_source_bundle(evidence, pages)}, ensure_ascii=False, separators=(',', ':')
    )


def check_bundle(bundle, evidence, aliases=None):
    """Lossless transport assertion, deliberately not a semantic support check."""
    aliases = aliases or {str(row['id']): str(row['id']) for row in evidence}
    bundle = expand_bundle(bundle)
    if len(bundle['sources']) != len(evidence):
        return False
    windows = {window['window_id']: window for window in bundle['context_windows']}
    for source, row in zip(bundle['sources'], evidence):
        if (
            source['source_id'] != aliases[str(row['id'])]
            or source['quote'] != row['quote']
            or source['page_id'] != row['page_id']
        ):
            return False
        if source.get('origin') != source_origin(row):
            return False
        context = (row.get('metadata') or {}).get('context') or {}
        location = source['context_not_quote'].get('window')
        if location:
            window = windows[location[0]]
            left, right = location[1] - window['start'], location[2] - window['start']
            span = (row.get('metadata') or {}).get('source_span') or {}
            if (
                location[3:] != [span.get('start'), span.get('end')]
                or left < 0
                or right > len(window['text'])
                or window['page_id'] != row['page_id']
                or window['text'][left:right]
                != context.get('before', '') + row['quote'] + context.get('after', '')
            ):
                return False
        elif any(
            source['context_not_quote'].get(key) != context[key]
            for key in ('before', 'after')
            if key in context
        ):
            return False
        if source['context_not_quote'].get('parent_quote', '') != context.get('parent_quote', ''):
            return False
    return True
