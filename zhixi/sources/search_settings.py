"""Desktop search selection, independent of model selection and research data.

Only the default provider is persisted. Keys live in this server process or in
explicit environment variables; reading settings never performs a search.
"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from threading import RLock
import tempfile


PROVIDERS = {'hybrid': '普通网页搜索（百度 / 360 / Yahoo）', 'baidu': '百度搜索 API', 'brave': 'Brave Search API'}
ENV_KEYS = {'baidu': 'WP_BAIDU_SEARCH_API_KEY', 'brave': 'WP_BRAVE_SEARCH_API_KEY'}
_lock = RLock()
_keys: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class Selection:
    provider: str
    api_key: str = field(default='', repr=False)


def _path(data_dir):
    return Path(data_dir).resolve() / 'search-settings.json'


def _provider(data_dir):
    path = _path(data_dir)
    if not path.exists():
        return 'hybrid'
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
        provider = saved['default_provider']
        if provider not in PROVIDERS:
            raise ValueError
        return provider
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('搜索设置无法读取，请在设置中重新保存；未自动改用其他服务。') from None


def _key(data_dir, provider):
    return _keys.get((str(_path(data_dir)), provider)) or os.environ.get(ENV_KEYS.get(provider, ''), '').strip()


def selection(data_dir, provider=None):
    with _lock:
        chosen = _provider(data_dir) if provider is None else provider
        if chosen not in PROVIDERS:
            raise ValueError('不支持此搜索服务')
        return Selection(chosen, _key(data_dir, chosen))


def required_selection(data_dir, provider=None):
    chosen = selection(data_dir, provider)
    if chosen.provider != 'hybrid' and not chosen.api_key:
        raise ValueError('搜索 API 未配置密钥；请在设置中填写，未自动改用其他服务。')
    return chosen


def describe(data_dir):
    with _lock:
        chosen = _provider(data_dir)
        providers = [
            {
                'id': name, 'name': title,
                'requires_key': name != 'hybrid',
                'has_key': bool(_key(data_dir, name)) if name != 'hybrid' else False,
                'key_source': 'server_memory' if (str(_path(data_dir)), name) in _keys
                else 'environment' if name != 'hybrid' and _key(data_dir, name) else None,
            }
            for name, title in PROVIDERS.items()
        ]
        return {'default_provider': chosen, 'providers': providers, 'key_storage': 'server_memory_or_environment'}


def save(data_dir, provider, api_key=None, *, clear_key=False):
    """Save locally, never validate credentials through a billable request."""
    if provider not in PROVIDERS:
        raise ValueError('不支持此搜索服务')
    if api_key is not None:
        if not isinstance(api_key, str) or len(api_key) > 2000 or any(ord(c) < 32 for c in api_key):
            raise ValueError('搜索密钥格式无效')
        api_key = api_key.strip()
    if clear_key and api_key:
        raise ValueError('不能同时填写和清除密钥')
    if provider == 'hybrid' and api_key:
        raise ValueError('匿名搜索不使用 API 密钥')
    with _lock:
        path = _path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix='.search-settings-', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({'default_provider': provider}, stream, ensure_ascii=False)
            os.replace(temporary, path)
        except OSError:
            raise ValueError('搜索设置保存失败，原设置未被替换。') from None
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        identity = (str(path), provider)
        if clear_key:
            _keys.pop(identity, None)
        elif api_key:
            _keys[identity] = api_key
        return describe(data_dir)
