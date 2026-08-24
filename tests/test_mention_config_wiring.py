"""`mentions:` config actually reaches the dataclass (#3050).

A dataclass field is only *settable* if `from_dict` reads it. `mention_max_agent_hops`
shipped without that read: the round-trip golden passed (it compares the dataclass field
set), the Settings UI had no entry, and no YAML could turn the feature on — a dead field
of exactly the kind `max_iterations` used to be.

The golden can't catch this class of bug, because it never parses a doc that *sets* the
field. These do.
"""

from __future__ import annotations

from graph.config import LangGraphConfig
from graph.settings_schema import FIELDS


def test_yaml_can_turn_agent_to_agent_addressing_on():
    cfg = LangGraphConfig.from_dict({"mentions": {"max_agent_hops": 2, "max_per_target": 4}})
    assert cfg.mention_max_agent_hops == 2
    assert cfg.mention_max_per_target == 4


def test_the_default_is_off():
    """Off is a safety property, not an accident — the one path that spends money with
    no human in the loop."""
    assert LangGraphConfig.from_dict({}).mention_max_agent_hops == 0
    assert LangGraphConfig().mention_max_agent_hops == 0


def test_a_partial_section_keeps_the_other_default():
    cfg = LangGraphConfig.from_dict({"mentions": {"max_agent_hops": 1}})
    assert cfg.mention_max_agent_hops == 1
    assert cfg.mention_max_per_target == LangGraphConfig.mention_max_per_target


def test_an_empty_section_does_not_crash_the_loader():
    # A commented-out `mentions:` block parses to None; from_dict normalizes it.
    assert LangGraphConfig.from_dict({"mentions": None}).mention_max_agent_hops == 0


def test_both_knobs_are_editable_in_settings():
    """Config the operator can't find is config they don't have. Every mention knob needs
    a Settings entry, or turning the feature on means hand-editing YAML."""
    by_attr = {f.attr: f for f in FIELDS}
    for attr, key in (
        ("mention_max_agent_hops", "mentions.max_agent_hops"),
        ("mention_max_per_target", "mentions.max_per_target"),
    ):
        assert attr in by_attr, f"{attr} has no Settings field"
        assert by_attr[attr].key == key, f"{attr} maps to {by_attr[attr].key}, not {key}"


def test_every_settings_key_for_mentions_round_trips_through_from_dict():
    """The invariant that generalizes the bug: a Settings field whose dotted key does not
    parse back is a control that silently does nothing."""
    for f in (f for f in FIELDS if f.attr.startswith("mention_")):
        section, _, leaf = f.key.partition(".")
        cfg = LangGraphConfig.from_dict({section: {leaf: 3}})
        assert getattr(cfg, f.attr) == 3, f"{f.key} does not reach {f.attr}"
