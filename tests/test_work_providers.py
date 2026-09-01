"""The ADR 0079 *Observe* extension point: a plugin projects its own queue into the ONE
``<working_state>`` block.

The gap this closes is specific and was observed in production: an agent whose entire job
lives on a plugin-owned board reported itself idle while one of its own cards sat stalled,
because ``working_state_block`` reads four CORE stores and had no way in. These tests pin
the seam's contract — and, just as importantly, that a bad provider can never take a turn
down with it, since this code runs inline on EVERY turn.
"""

from __future__ import annotations

import logging
import time

import pytest

from graph import work_providers
from graph.plugins.registry import PluginRegistry
from graph.work_providers import collect_work_sections, set_plugin_work_providers, work_provider_names


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every test starts from an empty registry and leaves one — the registry is module
    global, so a leaked provider would leak into the real working state of later tests."""
    set_plugin_work_providers({})
    yield
    set_plugin_work_providers({})


# ── rendering ────────────────────────────────────────────────────────────────


def test_items_render_into_a_labelled_section():
    set_plugin_work_providers(
        {"board:cards": lambda: [{"id": "bd-7xhm", "title": "Audit the registry", "state": "in_progress"}]},
        {"board:cards": {"plugin_id": "board", "label": "Open board cards"}},
    )
    assert collect_work_sections(8) == [("OPEN BOARD CARDS", ["- [in_progress] bd-7xhm Audit the registry"])]


def test_an_unlabelled_provider_still_gets_a_readable_heading():
    """A plugin that omits ``label`` must not surface its raw registry key as the heading."""
    set_plugin_work_providers({"board:cards": lambda: [{"id": "bd-1", "title": "T"}]})
    (heading, _lines), = collect_work_sections(8)
    assert heading == "OPEN WORK (board:cards)"


@pytest.mark.parametrize(
    "item,expected",
    [
        ({"id": "bd-1", "title": "T", "state": "ready"}, "- [ready] bd-1 T"),
        ({"id": "bd-1", "title": "T"}, "- bd-1 T"),
        ({"title": "no id"}, "- no id"),  # a provider shouldn't have to invent an id
        ({"id": "bd-1"}, "- bd-1"),
        ({"id": "bd-1", "title": "T", "hint": "awaiting delivery"}, "- bd-1 T — awaiting delivery"),
        ("a bare string", "- a bare string"),
    ],
)
def test_item_shapes_render(item, expected):
    set_plugin_work_providers({"p:q": lambda: [item]})
    (_heading, lines), = collect_work_sections(8)
    assert lines == [expected]


@pytest.mark.parametrize("junk", [{}, {"id": "", "title": "   "}, None, 42])
def test_unusable_items_are_dropped_not_rendered_blank(junk):
    """An item with nothing addressable and nothing to read earns no line — otherwise a
    malformed provider pads the prompt with empty bullets every turn."""
    set_plugin_work_providers({"p:q": lambda: [junk, {"id": "bd-ok", "title": "real"}]})
    (_heading, lines), = collect_work_sections(8)
    assert lines == ["- bd-ok real"]


def test_a_provider_with_no_work_contributes_no_section():
    set_plugin_work_providers({"p:q": lambda: []})
    assert collect_work_sections(8) == []


def test_a_long_line_is_trimmed():
    set_plugin_work_providers({"p:q": lambda: [{"id": "bd-1", "title": "x" * 500}]})
    (_heading, lines), = collect_work_sections(8)
    assert len(lines[0]) <= work_providers._LINE_CAP + 2  # + the "- " bullet
    assert lines[0].endswith("…")


# ── bounding + stability ─────────────────────────────────────────────────────


def test_cap_is_per_provider_so_one_queue_cannot_starve_another():
    set_plugin_work_providers(
        {
            "a:q": lambda: [{"id": f"a{i}"} for i in range(20)],
            "b:q": lambda: [{"id": f"b{i}"} for i in range(20)],
        }
    )
    sections = collect_work_sections(3)
    assert [len(lines) for _h, lines in sections] == [3, 3]


def test_sections_are_ordered_by_name_for_a_stable_prompt():
    """An unstable block would invalidate the prompt cache every turn for no benefit."""
    set_plugin_work_providers({"z:q": lambda: [{"id": "z"}], "a:q": lambda: [{"id": "a"}]})
    assert [h for h, _l in collect_work_sections(8)] == ["OPEN WORK (a:q)", "OPEN WORK (z:q)"]


# ── a bad provider must never break the turn ─────────────────────────────────


def test_a_raising_provider_is_skipped_and_the_others_still_render(caplog):
    def boom():
        raise RuntimeError("board unreachable")

    set_plugin_work_providers({"a:bad": boom, "b:good": lambda: [{"id": "ok"}]})
    with caplog.at_level(logging.WARNING):
        sections = collect_work_sections(8)
    assert [h for h, _l in sections] == ["OPEN WORK (b:good)"]
    assert "a:bad raised" in caplog.text


def test_a_raising_provider_warns_only_once():
    """It fails on every turn; warning every turn would flood the log."""
    def boom():
        raise RuntimeError("nope")

    set_plugin_work_providers({"a:bad": boom})
    import logging as _logging

    records = []
    handler = _logging.Handler()
    handler.emit = records.append
    log = _logging.getLogger("graph.work_providers")
    log.addHandler(handler)
    try:
        for _ in range(5):
            collect_work_sections(8)
    finally:
        log.removeHandler(handler)
    assert len([r for r in records if r.levelno >= _logging.WARNING]) == 1


def test_a_slow_provider_does_not_mute_a_later_exception(caplog):
    """The dedup must be per PROBLEM, not per provider.

    Keying it on the name alone meant a provider warned once for being slow was thereafter
    permanently mute about RAISING — the two branches shared one set, and an exception is
    the thing you most need to hear about. This provider is slow on the first call and
    throws on the second; both must be reported."""
    calls = {"n": 0}

    def slow_then_broken():
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(work_providers.SLOW_PROVIDER_S + 0.05)
            return [{"id": "bd-1"}]
        raise RuntimeError("board unreachable")

    set_plugin_work_providers({"p:q": slow_then_broken})
    with caplog.at_level(logging.WARNING):
        collect_work_sections(8)  # slow
        collect_work_sections(8)  # raises
    assert "took" in caplog.text  # the slow warning
    assert "raised" in caplog.text  # and the exception was NOT swallowed


def test_each_problem_kind_still_warns_only_once(caplog):
    """Per-problem dedup must not turn into per-call spam: the same failure repeated is
    still logged once."""

    def boom():
        raise RuntimeError("nope")

    set_plugin_work_providers({"p:q": boom})
    with caplog.at_level(logging.WARNING):
        for _ in range(4):
            collect_work_sections(8)
    assert caplog.text.count("raised") == 1


def test_a_non_list_return_is_skipped(caplog):
    set_plugin_work_providers({"p:q": lambda: {"id": "not-a-list"}})
    with caplog.at_level(logging.WARNING):
        assert collect_work_sections(8) == []
    assert "expected a list" in caplog.text


def test_reload_replaces_wholesale_so_a_removed_plugin_stops_rendering():
    """A reload that drops a plugin must drop its queue too — otherwise the working state
    keeps advertising a board nothing maintains any more (#1752, the stale-registry bug)."""
    set_plugin_work_providers({"gone:q": lambda: [{"id": "x"}]})
    assert work_provider_names() == ["gone:q"]
    set_plugin_work_providers({"kept:q": lambda: [{"id": "y"}]})
    assert work_provider_names() == ["kept:q"]
    assert [h for h, _l in collect_work_sections(8)] == ["OPEN WORK (kept:q)"]


# ── the registry seam ────────────────────────────────────────────────────────


def test_register_work_provider_namespaces_a_bare_name(tmp_path):
    reg = PluginRegistry("board", tmp_path)
    reg.register_work_provider("cards", lambda: [], label="Open board cards")
    assert list(reg.work_providers) == ["board:cards"]
    assert reg.work_provider_meta["board:cards"] == {"plugin_id": "board", "label": "Open board cards"}


def test_register_work_provider_keeps_an_already_namespaced_name(tmp_path):
    reg = PluginRegistry("board", tmp_path)
    reg.register_work_provider("other:cards", lambda: [])
    assert list(reg.work_providers) == ["other:cards"]


@pytest.mark.parametrize("name,fn", [("", lambda: []), ("cards", None), ("cards", "not callable")])
def test_register_work_provider_refuses_junk(tmp_path, name, fn):
    reg = PluginRegistry("board", tmp_path)
    reg.register_work_provider(name, fn)
    assert reg.work_providers == {}


# ── the payoff: it actually reaches <working_state> ──────────────────────────


def test_provider_work_reaches_the_working_state_block(monkeypatch):
    """The end-to-end assertion this whole seam exists for: a plugin's queue shows up in the
    block the agent is taught to treat as its own live commitments, in the SAME block as the
    core sections rather than a rival one."""
    from runtime.state import STATE

    from graph import projection

    for slot in ("goal_controller", "tasks_store", "watch_controller", "scheduler"):
        monkeypatch.setattr(STATE, slot, None, raising=False)

    set_plugin_work_providers(
        {"board:cards": lambda: [{"id": "bd-7xhm", "title": "Audit the registry", "state": "in_progress"}]},
        {"board:cards": {"plugin_id": "board", "label": "Open board cards"}},
    )
    block = projection.working_state_block({"session_id": "s1"})
    assert "<working_state>" in block
    assert "OPEN BOARD CARDS:" in block
    assert "- [in_progress] bd-7xhm Audit the registry" in block


def test_no_providers_leaves_the_block_byte_for_byte_unchanged(monkeypatch):
    """The seam is additive: an instance with no work providers must render exactly what it
    rendered before this existed."""
    from runtime.state import STATE

    from graph import projection

    for slot in ("goal_controller", "tasks_store", "watch_controller", "scheduler"):
        monkeypatch.setattr(STATE, slot, None, raising=False)
    set_plugin_work_providers({})
    assert projection.working_state_block({"session_id": "s1"}) == ""
