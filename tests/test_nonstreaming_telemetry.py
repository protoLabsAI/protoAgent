"""The non-streaming turn driver records telemetry (#3000).

`/v1/chat/completions`, `/api/chat`, and the plugin `HOST.invoke()` seam all run
through `server.chat._chat_langgraph`, which recorded nothing at all: no store
row, no Prometheus sample. Those turns spend real tokens, so every cost total,
success rate, and latency percentile described a subset of real traffic with no
indication it was a subset.

The tests below pin the durable end — what lands in the store — rather than the
call that was supposed to write it.
"""

from __future__ import annotations

import pytest

from observability.telemetry_store import TelemetryStore


@pytest.fixture
def store(tmp_path):
    return TelemetryStore(str(tmp_path / "telemetry.db"))


@pytest.fixture
def wired(store, monkeypatch):
    """Point STATE's telemetry holder at the test store."""
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "telemetry_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    return store


class _FakeUsageCallback:
    """Stands in for the turn's usage callback — the shape `_record_local_turn`
    reads off the sink."""

    def __init__(self, usage_metadata, llm_calls=0, tool_calls=0):
        self.usage_metadata = usage_metadata
        self.llm_calls = llm_calls
        self.tool_calls = tool_calls


def _chat_module():
    """`server.chat` — imported by path, since `server/__init__.py` re-exports the
    `chat` FUNCTION under that name and shadows the submodule attribute."""
    import importlib

    return importlib.import_module("server.chat")


def _record(sink, **kw):
    from server.chat import _record_local_turn

    kw.setdefault("session_id", "sess-1")
    kw.setdefault("origin", "v1")
    kw.setdefault("state", "completed")
    kw.setdefault("started", 0.0)
    _record_local_turn(sink, **kw)


def test_a_non_streaming_turn_writes_a_row(wired):
    cb = _FakeUsageCallback(
        {
            "claude-opus-4-8": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "input_token_details": {"cache_read": 400, "cache_creation": 100},
            }
        },
        llm_calls=3,
        tool_calls=2,
    )
    _record({"usage_cb": cb})

    rows = wired.recent()
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess-1"
    assert row["state"] == "completed" and row["success"] == 1
    assert row["model"] == "claude-opus-4-8"
    assert (row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (1000, 200, 1200)
    assert (row["cache_read_input_tokens"], row["cache_creation_input_tokens"]) == (400, 100)
    assert (row["llm_calls"], row["tool_calls"]) == (3, 2)
    assert row["cost_usd"] > 0  # priced from the model's real rate, not left at zero


def test_the_row_names_the_surface_that_spent_the_tokens(wired):
    # These turns have no A2A task, so the origin prefix is the only thing telling a
    # /v1 row apart from a console one — which is the question they exist to answer.
    for origin in ("v1", "api-chat", "plugin"):
        _record({"usage_cb": _FakeUsageCallback({"m": {"input_tokens": 5, "output_tokens": 1}})}, origin=origin)
    prefixes = sorted(r["task_id"].split(":")[0] for r in wired.recent())
    assert prefixes == ["api-chat", "plugin", "v1"]
    assert all(len(r["task_id"].split(":")[1]) == 12 for r in wired.recent())  # unique per turn


def test_cost_is_summed_per_model_not_on_the_totals(wired):
    """A turn that routed across two models bills each at its OWN rate.

    Collapsing the usage first and pricing once would charge the whole turn at
    whichever model happened to be listed — the exact undercount #2872 fixed for
    the streaming path.
    """
    from observability import pricing

    cb = _FakeUsageCallback(
        {
            "claude-opus-4-8": {"input_tokens": 1_000_000, "output_tokens": 0},
            "claude-haiku-4-5": {"input_tokens": 1_000_000, "output_tokens": 0},
        }
    )
    _record({"usage_cb": cb})

    expected = pricing.rate_for("claude-opus-4-8")["input"] * 1_000_000
    expected += pricing.rate_for("claude-haiku-4-5")["input"] * 1_000_000
    assert wired.recent()[0]["cost_usd"] == pytest.approx(expected, rel=1e-6)
    assert sorted(wired.recent()[0]["models"].split(",")) == ["claude-haiku-4-5", "claude-opus-4-8"]


def test_a_failed_turn_is_recorded_as_failed(wired):
    _record({"usage_cb": _FakeUsageCallback({"m": {"input_tokens": 9, "output_tokens": 1}})}, state="failed")
    row = wired.recent()[0]
    assert row["state"] == "failed" and row["success"] == 0


def test_a_control_plane_reply_gets_no_row(wired):
    # A turn that short-circuited before the graph — an unknown slash command,
    # "setup not complete", a HITL hold — never populates the sink. It spent
    # nothing, and counting it as a turn would corrupt every rate the store reports.
    _record({})
    assert wired.recent() == []


def test_a_turn_that_reached_the_graph_but_called_no_model_gets_no_row(wired):
    # An ACP turn meters externally and reports zeros; a tool-only short-circuit
    # makes no model call. Neither is spend worth a row.
    _record({"usage_cb": _FakeUsageCallback({})})
    assert wired.recent() == []


def test_telemetry_failure_never_reaches_the_caller(wired, monkeypatch):
    class _Exploding:
        def record(self, row):
            raise RuntimeError("disk full")

    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "telemetry_store", _Exploding(), raising=False)
    _record({"usage_cb": _FakeUsageCallback({"m": {"input_tokens": 1, "output_tokens": 1}})})  # must not raise


@pytest.mark.asyncio
async def test_the_wrapper_bills_a_turn_that_raised(wired, monkeypatch):
    """The row is written from a `finally`, so a turn that dies mid-flight still
    bills what it spent before it died — the expensive failures are exactly the
    ones worth seeing."""
    chat_mod = _chat_module()

    cb = _FakeUsageCallback({"m": {"input_tokens": 500, "output_tokens": 20}}, llm_calls=1)

    async def _boom(message, session_id, **kw):
        kw["_telemetry_sink"]["usage_cb"] = cb
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(chat_mod, "_chat_langgraph_impl", _boom)
    with pytest.raises(RuntimeError):
        await chat_mod._chat_langgraph("hi", "sess-boom", origin="v1")

    row = wired.recent()[0]
    assert row["state"] == "failed" and row["input_tokens"] == 500


@pytest.mark.asyncio
async def test_the_wrapper_reads_the_error_key_not_an_exception(wired, monkeypatch):
    """The impl catches its own exceptions and reports them as an assistant bubble
    carrying a structured `error`, so that key — not a raise — is what marks a
    turn failed."""
    chat_mod = _chat_module()

    cb = _FakeUsageCallback({"m": {"input_tokens": 7, "output_tokens": 2}})

    async def _soft_fail(message, session_id, **kw):
        kw["_telemetry_sink"]["usage_cb"] = cb
        return [{"role": "assistant", "content": "**Error:** nope", "error": {"type": "server_error"}}]

    monkeypatch.setattr(chat_mod, "_chat_langgraph_impl", _soft_fail)
    await chat_mod._chat_langgraph("hi", "sess-soft", origin="api-chat")

    assert wired.recent()[0]["state"] == "failed"


def test_the_usage_callback_works_against_the_real_langchain_base():
    """Drives the REAL `UsageMetadataCallbackHandler`, not a stand-in.

    Worth its own test because a stand-in cannot catch what this class actually
    gets wrong: the base's `on_tool_end` is an empty stub whose signature requires
    a keyword-only `run_id`, so an override that politely delegated to it raised
    `TypeError` inside a live turn's callback path — while every test that
    asserted the subclass's own shape stayed green.
    """
    import uuid

    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    chat = _chat_module()
    cb = chat._make_usage_callback()
    msg = AIMessage(
        content="hi",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 200,
            "total_tokens": 1200,
            "input_token_details": {"cache_read": 400},
        },
        response_metadata={"model_name": "claude-opus-5"},
    )
    result = LLMResult(generations=[[ChatGeneration(message=msg)]])

    cb.on_llm_end(result, run_id=uuid.uuid4())
    cb.on_llm_end(result, run_id=uuid.uuid4())
    cb.on_tool_end("out", run_id=uuid.uuid4())
    cb.on_tool_end("out")  # and without one — a counter must never break a turn

    assert (cb.llm_calls, cb.tool_calls) == (2, 2)
    models, totals, cost = chat._telemetry_usage(cb.usage_metadata)
    assert models == ["claude-opus-5"]
    assert totals["input_tokens"] == 2000 and totals["cache_read_input_tokens"] == 800
    assert cost == pytest.approx(2 * 1000 * 5e-6 + 2 * 200 * 25e-6)
