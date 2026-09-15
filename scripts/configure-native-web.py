"""Opt-in native shared-service configuration for unmodified Open WebUI v0.11.3.

Secrets come from environment variables, never arguments or printed plans.
Back up only changed settings privately before enabling. Preserve all unrelated
web settings because this release replaces the complete web configuration block.
Rollback restores only our changed settings and refuses consequential drift.
"""

import argparse
import copy
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx


MARKER = 'Managed AgentCore native shared-service phase1'
MODELS = {
    'agentcore-native-web-haiku': ('AgentCore native web (Haiku)', 'gateway_anthropic.anthropic.claude-haiku-4-5'),
    'agentcore-native-web-responses': ('AgentCore native web (Responses)', 'gw-west.openai.gpt-6-astra'),
}


class ConfigurationError(RuntimeError):
    pass


def settings(endpoint, service_key):
    parsed = urlsplit(endpoint)
    if (parsed.scheme != 'https' or not re.fullmatch(r'[a-z0-9]+\.lambda-url\.[a-z0-9-]+\.on\.aws', parsed.netloc)
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment
            or not isinstance(service_key, str) or not 32 <= len(service_key) <= 256):
        raise ConfigurationError('Expected a verified Lambda URL and strong service key')
    return {
        'ENABLE_WEB_SEARCH': True,
        'WEB_SEARCH_ENGINE': 'external',
        'EXTERNAL_WEB_SEARCH_URL': endpoint.rstrip('/') + '/search',
        'EXTERNAL_WEB_SEARCH_API_KEY': service_key,
        'WEB_LOADER_ENGINE': 'external',
        'EXTERNAL_WEB_LOADER_URL': endpoint.rstrip('/') + '/load',
        'EXTERNAL_WEB_LOADER_API_KEY': service_key,
        'WEB_SEARCH_RESULT_COUNT': 3,
        'WEB_SEARCH_CONCURRENT_REQUESTS': 1,
        'WEB_LOADER_CONCURRENT_REQUESTS': 1,
        'WEB_FETCH_MAX_CONTENT_LENGTH': 20000,
        'ENABLE_WEB_LOADER_SSL_VERIFICATION': True,
        'WEB_SEARCH_TRUST_ENV': False,
        'BYPASS_WEB_SEARCH_WEB_LOADER': True,
        'BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL': True,
        'ENABLE_WEB_SEARCH_CONFIRMATION': True,
        'WEB_SEARCH_CONFIRMATION_CONTENT': (
            'Web Search uses AWS-managed search with shared service limits. Queries leave this chat application. '
            'Keep source links/citations; snippets are not live page fetches. Do not submit secrets, bulk collect '
            'results, or save search-result content into knowledge/indexes pending retention review. '
            'Public HTML/text reading uses bounded HTTPS; configured JavaScript pages use AgentCore Browser.'
        ),
    }


class NativeConfigurator:
    def __init__(self, client, *, endpoint, service_key, backup_path, maintenance_confirmed=False):
        self.client = client
        self.desired = settings(endpoint, service_key)
        self.backup_path = Path(backup_path)
        self.maintenance_confirmed = maintenance_confirmed is True

    def request(self, method, route, body=None, optional=False):
        response = self.client.request(method, route, json=body)
        if optional and response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ConfigurationError(f'Native configuration API failed: {method} {route.split("?")[0]} HTTP {response.status_code}')
        return response.json()

    def inspect(self):
        if self.request('GET', '/api/version').get('version') != '0.11.3':
            raise ConfigurationError('Revalidate the pinned Open WebUI release before configuring')
        own = self.request('GET', '/api/v1/auths/')
        if own.get('role') != 'admin' or not own.get('id'):
            raise ConfigurationError('An explicitly authorized admin session is required')
        self.owner = own['id']
        self.web = self.request('GET', '/api/v1/retrieval/config')['web']
        if not set(self.desired).issubset(self.web):
            raise ConfigurationError('The running release lacks required settings')
        return {'action': 'plan', 'changed_fields': sorted(key for key in self.desired if self.web[key] != self.desired[key]),
                'identity': 'shared service, not verified per-user attribution', 'automatic_vector_ingestion': False}

    def _fresh(self, expected):
        if not self.maintenance_confirmed:
            raise ConfigurationError('Confirm an exclusive configuration maintenance window; the upstream API has no compare-and-swap')
        fresh = self.request('GET', '/api/v1/retrieval/config')['web']
        if any(fresh[key] != expected[key] for key in self.desired):
            raise ConfigurationError('Native settings drifted since inspection; inspect again before changing them')
        return fresh

    def _write_backup(self, backup):
        temporary = self.backup_path.with_suffix('.new')
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
            json.dump(backup, output)
        os.replace(temporary, self.backup_path)

    def apply(self, create_models=False):
        self.web = self._fresh(self.web)
        if self.backup_path.exists():
            backup = self._backup()
            if backup['installed'] != self.desired:
                raise ConfigurationError('Existing backup belongs to a different endpoint/key/configuration')
            if any(self.web[key] not in (backup['previous'][key], self.desired[key]) for key in self.desired):
                raise ConfigurationError('Native settings drifted; preserve and review before reapplying')
        else:
            self.backup_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup = {'owner': self.owner, 'previous': {key: self.web[key] for key in self.desired}, 'installed': self.desired, 'models': []}
            with os.fdopen(os.open(self.backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
                json.dump(backup, output)
        if create_models:
            planned = self._inspect_models()
            backup['models'] = sorted(set(backup.get('models', [])) | {form['id'] for form, existing in planned})
            self._write_backup(backup)
            for form, existing in planned:
                if existing is None:
                    self._set_active(form, False, create=True)
                elif existing['is_active'] and any(self.web[key] != self.desired[key] for key in self.desired):
                    self._set_active(form, False)
        updated = {**copy.deepcopy(self._fresh(self.web)), **self.desired}
        self.request('POST', '/api/v1/retrieval/config/update', {'web': updated})
        actual = self.request('GET', '/api/v1/retrieval/config')['web']
        if actual != updated:
            raise ConfigurationError('Persisted web settings differ from the intended merge; inspect private backup')
        self.web = actual
        if create_models:
            for form, existing in self._inspect_models():
                if existing is None:
                    raise ConfigurationError('Staged native preset disappeared before activation')
                if not existing['is_active']:
                    self._set_active(form, True)
            catalog = {item['id'] for item in self.request('GET', '/api/models')['data']}
            if not set(MODELS) <= catalog:
                raise ConfigurationError('New native presets missing from the refreshed catalog')
        return {'action': 'configured', 'native_search': True, 'loader': 'external', 'automatic_vector_ingestion': False}

    def _backup(self):
        if self.backup_path.stat().st_mode & 0o077:
            raise ConfigurationError('Backup with service credentials must be private (0600)')
        backup = json.loads(self.backup_path.read_text())
        if backup.get('owner') != self.owner or set(backup.get('previous', {})) != set(self.desired):
            raise ConfigurationError('Backup owner/schema mismatch')
        return backup

    def rollback(self):
        backup = self._backup()
        self.web = self._fresh(self.web)
        if any(self.web[key] not in (backup['installed'][key], backup['previous'][key]) for key in self.desired):
            raise ConfigurationError('Native settings changed since installation; refusing to overwrite drift')
        tracked = backup.get('models', [])
        if tracked:
            planned = self._inspect_models(check_catalog=False)
            for form, existing in planned:
                if form['id'] in tracked and existing and existing['is_active']:
                    self._set_active(form, False)
        restored = {**self._fresh(self.web), **backup['previous']}
        self.request('POST', '/api/v1/retrieval/config/update', {'web': restored})
        if self.request('GET', '/api/v1/retrieval/config')['web'] != restored:
            raise ConfigurationError('Rollback verification failed')
        return {'action': 'rolled-back', 'chat_data_deleted': False, 'unrelated_settings_preserved': True}

    def _set_active(self, form, active, create=False):
        route = '/api/v1/models/create' if create else '/api/v1/models/model/update?id=' + form['id']
        result = self.request('POST', route, {**form, 'is_active': active})
        if not isinstance(result, dict) or result.get('id') != form['id'] or result.get('is_active') is not active:
            raise ConfigurationError('Native preset update returned invalid or unconfirmed state')
        actual = next(existing for candidate, existing in self._inspect_models(check_catalog=False) if candidate['id'] == form['id'])
        if actual is None or actual.get('is_active') is not active:
            raise ConfigurationError('Native preset persisted state did not match the requested activation')

    def _inspect_models(self, check_catalog=True):
        if check_catalog:
            catalog = {item['id'] for item in self.request('GET', '/api/models')['data']}
            if not {definition[1] for definition in MODELS.values()} <= catalog:
                raise ConfigurationError('Required existing model lanes unavailable; do not enable connections implicitly')
        forms = []
        for identifier, (name, base) in MODELS.items():
            form = {'id': identifier, 'base_model_id': base, 'name': name, 'is_active': True, 'access_grants': [],
                    'params': {'function_calling': 'native', 'max_tokens': 1024},
                    'meta': {'description': MARKER, 'toolIds': [], 'filterIds': [], 'knowledge': [],
                             'builtinTools': {category: category == 'web_search' for category in (
                                 'automations', 'calendar', 'channels', 'chats', 'code_interpreter', 'files',
                                 'image_generation', 'knowledge', 'memory', 'notes', 'notifications',
                                 'subagents', 'tasks', 'time', 'user_input', 'web_search')},
                             'capabilities': {'builtin_tools': True, 'web_search': True, 'citations': True,
                                              'file_upload': False, 'file_context': False}}}
            existing = self.request('GET', '/api/v1/models/model?id=' + identifier, optional=True)
            if existing:
                normalized = {key: copy.deepcopy(existing.get(key)) for key in form}
                if isinstance(normalized.get('meta'), dict) and normalized['meta'].get('profile_image_url') is None:
                    normalized['meta'].pop('profile_image_url', None)
                normalized['is_active'] = True
                if (existing.get('user_id') != self.owner or type(existing.get('is_active')) is not bool
                        or normalized != form):
                    raise ConfigurationError('Existing native model differs; preserve administrator customization')
            forms.append((form, existing))
        return forms


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--backup', required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--apply', action='store_true')
    action.add_argument('--rollback', action='store_true')
    parser.add_argument('--create-models', action='store_true')
    parser.add_argument('--maintenance-window', action='store_true')
    arguments = parser.parse_args()
    base = urlsplit(arguments.base_url)
    if base.scheme != 'https' or not base.hostname or base.username or base.password or base.query or base.fragment or base.path not in ('', '/'):
        raise ConfigurationError('Expected the verified HTTPS application origin')
    token, key = os.environ.get('OWUI_ADMIN_TOKEN', ''), os.environ.get('NATIVE_WEB_SERVICE_KEY', '')
    if not token:
        raise ConfigurationError('Supply your authorized admin session through OWUI_ADMIN_TOKEN')
    with httpx.Client(base_url=arguments.base_url.rstrip('/'), headers={'Authorization': 'Bearer ' + token},
                      timeout=40, follow_redirects=False, trust_env=False) as client:
        configurator = NativeConfigurator(client, endpoint=arguments.endpoint, service_key=key, backup_path=arguments.backup,
                                          maintenance_confirmed=arguments.maintenance_window)
        result = configurator.inspect()
        if arguments.apply:
            result.update(configurator.apply(create_models=arguments.create_models))
        elif arguments.rollback:
            result = configurator.rollback()
        elif arguments.create_models:
            raise ConfigurationError('--create-models requires --apply')
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
