"""POST /api/config/models: every path is a live, refreshing probe through the shared cache (#4858).

The route lists models along one of five paths, and each one must go through the cached lister with
``refresh=True`` (the operator asked, so a success also refreshes the list Settings and the pickers
read). The paths: a provider-qualified OAuth lead, a provider-qualified route lead, a native-OAuth
``provider`` in the form, an explicit ``api_base``/``api_key`` in the form, and a blank form on an
unqualified lead (the default route). The response is always ``{models, error}``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import runtime.state as rs
from operator_api import config_routes


@pytest.fixture
def probe(monkeypatch):
    calls: list[tuple] = []

    def gateway(base, key="", *, allow_env_key=True, refresh=False):
        calls.append(("gateway", base, key, allow_env_key, refresh))
        return SimpleNamespace(models=["gw/a"], error="")

    def provider(ptype, cfg, *, refresh=False):
        calls.append(("provider", ptype, refresh))
        return SimpleNamespace(models=["oauth/b"], error="")

    import graph.providers.discovery as disc

    monkeypatch.setattr(disc, "cached_gateway_models", gateway)
    monkeypatch.setattr(disc, "cached_provider_models", provider)
    cfg = SimpleNamespace(model_name="plain-model", model_provider="")
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    app = FastAPI()
    config_routes.register_config_routes(app)
    return TestClient(app), calls, cfg


def test_qualified_oauth_lead_lists_the_subscription_with_refresh(probe, monkeypatch):
    client, calls, _ = probe
    monkeypatch.setattr(config_routes, "_qualified_live_target", lambda m: ("oauth", "anthropic-oauth", "opus"))
    resp = client.post("/api/config/models", json={})
    assert resp.json() == {"models": ["oauth/b"], "error": ""}
    assert calls == [("provider", "anthropic-oauth", True)]


def test_qualified_route_lead_probes_its_connection_with_refresh(probe, monkeypatch):
    client, calls, _ = probe
    target = ("route", "http://conn/v1", "conn-key", False, "smart")
    monkeypatch.setattr(config_routes, "_qualified_live_target", lambda m: target)
    resp = client.post("/api/config/models", json={})
    assert resp.json() == {"models": ["gw/a"], "error": ""}
    assert calls == [("gateway", "http://conn/v1", "conn-key", False, True)]


def test_a_typed_key_overrides_the_qualified_route_key(probe, monkeypatch):
    client, calls, _ = probe
    target = ("route", "http://conn/v1", "conn-key", False, "smart")
    monkeypatch.setattr(config_routes, "_qualified_live_target", lambda m: target)
    client.post("/api/config/models", json={"api_key": "typed"})
    assert calls == [("gateway", "http://conn/v1", "typed", False, True)]


def test_a_native_oauth_provider_in_the_form_lists_that_account(probe, monkeypatch):
    client, calls, _ = probe
    monkeypatch.setattr(config_routes, "_qualified_live_target", lambda m: None)
    resp = client.post("/api/config/models", json={"provider": "openai-codex"})
    assert resp.json()["models"] == ["oauth/b"]
    assert calls == [("provider", "openai-codex", True)]


def test_an_explicit_endpoint_in_the_form_is_probed_as_typed(probe, monkeypatch):
    client, calls, _ = probe
    resp = client.post("/api/config/models", json={"api_base": "http://typed/v1", "api_key": "k"})
    assert resp.json()["models"] == ["gw/a"]
    assert calls == [("gateway", "http://typed/v1", "k", True, True)]


def test_a_blank_form_on_an_unqualified_lead_probes_the_default_route(probe, monkeypatch):
    client, calls, _ = probe
    monkeypatch.setattr(config_routes, "_qualified_live_target", lambda m: None)
    import graph.config as gc

    monkeypatch.setattr(
        gc, "resolve_model_route", lambda cfg, *a: SimpleNamespace(base_url="http://dflt/v1", api_key="dk")
    )
    client.post("/api/config/models", json={})
    assert calls == [("gateway", "http://dflt/v1", "dk", True, True)]


def test_a_failed_probe_reports_its_error_in_the_same_shape(probe, monkeypatch):
    client, calls, _ = probe
    import graph.providers.discovery as disc

    monkeypatch.setattr(
        disc,
        "cached_gateway_models",
        lambda base, key="", *, allow_env_key=True, refresh=False: SimpleNamespace(models=[], error="401 Unauthorized"),
    )
    resp = client.post("/api/config/models", json={"api_base": "http://typed/v1"})
    assert resp.json() == {"models": [], "error": "401 Unauthorized"}
