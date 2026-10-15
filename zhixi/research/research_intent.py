"""Question-led collection. Model decisions are leads, never proof of coverage.

No network request or model call is made by a read endpoint. Deferred URLs and
their reasons remain in storage. A planning failure stops expansion explicitly.
"""

import json
import re
import sqlite3
from zhixi.research import research_execution as execution
import time
from hashlib import sha256
from urllib.parse import urlparse

from zhixi.research import goal_interpretation as interpretation
from zhixi.research.research_reasoning import (
    LOGIC_POLICY as LOGIC_POLICY,
    SCHEDULING_POLICY as SCHEDULING_POLICY,
    valid_weight as valid_weight,
)

from zhixi.research.research_contracts import (
    PLAN_SYSTEM as PLAN_SYSTEM,
    QUERY_SYSTEM as QUERY_SYSTEM,
    LINK_SYSTEM as LINK_SYSTEM,
    SOURCE_SYSTEM as SOURCE_SYSTEM,
    ROUTE_REVIEW_BATCH_SIZE as ROUTE_REVIEW_BATCH_SIZE,
    plan_fingerprint as plan_fingerprint,
    route_plan as route_plan,
    checked_decisions as checked_decisions,
    checked_source_batch as checked_source_batch,
    InvalidReasoningOutput as InvalidReasoningOutput,
)
from zhixi.research import research_model_calls as model_calls
from zhixi.research.research_model_calls import platform_hint, selection, active


def reason(ctx, project, system, payload):
    return model_calls.reason(ctx, project, system, payload)


def validated_reason(ctx, project, system, payload, validate):
    return model_calls.validated_reason(ctx, project, system, payload, validate)


def reasoning_schema(system, goal, payload):
    return model_calls.reasoning_schema(system, goal, payload)


def initialize(db):
    from zhixi.research import research_reading_state

    research_reading_state.initialize(db)
    db.execute(
        'CREATE TABLE IF NOT EXISTS research_route_checks (page_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, decision_json TEXT NOT NULL)'
    )
    db.execute('CREATE INDEX IF NOT EXISTS route_checks_project ON research_route_checks(project_id)')
    db.execute(
        'CREATE TABLE IF NOT EXISTS research_intents (project_id TEXT PRIMARY KEY, '
        "status TEXT NOT NULL, plan_json TEXT NOT NULL DEFAULT '{}', stage TEXT NOT NULL DEFAULT '', "
        "error TEXT NOT NULL DEFAULT '')"
    )


def state(db_path, identity):
    with sqlite3.connect(db_path, timeout=30) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_intents'"
        ).fetchone():
            return None
        row = db.execute(
            'SELECT status,plan_json,stage,error FROM research_intents WHERE project_id=?', (identity,)
        ).fetchone()
    return dict(status=row[0], plan=json.loads(row[1]), stage=row[2], error=row[3]) if row else None


def object_options(goal):
    return list(dict.fromkeys([goal, *[part for part in re.split(r'[\s，,。；;：:？！?!]+', goal) if part]]))


def checked_plan(raw, goal):
    return interpretation.checked(raw, goal, platform_hint(goal))


def checked_queries(raw, public_topic, subject=None):
    plan = interpretation.checked(raw, public_topic, platform_hint(public_topic), queries_only=True)
    return interpretation.search_queries(plan)


def url_role(url):
    return 'account_profile' if (urlparse(url).hostname or '').lower() == 'space.bilibili.com' else 'page'


def fail(ctx, identity, exc):
    from zhixi.research.research_control import pause

    message = '研究运行未完成，已暂停扩展并保留材料，可继续重试：' + str(exc)[:300]
    with ctx.research_workflow.LOCK, execution.connection(ctx.DB_PATH, identity) as db:
        if not pause(db, identity, ctx.utc_now(), time.time(), reason='planning_failed', error=message):
            return
        db.execute(
            "UPDATE research_intents SET status='failed',stage=?,error=? WHERE project_id=?",
            (message, message, identity),
        )
        ctx.add_research_event(identity, 'planning_failed', message, level='warning', connection=db)


def acquisition_plan(ctx, project):
    """Start with authorized input; let source reading develop the investigation.

    This is a reading brief, NOT a model-verified decomposition or evidence.
    Saved semantic plans and feedback rounds retain their existing behavior.
    """
    from zhixi.research.research_rounds import planning_view

    project = planning_view(ctx.DB_PATH, project)
    saved = state(ctx.DB_PATH, project['id'])
    plan = (saved or {}).get('plan', {})
    if project.get('feedback_round_id'):
        return prepare(ctx, project)
    if plan.get('basis') == 'literal_input_acquisition_v1':
        return interpretation.anchor_original_goal(plan, project['goal'])
    if plan:
        return prepare(ctx, project)
    if selection(ctx, project['id']) is None:
        return None
    public = project.get('public_search_topic')
    if public is None:
        public = project['goal']  # Legacy authorization, never replace explicit blank.
    plan = interpretation.anchor_original_goal(
        {
            'questions': [],
            'platforms': [],
            'profile_research': False,
            'basis': 'literal_input_acquisition_v1',
            'queries': [public] if public.strip() and project['search_provider'] != 'none' else [],
            'public_search_pending': False,
        },
        project['goal'],
    )
    with execution.connection(ctx.DB_PATH, project['id']) as db:
        existing = db.execute(
            'SELECT plan_json FROM research_intents WHERE project_id=?', (project['id'],)
        ).fetchone()
        if existing and json.loads(existing[0] or '{}'):
            return json.loads(existing[0])
        db.execute(
            'INSERT INTO research_intents(project_id,status,plan_json,stage,error) '
            "VALUES (?,'ready',?,'先读取指定入口；公开搜索前拆解检索方向，再据材料细化问题','') "
            'ON CONFLICT(project_id) DO UPDATE SET status=excluded.status,plan_json=excluded.plan_json,'
            "stage=excluded.stage,error=''",
            (project['id'], json.dumps(plan, ensure_ascii=False)),
        )
    return plan


def _public_query_batch(ctx, project, public, previous, engine_states=None):
    """Adapt model caching and cancellation; query policy belongs to source_queries."""
    from zhixi.sources import source_queries
    from zhixi.research.research_capacity import cached_call

    def raw_call(system, data, validate):
        return validated_reason(ctx, project, system, data, validate)

    def call(system, data, validate):
        return cached_call(ctx, project, system, data, raw_call, validate)

    def check_active():
        if not active(ctx, project['id']) or ctx.research_workflow.expire_collection(ctx, project['id']):
            raise ctx.JobCancelled('搜索规划已停止，未发送新查询')

    return source_queries.initial_queries(
        public,
        platform_hint(public),
        call,
        previous=previous,
        engine_states=engine_states,
        check_active=check_active,
    )


def prepare_initial_search(ctx, project, plan):
    """Split the public request once, when discovery actually starts (not seeds)."""
    if (
        not plan
        or plan.get('basis') != 'literal_input_acquisition_v1'
        or plan.get('query_decomposition')
        or plan.get('feedback_round_id')
        or plan.get('query_history')
        or plan.get('search_recovery')
    ):
        return plan
    public = project.get('public_search_topic')
    if public is None:
        public = project['goal']
    if not public.strip() or project['search_provider'] == 'none':
        return plan
    identity = project['id']
    ctx.add_research_event(identity, 'search_decomposition_started', '正在将公开研究目标拆成互补的检索方向')
    queries, check = _public_query_batch(ctx, project, public, [])
    if ctx.research_workflow.expire_collection(ctx, identity) or not active(ctx, identity):
        raise ctx.JobCancelled('搜索规划已停止')
    from zhixi.research.research_reflection import starting_directions

    updated = starting_directions({**plan, 'queries': queries, 'query_decomposition': check}, queries)
    with execution.connection(ctx.DB_PATH, project['id']) as db:
        status = db.execute('SELECT status FROM research_projects WHERE id=?', (identity,)).fetchone()
        saved = db.execute(
            'SELECT plan_json FROM research_intents WHERE project_id=?', (identity,)
        ).fetchone()
        if not status or status[0] not in {'queued', 'crawling'} or not saved or json.loads(saved[0]) != plan:
            raise ctx.JobCancelled('搜索规划现场已改变，未覆盖新的状态')
        db.execute(
            'UPDATE research_intents SET plan_json=?,stage=? WHERE project_id=?',
            (
                json.dumps(updated, ensure_ascii=False),
                '拆解未完全确认，保留公开原话与可用查询'
                if check['literal_fallback']
                else '检索方向已拆解，开始搜索实际材料',
                identity,
            ),
        )
        ctx.add_research_event(
            identity,
            'search_decomposed',
            f'已保存 {len(queries)} 条检索查询及拆解检查，保留完整研究目标',
            details={'queries': queries, 'query_check': check},
            connection=db,
        )
    return updated


def recover_empty_search(ctx, project, plan):
    """Recover a cold start with no originals; never spin until the time limit.

    One persisted recovery wave follows the initial decomposition/search. Later
    acquired material uses the existing incremental reading/query workflow.
    """
    import hashlib

    if not plan or plan.get('feedback_round_id') or project['search_provider'] == 'none':
        return False
    public = project.get('public_search_topic')
    if public is None:
        public = project['goal']
    if not public.strip():
        return False
    identity = project['id']
    if not active(ctx, identity) or ctx.research_workflow.expire_collection(ctx, identity):
        return True
    with execution.connection(ctx.DB_PATH, project['id']) as db:
        saved = db.execute(
            'SELECT plan_json FROM research_intents WHERE project_id=?', (identity,)
        ).fetchone()
        if not saved or json.loads(saved[0]) != plan:
            return False  # Never plan against an unpersisted/replaced snapshot.
        # Failed bodies and arbitrary discovered navigation are not available
        # material. Keep genuine originals and concrete unread entrypoints;
        # a pile of menu links must not hide an unavailable search service.
        from zhixi.research.research_acquisition import has_approved_pages
        from zhixi.web.research_entrypoints import has_pending

        if has_pending(db, identity) or has_approved_pages(db, identity, plan) or db.execute(
            "SELECT 1 FROM research_pages WHERE project_id=? AND (status='fetching' OR "
            "(status='fetched' AND trim(content)<>'' AND "
            "COALESCE(json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,"
            "'$.content_access.body_available'),1)<>0) OR "
            "(status IN ('queued','candidate') AND parent_page_id IS NULL AND "
            "json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,"
            "'$.discovery.search_engine') IS NOT NULL)) LIMIT 1",
            (identity,),
        ).fetchone():
            return False
        current = db.execute(
            'SELECT search_status,max_pages FROM research_projects WHERE id=?', (identity,)
        ).fetchone()
        if current[0] in {'queued', 'running'}:
            return False
        count = db.execute(
            "SELECT count(*) FROM research_pages WHERE project_id=? AND status='failed'", (identity,)
        ).fetchone()[0]
        if count >= current[1]:
            return False
        key = hashlib.sha256(public.encode()).hexdigest()
        def normalize(q):
            return ' '.join(q.split()).casefold()

        tried = {normalize(q) for q in plan.get('queries', [])}
        engine_states = []
        for kind, encoded in db.execute(
            'SELECT kind,details_json FROM research_events WHERE project_id=? '
            "AND kind IN ('search_started','search_engine_results') ORDER BY rowid",
            (identity,),
        ):
            details = json.loads(encoded)
            tried.update(normalize(q) for q in details.get('queries', []))
            tried.update(normalize(b['query']) for b in details.get('batches', []) if b.get('query'))
            if kind == 'search_engine_results':
                engine_states = [
                    {k: b.get(k) for k in ('engine', 'status', 'returned')}
                    for b in details.get('batches', [])
                ]
        from zhixi.research.research_acquisition import unavailable_search_batches

        unavailable = unavailable_search_batches(db, identity)
        if unavailable:
            # Keep original queries and transport details for the background
            # probe; the compact model-facing summary above is not sufficient.
            ctx.research_workflow.pause_unavailable_search(ctx, identity, unavailable, db)
            return True
        if plan.get('search_recovery', {}).get('key') == key:
            return False
    ctx.add_research_event(
        identity, 'search_recovery_started', '首轮尚未取得正文，正在按公开目标调整查询；不代表资料不存在'
    )
    queries, check = _public_query_batch(ctx, project, public, plan.get('queries', []), engine_states)
    fresh = []
    for query in queries:
        if normalize(query) not in tried:
            fresh.append(query)
            tried.add(normalize(query))
    queries = fresh
    if ctx.research_workflow.expire_collection(ctx, identity) or not active(ctx, identity):
        return True
    updated = {**plan, 'search_recovery': {'key': key, 'queries': queries, 'query_check': check}}
    if queries:
        updated['queries'] = queries
    with execution.connection(ctx.DB_PATH, project['id']) as db:
        status = db.execute('SELECT status FROM research_projects WHERE id=?', (identity,)).fetchone()
        saved = db.execute(
            'SELECT plan_json FROM research_intents WHERE project_id=?', (identity,)
        ).fetchone()
        if not status or status[0] not in {'queued', 'crawling'} or not saved or json.loads(saved[0]) != plan:
            return True
        db.execute(
            'UPDATE research_intents SET plan_json=?,stage=? WHERE project_id=?',
            (json.dumps(updated, ensure_ascii=False), '查询调整已保存，寻找有效入口', identity),
        )
        if queries:
            db.execute(
                "UPDATE research_projects SET search_status='queued',search_error='' WHERE id=?", (identity,)
            )
        ctx.add_research_event(
            identity,
            'search_recovery_planned',
            f'已调整为 {len(queries)} 条不重复的新查询',
            details=updated['search_recovery'],
            connection=db,
        )
    plan.clear()
    plan.update(updated)
    return bool(queries)


def prepare(ctx, project):
    from zhixi.research.research_rounds import planning_view

    project = planning_view(ctx.DB_PATH, project)
    identity = project['id']
    saved = state(ctx.DB_PATH, identity)
    if (
        saved
        and saved['plan'].get('basis') == 'natural_language_plan_with_fidelity_check_v3'
        and not saved['plan'].get('public_search_pending')
        and saved['plan'].get('feedback_round_id') == project.get('feedback_round_id')
    ):
        anchored = interpretation.anchor_original_goal(saved['plan'], project['goal'])
        if anchored != saved['plan']:
            with execution.connection(ctx.DB_PATH, project['id']) as db:
                db.execute(
                    'UPDATE research_intents SET plan_json=? WHERE project_id=?',
                    (json.dumps(anchored, ensure_ascii=False), identity),
                )
        return anchored
    if selection(ctx, identity) is None:
        return None
    with execution.connection(ctx.DB_PATH, project['id']) as db:
        db.execute(
            "INSERT INTO research_intents(project_id,status,stage) VALUES (?,'planning','正在理解研究目标并规划调查') "
            "ON CONFLICT(project_id) DO UPDATE SET status='planning',error='',stage=excluded.stage",
            (identity,),
        )
    public = project.get('public_search_topic')
    if public is None:
        public = project['goal']
    may_search = bool(public.strip() and project['search_provider'] != 'none')

    def call(system, payload, validate):
        return validated_reason(ctx, project, system, payload, validate)

    reusable = (saved or {}).get('plan', {})
    if (
        reusable.get('planning_strategy') == 'provisional_goal_checked_public_queries_v1'
        and reusable.get('subject') == project['goal']
        and reusable.get('feedback_round_id') == project.get('feedback_round_id')
    ):
        plan = reusable
    else:
        # Understanding the full goal never authorizes outbound query generation.
        # Public-only query construction has one owner, shared with fresh tasks.
        plan = interpretation.propose(project['goal'], platform_hint(project['goal']), False, call)
        plan = interpretation.anchor_original_goal(plan, project['goal'])
        plan.update(
            planning_strategy='provisional_goal_checked_public_queries_v1', public_search_pending=may_search
        )
        if project.get('feedback_round_id'):
            plan['feedback_round_id'] = project['feedback_round_id']
        with execution.connection(ctx.DB_PATH, project['id']) as db:
            db.execute(
                'UPDATE research_intents SET plan_json=? WHERE project_id=?',
                (json.dumps(plan, ensure_ascii=False), identity),
            )
            ctx.add_research_event(
                identity,
                'goal_understood',
                '已形成暂定调查方向，按原始问题寻找实际材料',
                details=plan,
                connection=db,
            )
    if may_search:
        if project.get('feedback_round_id'):
            from zhixi.sources.public_research_sources import candidates, search_context
            from zhixi.sources.source_queries import from_sources

            # Feedback has actual public material. Do not draft an initial
            # batch only to replace it with another independent query plan.
            proposals, query_check = from_sources(
                public, candidates(ctx.DB_PATH, identity), platform_hint(public), call,
                search_feedback=search_context(ctx.DB_PATH, identity),
            )
            plan['source_query_check'] = query_check
            plan['source_query_proposals'] = proposals
            plan['queries'] = interpretation.search_queries({'search_proposals': proposals, 'platforms': []})
        else:
            plan['queries'], query_check = _public_query_batch(ctx, project, public, [])
            plan['query_decomposition'] = query_check
            if query_check['literal_fallback']:
                plan['public_query_bootstrap'] = {
                    'basis': 'literal_authorized_public_topic',
                    'queries': plan['queries'],
                    'meaning': 'discovery_attempt_not_entity_confirmation',
                }
        plan['public_query_check'] = query_check
        ctx.add_research_event(
            identity,
            'public_queries_checked',
            '公开查询已核对用途与范围，搜索尝试不作为事实',
            details={**query_check, 'meaning': 'outbound_scope_check_not_source_facts'},
        )
    else:
        plan['queries'] = []
    plan['public_search_pending'] = False
    if project.get('feedback_round_id'):
        plan['feedback_round_id'] = project['feedback_round_id']
    plan = interpretation.anchor_original_goal(plan, project['goal'])
    with execution.connection(ctx.DB_PATH, project['id']) as db:
        db.execute(
            "UPDATE research_pages SET status='queued' WHERE project_id=? AND status='deferred_goal'",
            (identity,),
        )
        db.execute(
            "UPDATE research_intents SET status='ready',plan_json=?,stage='按原始问题读取线索，调查方向随证据更新',error='' WHERE project_id=?",
            (json.dumps(plan, ensure_ascii=False), identity),
        )
        ctx.add_research_event(
            identity, 'goal_planned', '暂定方向已记录，开始实际调查', details=plan, connection=db
        )
    return plan


def check_queue(ctx, project, plan):
    """Validate one bounded batch outside any DB transaction; retain all decisions."""
    identity = project['id']
    fingerprint = plan_fingerprint(plan)
    with execution.connection(ctx.DB_PATH, project['id']) as db:
        db.row_factory = sqlite3.Row
        from zhixi.web.research_entrypoints import approve_pending
        from zhixi.research.research_reasoning import scheduling_route

        # Restored probes predate origin attribution. This is scheduling metadata,
        # not another semantic judgment or a change to visit/defer authority.
        for pid, encoded in db.execute(
            'SELECT page_id,decision_json FROM research_route_checks WHERE project_id=?', (identity,)
        ).fetchall():
            route = json.loads(encoded)
            attributed = scheduling_route(route, plan)
            if attributed != route:
                db.execute('UPDATE research_route_checks SET decision_json=? WHERE page_id=? AND project_id=?',
                           (json.dumps(attributed, ensure_ascii=False), pid, identity))

        # Repair stale queue state without upgrading a prior defer to visit.
        # Allocation-only changes reuse decisions, including negative ones.
        db.execute(
            "UPDATE research_pages SET status='deferred_goal' WHERE project_id=? AND status='queued' "
            'AND EXISTS (SELECT 1 FROM research_route_checks c WHERE c.page_id=research_pages.id '
            "AND json_extract(c.decision_json,'$.plan_fingerprint')=? "
            "AND json_extract(c.decision_json,'$.decision')='defer')",
            (identity, fingerprint),
        )
        if approve_pending(ctx, db, identity, plan, fingerprint):
            return True
        if db.execute(
            'SELECT 1 FROM research_pages p JOIN research_route_checks c ON c.page_id=p.id '
            "WHERE p.project_id=? AND p.status='queued' AND json_extract(c.decision_json,'$.decision')='visit' "
            "AND json_extract(c.decision_json,'$.plan_fingerprint')=? LIMIT 1",
            (identity, fingerprint),
        ).fetchone():
            return False  # Fetch approved leads before spending time judging more navigation.
        rows = db.execute(
            'SELECT id,canonical_url AS url,link_text AS title,parent_page_id,depth, '
            "json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,'$.discovery.search_engine') AS engine, "
            "json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,'$.discovery.query') AS query, "
            "json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,'$.discovery.description') AS search_description, "
            "json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,'$.discovery.search_rank') AS search_rank "
            ",json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,'$.discovery.search_batch_status') AS search_batch_status "
            "FROM research_pages WHERE project_id=? AND status='queued' AND NOT EXISTS "
            "(SELECT 1 FROM research_route_checks c WHERE c.page_id=research_pages.id AND json_extract(c.decision_json,'$.plan_fingerprint')=?) "
            "ORDER BY CASE WHEN canonical_url LIKE 'https://space.bilibili.com/%' THEN 1 ELSE 0 END,"
            'relevance DESC,depth ASC,created_at ASC',
            (identity, fingerprint),
        ).fetchall()
        reviewed = db.execute(
            'SELECT p.parent_page_id, '
            "json_extract(CASE WHEN json_valid(p.extraction_json) THEN p.extraction_json ELSE '{}' END,'$.discovery.query') AS query "
            'FROM research_pages p JOIN research_route_checks c ON c.page_id=p.id WHERE p.project_id=? '
            "AND json_extract(c.decision_json,'$.plan_fingerprint')=?",
            (identity, fingerprint),
        ).fetchall()
        from zhixi.research.research_acquisition import candidate_batch, navigation_context

        rows = candidate_batch(
            [dict(row) for row in rows], [dict(row) for row in reviewed], ROUTE_REVIEW_BATCH_SIZE, plan=plan
        )
        for row in rows:
            location = navigation_context(db, identity, row)
            if location:
                row['navigation_context'] = location
            if row.get('engine'):
                row['metadata_scope'] = 'search_excerpt_not_fetched_body'
        from zhixi.research.research_learning import latest_feedback

        feedback = latest_feedback(db, identity, plan)
    if not rows:
        return False
    candidates, decisions = [], {}
    for row in rows:
        item = dict(row)
        explicit = row['depth'] == 0 and not row['engine'] and row['title'] == '起始页面'
        item.update(url_role=url_role(row['url']), explicit_user_seed=explicit)
        # A ranked search hit from an authorized query is a read-only probe,
        # not adopted evidence. Unmatched batches and arbitrary descendants
        # still need semantic judgment; URL/robots/scope checks remain in fetch.
        if (
            item.get('engine') and not item.get('parent_page_id')
            and item.get('query') and item.get('search_description')
            and item.get('search_batch_status') == 'returned'
            and isinstance(item.get('search_rank'), int)
            and ctx.link_relevance(item['query'], item['title'] + ' ' + item['search_description'], item['url']) > 0
        ):
            decisions[item['id']] = scheduling_route({
                'decision': 'visit', 'purpose': 'probe', 'question_ids': [],
                'reason': '读取已授权查询返回的具体搜索线索；摘要相关性暂定，正文尚未核实',
                'basis': 'ranked_search_probe_not_evidence', 'judgment_status': 'provisional',
                'query': item['query'], 'expected_usefulness': 0.5,
            }, plan)
            continue
        candidates.append(item)
    # Do not put a slow navigation judgment in front of these ready probes.
    # Unchecked siblings stay queued and are revisited after this fetch batch.
    if candidates and not decisions:
        from zhixi.research.research_capacity import route_decisions, cached_call

        def raw_call(system, data, validate):
            return validated_reason(ctx, project, system, data, validate)

        def call(system, data, validate):
            return cached_call(ctx, project, system, data, raw_call, validate)

        context = {'retrieval_feedback': feedback} if feedback else None
        try:
            decisions.update(route_decisions(call, LINK_SYSTEM, plan, candidates, context=context))
        except InvalidReasoningOutput as exc:
            # An invalid model judgment is not evidence of irrelevance. Reading
            # already-authorized search hits can still yield viewable originals.
            # Do not use model failure to expand arbitrary descendants forever.
            def readable_search_hit(item):
                return (
                    item.get('engine')
                    and not item.get('parent_page_id')
                    and item.get('search_batch_status') != 'query_text_not_found'
                    and ctx.link_relevance(
                        item.get('query') or project.get('public_search_topic') or '',
                        (item.get('title') or '') + ' ' + (item.get('search_description') or ''),
                        item['url'],
                    )
                    > 0
                )

            decisions = {
                item['id']: {
                    'decision': 'visit' if readable_search_hit(item) else 'defer',
                    'purpose': 'probe',
                    'question_ids': [],
                    'reason': '用途判断未完成；按已授权搜索线索读取原文，不视为支持研究结论'
                    if readable_search_hit(item)
                    else '用途判断未完成且缺少可直接读取的匹配线索；保留链接供查看，未自动扩展',
                    'basis': 'unresolved_model_judgment',
                    'judgment_status': 'unresolved',
                }
                for item in candidates
            }
            ctx.add_research_event(
                identity,
                'route_judgment_unresolved',
                '候选判断未完成，保留线索与原文读取能力，不采纳错误判断',
                details={'candidate_ids': list(decisions), 'error': str(exc)},
            )
    if not active(ctx, identity) or ctx.research_workflow.expire_collection(ctx, identity):
        return False
    with execution.connection(ctx.DB_PATH, project['id']) as db:
        for page_id, decision in decisions.items():
            decision['plan_fingerprint'] = fingerprint
            db.execute(
                'INSERT OR REPLACE INTO research_route_checks VALUES (?,?,?)',
                (page_id, identity, json.dumps(decision, ensure_ascii=False)),
            )
            if decision['decision'] == 'defer':
                db.execute(
                    "UPDATE research_pages SET status='deferred_goal',error=? WHERE id=? AND status='queued'",
                    (decision['reason'], page_id),
                )
                db.execute(
                    "UPDATE research_links SET decision='deferred_goal',reason=? WHERE target_page_id=?",
                    (decision['reason'], page_id),
                )
        unresolved = any(d.get('judgment_status') == 'unresolved' for d in decisions.values())
        stage = (
            '候选已整理，部分用途未判断；按线索读取原文' if unresolved else '候选已按问题检查，正在搜集证据'
        )
        db.execute(
            "UPDATE research_intents SET stage=?,status='ready',error='' WHERE project_id=?",
            (stage, identity),
        )
        ctx.add_research_event(
            identity,
            'routes_checked',
            f'路线检查：{sum(d["decision"] == "visit" for d in decisions.values())} 条可访问，'
            f'{sum(d["decision"] == "defer" for d in decisions.values())} 条暂缓（保留记录）',
            connection=db,
        )
    return True


def review_saved_sources(ctx, project, pages, selected, job_id, *, contents=None):
    """Old off-topic captures stay in history, not automatically in the report.

    Reuse accepted same-goal uses of identical originals, then inspect remaining
    spans in capacity-sized batches. Uses never authorize facts or other spans.
    """
    if not project.get('workflow'):
        return pages, None  # Capture-only compatibility API.
    started = ctx.time.perf_counter()
    model_calls = 0
    request = ctx.AnalyzeRequest(source_type='text', content=project['goal'], **selected.model_dump())

    def runtime_update(**details):
        with ctx.JOBS_LOCK:
            job_start = ctx.JOBS.get(job_id, {}).get('started_at')
        if details.get('model_started_at') and not job_start:
            details.update(started_at=details['model_started_at'], status='running')
        ctx.set_job(job_id, **details)

    def uncached_call(system, payload, validator):
        nonlocal model_calls
        for attempt in range(2):
            ctx.wait_if_job_paused(job_id)
            if ctx.job_cancelled(job_id):
                raise ctx.JobCancelled('研究报告已取消')
            catalog = json.dumps(payload, ensure_ascii=False)
            model_calls += 1
            if selected.provider == 'local':
                raw = ctx.run_qwen(
                    '',
                    '',
                    request,
                    lambda value, stage: ctx.set_job(job_id, stage=stage),
                    runtime_update=runtime_update,
                    job_id=job_id,
                    task_payload={
                        'task': 'research_reasoning',
                        'system': system,
                        'catalog': catalog,
                        'schema': reasoning_schema(system, project['goal'], payload),
                    },
                )
            else:
                config = selected.api_config
                if not config:
                    raise ValueError('API模型配置缺失')
                base = config.base_url.rstrip('/')
                runtime_update(model_started_at=ctx.time.time(), model_name=config.model, model_kind='api')
                response = ctx.httpx.post(
                    base if base.endswith('/chat/completions') else base + '/chat/completions',
                    headers={'Authorization': f'Bearer {config.api_key}'} if config.api_key else {},
                    json={
                        'model': config.model,
                        'messages': [
                            {'role': 'system', 'content': system},
                            {'role': 'user', 'content': catalog},
                        ],
                        'temperature': 0,
                        'max_tokens': 4000,
                    },
                    timeout=1800,
                )
                response.raise_for_status()
                raw = ctx.extract_json_object(response.json()['choices'][0]['message']['content'])
            try:
                return validator(raw)
            except (ValueError, KeyError, TypeError) as exc:
                if attempt:
                    raise
                payload = {'input': payload, 'invalid_response': raw, 'validation_error': str(exc)}

    def call(system, payload, validator):
        # Source selection precedes synthesis; it needs its own checkpoint too.
        from zhixi.storage.synthesis_checkpoints import cached_step

        settings = selected.model_dump(exclude={'api_config'})
        if selected.api_config:
            settings.update(api_model=selected.api_config.model, api_base_url=selected.api_config.base_url)
        key = (
            'source-use:'
            + sha256(
                json.dumps(
                    {
                        'system': system,
                        'payload': payload,
                        'settings': settings,
                        'goal': project['goal'],
                        'schema_version': 3,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
        )

        def run():
            return {'source_use_decisions': uncached_call(system, payload, validator)}

        ctx.wait_if_job_paused(job_id)
        if ctx.job_cancelled(job_id):
            raise ctx.JobCancelled('研究报告已取消')
        saved_step = cached_step(ctx.DB_PATH, job_id, None, key, run)
        if saved_step.get('runtime', {}).get('checkpoint_reused'):
            ctx.set_job(job_id, stage='恢复已完成的来源用途判断，不调用模型重算')
        return saved_step['source_use_decisions']

    saved = state(ctx.DB_PATH, project['id'])
    plan = (saved or {}).get('plan')
    if not plan or plan.get('basis') not in {
        'natural_language_plan_with_fidelity_check_v3',
        'literal_input_acquisition_v1',
    }:
        ctx.set_job(job_id, stage='根据研究目标确定报告需要哪些证据')
        plan = interpretation.propose(project['goal'], platform_hint(project['goal']), False, call)
    plan = interpretation.anchor_original_goal(plan, project['goal'])
    from zhixi.research.research_reading_state import page_spans, reusable_material_uses
    from zhixi.research.research_materials import resolve_preview_span
    from zhixi.content.source_identity import source_context

    decisions = {}
    candidates = []
    originals = []
    traces = {identity: [] for identity in pages}
    for identity, page in pages.items():
        content = contents[identity] if contents is not None else page.get('content', '')
        context = source_context(page)
        spans = list(page_spans(identity, page.get('title', ''), content, page.get('url', ''), context=context))
        originals.extend(spans)
        if not spans:
            decisions[identity] = {
                'decision': 'defer',
                'question_ids': [],
                'purpose': '',
                'reason': '该来源没有可供判断的原文，不能仅凭标题猜测内容。',
                'basis': 'no_original_text',
            }
        for span in spans:
            candidate = {
                'id': span['id'],
                'page_id': identity,
                'url': page.get('url', ''),
                'title': page.get('title', ''),
                'url_role': url_role(page.get('url', '')),
                'preview': span['quote'],
                'span': {
                    'id': span['id'],
                    'offset': span['offset'],
                    'characters': len(span['quote']),
                    'original_characters': span['original_characters'],
                },
                'preview_is_partial': True,
                **({'source_context': context} if context else {}),
            }
            candidates.append(candidate)
    from zhixi.research.research_capacity import source_use_decisions

    # SELECT only: reporting must not initialize, repair or rewrite reading history.
    with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        reused = reusable_material_uses(db, project['id'], project['goal'], originals)
    judged = {}
    for candidate in candidates:
        if candidate['id'] not in reused:
            continue
        use = reused[candidate['id']]
        resolved = resolve_preview_span(candidate, use['source_id'])
        judged[candidate['id']] = {
            **use,
            'question_ids': ['original-goal'] if use['decision'] == 'visit' else [],
            'basis_span_id': resolved['id'],
            'resolved_span': resolved,
            'basis_status': 'resolved_original_span_not_semantic_verification',
            'semantic_support': 'not_verified',
        }
    pending = [candidate for candidate in candidates if candidate['id'] not in judged]
    if pending:
        judged.update(source_use_decisions(
            call, plan, pending, project['goal'],
            progress=lambda done, total: ctx.set_job(job_id, stage=f'按原目标核对材料用途：{done}/{total} 段'),
        ))
    for candidate in candidates:
        identity = candidate['page_id']
        traces[identity].append({'span': candidate['span'], 'decision': judged[candidate['id']]})
    for identity, checks in traces.items():
        if identity in decisions:
            continue
        useful = [
            c['decision']
            for c in checks
            if c['decision']['decision'] in {'visit', 'unresolved'}
            and c['decision'].get('material_role') not in {'navigation', 'asset_pointer'}
        ]
        if useful:
            unresolved = any(d['decision'] == 'unresolved' for d in useful)
            decisions[identity] = {
                **useful[0],
                'decision': 'unresolved' if unresolved else 'visit',
                'basis': 'use_judgment_unresolved_originals_retained_for_inspection'
                if unresolved
                else 'selected_original_spans_only',
            }
        else:
            pointer = any(
                c['decision'].get('material_role') in {'navigation', 'asset_pointer'} for c in checks
            )
            decisions[identity] = {
                'decision': 'defer',
                'question_ids': [],
                'purpose': '',
                'basis': 'entrypoints_retained_not_answer_evidence'
                if pointer
                else 'all_original_spans_checked_for_use_not_fact_verification',
                'reason': '逐段检查未选出可用于本问题解释的正文；入口、原文及各段判断均保留，不推断全网不存在。',
            }
    included = {
        identity: page
        for identity, page in pages.items()
        if decisions[identity]['decision'] in {'visit', 'unresolved'}
    }
    return included, {
        'plan': plan,
        'decisions': decisions,
        'included_page_ids': list(included),
        'deferred_page_ids': [identity for identity in pages if identity not in included],
        'unresolved_page_ids': [
            identity for identity in pages if decisions[identity]['decision'] == 'unresolved'
        ],
        'considered_sources': [
            {
                'id': identity,
                'title': page.get('title', ''),
                'url': page.get('url', ''),
                **decisions[identity],
            }
            for identity, page in pages.items()
        ],
        'basis': 'model_source_use_from_original_spans_not_fact_verification',
        'span_checks': traces,
        'selection_scope': 'original_spans_v1',
        'seconds': round(ctx.time.perf_counter() - started, 2),
        'model_calls': model_calls,
        'stored_pages_deleted': False,
    }


def no_selected_sources_report(project, selection):
    summary = '本轮来源筛选未选出可用于回答研究问题的页面，暂时无法给出有依据的结论。已存材料仍可回查。'
    if (selection.get('material_view') or {}).get('unresolved_evidence_ids'):
        summary = '本轮未能确定材料的使用范围，暂未生成结论；这不代表原文无关或没有信息。完整材料及未完成的用途判断仍可回查。'
    children = [
        {
            'id': 'source-' + str(index),
            'label': item['title'] or item['url'],
            'note': item['reason'] + '\n来源：' + item['url'],
            'children': [],
        }
        for index, item in enumerate(selection['considered_sources'])
    ]
    return {
        'title': project['goal'],
        'summary': summary,
        'tree': {
            'id': 'root',
            'label': '当前证据不足',
            'note': summary,
            'children': [
                {
                    'id': 'source-selection',
                    'label': '为什么这些资料暂未用于结论',
                    'note': (
                        '以下是基于原文片段的用途判断，不是事实核验；完整原文和逐段判断仍可回查。'
                        if selection.get('span_checks') is not None
                        else '以下是基于标题与部分预览的路线判断，不是对全文的事实核验。原始页面仍保存在研究过程中。'
                    ),
                    'children': children,
                }
            ],
        },
        'keywords': [],
        'key_facts': [],
        'insights': [],
        'timeline': [],
        'source': {
            'type': 'research',
            'pages': len(children),
            'evidence': 0,
            'characters': 0,
            'title': project['goal'],
            'url': '',
        },
        'research': {
            'project_id': project['id'],
            'goal': project['goal'],
            'sources': [],
            'evidence': [],
            'limitations': [summary],
            'stop_reason': project.get('stop_reason', ''),
        },
        'research_source_selection': selection,
        'report_quality': {'status': 'evidence_only'},
        'runtime': {'provider': 'local', 'model': '来源路线检查；未生成无依据的正文结论', 'seconds': 0},
    }
