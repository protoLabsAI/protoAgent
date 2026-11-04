"""A null legacy endpoint must keep selecting the OpenAI SDK default (#4597).

The fix carries ``None`` ("use the OpenAI SDK default endpoint") through a connection's
``base_url``. Two things produce it: the loader migrating a pre-registry config whose
default route had a null ``model.api_base``, and an operator DECLARING the explicit
``sdk-default`` sentinel. A blank/absent endpoint stays ``""`` and still fails loudly, so a
connection's key never reaches ``api.openai.com`` on a default it did not ask for.

Crucially the SDK default is NOT resolved to a concrete URL at save time — the SDK reads
its own env (``OPENAI_BASE_URL`` / ``OPENAI_API_BASE``) at CALL time, so ``as_dict`` writes
the sentinel back verbatim and the environment never changes what is persisted.
"""

from unittest.mock import Mock

import pytest

from graph.config import (
    SDK_DEFAULT_BASE_URL,
    LangGraphConfig,
    _parse_providers,
    resolve_model_route,
    to_registry_shape,
)
from graph.llm import _gateway_configured, create_llm


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("model:\n  name: gateway:gpt-4o\n  api_base:\n  api_key: test-key\n")
    return LangGraphConfig.from_yaml(str(path))


# (env var to set, its value, the endpoint the SDK then resolves) — each proves the runtime
# resolves the default at CALL time from ITS OWN env, which is exactly why the saved value
# must stay the sentinel rather than a URL snapshotted at save time.
_SDK_ENV_CASES = [
    (None, None, "https://api.openai.com/v1/"),
    ("OPENAI_BASE_URL", "https://via-base-url.example/v1", "https://via-base-url.example/v1/"),
    ("OPENAI_API_BASE", "https://via-api-base.example/v1", "https://via-api-base.example/v1/"),
]


@pytest.mark.parametrize("env_var, value, expected_url", _SDK_ENV_CASES)
def test_migrated_connection_uses_sdk_default(migrated, monkeypatch, env_var, value, expected_url):
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    if env_var:
        monkeypatch.setenv(env_var, value)
    connection = migrated.provider_by_id("gateway")
    assert connection.base_url is None
    route = resolve_model_route(migrated, connection)
    assert route.base_url is None  # the resolved route passes NO base_url
    assert route.api_key == "test-key"
    # Building the real SDK client is offline and proves None reaches its own default logic,
    # resolved from whichever env the SDK itself reads — never a URL frozen at save time.
    llm = create_llm(migrated, model_name="gateway:gpt-4o")
    assert str(llm.root_client.base_url) == expected_url
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


def test_gateway_configured_accepts_a_none_endpoint_but_not_a_blank_one(migrated):
    """A ``None`` endpoint + a key reads as usable (the SDK default), whether it came from
    migration OR an operator-declared ``sdk-default``. A blank endpoint does NOT — its key
    must never reach ``api.openai.com`` on a default it did not ask for."""
    gw = migrated.provider_by_id("gateway")
    assert gw.base_url is None
    assert _gateway_configured(migrated, gw) is True

    # An operator-DECLARED sdk-default connection (providers_declared=True) is equally usable
    # — the sentinel makes "use the SDK default" an explicit choice, no migration needed.
    sdk = LangGraphConfig.from_dict(
        {"providers": [{"id": "sdk", "type": "openai-compat", "base_url": "sdk-default", "api_key": "secret"}]}
    )
    conn = sdk.provider_by_id("sdk")
    assert sdk.providers_declared is True
    assert conn.base_url is None and conn.api_key == "secret"
    assert _gateway_configured(sdk, conn) is True

    # A declared registry with a blank-endpoint connection + a key still fails loudly.
    declared = LangGraphConfig.from_dict({"providers": [{"id": "local", "type": "openai-compat", "api_key": "secret"}]})
    blank = declared.provider_by_id("local")
    assert blank.base_url == "" and blank.api_key == "secret"
    assert _gateway_configured(declared, blank) is False


def test_declared_bare_base_url_is_coerced_to_empty_not_none():
    """A declared entry with a bare ``base_url:`` (null in YAML) parses to "", not ``None``
    — only the explicit ``sdk-default`` sentinel means the SDK default, so a bare/blank
    endpoint fails the 'needs an endpoint' check instead of silently using it."""
    parsed = _parse_providers([{"id": "local", "type": "openai-compat", "base_url": None, "api_key": "secret"}], {})
    assert parsed[0].base_url == ""
    assert resolve_model_route(object(), parsed[0]).base_url == ""


def test_declared_sdk_default_sentinel_parses_to_none():
    """The explicit ``sdk-default`` sentinel is the ONE declared value that maps to ``None``,
    case/whitespace-tolerant — the resolved route then passes no base_url."""
    assert SDK_DEFAULT_BASE_URL == "sdk-default"
    for raw in ("sdk-default", "  SDK-Default  "):
        parsed = _parse_providers([{"id": "sdk", "type": "openai-compat", "base_url": raw}], {})
        assert parsed[0].base_url is None
        assert resolve_model_route(object(), parsed[0]).base_url is None


def test_registry_shape_drops_declared_null_so_it_inherits_the_host():
    """A DECLARED layer's null ``base_url`` is NOT emitted, so the Host⊕agent per-field
    merge inherits the Host endpoint rather than nulling it out (#3425) — a declared blank
    must never replace a box's real endpoint in the ``{**host, **entry}`` merge."""
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


@pytest.mark.parametrize("env_var, value, _unused", _SDK_ENV_CASES)
def test_as_dict_serializes_the_none_endpoint_to_the_sentinel(migrated, monkeypatch, env_var, value, _unused):
    """Serialized for a DECLARED registry, a ``None`` endpoint becomes the explicit
    ``sdk-default`` sentinel — NEVER a resolved URL — so a persisted entry reloads as
    ``None`` again and the SDK resolves its own default at call time. The environment does
    NOT change what is saved (that is the whole point of not materializing)."""
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    if env_var:
        monkeypatch.setenv(env_var, value)
    gw = migrated.provider_by_id("gateway")
    assert gw.base_url is None
    assert gw.as_dict() == {
        "id": "gateway",
        "type": "openai-compat",
        "label": "Gateway",
        "base_url": "sdk-default",
    }
    # Round-trips: the written sentinel reloads as None again.
    assert _parse_providers([gw.as_dict()], {})[0].base_url is None
    # A declared blank endpoint is still omitted (no URL to write), so it reloads as "".
    declared = LangGraphConfig.from_dict({"providers": [{"id": "local", "type": "openai-compat"}]})
    assert "base_url" not in declared.provider_by_id("local").as_dict()


def test_console_write_keeps_the_migrated_gateway_usable(migrated, tmp_path, monkeypatch):
    """The #4597 fix SURVIVES a console write. The provider routes rebuild the registry as a
    DECLARED ``providers:`` list (``[p.as_dict() for p in cfg.providers]``) on any add, patch
    or delete — even of a different connection. ``as_dict`` writes the migrated gateway's null
    endpoint back as the ``sdk-default`` sentinel and ``_apply_providers`` lifts its legacy
    key into the secrets overlay, so after the reload the gateway is a self-contained declared
    connection that still uses the SDK default (``base_url is None``) with its key intact —
    not the blank, keyless entry that revived #4597. The environment does not leak into the
    saved endpoint."""
    import asyncio

    import operator_api.provider_routes as provider_routes
    from graph import config_io
    from runtime.state import STATE

    # A box env that would mislead any save-time resolution — the saved value must ignore it.
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong-host.example/v1")
    monkeypatch.setenv("OPENAI_API_BASE", "https://also-wrong.example/v1")
    config_path = tmp_path / "langgraph-config.yaml"
    secrets_path = tmp_path / "secrets.yaml"
    config_path.write_text("model:\n  name: gateway:gpt-4o\n  api_base:\n  api_key: test-key\n")
    monkeypatch.setattr(config_io, "config_yaml_path", lambda: config_path)
    monkeypatch.setattr(config_io, "secrets_yaml_path", lambda: secrets_path)
    monkeypatch.setattr("graph.config._load_host_layer", lambda: {})
    monkeypatch.setattr("graph.plugins.host.HOST.apply_settings", None)  # disk-writer path

    cfg = LangGraphConfig.from_yaml(str(config_path))
    monkeypatch.setattr(STATE, "graph_config", cfg, raising=False)

    # Adding an UNRELATED connection rebuilds the whole list — the migrated gateway rides along.
    def _with_new(current):
        return provider_routes._entries_from_config(current) + [
            {"id": "local", "type": "openai-compat", "base_url": "http://local/v1"}
        ]

    asyncio.run(provider_routes._apply_providers(_with_new, {}))

    # The persisted endpoint is the sentinel — NO URL, and none of the misleading env hosts.
    written = config_io.load_yaml_doc(config_path)
    gw_entry = next(e for e in written["providers"] if e["id"] == "gateway")
    assert gw_entry["base_url"] == "sdk-default"
    assert "wrong-host" not in str(written) and "also-wrong" not in str(written)

    reloaded = LangGraphConfig.from_yaml(str(config_path))
    assert reloaded.providers_declared is True
    gw = reloaded.provider_by_id("gateway")
    assert gw is not None
    assert gw.base_url is None  # the SDK-default sentinel, not "" and not a frozen URL
    assert gw.api_key == "test-key"  # legacy model.api_key lifted into secrets.providers.gateway
    assert _gateway_configured(reloaded, gw) is True
    # Clear the misleading env so the client resolves the plain SDK default at call time.
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    llm = create_llm(reloaded, model_name="gateway:gpt-4o")
    assert str(llm.root_client.base_url) == "https://api.openai.com/v1/"
    assert llm.root_client.api_key == "test-key"


def test_validate_for_headless_accepts_the_null_endpoint_gateway(migrated):
    """``--setup`` / boot-time auto-complete must accept exactly the config this fixes — a
    qualified ``gateway:`` lead, a null ``api_base`` and a key — because ``create_llm``
    builds it. A declared connection with no endpoint still fails loudly."""
    from graph.config_io import validate_for_headless

    assert validate_for_headless(migrated) == (True, "ok")

    declared = LangGraphConfig.from_dict(
        {"providers": [{"id": "gateway", "type": "openai-compat", "api_key": "k"}], "model": {"name": "gateway:m"}}
    )
    ok, reason = validate_for_headless(declared)
    assert ok is False and "base_url" in reason


def test_registry_shape_keeps_a_migrating_null_api_base_so_it_re_migrates_on_import(monkeypatch):
    """A SNAPSHOT of a migrating layer with a null ``model.api_base`` keeps the null (stays
    pre-registry) instead of dropping it, so the import re-migrates to the SDK-default
    ``gateway`` rather than routing the key to the importer's box default (#4597)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    layer = {"model": {"name": "gateway:gpt-4o", "api_base": None, "api_key": "k"}}
    doc, credentials, aliases = to_registry_shape(layer)
    assert doc == {"model": {"name": "gateway:gpt-4o", "api_base": None}}
    assert credentials == {"model.api_key": "k"}
    assert aliases == {}
    # Idempotent even though the key was lifted out (the null survives a re-export).
    assert to_registry_shape(doc)[0] == doc

    # Re-applying the exported layer + its lifted key re-migrates to the same None gateway.
    merged = {"model": {**doc["model"], "api_key": credentials["model.api_key"]}}
    reimported = LangGraphConfig.from_dict(merged)
    assert reimported.providers_declared is False
    gw = reimported.provider_by_id("gateway")
    assert gw is not None and gw.base_url is None and gw.api_key == "k"
