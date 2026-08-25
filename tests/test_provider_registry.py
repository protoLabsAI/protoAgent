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


# ── S2: dispatch routes by registered connection, not by a hardcoded lane ──────

from graph.llm import _gateway_configured, create_llm, split_slot_target  # noqa: E402


def _registry_cfg() -> LangGraphConfig:
    return _cfg(
        providers=[
            {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1", "api_key": "pk"},
            {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1", "api_key": "lk"},
            {"id": "claude", "type": "anthropic-oauth"},
        ],
        model={"name": "prod-gateway:protolabs/reasoning"},
    )


def test_the_grammar_claims_any_registered_id():
    cfg = _registry_cfg()
    assert split_slot_target("local-vllm:qwen3-32b", cfg) == ("local-vllm", "qwen3-32b")
    assert split_slot_target("prod-gateway:protolabs/coder", cfg) == ("prod-gateway", "protolabs/coder")


def test_an_unregistered_prefix_stays_part_of_the_model_name():
    """The rule that keeps `bedrock:anthropic.claude` a model id rather than a route."""
    cfg = _registry_cfg()
    assert split_slot_target("bedrock:anthropic.claude", cfg) == ("", "bedrock:anthropic.claude")
    assert split_slot_target("protolabs/reasoning", cfg) == ("", "protolabs/reasoning")


def test_a_config_with_no_registry_still_speaks_the_legacy_lanes():
    """A bare config (a test, a caller that never loaded YAML) means what it always did."""
    bare = LangGraphConfig()
    assert bare.providers == []
    assert split_slot_target("gateway:protolabs/coder", bare) == ("gateway", "protolabs/coder")
    assert split_slot_target("openai-codex:gpt-5.5", bare) == ("openai-codex", "gpt-5.5")
    assert split_slot_target("prod-gateway:x", bare) == ("", "prod-gateway:x")


def test_two_gateways_build_against_their_own_endpoints():
    """The thing the single api_base/api_key pair made impossible."""
    cfg = _registry_cfg()
    prod = create_llm(cfg, model_name="prod-gateway:protolabs/coder")
    local = create_llm(cfg, model_name="local-vllm:qwen3-32b")
    assert str(prod.openai_api_base) == "https://prod/v1"
    assert str(local.openai_api_base) == "http://localhost:8000/v1"
    assert prod.model_name == "protolabs/coder"
    assert local.model_name == "qwen3-32b"
    # Each carries its OWN key, not the one config-level field.
    assert prod.openai_api_key.get_secret_value() == "pk"
    assert local.openai_api_key.get_secret_value() == "lk"


def test_a_connection_supplies_its_own_key_for_the_configured_check(monkeypatch):
    cfg = _registry_cfg()
    assert _gateway_configured(cfg, cfg.provider_by_id("prod-gateway")) is True
    # The config-level key and OPENAI_API_KEY are still the fallback for a connection
    # that carries none, so both have to be absent for "unconfigured" to mean anything.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    keyless = _cfg(providers=[{"id": "no-key", "base_url": "https://x/v1"}])
    keyless.api_key = ""
    assert _gateway_configured(keyless, keyless.provider_by_id("no-key")) is False


def test_a_keyless_connection_raises_naming_itself(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _cfg(providers=[{"id": "local-vllm", "base_url": "http://localhost:8000/v1"}])
    cfg.api_key = ""
    with pytest.raises(RuntimeError, match="'local-vllm' connection"):
        create_llm(cfg, model_name="local-vllm:qwen3-32b")
