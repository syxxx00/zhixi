"""Collection clock and automatic report hand-off; never treats limits as knowledge completion."""

import json
import sqlite3
import time
from pathlib import Path
from threading import RLock, Lock, Thread, Event
from contextlib import contextmanager

from zhixi.ops import research_runtime_view
from zhixi.research import research_control
from zhixi.research import research_execution as execution
from zhixi.research.research_control import ControlConflict as ControlConflict  # HTTP command error.
from zhixi.ops.research_runtime_receipts import publication_index

LOCK = RLock()
SELECTIONS = {}  # API credentials, like job credentials, live only in this process.
GUARDS = set()
GUARD_LOCK = Lock()
HANDOFFS = {}  # Count overlapping handoffs; protected by the owning RESEARCH_LOCK.


@contextmanager
def worker_handoff(ctx, project_id, workers):
    """Keep scheduler ownership until the successor has been registered."""
    key = (str(ctx.DB_PATH), project_id)
    owner = execution.CURRENT.get()
    with ctx.RESEARCH_LOCK:
        current = execution.is_current(owner)
        execution.release(ctx.DB_PATH, project_id, workers, owner)
        if current:
            HANDOFFS[key] = HANDOFFS.get(key, 0) + 1
    try:
        yield current
    finally:
        with ctx.RESEARCH_LOCK:
            if current:
                count = HANDOFFS[key] - 1
                if count:
                    HANDOFFS[key] = count
                else:
                    HANDOFFS.pop(key)


def collection_exited(ctx, project_id, registry):
    """One handoff decision for both collectors; stale exits have no authority."""
    with worker_handoff(ctx, project_id, registry) as current:
        if not current:
            return
        with execution.bind(None):
            deliver_if_stopped(ctx, project_id)
            project = ctx.research_project(project_id)
            if (
                project
                and project['status'] in {'queued', 'crawling'}
                and project['search_status'] == 'queued'
            ):
                dispatch_collection(ctx, project_id, 'queued')


def guard_tick(ctx, project_id):
    """A worker crash must not leave a forever-running clock behind."""
    value = coordination_state(ctx, project_id)
    if not value or value['phase'] != 'collecting':
        return False
    expire_collection(ctx, project_id)
    with LOCK, ctx.RESEARCH_LOCK:
        working = (
            execution.working(ctx.DB_PATH, project_id, ctx.RESEARCH_WORKERS)
            or execution.working(ctx.DB_PATH, project_id, ctx.RESEARCH_DISCOVERY_WORKERS)
            or HANDOFFS.get((str(ctx.DB_PATH), project_id), 0) > 0
        )
        if working:
            return True
        # Make the no-owner decision and pause atomic with worker registration.
        with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
            db.execute('BEGIN IMMEDIATE')
            stopped = research_control.pause(
                db,
                project_id,
                ctx.utc_now(),
                time.time(),
                reason='worker_interrupted',
                error='搜集进程异常结束，已保留材料；可继续重试。',
            )
    if not stopped:
        deliver_if_stopped(ctx, project_id)
    return False


def start_guard(ctx, project_id):
    key = (str(ctx.DB_PATH), project_id)
    with GUARD_LOCK:
        if key in GUARDS:
            return
        GUARDS.add(key)

    def watch():
        try:
            while not Event().wait(1):
                if str(ctx.DB_PATH) != key[0]:
                    return
                try:
                    if not guard_tick(ctx, project_id):
                        return
                except sqlite3.OperationalError:
                    continue
        finally:
            with GUARD_LOCK:
                GUARDS.discard(key)

    try:
        Thread(target=watch, daemon=True, name=f'research-deadline-{project_id[:8]}').start()
    except Exception:
        with GUARD_LOCK:
            GUARDS.discard(key)
        raise


def initialize(db):
    db.execute(
        'CREATE TABLE IF NOT EXISTS research_workflows (project_id TEXT PRIMARY KEY, '
        'limit_seconds REAL NOT NULL, elapsed_seconds REAL NOT NULL DEFAULT 0, segment_started REAL, '
        "phase TEXT NOT NULL DEFAULT 'collecting', selection_json TEXT NOT NULL, "
        "report_job_id TEXT, error TEXT NOT NULL DEFAULT '')"
    )
    if 'delivery_mode' not in {r[1] for r in db.execute('PRAGMA table_info(research_workflows)')}:
        db.execute("ALTER TABLE research_workflows ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'report'")


def recover_runtime(db):
    """Startup-only recovery; schema initialization must not pause live work."""
    db.execute("UPDATE research_workflows SET segment_started=NULL,phase='paused' WHERE phase='collecting'")
    db.execute(
        "UPDATE research_workflows SET phase='report_failed',error='报告交接被服务重启中断，请重试整理' "
        "WHERE phase='dispatching'"
    )


def configure(ctx, project_id, minutes, selection, *, collection_only=False):
    with LOCK, sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        store_configuration(db, project_id, minutes, selection, collection_only=collection_only)
    SELECTIONS[(str(ctx.DB_PATH), project_id)] = selection


def store_configuration(db, project_id, minutes, selection, *, collection_only=False):
    """Store with project creation; credentials are cached only after commit."""
    config = selection.model_dump()
    if config.get('api_config'):
        config['api_config']['api_key'] = ''
    db.execute(
        'INSERT INTO research_workflows(project_id,limit_seconds,selection_json,delivery_mode) VALUES (?,?,?,?)',
        (
            project_id,
            minutes * 60,
            json.dumps(config, ensure_ascii=False),
            'collection_only' if collection_only else 'report',
        ),
    )


def continue_collection(db_path, project_id, minutes, config, expected, now, *, record_event):
    """Commit a user continuation as one transaction, rejecting stale requests.

    Pure storage boundary: no HTTP, model call, worker launch or app context.
    The caller validates credentials first and launches only after commit.
    """
    config = {**config}
    if config.get('api_config'):
        config['api_config'] = {**config['api_config'], 'api_key': ''}
    with sqlite3.connect(db_path, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute(
            'SELECT status,control_revision,search_status FROM research_projects WHERE id=?', (project_id,)
        ).fetchone()
        if not row:
            raise LookupError('研究项目不存在')
        if (row[0], row[1]) != expected or row[0] in {'queued', 'crawling', 'cancelled'}:
            raise ValueError('任务状态已变化，请刷新后再继续；未覆盖暂停或取消操作。')
        workflow = db.execute(
            'SELECT phase,report_job_id FROM research_workflows WHERE project_id=?', (project_id,)
        ).fetchone()
        if workflow:
            job = db.execute(
                'SELECT status FROM job_runs WHERE id=? AND project_id=?', (workflow[1], project_id)
            ).fetchone()
            if workflow[0] == 'dispatching' or (job and job[0] not in {'completed', 'failed', 'cancelled'}):
                raise ValueError('报告任务尚未结束，请先处理当前报告任务')
            if workflow[0] in {'reporting', 'report_paused'} and not job:
                raise ValueError('报告交接状态不完整，请先检查报告任务')
            addition = minutes * 60 if row[0] in {'completed', 'completed_with_errors'} else 0
            db.execute(
                "UPDATE research_workflows SET phase='collecting',segment_started=NULL,"
                "limit_seconds=limit_seconds+?,report_job_id=NULL,error='',selection_json=? WHERE project_id=?",
                (addition, json.dumps(config, ensure_ascii=False), project_id),
            )
        else:
            db.execute(
                'INSERT INTO research_workflows(project_id,limit_seconds,selection_json) VALUES (?,?,?)',
                (project_id, minutes * 60, json.dumps(config, ensure_ascii=False)),
            )
        db.execute(
            "UPDATE research_projects SET control_revision=control_revision+1,mode='automatic',status='queued',stop_reason='',error='',"
            'max_pages=MAX(max_pages,500+(SELECT COUNT(*) FROM research_pages WHERE project_id=? '
            "AND status IN ('fetched','duplicate','irrelevant','failed'))),"
            'max_depth=MAX(max_depth,20),updated_at=? WHERE id=?',
            (project_id, now, project_id),
        )
        db.execute(
            "UPDATE research_pages SET status='queued' WHERE project_id=? AND status IN ('candidate','skipped_budget')",
            (project_id,),
        )
        record_event(
            project_id, 'automatic_continued', '继续自动搜集并整理报告，无需逐页批准。', connection=db
        )
        search_status = row[2]
        if search_status == 'failed':
            # A failed transport has not executed the saved queries successfully.
            # Resume those queries, without asking the model to invent new ones.
            db.execute(
                "UPDATE research_projects SET search_status='queued',search_error='' WHERE id=?",
                (project_id,),
            )
            search_status = 'queued'
    return search_status


def _workflow_row(db, project_id):
    row = db.execute('SELECT * FROM research_workflows WHERE project_id=?', (project_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    start = data.pop('segment_started')
    data['elapsed_seconds'] += max(0, time.time() - start) if start is not None else 0
    return data


def coordination_state(ctx, project_id):
    """Raw persisted coordinator phase and clock, never a display decision."""
    with sqlite3.connect(Path(ctx.DB_PATH).resolve().as_uri() + '?mode=ro', uri=True, timeout=30) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='research_workflows'").fetchone():
            return None
        data = _workflow_row(db, project_id)
        if data is not None:
            data.pop('selection_json', None)
        return data


def state(ctx, project_id):
    """Public compatibility fields plus the shared runtime view; not a scheduler input."""
    with sqlite3.connect(Path(ctx.DB_PATH).resolve().as_uri() + '?mode=ro', uri=True, timeout=30) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'research_workflows' not in tables:
            return None  # Read-only callers can inspect an older database before migration.
        data = _workflow_row(db, project_id)
        if data is None:
            return None
        config = json.loads(data.pop('selection_json'))
        # Display only the selected model identity, never credentials/endpoints
        # or full local paths. A saved API key is deliberately not persisted.
        local = config.get('local_config') or {}
        api = config.get('api_config') or {}
        name = (
            api.get('model', 'API模型')
            if config.get('provider') == 'api'
            else str(local.get('path') or ctx.MODEL_PATH).replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
        )
        data['model_summary'] = {
            'provider': config.get('provider', 'local'),
            'name': name,
            'requires_reauthorization': config.get('provider') == 'api'
            and (str(ctx.DB_PATH), project_id) not in SELECTIONS,
        }
        db.create_function('research_runtime_view', 1, research_runtime_view.project_json, deterministic=True)
        projected = db.execute(
            'WITH '
            + research_runtime_view.ctes(tables)
            + ' SELECT runtime_json FROM research_runtime WHERE id=?',
            (json.dumps(publication_index(db, project_id)), project_id),
        ).fetchone()
        if projected:
            data['runtime'] = runtime = json.loads(projected[0])
            data.update(phase=runtime['phase'], stage=runtime['stage'], error=runtime['error'])
            # Retain the existing report fields; feedback ownership is explicit
            # in runtime.job and never substitutes a different report_job_id.
            if runtime['job']['role'] == 'report' and runtime['job']['status']:
                data['report_status'] = runtime['job']['status']
                data['report_provider'] = runtime['job']['provider']
    return data


def start_clock(ctx, project_id):
    with LOCK, execution.connection(ctx.DB_PATH, project_id) as db:
        db.execute(
            'UPDATE research_workflows SET segment_started=COALESCE(segment_started,?) '
            "WHERE project_id=? AND phase='collecting' AND EXISTS "
            "(SELECT 1 FROM research_projects WHERE id=? AND status IN ('queued','crawling'))",
            (time.time(), project_id, project_id),
        )


def stop_clock(ctx, project_id):
    """Suspend elapsed-time accounting while a worker waits; never change phase."""
    with LOCK, execution.connection(ctx.DB_PATH, project_id) as db:
        research_control.settle_clock(db, project_id, time.time())


def pause_collection(ctx, project_id):
    with LOCK, sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        if research_control.pause(db, project_id, ctx.utc_now(), time.time()):
            ctx.add_research_event(project_id, 'project_paused', '研究项目已暂停。', connection=db)


def pause_unavailable_search(ctx, project_id, diagnostics, db):
    """A bounded search failure cannot publish a completed research result.

    Caller has established that no local collection work remains and owns the
    execution-fenced transaction. Existing controls settle the clock atomically.
    """
    from zhixi.research.research_search_recovery import candidates, REASON

    automatic = bool(candidates(diagnostics)) and db.execute(
        'SELECT 1 FROM research_workflows WHERE project_id=?', (project_id,)
    ).fetchone() is not None
    message = ('搜索连接暂不可用，已保留查询与剩余时间；后台将低频探测，恢复后自动继续。' if automatic else
               '搜索服务受阻，已保留查询与剩余时间；请检查验证、响应或服务配置后手动继续，不代表没有资料。')
    if not research_control.pause(
        db, project_id, ctx.utc_now(), time.time(), reason=REASON if automatic else 'search_unavailable', error=message
    ):
        return False
    ctx.add_research_event(
        project_id, 'search_unavailable', message, level='warning',
        details={'batches': diagnostics, 'query_rewrite_skipped': True, 'automatic_recovery': automatic}, connection=db,
    )
    return True


def cancel_collection(ctx, project_id):
    with LOCK, sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        if research_control.cancel(db, project_id, ctx.utc_now(), time.time()):
            ctx.add_research_event(
                project_id,
                'project_cancelled',
                '研究项目已取消，已获取内容继续保留。',
                level='warning',
                connection=db,
            )


def resume_legacy_collection(ctx, project_id, expected):
    with LOCK, sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        search_status = research_control.resume_legacy(db, project_id, expected, ctx.utc_now())
        ctx.add_research_event(project_id, 'project_resumed', '研究项目继续运行。', connection=db)
    return search_status


def dispatch_collection(ctx, project_id, search_status):
    """Launch after commit; failed registration must not leave a queued project.

    A watchdog error after the collector started is different: that collector
    still owns its work, so do not label it as an absent worker or stop it here.
    """
    with LOCK:
        with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
            row = db.execute('SELECT status FROM research_projects WHERE id=?', (project_id,)).fetchone()
        if not row or row[0] not in {'queued', 'crawling'}:
            return False
        start = (
            ctx.start_research_discovery_worker
            if search_status in {'queued', 'running'}
            else ctx.start_research_worker
        )
        try:
            return start(project_id)
        except Exception as exc:
            with ctx.RESEARCH_LOCK:
                live = execution.working(ctx.DB_PATH, project_id, ctx.RESEARCH_WORKERS) or execution.working(
                    ctx.DB_PATH, project_id, ctx.RESEARCH_DISCOVERY_WORKERS
                )
                if not live:
                    with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
                        db.execute('BEGIN IMMEDIATE')
                        message = '搜集进程未能启动，已保留现场，可继续重试：' + str(exc)[:300]
                        if research_control.pause(
                            db,
                            project_id,
                            ctx.utc_now(),
                            time.time(),
                            reason='worker_start_failed',
                            error=message,
                        ):
                            ctx.add_research_event(
                                project_id, 'worker_start_failed', message, level='warning', connection=db
                            )
            raise


def expired(ctx, project_id):
    value = coordination_state(ctx, project_id)
    return bool(
        value and value['phase'] == 'collecting' and value['elapsed_seconds'] >= value['limit_seconds']
    )


def expire_collection(ctx, project_id):
    if not expired(ctx, project_id):
        return False
    with execution.connection(ctx.DB_PATH, project_id) as db:
        changed = db.execute(
            "UPDATE research_projects SET status='completed',stop_reason='time_limit_reached',"
            "updated_at=? WHERE id=? AND status IN ('queued','crawling') AND EXISTS "
            '(SELECT 1 FROM research_workflows w WHERE w.project_id=research_projects.id '
            "AND w.phase='collecting' AND w.elapsed_seconds+CASE WHEN w.segment_started IS NULL "
            'THEN 0 ELSE MAX(0,?-w.segment_started) END >= w.limit_seconds)',
            (ctx.utc_now(), project_id, time.time()),
        )
        if changed.rowcount:
            ctx.add_research_event(
                project_id,
                'collection_time_reached',
                '资料搜集时限已到，保留未访问链接；按任务交付方式结束采集或整理报告，不代表研究问题已解决。',
                connection=db,
            )
    return bool(changed.rowcount)


def extend_time(ctx, project_id, minutes):
    # Serialize with expiry's conditional UPDATE: whichever commits first wins.
    # Never reset segment_started, elapsed_seconds, worker state, or model selection.
    with LOCK, sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute(
            'SELECT p.status,w.phase FROM research_projects p LEFT JOIN research_workflows w '
            'ON w.project_id=p.id WHERE p.id=?',
            (project_id,),
        ).fetchone()
        if not row:
            raise LookupError('研究项目不存在')
        status, phase = row
        if status not in {'queued', 'crawling', 'paused'} or phase not in {'collecting', 'paused'}:
            raise ValueError('本轮采集已结束或正在整理报告，不能直接延长；请在报告结束后选择继续搜集。')
        db.execute(
            'UPDATE research_workflows SET limit_seconds=limit_seconds+? WHERE project_id=?',
            (minutes * 60, project_id),
        )
        db.execute('UPDATE research_projects SET updated_at=? WHERE id=?', (ctx.utc_now(), project_id))
        ctx.add_research_event(
            project_id,
            'collection_time_extended',
            f'搜集时限延长 {minutes} 分钟；已用时间、运行或暂停状态保持不变。',
            connection=db,
        )


def deliver_if_stopped(ctx, project_id):
    """Called only after a collection worker exits, never by a GET/history lookup."""
    with LOCK:
        value = coordination_state(ctx, project_id)
        if not value or value['phase'] != 'collecting':
            return
        with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
            project = db.execute('SELECT status FROM research_projects WHERE id=?', (project_id,)).fetchone()
            if not project or project[0] not in {'completed', 'completed_with_errors'}:
                return
            if project_id in ctx.RESEARCH_WORKERS or project_id in ctx.RESEARCH_DISCOVERY_WORKERS:
                return
        from zhixi.research.research_rounds import collection_stopped

        if collection_stopped(ctx, project_id):
            return
        reserved_id = ctx.uuid.uuid4().hex
        with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
            db.execute('BEGIN IMMEDIATE')
            # Recheck inside the write transaction, including cross-process controls.
            ready = db.execute(
                'SELECT w.delivery_mode FROM research_workflows w JOIN research_projects p ON p.id=w.project_id '
                "WHERE p.id=? AND w.phase='collecting' AND p.status IN ('completed','completed_with_errors')",
                (project_id,),
            ).fetchone()
            if not ready:
                return
            if ready[0] == 'collection_only':
                research_control.settle_clock(db, project_id, time.time(), 'collection_ready')
                ctx.add_research_event(
                    project_id,
                    'collection_test_saved',
                    '材料收集测试已结束，原文与调查过程已保存；未启动报告生成。',
                    connection=db,
                )
                return
            research_control.settle_clock(db, project_id, time.time(), 'dispatching')
            db.execute(
                'UPDATE research_workflows SET report_job_id=?,error=? WHERE project_id=?',
                (reserved_id, '', project_id),
            )
        try:
            selection = SELECTIONS.get((str(ctx.DB_PATH), project_id))
            if selection is None:
                with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
                    config = json.loads(
                        db.execute(
                            'SELECT selection_json FROM research_workflows WHERE project_id=?', (project_id,)
                        ).fetchone()[0]
                    )
                if config.get('provider') == 'api':
                    raise ValueError(
                        'API 凭据未保存，服务重启后请重新选择模型并整理报告；不会切换到其他模型。'
                    )
                selection = ctx.ResearchSynthesisRequest(**config)
            with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
                count = db.execute(
                    'SELECT COUNT(*) FROM research_evidence WHERE project_id=?', (project_id,)
                ).fetchone()[0]
            _dispatch_report(ctx, project_id, reserved_id, selection, empty=not count)
        except Exception as exc:
            with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
                research_control.finish_dispatch(db, project_id, reserved_id, 'report_failed', str(exc))


def _dispatch_report(ctx, project_id, reserved_id, selection, *, empty=False, retried_from=None, attempt=1):
    """Execute an already committed reservation; initial delivery and retry share this path.

    No write transaction is held during thread launch or publication. The receipt
    cannot overwrite cancellation, another reservation or a newer collection.
    """
    try:
        if empty:
            publish_empty_report(ctx, project_id, job_id=reserved_id)
        else:
            ctx.enqueue_research_synthesis(
                project_id,
                selection,
                job_id=reserved_id,
                retried_from=retried_from,
                attempt=attempt,
            )
    except Exception as exc:
        with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
            research_control.finish_dispatch(db, project_id, reserved_id, 'report_failed', str(exc))
        raise
    with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        research_control.finish_dispatch(
            db, project_id, reserved_id, 'report_ready' if empty else 'reporting'
        )


def retry_report(ctx, project_id, selection):
    with LOCK:
        reserved_id = ctx.uuid.uuid4().hex
        with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
            db.execute('BEGIN IMMEDIATE')
            previous_id, attempt = research_control.report_retry(db, project_id)
            # Configuration validation does not load a model or start a worker.
            ctx.validate_research_synthesis(selection)
            config = selection.model_dump()
            if config.get('api_config'):
                config['api_config']['api_key'] = ''
            db.execute(
                "UPDATE research_workflows SET phase='dispatching',report_job_id=?,selection_json=?,error='' WHERE project_id=?",
                (reserved_id, json.dumps(config, ensure_ascii=False), project_id),
            )
        SELECTIONS[(str(ctx.DB_PATH), project_id)] = selection
        _dispatch_report(ctx, project_id, reserved_id, selection, retried_from=previous_id, attempt=attempt)


def publish_empty_report(ctx, project_id, *, job_id=None):
    """No model can infer an answer from absent evidence. Deliver a saved, honest finding."""
    with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        goal, reason = db.execute(
            'SELECT goal,stop_reason FROM research_projects WHERE id=?', (project_id,)
        ).fetchone()
        pages = db.execute(
            'SELECT url,error FROM research_pages WHERE project_id=?', (project_id,)
        ).fetchall()
    job_id = job_id or ctx.uuid.uuid4().hex
    summary = '本次未取得可用于回答问题的正文证据，因此暂时无法得出有依据的结论。'
    details = [f'{url}：{error or "未取得可用正文"}' for url, error in pages]
    result = {
        'title': goal,
        'summary': summary,
        'tree': {
            'id': 'root',
            'label': goal,
            'note': '',
            'children': [
                {
                    'id': 'collection-obstacles',
                    'label': '遇到的阻碍',
                    'note': '\n'.join(details) or '没有发现可访问的公开入口。',
                    'children': [],
                }
            ],
        },
        'research': {
            'project_id': project_id,
            'goal': goal,
            'sources': [],
            'evidence': [],
            'limitations': [summary, f'搜集停止原因：{reason}'],
            'stop_reason': reason,
        },
        'report_quality': {'status': 'evidence_only', 'summary_status': 'needs_review'},
        'source': {
            'type': 'research',
            'title': goal,
            'url': '',
            'pages': len(pages),
            'evidence': 0,
            'characters': 0,
        },
        'keywords': [],
        'key_facts': [],
        'insights': [],
        'timeline': [],
        'runtime': {'model': '未调用模型（无可用证据）', 'provider': 'local', 'seconds': 0},
    }
    request = ctx.AnalyzeRequest(source_type='text', content=goal)
    ctx.publish_report(ctx.DB_PATH, job_id, project_id, result, request, ctx.save_history)
    return job_id
