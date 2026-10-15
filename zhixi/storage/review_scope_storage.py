"""Lossless storage references for shared review scopes, not evidence filtering."""

import json


def compact_review_scopes(ledger):
    """Store each shared scope once; keep legacy reports readable via resolve_scope."""
    pool = ledger.setdefault('review_scopes', {})
    identities = {id(value): key for key, value in pool.items()}
    contents = {json.dumps(value, sort_keys=True, ensure_ascii=False): key for key, value in pool.items()}
    for unit in ledger.get('units', []):
        for review in unit.get('reviews', []):
            scope = review.get('review_scope')
            if not isinstance(scope, dict):
                if 'review_scope_ref' in review:
                    resolve_scope(ledger, review)  # Reject dangling references.
                continue
            serialized = json.dumps(scope, sort_keys=True, ensure_ascii=False)
            key = identities.get(id(scope)) or contents.get(serialized)
            if key is None:
                number = len(pool) + 1
                key = f'scope:{number}'
                while key in pool:
                    number += 1
                    key = f'scope:{number}'
                pool[key] = scope
                identities[id(scope)] = key
                contents[serialized] = key
            review['review_scope_ref'] = key
            del review['review_scope']
    ledger['scope_storage'] = 'shared_review_scopes_v1'
    return ledger


def resolve_scope(ledger, review):
    """Resolve one requested record only; never expand an entire report on read."""
    legacy = review.get('review_scope')
    if isinstance(legacy, dict):
        return legacy
    key = review.get('review_scope_ref')
    if key is None:
        return None
    pool = ledger.get('review_scopes', {})
    if not isinstance(key, str) or key not in pool:
        raise ValueError('复核范围引用缺失，不能视为已保存完整追溯记录')
    return pool[key]
