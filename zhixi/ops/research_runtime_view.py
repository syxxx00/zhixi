"""One read-only projection of collection, execution ownership and saved reports.

The projector has no clock, database or app dependency. Both readers execute it
as a connection-local SQLite function, so filtering/counting/paging use exactly
the same rules as a single workflow read. No views, tables or state are written.
Coordinator phases remain distinct from job outcomes and publication receipts;
this display projection does not authorize starting or taking over work.
"""

import json


def _error_message(encoded):
    try:
        value = json.loads(encoded or '{}')
    except (ValueError, TypeError):
        return ''
    return value.get('message', '') if isinstance(value, dict) else ''


def project(facts):
    """Project small, project-scoped facts; never inspect report content."""
    collection = facts['collection_status']
    coordination = facts.get('coordination_phase') or ''
    round_owned = coordination in {'collecting', 'feedback_revision'} and facts.get('round_count', 0) > 0
    prefix = 'round_job' if round_owned else 'report_job'
    job = {
        'id': facts.get(prefix + '_id'),
        'status': facts.get(prefix + '_status'),
        'stage': facts.get(prefix + '_stage') or '',
        'provider': facts.get(prefix + '_provider'),
        'error': _error_message(facts.get(prefix + '_error')),
        'role': 'feedback' if round_owned else 'report',
    }
    latest_id = facts.get('latest_report_id')
    view = {
        'collection_status': collection,
        'coordination_phase': coordination,
        'phase': coordination,
        'status': 'running' if collection == 'crawling' else collection,
        'stage': coordination,
        'error': facts.get('workflow_error') or facts.get('collection_error') or '',
        'job': job,
        'publication': {
            'status': 'saved' if latest_id else 'missing' if facts.get('reports_known') else 'unknown',
            'latest_report_id': latest_id,
            'latest_report_version': facts.get('latest_report_version'),
            'current_report_id': facts.get(prefix + '_report_id'),
            'current_outcome': facts.get('report_job_outcome') if prefix == 'report_job' else None,
        },
        'issue': '',
    }
    # A surviving child job must not erase the project's explicit controls.
    if collection == 'cancelled' or coordination == 'cancelled':
        view.update(status='cancelled', phase='cancelled', stage='cancelled')
    elif collection == 'paused' or coordination == 'paused':
        view.update(status='paused', phase='paused', stage='paused')
    elif round_owned and facts['round_count'] > 1:
        view.update(status='unavailable', issue='multiple_round_owners')
    elif coordination == 'collecting':
        # Collection may have stopped while its owner is finishing hand-off.
        if facts.get('segment_started') is not None or collection in {
            'crawling',
            'completed',
            'completed_with_errors',
        }:
            view['status'] = 'running'
    elif coordination == 'feedback_revision':
        view['stage'] = job['stage'] or coordination
        if not round_owned or not job['status']:
            view.update(status='unavailable', issue='missing_feedback_owner')
        elif job['status'] == 'completed':
            view.update(status='unavailable', issue='feedback_handoff_incomplete')
        else:
            view['status'] = job['status']
            if job['status'] in {'failed', 'cancelled'}:
                view['error'] = job['error'] or job['stage'] or view['error']
    elif coordination in {'dispatching', 'reporting', 'report_paused', 'report_ready', 'report_failed'}:
        if job['status']:
            view['stage'] = job['stage'] or coordination
            if job['status'] == 'completed':
                view.update(status='completed', phase='report_ready')
            elif job['status'] in {'failed', 'cancelled'}:
                view.update(status='failed', phase='report_failed')
                view['error'] = job['error'] or job['stage'] or view['error']
            elif job['status'] == 'paused':
                view.update(status='paused', phase='report_paused')
            else:
                view.update(
                    status='cancel_requested' if job['status'] == 'cancel_requested' else 'running',
                    phase='reporting',
                )
        elif coordination in {'reporting', 'report_paused'}:
            view.update(status='unavailable', issue='missing_report_job')
        else:
            view['status'] = {
                'dispatching': 'running',
                'report_ready': 'completed',
                'report_failed': 'failed',
            }[coordination]
    elif coordination == 'collection_ready':
        view['status'] = 'completed'
    elif coordination:
        view.update(status='unavailable', issue='unknown_coordination_phase')
    if view['phase'] == 'report_ready' and facts.get('report_job_outcome') == 'no_evidence':
        view.update(status='no_evidence', stage='未取得有效材料，已保存阻碍说明')
    view['finished'] = not view['issue'] and view['status'] in {
        'completed',
        'completed_with_errors',
        'failed',
        'cancelled',
        'no_evidence',
    }
    view['polling'] = not view['finished'] and (
        view['status'] in {'queued', 'running', 'cancel_requested', 'unavailable', 'interrupted'}
        or view['phase'] == 'report_paused'
        or (coordination == 'feedback_revision' and view['status'] == 'paused')
        or (collection == 'paused' and facts.get('stop_reason') == 'search_recovery_wait')
    )
    view['actions'] = actions(facts, view)
    return view


def actions(facts, view):
    """Display capabilities from the same facts; write commands still revalidate."""
    result = ['open-report'] if view['publication']['latest_report_id'] else []
    if view['issue'] or view['phase'] == 'cancelled':
        return result
    collection, phase, job = view['collection_status'], view['coordination_phase'], view['job']
    live_job = job['status'] in {'queued', 'running', 'paused', 'cancel_requested'}
    collecting = phase in {'collecting', 'paused'} and collection in {'queued', 'crawling', 'paused'}
    if collecting:
        result.append('extend-time')
        if collection != 'paused' or facts.get('stop_reason') == 'search_recovery_wait':
            result.append('pause')
    can_continue = (
        collection in {'paused', 'completed', 'completed_with_errors', 'awaiting_review'}
        and not live_job
        and phase != 'dispatching'
    )
    if can_continue and (phase != 'collecting' or collection == 'paused'):
        result += ['continue-automatic', 'continue-with-model']
    if (
        view['phase'] == 'report_failed'
        and collection in {'completed', 'completed_with_errors'}
        and not facts.get('round_count')
    ):
        result.append('retry-report')
    if live_job and job['provider'] == 'local' and job['role'] == 'report':
        if job['status'] == 'paused':
            result.append('resume-job')
        elif job['status'] in {'queued', 'running'}:
            result.append('pause-job')
    if collection not in {'completed', 'completed_with_errors', 'cancelled'}:
        result.append('cancel')
    idle = not live_job and phase not in {'collecting', 'dispatching', 'feedback_revision'}
    if idle and facts.get('pages_fetched', 0):
        result.append('synthesize')
    if idle and view['publication']['latest_report_id']:
        result.append('audit')
    return result


def project_json(encoded):
    """SQLite scalar adapter. Only metadata assembled by the query is decoded."""
    return json.dumps(project(json.loads(encoded)), ensure_ascii=False)


def ctes(tables):
    """Return metadata-only CTEs, adapting absent legacy tables without migration.

    Callers supply table names from their existing read transaction. SQL only
    selects facts and validates links; all state precedence lives in project().
    """
    workflows = (
        'SELECT project_id,phase,segment_started,report_job_id,error FROM research_workflows'
        if 'research_workflows' in tables
        else 'SELECT NULL project_id,NULL phase,NULL segment_started,NULL report_job_id,NULL error WHERE 0'
    )
    rounds = (
        'SELECT project_id,owner_job_id,COUNT(*) OVER(PARTITION BY project_id) owner_count,'
        'ROW_NUMBER() OVER(PARTITION BY project_id ORDER BY created_at DESC,id DESC) position '
        "FROM research_rounds WHERE phase IN ('collecting','ready_for_revision','revising')"
        if 'research_rounds' in tables
        else 'SELECT NULL project_id,NULL owner_job_id,0 owner_count,1 position WHERE 0'
    )
    reports = (
        'SELECT id,project_id,version FROM research_reports'
        if 'research_reports' in tables
        else 'SELECT NULL id,NULL project_id,NULL version WHERE 0'
    )
    publications = (
        'SELECT p.job_id,p.project_id,p.report_id FROM report_publications p JOIN rv_reports r '
        'ON r.id=p.report_id AND r.project_id=p.project_id'
        if 'report_publications' in tables
        else 'SELECT NULL job_id,NULL project_id,NULL report_id WHERE 0'
    )
    return f"""
    rv_receipts AS (
      SELECT json_extract(value,'$.job_id') job_id,json_extract(value,'$.project_id') project_id,
      json_extract(value,'$.report_id') report_id,json_extract(value,'$.outcome') outcome FROM json_each(?)
    ),
    rv_workflows AS ({workflows}),
    rv_rounds AS ({rounds}),
    rv_reports AS ({reports}),
    rv_publications AS ({publications}),
    research_runtime AS (
      SELECT p.id,research_runtime_view(json_object(
        'collection_status',p.status,'collection_error',p.error,'stop_reason',p.stop_reason,
        'coordination_phase',w.phase,'segment_started',w.segment_started,'workflow_error',w.error,
        'report_job_id',w.report_job_id,'report_job_status',j.status,
        'report_job_stage',j.stage,'report_job_provider',j.provider,'report_job_error',substr(j.error_json,1,16000),
        'round_count',COALESCE(r.owner_count,0),'round_job_id',r.owner_job_id,'round_job_status',rj.status,
        'round_job_stage',rj.stage,'round_job_provider',rj.provider,'round_job_error',substr(rj.error_json,1,16000),
        'pages_fetched',p.pages_fetched,'reports_known',{int('research_reports' in tables)},'latest_report_id',latest.id,
        'latest_report_version',latest.version,'report_job_report_id',pub.report_id,
        'round_job_report_id',rpub.report_id,'report_job_outcome',receipt.outcome
      )) runtime_json
      FROM research_projects p LEFT JOIN rv_workflows w ON w.project_id=p.id
      LEFT JOIN job_runs j ON j.id=w.report_job_id AND j.project_id=p.id
      LEFT JOIN rv_rounds r ON r.project_id=p.id AND r.position=1
      LEFT JOIN job_runs rj ON rj.id=r.owner_job_id AND rj.project_id=p.id
      LEFT JOIN rv_reports latest ON latest.id=(
        SELECT id FROM rv_reports WHERE project_id=p.id ORDER BY version DESC LIMIT 1)
      LEFT JOIN rv_publications pub ON pub.job_id=w.report_job_id AND pub.project_id=p.id
      LEFT JOIN rv_publications rpub ON rpub.job_id=r.owner_job_id AND rpub.project_id=p.id
      LEFT JOIN rv_receipts receipt ON receipt.job_id=w.report_job_id AND receipt.project_id=p.id
    )
    """
