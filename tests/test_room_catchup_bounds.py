"""The catch-up window's bounds are CONFIGURABLE, and the function stays pure (#3042).

They were module constants (`_CATCHUP_MAX_MESSAGES` / `_CATCHUP_MAX_CHARS`), which is
the shape a comparable runtime shipped and then spent its top two room complaints on:
the tail of a busy round is dropped, the only workaround is to re-mention, and
re-mentioning fragments the conversation the room exists to hold. The caps are now
arguments (defaulting to those constants, so `catchup_window` still takes no config) fed
by `room.catchup_max_messages` / `room.catchup_max_chars`.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

import graph.mention_op as mop
from graph.config import LangGraphConfig


def _history(n: int, size: int = 10) -> list:
    return [HumanMessage(content=f"m{i}".ljust(size, "x")) for i in range(n)]


# --- the caps as arguments ----------------------------------------------------


def test_the_defaults_are_the_old_constants():
    """A caller that passes nothing gets exactly the shipped window."""
    window, truncated = mop.catchup_window(_history(mop._CATCHUP_MAX_MESSAGES + 5), "proto")
    assert len(window) == mop._CATCHUP_MAX_MESSAGES and truncated is True


def test_a_wider_message_cap_stops_the_truncation():
    history = _history(60)
    assert mop.catchup_window(history, "proto")[1] is True
    window, truncated = mop.catchup_window(history, "proto", max_messages=100)
    assert len(window) == 60 and truncated is False


def test_a_narrower_message_cap_bites():
    window, truncated = mop.catchup_window(_history(10), "proto", max_messages=3)
    assert len(window) == 3 and truncated is True
    assert window[-1][1].startswith("m9")  # trimmed from the FRONT — newest kept


def test_a_wider_char_cap_stops_the_truncation():
    history = [HumanMessage(content="x" * 3000) for _ in range(4)]
    assert mop.catchup_window(history, "proto")[1] is True
    window, truncated = mop.catchup_window(history, "proto", max_chars=40000)
    assert len(window) == 4 and truncated is False


def test_a_narrower_char_cap_bites():
    window, truncated = mop.catchup_window(_history(10, size=100), "proto", max_chars=300)
    assert truncated is True
    assert sum(len(a) + len(t) for a, t in window) <= 300


def test_whichever_cap_trips_first_wins():
    """Generous on messages, tight on characters — the char cap still bounds the window."""
    window, _ = mop.catchup_window(_history(20, size=500), "proto", max_messages=1000, max_chars=1200)
    assert 0 < len(window) < 20


def test_a_zero_or_negative_cap_falls_back_rather_than_emptying_the_window():
    """An operator who zeroes a bound wants "don't bound it", never "send nothing" — an
    empty window silently strips a delegate's ENTIRE picture of the room."""
    for bad in (0, -1, None):
        window, _ = mop.catchup_window(_history(5), "proto", max_messages=bad, max_chars=bad)
        assert len(window) == 5


# --- the config → kwargs seam -------------------------------------------------


def test_catchup_caps_reads_the_configured_values():
    cfg = LangGraphConfig()
    cfg.room_catchup_max_messages = 5
    cfg.room_catchup_max_chars = 500
    assert mop.catchup_caps(cfg) == {"max_messages": 5, "max_chars": 500}


def test_catchup_caps_defaults_match_the_shipped_window():
    caps = mop.catchup_caps(LangGraphConfig())
    assert caps == {"max_messages": mop._CATCHUP_MAX_MESSAGES, "max_chars": mop._CATCHUP_MAX_CHARS}


def test_catchup_caps_survives_a_host_with_no_config_at_all():
    """`STATE.graph_config` is None on plenty of paths (early boot, a bare test wiring).
    A room dispatch must not die on an AttributeError reaching for a bound."""
    assert mop.catchup_caps(None)["max_messages"] == mop._CATCHUP_MAX_MESSAGES
    assert mop.catchup_caps(object())["max_chars"] == mop._CATCHUP_MAX_CHARS


def test_catchup_caps_ignores_a_junk_value():
    class _Junk:
        room_catchup_max_messages = "lots"
        room_catchup_max_chars = None

    caps = mop.catchup_caps(_Junk())
    assert caps == {"max_messages": mop._CATCHUP_MAX_MESSAGES, "max_chars": mop._CATCHUP_MAX_CHARS}


def test_the_config_defaults_are_the_module_defaults():
    """The two must not drift: a fresh config has to reproduce the constants exactly, or
    turning the feature "off" would still change every existing room."""
    cfg = LangGraphConfig()
    assert cfg.room_catchup_max_messages == mop._CATCHUP_MAX_MESSAGES
    assert cfg.room_catchup_max_chars == mop._CATCHUP_MAX_CHARS
    assert cfg.room_max_rounds == 1


# --- the ceilings are ENFORCED, not just declared ------------------------------
#
# `settings_schema` puts a `maximum=` on each of the three room knobs, but that only
# fences the Settings UI: `LangGraphConfig.from_dict` assigns whatever the YAML said, and
# nothing on the programmatic path sees the schema at all. Every one of these is a
# per-dispatch COST knob, and `max_rounds` is the sharp one — the chat driver holds the
# per-thread lock for the whole addressed run, so an unclamped `max_rounds: 500` parks
# the operator's own thread behind 500 sequential dispatches with no way to steer out.


def _schema_maximum(key: str) -> int:
    """The `maximum=` the settings schema declares for one `room.*` field."""
    from graph.settings_schema import FIELDS

    field = next(f for f in FIELDS if f.key == key)
    return field.maximum


def test_the_enforced_ceilings_match_the_ones_the_schema_declares():
    """A schema ceiling that drifts from the enforced one is a bound nobody applies.

    The constants are literals in `mention_op` (so it imports without the schema module),
    which is exactly why they need pinning to their source of truth.
    """
    assert mop._CATCHUP_MAX_MESSAGES_CEILING == _schema_maximum("room.catchup_max_messages")
    assert mop._CATCHUP_MAX_CHARS_CEILING == _schema_maximum("room.catchup_max_chars")
    assert mop._MAX_ROUNDS_CEILING == _schema_maximum("room.max_rounds")


def test_a_hand_edited_yaml_above_the_ceiling_is_clamped_not_obeyed():
    """`from_dict` assigns what the YAML said; the READ is what declines to act on it."""
    config = LangGraphConfig.from_dict(
        {"room": {"catchup_max_messages": 99999, "catchup_max_chars": 9999999, "max_rounds": 500}}
    )
    # The dataclass stays a faithful record of what the operator wrote...
    assert config.room_max_rounds == 500
    # ...and the room declines to act on it.
    assert mop.catchup_caps(config) == {
        "max_messages": mop._CATCHUP_MAX_MESSAGES_CEILING,
        "max_chars": mop._CATCHUP_MAX_CHARS_CEILING,
    }
    assert mop.round_cap(config) == mop._MAX_ROUNDS_CEILING


def test_a_value_inside_the_ceiling_is_untouched():
    """The clamp is a ceiling, not a rewrite — ordinary tuning still tunes."""
    config = LangGraphConfig.from_dict(
        {"room": {"catchup_max_messages": 120, "catchup_max_chars": 30000, "max_rounds": 3}}
    )
    assert mop.catchup_caps(config) == {"max_messages": 120, "max_chars": 30000}
    assert mop.round_cap(config) == 3


def test_the_window_itself_clamps_a_caller_that_passes_an_absurd_cap():
    """`catchup_window` is a public seam a second host calls directly with its own caps."""
    window, truncated = mop.catchup_window(
        _history(mop._CATCHUP_MAX_MESSAGES_CEILING + 50),
        "proto",
        max_messages=10**9,
        max_chars=10**9,
    )
    assert len(window) == mop._CATCHUP_MAX_MESSAGES_CEILING and truncated is True
