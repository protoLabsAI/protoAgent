"""Tests for the settings schema layer (graph/settings_schema.py)."""

from __future__ import annotations

from graph.config import LangGraphConfig
from graph.settings_schema import (
    FIELDS,
    build_schema,
    nest_updates,
    restart_keys,
    validate_flat,
)


def test_schema_groups_and_values():
    cfg = LangGraphConfig()
    groups = build_schema(cfg, model_options=["a", "b"])
    # Grouped + ordered by category: the Agent category leads, its sections in
    # FIELDS order (Model, Routing, Agent runtime, …).
    assert [g["section"] for g in groups][:3] == ["Model", "Routing", "Agent runtime"]
    fields = [f for g in groups for f in g["fields"]]
    # Every core FIELD is present. (build_schema also appends plugin-declared
    # settings — e.g. the discord plugin — so count only the core-keyed fields,
    # which keeps this robust to whichever plugins are installed.)
    core_keys = {f.key for f in FIELDS}
    assert len([f for f in fields if f["key"] in core_keys]) == len(FIELDS)
    for f in fields:
        assert {"key", "label", "type", "value", "default", "restart", "description"} <= set(f)
    # The model select is populated from the probed options.
    model = next(f for f in fields if f["key"] == "model.name")
    assert model["type"] == "select" and model["options"] == ["a", "b"]


def test_groups_carry_category_in_taxonomy_order():
    """ADR 0020: every group is tagged with a category, and categories appear
    contiguously in _CATEGORY_ORDER (so the console sub-nav is stable)."""
    from graph.settings_schema import _CATEGORY_ORDER

    groups = build_schema(LangGraphConfig())
    cats = [g["category"] for g in groups]
    assert all(cats), "every group must carry a category"
    assert cats[0] == "Agent"
    # First-appearance order of categories matches _CATEGORY_ORDER (contiguous).
    seen: list[str] = []
    for c in cats:
        if c not in seen:
            seen.append(c)
    assert seen == [c for c in _CATEGORY_ORDER if c in seen]
    # Known mappings hold.
    by_section = {g["section"]: g["category"] for g in groups}
    assert by_section["Knowledge"] == "Memory"
    assert by_section["Middleware"] == "System"


def test_secrets_are_redacted_with_is_set():
    cfg = LangGraphConfig()
    cfg.auth_token = "super-secret"
    fields = {f["key"]: f for g in build_schema(cfg) for f in g["fields"]}
    tok = fields["auth.token"]
    assert tok["type"] == "secret" and tok["value"] == "" and tok["is_set"] is True
    assert fields["model.api_key"]["is_set"] is False  # default blank


def test_current_values_reflect_config():
    cfg = LangGraphConfig()
    cfg.compaction_enabled = True
    cfg.aux_model = "protolabs/fast"
    fields = {f["key"]: f for g in build_schema(cfg) for f in g["fields"]}
    assert fields["compaction.enabled"]["value"] is True
    assert fields["routing.aux_model"]["value"] == "protolabs/fast"


def test_validate_rejects_bad_types_and_bounds():
    assert validate_flat({"compaction.enabled": "yes"})[0] is False     # not bool
    assert validate_flat({"model.temperature": 5})[0] is False          # > max 2
    assert validate_flat({"model.max_iterations": 0})[0] is False        # < min 1
    assert validate_flat({"routing.fallback_models": "x"})[0] is False   # not list
    assert validate_flat({"prompt_cache.ttl": "9m"})[0] is False         # not in options
    assert validate_flat({"nope.nope": 1})[0] is False                   # unknown key
    assert validate_flat({"model.temperature": 0.5, "compaction.enabled": True})[0] is True


def test_nest_updates_builds_yaml_shape_and_drops_blank_secrets():
    nested = nest_updates({
        "model.temperature": 0.5,
        "prompt_cache.warm.enabled": True,   # 3-level
        "auth.token": "",                    # blank secret → dropped (leave existing)
        "model.api_key": "sk-new",           # set secret → kept
    })
    assert nested == {
        "model": {"temperature": 0.5, "api_key": "sk-new"},
        "prompt_cache": {"warm": {"enabled": True}},
    }


def test_restart_keys_flags_only_restart_fields():
    keys = restart_keys({"runtime.autostart_on_boot": True, "model.temperature": 0.5})
    assert keys == ["runtime.autostart_on_boot"]
