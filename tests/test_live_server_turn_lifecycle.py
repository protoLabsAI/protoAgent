"""``_LIVE_SERVER_TURNS`` can't leak (#3933).

A server-fired turn is registered as operator-addressable on the executor's
``turn_started`` progress frame (server/a2a.py). Its deregistration used to depend on
the terminal hook running — which a crash path (a terminal status update that itself
raises) skips — and the registry had no bound. Now:

- the executor fires a ``turn_ended`` progress frame from its ``finally`` on EVERY
  exit, and the host forgets the turn on it;
- the terminal hook forgets the turn BEFORE its best-effort telemetry, so a telemetry
  failure can't skip it;
- the registry is capped like its sibling ``_ATTENDED_SESSIONS``, evicting the oldest
  entry with a warning.
"""

from __future__ import annotations

import logging

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import EventQueueLegacy as EventQueue
from a2a.types import Message, Part, Role, SendMessageRequest
from google.protobuf.struct_pb2 import Struct

from a2a_impl.executor import ProtoAgentExecutor, TurnOutcome, set_progress_hook, set_terminal_hook
from server import turn_control


@pytest.fixture(autouse=True)
def _clean_registry():
    turn_control._LIVE_SERVER_TURNS.clear()
    turn_control._ATTENDED_SESSIONS.clear()
    set_progress_hook(None)
    set_terminal_hook(None)
    yield
    turn_control._LIVE_SERVER_TURNS.clear()
    turn_control._ATTENDED_SESSIONS.clear()
    set_progress_hook(None)
    set_terminal_hook(None)


# ── the bound ─────────────────────────────────────────────────────────────────


def _register(i: int):
    return turn_control.register_live_server_turn(f"s{i}", f"t{i}", origin="scheduler", attended=True)


def test_registry_is_capped_and_evicts_the_oldest_with_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(turn_control, "_LIVE_SERVER_TURNS_MAX", 3)
    for i in range(3):
        assert _register(i) is not None

    with caplog.at_level(logging.WARNING):
        assert _register(3) is not None

    assert len(turn_control._LIVE_SERVER_TURNS) == 3
    assert turn_control.live_server_turn_control("s0", "t0") is None  # the oldest went
    assert all(turn_control._LIVE_SERVER_TURNS.get(turn_control._server_turn_key(f"s{i}", f"t{i}")) for i in (1, 2, 3))
    assert any("control registry at cap" in r.getMessage() and "s0" in r.getMessage() for r in caplog.records)


def test_re_registering_a_live_turn_at_the_cap_evicts_nothing(monkeypatch, caplog):
    monkeypatch.setattr(turn_control, "_LIVE_SERVER_TURNS_MAX", 2)
    _register(0)
    _register(1)

    with caplog.at_level(logging.WARNING):
        _register(1)  # a HITL resume re-announces the SAME turn

    assert len(turn_control._LIVE_SERVER_TURNS) == 2
    assert not [r for r in caplog.records if "control registry at cap" in r.getMessage()]


# ── deregistration is guaranteed ─────────────────────────────────────────────


class _TerminalUpdateFails(EventQueue):
    """Accepts the opening Task + the WORKING update, then fails every later enqueue —
    a terminal status update that raises, so the executor never reaches its terminal
    hook (the crash path that stranded registry entries)."""

    def __init__(self):
        super().__init__()
        self.n = 0

    async def enqueue_event(self, event):
        self.n += 1
        if self.n > 2:
            raise RuntimeError("event queue closed")
        return await super().enqueue_event(event)


def _context() -> RequestContext:
    md = Struct()
    md.update({"origin": "scheduler", "attended": True})
    msg = Message(message_id="m", role=Role.ROLE_USER, parts=[Part(text="tick")], metadata=md)
    return RequestContext(
        call_context=ServerCallContext(), request=SendMessageRequest(message=msg), task_id="t1", context_id="c1"
    )


async def test_a_turn_whose_terminal_update_crashes_still_deregisters():
    from server.a2a import _a2a_progress

    set_progress_hook(_a2a_progress)
    terminal: list = []
    set_terminal_hook(terminal.append)
    seen_mid_turn: list = []

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        seen_mid_turn.append(turn_control.live_server_turn_control("c1", "t1"))
        yield ("done", "ok")

    with pytest.raises(RuntimeError, match="event queue closed"):
        await ProtoAgentExecutor(stream).execute(_context(), _TerminalUpdateFails())

    assert seen_mid_turn and seen_mid_turn[0] is not None  # it WAS registered
    assert terminal == []  # the terminal hook never ran — the crash path
    assert turn_control.live_server_turn_control("c1", "t1") is None
    assert turn_control._LIVE_SERVER_TURNS == {}


async def test_the_executor_fires_turn_ended_on_a_normal_exit_too():
    frames: list = []
    set_progress_hook(lambda ctx, task, frame: frames.append(frame))

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("done", "ok")

    await ProtoAgentExecutor(stream).execute(_context(), EventQueue())

    assert frames[0]["phase"] == "turn_started"
    assert frames[-1] == {"phase": "turn_ended", "origin": "scheduler"}


def test_terminal_hook_forgets_the_turn_even_when_telemetry_raises(monkeypatch):
    import server.a2a as a2a

    _register(9)

    def _boom(outcome):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(a2a, "_record_a2a_telemetry", _boom)
    outcome = TurnOutcome(task_id="t9", context_id="s9", state="completed", text="", origin="scheduler")
    with pytest.raises(RuntimeError, match="telemetry down"):
        a2a._a2a_terminal(outcome)

    assert turn_control.live_server_turn_control("s9", "t9") is None
