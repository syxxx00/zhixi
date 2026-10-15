"""Read projections of saved results; never rewrite snapshots or run models.

Reader bodies keep the complete editable text and citation context. Heavy
source snapshots and generation records have separate, explicit read views.
Full reads remain available to internal consumers and lossless JSON export.
"""

import json
import sqlite3

from zhixi.reports.report_editing import editing_result


PROCESS_FIELDS = (
    'question_coverage', 'source_grounding', 'overall_report_check',
    'evidence_validation', 'delivery_repairs', 'synthesis_batches',
    'model_draft', 'research_source_selection', 'report_organization',
    'editorial_selection', 'raw_model_answer', 'processing_manifest',
)


def _object(fields, source='result_json'):
    return 'json_object(' + ','.join(
        f"'{key}',json_extract({source},'$.{key}')" for key in fields
    ) + ')'


def _remove(source, paths):
    return 'json_remove(' + source + ',' + ','.join(f"'{path}'" for path in paths) + ')'


def _reader(source):
    body = _remove(source, [
        *(f'$.{field}' for field in PROCESS_FIELDS if field not in {
            'question_coverage', 'evidence_validation',
        }),
        '$.source.material', '$.research.sources', '$.research.evidence',
        '$.research.semantic_audit', '$.research.semantic_audits',
    ])
    body = (
        f"json_set({body},'$.source.has_saved_material',"
        f"json_type({source},'$.source.material.stages')='array')"
    )
    check_source = f"json_extract({source},'$.overall_report_check')"
    check_header = (
        f"CASE WHEN json_type({source},'$.overall_report_check')='object' THEN "
        + f"json_set({_object(('status', 'scope'), check_source)},'$.fact_verification',"
          f"json(CASE json_type({source},'$.overall_report_check.fact_verification') "
          "WHEN 'false' THEN 'false' WHEN 'true' THEN 'true' ELSE 'null' END))"
        + ' ELSE NULL END'
    )
    body = f"json_set({body},'$.overall_report_check',json({check_header}))"
    sources = (
        "(SELECT json_group_array(json(json_set("
        + _remove('s.value', ['$.content', '$.extraction', '$.research_filter'])
        + ",'$.has_saved_material',json_type(s.value,'$.extraction.source_material.stages')='array'))) "
        + f"FROM json_each({source},'$.research.sources') s)"
    )
    evidence = (
        '(SELECT json_group_array(json('
        + _remove('e.value', ['$.metadata.material_use', '$.metadata.admission'])
        + f")) FROM json_each({source},'$.research.evidence') e)"
    )
    audit_source = f"json_extract({source},'$.research.semantic_audit')"
    audit_header = (
        f"CASE WHEN json_type({source},'$.research.semantic_audit')='object' THEN "
        + _object(('status', 'model', 'coverage', 'audited_claims', 'claim_count', 'model_summary'), audit_source)
        + ' ELSE NULL END'
    )
    return (
        f"CASE WHEN json_type({source},'$.research')='object' THEN "
        f"json_set({body},'$.research.sources',json({sources}),'$.research.evidence',json({evidence}),"
        f"'$.research.semantic_audit',json({audit_header}),"
        "'$.saved_result_view','reader') ELSE "
        f"json_set({body},'$.saved_result_view','reader') END"
    )


def _projection(view, source):
    if view == 'full':
        return source
    if view == 'reader':
        return _reader(source)
    if view == 'process':
        return (
            f"json_set({_object(PROCESS_FIELDS, source)},'$.research',"
            + _object(('semantic_audit', 'semantic_audits'), f"json_extract({source},'$.research')")
            + ')'
        )
    raise ValueError('未知保存结果视图')


def read_saved(
    connection, *, history_id=None, project_id=None, version=None, job_id=None,
    view='full', source_id=None,
):
    """Read exactly one saved identity, projecting in SQLite before decoding.

    The caller owns the connection/snapshot. All user-supplied IDs are bound
    parameters; SQL expressions contain only the fixed projection contract above.
"""
    if job_id is not None:
        table, column, where, params = 'job_runs', 'result_json', 'id=?', [job_id]
        identity = 'id,NULL'
    elif history_id is not None:
        table, column, where, params = 'analyses', 'result_json', 'id=?', [history_id]
        identity = 'id,NULL'
    else:
        table, column, where, params = 'research_reports', 'report_json', 'project_id=?', [project_id]
        if version is not None:
            where += ' AND version=?'
            params.append(version)
        identity = 'id,version'
    order = ' ORDER BY version DESC' if table == 'research_reports' else ''
    if view == 'material':
        # Locate within this frozen version, not the current research_pages.
        snapshot = column if source_id is None else (
            f"(SELECT s.value FROM json_each({column},'$.research.sources') s "
            "WHERE json_extract(s.value,'$.id')=? LIMIT 1)"
        )
        material_path = '$.source.material' if source_id is None else '$.extraction.source_material'
        title_path = '$.source.title' if source_id is None else '$.title'
        projected = (
            "json_object('title',json_extract(snapshot,?),"
            "'material',json_extract(snapshot,?),"
            "'manifest',json_extract(snapshot,'$.processing_manifest'))"
        )
        row = connection.execute(
            f'SELECT {identity},{projected},snapshot IS NOT NULL FROM '
            f'(SELECT *,{snapshot} AS snapshot FROM {table} WHERE {where}{order} LIMIT 1)',
            [title_path, material_path, *([source_id] if source_id is not None else []), *params],
        ).fetchone()
        if not row or not row[3]:
            return None
    else:
        row = connection.execute(
            f'SELECT {identity},{_projection(view, column)} FROM {table} WHERE {where}{order} LIMIT 1',
            params,
        ).fetchone()
        if not row:
            return None
    result = json.loads(row[2])
    if history_id is not None:
        result['history_id'] = history_id
    elif project_id is not None:
        result.update(report_id=row[0], report_version=row[1])
    return editing_result(result) if view in {'full', 'reader'} else result


def read_report(db_path, project_id, version, *, view='full', source_id=None):
    with sqlite3.connect(db_path, timeout=30) as connection:
        return read_saved(
            connection, project_id=project_id, version=version, view=view, source_id=source_id,
        )
