"""Application schema installation, separate from runtime interruption recovery."""

import sqlite3
import json
from pathlib import Path
from zhixi.research.research_planning import classify_source_role, classify_topic_bucket
from zhixi.research import research_intent
from zhixi.research import research_workflow

SCHEMA_VERSION = 2


def backfill_candidate_planning(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        "SELECT id,url,link_text,title,relevance,extraction_json FROM research_pages WHERE status='candidate' "
        "AND (NOT json_valid(extraction_json) OR json_extract(extraction_json,'$.planning.source_role') IS NULL "
        "OR json_extract(extraction_json,'$.planning.source_role') IN ('','unclassified'))"
    ).fetchall()
    updated = 0
    for page_id, url, link_text, title, relevance, raw in rows:
        try:
            extraction = json.loads(raw or '{}')
            extraction = extraction if isinstance(extraction, dict) else {}
        except (json.JSONDecodeError, TypeError):
            extraction = {}
        planning = extraction.get('planning') if isinstance(extraction.get('planning'), dict) else {}
        discovery = extraction.get('discovery') if isinstance(extraction.get('discovery'), dict) else {}
        label = str(link_text or title or '')
        planning.update(
            {
                'source_role': classify_source_role(
                    str(url or ''), label, str(discovery.get('query_role') or '')
                ),
                'topic_bucket': classify_topic_bucket(label, str(url or '')),
                'expected_gain': planning.get('expected_gain')
                if planning.get('expected_gain') is not None
                else float(relevance or 0),
                'reason': planning.get('reason') or '历史候选分类已回填；抓取时将结合已获取来源重新评估',
            }
        )
        extraction['planning'] = planning
        connection.execute(
            'UPDATE research_pages SET extraction_json=? WHERE id=?',
            (json.dumps(extraction, ensure_ascii=False), page_id),
        )
        updated += 1
    return updated


def retire_out_of_scope_sitemap_candidates(connection: sqlite3.Connection, now) -> int:
    # Keyset batches contain child data only. A large parent's extraction used
    # to be duplicated once per child by the JOIN + fetchall (tens of GB).
    from collections import OrderedDict

    parents = OrderedDict()
    last_rowid = 0
    retired = 0
    touched_projects: set[str] = set()
    while True:
        rows = connection.execute(
            'SELECT child.rowid,child.id,child.project_id,child.extraction_json,child.parent_page_id,project.scope '
            'FROM research_pages child JOIN research_projects project ON project.id=child.project_id '
            "WHERE child.rowid>? AND child.status IN ('candidate','queued','skipped_budget') "
            'AND child.parent_page_id IS NOT NULL ORDER BY child.rowid LIMIT 32',
            (last_rowid,),
        ).fetchall()
        if not rows:
            break
        last_rowid = rows[-1][0]
        for _, page_id, project_id, child_raw, parent_id, scope in rows:
            try:
                child = json.loads(child_raw or '{}')
            except (json.JSONDecodeError, TypeError):
                continue
            child = child if isinstance(child, dict) else {}
            planning = child.get('planning') if isinstance(child.get('planning'), dict) else {}
            if not str(planning.get('reason') or '').startswith('站点地图发现'):
                continue
            if parent_id not in parents:
                # Project only the two policy fields; never return the full parent
                # document to Python. Invalid historical JSON stays untouched.
                fields = connection.execute(
                    "SELECT CASE WHEN json_valid(extraction_json) THEN json_extract(extraction_json,'$.extraction') END,"
                    "CASE WHEN json_valid(extraction_json) THEN json_extract(extraction_json,'$.discovery.search_engine') END "
                    'FROM research_pages WHERE id=?',
                    (parent_id,),
                ).fetchone()
                parents[parent_id] = (str(fields[0] or ''), bool(fields[1])) if fields else ('', False)
                if len(parents) > 64:
                    parents.popitem(last=False)
            parents.move_to_end(parent_id)
            parent_method, has_search = parents[parent_id]
            if not (parent_method.startswith('adapter:') or (scope == 'any_public' and has_search)):
                continue
            planning['reason'] = '历史站点地图候选已按当前跨来源范围策略停用'
            child['planning'] = planning
            connection.execute(
                "UPDATE research_pages SET status='skipped_policy',extraction_json=?,updated_at=? WHERE id=?",
                (json.dumps(child, ensure_ascii=False), now(), page_id),
            )
            connection.execute(
                "UPDATE research_links SET decision='skipped',reason='跨来源结果不扩张外部站点全量地图' "
                'WHERE project_id=? AND target_page_id=?',
                (project_id, page_id),
            )
            touched_projects.add(project_id)
            retired += 1
    for project_id in touched_projects:
        connection.execute(
            'UPDATE research_projects SET links_found=(SELECT COUNT(*) FROM research_links WHERE project_id=?) '
            'WHERE id=?',
            (project_id, project_id),
        )
        remaining = int(
            connection.execute(
                "SELECT COUNT(*) FROM research_pages WHERE project_id=? AND status IN ('candidate','queued','fetching')",
                (project_id,),
            ).fetchone()[0]
        )
        if not remaining:
            connection.execute(
                "UPDATE research_projects SET status='completed',stop_reason='queue_exhausted',updated_at=? "
                "WHERE id=? AND status='awaiting_review'",
                (now(), project_id),
            )
    return retired


def initialize(data_dir: Path, db_path: Path, now) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=30) as connection:
        # Readers retain a snapshot while collection commits new pages. DELETE
        # journaling previously blocked the detail endpoint behind large writes.
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(
            'CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)'
        )
        version = connection.execute('SELECT COALESCE(MAX(version),0) FROM schema_migrations').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError('数据库来自更新版本，未用旧代码改写')
        from zhixi.storage import task_trash

        task_trash.initialize(connection)
        if version == SCHEMA_VERSION:
            return
        research_intent.initialize(connection)
        connection.execute(
            'CREATE TABLE IF NOT EXISTS research_capacity_steps '
            '(project_id TEXT, step_key TEXT, result_json TEXT, PRIMARY KEY(project_id,step_key))'
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS analyses (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_input TEXT NOT NULL,
                source_title TEXT NOT NULL,
                result_json TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS research_projects (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                mode TEXT NOT NULL,
                scope TEXT NOT NULL,
                max_pages INTEGER NOT NULL,
                max_depth INTEGER NOT NULL,
                pages_fetched INTEGER NOT NULL DEFAULT 0,
                pages_failed INTEGER NOT NULL DEFAULT 0,
                links_found INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                stop_reason TEXT NOT NULL DEFAULT '',
                search_provider TEXT NOT NULL DEFAULT 'none',
                search_status TEXT NOT NULL DEFAULT 'not_requested',
                search_error TEXT NOT NULL DEFAULT ''
            )"""
        )
        project_columns = {row[1] for row in connection.execute('PRAGMA table_info(research_projects)')}
        if 'stop_reason' not in project_columns:
            connection.execute(
                "ALTER TABLE research_projects ADD COLUMN stop_reason TEXT NOT NULL DEFAULT ''"
            )
        if 'search_provider' not in project_columns:
            connection.execute(
                "ALTER TABLE research_projects ADD COLUMN search_provider TEXT NOT NULL DEFAULT 'none'"
            )
        if 'search_status' not in project_columns:
            connection.execute(
                "ALTER TABLE research_projects ADD COLUMN search_status TEXT NOT NULL DEFAULT 'not_requested'"
            )
        if 'search_error' not in project_columns:
            connection.execute(
                "ALTER TABLE research_projects ADD COLUMN search_error TEXT NOT NULL DEFAULT ''"
            )
        if 'public_search_topic' not in project_columns:
            connection.execute('ALTER TABLE research_projects ADD COLUMN public_search_topic TEXT')
        if 'control_revision' not in project_columns:
            connection.execute(
                'ALTER TABLE research_projects ADD COLUMN control_revision INTEGER NOT NULL DEFAULT 0'
            )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS research_pages (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                url TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                parent_page_id TEXT,
                depth INTEGER NOT NULL,
                status TEXT NOT NULL,
                relevance REAL NOT NULL DEFAULT 0,
                link_text TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                page_type TEXT NOT NULL DEFAULT '',
                extraction_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, canonical_url)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS research_links (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                source_page_id TEXT NOT NULL,
                target_url TEXT NOT NULL,
                target_page_id TEXT,
                link_text TEXT NOT NULL DEFAULT '',
                relevance REAL NOT NULL DEFAULT 0,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(project_id, source_page_id, target_url)
            )"""
        )
        connection.execute(
            'CREATE INDEX IF NOT EXISTS idx_research_pages_queue ON research_pages(project_id, status, relevance)'
        )
        connection.execute(
            'CREATE INDEX IF NOT EXISTS idx_research_links_project ON research_links(project_id, source_page_id)'
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS research_duplicate_overrides (
                project_id TEXT NOT NULL,
                left_page_id TEXT NOT NULL,
                right_page_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, left_page_id, right_page_id)
            )"""
        )
        connection.execute(
            'CREATE INDEX IF NOT EXISTS idx_duplicate_overrides_project '
            'ON research_duplicate_overrides(project_id, updated_at)'
        )
        backfill_candidate_planning(connection)
        retire_out_of_scope_sitemap_candidates(connection, now)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS research_evidence (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                claim TEXT NOT NULL,
                quote TEXT NOT NULL,
                evidence_type TEXT NOT NULL,
                stance TEXT NOT NULL,
                confidence REAL NOT NULL,
                fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(project_id, page_id, fingerprint)
            )"""
        )
        evidence_columns = {row[1] for row in connection.execute('PRAGMA table_info(research_evidence)')}
        if 'metadata_json' not in evidence_columns:
            connection.execute(
                "ALTER TABLE research_evidence ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
            )
        evidence_indexes = connection.execute('PRAGMA index_list(research_evidence)').fetchall()
        legacy_evidence_unique = any(
            bool(index[2])
            and [row[2] for row in connection.execute(f"PRAGMA index_info('{index[1]}')").fetchall()]
            == ['project_id', 'fingerprint']
            for index in evidence_indexes
        )
        if legacy_evidence_unique:
            connection.execute('ALTER TABLE research_evidence RENAME TO research_evidence_legacy')
            connection.execute(
                """CREATE TABLE research_evidence (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    page_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    claim TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    evidence_type TEXT NOT NULL,
                    stance TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(project_id, page_id, fingerprint)
                )"""
            )
            connection.execute(
                'INSERT OR IGNORE INTO research_evidence '
                '(id,project_id,page_id,topic,claim,quote,evidence_type,stance,confidence,fingerprint,created_at,metadata_json) '
                'SELECT id,project_id,page_id,topic,claim,quote,evidence_type,stance,confidence,fingerprint,created_at,metadata_json '
                'FROM research_evidence_legacy'
            )
            connection.execute('DROP TABLE research_evidence_legacy')
        connection.execute(
            """CREATE TABLE IF NOT EXISTS research_reports (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                report_json TEXT NOT NULL,
                UNIQUE(project_id, version)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS research_events (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                level TEXT NOT NULL,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                page_id TEXT,
                details_json TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        connection.execute(
            'CREATE INDEX IF NOT EXISTS idx_research_events_project ON research_events(project_id, created_at)'
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS search_cache (
                engine TEXT NOT NULL,
                query TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                results_json TEXT NOT NULL,
                PRIMARY KEY(engine, query)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS job_runs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT 'analysis',
                status TEXT NOT NULL,
                progress REAL,
                stage TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                provider TEXT NOT NULL DEFAULT '',
                model_name TEXT NOT NULL DEFAULT '',
                project_id TEXT,
                attempt INTEGER NOT NULL DEFAULT 1,
                retried_from TEXT,
                request_json TEXT NOT NULL DEFAULT '{}',
                error_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                pause_started_at REAL,
                paused_seconds REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )"""
        )
        job_columns = {row[1] for row in connection.execute('PRAGMA table_info(job_runs)')}
        from zhixi.storage import job_store

        job_store.initialize(connection)
        if 'pause_started_at' not in job_columns:
            connection.execute('ALTER TABLE job_runs ADD COLUMN pause_started_at REAL')
        if 'paused_seconds' not in job_columns:
            connection.execute('ALTER TABLE job_runs ADD COLUMN paused_seconds REAL NOT NULL DEFAULT 0')
        connection.execute(
            'CREATE INDEX IF NOT EXISTS idx_research_evidence_project ON research_evidence(project_id, page_id)'
        )
        connection.execute(
            'CREATE TABLE IF NOT EXISTS report_questions ('
            'id TEXT PRIMARY KEY,project_id TEXT NOT NULL,report_id TEXT NOT NULL,report_version INTEGER NOT NULL,'
            'anchor_json TEXT NOT NULL,question TEXT NOT NULL,mode TEXT NOT NULL,parent_question_id TEXT,'
            "job_id TEXT NOT NULL,created_at TEXT NOT NULL,answer_json TEXT NOT NULL DEFAULT '{}')"
        )
        connection.execute(
            'CREATE INDEX IF NOT EXISTS idx_report_questions_project ON report_questions(project_id,report_version)'
        )
        connection.execute(
            'CREATE TABLE IF NOT EXISTS question_research_links ('
            'question_id TEXT NOT NULL,child_project_id TEXT NOT NULL,created_at TEXT NOT NULL,'
            'PRIMARY KEY(question_id,child_project_id))'
        )
        connection.execute(
            'CREATE TABLE IF NOT EXISTS coverage_research_links '
            '(report_id TEXT,gap_id TEXT,child_project_id TEXT,query TEXT,created_at TEXT,PRIMARY KEY(report_id,gap_id,child_project_id))'
        )
        research_workflow.initialize(connection)
        from zhixi.research import research_rounds

        research_rounds.initialize(connection)
        from zhixi.sources import public_research_sources

        public_research_sources.initialize(connection)
        connection.execute('INSERT INTO schema_migrations VALUES (?,?)', (SCHEMA_VERSION, now()))


def recover_runtime(db_path: Path) -> None:
    """Only the owning service startup calls this; never an ordinary reader."""
    with sqlite3.connect(db_path, timeout=30) as connection:
        research_workflow.recover_runtime(connection)
        from zhixi.research import research_rounds

        research_rounds.recover_runtime(connection)
        connection.execute(
            "UPDATE research_intents SET status='interrupted',stage='目标分析被重启中断，可继续任务' WHERE status='planning'"
        )
        connection.execute(
            "UPDATE research_projects SET control_revision=control_revision+1,status = 'paused', stop_reason = 'service_restarted', "
            "error = '服务重启后已暂停，可继续运行' WHERE status IN ('queued','crawling')"
        )
        connection.execute(
            "UPDATE research_projects SET search_status = 'queued' WHERE search_status = 'running'"
        )
        connection.execute("UPDATE research_pages SET status = 'queued' WHERE status = 'fetching'")
