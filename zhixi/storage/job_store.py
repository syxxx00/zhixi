"""Persistent job records, independent of HTTP, models and worker scheduling.

Revisions are assigned while the in-memory registry is locked. SQLite rejects
late snapshots. Startup restores execution candidates, not full report bodies;
single records and results are loaded on demand.
"""

import json
import sqlite3
import time


TERMINAL = frozenset({'completed', 'failed', 'cancelled'})
SUMMARY_COLUMNS = (
    'id,kind,status,progress,stage,created_at,started_at,finished_at,'
    'provider,model_name,project_id,attempt,retried_from,pause_started_at,'
    'paused_seconds,updated_at,revision'
)


def initialize(db):
    if 'revision' not in {row[1] for row in db.execute('PRAGMA table_info(job_runs)')}:
        db.execute('ALTER TABLE job_runs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')


def snapshot(job):
    """Caller must hold the registry lock through mutation and this snapshot."""
    job['_revision'] = int(job.get('_revision', 0)) + 1
    return job.copy()


def apply_changes(job, changes, *, expected_status=None, restart=False):
    current = job.get('status')
    if expected_status is not None and current not in expected_status:
        return False
    target = changes.get('status', current)
    if current in TERMINAL and not restart:
        # Allow attaching retry lineage, but not late progress/status callbacks.
        if any(key in changes for key in ('status', 'stage', 'progress', 'finished_at')):
            return False
    if current == 'cancel_requested' and target not in {'cancel_requested', 'cancelled'}:
        if target not in TERMINAL:
            return False
        changes = {**changes, 'status': 'cancelled', 'stage': '已取消', 'progress': None}
    if current == 'paused' and target in {'queued', 'running'} and expected_status != {'paused'}:
        return False
    job.update(changes)
    return True


def persist(db_path, job, request_data, now):
    request_data = dict(request_data)
    if isinstance(request_data.get('api_config'), dict):
        request_data['api_config'] = {**request_data['api_config'], 'api_key': ''}
    fields = SUMMARY_COLUMNS.split(',')
    values = {
        'kind': 'analysis',
        'status': 'queued',
        'stage': '',
        'created_at': time.time(),
        'provider': '',
        'model_name': '',
        'attempt': 1,
        **{k: v for k, v in job.items() if k in fields},
        'revision': int(job.get('_revision', 1)),
        'updated_at': now,
        'paused_seconds': float(job.get('paused_seconds') or 0),
        'request_json': json.dumps(request_data, ensure_ascii=False),
        'error_json': json.dumps(job.get('error_info') or {}, ensure_ascii=False),
        'result_json': json.dumps(job.get('result') or {}, ensure_ascii=False),
    }
    fields += ['request_json', 'error_json', 'result_json']
    updates = ','.join(f'{key}=excluded.{key}' for key in fields if key != 'id')
    with sqlite3.connect(db_path, timeout=30) as db:
        return bool(
            db.execute(
                f'INSERT INTO job_runs ({",".join(fields)}) VALUES ({",".join("?" for _ in fields)}) '
                f'ON CONFLICT(id) DO UPDATE SET {updates} WHERE excluded.revision > job_runs.revision',
                [values.get(key) for key in fields],
            ).rowcount
        )


def decode(row, request_factory):
    item = dict(row)
    item['_revision'] = item.pop('revision')
    errors = []
    for column, field in [
        ('request_json', 'request'),
        ('error_json', 'error_info'),
        ('result_json', 'result'),
    ]:
        if column not in item:
            continue
        encoded = item.pop(column)
        try:
            value = json.loads(encoded or '{}')
            if not isinstance(value, dict):
                raise ValueError('expected an object')
            item[field] = request_factory(item['kind'], value) if field == 'request' and value else value
        except (ValueError, TypeError):
            errors.append(column)
            item[field] = None if field == 'request' else {}
    item.setdefault('request', None)
    if errors:
        item['storage_error'] = {
            'code': 'stored_record_invalid',
            'fields': errors,
            'message': '此记录的部分字段损坏，原始数据保留；未自动恢复。',
        }
        item['request'] = None
    item['stage_started_at'] = item.get('started_at') or item.get('created_at')
    return item


def load(db_path, identity, request_factory, *, result=False):
    with sqlite3.connect(db_path, timeout=30) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            f'SELECT {SUMMARY_COLUMNS},request_json,error_json'
            + (',result_json' if result else '')
            + ' FROM job_runs WHERE id=?',
            (identity,),
        ).fetchone()
    return decode(row, request_factory) if row else None


def restore(db_path, request_factory, now):
    interrupted = {
        'code': 'service_restarted',
        'message': '后台服务重启，任务已中断并保留现场。',
        'technical': 'The in-process worker ended during service restart.',
        'retryable': True,
        'suggestion': '本地任务可以恢复；API 凭据需要重新授权。',
    }
    with sqlite3.connect(db_path, timeout=30) as db:
        db.execute(
            "UPDATE job_runs SET status='cancelled',stage='已取消',finished_at=?,"
            "updated_at=?,revision=revision+1 WHERE status='cancel_requested'",
            (time.time(), now),
        )
        db.execute(
            "UPDATE job_runs SET status='failed',stage='服务重启后中断',finished_at=?,"
            "error_json=?,updated_at=?,revision=revision+1 WHERE status IN ('queued','running')",
            (time.time(), json.dumps(interrupted, ensure_ascii=False), now),
        )
        db.row_factory = sqlite3.Row
        # No history-count limit and no result_json. Completed history is read
        # through paginated catalog/history APIs, or a single job ID on demand.
        rows = db.execute(
            f'SELECT {SUMMARY_COLUMNS},request_json,error_json FROM job_runs '
            "WHERE status IN ('paused','cancel_requested') OR (status='failed' AND "
            "CASE WHEN json_valid(error_json) THEN json_extract(error_json,'$.code') END "
            "= 'service_restarted') ORDER BY created_at"
        )
        return {row['id']: decode(row, request_factory) for row in rows}
