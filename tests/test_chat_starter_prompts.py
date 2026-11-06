"""Chat starter prompts (#4570) — the server half.

Covers the four moving parts of card 1/2:
  * parsing      — ``chat.show_starter_prompts`` / ``chat.starter_prompts`` off the YAML dict
  * validation   — ``_valid_starter_prompts`` drops malformed entries, coerces, caps at 8
  * resolution   — ``build_runtime_status``'s resolved ``starter_prompts`` list
  * round-trip   — a configured list survives a ``config_to_dict`` save of an unrelated key
"""

from __future__ import annotations

from graph.config import _STARTER_PROMPTS_MAX, LangGraphConfig, _valid_starter_prompts
from graph.config_io import apply_updates_to_yaml, config_to_dict, save_yaml_doc
from graph.settings_schema import FIELDS, build_schema
from operator_api.runtime import DEFAULT_STARTER_PROMPTS, build_runtime_status


# --------------------------------------------------------------------------- #
# Defaults constant — parity with the hard-coded apps/web fallback (#4570).
# --------------------------------------------------------------------------- #


def test_defaults_match_the_shipped_four() -> None:
    assert [p["label"] for p in DEFAULT_STARTER_PROMPTS] == [
        "Take a quick tour",
        "Summarise a web page",
        "Remember my preferences",
        "Ask Claude Code to add a test",
    ]
    assert DEFAULT_STARTER_PROMPTS[0]["prompt"] == "What can you do? Give me a quick tour of your tools."
    # Only the last chip carries a note.
    assert DEFAULT_STARTER_PROMPTS[-1]["note"] == "Needs Claude Code installed"
    assert all("note" not in p for p in DEFAULT_STARTER_PROMPTS[:-1])


# --------------------------------------------------------------------------- #
# Settings schema — both fields owned by the dedicated editor (#4570, bd-4wss).
# --------------------------------------------------------------------------- #


def test_chat_fields_are_ui_hidden_owned_by_the_dedicated_editor() -> None:
    # One-editor-per-key (#1076): the Settings ▸ Chat ▸ "Starter prompts" editor owns BOTH
    # the toggle and the list, so NEITHER renders in the generic Behavior panel.
    groups = build_schema(LangGraphConfig())
    by_key = {f["key"]: f for g in groups for f in g["fields"]}
    assert "chat.show_starter_prompts" not in by_key, "the dedicated editor owns the toggle now"
    assert "chat.starter_prompts" not in by_key

    # Still in FIELDS for config round-trip, and the toggle is a hot-reload bool
    # (restart False — runtime status recomputes the resolved list each poll).
    toggle = next(f for f in FIELDS if f.key == "chat.show_starter_prompts")
    assert toggle.ui_hidden is True
    assert toggle.type == "bool"
    assert toggle.restart is False
    assert toggle.description == (
        "The 'Try one of these' suggestions in an empty chat. Off hides them for this agent."
    )
    assert any(f.key == "chat.starter_prompts" and f.ui_hidden for f in FIELDS)


def test_chat_section_stays_mapped_to_a_category_even_without_a_rendered_group() -> None:
    # With both fields ui_hidden the "Chat" section renders no generic group, but it is still
    # declared by a core FIELD, so it must keep its _SECTION_CATEGORY entry (the core
    # section→category guard, test_settings_schema.py) — and it routes to Behavior.
    from graph.settings_schema import _SECTION_CATEGORY

    assert _SECTION_CATEGORY["Chat"] == "Behavior"
    assert not any(g["section"] == "Chat" for g in build_schema(LangGraphConfig()))


# --------------------------------------------------------------------------- #
# Parsing — from_dict reads the `chat:` section.
# --------------------------------------------------------------------------- #


def test_no_chat_section_uses_defaults_sentinel() -> None:
    cfg = LangGraphConfig.from_dict({})
    assert cfg.chat_show_starter_prompts is True
    assert cfg.chat_starter_prompts is None  # None ⇒ resolve to the built-in defaults


def test_empty_chat_section_parses_to_none() -> None:
    # A present-but-commented `chat:` block parses to None (the `or {}` guard).
    cfg = LangGraphConfig.from_dict({"chat": None})
    assert cfg.chat_show_starter_prompts is True
    assert cfg.chat_starter_prompts is None


def test_parses_toggle_and_list() -> None:
    cfg = LangGraphConfig.from_dict(
        {
            "chat": {
                "show_starter_prompts": False,
                "starter_prompts": [{"label": "A", "prompt": "do a"}],
            }
        }
    )
    assert cfg.chat_show_starter_prompts is False
    assert cfg.chat_starter_prompts == [{"label": "A", "prompt": "do a"}]


# --------------------------------------------------------------------------- #
# Validation — _valid_starter_prompts.
# --------------------------------------------------------------------------- #


def test_none_and_non_list_fall_back_to_defaults() -> None:
    assert _valid_starter_prompts(None) is None
    # Scalar junk must not raise — it reads as "use the defaults".
    assert _valid_starter_prompts("nonsense") is None
    assert _valid_starter_prompts(42) is None


def test_explicit_empty_list_means_none_shown() -> None:
    assert _valid_starter_prompts([]) == []


def test_drops_malformed_entries_and_keeps_good_ones() -> None:
    out = _valid_starter_prompts(
        [
            "not a dict",  # non-mapping → dropped
            {"prompt": "no label"},  # missing label → dropped
            {"label": "no prompt"},  # missing prompt → dropped
            {"label": "   ", "prompt": "blank label"},  # blank after strip → dropped
            {"label": "Good", "prompt": "do it", "note": "hi"},  # kept with note
            {"label": " Trim ", "prompt": " spaced ", "note": "   "},  # trimmed, blank note dropped
        ]
    )
    assert out == [
        {"label": "Good", "prompt": "do it", "note": "hi"},
        {"label": "Trim", "prompt": "spaced"},
    ]


def test_coerces_values_to_str() -> None:
    out = _valid_starter_prompts([{"label": 1, "prompt": 2, "note": 3}])
    assert out == [{"label": "1", "prompt": "2", "note": "3"}]


def test_caps_at_max() -> None:
    many = [{"label": f"L{i}", "prompt": f"p{i}"} for i in range(_STARTER_PROMPTS_MAX + 5)]
    out = _valid_starter_prompts(many)
    assert len(out) == _STARTER_PROMPTS_MAX
    # The first N are kept, in order.
    assert out[0] == {"label": "L0", "prompt": "p0"}
    assert out[-1] == {"label": f"L{_STARTER_PROMPTS_MAX - 1}", "prompt": f"p{_STARTER_PROMPTS_MAX - 1}"}


# --------------------------------------------------------------------------- #
# Resolution — build_runtime_status["starter_prompts"].
# --------------------------------------------------------------------------- #


def _resolved(cfg) -> list[dict]:
    return build_runtime_status(config=cfg, setup_complete=True, graph_loaded=True)["starter_prompts"]


def test_no_config_returns_defaults() -> None:
    status = build_runtime_status(config=None, setup_complete=False, graph_loaded=False)
    assert status["starter_prompts"] == DEFAULT_STARTER_PROMPTS


def test_no_chat_section_resolves_to_defaults() -> None:
    assert _resolved(LangGraphConfig.from_dict({})) == DEFAULT_STARTER_PROMPTS


def test_toggle_off_resolves_to_empty() -> None:
    cfg = LangGraphConfig.from_dict({"chat": {"show_starter_prompts": False}})
    assert _resolved(cfg) == []


def test_explicit_empty_list_resolves_to_empty() -> None:
    cfg = LangGraphConfig.from_dict({"chat": {"starter_prompts": []}})
    assert _resolved(cfg) == []


def test_custom_list_resolves_exactly() -> None:
    custom = [
        {"label": "Ship it", "prompt": "open a PR"},
        {"label": "Explain", "prompt": "explain this repo", "note": "read-only"},
    ]
    cfg = LangGraphConfig.from_dict({"chat": {"starter_prompts": custom}})
    assert _resolved(cfg) == custom


def test_resolved_list_is_a_copy_not_the_constant() -> None:
    # Mutating the served list must not scribble on the module default.
    status = build_runtime_status(config=None, setup_complete=False, graph_loaded=False)
    status["starter_prompts"].append({"label": "x", "prompt": "y"})
    assert len(DEFAULT_STARTER_PROMPTS) == 4


# --------------------------------------------------------------------------- #
# Round-trip — a configured list survives a settings save of an unrelated key.
# --------------------------------------------------------------------------- #


def test_starter_prompts_survive_config_to_dict_roundtrip(tmp_path) -> None:
    custom = [{"label": "Tour", "prompt": "give me a tour", "note": "beta"}]
    cfg = LangGraphConfig.from_dict({"chat": {"starter_prompts": custom}})

    # The console holds config_to_dict's view, changes ONE unrelated key, and saves it.
    d = config_to_dict(cfg)
    assert d["chat"]["starter_prompts"] == custom
    assert d["chat"]["show_starter_prompts"] is True
    d["model"]["temperature"] = 0.9  # the unrelated edit

    doc: dict = {}
    apply_updates_to_yaml(doc, d)
    out = tmp_path / "langgraph-config.yaml"
    save_yaml_doc(doc, out)
    reloaded = LangGraphConfig.from_yaml(str(out))

    assert reloaded.chat_starter_prompts == custom  # preserved through the save
    assert reloaded.temperature == 0.9  # the unrelated edit landed
