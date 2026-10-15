"""Read delivered prose and repair identified problems once, without a retry loop."""

from collections import deque
from copy import deepcopy
import json

from zhixi.models.llama_completion import output_length_failure
from zhixi.research.research_reasoning import SOURCE_CONTEXT_POLICY

SYSTEM = """这是报告交付前的整体检查。逐段原文核对已由上一阶段完成；本阶段检查报告是否回应原始目标、章节间是否矛盾、摘要是否扩大正文或原文的范围，以及会影响整体结论的明显问题。
paragraphs是报告表达，不是原文；originals是程序取回的关联原文，带各自来源。不要用外部常识认证或纠正事实；未展示原文不等于没有依据。
发现会改变整体理解的关系问题时，用originals对照主体、条件、时间、数字分母和观点归属；不要把逐段事实核对重做成冗长的逐句审稿。保留有依据的解释，不因一处错误否定整段或全部研究。
paragraphs是本批的完整文字或明确标注的分片；报告概览提供全局方向，不是事实背书。批次未见不等于整篇没有。
同一根源的问题只提出一次；确有具体问题时，在issues简洁说明，并在repairs指出最适合回原文修订的paragraph_id和reason。仅选本批实际看到的编号，不要求重写其他有效内容，不为凑修改而找问题。
不要把个人风格偏好当成错误。保留有归属的观点、尚未解决的矛盾和实际限定，不能强行得出唯一结论。
每项repairs须附candidate_quote，逐字选本批paragraphs中确实需要修改的原句；不要误述其已有条件后再反驳。局部没展示依据是检查范围不足，不是已发现错误；背景段不必独立回答整篇目标。
“有评论提到”“作者认为”等自然归属已经足够时，不要求再加分类标签或通用免责声明。修订应纠正具体的主体、条件、范围或断句，不把正常解释改成审核说明。
paragraphs中的source_ids是每段对应的原文读取范围，不是事实认证。originals只用于这些实际关联，不能拿另一段的原文或页面标题反驳本段。文章含未来趋势预测，不代表其中引用的历史统计、已发生战报也属于预测。缺少所需原文时记notes，不把缺上下文当成错误修订。
statement_bindings_not_verified逐条定位该段本版本的表述及采用的原文编号；kind和references是既有处理选择，不是认证。对照该表述实际文字与所选原文，不用同段其他引用替它补出依据，尤其核对来源观点是否在概括中变成事实。
返回JSON {"issues":["具体问题"],"notes":["核对范围和限制"],"repairs":[{"paragraph_id":"本批编号","candidate_quote":"待改原句","reason":"需依据原文解决的具体问题"}]}。没有发现问题不等于事实可靠。"""
SYSTEM += SOURCE_CONTEXT_POLICY

# Separate protocol: unchanged successful whole-batch checks keep their exact
# cache identity. Recovery narrows output obligations, not the visible evidence.
PART_SYSTEM = SYSTEM + """
target_indices是本次需检查的paragraphs零起始下标。只为这些段落输出issues和repairs；其余paragraphs完整保留为关联上下文，可用于判断矛盾，但不是本次修改目标。
若有target_span，它是目标段落text的[start,end]字符范围；只对该范围内的具体表述提出问题。整段和其他段落仍是判断条件、否定及跨段关系的上下文，candidate_quote须逐字位于目标范围内。
同一错误只记一次，不改写成多个近义问题，也不为上下文段落开修订意见。issues简要写清位置与错误关系，repairs只引用定位所需的原句或连续子句，不重抄整份报告。
原文、限定和反例仍须完整考虑；若证据不足以判断，就记入notes，不把检查范围缩小当成已核实。
"""

ATOMIC_SYSTEM = """这是报告整体检查中一次未完成的单条核对。paragraphs是报告表达，originals是来源材料；goal限定研究目标，其他完整段落用于理解条件及跨段关系。
只检查target_indices指定的一个段落；若该字段省略，输入只有一个段落。若有target_span，只判断该范围内的一处具体表述，不能把上下文段落当成本次目标。
判断是否存在影响整体理解的矛盾、范围扩大或不回答目标。逐段事实核对已做过，不以外部常识重做审稿；没有看到原文不等于报告有错。
只返回一个判断：ok、issue或uncertain。issue需给出目标范围内逐字存在的candidate_quote及具体错误关系；uncertain说明缺少什么依据。不要列举其他段落的问题，不重复写同一意见。
返回JSON {"verdict":"ok|issue|uncertain","candidate_quote":"","reason":"","note":""}。不适用的字符串写空串。"""
ATOMIC_SYSTEM += SOURCE_CONTEXT_POLICY


def schema():
    string = {'type': 'string'}
    return {
        'type': 'object', 'additionalProperties': False,
        'required': ['issues', 'notes', 'repairs'],
        'properties': {
            **{k: {'type': 'array', 'items': string} for k in ('issues', 'notes')},
            'repairs': {'type': 'array', 'items': {
                'type': 'object', 'additionalProperties': False,
                'required': ['paragraph_id', 'candidate_quote', 'reason'],
                'properties': {'paragraph_id': string, 'candidate_quote': string, 'reason': string},
            }},
        },
    }


def atomic_schema():
    return {
        'type': 'object', 'additionalProperties': False,
        'required': ['verdict', 'candidate_quote', 'reason', 'note'],
        'properties': {
            'verdict': {'type': 'string', 'enum': ['ok', 'issue', 'uncertain']},
            **{key: {'type': 'string', 'maxLength': 320}
               for key in ('candidate_quote', 'reason', 'note')},
        },
    }


def locate_atomic_quote(quote, original):
    """Return the unique literal span when only whitespace differs.

    This never supplies words missing from the report or chooses among repeated
    matches. Revisions receive the exact existing text, not a model paraphrase.
    """
    if quote in original:
        return quote
    compact_quote = ''.join(character for character in quote if not character.isspace())
    positions = [index for index, character in enumerate(original) if not character.isspace()]
    compact_original = ''.join(original[index] for index in positions)
    if not compact_quote:
        return None
    first = compact_original.find(compact_quote)
    if first < 0 or compact_original.find(compact_quote, first + 1) >= 0:
        return None
    return original[positions[first]:positions[first + len(compact_quote) - 1] + 1]


def check_inputs(report, goal, budget, evidence=(), pages=None, *, unit_paths=None):
    from zhixi.reports.research_report import report_units
    from zhixi.reports.report_review_scope import citation_scopes
    from zhixi.content.evidence_transport import compact_source_bundle
    from zhixi.reports.grounding_statements import unit_bindings
    from zhixi.reports.report_organization import prose_slice

    base = {'goal': goal, 'scope': 'all_prose_in_batches_not_all_pairwise_relations_or_fact_verification'}
    units = list(report_units(report))
    scopes = citation_scopes(units, evidence) if evidence else {}
    if unit_paths is not None:
        units = [unit for unit in units if unit[0] in unit_paths]
        base['scope'] = 'selected_prose_after_repair; unchanged_prose_checked_before_repair'
    by_id = {str(row['id']): row for row in evidence}
    summary = report.get('summary', '')
    if len(summary) < budget // 3:
        base['overview_not_evidence'] = summary

    def encode(items):
        payload = {**base, 'paragraphs': items}
        # Share the available original-context space among actual targets.
        # Exhausting the first unit's page used to leave all later units with
        # unrelated originals, despite their own citations being available.
        from itertools import zip_longest
        sequences = [scopes.get(item['id'], {}).get('evidence_ids', []) for item in items]
        # Reopen the originals actually adopted by each shown statement first.
        # Context expansion may include many unrelated neighbours; their order
        # must not consume the space before another statement's direct source.
        direct = [[ref['evidence_id'] for ref in binding['references']
                   if ref['evidence_id'] in by_id]
                  for item in items for binding in item.get('statement_bindings_not_verified', [])]
        ids = list(dict.fromkeys(
            identity for groups in (direct, sequences) for slot in zip_longest(*groups)
            for identity in slot if identity is not None))
        # Fit complete records and their context, not a model-authored quote.
        # Unshown sources stay retrievable and are never treated as absent.
        payload['originals_omitted_count'] = len(ids)
        payload['originals_available_ids'] = []
        selected = []
        for identity in ids:
            trial = compact_source_bundle([*selected, by_id[identity]], pages or {})
            candidate = {**payload, 'originals': trial,
                         'originals_omitted_count': len(ids) - len(selected) - 1,
                         'originals_available_ids': [str(row['id']) for row in [*selected, by_id[identity]]]}
            if len(json.dumps(candidate, ensure_ascii=False)) <= budget:
                payload = candidate
                selected.append(by_id[identity])
        return json.dumps(payload, ensure_ascii=False)

    def emit(items):
        text = encode(items)
        available = set(json.loads(text)['originals_available_ids'])
        starved = any(
            (item['source_ids'] and not available.intersection(item['source_ids']))
            or any(ref['evidence_id'] in by_id and ref['evidence_id'] not in available
                   for binding in item.get('statement_bindings_not_verified', []) for ref in binding['references'])
            for item in items)
        if starved and len(items) > 1:
            # One generic related quote is not enough if this batch omits the
            # adopted source of another claim. Split only starved groups, not
            # every detail. A single unit can still exceed context capacity;
            # its omitted originals stay explicit, never marked as checked.
            midpoint = len(items) // 2
            yield from emit(items[:midpoint])
            yield from emit(items[midpoint:])
        else:
            yield text

    def prose_size(items):
        ids = {identity for item in items for identity in scopes.get(item['id'], {}).get('evidence_ids', [])}
        return len(json.dumps({**base, 'paragraphs': items, 'originals_omitted_count': len(ids),
                               'originals_available_ids': []}, ensure_ascii=False))

    group = []
    for path, heading, owner, field in units:
        text, offset = str(owner[field]), 0
        source_ids = scopes.get(path, {}).get('evidence_ids', [])
        bindings = unit_bindings(owner, field, set(by_id))

        def row_at(end):
            fragment = prose_slice({'text': text, 'statement_bindings_not_verified': bindings}, offset, end)
            return {'id': path, 'title': heading, 'offset': offset, 'text': fragment['text'],
                    'source_ids': source_ids, **({'statement_bindings_not_verified':
                        fragment['statement_bindings_not_verified']} if bindings else {})}
        # Reserve space in the existing call rather than adding a separate
        # source audit for every paragraph. Long originals remain explicit gaps.
        # The newly written overview is a high-impact synthesis boundary.
        # Reserve context there, not on every already-reviewed detail: doing
        # the latter more than doubled the old long report's review batches.
        overview = field == 'summary' and bool(source_ids)
        prose_budget = budget // 2 if overview else budget
        while offset < len(text):
            row = row_at(len(text))
            if prose_size([{**row, 'text': ''}]) >= prose_budget:
                prose_budget = budget
            if prose_size([row]) > prose_budget:
                low, high = 0, len(row['text'])
                while low < high:
                    mid = (low + high + 1) // 2
                    if prose_size([row_at(offset + mid)]) <= prose_budget:
                        low = mid
                    else:
                        high = mid - 1
                if not low:
                    raise ValueError('目标与段落标题超过检查容量；原报告保留')
                row = row_at(offset + low)
            group_budget = min(prose_budget, budget // 2) if any(
                item['id'] == 'summary' and scopes.get('summary', {}).get('evidence_ids') for item in group
            ) else prose_budget
            if group and prose_size([*group, row]) > group_budget:
                yield from emit(group)
                group = []
            group.append(row)
            offset += len(row['text'])
    if group:
        yield from emit(group)


def split_check_input(text):
    """Halve review targets, preserving every original paragraph and source.

    A single target may split at sentence or clause boundaries. The full
    paragraph remains visible as context, including text outside that span.
    """
    payload = json.loads(text)
    targets = payload.get('target_indices', list(range(len(payload['paragraphs']))))
    if not targets:
        return []
    if len(targets) == 1:
        if 'target_indices' not in payload:
            groups = [(targets, None)]  # First retry: same text, focused response.
        else:
            fragment = payload['paragraphs'][targets[0]]['text']
            start, end = payload.get('target_span', [0, len(fragment)])
            boundaries = [i + 1 for i in range(start, end - 1)
                          if fragment[i] in '。！？!?；;\n，,']
            if not boundaries:
                boundaries = [i + 1 for i in range(start, end - 1) if fragment[i].isspace()]
            if not boundaries:
                return []
            middle = min(boundaries, key=lambda i: abs(i - (start + end) / 2))
            groups = [(targets, [start, middle]), (targets, [middle, end])]
    else:
        middle = len(targets) // 2
        groups = [(targets[:middle], None), (targets[middle:], None)]
    result = []
    for group, span in groups:
        child = {**payload, 'scope': 'targeted', 'target_indices': group}
        if span is not None:
            child['target_span'] = span
        encoded = json.dumps(child, ensure_ascii=False, separators=(',', ':'))
        if encoded == text:
            return []
        result.append(encoded)
    return result


def check_report(report, goal, budget, call, *, evidence=(), pages=None, unit_paths=None):
    result = {'status': 'incomplete', 'fact_verification': False, 'issues': [], 'notes': [],
              'repairs': [], 'steps': [], 'scope': 'all_prose_in_batches_not_all_pairwise_relations'}
    try:
        inputs = list(check_inputs(report, goal, budget, evidence, pages, unit_paths=unit_paths))
    except ValueError as exc:
        return {**result, 'error': str(exc)}
    if not inputs:
        return {**result, 'error': '没有可检查的交付正文'}
    if unit_paths is not None:
        result['scope'] = 'selected_prose_after_repair; unchanged_prose_checked_before_repair'
        result['selected_unit_paths'] = sorted(unit_paths)
    invalid = False
    pending = deque((text, 'grounding:report_check') for text in inputs)
    index = 0
    while pending:
        text, phase = pending.popleft()
        index += 1
        payload = json.loads(text)
        shown = [payload['paragraphs'][i] for i in payload.get(
            'target_indices', range(len(payload['paragraphs'])))]
        if 'target_span' in payload:
            start, end = payload['target_span']
            shown = [{**shown[0], 'text': shown[0]['text'][start:end],
                      'offset': shown[0]['offset'] + start}]
        known = {p['id'] for p in shown}
        try:
            raw = call(text, phase, index, index + len(pending))
        except RuntimeError as exc:
            if not output_length_failure(exc):
                raise  # Cancellation, memory and transport errors keep their own recovery path.
            parts = split_check_input(text)
            if any(len(part) > budget for part in parts):
                parts = []  # Recovery metadata must not defeat the original input limit.
            atomic = not parts and phase != 'grounding:report_check_atomic' and len(shown) == 1
            result['steps'].append({
                'paragraph_ids': sorted(known), 'characters': len(text), 'error': str(exc),
                'status': 'output_limit_split' if parts else 'output_limit_atomic' if atomic
                else 'incomplete_output_limit',
                'targets': [{'id': p['id'], 'offset': p['offset']} for p in shown],
            })
            if parts:
                pending.extendleft((part, 'grounding:report_check_part') for part in reversed(parts))
            elif atomic:
                pending.appendleft((text, 'grounding:report_check_atomic'))
            else:
                invalid = True
            continue
        atomic_result = None
        if phase == 'grounding:report_check_atomic':
            atomic_result = deepcopy(raw)
            if not isinstance(raw, dict) or raw.get('verdict') not in {'ok', 'issue', 'uncertain'}:
                invalid = True
                result['steps'].append({'paragraph_ids': sorted(known), 'characters': len(text),
                                        'status': 'invalid_atomic_result', 'result': raw})
                continue
            if raw['verdict'] == 'issue':
                if not all(isinstance(raw.get(key), str) and raw[key].strip()
                           for key in ('candidate_quote', 'reason')):
                    invalid = True
                    result['steps'].append({'paragraph_ids': sorted(known), 'characters': len(text),
                                            'status': 'invalid_atomic_result', 'result': raw})
                    continue
                quote = locate_atomic_quote(raw['candidate_quote'], shown[0]['text'])
                if quote is None:
                    invalid = True
                    result['steps'].append({'paragraph_ids': sorted(known), 'characters': len(text),
                                            'status': 'invalid_atomic_result', 'result': raw})
                    continue
                raw = {'issues': [f"{shown[0]['id']}：{raw.get('reason', '')}"], 'notes': [],
                       'repairs': [{'paragraph_id': shown[0]['id'],
                                    'candidate_quote': quote,
                                    'reason': raw.get('reason', '')}]}
            elif raw['verdict'] == 'uncertain':
                raw = {'issues': [f"{shown[0]['id']}：需要补查依据"],
                       'notes': [str(raw.get('note') or '当前材料不足以判断')], 'repairs': []}
            else:
                raw = {'issues': [], 'notes': [], 'repairs': []}
        step = {'paragraph_ids': sorted(known), 'characters': len(text), 'result': raw}
        if atomic_result is not None:
            step['atomic_result'] = atomic_result
        result['steps'].append(step)
        if not isinstance(raw, dict) or any(
            not isinstance(raw.get(k), list) or any(not isinstance(v, str) for v in raw[k])
            for k in ('issues', 'notes')
        ):
            invalid = True
            step['status'] = 'invalid_result'
            continue
        result['issues'].extend(raw['issues'])
        result['notes'].extend(raw['notes'])
        repairs = raw.get('repairs', [])
        if not isinstance(repairs, list):
            invalid = True
            step['status'] = 'invalid_result'
            continue
        for item in repairs:
            if (not isinstance(item, dict) or item.get('paragraph_id') not in known
                    or not isinstance(item.get('reason'), str) or not item['reason'].strip()):
                invalid = True
                step['status'] = 'invalid_repairs'
                step.setdefault('uncompleted_paragraph_ids', []).extend(sorted(known))
            else:
                if 'candidate_quote' in item:
                    from zhixi.reports.report_shared_review import located_issues

                    candidate = '\n'.join(p['text'] for p in shown if p['id'] == item['paragraph_id'])
                    valid, rejected = located_issues([item], candidate)
                    if not valid:
                        result.setdefault('rejected_repairs', []).extend(rejected)
                        invalid = True
                        step['status'] = 'invalid_repairs'
                        step.setdefault('uncompleted_paragraph_ids', []).append(item['paragraph_id'])
                        continue
                result['repairs'].append(item)
    result['issues'] = list(dict.fromkeys(result['issues']))
    result['uncompleted_paragraph_ids'] = sorted({
        identity for step in result['steps']
        if step.get('status') in {'invalid_result', 'invalid_repairs', 'invalid_atomic_result',
                                 'incomplete_output_limit'}
        for identity in step.get('uncompleted_paragraph_ids', step['paragraph_ids'])
    })
    result['status'] = 'incomplete' if invalid else 'issues_found' if result['issues'] or result['repairs'] else 'no_issues_reported'
    return result


def finalize_report(report, evidence, pages, goal, budget, analyze, *, rewrite):
    """Repair targets and selected dependents once, then inspect that scope only."""
    from zhixi.reports.research_report import report_units
    from zhixi.reports.report_review_scope import citation_scopes

    def call(*args):
        value = analyze(*args)
        runtime = (value.get('runtime') or {}) if isinstance(value, dict) else {}
        saved = report.setdefault('runtime', {})
        saved['seconds'] = saved.get('seconds', 0) + float(runtime.get('seconds') or 0)
        key = 'checkpoint_steps' if runtime.get('checkpoint_reused') else 'model_calls'
        saved[key] = saved.get(key, 0) + 1
        return value

    before = check_report(report, goal, budget, call, evidence=evidence, pages=pages)
    report['overall_report_check'] = before
    units = {path: (heading, owner, field) for path, heading, owner, field in report_units(report)}
    repairs, located_repairs = {}, {}
    for item in before['repairs']:
        repairs.setdefault(item['paragraph_id'], []).append(item['reason'])
        if item.get('candidate_quote'):
            located_repairs.setdefault(item['paragraph_id'], []).append({
                'candidate_quote': item['candidate_quote'], 'reason': item['reason'],
            })
    if not repairs:
        return report
    from zhixi.reports.report_composition import dependency_paths

    selected_dependencies = dependency_paths(list(report_units(report)))
    delivery = {'before': before, 'changes': [], 'source_scopes': {}, 'dependent_grounding': [], 'dependencies': {},
                'propagation_basis': 'selected_prose_dependencies_with_legacy_ancestor_fallback'}
    report['delivery_repairs'] = delivery
    pending, completed = deepcopy(repairs), set()

    def depends(parent, child):
        if parent in selected_dependencies:
            return child in selected_dependencies[parent]
        return (parent == 'summary' and child != 'summary') or (
            parent.startswith('tree') and child.startswith(parent + '.'))

    while pending:
        # A summary explicitly flagged by the checker still waits for its
        # detailed targets. No unit is rewritten twice to satisfy a checker.
        ready = [identity for identity in pending
                 if not any(depends(identity, other) for other in pending if other != identity)]
        if not ready:
            raise RuntimeError('修订的正文采用关系形成循环，未按任意顺序覆盖报告；原记录可供重试')
        scopes = citation_scopes(list(report_units(report)), evidence)
        children = []
        for identity in ready:
            heading, owner, field = units[identity]
            reasons = list(pending[identity])
            changed = delivery['dependencies'].get(identity, [])
            if changed:
                from zhixi.reports.grounding_context import retrieve_context_records

                # A dependent passage needs the originals actually selected
                # by the corrected body, not that body's prose as new facts.
                # Keep its old scope too, so a false correction can be rejected.
                ids = set(scopes[identity].get('context', {}).get('selected_evidence_ids', []))
                for path in changed:
                    ids.update(scopes[path].get('context', {}).get('selected_evidence_ids', []))
                rows, context = retrieve_context_records(evidence, ids)
                scopes[identity] = {**scopes[identity], 'evidence_ids': [str(r['id']) for r in rows],
                    'context': context, 'basis': 'existing_unit_and_corrected_dependency_originals'}
                shown = []
                for path in changed:
                    if len(','.join([*shown, path])) > min(1200, budget // 8):
                        break
                    shown.append(path)
                reasons.append(
                    '关联正文已按原文修订。变更定位：' + ','.join(shown)
                    + f'；另有{len(changed) - len(shown)}处定位保存在修订记录。'
                    '请回读所给原文，核对本概括的对象、条件、时间与观点归属；仅修订依赖的表述，仍成立可保留。'
                    '变更记录不是新增证据，不要求把详细正文复述到概括中。')
            # Carry the current mixed-prose addresses into a targeted repair.
            # If the writer retains the actual text, it must not relabel a
            # copied original (or source opinion) as one newly checked claim.
            receipt_keys = (
                ('summary_attribution', 'summary_composition_attribution') if field == 'summary'
                else ('statement_attribution', 'composition_attribution'))
            addresses = {target: deepcopy(owner[source])
                         for source, target in zip(receipt_keys, ('statement_attribution', 'composition_attribution'))
                         if source in owner}
            children.append({'label': heading, 'note': owner[field], 'children': [], **addresses,
                             'citations': scopes[identity].get('context', {}).get('selected_evidence_ids', []),
                             'parent_headings': deepcopy(owner.get('parent_headings', [])),
                             'revision_request': {'candidate': owner[field], 'issues': reasons,
                                                  'located_issues': deepcopy(located_repairs.get(identity, []))}})
            delivery['source_scopes'][identity] = scopes[identity]
        candidate = {'title': goal, 'tree': {'label': goal, 'children': children},
                     'source_review_policy': 'citation_first_v1'}
        rewritten = rewrite(candidate, evidence, pages, goal, budget, call, check_delivery=False,
                            repair_scopes={f'tree.{i}': scopes[identity] for i, identity in enumerate(ready)})
        ledger = rewritten['source_grounding']
        if 'grounding' not in delivery:
            delivery['grounding'] = ledger
        else:
            delivery['dependent_grounding'].append({'paragraph_ids': ready, 'grounding': ledger})
        records = {u['path']: u for u in ledger['units']}
        for i, identity in enumerate(ready):
            pending.pop(identity)
            completed.add(identity)
            _, owner, field = units[identity]
            record = records.get(f'tree.{i}', {})
            if record.get('status') not in {'rewritten', 'retained'}:
                delivery['changes'].append({'paragraph_id': identity, 'status': 'unresolved'})
                continue
            previous = owner[field]
            text = previous if record['status'] == 'retained' else record['text']
            delivery['changes'].append({'paragraph_id': identity, 'before': previous, 'after': text,
                                        'status': 'retained' if text == previous else 'revised'})
            owner[field] = text
            attribution = deepcopy(record.get('attribution', {}))
            if field == 'summary':
                if text != previous:
                    report.pop('summary_composition_attribution', None)
                report['summary_attribution'] = attribution
                report['summary_support_status'] = 'model_attributed'
            else:
                # These describe the previous paragraph/version. A successful
                # rewrite must not retain copied quotes or an old source basis
                # as if they still explained the new prose.
                if text != previous:
                    owner.pop('retained_originals', None)
                    owner.pop('source_basis_not_verified', None)
                    owner.pop('support_status', None)
                    owner.pop('composition_attribution', None)
                owner['statement_attribution'] = attribution
                owner['citations'] = list(dict.fromkeys(e['evidence_id'] for e in record.get('excerpts', [])))
            if text == previous:
                continue
            for parent in units:
                if parent in completed or not depends(parent, identity):
                    continue
                pending.setdefault(parent, [])
                delivery['dependencies'].setdefault(parent, []).append(identity)
    report['overall_report_check'] = check_report(
        report, goal, budget, call, evidence=evidence, pages=pages, unit_paths=completed)
    after = report['overall_report_check']
    # Rechecking repaired prose cannot certify untouched, unfinished scopes.
    # Keep the initial record in delivery.before, not rerun all prose or hide it.
    uncompleted = set(before.get('uncompleted_paragraph_ids', [])) - completed
    after['uncompleted_paragraph_ids'] = sorted(uncompleted | set(after.get('uncompleted_paragraph_ids', [])))
    after['prior_scope_findings'] = [
        {'paragraph_ids': sorted(set(step['paragraph_ids']) - completed),
         'issues': deepcopy(step['result']['issues'])}
        for step in before['steps']
        if set(step['paragraph_ids']) - completed
        and not (set(step['paragraph_ids']) & completed)
        and isinstance(step.get('result'), dict)
        and isinstance(step['result'].get('issues'), list) and step['result']['issues']
    ]
    if after['uncompleted_paragraph_ids']:
        after['status'] = 'incomplete'
    elif after['prior_scope_findings'] and after['status'] == 'no_issues_reported':
        after['status'] = 'issues_found'
    report['delivery_repairs']['remaining_repairs'] = deepcopy(report['overall_report_check']['repairs'])
    changes_finished = all(change['status'] in {'revised', 'retained'} for change in delivery['changes'])
    delivery['status'] = (
        'unresolved' if not changes_finished or after['status'] == 'issues_found'
        else 'check_incomplete' if after['status'] == 'incomplete'
        else 'revised'
    )
    if report['delivery_repairs']['status'] == 'unresolved':
        report.setdefault('warnings', []).append('已完成一次针对性处理，仍有未解决的修改意见；正文与原文保留，详见处理记录。')
    return report
