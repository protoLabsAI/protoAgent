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
