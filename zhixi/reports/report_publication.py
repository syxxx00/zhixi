"""Atomic, retry-safe publication of reports and their history entries."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3
import uuid


class ConcurrentReportChange(ValueError):
    """An automatic revision must re-evaluate a newer report, not overwrite it."""


def _find(db, job_id, project_id):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_publications'"
    ).fetchone():
        return None
    seen = set()
    while job_id and job_id not in seen:
        seen.add(job_id)
        row = db.execute(
            'SELECT r.report_json FROM report_publications p JOIN research_reports r ON r.id=p.report_id '
            'WHERE p.job_id=? AND p.project_id=? AND r.project_id=?',
            (job_id, project_id, project_id),
        ).fetchone()
        if row:
            return json.loads(row[0])
        parent = db.execute(
            'SELECT retried_from FROM job_runs WHERE id=? AND project_id=?', (job_id, project_id)
        ).fetchone()
        job_id = parent[0] if parent else None
    return None


def published_report(db_path, job_id, project_id):
    """Recover an already committed result before repeating expensive inference."""
    with sqlite3.connect(db_path) as db:
        return _find(db, job_id, project_id)


def publish_report(db_path, job_id, project_id, result, request, save_history, *, expected_version=None):
    with sqlite3.connect(db_path, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute(
            'CREATE TABLE IF NOT EXISTS report_publications '
            '(job_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, report_id TEXT NOT NULL UNIQUE)'
        )
        existing = _find(db, job_id, project_id)
        if existing is not None:
            return existing
        current = db.execute(
            'SELECT COALESCE(MAX(version),0) FROM research_reports WHERE project_id=?', (project_id,)
        ).fetchone()[0]
        if expected_version is not None and current != expected_version:
            raise ConcurrentReportChange('报告已有新版，自动修订需要重新对齐，未覆盖已有成果')
        saved = deepcopy(result)
        # Legacy callers may still carry shared inline scopes. Normalize the
        # copy before JSON expands them; never mutate the caller's report.
        if isinstance(saved.get('source_grounding'), dict):
            from zhixi.storage.review_scope_storage import compact_review_scopes

            compact_review_scopes(saved['source_grounding'])
        report_id = uuid.uuid4().hex
        version = current + 1
        saved.update(history_id=job_id, report_id=report_id, report_version=version)
        from zhixi.media.research_media import publish_media_snapshot

        previous = (
            (result['report_id'], result['report_version'])
            if result.get('report_id') and result.get('report_version')
            else None
        )
        publish_media_snapshot(db, project_id, saved, previous_report=previous)
        db.execute(
            'INSERT INTO research_reports VALUES (?,?,?,?,?)',
            (
                report_id,
                project_id,
                version,
                datetime.now(timezone.utc).isoformat(timespec='seconds'),
                json.dumps(saved, ensure_ascii=False),
            ),
        )
        save_history(job_id, request, saved, connection=db)
        db.execute('INSERT INTO report_publications VALUES (?,?,?)', (job_id, project_id, report_id))
    return saved
