"""Persistent, version-bound question jobs using the application's model queue."""

import json
import hashlib
import sqlite3
import time
import uuid
from threading import Thread
from typing import Literal

from fastapi import HTTPException

from zhixi.reports.report_questions import validate_selection, select_question_evidence
from zhixi.storage.synthesis_checkpoints import cached_step
from zhixi.reports.report_question_service import QuestionMaterial, answer_question, completion_stage


def saved_report(db_path, project_id: str, version: int) -> tuple[str, dict]:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            'SELECT id,report_json FROM research_reports WHERE project_id=? AND version=?',
            (project_id, version),
        ).fetchone()
    if not row:
        raise ValueError('指定的报告版本不存在')
    return row[0], {**json.loads(row[1]), 'report_id': row[0], 'report_version': version}


def question_material(db_path, project_id: str, request) -> QuestionMaterial:
    """Read the exact report version and validate its selected passage once."""
    report_id, report = saved_report(db_path, project_id, request.report_version)
    anchor = validate_selection(
        report, request.anchor_id, request.selected_text, request.start, request.end, request.text_hash
    )
    research = report.get('research') or {}
    with sqlite3.connect(db_path) as db:
        original = db.execute('SELECT goal FROM research_projects WHERE id=?', (project_id,)).fetchone()
    if original:
        report['research'] = {**research, 'goal': original[0]}
    pages = {item['id']: item for item in research.get('sources') or []}
    selected, scope = select_question_evidence(
        research.get('evidence') or [], pages, request.question, anchor, request.mode
    )
    parent_context = None
    if request.parent_question_id:
        with sqlite3.connect(db_path) as connection:
            parent = connection.execute(
                'SELECT question,answer_json FROM report_questions WHERE id=? AND project_id=? AND report_version=?',
                (request.parent_question_id, project_id, request.report_version),
            ).fetchone()
        if not parent:
            raise ValueError('追问对象不属于当前报告版本')
        parent_context = {'question': parent[0], 'answer': json.loads(parent[1]).get('summary', '')}
    return QuestionMaterial(report_id, report, anchor, selected, pages, scope, parent_context)


def retained_question_answer(db_path, question_id, job_id, previous_job) -> dict:
    with sqlite3.connect(db_path, timeout=30) as connection:
        row = connection.execute(
            'SELECT answer_json FROM report_questions WHERE id=? AND job_id=?', (question_id, job_id)
        ).fetchone()
    return json.loads(row[0]) if row else {}


def question_plan(db_path, project_id, request):
    """Read-only preflight. This exact scope is also checked before enqueue."""
    material = question_material(db_path, project_id, request)
    from zhixi.reports.report_questions import report_context

    context = report_context(material.report, request.question, material.anchor)
    plan = {
        'report_version': request.report_version,
        'question': request.question.strip(),
        'selected_text': request.selected_text,
        'mode': request.mode,
        'model': request.model_dump(exclude={'execution_plan_hash', 'question_id', 'api_config'}),
        'api_model': request.api_config.model if request.api_config else '',
        'api_endpoint': request.api_config.base_url if request.api_config else '',
        'steps': [f'读取 V{request.report_version} 的报告目录、最多 {len(context["paragraphs"])} 段已保存正文和 '
                  f'最多 {material.scope["selected_count"]}/{material.scope["available_count"]} 条相关原文；'
                  '本地模型按总容量保留完整段落与原文，实际采用范围随回答保存',
                  '直接回答本次问题，不重做整份研究报告'],
        'material_scope': material.scope,
        'changes_report': request.apply_changes,
        'network_minutes': request.feedback_research_minutes if request.apply_changes else None,
    }
    if request.apply_changes:
        plan['steps'].append('判断所选段落是否应修订；修订时检查关联表述并保存新版本，保留原版。核对可能分批调用模型')
        if request.feedback_research_minutes:
            plan['steps'].append(f'现有材料不足时，沿原授权公开主题补查，搜集最多 {request.feedback_research_minutes:g} 分钟；回答与修订另计')
    else:
        plan['steps'].append('只返回解释和后续处理建议；不改正文、不新增联网、不自动恢复研究')
    digest = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {**{k: v for k, v in plan.items() if k not in {'model', 'selected_text', 'api_endpoint'}}, 'hash': digest}


def save_question_answer(db_path, question_id: str, job_id: str, answer: dict) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            'UPDATE report_questions SET answer_json=? WHERE id=? AND job_id=?',
            (json.dumps(answer, ensure_ascii=False), question_id, job_id),
        )


def question_analyzer(ctx, job_id, request, material: QuestionMaterial, previous_job):
    """Adapt the application's model queue to the service's AnalyzeBatch callable."""
    report = material.report
    parent_context = material.parent_context

    def runtime_update(**details):
        if details.get('model_started_at'):
            with ctx.JOBS_LOCK:
                started = ctx.JOBS.get(job_id, {}).get('started_at')
            if not started:
                details.update(started_at=details['model_started_at'], status='running')
        ctx.set_job(job_id, **details)

    def analyze(corpus, phase, index, total):
        ctx.wait_if_job_paused(job_id)
        if ctx.job_cancelled(job_id):
            raise ctx.JobCancelled('报告问答已取消')
        probe = ctx.AnalyzeRequest(
            source_type='text',
            content=corpus,
            provider=request.provider,
            local_config=request.local_config,
            api_config=request.api_config,
            structure_mode='concise' if request.mode == 'quick' else 'detailed',
        )
        config = request.model_dump(exclude={'api_config', 'question_id'})
        config['api_endpoint'] = request.api_config.base_url if request.api_config else ''
        config['api_model'] = request.api_config.model if request.api_config else ''
        step_key = hashlib.sha256(
            json.dumps(
                {
                    'revision': 'question-v17-combined-input-budget',
                    'config': config,
                    'corpus': corpus,
                    'phase': phase,
                    'parent_context': parent_context,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        batch_result = cached_step(
            ctx.DB_PATH,
            job_id,
            previous_job,
            step_key,
            lambda: ctx.analyze_content(
                probe,
                lambda value, stage: ctx.set_job(
                    job_id,
                    progress=None,
                    stage=f'{"处理已确认的修订" if phase.startswith("feedback:") else "根据已有报告回答" if phase == "question:answer" else "核对材料"} {index}/{total}：{stage}',
                ),
                runtime_update,
                source_context_override={
                    'page_type': 'research',
                    'research_goal': request.question,
                    'synthesis_phase': phase,
                    'report_question': {
                        'selected_passage': request.selected_text,
                        'report_title': report.get('title'),
                        'mode': request.mode,
                        'previous_exchange': parent_context,
                    },
                },
            ),
        )
        if batch_result.get('runtime', {}).get('checkpoint_reused'):
            ctx.set_job(job_id, stage=f'恢复已完成的问答步骤 {index}/{total}')
        return batch_result

    return analyze


def report_question_worker(ctx, job_id, project_id, request) -> None:
    """Task ownership/error adapter. Question semantics live in answer_question."""
    with ctx.JOB_WORKERS_LOCK:
        ctx.JOB_WORKERS.add(job_id)
    try:
        material = question_material(ctx.DB_PATH, project_id, request)
        ctx.JOB_CONTEXT.job_id = job_id
        with ctx.JOBS_LOCK:
            previous_job = ctx.JOBS.get(job_id, {}).get('retried_from')
        analyze = question_analyzer(ctx, job_id, request, material, previous_job)

        def check_cancelled():
            if ctx.job_cancelled(job_id):
                raise ctx.JobCancelled('报告问答已取消')

        answer = answer_question(
            material,
            request.question,
            analyze=analyze,
            check_cancelled=check_cancelled,
            retained_answer=lambda: retained_question_answer(
                ctx.DB_PATH, request.question_id, job_id, previous_job
            ),
            save_answer=lambda value: save_question_answer(ctx.DB_PATH, request.question_id, job_id, value),
            set_stage=lambda stage: ctx.set_job(job_id, stage=stage),
            apply_feedback=lambda value: ctx.process_report_feedback(
                ctx,
                job_id,
                project_id,
                request,
                material.report,
                material.anchor,
                value,
                material.evidence,
                material.scope,
                analyze,
            ),
            allow_changes=request.apply_changes,
        )
        ctx.set_job(
            job_id,
            status='completed',
            progress=100,
            stage=completion_stage(answer['feedback']),
            finished_at=time.time(),
            result={
                'question_id': request.question_id,
                'project_id': project_id,
                'report_id': material.report_id,
                'report_version': request.report_version,
                'answer': answer,
                'feedback': answer['feedback'],
            },
        )
    except ctx.JobCancelled:
        ctx.set_job(
            job_id, status='cancelled', progress=None, stage='报告问答已取消', finished_at=time.time()
        )
    except Exception as exc:
        error = ctx.classify_job_error(exc, '报告问答')
        retained = retained_question_answer(ctx.DB_PATH, request.question_id, job_id, True)
        ctx.set_job(
            job_id,
            status='failed',
            progress=None,
            stage='回答已保存，后续修订未完成' if retained.get('summary') else '报告问答失败',
            error=error['message'],
            error_info=error,
            finished_at=time.time(),
        )
    finally:
        with ctx.JOB_WORKERS_LOCK:
            ctx.JOB_WORKERS.discard(job_id)


def enqueue_report_question(ctx, project_id, request, *, retried_from=None, attempt=1):
    if not request.question.strip():
        raise ValueError('请输入具体问题')
    if request.apply_changes and not retried_from:
        plan = question_plan(ctx.DB_PATH, project_id, request)
        if request.execution_plan_hash != plan['hash']:
            raise ValueError('请先查看并确认本次处理范围；问题或设置变化后需重新确认')
    elif request.feedback_research_minutes is not None and not request.apply_changes:
        raise ValueError('仅回答模式不启动补查；请先查看并确认修订/补查范围')
    report_id, report = saved_report(ctx.DB_PATH, project_id, request.report_version)
    anchor = validate_selection(
        report, request.anchor_id, request.selected_text, request.start, request.end, request.text_hash
    )
    probe = ctx.AnalyzeRequest(
        source_type='text',
        content='报告问答模型配置检查',
        provider=request.provider,
        local_config=request.local_config,
        api_config=request.api_config,
    )
    model = (
        str(ctx.resolve_local_model(probe).name)
        if request.provider == 'local'
        else (request.api_config.model if request.api_config else '')
    )
    if not model:
        raise ValueError('请配置要使用的模型')
    request = request.model_copy(deep=True)
    request.question = request.question.strip()
    job_id = uuid.uuid4().hex
    with sqlite3.connect(ctx.DB_PATH) as connection:
        if (
            request.parent_question_id
            and not connection.execute(
                'SELECT 1 FROM report_questions WHERE id=? AND project_id=? AND report_version=?',
                (request.parent_question_id, project_id, request.report_version),
            ).fetchone()
        ):
            raise ValueError('追问必须关联同一报告版本的问题')
        if retried_from:
            changed = connection.execute(
                'UPDATE report_questions SET job_id=? WHERE id=? AND job_id=? AND project_id=?',
                (job_id, request.question_id, retried_from, project_id),
            ).rowcount
            if not changed:
                raise ValueError('该问题已有新的任务，请刷新问答记录')
        else:
            request.question_id = uuid.uuid4().hex
            connection.execute(
                'INSERT INTO report_questions VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (
                    request.question_id,
                    project_id,
                    report_id,
                    request.report_version,
                    json.dumps(anchor, ensure_ascii=False),
                    request.question,
                    request.mode,
                    request.parent_question_id,
                    job_id,
                    ctx.utc_now(),
                    '{}',
                ),
            )
    job = {
        'id': job_id,
        'kind': 'report_question',
        'project_id': project_id,
        'status': 'queued',
        'progress': None,
        'stage': '等待报告问答',
        'created_at': time.time(),
        'stage_started_at': time.time(),
        'provider': request.provider,
        'model_name': model,
        'request': request,
        'label': request.question,
        'question_id': request.question_id,
        'retried_from': retried_from,
        'attempt': attempt,
    }
    with ctx.JOBS_LOCK:
        ctx.JOBS[job_id] = job
    ctx.persist_job(job)
    Thread(
        target=report_question_worker,
        args=(ctx, job_id, project_id, request),
        daemon=True,
        name=f'report-question-{job_id[:8]}',
    ).start()
    return ctx.public_job(job)


def install_question_routes(api, ctx):
    """Register HTTP endpoints only; application composition owns worker bindings."""

    @api.post('/api/research/{project_id}/questions/preview')
    def preview_question(project_id: str, request: ctx.ReportQuestionRequest):
        try:
            return question_plan(ctx.DB_PATH, project_id, request)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

    @api.post('/api/research/{project_id}/questions', status_code=202)
    def submit_question(project_id: str, request: ctx.ReportQuestionRequest):
        try:
            return ctx.enqueue_report_question(project_id, request)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

    @api.get('/api/research/{project_id}/questions')
    def question_history(project_id: str, report_version: int | None = None, summary_only: bool = False):
        with sqlite3.connect(ctx.DB_PATH) as connection:
            connection.row_factory = sqlite3.Row
            fields = (
                "q.id,q.project_id,q.report_id,q.report_version,q.anchor_json,q.question,q.mode,"
                "q.parent_question_id,q.job_id,q.created_at,"
                "json_object('summary',json_extract(q.answer_json,'$.summary'),"
                "'feedback',json_extract(q.answer_json,'$.feedback'),"
                "'question_scope',json_extract(q.answer_json,'$.question_scope')) AS answer_json"
                if summary_only else 'q.*'
            )
            rows = connection.execute(
                f'SELECT {fields},j.status,j.stage,j.error_json FROM report_questions q '
                'LEFT JOIN job_runs j ON j.id=q.job_id WHERE q.project_id=? ORDER BY q.created_at,q.rowid',
                (project_id,),
            ).fetchall()
            links = connection.execute(
                'SELECT link.question_id,project.id,project.goal,project.status FROM question_research_links link '
                'JOIN research_projects project ON project.id=link.child_project_id '
                'JOIN report_questions question ON question.id=link.question_id WHERE question.project_id=?',
                (project_id,),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item['anchor'] = json.loads(item.pop('anchor_json'))
            item['answer'] = json.loads(item.pop('answer_json'))
            item['error_info'] = json.loads(item.pop('error_json') or '{}')
            item['anchor_state'] = (
                'current_version' if report_version == item['report_version'] else 'other_version'
            )
            item['supplementary_research'] = [
                dict(link) for link in links if link['question_id'] == item['id']
            ]
            output.append(item)
        return output

    @api.get('/api/research/{project_id}/questions/{question_id}')
    def question_answer_detail(project_id: str, question_id: str):
        with sqlite3.connect(ctx.DB_PATH) as db:
            row = db.execute('SELECT answer_json FROM report_questions WHERE id=? AND project_id=?',
                             (question_id, project_id)).fetchone()
        if not row:
            raise HTTPException(404, detail='问题不存在')
        answer = json.loads(row[0])
        # Logs remain stored for OPS, not repeatedly embedded into reader HTML.
        return {k: v for k, v in answer.items() if k in {
            'summary', 'tree', 'research', 'question_scope', 'feedback', 'proposal', 'report_quality'
        }}

    @api.post('/api/research/{project_id}/questions/{question_id}/research', status_code=202)
    def supplement_research(project_id: str, question_id: str, request: ctx.QuestionResearchRequest):
        if len(request.query.strip()) < 2:
            raise HTTPException(400, detail='请输入具体搜索词')
        with sqlite3.connect(ctx.DB_PATH) as connection:
            row = connection.execute(
                'SELECT report_id,report_version FROM report_questions WHERE id=? AND project_id=?',
                (question_id, project_id),
            ).fetchone()
        if not row:
            raise HTTPException(404, detail='原问题不存在')
        # Only the separately confirmed search query leaves the machine. Do not
        # silently attach selected text, report contents or conversation history.
        child = ctx.create_research(
            ctx.ResearchRequest(
                goal=request.query.strip(),
                max_pages=500 if request.auto_report else request.max_pages,
                max_depth=20 if request.auto_report else 2,
                mode=request.mode,
                scope='any_public',
                search_provider='hybrid',
                time_limit_minutes=request.time_limit_minutes,
                auto_report=request.auto_report,
                report_model=request.report_model,
            )
        )
        with sqlite3.connect(ctx.DB_PATH) as connection:
            connection.execute(
                'INSERT INTO question_research_links VALUES (?,?,?)',
                (question_id, child['id'], ctx.utc_now()),
            )
            ctx.add_research_event(
                child['id'],
                'question_followup_created',
                '由报告问题发起的补充研究。',
                details={
                    'parent_project_id': project_id,
                    'parent_report_id': row[0],
                    'parent_report_version': row[1],
                    'parent_question_id': question_id,
                },
                connection=connection,
            )
        return {
            **child,
            'parent_question_id': question_id,
            'parent_report_id': row[0],
            'parent_report_version': row[1],
        }

    @api.get('/api/research/{project_id}/reports/{version}')
    def report_version(project_id: str, version: int,
                       view: Literal['full', 'reader', 'process', 'material'] = 'full',
                       source_id: str | None = None):
        from zhixi.reports.report_reading import read_report

        result = read_report(ctx.DB_PATH, project_id, version, view=view, source_id=source_id)
        if result is None:
            raise HTTPException(404, detail='指定的报告版本或材料不存在')
        return result

    @api.post('/api/research/{project_id}/reports/{version}/gaps/{gap_id}/research', status_code=202)
    def research_report_gap(project_id: str, version: int, gap_id: str, request: ctx.QuestionResearchRequest):
        try:
            report_id, report = saved_report(ctx.DB_PATH, project_id, version)
        except ValueError as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        gap = next(
            (
                item
                for item in (report.get('question_coverage') or {}).get('questions', [])
                if item.get('id') == gap_id
            ),
            None,
        )
        if not gap or gap.get('status') == 'incomplete':
            raise HTTPException(400, detail='该问题尚未完成材料核对，不能从无效判断发起补查')
        if len(request.query.strip()) < 2:
            raise HTTPException(400, detail='请输入将发送到搜索引擎的具体查询')
        child = ctx.create_research(
            ctx.ResearchRequest(
                goal=request.query.strip(),
                max_pages=500 if request.auto_report else request.max_pages,
                max_depth=20 if request.auto_report else 2,
                mode=request.mode,
                scope='any_public',
                search_provider='hybrid',
                time_limit_minutes=request.time_limit_minutes,
                auto_report=request.auto_report,
                report_model=request.report_model,
            )
        )
        with sqlite3.connect(ctx.DB_PATH) as connection:
            connection.execute(
                'CREATE TABLE IF NOT EXISTS coverage_research_links '
                '(report_id TEXT,gap_id TEXT,child_project_id TEXT,query TEXT,created_at TEXT,PRIMARY KEY(report_id,gap_id,child_project_id))'
            )
            connection.execute(
                'INSERT INTO coverage_research_links VALUES (?,?,?,?,?)',
                (report_id, gap_id, child['id'], request.query.strip(), ctx.utc_now()),
            )
            ctx.add_research_event(
                child['id'],
                'report_gap_followup',
                '由报告问题核对发起的补充研究。',
                details={
                    'parent_project_id': project_id,
                    'parent_report_id': report_id,
                    'parent_report_version': version,
                    'gap_id': gap_id,
                },
                connection=connection,
            )
        return {**child, 'parent_report_id': report_id, 'parent_report_version': version, 'gap_id': gap_id}

    @api.get('/api/research/{project_id}/reports/{version}/gap-research')
    def report_gap_history(project_id: str, version: int):
        try:
            report_id, _ = saved_report(ctx.DB_PATH, project_id, version)
        except ValueError as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        with sqlite3.connect(ctx.DB_PATH) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                'SELECT link.gap_id,project.id,project.goal,project.status FROM coverage_research_links link '
                'JOIN research_projects project ON project.id=link.child_project_id WHERE link.report_id=? ORDER BY link.created_at',
                (report_id,),
            ).fetchall()
            reports = connection.execute(
                'SELECT r.project_id,r.id,r.version,r.created_at FROM research_reports r '
                'WHERE r.project_id IN (SELECT child_project_id FROM coverage_research_links WHERE report_id=?) '
                'ORDER BY r.version DESC',
                (report_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                'reports': [dict(report) for report in reports if report['project_id'] == row['id']],
            }
            for row in rows
        ]

    @api.post('/api/research/{project_id}/reports/{version}/audit', status_code=202)
    def audit_report_version(project_id: str, version: int, selection: ctx.ResearchSynthesisRequest):
        try:
            saved_report(ctx.DB_PATH, project_id, version)
            return ctx.enqueue_research_audit(
                project_id, ctx.ResearchAuditRequest(**selection.model_dump(), report_version=version)
            )
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
