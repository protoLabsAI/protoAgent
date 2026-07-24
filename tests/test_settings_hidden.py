"""settings.hidden (#2172) — the settings half of tools.hidden.

The two-point lock: a hidden setting is (1) dropped from the schema build_schema
returns (never rendered, so never toggleable from the UI) and (2) refused by the
write path (validate_flat) and the reset path — the config file is the boundary,
the UI is presentation (ADR 0071). Hiding does NOT change the live value.
"""

from __future__ import annotations

from graph.config import LangGraphConfig
from graph.settings_schema import build_schema, is_hidden_setting, validate_flat


def _keys(groups: list[dict]) -> set[str]:
    return {f["key"] for g in groups for f in g["fields"]}


def _sections(groups: list[dict]) -> set[str]:
    return {g["section"] for g in groups}


# ── the matcher ──────────────────────────────────────────────────────────────────────

def test_is_hidden_setting_exact_and_prefix():
    assert is_hidden_setting("goal.enabled", ["goal.enabled"])
    assert is_hidden_setting("goal.enabled", ["goal"])  # group prefix
    assert not is_hidden_setting("goal.enabled", ["goal.max_iterations"])
    # "goal" must not swallow a sibling group that merely shares the spelling prefix
    assert not is_hidden_setting("goals.enabled", ["goal"])
    assert not is_hidden_setting("goal.enabled", None)
    assert not is_hidden_setting("goal.enabled", [])


# ── config parse ─────────────────────────────────────────────────────────────────────

def test_settings_hidden_parses_from_yaml(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("settings:\n  hidden: [goal, compaction.trigger]\n")
    cfg = LangGraphConfig.from_yaml(cfg_file)
    assert cfg.settings_hidden == ["goal", "compaction.trigger"]
    # absent → empty, like tools.hidden
    cfg_file.write_text("goal:\n  enabled: true\n")
    assert LangGraphConfig.from_yaml(cfg_file).settings_hidden == []


# ── presentation half: build_schema ──────────────────────────────────────────────────

def test_build_schema_drops_a_hidden_field_only():
    cfg = LangGraphConfig(settings_hidden=["goal.max_iterations"])
    keys = _keys(build_schema(cfg))
    assert "goal.max_iterations" not in keys
    # a RENDERED sibling survives ("goal.enabled" wouldn't do here — it's statically
    # ui_hidden, owned by a dedicated panel, so it's absent from the schema regardless)
    assert "goal.eval_model" in keys


def test_build_schema_drops_a_whole_hidden_group():
    baseline = build_schema(LangGraphConfig())
    assert any(f["key"].startswith("goal.") for g in baseline for f in g["fields"])

    cfg = LangGraphConfig(settings_hidden=["goal"])
    groups = build_schema(cfg)
    assert not any(k.startswith("goal.") for k in _keys(groups))
    # the group vanishes entirely — no empty husk section
    assert not any(f["key"].startswith("goal.") for g in groups for f in g["fields"])
    assert _sections(baseline) - _sections(groups)  # something actually disappeared


def test_build_schema_unaffected_when_hidden_empty():
    assert _keys(build_schema(LangGraphConfig())) == _keys(build_schema(LangGraphConfig(settings_hidden=[])))


# ── enforcement half: the write path ─────────────────────────────────────────────────

def test_validate_flat_refuses_hidden_keys():
    ok, err = validate_flat({"goal.enabled": True}, hidden=["goal"])
    assert not ok and "settings.hidden" in (err or "")
    ok, err = validate_flat({"goal.enabled": True}, hidden=["goal.enabled"])
    assert not ok
    # same payload sails through without the lock
    ok, err = validate_flat({"goal.enabled": True})
    assert ok, err


def test_validate_flat_still_type_checks_unhidden_keys():
    ok, err = validate_flat({"goal.enabled": "yes"}, hidden=["compaction"])
    assert not ok and "boolean" in (err or "")
