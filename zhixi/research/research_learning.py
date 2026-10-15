"""Read new original spans between collection batches, then adapt investigation.

The original question remains authoritative. A source-backed direction is a
lead, not a verified conclusion. Private observations never become public query
text; the existing public-provenance pipeline rebuilds outward searches.
"""

from copy import deepcopy
import hashlib
import json
from zhixi.research import research_execution as execution
from time import perf_counter
from zhixi.models.llama_completion import output_length_failure
from zhixi.research.research_reasoning import (
    LOGIC_POLICY as LOGIC_POLICY,
    SCHEDULING_POLICY as SCHEDULING_POLICY,
    valid_weight as valid_weight,
)

from zhixi.research.research_reading_state import (
    SYSTEM as SYSTEM,
    schema as schema,
    checked as checked,
    page_spans as page_spans,
    original_spans as original_spans,
    scheduled_unread as scheduled_unread,
    initialize as initialize,
    next_spans as next_spans,
    has_unread as has_unread,
    rejected_batch_count as rejected_batch_count,
    pending_followup,
    reading_history,
    direction_window,
)


def acquisition_feedback(db, identity, spans, reading):
    """Correlate expected use with what was actually read, not a truth score.

    Observations are model interpretations. Exact span IDs point back to the
    stored originals; quote volume and observation count never mean coverage.
    """
    feedback = []
    for page_id in dict.fromkeys(s['page_id'] for s in spans):
        row = db.execute(
            'SELECT decision_json FROM research_route_checks WHERE project_id=? AND page_id=?',
            (identity, page_id),
        ).fetchone()
        route = json.loads(row[0]) if row else {}
        source_ids = {s['id'] for s in spans if s['page_id'] == page_id}
        notes = [o for o in reading.get('observations', []) if o['source_id'] in source_ids]
        expectation = {
            k: route[k] for k in ('question_ids', 'purpose', 'expected_usefulness', 'reason', 'attribution_basis')
            if k in route
        }
        for note in notes or [None]:
            # A retained quotation is not permission to reuse a guessed label
            # or commentary as the next route's factual context.
            retained = note and note.get('statement_kind') == 'retained_original'
            feedback.append(
                {
                    'page_id': page_id,
                    'expected_use': expectation,
                    'reading_source_ids': sorted(source_ids),
                    'observed_return': {
                        k: note[k]
                        for k in (('source_id', 'statement_kind', 'assessment_status') if retained else (
                            'source_id', 'note', 'statement_kind', 'subject', 'scope',
                            'alternatives', 'assessment_status',
                        ))
                        if k in note
                    }
                    if note
                    else None,
                    'meaning': 'model_reading_not_verified_evidence'
                    if note
                    else 'no_observation_in_this_batch_not_whole_page_irrelevant',
                }
            )
    return feedback


def latest_feedback(db, identity, plan):
    """Bounded recent returns; an empty latest read must not erase prior work."""
    output = []
    for (encoded,) in db.execute(
        'SELECT result_json FROM research_reading_rounds WHERE project_id=? ORDER BY rowid DESC', (identity,)
    ):
        record = json.loads(encoded)
        if (
            record.get('feedback_round_id') == plan.get('feedback_round_id')
            and record.get('reading_status') == 'accepted'
        ):
            feedback = deepcopy(record.get('acquisition_feedback', []))
            for item in feedback:
                observed = item.get('observed_return') or {}
                if observed.get('statement_kind') == 'retained_original':
                    item['observed_return'] = {
                        k: observed[k] for k in ('source_id', 'statement_kind', 'assessment_status')
                        if k in observed
                    }
            for item in feedback:
                if len(json.dumps([*output, item], ensure_ascii=False)) <= 3000:
                    output.append(item)
            if len(json.dumps(output, ensure_ascii=False)) > 2400:
                break
    return output


def read_parts(ctx, project, payload, questions):
    """Split truncated reads without losing the goal, source IDs or raw cache.

    Each smaller request still sees the full user goal. Only complete, validated
    outputs may enter the reading; a singleton failure remains explicitly rejected.
    Provider/cancellation errors propagate instead of being labelled bad evidence.
    """
    from zhixi.research import research_model_calls as intent
    from zhixi.research.research_capacity import cached_call

    spans = payload['source_spans']
    visible_ids = {q['id'] for q in payload['previous_questions']}
    visible = [q for q in questions if q['id'] in visible_ids]

    def call(system, data, validate):
        return intent.validated_reason(ctx, project, system, data, validate)

    try:
        return [
            cached_call(ctx, project, SYSTEM, payload, call, lambda raw: checked(raw, spans, visible))
        ]
    except RuntimeError as exc:
        if not output_length_failure(exc):
            raise
        field = 'source_spans' if len(spans) > 1 else 'previous_questions'
        rows = payload[field]
        can_split = len(rows) > 1
        ctx.add_research_event(
            project['id'],
            'reading_output_split' if can_split else 'reading_output_unresolved',
            '阅读输出被截断，拆小当前批次重试；不采用半截结果。'
            if can_split else '最小阅读批次仍被截断，保留原文和失败记录，继续其他材料。',
            level='warning',
            details={
                'source_ids': [s['id'] for s in spans],
                'question_ids': [q['id'] for q in payload['previous_questions']],
                'split_field': field if can_split else None,
                'error': str(exc),
            },
        )
        if not can_split:
            raise intent.InvalidReasoningOutput(str(exc)) from exc
        middle = len(rows) // 2
        return [
            part
            for group in (rows[:middle], rows[middle:])
            for part in read_parts(ctx, project, {**payload, field: group}, questions)
        ]


def advance(ctx, project, plan, *, read_before_fetch=False, defer_followup=False):
    """One new reading batch after approved leads drain; no network inside a DB lock.

    A committed reading is never repeated merely because the queue is empty.
    New queries are handed to the same discovery worker and same time budget.
    """
    from zhixi.research import research_model_calls as intent
    from zhixi.research.research_rounds import planning_view

    project = planning_view(ctx.DB_PATH, project)
    identity = project['id']
    if not intent.active(ctx, identity) or ctx.research_workflow.expire_collection(ctx, identity):
        return plan, False
    from zhixi.research.research_reasoning import pending_reorientation
    from zhixi.research.research_acquisition import unavailable_search_batches

    with execution.connection(ctx.DB_PATH, identity, write=False) as db:
        unavailable = bool(unavailable_search_batches(db, identity))
    if (action := pending_reorientation(plan)) and not unavailable:
        # Global allocation owns its own continuation. Do not rewrite or replay
        # a completed local reading to make its needs_public_search flag true.
        return complete_queries(ctx, project, plan, None, {
            'reorientation_id': action['id'], 'source_ids': [], 'source_spans': [],
            'query_state': 'pending', 'direct_link_state': 'delegated_to_candidate_queue',
        })
    current_questions = [q for q in plan['questions'] if q.get('schedule_state') != 'proposed']
    # Only active question text enters this read, not the recursively growing
    # audit history. Every original requirement remains immutable and visible.
    base = {
        'original_goal': project.get('original_goal', project['goal']),
        **({'investigation_context': project['goal']} if project.get('original_goal') else {}),
        'focus_ids': plan.get('exploration_review', {}).get('focus_ids', []),
        'previous_questions': [
            {
                'id': q['id'],
                'question': q['question'],
                'editable': False,  # Local reading proposes; it cannot rewrite global directions.
                'origin': 'model_proposed_direction_not_fact'
                if q.get('reading_basis') or q.get('provisional') else 'user_requirement',
                'research_weight': q.get('research_weight', 0.5),
                'starting_point': q.get('starting_point', False),
                'public_direction': q.get('public_direction', False),
                'weight_reason': q.get('weight_reason', '尚未调整投入'),
            }
            for q in current_questions
        ],
    }
    with execution.connection(ctx.DB_PATH, identity) as db:
        approved = db.execute(
            'SELECT 1 FROM research_pages p JOIN research_route_checks c ON c.page_id=p.id '
            "WHERE p.project_id=? AND p.status='queued' AND json_extract(c.decision_json,'$.decision')='visit' "
            "AND json_extract(c.decision_json,'$.plan_fingerprint')=? LIMIT 1",
            (identity, intent.plan_fingerprint(plan)),
        ).fetchone()
        if approved and not read_before_fetch:
            return plan, False
        waiting = None
        for key, encoded in db.execute(
            'SELECT reading_key,result_json FROM research_reading_rounds WHERE project_id=? ORDER BY rowid',
            (identity,),
        ):
            record = json.loads(encoded)
            if (
                pending_followup(record)
                and not (unavailable and record.get('query_state') == 'pending')
                and record.get('feedback_round_id') == plan.get('feedback_round_id')
            ):
                waiting = (key, record)
                break
        from zhixi.research.research_capacity import size

        reading_count = db.execute(
            'SELECT COUNT(*) FROM research_reading_rounds WHERE project_id=?', (identity,),
        ).fetchone()[0]
        base = direction_window(base, reading_count)
        visible_ids = {q['id'] for q in base['previous_questions']}
        visible_questions = [q for q in current_questions if q['id'] in visible_ids]
        spans = next_spans(db, identity, plan.get('feedback_round_id'), base,
                           scheduling_questions=current_questions)
        # History uses spare capacity only: it must not shrink the new-reading
        # batch or add another model pass to reread already completed material.
        if spans and not waiting:
            review = plan.get('exploration_review', {})
            spare = max(0, 7000 - size({**base, 'source_spans': spans}) - 30)
            history = reading_history(db, identity, plan, budget=min(2500, spare),
                                      cursor=review.get('reading_cursor', 0),
                                      after_rowid=review.get('reading_through_rowid', 0))
            if history['readings']:
                base['prior_readings'] = history
    if waiting:
        return complete_queries(ctx, project, plan, *waiting)
    if not spans:
        return plan, False
    reading_started = perf_counter()

    try:
        payload = {**base, 'source_spans': spans}
        parts = read_parts(ctx, project, payload, visible_questions)
        reading = {'needs_public_search': any(p['needs_public_search'] for p in parts)}
        for field in ('observations', 'questions', 'question_updates', 'priority_updates'):
            values = [item for part in parts for item in part.get(field, [])]
            reading[field] = list(
                {json.dumps(v, sort_keys=True, ensure_ascii=False): v for v in values}.values()
            )
        if any('material_uses' in part for part in parts):
            # Retain all returned entries, including invalid/unknown uses. A
            # split read's conflicting judgments must fall back at report time.
            if len(parts) == 1:
                reading['material_uses'] = parts[0]['material_uses']
            else:
                reading['material_uses'] = []
                for part in parts:
                    if 'material_uses' in part:
                        uses = part['material_uses']
                        reading['material_uses'].extend(
                            uses if isinstance(uses, list) else [{'invalid_material_uses': uses}]
                        )
        try:
            reading = checked(reading, spans, visible_questions)
        except (ValueError, TypeError, KeyError) as exc:
            # Individually valid sub-batches can still propose incompatible
            # changes. Never apply a conflicting combined plan or stop collection.
            raise intent.InvalidReasoningOutput(str(exc)) from exc
    except intent.InvalidReasoningOutput as exc:
        # The actual invalid returns are retained by validated_reason. Do not
        # turn them into observations, questions, queries or successful reads.
        # Retain the original material for report reading and later feedback.
        preserve_rejected_reading(ctx, project, plan, spans, exc)
        return plan, False
    public = project.get('public_search_topic')
    if public is None:
        public = project['goal']
    needs_queries = bool(
        reading['needs_public_search'] and public.strip() and project['search_provider'] != 'none'
    )
    if ctx.research_workflow.expire_collection(ctx, identity) or not intent.active(ctx, identity):
        return plan, False
    updated = deepcopy(plan)
    key = hashlib.sha256(
        json.dumps([plan.get('feedback_round_id'), [s['id'] for s in spans]]).encode()
    ).hexdigest()
    # Old/raw outputs may still suggest revisions. Keep those in the reading
    # record, but neither delete a direction nor change its allocation here.
    existing = {q['question'].strip().casefold() for q in updated['questions']}
    for index, q in enumerate(reading['questions']):
        if q['question'].strip().casefold() in existing:
            continue
        parent = next(p for p in updated['questions'] if p['id'] == q['parent_question_id'])
        updated['questions'].append(
            {
                'id': f'read-{key}-{index}',
                'question': q['question'],
                'schedule_state': 'proposed',
                'provisional': True,
                'research_weight': parent.get('research_weight', 0.5),
                'weight_reason': q['reason'],
                'goal_quote': parent.get('goal_quote', project['goal']),
                'evidence': q['reason'],
                'reading_basis': {
                    'source_id': q['source_id'],
                    'quote': q['quote'],
                    'parent_question_id': q['parent_question_id'],
                    'citation_scope': q.get('citation_scope', 'literal_excerpt_not_claim_verification'),
                    'status': 'source_backed_question_not_verified_conclusion',
                },
            }
        )
        existing.add(q['question'].strip().casefold())
    record = {
        'source_ids': [s['id'] for s in spans],
        'source_spans': spans,
        'reading': reading,
        'reading_status': 'accepted',
        'reading_goal': base['original_goal'],
        'feedback_round_id': plan.get('feedback_round_id'),
        'query_check': None,
        'new_queries': [],
        'query_state': 'pending' if needs_queries else 'not_requested',
        'meaning': 'reading_and_investigation_update_not_answer_coverage_or_fact_verification',
        'direction_authority': 'local_suggestions_only_global_review_controls_allocation',
        'direction_window': {'shown_ids': [q['id'] for q in base['previous_questions']], 'omitted': base['omitted_directions'],
                             'meaning': 'reading_hints_not_plan_deletion_or_coverage'},
    }
    with execution.connection(ctx.DB_PATH, identity) as db:
        status = db.execute('SELECT status FROM research_projects WHERE id=?', (identity,)).fetchone()
        current = db.execute(
            'SELECT plan_json FROM research_intents WHERE project_id=?', (identity,)
        ).fetchone()
        if (
            not status
            or status[0] not in {'queued', 'crawling'}
            or not current
            or json.loads(current[0]) != plan
        ):
            return plan, False
        record['acquisition_feedback'] = acquisition_feedback(db, identity, spans, reading)
        record['work'] = {
            'reading_wall_seconds': perf_counter() - reading_started,
            'original_characters': sum(len(s['quote']) for s in spans),
            'meaning': 'wall_time_and_read_input_not_coverage_or_active_model_time',
        }
        inserted = db.execute(
            'INSERT OR IGNORE INTO research_reading_rounds VALUES (?,?,?)',
            (identity, key, json.dumps(record, ensure_ascii=False)),
        )
        if not inserted.rowcount:
            return plan, False
        db.execute(
            "UPDATE research_intents SET plan_json=?,stage='已保存原文与暂定线索，未改写整体方向' WHERE project_id=?",
            (json.dumps(updated, ensure_ascii=False), identity),
        )
        if intent.plan_fingerprint(updated) != intent.plan_fingerprint(plan):
            db.execute(
                "UPDATE research_pages SET status='queued' WHERE project_id=? AND status='deferred_goal'",
                (identity,),
            )
        ctx.add_research_event(
            identity,
            'evidence_direction_updated',
            f'已保存 {len(spans)} 段原文的阅读结果与待考虑线索'
            + ('，接着规划公开补查' if needs_queries else ''),
            details=record,
            connection=db,
        )
    # Collection can yield here to acquire a new batch. The saved read is the
    # continuation checkpoint; never repeat inference to resume link/query work.
    if defer_followup:
        return updated, False
    return complete_queries(ctx, project, updated, key, record)


def preserve_rejected_reading(ctx, project, plan, spans, error):
    """Checkpoint a finite failed attempt without stopping independent work.

    next_spans skips this attempt only within its collection round. It does not
    erase source content or mark it understood; final source reading is separate.
    Provider failures and user stops never enter this path.
    """
    from zhixi.research import research_model_calls as intent

    identity = project['id']
    if ctx.research_workflow.expire_collection(ctx, identity) or not intent.active(ctx, identity):
        return
    key = hashlib.sha256(
        json.dumps([plan.get('feedback_round_id'), [s['id'] for s in spans]]).encode()
    ).hexdigest()
    record = {
        'source_ids': [s['id'] for s in spans],
        'source_spans': spans,
        'reading': None,
        'reading_status': 'rejected',
        'reading_goal': project.get('original_goal', project['goal']),
        'error': str(error),
        'attempts': 2,
        'feedback_round_id': plan.get('feedback_round_id'),
        'query_state': 'not_requested',
        'meaning': 'interpretation_rejected_originals_retained_not_successful_reading',
    }
    with execution.connection(ctx.DB_PATH, identity) as db:
        status = db.execute('SELECT status FROM research_projects WHERE id=?', (identity,)).fetchone()
        current = db.execute(
            'SELECT plan_json FROM research_intents WHERE project_id=?', (identity,)
        ).fetchone()
        if (
            not status
            or status[0] not in {'queued', 'crawling'}
            or not current
            or json.loads(current[0]) != plan
        ):
            return
        saved = db.execute(
            'INSERT OR IGNORE INTO research_reading_rounds VALUES (?,?,?)',
            (identity, key, json.dumps(record, ensure_ascii=False)),
        )
        if saved.rowcount:
            ctx.add_research_event(
                identity,
                'reading_interpretation_rejected',
                '本批阅读解释未通过检查，原文已保留；继续其他材料，不采用错误解释。',
                level='warning',
                details=record,
                connection=db,
            )


def complete_queries(ctx, project, plan, key, record):
    """Resume only the public-query phase; the accepted reading is already safe."""
    from zhixi.research import research_model_calls as intent
    from zhixi.sources.public_research_sources import candidates, search_context, query_outcome
    from zhixi.sources.source_queries import from_sources

    identity = project['id']
    if ctx.research_workflow.expire_collection(ctx, identity) or not intent.active(ctx, identity):
        return plan, False
    from zhixi.research.research_acquisition import unavailable_search_batches

    if record.get('query_state') == 'pending':
        with execution.connection(ctx.DB_PATH, identity, write=False) as db:
            if unavailable_search_batches(db, identity):
                return plan, False  # Preserve pending work for explicit search retry.
    public = project.get('public_search_topic')
    if public is None:
        public = project['goal']
    global_action = plan.get('exploration_review', {}).get('pending_search', {}) if key is None else None
    if key is None and (not global_action or global_action.get('state') != 'pending'
                        or global_action.get('id') != record.get('reorientation_id')):
        return plan, False
    if global_action is not None:
        with execution.connection(ctx.DB_PATH, identity, write=False) as db:
            current = db.execute('SELECT plan_json FROM research_intents WHERE project_id=?', (identity,)).fetchone()
        if not current or json.loads(current[0]) != plan:
            return plan, False
    authorized = bool(public.strip() and project['search_provider'] != 'none'
                      and (global_action is None or global_action.get('public_topic') == public))
    # Rebuild from current execution-owned PUBLIC originals, never private
    # observations, questions or report prose in the stored reading record.
    page_ids = {s['page_id'] for s in record['source_spans']}
    if (not record.get('direct_links_checked')
            and record.get('direct_link_state') != 'delegated_to_candidate_queue'):
        from zhixi.research.research_actions import follow_read_links

        reading = record.get('reading') or {}
        originals_only = (
            bool(reading.get('observations'))
            and all(o.get('statement_kind') == 'retained_original' for o in reading['observations'])
            and not reading.get('questions')
            and not reading.get('needs_public_search')
        )
        # Retaining an unclear passage is not a request to chase its links.
        # Other directions/pages remain available to the regular collector.
        search_requested = (record.get('query_state') == 'pending' and public.strip()
                            and project['search_provider'] != 'none')
        if search_requested:
            # Reading requested new evidence, not necessarily this page's links.
            # Keep links in the normal candidate queue; don't spend a mandatory
            # extra model call on navigation before every outward query batch.
            chosen = []
            record = {**record, 'direct_link_state': 'delegated_to_candidate_queue'}
        else:
            chosen = [] if originals_only else follow_read_links(ctx, project, plan, record)
            record = {**record, 'direct_links_checked': True, 'dispatched_link_ids': chosen}
        with execution.connection(ctx.DB_PATH, identity) as db:
            db.execute(
                'UPDATE research_reading_rounds SET result_json=? WHERE project_id=? AND reading_key=?',
                (json.dumps(record, ensure_ascii=False), identity, key),
            )
        if chosen:
            return plan, False  # Fetch selected concrete destinations before generic searches.
    if record.get('query_state') != 'pending':
        return plan, False
    from zhixi.research.research_capacity import cached_call

    def raw_call(system, data, validate):
        return intent.validated_reason(ctx, project, system, data, validate)

    def call(system, data, validate):
        return cached_call(ctx, project, system, data, raw_call, validate)

    if authorized:
        sources = [
            s for s in candidates(ctx.DB_PATH, identity, record['source_spans']) if s['page_id'] in page_ids
        ]
        feedback = search_context(ctx.DB_PATH, identity)
        if global_action is not None:
            feedback['preferred_starting_queries'] = global_action['directions']
        elif plan.get('exploration_review', {}).get('pending_search'):
            # An explicit global continuation now owns this preference, including
            # the empty-result case; local follow-up must not replay it.
            feedback['preferred_starting_queries'] = []
        proposals, check = from_sources(
            public,
            sources,
            intent.platform_hint(public),
            call,
            search_feedback=feedback,
        )
        check['input_scope'] = {
            'kind': 'global_public_reorientation' if global_action is not None else 'new_reading_spans_not_whole_pages',
            'reading_source_ids': record['source_ids'],
            'public_source_ids': [s['id'] for s in sources],
            'public_characters': sum(len(s['quote']) for s in sources),
            'meaning': 'input_work_not_evidence_coverage',
        }
    else:
        proposals, check = [], {'reason': 'public_search_not_authorized'}
    if ctx.research_workflow.expire_collection(ctx, identity) or not intent.active(ctx, identity):
        return plan, False
    updated = deepcopy(plan)
    history = list(dict.fromkeys([*plan.get('query_history', []), *plan.get('queries', [])]))
    queries = intent.interpretation.search_queries({'search_proposals': proposals, 'platforms': []})
    normalized = {q.strip().casefold() for q in history}
    queries = [q for q in queries if q.strip().casefold() not in normalized]
    outcome = query_outcome(check, queries)
    if queries:
        updated.update(query_history=[*history, *queries], queries=queries)
        if global_action is not None:
            from zhixi.research.research_reasoning import query_origins_for_directions

            updated['query_origins'] = {**plan.get('query_origins', {}),
                                        **query_origins_for_directions(queries, global_action['directions'])}
    completed = {**record, 'query_state': 'completed', 'query_check': check,
                 'new_queries': queries, 'query_outcome': outcome}
    if global_action is not None:
        updated['exploration_review']['pending_search'] = {
            **global_action, 'state': 'queued' if queries else 'completed' if authorized else 'cancelled_scope',
            'queries': queries, 'query_check': check, 'query_outcome': outcome,
        }
    if checkpoint := updated.get('exploration_review', {}).get('work_state'):
        from zhixi.research.research_reflection import direction_state

        checkpoint['directions'] = direction_state(updated)
    with execution.connection(ctx.DB_PATH, identity) as db:
        status = db.execute('SELECT status FROM research_projects WHERE id=?', (identity,)).fetchone()
        current = db.execute(
            'SELECT plan_json FROM research_intents WHERE project_id=?', (identity,)
        ).fetchone()
        saved = db.execute(
            'SELECT result_json FROM research_reading_rounds WHERE project_id=? AND reading_key=?',
            (identity, key),
        ).fetchone() if key is not None else None
        if (
            not status
            or status[0] not in {'queued', 'crawling'}
            or not current
            or json.loads(current[0]) != plan
            or (key is not None and (not saved or json.loads(saved[0]).get('query_state') != 'pending'))
        ):
            return plan, False
        if global_action is not None:
            scope = db.execute('SELECT public_search_topic,goal,search_provider FROM research_projects WHERE id=?',
                               (identity,)).fetchone()
            actual_public = scope[1] if scope[0] is None else scope[0]
            if actual_public != public or scope[2] != project['search_provider']:
                return plan, False
        if key is not None:
            db.execute(
                'UPDATE research_reading_rounds SET result_json=? WHERE project_id=? AND reading_key=?',
                (json.dumps(completed, ensure_ascii=False), identity, key),
            )
        db.execute(
            "UPDATE research_intents SET plan_json=?,stage='阅读结果已保存，公开补查规划已完成' WHERE project_id=?",
            (json.dumps(updated, ensure_ascii=False), identity),
        )
        if queries:
            db.execute("UPDATE research_projects SET search_status='queued' WHERE id=?", (identity,))
        outcome_text = {
            'queued': '已加入搜索队列，尚不代表取得材料', 'duplicates_only': '通过审核的查询均与已有查询重复',
            'audit_rejected': '候选查询均未通过审核，未执行搜索', 'no_proposals': '未提出候选查询',
            'invalid_proposals': '候选查询格式或引用无效', 'output_incomplete': '规划或审核输出未完成',
            'not_authorized': '当前未授权公开搜索', 'unknown': '没有可执行查询，原因记录不完整',
        }
        ctx.add_research_event(
            identity,
            'reorientation_queries_completed' if global_action is not None else 'reading_queries_completed',
            f'公开查询规划新增 {len(queries)} 条：{outcome_text[outcome["status"]]}',
            details={'reading_key': key, 'reorientation_id': record.get('reorientation_id'),
                     'new_queries': queries, 'query_check': check, 'query_outcome': outcome},
            connection=db,
        )
    return updated, bool(queries)
