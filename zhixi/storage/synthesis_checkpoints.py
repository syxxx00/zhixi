"""Exact-input, lineage-scoped synthesis checkpoints, independent of execution."""

import json
import sqlite3
import time
from typing import Callable


def reusable_step(result, phase=''):
    """Do not make an explicit failed generation permanent across retries.

    Stored invalid attempts stay available for diagnosis. This predicate is
    structural; a reusable result has not thereby passed factual verification.
    """
    if not isinstance(result, dict):
        return False
    if 'runtime' in result and not isinstance(result['runtime'], dict):
        return False
    validation = result.get('generation_validation') or {}
    if not isinstance(validation, dict) or validation.get('status') == 'degraded':
        return False
    if phase == 'organization' or phase.startswith('organization:outline:') or phase.startswith('organization:compose:'):
        from zhixi.reports.report_outline import valid_outline

        if phase.startswith(('organization:compose:lineage_v1:', 'organization:compose:lineage_v2:')):
            # Resolving actual input selections happens at the consumer;
            # old summary-only checkpoints cannot claim the new handoff.
            return (isinstance(result.get('title'), str) and bool(result['title'].strip())
                    and isinstance(result.get('statements'), list) and bool(result['statements'])
                    and all(isinstance(s, dict) and isinstance(s.get('text'), str)
                            and ((phase.startswith('organization:compose:lineage_v2:') and s['text'] == '')
                                 if s.get('kind') == 'retained_original' else bool(s['text'].strip()))
                            and isinstance(s.get('references'), list) and bool(s['references'])
                            for s in result['statements']))

        # Dedicated chapter/summary producers return title/summary. Exact
        # corpus and phase remain part of the cache key; no topic is inherited.
        return valid_outline(result) or (phase.startswith('organization:compose:')
                                         and isinstance(result.get('title'), str) and bool(result['title'].strip())
                                         and isinstance(result.get('summary'), str) and bool(result['summary'].strip()))
    if phase == 'organization:materials':
        sections = result.get('sections')
        return isinstance(sections, list) and bool(sections) and all(
            isinstance(s, dict) and isinstance(s.get('title'), str) and bool(s['title'].strip())
            and isinstance(s.get('focus'), str) and bool(s['focus'].strip())
            and isinstance(s.get('card_ids'), list) and bool(s['card_ids']) for s in sections)
    return True


def restored_step(encoded, phase=''):
    """A damaged checkpoint is not usable, but its stored attempt is retained."""
    try:
        result = json.loads(encoded)
    except (ValueError, TypeError):
        return None
    return result if reusable_step(result, phase) else None


def lineage_step(connection, job_id, previous_job, key, phase=''):
    """Nearest valid exact-input checkpoint, through empty retries, same project/kind."""
    has_jobs = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_runs'"
    ).fetchone()

    def get_job(identity):
        return (
            connection.execute(
                'SELECT project_id,kind,retried_from FROM job_runs WHERE id=?', (identity,)
            ).fetchone()
            if has_jobs
            else None
        )

    root = get_job(job_id)
    scope = tuple(root[:2]) if root else None
    identity = job_id
    seen = set()
    while identity and identity not in seen:
        seen.add(identity)
        job = get_job(identity)
        if scope and (not job or tuple(job[:2]) != scope):
            break
        row = connection.execute(
            'SELECT result_json FROM research_synthesis_steps WHERE job_id=? AND step_key=?', (identity, key)
        ).fetchone()
        result = restored_step(row[0], phase) if row else None
        if result is not None:
            return result
        identity = job[2] if job else previous_job if identity == job_id else None
    return None


def cached_step(db_path, job_id: str, previous_job: str | None, key: str, run: Callable[[], dict]) -> dict:
    """Persist whole completed steps and reuse them only within a retry lineage."""
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            'CREATE TABLE IF NOT EXISTS research_synthesis_steps '
            '(job_id TEXT,step_key TEXT,created_at TEXT,result_json TEXT,PRIMARY KEY(job_id,step_key))'
        )
        result = lineage_step(connection, job_id, previous_job, key)
    if result is not None:
        runtime = result.setdefault('runtime', {})
        runtime.setdefault('original_generation_seconds', runtime.get('seconds', 0))
        runtime.update(seconds=0, checkpoint_reused=True)
    else:
        result = run()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            'INSERT OR REPLACE INTO research_synthesis_steps VALUES (?,?,?,?)',
            (job_id, key, str(time.time()), json.dumps(result, ensure_ascii=False)),
        )
    return result
