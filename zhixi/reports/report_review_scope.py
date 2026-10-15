"""Provenance-led review scopes, never keyword truth filters."""

from zhixi.reports.grounding_context import retrieve_context_records


def rewrite_context_ids(owner, field, reviews, evidence):
    """Keep a unit's acquisition scope distinct from a reviewer's support vote.

    These IDs authorize reading, never retaining a claim. The production caller
    enables this for explicit focused/repair scopes only, not legacy full scans.
    Direct experimental use can fall back to this unit's already-read batches.
    Existing writer packing handles size.
    """
    known = {str(row['id']) for row in evidence}
    selected = {ref['evidence_id'] for review in reviews if not review.get('error')
                for ref in review.get('excerpts', [])} & known
    original_scope = citation_scopes([('unit', '', owner, field)], evidence)['unit']['evidence_ids']
    scoped = set(original_scope)
    for review in reviews:
        if not review.get('error'):
            scoped.update(review.get('review_scope', {}).get('evidence_ids', []))
    basis = 'unit_citations_and_review_scope'
    if not scoped:
        scoped = {identity for review in reviews if not review.get('error')
                  for identity in review.get('evidence_ids', [])}
        basis = 'already_read_unit_batches'
    kept = selected | (scoped & known)
    return kept, {'policy': 'read_scope_trial_v1', 'basis': basis,
                  'model_selected_ids': sorted(selected), 'read_scope_ids': sorted(scoped & known),
                  'added_context_ids': sorted(kept - selected), 'semantic_verification': False}


def citation_scopes(units, evidence):
    known = {str(r['id']) for r in evidence}

    def citations(node):
        # These are retrieval candidates, not proofs. Editorial summaries use
        # proposed_citations until their newly composed prose has been checked.
        values = {
            str(v) for key in ('citations', 'proposed_citations', 'candidate_citations')
            for v in node.get(key, []) if str(v) in known
        }
        for child in node.get('children') or []:
            values.update(citations(child))
        return values

    scopes = {}
    all_cited = set().union(*(citations(owner) for _, _, owner, _ in units))
    for path, heading, owner, field in units:
        ids = all_cited if field == 'summary' else citations(owner)
        basis = 'report_citations' if field == 'summary' else 'unit_and_detail_citations'
        if field == 'summary':
            from zhixi.reports.grounding_statements import unit_bindings

            bindings = unit_bindings(owner, field, known)
            selected = {ref['evidence_id'] for b in bindings for ref in b['references']}
            if selected:
                ids, basis = selected, 'current_summary_selected_sources_not_verification'
        rows, context = retrieve_context_records(evidence, ids) if ids else ([], {})
        scopes[path] = {
            'evidence_ids': [str(r['id']) for r in rows],
            'basis': basis,
            'context': context,
            'whole_corpus_claim_allowed': False,
        }
    return scopes


def focused_reviews(units, evidence, pages, aliases, goal, budget, call, resolve, notes):
    from zhixi.reports.report_shared_review import shared_reviews

    scopes = citation_scopes(units, evidence)
    groups = {}
    for unit in units:
        ids = tuple(scopes[unit[0]]['evidence_ids'])
        groups.setdefault(ids, []).append(unit)
    result = {}
    group_index = 0
    group_total = sum(bool(ids) for ids in groups)
    for ids, group in groups.items():
        if not ids:
            for path, *_ in group:
                result[path] = [
                    {
                        'error': '草稿未关联可追溯原文，不能核实；不代表全库没有依据',
                        'evidence_ids': [],
                        'review_scope': scopes[path],
                    }
                ]
            continue
        group_index += 1

        def group_call(text, phase, index, total):
            # Local batches (and context retries) belong to this source group.
            # Report the position in the full review, not a fresh 1/1 per group.
            return call(text, phase, group_index, group_total)

        wanted = set(ids)
        rows = [r for r in evidence if str(r['id']) in wanted]
        reviewed = shared_reviews(
            group,
            rows,
            pages,
            aliases,
            goal,
            budget,
            group_call,
            resolve,
            notes,
            review_policy='citation_first_v1: local absence is not global absence',
        )
        for unit in group:
            path = unit[0]
            entries = reviewed.get(path)
            if entries is None:
                # Do not send an oversized unit back into the old full scan.
                entries = [{'error': '完整草稿及其依据超过核对容量，原稿保留', 'evidence_ids': list(ids)}]
            for entry in entries:
                entry['review_scope'] = scopes[path]
            result[path] = entries
            refs = [r for e in entries for r in e.get('excerpts', [])]
            needs_more = not any(r['relation'] == 'supports' for r in refs) or any(
                r['relation'] == 'contradicts' for r in refs
            )
            if not needs_more or any(e.get('error') for e in entries):
                continue
            # A disputed passage warrants its own context, not every unrelated
            # record on the same page. Further reading remains explicit.
            neighbors, _ = retrieve_context_records(evidence, {r['evidence_id'] for r in refs})
            extra = [r for r in neighbors if str(r['id']) not in wanted]
            if extra:
                expansion = shared_reviews(
                    [unit],
                    extra,
                    pages,
                    aliases,
                    goal,
                    budget,
                    group_call,
                    resolve,
                    notes,
                    review_policy='citation_first_v1: referenced context, not whole page',
                )
                for entry in expansion.get(path, [{'error': '扩展原文超过核对容量', 'evidence_ids': []}]):
                    entry['review_scope'] = {
                        'basis': 'referenced_context_expansion',
                        'reason': 'no_support_or_conflict',
                        'whole_corpus_claim_allowed': False,
                    }
                    entries.append(entry)
    return result
