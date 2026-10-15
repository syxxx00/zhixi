"""Require an observed natural completion, not merely valid JSON or HTTP 200."""


def output_length_failure(exc):
    """Recognize only an observed length stop, including subprocess envelopes."""
    return isinstance(exc, RuntimeError) and any(
        message in str(exc)
        for message in (
            '本地模型未形成自然结束的完整输出（length）',
            'API模型未形成自然结束的完整输出（length）',
        )
    )


def checked_completion_text(response, observe):
    try:
        choice = response['choices'][0]
        content = choice['message']['content']
        reasoning = choice['message'].get('reasoning_content')
        finish = choice.get('finish_reason')
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        observe({'status': 'invalid', 'error': 'missing_completion_fields'})
        raise RuntimeError('llama.cpp 返回中缺少完整生成字段') from exc
    complete = finish == 'stop' and isinstance(content, str) and bool(content.strip())
    record = {
        'status': 'completed' if complete else 'incomplete',
        'finish_reason': finish,
        'usage': response.get('usage'),
        'timings': response.get('timings'),
        'content_characters': len(content) if isinstance(content, str) else None,
        # Observe that the configured mode ran, without copying private traces
        # into report evidence, future prompts, or public task telemetry.
        'reasoning_observed': isinstance(reasoning, str) and bool(reasoning.strip()),
        'reasoning_characters': len(reasoning) if isinstance(reasoning, str) else 0,
        'partial_text': None if complete else content if isinstance(content, str) else None,
        'semantic_acceptance': 'not_assessed',
    }
    observe(record)
    if not complete:
        raise RuntimeError(
            f'本地模型未形成自然结束的完整输出（{finish or "结束原因缺失"}）；已记录未完成输出，未作为完整结果采用'
        )
    return content
