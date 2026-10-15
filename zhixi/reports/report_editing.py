"""Validate editable report fields without changing their text or hierarchy.

Only author-editable fields survive. Browser layout and derived support verdicts
must not become trusted persisted edits. Invalid shapes fail before any write.
"""

import hashlib
import json


def edit_revision(report):
    """Identify the displayed editable content, not audit/layout metadata.

    The token is computed on reads; no migration or stored revision is needed.
    Include the report identity so a same-text newer version is still distinct.
    """
    tree = report.get('tree', {})
    try:
        tree = editable_tree(tree)
    except ValueError:
        # Reading legacy data must not apply the stricter write contract.
        # Hash its original shape; a new edit is still validated before saving.
        pass
    baseline = {
        'title': report.get('title', ''),
        'summary': report.get('summary', ''),
        'keywords': report.get('keywords', []),
        'tree': tree,
        'report_id': report.get('report_id'),
        'report_version': report.get('report_version'),
    }
    encoded = json.dumps(baseline, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def editing_result(report):
    """Expose an edit baseline without persisting transport-only metadata."""
    return {**report, 'edit_revision': edit_revision(report)}


def same_legacy_report(first, second):
    """Compare the entire stored payload except version/audit transport fields.

    Only for unversioned history compatibility; the caller must also verify
    the project and latest report identity in the same write transaction.
    An editable-field hash alone could hide changed source snapshots.
    """
    def content(report):
        data = {key: value for key, value in report.items() if key not in {
            'report_id', 'report_version', 'edit_revision', 'saved_result_view',
        }}
        if isinstance(data.get('research'), dict):
            data['research'] = {key: value for key, value in data['research'].items()
                                if key not in {'semantic_audit', 'semantic_audits'}}
        return data

    return content(first) == content(second)


def editable_tree(node):
    if not isinstance(node, dict):
        raise ValueError('报告节点必须是对象，未保存异常结构。')
    result = {}
    pending = [(node, result)]
    visited = set()
    while pending:
        source, target = pending.pop()
        if id(source) in visited:
            raise ValueError('报告包含循环或重复节点对象，未保存异常结构。')
        visited.add(id(source))
        children = source.get('children', [])
        if not isinstance(children, list) or any(not isinstance(child, dict) for child in children):
            raise ValueError('报告 children 必须是节点列表，未丢弃内容或保存异常结构。')
        for field, default in (('id', ''), ('label', '未命名'), ('note', '')):
            value = source.get(field, default)
            if not isinstance(value, str):
                raise ValueError(f'报告节点 {field} 必须是文本，未自动转换或保存。')
            target[field] = value
        citations = source.get('citations', [])
        if not isinstance(citations, list) or any(not isinstance(item, str) for item in citations):
            raise ValueError('报告 citations 必须是引用编号列表，未丢弃引用或保存。')
        target['citations'] = list(citations)
        target['children'] = [{} for _ in children]
        pending.extend(reversed(list(zip(children, target['children']))))
    return result
