"""A null legacy endpoint must keep selecting the OpenAI SDK default (#4597)."""

from unittest.mock import Mock

import pytest

from graph.config import LangGraphConfig, resolve_model_route, to_registry_shape
from graph.llm import create_llm


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("model:\n  name: gateway:gpt-4o\n  api_base:\n  api_key: test-key\n")
    return LangGraphConfig.from_yaml(str(path))


@pytest.mark.parametrize("sdk_base", [None, "https://sdk.example/v1"])
def test_migrated_connection_uses_sdk_default(migrated, monkeypatch, sdk_base):
    if sdk_base is None:
        monkeypatch.delenv("OPENAI_API_BASE", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    else:
        monkeypatch.delenv("OPENAI_API_BASE", raising=False)
        monkeypatch.setenv("OPENAI_BASE_URL", sdk_base)
    connection = migrated.provider_by_id("gateway")
    assert connection.base_url is None
    route = resolve_model_route(migrated, connection)
    assert route.base_url is None
    assert route.api_key == "test-key"
    # Building the real SDK client is offline and proves None reaches its default logic.
    llm = create_llm(migrated, model_name="gateway:gpt-4o")
    assert str(llm.root_client.base_url) == (sdk_base or "https://api.openai.com/v1") + "/"
    assert llm.root_client.api_key == "test-key"


def test_discovery_and_listing_skip_unspecified_http_endpoint(migrated, monkeypatch):
    from graph import config_io
    from graph.providers.discovery import available_model_lanes

    listing = Mock(side_effect=AssertionError("No explicit URL to probe"))
    original_listing = config_io.list_gateway_models
    monkeypatch.setattr(config_io, "list_gateway_models", listing)
    lane = next(lane for lane in available_model_lanes(migrated) if lane["provider"] == "gateway")
    assert lane["models"] == []
    assert lane["error"] == "No base URL configured for this connection."
    listing.assert_not_called()
    # The listing helper itself also tolerates a null URL, without an HTTP call.
    assert original_listing(None, "test-key", allow_env_key=False) == ([], "api_base is empty")


def test_egress_does_not_add_a_bogus_host(migrated, monkeypatch):
    from security import egress
    from server.agent_init import apply_egress_allowlist

    monkeypatch.setattr(egress, "_allowed", [])
    migrated.egress_allowed_hosts = ["allowed.example"]
    apply_egress_allowlist(migrated)
    assert egress.allowed_hosts() == ["allowed.example"]
    migrated.egress_allowed_hosts = []
    apply_egress_allowlist(migrated)
    assert not egress.is_enabled()


def test_context_probe_uses_family_fallback_without_http(migrated, monkeypatch):
    from graph import model_window

    fetch = Mock(side_effect=AssertionError("No explicit URL to probe"))
    monkeypatch.setattr(model_window, "_fetch_window_map", fetch)
    assert model_window.context_window_for_slot(migrated, "gateway:gpt-4o") == 128_000
    assert model_window.context_window_for_slot(migrated, "gateway:unknown-model") is None
    fetch.assert_not_called()


def test_registry_shape_preserves_explicit_null():
    doc, _, _ = to_registry_shape({"providers": [{"id": "gateway", "base_url": None}]})
    assert doc["providers"][0]["base_url"] is None


@pytest.mark.parametrize("endpoint", ["", "   "])
def test_empty_endpoint_remains_invalid(endpoint):
    cfg = LangGraphConfig.from_dict({"providers": [{"id": "gateway", "base_url": endpoint, "api_key": "test-key"}]})
    assert resolve_model_route(cfg, cfg.provider_by_id("gateway")).base_url == ""
    with pytest.raises(RuntimeError, match="'gateway' connection"):
        create_llm(cfg, model_name="gateway:gpt-4o")
