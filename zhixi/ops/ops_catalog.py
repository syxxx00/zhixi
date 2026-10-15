"""Read-only, paginated OPS directory. Lists never select requests or report bodies."""

import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

from zhixi.ops.ops_experiments import list_experiments
from zhixi.ops import research_runtime_view
from zhixi.ops.research_runtime_receipts import publication_index


def connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.create_function('research_runtime_view', 1, research_runtime_view.project_json, deterministic=True)
    return db


# All sources have the same small projection; SQLite filters/sorts/pages it.
CATALOG = """
publications AS (SELECT * FROM rv_receipts), catalog AS (
 SELECT 'job' AS kind,j.id,COALESCE(NULLIF(a.source_title,''),NULLIF(p.goal,''),NULLIF(j.model_name,''),j.id) AS title,
 j.status,j.stage,j.kind AS phase,CAST(strftime('%s',j.updated_at) AS REAL) AS updated_at,
 j.created_at,j.project_id,j.model_name,NULL runtime_json
 FROM job_runs j LEFT JOIN analyses a ON a.id=j.id LEFT JOIN research_projects p ON p.id=j.project_id
 UNION ALL
 SELECT 'research',p.id,p.goal,json_extract(v.runtime_json,'$.status'),
 json_extract(v.runtime_json,'$.stage'),json_extract(v.runtime_json,'$.phase'),
 CAST(strftime('%s',p.updated_at) AS REAL),CAST(strftime('%s',p.created_at) AS REAL),p.id,'',v.runtime_json
 FROM research_projects p JOIN research_runtime v ON v.id=p.id
 UNION ALL
 SELECT CASE WHEN pub.job_id IS NOT NULL THEN 'report' ELSE 'analysis' END,a.id,a.source_title,
 COALESCE(pub.outcome,'saved'),CASE WHEN pub.outcome='no_evidence' THEN '未取得有效材料，已保存阻碍说明'
 WHEN pub.job_id IS NOT NULL THEN '报告版本已保存，不代表当前研究已结束' ELSE '分析结果已保存' END,
 COALESCE(pub.outcome,'saved'),
 CAST(strftime('%s',a.updated_at) AS REAL),CAST(strftime('%s',a.created_at) AS REAL),pub.project_id,'',NULL
 FROM analyses a LEFT JOIN publications pub ON pub.job_id=a.id
 WHERE NOT EXISTS (SELECT 1 FROM job_runs j WHERE j.id=a.id)
 UNION ALL
 SELECT 'experiment',json_extract(value,'$.id'),COALESCE(json_extract(value,'$.goal'),json_extract(value,'$.id')),
 json_extract(value,'$.status'),json_extract(value,'$.stage'),json_extract(value,'$.phase'),
 COALESCE(json_extract(value,'$.updated_at'),0),NULL,json_extract(value,'$.project_id'),'',NULL FROM json_each(?)
)
"""


def catalog_sql(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return 'WITH ' + research_runtime_view.ctes(tables) + ',' + CATALOG


def catalog_item(row):
    item = dict(row)
    encoded = item.pop('runtime_json', None)
    if encoded:
        item['runtime'] = runtime = json.loads(encoded)
        item['error'] = runtime['error']
    return item


def directory(root, db_path, *, q='', kind='', status='', page=1, page_size=20, sort='updated', since=0):
    if kind not in {'', 'job', 'research', 'analysis', 'report', 'experiment'}:
        raise ValueError('未知任务类型')
    if sort not in {'updated', 'oldest', 'title'}:
        raise ValueError('未知排序')
    page_size = max(1, min(int(page_size), 100))
    experiments = list_experiments(root)
    filters = []
    where = ['1=1']
    if q.strip():
        # instr implements literal search: %, _ and quotes are not SQL wildcards.
        where.append(
            "(instr(lower(title),lower(?)) OR instr(lower(id),lower(?)) OR instr(lower(COALESCE(project_id,'')),lower(?)))"
        )
        filters.extend([q.strip()] * 3)
    if kind:
        where.append('kind=?')
        filters.append(kind)
    if status:
        if status == 'active':
            where.append("status IN ('running','queued','pause_requested','resuming','cancel_requested')")
        elif status == 'attention':
            where.append("status IN ('failed','interrupted','unavailable')")
        else:
            where.append('status=?')
            filters.append(status)
    if since:
        where.append('updated_at>=?')
        filters.append(float(since))
    order = {'updated': 'updated_at DESC', 'oldest': 'updated_at ASC', 'title': 'title COLLATE NOCASE ASC'}[
        sort
    ]
    with connect(db_path) as db:
        db.execute('BEGIN')
        sql = catalog_sql(db)
        params = [json.dumps(publication_index(db)), json.dumps(experiments, ensure_ascii=False), *filters]
        filtered = sql + ' SELECT * FROM catalog WHERE ' + ' AND '.join(where)
        total = db.execute('SELECT COUNT(*) FROM (' + filtered + ')', params).fetchone()[0]
        page = min(max(1, int(page)), max(1, (total + page_size - 1) // page_size))
        items = [
            catalog_item(r)
            for r in db.execute(
                filtered + f' ORDER BY {order},kind,id LIMIT ? OFFSET ?',
                params + [page_size, (page - 1) * page_size],
            )
        ]
        counts = {
            r[0]: r[1]
            for r in db.execute(sql + 'SELECT status,COUNT(*) FROM catalog GROUP BY status', params[:2])
        }
    # Only list labels are shortened; the detail and original data remain complete.
    for item in items:
        item['title'] = str(item['title'] or item['id'])[:500]
        item['stage'] = str(item['stage'] or '')[:500]
    return dict(items=items, total=total, page=page, page_size=page_size, counts=counts)


def experiment(root, identity):
    if not identity or Path(identity).name != identity or '/' in identity or '\\' in identity:
        raise ValueError('无效任务标识')
    rows = list_experiments(root, identity)
    if not rows:
        raise LookupError('实验不存在')
    return rows[0]


def task_database(root, db_path, kind, identity):
    if kind == 'experiment':
        item = experiment(root, identity)
        if not item.get('artifact_path'):
            raise LookupError('实验现场暂不可读')
        return Path(item['artifact_path']).parent / 'history.sqlite3', item.get('project_id')
    if kind == 'research':
        with connect(db_path) as db:
            if not db.execute('SELECT 1 FROM research_projects WHERE id=?', (identity,)).fetchone():
                raise LookupError('研究不存在')
        return Path(db_path), identity
    raise ValueError('该类型没有研究材料')


def detail(root, db_path, kind, identity):
    if kind == 'experiment':
        item = experiment(root, identity)
        item.update(kind=kind, title=item.get('goal') or identity)
    elif kind == 'job':
        with connect(db_path) as db:
            row = db.execute(
                'SELECT id,kind,status,stage,progress,model_name,provider,project_id,created_at,started_at,'
                'finished_at,paused_seconds,pause_started_at,attempt,retried_from,updated_at,revision,substr(error_json,1,16000) error_json '
                'FROM job_runs WHERE id=?',
                (identity,),
            ).fetchone()
            if not row:
                raise LookupError('任务不存在')
            item = dict(row)
            saved = db.execute('SELECT 1 FROM analyses WHERE id=?', (identity,)).fetchone()
        item['phase'] = item.pop('kind')
        item.update(kind=kind, title=item['model_name'] or identity)
        try:
            item['error'] = json.loads(item.pop('error_json')).get('message', '')
        except ValueError:
            item['error'] = '错误详情较长，请在原任务中查看'
        item['elapsed_seconds'] = max(
            0,
            (item['finished_at'] or time.time())
            - (item['started_at'] or time.time())
            - item['paused_seconds'],
        )
        if item['status'] == 'paused' and item['pause_started_at']:
            item['elapsed_seconds'] = max(
                0, item['elapsed_seconds'] - (time.time() - item['pause_started_at'])
            )
        if saved:
            item['open_url'] = '/?history=' + quote(identity, safe='')
        elif item['project_id']:
            item['open_url'] = '/?research=' + quote(item['project_id'], safe='')
    elif kind in {'analysis', 'report'}:
        with connect(db_path) as db:
            db.execute('BEGIN')
            row = db.execute(
                catalog_sql(db) + "SELECT * FROM catalog WHERE kind IN ('analysis','report') AND id=?",
                (json.dumps(publication_index(db)), '[]', identity),
            ).fetchone()
        if not row:
            raise LookupError('分析不存在')
        item = catalog_item(row)
        item['open_url'] = '/?history=' + quote(identity, safe='')
        if item['project_id']:
            item['project_url'] = '/?research=' + quote(item['project_id'], safe='')
    elif kind == 'research':
        with connect(db_path) as db:
            db.execute('BEGIN')
            row = db.execute(
                catalog_sql(db) + "SELECT * FROM catalog WHERE kind='research' AND id=?",
                (json.dumps(publication_index(db, identity)), '[]', identity),
            ).fetchone()
            if not row:
                raise LookupError('研究不存在')
            item = catalog_item(row)
            extra = db.execute(
                'SELECT pages_fetched,pages_failed,links_found,error AS collection_error FROM research_projects WHERE id=?',
                (identity,),
            ).fetchone()
            item.update(dict(extra))
            item['open_url'] = '/?research=' + quote(identity, safe='')
    else:
        raise ValueError('未知任务类型')
    if kind in {'experiment', 'research'}:
        path, project_id = task_database(root, db_path, kind, identity)
        item['reports'] = []
        if path.is_file() and project_id:
            with connect(path) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='research_reports'").fetchone():
                    item['reports'] = [
                        dict(r)
                        for r in db.execute(
                            'SELECT id,version,created_at FROM research_reports WHERE project_id=? ORDER BY version DESC LIMIT 100',
                            (project_id,),
                        )
                    ]
    return item


def records(root, db_path, kind, identity, section, q='', page=1, page_size=20):
    path, project_id = task_database(root, db_path, kind, identity)
    if section not in {'pages', 'events'}:
        raise ValueError('未知记录类型')
    page_size = max(1, min(int(page_size), 100))
    table = 'research_pages' if section == 'pages' else 'research_events'
    fields = (
        'id,substr(title,1,500) title,substr(url,1,4000) url,status,substr(error,1,4000) error,updated_at'
        if section == 'pages'
        else 'id,kind,level,substr(message,1,8000) message,created_at'
    )
    search = "COALESCE(title,'')||' '||url" if section == 'pages' else "kind||' '||message"
    order = 'updated_at' if section == 'pages' else 'created_at'
    params = [project_id, q]
    clause = f' FROM {table} WHERE project_id=? AND instr(lower({search}),lower(?))>0'
    with connect(path) as db:
        if not db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
            return dict(items=[], total=0, page=1, page_size=page_size)
        total = db.execute('SELECT COUNT(*)' + clause, params).fetchone()[0]
        page = min(max(1, int(page)), max(1, (total + page_size - 1) // page_size))
        items = [
            dict(r)
            for r in db.execute(
                'SELECT ' + fields + clause + f' ORDER BY {order} DESC,id LIMIT ? OFFSET ?',
                params + [page_size, (page - 1) * page_size],
            )
        ]
    return dict(items=items, total=total, page=page, page_size=page_size)


def report_preview(root, db_path, kind, identity, report_id, page=1, page_size=20):
    """Read only saved prose through a blob stream; omit the large audit/source pool."""
    import ijson

    path, project_id = task_database(root, db_path, kind, identity)
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 50))
    start = (page - 1) * page_size
    items = []
    stack = []
    count = 0
    title = ''
    summary = ''
    summary_truncated = False
    with connect(path) as db:
        row = db.execute(
            'SELECT rowid FROM research_reports WHERE id=? AND project_id=?', (report_id, project_id)
        ).fetchone()
        if not row:
            raise LookupError('报告不存在或不属于该研究')
        with db.blobopen('research_reports', 'report_json', row[0], readonly=True) as blob:
            for prefix, event, value in ijson.parse(blob):
                if prefix == 'title' and event == 'string':
                    title = value[:1000]
                if prefix == 'summary' and event == 'string':
                    summary = value[:16000]
                    summary_truncated = len(value) > 16000
                if not prefix.startswith('tree'):
                    continue
                if event == 'start_map' and (prefix == 'tree' or prefix.endswith('.children.item')):
                    node = {'index': count, 'depth': len(stack), 'label': '', 'note': '', 'truncated': False}
                    count += 1
                    stack.append((prefix, node))
                    if start <= node['index'] < start + page_size:
                        items.append(node)
                elif stack:
                    node_prefix, node = stack[-1]
                    if event == 'end_map' and prefix == node_prefix:
                        stack.pop()
                    elif (
                        event == 'string'
                        and prefix in {node_prefix + '.label', node_prefix + '.note'}
                        and start <= node['index'] < start + page_size
                    ):
                        node[prefix.rsplit('.', 1)[-1]] = value[:16000]
                        node['truncated'] |= len(value) > 16000
    return dict(
        title=title,
        summary=summary,
        summary_truncated=summary_truncated,
        items=items,
        total=count,
        page=page,
        page_size=page_size,
        notice='保存版正文预览，不重新生成或修改结论；完整引用、审查信息在原报告中。',
    )


def report_file(root, db_path, kind, identity, report_id):
    path, project_id = task_database(root, db_path, kind, identity)
    with connect(path) as db:
        if not db.execute(
            'SELECT 1 FROM research_reports WHERE id=? AND project_id=?', (report_id, project_id)
        ).fetchone():
            raise LookupError('报告不存在或不属于该研究')

    def chunks():
        with connect(path) as db:
            row = db.execute(
                'SELECT rowid FROM research_reports WHERE id=? AND project_id=?', (report_id, project_id)
            ).fetchone()
            if not row:
                raise LookupError('报告已不可读')
            with db.blobopen('research_reports', 'report_json', row[0], readonly=True) as blob:
                while block := blob.read(64 * 1024):
                    yield block

    return chunks()
