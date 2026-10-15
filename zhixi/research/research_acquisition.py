"""Model-independent acquisition ordering; scheduling hints are not evidence."""

from collections import defaultdict
import json
import re
from zhixi.research.research_reasoning import pending_reorientation, query_origin_ids


class SearchAccessError(RuntimeError):
    def __init__(self, message, code='access_blocked', diagnostics=None):
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or []


def check_search_access(response, engine):
    """Access challenges are not empty searches; never solve/bypass them here."""
    status = getattr(response, 'status_code', 200)
    if status == 429:
        raise SearchAccessError(f'{engine} 搜索限流', 'rate_limited')
    if status == 403:
        raise SearchAccessError(f'{engine} 拒绝自动访问', 'access_blocked')
    url = str(getattr(response, 'url', ''))
    title = re.search(r'<title[^>]*>(.*?)</title>', response.text, re.I | re.S)
    title = title.group(1).strip() if title else ''
    if re.search(r'/(?:captcha|antispider|sorry)(?:/|\?)', url, re.I) or title in {
        '百度安全验证',
        'Startpage Blocked',
        'Access Verification',
        '访问验证',
    }:
        raise SearchAccessError(f'{engine} 搜索需要访问验证，未取得搜索结果', 'challenge_required')
    if re.search(
        r'anomaly-modal|id=["\']challenge-form|id=["\']b_captcha|'
        r'bots use DuckDuckGo|select all squares',
        response.text,
        re.I,
    ):
        raise SearchAccessError(f'{engine} 返回验证码，未取得搜索结果', 'challenge_required')


class SearchResults(list):
    """List-compatible results plus observable engine failures/empty batches."""

    def __init__(self, rows, diagnostics):
        super().__init__(rows)
        self.diagnostics = diagnostics


def search_unavailable(batches):
    """No usable engine response is an access failure, not a semantic gap.

    A normal empty response still permits query refinement. Partial successes
    must keep feeding acquisition even when another provider is unavailable.
    """
    failures = {
        'transport_error', 'http_error', 'access_blocked', 'rate_limited',
        'challenge_required', 'unexpected_redirect', 'unexpected_response',
        'parse_failed', 'response_too_large', 'access_cooldown', 'request_deadline',
    }
    return bool(batches) and all(batch.get('status') in failures for batch in batches)


def unavailable_search_batches(db, identity):
    """Derive current access failure from executor receipts, not model guesses.

    Reuse the search sub-state and latest attempt: a queued retry, successful
    response or ordinary empty response clears this scheduling gate. No timer,
    permanent blacklist or extra mutable availability state is introduced.
    """
    state = db.execute('SELECT search_status FROM research_projects WHERE id=?', (identity,)).fetchone()
    if not state or state[0] != 'failed':
        return []
    event = db.execute(
        'SELECT kind,details_json FROM research_events WHERE project_id=? '
        "AND kind IN ('search_started','search_engine_results','search_completed') "
        'ORDER BY rowid DESC LIMIT 1', (identity,),
    ).fetchone()
    batches = json.loads(event[1]).get('batches', []) if event and event[0] == 'search_engine_results' else []
    return batches if search_unavailable(batches) else []


def merge_search_results(completed, canonicalize, limit=None, *, relevance_fn=None):
    """Interleave engine ranks across queries, retaining every hit's provenance.

    A site with many useful pages is not penalized for sharing a hostname.
    None means retain all returned candidates, not unbounded network requests.
    """
    hits = []
    for index, engine, query, results, cache in completed:
        matches = (
            [
                relevance_fn(
                    query, row.get('title', '') + ' ' + row.get('description', ''), row.get('url', '')
                )
                for row in results
            ]
            if relevance_fn
            else [None] * len(results)
        )
        batch_status = 'query_text_not_found' if matches and all(v == 0 for v in matches) else 'returned'
        for rank, row in enumerate(results, 1):
            hits.append((rank, index, engine, query, row, cache, matches[rank - 1], batch_status))
    merged = {}
    for rank, index, engine, query, row, cache, match, batch_status in sorted(hits, key=lambda v: v[:3]):
        url = canonicalize(row.get('url', ''))
        if not url:
            continue
        hit = {
            'query': query,
            'search_engine': engine,
            'rank': rank,
            'title': row.get('title', ''),
            'description': row.get('description', ''),
            'search_cache': cache,
            'query_text_match': match,
            'batch_status': batch_status,
        }
        if url not in merged:
            merged[url] = {
                **row,
                'url': url,
                'query': query,
                'search_engine': engine,
                'search_cache': cache,
                'search_rank': rank,
                'search_hits': [],
                'query_text_match': match,
                'search_batch_status': batch_status,
                'discovery_order': len(merged),
            }
        if hit not in merged[url]['search_hits']:
            merged[url]['search_hits'].append(hit)
        if not merged[url].get('description') and hit['description']:
            merged[url]['description'] = hit['description']
        if (match or 0) > (merged[url].get('query_text_match') or 0):
            merged[url].update(
                query=query,
                search_engine=engine,
                search_rank=rank,
                query_text_match=match,
                search_batch_status=batch_status,
                title=row.get('title', ''),
                description=row.get('description', ''),
                search_cache=cache,
            )
    rows = list(merged.values())
    return rows if limit is None else rows[:limit]


def candidate_batch(rows, reviewed, limit, *, plan=None):
    """Work-conserving rotation of discovery branches, not domain/subject quotas.

    Ranking within a branch remains a hint. Every candidate stays in storage.
    Previously checked branches yield to untouched ones across worker restarts.
    """

    def branch(row):
        if row.get('parent_page_id'):
            return ('page', row['parent_page_id'])
        return ('query', row.get('query') or '')

    spent = defaultdict(int)
    for row in reviewed:
        spent[branch(row)] += 1
    queues = defaultdict(list)
    for row in rows:
        queues[branch(row)].append(row)
    for key, values in queues.items():
        if key[0] == 'query':
            values.sort(
                key=lambda row: (
                    row.get('search_batch_status') == 'query_text_not_found',
                    row.get('search_rank') if isinstance(row.get('search_rank'), int) else 10**9,
                )
            )
    from zhixi.research.research_reasoning import allocation_focus

    plan = plan or {}
    focus = allocation_focus(plan.get('exploration_review', {}).get('focus_ids', []))
    result = []
    while queues and len(result) < limit:
        key = min(
            queues,
            key=lambda k: (queues[k][0].get('search_batch_status') == 'query_text_not_found',
                           not bool(focus.intersection(query_origin_ids(queues[k][0].get('query'), plan))),
                           spent[k]),
        )
        result.append(queues[key].pop(0))
        spent[key] += 1
        if not queues[key]:
            del queues[key]
    return result


def has_approved_pages(db, identity, plan):
    """Already checked destinations can be fetched without another model call."""
    from zhixi.research.research_contracts import plan_fingerprint

    return bool(db.execute(
        'SELECT 1 FROM research_pages p JOIN research_route_checks c '
        'ON c.page_id=p.id AND c.project_id=p.project_id '
        "WHERE p.project_id=? AND p.status='queued' "
        "AND json_extract(c.decision_json,'$.decision')='visit' "
        "AND json_extract(c.decision_json,'$.plan_fingerprint')=? LIMIT 1",
        (identity, plan_fingerprint(plan)),
    ).fetchone())


def collection_turn(db, identity, plan):
    """Drain a checked acquisition batch, then yield to one reading step.

    This is not a time/coverage percentage and never overrides goal weights
    within the selected work type. Route checks and the event survive restart.
    Unchecked links still require review; this does not authorize a bulk crawl.
    """
    from zhixi.research.research_reading_state import has_unread, pending_followup

    budget = db.execute('SELECT max_pages FROM research_projects WHERE id=?', (identity,)).fetchone()
    processed = db.execute(
        'SELECT count(*) FROM research_pages WHERE project_id=? '
        "AND status IN ('fetched','duplicate','irrelevant','failed')",
        (identity,),
    ).fetchone()[0]
    if budget and processed >= budget[0]:
        return 'acquire'  # Reach the existing page-budget handoff before another read.
    if has_approved_pages(db, identity, plan):
        return 'acquire'
    unavailable = bool(unavailable_search_batches(db, identity))
    if pending_reorientation(plan) and not unavailable:
        return 'read'  # Resume the global action even without unread/local follow-up work.
    queued = db.execute(
        "SELECT 1 FROM research_pages WHERE project_id=? AND status='queued' LIMIT 1", (identity,)
    ).fetchone()
    unread = has_unread(db, identity, plan)
    pending = any(
        pending_followup(json.loads(row[0]))
        and not (unavailable and json.loads(row[0]).get('query_state') == 'pending')
        and json.loads(row[0]).get('feedback_round_id') == plan.get('feedback_round_id')
        for row in db.execute(
            'SELECT result_json FROM research_reading_rounds WHERE project_id=?', (identity,)
        )
    )
    if not unread and not pending:
        return 'acquire'  # Allows the collector's normal exhausted-queue exit.
    if not queued:
        return 'read'
    previous = db.execute(
        'SELECT details_json FROM research_events WHERE project_id=? '
        "AND kind='collection_turn' ORDER BY rowid DESC LIMIT 1",
        (identity,),
    ).fetchone()
    last = json.loads(previous[0]).get('action') if previous else None
    return 'acquire' if last == 'read' else 'read'


def navigation_context(db, identity, candidate):
    """Small, explicit location excerpt, never claim to have read the destination."""
    parent = candidate.get('parent_page_id')
    if not parent:
        return None
    row = db.execute(
        'SELECT title,url,content FROM research_pages WHERE id=? AND project_id=?', (parent, identity)
    ).fetchone()
    if not row:
        return None
    context = {
        'parent_page_id': parent,
        'parent_title': row[0],
        'parent_url': row[1],
        'link_text': candidate.get('title', ''),
        'meaning': 'parent_navigation_not_destination_evidence',
    }
    label, content = candidate.get('title') or '', row[2] or ''
    position = content.find(label) if label else -1
    if position >= 0:
        start, end = max(0, position - 160), min(len(content), position + len(label) + 160)
        context.update(excerpt=content[start:end], start=start, end=end, excerpt_is_partial=True)
    return context
