"""Answer a question from an explicitly selected, version-bound material set.

This module knows neither HTTP routes nor the application's job registry. The
caller owns scheduling, cancellation, model configuration and persistence. An
answer is saved before feedback editing so a failed edit can reuse the answer.
"""

from collections.abc import Callable
from dataclasses import dataclass

import json

from zhixi.reports.report_questions import answer_payload, normalize_answer


AnalyzeBatch = Callable[[str, str, int, int], dict]


@dataclass(frozen=True)
class QuestionMaterial:
    """The report/anchor are context; only evidence contains source quotations."""

    report_id: str
    report: dict
    anchor: dict
    evidence: list[dict]
    pages: dict[str, dict]
    scope: dict
    parent_context: dict | None


def answer_question(
    material: QuestionMaterial,
    question: str,
    *,
    analyze: AnalyzeBatch,
    retained_answer: Callable[[], dict],
    save_answer: Callable[[dict], None],
    set_stage: Callable[[str], None],
    check_cancelled: Callable[[], None],
    apply_feedback: Callable[[dict], dict],
    allow_changes: bool = False,
) -> dict:
    """Return the persisted answer, including its feedback outcome.

    Exceptions propagate to the task owner; this service does not reinterpret
    cancellation as failure, choose a model, or publish an extra report.
    """
    answer = retained_answer()
    if not answer.get('question_scope') or not answer.get('report_quality'):
        set_stage('读取已有报告与相关原文，直接回答问题；不重新生成报告')
        payload = answer_payload(material, question)
        raw = analyze(json.dumps(payload, ensure_ascii=False), 'question:answer', 1, 1)
        capacity_scope = (raw.get('runtime') or {}).get('question_input_scope')
        evidence, scope = material.evidence, dict(material.scope)
        context_scope = {k: v for k, v in payload['report_context'].items() if k.startswith('omitted_')}
        if capacity_scope is not None:
            if not isinstance(capacity_scope, dict):
                raise ValueError('问答实际输入范围格式无效')
            ids = capacity_scope.get('included_evidence_ids')
            known = {str(e['id']) for e in evidence}
            if (not isinstance(ids, list) or any(not isinstance(i, str) for i in ids)
                    or len(ids) != len(set(ids)) or any(i not in known for i in ids)):
                raise ValueError('问答实际输入范围与本次保存材料不一致')
            evidence = [e for e in evidence if str(e['id']) in set(ids)]
            scope.update(selected_count=len(evidence), capacity_selection=capacity_scope['strategy'],
                         capacity_reduced=bool(capacity_scope.get('capacity_reduced')),
                         omitted_evidence_ids=list(dict.fromkeys([
                             *scope.get('omitted_evidence_ids', []),
                             *(str(e['id']) for e in material.evidence if str(e['id']) not in set(ids)),
                         ])))
            context_scope = capacity_scope['report_context_scope']
        answer = normalize_answer(raw, evidence)
        answer['research'] = {
            'evidence': [{'id': e['id'], 'page_id': e['page_id'], 'quote': e['quote']} for e in evidence],
            'sources': [{k: p.get(k, '') for k in ('id', 'title', 'url')} for p in material.pages.values()
                        if p['id'] in {e['page_id'] for e in evidence}],
        }
        answer['question_scope'] = scope
        answer['report_context_scope'] = context_scope
    else:
        set_stage('恢复已保存的回答，继续处理修订')

    check_cancelled()
    save_answer(answer)
    if allow_changes:
        set_stage('回答已保留，按已确认范围处理修订')
        answer['feedback'] = apply_feedback(answer)
    else:
        proposal = answer.get('proposal', {})
        answer['feedback'] = {'status': 'needs_confirmation' if proposal.get('action', 'none') != 'none' else 'explained',
                              'proposal': proposal, 'report_unchanged': True}
    save_answer(answer)
    return answer


def completion_stage(feedback: dict) -> str:
    stages = {
        'revised': '回答已保存，报告已自动更新',
        'limited': '补查已结束，回答已保存，部分问题仍缺资料',
        'needs_research': '回答已保存，仍需补充资料',
        'needs_confirmation': '回答已保存；后续处理建议等待确认，未修改报告或联网',
    }
    return stages.get(feedback['status'], '回答已保存，无需修改正文')
