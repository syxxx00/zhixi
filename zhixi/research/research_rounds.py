"""Same-project collection rounds and their ownership of report hand-off.

This ledger does not grant search permission. Starting a round requires an
explicit collection allowance and inherits the project's public search scope.
Questions, draft answers, credentials and private attachments are not queries.
"""

import json
import sqlite3
import time
import uuid
from zhixi.research import research_execution as execution


class RoundConflict(ValueError):
    pass


class RoundBusy(RoundConflict):
    """Another nonterminal execution owns this project; wait without new time."""


def initialize(db):
    db.execute(
        'CREATE TABLE IF NOT EXISTS research_rounds ('
        'id TEXT PRIMARY KEY, project_id TEXT NOT NULL, question_id TEXT NOT NULL UNIQUE, '
        'owner_job_id TEXT NOT NULL, base_report_version INTEGER NOT NULL, '
        'phase TEXT NOT NULL, focus_json TEXT NOT NULL, allowance_seconds REAL NOT NULL DEFAULT 0, '
        "created_at REAL NOT NULL, updated_at REAL NOT NULL, outcome_json TEXT NOT NULL DEFAULT '{}')"
    )
    db.execute('CREATE INDEX IF NOT EXISTS rounds_project ON research_rounds(project_id,created_at)')


def recover_runtime(db):
    # Existing workers are not evidence of live execution after a server restart.
    # Preserve the round for explicit recovery, without resuming paused projects.
    db.execute(
        "UPDATE research_rounds SET phase='interrupted',updated_at=? WHERE phase IN ('collecting','revising')",
        (time.time(),),
    )


def _row(db, identity):
    db.row_factory = sqlite3.Row
    row = db.execute('SELECT * FROM research_rounds WHERE id=?', (identity,)).fetchone()
    if row is None:
        raise ValueError('补查轮次不存在')
    item = dict(row)
    item['focus'] = json.loads(item.pop('focus_json'))
    item['outcome'] = json.loads(item.pop('outcome_json'))
    return item


def read(db_path, identity):
    with sqlite3.connect(db_path, timeout=30) as db:
        return _row(db, identity)


def claim_retry(db_path, identity, job_id):
    """Only a persisted retry descendant can inherit a feedback round.

    The old job must already be terminal. This is not permission to resume a
    user-paused collector or to replenish its allowance.
    """
    with sqlite3.connect(db_path, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        item = _row(db, identity)
        if item['owner_job_id'] == job_id:
            return item
        owner = db.execute(
            'SELECT status FROM job_runs WHERE id=? AND project_id=?',
            (item['owner_job_id'], item['project_id']),
        ).fetchone()
        if not owner or owner[0] not in {'failed', 'cancelled'}:
            raise RoundConflict('原反馈任务尚未失败或取消，不能接管')
        cursor, seen = job_id, set()
        while cursor and cursor not in seen and cursor != item['owner_job_id']:
            seen.add(cursor)
            row = db.execute(
                "SELECT retried_from FROM job_runs WHERE id=? AND project_id=? AND kind='report_question'",
                (cursor, item['project_id']),
            ).fetchone()
            cursor = row[0] if row else None
        if cursor != item['owner_job_id']:
            raise RoundConflict('新任务不属于原反馈的重试链')
        db.execute(
            'UPDATE research_rounds SET owner_job_id=?,updated_at=? WHERE id=?',
            (job_id, time.time(), identity),
        )
        return _row(db, identity)


def stop_owned_collection(ctx, identity, owner_job_id, *, cancelled=False, error=''):
    """Stop only this round's execution; retain pages and any manual pause.

    Updating clock, round and project together prevents a half-stopped round
    from looking like completed research or being handed to report synthesis.
    """
    with ctx.research_workflow.LOCK, sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        item = _row(db, identity)
        if item['owner_job_id'] != owner_job_id:
            raise RoundConflict('不能停止其他反馈任务的补查')
        if item['phase'] in {'planned', 'interrupted'} and cancelled:
            db.execute(
                "UPDATE research_rounds SET phase='cancelled',updated_at=? WHERE id=?",
                (time.time(), identity),
            )
            return _row(db, identity)  # Waiting never owned the project or clock.
        if item['phase'] != 'collecting':
            return item
        phase = 'cancelled' if cancelled else 'interrupted'
        now = time.time()
        # An old feedback failure does not own a later cancellation or report handoff.
        db.execute(
            'UPDATE research_workflows SET elapsed_seconds=elapsed_seconds+CASE WHEN segment_started IS NULL '
            "THEN 0 ELSE MAX(0,?-segment_started) END,segment_started=NULL,phase='paused' WHERE project_id=? "
            "AND phase IN ('collecting','paused') AND EXISTS (SELECT 1 FROM research_projects p "
            "WHERE p.id=research_workflows.project_id AND p.status IN ('queued','crawling','paused'))",
            (now, item['project_id']),
        )
        from zhixi.research.research_control import pause

        pause(
            db,
            item['project_id'],
            ctx.utc_now(),
            now,
            reason='feedback_cancelled' if cancelled else 'feedback_interrupted',
            error=error,
        )
        outcome = {**item['outcome'], 'execution_error': error, 'question_answered': False}
        db.execute(
            'UPDATE research_rounds SET phase=?,outcome_json=?,updated_at=? WHERE id=?',
            (phase, json.dumps(outcome, ensure_ascii=False), now, identity),
        )
        ctx.add_research_event(
            item['project_id'],
            'feedback_collection_stopped',
            '补查执行已停止，材料和原报告保留',
            details={'round_id': identity, 'phase': phase, 'error': error},
            connection=db,
        )
        return _row(db, identity)


def dispatch_collection(ctx, identity, owner_job_id):
    """Launch the existing collector, not an independent second research task."""
    item = read(ctx.DB_PATH, identity)
    if item['owner_job_id'] != owner_job_id or item['phase'] != 'collecting':
        raise RoundConflict('只有当前轮次的执行者可以启动搜集')
    project = ctx.research_project(item['project_id'])
    if not project or project['status'] not in {'queued', 'crawling'}:
        raise RoundConflict('研究已停止或暂停，未启动搜集')
    try:
        worker = (
            ctx.start_research_discovery_worker
            if project['search_status'] in {'queued', 'running'}
            else ctx.start_research_worker
        )
        started = worker(item['project_id'])
        if not started:
            with ctx.RESEARCH_LOCK:
                live = execution.working(
                    ctx.DB_PATH, item['project_id'], ctx.RESEARCH_WORKERS
                ) or execution.working(ctx.DB_PATH, item['project_id'], ctx.RESEARCH_DISCOVERY_WORKERS)
            if not live:
                raise RuntimeError('搜集进程没有启动')
        return read(ctx.DB_PATH, identity)
    except Exception as exc:
        stop_owned_collection(ctx, identity, owner_job_id, error=str(exc))
        raise


def planning_view(db_path, project):
    """Give the planner feedback context without changing the original goal or
    making private feedback part of the legacy implicit public-search topic.
    """
    with sqlite3.connect(db_path, timeout=30) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='research_rounds' AND type='table'"
        ).fetchone():
            return dict(project)
        row = db.execute(
            "SELECT id,focus_json FROM research_rounds WHERE project_id=? AND phase='collecting'",
            (project['id'],),
        ).fetchone()
    if not row:
        return dict(project)
    focus = json.loads(row[1])
    public = project.get('public_search_topic')
    if public is None:
        public = project['goal']  # Resolve legacy consent BEFORE attaching feedback.
    return {
        **project,
        'original_goal': project['goal'],
        'feedback_round_id': row[0],
        'public_search_topic': public,
        'goal': project['goal']
        + '\n\n本轮处理用户对原报告的反馈：\n'
        + focus['user_feedback']
        + '\n应重新核实的资料缺口（前一模型的暂定判断，不是事实）：\n'
        + '\n'.join(focus['missing'])
        + '\n保留原始研究用途，调查应服务于这次反馈，不把暂定缺口当作已证实前提。',
    }


def record(db_path, project_id, question_id, job_id, version, question, decision):
    """One durable round per feedback; retries retain its identity and materials."""
    focus = {
        'user_feedback': question,
        'missing': list(decision['missing']),
        'reason': decision['reason'],
        'provenance': 'feedback_not_source_evidence',
    }
    now = time.time()
    with sqlite3.connect(db_path, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute(
            'SELECT id,project_id FROM research_rounds WHERE question_id=?', (question_id,)
        ).fetchone()
        if existing:
            if existing[1] != project_id:
                raise RoundConflict('反馈轮次不属于当前研究')
            return _row(db, existing[0])
        if not db.execute(
            'SELECT 1 FROM research_reports WHERE project_id=? AND version=?', (project_id, version)
        ).fetchone():
            raise ValueError('反馈的基准报告不存在')
        identity = uuid.uuid4().hex
        db.execute(
            'INSERT INTO research_rounds(id,project_id,question_id,owner_job_id,base_report_version,'
            "phase,focus_json,created_at,updated_at) VALUES (?,?,?,?,?,'planned',?,?,?)",
            (
                identity,
                project_id,
                question_id,
                job_id,
                version,
                json.dumps(focus, ensure_ascii=False),
                now,
                now,
            ),
        )
        return _row(db, identity)


def start_collection(ctx, identity, owner_job_id, allowance_seconds, selection):
    """Atomically transfer a stopped project's collector to this feedback round.

    No threads are started here; the caller must dispatch the existing collector
    and retain this round as its result owner. Missing dispatch is not completion.
    """
    if not isinstance(allowance_seconds, (int, float)) or not 60 <= allowance_seconds <= 86400:
        raise ValueError('补查必须有明确的额外搜集时间（1–1440分钟）')
    config = selection.model_dump()
    if config.get('api_config'):
        config['api_config']['api_key'] = ''
    with ctx.research_workflow.LOCK, sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        item = _row(db, identity)
        if item['owner_job_id'] != owner_job_id:
            raise RoundConflict('不能接管另一个反馈任务的补查')
        if item['phase'] != 'planned':
            raise RoundConflict('该轮次已启动或停止，不能再次分配搜集时间')
        project = db.execute(
            'SELECT status,search_provider,public_search_topic,goal FROM research_projects WHERE id=?',
            (item['project_id'],),
        ).fetchone()
        if not project:
            raise ValueError('原研究项目不存在')
        if project[0] in {'queued', 'crawling'}:
            raise RoundBusy('原研究仍在搜集，等待其交付后继续本反馈')
        if project[0] not in {'completed', 'completed_with_errors'}:
            raise RoundConflict('原研究仍在运行、已暂停或取消，未自动恢复或并发扩展')
        public_topic = project[2] if project[2] is not None else project[3]
        if project[1] == 'none' or not public_topic.strip():
            raise ValueError('原研究未授权公开搜索；反馈和报告未作为搜索词外发')
        active = db.execute(
            "SELECT owner_job_id FROM research_rounds WHERE project_id=? AND phase IN ('collecting','revising','ready_for_revision')",
            (item['project_id'],),
        ).fetchone()
        if active:
            owner = db.execute(
                'SELECT status FROM job_runs WHERE id=? AND project_id=?', (active[0], item['project_id'])
            ).fetchone()
            if owner and owner[0] in {'queued', 'running', 'paused'}:
                raise RoundBusy('同一研究的前一轮反馈尚未交付，正在等待')
            raise RoundConflict('前一轮反馈执行已中断，材料仍保留，未抢占其修订归属')
        workflow = db.execute(
            'SELECT phase,report_job_id FROM research_workflows WHERE project_id=?', (item['project_id'],)
        ).fetchone()
        if workflow and workflow[1]:
            job = db.execute('SELECT status FROM job_runs WHERE id=?', (workflow[1],)).fetchone()
            if job and job[0] not in {'completed', 'failed', 'cancelled'}:
                raise RoundBusy('原报告仍在生成或暂停，等待原执行者交付')
        if workflow and workflow[0] == 'dispatching':
            raise RoundBusy('原报告正在交接，稍后自动重试')
        # Store each round's allowance, not an unexplained change to '30 minutes'.
        db.execute(
            'INSERT INTO research_workflows(project_id,limit_seconds,selection_json) VALUES (?,?,?) '
            'ON CONFLICT(project_id) DO UPDATE SET limit_seconds=research_workflows.elapsed_seconds+excluded.limit_seconds,'
            "phase='collecting',segment_started=NULL,selection_json=excluded.selection_json,report_job_id=NULL,error=''",
            (item['project_id'], allowance_seconds, json.dumps(config, ensure_ascii=False)),
        )
        db.execute(
            "UPDATE research_rounds SET phase='collecting',allowance_seconds=?,updated_at=? WHERE id=?",
            (allowance_seconds, time.time(), identity),
        )
        db.execute(
            "UPDATE research_projects SET control_revision=control_revision+1,status='queued',mode='automatic',search_status='queued',search_error='',"
            "stop_reason='',error='',max_pages=MAX(max_pages,500+(SELECT COUNT(*) FROM research_pages "
            "WHERE project_id=? AND status IN ('fetched','duplicate','irrelevant','failed'))),updated_at=? WHERE id=?",
            (item['project_id'], ctx.utc_now(), item['project_id']),
        )
        ctx.add_research_event(
            item['project_id'],
            'feedback_round_started',
            '开始同项目补查；原报告保留',
            details={
                'round_id': identity,
                'question_id': item['question_id'],
                'additional_seconds': allowance_seconds,
            },
            connection=db,
        )
        ctx.research_workflow.SELECTIONS[(str(ctx.DB_PATH), item['project_id'])] = selection
    return read(ctx.DB_PATH, identity)


def collection_stopped(ctx, project_id):
    """Route the existing collector's terminal hand-off back to its feedback.

    Called by the collector lifecycle, never by history/GET. The owner consumes
    ready_for_revision and publishes a version, not a second standalone report.
    """
    with ctx.research_workflow.LOCK, sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute(
            'SELECT id,phase FROM research_rounds WHERE project_id=? '
            "AND phase IN ('collecting','ready_for_revision','revising')",
            (project_id,),
        ).fetchone()
        if not row:
            return False
        if row[1] != 'collecting':
            return True
        project = db.execute(
            'SELECT status,stop_reason FROM research_projects WHERE id=?', (project_id,)
        ).fetchone()
        if not project or project[0] not in {'completed', 'completed_with_errors'}:
            return True  # This round owns the hand-off; pause is not completion.
        db.execute(
            "UPDATE research_rounds SET phase='ready_for_revision',updated_at=?,outcome_json=? WHERE id=?",
            (
                time.time(),
                json.dumps({'collection_stop_reason': project[1], 'question_answered': False}),
                row[0],
            ),
        )
        # Keep result ownership and clock transition in the SAME commit. A
        # crash between two commits used to permit an extra standalone report.
        db.execute(
            'UPDATE research_workflows SET elapsed_seconds=elapsed_seconds+CASE WHEN segment_started IS NULL '
            "THEN 0 ELSE MAX(0,?-segment_started) END,segment_started=NULL,phase='feedback_revision' WHERE project_id=?",
            (time.time(), project_id),
        )
        ctx.add_research_event(
            project_id,
            'feedback_materials_ready',
            '补查搜集停止，交回原反馈进行修订',
            details={'round_id': row[0]},
            connection=db,
        )
    return True
