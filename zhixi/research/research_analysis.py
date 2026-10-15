from __future__ import annotations

import hashlib
import json
import datetime as dt
import re
from collections import defaultdict
from bisect import bisect_right
from typing import Any, Iterable
from urllib.parse import urlparse

from zhixi.content.text_cleaning import extract_timeline


SENTENCE_SPLIT = re.compile(r'(?<=[。！？!?；;])\s+|\n+')
OPINION_POSITIVE = ('支持', '喜欢', '优秀', '好评', '推荐', '满意', '期待', '认可', '值得')
OPINION_NEGATIVE = ('反对', '不满', '失望', '差评', '争议', '问题', '质疑', '担忧', '不推荐')
SOCIAL_COMMENT_PATTERN = re.compile(r'^(?:热门评论|时间序列评论|热门评论回复|时间序列评论回复)[（:：]')
SOCIAL_COMMENT_DETAIL = re.compile(r'^(热门评论|时间序列评论)(回复)?（点赞\s*(\d+)(?:，回复\s*(\d+))?）：')
QUANTITY_PATTERN = re.compile(
    r'(?<![a-z0-9])(-?\d+(?:\.\d+)?)\s*(亿元|万元|亿美元|%|％|万|亿|元|秒|分钟|小时|天|年|月|日|人|条|次|个|名|s|ms)',
    re.I,
)
FACT_PREDICATES = (
    '上线',
    '发布',
    '开放',
    '支持',
    '提供',
    '包含',
    '允许',
    '启用',
    '完成',
    '取消',
    '停止',
    '属于',
    '采用',
)
NEGATION_PATTERN = re.compile(r'尚未|并未|没有|并非|不是|不可|不能|不再|未|不|无')
RESEARCH_FEED_BOUNDARY = re.compile(
    r'^(?:#|猜你想搜|相关推荐|相关(?:帖子|内容|资讯|搜索)|热门推荐|更多推荐|'
    r'前往论坛|去论坛|进入论坛|下载\s*(?:App|客户端)|打开\s*App)',
    re.I,
)
RESEARCH_FOOTER_BOUNDARY = re.compile(
    r'^(?:【?关于[^】]{0,30}】?|官方网站\s*[：:]|官方微博\s*[：:]|微信公众号\s*[：:]|'
    r'版权声明|免责声明|相关阅读)',
    re.I,
)
RESEARCH_PROMOTION_PATTERN = re.compile(
    r'(?:无需下载|即点即玩|一键登录|赶紧试试|畅玩|低配手机|不占空间|不耗电|'
    r'下载.*?(?:App|客户端)|云游戏.*?(?:官网|平台))',
    re.I,
)


def semantic_terms(value: str) -> set[str]:
    value = value.lower()
    terms = set(re.findall(r'[a-z0-9]{2,}', value))
    for chunk in re.findall(r'[\u4e00-\u9fff]+', value):
        if len(chunk) <= 4:
            terms.add(chunk)
        terms.update(chunk[index : index + 2] for index in range(max(0, len(chunk) - 1)))
    return {term for term in terms if term}


def research_goal_groups(goal: str) -> list[str]:
    """Compatibility name: literal lexical hints, NOT entities or required topics.

    Do not strip task words with a game-specific template or truncate later goal
    clauses. The authoritative question remains the complete user goal (Q0).
    """
    return sorted(semantic_terms(goal))


def research_goal_coverage(goal: str, title: str, text: str) -> dict[str, Any]:
    """Lexical overlap only; never a relevance, support or completion verdict."""
    groups = research_goal_groups(goal)
    normalized_title = re.sub(r'\s+', '', title).lower()
    normalized_text = re.sub(r'\s+', '', text).lower()
    details = []
    for group in groups:
        compact = re.sub(r'\s+', '', group).lower()
        if compact in normalized_title:
            score, location = 1.0, 'title'
        elif compact in normalized_text:
            score, location = 0.9, 'content'
        elif len(compact) >= 4:
            parts = {compact[index : index + 2] for index in range(len(compact) - 1)}
            coverage = len(
                {part for part in parts if part in normalized_title or part in normalized_text}
            ) / len(parts)
            score, location = round(coverage * 0.7, 3), 'partial'
        else:
            score, location = 0.0, 'missing'
        details.append({'group': group, 'score': score, 'location': location})
    score = sum(item['score'] for item in details) / max(1, len(details))
    return {
        'score': round(score, 3),
        'groups': details,
        'basis': 'literal_word_overlap_not_semantic_coverage',
        'semantic_status': 'not_assessed',
    }


def select_research_content(title: str, text: str, extraction: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Keep the complete extracted page and label candidate structural regions.

    The title, link ratio and boundary-looking words are not enough to identify
    one authoritative body. Do not erase directories, later sections or dissent
    on that basis, or manufacture source text from unlocated parser metadata.
    """
    original = text or ''
    access = extraction.get('content_access', {})
    if access.get('body_available') is False:
        return '', {
            'schema_version': 2,
            'method': 'unavailable-document-body',
            'original_characters': len(original),
            'selected_characters': 0,
            'original_preserved': True,
            'reason': access.get('message', '未取得目标正文'),
            'semantic_relevance': 'not_assessed',
        }
    structure = extraction.get('structure') if isinstance(extraction.get('structure'), dict) else {}
    headings = {
        str(section.get('heading') or '')
        for section in structure.get('sections', [])
        if isinstance(section, dict) and section.get('heading')
    }
    blocks = []
    section = ''
    for match in re.finditer(r'[^\n]*(?:\n|$)', original):
        if not match.group():
            continue
        line = match.group().strip()
        hints = []
        if line in headings:
            section = line
            hints.append('declared_section_heading')
        if RESEARCH_FEED_BOUNDARY.search(line):
            hints.append('feed_or_heading_candidate')
        if RESEARCH_FOOTER_BOUNDARY.search(line):
            hints.append('footer_or_section_candidate')
        if RESEARCH_PROMOTION_PATTERN.search(line):
            hints.append('promotion_candidate')
        blocks.append(
            {
                'id': f'region:{len(blocks) + 1}',
                'start': match.start(),
                'end': match.end(),
                'heading': section,
                'hints': hints,
                'disposition': 'retained',
                'semantic_role': 'not_assessed',
            }
        )
    from zhixi.content.text_transforms import apply_text_edits

    selected, transform = apply_text_edits(original, [])
    return selected, {
        'schema_version': 2,
        'method': 'all-extracted-regions-with-hints',
        'original_characters': len(original),
        'selected_characters': len(selected),
        'removed_characters': 0,
        'blocks': blocks,
        'transform': transform,
        'semantic_relevance': 'not_assessed',
        'unlocated_headings': sorted(headings - {block['heading'] for block in blocks}),
    }


def similarity(left: str, right: str) -> float:
    a, b = semantic_terms(left), semantic_terms(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def sentence_units(text: str) -> list[str]:
    compact = re.sub(r'[ \t]+', ' ', text)
    units: list[str] = []
    for raw in SENTENCE_SPLIT.split(compact):
        value = raw.strip(' \r\n-•·|，,')
        if 18 <= len(value) <= 600 and value not in units:
            units.append(value)
    return units


def evidence_fingerprint(quote: str) -> str:
    # Exact text identity. Case, whitespace, punctuation, operators and units
    # can change meaning; similarity belongs in a separate candidate relation.
    return hashlib.sha256(quote.encode('utf-8')).hexdigest()


def social_comment_metadata(quote: str) -> dict[str, Any]:
    match = SOCIAL_COMMENT_DETAIL.search(quote)
    if not match:
        return {}
    return {
        'sampling_strategy': 'hot' if match.group(1) == '热门评论' else 'recent',
        'is_reply': bool(match.group(2)),
        'likes': int(match.group(3)),
        'replies': int(match.group(4) or 0),
    }


def evidence_sentence_is_substantive(sentence: str, title: str = '') -> bool:
    value = sentence.strip()
    normalized = re.sub(r'[^a-z0-9\u4e00-\u9fff]', '', value.lower())
    normalized_title = re.sub(r'[^a-z0-9\u4e00-\u9fff]', '', title.lower())
    if not normalized or (normalized_title and len(normalized) >= 8 and normalized in normalized_title):
        return False
    if re.fullmatch(r'玩家\s+.{1,40}\s+发布时间\s+(?:19|20)\d{2}[-/].+', value):
        return False
    if value.count('#') >= 2 and len(value.split('#', 1)[0].strip()) < 20:
        return False
    if value.startswith('本文介绍') and re.search(r'欢迎.*(?:平台|交流|讨论)', value):
        return False
    if re.match(r'^(?:\d{1,2}月\d{1,2}日上线后，)?祝各位', value):
        return False
    return True


def evidence_matches_goal(goal: str, quote: str, evidence_type: str = '') -> bool:
    """Deprecated inclusion hook: retain nonempty context, not a match verdict.

    Omitted subjects, different terminology and counterexamples cannot safely be
    rejected by words in a standalone quote. Use versioned original-text review.
    """
    return bool(quote.strip())


def extract_evidence(
    page_id: str,
    title: str,
    text: str,
    goal: str,
    limit: int | None = None,
    *,
    source_context: dict[str, Any] | None = None,
    coverage: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    from zhixi.content.evidence_context import alignment, source_units

    source_context = source_context or {}
    platform = source_context.get('platform') or {}
    samples = platform.get('comment_samples') or []
    groups = research_goal_groups(goal)
    goal_terms = semantic_terms(goal)
    units = list(source_units(text))
    if samples:
        # Structured samples preserve separate comments with identical wording.
        units = [unit for unit in units if not unit['social']]
        for index, sample in enumerate(samples):
            if not isinstance(sample, dict) or not str(sample.get('text') or '').strip():
                continue
            label = '时间序列评论' if sample.get('sampling_strategy') == 'recent' else '热门评论'
            label += '回复' if sample.get('is_reply') else ''
            quote = (
                f'{label}（点赞 {sample.get("likes") or 0}'
                + ('' if sample.get('is_reply') else f'，回复 {sample.get("replies") or 0}')
                + f'）：{sample["text"]}'
            )
            position = text.find(quote)
            units.append(
                {
                    'text': quote,
                    'start': position if position >= 0 else None,
                    'end': position + len(quote) if position >= 0 else None,
                    'social': True,
                    'sample': sample,
                    'sample_index': index,
                }
            )
    ranked = []
    excluded = []
    seen = set()
    regions = sorted(
        (source_context.get('research_filter') or {}).get('blocks') or [], key=lambda item: item['start']
    )
    region_starts = [region['start'] for region in regions]
    for index, unit in enumerate(units):
        quote = unit['text']
        social = unit['social']
        sample = unit.get('sample') or {}
        reason = ''
        admission_hints = []
        if not evidence_sentence_is_substantive(quote, title):
            admission_hints.append('title_or_chrome_candidate')
        elif not social and len(quote) < 8:
            admission_hints.append('short_fragment_requires_context')
        elif social:
            message = re.split(r'[：:]', quote, maxsplit=1)[-1]
            informative = re.sub(r'〔表情〕|\[[^\]\n]{1,80}\]|[^a-z0-9\u4e00-\u9fff]', '', message.lower())
            if not informative:
                admission_hints.append('emote_requires_context')
        elif source_context.get('page', {}).get('kind') == 'social_video' and re.match(
            r'^(?:视频标题|BV号|视频时长|发布账号|发布时间戳|分P)[：:]', quote
        ):
            admission_hints.append('platform_metadata')
        occurrence = (
            {'quote': quote, 'comment_id': sample.get('comment_id')}
            if sample.get('comment_id')
            else {
                'quote': quote,
                'start': unit['start'],
                'end': unit['end'],
                'sample_index': unit.get('sample_index'),
            }
        )
        fingerprint = evidence_fingerprint(json.dumps(occurrence, ensure_ascii=False, sort_keys=True))
        if fingerprint in seen:
            reason = 'duplicate_fragment'
        if reason:
            excluded.append({'start': unit['start'], 'end': unit['end'], 'reason': reason})
            continue
        seen.add(fingerprint)
        parent = str(sample.get('parent_text') or '')
        relevance = alignment(groups, quote, title, parent_quote=parent)
        metadata = {
            **social_comment_metadata(quote),
            **{
                key: sample[key]
                for key in (
                    'comment_id',
                    'created_at_unix',
                    'likes',
                    'replies',
                    'sampling_strategy',
                    'is_reply',
                    'sample_page',
                    'sample_cursor',
                    'parent_comment_id',
                )
                if key in sample
            },
            'alignment': relevance,
            'fingerprint_policy': 'exact_text_and_source_occurrence_v2',
            'text_fingerprint': evidence_fingerprint(quote),
            'admission': {
                'status': 'retained_for_review',
                'hints': admission_hints,
                'semantic_role_verified': False,
            },
            'source_span': {'start': unit['start'], 'end': unit['end']},
            'context': {'page_title': title, 'parent_quote': parent},
        }
        if unit['start'] is not None:
            region_index = max(0, bisect_right(region_starts, unit['start']) - 1)
            matched_regions = []
            while region_index < len(regions) and regions[region_index]['start'] < unit['end']:
                region = regions[region_index]
                if unit['start'] < region['end']:
                    matched_regions.append(
                        {
                            'id': region['id'],
                            'heading': region.get('heading', ''),
                            'hints': region.get('hints', []),
                            'semantic_role': 'not_assessed',
                        }
                    )
                region_index += 1
            metadata['context']['page_regions'] = matched_regions
        if 'sample_index' in unit:
            metadata['source_record'] = {'field': 'platform.comment_samples', 'index': unit['sample_index']}
        if not social and unit['start'] is not None:
            metadata['context'].update(
                before=text[max(0, unit['start'] - 240) : unit['start']],
                after=text[unit['end'] : unit['end'] + 240],
            )
        positive = any(word in quote for word in OPINION_POSITIVE)
        negative = any(word in quote for word in OPINION_NEGATIVE)
        opinion = social or positive or negative or bool(re.search(r'认为|吐槽|评价|争议|担忧', quote))
        score = len(semantic_terms(quote) & goal_terms) / max(1, len(goal_terms))
        item = {
            'page_id': page_id,
            'topic': title[:160] or '页面内容',
            'claim': quote,
            'quote': quote,
            'type': 'opinion' if opinion else 'fact',
            'stance': 'mixed'
            if positive and negative
            else 'positive'
            if positive
            else 'negative'
            if negative
            else 'unclear'
            if opinion
            else 'neutral',
            'confidence': round(min(0.9, 0.5 + score * 0.3), 2),
            'fingerprint': fingerprint,
            'metadata': metadata,
        }
        ranked.append((score, -index, item))
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    items = [row[2] for row in ranked]
    if limit is not None and len(items) > limit:
        # Explicit caller budgets retain both sampling strategies before filling
        # the remainder by relevance. Default extraction has no top-k cutoff.
        selected = []
        for strategy in ('hot', 'recent'):
            selected.extend(
                [item for item in items if item['metadata'].get('sampling_strategy') == strategy][
                    : max(0, limit // 3)
                ]
            )
        selected_ids = {item['fingerprint'] for item in selected}
        selected.extend(item for item in items if item['fingerprint'] not in selected_ids)
        kept = selected[: max(0, limit)]
    else:
        kept = items
    if coverage is not None:
        coverage.update(
            {
                'unit_count': len(units),
                'retained_count': len(kept),
                'eligible_count': len(items),
                'excluded_count': len(excluded),
                'excluded': excluded,
                'deferred_fingerprints': [item['fingerprint'] for item in items if item not in kept],
                'explicit_limit': limit,
                'version': 2,
            }
        )
    return kept


def information_gain(text: str, previous_texts: Iterable[str]) -> float:
    """Legacy name for lexical novelty, not semantic gain or a stopping rule."""
    current = semantic_terms(text)
    if not current:
        return 0.0
    previous: set[str] = set()
    for value in previous_texts:
        previous.update(semantic_terms(value))
    return round(len(current - previous) / len(current), 3)


def near_duplicate_match(
    text: str, previous_pages: Iterable[dict[str, str]], threshold: float = 0.9
) -> dict[str, Any] | None:
    """Flag highly overlapping pages without deleting either source."""
    current_terms = semantic_terms(text)
    if len(current_terms) < 20:
        return None
    best: dict[str, Any] | None = None
    for page in previous_pages:
        previous_text = page.get('text', '')
        previous_terms = semantic_terms(previous_text)
        if len(previous_terms) < 20:
            continue
        length_ratio = min(len(text), len(previous_text)) / max(len(text), len(previous_text), 1)
        if length_ratio < 0.8:
            continue
        overlap = len(current_terms & previous_terms) / len(current_terms | previous_terms)
        if overlap >= threshold and (best is None or overlap > best['similarity']):
            best = {
                'page_id': page.get('id', ''),
                'similarity': round(overlap, 3),
                'length_ratio': round(length_ratio, 3),
            }
    return best


def duplicate_page_clusters(
    pages: dict[str, dict[str, Any]], overrides: Iterable[dict[str, Any]] = ()
) -> list[dict[str, Any]]:
    """Resolve exact and near-duplicate links into deterministic transitive project clusters."""
    parent = {page_id: page_id for page_id in pages}

    def find(page_id: str) -> str:
        while parent[page_id] != page_id:
            parent[page_id] = parent[parent[page_id]]
            page_id = parent[page_id]
        return page_id

    normalized_overrides = []
    for raw in overrides:
        left, right = sorted((str(raw.get('left_page_id') or ''), str(raw.get('right_page_id') or '')))
        decision = str(raw.get('decision') or '')
        if (
            left
            and right
            and left != right
            and left in pages
            and right in pages
            and decision in {'same', 'different'}
        ):
            normalized_overrides.append(
                {**raw, 'left_page_id': left, 'right_page_id': right, 'decision': decision}
            )
    forbidden = {
        (item['left_page_id'], item['right_page_id'])
        for item in normalized_overrides
        if item['decision'] == 'different'
    }

    def union(left: str, right: str) -> bool:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return True
        for forbidden_left, forbidden_right in forbidden:
            roots = {find(forbidden_left), find(forbidden_right)}
            if roots == {left_root, right_root}:
                return False
        parent[max(left_root, right_root)] = min(left_root, right_root)
        return True

    automatic_relationships = []
    for page_id, page in pages.items():
        extraction = page.get('extraction') if isinstance(page.get('extraction'), dict) else {}
        exact_target = str(extraction.get('duplicate_of') or '')
        near = extraction.get('near_duplicate') if isinstance(extraction.get('near_duplicate'), dict) else {}
        near_target = str(near.get('page_id') or '')
        target = exact_target or near_target
        if not target or target not in pages or target == page_id:
            continue
        relation = {'page_id': page_id, 'target_page_id': target, 'kind': 'exact' if exact_target else 'near'}
        if not exact_target:
            relation.update(
                similarity=float(near.get('similarity') or 0),
                length_ratio=float(near.get('length_ratio') or 0),
            )
        automatic_relationships.append(relation)

    relationships = []
    override_conflicts = []
    for item in normalized_overrides:
        if item['decision'] != 'same':
            continue
        if union(item['left_page_id'], item['right_page_id']):
            relationships.append(
                {
                    'page_id': item['left_page_id'],
                    'target_page_id': item['right_page_id'],
                    'kind': 'manual',
                    'reason': str(item.get('reason') or '人工确认相同内容'),
                }
            )
        else:
            override_conflicts.append(item)
    relation_priority = {'exact': 0, 'near': 1}
    for relation in sorted(
        automatic_relationships,
        key=lambda item: (
            relation_priority[item['kind']],
            -float(item.get('similarity') or 1),
            item['page_id'],
            item['target_page_id'],
        ),
    ):
        pair = tuple(sorted((relation['page_id'], relation['target_page_id'])))
        if pair in forbidden:
            continue
        if union(relation['page_id'], relation['target_page_id']):
            relationships.append(relation)

    components: dict[str, list[str]] = defaultdict(list)
    for page_id in pages:
        components[find(page_id)].append(page_id)
    clusters = []
    for member_ids in components.values():
        if len(member_ids) < 2:
            continue
        members = sorted(member_ids)
        directed_sources = {item['page_id'] for item in relationships if item['kind'] != 'manual'}
        terminal = [page_id for page_id in members if page_id not in directed_sources]
        representative = min(
            terminal or members,
            key=lambda page_id: (
                pages[page_id].get('status') != 'fetched',
                str(pages[page_id].get('created_at') or ''),
                page_id,
            ),
        )
        relations = [
            item for item in relationships if item['page_id'] in members and item['target_page_id'] in members
        ]
        hosts = {urlparse(str(pages[page_id].get('url') or '')).hostname for page_id in members}
        kinds = {item['kind'] for item in relations}
        similarities = [item['similarity'] for item in relations if item['kind'] == 'near']
        cluster_id = 'dup-' + hashlib.sha256('|'.join(members).encode('utf-8')).hexdigest()[:12]
        clusters.append(
            {
                'id': cluster_id,
                'representative_page_id': representative,
                'page_ids': members,
                'page_count': len(members),
                'relationship_count': len(relations),
                'kind': 'mixed' if len(kinds) > 1 else next(iter(kinds), 'near'),
                'exact_duplicate_count': sum(item['kind'] == 'exact' for item in relations),
                'near_duplicate_count': sum(item['kind'] == 'near' for item in relations),
                'manual_merge_count': sum(item['kind'] == 'manual' for item in relations),
                'host_count': len({host for host in hosts if host}),
                'cross_host': len({host for host in hosts if host}) > 1,
                'max_similarity': max(similarities) if similarities else None,
                'relationships': relations,
                'override_conflicts': [
                    item
                    for item in override_conflicts
                    if item['left_page_id'] in members or item['right_page_id'] in members
                ],
            }
        )
    return sorted(clusters, key=lambda item: (-item['page_count'], item['id']))


def claim_quantities(value: str) -> dict[str, set[str]]:
    aliases = {'％': '%', 's': '秒', 'ms': '毫秒'}
    quantities: dict[str, set[str]] = defaultdict(set)
    for number, unit in QUANTITY_PATTERN.findall(value):
        try:
            normalized_number = format(float(number), '.12g') if '.' in number else str(int(number))
        except ValueError:
            normalized_number = number
        normalized_unit = aliases.get(unit.lower(), unit)
        quantities[normalized_unit].add(normalized_number)
    return dict(quantities)


def quantity_context(value: str) -> str:
    return QUANTITY_PATTERN.sub(' 数值 ', value)


def predicate_assertions(value: str) -> list[dict[str, str]]:
    """Extract conservative predicate/polarity signatures for review checks."""

    def normalize(raw: str) -> str:
        raw = re.sub(r'(?:目前|当前|此前|曾经|已经|已然|仍然|仍|已经|已)', '', raw)
        raw = NEGATION_PATTERN.sub('', raw)
        return re.sub(r'[^a-z0-9\u4e00-\u9fff]+', '', raw.lower())

    predicate_positions = [
        match.start() for predicate in FACT_PREDICATES for match in re.finditer(re.escape(predicate), value)
    ]
    subject = normalize(value[: min(predicate_positions)]) if predicate_positions else ''
    assertions = []
    for predicate in FACT_PREDICATES:
        for match in re.finditer(re.escape(predicate), value):
            clause = re.split(r'[，。；;！？!?]|(?:但是|但)', value[: match.start()])[-1]
            negative = bool(NEGATION_PATTERN.search(clause[-30:]))
            following = re.split(r'[，。；;！？!?]|(?:但是|但)', value[match.end() :], maxsplit=1)[0]
            local_context = normalize(clause + predicate + following)
            if subject and not local_context.startswith(subject):
                local_context = subject + local_context
            assertions.append(
                {
                    'predicate': predicate,
                    'polarity': 'negative' if negative else 'positive',
                    'context': local_context,
                    'subject': subject,
                    'object': normalize(following),
                }
            )
    return assertions


def find_conflicts(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evidence:
        by_topic[item.get('topic', '')].append(item)
    for topic, items in by_topic.items():
        stances = {item.get('stance') for item in items if item.get('stance') not in {None, 'neutral'}}
        if 'positive' in stances and 'negative' in stances:
            conflicts.append(
                {
                    'topic': topic,
                    'kind': 'stance_divergence',
                    'message': '不同来源或片段对该主题存在正负立场分歧。',
                    'evidence_ids': [
                        item['id'] for item in items if item.get('stance') in {'positive', 'negative'}
                    ],
                }
            )
    opinions = [item for item in evidence if item.get('stance') in {'positive', 'negative'}]
    known_pairs: set[tuple[str, str]] = set()
    for index, left in enumerate(opinions):
        for right in opinions[index + 1 :]:
            if left.get('stance') == right.get('stance') or left.get('page_id') == right.get('page_id'):
                continue
            pair = tuple(sorted((left['id'], right['id'])))
            if pair in known_pairs or similarity(left.get('claim', ''), right.get('claim', '')) < 0.1:
                continue
            known_pairs.add(pair)
            conflicts.append(
                {
                    'topic': left.get('topic') or right.get('topic') or '相关观点',
                    'kind': 'cross_source_stance_divergence',
                    'message': '语义相关的跨来源观点存在正负立场分歧。',
                    'evidence_ids': list(pair),
                }
            )
    facts = [item for item in evidence if item.get('type', item.get('evidence_type')) == 'fact']
    numeric_pairs: set[tuple[str, str, str]] = set()
    for index, left in enumerate(facts):
        left_quantities = claim_quantities(left.get('claim', ''))
        if not left_quantities:
            continue
        for right in facts[index + 1 :]:
            if left.get('page_id') == right.get('page_id'):
                continue
            right_quantities = claim_quantities(right.get('claim', ''))
            shared_units = set(left_quantities) & set(right_quantities)
            mismatched = [
                unit for unit in shared_units if left_quantities[unit].isdisjoint(right_quantities[unit])
            ]
            context_score = similarity(
                quantity_context(left.get('claim', '')), quantity_context(right.get('claim', ''))
            )
            for unit in mismatched:
                pair = tuple(sorted((str(left.get('id', '')), str(right.get('id', ''))))) + (unit,)
                if pair in numeric_pairs or context_score < 0.28:
                    continue
                numeric_pairs.add(pair)
                conflicts.append(
                    {
                        'topic': left.get('topic') or right.get('topic') or '数值事实',
                        'kind': 'numeric_claim_mismatch',
                        'severity': 'needs_review',
                        'message': f'语义相近的跨页面事实使用了不同的“{unit}”数值，可能是口径、时间或事实差异，需回查原文。',
                        'unit': unit,
                        'left_values': sorted(left_quantities[unit]),
                        'right_values': sorted(right_quantities[unit]),
                        'context_similarity': round(context_score, 3),
                        'evidence_ids': list(pair[:2]),
                    }
                )
    predicate_pairs: set[tuple[str, str, str]] = set()
    for index, left in enumerate(facts):
        left_assertions = predicate_assertions(left.get('claim', ''))
        for right in facts[index + 1 :]:
            if left.get('page_id') == right.get('page_id'):
                continue
            right_assertions = predicate_assertions(right.get('claim', ''))
            for left_assertion in left_assertions:
                for right_assertion in right_assertions:
                    if (
                        left_assertion['predicate'] != right_assertion['predicate']
                        or left_assertion['polarity'] == right_assertion['polarity']
                    ):
                        continue
                    left_object, right_object = (
                        left_assertion.get('object', ''),
                        right_assertion.get('object', ''),
                    )
                    if left_object and right_object and similarity(left_object, right_object) < 0.5:
                        continue
                    context_score = similarity(left_assertion['context'], right_assertion['context'])
                    if (
                        len(left_assertion.get('subject', '')) >= 4
                        and len(right_assertion.get('subject', '')) >= 4
                    ):
                        context_score = max(
                            context_score, similarity(left_assertion['subject'], right_assertion['subject'])
                        )
                    pair = tuple(sorted((str(left.get('id', '')), str(right.get('id', ''))))) + (
                        left_assertion['predicate'],
                    )
                    if pair in predicate_pairs or context_score < 0.58:
                        continue
                    predicate_pairs.add(pair)
                    conflicts.append(
                        {
                            'topic': left.get('topic') or right.get('topic') or '事实状态',
                            'kind': 'factual_polarity_mismatch',
                            'severity': 'needs_review',
                            'predicate': left_assertion['predicate'],
                            'message': f'跨页面对同一语境中的“{left_assertion["predicate"]}”给出相反肯定/否定表述，可能源于时间、版本或事实差异，需回查原文。',
                            'context_similarity': round(context_score, 3),
                            'evidence_ids': list(pair[:2]),
                        }
                    )
    return conflicts


def attach_citations(tree: dict[str, Any], evidence: list[dict[str, Any]], max_per_node: int = 3) -> None:
    """Bind direct source excerpts; keep semantic matches as unverified candidates."""
    from zhixi.reports.research_report import bind_node_support

    def visit(node: dict[str, Any]) -> None:
        bind_node_support(node, evidence, similarity, max_per_node)
        for child in node.get('children', []) if isinstance(node.get('children'), list) else []:
            if isinstance(child, dict):
                visit(child)

    visit(tree)


def validate_research_report(result: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    from zhixi.reports.research_report import assemble_report

    def source_timeline(items: list[dict[str, Any]]) -> list[dict[str, str]]:
        rows = []
        seen = set()
        for item in items:
            for row in extract_timeline(str(item.get('quote', ''))):
                key = (row['date'], row['event'])
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
        return rows

    return assemble_report(result, evidence, similarity, source_timeline)


def citation_metrics(tree: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, float | int]:
    valid_ids = {str(item.get('id', '')) for item in evidence if item.get('id')}
    cited: set[str] = set()
    invalid: set[str] = set()
    substantive_nodes = 0
    cited_nodes = 0

    def visit(node: Any) -> None:
        nonlocal substantive_nodes, cited_nodes
        if not isinstance(node, dict):
            return
        citations = {str(item) for item in node.get('citations', []) if str(item) in valid_ids}
        invalid.update(str(item) for item in node.get('citations', []) if str(item) not in valid_ids)
        cited.update(citations)
        if str(node.get('note', '')).strip():
            substantive_nodes += 1
            if citations:
                cited_nodes += 1
        for child in node.get('children', []) if isinstance(node.get('children'), list) else []:
            visit(child)

    visit(tree)
    return {
        'valid_citations': len(cited),
        'invalid_citations': len(invalid),
        'substantive_nodes': substantive_nodes,
        'cited_nodes': cited_nodes,
        'node_coverage': round(cited_nodes / max(1, substantive_nodes), 3),
        'evidence_utilization': round(len(cited) / max(1, len(valid_ids)), 3),
    }


def source_reliability(
    pages: list[dict[str, Any]], goal: str = '', duplicate_overrides: Iterable[dict[str, Any]] = ()
) -> tuple[dict[str, Any], list[str]]:
    """Describe source diversity and sampling boundaries without pretending to score truth."""
    hosts = {urlparse(str(page.get('url', ''))).hostname for page in pages if page.get('url')}
    types: dict[str, int] = defaultdict(int)
    roles: dict[str, int] = defaultdict(int)
    social_samples: list[dict[str, Any]] = []
    near_duplicates = 0
    identity_signals = {
        'declared_publisher': 0,
        'declared_site_name': 0,
        'same_host_canonical': 0,
        'identity_declared': 0,
        'verified_first_party': 0,
    }
    publication_dates: list[str] = []
    undated_sources = 0
    for page in pages:
        types[str(page.get('page_type') or 'unknown')] += 1
        extraction = page.get('extraction') if isinstance(page.get('extraction'), dict) else {}
        planning = extraction.get('planning') if isinstance(extraction.get('planning'), dict) else {}
        roles[str(planning.get('source_role') or 'unclassified')] += 1
        near_duplicates += int(isinstance(extraction.get('near_duplicate'), dict))
        identity = (
            extraction.get('source_identity') if isinstance(extraction.get('source_identity'), dict) else {}
        )
        identity_signals['declared_publisher'] += int(bool(identity.get('publisher')))
        identity_signals['declared_site_name'] += int(bool(identity.get('site_name')))
        identity_signals['same_host_canonical'] += int(bool(identity.get('canonical_same_host')))
        identity_signals['identity_declared'] += int(
            identity.get('first_party_status') == 'identity_declared'
        )
        identity_signals['verified_first_party'] += int(identity.get('first_party_status') == 'verified')
        metadata = extraction.get('metadata') if isinstance(extraction.get('metadata'), dict) else {}
        from zhixi.content.source_identity import source_context

        context = source_context(page)
        published = str(context.get('published_at') or (
            metadata.get('published_at') if not context.get('conflicting_published_at') else ''
        ) or '')
        platform = extraction.get('platform') if isinstance(extraction.get('platform'), dict) else {}
        if not published and platform.get('published_at_unix'):
            try:
                published = (
                    dt.datetime.fromtimestamp(int(platform['published_at_unix']), dt.timezone.utc)
                    .date()
                    .isoformat()
                )
            except (ValueError, TypeError, OverflowError, OSError):
                published = ''
        normalized_date = re.search(r'(?:19|20)\d{2}(?:-\d{1,2})?(?:-\d{1,2})?', published)
        if normalized_date:
            publication_dates.append(normalized_date.group(0))
        else:
            undated_sources += 1
        if platform:
            sampling = platform.get('sampling') if isinstance(platform.get('sampling'), dict) else {}
            strategies = sampling.get('strategies') if isinstance(sampling.get('strategies'), dict) else {}
            social_samples.append(
                {
                    'comments_sampled': int(platform.get('comments_sampled') or 0),
                    'comments_total': int(platform.get('comments_total') or 0),
                    'pages_requested': sum(
                        int(item.get('pages_requested') or 0)
                        for item in strategies.values()
                        if isinstance(item, dict)
                    ),
                    'pages_succeeded': sum(
                        int(item.get('pages_succeeded') or 0)
                        for item in strategies.values()
                        if isinstance(item, dict)
                    ),
                    'stop_reasons': {
                        name: str(item.get('stop_reason') or 'unknown')
                        for name, item in strategies.items()
                        if isinstance(item, dict)
                    },
                }
            )
    pages_by_id = {str(page.get('id') or f'source-{index}'): page for index, page in enumerate(pages)}
    duplicate_clusters = duplicate_page_clusters(pages_by_id, duplicate_overrides)
    clustered_duplicate_pages = sum(item['page_count'] - 1 for item in duplicate_clusters)
    metrics = {
        'source_count': len(pages),
        'host_count': len({host for host in hosts if host}),
        'page_types': dict(types),
        'source_roles': dict(roles),
        'social_samples': social_samples,
        'near_duplicate_candidates': near_duplicates,
        'identity_signals': identity_signals,
        'duplicate_cluster_count': len(duplicate_clusters),
        'duplicate_cluster_pages': sum(item['page_count'] for item in duplicate_clusters),
        'independent_source_count': len(pages) - clustered_duplicate_pages,
        'temporal_coverage': {
            'dated_sources': len(publication_dates),
            'undated_sources': undated_sources,
            'oldest_published_at': min(publication_dates) if publication_dates else None,
            'newest_published_at': max(publication_dates) if publication_dates else None,
            'assessed_at': dt.datetime.now(dt.timezone.utc).date().isoformat(),
        },
    }
    limitations = ['结论仅代表当前已获取的公开页面。']
    if len(pages) == 1:
        limitations.append('当前仅有 1 个页面来源，不能代表整个站点或全部用户群体。')
    if len(hosts) == 1:
        limitations.append('当前来源集中于 1 个站点，尚未完成跨来源印证。')
    if roles and not any(role in roles for role in {'reference', 'news', 'primary'}):
        limitations.append('当前缺少资料、新闻或已核验第一方来源，事实背景仍需补充。')
    if roles.get('official_lead'):
        limitations.append('“官方线索”仅表示由官方检索路径发现，尚未自动证明该页面属于第一方来源。')
    if duplicate_clusters:
        limitations.append(
            f'发现 {len(duplicate_clusters)} 个重复内容簇，涉及 {sum(item["page_count"] for item in duplicate_clusters)} 个页面；'
            f'报告按 {len(pages) - clustered_duplicate_pages} 个独立来源单元计数，所有页面与正文仍保留。'
        )
    if re.search(r'最新|当前|目前|近期|现状|截至|today|latest|current', goal, re.I):
        if not publication_dates:
            limitations.append(
                '研究目标具有时间敏感性，但当前来源均未提取到可靠发布日期，不能证明结论代表现状。'
            )
        elif undated_sources:
            limitations.append(
                f'研究目标具有时间敏感性；{undated_sources} 个来源缺少可靠发布日期，需谨慎解释“最新/当前”。'
            )
    if social_samples:
        sampled = sum(item['comments_sampled'] for item in social_samples)
        total = sum(item['comments_total'] for item in social_samples)
        succeeded = sum(item['pages_succeeded'] for item in social_samples)
        requested = sum(item['pages_requested'] for item in social_samples)
        page_note = f'，成功读取 {succeeded}/{requested} 个采样页' if requested else ''
        limitations.append(
            f'社交平台共采样 {sampled} 条公开评论（页面显示评论总量约 {total}{page_note}）；'
            '热门与时间序列均为有界样本，仍存在排序、时间窗口与幸存者偏差。'
        )
        stop_reasons = {reason for item in social_samples for reason in item['stop_reasons'].values()}
        if 'page_budget' in stop_reasons:
            limitations.append('部分评论序列因达到采样页预算而停止；这是一项资源边界，不表示已读完全部评论。')
        if 'request_failed' in stop_reasons:
            limitations.append('部分评论后续页请求失败；此前成功页面已保留，缺失区间没有被当作空评论。')
        if 'api_rejected' in stop_reasons:
            limitations.append('平台接口拒绝了部分评论序列，当前观点样本可能只覆盖另一种排序策略。')
        if 'cursor_stalled' in stop_reasons:
            limitations.append('部分评论序列的分页游标未前进，系统为避免重复请求而停止。')
    limitations.append('自动观点分类可能无法完全识别反讽、玩梗或隐含语境。')
    return metrics, limitations


def corroboration_clusters(
    evidence: list[dict[str, Any]],
    pages: dict[str, dict[str, Any]],
    duplicate_overrides: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Group lexical candidates and deduplicated pages, never infer support."""
    clusters: list[dict[str, Any]] = []
    for item in evidence:
        claim = str(item.get('claim') or item.get('quote') or '').strip()
        if not claim:
            continue
        best = None
        best_score = 0.0
        for cluster in clusters:
            score = similarity(claim, cluster['representative_claim'])
            if score > best_score:
                best, best_score = cluster, score
        if best is None or best_score < 0.24:
            best = {
                'representative_claim': claim,
                'evidence_ids': [],
                'page_ids': [],
                'stances': [],
                'fingerprints': [],
            }
            clusters.append(best)
        best['evidence_ids'].append(item.get('id'))
        best['page_ids'].append(item.get('page_id'))
        best['stances'].append(item.get('stance', 'neutral'))
        best['fingerprints'].append(item.get('fingerprint') or evidence_fingerprint(claim))

    duplicate_clusters = duplicate_page_clusters(pages, duplicate_overrides)
    duplicate_cluster_by_page = {
        page_id: cluster['id'] for cluster in duplicate_clusters for page_id in cluster['page_ids']
    }
    output = []
    for cluster in clusters:
        page_ids = list(dict.fromkeys(value for value in cluster.pop('page_ids') if value))
        hosts = {urlparse(str(pages.get(page_id, {}).get('url', ''))).hostname for page_id in page_ids}
        non_neutral = {value for value in cluster.pop('stances') if value not in {None, 'neutral'}}
        fingerprints = {value for value in cluster.pop('fingerprints') if value}
        independent_page_ids = []
        seen_duplicate_clusters = set()
        for page_id in page_ids:
            duplicate_cluster_id = duplicate_cluster_by_page.get(page_id)
            if duplicate_cluster_id and duplicate_cluster_id in seen_duplicate_clusters:
                continue
            if duplicate_cluster_id:
                seen_duplicate_clusters.add(duplicate_cluster_id)
            independent_page_ids.append(page_id)
        independent_hosts = {
            urlparse(str(pages.get(page_id, {}).get('url', ''))).hostname for page_id in independent_page_ids
        }
        source_count = len(page_ids)
        host_count = len({host for host in hosts if host})
        independent_source_count = len(independent_page_ids)
        independent_host_count = len({host for host in independent_hosts if host})
        repeated = source_count >= 2 and len(fingerprints) == 1
        if repeated:
            assessment = 'cross_site_repeated_text' if host_count >= 2 else 'same_site_repeated_text'
        elif independent_host_count >= 2:
            assessment = 'cross_site_candidates'
        elif independent_source_count >= 2:
            assessment = 'multi_page_candidates'
        elif source_count >= 2:
            assessment = 'near_duplicate_material'
        else:
            assessment = 'single_source'
        output.append(
            {
                **cluster,
                'source_count': source_count,
                'host_count': host_count,
                'independent_source_count': independent_source_count,
                'independent_host_count': independent_host_count,
                'duplicate_cluster_count': len(seen_duplicate_clusters),
                'assessment': assessment,
                'support_status': 'not_assessed',
                'grouping_basis': 'lexical_similarity',
                'stance_divergence': 'positive' in non_neutral and 'negative' in non_neutral,
            }
        )
    return sorted(
        output,
        key=lambda item: (
            item['independent_host_count'],
            item['independent_source_count'],
            item['source_count'],
            len(item['evidence_ids']),
        ),
        reverse=True,
    )


def evidence_corpus(evidence: list[dict[str, Any]], pages: dict[str, dict[str, str]]) -> str:
    blocks = []
    for item in evidence:
        page = pages.get(item['page_id'], {})
        metadata = item.get('metadata') if isinstance(item.get('metadata'), dict) else {}
        engagement = (
            (
                f'\n采样：{metadata.get("sampling_strategy")}；点赞：{metadata.get("likes", 0)}；'
                f'回复：{metadata.get("replies", 0)}'
            )
            if metadata.get('sampling_strategy')
            else ''
        )
        context = metadata.get('context') or {}
        attribution = metadata.get('alignment') or {}
        context_line = (
            (
                '\n上下文线索：'
                + json.dumps({'归属检查': attribution, '相邻原文': context}, ensure_ascii=False)
            )
            if attribution or context
            else ''
        )
        blocks.append(
            f'[证据 {item["id"]}]\n来源标题：{page.get("title", "")}\n来源网址：{page.get("url", "")}\n'
            f'类型：{item.get("type", "fact")}；立场：{item.get("stance", "neutral")}{engagement}{context_line}\n原文：{item["quote"]}'
        )
    return (
        '研究目标及经过清洗、去重的原文证据如下。只能依据这些证据进行综合，并保留分歧与不确定性。\n\n'
        + '\n\n'.join(blocks)
    )


def pack_evidence_corpus(
    evidence: list[dict[str, Any]],
    pages: dict[str, dict[str, str]],
    goal: str,
    character_budget: int,
    *,
    preserve_order: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Pack whole source blocks and expose every deferred item; never slice a quote."""
    from zhixi.content.evidence_transport import encode_bundle

    included: list[dict[str, Any]] = []
    deferred = []
    for index, item in enumerate(evidence):
        candidate = encode_bundle([*included, item], pages, goal)
        if len(candidate) <= character_budget:
            included.append(item)
        else:
            if preserve_order:
                # Whole-document reading must not skip a long condition/code
                # line to fill the remaining space with later small fragments.
                deferred.extend(str(row['id']) for row in evidence[index:])
                break
            deferred.append(str(item['id']))
    if not included:
        raise ValueError('输入预算不足以容纳一条完整证据，请提高预算或检查来源内容')
    corpus = encode_bundle(included, pages, goal)
    return corpus, {
        'total_count': len(evidence),
        'included_count': len(included),
        'included_evidence_ids': [str(item['id']) for item in included],
        'deferred_evidence_ids': deferred,
        'characters': len(corpus),
        'character_budget': character_budget,
        'transport': json.loads(corpus)['format'],
    }
