"""The provider registry (ADR 0106) — S1: schema, migration, lookups.

Providers are CONNECTIONS, not a mode: `type` is the kind, `id` is which one, and
several entries may share a type. That is what makes two gateways possible, and it is
what removes the single lead provider every "isn't the current default" message
descended from.
"""

from __future__ import annotations

import pytest

from graph.config import (
    PROVIDER_TYPE_OPENAI_COMPAT,
    LangGraphConfig,
    Provider,
    valid_provider_id,
)


def _cfg(**doc) -> LangGraphConfig:
    return LangGraphConfig.from_dict(doc)


# ── ids are the stored vocabulary, so they are constrained ────────────────────


@pytest.mark.parametrize("pid", ["gateway", "prod-gateway", "local_vllm", "g1", "openai-codex"])
def test_valid_ids(pid):
    assert valid_provider_id(pid)


@pytest.mark.parametrize("pid", ["", " ", "Bad", "has:colon", "has/slash", "-leading", "has space", "é"])
def test_invalid_ids(pid):
    """A colon would break the `<provider>:<model>` split; a slash collides with gateway
    aliases. Both are excluded so the grammar stays unambiguous by construction."""
    assert not valid_provider_id(pid)


def test_a_malformed_entry_is_dropped_not_fatal():
    """A typo in one connection must not take the agent down."""
    cfg = _cfg(providers=[{"id": "Bad:Id"}, {"id": "ok"}, "not-a-mapping", {"id": "x", "type": "nope"}])
    assert cfg.provider_ids() == ["ok"]


def test_duplicate_ids_keep_the_first():
    cfg = _cfg(providers=[{"id": "g", "base_url": "first"}, {"id": "g", "base_url": "second"}])
    assert [p.base_url for p in cfg.providers] == ["first"]


# ── the thing that was impossible ─────────────────────────────────────────────


def test_two_openai_compatible_gateways():
    cfg = _cfg(
        providers=[
            {"id": "prod-gateway", "type": "openai-compat", "label": "Production", "base_url": "https://api/v1"},
            {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
        ]
    )
    assert cfg.provider_ids() == ["prod-gateway", "local-vllm"]
    assert cfg.provider_by_id("local-vllm").base_url == "http://localhost:8000/v1"
    assert cfg.provider_by_id("PROD-GATEWAY").label == "Production"  # lookup is case-insensitive
    assert cfg.provider_by_id("nope") is None


def test_label_is_display_only_and_falls_back_to_the_id():
    assert Provider(id="gateway").display() == "gateway"
    assert Provider(id="gateway", label="Production").display() == "Production"


# ── migration is as close to an identity function as it can be ────────────────


def test_a_legacy_gateway_config_migrates_to_the_gateway_id():
    """The id is literally `gateway` so every stored `gateway:<model>` keeps resolving."""
    cfg = _cfg(model={"provider": "openai", "api_base": "https://gw/v1", "api_key": "k"})
    assert cfg.provider_ids() == ["gateway"]
    p = cfg.provider_by_id("gateway")
    assert (p.type, p.base_url, p.api_key) == (PROVIDER_TYPE_OPENAI_COMPAT, "https://gw/v1", "k")


@pytest.mark.parametrize("lead", ["anthropic-oauth", "openai-codex"])
def test_a_legacy_native_oauth_config_migrates_under_its_own_lane_id(lead):
    cfg = _cfg(model={"provider": lead, "name": "some-model", "api_base": "", "api_key": ""})
    assert lead in cfg.provider_ids()
    assert cfg.provider_by_id(lead).type == lead


def test_a_legacy_native_provider_with_a_gateway_keeps_both_lanes():
    """Mixing was already possible via qualified slots; the registry must not lose it."""
    cfg = _cfg(model={"provider": "openai-codex", "api_base": "https://gw/v1", "api_key": "k"})
    assert cfg.provider_ids() == ["gateway", "openai-codex"]


def test_an_explicit_registry_is_not_overwritten_by_migration():
    cfg = _cfg(
        providers=[{"id": "only-mine", "base_url": "https://x/v1"}],
        model={"provider": "openai", "api_base": "https://legacy/v1", "api_key": "k"},
    )
    assert cfg.provider_ids() == ["only-mine"]


def test_a_config_with_no_model_source_at_all_migrates_to_nothing():
    assert _cfg(model={"provider": "openai", "api_base": "", "api_key": ""}).providers == []


# ── secrets overlay, exactly as model.api_key behaved ─────────────────────────


def test_a_provider_key_comes_from_secrets_over_inline(tmp_path):
    cfg = LangGraphConfig.from_dict(
        {"providers": [{"id": "gw", "api_key": "inline"}]},
        secrets={"providers": {"gw": "from-secrets"}},
    )
    assert cfg.provider_by_id("gw").api_key == "from-secrets"


def test_as_dict_redacts_the_key_by_default():
    p = Provider(id="gw", base_url="https://x/v1", api_key="shh")
    assert "api_key" not in p.as_dict()
    assert p.as_dict(redact=False)["api_key"] == "shh"
