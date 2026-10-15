"""Collection execution identity and fenced transactions, not research policy.

A control revision identifies a collection generation. Worker callbacks carry
that revision; a write checks it inside the same SQLite transaction as its data.
No global SQLite patch, process cancellation or model dependency is involved.
"""

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps


class StaleExecution(Exception):
    """This callback belongs to a collection generation that has ended."""


@dataclass(frozen=True)
class Execution:
    database: str
    project_id: str
    revision: int


CURRENT = ContextVar('collection_execution', default=None)
OWNERS = {}  # (database, project, collector/discovery registry identity) -> Execution


def snapshot(db_path, project_id):
    with sqlite3.connect(db_path, timeout=30) as db:
        row = db.execute(
            'SELECT control_revision FROM research_projects WHERE id=?', (project_id,)
        ).fetchone()
    return Execution(str(db_path), project_id, row[0]) if row else None


def matches(db, execution):
    if execution is None:
        return True
    row = db.execute(
        'SELECT control_revision FROM research_projects WHERE id=?', (execution.project_id,)
    ).fetchone()
    return bool(row and row[0] == execution.revision)


def is_current(execution=None):
    execution = execution or CURRENT.get()
    if execution is None:
        return True  # Commands and standalone report readers are not collection workers.
    with sqlite3.connect(execution.database, timeout=30) as db:
        return matches(db, execution)


@contextmanager
def bind(execution):
    token = CURRENT.set(execution)
    try:
        yield
    finally:
        CURRENT.reset(token)


@contextmanager
def connection(db_path, project_id, *, write=True):
    with sqlite3.connect(db_path, timeout=30) as db:
        if write:
            db.execute('BEGIN IMMEDIATE')
        execution = CURRENT.get()
        if execution and (
            execution.database != str(db_path)
            or execution.project_id != project_id
            or not matches(db, execution)
        ):
            raise StaleExecution('旧采集执行已结束，未写入新一轮现场')
        yield db


def scoped(get_db):
    """Also fence explicit/direct worker calls, not just OS-thread entry points."""

    def decorate(worker):
        @wraps(worker)
        def run(project_id):
            execution = CURRENT.get() or snapshot(get_db(), project_id)
            if execution is None:
                return
            with bind(execution):
                try:
                    return worker(project_id)
                except StaleExecution:
                    return  # A user control superseded this invocation; it is not task failure.

        return run

    return decorate


def release(db_path, project_id, registry, execution):
    """Caller holds registry lock; an old thread cannot remove a newer owner."""
    key = (str(db_path), project_id, id(registry))
    owner = OWNERS.get(key)
    if owner is None or owner is execution:
        registry.discard(project_id)
        OWNERS.pop(key, None)


def working(db_path, project_id, registry):
    if project_id not in registry:
        return False
    owner = OWNERS.get((str(db_path), project_id, id(registry)))
    return owner is None or is_current(owner)


def start(db_path, project_id, registry, lock, thread_factory, worker):
    """Reserve this generation, launch, and release only our reservation on failure."""
    with lock:
        execution = snapshot(db_path, project_id)
        if execution is None:
            return False
        if working(db_path, project_id, registry):
            return False
        key = (str(db_path), project_id, id(registry))
        OWNERS[key] = execution
        registry.add(project_id)

    def run():
        try:
            with bind(execution):
                worker(project_id)
        finally:
            with lock:
                release(db_path, project_id, registry, execution)

    try:
        thread_factory(target=run, daemon=True, name=f'research-{worker.__name__}-{project_id[:8]}').start()
    except Exception:
        with lock:
            release(db_path, project_id, registry, execution)
        raise
    return True
