"""Statement-level editorial lineage, not source verification.

The editor selects existing prose. The program follows those selections to
their original source addresses; it does not infer support from shared labels.
Intermediate reductions have no reader-facing path, so carry their selected
dependencies forward instead of making them new evidence.
"""

from copy import deepcopy
from hashlib import sha256

from zhixi.reports.grounding_statements import KINDS


PROTOCOL = 'statement_lineage_v2'
PHASE_PROTOCOL = PROTOCOL.removeprefix('statement_')
POLICY = """
每条概括写在statements中：text为自然、连贯的说明，kind保留观察、来源观点或推断的区别；references逐条选择本次paragraphs的paragraph_index及其statement_bindings_not_verified的statement_index，均为0起始下标。
需要在概括中保住已经选定的原表达时，可用kind=retained_original、text=""，references只选择kind=retained_original且非partial_statement的表述；程序原样采用，不再要求你抄写或解释。不能将普通草稿、观点或整段标成原文。正常解释仍写在相邻statements中，不强制引用、不把原句集合代替回答；只选对本次概括有实际用途的内容。
只有该段没有逐表述绑定时才可用statement_index=-1，表示读取整段但缺少句级对应。不能把某条观点借给同段另一条事实，也不能把整批来源都挂在每句上。
同一条解释确实组合多处内容时可选多个references，但关联本身也须在正文中成立；有来源编号并不证明新的因果或共性。保留作者、对象、时期和适用条件，不把来源的解释升级成事实。
partial_statement是运输分片，不表示完整的前提；selected_context_incomplete表示前层采用过分片，不能凭概括补出未见条件。不要把这些处理字段写成正文免责声明，实际条件仍须从正文保留、最终回到完整原文核对。
references是编辑采用关系，不是核验通过；它让后续概括及修订能找回所用正文与原文。不要在text里写编号、分类标签或核对流程。
只返回JSON {"title":"中性具体主题","statements":[{"text":"连贯具体的概括，原样采用时为空字符串","kind":"source_observation|source_opinion|inference|unknown|retained_original","references":[{"paragraph_index":0,"statement_index":0}]}]}。
"""


def schema():
    return {'type': 'object', 'additionalProperties': False, 'required': ['title', 'statements'],
            'properties': {
                'title': {'type': 'string', 'minLength': 1},
                'statements': {'type': 'array', 'minItems': 1, 'items': {
                    'type': 'object', 'additionalProperties': False,
                    'required': ['text', 'kind', 'references'],
                    'properties': {
                        'text': {'type': 'string'},
                        'kind': {'type': 'string', 'enum': [
                            'source_observation', 'source_opinion', 'inference', 'unknown', 'retained_original']},
                        'references': {'type': 'array', 'minItems': 1, 'items': {
                            'type': 'object', 'additionalProperties': False,
                            'required': ['paragraph_index', 'statement_index'],
                            'properties': {'paragraph_index': {'type': 'integer', 'minimum': 0},
                                           'statement_index': {'type': 'integer', 'minimum': -1}},
                        }},
                    },
                }},
            }}


def current_receipt(text, receipt):
    """Reject old-version routing just as old source spans are rejected."""
    if (isinstance(receipt, dict) and receipt.get('protocol') in (PROTOCOL, 'statement_lineage_v1')
            and receipt.get('text_sha256') == sha256(text.encode()).hexdigest()):
        return receipt
    return None


def _selected_original(item, binding, left, right):
    """Authenticate an existing copy operation, not a model's kind label.

    Quotes remain in the program-side receipt, not another corpus sent to the
    editor. A fragment can carry a whole literal; a partial literal cannot be
    adopted as the full original. This does not decide semantic adequacy.
    """
    if not binding or binding.get('kind') != 'retained_original' or binding.get('partial_statement'):
        raise ValueError('原样采用须选择完整的已保留原文表述')
    fields = item.get('reviewed_fields', {})
    for key in ('statement_attribution', 'composition_attribution'):
        receipt = fields.get(key)
        if not isinstance(receipt, dict) or receipt.get('text_sha256') != sha256(item['text'].encode()).hexdigest():
            continue
        records = receipt.get('statements')
        if not isinstance(records, list):
            continue
        for record in records:
            if (not isinstance(record, dict) or record.get('start') != left or record.get('end') != right
                    or record.get('text') != item['text'][left:right]
                    or record.get('kind') != 'retained_original'
                    or record.get('classification') != 'program_copied_original_not_interpreted'):
                continue
            refs = record.get('excerpts') or []
            if (isinstance(refs, list) and refs and all(isinstance(ref, dict) and ref.get('relation') == 'context'
                             and isinstance(ref.get('evidence_id'), str)
                             and isinstance(ref.get('quote'), str) and ref['quote'] for ref in refs)
                    and {ref.get('evidence_id') for ref in refs} == {
                        ref.get('evidence_id') for ref in binding.get('references', [])}
                    and record['text'] == '\n\n'.join('“' + ref['quote'] + '”' for ref in refs)):
                return record
    raise ValueError('所选表述没有当前有效的原文复制记录，未将模型文字当原文')


def dependency_paths(units):
    """Read only current, addressable selections; unknown is not independence.

    Missing or stale legacy receipts stay outside this map. Callers retain
    their conservative legacy behavior rather than inventing sibling links.
    """
    units = list(units)
    texts = {path: str(owner[field]) for path, _, owner, field in units}
    result = {}
    for path, _, owner, field in units:
        key = 'summary_composition_attribution' if field == 'summary' else 'composition_attribution'
        receipt = current_receipt(texts[path], owner.get(key))
        if not receipt or not receipt.get('statements'):
            continue
        selected, valid = set(), True
        for record in receipt['statements']:
            start, end = record.get('start'), record.get('end')
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(texts[path])
                    or texts[path][start:end] != record.get('text') or not record.get('dependencies')):
                valid = False
                break
            for dependency in record['dependencies']:
                target = dependency.get('path')
                text = texts.get(target)
                left, right = dependency.get('start'), dependency.get('end')
                if (text is None or target == path
                        or dependency.get('text_sha256') != sha256(text.encode()).hexdigest()
                        or type(left) is not int or type(right) is not int or not 0 <= left < right <= len(text)):
                    valid = False
                    break
                selected.add(target)
        if valid:
            result[path] = selected
    return result


def render(raw, payload, catalog):
    """Resolve this call's indices, including actual fragments, atomically.

    Legacy prose remains readable but gets no invented statement receipt.
    The production schema and cache phase request the new protocol explicitly.
    """
    if 'statements' not in raw:
        return raw
    if not isinstance(raw.get('title'), str) or not raw['title'].strip():
        raise ValueError('概括缺少主题')
    values = raw['statements']
    if not isinstance(values, list) or not values:
        raise ValueError('概括缺少逐表述内容')
    records, parts, cursor = [], [], 0
    for statement in values:
        if not isinstance(statement, dict) or set(statement) != {'text', 'kind', 'references'}:
            raise ValueError('概括须分别返回正文、性质与采用关系')
        text, kind, references = statement['text'], statement['kind'], statement['references']
        if (not isinstance(text, str) or not isinstance(kind, str)
                or kind not in KINDS or kind == 'research_proposal'
                or (text != '' if kind == 'retained_original' else not text.strip())
                or not isinstance(references, list) or not references):
            raise ValueError('概括表述或采用关系无效')
        excerpts, dependencies, inputs, originals, incomplete = {}, {}, [], {}, False
        for ref in references:
            if not isinstance(ref, dict) or set(ref) != {'paragraph_index', 'statement_index'}:
                raise ValueError('概括采用关系必须定位本次输入')
            pi, si = ref['paragraph_index'], ref['statement_index']
            if type(pi) is not int or not 0 <= pi < len(payload['paragraphs']) or type(si) is not int:
                raise ValueError('概括采用了不存在的正文')
            paragraph = payload['paragraphs'][pi]
            bindings = paragraph.get('statement_bindings_not_verified') or []
            if not ((bindings and 0 <= si < len(bindings)) or (not bindings and si == -1)):
                raise ValueError('概括采用了不存在的表述或用整段代替已有句级绑定')
            item = catalog[paragraph['prose_id']]
            offset = paragraph.get('fragment_start', 0)
            if paragraph['text'] != item['text'][offset:offset + len(paragraph['text'])]:
                raise ValueError('概括输入版本不匹配')
            binding = bindings[si] if bindings else None
            incomplete = incomplete or bool(binding and (
                binding.get('partial_statement') or binding.get('selected_context_incomplete'))
            ) or offset > 0 or len(paragraph['text']) != len(item['text'])
            left, right = (offset + binding['start'], offset + binding['end']) if binding else (
                offset, offset + len(paragraph['text']))
            refs = binding['references'] if binding else [
                {'evidence_id': str(identity), 'relation': 'context'} for identity in item.get('citations', [])]
            if kind == 'retained_original':
                original = _selected_original(item, binding, left, right)
                originals[(paragraph['prose_id'], left, right)] = original
                refs = original['excerpts']
            for source in refs:
                excerpts[(source['evidence_id'], source['relation'])] = deepcopy(source)
            if item.get('report_path'):
                dependency = {'path': item['report_path'], 'text_sha256': sha256(item['text'].encode()).hexdigest(),
                              'start': left, 'end': right}
                dependencies[(dependency['path'], left, right)] = dependency
            else:
                receipt = current_receipt(item['text'], item.get('reviewed_fields', {}).get('composition_attribution'))
                for record in (receipt or {}).get('statements', []):
                    if record['start'] < right and left < record['end']:
                        for dependency in record.get('dependencies', []):
                            dependencies[(dependency['path'], dependency['start'], dependency['end'])] = deepcopy(dependency)
            inputs.append({**ref, 'fragment_start': offset, 'partial_statement': bool(
                binding and binding.get('partial_statement')),
                'mapping': 'selected_statement' if binding else 'whole_paragraph_without_statement_mapping'})
        if kind == 'retained_original':
            text = '\n\n'.join(record['text'] for record in originals.values())
            # Keep repeated literal selections addressable too. Deduplicating
            # their source IDs must not erase a copied piece's quote record.
            selected_excerpts = [deepcopy(ref) for record in originals.values() for ref in record['excerpts']]
        else:
            selected_excerpts = list(excerpts.values())
        if parts:
            cursor += 2
        records.append({'text': text, 'kind': kind, 'start': cursor, 'end': cursor + len(text),
                        'excerpts': selected_excerpts, 'dependencies': list(dependencies.values()),
                        'input_selections': inputs, 'semantic_support': 'not_verified',
                        **({'classification': 'program_copied_original_not_interpreted'}
                           if kind == 'retained_original' else {}),
                        **({'selected_context_incomplete': True} if incomplete else {})})
        parts.append(text)
        cursor += len(text)
    prose = '\n\n'.join(parts)
    receipt = {'protocol': PROTOCOL, 'text_sha256': sha256(prose.encode()).hexdigest(),
               'offset_unit': 'unicode_code_points', 'statements': records,
               'meaning': 'model_selected_prose_and_source_addresses_not_verification'}
    return {**raw, 'summary': prose, 'composition_attribution': receipt,
            'sections': [{'title': raw['title'], 'explanation': prose, 'composition_attribution': receipt}]}
