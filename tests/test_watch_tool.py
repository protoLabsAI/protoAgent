"""The LLM-facing create_watch tool (ADR 0067) — in particular its lifetime knobs.

Until this, the tool took only condition/check/check_args/run_prompt/watch_id, so EVERY
agent-created watch had no expiry and no stall detection and polled at the global cadence
until a human cleared it. `interval_s` / `expires_in_s` / `stall_after` close that gap; the
controller and the operator API always supported them.
"""

from __future__ import annotations

import pytest

from graph.config import LangGraphConfig
from graph.watches.controller import WatchController
from graph.watches.store import WatchStore
from runtime.state import STATE
from tools.lg_tools import _build_watch_tools


def _tools(monkeypatch, tmp_path):
    """The four watch tools, wired to a throwaway store with one plugin verifier known."""
    ctrl = WatchController(LangGraphConfig(), WatchStore(tmp_path))
    monkeypatch.setattr(STATE, "watch_controller", ctrl, raising=False)
    monkeypatch.setattr("graph.goals.verifiers._PLUGIN_VERIFIERS", {"p:v": object()})
    create, listing, update, clear = _build_watch_tools()
    return ctrl, create, listing, update, clear


def _only(ctrl):
    watches = ctrl.list_watches()
    assert len(watches) == 1, f"expected exactly one watch, got {[w.id for w in watches]}"
    return watches[0]


def test_create_watch_without_knobs_is_unbounded(monkeypatch, tmp_path):
    # The pre-existing shape, pinned: no deadline, no stall threshold, default cadence.
    ctrl, create, _l, update, _c = _tools(monkeypatch, tmp_path)
    create.invoke({"condition": "deploy is green", "check": "p:v"})
    w = _only(ctrl)
    assert (w.deadline, w.stall_after, w.interval_s) == (None, None, None)


def test_expires_in_s_is_relative_to_now(monkeypatch, tmp_path):
    """The tool takes a SPAN, the store keeps an absolute epoch. A model has no reliable
    "now" — asked for an ISO timestamp it guesses, and a guess in the past would expire the
    watch on its first tick — so the relative form is the one exposed to the agent."""
    from time import time

    ctrl, create, _l, _u, _c = _tools(monkeypatch, tmp_path)
    before = time()
    create.invoke({"condition": "release ships", "check": "p:v", "expires_in_s": 3600})
    w = _only(ctrl)
    assert w.deadline is not None
    assert before + 3600 <= w.deadline <= time() + 3600  # ~an hour out, not an epoch echo


@pytest.mark.parametrize("bad", [0, -60])
def test_expires_in_s_rejects_a_non_positive_span(monkeypatch, tmp_path, bad):
    # A past/zero span would arm a watch that expires immediately — refuse rather than
    # silently create a watch that dies on its first tick.
    ctrl, create, _l, _u, _c = _tools(monkeypatch, tmp_path)
    out = create.invoke({"condition": "c", "check": "p:v", "expires_in_s": bad})
    assert "must be positive" in out
    assert ctrl.list_watches() == []


def test_interval_and_stall_after_reach_the_store(monkeypatch, tmp_path):
    ctrl, create, _l, update, _c = _tools(monkeypatch, tmp_path)
    create.invoke({"condition": "nightly build", "check": "p:v", "interval_s": 1800, "stall_after": 3})
    w = _only(ctrl)
    assert w.interval_s == 1800.0
    assert w.stall_after == 3


def test_a_non_numeric_interval_never_reaches_the_store(monkeypatch, tmp_path):
    """`_due` does arithmetic on `interval_s` every tick, so a junk value stored verbatim
    would raise inside the loop for as long as the watch exists. The `float | None`
    annotation makes the tool SCHEMA reject it, before any of our code runs — this pins
    that, so replacing the annotation with a looser one has to fail here."""
    from pydantic import ValidationError

    ctrl, create, _l, _u, _c = _tools(monkeypatch, tmp_path)
    with pytest.raises(ValidationError):
        create.invoke({"condition": "c", "check": "p:v", "interval_s": "soon"})
    assert ctrl.list_watches() == []


def test_unknown_verifier_still_refused_with_the_knobs_present(monkeypatch, tmp_path):
    # The knobs must not become a way around the plugin-verifier trust gate (ADR 0067 D4).
    ctrl, create, _l, _u, _c = _tools(monkeypatch, tmp_path)
    out = create.invoke({"condition": "c", "check": "shell:rm", "expires_in_s": 60, "stall_after": 2})
    assert "unknown plugin verifier" in out
    assert ctrl.list_watches() == []


# --- what the agent reads back (status_line → list_watches + <working_state>) ---


def test_status_line_shows_the_lifetime_knobs_it_was_given(monkeypatch, tmp_path):
    ctrl, create, listing, _u, _c = _tools(monkeypatch, tmp_path)
    create.invoke(
        {
            "condition": "rollout finishes",
            "check": "p:v",
            "interval_s": 1800,
            "expires_in_s": 7200,
            "stall_after": 3,
        }
    )
    line = listing.invoke({})
    assert "every 30m" in line
    assert "expires in 2h" in line
    assert "stall after 3" in line


def test_status_line_stays_bare_without_knobs(monkeypatch, tmp_path):
    ctrl, create, listing, _u, _c = _tools(monkeypatch, tmp_path)
    create.invoke({"condition": "deploy is green", "check": "p:v"})
    line = listing.invoke({})
    assert "deploy is green" in line
    assert "(" in line  # the id parens
    assert "expires" not in line and "every" not in line and "stall" not in line


def test_status_line_marks_a_lapsed_deadline(tmp_path):
    from graph.watches.types import Watch

    w = Watch(id="w", condition="c", deadline=1.0)  # epoch 1970, long past
    assert "past its deadline" in w.status_line()


def test_duration_renders_each_band():
    from graph.watches.types import _duration

    assert _duration(45) == "45s"
    assert _duration(600) == "10m"
    assert _duration(7200) == "2h"
    assert _duration(3 * 86400) == "3d"
    assert _duration(-5) == "0s"  # a just-lapsed deadline never renders negative


# --- update_watch: adjusting a live watch without losing its history -------


@pytest.mark.asyncio
async def test_update_watch_tool_adjusts_without_recreating(monkeypatch, tmp_path):
    ctrl, create, _l, update, _c = _tools(monkeypatch, tmp_path)
    create.invoke({"condition": "rollout finishes", "check": "p:v", "watch_id": "w", "interval_s": 60})
    out = await update.ainvoke({"watch_id": "w", "interval_s": 1800, "expires_in_s": 10800})
    assert "Watch updated" in out
    w = ctrl.store.get("w")
    assert w.interval_s == 1800
    assert w.deadline is not None
    assert w.condition == "rollout finishes"  # untouched


@pytest.mark.asyncio
async def test_update_watch_tool_can_clear_a_deadline(monkeypatch, tmp_path):
    """A nullable number can't express "clear it" — `None` on a tool arg means "not
    supplied" — so the tool takes an explicit `clear_deadline` flag."""
    ctrl, create, _l, update, _c = _tools(monkeypatch, tmp_path)
    create.invoke({"condition": "c", "check": "p:v", "watch_id": "w", "expires_in_s": 3600})
    assert ctrl.store.get("w").deadline is not None
    await update.ainvoke({"watch_id": "w", "clear_deadline": True})
    assert ctrl.store.get("w").deadline is None


@pytest.mark.asyncio
async def test_update_watch_tool_rejects_contradictory_deadline_args(monkeypatch, tmp_path):
    ctrl, create, _l, update, _c = _tools(monkeypatch, tmp_path)
    create.invoke({"condition": "c", "check": "p:v", "watch_id": "w"})
    out = await update.ainvoke({"watch_id": "w", "expires_in_s": 60, "clear_deadline": True})
    assert "not both" in out


@pytest.mark.asyncio
async def test_update_watch_tool_needs_something_to_change(monkeypatch, tmp_path):
    ctrl, create, _l, update, _c = _tools(monkeypatch, tmp_path)
    create.invoke({"condition": "c", "check": "p:v", "watch_id": "w"})
    assert "nothing to update" in await update.ainvoke({"watch_id": "w"})


@pytest.mark.asyncio
async def test_update_watch_tool_cannot_touch_an_operator_verifier(monkeypatch, tmp_path):
    # The tool runs untrusted, so the ADR 0067 D4 boundary holds through it too.
    ctrl, _create, _l, update, _c = _tools(monkeypatch, tmp_path)
    ctrl.create(condition="prod is healthy", watch_id="w", verifier={"type": "command", "command": "true"}, trusted=True)
    out = await update.ainvoke({"watch_id": "w", "condition": "anything"})
    assert "only its operator can edit it" in out
    assert ctrl.store.get("w").condition == "prod is healthy"


# --- repeat / on_change on the agent tool -----------------------------------


def test_create_watch_defaults_to_a_one_shot_tripwire(monkeypatch, tmp_path):
    ctrl, create, _l, _u, _c = _tools(monkeypatch, tmp_path)
    create.invoke({"condition": "deploy lands", "check": "p:v"})
    w = _only(ctrl)
    assert (w.trigger, w.repeat) == ("met", False)


def test_create_watch_repeat_keeps_it_armed(monkeypatch, tmp_path):
    ctrl, create, _l, _u, _c = _tools(monkeypatch, tmp_path)
    create.invoke({"condition": "a PR lands", "check": "p:v", "repeat": True})
    w = _only(ctrl)
    assert (w.trigger, w.repeat) == ("met", True)


def test_on_change_implies_repeat(monkeypatch, tmp_path):
    # A change monitor that stopped after one move would be a strange tripwire.
    ctrl, create, _l, _u, _c = _tools(monkeypatch, tmp_path)
    create.invoke({"condition": "the treasury", "check": "p:v", "on_change": True})
    w = _only(ctrl)
    assert (w.trigger, w.repeat) == ("change", True)


@pytest.mark.asyncio
async def test_update_watch_can_promote_a_tripwire_to_a_monitor(monkeypatch, tmp_path):
    ctrl, create, _l, update, _c = _tools(monkeypatch, tmp_path)
    create.invoke({"condition": "c", "check": "p:v", "watch_id": "w"})
    assert (ctrl.store.get("w").trigger, ctrl.store.get("w").repeat) == ("met", False)
    await update.ainvoke({"watch_id": "w", "on_change": True})
    w = ctrl.store.get("w")
    assert (w.trigger, w.repeat) == ("change", True)
