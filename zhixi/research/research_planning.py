from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable
from urllib.parse import urlparse


SOCIAL_HOSTS = ('bilibili.com', 'weibo.com', 'douyin.com', 'youtube.com', 'xiaohongshu.com')
COMMUNITY_HOSTS = ('tieba.baidu.com', 'zhihu.com', 'reddit.com', 'nga.cn', 'fandom.com')
NEWS_HOST_MARKERS = ('news', 'xinhuanet', 'people.com.cn', 'chinanews', 'thepaper', 'reuters', 'apnews')
REFERENCE_MARKERS = ('wiki', 'wikipedia', 'baike.baidu.com', '百科')


def host_key(url: str) -> str:
    """Host diversity is not a claim about ownership or independent evidence."""
    host = (urlparse(url).hostname or '').lower().removeprefix('www.')
    return host.rstrip('.')


def classify_source_role(url: str, title: str = '', discovery_role: str = '') -> str:
    """Assign an explainable source function, not an ungrounded credibility score."""
    host = (urlparse(url).hostname or '').lower()
    haystack = f'{host} {title}'.lower()
    if any(host == item or host.endswith(f'.{item}') for item in SOCIAL_HOSTS):
        return 'social'
    if any(marker in haystack for marker in REFERENCE_MARKERS):
        return 'reference'
    if (
        host.startswith(('bbs.', 'forum.'))
        or any(host == item or host.endswith(f'.{item}') for item in COMMUNITY_HOSTS)
        or re.search(r'论坛|社区|讨论区|贴吧', title, re.I)
    ):
        return 'community'
    if any(marker in haystack for marker in NEWS_HOST_MARKERS) or re.search(r'新闻|报道|资讯', title, re.I):
        return 'news'
    if discovery_role == 'official_candidate' or re.search(r'官网|官方公告|官方资料', title, re.I):
        return 'official_lead'
    return 'general'


def classify_topic_bucket(title: str, url: str = '') -> str:
    """Compatibility entry point; a title does not establish research coverage.

    The former game taxonomy assigned unrelated material to characters/stages.
    Until a question-linked topic plan exists, retain an explicit unknown state.
    """
    return 'unclassified'


def candidate_topic(item):
    if item.get('topic_basis') in {'user', 'model_question'} and isinstance(item.get('topic_bucket'), str):
        return item['topic_bucket'].strip() or 'unclassified'
    return 'unclassified'


def expected_candidate_gain(
    relevance: float,
    role: str,
    host: str,
    topic: str,
    seen_roles: Counter[str],
    seen_hosts: Counter[str],
    seen_topics: Counter[str],
) -> tuple[float, str]:
    """Estimate pre-fetch value from relevance and source diversity.

    This is a ranking heuristic, not expected semantic information gain.
    The post-fetch word novelty ratio is likewise not a count of new facts.
    """
    role_bonus = 0.14 if not seen_roles[role] else max(-0.12, 0.04 - 0.025 * seen_roles[role])
    host_bonus = 0.18 if not seen_hosts[host] else max(-0.22, -0.055 * seen_hosts[host])
    topic_bonus = (
        0
        if topic == 'unclassified'
        else 0.1
        if not seen_topics[topic]
        else max(-0.1, 0.02 - 0.025 * seen_topics[topic])
    )
    expected = max(0.0, min(1.0, relevance * 0.68 + 0.14 + role_bonus + host_bonus + topic_bonus))
    reasons = [f'文本相关线索 {relevance:.2f}', f'来源角色线索 {role}']
    reasons.append('新增站点' if not seen_hosts[host] else f'该站点已有 {seen_hosts[host]} 页')
    reasons.append('补充来源类型' if not seen_roles[role] else f'该类型已有 {seen_roles[role]} 页')
    reasons.append(
        '尚未按研究问题分类，不计主题加权'
        if topic == 'unclassified'
        else '补充已指定主题'
        if not seen_topics[topic]
        else f'该指定主题已有 {seen_topics[topic]} 页'
    )
    return round(expected, 3), '；'.join(reasons)


def assess_candidates(
    candidates: Iterable[dict[str, Any]], observed: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    seen_roles: Counter[str] = Counter()
    seen_hosts: Counter[str] = Counter()
    seen_topics: Counter[str] = Counter()
    for item in observed:
        role = item.get('source_role') or classify_source_role(item.get('url', ''), item.get('title', ''))
        seen_roles[role] += 1
        seen_hosts[host_key(item.get('url', ''))] += 1
        seen_topics[candidate_topic(item)] += 1
    assessed = []
    for item in candidates:
        copy = dict(item)
        label = copy.get('title') or copy.get('link_text') or ''
        role = copy.get('source_role') or classify_source_role(copy.get('url', ''), label)
        topic = candidate_topic(copy)
        expected, reason = expected_candidate_gain(
            float(copy.get('relevance') or 0),
            role,
            host_key(copy.get('url', '')),
            topic,
            seen_roles,
            seen_hosts,
            seen_topics,
        )
        copy.update(
            source_role=role,
            topic_bucket=topic,
            expected_gain=expected,
            planning_reason=reason,
            ranking_basis='text_role_host_hints_not_semantic_coverage',
            topic_basis=copy.get('topic_basis') if topic != 'unclassified' else 'unclassified',
        )
        assessed.append(copy)
    return sorted(assessed, key=lambda item: (item['expected_gain'], item.get('relevance', 0)), reverse=True)


def diversify_discovery(
    results: Iterable[dict[str, Any]], goal: str, relevance_fn, limit: int
) -> list[dict[str, Any]]:
    """Greedily select search results while accounting for role/host saturation."""
    remaining = []
    for result in results:
        item = dict(result)
        item['relevance'] = relevance_fn(goal, item.get('title', ''), item['url'])
        item['source_role'] = classify_source_role(
            item['url'], item.get('title', ''), item.get('query_role', '')
        )
        remaining.append(item)
    chosen: list[dict[str, Any]] = []
    while remaining and len(chosen) < limit:
        assessed = assess_candidates(remaining, chosen)
        pick = assessed[0]
        chosen.append(pick)
        remaining = [item for item in remaining if item['url'] != pick['url']]
    return chosen
