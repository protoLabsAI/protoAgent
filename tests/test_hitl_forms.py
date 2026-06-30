"""Server-side HITL form primitive (Sprint A): the `request_user_input` tool +
the interrupt→input-required payload shaping that lets a JSON-schema form survive
to the A2A layer / console (generalizing `ask_human`)."""

from __future__ import annotations

import server
from tools.lg_tools import DEFERRED_BASE_TOOL_NAMES, get_all_tools


def _tool_names() -> set[str]:
    return {getattr(t, "name", "") for t in get_all_tools()}


def test_request_user_input_registered_as_default_lead_tool():
    names = _tool_names()
    assert "request_user_input" in names
    assert "ask_human" in names  # the free-text sibling stays too


def test_request_user_input_in_deferred_base():
    # Must stay visible when tool-deferral is on (it's a core HITL capability).
    assert "request_user_input" in DEFERRED_BASE_TOOL_NAMES


def test_request_user_input_rejects_empty_steps():
    # A zero-step form degrades to a bare free-text box (dropping the structured
    # contract), so the tool guards it and returns guidance instead of interrupting.
    from tools.lg_tools import request_user_input

    out = request_user_input.invoke({"title": "Pick", "steps": []})
    assert isinstance(out, str) and out.startswith("Error:")
    assert "ask_human" in out


# ── interrupt → input-required payload shaping ────────────────────────────────


def test_form_payload_passes_through():
    val = {"kind": "form", "title": "Pick", "description": "", "steps": [{"schema": {}}]}
    assert server._interrupt_payload(val) is val


def test_question_payload_passes_through():
    assert server._interrupt_payload({"question": "Merge it?"}) == {"question": "Merge it?"}


def test_plain_value_degrades_to_question():
    assert server._interrupt_payload("just text") == {"question": "just text"}
    assert server._interrupt_payload(None) == {"question": "Input required."}
    # A dict that's neither a question nor a form is stringified into a question
    # (never silently dropped).
    out = server._interrupt_payload({"foo": 1})
    assert "question" in out and "foo" in out["question"]


# ── autonomous-turn HITL guard ────────────────────────────────────────────────
# A scheduler/inbox/webhook/background turn has no operator watching the chat, so a HITL
# pause would park the task in input-required forever (and that state is TTL-exempt). The
# native turn must auto-answer the interrupt and complete instead of parking.

import importlib

import pytest

from runtime.state import STATE

# `server.chat` the attribute is shadowed by the re-exported `chat` function in
# server/__init__.py, so resolve the actual submodule from sys.modules.
chat_mod = importlib.import_module("server.chat")


class _FakeTurnStream:
    """Stand-in for ``_run_turn_stream``: records each pass's ``resume_value`` and yields a
    HITL interrupt on the first (fresh) pass, then a real answer once resumed."""

    def __init__(self):
        self.resume_values: list = []

    def __call__(self, message, session_id, config, *, resume_value=None, **_kw):
        self.resume_values.append(resume_value)
        first = len(self.resume_values) == 1

        async def _gen():
            if first:
                yield ("input_required", {"question": "Which environment?"})
            else:
                yield ("text", "Proceeding with staging.")
                yield ("__raw__", "Proceeding with staging.")

        return _gen()


async def _collect(agen):
    return [frame async for frame in agen]


@pytest.mark.asyncio
async def test_autonomous_turn_auto_answers_hitl(monkeypatch):
    # No goal controller → the goal-verification block is skipped; isolate the turn loop.
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    fake = _FakeTurnStream()
    monkeypatch.setattr(chat_mod, "_run_turn_stream", fake)

    frames = await _collect(
        chat_mod._run_native_turn(
            "run the deploy",
            "s-auto",
            {"configurable": {"thread_id": "t-auto"}},
            request_metadata={"origin": "scheduler"},
        )
    )
    kinds = [k for k, _ in frames]
    assert "input_required" not in kinds  # never parks an autonomous turn
    assert ("done", "Proceeding with staging.") in frames  # it ran to completion
    # The interrupt was resumed with the no-operator sentinel (first pass is the fresh input).
    assert fake.resume_values[0] is None
    assert chat_mod._AUTONOMOUS_HITL_SENTINEL in fake.resume_values


@pytest.mark.asyncio
async def test_operator_turn_still_parks_on_hitl(monkeypatch):
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    fake = _FakeTurnStream()
    monkeypatch.setattr(chat_mod, "_run_turn_stream", fake)

    frames = await _collect(
        chat_mod._run_native_turn(
            "merge it",
            "s-op",
            {"configurable": {"thread_id": "t-op"}},
            request_metadata={},  # empty origin = live operator → must still park
        )
    )
    kinds = [k for k, _ in frames]
    assert "input_required" in kinds  # parked for the human to answer
    assert "done" not in kinds  # a parked turn yields no terminal answer
    assert fake.resume_values == [None]  # never auto-resumed


class _AlwaysAsksStream:
    """A model that re-asks on every pass — exercises the over-cap give-up path."""

    def __init__(self):
        self.resume_values: list = []

    def __call__(self, message, session_id, config, *, resume_value=None, **_kw):
        self.resume_values.append(resume_value)

        async def _gen():
            yield ("input_required", {"question": "again?"})

        return _gen()


@pytest.mark.asyncio
async def test_autonomous_turn_force_completes_after_cap(monkeypatch):
    # A model that ignores the no-operator sentinel and keeps asking must still NEVER park an
    # autonomous turn: after the cap it force-completes and clears the stray interrupt.
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    fake = _AlwaysAsksStream()
    monkeypatch.setattr(chat_mod, "_run_turn_stream", fake)
    cleared: list = []

    async def _fake_clear(config):
        cleared.append(config)

    monkeypatch.setattr(chat_mod, "_clear_pending_interrupt", _fake_clear)

    frames = await _collect(
        chat_mod._run_native_turn(
            "run the deploy",
            "s-cap",
            {"configurable": {"thread_id": "t-cap"}},
            request_metadata={"origin": "scheduler"},
        )
    )
    kinds = [k for k, _ in frames]
    assert "input_required" not in kinds  # never parks
    assert kinds[-1] == "done"  # forced to a terminal state
    # cap auto-answers (each resumed with the sentinel) + 1 fresh pass + 1 give-up pass.
    assert fake.resume_values[0] is None
    assert fake.resume_values.count(chat_mod._AUTONOMOUS_HITL_SENTINEL) == chat_mod._MAX_AUTONOMOUS_AUTOANSWERS
    assert len(fake.resume_values) == chat_mod._MAX_AUTONOMOUS_AUTOANSWERS + 1
    assert len(cleared) == 1  # the stray interrupt was cleared exactly once
