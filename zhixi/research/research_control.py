"""Durable collection controls, applied inside the coordinator's write transaction.

No HTTP, model selection, worker launch or display-state dependency. A caller
must commit the project, its clock and the accompanying event together.
"""


class ControlConflict(ValueError):
    """The stored execution owner does not permit this command."""


def settle_clock(db, project_id, clock, phase=None):
    if not db.in_transaction:
        raise RuntimeError('Clock transitions require a write transaction')
    db.execute(
        'UPDATE research_workflows SET elapsed_seconds=elapsed_seconds+'
        'CASE WHEN segment_started IS NULL THEN 0 ELSE MAX(0,?-segment_started) END,'
        'segment_started=NULL,phase=COALESCE(?,phase) WHERE project_id=?',
        (clock, phase, project_id),
    )


def pause(db, project_id, now, clock, *, reason='user_paused', error=''):
    """Pause live collection; a delayed failure cannot replace a user's stop."""
    if not db.in_transaction:
        raise RuntimeError('Collection controls require a write transaction')
    row = db.execute('SELECT status,stop_reason FROM research_projects WHERE id=?', (project_id,)).fetchone()
    if not row:
        raise LookupError('研究项目不存在')
    taking_over = row[0] == 'paused' and row[1] == 'search_recovery_wait' and reason == 'user_paused'
    if row[0] in {'paused', 'cancelled', 'completed', 'completed_with_errors'} and not taking_over:
        return False
    if reason != 'user_paused' and row[0] not in {'queued', 'crawling'}:
        return False
    db.execute(
        "UPDATE research_projects SET control_revision=control_revision+1,status='paused',stop_reason=?,error=?,updated_at=?,"
        "search_status=CASE WHEN search_status='running' THEN 'queued' ELSE search_status END WHERE id=?",
        (reason, error, now, project_id),
    )
    db.execute(
        "UPDATE research_pages SET status='queued' WHERE project_id=? AND status='fetching'", (project_id,)
    )
    settle_clock(db, project_id, clock, 'paused')
    return True


def cancel(db, project_id, now, clock):
    """Cancel collection, retaining originals, reports and separate report jobs."""
    if not db.in_transaction:
        raise RuntimeError('Collection controls require a write transaction')
    row = db.execute('SELECT status FROM research_projects WHERE id=?', (project_id,)).fetchone()
    if not row:
        raise LookupError('研究项目不存在')
    if row[0] == 'cancelled':
        return False
    db.execute(
        "UPDATE research_projects SET control_revision=control_revision+1,status='cancelled',stop_reason='user_cancelled',updated_at=? WHERE id=?",
        (now, project_id),
    )
    db.execute(
        "UPDATE research_pages SET status='cancelled',updated_at=? "
        "WHERE project_id=? AND status IN ('queued','candidate','fetching')",
        (now, project_id),
    )
    settle_clock(db, project_id, clock, 'cancelled')
    return True


def resume_legacy(db, project_id, expected, now):
    """Compatibility for capture-only projects; never create a second workflow."""
    if not db.in_transaction:
        raise RuntimeError('Collection controls require a write transaction')
    row = db.execute(
        'SELECT status,control_revision,search_status FROM research_projects WHERE id=?', (project_id,)
    ).fetchone()
    if not row:
        raise LookupError('研究项目不存在')
    workflow = db.execute('SELECT 1 FROM research_workflows WHERE project_id=?', (project_id,)).fetchone()
    if workflow or (row[0], row[1]) != expected:
        raise ValueError('任务状态已变化，请刷新后再继续；未覆盖暂停或取消操作。')
    if row[0] in {'queued', 'crawling', 'cancelled', 'completed', 'completed_with_errors'}:
        raise ValueError('当前任务不能重复继续，请刷新查看状态。')
    db.execute(
        "UPDATE research_projects SET control_revision=control_revision+1,status='queued',error='',stop_reason='',"
        "search_status=CASE WHEN search_status='failed' THEN 'queued' ELSE search_status END,updated_at=? WHERE id=?",
        (now, project_id),
    )
    return 'queued' if row[2] == 'failed' else row[2]


def report_retry(db, project_id):
    """Authorize against stored owners/outcomes, not a projected UI label."""
    row = db.execute(
        'SELECT p.status,w.phase,w.report_job_id,j.status,j.attempt,j.project_id '
        'FROM research_workflows w JOIN research_projects p ON p.id=w.project_id '
        'LEFT JOIN job_runs j ON j.id=w.report_job_id WHERE p.id=?',
        (project_id,),
    ).fetchone()
    if not row:
        raise LookupError('没有自动报告任务')
    status, phase, previous_id, job_status, attempt, owner_project = row
    if owner_project is not None and owner_project != project_id:
        raise ControlConflict('报告作业归属不一致，未接管其他研究的任务')
    failed = (phase == 'report_failed' and job_status in {None, 'failed', 'cancelled'}) or (
        phase in {'reporting', 'report_paused', 'report_ready'} and job_status in {'failed', 'cancelled'}
    )
    active_round = db.execute(
        "SELECT 1 FROM research_rounds WHERE project_id=? AND phase IN ('collecting','ready_for_revision','revising')",
        (project_id,),
    ).fetchone()
    if status not in {'completed', 'completed_with_errors'} or not failed or active_round:
        raise ControlConflict('仅能重试失败且已停止采集的报告，当前任务未重复提交')
    return previous_id, (attempt + 1) if attempt else 1


def finish_dispatch(db, project_id, reserved_id, phase, error=''):
    """A dispatch receipt can update only the reservation it actually owns."""
    return (
        db.execute(
            'UPDATE research_workflows SET phase=?,error=? WHERE project_id=? '
            "AND phase='dispatching' AND report_job_id=? AND EXISTS "
            "(SELECT 1 FROM research_projects WHERE id=? AND status IN ('completed','completed_with_errors'))",
            (phase, error, project_id, reserved_id, project_id),
        ).rowcount
        == 1
    )
