"""Invalid configuration cannot silently turn into default policy."""
from pathlib import Path

import pytest

from graph.config import LangGraphConfig
from graph.config_load import ConfigLoadError, _load_secrets_doc, _read_config_docs, _resolve_plugin_config
from graph.plugins.pconfig import PluginConfigSchema

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTOAGENT_BOX_ROOT', str(tmp_path / 'box'))
    monkeypatch.setenv('PROTOAGENT_HOME', str(tmp_path / 'instance'))
    monkeypatch.setenv('PROTOAGENT_HOST_CONFIG', str(tmp_path / 'host-config.yaml'))


@pytest.mark.parametrize('document', ['[]', 'false', '0', 'text', 'auth: [SECRET_VALUE'])
@pytest.mark.parametrize('target', ['agent', 'host', 'secrets'])
def test_invalid_files_reject_without_echoing_values(tmp_path, document, target):
    path = tmp_path / 'agent.yaml'
    path.write_text('{}')
    bad = {'agent': path, 'host': tmp_path / 'host-config.yaml', 'secrets': tmp_path / 'secrets.yaml'}[target]
    bad.write_text(document)
    with pytest.raises(ConfigLoadError) as exc:
        LangGraphConfig.from_yaml(path)
    assert str(bad) in str(exc.value)
    assert 'SECRET_VALUE' not in str(exc.value)
    assert exc.value.__suppress_context__ or 'expected a mapping' in str(exc.value)


@pytest.mark.parametrize('target', ['agent', 'host', 'secrets'])
def test_directory_at_file_path_rejected(tmp_path, target):
    path = tmp_path / 'agent.yaml'
    bad = {'agent': path, 'host': tmp_path / 'host-config.yaml', 'secrets': tmp_path / 'secrets.yaml'}[target]
    bad.mkdir()
    with pytest.raises(ConfigLoadError, match='unreadable configuration file'):
        LangGraphConfig.from_yaml(path)


def test_read_permission_error_is_not_absence(tmp_path, monkeypatch):
    path = tmp_path / 'secrets.yaml'
    path.write_text('{}')
    original = Path.open
    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError('SECRET_VALUE')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', denied)
    with pytest.raises(ConfigLoadError, match='unreadable') as exc:
        _load_secrets_doc(tmp_path)
    assert 'SECRET_VALUE' not in str(exc.value)


def test_missing_optional_files_and_empty_documents(tmp_path):
    assert _read_config_docs(tmp_path / 'agent.yaml') == ({}, {}, False)
    (tmp_path / 'agent.yaml').write_text('# empty\n')
    assert _read_config_docs(tmp_path / 'agent.yaml') == ({}, {}, True)


@pytest.fixture
def schema(monkeypatch):
    monkeypatch.setattr('graph.plugins.pconfig.discover_plugin_config', lambda *a, **k: [
        PluginConfigSchema('sample', 'sample', defaults={'allow': ['default']}, secrets=['token'])
    ])


@pytest.mark.parametrize('value', [False, 0, [], 'SECRET_VALUE', 12])
@pytest.mark.parametrize('overlay', [False, True])
def test_plugin_section_shapes_rejected(tmp_path, schema, value, overlay):
    data, secrets = ({}, {'sample': value}) if overlay else ({'sample': value}, {})
    with pytest.raises(ConfigLoadError, match='sample: expected a mapping') as exc:
        _resolve_plugin_config(data, secrets, tmp_path)
    assert 'SECRET_VALUE' not in str(exc.value)


def test_explicit_empty_policy_and_secret_preserved(tmp_path, schema):
    result = _resolve_plugin_config({'sample': {'allow': [], 'token': 'inline'}}, {'sample': {'token': ''}}, tmp_path)
    assert result == {'sample': {'allow': [], 'token': ''}}


def test_discovery_failure_rejects_without_exception_values(tmp_path, monkeypatch):
    def fail(*a, **kwargs):
        assert kwargs['strict'] is True
        raise RuntimeError('SECRET_VALUE')
    monkeypatch.setattr('graph.plugins.pconfig.discover_plugin_config', fail)
    with pytest.raises(ConfigLoadError, match='schema discovery failed') as exc:
        _resolve_plugin_config({}, {}, tmp_path)
    assert 'SECRET_VALUE' not in str(exc.value)


@pytest.mark.parametrize('value', [False, [], 0, 'string'])
def test_from_dict_rejects_wrong_top_level_types(value):
    with pytest.raises(ConfigLoadError):
        LangGraphConfig.from_dict(value)


@pytest.mark.parametrize('value', ['false', '[]', '0', 'scalar'])
def test_host_section_cannot_disappear_during_filtering(tmp_path, value):
    (tmp_path / 'host-config.yaml').write_text('model: ' + value)
    with pytest.raises(ConfigLoadError, match='host-config.yaml: model: expected a mapping'):
        LangGraphConfig.from_yaml(tmp_path / 'agent.yaml')
