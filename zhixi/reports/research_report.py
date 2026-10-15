"""Domain-neutral report assembly and conservative source support checks.

Exact source support is intentionally narrower than semantic relevance. A
paraphrase may be correct, but it needs a separate semantic review before it is
presented as verified. Raw drafts remain available for that review.
"""

from __future__ import annotations

import copy
import re
from hashlib import sha256
from typing import Any


SCAFFOLD = re.compile(
    r'\[证据\s+[a-f0-9-]+\]|来源标题[：:]|来源网址[：:]|研究目标[：:]|'
    r'研究目标及经过清洗|只能依据这些证据|类型[：:].*立场[：:]|'
    r'上下文线索[：:]\s*\{|\"(?:归属检查|matched_subjects|source_span)\"[：:]',
    re.I,
)


def compact(value: str) -> str:
    return re.sub(r'\s+', '', value).strip('。.!！?？;；')


def saved_report_stage(report):
    """Execution/publication wording, never a claim of factual certification."""
    if (report.get('generation_validation') or {}).get('status') == 'degraded':
        return '部分结果已保存，模型生成降级，建议重试综合'
    if (report.get('source_grounding') or {}).get('status') == 'incomplete':
        return '局部结果与原文已保存；未完成草稿保留在诊断记录'
    limits = []
    check = (report.get('overall_report_check') or {}).get('status')
    delivery = (report.get('report_quality') or {}).get('delivery_status')
    if check == 'incomplete' or delivery == 'incomplete':
        limits.append('交付检查未完成')
    if (report.get('question_coverage') or {}).get('status') == 'incomplete':
        limits.append('问题覆盖核对未完成')
    if (report.get('delivery_repairs') or {}).get('status') == 'unresolved' or delivery == 'unresolved_repairs':
        limits.append('修订仍有未解决意见')
    elif check == 'issues_found' or delivery == 'issues_found':
        limits.append('检查仍有具体意见')
    return '报告已保存' + ('，' + '；'.join(limits) if limits else '')


def directly_supported(text: str, quote: str) -> bool:
    """Require the entire claim in the quote, not token overlap or bag-of-numbers."""
    claim, source = compact(text), compact(quote)
    return len(claim) >= 6 and claim in source


def publication_view(result):
    """Exclude unfinished drafts from new delivery, not from diagnostic history.

    A successful rewrite is still model interpretation. When a unit failed,
    its dependent synopsis cannot certify a one-sided partial report either.
    This function is used during generation only, never when loading old reports.
    """
    ledger = result.get('source_grounding') or {}
    if result.get('publication', {}).get('policy') == 'completed_units_and_originals_v1':
        return result
    if not ledger.get('units'):
        return result
    units = {u['path']: u for u in ledger['units']}
    failed = {path for path, u in units.items() if u['status'] not in {'rewritten', 'retained'}}

    def prune(node, path='tree'):
        value = copy.deepcopy(node)
        value['children'] = [
            child
            for i, n in enumerate(node.get('children') or [])
            if (child := prune(n, f'{path}.{i}')) is not None
        ]
        identity = node.get('grounding_unit', path)
        if identity in failed:
            value.update(note='', citations=[], label='相关材料')
            value.pop('statement_attribution', None)
        if path != 'tree' and not value.get('note') and not value['children']:
            return None
        return value

    result['tree'] = prune(result.get('tree') or {})
    result['insights'] = [
        item
        for i, item in enumerate(result.get('insights') or [])
        if item.get('grounding_unit', f'insight:{i}') not in failed
    ]
    if failed:
        result['summary'] = (
            '本轮提供已完成整理的局部内容与可回查原文；部分段落尚未完成核对，暂不作覆盖整个问题的结论。'
        )
        result['summary_support_status'] = 'coverage_notice'
        # Draft-wide inferences may depend on failed/counterevidence sections.
        # Their original text is still in the unit ledger.
        result['insights'] = []
    elif (unit := units.get('summary')) and unit.get('attribution'):
        result['summary_attribution'] = copy.deepcopy(unit['attribution'])
    result['publication'] = {
        'policy': 'completed_units_and_originals_v1',
        'status': 'partial' if failed else 'model_interpretation',
        'withheld_unit_paths': sorted(failed),
        'drafts_retained_in': 'source_grounding.units',
        'semantic_verification': False,
    }
    return result


def attributed_references(text, attribution, evidence):
    """Validate an unchanged source/address ledger; no claim of semantic truth.

    Current editorial receipts route normal prose by source ID, not another
    model-supplied source quote. Literal selections still require their exact
    frozen quotes and actual program-copy operation.
    """
    if not isinstance(attribution, dict) or attribution.get('text_sha256') != sha256(text.encode()).hexdigest():
        return None
    statements = attribution.get('statements')
    if not isinstance(statements, list) or not statements:
        return None
    originals = {str(row['id']): row for row in evidence}
    from zhixi.reports.report_composition import current_receipt

    editorial = current_receipt(text, attribution) is not None
    refs = []
    for statement in statements:
        if not isinstance(statement, dict) or not isinstance(statement.get('excerpts'), list):
            return None
        start, end = statement.get('start'), statement.get('end')
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                or text[start:end] != statement.get('text')):
            return None
        literal = statement.get('kind') == 'retained_original'
        if editorial and literal:
            selected = statement.get('excerpts') or []
            if (statement.get('classification') != 'program_copied_original_not_interpreted'
                    or not selected or any(not isinstance(ref, dict) or not isinstance(ref.get('quote'), str)
                                           or ref.get('relation') != 'context' for ref in selected)
                    or statement['text'] != '\n\n'.join('“' + ref['quote'] + '”' for ref in selected)):
                return None
        for ref in statement.get('excerpts', []):
            if not isinstance(ref, dict):
                return None
            identity = ref.get('evidence_id')
            if identity not in originals:
                return None
            if editorial and ref.get('relation') not in {'supports', 'contradicts', 'context'}:
                return None
            if ((not editorial or literal or 'quote' in ref)
                    and ref.get('quote') != originals[identity].get('quote')):
                return None
            refs.append(identity)
    return list(dict.fromkeys(refs))


def bind_node_support(
    node: dict[str, Any], evidence: list[dict[str, Any]], similarity, max_candidates: int = 3
) -> None:
    claim = str(node.get('note') or '')
    known = {str(item['id']) for item in evidence if item.get('id')}
    proposed = node.get('proposed_citations', node.get('citations', []))
    node['proposed_citations'] = list(dict.fromkeys(value for value in proposed if isinstance(value, str)))
    attributed = attributed_references(claim, node.get('statement_attribution'), evidence)
    if attributed is not None:
        node.update(citations=attributed, candidate_citations=[], support_status='model_attributed')
        return
    matches = [
        str(item['id'])
        for item in evidence
        if item.get('id') and directly_supported(claim, str(item.get('quote') or item.get('claim') or ''))
    ]
    # Prefer an explicit exact source over duplicate copies of the same passage.
    explicit = [item for item in node.get('citations', []) if item in matches]
    node['citations'] = list(dict.fromkeys(explicit or matches))
    node['support_status'] = 'source_excerpt' if matches else 'needs_review' if claim else 'heading'
    if matches or not claim:
        node['candidate_citations'] = []
        return
    ranked = sorted(
        (
            (similarity(claim, str(item.get('quote') or item.get('claim') or '')), str(item['id']))
            for item in evidence
            if item.get('id')
        ),
        reverse=True,
    )
    node['candidate_citations'] = list(
        dict.fromkeys(
            [value for value in node['proposed_citations'] if value in known]
            + [item_id for score, item_id in ranked[:max_candidates] if score >= 0.08]
        )
    )


def assemble_report(
    result: dict[str, Any], evidence: list[dict[str, Any]], similarity, timeline_builder
) -> dict[str, Any]:
    draft = copy.deepcopy({key: result.get(key) for key in ('summary', 'tree', 'key_facts', 'insights')})
    publication_view(result)
    omitted: list[dict[str, str]] = []
    checked: list[dict[str, Any]] = []

    def clean(node: Any, path: str = '0', root: bool = False) -> dict[str, Any] | None:
        if not isinstance(node, dict):
            return None
        label = str(node.get('label') or '').strip()
        original_note = str(node.get('note') or '')
        addressed = any(isinstance(node.get(key), dict)
                        and node[key].get('text_sha256') == sha256(original_note.encode()).hexdigest()
                        for key in ('statement_attribution', 'composition_attribution'))
        # Addressed prose must keep its actual bytes/code-point offsets. Legacy
        # display cleanup cannot silently invalidate a current mixed receipt.
        note = original_note if addressed else re.sub(r'^原文[：:]\s*', '', original_note.strip())
        children = [
            value
            for i, child in enumerate(node.get('children') or [])
            if (value := clean(child, f'{path}.{i}'))
        ]
        if children and re.fullmatch(r'包含\s*\d+\s*条(?:相互关联的)?具体信息[。.]?', note):
            note = ''
        invalid = bool(SCAFFOLD.search(label + ' ' + note) or '该数字表述未通过原文核验' in note)
        if invalid:
            omitted.append({'path': path, 'reason': 'internal_scaffolding_or_failed_grounding'})
            if not children and not root:
                return None
            label, note = (str(result.get('title') or '研究报告') if root else '相关分析'), ''
        if not root and not children and not note:
            return None
        if label in {'原文', '具体而言', '此时', '此外'}:
            label = re.split(r'[。！？!?；;，,]', note, maxsplit=1)[0][:36] or '原文摘录'
        value = {
            'id': f'section:{path}',
            'label': label or '分析',
            'note': note,
            'children': children,
            'citations': node.get('citations') or [],
        }
        for field in (
            'reader_details',
            'draft_paragraph_id',
            'draft_batch',
            'paragraph_ids',
            'proposed_citations',
            'grounding_unit',
            'parent_headings',
            'statement_attribution',
            'composition_attribution',
        ):
            if field in node:
                value[field] = copy.deepcopy(node[field])
        bind_node_support(value, evidence, similarity)
        if note and not root:
            checked.append(value)
        return value

    tree = clean(result.get('tree'), root=True)
    fallback = not checked
    if fallback:
        children = []
        for index, item in enumerate(evidence):
            quote = str(item.get('quote') or item.get('claim') or '').strip()
            if not quote or SCAFFOLD.search(quote):
                continue
            value = {
                'id': f'evidence:{item.get("id") or index}',
                'label': re.split(r'[。！？!?；;，,]', quote, maxsplit=1)[0][:36],
                'note': quote,
                'children': [],
                'citations': [str(item['id'])] if item.get('id') else [],
            }
            bind_node_support(value, evidence, similarity)
            children.append(value)
        checked = children
        tree = {
            'id': 'section:0',
            'label': str(result.get('title') or '研究报告'),
            'note': '',
            'children': [
                {
                    'id': 'section:excerpts',
                    'label': '可回查原文',
                    'note': '',
                    'children': children,
                    'citations': [],
                    'support_status': 'heading',
                }
            ],
            'citations': [],
            'support_status': 'heading',
        }

    source_count = len({item.get('page_id') for item in evidence if item.get('page_id')})
    summary = str(result.get('summary') or '').strip()
    if not summary or SCAFFOLD.search(summary) or fallback or '该数字表述未通过原文核验' in summary:
        summary = f'已整理 {source_count} 个来源、{len(evidence)} 条证据。当前提供可回查材料，尚不能视为对研究问题的完整回答。'
        summary_status = 'coverage_notice'
    else:
        summary_status = (
            'coverage_notice'
            if result.get('summary_support_status') == 'coverage_notice'
            else 'model_attributed'
            if attributed_references(summary, result.get('summary_attribution'), evidence) is not None
            else 'source_excerpt'
            if any(
                directly_supported(summary, str(item.get('quote') or item.get('claim') or ''))
                for item in evidence
            )
            else 'needs_review'
        )
    result['summary'], result['tree'] = summary, tree
    result['key_facts'] = [
        {
            'label': node['label'],
            'value': node['note'],
            'citations': node['citations'],
            'support_status': node['support_status'],
        }
        for node in checked
        if not node['children']
    ]
    insights = []
    for item in result.get('insights') or []:
        if not isinstance(item, dict):
            continue
        detail = str(item.get('detail') or '')
        if not detail or SCAFFOLD.search(detail) or '该数字表述未通过原文核验' in detail:
            continue
        value = {**item, 'note': detail}
        bind_node_support(value, evidence, similarity)
        value.pop('note')
        insights.append(value)
    result['insights'] = insights
    used = {item_id for node in checked for item_id in node['citations']}
    pending = [node['id'] for node in checked if node['support_status'] == 'needs_review']
    result['report_quality'] = {
        'schema_version': 1,
        'status': 'evidence_only'
        if fallback
        else 'needs_review'
        if pending
        or summary_status == 'needs_review'
        or any(item['support_status'] == 'needs_review' for item in insights)
        else 'model_attributed'
        if any(node['support_status'] == 'model_attributed' for node in checked)
        else 'source_grounded',
        'summary_status': summary_status,
        'answer_status': 'not_assessed',
        'pending_node_ids': pending,
        'omitted_nodes': omitted,
        'evidence_count': len(evidence),
        'used_evidence_count': len(used),
        'unrepresented_evidence_ids': [
            str(item['id']) for item in evidence if item.get('id') and str(item['id']) not in used
        ],
    }
    result['model_draft'] = draft
    if result.get('publication', {}).get('status') == 'partial' and not fallback:
        result['report_quality']['status'] = 'partial'
    delivery_check = result.get('overall_report_check', {})
    unresolved_repair = result.get('delivery_repairs', {}).get('status') == 'unresolved'
    if delivery_check.get('status') in {'issues_found', 'incomplete'} or unresolved_repair:
        result['report_quality']['delivery_status'] = (
            'unresolved_repairs' if unresolved_repair else delivery_check['status']
        )
        if result['report_quality']['status'] not in {'partial', 'evidence_only'}:
            result['report_quality']['status'] = 'needs_review'
    # Timeline belongs to the delivered report, not the unfiltered source bin.
    # All originals remain in research.evidence, including withheld drafts.
    timeline_ids = used | set(attributed_references(summary, result.get('summary_attribution'), evidence) or [])
    timeline_rows = evidence if fallback else [row for row in evidence if str(row['id']) in timeline_ids]
    result['timeline'] = timeline_builder(timeline_rows)
    return result


def report_units(report):
    """Yield every prose field once, without depth/length/count limits."""
    if report.get('summary'):
        yield 'summary', report.get('title', '报告概览'), report, 'summary'
    stack = [(report.get('tree') or {}, 'tree')]
    while stack:
        node, path = stack.pop()
        if node.get('note'):
            yield path, node.get('label', ''), node, 'note'
        stack.extend(
            (child, f'{path}.{i}') for i, child in reversed(list(enumerate(node.get('children') or [])))
        )
    for index, item in enumerate(report.get('insights') or []):
        if item.get('detail'):
            yield f'insight:{index}', item.get('title', ''), item, 'detail'
