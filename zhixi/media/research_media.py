"""Bridge frozen page metadata to atomic report attachments; never fetch media."""

from copy import deepcopy
from hashlib import sha256
from datetime import datetime, timezone
import json

from zhixi.media.media_evidence import discover_media_candidates, normalize_media


def checked_media_discovery(url, html, validate_url):
    """Candidates are metadata, not observations or permission to embed images."""
    try:
        found = discover_media_candidates(url, html)
    except ValueError as error:
        return {'items': [], 'coverage': {'status': 'unsupported_source', 'reason': str(error)}}
    checked = {}
    accepted, rejected = [], []
    for item in found['items']:
        try:
            for address in (item['source_url'], item['canonical_media_url']):
                if address not in checked:
                    try:
                        validate_url(address)
                        checked[address] = ''
                    except ValueError as error:
                        checked[address] = str(error)
                if checked[address]:
                    raise ValueError(checked[address])
            accepted.append(
                {
                    **item,
                    'captured_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    'retrieval_path': 'page_html_metadata_not_media_bytes',
                }
            )
        except ValueError as error:
            rejected.append({'url': item['canonical_media_url'], 'reason': str(error)})
    return {
        'items': accepted,
        'rejected': rejected,
        'coverage': {
            **found['coverage'],
            'public_url_accepted': len(accepted),
            'public_url_rejected': len(rejected),
            'media_fetched': False,
            'media_understood': False,
        },
    }


def _hash(record):
    return sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def publish_media_snapshot(db, project_id, saved, *, previous_report=None):
    """Use caller's transaction. Preserve exact records and old anchors; no inference.

    Current page candidates come from saved source extraction, never mutable live
    research_pages. Frozen existing report attachments are inherited only within
    the same project, with unchanged paragraph hash required for inline placement.
    """
    from zhixi.media.media_evidence import ensure_media_tables
    from zhixi.reports.report_questions import report_anchor

    ensure_media_tables(db)
    attachments = []
    inherited = set()
    if previous_report:
        old_id, old_version = previous_report
        row = db.execute(
            'SELECT id FROM research_reports WHERE project_id=? AND version=?', (project_id, old_version)
        ).fetchone()
        if not row or row[0] != old_id:
            raise ValueError('旧媒体报告版本不匹配')
        for binding_row in db.execute(
            'SELECT binding_json FROM report_media_bindings WHERE project_id=? AND report_version=?',
            (project_id, old_version),
        ):
            binding = json.loads(binding_row[0])
            row = db.execute(
                'SELECT record_json FROM media_evidence_records WHERE project_id=? AND evidence_id=? AND version=?',
                (project_id, binding['evidence_id'], binding['media_version']),
            ).fetchone()
            if not row:
                raise ValueError('旧报告媒体记录缺失，未保存不完整新版')
            record = json.loads(row[0])
            if binding['report_id'] != old_id or binding['record_hash'] != record['record_hash']:
                raise ValueError('旧报告媒体版本哈希不匹配')
            if _hash({k: v for k, v in record.items() if k != 'record_hash'}) != record['record_hash']:
                raise ValueError('旧媒体记录内容与版本哈希不一致')
            anchor, text_hash = binding['anchor_id'], binding['anchor_text_hash']
            lost_anchor = False
            if anchor:
                try:
                    text = str(report_anchor(saved, anchor).get('note') or '')
                    lost_anchor = sha256(text.encode()).hexdigest() != text_hash
                except ValueError:
                    lost_anchor = True
            attachments.append(
                {
                    'record': record,
                    'anchor_id': '' if lost_anchor else anchor,
                    'anchor_text_hash': '' if lost_anchor else text_hash,
                    'old_anchor_unverified': lost_anchor,
                }
            )
            inherited.add(record['evidence_id'])
    for source in saved.get('research', {}).get('sources', []):
        discovery = source.get('extraction', {}).get('media_discovery', {})
        for raw in discovery.get('items', []):
            record = normalize_media(raw)
            # Only metadata produced by the HTML pipeline belongs in this path.
            if record['provider'] == 'local' or record['understanding_status'] != 'metadata_only':
                raise ValueError('页面媒体候选不能冒充本地文件或实际内容观察')
            identity = _hash(record)
            record.update(
                evidence_id='html_' + identity,
                version=1,
                recorded_at=record.get('captured_at') or source.get('created_at') or '',
            )
            record['record_hash'] = _hash(record)
            if record['evidence_id'] in inherited:
                continue
            inherited.add(record['evidence_id'])
            attachments.append(
                {'record': record, 'anchor_id': '', 'anchor_text_hash': '', 'old_anchor_unverified': False}
            )
    manifest = []
    for item in attachments:
        record = item['record']
        eid, version = record['evidence_id'], record['version']
        previous = db.execute(
            'SELECT record_json FROM media_evidence_records WHERE project_id=? AND evidence_id=? AND version=?',
            (project_id, eid, version),
        ).fetchone()
        if previous and json.loads(previous[0]) != record:
            raise ValueError('媒体快照ID内容冲突')
        db.execute(
            'INSERT OR IGNORE INTO media_evidence_records VALUES (?,?,?,?)',
            (project_id, eid, version, json.dumps(record, ensure_ascii=False)),
        )
        binding = {
            'evidence_id': eid,
            'media_version': version,
            'record_hash': record['record_hash'],
            'anchor_id': item['anchor_id'],
            'anchor_text_hash': item['anchor_text_hash'],
            'report_id': saved['report_id'],
        }
        db.execute(
            'INSERT INTO report_media_bindings VALUES (?,?,?,?,?)',
            (project_id, saved['report_version'], eid, version, json.dumps(binding)),
        )
        manifest.append(
            {**deepcopy(record), 'binding': binding, 'old_anchor_unverified': item['old_anchor_unverified']}
        )
    if attachments or any(
        'media_discovery' in s.get('extraction', {}) for s in saved.get('research', {}).get('sources', [])
    ):
        saved.setdefault('research', {})['media_snapshot'] = {
            'schema_version': 'MediaEvidence/v1',
            'items': manifest,
            'basis': 'saved_page_metadata_and_exact_report_bindings_not_model_claims',
            'unverified_anchor_count': sum(i['old_anchor_unverified'] for i in attachments),
            'source_coverage': [
                {'source_id': s.get('id'), **deepcopy(s['extraction']['media_discovery'].get('coverage', {}))}
                for s in saved.get('research', {}).get('sources', [])
                if 'media_discovery' in s.get('extraction', {})
            ],
        }
