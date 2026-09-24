"""Shipped configs declare an explicit `providers:` registry (ADR 0106, #3128 path 2).

The #3128 removal of the retired `model.provider`/`model.api_base`/`model.api_key`
fields is only safe once every shipped config declares its own `providers:` block —
otherwise deleting the fields also deletes what `_migrated_providers` synthesises the
`gateway` connection FROM, and the config resolves no endpoint.

This pins the migration for the canonical shipped example: it declares `providers:`
explicitly (so the load-time migration is NOT triggered for it), and a config with the
retired fields removed keeps resolving the SAME gateway endpoint and key it does today.
The migration fallback itself is untouched — a config that declares nothing still gets
it (`test_provider_registry_floors.py` covers the floor).
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from graph.config import LangGraphConfig, resolve_model_route

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "config" / "langgraph-config.example.yaml"


def _raw() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def test_example_file_literally_declares_a_providers_list() -> None:
    """r1: the shipped file itself carries a top-level `providers:` list with `gateway`."""
    raw = _raw()
    providers = raw.get("providers")
    assert isinstance(providers, list) and providers, (
        "config/langgraph-config.example.yaml must declare an explicit `providers:` list "
        "(ADR 0106 / #3128) instead of leaning on the load-time migration."
    )
    gateway = next((p for p in providers if isinstance(p, dict) and p.get("id") == "gateway"), None)
    assert gateway is not None, "the example must declare a `gateway` connection"
    assert gateway.get("type") == "openai-compat"
    assert gateway.get("base_url") == "http://gateway:4000/v1"


def test_example_loads_as_a_declared_registry_not_a_migrated_one() -> None:
    """r1/r3: loading the example sets `providers_declared`, so the legacy-lane floor and
    the load-time migration never speak for it."""
    cfg = LangGraphConfig.from_dict(copy.deepcopy(_raw()))
    assert cfg.providers_declared is True
    assert cfg.provider_ids() == ["gateway"]
    assert cfg.provider_by_id("gateway").base_url == "http://gateway:4000/v1"


def test_removing_the_retired_fields_resolves_the_same_gateway(monkeypatch) -> None:
    """r2: the declared example resolves the SAME endpoint + key that the pre-registry
    (migrated) shape does today. The migrated `gateway` label is a synthesised default and
    ADR 0106 makes `label` freely editable, so the route — endpoint + key — is what must be
    identical, not the cosmetic label."""
    # The migration path reads OPENAI_API_KEY for the default route's key; the shipped file
    # carries no key either way, so scrub the env to compare like for like (a set env key is
    # a migration-only convenience a declared connection deliberately does not borrow).
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    raw = _raw()
    after = LangGraphConfig.from_dict(copy.deepcopy(raw))

    legacy = copy.deepcopy(raw)
    legacy.pop("providers", None)  # the pre-#3128 shape: no registry -> migrate at load
    before = LangGraphConfig.from_dict(legacy)
    assert before.providers_declared is False, "the legacy shape must still hit the migration fallback"

    before_gw, after_gw = before.provider_by_id("gateway"), after.provider_by_id("gateway")
    assert before_gw is not None and after_gw is not None
    # Same connection identity and same resolved route.
    assert (after_gw.id, after_gw.type) == (before_gw.id, before_gw.type)
    br, ar = resolve_model_route(before, before_gw), resolve_model_route(after, after_gw)
    assert (ar.base_url, ar.api_key) == (br.base_url, br.api_key)
    # And that route is exactly the App default the migration built from.
    assert ar.base_url == LangGraphConfig().api_base


def test_a_key_in_secrets_moves_onto_the_declared_gateway(monkeypatch) -> None:
    """r2 (key half): when a key IS present, the declared `providers.gateway` secret resolves
    to the same key the legacy `model.api_key` secret migrated to — the endpoint + key both
    survive the field removal."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    raw = _raw()

    # After: declared registry, key supplied under secrets `providers.gateway` (ADR 0106).
    after = LangGraphConfig.from_dict(copy.deepcopy(raw), secrets={"providers": {"gateway": "sk-live"}})
    # Before: the pre-registry shape, same key under the retired `model.api_key` secret.
    legacy = copy.deepcopy(raw)
    legacy.pop("providers", None)
    before = LangGraphConfig.from_dict(legacy, secrets={"model": {"api_key": "sk-live"}})

    br = resolve_model_route(before, before.provider_by_id("gateway"))
    ar = resolve_model_route(after, after.provider_by_id("gateway"))
    assert (ar.base_url, ar.api_key) == (br.base_url, br.api_key) == ("http://gateway:4000/v1", "sk-live")
