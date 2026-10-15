"""Compose readable prose from individually attributed model statements.

Offsets are in Unicode code points of the resulting saved prose. A classified
statement and a valid source ID are not semantic verification.
"""

from hashlib import sha256


KINDS = {
    'source_observation': '',
    'source_opinion': '',
    'inference': '',
    'research_proposal': '',
    'unknown': '',
    'retained_original': '',
}


# Shared writing instruction, not a semantic validator or a regex rewrite of
# saved prose. Attribution and offsets remain separate from reader-facing text.
READER_PROSE_POLICY = """
交付对象是不了解处理流程的读者，正文应是围绕研究问题的连贯说明，不是审核记录。
把来源性质融入句子，例如“在这段视频的评论区，有玩家提到……”或“该报告作者认为……”。主体和范围必须来自所给材料，不要自动把来源叫官方或把单个评论叫普遍共识。
已交代归属后，不逐句重复“未经核实”“非官方定论”“仅为个人观点”；只保留会改变理解的具体条件、分歧或限制。
不要输出“原文观点：”“推断：”“建议补查：”“原文暂不解释”等机械前缀。观察、观点、推断等分类放在结构字段中，不当作章节或正文标签。
不要介绍程序如何核对、编号如何匹配或提醒用户再审一遍；这些属于处理记录。未完成的处理、引用缺失等记入unresolved等诊断字段，不用空泛免责声明替代能说明的内容。
原句有价值时用必要的短引文并自然说明出处；能准确转述就正常转述，不把整条记录和上下文重复铺满正文。必要限制不能删除，也不能通过删掉标签把猜测写成事实。
仅有一个讨论区时，按该讨论区的观察写，不据此宣称整个行业的事实、市场份额、趋势或共识；无法覆盖原目标的范围在概览交代一次，不逐段复读。
captured_form是取得页面的形态提示，不是可信度评分。目录里的标题只能说明该目录列出这一说法，不能假装读过链接目标；门户页中的实际正文仍可以使用，不按网页类型整页排除。
概括不能比依据更强：作者解释、个案结果、尚待确认的关系，到章节标题与摘要仍须保留原来的主体和范围。不要把“某作者用X解释Y”压缩成“X就是Y的根本原因”；合理推断应写清连接它的实际材料与适用条件，不只在错误断言上加“可能”。
"""


def prose_completion_issue(text):
    """Flag obvious unfinished prose, not grammar, truth, or missing periods.

    Literal originals are not rewritten. This narrow signal asks the existing
    writer to finish a broken sentence, without another model-review layer.
    """
    text = text.rstrip()
    if not text:
        return ''
    if text.endswith(('“', '‘', '「', '『', '（')):
        return '正文以开启引文或括号结束，需要根据原文补成完整表述。'
    pairs = (('“', '”'), ('‘', '’'), ('「', '」'), ('『', '』'), ('（', '）'))
    if text[-1] in '+-*/=：，、' and any(text.count(a) > text.count(b) for a, b in pairs):
        return '正文在未闭合引文或括号内以连接符结束，需要依据原文完成句子。'
    return ''


def statement_bindings(text, attribution, known_ids):
    """Carry exact statement/source links, not a union of paragraph labels.

    The hash and character spans bind a receipt to this version of the prose.
    They establish addressing only, never the truth of a model's inference.
    Legacy/coarse receipts remain readable without invented sentence links.
    """
    if not isinstance(attribution, dict) or attribution.get('text_sha256') != sha256(text.encode()).hexdigest():
        return []
    bindings = []
    for item in attribution.get('statements', []):
        if not isinstance(item, dict):
            continue
        start, end = item.get('start'), item.get('end')
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                or text[start:end] != item.get('text') or not isinstance(item.get('kind'), str)):
            continue
        references = [{'evidence_id': ref['evidence_id'], 'relation': ref['relation']}
                      for ref in item.get('excerpts', [])
                      if isinstance(ref, dict) and ref.get('evidence_id') in known_ids
                      and ref.get('relation') in {'supports', 'contradicts', 'context'}]
        bindings.append({'start': start, 'end': end, 'kind': item['kind'], 'references': references,
                         **({'selected_context_incomplete': True} if item.get('selected_context_incomplete') else {})})
    return bindings


def unit_bindings(owner, field, known_ids):
    """Prefer current original-grounding receipts; editorial links are routing.

    An old nonempty receipt must not mask a valid receipt for the new version.
    Neither kind of receipt constitutes semantic validation here.
    """
    keys = ('summary_attribution', 'summary_composition_attribution') if field == 'summary' else (
        'statement_attribution', 'composition_attribution')
    for key in keys:
        bindings = statement_bindings(str(owner[field]), owner.get(key), known_ids)
        if bindings:
            return bindings
    return []


def compose_statements(raw, originals, resolve_references):
    values = raw.get('statements')
    if not isinstance(values, list) or not values or 'text' in raw or 'references' in raw:
        raise ValueError('重写须逐条提供表述及其引用，不能用整段正文与共用引用代替')
    records, parts, excerpts, seen = [], [], [], set()
    cursor = 0
    for index, item in enumerate(values):
        if not isinstance(item, dict) or set(item) != {'text', 'kind', 'references'}:
            raise ValueError('每条表述必须包含正文、性质及独立引用')
        kind, text = item['kind'], item['text']
        if (
            not isinstance(kind, str)
            or kind not in KINDS
            or not isinstance(text, str)
            or (not text.strip() and kind != 'retained_original')
        ):
            raise ValueError('表述性质或正文无效')
        refs = resolve_references({'references': item['references']}, originals)
        if kind in {'source_observation', 'source_opinion', 'inference'} and not refs:
            raise ValueError('原文观察、原文观点和推断必须分别给出依据，不可借用另一条引用')
        if kind == 'retained_original':
            if not refs:
                raise ValueError('保留原文必须选择可取回的原始记录')
            # This action publishes the exact available text, not the model's
            # attempted expansion of it or a claim of support/contradiction.
            refs = list({
                ref['evidence_id']: {**ref, 'relation': 'context', 'semantic_support': 'not_assessed'}
                for ref in refs
            }.values())
            rendered = '\n\n'.join('“' + ref['quote'] + '”' for ref in refs)
        else:
            rendered = text
        if parts:
            cursor += 2  # Exactly the separator used below, not inferred later.
        records.append(
            {
                'index': index,
                'kind': kind,
                'model_text': text,
                'text': rendered,
                'start': cursor,
                'end': cursor + len(rendered),
                'excerpts': refs,
                'classification': 'program_copied_original_not_interpreted'
                if kind == 'retained_original'
                else 'model_labeled_not_verified',
                'semantic_support': 'not_verified',
            }
        )
        parts.append(rendered)
        cursor += len(rendered)
        for ref in refs:
            key = (ref['evidence_id'], ref['relation'])
            if key not in seen:
                excerpts.append(ref)
                seen.add(key)
    prose = '\n\n'.join(parts)
    if any(item['kind'] != 'retained_original' for item in records) and prose_completion_issue(prose):
        raise ValueError('模型正文仍有明确的未完成句子，未作为完整表述采用')
    return (
        prose,
        {
            'text_sha256': sha256(prose.encode('utf-8')).hexdigest(),
            'offset_unit': 'unicode_code_points',
            'statements': records,
        },
        excerpts,
    )
