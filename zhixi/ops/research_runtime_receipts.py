"""Bounded legacy receipt metadata shared by workflow and OPS readers.

No writes or large report loads: only existing small, jobless publication
notices are classified. The pure runtime projector consumes their outcomes.
"""

import json


def publication_index(db, project_id=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='report_publications'").fetchone():
        return []
    rows = db.execute(
        'SELECT p.job_id,p.project_id,p.report_id,r.rowid AS report_rowid '
        'FROM report_publications p JOIN research_reports r ON r.id=p.report_id '
        'AND r.project_id=p.project_id WHERE NOT EXISTS '
        '(SELECT 1 FROM job_runs j WHERE j.id=p.job_id)'
        + (' AND p.project_id=?' if project_id is not None else ''),
        (project_id,) if project_id is not None else (),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item['outcome'] = 'saved'
        with db.blobopen('research_reports', 'report_json', item.pop('report_rowid'), readonly=True) as blob:
            if len(blob) <= 65536:
                try:
                    report = json.loads(blob.read())
                    source, research = report.get('source', {}), report.get('research', {})
                    if (
                        source.get('type') == 'research'
                        and source.get('evidence') == 0
                        and research.get('evidence') == []
                        and research.get('sources') == []
                    ):
                        item['outcome'] = 'no_evidence'
                except (ValueError, AttributeError):
                    pass
        items.append(item)
    return items
