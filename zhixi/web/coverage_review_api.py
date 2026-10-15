"""Recheck saved evidence without regenerating or mutating the original report."""

from copy import deepcopy
import hashlib
import json
import sqlite3
import time
import uuid
from threading import Thread

from fastapi import HTTPException

from zhixi.reports.question_coverage import assess_question_coverage
from zhixi.reports.research_synthesis import cached_step
from zhixi.reports.report_publication import publish_report, published_report


def install_coverage_routes(api, ctx):
    def load(project_id, version):
        with sqlite3.connect(ctx.DB_PATH) as db:
            row = db.execute(
                'SELECT id,report_json FROM research_reports WHERE project_id=? AND version=?',
                (project_id, version),
            ).fetchone()
        if not row:
            raise ValueError('指定的报告版本不存在')
        return row[0], json.loads(row[1])

    def worker(job_id, project_id, request):
        with ctx.JOB_WORKERS_LOCK:
            ctx.JOB_WORKERS.add(job_id)
        try:
            saved = published_report(ctx.DB_PATH, job_id, project_id)
            if saved is not None:
                ctx.set_job(
                    job_id,
                    status='completed',
                    progress=100,
                    stage='已恢复先前保存的核对版本，未重复生成',
                    result=saved,
                    report_id=saved['report_id'],
                    finished_at=time.time(),
                    request=None,
                )
                return
            report_id, original = load(project_id, request.report_version)
            result = deepcopy(original)
            research = original.get('research') or {}
            evidence = research.get('evidence') or []
            pages = {p['id']: p for p in research.get('sources') or []}
            if not evidence:
                raise ValueError('该报告版本没有可供核对的原文证据')
            goal = research.get('goal') or original.get('title') or '研究问题'
            ctx.JOB_CONTEXT.job_id = job_id
            with ctx.JOBS_LOCK:
                previous = ctx.JOBS.get(job_id, {}).get('retried_from')

            def runtime_update(**details):
                with ctx.JOBS_LOCK:
                    started = ctx.JOBS.get(job_id, {}).get('started_at')
                if details.get('model_started_at') and not started:
                    details.update(started_at=details['model_started_at'], status='running')
                ctx.set_job(job_id, **details)

            def analyze(corpus, phase, index, total):
                ctx.wait_if_job_paused(job_id)
                if ctx.job_cancelled(job_id):
                    raise ctx.JobCancelled('问题覆盖核对已取消')
                settings = request.model_dump(exclude={'api_config'})
                settings['api_endpoint'] = request.api_config.base_url if request.api_config else ''
                settings['api_model'] = request.api_config.model if request.api_config else ''
                key = hashlib.sha256(
                    json.dumps(
                        {
                            'protocol': 'goal-anchored-coverage-v3',
                            'report_id': report_id,
                            'settings': settings,
                            'corpus': corpus,
                            'phase': phase,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                probe = ctx.AnalyzeRequest(
                    source_type='text',
                    content=corpus,
                    provider=request.provider,
                    local_config=request.local_config,
                    api_config=request.api_config,
                    structure_mode=request.structure_mode,
                )
                return cached_step(
                    ctx.DB_PATH,
                    job_id,
                    previous,
                    key,
                    lambda: ctx.analyze_content(
                        probe,
                        lambda value, stage: ctx.set_job(
                            job_id, progress=None, stage=f'问题核对 {index}/{total}：{stage}'
                        ),
                        runtime_update,
                        source_context_override={
                            'page_type': 'research',
                            'research_goal': goal,
                            'synthesis_phase': phase,
                        },
                    ),
                )

            ledger = assess_question_coverage(evidence, pages, goal, 12000, analyze)
            if ctx.job_cancelled(job_id):
                raise ctx.JobCancelled('问题覆盖核对已取消，原报告未改动')
            probe = ctx.AnalyzeRequest(
                source_type='text',
                content=goal,
                provider=request.provider,
                local_config=request.local_config,
                api_config=request.api_config,
            )
            model = (
                ctx.resolve_local_model(probe).name
                if request.provider == 'local'
                else request.api_config.model
            )
            ledger.update(
                model=model,
                provider=request.provider,
                created_at=ctx.utc_now(),
                run_id=job_id,
                based_on_report_id=report_id,
                based_on_report_version=request.report_version,
            )
            result['question_coverage'] = ledger
            result['coverage_review_origin'] = {
                'report_id': report_id,
                'report_version': request.report_version,
                'body_regenerated': False,
            }
            result = publish_report(ctx.DB_PATH, job_id, project_id, result, probe, ctx.save_history)
            new_id = result['report_id']
            ctx.set_job(
                job_id,
                status='completed',
                progress=100,
                stage='覆盖核对已保存为新版本'
                if ledger['status'] == 'reviewed'
                else '新版本已保存，部分问题核对未完成',
                result=result,
                report_id=new_id,
                finished_at=time.time(),
                request=None,
            )
        except ctx.JobCancelled:
            ctx.set_job(
                job_id, status='cancelled', progress=None, stage='问题覆盖核对已取消', finished_at=time.time()
            )
        except Exception as exc:
            error = ctx.classify_job_error(exc, '问题覆盖核对')
            ctx.set_job(
                job_id,
                status='failed',
                progress=None,
                stage='问题覆盖核对失败',
                error=error['message'],
                error_info=error,
                finished_at=time.time(),
            )
        finally:
            with ctx.JOB_WORKERS_LOCK:
                ctx.JOB_WORKERS.discard(job_id)

    def enqueue(project_id, request, *, retried_from=None, attempt=1):
        load(project_id, request.report_version)
        probe = ctx.AnalyzeRequest(
            source_type='text',
            content='问题覆盖核对配置',
            provider=request.provider,
            local_config=request.local_config,
            api_config=request.api_config,
        )
        model = (
            ctx.resolve_local_model(probe).name
            if request.provider == 'local'
            else request.api_config.model
            if request.api_config
            else ''
        )
        if not model:
            raise ValueError('请配置模型')
        job_id = uuid.uuid4().hex
        job = {
            'id': job_id,
            'kind': 'coverage_review',
            'status': 'queued',
            'progress': 0,
            'stage': '等待问题覆盖核对',
            'created_at': time.time(),
            'stage_started_at': time.time(),
            'provider': request.provider,
            'model_name': model,
            'project_id': project_id,
            'request': request,
            'retried_from': retried_from,
            'attempt': attempt,
        }
        with ctx.JOBS_LOCK:
            ctx.JOBS[job_id] = job
        ctx.persist_job(job)
        Thread(target=worker, args=(job_id, project_id, request), daemon=True).start()
        return ctx.public_job(job)

    @api.post('/api/research/{project_id}/reports/{version}/coverage', status_code=202)
    def recheck(project_id: str, version: int, selection: ctx.ResearchSynthesisRequest):
        try:
            return enqueue(
                project_id, ctx.ResearchAuditRequest(**selection.model_dump(), report_version=version)
            )
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

    return enqueue, worker
