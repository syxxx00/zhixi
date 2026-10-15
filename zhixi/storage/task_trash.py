"""Recoverable, server-side task removal.

Queue cards are projections of jobs and research projects. This table owns only
visibility; callers settle execution before archiving an item. Permanent purge
is deliberately separate and allowed only for an item already in the trash.
"""

import json
import sqlite3


PROJECT_TABLES = (
    'research_capacity_steps', 'research_duplicate_overrides', 'research_events',
    'research_evidence', 'research_intents', 'research_links', 'research_pages',
    'research_public_captures', 'research_reading_rounds', 'research_reports',
    'research_rounds', 'research_route_checks', 'research_workflows',
    'report_questions', 'report_publications', 'report_media_bindings',
    'media_evidence_records', 'media_review_events',
)


def initialize(db):
    db.execute(
        'CREATE TABLE IF NOT EXISTS task_trash ('
        'kind TEXT NOT NULL,item_id TEXT NOT NULL,trashed_at TEXT NOT NULL,'
        'label TEXT NOT NULL,PRIMARY KEY(kind,item_id))'
    )


def archive(db_path, kind, item_id, label, now):
    with sqlite3.connect(db_path, timeout=30) as db:
        initialize(db)
        db.execute(
            'INSERT OR IGNORE INTO task_trash(kind,item_id,trashed_at,label) VALUES (?,?,?,?)',
            (kind, item_id, now, label),
        )


def restore(db_path, kind, item_id):
    with sqlite3.connect(db_path, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute(
            'SELECT 1 FROM task_trash WHERE kind=? AND item_id=?', (kind, item_id)
        ).fetchone():
            return False
        if kind == 'research':
            active = db.execute(
                "SELECT 1 FROM job_runs WHERE project_id=? AND status IN ('queued','running','cancel_requested') LIMIT 1",
                (item_id,),
            ).fetchone()
        elif kind == 'job':
            active = db.execute(
                "SELECT 1 FROM job_runs WHERE id=? AND status IN ('queued','running','cancel_requested')",
                (item_id,),
            ).fetchone()
        else:
            active = None
        if active:
            raise ValueError('任务仍在停止中，请稍后再恢复')
        return bool(db.execute(
            'DELETE FROM task_trash WHERE kind=? AND item_id=?', (kind, item_id)
        ).rowcount)


def entries(db_path):
    with sqlite3.connect(db_path, timeout=30) as db:
        initialize(db)
        return [
            {'kind': kind, 'id': item_id, 'trashed_at': when, 'label': label}
            for kind, item_id, when, label in db.execute(
                'SELECT kind,item_id,trashed_at,label FROM task_trash ORDER BY trashed_at DESC'
            )
        ]


def purge(db_path, kind, item_id):
    """Remove one archived object and only its owned records; return removed job IDs."""
    with sqlite3.connect(db_path, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute(
            'SELECT 1 FROM task_trash WHERE kind=? AND item_id=?', (kind, item_id)
        ).fetchone():
            raise LookupError('记录不在回收站')
        job_ids = []
        if kind == 'research':
            job_ids = [row[0] for row in db.execute(
                'SELECT id FROM job_runs WHERE project_id=?', (item_id,)
            )]
            active = db.execute(
                "SELECT 1 FROM job_runs WHERE project_id=? AND status IN ('queued','running','paused','cancel_requested') LIMIT 1",
                (item_id,),
            ).fetchone()
            if active:
                raise ValueError('研究仍有未结束的作业，请等待取消完成后再永久删除')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            known = set(PROJECT_TABLES) | {'research_projects', 'job_runs'}
            for table in tables:
                if table not in known and 'project_id' in {
                    column[1] for column in db.execute(f'PRAGMA table_info("{table}")')
                }:
                    raise ValueError(f'尚未确认 {table} 的归属，未删除任何数据')
            report_ids = [row[0] for row in db.execute(
                'SELECT id FROM research_reports WHERE project_id=?', (item_id,)
            )]
            question_ids = [row[0] for row in db.execute(
                'SELECT id FROM report_questions WHERE project_id=?', (item_id,)
            )]
            if 'research_synthesis_steps' in tables:
                for job_id in job_ids:
                    db.execute('DELETE FROM research_synthesis_steps WHERE job_id=?', (job_id,))
            for question_id in question_ids:
                db.execute('DELETE FROM question_research_links WHERE question_id=?', (question_id,))
            db.execute('DELETE FROM question_research_links WHERE child_project_id=?', (item_id,))
            for report_id in report_ids:
                db.execute('DELETE FROM coverage_research_links WHERE report_id=?', (report_id,))
            db.execute('DELETE FROM coverage_research_links WHERE child_project_id=?', (item_id,))
            db.execute(
                'DELETE FROM analyses WHERE '
                "CASE WHEN json_valid(result_json) THEN json_extract(result_json,'$.research.project_id') END=?",
                (item_id,),
            )
            for table in PROJECT_TABLES:
                if table in tables:
                    db.execute(f'DELETE FROM "{table}" WHERE project_id=?', (item_id,))
            db.execute('DELETE FROM job_runs WHERE project_id=?', (item_id,))
            db.execute('DELETE FROM research_projects WHERE id=?', (item_id,))
        elif kind == 'job':
            row = db.execute('SELECT status,project_id FROM job_runs WHERE id=?', (item_id,)).fetchone()
            if row and row[0] not in {'completed', 'failed', 'cancelled'}:
                raise ValueError('作业尚未结束，请等待取消完成后再永久删除')
            if row and row[1]:
                raise ValueError('该作业属于研究项目，请在项目回收站处理')
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='research_synthesis_steps'").fetchone():
                db.execute('DELETE FROM research_synthesis_steps WHERE job_id=?', (item_id,))
            db.execute('DELETE FROM analyses WHERE id=?', (item_id,))
            db.execute('DELETE FROM job_runs WHERE id=?', (item_id,))
            job_ids = [item_id]
        elif kind == 'analysis':
            row = db.execute('SELECT result_json FROM analyses WHERE id=?', (item_id,)).fetchone()
            if row:
                try:
                    project_id = (json.loads(row[0]).get('research') or {}).get('project_id')
                except (ValueError, TypeError):
                    project_id = None
                if project_id:
                    raise ValueError('这是研究报告的历史副本；请在对应研究项目的回收站处理')
            db.execute('DELETE FROM analyses WHERE id=?', (item_id,))
        else:
            raise ValueError('未知的回收站记录类型')
        db.execute('DELETE FROM task_trash WHERE kind=? AND item_id=?', (kind, item_id))
        return job_ids
