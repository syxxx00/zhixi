"""Lossless shape normalization and explicit research-generation degradation.

These helpers do not judge, repair or replace model propositions. Source excerpt
fallback is a different output mode, never a successful semantic synthesis.
"""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from zhixi.content.evidence_context import research_original_quotes
from zhixi.models.model_text_planning import split_source_chunks


def text_excerpt_fallback(source: str, title: str) -> dict[str, Any]:
    """Preserve every plain-text character, including short lines and code.

    Organizer catalogs are explicitly intermediate drafts, not source quotes.
    The full input is retained even when a structured view is also available.
    """
    try:
        structured = json.loads(source)
    except json.JSONDecodeError:
        structured = None
    basis = 'input_text_not_synthesis'
    branches = []
    if isinstance(structured, list) and all(
        isinstance(item, dict) and 'statement' in item for item in structured
    ):
        basis = 'intermediate_drafts_not_originals'
        branches = [
            {'label': str(item.get('id') or f'草稿 {index}'), 'note': str(item['statement']), 'children': []}
            for index, item in enumerate(structured, 1)
        ]
    elif isinstance(structured, dict) and isinstance(structured.get('sections'), list):
        basis = 'structured_input_not_synthesis'
        for index, section in enumerate(structured['sections'], 1):
            if not isinstance(section, dict):
                branches.append(
                    {
                        'label': f'保留区块 {index}',
                        'note': json.dumps(section, ensure_ascii=False),
                        'children': [],
                    }
                )
                continue
            items = section.get('items')
            branches.append(
                {
                    'label': str(section.get('heading') or f'区块 {index}'),
                    'note': '按输入区块保留，尚未完成归纳。',
                    'children': [
                        {
                            'label': f'条目 {item_index}',
                            'note': item if isinstance(item, str) else json.dumps(item, ensure_ascii=False),
                            'children': [],
                        }
                        for item_index, item in enumerate(items if isinstance(items, list) else [], 1)
                    ],
                }
            )
    if not branches:
        branches = [
            {'label': f'原文区段 {index}', 'note': chunk, 'children': []}
            for index, chunk in enumerate(split_source_chunks(source), 1)
        ]
    warning = '模型结构化输出修复失败；以下仅保留输入材料或中间草稿，尚未完成分析综合。'
    return {
        'title': title or '待重新分析的材料',
        'summary': warning,
        'language': 'zh',
        'keywords': [],
        'insights': [],
        'tree': {'label': title or '保留材料', 'note': warning, 'children': branches},
        'warnings': [warning],
        'fallback_input': source,
        'generation_validation': {'status': 'degraded', 'basis': basis},
    }


def research_excerpt_fallback(source: str, title: str) -> dict[str, Any]:
    """Only quote known original fields, never sentence-split the envelope JSON."""
    units = research_original_quotes(source)
    basis = 'original_quotes'
    if not units:
        try:
            catalog = json.loads(source)
        except json.JSONDecodeError:
            catalog = None
        if isinstance(catalog, list) and all(
            isinstance(item, dict) and 'statement' in item for item in catalog
        ):
            units = [str(item['statement']) for item in catalog if item['statement']]
            basis = 'intermediate_drafts_not_originals'
    if not units:
        raise ValueError(
            '研究模型 JSON 修复失败，输入不是可辨认的原文记录或事实目录；未把上下文当正文生成保底结果。'
        )
    warning = '模型 JSON 修复失败，当前仅保留摘录或中间草稿，不代表已完成研究综合。'
    return {
        'title': title or '待重新综合的研究材料',
        'summary': warning,
        'keywords': [],
        'insights': [],
        'tree': {
            'label': title or '研究材料',
            'note': warning,
            'children': [
                {'label': f'保留材料 {index}', 'note': unit, 'children': []}
                for index, unit in enumerate(units, 1)
            ],
        },
        'warnings': [warning],
        'generation_validation': {'status': 'degraded', 'basis': basis, 'excerpt_count': len(units)},
    }


def normalize_research_result(raw: dict[str, Any], source_title: str = '') -> dict[str, Any]:
    """Normalize the schema without shortening prose, inventing or collapsing nodes.

    Iterative traversal avoids imposing a hidden depth limit on a valid tree.
    Invalid/cyclic shapes fail visibly instead of silently losing their content.
    """
    result = {key: deepcopy(value) for key, value in raw.items() if key != 'tree'}
    source_tree = raw.get('tree')
    if not isinstance(source_tree, dict):
        raise ValueError('研究结果缺少有效 tree，无法保留完整结构，请重试综合。')
    tree: dict[str, Any] = {}
    stack = [(source_tree, tree)]
    visited = set()
    facts = []
    while stack:
        source, target = stack.pop()
        if id(source) in visited:
            raise ValueError('研究结果包含循环或重复节点对象，无法无损规范化。')
        visited.add(id(source))
        children = source.get('children', [])
        if children is None:
            children = []
        if not isinstance(children, list) or any(not isinstance(child, dict) for child in children):
            raise ValueError('研究结果 children 不是节点列表，未静默丢弃异常内容。')
        target.update({key: deepcopy(value) for key, value in source.items() if key != 'children'})
        target['label'] = str(source.get('label') or '未命名')
        target['note'] = str(source.get('note') or '')
        target['children'] = [{} for _ in children]
        if not children and target['note']:
            facts.append({'label': target['label'], 'value': target['note']})
        stack.extend(reversed(list(zip(children, target['children']))))
    result['tree'] = tree
    result['title'] = str(raw.get('title') or source_title or tree['label'])
    result['summary'] = str(raw.get('summary') or '')
    result['key_facts'] = facts
    result.setdefault('keywords', [])
    result.setdefault('insights', [])
    result.setdefault('warnings', [])
    result['normalization'] = {'schema_version': 1, 'policy': 'preserve_model_prose_and_structure'}
    return result
