"""Lossless question batching. A batch is not a replacement for the full plan."""

import json
import hashlib
import sqlite3
from zhixi.research import research_execution as execution
from copy import deepcopy


def size(value):
    return len(json.dumps(value, ensure_ascii=False))


def groups(rows, build, budget=7000):
    batch = []
    for row in rows:
        if size(build([*batch, row])) > budget:
            if batch:
                yield batch
                batch = []
            if size(build([row])) > budget:
                raise ValueError('单项与完整目标仍超过调用容量；不能静默删除目标或来源')
        batch.append(row)
    if batch:
        yield batch


def cached_call(ctx, project, system, payload, call, validate):
    """Cache raw outputs and revalidate with today's contract on every hit.

    Validators may normalize their output; feeding a normalized result back as
    raw model output is incorrect. Invalid/stale caches are recomputed locally,
    without modifying original materials or previously published reports.
    """
    with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        selected = db.execute(
            'SELECT selection_json FROM research_workflows WHERE project_id=?', (project['id'],)
        ).fetchone()
        # Configuration is hashed, never copied into public queries/logs.
        key = hashlib.sha256(
            json.dumps(
                [3, system, payload, selected[0] if selected else None], ensure_ascii=False, sort_keys=True
            ).encode()
        ).hexdigest()
        row = db.execute(
            'SELECT result_json FROM research_capacity_steps WHERE project_id=? AND step_key=?',
            (project['id'], key),
        ).fetchone()
    if row:
        try:
            cached = json.loads(row[0])
            if isinstance(cached, dict) and cached.get('schema') == 3 and 'raw' in cached:
                return validate(cached['raw'])
        except (ValueError, TypeError, KeyError):
            pass  # A cache miss, not a failure of the research record.
    accepted = []

    def checked(raw):
        original = deepcopy(raw)
        value = validate(raw)
        accepted[:] = [original]
        return value

    result = call(system, payload, checked)
    if not accepted:
        return result  # A caller bypassing validation must not create reusable state.
    with execution.connection(ctx.DB_PATH, project['id']) as db:
        db.execute(
            'INSERT OR REPLACE INTO research_capacity_steps VALUES (?,?,?)',
            (project['id'], key, json.dumps({'schema': 3, 'raw': accepted[0]}, ensure_ascii=False)),
        )
    return result


def route_decisions(call, system, plan, candidates, budget=7000, *, context=None):
    from zhixi.research.research_contracts import route_plan, checked_decisions, checked_source_batch, SOURCE_SYSTEM

    route = route_plan(plan)
    context = context or {}
    # Read-link context used to be built by the caller and silently discarded.
    # Batch the original spans too; do not solve that bug with a larger prompt
    # that can overflow on the next model or after a long research session.
    grouped_keys = ('source_spans', 'retrieval_feedback')
    records = [(key, row) for key in grouped_keys for row in context.get(key, [])]
    fixed = {k: v for k, v in context.items() if k not in grouped_keys}

    def build(questions, candidates, records):
        return {
            **fixed,
            **{
                key: [row for kind, row in records if kind == key] for key in grouped_keys if context.get(key)
            },
            'plan': {**route, 'questions': questions},
            'candidates': candidates,
        }

    # Keep every question and every candidate; a visit for any goal angle wins.
    # Defer is adopted only after all question batches have considered the item.
    output = {}
    if not candidates or not route['questions']:
        return output
    largest_candidate = max(candidates, key=size)
    largest_span = max(records, key=size, default=None)
    question_groups = list(
        groups(
            route['questions'],
            lambda qs: build(qs, [largest_candidate], [largest_span] if largest_span else []),
            budget,
        )
    )
    for qs in question_groups:
        span_groups = list(groups(records, lambda spans: build(qs, [largest_candidate], spans), budget)) or [
            []
        ]
        for spans in span_groups:
            for cs in groups(candidates, lambda rows: build(qs, rows, spans), budget):
                checker = checked_source_batch if system == SOURCE_SYSTEM else checked_decisions
                decisions = call(
                    system, build(qs, cs, spans), lambda raw: checker(raw, cs, {'questions': qs})
                )
                for identity, decision in decisions.items():
                    old = output.get(identity)
                    rank = {'defer': 0, 'unresolved': 1, 'visit': 2}
                    if old is None or rank[decision['decision']] > rank[old['decision']]:
                        output[identity] = decision
                    elif old['decision'] == decision['decision'] == 'visit':
                        old['question_ids'] = list(
                            dict.fromkeys([*old['question_ids'], *decision['question_ids']])
                        )
                        if 'expected_usefulness' in decision:
                            old['expected_usefulness'] = max(
                                old.get('expected_usefulness', 0), decision['expected_usefulness']
                            )
    return output


def source_use_decisions(call, plan, candidates, goal, budget=7000, *, progress=None):
    """Read each original once against the goal, not once per plan window.

    Research directions are rotating hints, not a checklist that determines
    eligibility. Missing hints never authorize dropping the original goal or
    treating unmentioned angles as absent. Link routing keeps its own policy.
    """
    from zhixi.research.research_contracts import route_plan, checked_source_batch, SOURCE_SYSTEM

    route = route_plan(plan)
    anchors = [q for q in route['questions'] if q['id'] == 'original-goal']
    if not anchors:
        raise ValueError('来源用途判断必须保留完整原目标')
    optional = [{'id': q['id'], 'question': q['question'], 'provisional': True}
                for q in route['questions'] if q['id'] != 'original-goal']
    # We are deciding source use, not planning investment or validating the
    # earlier direction. Do not repeat model scheduling explanations here.
    base = {'original_goal': goal, 'plan': {'questions': anchors},
            'direction_scope': 'rotating_hints_not_required_answers; original_goal_is_authoritative'}
    pending, output, cursor = list(candidates), {}, 0
    while pending:
        frame = deepcopy(base)
        largest = max(pending, key=size)
        for question in optional[cursor:] + optional[:cursor]:
            trial = {**frame, 'plan': {**frame['plan'], 'questions': [*frame['plan']['questions'], question]}}
            # Leave capacity for originals; more plan detail is not more proof.
            if size(trial) <= budget // 6 and size({**trial, 'candidates': [largest]}) <= budget:
                frame = trial
        batch = next(groups(pending, lambda rows: {**frame, 'candidates': rows}, budget))
        if progress:
            progress(len(output), len(candidates))
        output.update(call(SOURCE_SYSTEM, {**frame, 'candidates': batch},
                           lambda raw: checked_source_batch(raw, batch, frame['plan'])))
        pending = pending[len(batch):]
        cursor = (cursor + 1) % max(1, len(optional))
    return output
