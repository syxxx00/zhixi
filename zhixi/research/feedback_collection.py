"""Execute a feedback-owned collection round and return original materials.

This is an execution adapter to the existing collector. It neither creates a
child project nor calls a second report generator, nor generates search queries.
"""

import json
import sqlite3
import time

from zhixi.research import research_rounds as rounds


def stored_materials(db_path, project_id):
    with sqlite3.connect(db_path, timeout=30) as db:
        db.row_factory = sqlite3.Row
        pages = [
            dict(row)
            for row in db.execute(
                "SELECT id,url,title,page_type FROM research_pages WHERE project_id=? AND status='fetched'",
                (project_id,),
            )
        ]
        rows = db.execute(
            'SELECT e.* FROM research_evidence e JOIN research_pages p ON p.id=e.page_id '
            "WHERE e.project_id=? AND p.project_id=? AND p.status='fetched' ORDER BY e.created_at,e.id",
            (project_id, project_id),
        ).fetchall()
    evidence = []
    for row in rows:
        value = dict(row)
        value['metadata'] = json.loads(value.pop('metadata_json') or '{}')
        value['type'] = value.pop('evidence_type')
        evidence.append(value)
    return evidence, pages


def resume_interrupted(ctx, item, selection):
    """Resume an owned execution failure, not a human pause; keep remaining time."""
    with ctx.research_workflow.LOCK, sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        project = db.execute(
            'SELECT status,stop_reason FROM research_projects WHERE id=?', (item['project_id'],)
        ).fetchone()
        if (
            not project
            or project[0] != 'paused'
            or project[1]
            not in {
                'feedback_interrupted',
                'worker_interrupted',
                'planning_failed',
                'service_restarted',
                'server_restarted',
            }
        ):
            raise rounds.RoundConflict('研究已暂停或取消；未自动恢复人工暂停，也未增加搜集时间')
        current = db.execute(
            'SELECT phase,owner_job_id FROM research_rounds WHERE id=?', (item['id'],)
        ).fetchone()
        if current != ('interrupted', item['owner_job_id']):
            raise rounds.RoundConflict('补查归属或状态已变化')
        db.execute(
            "UPDATE research_rounds SET phase='collecting',updated_at=? WHERE id=?", (time.time(), item['id'])
        )
        db.execute(
            "UPDATE research_workflows SET phase='collecting',segment_started=NULL,error='' WHERE project_id=?",
            (item['project_id'],),
        )
        db.execute(
            "UPDATE research_projects SET control_revision=control_revision+1,status='queued',stop_reason='',error='',search_status=CASE "
            "WHEN search_status='running' THEN 'queued' ELSE search_status END WHERE id=?",
            (item['project_id'],),
        )
    ctx.research_workflow.SELECTIONS[(str(ctx.DB_PATH), item['project_id'])] = selection


def obtain(ctx, job_id, project_id, request, version, decision):
    if request.feedback_research_minutes is None:
        raise ValueError('本次未提供追加搜集时间，未启动联网补查')
    item = rounds.record(
        ctx.DB_PATH, project_id, request.question_id, job_id, version, request.question, decision
    )
    item = rounds.claim_retry(ctx.DB_PATH, item['id'], job_id)
    selection = ctx.ResearchSynthesisRequest(
        provider=request.provider,
        local_config=request.local_config,
        api_config=request.api_config,
        structure_mode=request.structure_mode,
    )
    if item['phase'] == 'planned':
        while True:
            ctx.wait_if_job_paused(job_id)
            if ctx.job_cancelled(job_id):
                rounds.stop_owned_collection(ctx, item['id'], job_id, cancelled=True)
                raise ctx.JobCancelled('已取消等待中的反馈补查，原研究未改变')
            try:
                item = rounds.start_collection(
                    ctx, item['id'], job_id, request.feedback_research_minutes * 60, selection
                )
                break
            except rounds.RoundBusy as busy:
                ctx.set_job(job_id, stage=str(busy), progress=None)
                # No allowance/clock is allocated until the atomic claim wins.
                # A pause/cancel/error is not classified as temporary contention.
                time.sleep(0.1)
    elif item['phase'] == 'interrupted':
        resume_interrupted(ctx, item, selection)
        item = rounds.read(ctx.DB_PATH, item['id'])
    try:
        if item['phase'] == 'collecting':
            ctx.set_job(job_id, stage='正在同一研究内补查，搜集结束后自动修订', progress=None)
            rounds.dispatch_collection(ctx, item['id'], job_id)
        while True:
            if ctx.job_cancelled(job_id):
                rounds.stop_owned_collection(ctx, item['id'], job_id, cancelled=True)
                raise ctx.JobCancelled('反馈补查已取消，原报告和取得的材料保留')
            if ctx.job_paused(job_id):
                rounds.stop_owned_collection(ctx, item['id'], job_id, error='反馈任务暂停')
                ctx.wait_if_job_paused(job_id)
                if ctx.job_cancelled(job_id):
                    raise ctx.JobCancelled('反馈补查已取消')
                # Let the old collector observe its stopped project before
                # restoring it, so it cannot overlap a resumed collector.
                while True:
                    with ctx.RESEARCH_LOCK:
                        active = (
                            project_id in ctx.RESEARCH_WORKERS or project_id in ctx.RESEARCH_DISCOVERY_WORKERS
                        )
                    if not active:
                        break
                    if ctx.job_cancelled(job_id):
                        raise ctx.JobCancelled('反馈补查已取消')
                    time.sleep(0.1)
                item = rounds.read(ctx.DB_PATH, item['id'])
                resume_interrupted(ctx, item, selection)
                rounds.dispatch_collection(ctx, item['id'], job_id)
            item = rounds.read(ctx.DB_PATH, item['id'])
            if item['phase'] in {'ready_for_revision', 'revising', 'completed'}:
                evidence, pages = stored_materials(ctx.DB_PATH, project_id)
                return item, evidence, pages
            project = ctx.research_project(project_id)
            if not project or project['status'] in {'paused', 'cancelled'}:
                raise rounds.RoundConflict('研究已暂停、取消或中断，原反馈回答已保留，未自动恢复')
            # The regular lifecycle guard performs deadline checks and recovery;
            # this observer doesn't infer that a timeout means the worker died.
            time.sleep(0.1)
    except Exception as exc:
        rounds.stop_owned_collection(
            ctx, item['id'], job_id, cancelled=ctx.job_cancelled(job_id), error=str(exc)
        )
        raise


def complete(ctx, identity, job_id, result):
    with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        item = rounds._row(db, identity)
        if item['owner_job_id'] != job_id:
            raise rounds.RoundConflict('修订结果不属于当前补查执行者')
        outcome = {
            **item['outcome'],
            'feedback_result': result,
            'feedback_handled': result['status'] in {'revised', 'explained'},
            'semantic_status': 'model_judgment_not_fact_verification',
        }
        db.execute(
            "UPDATE research_rounds SET phase='completed',outcome_json=?,updated_at=? WHERE id=?",
            (json.dumps(outcome, ensure_ascii=False), time.time(), identity),
        )
        db.execute(
            "UPDATE research_workflows SET phase='reporting',report_job_id=? WHERE project_id=? AND phase='feedback_revision'",
            (job_id, item['project_id']),
        )
