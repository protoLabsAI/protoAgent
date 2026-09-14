"""`graph.config.to_registry_shape` — one config LAYER without the retired model fields (#3128).

Load-time migration (`_migrated_providers`) answers "what registry does this merged config
imply" in memory. A snapshot needs the same answer as a document, for ONE layer that will
land on a box it knows nothing about — so it may only state what the layer says, and must
never state it as an empty string (`base_url: ""` replaces a box endpoint, #3425).
"""

from __future__ import annotations

import copy

import pytest

from graph.config import legacy_gateway_connection, to_registry_shape

GW = {"id": "gateway", "type": "openai-compat", "base_url": "https://gw/v1"}

CASES = {
    "legacy, endpoint pinned": (
        {"model": {"provider": "openai", "name": "protolabs/reasoning", "api_base": "https://gw/v1", "api_key": "k"}},
        {"providers": [GW], "model": {"name": "gateway:protolabs/reasoning"}},
        {"gateway": "k"},
    ),
    "legacy, provider left to its default": (
        {"model": {"name": "protolabs/reasoning", "api_base": "https://gw/v1"}},
        {"providers": [GW], "model": {"name": "gateway:protolabs/reasoning"}},
        {},
    ),
    "legacy, native lead with a pinned endpoint": (
        {"model": {"provider": "anthropic-oauth", "name": "claude-sonnet-4-5", "api_base": "https://gw/v1"}},
        {
            "providers": [GW, {"id": "anthropic-oauth", "type": "anthropic-oauth"}],
            "model": {"name": "anthropic-oauth:claude-sonnet-4-5"},
        },
        {},
    ),
    "legacy, native lead inheriting the box endpoint": (
        {"model": {"provider": "openai-codex", "name": "gpt-5-codex"}},
        {"model": {"name": "openai-codex:gpt-5-codex"}},
        {},
    ),
    "legacy, nothing pinned (a fleet member's name override)": (
        {"model": {"name": "protolabs/fast", "temperature": 0.3}},
        {"model": {"name": "protolabs/fast", "temperature": 0.3}},
        {},
    ),
    "legacy, only retired keys": (
        {"model": {"provider": "openai", "api_key": "k"}, "identity": {"name": "x"}},
        {"identity": {"name": "x"}},
        {"gateway": "k"},
    ),
    "registry already declared": (
        {
            "providers": [{"id": "prod", "type": "openai-compat", "base_url": "https://prod/v1", "api_key": "pk"}],
            "model": {"name": "prod:protolabs/coder"},
        },
        {"providers": [{"id": "prod", "type": "openai-compat", "base_url": "https://prod/v1"}], "model": {"name": "prod:protolabs/coder"}},
        {"prod": "pk"},
    ),
    "registry beside stale retired keys (the registry wins)": (
        {
            "providers": [{"id": "gateway", "type": "openai-compat", "base_url": "https://new/v1"}],
            "model": {"name": "protolabs/reasoning", "provider": "openai", "api_base": "https://old/v1", "api_key": "k"},
        },
        {"providers": [{"id": "gateway", "type": "openai-compat", "base_url": "https://new/v1"}], "model": {"name": "protolabs/reasoning"}},
        {"gateway": "k"},
    ),
    "registry with a native lead named by a custom id": (
        {"providers": [{"id": "claude", "type": "anthropic-oauth"}], "model": {"provider": "anthropic-oauth", "name": "claude-x"}},
        {"providers": [{"id": "claude", "type": "anthropic-oauth"}], "model": {"name": "claude:claude-x"}},
        {},
    ),
    "no model section at all": ({"identity": {"name": "x"}}, {"identity": {"name": "x"}}, {}),
}


@pytest.mark.parametrize("case", list(CASES))
def test_the_shape(case):
    layer, want_doc, want_credentials = CASES[case]
    doc, credentials = to_registry_shape(layer)
    assert doc == want_doc
    assert credentials == want_credentials


@pytest.mark.parametrize("case", list(CASES))
def test_never_a_retired_field_a_credential_or_a_blank(case):
    doc, _ = to_registry_shape(CASES[case][0])
    assert not {"provider", "api_base", "api_key"} & set(doc.get("model") or {})
    for entry in doc.get("providers") or []:
        assert "api_key" not in entry
        assert all(v not in ("", None) for v in entry.values()), entry


@pytest.mark.parametrize("case", list(CASES))
def test_idempotent_and_pure(case):
    layer = CASES[case][0]
    snapshot = copy.deepcopy(layer)
    doc, _ = to_registry_shape(layer)
    assert layer == snapshot, "to_registry_shape mutated its input"
    assert to_registry_shape(doc) == (doc, {})


def test_blank_connection_fields_are_left_out_so_the_box_value_is_inherited():
    doc, _ = to_registry_shape({"providers": [{"id": "gateway", "type": "openai-compat", "base_url": "", "label": None}]})
    assert doc["providers"] == [{"id": "gateway", "type": "openai-compat"}]


def test_the_first_of_a_duplicated_id_owns_the_key_as_at_load():
    doc, credentials = to_registry_shape(
        {"providers": [{"id": "g", "api_key": "first"}, {"id": "g", "api_key": "second"}]}
    )
    assert credentials == {"g": "first"}


def test_a_malformed_entry_is_left_for_the_loader_to_reject():
    doc, _ = to_registry_shape({"providers": ["not-a-mapping", {"id": "ok", "type": "openai-compat"}]})
    assert doc["providers"] == ["not-a-mapping", {"id": "ok", "type": "openai-compat"}]


@pytest.mark.parametrize("name", ["gateway:protolabs/coder", "acp:claude", "openai-codex:gpt-5", "prod:m"])
def test_an_already_routed_name_is_left_alone(name):
    layer = {
        "providers": [{"id": "prod", "type": "openai-compat", "base_url": "https://prod/v1"}],
        "model": {"provider": "anthropic-oauth", "name": name},
    }
    assert to_registry_shape(layer)[0]["model"]["name"] == name


def test_an_unregistered_prefix_is_part_of_the_model_id():
    """`bedrock:anthropic.claude` is a model id, not a route — the grammar's own rule."""
    doc, _ = to_registry_shape({"model": {"provider": "anthropic-oauth", "name": "bedrock:anthropic.claude"}})
    assert doc["model"]["name"] == "anthropic-oauth:bedrock:anthropic.claude"


@pytest.mark.parametrize(
    ("layer", "want"),
    [
        ({"model": {"name": "protolabs/reasoning"}}, "gateway"),
        ({"providers": [{"id": "prod", "type": "openai-compat"}], "model": {"name": "prod:m"}}, "prod"),
        ({"providers": [{"id": "claude", "type": "anthropic-oauth"}], "model": {"name": "claude:x"}}, "gateway"),
        ({"model": {"name": "unregistered:m"}}, "gateway"),
        ({}, "gateway"),
    ],
)
def test_legacy_gateway_connection(layer, want):
    assert legacy_gateway_connection(layer) == want
