"""Bounded output-limit recovery; never accept a truncated completion."""

import json
from zhixi.sources.source_queries import output_length_failure


def compact_retry(call, corpus, phase, index, total, failures, initial_error=None):
    """Keep the complete input and original schema, retry once with concise prose.

    This belongs outside the checkpoint callback: each actual attempt has its
    own exact-input checkpoint and raw capture. Failed results are not cached.
    """
    try:
        if initial_error is not None:
            raise initial_error
        return call(corpus, phase, index, total)
    except RuntimeError as exc:
        if not output_length_failure(exc):
            raise
        record = {'phase': phase, 'index': index, 'total': total, 'error': str(exc), 'status': 'retrying'}
        failures.append(record)
    payload = json.loads(corpus)
    payload['output_recovery_instruction'] = (
        '上次输出达到长度上限，未被采用。保留原定JSON结构；简洁表达、避免复述输入和重复解释。'
        '不得遗漏要求逐项返回的编号、必要条件或来源，不得以简洁为由猜测、删除材料或改变结论。'
    )
    try:
        result = call(json.dumps(payload, ensure_ascii=False), phase, index, total)
    except RuntimeError as exc:
        if not output_length_failure(exc):
            raise
        record.update(status='unresolved', retry_error=str(exc))
        return {
            'output_limit_unresolved': True,
            'parsing_error': '输出超长且紧凑重试仍未完成；未采用截断输出',
            'output_recovery': dict(record),
        }
    record['status'] = 'retried_not_semantically_verified'
    return result


def assign_with_recovery(call, corpus, index, total, failures):
    """Batch assignment can split; a failed singleton cannot be auto-archived."""
    try:
        return call(corpus, 'organization:repair', index, total)
    except RuntimeError as exc:
        if not output_length_failure(exc):
            raise
        data = json.loads(corpus)
        paragraphs = data.get('paragraphs')
        if not isinstance(paragraphs, list) or len(paragraphs) < 2:
            raw = compact_retry(call, corpus, 'organization:repair', index, total, failures, exc)
            if raw.get('output_limit_unresolved'):
                raise ValueError(raw['parsing_error'])
            return raw
        failures.append(
            {
                'phase': 'organization:repair',
                'index': index,
                'error': str(exc),
                'status': 'split',
                'paragraph_ids': [p['paragraph_id'] for p in paragraphs],
            }
        )
    middle = len(paragraphs) // 2
    results = [
        assign_with_recovery(
            call, json.dumps({**data, 'paragraphs': group}, ensure_ascii=False), index, total, failures
        )
        for group in (paragraphs[:middle], paragraphs[middle:])
    ]
    # Parent validator still checks every original ID, range and duplicate.
    return {
        'assignments': [item for result in results for item in result.get('assignments', [])],
        'split_results': results,
    }
