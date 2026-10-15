"""Lossless reading slices for oversized records, not a cleaning operation."""

from copy import deepcopy
from hashlib import sha256


def reading_fragments(evidence, max_characters=2500):
    if max_characters < 1:
        raise ValueError('分段长度必须为正数')
    output, manifest = [], []
    for row in evidence:
        text = row['quote']
        if len(text) <= max_characters:
            output.append(row)
            continue
        parts = []
        for start in range(0, len(text), max_characters):
            end = min(len(text), start + max_characters)
            part = deepcopy(row)
            part['id'] = f'{row["id"]}::chars:{start}:{end}'
            part['quote'] = text[start:end]
            part['claim'] = part['quote']
            meta = part.setdefault('metadata', {})
            span = meta.get('source_span') or {}
            origin = span.get('start')
            meta['source_span'] = (
                {'start': origin + start, 'end': origin + end}
                if type(origin) is int and origin >= 0 and span.get('end') == origin + len(text)
                else {'start': None, 'end': None}
            )
            context = meta.setdefault('context', {})
            if start:
                context.pop('before', None)
            if end < len(text):
                context.pop('after', None)
            context['reading_fragment'] = {
                'original_evidence_id': str(row['id']),
                'start': start,
                'end': end,
                'total_characters': len(text),
                'notice': '同一原始记录的连续片段，不是独立样本；须结合相邻片段，不将截断边界当作句子结尾。',
            }
            meta['reading_fragment'] = dict(context['reading_fragment'])
            parts.append(part)
        assert ''.join(part['quote'] for part in parts) == text
        output.extend(parts)
        manifest.append(
            {
                'original_evidence_id': str(row['id']),
                'characters': len(text),
                'sha256': sha256(text.encode()).hexdigest(),
                'fragment_ids': [part['id'] for part in parts],
                'reconstructed_exactly': True,
            }
        )
    return output, {
        'method': 'lossless_contiguous_reading_fragments_v1',
        'split_records': manifest,
        'stored_originals_modified': False,
        'semantic_completeness': 'not_assessed',
    }
