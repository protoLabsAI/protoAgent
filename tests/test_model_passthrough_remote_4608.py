"""Remote passthrough opt-in keeps operator bearer authentication (#4608 part 2)."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from a2a_impl import auth
from graph.config import LangGraphConfig
from graph.config_io import apply_updates_to_yaml, config_to_dict, save_yaml_doc
from graph.settings_schema import FIELDS, _SECTION_CATEGORY
from operator_api.model_passthrough_routes import register_model_passthrough_routes
from runtime.state import STATE

PATH = "/api/models/local/v1/chat/completions"
BODY = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]}
HEADERS = {"Authorization": "Bearer remote-test-token"}


def _config(allow_remote=None):
    doc = {"providers": [{"id": "local", "type": "openai-compat", "base_url": "http://localhost:8000/v1"}]}
    if allow_remote is not None:
        doc["model_passthrough"] = {"allow_remote": allow_remote}
    return LangGraphConfig.from_dict(doc)


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setattr(STATE, "graph_config", _config())
    monkeypatch.setattr(STATE, "telemetry_store", None)
    monkeypatch.setattr(auth, "_BEARER", ["remote-test-token"])
    model = AsyncMock()
    model.ainvoke.return_value = AIMessage(content="remote reply")
    monkeypatch.setattr("graph.llm._build_gateway_llm", lambda *args, **kwargs: model)
    app = FastAPI()
    register_model_passthrough_routes(app)
    app.add_middleware(auth.A2AAuthMiddleware)
    return app, model


def test_remote_setting_is_read_live_per_request(harness, monkeypatch):
    app, model = harness
    assert STATE.graph_config.model_passthrough_allow_remote is False
    with TestClient(app, client=("10.1.2.3", 5000)) as client:
        for enabled, expected in [(None, 403), (True, 200), (False, 403)]:
            monkeypatch.setattr(STATE, "graph_config", _config(enabled))
            response = client.post(PATH, json=BODY, headers=HEADERS)
            assert response.status_code == expected
            if expected == 200:
                assert response.json()["choices"][0]["message"]["content"] == "remote reply"
            else:
                assert response.json()["detail"] == "model passthrough is loopback-only"
    model.ainvoke.assert_awaited_once()


@pytest.mark.parametrize("allow_remote", [False, True])
@pytest.mark.parametrize("host", ["10.1.2.3", "127.0.0.1", "::1"])
def test_missing_bearer_is_refused_in_both_modes(harness, monkeypatch, allow_remote, host):
    app, model = harness
    monkeypatch.setattr(STATE, "graph_config", _config(allow_remote))
    with TestClient(app, client=(host, 5000)) as client:
        assert client.post(PATH, json=BODY).status_code == 401
    model.ainvoke.assert_not_awaited()


@pytest.mark.parametrize("allow_remote", [False, True])
def test_setting_schema_and_save_roundtrip(tmp_path, allow_remote):
    field = next(f for f in FIELDS if f.key == "model_passthrough.allow_remote")
    assert field.attr == "model_passthrough_allow_remote"
    assert field.type == "bool"
    assert field.description == "Let callers off this machine use the raw model passthrough. Bearer auth still applies."
    assert _SECTION_CATEGORY[field.section] == "Model"
    doc = {}
    apply_updates_to_yaml(doc, config_to_dict(_config(allow_remote)))
    path = tmp_path / "config.yaml"
    save_yaml_doc(doc, path)
    assert LangGraphConfig.from_yaml(str(path)).model_passthrough_allow_remote is allow_remote
