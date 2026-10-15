"""Check the combined rendered input and output reserve without truncation."""


class InputCapacityError(ValueError):
    pass


def check_input_budget(input_tokens, output_tokens, context_length, *, margin=32):
    if type(context_length) is not int or context_length <= 0:
        raise InputCapacityError('model_input_capacity: 未取得当前模型的上下文容量，未执行问答生成。')
    if type(input_tokens) is not int or input_tokens < 0 or type(output_tokens) is not int or output_tokens <= 0:
        raise InputCapacityError('model_input_capacity: 无法核对完整输入和回答空间，未执行问答生成。')
    observation = {'input_tokens': input_tokens, 'output_reserve_tokens': output_tokens,
                   'template_boundary_reserve_tokens': margin, 'context_length': context_length,
                   'scope': 'whole_rendered_prompt', 'input_truncated': False}
    if input_tokens + output_tokens + margin > context_length:
        raise InputCapacityError(
            f'model_input_capacity: 完整输入 {input_tokens} tokens 加回答空间 {output_tokens} '
            f'及边界余量 {margin} 超过上下文 {context_length}；未截断问题、选文或原文，未执行生成。')
    return observation


def render_messages(tokenizer, messages, *, enable_thinking=False):
    """One template path for HF measurement and generation, including fallback."""
    try:
        return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True,
                                             enable_thinking=enable_thinking)
    except (ValueError, TypeError, AttributeError):
        return '\n\n'.join(
            f'{str(item.get("role", "user")).upper()}:\n{str(item.get("content", ""))}'
            for item in messages if isinstance(item, dict)) + '\n\nASSISTANT:\n'


def model_context_length(model):
    config = getattr(model, 'config', None)
    text_config = getattr(config, 'text_config', None)
    return getattr(text_config, 'max_position_embeddings', None) or getattr(config, 'max_position_embeddings', None)


def provider_capacity_detail(body):
    """Recognize an explicit provider refusal, not guess its context limit."""
    error = body.get('error') if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None
    code, message = str(error.get('code') or ''), str(error.get('message') or '')
    text = (code + ' ' + message).lower()
    if any(term in text for term in ('context_length_exceeded', 'maximum context length', 'too many tokens')):
        return message or code
    return None
