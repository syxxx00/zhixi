"""Retrieve existing original records from visible context, never invent citations.

Expand the explicitly requested records' visible context once. Neighbours do
not recursively authorize their own neighbours. A later explicit read can ask
for another record's context. Offsets locate text, not factual support.
"""

from hashlib import sha256
import json

from zhixi.content.evidence_transport import source_origin, source_bundle


def retrieve_context_records(evidence, selected_ids):
    selected_ids = set(selected_ids)
    by_id = {str(row['id']): row for row in evidence}
    if len(by_id) != len(evidence) or not selected_ids <= by_id.keys():
        raise ValueError('上下文取回需要唯一且存在的原始材料编号')
    windows, unlocated = {}, []

    if any(source_origin(by_id[identity]) == 'model_generated_not_original' for identity in selected_ids):
        raise ValueError('不能从模型草稿构建原始上下文出处')

    def add_windows(retained):
        bundle = source_bundle([row for identity, row in by_id.items() if identity in retained], {})
        for source in bundle['sources']:
            if not source['context_not_quote'].get('window') and source['source_id'] not in unlocated:
                unlocated.append(source['source_id'])
        for original in bundle['context_windows']:
            descriptor = {
                'page_id': original['page_id'],
                'comment_id': original.get('comment_id'),
                'start': original['start'],
                'end': original['end'],
                'text_sha256': sha256(original['text'].encode('utf-8')).hexdigest(),
            }
            key = sha256(
                json.dumps(descriptor, sort_keys=True, ensure_ascii=False).encode('utf-8')
            ).hexdigest()
            window = windows.setdefault(
                key, {**descriptor, 'context_id': key, 'text': original['text'], 'trigger_evidence_ids': []}
            )
            for source in bundle['sources']:
                ref = source['context_not_quote'].get('window')
                if (
                    ref
                    and ref[0] == original['window_id']
                    and source['source_id'] not in window['trigger_evidence_ids']
                ):
                    window['trigger_evidence_ids'].append(source['source_id'])

    additions, rounds = [], []
    retained = set(selected_ids)
    pending = [identity for identity in by_id if identity in retained]
    if pending:
        add_windows(retained)
        discovered = []
        for identity, row in by_id.items():
            if identity in retained or source_origin(row) == 'model_generated_not_original':
                continue
            metadata = row.get('metadata') or {}
            span = metadata.get('source_span') or {}
            start, end = span.get('start'), span.get('end')
            quote = row['quote']
            if not (
                quote
                and type(start) is int
                and type(end) is int
                and 0 <= start < end
                and end - start == len(quote)
            ):
                continue
            matches = [
                window['context_id']
                for window in windows.values()
                if row['page_id'] == window['page_id']
                and metadata.get('comment_id') == window['comment_id']
                and window['start'] <= start < end <= window['end']
                and window['text'][start - window['start'] : end - window['start']] == quote
            ]
            if matches:
                additions.append(
                    {
                        'evidence_id': identity,
                        'page_id': row['page_id'],
                        'source_span': {'start': start, 'end': end},
                        'context_ids': matches,
                        'retrieval_round': len(rounds) + 1,
                    }
                )
                discovered.append(identity)
        rounds.append({'context_record_ids': pending, 'additional_evidence_ids': discovered})
        retained.update(discovered)
    return [row for row in evidence if str(row['id']) in retained], {
        'method': 'explicit_original_context_v3',
        'scope': 'selected_records_visible_windows_not_transitive_or_semantic_support',
        'semantic_support': 'not_assessed',
        'selected_evidence_ids': [identity for identity in by_id if identity in selected_ids],
        'unlocated_context_evidence_ids': unlocated,
        'rounds': rounds,
        'closure_complete': False,
        'requested_windows_complete': True,
        'further_context_requires_explicit_read': True,
        'windows': [{k: v for k, v in window.items() if k != 'text'} for window in windows.values()],
        'additional_records': additions,
    }
