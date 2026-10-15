"""Bounded whole-report outline reduction; detailed drafts remain elsewhere."""

from copy import deepcopy
from hashlib import sha256
import json

from zhixi.reports.report_organization import (
    organization_input, paragraph_catalog, apply_organization, missing_assignment_input,
    merge_assignments, single_assignment_inputs, batch_assignment_inputs, checked_batch_assignments,
    merged_source_basis,
    prose_slice,
)
from zhixi.sources.source_queries import output_length_failure


def recover_outline(corpus, phase, index, total, analyze, trace, runtimes, level, depth=0, *, composition_catalog=None):
    """Retry only length failures, transporting all prose in bounded subdivisions."""
    try:
        raw = analyze(corpus, phase, index, total)
    except RuntimeError as exc:
        if not output_length_failure(exc):
            raise
        payload = json.loads(corpus)
        paragraphs = payload['paragraphs']
        entry = {
            'level': level,
            'index': index,
            'input': corpus,
            'error': str(exc),
            'depth': depth,
            'status': 'split',
        }
        trace.setdefault('output_recovery', []).append(entry)
        if depth >= 3 or not paragraphs:
            entry['status'] = 'unresolved'
            return None
        if len(paragraphs) > 1:
            middle = len(paragraphs) // 2
            groups = [paragraphs[:middle], paragraphs[middle:]]
        else:
            item = paragraphs[0]
            text = item['text']
            if len(text) < 2:
                entry['status'] = 'unresolved'
                return None
            middle = len(text) // 2
            groups = [[prose_slice(item, 0, middle)], [prose_slice(item, middle, len(text))]]
        results = []
        for group in groups:
            child = json.dumps({**payload, 'paragraphs': group}, ensure_ascii=False)
            parts = recover_outline(child, phase, index, total, analyze, trace, runtimes, level, depth + 1,
                                    composition_catalog=composition_catalog)
            if parts is None:
                return None
            results.extend(parts)
        return results
    # New composition producers return prose, not an outline. Keep the actual
    # response in the trace and adapt only the consumer's in-memory shape.
    runtimes.append(raw.get('runtime') or {})
    trace['steps'].append({'level': level, 'index': index, 'input': corpus, 'result': deepcopy(raw)})
    if composition_catalog is not None:
        from zhixi.reports.report_composition import render

        try:
            raw = render(raw, json.loads(corpus), composition_catalog)
        except ValueError as exc:
            trace['steps'][-1]['composition_error'] = str(exc)
            return None
    if json.loads(corpus).get('composition_scope') and isinstance(raw.get('title'), str):
        raw = {**raw, 'sections': [{'title': raw['title'], 'explanation': raw.get('summary', '')}]}
    return [raw] if valid_outline(raw) else None


def outline_input(items, goal, composition_scope=None):
    data = json.loads(organization_input(items, goal))
    data['scope'] = '仅本次输入的草稿主题；不能由局部材料宣称全篇缺失或研究完成；非原文证据'
    if composition_scope:
        from zhixi.reports.report_composition import PROTOCOL

        data['composition_scope'] = composition_scope
        data['composition_protocol'] = PROTOCOL
        for paragraph, item in zip(data['paragraphs'], items):
            paragraph.update(prose_id=item.get('id', ''), fragment_start=item.get('fragment_start', 0))
    return json.dumps(data, ensure_ascii=False)


def valid_outline(raw):
    """Structural eligibility only, not a claim of semantic correctness."""
    if not isinstance(raw, dict):
        return False
    sections = raw.get('sections')
    return (
        isinstance(raw.get('summary'), str)
        and bool(raw['summary'].strip())
        and isinstance(sections, list)
        and bool(sections)
        and all(
            isinstance(s, dict)
            and isinstance(s.get('title'), str)
            and bool(s['title'].strip())
            and isinstance(s.get('explanation'), str)
            and bool(s['explanation'].strip())
            for s in sections
        )
    )


def outline_batches(catalog, goal, budget, composition_scope=None):
    """Transport every character, splitting oversized prose without discarding it."""
    group = []
    for item in catalog:
        remaining = item['text']
        start = 0
        pieces = []
        if len(outline_input([item], goal, composition_scope)) <= budget:
            pieces.append(item)
        else:
            while remaining:
                low, high = 0, len(remaining)
                while low < high:
                    middle = (low + high + 1) // 2
                    candidate = prose_slice(item, start, start + middle)
                    if len(outline_input([candidate], goal, composition_scope)) <= budget:
                        low = middle
                    else:
                        high = middle - 1
                if not low:
                    raise ValueError('编排输入中的问题或标题超过单次容量，完整草稿已保留')
                pieces.append(prose_slice(item, start, start + low))
                remaining = remaining[low:]
                start += low
        for piece in pieces:
            if group and len(outline_input([*group, piece], goal, composition_scope)) > budget:
                yield outline_input(group, goal, composition_scope)
                group = []
            group.append(piece)
    if group:
        yield outline_input(group, goal, composition_scope)


def organize_outline(catalog, goal, budget, analyze, initial_corpus=None, *, composition_scope=None):
    """Reduce all draft themes before routing all original detailed paragraphs.

    Intermediate outlines are not evidence or replacements for the catalog.
    Non-shrinking or malformed model output stops this stage honestly; model
    transport/cancellation errors propagate to the existing job recovery path.
    """
    trace = {'steps': [], 'basis': 'model_drafts_not_original_evidence'}
    runtimes = []
    current = deepcopy(catalog)
    level = 0
    while True:
        composition_catalog = None
        if composition_scope:
            # Compact input IDs refer to a program-side catalog. Large source
            # quotes and transitive dependency receipts are not resent to the
            # editor or used as model-authored evidence.
            for i, item in enumerate(current):
                item['id'] = f'C{level}_{i}'
            composition_catalog = {item['id']: item for item in current}
        try:
            inputs = (
                [initial_corpus]
                if level == 0 and initial_corpus is not None
                else list(outline_batches(current, goal, budget, composition_scope))
            )
        except ValueError as exc:
            trace.update(status='invalid', reason=str(exc))
            return None, trace, runtimes
        if not inputs:
            trace.update(status='invalid', reason='没有可整体编排的草稿段落')
            return None, trace, runtimes
        partials = []
        for index, corpus in enumerate(inputs, 1):
            from zhixi.reports.report_composition import PHASE_PROTOCOL

            phase = ('organization:compose:' + PHASE_PROTOCOL + ':' + composition_scope + f':{level}' if composition_scope else
                     'organization' if len(inputs) == 1 else f'organization:outline:{level}')
            parts = recover_outline(corpus, phase, index, len(inputs), analyze, trace, runtimes, level,
                                    composition_catalog=composition_catalog)
            if parts is None:
                trace.update(
                    status='invalid', reason='整体编排未返回完整章节与解释（含超长恢复未完成），原草稿已保留'
                )
                return None, trace, runtimes
            # Provenance describes this input window, not factual support for
            # a generated sentence. Carry it through transport reductions too.
            data = json.loads(corpus)
            contexts = data.get('source_basis_contexts_not_verified', [])
            basis = merged_source_basis(contexts[p['source_basis_ref']] for p in data['paragraphs']
                                        if 'source_basis_ref' in p)
            partials.extend((part, basis) for part in parts)
        if len(partials) == 1:
            trace.update(status='outlined', reduction_levels=level)
            return partials[0][0], trace, runtimes
        reduced = []
        for index, (partial, basis) in enumerate(partials, 1):
            reviewed = {'reviewed_fields': {'source_basis_not_verified': basis}} if basis else {}
            if composition_scope and partial.get('composition_attribution'):
                from zhixi.reports.grounding_statements import statement_bindings

                receipt = partial['composition_attribution']
                ids = list(dict.fromkeys(ref['evidence_id'] for record in receipt['statements']
                                         for ref in record['excerpts']))
                reduced.append({'heading': partial['title'], 'text': partial['summary'], 'batch': index,
                                'citations': ids,
                                'statement_bindings_not_verified': statement_bindings(partial['summary'], receipt, set(ids)),
                                'reviewed_fields': {**reviewed.get('reviewed_fields', {}),
                                                    'composition_attribution': receipt}})
                continue  # One actual composed overview, not its duplicate section copy.
            reduced.append(
                {'heading': '局部草稿概览（非原文证据）', 'text': partial['summary'], 'batch': index, **reviewed}
            )
            reduced.extend(
                {'heading': section['title'], 'text': section['explanation'], 'batch': index, **reviewed}
                for section in partial['sections']
            )
        # New receipts add fixed addressing overhead; compare actual prose
        # progress, not the size of evidence/dependency metadata. Each next
        # invocation still has to fit its bounded transport envelope.
        before_size = (sum(len(p['text']) for p in current) if composition_scope else
                       len(organization_input(current, goal)))
        after_size = (sum(len(p['text']) for p in reduced) if composition_scope else
                      len(organization_input(reduced, goal)))
        if after_size >= before_size:
            trace.update(status='invalid', reason='跨批编排未缩小输入，停止重复归纳；完整草稿已保留')
            return None, trace, runtimes
        current = reduced
        level += 1


def compose_assigned_overviews(report, goal, budget, analyze, runtimes, trace, *, preserve_grouping=False):
    """Write from actual assigned bodies, never publish the routing proposal.

    One-paragraph chapters need no second paraphrase. Larger inputs use the
    existing bounded transport/recovery, with the same cancellation adapter.
    A failed composition leaves the original report with the caller, not a
    partly updated mixture of new bodies and an old causal overview.
    """
    trace.update(basis='actual_assigned_body_not_routing_outline', chapters=[])

    def body_catalog(details, prefix):
        catalog = paragraph_catalog([{'result': {'tree': {'children': details}}}])
        for item in catalog:
            item['report_path'] = prefix + item['path'][1:]
        return catalog

    for index, chapter in enumerate(report['tree']['children'], 1):
        details = chapter.get('children') or []
        if not details:
            continue  # Single-paragraph report: apply_organization copied it.
        if len(details) == 1:
            if preserve_grouping:
                chapter['note'] = ''
                chapter.pop('reader_details', None)
                trace['chapters'].append({'index': index, 'status': 'single_body_no_duplicate_overview'})
                continue
            chapter['note'] = details[0]['note']
            chapter['label'] = details[0]['label']
            chapter['citations'] = deepcopy(details[0].get('citations', []))
            if details[0].get('statement_attribution'):
                chapter['statement_attribution'] = deepcopy(details[0]['statement_attribution'])
            copied = details[0].get('composition_attribution') or details[0].get('statement_attribution')
            if copied and copied.get('text_sha256') == sha256(chapter['note'].encode()).hexdigest():
                from zhixi.reports.report_composition import PROTOCOL

                receipt = deepcopy(copied)
                receipt.update(protocol=PROTOCOL, meaning='copied_prose_addresses_not_verification')
                for record in receipt.get('statements', []):
                    record['dependencies'] = [{'path': f'tree.{index - 1}.0',
                        'text_sha256': receipt['text_sha256'], 'start': record.get('start'), 'end': record.get('end')}]
                    record.pop('input_selections', None)
                chapter['composition_attribution'] = receipt
            if details[0].get('source_basis_not_verified'):
                chapter['source_basis_not_verified'] = deepcopy(details[0]['source_basis_not_verified'])
            trace['chapters'].append({'index': index, 'status': 'copied_single_body'})
            continue
        catalog = body_catalog(details, f'tree.{index - 1}')
        basis = merged_source_basis(p['reviewed_fields'].get('source_basis_not_verified') for p in catalog)
        chapter['citations'] = list(dict.fromkeys(identity for p in catalog for identity in p['citations']))
        if basis:
            chapter['source_basis_not_verified'] = basis
        written, receipt, calls = organize_outline(
            catalog, goal, budget, analyze, composition_scope='assigned_chapter',
        )
        runtimes.extend(calls)
        trace['chapters'].append({'index': index, 'paragraph_ids': chapter.get('paragraph_ids', []), 'composition': receipt})
        if written is None:
            if preserve_grouping:
                chapter['note'] = ''  # No invented preface; actual body remains.
                chapter.pop('reader_details', None)
                trace['chapters'][-1]['status'] = 'body_preserved_without_overview'
                continue
            raise ValueError('章节概括未完成，未沿用归组前的概览；完整详细正文保留')
        if len(written['sections']) == 1:
            chapter['label'] = written['sections'][0]['title']
            chapter['note'] = written['sections'][0]['explanation']
        else:
            # Compatible API/older producer: a complete summary of the actual
            # body is usable even when the model additionally lists subtopics.
            # Do not discard all grouping or silently select just one subtopic.
            chapter['note'] = written['summary']
        if written.get('composition_attribution'):
            chapter['composition_attribution'] = deepcopy(written['composition_attribution'])
            chapter['citations'] = list(dict.fromkeys(ref['evidence_id']
                for statement in written['composition_attribution']['statements'] for ref in statement['excerpts']))
            chapter.pop('statement_attribution', None)
    summaries = []
    for i, chapter in enumerate(report['tree']['children'], 1):
        if chapter.get('note'):
            from zhixi.reports.grounding_statements import unit_bindings

            summaries.append({'heading': chapter['label'], 'text': chapter['note'], 'batch': i,
                              'report_path': f'tree.{i - 1}', 'citations': deepcopy(chapter.get('citations', [])),
                              'statement_bindings_not_verified': unit_bindings(
                                  chapter, 'note', set(chapter.get('citations', []))),
                              'reviewed_fields': {key: deepcopy(chapter[key]) for key in (
                                  'statement_attribution', 'composition_attribution', 'source_basis_not_verified')
                                  if key in chapter}})
        else:
            # Failed prefaces cannot erase the chapter from the final answer.
            summaries.extend(body_catalog(chapter.get('children', []), f'tree.{i - 1}'))
    written, receipt, calls = organize_outline(
        summaries, goal, budget, analyze, composition_scope='report_summary',
    )
    runtimes.extend(calls)
    trace['summary'] = receipt
    if written is None:
        if preserve_grouping:
            report['summary'] = ''
            trace['status'] = 'body_preserved_without_summary'
            return
        raise ValueError('报告概览未完成，未采用归组前的旧概览；完整详细正文保留')
    report['summary'] = written['summary']
    report.pop('summary_attribution', None)
    report.pop('summary_composition_attribution', None)
    if written.get('composition_attribution'):
        report['summary_composition_attribution'] = deepcopy(written['composition_attribution'])


def annotate_source_basis(catalog, evidence, pages):
    """Attach captured identity, not a source-support certificate."""
    from zhixi.content.source_identity import source_context
    from zhixi.reports.grounding_statements import statement_bindings

    originals = {str(row['id']): row for row in evidence}
    for item in catalog:
        sources = {}
        for identity in item['citations']:
            row = originals.get(str(identity))
            if not row or row['page_id'] in sources:
                continue
            page = (pages or {}).get(row['page_id'], {})
            sources[row['page_id']] = {'page_id': row['page_id'], 'title': page.get('title', ''),
                                      'url': page.get('url', ''), **source_context(page)}
        attribution = (item['reviewed_fields'].get('statement_attribution')
                       or item['reviewed_fields'].get('composition_attribution') or {})
        if attribution.get('text_sha256') != sha256(item['text'].encode('utf-8')).hexdigest():
            attribution = {}
        kinds = list(dict.fromkeys(s.get('kind') for s in attribution.get('statements', []) if s.get('kind')))
        if sources or kinds:
            item['reviewed_fields']['source_basis_not_verified'] = {
                'sources': list(sources.values()), 'statement_kinds': kinds,
                'evidence_pages': {str(identity): originals[str(identity)]['page_id']
                                   for identity in item['citations'] if str(identity) in originals},
                'meaning': 'captured_provenance_and_model_labels_not_independent_verification',
            }
        else:
            item['reviewed_fields'].pop('source_basis_not_verified', None)
        item['statement_bindings_not_verified'] = statement_bindings(item['text'], attribution, originals)


def compose_material_report(result, goal, budget, analyze, *, evidence=(), pages=None):
    """Finish prearranged topic bodies, without another global prose outline."""
    runtimes, trace = [], {}
    composed = deepcopy(result)
    for chapter in composed['tree']['children']:
        catalog = paragraph_catalog([{'result': {'tree': {'children': chapter.get('children', [])}}}])
        annotate_source_basis(catalog, evidence, pages)
        basis = merged_source_basis(p['reviewed_fields'].get('source_basis_not_verified') for p in catalog)
        if basis:
            chapter['source_basis_not_verified'] = basis
        def attach(nodes):
            for node in nodes:
                match = next((p for p in catalog if p['heading'] == node.get('label')
                              and p['text'] == node.get('note') and p['citations'] == node.get('citations', [])), None)
                if match and match['reviewed_fields'].get('source_basis_not_verified'):
                    node['source_basis_not_verified'] = deepcopy(match['reviewed_fields']['source_basis_not_verified'])
                attach(node.get('children') or [])
        attach(chapter.get('children') or [])
    composed['tree']['children'] = [c for c in composed['tree']['children'] if c.get('children') or c.get('note')]
    compose_assigned_overviews(composed, goal, budget, analyze, runtimes, trace, preserve_grouping=True)
    result.update(tree=composed['tree'], summary=composed['summary'], insights=[])
    result.pop('summary_attribution', None)
    result.pop('summary_composition_attribution', None)
    if composed.get('summary_composition_attribution'):
        result['summary_composition_attribution'] = composed['summary_composition_attribution']
    result['summary_support_status'] = 'model_overview_of_retained_topic_bodies'
    result['report_organization'] = {
        'status': 'organized' if composed['summary'] and not any(
                      c.get('status') == 'body_preserved_without_overview' for c in trace['chapters'])
                  else 'grouped_body_preserved',
        'basis': 'neutral_material_topics_then_actual_reviewed_body',
        'section_count': len(composed['tree']['children']), 'assigned_composition': trace,
    }
    return result, runtimes


def organize_for_reader(result, goal, budget, analyze, *, evidence=(), pages=None):
    """Compose once, from retained reviewed prose rather than unreviewed batches."""
    runtimes = []
    drafts = [{"result": result}]
    catalog = paragraph_catalog(drafts)
    if not catalog:
        return result, runtimes
    annotate_source_basis(catalog, evidence, pages)
    organization_text = organization_input(catalog, goal)
    organization = {
        'status': 'pending',
        'paragraph_count': len(catalog),
        'characters': len(organization_text),
        'basis': 'retained_reviewed_prose_not_original_evidence',
    }
    if len(organization_text) > budget:
        organized, outline_trace, outline_runtimes = organize_outline(catalog, goal, budget, analyze)
        runtimes.extend(outline_runtimes)
        organization['outline_reduction'] = outline_trace
        if organized is None:
            organization.update(status='invalid', reason=outline_trace['reason'])
    else:
        organized, outline_trace, outline_runtimes = organize_outline(
            catalog, goal, budget, analyze, initial_corpus=organization_text
        )
        runtimes.extend(outline_runtimes)
        organization['outline_reduction'] = outline_trace
        if organized is None:
            organization.update(status='invalid', reason=outline_trace['reason'])
    if organized is not None:
        organization['raw_result'] = deepcopy(organized)
        try:
            organized.pop('deferred_paragraphs', None)
            for section in organized.get('sections') or []:
                if isinstance(section, dict):
                    # Outline writing has no assignment authority. IDs are
                    # obtained only from the separate complete routing stage.
                    section['paragraph_ids'] = []
            if len(catalog) == 1 and len(organized.get('sections', [])) == 1:
                organized['sections'][0]['paragraph_ids'] = [catalog[0]['id']]
            missing_input = missing_assignment_input(organized, catalog, goal)
            if missing_input:
                repairs = []
                groups = list(batch_assignment_inputs(missing_input, character_budget=min(budget, 6000)))
                assignments = {}
                unresolved = []
                from zhixi.reports.report_output_recovery import assign_with_recovery

                assignment_failures = organization.setdefault('output_recovery', [])
                for index, (paragraphs, group) in enumerate(groups, 1):
                    item = assign_with_recovery(analyze, group, index, len(groups), assignment_failures)
                    runtimes.append(item.get('runtime') or {})
                    accepted, pending = checked_batch_assignments(
                        item, paragraphs, len(organized['sections'])
                    )
                    assignments.update(accepted)
                    unresolved.extend(pending)
                    repairs.append(
                        {
                            'paragraph_ids': [p['id'] for p in paragraphs],
                            'result': deepcopy(item),
                            'accepted_ids': list(accepted),
                            'pending_ids': [p['id'] for p in pending],
                        }
                    )
                # Retry only invalid/missing assignments; valid batch entries
                # are retained. Full drafts and every raw attempt remain inspectable.
                repair_request = json.loads(missing_input)
                repair_request['missing_paragraphs'] = unresolved
                singles = list(single_assignment_inputs(json.dumps(repair_request, ensure_ascii=False)))
                for index, (identifier, group) in enumerate(singles, 1):
                    item = assign_with_recovery(
                        analyze,
                        group,
                        len(groups) + index,
                        len(groups) + len(singles),
                        assignment_failures,
                    )
                    runtimes.append(item.get('runtime') or {})
                    assignments[identifier] = (
                        {'section_index': -1, 'reason': item.get('reason')}
                        if item.get('section_index') == -1
                        else item.get('section_index')
                    )
                    repairs.append(
                        {'paragraph_id': identifier, 'result': deepcopy(item), 'fallback': True}
                    )
                organization['assignment_repair'] = deepcopy(repairs)
                organization['assignment_transport'] = {
                    'protocol': 'batch_v1',
                    'batch_calls': len(groups),
                    'single_fallback_calls': len(singles),
                    'semantic_routing': 'not_verified',
                }
                repair = {
                    'assignments': [
                        {
                            'paragraph_id': p['id'],
                            **(
                                assignments[p['id']]
                                if isinstance(assignments[p['id']], dict)
                                else {'section_index': assignments[p['id']]}
                            ),
                        }
                        for p in catalog
                    ]
                }
                organized = merge_assignments(organized, repair, catalog)
            composed = apply_organization(organized, catalog, goal)
            composition = organization.setdefault('assigned_composition', {})
            compose_assigned_overviews(
                composed, goal, budget, analyze, runtimes, composition,
            )
            result.update(composed)
            result['summary_support_status'] = 'model_overview_of_reviewed_paragraphs'
            # Interpretations are now integrated into the global section prose;
            # all prior insight drafts remain in synthesis_batches for inspection.
            result['insights'] = []
            organization.update(status='organized', section_count=len(result['tree']['children']))
        except ValueError as exc:
            organization.update(status='invalid', reason=str(exc))
    if organization['status'] != 'organized':
        result.setdefault('warnings', []).append(organization['reason'])
    result['report_organization'] = organization
    return result, runtimes
