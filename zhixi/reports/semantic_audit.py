from __future__ import annotations

import json
from typing import Any

from zhixi.research.research_analysis import similarity
from zhixi.reports.audit_decisions import atomic_spans


ISSUE_KINDS = {
    'unsupported_claim',
    'overstatement',
    'missing_context',
    'conflict_ignored',
    'ambiguous_attribution',
    'uncited_claim',
    'other',
}
SEVERITIES = {'low', 'medium', 'high'}


def collect_report_claims(result: dict[str, Any], limit: int | None = None) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []

    def add(claim_id, kind, text, heading='', citations=None):
        spans = atomic_spans(text)
        for index, span in enumerate(spans):
            if limit is not None and len(claims) >= limit:
                return
            claims.append(
                {
                    'id': claim_id if len(spans) == 1 else f'{claim_id}:sentence:{index}',
                    'parent_claim_id': claim_id,
                    'kind': kind,
                    'text': span['text'],
                    'heading': heading,
                    'paragraph': text,
                    'span': {'start': span['start'], 'end': span['end']},
                    'citations': list(dict.fromkeys(citations or [])),
                }
            )

    summary = str(result.get('summary') or '').strip()
    if summary:
        add('summary', 'summary', summary)
    for index, insight in enumerate(result.get('insights') or []):
        if not isinstance(insight, dict):
            continue
        text = str(insight.get('detail') or '').strip()
        if text:
            add(
                f'insight:{index}', 'insight', text, str(insight.get('title') or ''), insight.get('citations')
            )

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict) or (limit is not None and len(claims) >= limit):
            return
        label = str(node.get('label') or '').strip()
        note = str(node.get('note') or '').strip()
        if note:
            citations = [str(item) for item in (node.get('citations') or []) if item]
            add(f'node:{path}', 'tree_node', note, label, citations)
        for index, child in enumerate(node.get('children') or []):
            walk(child, f'{path}.{index}')

    walk(result.get('tree'), '0')
    return claims if limit is None else claims[:limit]


def audit_model_payload(result: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    claims = collect_report_claims(result)
    evidence_rows = [
        {
            'id': str(item.get('id') or ''),
            'page_id': str(item.get('page_id') or ''),
            'type': str(item.get('type') or item.get('evidence_type') or 'fact'),
            'stance': str(item.get('stance') or 'neutral'),
            'claim': str(item.get('claim') or ''),
            'quote': str(item.get('quote') or ''),
            'metadata': item.get('metadata') or {},
        }
        for item in evidence
        if item.get('id')
    ]
    for claim in claims:
        cited = [item for item in claim['citations'] if any(row['id'] == item for row in evidence_rows)]
        ranked = sorted(
            evidence_rows,
            key=lambda row: similarity(claim['text'], f'{row["claim"]} {row["quote"]}'),
            reverse=True,
        )
        claim['candidate_evidence_ids'] = list(dict.fromkeys([*cited, *(row['id'] for row in ranked[:8])]))
    return {
        'task': 'semantic_audit',
        'claims': claims,
        'evidence': evidence_rows,
    }


def audit_batches(
    payload: dict[str, Any], size: int = 1, character_budget: int = 18000
) -> list[dict[str, Any]]:
    claims = payload.get('claims') or []
    evidence = {item['id']: item for item in (payload.get('evidence') or []) if item.get('id')}
    batches = []

    def pack(selected_claims):
        selected_ids = list(
            dict.fromkeys(
                evidence_id
                for claim in selected_claims
                for evidence_id in (claim.get('candidate_evidence_ids') or claim.get('citations') or [])
                if evidence_id in evidence
            )
        )
        return {'claims': selected_claims, 'evidence': [evidence[item] for item in selected_ids]}

    pending = []
    for claim in claims:
        candidate = pack([*pending, claim])
        if pending and (
            len(pending) >= max(1, size) or len(json.dumps(candidate, ensure_ascii=False)) > character_budget
        ):
            batches.append(pack(pending))
            pending = []
        pending.append(claim)
    if pending:
        batches.append(pack(pending))
    # A single oversized claim is never silently cropped. The caller receives
    # the full block and can expose the over-budget condition or reject it.
    for batch in batches:
        batch['characters'] = len(json.dumps(batch, ensure_ascii=False))
        batch['over_budget'] = batch['characters'] > character_budget
    return batches


def normalize_semantic_audit(
    raw: Any, claims: list[dict[str, Any]], evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    claims_by_id = {item['id']: item for item in claims}
    evidence_ids = {str(item.get('id')) for item in evidence if item.get('id')}
    issues = []
    seen = set()
    for item in raw.get('issues') or []:
        if not isinstance(item, dict):
            continue
        claim_id = str(item.get('claim_id') or '')
        if claim_id not in claims_by_id:
            continue
        kind = str(item.get('kind') or 'other')
        kind = kind if kind in ISSUE_KINDS else 'other'
        key = (claim_id, kind)
        if key in seen:
            continue
        seen.add(key)
        severity = str(item.get('severity') or 'medium')
        valid_ids = list(
            dict.fromkeys(
                str(value) for value in (item.get('evidence_ids') or []) if str(value) in evidence_ids
            )
        )[:20]
        explanation = str(item.get('explanation') or '').strip()[:1000]
        severity = severity if severity in SEVERITIES else 'medium'
        issues.append(
            {
                'claim_id': claim_id,
                'claim_text': claims_by_id[claim_id]['text'],
                'kind': kind,
                'severity': severity,
                'evidence_ids': valid_ids,
                'explanation': explanation[:1000],
                'suggested_revision': str(item.get('suggested_revision') or '').strip()[:1000],
            }
        )

    model_audited_ids = {item['claim_id'] for item in issues}
    for claim in claims:
        if claim['kind'] == 'tree_node' and not claim['citations']:
            key = (claim['id'], 'uncited_claim')
            if key not in seen:
                seen.add(key)
                issues.append(
                    {
                        'claim_id': claim['id'],
                        'claim_text': claim['text'],
                        'kind': 'uncited_claim',
                        'severity': 'medium',
                        'evidence_ids': [],
                        'explanation': '该报告节点没有绑定任何可回查证据。',
                        'suggested_revision': '补充证据引用或删除该节点。',
                    }
                )

    confirmed = []
    for item in raw.get('confirmed') or []:
        if not isinstance(item, dict):
            continue
        claim_id = str(item.get('claim_id') or '')
        if claim_id not in claims_by_id or claim_id in confirmed:
            continue
        valid_support = {
            str(value) for value in (item.get('evidence_ids') or []) if str(value) in evidence_ids
        }
        if not valid_support or claim_id in model_audited_ids:
            continue
        confirmed.append(claim_id)
    audited_ids = set(confirmed) | model_audited_ids
    coverage = len(audited_ids) / len(claims) if claims else 1.0
    material = [item for item in issues if item['severity'] in {'medium', 'high'}]
    status = 'needs_review' if material else ('passed' if coverage == 1 else 'incomplete')
    return {
        'status': status,
        'claim_count': len(claims),
        'audited_claims': len(audited_ids),
        'coverage': round(coverage, 4),
        'issues': issues,
        'confirmed_claim_ids': confirmed,
        'unreviewed_claim_ids': [item['id'] for item in claims if item['id'] not in audited_ids],
        'decisions': raw.get('decisions') or [],
        'protocol': raw.get('protocol', 'legacy'),
        'scope': 'source_support_not_external_truth',
        'model_summary': str(raw.get('summary') or '').strip()[:1500],
    }
