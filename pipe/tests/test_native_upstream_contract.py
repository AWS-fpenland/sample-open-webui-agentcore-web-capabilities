"""Execute only the pinned external-hook functions with offline dependencies."""

import ast
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def definition(path, name, namespace):
    configured = os.environ.get('OWUI_TEST_NATIVE_SOURCE_DIR')
    root = Path(configured) if configured is not None else Path('/tmp/open-webui-v0.11.3')
    source = root / path
    if not source.is_file():
        if configured is not None:
            pytest.fail('Configured pinned native source is missing: ' + str(source))
        pytest.skip('Pinned native source is not installed')
    syntax = ast.parse(source.read_text())
    node = next(item for item in syntax.body if isinstance(item, (ast.FunctionDef, ast.ClassDef)) and item.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), namespace)
    return namespace[name]


def test_pinned_external_search_post_contract():
    calls = []
    records = [{'link': 'https://public.example/page', 'title': 'Fixture', 'snippet': 'Public fixture.'}]
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: records)
    namespace = {'List': list, 'Optional': __import__('typing').Optional, 'Request': object,
                 'SearchResult': lambda **item: item, 'get_filtered_results': lambda items, filters: items,
                 'requests': SimpleNamespace(post=post), 'log': logging.getLogger(__name__),
                 'include_user_info_headers': lambda headers, user: headers,
                 'FORWARD_SESSION_INFO_HEADER_CHAT_ID': 'X-Synthetic-Chat'}
    search = definition('backend/open_webui/retrieval/web/external.py', 'search_external', namespace)
    result = search(SimpleNamespace(state=SimpleNamespace()), 'https://adapter.example/search', 'synthetic-key', 'query', 3)
    assert result == records
    assert calls[0][1]['json'] == {'query': 'query', 'count': 3}
    assert calls[0][1]['headers']['Authorization'] == 'Bearer synthetic-key'


def test_pinned_external_loader_post_contract():
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [
            {'page_content': 'Rendered fixture', 'metadata': {'source': 'https://public.example/page'}}])
    typing = __import__('typing')
    namespace = {'List': list, 'Iterator': typing.Iterator, 'Union': typing.Union,
                 'BaseLoader': object, 'Document': SimpleNamespace, 'requests': SimpleNamespace(post=post),
                 'log': logging.getLogger(__name__)}
    loader = definition('backend/open_webui/retrieval/loaders/external_web.py', 'ExternalWebLoader', namespace)
    documents = list(loader(['https://public.example/page'], 'https://adapter.example/load', 'synthetic-key').lazy_load())
    assert calls[0][1]['json'] == {'urls': ['https://public.example/page']}
    assert calls[0][1]['headers']['Authorization'] == 'Bearer synthetic-key'
    assert documents[0].page_content == 'Rendered fixture'
    assert documents[0].metadata['source'] == 'https://public.example/page'
