"""Recognize observed access shells, not prose mentioning CAPTCHA or payment.

This is a transport/DOM check, never a semantic relevance filter. Unknown pages
and readable previews remain available; original responses are not rewritten.
"""

from html.parser import HTMLParser
from urllib.parse import urlparse


class AccessVerificationError(ValueError):
    def __init__(self, raw_html, signals):
        super().__init__('网站返回了访问验证页面，尚未取得目标正文；未绕过验证，可寻找其他公开来源。')
        from zhixi.content.source_material import snapshot

        self.access_diagnostic = {
            'kind': 'access_verification',
            'signals': signals,
            'response_snapshot': snapshot(raw_html, 'access_verification_html_not_source_evidence'),
        }


def verification_destination(url):
    """Known verification endpoints, not a ban on the providers' content."""
    parsed = urlparse(url)
    return parsed.hostname in {'captcha.so.com', 'qcaptcha.so.com'} or (
        parsed.hostname == 'wappass.baidu.com' and parsed.path.startswith('/static/captcha/')
    )


class _VerificationMarkup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = set()
        self.scripts = []
        self.in_script = False
        self.script_urls = []
        self.element_ids = set()
        self.title = []
        self.in_title = False
        self.stack = []
        self.visible_text = []
        self.preview_text = []
        self.article_text = []
        self.preview_found = False
        self.classes = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get('class') or '').split())
        self.classes.update(classes)
        blocked = tag in {'script', 'style', 'head', 'textarea', 'template'} or (
            bool(self.stack) and self.stack[-1][1]
        )
        preview = (attrs.get('id') == 'contentcontainer' and 'reader_inner' in classes) or (
            bool(self.stack) and self.stack[-1][2]
        )
        self.preview_found = self.preview_found or preview
        if tag not in {
            'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
            'link', 'meta', 'param', 'source', 'track', 'wbr',
        }:
            self.stack.append((tag, blocked, preview))
        if tag == 'meta':
            self.meta.add((attrs.get('name') or '').lower())
        if attrs.get('id'):
            self.element_ids.add(attrs['id'])
        if tag == 'title':
            self.in_title = True
        if tag == 'script':
            self.in_script = True
            if attrs.get('src'):
                self.script_urls.append(attrs['src'])

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag == 'script':
            self.in_script = False
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.in_script:
            self.scripts.append(data)
        if self.in_title:
            self.title.append(data)
        if not self.stack or not self.stack[-1][1]:
            self.visible_text.append(data)
            if self.stack and self.stack[-1][2]:
                self.preview_text.append(data)
            elif data.strip() and any(tag == 'article' for tag, _, _ in self.stack):
                self.article_text.append(data)


def _script_locations(parser):
    for url in parser.script_urls:
        try:
            parsed = urlparse('https:' + url if url.startswith('//') else url)
            yield (parsed.hostname or '').lower(), parsed.path
        except ValueError:
            continue  # Invalid assets do not turn an otherwise readable page into a fetch failure.


def reject_access_verification(raw_html):
    parser = _VerificationMarkup()
    parser.feed(raw_html)
    # Observed provider response contract, independent of the requested domain.
    # Literal tutorials, escaped code and a normal CAPTCHA widget do not match
    # both active response metadata and this challenge application identifier.
    markers = {'aliyun_waf_aa', 'aliyun_waf_bb'}
    active = any(
        'CF_APP_WAF' in script
        or ('initAliyunCaptcha' in script and ('Access Verification' in script or '访问验证' in script))
        for script in parser.scripts
    )
    if markers.issubset(parser.meta) and active:
        raise AccessVerificationError(
            raw_html, ['aliyun_waf_response_metadata', 'active_waf_verification_application']
        )
    # A second Aliyun response embeds an opaque encrypted payload rather than
    # an interactive widget. Never send that payload to semantic reading.
    if (
        markers.issubset(parser.meta)
        and 'renderData' in parser.element_ids
        and any('getRenderData' in script and 'window._waf_' in script for script in parser.scripts)
    ):
        raise AccessVerificationError(
            raw_html, ['aliyun_waf_response_metadata', 'active_waf_encoded_render_data']
        )
    # The observed Volcengine response has only this challenge text in its
    # visible body. A tutorial quoting the text, or a widget beside an article,
    # is not a challenge-only response.
    visible = ''.join(''.join(parser.visible_text).split())
    if (
        ''.join(parser.title).strip() == '百度安全验证'
        and not parser.article_text
        and all(text in visible for text in ('网络不给力', '返回首页', '问题反馈'))
    ):
        raise AccessVerificationError(raw_html, ['baidu_verification_title', 'baidu_network_error_shell'])
    if (
        visible == '火山引擎正在进行安全检测...为保障您的访问安全，系统正在检测当前网络环境，该过程通常需要几秒钟，请耐心等待'
        and {'spinner', 'card', 'brand'}.issubset(parser.classes)
        and parser.scripts
    ):
        raise AccessVerificationError(raw_html, ['volcengine_challenge_only_body', 'active_challenge_script'])
    # Observed Bilibili HTTP-200 challenge shell, not a ban on its domain or
    # pages mentioning CAPTCHA. Require the page title, application root and
    # both actual SDK/application assets; a normal embedded widget is allowed.
    paths = []
    for host, path in _script_locations(parser):
        if host == 'hdslb.com' or host.endswith('.hdslb.com'):
            paths.append(path)
    if (
        ''.join(parser.title).strip() == '验证码_哔哩哔哩'
        and 'risk-captcha-app' in parser.element_ids
        and '/bfs/seed/jinkela/risk-captcha-sdk/CaptchaLoader.js' in paths
        and any(
            path.startswith('/bfs/static/jinkela/risk-captcha/') and path.endswith('.js') for path in paths
        )
    ):
        raise AccessVerificationError(
            raw_html,
            [
                'bilibili_verification_title',
                'risk_captcha_application_root',
                'active_risk_captcha_sdk_and_application',
            ],
        )


def document_access(raw_html):
    """Observed canvas viewer: metadata is not the target document's text.

    Do not reject a domain, a short article, or a preview containing actual text.
    Empty/canvas-only viewers may still have useful destination links. Retain those
    and the response for inspection, but do not infer a document from its title.
    """
    parser = _VerificationMarkup()
    parser.feed(raw_html)
    viewer_asset = any(
        host.endswith('.douding.cn') and path.startswith('/js/canvas/')
        for host, path in _script_locations(parser)
    )
    if not (viewer_asset and parser.preview_found and 'container_wrapper' in parser.element_ids):
        return None
    readable = bool(''.join(parser.preview_text).strip())
    if not readable and parser.article_text:
        return None  # An article embedding an empty viewer is not an empty article.
    return {
        'kind': 'document_preview' if readable else 'document_body_unavailable',
        'body_available': readable,
        'signals': [
            'canvas_viewer_asset', 'document_reader_container',
            'preview_text_present' if readable else 'no_document_text_in_reader',
        ],
        'message': (
            '已取得文档预览文字，不代表完整文档。' if readable else
            '仅取得文档信息与阅读器，未取得文档正文；可能需要动态预览、登录或付费。'
        ),
    }
