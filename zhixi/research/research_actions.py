"""Turn known links from a read public page into concrete queued visits."""

import json
import sqlite3
from zhixi.research import research_execution as execution


def follow_read_links(ctx, project, plan, record):
    from zhixi.research import research_model_calls as intent

    page_ids = sorted({s['page_id'] for s in record['source_spans']})
    if not page_ids:
        return []
    fingerprint = intent.plan_fingerprint(plan)
    with execution.connection(ctx.DB_PATH, project['id']) as db:
        db.row_factory = sqlite3.Row
        placeholders = ','.join('?' for _ in page_ids)
        rows = db.execute(
            'SELECT p.id,p.canonical_url AS url,p.link_text AS title,p.parent_page_id '
            'FROM research_pages p JOIN research_pages parent ON parent.id=p.parent_page_id AND parent.project_id=p.project_id '
            f"WHERE p.project_id=? AND p.parent_page_id IN ({placeholders}) AND p.status='queued' "
            "AND parent.status='fetched' AND (EXISTS (SELECT 1 FROM research_public_captures c "
            'WHERE c.page_id=parent.id AND c.project_id=p.project_id) OR '
            "json_extract(CASE WHEN json_valid(parent.extraction_json) THEN parent.extraction_json ELSE '{}' END, "
            "'$.retrieval_access')='anonymous_public_http') "
            'AND EXISTS (SELECT 1 FROM research_links l WHERE l.project_id=p.project_id '
            'AND l.source_page_id=parent.id AND l.target_page_id=p.id) '
            'AND NOT EXISTS (SELECT 1 FROM research_route_checks r WHERE r.page_id=p.id '
            "AND json_extract(r.decision_json,'$.plan_fingerprint')=?) "
            'ORDER BY p.relevance DESC,p.created_at ASC LIMIT ?',
            (project['id'], *page_ids, fingerprint, intent.ROUTE_REVIEW_BATCH_SIZE),
        ).fetchall()
    if not rows:
        return []
    candidates = [dict(r) for r in rows]
    context = {
        'source_spans': record['source_spans'],
        'action_context': '这些链接已从获准读取的页面实际发现。选择能取得所需具体内容的链接；后续仍执行地址与研究范围检查。这不授予把来源或研究意图外发搜索的权限。目录、链接名称和页码只定位目标，不证明内容已取得。',
    }
    try:
        from zhixi.research.research_capacity import route_decisions, cached_call

        def raw_call(system, data, validate):
            return intent.validated_reason(ctx, project, system, data, validate)

        def call(system, data, validate):
            return cached_call(ctx, project, system, data, raw_call, validate)

        decisions = route_decisions(call, intent.LINK_SYSTEM, plan, candidates, context=context)
    except intent.InvalidReasoningOutput as exc:
        ctx.add_research_event(project['id'], 'direct_link_check_rejected', str(exc))
        return []
    if not intent.active(ctx, project['id']) or ctx.research_workflow.expire_collection(ctx, project['id']):
        return []
    chosen = []
    with execution.connection(ctx.DB_PATH, project['id']) as db:
        for identity, decision in decisions.items():
            decision.update(plan_fingerprint=fingerprint, action='follow_discovered_link')
            db.execute(
                'INSERT OR REPLACE INTO research_route_checks VALUES (?,?,?)',
                (identity, project['id'], json.dumps(decision, ensure_ascii=False)),
            )
            if decision['decision'] == 'visit':
                chosen.append(identity)
            else:
                db.execute(
                    "UPDATE research_pages SET status='deferred_goal',error=? WHERE id=? AND project_id=? AND status='queued'",
                    (decision['reason'], identity, project['id']),
                )
        ctx.add_research_event(
            project['id'],
            'reading_links_dispatched',
            f'已读页面的具体链接：选择{len(chosen)}条交给抓取队列',
            details={
                'candidates': candidates,
                'decisions': decisions,
                'selected_ids': chosen,
                'reading_source_ids': record.get('source_ids', []),
            },
            connection=db,
        )
    return chosen
