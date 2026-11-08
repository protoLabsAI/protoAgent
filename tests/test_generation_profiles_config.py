"""ADR 0128 S1a: optional profiles validate and survive config saves."""

from dataclasses import asdict
import json
import logging

import pytest

from graph.config import GenerationProfile, LangGraphConfig
from graph.config_io import apply_updates_to_yaml, config_to_dict, save_yaml_doc
from graph.settings_schema import FIELDS, build_schema


@pytest.fixture(autouse=True)
def isolate_plugins(monkeypatch):
    monkeypatch.setattr("graph.config._resolve_plugin_config", lambda *a, **k: {})


PROFILES = {
    "routing": {"thinking": "disabled", "max_tokens": 4096},
    "judgement": {
        "thinking": "enabled",
        "reasoning_effort": "high",
        "max_tokens": 16384,
        "thinking_budget": 4096,
        "chat_template_kwargs": {"enable_thinking": True, "nested": {"flag": False}},
        "context_budget": 48000,
    },
}


def load(tmp_path, data):
    path = tmp_path / "langgraph-config.yaml"
    save_yaml_doc(data, path)
    return LangGraphConfig.from_yaml(str(path))


def test_parse_two_profiles(tmp_path):
    config = load(tmp_path, {"model": {"profiles": PROFILES}})
    assert config.model_profiles == {name: GenerationProfile(**values) for name, values in PROFILES.items()}
    assert config.model_profiles["routing"].context_budget is None
    assert all(value is None for value in asdict(GenerationProfile()).values())


@pytest.mark.parametrize("key,value", [
    ("unknown", 1),
    ("thinking", "auto"), ("thinking", []),
    ("reasoning_effort", "extreme"), ("reasoning_effort", {}),
    *[(key, value) for key in ("max_tokens", "thinking_budget", "context_budget")
      for value in (0, -1, True, 1.5, "42", [])],
    ("chat_template_kwargs", []),
])
def test_invalid_field_is_dropped_and_warns(tmp_path, caplog, key, value):
    values = {"thinking": "enabled", key: value}
    with caplog.at_level(logging.WARNING, logger="protoagent.config"):
        config = load(tmp_path, {"model": {"profiles": {"custom": values}}})
    profile = config.model_profiles["custom"]
    assert profile == GenerationProfile(thinking=None if key == "thinking" else "enabled")
    assert any("model.profiles.custom" in record.message and key in record.message for record in caplog.records)


@pytest.mark.parametrize("value", [None, [], "invalid", 42, False])
def test_invalid_profile_is_dropped_and_warns(tmp_path, caplog, value):
    with caplog.at_level(logging.WARNING, logger="protoagent.config"):
        config = load(tmp_path, {"model": {"profiles": {"bad": value, "valid": {}}}})
    assert config.model_profiles == {"valid": GenerationProfile()}
    assert "model.profiles.bad" in caplog.text


@pytest.mark.parametrize("value", [[], "invalid", 42, False])
def test_invalid_block_is_dropped_and_warns(tmp_path, caplog, value):
    with caplog.at_level(logging.WARNING, logger="protoagent.config"):
        config = load(tmp_path, {"model": {"profiles": value}})
    assert config.model_profiles == {}
    assert "model.profiles must be a mapping" in caplog.text


@pytest.mark.parametrize("thinking", ["", "enabled", "disabled"])
@pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
def test_supported_enums_and_null_fields(tmp_path, caplog, thinking, effort):
    values = dict.fromkeys(asdict(GenerationProfile()))
    values.update(thinking=thinking, reasoning_effort=effort)
    config = load(tmp_path, {"model": {"profiles": {"custom": values}}})
    assert config.model_profiles["custom"] == GenerationProfile(thinking=thinking, reasoning_effort=effort)
    assert "model.profiles" not in caplog.text


def test_full_round_trip(tmp_path):
    original = load(tmp_path, {"model": {"profiles": PROFILES}})
    serialized = config_to_dict(original)
    assert serialized["model"]["profiles"] == PROFILES
    doc = {"model": {"temperature": 0.7}}
    apply_updates_to_yaml(doc, serialized)
    assert doc["model"]["profiles"] == PROFILES
    reloaded = load(tmp_path, doc)
    assert reloaded.model_profiles == original.model_profiles


def test_sparse_profiles_preserve_nested_template_values(tmp_path):
    profiles = {
        "empty": {},
        "custom": {
            "thinking": "",
            "chat_template_kwargs": {"optional": None, "stops": ["end"], "enabled": False},
        },
    }
    config = load(tmp_path, {"model": {"profiles": profiles}})
    serialized = config_to_dict(config)
    assert serialized["model"]["profiles"] == profiles
    schema = build_schema(config, include_hidden=True)
    entry = next(e for group in schema for e in group["fields"] if e["key"] == "model.profiles")
    assert entry["value"] == profiles
    doc = {}
    apply_updates_to_yaml(doc, serialized)
    assert load(tmp_path, doc).model_profiles == config.model_profiles


def test_delete_saved_profiles_uses_existing_merge_contract(tmp_path):
    doc = {"model": {"profiles": dict(PROFILES), "max_tokens": 1234}}
    # Nested maps merge; None explicitly deletes a member, while {} is a no-op.
    apply_updates_to_yaml(doc, {"model": {"profiles": {}}})
    assert doc["model"]["profiles"] == PROFILES
    apply_updates_to_yaml(doc, {"model": {"profiles": dict.fromkeys(PROFILES)}})
    assert doc == {"model": {"profiles": {}, "max_tokens": 1234}}
    assert load(tmp_path, doc).model_profiles == {}


def test_absent_profiles_preserve_existing_config(tmp_path):
    data = {"model": {"max_tokens": 1234, "thinking": "enabled", "reasoning_effort": "medium"}}
    absent = load(tmp_path, data)
    assert absent.model_profiles == {}
    data["model"]["profiles"] = {}
    assert asdict(load(tmp_path, data)) == asdict(absent)
    data["model"]["profiles"] = PROFILES
    configured = asdict(load(tmp_path, data))
    configured.pop("model_profiles")
    baseline = asdict(absent)
    baseline.pop("model_profiles")
    assert configured == baseline
    assert LangGraphConfig().model_profiles == {}


def test_settings_schema_exposes_profiles():
    field = next(field for field in FIELDS if field.key == "model.profiles")
    assert (field.attr, field.type, field.section) == ("model_profiles", "dict", "Model & runtime")
    assert "ADR 0128" in field.description
    # ui_hidden: the generic Settings inputs can't express a dict-of-dicts, so a
    # visible field would render "[object Object]" and save a string over the profiles.
    assert field.ui_hidden


def test_hidden_field_not_rendered_but_round_trips(tmp_path):
    config = load(tmp_path, {"model": {"profiles": PROFILES}})
    # The generic console render (include_hidden=False) skips the ui_hidden field…
    rendered = {e["key"] for group in build_schema(config) for e in group["fields"]}
    assert "model.profiles" not in rendered
    # …but `config explain` asks for hidden fields, and the result must json.dumps
    # (a dict of GenerationProfile dataclasses used to raise TypeError here).
    schema = build_schema(config, include_hidden=True)
    entry = next(e for group in schema for e in group["fields"] if e["key"] == "model.profiles")
    assert entry["value"] == PROFILES
    assert entry["default"] == {}
    json.dumps(schema)
