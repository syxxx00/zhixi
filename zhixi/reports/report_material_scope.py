"""Map reviewed source spans to a report's reading view, not the stored index.

Selection is a model's use judgment, never entity or claim verification. Full
originals remain in the database; clipped records retain occurrence/provenance
and neighbouring conditions. Nothing in this module grants network permission.
"""

from copy import deepcopy
from hashlib import sha256

from zhixi.content.evidence_context import research_original_quotes


def attach_reading_context(pages, contents, records):
    """Carry matched earlier reading judgments, never promote them to sources.

    A saved source-span ID includes the original's hash and offset. Only an
    unchanged span can carry its old interpretation into a new writing view.
    The full records stay in the reading ledger, not duplicated on every quote.
    """
    from zhixi.research.research_reading_state import page_spans

    wanted = {s.get('id') for record in records if record.get('reading_status') == 'accepted'
              for s in record.get('source_spans', [])}
    valid = {span['id']: span for page_id, text in contents.items() if page_id in pages
             for span in page_spans(page_id, '', text) if span['id'] in wanted}
    structured = {page_id for page_id, text in contents.items() if research_original_quotes(text)}
    result = deepcopy(pages)
    seen = set()
    for record in records:
        if record.get('reading_status') != 'accepted':
            continue
        supplied = {s['id']: s for s in record.get('source_spans', [])}
        for observation in (record.get('reading') or {}).get('observations', []):
            identity = observation.get('source_id')
            span = valid.get(identity)
            if not span or supplied.get(identity, {}).get('quote') != span['quote']:
                continue
            kind = observation.get('statement_kind')
            if kind not in {'source_statement', 'interpretation', 'open_question', 'retained_original'}:
                continue
            hint = {'span_id': identity, 'offset': span['offset'], 'characters': len(span['quote']),
                    'statement_kind': kind}
            if span['page_id'] in structured:
                # Comment and media envelopes restart offsets per original.
                # Offset overlap alone must not attach another speaker's note.
                hint['coordinate_scope'] = 'structured_original'
            # An original-retention action does not authorize the model's
            # attempted explanation stored beside it in legacy records.
            if kind != 'retained_original':
                hint.update({key: observation[key] for key in ('subject', 'scope', 'alternatives', 'note')
                             if isinstance(observation.get(key), str) and observation[key]})
            key = repr(hint)
            if key not in seen:
                seen.add(key)
                result[span['page_id']].setdefault('reading_context_not_evidence', []).append(hint)
    return result


def select_materials(evidence, selection, contents):
    if not selection or selection.get('selection_scope') != 'original_spans_v1':
        return evidence
    views, output, deferred, unresolved = {}, [], [], []
    for page_id, text in contents.items():
        originals = research_original_quotes(text) or [text]
        views[page_id] = (text, originals)
    for row in evidence:
        page_id, quote = row['page_id'], row['quote']
        text, originals = views[page_id]
        span = (row.get('metadata') or {}).get('source_span') or {}
        start, end = span.get('start'), span.get('end')
        located = []
        if originals == [text] and type(start) is int and type(end) is int and text[start:end] == quote:
            located = [(0, start)]
        else:
            for index, original in enumerate(originals):
                position = original.find(quote)
                if position >= 0 and original.find(quote, position + 1) < 0:
                    located.append((index, position))
        if len(located) != 1:
            # Structured comments can have identical text but different authors
            # or occurrence coordinates. Alignment failure is not irrelevance.
            unresolved.append(str(row['id']))
            deferred.append({'evidence_id': str(row['id']), 'reason': 'unaligned_original'})
            continue
        index, position = located[0]
        original = originals[index]
        prefix = f'{page_id}:{index}:{sha256(original.encode()).hexdigest()}:'
        used = False
        deferred_reason = 'no_selected_span_overlap'
        for check in selection['span_checks'].get(page_id, []):
            decision, checked = check['decision'], check['span']
            if not checked['id'].startswith(prefix):
                continue
            left = max(position, checked['offset'])
            right = min(position + len(quote), checked['offset'] + checked['characters'], len(original))
            if left >= right:
                continue
            if decision['decision'] == 'unresolved':
                # Keeping an original is not permission to turn it into prose.
                # In particular, an invalid model quote cannot admit a whole
                # navigation list merely because the judgment was inconclusive.
                unresolved.append(str(row['id']))
            if (
                decision['decision'] != 'visit'
                or decision.get('material_role') in {'navigation', 'asset_pointer'}
            ):
                continue
            if 'basis_span_id' in decision or 'resolved_span' in decision:
                resolved = decision.get('resolved_span')
                if (
                    not isinstance(resolved, dict)
                    or resolved.get('id') != checked['id']
                    or decision.get('basis_span_id') != checked['id']
                    or type(resolved.get('offset')) is not int
                    or type(resolved.get('characters')) is not int
                    or resolved['offset'] != checked['offset']
                    or resolved['characters'] != checked['characters']
                    or resolved['offset'] < 0
                    or resolved['characters'] <= 0
                    or resolved['offset'] + resolved['characters'] > len(original)
                    or resolved.get('preview_sha256') != sha256(
                        original[resolved['offset']:resolved['offset'] + resolved['characters']].encode('utf-8')
                    ).hexdigest()
                ):
                    unresolved.append(str(row['id']))
                    deferred_reason = 'invalid_resolved_span'
                    continue
                # Use the resolved selection, not model quote text or a page
                # acceptance flag. Legacy traces without ID transport retain
                # their existing checked-span behavior.
                checked = resolved
                left = max(position, checked['offset'])
                right = min(position + len(quote), checked['offset'] + checked['characters'])
            part = deepcopy(row)
            part['quote'] = original[left:right]
            part['claim'] = part['quote']
            meta = part.setdefault('metadata', {})
            meta['material_use'] = {
                **deepcopy(decision),
                'source_span_id': checked['id'],
                'scope': 'this_span_only_not_other_entities_or_whole_page',
                'semantic_support': 'not_verified',
            }
            if left != position or right != position + len(quote):
                part['id'] = f'{row["id"]}::use:{left - position}:{right - position}'
                meta['selection_fragment'] = {
                    'original_evidence_id': str(row['id']),
                    'start': left - position,
                    'end': right - position,
                    'independent_sample': False,
                }
                # Keep existing comment/parent metadata and original coordinate
                # system. A boundary is not a new sentence or independent fact.
                meta['source_span'] = (
                    {'start': start + left - position, 'end': start + right - position}
                    if type(start) is int and end == start + len(quote)
                    else {'start': None, 'end': None}
                )
                context = meta.setdefault('context', {})
                context['before'] = (context.get('before', '') + quote[: left - position])[-240:]
                context['after'] = (quote[right - position :] + context.get('after', ''))[:240]
                context['selection_boundary'] = '仅按阅读用途分片；邻近条件仍需理解，不把分片作为独立样本。'
            output.append(part)
            used = True
        if not used:
            deferred.append({'evidence_id': str(row['id']), 'reason': deferred_reason})
    selection['material_view'] = {
        'included_records': len(output),
        'deferred_records': deferred,
        'unresolved_evidence_ids': list(dict.fromkeys(unresolved)),
        'stored_originals_modified': False,
        'meaning': 'reading_scope_not_semantic_verification',
    }
    return output
