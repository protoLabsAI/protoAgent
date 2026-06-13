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
    # Grouped + ordered by category: the Agent category leads, runtime first.
    assert [g["section"] for g in groups][:3] == ["Agent runtime", "Model", "Routing"]
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


# ── #964 text type + #963 depends_on ──────────────────────────────────────────


def _fake_plugin_specs(monkeypatch, specs: list[dict]):
    """Install fake plugin-declared settings specs (ADR 0019) so build_schema /
    validate_flat see them as a plugin's `settings:`. Returns nothing — the schema
    is read through the monkeypatched `_plugin_field_specs`."""
    from types import SimpleNamespace

    sch = SimpleNamespace(
        section="artifact",
        defaults={s["key"]: s.get("default") for s in specs},
        test=False,
    )
    tuples = [(sch, f"artifact.{s['key']}", s["key"], s) for s in specs]
    monkeypatch.setattr("graph.settings_schema._plugin_field_specs", lambda: tuples)


def test_text_field_renders_as_text_and_validates_like_string(monkeypatch):
    """#964 — a scalar `text` field surfaces its type verbatim and validates like a
    plain string (a multiline value is fine; no list/number coercion)."""
    _fake_plugin_specs(monkeypatch, [
        {"key": "ask_system", "label": "Ask system", "type": "text", "default": ""},
    ])
    fields = {f["key"]: f for g in build_schema(LangGraphConfig()) for f in g["fields"]}
    assert fields["artifact.ask_system"]["type"] == "text"
    assert validate_flat({"artifact.ask_system": "line 1\nline 2"})[0] is True


def test_depends_on_resolves_plugin_short_key_to_full_key(monkeypatch):
    """#963 — a plugin spec's `depends_on.key` is a SHORT sibling key; build_schema
    resolves it to the full dotted path the console matches against."""
    _fake_plugin_specs(monkeypatch, [
        {"key": "ask_enabled", "label": "Interactive", "type": "bool", "default": False},
        {"key": "ask_system", "label": "Ask system", "type": "text",
         "depends_on": {"key": "ask_enabled", "equals": True}},
    ])
    fields = {f["key"]: f for g in build_schema(LangGraphConfig()) for f in g["fields"]}
    assert fields["artifact.ask_system"]["depends_on"] == {"key": "artifact.ask_enabled", "equals": True}
    # An already-qualified key is left as-is (no double prefix).
    _fake_plugin_specs(monkeypatch, [
        {"key": "x", "label": "X", "type": "text",
         "depends_on": {"key": "artifact.ask_enabled", "equals": True}},
    ])
    fields = {f["key"]: f for g in build_schema(LangGraphConfig()) for f in g["fields"]}
    assert fields["artifact.x"]["depends_on"]["key"] == "artifact.ask_enabled"


def test_core_field_depends_on_passed_through(monkeypatch):
    """#963 — a core Field's `depends_on` (full dotted key) flows through unchanged."""
    from graph.settings_schema import Field

    demo = Field("demo.child", "compaction_keep_messages", "Child", "number", "Demo",
                 depends_on={"key": "compaction.enabled", "equals": True})
    monkeypatch.setattr("graph.settings_schema.FIELDS", [demo])
    groups = build_schema(LangGraphConfig())
    entry = next(e for g in groups for e in g["fields"] if e["key"] == "demo.child")
    assert entry["depends_on"] == {"key": "compaction.enabled", "equals": True}
