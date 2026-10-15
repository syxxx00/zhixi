"""Preserve research originals and resolve source-use IDs without interpreting them."""

import json
from hashlib import sha256
from pathlib import Path

from zhixi.content.source_material import decode_file, material_record


def resolve_preview_span(candidate, span_id):
    """Bind one existing preview ID to program-owned coordinates and exact text.

    The digest checks transport only. It neither endorses the model's use
    judgment nor extends that judgment to the rest of the page.
    """
    if not isinstance(span_id, str) or not span_id or span_id != candidate['id']:
        raise ValueError('未知或不属于本候选的原文片段编号')
    preview = candidate.get('preview')
    if not isinstance(preview, str) or not preview.strip():
        raise ValueError('所选片段没有可取回的本批原文')
    span = candidate.get('span', {'id': span_id, 'offset': 0, 'characters': len(preview)})
    if (
        not isinstance(span, dict)
        or span.get('id') != span_id
        or type(span.get('offset')) is not int
        or span['offset'] < 0
        or type(span.get('characters')) is not int
        or span['characters'] != len(preview)
    ):
        raise ValueError('本批原文片段的位置或长度无效')
    return {
        'id': span_id,
        'offset': span['offset'],
        'characters': len(preview),
        'preview_sha256': sha256(preview.encode('utf-8')).hexdigest(),
    }


def prepare(ctx, materials):
    if sum(len(item.content) for item in materials) > 1_000_000:
        raise ValueError('本次附加材料超过100万字，请分次提交；未截断材料。')
    prepared = []
    for item in materials:
        original = item.content
        extracted = original
        title = item.title.strip() or '附加文本'
        file_record = None
        if item.imported_file is not None:
            decoded, bytes_record = decode_file(item.imported_file.data_base64)
            if decoded != original:
                raise ValueError('附加文件字节与文本不一致，未创建研究。')
            title = item.imported_file.name
            suffix = Path(title).suffix.lower()
            if suffix not in {'.txt', '.md', '.html', '.htm'}:
                raise ValueError('研究附件支持TXT、Markdown和HTML文本文件。')
            file_record = {'name': title, **bytes_record}
            if suffix in {'.html', '.htm'}:
                parser = ctx.ArticleParser()
                parser.feed(original)
                _, extracted = parser.result()
        cleaned, cleaning = ctx.clean_source_text(extracted)
        if not cleaned.strip():
            raise ValueError(f'附加材料“{title}”没有可读内容，未创建研究。')
        record = material_record(
            original,
            cleaned,
            cleaned,
            kind='user_supplied_file' if file_record else 'user_supplied_text',
            extracted=extracted,
            cleaning_transform=cleaning.get('transform'),
        )
        if file_record:
            record['file'] = file_record
        prepared.append(
            {
                'title': title,
                'content': cleaned,
                'extraction': {
                    **cleaning,
                    'source_material': record,
                    'origin': 'user_supplied_private_material',
                    'public_query_eligible': False,
                    'page': {'kind': 'local_material'},
                    'access_note': '用户提供材料，未通过网址读取，未授权作为公开搜索内容。',
                },
            }
        )
    return prepared


def insert(ctx, db, project_id, goal, materials):
    for item in materials:
        identity = ctx.uuid.uuid4().hex
        now = ctx.utc_now()
        db.execute(
            'INSERT INTO research_pages(id,project_id,url,canonical_url,depth,status,title,content,page_type,extraction_json,created_at,updated_at) '
            "VALUES (?,?,'',?,0,'fetched',?,?,'local_material',?,?,?)",
            (
                identity,
                project_id,
                'local-material:' + identity,
                item['title'],
                item['content'],
                json.dumps(item['extraction'], ensure_ascii=False),
                now,
                now,
            ),
        )
        ctx.index_page_evidence(
            db, project_id, identity, item['title'], item['content'], goal, item['extraction']
        )
        ctx.add_research_event(
            project_id,
            'input_material_saved',
            '附加原材料已保存，供同一研究读取',
            page_id=identity,
            details={
                'title': item['title'],
                'characters': len(item['content']),
                'public_query_eligible': False,
            },
            connection=db,
        )
