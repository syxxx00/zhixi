from __future__ import annotations

import re
from typing import Any
from zhixi.content.text_transforms import apply_text_edits


DATE_PATTERN = re.compile(
    r'(?:(?:19|20)\d{2}\s*年\s*(?:\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?|'
    r'(?:19|20)\d{2}(?:\s*[-/.]\s*|\s+)\d{1,2}\s*[-/.]\s*\d{1,2}|'
    r'(?:19|20)\d{2}\s*[-/.]\s*\d{1,2}|'
    r'(?:19|20)\d{2}\s*[-–—]\s*(?:19|20)\d{2}|(?:19|20)\d{2})'
)
TIME_PATTERN = re.compile(r'\b\d{1,2}\s*:\s*\d{2}(?:\s*:\s*\d{2})?\b')
SOURCE_PATTERN = re.compile(r'(?:来源|稿源|信息来源|文章来源)[：:]\s*(.+)', re.I)
BYLINE_PATTERN = re.compile(r'(?:责任编辑|编辑|校对|审核|记者|作者)[：:]\s*[^；;|]{1,80}', re.I)
UI_NOISE_PATTERN = re.compile(
    r'^(?:登录|注册|收藏|分享|打印|关闭|返回顶部|扫一扫|打开微信|客户端|手机版|'
    r'上一篇|下一篇|相关阅读|相关推荐|更多精彩|点击查看|展开全文|收起全文|加载更多|'
    r'首页|新闻|资讯|正文|播报|讨论|搜词条)[\s>»·|]*$',
    re.I,
)
LEGAL_NOISE_PATTERN = re.compile(
    r'(?:版权声明|免责声明|版权所有|Copyright\s*©?|ICP备|公网安备|违法和不良信息举报)', re.I
)
PROMOTIONAL_SECTION_PATTERN = re.compile(
    r'^(?:站内推广|推广信息|广告)[：:]|^【关于.{1,30}(?:平台|产品|服务|游戏)】$', re.I
)
PROMOTIONAL_SIGNALS = (
    '一键登录',
    '无需下载',
    '免下载安装',
    '即点即玩',
    'App Store',
    '赶紧试试',
    '立即体验',
    '马上体验',
    '点击下载',
    '畅享',
    '官方网站',
    '官方微博',
    '微信公众号',
)
NUMBER_CLAIM_PATTERN = re.compile(
    r'(?<![A-Za-z0-9_.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*'
    r'(%|％|万亿元|亿元|亿美元|万元|万|亿块|亿|千|百|倍|人|项|个|家|年|月|日|小时|分钟|秒|ms|s|GB|MB|TB)?',
    re.I,
)


def normalize_line(value: str) -> str:
    value = (
        value.replace('\ufeff', '')
        .replace('\u200b', '')
        .replace('\u2060', '')
        .replace('\x08', '')
        .replace('\xa0', ' ')
    )
    value = re.sub(r'[\t\r ]+', ' ', value).strip()
    return value


def normalize_date_label(value: str) -> str:
    numbers = re.findall(r'\d+', value)
    if not numbers:
        return value.strip()
    if '年' in value:
        return (
            numbers[0]
            + '年'
            + (str(int(numbers[1])) + '月' if len(numbers) > 1 else '')
            + (str(int(numbers[2])) + '日' if len(numbers) > 2 else '')
        )
    if len(numbers) >= 3:
        return f'{numbers[0]}-{int(numbers[1]):02d}-{int(numbers[2]):02d}'
    if len(numbers) == 2 and len(numbers[1]) <= 2:
        return f'{numbers[0]}-{int(numbers[1]):02d}'
    return re.sub(r'\s+', '', value)


def _metadata_only(line: str) -> tuple[bool, dict[str, str]]:
    metadata: dict[str, str] = {}
    source_match = SOURCE_PATTERN.search(line)
    if source_match:
        metadata['source'] = source_match.group(1).strip(' ；;，,')[:160]
    date_match = DATE_PATTERN.search(line)
    time_match = TIME_PATTERN.search(line)
    explicit_publication = bool(re.search(r'(?:发布时间|发布日期|更新时间|更新于|发布于)[：:]?', line, re.I))
    if date_match and (source_match or time_match or explicit_publication):
        metadata['published_at'] = normalize_date_label(date_match.group(0))

    residual = line
    if date_match:
        residual = residual[: date_match.start()] + residual[date_match.end() :]
    residual = TIME_PATTERN.sub('', residual)
    residual = SOURCE_PATTERN.sub('', residual)
    residual = BYLINE_PATTERN.sub('', residual)
    residual = residual.strip(' \t；;，,。.:：·|—-_/()（）[]【】')
    has_metadata = bool(source_match or time_match or explicit_publication or BYLINE_PATTERN.search(line))
    return has_metadata and len(residual) < 6, metadata


def clean_source_text(text: str, *, web: bool = False, title: str = '') -> tuple[str, dict[str, Any]]:
    """Keep uncertain content; apply only explicit, traceable text edits.

    A line looking like a footer or advertisement does not prove that it is
    irrelevant. HTML extraction handles known DOM chrome upstream; this layer
    must not turn a substring/duplicate heuristic into silent content loss.
    """
    metadata: dict[str, str] = {}
    removed = {
        'empty': 0,
        'ui': 0,
        'metadata': 0,
        'duplicate': 0,
        'url': 0,
        'legal': 0,
        'promotional': 0,
        'title': 0,
        'control': 0,
    }
    edits, blocks = [], []
    seen: dict[str, str] = {}
    fenced = False
    for match in re.finditer(r'[^\n]*(?:\n|$)', text):
        raw_line = match.group()
        if not raw_line:
            continue
        line = normalize_line(raw_line)
        block_id = f'line:{len(blocks) + 1}'
        hints = []
        code = fenced or raw_line.startswith(('\t', '    ')) or line.startswith(('```', '~~~'))
        if line.startswith(('```', '~~~')):
            fenced = not fenced
        if web and not code:
            if title and line == normalize_line(title):
                hints.append('page_title_candidate')
            if UI_NOISE_PATTERN.fullmatch(line):
                hints.append('interface_label_candidate')
            if LEGAL_NOISE_PATTERN.search(line):
                hints.append('legal_text_candidate')
            if (
                PROMOTIONAL_SECTION_PATTERN.search(line)
                or sum(token.casefold() in line.casefold() for token in PROMOTIONAL_SIGNALS) >= 3
            ):
                hints.append('promotional_text_candidate')
            if re.fullmatch(r'(?:https?://|www\.)\S+', line, re.I):
                hints.append('standalone_link')
        # Exact repetition is an annotation, not a reason to lose an occurrence
        # with potentially different surrounding attribution or conditions.
        if raw_line in seen:
            hints.append('exact_repetition_candidate')
        block = {
            'id': block_id,
            'start': match.start(),
            'end': match.end(),
            'disposition': 'retained',
            'hints': hints,
            'semantic_role': 'not_assessed',
            'code_candidate': code,
        }
        if raw_line in seen:
            block['same_text_as'] = seen[raw_line]
        else:
            seen[raw_line] = block_id
        is_metadata, found = _metadata_only(line)
        publication_line = (
            is_metadata
            and DATE_PATTERN.search(line)
            and (
                TIME_PATTERN.search(line)
                or re.match(r'^(?:发布时间|发布日期|更新时间|更新于|发布于)[：:]?', line)
            )
        )
        if web and not code and publication_line:
            metadata.update({key: value for key, value in found.items() if value})
            removed['metadata'] += 1
            block.update(disposition='excluded_from_body', reason='publication_metadata_line')
            edits.append(
                {
                    'start': match.start(),
                    'end': match.end(),
                    'replacement': '',
                    'reason': 'publication_metadata_line',
                }
            )
        else:
            for control in re.finditer('\x08', raw_line):
                removed['control'] += 1
                edits.append(
                    {
                        'start': match.start() + control.start(),
                        'end': match.start() + control.end(),
                        'replacement': '',
                        'reason': 'stray_backspace_control',
                    }
                )
        blocks.append(block)
    cleaned, transform = apply_text_edits(text, edits)
    report: dict[str, Any] = {
        'schema_version': 2,
        'original_characters': len(text),
        'cleaned_characters': len(cleaned),
        'original_lines': len(blocks),
        'cleaned_lines': len(blocks) - removed['metadata'],
        'removed': removed,
        'metadata': metadata,
        'blocks': blocks,
        'transform': transform,
        'policy': 'preserve_uncertain_lines_and_all_repetitions',
    }
    return cleaned, report


def timeline_event(unit: str, match: re.Match[str]) -> str:
    event = (unit[: match.start()] + unit[match.end() :]).strip()
    event = re.sub(
        r'^[\s；;，,:：·|—-]*(?:\d{1,2}\s*:\s*\d{2}(?:\s*:\s*\d{2})?)?[\s；;，,:：·|—-]*', '', event
    )
    metadata_only, _ = _metadata_only(unit)
    if metadata_only:
        return ''
    return event[:300]


def extract_timeline(text: str) -> list[dict[str, str]]:
    # Periods inside versions/decimals (for example HTML 4.01) are not sentence boundaries.
    units = [item.strip() for item in re.split(r'\n+|(?<=[。！？])\s*|(?<=[.!?])\s+', text) if item.strip()]
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for unit in units:
        if re.search(r'引用日期|参考资料|https?://', unit, re.I):
            continue
        match = DATE_PATTERN.search(unit)
        event = timeline_event(unit, match) if match else ''
        signature = re.sub(r'\s+', '', event).casefold()
        if match and len(event) >= 6 and signature not in seen:
            seen.add(signature)
            rows.append({'date': normalize_date_label(match.group(0)), 'event': event})

    def date_key(row: dict[str, str]) -> tuple[int, int, int]:
        values = [int(value) for value in re.findall(r'\d+', row['date'])[:3]]
        return tuple((values + [0, 0, 0])[:3])

    rows.sort(key=date_key)
    return rows


def _number_claims(value: str) -> list[tuple[str, str]]:
    claims: list[tuple[str, str]] = []
    for match in NUMBER_CLAIM_PATTERN.finditer(value):
        number, unit = match.group(1).replace(',', ''), (match.group(2) or '')
        significant = bool(unit or '.' in number or len(number) >= 4)
        if significant:
            claims.append((number.lstrip('0') or '0', unit.lower().replace('％', '%')))
    return claims


def ground_numeric_claims(result: dict[str, Any], source: str) -> dict[str, Any]:
    """Check numeric tokens without rewriting, truncating, or proving a claim.

    Matching values are only a lexical signal. They do not verify the associated
    subject, scope, time, negation, or interpretation.
    """
    from zhixi.content.evidence_context import source_units as original_units
    from zhixi.reports.audit_decisions import atomic_spans

    units = [item['text'] for item in original_units(source)]
    source_claims = {claim for unit in units for claim in _number_claims(unit)}
    issues = []
    checked_count = 0

    def verify_text(value: Any, field: str) -> None:
        nonlocal checked_count
        text = str(value or '').strip()
        for span in atomic_spans(text):
            statement = span['text']
            claims = _number_claims(statement)
            checked_count += len(claims)
            unmatched = list(dict.fromkeys(claim for claim in claims if claim not in source_claims))
            if not unmatched:
                continue
            values = {number for number, unit in unmatched}
            candidates = [unit for unit in units if values & {number for number, _ in _number_claims(unit)}]
            issues.append(
                {
                    'field': field,
                    'original': statement,
                    'unmatched_numeric_tokens': [number + unit for number, unit in unmatched],
                    'source_value_candidates': candidates,
                    'explanation': '该数字或单位表达未与原文逐字对应，需结合主体和语境核对；表达不同本身不等于事实错误。',
                }
            )

    verify_text(result.get('summary'), 'summary')
    for index, insight in enumerate(result.get('insights') or []):
        if isinstance(insight, dict):
            verify_text(insight.get('detail'), f'insights[{index}]')

    def walk(node: Any, path='tree'):
        if not isinstance(node, dict):
            return
        verify_text(node.get('label'), path + '.label')
        verify_text(node.get('note'), path + '.note')
        for index, child in enumerate(node.get('children') or []):
            walk(child, f'{path}.children[{index}]')

    walk(result.get('tree'))
    result['evidence_validation'] = {
        'schema_version': 2,
        'scope': 'numeric_tokens_only',
        'status': 'needs_review' if issues else 'value_matches' if checked_count else 'not_applicable',
        'checked_numeric_claims': checked_count,
        'source_numeric_tokens': len(source_claims),
        'issues': issues,
        'corrections': [],
        'text_modified': False,
    }
    return result
