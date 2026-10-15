from __future__ import annotations

import json
import re
from typing import Any


PORTAL_TITLE_PATTERN = re.compile(r'(?:^|[-—|]\s*)(?:首页|主页|Home)(?:\s*[-—|]|$)', re.I)
INCOMPLETE_DYNAMIC_PATTERN = re.compile(r'(?:现在时间[：:]?\s*[。.]?$|将于\s*(?:后)?\s*结束)')
VISUAL_BADGE_PATTERN = re.compile(r'^(?:(?:限|兑|NEW|Image)\s*){2,}$', re.I)
UI_LINES = {
    '跳转到导航',
    '跳转到搜索',
    '导航菜单',
    '个人工具',
    '查看',
    '搜索',
    '工具',
    'Languages',
    '创建账号',
    '登录',
    '查看源代码',
    '查看历史',
    '刷新',
    '复制短链接',
}


def source_task_prompt(context: dict[str, Any]) -> str:
    """Describe the user's task, not a fixed taxonomy of page contents."""
    if context.get('page_type') == 'research':
        return ''
    reading_guide = (
        '摘要先用具体表述说明材料讲了什么及关键结论，不用‘涉及多个方面、具有重要意义’代替内容。'
        '节点标题简洁，节点说明保留理解所需的主体、动作、对象、条件与例子；'
        '按原文实际关系组织层级，同页出现或相邻排列不代表隶属、因果或共同特征。'
        '保留否定、例外、时间与适用范围，区分作者观点与事实陈述，不臆造原文没有的数字或设计意图。'
        '详略遵循用户的结构设置；短材料不为凑层级重复扩写，长材料不只剩关键词。'
    )
    task = context.get('input_task')
    if task is None:
        task = (
            'webpage'
            if context.get('page_type')
            in {'portal', 'directory', 'tutorial', 'wiki', 'social_video', 'article'}
            else 'text'
        )
    if task != 'webpage':
        return (
            '解释所给文字的具体内容、条件、例子和关系；不要求研究目标，不添加外部研究结论。' + reading_guide
        )
    linked_guide = (
        (
            '输入包含已实际读取的补充网页；以用户输入页面为中心，说明补充内容如何帮助理解它，不写成泛主题报告。'
            '每个关键补充说明标注页面编号（如[W2]）；不同页面的信息保持归属，不把子页内容说成本页原文。'
            '只有pages里的content是原始材料，linked_reading中的状态与限制不是正文证据。'
        )
        if context.get('linked_reading')
        else ''
    )
    return (
        reading_guide
        + linked_guide
        + (
            '当前任务是网页理解，不是全站探索。让读者仅看结果就理解这个页面是什么、主要用途、'
            '包含哪些重要内容或功能及入口；有连续正文时同时解释正文的具体知识、论证、条件和例子。'
            '页面用途与正文分析不是二选一，不能只返回页面类型、栏目名或抄录条目来代替解释。'
            '依据实际取得的内容组织结果，不强制固定章节；识别出的页面类型只是线索。'
            '区分当前页面内容与通向其他页面的入口：未访问的链接不等于已读子页，'
            '功能说明不等于实际操作测试，媒体标题/字幕不等于已观看或听取。'
            '只取得部分内容时说明具体限制，不能仅凭域名猜测功能。'
            '分批读取时先保留各段具体内容，最后综合才判断整体页面用途；'
            '某一批没有正文不能推断全页没有正文。结构化输入中的字段和包装不是页面论点。'
        )
    )


def deterministic_page_result(
    source_text: str, source_title: str, context: dict[str, Any]
) -> dict[str, Any] | None:
    """Build evidence-preserving results for page types where free-form generation lowers quality."""
    page_type = str(context.get('page_type') or '')
    if page_type == 'directory' and str(context.get('extraction') or '').startswith('full-page-fallback'):
        return None
    if page_type in {'portal', 'directory', 'tutorial'}:
        try:
            source = json.loads(source_text)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(source, dict) or not isinstance(source.get('sections'), list):
            return None
        title = source_title.strip() or '页面内容概览'
        branches: list[dict[str, Any]] = []
        examples: list[str] = []
        for section in source['sections']:
            if not isinstance(section, dict):
                continue
            heading = str(section.get('heading') or '未命名区块').strip()
            raw_items = [str(item) for item in (section.get('items') or []) if str(item).strip()]
            code_items = [
                item
                for item in raw_items
                if len(re.findall(r'<[^>]+>', item)) >= 3 or re.search(r'<?!?DOCTYPE\s+.+PUBLIC', item, re.I)
            ]
            items = [item for item in raw_items if item not in code_items]
            if not items:
                if not code_items:
                    continue
            examples.extend(items[:2])
            children = [{'label': item[:60], 'note': item, 'children': []} for item in items]
            if code_items:
                children.extend(
                    {'label': f'代码示例 {index}', 'note': code, 'children': []}
                    for index, code in enumerate(code_items, 1)
                )
            branches.append(
                {
                    'label': heading,
                    'note': f'此标题下保留 {len(items)} 项文本和 {len(code_items)} 项代码候选；请展开查看完整内容。',
                    'children': children,
                }
            )
        kind = {'portal': '入口/首页', 'directory': '目录', 'tutorial': '分章节教学'}[page_type]
        headings = [branch['label'] for branch in branches]
        if page_type == 'tutorial':
            summary = (
                f'这是一篇分章节教程，内容依照原网页章节组织。主要讲解：{"、".join(headings[:12]) or "未识别"}。'
                '思维导图保留各章节中的概念、规则和示例，可逐章展开查看。'
            )
        else:
            summary = f'这是一个{kind}页面，以下按网页真实栏目整理可见内容。主要区块为：{"、".join(headings[:8]) or "未识别"}。'
        if examples and page_type != 'tutorial':
            summary += f'其中可直接看到的条目包括：{"、".join(examples[:6])}。'
        return {
            'title': title,
            'summary': summary,
            'language': 'zh',
            'keywords': headings,
            'insights': [],
            'tree': {
                'label': title,
                'note': '按抽取文本中的标题顺序整理；标题归属和材料意义仍需核对。',
                'children': branches,
            },
            'analysis_mode': 'structured_material_not_semantic_synthesis',
            'warnings': [
                '当前为页面材料整理，不代表已完成语义分析；扁平文本中的标题归属可能与网页布局不同。'
            ],
        }
    if page_type != 'social_video':
        return None
    lines = [item.strip() for item in source_text.splitlines() if item.strip()]
    metadata = [
        item
        for item in lines
        if re.match(r'^(?:视频标题|BV号|视频时长|发布账号|发布时间戳|视频简介|字幕|分P)[：:]', item)
    ]
    grouped = [
        ('视频信息', metadata),
        ('热门评论样本', [item for item in lines if item.startswith('热门评论（')]),
        ('热门评论回复样本', [item for item in lines if item.startswith(('热门评论回复（', '评论回复（'))]),
        ('时间序列评论样本', [item for item in lines if item.startswith('时间序列评论（')]),
        ('时间序列回复样本', [item for item in lines if item.startswith('时间序列评论回复（')]),
    ]
    branches = []
    for label, items in grouped:
        if items:
            branches.append(
                {
                    'label': label,
                    'note': '以下内容按页面公开数据原样保留。',
                    'children': [
                        {
                            'label': re.sub(r'^[^：:]{1,30}[：:]', '', item).strip()[:22] or label,
                            'note': item,
                            'children': [],
                        }
                        for item in items
                    ],
                }
            )
    title = source_title.strip() or 'B站公开视频与评论样本'
    discussion = [item for label, items in grouped[1:] for item in items if '回复样本' not in label][:2]
    examples = [re.sub(r'^[^：:]{1,40}[：:]', '', item).strip() for item in discussion]
    summary = f'这是“{title}”的公开页面数据。评论部分采用有限的公开抽样，不代表全部用户或整个平台。'
    if examples:
        summary += f'样本中可直接看到的讨论包括：{"；".join(examples)}。'
    return {
        'title': title,
        'summary': summary,
        'language': 'zh',
        'keywords': ['公开评论样本', '热门评论', '时间序列评论'],
        'insights': [],
        'tree': {'label': title, 'note': '仅展示已取得的公开页面信息与评论样本。', 'children': branches},
        'warnings': [str(item) for item in (context.get('warnings') or []) if str(item).strip()],
    }


def classify_page(title: str, text: str, metrics: dict[str, Any], extraction: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    link_ratio = float(metrics.get('link_ratio') or 0)
    headings = int(metrics.get('headings') or 0)
    list_items = int(metrics.get('list_items') or 0)
    paragraphs = int(metrics.get('paragraphs') or 0)
    complete = sum(bool(re.search(r'[。！？.!?]$', line)) and len(line) >= 18 for line in lines)
    prose_ratio = round(complete / max(1, len(lines)), 3)
    short_ratio = round(sum(len(line) <= 30 for line in lines) / max(1, len(lines)), 3)
    homepage_hint = bool(PORTAL_TITLE_PATTERN.search(title)) or title.strip().startswith(('首页', '主页'))

    reasons: list[str] = []
    if homepage_hint:
        reasons.append('标题表明这是首页或主页')
    if link_ratio >= 0.4:
        reasons.append(f'链接文字占比较高（{link_ratio:.1%}）')
    if headings >= 3:
        reasons.append(f'包含 {headings} 个标题区块')
    if list_items >= 8:
        reasons.append(f'包含 {list_items} 个列表项')

    article_like = paragraphs >= 5 and link_ratio < 0.2 and len(text) >= 500
    tutorial_like = (
        article_like
        and headings >= 4
        and ('教程' in title or extraction in {'container:article-intro', 'container:article-body'})
    )
    if tutorial_like:
        kind, confidence = 'tutorial', 'high'
        reasons.append('低链接占比且包含多个连续教学章节')
    elif article_like:
        kind, confidence = 'article', 'high' if headings >= 2 else 'medium'
        reasons.append('低链接占比且包含连续正文/教学段落')
    elif homepage_hint or (link_ratio >= 0.4 and headings >= 3) or (list_items >= 12 and prose_ratio < 0.35):
        kind, confidence = 'portal', 'high' if homepage_hint and headings >= 2 else 'medium'
    elif link_ratio >= 0.72 and prose_ratio < 0.12 and paragraphs < 3:
        kind, confidence = 'directory', 'high'
    elif headings >= 2 and extraction in {'container:mw-parser-output', 'container:entry-content'}:
        kind, confidence = 'wiki', 'high'
    elif paragraphs >= 2 and prose_ratio >= 0.18:
        kind, confidence = 'article', 'high' if link_ratio < 0.25 else 'medium'
    else:
        kind, confidence = 'document', 'low'

    return {
        'kind': kind,
        'confidence': confidence,
        'reasons': reasons,
        'signals': {
            'link_ratio': link_ratio,
            'prose_ratio': prose_ratio,
            'short_line_ratio': short_ratio,
            'headings': headings,
            'list_items': list_items,
            'paragraphs': paragraphs,
        },
    }


def _repair_dynamic_line(line: str, warnings: list[str]) -> str:
    if re.search(r'现在时间[：:]?\s*[。.]?$', line):
        warnings.append('页面中的动态当前时间没有在静态 HTML 中渲染，已忽略空值。')
        return ''
    if re.search(r'将于\s*(?:后)?\s*结束', line):
        warnings.append('页面中的活动结束时间没有在静态 HTML 中渲染，已保留活动名称并标记时间缺失。')
        line = re.sub(r'将于\s*(?:后)?\s*结束[。.]?', '（结束时间未加载）', line)
    return line


def structure_for_model(
    text: str, profile: dict[str, Any], headings: list[str] | None = None
) -> tuple[str, dict[str, Any]]:
    if profile.get('kind') not in {'portal', 'directory', 'wiki', 'tutorial'}:
        return text, {'mode': 'prose', 'sections': [], 'warnings': []}

    known_headings = {item.strip() for item in (headings or []) if item.strip()}
    sections: list[dict[str, Any]] = []
    current = {'heading': '页面概览', 'items': []}
    warnings: list[str] = []
    records = []
    removed = {'ui': 0, 'visual_badges': 0, 'duplicates': 0, 'dynamic_empty': 0}

    for match in re.finditer(r'[^\n]*(?:\n|$)', text):
        raw = match.group()
        if not raw:
            continue
        line = raw.strip()
        hints = []
        if line in UI_LINES or line.startswith('检索自“'):
            hints.append('interface_text_candidate')
        if VISUAL_BADGE_PATTERN.fullmatch(line):
            hints.append('visual_badge_candidate_not_entity_names')
        if INCOMPLETE_DYNAMIC_PATTERN.search(line):
            hints.append('unresolved_dynamic_value')
            warnings.append('有动态字段在当前文本中为空；保留原串，不推测时间或缺失内容。')
        if line in known_headings:
            if current['items']:
                sections.append(current)
            current = {'heading': line, 'items': []}
            records.append(
                {
                    'start': match.start(),
                    'end': match.end(),
                    'role': 'heading',
                    'heading': line,
                    'hints': hints,
                }
            )
            continue
        records.append(
            {
                'start': match.start(),
                'end': match.end(),
                'role': 'item',
                'heading': current['heading'],
                'item_index': len(current['items']),
                'hints': hints,
            }
        )
        current['items'].append(raw)
    if current['items']:
        sections.append(current)

    sections = [section for section in sections if section['items']]
    structured = {
        'page_type': profile.get('kind'),
        'analysis_policy': (
            '这是门户或目录型页面。保留功能分类、当前状态、更新分组和条目之间的层级；'
            '不要把它写成论说文，不要丢弃链接型条目，也不要把视觉角标当作事实。'
        ),
        'sections': sections,
        'extraction_warnings': list(dict.fromkeys(warnings)),
    }
    report = {
        'mode': 'structured-sections',
        'sections': sections,
        'section_count': len(sections),
        'item_count': sum(len(section['items']) for section in sections),
        'warnings': structured['extraction_warnings'],
        'removed': removed,
        'line_records': records,
        'grouping_basis': 'linear_heading_order_not_verified_dom_hierarchy',
    }
    return json.dumps(structured, ensure_ascii=False), report


def validate_result_quality(
    result: dict[str, Any], source_text: str, profile: dict[str, Any]
) -> dict[str, Any]:
    labels: list[str] = []
    notes: list[str] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        labels.append(str(node.get('label') or '').strip())
        notes.append(str(node.get('note') or '').strip())
        for child in node.get('children') or []:
            walk(child)

    walk(result.get('tree'))
    normalized = [re.sub(r'\s+', '', label).casefold() for label in labels if label]
    duplicate_ratio = round(1 - len(set(normalized)) / max(1, len(normalized)), 3)
    generic_sections = sum(bool(re.fullmatch(r'内容区段\s*\d+', label)) for label in labels)
    generic_labels = sum(
        label in {'主要主题', '具体点', '关键内容', '相关信息', '核心内容', '其他'} for label in labels
    )
    vague_pattern = re.compile(
        r'^(?:为用户)?(?:提供|帮助|支持|确保|促进|提升|实现|便于|旨在).{0,36}(?:服务|支持|体验|需求|目标|资源|信息|功能)?[。.]?$'
    )
    informative_notes = [note for note in notes[1:] if len(note) >= 6]
    vague_notes = [note for note in informative_notes if vague_pattern.fullmatch(note)]
    vague_ratio = round(len(vague_notes) / max(1, len(informative_notes)), 3)
    summary = str(result.get('summary') or '').strip()
    warnings = [str(item) for item in (result.get('warnings') or []) if str(item).strip()]
    issues: list[str] = []
    if (result.get('evidence_validation') or {}).get('issues'):
        issues.append('有数字或单位表达需结合原文核对；程序未改写正文。')

    if len(summary) < 20:
        issues.append('摘要过短，无法充分说明页面内容。')
    if re.fullmatch(r'(?:跳转到导航|跳转到搜索|OPERATORS|LOGISTICS|\s)+', summary, re.I):
        issues.append('摘要主要由导航词组成。')
    if duplicate_ratio >= 0.35:
        issues.append(f'节点重复比例较高（{duplicate_ratio:.1%}）。')
    if generic_sections:
        issues.append('结果包含机械生成的“内容区段”，说明模型结构化输出已降级。')
    if generic_labels >= 2:
        issues.append('多个节点仍是“主要主题/具体点”等占位标签，无法直接理解。')
    if not labels:
        issues.append('结果没有有效节点。')
    if len(source_text) >= 800 and len(labels) < 4:
        issues.append('结果节点数量与输入信息量明显不匹配。')
    unique_units = {
        re.sub(r'\s+', '', item).casefold()
        for item in re.split(r'\n+|(?<=[。！？!?])\s*|(?<=\.)\s+', source_text)
        if len(item.strip()) >= 8
    }
    density_factor = {'concise': 0.1, 'auto': 0.15, 'detailed': 0.2, 'exhaustive': 0.3}.get(
        str(profile.get('structure_mode') or 'auto'), 0.15
    )
    expected_nodes = min(24, max(4, len(source_text) // 1200 + 2, round(len(unique_units) * density_factor)))
    if len(source_text) >= 4_000 and len(labels) < expected_nodes:
        issues.append(f'长文本仅形成 {len(labels)} 个节点，重要内容覆盖不足。')
    if vague_ratio >= 0.25 and len(vague_notes) >= 2:
        issues.append('多个节点使用抽象作用描述，缺少人物、事实、条件或结果。')

    generation_degraded = (
        (result.get('generation_validation') or {}).get('status') == 'degraded'
        or generic_sections
        or any(
            '模型' in warning and any(token in warning for token in ('非法 JSON', '保底', '输出损坏'))
            for warning in warnings
        )
    )
    status = 'degraded' if generation_degraded else ('needs_review' if issues or warnings else 'normal')
    warnings.extend(issue for issue in issues if issue not in warnings)
    result['warnings'] = list(dict.fromkeys(warnings))
    validation = {
        'status': status,
        'page_type': profile.get('kind', 'document'),
        'issues': issues,
        'metrics': {
            'nodes': len(labels),
            'duplicate_ratio': duplicate_ratio,
            'summary_characters': len(summary),
            'vague_note_ratio': vague_ratio,
            'expected_nodes_for_length': expected_nodes,
            'generic_labels': generic_labels,
        },
    }
    result['quality_validation'] = validation
    return validation
