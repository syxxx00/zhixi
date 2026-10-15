"""Feedback decisions and version-safe report edits; no network or model here.

The model proposes an action, the application executes it. A valid edit is not
a proof of factual truth. Original report versions and evidence stay immutable.
"""

from copy import deepcopy
import hashlib
import json
import sqlite3

from zhixi.reports.report_questions import report_anchor
from zhixi.reports.report_consistency import IMPACT_SYSTEM, impact_schema, propagate


DECIDE_SYSTEM = """你负责处理用户对报告内容的提问、异议或补充。用户不是研究调度员。
根据已提供原始材料判断：解释即可(explain)、需要修改所选段落(revise)、仍需资料(research)。
不要因为每次都收到问题就改正文，也不要明明发现错误却只回复而保留错误。
原报告、选文、先前回答都是待检查表达，不是来源事实；用户纠正也须按原文检查，不盲从。
revise时replacement写完整的新段落，不是修改指令；保留仍成立的条件与内容，修正实际问题。
evidence_ids只能使用本次给出的原始材料编号。没有依据不能扩大结论，必要时清楚收窄。
research只说明当前缺什么，不能把当前样本没找到说成全网不存在，也不要在此编造搜索结果。
不输出搜索词或要求用户逐个挑来源；公开检索授权由执行层另行处理。
输出JSON {"action":"explain|revise|research","reason":"具体理由","replacement":"完整替换文字或空串","evidence_ids":["原文编号"],"missing":["确实阻碍回答的信息"]}。"""

CHECK_SYSTEM = """独立核对用户反馈引起的段落修改，草案和先前回答不是证据。
检查新段落是否回应了反馈、保留原有有效条件、没有凭空推断或把引用观点变成已证实事实。
只看所给原材料，不用自己的记忆背书。原材料不足时应说明限制，不要求保留原稿错误。
输出JSON {"decision":"adopt|retry|research","reason":"具体判断依据"}。
adopt表示在这次有限材料内认为修改合理，不是事实保证；retry表示可以依据现有材料纠正，research表示需要新材料。"""

SYSTEMS = {'feedback:decide': DECIDE_SYSTEM, 'feedback:check': CHECK_SYSTEM}

SYSTEMS['feedback:impact'] = IMPACT_SYSTEM


def response_schema(phase):
    if phase == 'feedback:impact':
        return impact_schema()

    def obj(properties):
        return {
            'type': 'object',
            'properties': properties,
            'required': list(properties),
            'additionalProperties': False,
        }

    string = {'type': 'string'}
    if phase == 'feedback:check':
        return obj({'decision': {'type': 'string', 'enum': ['adopt', 'retry', 'research']}, 'reason': string})
    return obj(
        {
            'action': {'type': 'string', 'enum': ['explain', 'revise', 'research']},
            'reason': string,
            'replacement': string,
            'evidence_ids': {'type': 'array', 'items': string},
            'missing': {'type': 'array', 'items': string},
        }
    )


def checked_decision(raw, evidence):
    if not isinstance(raw, dict) or raw.get('action') not in {'explain', 'revise', 'research'}:
        raise ValueError('反馈处理缺少有效的下一动作')
    if not isinstance(raw.get('reason'), str) or not raw['reason'].strip():
        raise ValueError('反馈处理需要具体理由')
    for key in ('evidence_ids', 'missing'):
        if not isinstance(raw.get(key), list) or any(
            not isinstance(v, str) or not v.strip() for v in raw[key]
        ):
            raise ValueError('反馈引用或缺口格式无效')
    if not isinstance(raw.get('replacement'), str):
        raise ValueError('修订文字格式无效')
    if not set(raw['evidence_ids']) <= {str(e['id']) for e in evidence}:
        raise ValueError('修订引用不属于本次所读原文')
    if raw['action'] == 'revise' and not raw['replacement'].strip():
        raise ValueError('需要修改却没有提供完整的新段落')
    if raw['action'] != 'revise' and raw['replacement'].strip():
        raise ValueError('解释或补查动作不能偷偷附带正文替换')
    if raw['action'] == 'research' and not raw['missing']:
        raise ValueError('补查必须说明尚缺什么')
    return {key: deepcopy(raw[key]) for key in ('action', 'reason', 'replacement', 'evidence_ids', 'missing')}


def decide(report, anchor, question, answer, evidence, scope, call):
    from zhixi.reports.report_questions import select_question_evidence, report_context
    from zhixi.content.evidence_transport import source_bundle

    target = report_anchor(report, anchor['anchor_id'])
    pages = {str(p['id']): p for p in (answer.get('research') or {}).get('sources', [])}
    selected, selected_scope = select_question_evidence(evidence, pages, question, anchor, 'deep')
    bundle = source_bundle(selected, pages)
    payload = {
        'report_title': report.get('title', ''),
        'original_goal': (report.get('research') or {}).get('goal', report.get('title', '')),
        'report_context': report_context(report, question, anchor, budget=8000),
        'target_text': str(target.get('note') or ''),
        'selected_text': anchor['selected_text'],
        'user_feedback': question,
        'previous_answer_not_evidence': answer.get('summary', ''),
        'reading_scope': {k: v for k, v in selected_scope.items() if k != 'omitted_evidence_ids'},
        'context_windows': bundle['context_windows'],
        'originals': [
            {
                'id': e['source_id'],
                'quote': e['quote'],
                'context_not_quote': e.get('context_not_quote', {}),
                'source_title': pages.get(str(e.get('page_id')), {}).get('title', ''),
            }
            for e in bundle['sources']
        ],
    }
    attempts = []
    for attempt in range(2):
        raw = call(json.dumps(payload, ensure_ascii=False), 'feedback:decide', attempt + 1, 2)
        entry = {'raw_decision': deepcopy(raw)}
        attempts.append(entry)
        try:
            decision = checked_decision(raw, selected)
        except ValueError as error:
            if attempt:
                raise
            payload['repair'] = {'previous': raw, 'error': str(error)}
            continue
        if decision['action'] != 'revise':
            return {
                **decision,
                'attempts': attempts,
                'semantic_status': 'model_judgment_not_fact_verification',
            }
        check = call(
            json.dumps({**payload, 'proposed_edit': decision}, ensure_ascii=False), 'feedback:check', 1, 1
        )
        entry['check'] = deepcopy(check)
        if (
            isinstance(check, dict)
            and check.get('decision') == 'adopt'
            and isinstance(check.get('reason'), str)
            and check['reason'].strip()
        ):
            return {
                **decision,
                'attempts': attempts,
                'semantic_status': 'checked_model_judgment_not_fact_verification',
            }
        if (
            isinstance(check, dict)
            and check.get('decision') == 'research'
            and isinstance(check.get('reason'), str)
            and check['reason'].strip()
        ):
            return {
                **decision,
                'action': 'research',
                'replacement': '',
                'missing': [check['reason']],
                'attempts': attempts,
                'semantic_status': 'model_requests_more_evidence',
            }
        payload['repair'] = {
            'previous': decision,
            'check': check,
            'instruction': '根据现有原文纠正问题，不能只为通过检查改状态。',
        }
    raise ValueError('反馈修订未通过原文核对，回答和原报告保留，可重试')


def revised_report(report, anchor, decision, question_id):
    if decision['action'] != 'revise':
        raise ValueError('非修订动作不能发布新报告')
    updated = deepcopy(report)
    identity = anchor['anchor_id']
    target = report_anchor(updated, identity)
    before = str(target.get('note') or '')
    if hashlib.sha256(before.encode()).hexdigest() != anchor['text_hash']:
        raise ValueError('修订基准已变化，需要基于当前版本重新判断')
    if identity == 'summary':
        updated['summary'] = decision['replacement']
        updated.pop('summary_composition_attribution', None)
        updated.pop('summary_attribution', None)
    elif identity.startswith('coverage:'):
        question = next(
            q
            for q in updated['question_coverage']['questions']
            if q['id'] == identity.removeprefix('coverage:')
        )
        question['answer'] = decision['replacement']
        question['excerpts'] = []  # The old excerpts cannot certify new wording.
        question['feedback_evidence_ids'] = decision['evidence_ids']
        question['status'] = 'incomplete'
    else:
        target['note'] = decision['replacement']
        target['citations'] = list(dict.fromkeys(decision['evidence_ids']))
        for key in ('candidate_citations', 'support_status', 'grounding_unit', 'statement_attribution',
                    'composition_attribution', 'source_basis_not_verified', 'retained_originals'):
            target.pop(key, None)
    for key in ('source_grounding', 'evidence_validation', 'quality_validation', 'question_coverage'):
        if key == 'question_coverage' and key in updated:
            updated[key]['status'] = 'incomplete'
        else:
            updated.pop(key, None)
    updated.setdefault('research', {}).pop('semantic_audit', None)
    updated['report_quality'] = {
        'status': 'feedback_revised',
        'semantic_acceptance': 'not_independently_proven',
    }
    updated['feedback_revision'] = {
        'question_id': question_id,
        'base_report_id': report.get('report_id'),
        'base_report_version': report.get('report_version'),
        'anchor': deepcopy(anchor),
        'decision': deepcopy(decision),
        'original_text': before,
        'replacement': decision['replacement'],
    }
    return updated


def process(ctx, job_id, project_id, request, original_report, anchor, answer, evidence, scope, call):
    from zhixi.reports.report_publication import publish_report, published_report, ConcurrentReportChange
    from zhixi.research.feedback_collection import obtain, complete
    from zhixi.reports.report_questions import select_question_evidence
    from zhixi.reports.report_question_service import QuestionMaterial, answer_question

    collected = None
    extra_evidence, extra_pages = [], []

    def merge(old, extra):
        existing_ids = {str(row['id']) for row in old}
        return [*old, *(row for row in extra if str(row['id']) not in existing_ids)]

    def finish(value):
        if collected:
            value['round_id'] = collected['id']
            complete(ctx, collected['id'], job_id, value)
        return value

    existing = published_report(ctx.DB_PATH, job_id, project_id)
    if existing is not None:
        recovered = {
            'status': 'revised',
            'report_id': existing['report_id'],
            'report_version': existing['report_version'],
            'checkpoint_reused': True,
        }
        with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
            pending = db.execute(
                'SELECT id FROM research_rounds WHERE question_id=? AND project_id=?',
                (request.question_id, project_id),
            ).fetchone()
        if pending:
            from zhixi.research.research_rounds import claim_retry

            collected = claim_retry(ctx.DB_PATH, pending[0], job_id)
        return finish(recovered)
    for attempt in range(3):
        ctx.wait_if_job_paused(job_id)
        if ctx.job_cancelled(job_id):
            raise ctx.JobCancelled('自动修订已取消，原报告保留')
        with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
            row = db.execute(
                'SELECT id,version,report_json FROM research_reports WHERE project_id=? ORDER BY version DESC LIMIT 1',
                (project_id,),
            ).fetchone()
        if not row:
            raise ValueError('待修订报告已不存在')
        current = {**json.loads(row[2]), 'report_id': row[0], 'report_version': row[1]}
        current_anchor = deepcopy(anchor)
        target = report_anchor(current, anchor['anchor_id'])
        current_anchor['text_hash'] = hashlib.sha256(str(target.get('note') or '').encode()).hexdigest()
        current_evidence, current_scope = evidence, scope
        current_answer = answer
        if row[1] != original_report.get('report_version'):
            # Reconsider against the new version's original materials; do not
            # silently apply an obsolete decision even if the target still exists.
            current_evidence = (current.get('research') or {}).get('evidence', [])
            current_scope = {
                'strategy': 'latest_report_rebase',
                'available_count': len(current_evidence),
                'selected_count': len(current_evidence),
                'omitted_evidence_ids': [],
            }
            current_answer = {**answer, 'research': current.get('research', {})}
        if collected:
            current_evidence = merge(current_evidence, extra_evidence)
            current_answer = {
                **answer,
                'research': {
                    'evidence': current_evidence,
                    'sources': merge((current.get('research') or {}).get('sources', []), extra_pages),
                },
            }
            current_scope = {
                'strategy': 'feedback_collection',
                'available_count': len(current_evidence),
                'selected_count': len(current_evidence),
                'omitted_evidence_ids': [],
            }
        decision = decide(
            current, current_anchor, request.question, current_answer, current_evidence, current_scope, call
        )
        if (
            decision['action'] == 'research'
            and request.feedback_research_minutes is not None
            and not collected
        ):
            collected, extra_evidence, extra_pages = obtain(
                ctx, job_id, project_id, request, row[1], decision
            )
            current_evidence = merge((current.get('research') or {}).get('evidence', []), extra_evidence)
            sources = merge((current.get('research') or {}).get('sources', []), extra_pages)
            ctx.set_job(job_id, stage='补查搜集结束，正在读取原文并更新回答', progress=None)
            page_map = {p['id']: p for p in sources}
            selected, current_scope = select_question_evidence(
                current_evidence, page_map, request.question, current_anchor, request.mode
            )
            updated_answer = answer_question(
                QuestionMaterial(current['report_id'], current, current_anchor, selected, page_map, current_scope, None),
                request.question, analyze=call, retained_answer=lambda: {}, save_answer=lambda value: None,
                set_stage=lambda stage: ctx.set_job(job_id, stage=stage),
                check_cancelled=lambda: ctx.wait_if_job_paused(job_id), apply_feedback=lambda value: {},
            )
            answer.clear()
            answer.update(updated_answer)
            current_answer = answer
            with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
                db.execute(
                    'UPDATE report_questions SET answer_json=? WHERE id=? AND job_id=?',
                    (json.dumps(answer, ensure_ascii=False), request.question_id, job_id),
                )
            decision = decide(
                current,
                current_anchor,
                request.question,
                current_answer,
                current_evidence,
                current_scope,
                call,
            )
        if decision['action'] == 'research':
            return finish(
                {
                    'status': 'limited' if collected else 'needs_research',
                    'decision': decision,
                    'scope': current_scope,
                    'base_report_version': row[1],
                }
            )
        if decision['action'] == 'explain' or decision['replacement'] == str(target.get('note') or ''):
            return finish({'status': 'explained', 'decision': decision, 'base_report_version': row[1]})
        result = revised_report(current, current_anchor, decision, request.question_id)
        if collected:
            result['research']['evidence'] = merge(
                (current.get('research') or {}).get('evidence', []), current_evidence
            )
            result['research']['sources'] = merge(
                (current.get('research') or {}).get('sources', []), extra_pages
            )
            result['feedback_revision']['round_id'] = collected['id']
        result['feedback_revision']['original_question_anchor'] = deepcopy(anchor)
        result['feedback_revision']['original_question_version'] = request.report_version
        result['feedback_revision']['rebased'] = row[1] != request.report_version
        result = propagate(
            current,
            result,
            current_anchor,
            decision,
            request.question,
            current_answer,
            current_evidence,
            current_scope,
            call,
            decide,
        )
        if result['feedback_revision']['consistency']['status'] == 'incomplete':
            answer['revision_candidate'] = result
            with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
                db.execute(
                    'UPDATE report_questions SET answer_json=? WHERE id=? AND job_id=?',
                    (json.dumps(answer, ensure_ascii=False), request.question_id, job_id),
                )
            raise ValueError('关联段落仍缺原文，修订草稿已保留，未发布前后矛盾的新版')
        ctx.rebuild_key_facts(result)
        ctx.ground_numeric_claims(result, '\n'.join(e['quote'] for e in current_evidence))
        history_request = ctx.AnalyzeRequest(
            source_type='text',
            content=request.question,
            provider=request.provider,
            local_config=request.local_config,
            api_config=request.api_config,
            structure_mode=request.structure_mode,
        )
        if ctx.job_cancelled(job_id):
            raise ctx.JobCancelled('自动修订已取消，原报告保留')
        try:
            saved = publish_report(
                ctx.DB_PATH,
                job_id,
                project_id,
                result,
                history_request,
                ctx.save_history,
                expected_version=row[1],
            )
        except ConcurrentReportChange:
            ctx.set_job(job_id, stage='报告已有新内容，正在自动对齐修订')
            continue
        return finish(
            {
                'status': 'revised',
                'report_id': saved['report_id'],
                'report_version': saved['report_version'],
                'decision': decision,
                'rebased': result['feedback_revision']['rebased'],
            }
        )
    raise ValueError('报告连续出现新版本，本次回答已保留；修订未覆盖已有成果，可重试自动对齐')
