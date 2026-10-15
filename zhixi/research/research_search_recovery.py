"""Low-frequency anonymous search recovery for automatically paused collection.

The existing pause reason is the opt-in; user pauses, old failures and finished
projects are never adopted. Probe receipts persist the backoff in research_events.
No model, new query, paid provider, collection clock or second task queue here.
"""

import datetime as dt
import json
import logging
import sqlite3
import time
from threading import Event, Lock, Thread

from zhixi.research.research_acquisition import SearchAccessError
from zhixi.sources import search_sources
from zhixi.sources import search_settings

REASON = 'search_recovery_wait'
DELAYS = (120, 300, 600, 900)
_tick_lock = Lock()
logger = logging.getLogger(__name__)


def retryable(batch):
    """Access refusals and broken parsing need attention, not repeated traffic."""
    if batch.get('engine') not in search_sources.ENDPOINTS:
        return False  # Never silently spend API quota for a health check.
    status = batch.get('status')
    details = batch.get('request_details') or []
    errors = [error for detail in details for error in detail.get('errors', [])]
    if status == 'access_cooldown':
        status = errors[-1].get('status') if errors else next(
            (detail.get('cause') for detail in details if detail.get('cause')), status
        )
    if any(error.get('cause') == 'tls_certificate' for error in errors):
        return False
    if status == 'http_error':
        return any(error.get('http_status') in {500, 502, 503, 504} for error in errors)
    return status in {'transport_error', 'request_deadline', 'rate_limited'}


def candidates(batches):
    selected = {}
    for batch in batches:
        if retryable(batch) and isinstance(batch.get('query'), str) and batch['query'].strip():
            selected.setdefault(batch['engine'], batch)
    return list(selected.values())


def delay(attempt, batches):
    requested = [float(detail.get('retry_after_seconds') or 0)
                 for batch in batches for detail in batch.get('request_details', [])]
    return max(DELAYS[min(attempt, len(DELAYS) - 1)], *requested, 0)


def _snapshot(db, project_id):
    row = db.execute(
        'SELECT p.control_revision,p.search_provider,p.public_search_topic,w.limit_seconds,w.elapsed_seconds '
        'FROM research_projects p JOIN research_workflows w ON w.project_id=p.id '
        "WHERE p.id=? AND p.status='paused' AND p.stop_reason=? AND w.phase='paused'",
        (project_id, REASON),
    ).fetchone()
    if not row or row[2] == '' or row[4] >= row[3]:
        return None
    pause = db.execute(
        "SELECT rowid,created_at,details_json FROM research_events WHERE project_id=? "
        "AND kind='search_unavailable' ORDER BY rowid DESC LIMIT 1", (project_id,),
    ).fetchone()
    if not pause:
        return None
    batches = json.loads(pause[2]).get('batches', [])
    probe = db.execute(
        "SELECT created_at,details_json FROM research_events WHERE project_id=? "
        "AND kind='search_recovery_probe' AND rowid>? ORDER BY rowid DESC LIMIT 1", (project_id, pause[0]),
    ).fetchone()
    receipt = json.loads(probe[1]) if probe else {}
    if receipt and receipt.get('revision') != row[0]:
        return None
    choices = [b for b in candidates(batches) if b['engine'] not in receipt.get('blocked_engines', [])]
    if not choices:
        return None
    last = dt.datetime.fromisoformat(probe[0] if probe else pause[1]).timestamp()
    return {
        'id': project_id, 'revision': row[0], 'provider': row[1], 'choices': choices,
        'attempt': receipt.get('attempt', 0), 'blocked_engines': receipt.get('blocked_engines', []),
        'due': receipt.get('next_check_at', last + delay(0, choices)),
    }


def tick(ctx, *, clock=time.time, stopped=lambda: False):
    """One serial pass. Network I/O never holds the SQLite writer lock."""
    if not _tick_lock.acquire(blocking=False):
        return
    try:
        with sqlite3.connect(ctx.DB_PATH, timeout=5) as db:
            ids = [r[0] for r in db.execute(
                "SELECT id FROM research_projects WHERE status='paused' AND stop_reason=? ORDER BY updated_at",
                (REASON,),
            )]
        for project_id in ids:
            if stopped():
                return
            with sqlite3.connect(ctx.DB_PATH, timeout=5) as db:
                state = _snapshot(db, project_id)
            if not state or state['due'] > clock():
                continue

            def active():
                if stopped():
                    return False
                with sqlite3.connect(ctx.DB_PATH, timeout=5) as db:
                    row = db.execute('SELECT status,stop_reason,control_revision FROM research_projects WHERE id=?',
                                     (project_id,)).fetchone()
                return row == ('paused', REASON, state['revision'])

            choice = state['choices'][state['attempt'] % len(state['choices'])]
            engine, query = choice['engine'], choice['query']
            receipt = {'revision': state['revision'], 'attempt': state['attempt'] + 1,
                       'engine': engine, 'query': query, 'blocked_engines': state['blocked_engines'],
                       'model_called': False, 'collection_clock_started': False}
            blocked = False
            try:
                selected = search_settings.selection(ctx.DATA_DIR, None if state['provider'] == 'hybrid'
                                                     else 'hybrid')
                if selected.provider != 'hybrid' or state['provider'] not in {'hybrid', engine}:
                    raise ValueError('搜索服务设置已改变，请确认后手动继续；未自动切换服务。')
                rows, _ = ctx.cached_search_discovery(
                    engine, query,
                    lambda q, n: search_sources.discover(engine, q, n, should_continue=active), 10, fresh=True,
                )
                receipt.update(status='healthy', returned=len(rows))
                message = '搜索探测取得正常响应，准备沿原查询和剩余时间继续；不代表已取得正文。'
            except SearchAccessError as exc:
                if not active():
                    continue
                batch = {'engine': engine, 'status': exc.code, 'request_details': exc.diagnostics}
                receipt.update(status=exc.code)
                if not retryable(batch):
                    receipt['blocked_engines'] = [*receipt['blocked_engines'], engine]
                blocked = all(b['engine'] in receipt['blocked_engines'] for b in state['choices'])
                receipt['next_check_at'] = clock() + delay(receipt['attempt'], [batch])
                message = ('搜索返回验证、拒绝或异常页面，已停止自动探测，请处理后手动继续。' if blocked else
                           '搜索仍受阻，保留现场并降低探测频率；未调用模型或消耗采集时间。')
            except ValueError as exc:
                blocked = True
                receipt.update(status='settings_changed')
                message = str(exc)

            if not active():
                continue
            with sqlite3.connect(ctx.DB_PATH, timeout=5) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT status,stop_reason,control_revision FROM research_projects WHERE id=?',
                                 (project_id,)).fetchone()
                if row != ('paused', REASON, state['revision']) or stopped():
                    continue
                ctx.add_research_event(project_id, 'search_recovery_probe', message,
                                       details=receipt, connection=db)
                db.execute('UPDATE research_projects SET error=?,updated_at=? WHERE id=?',
                           (message, ctx.utc_now(), project_id))
                if blocked:
                    db.execute("UPDATE research_projects SET stop_reason='search_unavailable',"
                               'control_revision=control_revision+1 WHERE id=?', (project_id,))
            if receipt['status'] == 'healthy' and not stopped():
                # Canonical continuation validates the original model and fences
                # the same revision again, including pauses racing this receipt.
                try:
                    ctx.continue_after_search_probe(project_id, ('paused', state['revision']))
                except Exception as exc:
                    with sqlite3.connect(ctx.DB_PATH, timeout=5) as db:
                        changed = db.execute(
                            "UPDATE research_projects SET stop_reason='search_unavailable',"
                            "control_revision=control_revision+1,error=? WHERE id=? AND status='paused' "
                            'AND stop_reason=? AND control_revision=?',
                            ('搜索已恢复，但原任务需手动确认后继续。', project_id, REASON, state['revision']),
                        )
                        if changed.rowcount:
                            ctx.add_research_event(project_id, 'search_recovery_needs_attention',
                                                   '搜索已恢复，但原模型或任务配置需要重新确认；未切换模型。',
                                                   details={'error_type': type(exc).__name__}, connection=db)
    finally:
        _tick_lock.release()


def start(ctx):
    """One service-owned watcher, stopped by lifespan; no Codex dependency."""
    stop = Event()

    def watch():
        while not stop.wait(15):
            try:
                tick(ctx, stopped=stop.is_set)
            except Exception:
                logger.exception('Search recovery tick failed; paused tasks retained')

    thread = Thread(target=watch, daemon=True, name='research-search-recovery')
    thread.start()
    return stop, thread
