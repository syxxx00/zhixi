from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlparse

import httpx

from zhixi.content.content_pipeline import structure_for_model
from zhixi.content.text_cleaning import clean_source_text
from zhixi.sources.bilibili_subtitles import read_captions


FetchResult = tuple[str, str, str, str, dict]
BILIBILI_COMMENT_PAGE_LIMIT = 3
BILIBILI_COMMENT_PAGE_SIZE = 20


class Matcher(Protocol):
    def __call__(self, url: str) -> str: ...


class Fetcher(Protocol):
    def __call__(self, url: str, identity: str) -> FetchResult: ...


@dataclass(frozen=True)
class PlatformAdapter:
    name: str
    match: Matcher
    fetch: Fetcher
    anonymous_public_access: bool = False


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: list[PlatformAdapter] = []

    def register(self, adapter: PlatformAdapter) -> None:
        if any(item.name == adapter.name for item in self._adapters):
            raise ValueError(f'平台适配器名称重复：{adapter.name}')
        self._adapters.append(adapter)

    def names(self) -> list[str]:
        return [item.name for item in self._adapters]

    def replace_group(self, prefix: str, adapters: list[PlatformAdapter]) -> None:
        """Atomically replace one named adapter group after validating the new set."""
        names = [item.name for item in adapters]
        if any(not name.startswith(prefix) for name in names):
            raise ValueError(f'适配器组名称必须以 {prefix} 开头')
        if len(names) != len(set(names)):
            raise ValueError('配置中存在重复的平台适配器名称')
        retained = [item for item in self._adapters if not item.name.startswith(prefix)]
        retained_names = {item.name for item in retained}
        if retained_names.intersection(names):
            raise ValueError('配置的平台适配器与内置名称冲突')
        self._adapters = [*retained, *adapters]

    def fetch(self, url: str, validate_url: Callable[[str], None]) -> FetchResult | None:
        for adapter in self._adapters:
            identity = adapter.match(url)
            if identity:
                validate_url(url)
                result = adapter.fetch(url, identity)
                if adapter.anonymous_public_access:
                    result[4]['retrieval_access'] = 'anonymous_public_http'
                return result
        return None


def bilibili_video_id(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname not in {'bilibili.com', 'www.bilibili.com', 'm.bilibili.com'}:
        return ''
    match = re.search(r'/video/(BV[a-zA-Z0-9]+|av[0-9]+)(?=/|$)', parsed.path, re.I)
    return match.group(1) if match else ''


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def sanitize_bilibili_comment(content: Any) -> str:
    if not isinstance(content, dict):
        return ''
    message = str(content.get('message') or '').strip()
    emotes = content.get('emote') if isinstance(content.get('emote'), dict) else {}
    for token in sorted((str(key) for key in emotes), key=len, reverse=True):
        if token:
            message = message.replace(token, '〔表情〕')
    # Some public replies omit the emote dictionary while retaining Bilibili's
    # year-prefixed custom-emote token. Restrict fallback cleaning to that shape.
    message = re.sub(r'\[20\d{2}[^\]\n]{1,80}_[^\]\n]{1,30}\]', '〔表情〕', message)
    message = re.sub(r'(?<![\w@])@[^\s:：，。！？,!！?？\[\]]{1,40}', '@用户', message)
    # A multi-line reply is still one comment.  Keeping its line break made the
    # evidence splitter misclassify the continuation as an unattributed fact.
    return re.sub(r'\s+', ' ', message).strip()


def bilibili_display_title(value: Any, bvid: str) -> str:
    title = str(value or '').strip()
    if not title or re.search(r'(?i)(?:^studio_video_|\.(?:mp4|flv|m4s|webm)$)', title):
        return f'B站视频 {bvid}'
    return title


def sample_bilibili_comments(
    client: Any,
    aid: Any,
    *,
    page_limit: int = BILIBILI_COMMENT_PAGE_LIMIT,
    page_size: int = BILIBILI_COMMENT_PAGE_SIZE,
) -> dict[str, Any]:
    """Collect bounded hot/recent pages while preserving partial results and coverage."""
    lines: list[str] = []
    samples: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    total_comments = 0
    coverage: dict[str, dict[str, Any]] = {}
    page_limit = max(1, min(int(page_limit), 10))
    page_size = max(1, min(int(page_size), 20))

    for mode, label, strategy in ((3, '热门评论', 'hot'), (2, '时间序列评论', 'recent')):
        cursor: Any = 0
        details: dict[str, Any] = {
            'pages_requested': 0,
            'pages_succeeded': 0,
            'top_level_seen': 0,
            'samples_kept': 0,
            'replies_kept': 0,
            'duplicates_skipped': 0,
            'stop_reason': 'page_budget',
            'next_cursor_available': False,
        }
        coverage[strategy] = details
        for page_index in range(page_limit):
            details['pages_requested'] += 1
            try:
                response = client.get(
                    'https://api.bilibili.com/x/v2/reply/main',
                    params={
                        'type': 1,
                        'oid': aid,
                        'mode': mode,
                        'next': cursor,
                        'ps': page_size,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                details['stop_reason'] = 'request_failed'
                warnings.append(
                    f'{label}第 {page_index + 1} 页读取失败，已保留此前样本：{type(exc).__name__}'
                )
                break
            if payload.get('code') != 0:
                details['stop_reason'] = 'api_rejected'
                warnings.append(f'{label}接口不可用：{payload.get("message", "未知原因")}')
                break
            data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
            cursor_data = data.get('cursor') if isinstance(data.get('cursor'), dict) else {}
            replies = data.get('replies') if isinstance(data.get('replies'), list) else []
            details['pages_succeeded'] += 1
            details['top_level_seen'] += len(replies)
            total_comments = max(total_comments, _integer(cursor_data.get('all_count')))
            if not replies:
                details['stop_reason'] = 'empty_page'
                break
            for reply in replies:
                if not isinstance(reply, dict):
                    continue
                reply_id = str(reply.get('rpid') or '')
                if reply_id and reply_id in seen_ids:
                    details['duplicates_skipped'] += 1
                else:
                    if reply_id:
                        seen_ids.add(reply_id)
                    message = sanitize_bilibili_comment(reply.get('content'))
                    if message:
                        lines.append(
                            f'{label}（点赞 {_integer(reply.get("like"))}，回复 {_integer(reply.get("rcount"))}）：{message}'
                        )
                        samples.append(
                            {
                                'comment_id': reply_id,
                                'sampling_strategy': strategy,
                                'is_reply': False,
                                'sample_page': page_index + 1,
                                'sample_cursor': str(cursor),
                                'created_at_unix': _integer(reply.get('ctime')),
                                'likes': _integer(reply.get('like')),
                                'replies': _integer(reply.get('rcount')),
                                'text': message,
                            }
                        )
                        details['samples_kept'] += 1
                for child in (reply.get('replies') or [])[:2]:
                    if not isinstance(child, dict):
                        continue
                    child_id = str(child.get('rpid') or '')
                    if child_id and child_id in seen_ids:
                        details['duplicates_skipped'] += 1
                        continue
                    if child_id:
                        seen_ids.add(child_id)
                    child_message = sanitize_bilibili_comment(child.get('content'))
                    if child_message:
                        lines.append(f'{label}回复（点赞 {_integer(child.get("like"))}）：{child_message}')
                        samples.append(
                            {
                                'comment_id': child_id,
                                'sampling_strategy': strategy,
                                'is_reply': True,
                                'parent_comment_id': reply_id,
                                'parent_text': sanitize_bilibili_comment(reply.get('content')),
                                'sample_page': page_index + 1,
                                'sample_cursor': str(cursor),
                                'created_at_unix': _integer(child.get('ctime')),
                                'likes': _integer(child.get('like')),
                                'replies': _integer(child.get('rcount')),
                                'text': child_message,
                            }
                        )
                        details['samples_kept'] += 1
                        details['replies_kept'] += 1
            is_end = bool(cursor_data.get('is_end'))
            next_cursor = cursor_data.get('next')
            details['next_cursor_available'] = not is_end and next_cursor not in {None, '', cursor}
            if is_end:
                details['stop_reason'] = 'api_end'
                break
            if next_cursor in {None, '', cursor}:
                details['stop_reason'] = 'cursor_stalled'
                warnings.append(f'{label}分页游标没有前进，已停止以避免重复请求。')
                break
            cursor = next_cursor
        else:
            details['stop_reason'] = 'page_budget'

    return {
        'lines': lines,
        'samples': samples,
        'comments_total': total_comments,
        'unique_comment_ids': len(seen_ids),
        'coverage': coverage,
        'warnings': warnings,
    }


def fetch_bilibili_video(url: str, bvid: str) -> FetchResult:
    input_aid = int(bvid[2:]) if re.fullmatch(r'av[0-9]+', bvid, re.I) else None
    video_id = f'av{input_aid}' if input_aid is not None else bvid
    bvid = '' if input_aid is not None else bvid
    video_params = {'aid': input_aid} if input_aid is not None else {'bvid': bvid}
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': f'https://www.bilibili.com/video/{video_id}/',
        'Accept': 'application/json,text/plain,*/*',
    }
    warnings = ['B站评论采用公开热门序列与时间序列分层抽样，不代表全部用户或整个平台总体。']
    with httpx.Client(headers=headers, timeout=25.0, follow_redirects=False) as client:
        view_data: dict[str, Any] = {}
        try:
            view_response = client.get('https://api.bilibili.com/x/web-interface/view', params=video_params)
            view_response.raise_for_status()
            view_payload = view_response.json()
            view_data = (view_payload.get('data') or {}) if view_payload.get('code') == 0 else {}
            if not view_data:
                warnings.append('视频详情接口未返回发布者与统计信息。')
        except (httpx.HTTPError, ValueError, TypeError):
            warnings.append('视频详情接口读取失败，继续采集分P、字幕和评论。')
        if input_aid is not None and view_data.get('bvid'):
            bvid = view_data['bvid']
            video_id = bvid
            video_params = {'bvid': bvid}
        pages_response = client.get(
            'https://api.bilibili.com/x/player/pagelist', params={**video_params, 'jsonp': 'jsonp'}
        )
        pages_response.raise_for_status()
        pages_payload = pages_response.json()
        if pages_payload.get('code') != 0 or not pages_payload.get('data'):
            raise ValueError(f'B站播放器接口没有返回有效分P信息：{pages_payload.get("message", "未知原因")}')
        parts = pages_payload['data']
        page_number = _integer(parse_qs(urlparse(url).query).get('p', [1])[0])
        if page_number < 1 or page_number > len(parts):
            raise ValueError('指定的 B 站分P不存在')
        first = parts[page_number - 1]
        cid = first.get('cid')
        title = bilibili_display_title(view_data.get('title') or first.get('part'), video_id)
        player_data = {}
        player_failed = access_denied = False
        try:
            player_response = client.get(
                'https://api.bilibili.com/x/player/wbi/v2', params={**video_params, 'cid': cid}
            )
            player_response.raise_for_status()
            player_payload = player_response.json()
            player_data = (player_payload.get('data') or {}) if player_payload.get('code') == 0 else {}
            player_failed = player_payload.get('code') != 0
            access_denied = player_payload.get('code') in (-101, -352, -403, -412)
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            player_failed = True
            access_denied = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (
                401,
                403,
                412,
                429,
            )
        if player_failed:
            warnings.append('播放器信息读取失败，已保留视频详情；字幕状态单独记录。')
        aid = view_data.get('aid') or player_data.get('aid') or input_aid

        owner = view_data.get('owner') if isinstance(view_data.get('owner'), dict) else {}
        stat = view_data.get('stat') if isinstance(view_data.get('stat'), dict) else {}
        description = str(view_data.get('desc') or '').strip()
        lines = [f'视频标题：{title}']
        if bvid:
            lines.append(f'BV号：{bvid}')
        lines.append(f'视频时长：{first.get("duration", 0)} 秒')
        if owner.get('name'):
            lines.append(f'发布账号：{owner["name"]}')
        if view_data.get('pubdate'):
            lines.append(f'发布时间戳：{int(view_data["pubdate"])}')
        if description:
            lines.append(f'视频简介：{description}')
        if len(parts) > 1:
            lines.append('分P：' + '；'.join(str(item.get('part') or '未命名') for item in parts[:50]))
        captions = read_captions(
            client, bvid, aid, cid, player_data, player_failed=player_failed, access_denied=access_denied
        )
        subtitle_count = len(captions['tracks'])
        for track in captions['tracks']:
            origin = 'B站AI自动字幕，可能有识别错误' if track['automatic'] else '平台字幕，未独立校对'
            for segment in track['segments']:
                lines.append(
                    f'字幕（{origin}，P{page_number}，{segment["from"]:.3f}–{segment["to"]:.3f}秒）：{segment["content"]}'
                )
        warnings.extend(captions['warnings'])
        if not subtitle_count:
            warnings.append('未取得字幕正文；匿名接口未返回轨道不等于视频无字幕，也不代表已观看视频。')

        comment_result = (
            sample_bilibili_comments(client, aid)
            if aid
            else {
                'lines': [],
                'samples': [],
                'comments_total': 0,
                'unique_comment_ids': 0,
                'coverage': {},
                'warnings': ['缺少视频 aid，未请求评论；已保留视频详情。'],
            }
        )
        lines.extend(comment_result['lines'])
        warnings.extend(comment_result['warnings'])
        comment_samples = comment_result['samples']
        sampled = len(comment_samples)
        total_comments = comment_result['comments_total']
        if not sampled:
            warnings.append('没有取得公开评论，只能分析视频元数据或字幕。')
        text, base_cleaning = clean_source_text('\n'.join(lines), web=True, title=title)
        profile = {
            'kind': 'social_video',
            'confidence': 'high',
            'reasons': ['B站公开视频适配器'],
            'signals': {
                'comments_total': total_comments,
                'comments_sampled': sampled,
                'subtitles': subtitle_count,
            },
        }
        model_text, structure = structure_for_model(
            text, profile, ['视频信息', '字幕', '热门评论', '时间序列评论']
        )
        cleaning = {
            **base_cleaning,
            'page': profile,
            'structure': structure,
            'extraction': 'adapter:bilibili-video',
            'discovered_links': [],
            'source_identity': {
                'publisher': str(owner.get('name') or '')[:120],
                'platform_account_id': str(owner.get('mid') or ''),
                'identity_signals_available': bool(owner.get('name') or owner.get('mid')),
                'fetched_host': 'www.bilibili.com',
                'first_party_status': 'unverified',
                'reason': '平台发布账号是身份线索；未与研究对象的官方账号注册信息进行外部核验。',
            },
            'platform': {
                'name': 'bilibili',
                'bvid': bvid,
                'aid': aid,
                'cid': cid,
                'page_number': page_number,
                'subtitle_capture': captions,
                'media_content': {
                    'video_observed': False,
                    'audio_transcribed': False,
                    'subtitle_tracks': subtitle_count,
                },
                'published_at_unix': int(view_data['pubdate']) if view_data.get('pubdate') else None,
                'video_stats': (
                    {
                        key: int(stat.get(key) or 0)
                        for key in ('view', 'danmaku', 'reply', 'favorite', 'coin', 'share', 'like')
                    }
                    if stat
                    else {}
                ),
                'comments_total': total_comments,
                'comments_sampled': sampled,
                'comment_samples': comment_samples,
                'sampling': {
                    'page_limit_per_strategy': BILIBILI_COMMENT_PAGE_LIMIT,
                    'page_size': BILIBILI_COMMENT_PAGE_SIZE,
                    'unique_comment_ids': comment_result['unique_comment_ids'],
                    'strategies': comment_result['coverage'],
                },
            },
            'warnings': warnings,
        }
        canonical = f'https://www.bilibili.com/video/{video_id}' + (
            f'?p={page_number}' if page_number != 1 else ''
        )
        return title, text, model_text, canonical, cleaning


PLATFORM_ADAPTERS = AdapterRegistry()
PLATFORM_ADAPTERS.register(
    PlatformAdapter('bilibili-video', bilibili_video_id, fetch_bilibili_video, anonymous_public_access=True)
)


def register_platform_adapter(adapter: PlatformAdapter) -> None:
    PLATFORM_ADAPTERS.register(adapter)
