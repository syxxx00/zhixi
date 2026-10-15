"""Model reasoning boundary shared by planning and incremental reading."""

import json
import re
import sqlite3
from zhixi.research import research_execution as execution
from zhixi.research import goal_interpretation as interpretation
from zhixi.models.llama_completion import output_length_failure
from zhixi.research.research_contracts import (
    PLAN_SYSTEM as PLAN_SYSTEM,
    QUERY_SYSTEM as QUERY_SYSTEM,
    LINK_SYSTEM as LINK_SYSTEM,
    SOURCE_SYSTEM as SOURCE_SYSTEM,
    ROUTE_REVIEW_BATCH_SIZE as ROUTE_REVIEW_BATCH_SIZE,
    plan_fingerprint as plan_fingerprint,
    route_plan as route_plan,
    InvalidReasoningOutput as InvalidReasoningOutput,
)


def platform_hint(goal):
    rules = [
        (r'b站|哔哩哔哩|bilibili', 'bilibili.com'),
        (r'知乎|zhihu', 'zhihu.com'),
        (r'微博|weibo', 'weibo.com'),
        (r'抖音|douyin', 'douyin.com'),
        (r'小红书|xiaohongshu', 'xiaohongshu.com'),
        (r'youtube|油管', 'youtube.com'),
    ]
    domains = [host for pattern, host in rules if re.search(pattern, goal, re.I)]
    domains.extend(
        match.lower().removeprefix('www.')
        for match in re.findall(r'\b(?:[a-z0-9-]+\.)+(?:com|org|net|cn|wiki|edu)\b', goal, re.I)
    )
    return list(dict.fromkeys(domains))


def reasoning_schema(system, goal, payload):
    """Constrain identifiers/goal quotations at decoding time, not just afterward."""
    string = {'type': 'string'}

    def obj(properties):
        return {
            'type': 'object',
            'properties': properties,
            'required': list(properties),
            'additionalProperties': False,
        }

    def array(items):
        return {'type': 'array', 'items': items}

    def enum(values):
        return {'type': 'string', 'enum': values}

    while isinstance(payload, dict) and 'input' in payload:
        payload = payload['input']
    from zhixi.research import research_reading_state as research_learning
    from zhixi.research import research_reflection

    if system == research_learning.SYSTEM:
        return research_learning.schema(payload)
    if system == research_reflection.SYSTEM:
        return research_reflection.schema(payload)
    if system == research_reflection.PUBLIC_SYSTEM:
        return research_reflection.public_schema()
    from zhixi.sources import source_queries

    source_schema = source_queries.schema(system, payload)
    if source_schema is not None:
        return source_schema
    if system in {PLAN_SYSTEM, QUERY_SYSTEM}:
        topic = payload.get('authorized_public_topic', payload.get('goal', goal))
        return interpretation.schema(topic, platform_hint(topic), queries_only=system == QUERY_SYSTEM)
    if system == interpretation.AUDIT_SYSTEM:
        identifiers = enum([item['id'] for item in payload['items']])
        checks = obj(
            {
                item['id']: obj({'faithful': {'type': 'boolean'}, 'reason': string})
                for item in payload['items']
            }
        )
        return obj(
            {'checks': checks, 'named_entities': array(obj({'name': string, 'item_ids': array(identifiers)}))}
        )
    if system == SOURCE_SYSTEM:
        from zhixi.research.research_contracts import source_selection_schema

        return source_selection_schema(payload)
    if system == LINK_SYSTEM:
        decision = obj(
            {
                'decision': enum(['visit', 'defer']),
                'question_ids': array(enum([q['id'] for q in payload['plan']['questions']])),
                'purpose': enum(['primary', 'context', 'probe', '']),
                'reason': string,
            }
        )
        decision['properties']['expected_usefulness'] = {'type': 'number', 'minimum': 0, 'maximum': 1}
        decision['required'].append('expected_usefulness')
        return obj({'decisions': obj({c['id']: decision for c in payload['candidates']})})
    return {'type': 'object'}


def validated_reason(ctx, project, system, payload, validate):
    raw = reason(ctx, project, system, payload)
    try:
        return validate(raw)
    except (ValueError, TypeError, KeyError) as exc:
        if project.get('id'):
            ctx.add_research_event(
                project['id'],
                'planning_output_rejected',
                '规划输出未通过检查，正在自动修正',
                level='warning',
                details={'error': str(exc), 'raw_output': raw},
            )
        # A schema correction is bounded and retains the same task/authority.
        repair = {
            'input': payload,
            'invalid_response': raw,
            'validation_error': str(exc),
            'instruction': '仅修正上述结构或遗漏，重新返回完整JSON，不扩大研究范围。',
        }
        if len(json.dumps(repair, ensure_ascii=False)) > 7500:
            # The invalid draft remains in the event ledger, not duplicated in
            # a full context that would overflow again during repair.
            repair.pop('invalid_response')
        corrected = reason(ctx, project, system, repair)
        try:
            return validate(corrected)
        except (ValueError, TypeError, KeyError) as final_exc:
            if project.get('id'):
                ctx.add_research_event(
                    project['id'],
                    'planning_output_rejected',
                    '自动修正后的输出仍未通过检查，已保留实际返回内容',
                    level='warning',
                    details={'attempt': 2, 'error': str(final_exc), 'raw_output': corrected},
                )
            raise InvalidReasoningOutput(str(final_exc)) from final_exc


def selection(ctx, identity):
    selected = ctx.research_workflow.SELECTIONS.get((str(ctx.DB_PATH), identity))
    if selected:
        return selected
    with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        row = db.execute(
            'SELECT selection_json FROM research_workflows WHERE project_id=?', (identity,)
        ).fetchone()
    if not row:
        return None  # Old capture-only API has no authorized model selection.
    config = json.loads(row[0])
    if config.get('provider') == 'api':
        raise ValueError('请重新选择 API 模型以恢复研究规划；不会自动切换模型。')
    return ctx.ModelCheckRequest(**config)


def active(ctx, identity):
    if not execution.is_current():
        return False
    with sqlite3.connect(ctx.DB_PATH, timeout=30) as db:
        row = db.execute('SELECT status FROM research_projects WHERE id=?', (identity,)).fetchone()
    return bool(row and row[0] in {'queued', 'crawling'})


def reason(ctx, project, system, payload):
    identity = project['id']
    owner = execution.CURRENT.get()
    if not active(ctx, identity):
        raise ctx.JobCancelled('研究已暂停或取消')
    selected = selection(ctx, identity)
    if selected is None:
        raise ValueError('该旧任务没有研究模型，请选择模型后继续自动研究')
    request = ctx.AnalyzeRequest(source_type='text', content=project['goal'], **selected.model_dump())
    # Waiting for the shared model does not consume the collection clock.
    ctx.research_workflow.stop_clock(ctx, identity)
    from zhixi.sources import source_queries
    from zhixi.research import research_reading_state as research_learning
    from zhixi.research import research_reflection

    label = {
        PLAN_SYSTEM: '理解目标与规划调查',
        QUERY_SYSTEM: '规划公开搜索',
        research_learning.SYSTEM: '阅读原文并记录暂定线索',
        research_reflection.SYSTEM: '回看完整目标与时间投入',
        research_reflection.PUBLIC_SYSTEM: '根据公开检索成果重新安排搜索方向',
        interpretation.AUDIT_SYSTEM: '检查规划是否偏离原意',
        source_queries.SELECT_SYSTEM: '根据问题寻找可用公开线索',
        source_queries.QUERY_SYSTEM: '拆解公开主题并规划搜索'
        if not payload.get('public_sources')
        else '根据已读材料规划后续搜索',
        source_queries.AUDIT_SYSTEM: '检查后续搜索的对象与范围',
    }.get(system, '检查候选能否帮助回答问题')

    def progress(value, stage):
        with execution.bind(owner):
            if value >= 20:
                ctx.research_workflow.start_clock(ctx, identity)
            with execution.connection(ctx.DB_PATH, identity) as db:
                db.execute(
                    'UPDATE research_intents SET stage=? WHERE project_id=?',
                    (label + ' · ' + stage, identity),
                )

    completion = {}

    def observe(**runtime):
        if isinstance(runtime.get('model_completion'), dict):
            completion.update(runtime['model_completion'])

    try:
        progress(16, '等待模型')
        catalog = json.dumps(payload, ensure_ascii=False)
        if selected.provider == 'local':
            raw = ctx.run_qwen(
                '',
                '',
                request,
                progress=progress,
                runtime_update=observe,
                task_payload={
                    'task': 'research_reasoning',
                    'system': system,
                    'catalog': catalog,
                    'schema': reasoning_schema(system, project['goal'], payload),
                },
                should_stop=lambda: not execution.is_current(owner) or not active(ctx, identity),
            )
        else:
            config = selected.api_config
            if not config:
                raise ValueError('API 模型配置缺失')
            base = config.base_url.rstrip('/')
            headers = {'Authorization': f'Bearer {config.api_key}'} if config.api_key else {}
            ctx.research_workflow.start_clock(ctx, identity)
            response = ctx.httpx.post(
                base if base.endswith('/chat/completions') else base + '/chat/completions',
                headers=headers,
                json={
                    'model': config.model,
                    'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': catalog}],
                    'temperature': 0,
                    'max_tokens': 4000,
                },
                timeout=1800,
            )
            response.raise_for_status()
            choice = response.json()['choices'][0]
            if choice.get('finish_reason') == 'length':
                completion.update(
                    status='incomplete', finish_reason='length', partial_text=choice['message'].get('content')
                )
                raise RuntimeError('API模型未形成自然结束的完整输出（length）')
            raw = ctx.extract_json_object(choice['message']['content'])
        if not active(ctx, identity):
            raise ctx.JobCancelled('研究已暂停或取消，未继续抓取')
        return raw
    except RuntimeError as exc:
        if output_length_failure(exc) and active(ctx, identity):
            ctx.add_research_event(
                identity,
                'reasoning_output_incomplete',
                '模型输出达到容量上限，未采用；已记录本次未完成输出，交由当前步骤恢复。',
                level='warning',
                details={'step': label, 'error': str(exc), 'completion': completion},
            )
        raise
    finally:
        ctx.research_workflow.start_clock(ctx, identity)
