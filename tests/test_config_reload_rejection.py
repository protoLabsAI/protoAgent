"""Reject malformed configuration before changing live runtime or credentials."""
import os
from types import SimpleNamespace

import pytest

from graph.config import LangGraphConfig
from graph.config_load import ConfigLoadError
from graph.plugins.pconfig import PluginConfigSchema
from runtime.state import STATE

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTOAGENT_BOX_ROOT', str(tmp_path / 'box'))
    monkeypatch.setenv('PROTOAGENT_HOME', str(tmp_path / 'instance'))
    monkeypatch.setenv('PROTOAGENT_HOST_CONFIG', str(tmp_path / 'host.yaml'))
    monkeypatch.setattr('graph.plugins.pconfig.discover_plugin_config', lambda *a, **k: [PluginConfigSchema('sample', 'sample')])
    path = tmp_path / 'agent.yaml'
    path.write_text('secrets_manager:\n  enabled: true\nsample: []\n')
    import graph.config_io as cio
    monkeypatch.setattr(cio, 'config_yaml_path', lambda: path)
    monkeypatch.setattr(cio, 'ensure_live_config', lambda: None)
    return path


def forbid_hydration(monkeypatch):
    calls = []
    def hydrate(*args):
        calls.append(True)
        monkeypatch.setenv('OPENAI_API_KEY', 'changed')
    monkeypatch.setattr('graph.config._hydrate_external_secrets', hydrate)
    return calls


@pytest.mark.parametrize('bad', ['sample: []', 'sample: false', 'sample: 0', 'plugins: scalar', 'auth: [unclosed'])
def test_rejected_reload_retains_live_state_and_environment(config_file, monkeypatch, bad):
    import server.agent_init as ai
    import tools.lg_tools as tools
    config_file.write_text('secrets_manager:\n  enabled: true\n' + bad + '\n')
    graph = object()
    config = SimpleNamespace(auth_token='old-token')
    monkeypatch.setattr(STATE, 'graph', graph)
    monkeypatch.setattr(STATE, 'graph_config', config)
    monkeypatch.setattr(tools, '_disabled_tools', {'shell'})
    monkeypatch.setenv('OPENAI_API_KEY', 'existing')
    before = dict(os.environ)
    calls = forbid_hydration(monkeypatch)
    ok, message = ai._reload_langgraph_agent()
    assert not ok and 'config load failed' in message
    assert STATE.graph is graph and STATE.graph_config is config
    assert tools._disabled_tools == {'shell'}
    assert dict(os.environ) == before
    assert calls == []


def test_startup_rejects_before_publishing_config(config_file, monkeypatch):
    import graph.config_io as cio
    import server.agent_init as ai
    monkeypatch.setattr(cio, 'apply_seed_merge', lambda: None)
    monkeypatch.setattr(cio, 'ensure_live_soul', lambda: None)
    monkeypatch.setattr('infra.paths.check_data_version', lambda: None)
    monkeypatch.setattr('infra.node_runtime.augment_path_with_managed_node', lambda: None)
    monkeypatch.setattr(STATE, 'graph_config', None)
    calls = forbid_hydration(monkeypatch)
    with pytest.raises(ConfigLoadError, match='sample: expected a mapping'):
        ai._init_langgraph_agent()
    assert STATE.graph_config is None
    assert calls == []


def test_valid_config_hydrates_once_without_rediscovery(config_file, monkeypatch):
    config_file.write_text('secrets_manager:\n  enabled: true\nsample: {allow: []}\n')
    discoveries = []
    def discover(*a, **kwargs):
        discoveries.append(True)
        if len(discoveries) > 1:
            raise RuntimeError('discovery changed')
        return [PluginConfigSchema('sample', 'sample')]
    monkeypatch.setattr('graph.plugins.pconfig.discover_plugin_config', discover)
    calls = forbid_hydration(monkeypatch)
    config = LangGraphConfig.from_yaml(config_file)
    assert config.plugin_config == {'sample': {'allow': []}}
    assert len(discoveries) == len(calls) == 1
    assert os.environ['OPENAI_API_KEY'] == 'changed'


def test_valid_hydration_refreshes_environment_defaults(config_file, monkeypatch):
    config_file.write_text('secrets_manager:\n  enabled: true\n')
    monkeypatch.delenv('PROTOAGENT_MODEL', raising=False)
    monkeypatch.setattr('graph.config._hydrate_external_secrets', lambda *a: monkeypatch.setenv('PROTOAGENT_MODEL', 'hydrated-model'))
    assert LangGraphConfig.from_yaml(config_file).model_name == 'hydrated-model'


def test_secret_sync_rejects_invalid_candidate_before_applying(config_file, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from operator_api.secrets_routes import register_secrets_routes
    calls = []
    monkeypatch.setattr('infra.secrets.hydrate_from_docs', lambda *a, **k: calls.append(True))
    app = FastAPI()
    register_secrets_routes(app)
    with TestClient(app) as client:
        response = client.post('/api/secrets/sync')
    assert response.status_code == 400
    assert 'sample: expected a mapping' in response.json()['detail']
    assert calls == []


@pytest.mark.asyncio
async def test_background_refresh_rejects_invalid_candidate(config_file, monkeypatch, caplog):
    import asyncio
    import server.maintenance_loops as loops
    calls, sleeps = [], []
    monkeypatch.setattr(STATE, 'graph_config', SimpleNamespace(secrets_manager_enabled=True, secrets_manager_refresh_seconds=30))
    monkeypatch.setattr('infra.secrets.hydrate_from_docs', lambda *a, **k: calls.append(True))
    async def one_iteration(*args):
        sleeps.append(True)
        if len(sleeps) > 1:
            raise asyncio.CancelledError()
    monkeypatch.setattr(asyncio, 'sleep', one_iteration)
    with pytest.raises(asyncio.CancelledError):
        await loops._secrets_refresh_loop()
    assert calls == []
    assert 'sample: expected a mapping' in caplog.text


@pytest.mark.parametrize('failure', ['configuration', 'provider'])
def test_secret_connection_test_contains_errors_without_values(config_file, monkeypatch, failure, caplog):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from operator_api.secrets_routes import register_secrets_routes
    def broken_fetch(cfg):
        raise TimeoutError('DO_NOT_ECHO_SECRET')
    monkeypatch.setattr('infra.secrets.get_provider', lambda _: SimpleNamespace(fetch=broken_fetch))
    if failure == 'configuration':
        config_file.write_text('auth: [DO_NOT_ECHO_SECRET')
    before = dict(os.environ)
    app = FastAPI()
    register_secrets_routes(app)
    with TestClient(app) as client:
        response = client.post('/api/secrets/test', json={})
    assert response.status_code == (400 if failure == 'configuration' else 200)
    if failure == 'provider':
        assert response.json()['ok'] is False
        assert response.json()['error_kind'] == 'internal'
        assert 'provider violated the fetch result contract' in caplog.text
    assert 'DO_NOT_ECHO_SECRET' not in caplog.text
    assert 'DO_NOT_ECHO_SECRET' not in response.text
    assert dict(os.environ) == before


def test_connection_test_preserves_opaque_credential_whitespace(config_file, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from infra.secrets import FetchResult
    from operator_api.secrets_routes import register_secrets_routes
    seen = []
    def fetch(cfg):
        seen.append(cfg.client_secret)
        return FetchResult(values={})
    monkeypatch.setattr('infra.secrets.get_provider', lambda _: SimpleNamespace(fetch=fetch))
    app = FastAPI()
    register_secrets_routes(app)
    with TestClient(app) as client:
        response = client.post('/api/secrets/test', json={'client_secret': ' opaque credential '})
    assert response.json()['ok'] is True
    assert seen == [' opaque credential ']


@pytest.mark.parametrize('route,seam', [
    ('sync', 'infra.secrets.hydrate_from_docs'),
    ('test', 'infra.secrets.source_from_docs'),
])
def test_secrets_route_unexpected_failure_is_safe(config_file, monkeypatch, caplog, route, seam):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from operator_api.secrets_routes import register_secrets_routes
    config_file.write_text('{}')
    def broken(*a, **k):
        raise RuntimeError('DO_NOT_ECHO_SECRET')
    monkeypatch.setattr(seam, broken)
    app = FastAPI()
    register_secrets_routes(app)
    before = dict(os.environ)
    with TestClient(app) as client:
        response = client.post(f'/api/secrets/{route}', json={})
    assert response.status_code == 500
    assert 'failed unexpectedly' in response.json()['detail']
    assert 'DO_NOT_ECHO_SECRET' not in response.text + caplog.text
    assert dict(os.environ) == before


def test_invalid_source_allowlist_returns_configuration_error(config_file, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from operator_api.secrets_routes import register_secrets_routes
    config_file.write_text('plugins:\n  sources:\n    allow: invalid\n')
    calls = []
    monkeypatch.setattr('infra.secrets.hydrate_from_docs', lambda *a, **k: calls.append(True))
    app = FastAPI()
    register_secrets_routes(app)
    with TestClient(app) as client:
        response = client.post('/api/secrets/sync')
    assert response.status_code == 400
    assert 'plugins.sources.allow' in response.json()['detail']
    assert calls == []


def test_invalid_secrets_connection_fields_are_safe(config_file):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from operator_api.secrets_routes import register_secrets_routes
    config_file.write_text('secrets_manager:\n  timeout_seconds: DO_NOT_ECHO_SECRET\n')
    app = FastAPI()
    register_secrets_routes(app)
    with TestClient(app) as client:
        response = client.post('/api/secrets/test', json={})
    assert response.status_code == 400
    assert 'DO_NOT_ECHO_SECRET' not in response.text


@pytest.mark.parametrize('outcome,status_code', [('success', 200), ('disabled', 400), ('required', 502)])
def test_secret_sync_outcomes(config_file, monkeypatch, outcome, status_code):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from infra.secrets import SecretsRequiredError
    from operator_api.secrets_routes import register_secrets_routes
    config_file.write_text('{}')
    def hydrate(*a, **k):
        assert k['force'] is True
        if outcome == 'required':
            raise SecretsRequiredError('required source unavailable')
        return None if outcome == 'disabled' else object()
    monkeypatch.setattr('infra.secrets.hydrate_from_docs', hydrate)
    monkeypatch.setattr('infra.secrets.status', lambda: {'ok': True, 'names': ['EXAMPLE_KEY']})
    app = FastAPI()
    register_secrets_routes(app)
    with TestClient(app) as client:
        response = client.post('/api/secrets/sync')
        assert client.get('/api/secrets/status').json() == {'ok': True, 'names': ['EXAMPLE_KEY']}
    assert response.status_code == status_code
    if outcome == 'success':
        assert response.json() == {'ok': True, 'names': ['EXAMPLE_KEY']}
