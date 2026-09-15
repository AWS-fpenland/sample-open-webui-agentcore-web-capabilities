"""Native configuration persistence and drift-preserving rollback contracts."""

import copy
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


KEY = 'synthetic-service-key-not-a-real-secret-0123456789'
ENDPOINT = 'https://synthetic.lambda-url.us-east-1.on.aws/'
SPEC = importlib.util.spec_from_file_location('configure_native_web', Path(__file__).resolve().parents[2] / 'scripts/configure-native-web.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class API:
    def __init__(self):
        self.web = {key: None for key in MODULE.settings(ENDPOINT, KEY)}
        self.web.update({'ENABLE_WEB_SEARCH': False, 'WEB_SEARCH_ENGINE': '', 'unrelated_provider_key': 'preserve-private-value'})
        self.before = copy.deepcopy(self.web)
        self.models = {}
        self.writes = []
        self.version = '0.11.3'
        self.role = 'admin'
        self.model_update_failure = None

    def handle(self, request):
        route = request.url.raw_path.decode()
        if request.method == 'POST':
            payload = json.loads(request.content)
            self.writes.append((route, payload))
            if route == '/api/v1/retrieval/config/update':
                assert set(payload) == {'web'}
                self.web = payload['web']
            elif route == '/api/v1/models/create':
                self.models[payload['id']] = {**payload, 'meta': {**payload['meta'], 'profile_image_url': None}, 'user_id': 'own-admin'}
                return httpx.Response(200, json=self.models[payload['id']])
            elif route.startswith('/api/v1/models/model/update?id='):
                identifier = route.split('=', 1)[1]
                if self.model_update_failure == 'null':
                    return httpx.Response(200, content=b'null', headers={'content-type': 'application/json'})
                if self.model_update_failure == 'unchanged':
                    return httpx.Response(200, json={**self.models[identifier], 'is_active': payload['is_active']})
                self.models[identifier] = {**payload, 'meta': {**payload['meta'], 'profile_image_url': None}, 'user_id': 'own-admin'}
                return httpx.Response(200, json=self.models[identifier])
            else:
                raise AssertionError('Unexpected mutation')
            return httpx.Response(200, json={'ok': True})
        if route == '/api/version':
            return httpx.Response(200, json={'version': self.version})
        if route == '/api/v1/auths/':
            return httpx.Response(200, json={'id': 'own-admin', 'role': self.role})
        if route == '/api/v1/retrieval/config':
            return httpx.Response(200, json={'web': self.web, 'other_rag_setting': 'preserve'})
        if route == '/api/models':
            return httpx.Response(200, json={'data': [{'id': identifier} for identifier in
                [definition[1] for definition in MODULE.MODELS.values()] + list(self.models)]})
        if route.startswith('/api/v1/models/model?id='):
            model = self.models.get(route.split('=', 1)[1])
            return httpx.Response(200 if model else 404, json=model or {})
        raise AssertionError('Unexpected route')


@pytest.fixture
def setup(tmp_path):
    api = API()
    with httpx.Client(base_url='https://app.example.invalid', transport=httpx.MockTransport(api.handle)) as client:
        configurator = MODULE.NativeConfigurator(client, endpoint=ENDPOINT, service_key=KEY, backup_path=tmp_path / 'private.json', maintenance_confirmed=True)
        yield api, configurator


def test_plan_never_writes_or_discloses_secrets(setup):
    api, configurator = setup
    report = configurator.inspect()
    assert report['action'] == 'plan' and not api.writes
    assert KEY not in json.dumps(report) and 'preserve-private-value' not in json.dumps(report)
    assert not configurator.backup_path.exists()


def test_apply_preserves_unrelated_fields_and_uses_native_contract(setup):
    api, configurator = setup
    configurator.inspect()
    configurator.apply()
    assert api.web['unrelated_provider_key'] == api.before['unrelated_provider_key']
    assert api.web['EXTERNAL_WEB_SEARCH_URL'] == ENDPOINT + 'search'
    assert api.web['EXTERNAL_WEB_LOADER_URL'] == ENDPOINT + 'load'
    assert api.web['BYPASS_WEB_SEARCH_WEB_LOADER'] and api.web['BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL']
    assert api.web['ENABLE_WEB_SEARCH'] and api.web['WEB_LOADER_ENGINE'] == 'external'
    assert configurator.backup_path.stat().st_mode & 0o077 == 0
    configurator.inspect()
    configurator.apply()
    assert json.loads(configurator.backup_path.read_text())['previous'] == {key: api.before[key] for key in configurator.desired}


def test_rollback_restores_only_owned_settings_and_keeps_new_unrelated_edits(setup):
    api, configurator = setup
    configurator.inspect()
    configurator.apply()
    api.web['unrelated_provider_key'] = 'new-owner-setting'
    configurator.inspect()
    configurator.rollback()
    assert api.web['ENABLE_WEB_SEARCH'] is False
    assert api.web['unrelated_provider_key'] == 'new-owner-setting'


@pytest.mark.parametrize('operation', ['apply', 'rollback'])
def test_consequential_drift_refuses_mutation(setup, operation):
    api, configurator = setup
    configurator.inspect()
    configurator.apply()
    api.web['WEB_SEARCH_ENGINE'] = 'owner-changed-provider'
    configurator.inspect()
    before = len(api.writes)
    with pytest.raises(MODULE.ConfigurationError, match='drift|changed'):
        getattr(configurator, operation)()
    assert len(api.writes) == before


def test_owned_native_presets_use_builtins_not_explicit_canary_tools(setup):
    api, configurator = setup
    configurator.inspect()
    configurator.apply(create_models=True)
    assert len(api.models) == 2
    for model in api.models.values():
        assert model['params']['function_calling'] == 'native'
        assert model['meta']['capabilities']['builtin_tools'] and model['meta']['capabilities']['web_search']
        assert model['meta']['toolIds'] == model['meta']['filterIds'] == model['meta']['knowledge'] == []
        assert [category for category, enabled in model['meta']['builtinTools'].items() if enabled] == ['web_search']
    assert [payload['is_active'] for route, payload in api.writes if route == '/api/v1/models/create'] == [False, False]
    configurator.inspect()
    configurator.apply(create_models=True)
    assert len([route for route, payload in api.writes if route == '/api/v1/models/create']) == 2


def test_presets_refuse_unowned_or_modified_models(setup):
    api, configurator = setup
    configurator.inspect()
    configurator.apply(create_models=True)
    api.models['agentcore-native-web-haiku']['user_id'] = 'someone-else'
    with pytest.raises(MODULE.ConfigurationError, match='Existing native model'):
        configurator._inspect_models()


@pytest.mark.parametrize('operation', ['apply', 'rollback'])
def test_fresh_read_preserves_changes_between_inspection_and_write(setup, operation):
    api, configurator = setup
    configurator.inspect()
    if operation == 'rollback':
        configurator.apply()
        configurator.inspect()
    api.web['unrelated_provider_key'] = 'changed-after-inspect'
    getattr(configurator, operation)()
    assert api.web['unrelated_provider_key'] == 'changed-after-inspect'


def test_changed_native_setting_after_inspect_refused(setup):
    api, configurator = setup
    configurator.inspect()
    api.web['WEB_SEARCH_ENGINE'] = 'changed-after-inspect'
    with pytest.raises(MODULE.ConfigurationError, match='drifted since'):
        configurator.apply()
    assert not api.writes


def test_rollback_deactivates_presets_before_restoring_provider(setup):
    api, configurator = setup
    configurator.inspect()
    configurator.apply(create_models=True)
    api.writes.clear()
    configurator.inspect()
    configurator.rollback()
    assert all(not model['is_active'] for model in api.models.values())
    assert [route for route, payload in api.writes][-1] == '/api/v1/retrieval/config/update'


def test_configuration_failure_keeps_staged_models_inactive(setup, monkeypatch):
    api, configurator = setup
    configurator.inspect()
    original = configurator.request
    def fail(method, route, *args, **kwargs):
        if method == 'POST' and route == '/api/v1/retrieval/config/update':
            raise MODULE.ConfigurationError('synthetic write failure')
        return original(method, route, *args, **kwargs)
    monkeypatch.setattr(configurator, 'request', fail)
    with pytest.raises(MODULE.ConfigurationError, match='synthetic write'):
        configurator.apply(create_models=True)
    assert len(api.models) == 2 and all(not model['is_active'] for model in api.models.values())
    monkeypatch.setattr(configurator, 'request', original)
    configurator.inspect()
    configurator.apply(create_models=True)
    assert all(model['is_active'] for model in api.models.values())


def test_mutation_requires_exclusive_configuration_maintenance(setup):
    api, configurator = setup
    configurator.inspect()
    configurator.maintenance_confirmed = False
    with pytest.raises(MODULE.ConfigurationError, match='maintenance'):
        configurator.apply()
    assert not api.writes


@pytest.mark.parametrize('failure', ['null', 'unchanged'])
def test_unconfirmed_deactivation_blocks_provider_rollback(setup, failure):
    api, configurator = setup
    configurator.inspect()
    configurator.apply(create_models=True)
    api.model_update_failure = failure
    api.writes.clear()
    configurator.inspect()
    with pytest.raises(MODULE.ConfigurationError):
        configurator.rollback()
    assert api.web['ENABLE_WEB_SEARCH'] is True
    assert not any(route == '/api/v1/retrieval/config/update' for route, payload in api.writes)


@pytest.mark.parametrize('version,role', [('0.11.4', 'admin'), ('0.11.3', 'user')])
def test_wrong_version_or_nonadmin_refused(setup, version, role):
    api, configurator = setup
    api.version, api.role = version, role
    with pytest.raises(MODULE.ConfigurationError):
        configurator.inspect()
    assert not api.writes


@pytest.mark.parametrize('endpoint', ['http://localhost/', 'https://example.com/', ENDPOINT + '?key=value', ENDPOINT + 'load'])
def test_unverified_endpoint_shapes_refused(endpoint):
    with pytest.raises(MODULE.ConfigurationError):
        MODULE.settings(endpoint, KEY)


def test_missing_supported_setting_refused(setup):
    api, configurator = setup
    del api.web['BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL']
    with pytest.raises(MODULE.ConfigurationError, match='lacks required'):
        configurator.inspect()


def test_world_readable_backup_refused(setup):
    api, configurator = setup
    configurator.inspect()
    configurator.apply()
    configurator.backup_path.chmod(0o644)
    configurator.inspect()
    with pytest.raises(MODULE.ConfigurationError, match='private'):
        configurator.rollback()
