"""#4726 — a telemetry ``turns`` row records why a turn that did not complete ended.

``TurnOutcome.error`` existed, and so did the stall guard's stop reason, but ``turns`` had
no column for either. A failed turn's row said ``failed`` and nothing else. The row now has
an ``error`` column, redacted and clipped by ``record_turn``. These tests pin:

1. the column reaches an existing store, both a plain old one (guarded ALTER) and a
   pre-#3001 one whose ``task_id PRIMARY KEY`` forces the ``_migrate_to_row_id`` rebuild;
2. ``record_turn`` stores a redacted, clipped reason for a failed leg and none for a
   completed or parked one;
3. the reason reaches the stored row through the real drivers: the A2A executor (an error
   frame, a raise, a stall), the ``/v1`` non-streaming driver, and an ACP coder run;
4. a bare cancel inside a guarded turn's work records the stall guard's stop reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import importlib
import sqlite3
import sys

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import EventQueueLegacy as EventQueue
from a2a.types import Message, Part, Role, SendMessageRequest

from a2a_impl.executor import ProtoAgentExecutor, set_terminal_hook
from graph.config import LangGraphConfig
from graph.turn_liveness import TurnStop, bind_turn_stop
from observability.telemetry_store import TelemetryStore
from tests._turn_driver_fakes import Invoke, Raise, ScriptedGraph, text

chat_mod = importlib.import_module("server.chat")

# A token shape the redactor knows (`sk-` + 20 or more of its alphabet).
_SECRET = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"

_OLD_COLS = (
    "session_id TEXT, state TEXT, success INTEGER, model TEXT, "
    "models TEXT, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, "
    "cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER, cost_usd REAL, "
    "duration_ms INTEGER, llm_calls INTEGER, tool_calls INTEGER, created_at TEXT, ended_at TEXT"
)


def _columns(path: str) -> list[str]:
    db = sqlite3.connect(path)
    try:
        return [r[1] for r in db.execute("PRAGMA table_info(turns)")]
    finally:
        db.close()


def _row(task_id: str, **over) -> dict:
    base = dict(
        task_id=task_id, session_id="s1", state="failed", success=0, model="m", ended_at="2026-11-03T00:00:00+00:00"
    )
    base.update(over)
    return base


# ── 1. the column migrates onto an existing store ─────────────────────────────────


@pytest.mark.parametrize(
    "shape",
    [
        # Post-#3001 (row_id surrogate key) but from before the column: the guarded ALTER.
        pytest.param(f"row_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, {_OLD_COLS}", id="old"),
        # Pre-#3001: `task_id TEXT PRIMARY KEY` cannot be altered, so the table is rebuilt.
        pytest.param(f"task_id TEXT PRIMARY KEY, {_OLD_COLS}", id="pre-3001"),
    ],
)
def test_the_error_column_reaches_an_existing_store_and_keeps_its_rows(tmp_path, shape):
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE turns ({shape})")
    db.execute(
        "INSERT INTO turns (task_id, state, model, input_tokens, ended_at) "
        "VALUES ('legacy-1', 'failed', 'm', 7, '2026-05-01T00:00:00+00:00')"
    )
    db.commit()
    db.close()

    store = TelemetryStore(path)
    assert "error" in _columns(path)
    assert "row_id" in _columns(path)

    (legacy,) = store.recent()
    assert legacy["task_id"] == "legacy-1" and legacy["input_tokens"] == 7  # history kept
    assert legacy["error"] is None  # a row from before the column says nothing, not ""

    store.record(_row("new-1", error="upstream 500"))
    TelemetryStore(path)  # re-opening is a no-op
    by_id = {r["task_id"]: r["error"] for r in store.recent()}
    assert by_id == {"legacy-1": None, "new-1": "upstream 500"}


def test_a_fresh_store_has_the_column(tmp_path):
    path = str(tmp_path / "fresh.db")
    TelemetryStore(path)
    assert _columns(path)[-1] == "error"


# ── 2. record_turn stores a redacted, clipped reason ────────────────────────────────


@pytest.fixture
def store(monkeypatch, tmp_path):
    import runtime.state as rs

    s = TelemetryStore(str(tmp_path / "telemetry.db"))
    monkeypatch.setattr(rs.STATE, "telemetry_store", s, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    return s


def _errors(store: TelemetryStore) -> dict[str, str | None]:
    return {r["task_id"]: r["error"] for r in store.recent()}


def test_record_turn_stores_a_redacted_clipped_error_for_a_failed_turn(store):
    from server.turn_telemetry import _ERROR_MAX, record_turn

    long_tail = "x" * 2000
    record_turn(
        task_id="f", session_id="s", state="failed", error=f"401 from provider, key {_SECRET} rejected. {long_tail}"
    )

    err = _errors(store)["f"]
    assert _SECRET not in err and "[REDACTED]" in err
    assert err.startswith("401 from provider, key [REDACTED] rejected.")
    assert len(err) == _ERROR_MAX and err.endswith("…")


def test_a_secret_straddling_the_clip_point_is_still_redacted(store):
    # Redact first, then clip. Clipping first would cut the token in half, and half a
    # token no longer matches the redactor.
    from server.turn_telemetry import _ERROR_MAX, record_turn

    lead = "e " * ((_ERROR_MAX - 10) // 2)
    record_turn(task_id="f", session_id="s", state="failed", error=f"{lead}{_SECRET}")
    err = _errors(store)["f"]
    assert _SECRET[:8] not in err


def test_completed_and_parked_turns_store_no_error(store):
    from server.turn_telemetry import record_turn

    record_turn(task_id="ok", session_id="s", state="completed", error="ignored")
    record_turn(task_id="hitl", session_id="s", state="input_required", error="ignored")
    record_turn(task_id="short", session_id="s", state="failed", error="  timed out  ")
    assert _errors(store) == {"ok": "", "hitl": "", "short": "timed out"}


# ── 4. the stall guard's stop reason fills in for a bare cancel ─────────────────────


def _in_guarded_turn(reason: str, fn) -> None:
    """Run ``fn`` the way the stall guard runs a turn's work: in a context copy with a
    ``TurnStop`` bound, whose reason the guard set when it gave up on the turn."""
    stop = TurnStop()
    stop.reason = reason
    ctx = contextvars.copy_context()
    bind_turn_stop(ctx, stop)
    ctx.run(fn)


def test_a_bare_cancel_under_a_stalled_turn_records_the_stop_reason(store):
    from server.turn_telemetry import record_turn

    stalled = "The turn stalled: no progress for 300s while running tool delegate_to."
    _in_guarded_turn(stalled, lambda: record_turn(task_id="c1", session_id="s", state="failed", error="cancelled"))
    _in_guarded_turn(stalled, lambda: record_turn(task_id="c2", session_id="s", state="canceled", error=""))
    # A real reason from the caller wins over the stop reason.
    _in_guarded_turn(stalled, lambda: record_turn(task_id="c3", session_id="s", state="failed", error="AcpError: boom"))
    # Outside a guarded turn there is no stop reason, so a plain cancel stays plain.
    record_turn(task_id="c4", session_id="s", state="canceled", error="cancelled")
    record_turn(task_id="c5", session_id="s", state="canceled", error="")

    assert _errors(store) == {"c1": stalled, "c2": stalled, "c3": "AcpError: boom", "c4": "cancelled", "c5": ""}


# ── 3. through the real drivers ─────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Real telemetry store and real drivers; only the graph and tracing are faked."""
    from observability import tracing

    import runtime.state as rs

    s = TelemetryStore(str(tmp_path / "telemetry.db"))
    cfg = LangGraphConfig()
    cfg.model_name = "configured-default-model"
    for attr, val in {
        "telemetry_store": s,
        "goal_controller": None,
        "background_mgr": None,
        "watch_controller": None,
        "scheduler": None,
        "graph_auth_error": None,
        "thread_id_resolver": None,
        "checkpointer": object(),
        "knowledge_store": None,
        "graph_config": cfg,
    }.items():
        monkeypatch.setattr(rs.STATE, attr, val, raising=False)

    @contextlib.asynccontextmanager
    async def trace_session(session_id, name="agent-session", metadata=None, input=None, incognito=False):
        yield None

    monkeypatch.setattr(tracing, "trace_session", trace_session)
    monkeypatch.setattr(tracing, "flush", lambda: None)
    monkeypatch.setattr(tracing, "set_session_output", lambda out: None)

    def install(streams=(), invokes=()):
        monkeypatch.setattr(rs.STATE, "graph", ScriptedGraph(streams=streams, invokes=invokes), raising=False)

    s.install = install
    return s


@pytest.fixture
def a2a_hook():
    """The real A2A telemetry recorder as the executor's terminal hook."""
    from server.a2a import _record_a2a_telemetry

    set_terminal_hook(_record_a2a_telemetry)
    yield
    set_terminal_hook(None)


def _request_context(task_id: str = "t-1") -> RequestContext:
    req = SendMessageRequest(message=Message(message_id="m-1", role=Role.ROLE_USER, parts=[Part(text="hi")]))
    return RequestContext(call_context=ServerCallContext(), request=req, task_id=task_id, context_id="c-1")


async def test_a_turn_that_raises_mid_stream_stores_why_through_the_a2a_executor(env, a2a_hook):
    env.install(streams=[[text("r1", "partial "), Raise(RuntimeError(f"provider exploded, key {_SECRET}"))]])

    await ProtoAgentExecutor(chat_mod._chat_langgraph_stream).execute(_request_context(), EventQueue())

    (row,) = env.recent()
    assert row["state"] == "failed"
    assert "provider exploded" in row["error"]
    assert _SECRET not in row["error"]


async def test_an_error_frame_stores_its_message_through_the_a2a_executor(env, a2a_hook):
    async def stream(text, context_id, **kwargs):
        yield ("text", "partial")
        yield ("error", "invalid api key")

    await ProtoAgentExecutor(stream).execute(_request_context(), EventQueue())

    (row,) = env.recent()
    assert (row["state"], row["error"]) == ("failed", "invalid api key")


async def test_a_stalled_turn_stores_the_stall_message_through_the_a2a_executor(env, a2a_hook):
    async def stream(text, context_id, **kwargs):
        yield ("text", "working")
        await asyncio.sleep(3600)
        yield ("done", "never")

    await ProtoAgentExecutor(stream, stall_timeout_provider=lambda: 0.05).execute(_request_context(), EventQueue())

    (row,) = env.recent()
    assert row["state"] == "failed"
    assert row["error"].startswith("The turn stalled: no progress for 0.05s")


async def test_a_completed_a2a_turn_stores_no_error(env, a2a_hook):
    async def stream(text, context_id, **kwargs):
        yield ("done", "fine")

    await ProtoAgentExecutor(stream).execute(_request_context(), EventQueue())
    (row,) = env.recent()
    assert (row["state"], row["error"]) == ("completed", "")


async def test_a_failed_v1_turn_stores_why(env):
    class _Upstream(Exception):
        status_code = 429

    env.install(invokes=[Invoke(raises=_Upstream("rate limited, retry after 20s"))])

    await chat_mod.chat("hello", "s-fail", origin="v1")

    (row,) = env.recent()
    assert row["state"] == "failed"
    assert "rate limited, retry after 20s" in row["error"]


async def test_a_failed_coder_run_stores_why(store, tmp_path):
    from plugins.coding_agent.acp_client import AcpClient, AcpError
    from tests.test_coder_telemetry import _ERROR_BODY, _agent

    client = AcpClient(sys.executable, _agent(tmp_path, "coder", _ERROR_BODY), cwd=str(tmp_path), name="coder")
    try:
        with pytest.raises(AcpError):
            await client.prompt("ship it", timeout=30.0)
    finally:
        await client.close()

    (row,) = store.recent()
    assert row["state"] == "failed"
    assert "the coder blew up" in row["error"]


# ── 6. review follow-ups: incognito, background-job errors, fail-closed helper ─────


def test_an_incognito_turn_keeps_its_state_but_stores_no_error_text(store):
    # A provider or exception message can echo the user's input, and an incognito turn
    # keeps timing and usage but no content (ADR 0069).
    from server.turn_telemetry import record_turn

    record_turn(task_id="inc", session_id="s", state="failed", error="echoed: my private text", incognito=True)
    row = next(r for r in store.recent() if r["task_id"] == "inc")
    assert (row["state"], row["error"]) == ("failed", "")


def test_an_incognito_a2a_turn_reaches_record_turn_as_incognito(monkeypatch):
    import server.turn_telemetry as tt
    from a2a_impl.executor import TurnOutcome
    from server.a2a import _record_a2a_telemetry

    seen = {}
    monkeypatch.setattr(tt, "record_turn", lambda **kw: seen.update(kw))
    _record_a2a_telemetry(
        TurnOutcome(task_id="t", context_id="c", state="failed", text="", error="boom", incognito=True)
    )
    assert (seen["error"], seen["incognito"]) == ("boom", True)


def test_safe_error_text_fails_closed_when_redaction_breaks(monkeypatch):
    import graph.middleware.redaction as red
    from observability.error_text import WITHHELD, safe_error_text

    def _boom(_text):
        raise RuntimeError("redactor down")

    monkeypatch.setattr(red, "redact", _boom)
    assert safe_error_text(f"key {_SECRET}") == WITHHELD
    assert safe_error_text("   ") == ""


def test_a_background_job_error_is_redacted_before_it_is_stored_and_published(monkeypatch):
    import types

    import runtime.state as rs
    import server.a2a as a2a_mod

    settled, published = {}, []

    class _Store:
        def mark_complete(self, job_id, status, text, *, cost_usd=None, error=""):
            settled.update(job_id=job_id, status=status, error=error)

        def get(self, job_id):
            return None

    monkeypatch.setattr(rs.STATE, "background_mgr", types.SimpleNamespace(store=_Store()), raising=False)
    monkeypatch.setattr(a2a_mod._event_bus, "publish", lambda *a, **k: published.append(a))
    outcome = types.SimpleNamespace(
        context_id="background:bg-0123456789ab", trigger="", state="failed", text="", error=f"401: {_SECRET}"
    )
    a2a_mod._handle_background_terminal(outcome)
    assert settled["status"] == "failed"
    assert _SECRET not in settled["error"] and "[REDACTED]" in settled["error"]


def test_the_background_routes_redact_an_error_stored_before_redaction():
    import types

    from operator_api.routes import _bg_job_out

    job = types.SimpleNamespace(to_dict=lambda: {"id": "bg-1", "error": f"raw {_SECRET}", "result": "r"})
    out = _bg_job_out(job)
    assert _SECRET not in out["error"] and out["result"] == "r"
    assert _bg_job_out(types.SimpleNamespace(to_dict=lambda: {"id": "bg-2", "error": ""}))["error"] == ""
