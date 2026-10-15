"""Explicit user entrypoints are reading requests, not factual endorsements."""

import json
from urllib.parse import urlparse


def normalize_seeds(ctx, values):
    result = []
    for index, value in enumerate(values, 1):
        value = ctx.normalize_url(value)
        if not value.strip():
            continue
        try:
            parsed = urlparse(value)
            canonical = ctx.canonicalize_url(value)
            valid = bool(canonical) and not (parsed.username or parsed.password)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                f'第{index}个起始网址格式无效：请提供完整的 http:// 或 https:// 公开网址，不含账号密码。未忽略该入口，也未转为自动搜索。'
            )
        if canonical not in result:
            result.append(canonical)
    return result


def is_user_seed(row):
    row = dict(row)
    extraction = row.get('extraction_json') or '{}'
    try:
        extraction = json.loads(extraction) if isinstance(extraction, str) else extraction
    except (ValueError, TypeError):
        extraction = {}
    discovery = extraction.get('discovery', {}) if isinstance(extraction, dict) else {}
    if 'explicit_user_seed' in discovery:
        return discovery['explicit_user_seed'] is True
    # Old tasks retain a recognizable creation label. Do not reinterpret all
    # depth-zero search results as user input, or local attachments as URLs.
    return row.get('depth') == 0 and row.get('link_text') == '起始页面' and not discovery.get('search_engine')


def has_pending(db, project_id):
    fields = ('depth', 'link_text', 'extraction_json')
    return any(
        is_user_seed(dict(zip(fields, row)))
        for row in db.execute(
            'SELECT depth,link_text,extraction_json FROM research_pages '
            "WHERE project_id=? AND depth=0 AND status='queued'",
            (project_id,),
        )
    )


def scope_hosts(db, project_id):
    rows = [
        dict(zip(('canonical_url', 'depth', 'link_text', 'extraction_json'), r))
        for r in db.execute(
            'SELECT canonical_url,depth,link_text,extraction_json FROM research_pages WHERE project_id=? AND depth=0',
            (project_id,),
        )
    ]
    explicit = [row for row in rows if is_user_seed(row)]
    # With no supplied seeds, the initial discovered entrypoints bootstrap
    # same-host research as before. Search results cannot widen explicit scope.
    return {
        urlparse(row['canonical_url']).hostname
        for row in (explicit or rows)
        if urlparse(row['canonical_url']).scheme in {'http', 'https'}
    }


def approve_pending(ctx, db, project_id, plan, fingerprint):
    if not plan.get('questions'):
        return 0
    fields = ('id', 'depth', 'link_text', 'extraction_json')
    rows = [
        dict(zip(fields, row))
        for row in db.execute(
            "SELECT id,depth,link_text,extraction_json FROM research_pages WHERE project_id=? AND depth=0 AND status='queued'",
            (project_id,),
        )
    ]
    question = next(
        (q for q in plan['questions'] if q.get('question') == plan.get('subject')), plan['questions'][0]
    )
    inserted = 0
    for row in rows:
        if not is_user_seed(row):
            continue
        previous = db.execute(
            'SELECT decision_json FROM research_route_checks WHERE page_id=?', (row['id'],)
        ).fetchone()
        if previous and json.loads(previous[0]).get('plan_fingerprint') == fingerprint:
            continue
        decision = dict(
            decision='visit',
            question_ids=[question['id']],
            purpose='probe',
            expected_usefulness=0.5,
            reason='用户指定的起始网址，先读取实际内容以判断用途；不代表内容可信、相关或结论已成立。',
            basis='explicit_user_read_request_not_evidence',
            plan_fingerprint=fingerprint,
        )
        db.execute(
            'INSERT OR REPLACE INTO research_route_checks VALUES (?,?,?)',
            (row['id'], project_id, json.dumps(decision, ensure_ascii=False)),
        )
        inserted += 1
    if inserted:
        ctx.add_research_event(
            project_id,
            'user_entrypoints_scheduled',
            f'{inserted}个用户起始网址已安排读取；仍执行公开地址、robots和正文用途检查。',
            connection=db,
        )
    return inserted
