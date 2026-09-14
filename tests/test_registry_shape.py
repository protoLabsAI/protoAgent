"""`graph.config.to_registry_shape` — one config LAYER, retired model fields moved into the registry
only where the registry can say the same thing (#3128).

Until #3128 re-points them, each retired field means something the registry does not:
`model.provider` routes every BARE model value, and `model.api_base` / `model.api_key` are the
default route's and the gateway-only readers' endpoint and key — the `gateway` connection only
in a config the loader migrates. So a retired value leaves the layer only where it has a
registry equivalent, and `aliases` says which connection it pointed at.
"""

from __future__ import annotations

import copy

import pytest

from graph.config import to_registry_shape

GW = {"id": "gateway", "type": "openai-compat", "base_url": "https://gw/v1"}
REGISTRY_HOST = {"providers": [{"id": "gateway", "type": "openai-compat", "base_url": "https://box/v1"}]}

# case: (layer, host, doc, credentials, aliases)
CASES = {
    "legacy, endpoint pinned: it IS the gateway connection": (
        {"model": {"provider": "openai", "name": "protolabs/reasoning", "api_base": "https://gw/v1", "api_key": "k"}},
        None,
        {"model": {"name": "protolabs/reasoning"}, "providers": [GW]},
        {"providers.gateway": "k"},
        {"api_key": "gateway", "api_base": "gateway"},
    ),
    "legacy, endpoint pinned on a registry host: it is NOT a connection, so it stays": (
        {"model": {"provider": "openai", "name": "protolabs/reasoning", "api_base": "https://gw/v1", "api_key": "k"}},
        REGISTRY_HOST,
        {"model": {"name": "protolabs/reasoning", "api_base": "https://gw/v1"}},
        {"model.api_key": "k"},
        {},
    ),
    "legacy, subscription lead with a pinned endpoint": (
        {
            "model": {"provider": "anthropic-oauth", "name": "claude-sonnet-4-5", "api_base": "https://gw/v1"},
            "routing": {"aux_model": "claude-haiku-4-5", "fallback_models": ["protolabs/coder", "claude-x"]},
            "compaction": {"model": "gateway:protolabs/fast"},
            "subagents": {"researcher": {"model": "claude-y"}},
        },
        None,
        {
            "model": {"name": "anthropic-oauth:claude-sonnet-4-5"},
            "routing": {"aux_model": "anthropic-oauth:claude-haiku-4-5", "fallback_models": ["protolabs/coder", "anthropic-oauth:claude-x"]},
            "compaction": {"model": "gateway:protolabs/fast"},
            "subagents": {"researcher": {"model": "anthropic-oauth:claude-y"}},
            "providers": [GW, {"id": "anthropic-oauth", "type": "anthropic-oauth"}],
        },
        {},
        {"api_key": "gateway", "api_base": "gateway", "provider": "anthropic-oauth"},
    ),
    "legacy, subscription lead inheriting the box endpoint": (
        {"model": {"provider": "openai-codex", "name": "gpt-5-codex"}, "goal": {"eval_model": "gpt-5-mini"}},
        None,
        {"model": {"name": "openai-codex:gpt-5-codex"}, "goal": {"eval_model": "openai-codex:gpt-5-mini"}},
        {},
        {"provider": "openai-codex"},
    ),
    "legacy, nothing pinned: the key is the retiring readers', not a connection's": (
        {"model": {"name": "protolabs/fast", "temperature": 0.3, "api_key": "k"}},
        None,
        {"model": {"name": "protolabs/fast", "temperature": 0.3}},
        {"model.api_key": "k"},
        {},
    ),
    "explicit default provider with no name: it also picks the Host model, so it stays": (
        {"model": {"provider": "openai"}, "identity": {"name": "x"}},
        None,
        {"model": {"provider": "openai"}, "identity": {"name": "x"}},
        {},
        {},
    ),
    "registry declared: keys lifted per connection": (
        {
            "providers": [{"id": "prod", "type": "openai-compat", "base_url": "https://prod/v1", "api_key": "pk"}],
            "model": {"name": "prod:protolabs/coder"},
        },
        None,
        {"providers": [{"id": "prod", "type": "openai-compat", "base_url": "https://prod/v1"}], "model": {"name": "prod:protolabs/coder"}},
        {"providers.prod": "pk"},
        {},
    ),
    "registry beside a legacy endpoint the gateway entry has: that endpoint, one connection": (
        {"providers": [GW], "model": {"name": "protolabs/reasoning", "provider": "openai", "api_base": "https://gw/v1", "api_key": "k"}},
        None,
        {"providers": [GW], "model": {"name": "protolabs/reasoning"}},
        {"model.api_key": "k"},
        {"api_base": "gateway"},
    ),
    "registry beside a legacy endpoint no connection has: it stays": (
        {"providers": [{"id": "local", "type": "openai-compat", "base_url": "http://127.0.0.1:8080/v1"}], "model": {"name": "local:qwen3", "api_base": "https://old/v1"}},
        None,
        {"providers": [{"id": "local", "type": "openai-compat", "base_url": "http://127.0.0.1:8080/v1"}], "model": {"name": "local:qwen3", "api_base": "https://old/v1"}},
        {},
        {},
    ),
    "registry with a subscription lead named by a custom id": (
        {"providers": [{"id": "claude", "type": "anthropic-oauth"}], "model": {"provider": "anthropic-oauth", "name": "claude-x"}},
        None,
        {"providers": [{"id": "claude", "type": "anthropic-oauth"}], "model": {"name": "claude:claude-x"}},
        {},
        {"provider": "claude"},
    ),
    "a subscription lead qualified elsewhere: its bare slots still follow model.provider": (
        {"model": {"provider": "anthropic-oauth", "name": "gateway:protolabs/reasoning"}, "routing": {"aux_model": "claude-haiku-4-5"}},
        None,
        {"model": {"name": "gateway:protolabs/reasoning"}, "routing": {"aux_model": "anthropic-oauth:claude-haiku-4-5"}},
        {},
        {"provider": "anthropic-oauth"},
    ),
    "the default provider leaves bare slots bare": (
        {"model": {"provider": "openai", "name": "anthropic-oauth:claude-sonnet-4-5"}, "routing": {"aux_model": "gpt-5-mini"}},
        None,
        {"model": {"name": "anthropic-oauth:claude-sonnet-4-5"}, "routing": {"aux_model": "gpt-5-mini"}},
        {},
        {},
    ),
    "no model section at all": ({"identity": {"name": "x"}}, None, {"identity": {"name": "x"}}, {}, {}),
}


@pytest.mark.parametrize("case", list(CASES))
def test_the_shape(case):
    layer, host, want_doc, want_credentials, want_aliases = CASES[case]
    assert to_registry_shape(layer, host=host) == (want_doc, want_credentials, want_aliases)


@pytest.mark.parametrize("case", list(CASES))
def test_no_credential_and_no_blank_ever_left_in_the_doc(case):
    layer, host, *_ = CASES[case]
    doc, _, _ = to_registry_shape(layer, host=host)
    assert "api_key" not in (doc.get("model") or {})
    for entry in doc.get("providers") or []:
        assert "api_key" not in entry
        assert all(v not in ("", None) for v in entry.values()), entry


@pytest.mark.parametrize("case", list(CASES))
def test_idempotent_and_pure(case):
    layer, host, *_ = CASES[case]
    snapshot = copy.deepcopy(layer)
    doc, _, _ = to_registry_shape(layer, host=host)
    assert layer == snapshot, "to_registry_shape mutated its input"
    assert to_registry_shape(doc, host=host)[0] == doc


def test_blank_values_are_left_out_so_the_box_value_is_inherited():
    doc, _, _ = to_registry_shape(
        {"providers": [{"id": "gateway", "type": "openai-compat", "base_url": "", "label": None}], "model": {"name": "m", "api_base": ""}}
    )
    assert doc == {"providers": [{"id": "gateway", "type": "openai-compat"}], "model": {"name": "m"}}


def test_the_first_of_a_duplicated_id_owns_the_key_as_at_load():
    _, credentials, _ = to_registry_shape({"providers": [{"id": "g", "api_key": "first"}, {"id": "g", "api_key": "second"}]})
    assert credentials == {"providers.g": "first"}


def test_a_malformed_entry_is_left_for_the_loader_to_reject():
    doc, _, _ = to_registry_shape({"providers": ["not-a-mapping", {"id": "ok", "type": "openai-compat"}]})
    assert doc["providers"] == ["not-a-mapping", {"id": "ok", "type": "openai-compat"}]


@pytest.mark.parametrize("name", ["gateway:protolabs/coder", "acp:claude", "openai-codex:gpt-5", "prod:m", "protolabs/reasoning"])
def test_a_value_a_subscription_lead_would_not_claim_is_left_alone(name):
    """Qualified values already say where they go; a `/` alias routes through the gateway."""
    layer = {
        "providers": [{"id": "prod", "type": "openai-compat", "base_url": "https://prod/v1"}],
        "model": {"provider": "anthropic-oauth", "name": name},
    }
    assert to_registry_shape(layer)[0]["model"]["name"] == name


def test_an_unregistered_prefix_is_part_of_the_model_id():
    """`bedrock:anthropic.claude` is a model id, not a route — the grammar's own rule."""
    doc, _, _ = to_registry_shape({"model": {"provider": "anthropic-oauth", "name": "bedrock:anthropic.claude"}})
    assert doc["model"]["name"] == "anthropic-oauth:bedrock:anthropic.claude"
