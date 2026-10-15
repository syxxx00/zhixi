"""Read existing caption tracks; never download video or infer visual content.

Protocol reference (not an official API contract):
https://github.com/ccBilly-aipm/bilibili-ai-subtitle/blob/main/docs/FLOW_CN.md
The JSON and Protobuf interfaces can both return empty data anonymously.
"""

from __future__ import annotations

import math
from urllib.parse import urlparse

import httpx


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError('截断的字幕元数据')
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
    raise ValueError('无效字幕元数据整数')


def _fields(data: bytes):
    offset = 0
    while offset < len(data):
        key, offset = _varint(data, offset)
        number, wire = key >> 3, key & 7
        if not number:
            raise ValueError('无效字幕字段')
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire in (1, 2, 5):
            size = 8 if wire == 1 else 4
            if wire == 2:
                size, offset = _varint(data, offset)
            if offset + size > len(data):
                raise ValueError('截断的字幕字段')
            value = data[offset : offset + size]
            offset += size
        else:
            raise ValueError('不支持的字幕元数据编码')
        yield number, wire, value


def protobuf_tracks(data: bytes) -> list[dict]:
    tracks = []
    for number, wire, container in _fields(data):
        if (number, wire) != (1, 2):
            continue
        for number, wire, raw in _fields(container):
            if (number, wire) != (3, 2):
                continue
            track = {}
            for field, wire, value in _fields(raw):
                if wire == 2 and field in (3, 4, 5):
                    track[{3: 'lan', 4: 'lan_doc', 5: 'subtitle_url'}[field]] = value.decode('utf-8')
            if track.get('subtitle_url'):
                tracks.append(track)
    return tracks


def caption_url(value: str) -> str:
    value = 'https:' + value if value.startswith('//') else value
    parsed = urlparse(value)
    host = parsed.hostname or ''
    if (
        parsed.scheme != 'https'
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or not (host == 'hdslb.com' or host.endswith('.hdslb.com'))
    ):
        raise ValueError('字幕地址不是允许的 B 站 HTTPS 字幕来源')
    return value


def read_captions(
    client,
    bvid: str,
    aid: int,
    cid: int,
    player: dict,
    *,
    player_failed: bool = False,
    access_denied: bool = False,
) -> dict:
    result = {
        'status': 'not_available_anonymously',
        'tracks': [],
        'warnings': [],
        'access': 'anonymous',
        'video_observed': False,
        'audio_transcribed': False,
        'probes': [],
        'need_login': bool(player.get('need_login_subtitle')),
    }
    subtitle = player.get('subtitle') or {}
    tracks = subtitle.get('subtitles') or []
    result['probes'].append(
        {
            'interface': 'player_json',
            'status': 'failed' if player_failed else 'ok',
            'track_count': len(tracks),
        }
    )
    if access_denied or result['need_login']:
        result['status'] = 'access_restricted' if access_denied else 'login_required'
        return result
    if not tracks and aid and cid:
        try:
            response = client.get(
                'https://api.bilibili.com/x/v2/subtitle/web/view',
                params={
                    'oid': cid,
                    'pid': aid,
                    'context_ext': '{"video_type":1}',
                    'type': 1,
                    'cur_production_type': 0,
                    'preferred_language': 'ai-zh',
                    'playlist_switch': 0,
                },
            )
            response.raise_for_status()
            if response.headers.get('content-type', '').startswith('application/json'):
                payload = response.json()
                result['probes'].append(
                    {'interface': 'subtitle_protobuf', 'status': 'api_rejected', 'code': payload.get('code')}
                )
            else:
                tracks = protobuf_tracks(response.content)
                result['probes'].append(
                    {'interface': 'subtitle_protobuf', 'status': 'ok', 'track_count': len(tracks)}
                )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            result['probes'].append(
                {'interface': 'subtitle_protobuf', 'status': 'failed', 'error_type': type(exc).__name__}
            )
            result['warnings'].append('新字幕接口未读取成功；不能据此判断视频没有字幕。')
    for track in tracks:
        try:
            url = caption_url(str(track.get('subtitle_url') or track.get('ai_subtitle_url') or ''))
            response = client.get(url)
            response.raise_for_status()
            segments = []
            for row in response.json().get('body') or []:
                start, end = float(row['from']), float(row['to'])
                if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
                    raise ValueError('字幕时间轴无效')
                segments.append({'from': start, 'to': end, 'content': str(row.get('content') or '')})
            if segments:
                result['tracks'].append(
                    {
                        'bvid': bvid,
                        'cid': cid,
                        'language': track.get('lan', ''),
                        'label': track.get('lan_doc', ''),
                        'automatic': str(track.get('lan', '')).startswith('ai-'),
                        'segments': segments,
                    }
                )
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            result['warnings'].append(
                f'一条字幕未能完整读取（{type(exc).__name__}）；未保存部分内容或签名地址。'
            )
    if result['tracks']:
        result['status'] = 'partial' if result['warnings'] else 'available'
    elif tracks or player_failed or any(p['status'] != 'ok' for p in result['probes']):
        result['status'] = 'unavailable'
    return result
