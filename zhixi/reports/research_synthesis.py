"""Write sources, review concrete paragraphs, then compose the reader report."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Callable

from zhixi.research.research_analysis import pack_evidence_corpus
from zhixi.storage.synthesis_checkpoints import (
    reusable_step as reusable_step,
    restored_step as restored_step,
    lineage_step as lineage_step,
    cached_step as cached_step,
)
from zhixi.content.evidence_context import source_ordered_evidence



def evidence_batches(evidence: list[dict], pages: dict, goal: str, budget: int):
    remaining = source_ordered_evidence(evidence)
    while remaining:
        corpus, coverage = pack_evidence_corpus(remaining, pages, goal, budget, preserve_order=True)
        selected = set(coverage['included_evidence_ids'])
        yield corpus, coverage
        remaining = [item for item in remaining if str(item['id']) not in selected]


def write_evidence_batch(corpus, coverage, evidence, pages, goal, analyze, index, total, failures, chapter_task=None):
    """Split only failed writing work; keep stable original batches/checkpoints."""
    try:
        from zhixi.content.evidence_transport import compact_source_bundle

        selected_ids = set(coverage['included_evidence_ids'])
        rows = [r for r in source_ordered_evidence(evidence) if str(r['id']) in selected_ids]
        aliases = {str(row['id']): f'S{i}' for i, row in enumerate(rows, 1)}
        originals = {v: k for k, v in aliases.items()}
        text = json.dumps({'goal': goal, **compact_source_bundle(rows, pages, aliases),
                           **({'chapter_task': chapter_task} if chapter_task else {})}, ensure_ascii=False,
                          separators=(',', ':'))
        draft = deepcopy(analyze(text, 'evidence', index, total))

        def restore(value):
            if isinstance(value, dict):
                for key, item in list(value.items()):
                    if key in {'citations', 'original_source_ids', 'candidate_citations', 'proposed_citations'} and isinstance(item, list):
                        value[key] = [originals.get(v, v) if isinstance(v, str) else v for v in item]
                    elif key in {'source_id', 'evidence_id'} and isinstance(item, str):
                        value[key] = originals.get(item, item)
                    else:
                        restore(item)
            elif isinstance(value, list):
                for item in value:
                    restore(item)
        restore(draft)
        draft['batch_source_aliases'] = originals
        return [{'coverage': coverage, 'result': deepcopy(draft)}]
    except RuntimeError as exc:
        from zhixi.sources.source_queries import output_length_failure

        if not output_length_failure(exc):
            raise
        selected = set(coverage['included_evidence_ids'])
        rows = [r for r in source_ordered_evidence(evidence) if str(r['id']) in selected]
        failures.append(
            {
                'original_batch': index,
                'evidence_ids': [str(r['id']) for r in rows],
                'error': str(exc),
                'status': 'split' if len(rows) > 1 else 'unresolved',
            }
        )
        if len(rows) <= 1:
            return []  # No endless retries or fabricated replacement prose.
        from zhixi.content.evidence_transport import encode_bundle

        middle = len(rows) // 2
        parts = []
        for group in (rows[:middle], rows[middle:]):
            text = encode_bundle(group, pages, goal)
            part = {
                **coverage,
                'included_count': len(group),
                'included_evidence_ids': [str(r['id']) for r in group],
                'characters': len(text),
                'split_from_batch': index,
            }
            parts.extend(
                write_evidence_batch(text, part, group, pages, goal, analyze, index, total, failures, chapter_task)
            )
        return parts


def synthesize_all(
    evidence: list[dict], pages: dict, goal: str, budget: int, analyze: Callable[[str, str, int, int], dict],
    *, material_selection=None,
) -> tuple[dict, dict]:
    """analyze is cancellation/checkpoint-aware; no model invocation is hidden here.

    Original blocks enter bounded writing batches. Review precedes global
    composition so invalid drafts cannot determine the final overview. Retained
    drafts, review decisions and specific delivery repairs remain inspectable;
    they do not establish independent factual truth.
    """
    topic_trace, topic_runtimes = None, []
    if material_selection is not None:
        from zhixi.reports.report_material_outline import plan_materials

        chapters, topic_trace, topic_runtimes = plan_materials(
            evidence, pages, material_selection, goal, budget, analyze,
        )
        batches = []
        for chapter in chapters:
            task = {k: chapter[k] for k in ('title', 'focus')}
            reserve = len(json.dumps({'chapter_task': task}, ensure_ascii=False))
            for corpus, coverage in evidence_batches(chapter['evidence'], pages, goal, budget - reserve):
                batches.append((corpus, {**coverage, 'chapter_task': task}))
    else:
        # Older callers without a material-use ledger retain their complete
        # source transport. The writer, review and composition are shared.
        batches = list(evidence_batches(evidence, pages, goal, budget))
    if not batches:
        raise ValueError('没有可用于综合的完整证据')
    drafts = []
    all_drafts = []
    runtimes = list(topic_runtimes)
    writing_failures = []
    for index, (corpus, coverage) in enumerate(batches):
        parts = write_evidence_batch(
            corpus, coverage, evidence, pages, goal, analyze, index + 1, len(batches), writing_failures,
            coverage.get('chapter_task'),
        )
        all_drafts.extend(parts)
        for part in parts:
            if (part['result'].get('generation_validation') or {}).get('status') == 'degraded':
                writing_failures.append(
                    {
                        'original_batch': index + 1,
                        'evidence_ids': part['coverage']['included_evidence_ids'],
                        'status': 'unresolved',
                        'reason': 'invalid_draft_retained_not_report_prose',
                    }
                )
            else:
                drafts.append(part)
        runtimes.extend(part['result'].get('runtime') or {} for part in parts)
    if not drafts:
        raise ValueError('所有正文批次均未完成，原文与失败调用保留，未生成虚假报告')
    result = deepcopy(drafts[0]['result'])
    result['title'] = goal
    result['summary'] = ''  # Compose the overview only after paragraph review.
    result['insights'] = []
    branches = []
    chapter_nodes = {}
    for draft in drafts:
        tree = draft['result'].get('tree') or {}
        details = deepcopy(tree.get('children') or ([tree] if tree.get('note') else []))
        task = draft['coverage'].get('chapter_task')
        if task:
            if task['title'] not in chapter_nodes:
                node = {'label': task['title'], 'note': '', 'children': [], 'citations': [],
                        'reader_details': True}
                chapter_nodes[task['title']] = node
                branches.append(node)
            chapter_nodes[task['title']]['children'].extend(details)
        else:
            branches.extend(details)
    result['tree'] = {'label': goal, 'note': '', 'children': branches}
    result['warnings'] = list(dict.fromkeys(w for d in drafts for w in d['result'].get('warnings', [])))
    coverage = {
        'total_count': len(evidence),
        'included_count': len(evidence),
        'included_evidence_ids': [str(item['id']) for item in evidence],
        'deferred_evidence_ids': [],
        'batch_count': len(batches),
        'batches': [draft['coverage'] for draft in drafts],
        'character_budget': budget,
        'characters': sum(len(corpus) for corpus, _ in batches),
        'overview_basis': 'retained_reviewed_paragraphs',
    }
    result['synthesis_batches'] = all_drafts
    if topic_trace is not None:
        result['material_outline'] = topic_trace
    result['writing_output_recovery'] = writing_failures
    result['overview_output_recovery'] = []  # Compatibility with saved trace readers.
    unresolved = {
        identity
        for failure in writing_failures
        if failure['status'] == 'unresolved'
        for identity in failure['evidence_ids']
    }
    if unresolved:
        coverage.update(
            included_count=len(evidence) - len(unresolved),
            included_evidence_ids=[str(r['id']) for r in evidence if str(r['id']) not in unresolved],
            deferred_evidence_ids=sorted(unresolved),
        )
        result.setdefault('warnings', []).append('部分原文的分批写作未完成；记录已保留，不计作已完成分析。')
    result['synthesis_reductions'] = []
    degraded_steps = [
        {'phase': phase, 'index': index + 1, 'validation': deepcopy(step['result']['generation_validation'])}
        for phase, steps in (('evidence', all_drafts),)
        for index, step in enumerate(steps)
        if (step['result'].get('generation_validation') or {}).get('status') == 'degraded'
    ]
    if degraded_steps:
        result['generation_validation'] = {'status': 'degraded', 'steps': degraded_steps}
        result.setdefault('warnings', []).append(
            '模型部分步骤生成降级，保留材料不等于研究综合完成；可重新综合。'
        )
    result.setdefault('runtime', {})['seconds'] = round(
        sum(float(item.get('seconds') or 0) for item in runtimes), 1
    )
    result['runtime']['model_calls'] = sum(not item.get('checkpoint_reused') for item in runtimes)
    result['runtime']['checkpoint_steps'] = sum(bool(item.get('checkpoint_reused')) for item in runtimes)
    from zhixi.reports.report_grounding import ground_report

    result['source_review_policy'] = 'citation_first_v1'
    result = ground_report(result, evidence, pages, goal, budget, analyze, check_delivery=False)
    for key, value in result['source_grounding']['runtime'].items():
        result['runtime'][key] = result['runtime'].get(key, 0) + value
    from zhixi.reports.report_outline import organize_for_reader, compose_material_report
    from zhixi.reports.report_overall_check import finalize_report

    if result.get('tree', {}).get('children'):
        editor = compose_material_report if topic_trace is not None else organize_for_reader
        result, editorial_runtimes = editor(result, goal, budget, analyze, evidence=evidence, pages=pages)
        result.setdefault('runtime', {})['seconds'] += sum(float(r.get('seconds') or 0) for r in editorial_runtimes)
        result['runtime']['model_calls'] += sum(not r.get('checkpoint_reused') for r in editorial_runtimes)
        result['runtime']['checkpoint_steps'] += sum(bool(r.get('checkpoint_reused')) for r in editorial_runtimes)
        result = finalize_report(result, evidence, pages, goal, budget, analyze, rewrite=ground_report)
    coverage['final_prose_basis'] = 'reviewed_or_revised_prose_unfinished_drafts_in_diagnostics'
    coverage['source_grounding_status'] = result['source_grounding']['status']
    return result, coverage
