"""Local source snapshots and transport accounting, not semantic coverage scores.

Offsets count Python Unicode code points; hashes use UTF-8. Decoded HTTP text
is not the original response bytes, and repacked evidence is not an exact
character partition. Keep these distinctions explicit for future replay.
"""

from __future__ import annotations

import hashlib
import base64
import binascii
from zhixi.content.text_transforms import text_identity


def decode_file(data_base64: str) -> tuple[str, dict]:
    try:
        data = base64.b64decode(data_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError('文件字节编码损坏，请重新导入') from exc
    encoding = 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    try:
        text = data.decode(encoding, errors='strict')
    except UnicodeError as exc:
        raise ValueError(
            '无法可靠识别文件编码；请转换为 UTF-8 或带 BOM 的 UTF-16 后重试，未用替换字符继续分析'
        ) from exc
    return text, {
        'data_base64': data_base64,
        'byte_count': len(data),
        'sha256_bytes': hashlib.sha256(data).hexdigest(),
        'encoding': encoding,
    }


def snapshot(text: str, kind: str) -> dict:
    return {'kind': kind, 'text': text, **text_identity(text)}


def material_record(
    original: str,
    cleaned: str,
    model_input: str,
    *,
    kind: str,
    extracted: str | None = None,
    cleaning_transform: dict | None = None,
) -> dict:
    stages = [snapshot(original, kind)]
    if extracted is not None:
        stages.append(snapshot(extracted, 'extracted_text'))
    stages.extend([snapshot(cleaned, 'cleaned_text'), snapshot(model_input, 'model_input')])
    from zhixi.content.text_transforms import apply_text_edits, verify_transform

    transforms = []
    cleaning_index = 1 if extracted is not None else 0
    for index, (before, after) in enumerate(zip(stages, stages[1:])):
        record = {
            'from_stage': index,
            'to_stage': index + 1,
            'changed': before['text'] != after['text'],
            'span_mapping': 'not_yet_recorded',
        }
        mapping = cleaning_transform if index == cleaning_index else None
        if mapping is None and not record['changed']:
            _, mapping = apply_text_edits(before['text'], [])
        if mapping is not None:
            if not verify_transform(before['text'], after['text'], mapping):
                raise ValueError('清洗变换与原材料不一致，未保存错误跨度记录')
            record.update(span_mapping='verified_character_accounting', transform=mapping)
        transforms.append(record)
    return {'schema_version': 1, 'storage': 'local', 'stages': stages, 'transforms': transforms}


def processing_manifest(source: str, chunks: list[str], outcomes: list[dict], *, method: str) -> dict:
    if len(chunks) != len(outcomes):
        raise ValueError('处理批次与结果记录不一致')
    exact = ''.join(chunks) == source
    offset = 0
    records = []
    for index, (chunk, outcome) in enumerate(zip(chunks, outcomes), 1):
        row = {**outcome, 'index': index, **text_identity(chunk)}
        if exact:
            row.update(start=offset, end=offset + len(chunk))
        records.append(row)
        offset += len(chunk)
    return {
        'schema_version': 1,
        'input': text_identity(source),
        'method': method,
        'partition': 'exact_character_partition' if exact else 'repacked_records',
        'offset_unit': 'unicode_code_points',
        'chunks': records,
        'semantic_coverage': 'not_verified',
        'note': '批次完成只表示输入已交给相应处理步骤，不代表模型完整理解或结论正确。',
    }
