"""Media provenance and immutable report attachments. No remote media downloader.

Display observations are separate from content understanding. All HTML metadata is
untrusted; discovered candidates are NOT permission or successful playback.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urljoin, urlsplit
import uuid

SCHEMA = 'MediaEvidence/v1'
DISPLAY = {
    'needs_activation',
    'permission_unknown',
    'site_restricted',
    'login_required',
    'load_failed',
    'offline',
    'external_only',
    'missing',
    'stale',
    'available_local',
}
UNDERSTANDING = {'not_analyzed', 'metadata_only', 'image_observed', 'subtitles_only', 'audiovisual_observed'}
SAFE_ID = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def public_url(value):
    """Syntactic public URL check only; this module never fetches it server-side."""
    if not isinstance(value, str) or len(value) > 4096 or re.search(r'[\s\\\x00-\x1f]', value):
        raise ValueError('媒体地址无效')
    u = urlsplit(value)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.port not in (None, 443):
        raise ValueError('媒体地址必须是公开 HTTPS 地址且不能含凭据')
    host = u.hostname.lower()
    if '.' not in host or host.endswith(('.localhost', '.local', '.internal', '.test', '.invalid')):
        raise ValueError('不允许本地媒体主机')
    if re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]+)', host.rsplit('.', 1)[-1]):
        raise ValueError('不允许数字或变体 IP 媒体主机')
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ValueError('不允许私网媒体地址')
    except ValueError as error:
        if re.fullmatch(r'[0-9.]+', host) or ':' in host or '私网' in str(error):
            raise ValueError('不允许 IP 媒体地址') from error
    return value


def youtube_id(url):
    u = urlsplit(public_url(url))
    host = u.hostname.lower()
    if host == 'youtu.be':
        value = u.path.removeprefix('/')
    elif host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'www.youtube-nocookie.com'}:
        if u.path == '/watch':
            values = parse_qs(u.query).get('v', [])
            value = values[0] if len(values) == 1 else ''
        else:
            match = re.fullmatch(r'/(?:embed|shorts)/([A-Za-z0-9_-]{11})', u.path)
            value = match[1] if match else ''
    else:
        raise ValueError('尚不支持此视频提供方内嵌')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', value):
        raise ValueError('YouTube 媒体 ID 无效')
    return value


def normalize_media(raw):
    kind = raw.get('media_kind')
    if kind not in {'image', 'video'}:
        raise ValueError('未知媒体类型')
    source = public_url(raw.get('source_url', ''))
    provider = raw.get('provider', 'external')
    url = public_url(raw.get('canonical_media_url') or source)
    media_id = ''
    if provider == 'youtube':
        if kind != 'video':
            raise ValueError('YouTube 记录必须为视频')
        media_id = youtube_id(url)
        if raw.get('media_id') and raw['media_id'] != media_id:
            raise ValueError('媒体 ID 与地址不一致')
        url = 'https://www.youtube.com/watch?v=' + media_id
    elif provider not in {'local', 'external'}:
        raise ValueError('未知媒体提供方')
    status = raw.get('display_status', 'needs_activation' if provider == 'youtube' else 'permission_unknown')
    understanding = raw.get('understanding_status', 'not_analyzed')
    if status not in DISPLAY or understanding not in UNDERSTANDING:
        raise ValueError('未知媒体状态；文件存在或 HTTP 成功不能冒充已播放')
    if understanding not in {'not_analyzed', 'metadata_only'} and not (
        raw.get('understanding_method') and raw.get('observation')
    ):
        raise ValueError('媒体理解必须记录实际观察与方法')
    locator = dict(raw.get('locator') or {})
    bbox = locator.get('normalized_bbox')
    if bbox is not None:
        if (
            kind != 'image'
            or not isinstance(bbox, list)
            or len(bbox) != 4
            or any(type(x) not in (int, float) or not 0 <= x <= 1 for x in bbox)
            or bbox[2] <= 0
            or bbox[3] <= 0
            or bbox[0] + bbox[2] > 1
            or bbox[1] + bbox[3] > 1
        ):
            raise ValueError('图片区域必须是有效的归一化 x/y/width/height')
    if 'start_ms' in locator or 'end_ms' in locator:
        start, end = locator.get('start_ms', 0), locator.get('end_ms')
        if (
            kind != 'video'
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= 86_400_000
        ):
            raise ValueError('媒体时间段无效')
    fields = (
        'title',
        'author',
        'source_role',
        'rights_status',
        'rights_basis',
        'display_reason',
        'understanding_method',
        'observation',
        'observed_at',
        'captured_at',
        'retrieval_path',
        'discovery_query',
        'discovery_method',
        'duplicate_of',
        'capture_status',
    )
    result = {k: str(raw.get(k) or '') for k in fields}
    oversized = [k for k, value in result.items() if len(value) > 12000]
    if oversized:
        raise ValueError('媒体字段超过 12000 字符，未保存且未截断：' + ', '.join(oversized))
    result.update(
        schema_version=SCHEMA,
        media_kind=kind,
        provider=provider,
        source_url=source,
        canonical_media_url=url,
        media_id=media_id,
        display_status=status,
        understanding_status=understanding,
        locator=locator,
    )
    # Arbitrary embed HTML/URLs, paths and caller-supplied hashes are deliberately not copied.
    return result


class MediaParser(HTMLParser):
    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.page_url, self.items, self.seen = public_url(page_url), [], set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        value = attrs.get('src') if tag in {'img', 'iframe'} else attrs.get('href') if tag == 'a' else None
        if tag == 'meta' and attrs.get('property', '').lower() in {'og:image', 'og:video', 'og:video:url'}:
            value = attrs.get('content')
        if not value:
            return
        try:
            url = public_url(urljoin(self.page_url, value))
            try:
                mid = youtube_id(url)
            except ValueError:
                mid = None
            is_image = tag == 'img' or (tag == 'meta' and attrs.get('property', '').lower() == 'og:image')
            if not mid and not is_image:
                return
            item = normalize_media(
                {
                    'media_kind': 'video' if mid else 'image',
                    'provider': 'youtube' if mid else 'external',
                    'source_url': self.page_url,
                    'canonical_media_url': url,
                    'title': attrs.get('alt') or attrs.get('title') or '页面媒体候选',
                    'understanding_status': 'metadata_only',
                    'discovery_method': 'page_html_metadata',
                    'rights_status': 'unknown',
                    'display_reason': '发现地址不等于已展示或已分析；图片展示权限待核实'
                    if not mid
                    else '待用户启用及实际播放核验',
                }
            )
            key = item['canonical_media_url']
            if key not in self.seen:
                self.items.append(item)
                self.seen.add(key)
        except (ValueError, TypeError):
            return


def discover_media_candidates(page_url, html, limit=None):
    """Static, supported tags only; caller owns network/robots validation and budgets."""
    parser = MediaParser(page_url)
    parser.feed(html)
    items = parser.items if limit is None else parser.items[: max(int(limit), 0)]
    return {
        'items': items,
        'coverage': {
            'scope': 'static_img_og_image_youtube_links_iframes',
            'dynamic_css_media_checked': False,
            'discovered_unique': len(parser.items),
            'returned': len(items),
            'omitted': len(parser.items) - len(items),
            'status': 'budget_limited' if len(items) < len(parser.items) else 'supported_static_tags_scanned',
        },
    }


def ensure_media_tables(connection):
    """Create tables within caller's transaction; never commit or open another connection."""
    for statement in (
        """CREATE TABLE IF NOT EXISTS media_evidence_records (
                    project_id TEXT NOT NULL, evidence_id TEXT NOT NULL, version INTEGER NOT NULL,
                    record_json TEXT NOT NULL, PRIMARY KEY(project_id,evidence_id,version))""",
        """CREATE TABLE IF NOT EXISTS report_media_bindings (
                    project_id TEXT NOT NULL, report_version INTEGER NOT NULL, evidence_id TEXT NOT NULL,
                    media_version INTEGER NOT NULL, binding_json TEXT NOT NULL,
                    PRIMARY KEY(project_id,report_version,evidence_id))""",
        """CREATE TABLE IF NOT EXISTS media_review_events (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, report_version INTEGER NOT NULL,
                    evidence_id TEXT NOT NULL, media_version INTEGER NOT NULL, event_json TEXT NOT NULL)""",
    ):
        connection.execute(statement)


class MediaStore:
    def __init__(self, db_path, artifact_dir):
        self.db_path, self.artifact_dir = Path(db_path), Path(artifact_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            ensure_media_tables(db)

    def connect(self):
        return sqlite3.connect(self.db_path, timeout=30)

    def add(self, project_id, raw, *, evidence_id=None, image_bytes=None):
        record = normalize_media(raw)
        eid = evidence_id or 'media_' + uuid.uuid4().hex
        if not SAFE_ID.fullmatch(eid):
            raise ValueError('媒体证据 ID 无效')
        if record['provider'] == 'local':
            if image_bytes is None or record['media_kind'] != 'image':
                raise ValueError('本地证据需提供授权图片字节')
            if len(image_bytes) > 20_000_000:
                raise ValueError('图片超出 20MB 限制')
            from PIL import Image

            with Image.open(BytesIO(image_bytes)) as img:
                if img.format not in {'PNG', 'JPEG', 'WEBP'} or img.width * img.height > 40_000_000:
                    raise ValueError('仅接受限定大小的 PNG/JPEG/WebP 栅格图片')
                width, height, fmt = img.width, img.height, img.format
                img.verify()
            digest = sha256(image_bytes).hexdigest()
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            path = self.artifact_dir / digest
            try:
                with path.open('xb') as out:
                    out.write(image_bytes)
            except FileExistsError:
                if sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError('已有图片哈希不一致')
            record.update(
                content_hash=digest,
                mime_type={'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}[fmt],
                bytes=len(image_bytes),
                width=width,
                height=height,
                display_status='available_local',
            )
        elif image_bytes is not None:
            raise ValueError('禁止将远程媒体下载后伪装为本地证据')
        elif record['display_status'] == 'available_local':
            raise ValueError('远程媒体不能声明本地可用')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute(
                'SELECT record_json FROM media_evidence_records WHERE project_id=? AND evidence_id=? ORDER BY version DESC LIMIT 1',
                (project_id, eid),
            ).fetchone()
            previous = json.loads(prior[0]) if prior else None
            if previous and not raw.get('duplicate_of'):
                record['duplicate_of'] = previous.get('duplicate_of', '')
            # Identical imports are idempotent; revisions never overwrite the old snapshot.
            if previous and all(previous.get(k) == v for k, v in record.items()):
                return previous
            if not record.get('duplicate_of'):
                for row in db.execute(
                    'SELECT record_json FROM media_evidence_records WHERE project_id=? AND evidence_id<>?',
                    (project_id, eid),
                ):
                    other = json.loads(row[0])
                    same = (
                        record.get('content_hash') == other.get('content_hash')
                        if record.get('content_hash')
                        else (
                            record['provider'] != 'local'
                            and record['canonical_media_url'] == other['canonical_media_url']
                        )
                    )
                    if same:
                        record['duplicate_of'] = other['evidence_id']
                        break
            record.update(
                evidence_id=eid, version=previous['version'] + 1 if previous else 1, recorded_at=now()
            )
            record['record_hash'] = sha256(
                json.dumps(record, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            db.execute(
                'INSERT INTO media_evidence_records VALUES (?,?,?,?)',
                (project_id, eid, record['version'], json.dumps(record, ensure_ascii=False)),
            )
        return record

    def get(self, project_id, evidence_id, version):
        with self.connect() as db:
            row = db.execute(
                'SELECT record_json FROM media_evidence_records WHERE project_id=? AND evidence_id=? AND version=?',
                (project_id, evidence_id, version),
            ).fetchone()
        if not row:
            raise ValueError('媒体证据或版本不存在')
        return json.loads(row[0])

    def report(self, project_id, version):
        with self.connect() as db:
            row = db.execute(
                'SELECT id,report_json FROM research_reports WHERE project_id=? AND version=?',
                (project_id, version),
            ).fetchone()
        if not row:
            raise ValueError('报告版本不存在')
        report = json.loads(row[1])
        report['report_id'] = row[0]
        return report

    def bind_report(
        self,
        project_id,
        report_version,
        evidence_id,
        media_version,
        *,
        record_hash,
        anchor_id='',
        anchor_text_hash='',
    ):
        record = self.get(project_id, evidence_id, media_version)
        report = self.report(project_id, report_version)
        if record_hash != record['record_hash']:
            raise ValueError('媒体引用版本哈希不一致')
        if anchor_id:
            from zhixi.reports.report_questions import report_anchor

            note = str(report_anchor(report, anchor_id).get('note') or '')
            if sha256(note.encode()).hexdigest() != anchor_text_hash:
                raise ValueError('报告段落已变化，不能绑定旧媒体引用')
        binding = {
            'evidence_id': evidence_id,
            'media_version': media_version,
            'record_hash': record_hash,
            'anchor_id': anchor_id,
            'anchor_text_hash': anchor_text_hash,
            'report_id': report['report_id'],
        }
        with self.connect() as db:
            old = db.execute(
                'SELECT binding_json FROM report_media_bindings WHERE project_id=? AND report_version=? AND evidence_id=?',
                (project_id, report_version, evidence_id),
            ).fetchone()
            if old and json.loads(old[0]) != binding:
                raise ValueError('已有报告媒体绑定不可覆盖；请另存报告版本')
            db.execute(
                'INSERT OR IGNORE INTO report_media_bindings VALUES (?,?,?,?,?)',
                (project_id, report_version, evidence_id, media_version, json.dumps(binding)),
            )
        return binding

    def list_report(self, project_id, report_version):
        report = self.report(project_id, report_version)
        with self.connect() as db:
            bindings = [
                json.loads(row[0])
                for row in db.execute(
                    'SELECT binding_json FROM report_media_bindings WHERE project_id=? AND report_version=? ORDER BY rowid',
                    (project_id, report_version),
                )
            ]
        items = []
        for binding in bindings:
            record = self.get(project_id, binding['evidence_id'], binding['media_version'])
            state = 'valid'
            if (
                record['record_hash'] != binding['record_hash']
                or binding.get('report_id') != report['report_id']
            ):
                state = 'stale'
            if binding['anchor_id']:
                from zhixi.reports.report_questions import report_anchor

                try:
                    note = str(report_anchor(report, binding['anchor_id']).get('note') or '')
                    if sha256(note.encode()).hexdigest() != binding['anchor_text_hash']:
                        state = 'stale'
                except ValueError:
                    state = 'stale'
            if record['provider'] == 'local':
                path = self.artifact_dir / record['content_hash']
                if not path.is_file():
                    record.update(display_status='missing', display_reason='已登记图片文件缺失')
                elif sha256(path.read_bytes()).hexdigest() != record['content_hash']:
                    record.update(display_status='stale', display_reason='图片内容与保存哈希不一致')
                else:
                    record['asset_url'] = f'/api/media-assets/{record["content_hash"]}'
            items.append({**record, 'binding': binding, 'binding_status': state})
        return items

    def add_review(
        self,
        project_id,
        report_version,
        evidence_id,
        media_version,
        record_hash,
        text,
        *,
        kind='objection',
        parent_id='',
    ):
        if (
            kind not in {'objection', 'revision_note'}
            or not isinstance(text, str)
            or not 1 <= len(text.strip()) <= 4000
        ):
            raise ValueError('异议/修订说明无效')
        items = self.list_report(project_id, report_version)
        if not any(
            i['evidence_id'] == evidence_id
            and i['version'] == media_version
            and i['record_hash'] == record_hash
            and i['binding_status'] == 'valid'
            for i in items
        ):
            raise ValueError('异议引用不属于当前报告媒体版本或锚点已过期')
        event = dict(
            id=uuid.uuid4().hex,
            kind=kind,
            text=text.strip(),
            evidence_id=evidence_id,
            media_version=media_version,
            record_hash=record_hash,
            parent_id=parent_id,
            created_at=now(),
            status='recorded_not_adjudicated',
        )
        with self.connect() as db:
            if (
                parent_id
                and not db.execute(
                    'SELECT 1 FROM media_review_events WHERE id=? AND project_id=? AND report_version=? AND evidence_id=? AND media_version=?',
                    (parent_id, project_id, report_version, evidence_id, media_version),
                ).fetchone()
            ):
                raise ValueError('修订引用的异议不存在或版本不匹配')
            db.execute(
                'INSERT INTO media_review_events VALUES (?,?,?,?,?,?)',
                (
                    event['id'],
                    project_id,
                    report_version,
                    evidence_id,
                    media_version,
                    json.dumps(event, ensure_ascii=False),
                ),
            )
        return event


def install_media_routes(api, ctx):
    """Call once from app.py; ctx provides DB_PATH and DATA_DIR dynamically."""
    from fastapi import HTTPException, Body
    from fastapi.responses import Response

    def store():
        return MediaStore(ctx.DB_PATH, Path(ctx.DATA_DIR) / 'media_artifacts')

    @api.get('/api/research/{project_id}/reports/{version}/media')
    def read_media(project_id: str, version: int):
        try:
            s = store()
            items = s.list_report(project_id, version)
            with s.connect() as db:
                reviews = [
                    json.loads(row[0])
                    for row in db.execute(
                        'SELECT event_json FROM media_review_events WHERE project_id=? AND report_version=? ORDER BY rowid',
                        (project_id, version),
                    )
                ]
            return {
                'schema_version': SCHEMA,
                'report_id': s.report(project_id, version)['report_id'],
                'items': items,
                'reviews': reviews,
            }
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @api.post('/api/research/{project_id}/reports/{version}/media/reviews')
    def review_media(project_id: str, version: int, body: dict = Body(...)):
        try:
            return store().add_review(
                project_id,
                version,
                body['evidence_id'],
                body['media_version'],
                body['record_hash'],
                body['text'],
                kind=body.get('kind', 'objection'),
                parent_id=body.get('parent_id', ''),
            )
        except (KeyError, ValueError, TypeError) as error:
            raise HTTPException(400, str(error)) from error

    @api.get('/api/media-assets/{digest}')
    def asset(digest: str):
        if not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise HTTPException(404, '图片不存在')
        s = store()
        with s.connect() as db:
            records = [
                json.loads(row[0]) for row in db.execute('SELECT record_json FROM media_evidence_records')
            ]
        record = next(
            (r for r in records if r.get('content_hash') == digest and r['provider'] == 'local'), None
        )
        path = s.artifact_dir / digest
        if not record or not path.is_file():
            raise HTTPException(404, '图片文件缺失')
        data = path.read_bytes()
        if sha256(data).hexdigest() != digest:
            raise HTTPException(409, '图片哈希变化，旧引用失效')
        return Response(
            data,
            media_type=record['mime_type'],
            headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'},
        )
