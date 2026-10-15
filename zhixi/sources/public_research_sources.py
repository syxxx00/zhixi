"""Execution-owned provenance for material eligible to inform public queries.

A URL in an attachment/report is not public provenance. Eligibility requires
anonymous retrieval plus a search-discovered URL or a link from such a capture.
Private attachments and model drafts never enter this table through input APIs.
"""

import hashlib
import json
import sqlite3
from zhixi.content.source_identity import source_context


def query_outcome(check, new_queries):
    """Describe the planning result, never evidence quality or search success."""
    batches = [b for b in check.get('query_batches', []) if not b.get('status', '').endswith('_split')]
    approved = {q['query'].strip() for b in batches for q in b.get('accepted', [])}
    rejected = sum(not c['faithful'] for b in batches for c in (b.get('audit') or {}).get('checks', [])
                   if c['id'].startswith('query:'))
    incomplete = any(b.get('status', '').endswith('_unresolved') for b in batches)
    invalid = any(b.get('rejected_proposals') for b in batches)
    status = ('not_authorized' if check.get('reason') == 'public_search_not_authorized' else
              'queued' if new_queries else
              'output_incomplete' if incomplete else
              'duplicates_only' if approved else
              'audit_rejected' if rejected else
              'invalid_proposals' if invalid else
              'no_proposals' if batches else 'unknown')
    return {'status': status, 'approved_count': len(approved), 'audit_rejected_count': rejected,
            'new_query_count': len(new_queries), 'output_incomplete': incomplete,
            'meaning': 'query_planning_result_not_search_execution_or_evidence'}


def reorientation_feedback(db, project_id, *, budget=1200):
    """Whitelist public query outcomes; never copy a reading/plan/error record."""
    row = db.execute(
        "SELECT rowid,id,details_json FROM research_events WHERE project_id=? "
        "AND kind='reorientation_queries_completed' ORDER BY rowid DESC LIMIT 1", (project_id,),
    ).fetchone()
    if not row:
        return None
    details = json.loads(row[2])
    check = details.get('query_check', {})
    queries = details.get('new_queries', [])
    feedback = {'event_id': row[1], 'outcome': query_outcome(check, queries),
                'search_request_outcomes': {}, 'rejected_queries': [], 'omitted_rejections': 0}
    # Queuing a query is not executing it. Match only subsequent executor
    # receipts for this action's actual queries, including recovery attempts.
    feedback['search_request_outcomes'] = dict(db.execute(
        "SELECT json_extract(b.value,'$.status'),COUNT(*) FROM research_events e,"
        "json_each(e.details_json,'$.batches') b WHERE e.project_id=? AND e.rowid>? "
        "AND e.kind='search_engine_results' AND json_extract(b.value,'$.query') "
        "IN (SELECT value FROM json_each(?)) GROUP BY 1",
        (project_id, row[0], json.dumps(queries)),
    ))
    rejected = []
    for batch in check.get('query_batches', []):
        by_id = {f'query:{i}': q for i, q in enumerate(batch.get('proposals', []))}
        for item in (batch.get('audit') or {}).get('checks', []):
            if not item['faithful'] and item['id'] in by_id:
                rejected.append({'query': by_id[item['id']]['query'], 'reason': item['reason']})
    feedback['omitted_rejections'] = len(rejected)
    for item in rejected:
        candidate = {**feedback, 'rejected_queries': [*feedback['rejected_queries'], item],
                     'omitted_rejections': feedback['omitted_rejections'] - 1}
        if len(json.dumps(candidate, ensure_ascii=False)) <= budget:
            feedback = candidate
    return feedback


def search_context(db_path, project_id, *, budget=1800):
    """A bounded view of public activity, not a checklist or semantic coverage.

    Only execution-owned search events and registered anonymous captures enter
    this view. Never project private questions, reading notes or report prose
    into outbound queries, even when they cite a public page. Whole records are
    kept or omitted; the view is explicitly partial and cannot prove absence.
    """
    context = {
        'phase': 'continuation',
        'meaning': 'public_activity_not_verified_facts_or_completed_requirements',
        'history_scope': 'partial_recent_view_not_all_materials',
        'initial_queries': [],
        'recent_searches': [],
        'recent_materials': [],
        'preferred_starting_queries': [],
    }
    with sqlite3.connect(db_path, timeout=30, uri=True) as db:
        context['public_capture_count'] = db.execute(
            'SELECT COUNT(*) FROM research_public_captures WHERE project_id=?', (project_id,)
        ).fetchone()[0]
        # Reserve space for execution feedback before filling optional history.
        action = reorientation_feedback(db, project_id, budget=budget // 2)
        if action and len(json.dumps({**context, 'last_reorientation': action}, ensure_ascii=False)) <= budget:
            context['last_reorientation'] = action
        # These events are emitted by the public discovery executor. Initial
        # queries describe attempted directions, not immutable sub-requirements.
        initial = db.execute(
            'SELECT value FROM json_each(COALESCE((SELECT details_json FROM research_events '
            "WHERE project_id=? AND kind='search_started' ORDER BY rowid LIMIT 1),'{}'),'$.queries')",
            (project_id,),
        )
        searches = db.execute(
            "SELECT json_extract(b.value,'$.query'),json_extract(b.value,'$.engine'),"
            "json_extract(b.value,'$.status'),json_extract(b.value,'$.returned') "
            "FROM research_events e,json_each(e.details_json,'$.batches') b "
            "WHERE e.project_id=? AND e.kind='search_engine_results' "
            'ORDER BY e.rowid DESC,CAST(b.key AS INTEGER)',
            (project_id,),
        )
        materials = db.execute(
            "SELECT c.page_id,c.url,json_extract(c.provenance_json,'$.source_title'),p.status "
            'FROM research_public_captures c JOIN research_pages p '
            'ON p.id=c.page_id AND p.project_id=c.project_id '
            'WHERE c.project_id=? ORDER BY c.rowid DESC',
            (project_id,),
        )
        review = db.execute(
            "SELECT rowid,details_json FROM research_events WHERE project_id=? AND kind='exploration_review' "
            'ORDER BY rowid DESC LIMIT 1', (project_id,),
        ).fetchone()
        consumed = review and db.execute(
            "SELECT 1 FROM research_events WHERE project_id=? AND rowid>? "
            "AND kind IN ('reading_queries_completed','reorientation_queries_completed','search_started') LIMIT 1",
            (project_id, review[0]),
        ).fetchone()
        preference = json.loads(review[1]).get('preferred_public_queries', []) if review and not consumed else []
        streams = {
            'preferred_starting_queries': iter(preference),
            'initial_queries': (r[0] for r in initial),
            'recent_searches': (
                dict(zip(('query', 'engine', 'status', 'returned_candidates'), r)) for r in searches
            ),
            'recent_materials': (
                dict(zip(('page_id', 'url', 'captured_title', 'fetch_status'), r)) for r in materials
            ),
        }
        # Share the input budget across directions, outcomes and captures so a
        # long list of queries cannot hide every other kind of progress.
        while streams:
            for field in list(streams):
                row = next(streams[field], None)
                if row is None:
                    del streams[field]
                    continue
                context[field].append(row)
                if len(json.dumps(context, ensure_ascii=False)) > budget:
                    context[field].pop()
                    del streams[field]
    return context


def initialize(db):
    db.execute(
        'CREATE TABLE IF NOT EXISTS research_public_captures (page_id TEXT PRIMARY KEY, '
        'project_id TEXT NOT NULL,url TEXT NOT NULL,content_sha256 TEXT NOT NULL,provenance_json TEXT NOT NULL)'
    )


def register_capture(db, project_id, page, final_url, content, cleaning, discovery, *, title):
    if cleaning.get('retrieval_access') != 'anonymous_public_http':
        return False
    parent = page['parent_page_id']
    engine = (discovery or {}).get('search_engine')
    public_parent = (
        db.execute(
            'SELECT 1 FROM research_public_captures WHERE page_id=? AND project_id=?', (parent, project_id)
        ).fetchone()
        if parent
        else None
    )
    if not engine and not public_parent:
        return False
    provenance = {
        'origin': 'search_discovery' if engine else 'public_source_link',
        'engine': engine,
        'parent_page_id': parent,
        'access': 'anonymous_http',
        'source_title': title,
        'meaning': 'publicly_retrieved_material_not_verified_facts',
        'source_context': source_context({'extraction': cleaning}),
    }
    db.execute(
        'INSERT OR REPLACE INTO research_public_captures VALUES (?,?,?,?,?)',
        (
            page['id'],
            project_id,
            final_url,
            hashlib.sha256(content.encode()).hexdigest(),
            json.dumps(provenance),
        ),
    )
    return True


def candidates(db_path, project_id, source_spans=None):
    """Return public originals, optionally restricted to this reading increment.

    A read span is a locator, not trusted text. Reconstruct it from the hashed
    public capture; never forward the reading's notes or supplied quote/title.
    This avoids reselecting every sentence on a large page after each read.
    The legacy evidence-based view remains available for whole-report feedback.
    """
    requested = None if source_spans is None else {(s['page_id'], s['id']) for s in source_spans}
    if requested == set():
        return []
    requested_pages = sorted({pid for pid, _ in requested}) if requested is not None else []
    with sqlite3.connect(db_path, timeout=30, uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            'SELECT p.id,p.url,p.title,p.content,c.content_sha256,c.provenance_json FROM research_pages p '
            'JOIN research_public_captures c ON c.page_id=p.id AND c.project_id=p.project_id '
            "WHERE p.project_id=? AND p.status='fetched' AND p.url=c.url"
            + (' AND p.id IN (' + ','.join('?' for _ in requested_pages) + ')' if requested_pages else ''),
            (project_id, *requested_pages),
        ).fetchall()
        sources = []
        for page in rows:
            if hashlib.sha256(page['content'].encode()).hexdigest() != page['content_sha256']:
                continue
            provenance = json.loads(page['provenance_json'])
            title = provenance['source_title']
            context = provenance.get('source_context') or {}
            if requested is not None:
                from zhixi.research.research_reading_state import page_spans

                for span in page_spans(page['id'], title, page['content'], page['url']):
                    if (page['id'], span['id']) in requested:
                        sources.append(
                            {
                                'id': span['id'],
                                'page_id': page['id'],
                                'title': title,
                                'quote': span['quote'],
                                'quote_offset': span['offset'],
                                'original_quote_characters': span['original_characters'],
                                'origin': 'public_capture_not_verified_fact',
                                **({'source_context': context} if context else {}),
                            }
                        )
                continue
            from zhixi.content.evidence_context import research_original_units

            literal_source = '\n'.join(research_original_units(page['content']))
            for row in db.execute(
                'SELECT id,quote,evidence_type FROM research_evidence WHERE project_id=? AND page_id=?',
                (project_id, page['id']),
            ):
                if row['evidence_type'] in {'model_summary', 'model_draft'}:
                    continue
                if row['quote'] not in page['content'] and row['quote'] not in literal_source:
                    continue
                for offset in range(0, len(row['quote']), 2000):
                    sources.append(
                        {
                            'id': f'{row["id"]}@{offset}',
                            'evidence_id': row['id'],
                            'page_id': page['id'],
                            'title': title,
                            'quote': row['quote'][offset : offset + 2000],
                            'quote_offset': offset,
                            'original_quote_characters': len(row['quote']),
                            'origin': 'public_capture_not_verified_fact',
                            **({'source_context': context} if context else {}),
                        }
                    )
    return sources
