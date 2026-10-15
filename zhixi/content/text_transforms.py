"""Exact, replayable text edits. Decisions are separate from their application."""

from __future__ import annotations

import hashlib


def text_identity(text: str) -> dict:
    return {'characters': len(text), 'sha256_utf8': hashlib.sha256(text.encode('utf-8')).hexdigest()}


def apply_text_edits(source: str, edits: list[dict]) -> tuple[str, dict]:
    """Apply nonoverlapping edits, accounting for every source/output character.

    The original snapshot is required for replay; offset units are Unicode code
    points, never UTF-8 bytes or browser UTF-16 positions. No fuzzy diff guesses.
    """
    cursor = output_cursor = 0
    parts, spans = [], []

    def append(start, end, replacement, action, reason):
        nonlocal output_cursor
        spans.append(
            {
                'source_start': start,
                'source_end': end,
                'output_start': output_cursor,
                'output_end': output_cursor + len(replacement),
                'action': action,
                'reason': reason,
            }
        )
        parts.append(replacement)
        output_cursor += len(replacement)

    for edit in sorted(edits, key=lambda item: item['start']):
        start, end = edit['start'], edit['end']
        replacement = edit.get('replacement', '')
        if not isinstance(start, int) or not isinstance(end, int) or not cursor <= start < end <= len(source):
            raise ValueError('文本变换跨度重叠或越界，未应用不可靠清洗')
        if not isinstance(replacement, str):
            raise ValueError('文本替换值必须为字符串')
        if start > cursor:
            append(cursor, start, source[cursor:start], 'keep', 'unchanged')
        append(start, end, replacement, 'replace' if replacement else 'remove', str(edit['reason']))
        cursor = end
    if cursor < len(source):
        append(cursor, len(source), source[cursor:], 'keep', 'unchanged')
    output = ''.join(parts)
    return output, {
        'schema_version': 1,
        'source': text_identity(source),
        'output': text_identity(output),
        'offset_unit': 'unicode_code_points',
        'spans': spans,
        'policy': 'explicit_nonoverlapping_edits_no_fuzzy_mapping',
    }


def verify_transform(source: str, output: str, record: dict) -> bool:
    """Check accounting/retained spans, not whether deleting content was justified."""
    if record.get('source') != text_identity(source) or record.get('output') != text_identity(output):
        return False
    source_cursor = output_cursor = 0
    for span in record.get('spans', []):
        a, b, x, y = (span.get(key) for key in ('source_start', 'source_end', 'output_start', 'output_end'))
        if not all(isinstance(value, int) for value in (a, b, x, y)):
            return False
        if a != source_cursor or x != output_cursor or not a < b <= len(source) or not x <= y <= len(output):
            return False
        if span.get('action') == 'keep' and source[a:b] != output[x:y]:
            return False
        if span.get('action') == 'remove' and x != y:
            return False
        if span.get('action') not in {'keep', 'remove', 'replace'}:
            return False
        source_cursor, output_cursor = b, y
    return source_cursor == len(source) and output_cursor == len(output)
