"""ADR 0128 S1a: optional profiles validate and survive config saves."""

from dataclasses import asdict
import logging

import pytest

from graph.config import GenerationProfile, LangGraphConfig
from graph.config_io import apply_updates_to_yaml, config_to_dict, save_yaml_doc
from graph.settings_schema import FIELDS


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
