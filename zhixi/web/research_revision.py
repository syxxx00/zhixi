"""Re-synthesize frozen source evidence from a report and its linked followups."""

from copy import deepcopy
import hashlib
import json
import sqlite3

from fastapi import HTTPException


def _identity(prefix, origin):
    return (
        prefix + hashlib.sha256(json.dumps(origin, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    )


def combine_snapshots(snapshots):
    pages, evidence, overrides, origins = {}, {}, [], []
    for ref, report in snapshots:
        research = report.get('research') or {}
        sources = research.get('sources') or []
        rows = research.get('evidence') or []
        if not sources or not rows:
            raise ValueError('所选报告没有完整的原文证据与来源，不能把模型总结当补查原文')
        page_ids = {}
        for source in sources:
            old_id = str(source['id'])
            if old_id in page_ids:
                raise ValueError('报告快照包含重复来源编号')
            origin = deepcopy(source.get('origin_snapshot') or {**ref, 'source_id': old_id})
            page_id = _identity('source:', origin)
            page_ids[old_id] = page_id
            page = deepcopy(source)
            page.update(id=page_id, origin_snapshot=origin)
            # Parent IDs refer to the original snapshot; do not misbind them.
            if page.get('parent_page_id'):
                page['original_parent_page_id'] = page.pop('parent_page_id')
            if page_id in pages and pages[page_id] != page:
                raise ValueError('同一来源快照对应不同内容，需明确选择材料版本')
            pages[page_id] = page
        count_before = len(evidence)
        seen = set()
        for row in rows:
            old_id = str(row['id'])
            if old_id in seen:
                raise ValueError('报告快照包含重复证据编号')
            seen.add(old_id)
            if (
                str(row.get('page_id')) not in page_ids
                or not isinstance(row.get('quote'), str)
                or not row['quote'].strip()
            ):
                raise ValueError('补查证据缺少可定位来源或完整原文')
            origin = deepcopy(row.get('origin_snapshot') or {**ref, 'evidence_id': old_id})
            identity = _identity('evidence:', origin)
            item = deepcopy(row)
            item.update(id=identity, page_id=page_ids[str(row['page_id'])], origin_snapshot=origin)
            if identity in evidence and evidence[identity] != item:
                raise ValueError('同一证据快照对应不同原文或上下文，未自动覆盖')
            evidence[identity] = item
        for raw in research.get('duplicate_overrides') or []:
            if raw.get('left_page_id') in page_ids and raw.get('right_page_id') in page_ids:
                item = {
                    **deepcopy(raw),
                    'left_page_id': page_ids[raw['left_page_id']],
                    'right_page_id': page_ids[raw['right_page_id']],
                }
                if item not in overrides:
                    overrides.append(item)
        origins.append(
            {
                **ref,
                'title': str(report.get('title') or research.get('goal') or '研究报告'),
                'evidence_count': len(rows),
                'added_evidence_count': len(evidence) - count_before,
            }
        )
    return list(evidence.values()), pages, overrides, origins


def load_revision_inputs(db_path, project_id, version, supplements):
    if not version or not supplements:
        raise ValueError('请同时选择原报告版本和补查报告版本')
    with sqlite3.connect(db_path) as db:

        def read(pid, v):
            row = db.execute(
                'SELECT id,report_json FROM research_reports WHERE project_id=? AND version=?', (pid, v)
            ).fetchone()
            if not row:
                raise ValueError('所选报告版本不存在')
            return {'project_id': pid, 'report_id': row[0], 'report_version': v}, json.loads(row[1])

        base, original = read(project_id, version)
        snapshots = [(base, original)]
        seen = set()
        for selection in supplements:
            pid, v = selection['project_id'], selection['report_version']
            if pid == project_id or (pid, v) in seen:
                raise ValueError('不能重复选择同一补查版本或将原项目作为自身补查')
            seen.add((pid, v))
            linked = db.execute(
                'SELECT 1 FROM coverage_research_links WHERE report_id=? AND child_project_id=?',
                (base['report_id'], pid),
            ).fetchone()
            if not linked:
                raise ValueError('该补查任务未关联所选原报告版本')
            snapshots.append(read(pid, v))
    goal = (original.get('research') or {}).get('goal')
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError('原报告缺少完整研究目标，不能用补查查询替代')
    evidence, pages, overrides, origins = combine_snapshots(snapshots)
    if not any(item['added_evidence_count'] for item in origins[1:]):
        raise ValueError('所选补查原文已包含在原报告材料中，没有新增快照')
    revision = {
        'base_report_id': base['report_id'],
        'base_report_version': version,
        'input_snapshots': origins,
        'basis': 'saved_original_evidence_not_model_summaries',
        'original_goal_preserved': True,
        'source_fetches_executed': False,
    }
    return goal, evidence, pages, overrides, revision


def install_revision_routes(api, ctx):
    @api.post(
        '/api/research/{project_id}/reports/{version}/supplements/{child_id}/reports/{child_version}/incorporate',
        status_code=202,
    )
    def incorporate(
        project_id: str,
        version: int,
        child_id: str,
        child_version: int,
        selection: ctx.ResearchSynthesisRequest,
    ):
        reference = {'project_id': child_id, 'report_version': child_version}
        try:
            load_revision_inputs(ctx.DB_PATH, project_id, version, [reference])
            request = selection.model_copy(
                update={
                    'base_report_version': version,
                    'supplementary_reports': [ctx.ReportInputReference(**reference)],
                }
            )
            return ctx.enqueue_research_synthesis(project_id, request)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
