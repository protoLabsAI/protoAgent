"""A null legacy endpoint must keep selecting the OpenAI SDK default (#4597).

The fix carries ``None`` through the MIGRATED ``gateway`` connection only. The
authoritative "this is the migrated gateway" signal is ``base_url is None``: an
operator-DECLARED connection can never hold it (``_parse_providers`` coerces a blank/null
endpoint to ``""``), so a declared blank endpoint still fails loudly and never routes a
connection's key to the SDK default.
"""

from unittest.mock import Mock

import pytest

from graph.config import LangGraphConfig, _parse_providers, resolve_model_route, to_registry_shape
from graph.llm import _gateway_configured, create_llm


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


def test_gateway_configured_accepts_migrated_null_but_not_declared_blank(migrated):
    """The Blocker's line, pinned both ways: the migrated gateway's ``None`` endpoint + a
    key reads as usable (SDK default), while a DECLARED connection whose endpoint is blank
    does NOT — its key must never reach ``api.openai.com``."""
    gw = migrated.provider_by_id("gateway")
    assert gw.base_url is None and not migrated.providers_declared
    assert _gateway_configured(migrated, gw) is True

    # A declared registry (providers_declared=True) with a blank-endpoint connection + key.
    declared = LangGraphConfig.from_dict(
        {"providers": [{"id": "local", "type": "openai-compat", "api_key": "secret"}]}
    )
    conn = declared.provider_by_id("local")
    assert declared.providers_declared is True
    assert conn.base_url == "" and conn.api_key == "secret"
    assert _gateway_configured(declared, conn) is False


def test_declared_bare_base_url_is_coerced_to_empty_not_none():
    """A declared entry with a bare ``base_url:`` (null in YAML) parses to "", not ``None``
    — so ``base_url is None`` stays a migration-only signal and the declared connection
    fails the 'needs an endpoint' check instead of silently using the SDK default."""
    parsed = _parse_providers(
        [{"id": "local", "type": "openai-compat", "base_url": None, "api_key": "secret"}], {}
    )
    assert parsed[0].base_url == ""
    assert resolve_model_route(object(), parsed[0]).base_url == ""


def test_registry_shape_drops_declared_null_so_it_inherits_the_host():
    """A declared layer's null ``base_url`` is NOT emitted, so the Host⊕agent per-field
    merge inherits the Host endpoint rather than nulling it out (#3425). Previously the PR
    kept the null, which replaced a box's real endpoint in the ``{**host, **entry}`` merge."""
    doc, _, _ = to_registry_shape(
        {"providers": [{"id": "gateway", "type": "openai-compat", "base_url": None, "api_key": "k"}]},
        host={"providers": [{"id": "gateway", "type": "openai-compat", "base_url": "http://host:4000/v1"}]},
    )
    assert "base_url" not in doc["providers"][0]


@pytest.mark.parametrize("endpoint", ["", "   "])
def test_empty_endpoint_remains_invalid(endpoint):
    cfg = LangGraphConfig.from_dict({"providers": [{"id": "gateway", "base_url": endpoint, "api_key": "test-key"}]})
    assert resolve_model_route(cfg, cfg.provider_by_id("gateway")).base_url == ""
    with pytest.raises(RuntimeError, match="'gateway' connection"):
        create_llm(cfg, model_name="gateway:gpt-4o")
