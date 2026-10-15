from __future__ import annotations

import ast
import json
import re
from typing import Any


def _object_slice(value: str) -> str:
    value = re.sub(r'^```(?:json)?\s*', '', value.strip(), flags=re.I)
    start = value.find('{')
    if start < 0:
        raise json.JSONDecodeError('no JSON object', value, 0)
    stack: list[str] = []
    in_string = False
    escaped = False
    for index, character in enumerate(value[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in '[{':
            stack.append(character)
        elif character in ']}':
            if stack:
                stack.pop()
            if not stack:
                return value[start : index + 1]
    return value[start:].removesuffix('```').strip()


def _balance(value: str) -> str:
    stack: list[str] = []
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in '[{':
            stack.append(character)
        elif character == ']' and stack and stack[-1] == '[':
            stack.pop()
        elif character == '}' and stack and stack[-1] == '{':
            stack.pop()
    if in_string:
        value += '"'
    return value + ''.join('}' if opener == '{' else ']' for opener in reversed(stack))


def _repair_syntax(value: str) -> str:
    value = value.replace('\ufeff', '')
    value = re.sub(r'//[^\n\r]*', '', value)
    value = re.sub(r'/\*.*?\*/', '', value, flags=re.S)
    value = re.sub(r'([{,]\s*)([A-Za-z_\u4e00-\u9fff][\w\-\u4e00-\u9fff]*)(\s*:)', r'\1"\2"\3', value)
    value = re.sub(r'[，,]\s*([}\]])', r'\1', value)
    value = re.sub(r'([}\]])\s*(?=\"[^\"\n]+\"\s*:)', r'\1,', value)
    value = re.sub(r'([}\]])\s*(?=[{\[])', r'\1,', value)
    value = re.sub(
        r'(\"|\d|true|false|null)\s*\n(?=\s*\"[^\"\n]+\"\s*:)',
        lambda match: match.group(1) + ',\n',
        value,
        flags=re.I,
    )
    return _balance(value)


def loads_repaired(answer: str) -> dict[str, Any]:
    candidate = _object_slice(answer)
    repaired = _repair_syntax(candidate)
    attempts = [candidate, repaired]
    last_error: json.JSONDecodeError | None = None
    for attempt in attempts:
        try:
            parsed = json.loads(attempt, strict=False)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            last_error = exc
    try:
        python_value = re.sub(r'\bnull\b', 'None', repaired, flags=re.I)
        python_value = re.sub(r'\btrue\b', 'True', python_value, flags=re.I)
        python_value = re.sub(r'\bfalse\b', 'False', python_value, flags=re.I)
        parsed = ast.literal_eval(python_value)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, SyntaxError):
        pass
    if last_error:
        raise last_error
    raise json.JSONDecodeError('JSON root is not an object', candidate, 0)
