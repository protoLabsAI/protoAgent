"""A2A peer cost-v1 → the calling turn's telemetry (#3016).

A protoAgent peer measures the turn it runs for us and ships the numbers back on the
terminal artifact's cost-v1 extension metadata. The delegates a2a adapter used to read
only ``_extract_text`` and drop them, so a hub that fanned work out to members reported
only its own thinking. These tests cover the whole path:

  - reading the payload off the EXACT shape our own executor emits (the fixture is a
    real turn through the real a2a-sdk app, not a hand-written envelope);
  - the adapter emitting one tagged ``usage`` custom event per delegation, on both the
    inline and the polled terminal path — and NOT on the three paths that must stay
    unbilled: a park, a resume of an already-finished task, and a detached background
    delegation (which inherits the tool body's run context and so has to be excluded
    deliberately rather than by accident);
  - the durable outcomes — the peer's tokens and cost in the calling turn's accumulator,
    in the STORED telemetry row, and in the cost-v1 this instance itself puts back on
    the wire (so a hub→member→member chain rolls up) — while the peer's prompt size
    stays out of the lead thread's context fill, and the `peer:` marker stays out of
    every field that names the model that ran;
  - silent degradation for a peer that emits no cost-v1, and for one whose payload we
    can't read at all: telemetry never turns a good delegation into a failed one.
"""

from __future__ import annotations

import asyncio
import json
import math

import httpx
import protolabs_a2a as pa
import pytest
from langchain_core.callbacks import adispatch_custom_event
from langchain_core.runnables import RunnableLambda

from plugins.delegates.adapters import ADAPTERS, _peer_usage_row
from plugins.delegates.registry import DelegateRegistry
from tests.test_a2a_handler import _build_app, _poll_terminal, _send_msg
from tests.test_telemetry import _run_turn_outcome


@pytest.fixture(autouse=True)
def _allow_peer_urls(monkeypatch):
    """The fake peer URLs aren't real destinations; the SSRF policy is tested elsewhere."""
    from security import policy

    monkeypatch.setattr(policy, "check_url", lambda url: None)


# ── reading the wire shape our own executor emits ─────────────────────────────


async def test_peer_usage_row_reads_the_shape_our_own_executor_emits():
    """The fixture is a REAL turn through the real a2a-sdk app: whatever
    ``_terminal_parts`` puts on the wire is what the reader must parse. A
    hand-written envelope would keep passing after the emitter moved (as it did
    when protolabs-a2a 0.3.0 took the payload off DataParts)."""

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("text", "peer answer")
        yield (
            "usage",
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 5,
                "cost_usd": 0.004,
                "model": "peer-lead",
            },
        )
        yield ("usage", {"input_tokens": 140, "output_tokens": 20, "cost_usd": 0.002, "model": "peer-lead"})
        yield ("done", "peer answer")

    app = _build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await _send_msg(c)).json()["result"]["task"]
        final = await _poll_terminal(c, task["id"])

    # Both envelopes the adapter can hold: GetTask returns the bare task, SendMessage
    # wraps it in {"task": …} — `_extract_text` tolerates both and so must this.
    for envelope in (final, {"task": final}):
        row = _peer_usage_row(envelope, "orbis")
        assert row is not None, "the peer's own cost-v1 was not found on the terminal artifact"
        # The peer's SUMMED turn spend, verbatim — not re-derived from our pricing table.
        assert row["input_tokens"] == 240
        assert row["output_tokens"] == 70
        assert row["cache_read_input_tokens"] == 20
        assert row["cache_creation_input_tokens"] == 5
        assert row["cost_usd"] == pytest.approx(0.006)
        # Tagged: `peer` keeps it out of the lead thread's context fill, and the
        # `peer:` model marker is what makes peer spend legible in the stored row.
        assert row["peer"] == "orbis"
        assert row["model"] == "peer:orbis"


def _artifact(metadata=None, text="hi from peer"):
    art = {"parts": [{"text": text}]}
    if metadata is not None:
        art["metadata"] = metadata
    return art


def _cost_meta(usage, *, cost_usd=None, duration_ms=12):
    return pa.cost_metadata(usage, cost_usd=cost_usd, duration_ms=duration_ms, success=True)


def test_peer_usage_row_is_none_without_cost_v1():
    """A non-protoAgent A2A agent emits no cost-v1 — nothing to bill, and the caller
    must behave exactly as it did before #3016."""
    assert _peer_usage_row({"task": {"artifacts": [_artifact()]}}, "foreign") is None
    assert _peer_usage_row({"artifacts": [_artifact(metadata={"someOtherExt": {"x": 1}})]}, "foreign") is None
    assert _peer_usage_row({}, "foreign") is None
    assert _peer_usage_row(None, "foreign") is None
    assert _peer_usage_row("not a dict at all", "foreign") is None


def test_peer_usage_row_falls_back_to_the_terminal_status_message():
    """The extension permits terminal telemetry on the status message's metadata as
    well as the artifact's; a peer that puts it there must still be billed."""
    result = {
        "task": {
            "status": {"state": "TASK_STATE_COMPLETED", "message": {"metadata": _cost_meta({"input_tokens": 7})}},
            "artifacts": [_artifact()],
        }
    }
    assert _peer_usage_row(result, "p")["input_tokens"] == 7


def test_peer_usage_row_takes_the_LAST_cost_bearing_artifact():
    """A peer that appends a fresh artifact per leg must bill the leg we just caused,
    not the first one still hanging off the task."""
    result = {
        "task": {
            "artifacts": [
                _artifact(metadata=_cost_meta({"input_tokens": 1}, cost_usd=0.1)),
                _artifact(metadata=_cost_meta({"input_tokens": 900}, cost_usd=0.9)),
            ]
        }
    }
    row = _peer_usage_row(result, "p")
    assert row["input_tokens"] == 900 and row["cost_usd"] == pytest.approx(0.9)


def test_peer_usage_row_coerces_wire_numbers():
    """Proto-JSON round-trips numbers as floats and a foreign peer may send strings —
    the accumulator adds these straight into ints, so coerce rather than trust."""
    result = {
        "artifacts": [
            _artifact(
                metadata={
                    pa.COST_EXT_URI: {
                        "usage": {"input_tokens": 240.0, "output_tokens": "70", "cache_read_input_tokens": None},
                        "costUsd": "0.006",
                    }
                }
            )
        ]
    }
    row = _peer_usage_row(result, "p")
    assert row["input_tokens"] == 240 and isinstance(row["input_tokens"], int)
    assert row["output_tokens"] == 70
    assert row["cache_read_input_tokens"] == 0
    assert row["cost_usd"] == pytest.approx(0.006)


def test_peer_usage_row_without_cost_usd_bills_tokens_and_no_cost():
    """cost-v1 omits ``costUsd`` when the peer couldn't price the turn. Bill the tokens
    it did report and leave cost at zero — an undercount we can see beats a number we
    invented from a pricing table for models we never saw."""
    result = {"artifacts": [_artifact(metadata=_cost_meta({"input_tokens": 30, "output_tokens": 4}))]}
    row = _peer_usage_row(result, "p")
    assert row["input_tokens"] == 30 and row["output_tokens"] == 4
    assert row["cost_usd"] == 0.0


def test_peer_usage_row_is_none_for_an_all_zero_payload():
    """A cost-v1 fragment carrying neither tokens nor cost says nothing; emitting a row
    for it would add an empty llm_call to the turn."""
    assert _peer_usage_row({"artifacts": [_artifact(metadata=_cost_meta({"input_tokens": 0}))]}, "p") is None


# ── the adapter emits it on the calling turn's stream ─────────────────────────


class _PeerClient:
    """A fake httpx client standing in for an A2A peer: ``responses`` are returned to
    successive JSON-RPC POSTs (last one repeats). No ``get`` — so the adapter's
    agent-card pre-flight degrades to "unknown version" and dispatch proceeds, exactly
    as it does against an offline card."""

    def __init__(self, *responses, **kw):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        payload = self._responses[0] if len(self._responses) == 1 else self._responses.pop(0)
        return _Resp(payload)


class _Resp:
    """A peer's HTTP reply. A ``str`` payload is a RAW JSON body and is decoded by the
    real ``json.loads``, because for #3038 the spelling is the whole point: a 400-digit
    integer LITERAL decodes to a Python ``int`` that ``float()`` refuses outright, where
    the string ``"1e400"`` merely overflows to ``inf`` — two different failures a
    pre-decoded dict cannot tell apart. Dict payloads stay as they were."""

    def __init__(self, payload):
        self._p = payload
        self.status_code = 200
        self.text = payload if isinstance(payload, str) else str(payload)

    def json(self):
        return json.loads(self._p) if isinstance(self._p, str) else self._p


def _delegate_to(peer_url="https://peer/a2a", name="orbis"):
    from plugins.delegates import _build_delegate_to

    return _build_delegate_to(DelegateRegistry([{"name": name, "type": "a2a", "url": peer_url}]))


async def _dispatch_capturing_usage(monkeypatch, *responses, target="orbis", **tool_args):
    """Run ``delegate_to`` under ``astream_events`` — the same consumer
    ``server/chat.py::_run_turn_stream`` is — and return (reply, usage payloads)."""
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _PeerClient(*responses))
    tool = _delegate_to(name=target)
    usage: list[dict] = []
    reply = ""
    args = {"target": target, "query": "do the thing", **tool_args}
    async for ev in tool.astream_events(args, version="v2"):
        if ev["event"] == "on_custom_event" and ev["name"] == "usage":
            usage.append(ev["data"])
        elif ev["event"] == "on_tool_end":
            reply = ev["data"]["output"]
    return reply, usage


_PEER_COST = _cost_meta(
    {"input_tokens": 700, "output_tokens": 55, "cache_read_input_tokens": 10},
    cost_usd=0.0123,
)


async def test_a2a_dispatch_emits_the_peers_cost_on_the_turn_stream(monkeypatch):
    """The whole point (#3016): a delegation to a protoAgent peer surfaces the peer's
    reported spend as exactly one tagged ``usage`` event — the lane #2872 built — while
    the tool still returns the peer's text unchanged."""
    inline = {"result": {"task": {"id": "t1", "artifacts": [_artifact(metadata=_PEER_COST)]}}}
    reply, usage = await _dispatch_capturing_usage(monkeypatch, inline)

    assert str(reply) == "hi from peer"
    assert len(usage) == 1, f"expected exactly one peer usage event, got {usage}"
    row = usage[0]
    assert row["input_tokens"] == 700 and row["output_tokens"] == 55
    assert row["cache_read_input_tokens"] == 10
    assert row["cost_usd"] == pytest.approx(0.0123)
    assert row["peer"] == "orbis" and row["model"] == "peer:orbis"


async def test_a2a_dispatch_without_cost_v1_emits_nothing(monkeypatch):
    """Silent degradation: a peer that reports no cost-v1 (any non-protoAgent A2A
    agent) is billed nothing and answers exactly as before."""
    plain = {"result": {"task": {"id": "t1", "artifacts": [_artifact()]}}}
    reply, usage = await _dispatch_capturing_usage(monkeypatch, plain)
    assert str(reply) == "hi from peer"
    assert usage == []


async def test_polled_peer_delegation_also_bills(monkeypatch):
    """An async-style peer (SendMessage returns a WORKING task, the answer arrives on a
    GetTask poll) reaches a different return path in the adapter — it must bill too."""
    working = {"result": {"task": {"id": "t1", "status": {"state": "TASK_STATE_WORKING"}, "artifacts": []}}}
    done = {"result": {"id": "t1", "status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [_artifact(_PEER_COST)]}}
    reply, usage = await _dispatch_capturing_usage(monkeypatch, working, done)
    assert str(reply) == "hi from peer"
    assert len(usage) == 1 and usage[0]["cost_usd"] == pytest.approx(0.0123)


async def test_dispatch_outside_a_run_context_still_returns_the_reply(monkeypatch):
    """A dispatch with no LangChain run context around it at all — the CLI runner, a
    plugin's background surface calling ``HOST.invoke_delegate``, this very test — has
    nowhere to dispatch the event. Telemetry must never break a delegation: the reply
    still comes back. (NOT the background-delegation case, which inherits a context and
    is covered below.)"""
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _PeerClient({"result": {"task": {"id": "t1", "artifacts": [_artifact(metadata=_PEER_COST)]}}}),
    )
    d = ADAPTERS["a2a"].parse({"name": "orbis", "type": "a2a", "url": "https://peer/a2a"})
    assert await ADAPTERS["a2a"].dispatch(d, "q") == "hi from peer"


# ── a detached background delegation bills nothing ────────────────────────────


class _DetachingBgManager:
    """A ``BackgroundManager`` stand-in that detaches the work the way the real one does
    — ``asyncio.create_task`` (``background/manager.py::spawn_work``). The mechanism is
    the point of the test below: ``create_task`` COPIES the spawning context, so the job
    inherits the ``delegate_to`` tool body's LangChain run context."""

    def __init__(self):
        self.task: asyncio.Task | None = None

    async def spawn_work(self, *, work, **kwargs):
        self.task = asyncio.create_task(work())
        return "job-abc123"


async def test_a_detached_background_delegation_does_not_bill_the_spawning_turn(monkeypatch):
    """``background=True`` (ADR 0050) must bill NOTHING — deterministically (#3016).

    The job is detached, but ``asyncio.create_task`` hands it a copy of the tool body's
    context, so it CAN still reach the spawning turn's event stream: left ungated, a peer
    that answered while the turn was still streaming would bill that turn and a peer that
    answered a second later would be dropped — the same delegation writing two different
    rows depending on the peer's latency. Detached work belongs to the later turn its
    reply is drained into, not to the turn that spawned it, so it is excluded outright.

    The turn below is shaped exactly like the case that would otherwise bill: the lead
    keeps working (and the stream stays open) until after the peer has answered.
    """
    from runtime.state import STATE

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _PeerClient({"result": {"task": {"id": "t1", "artifacts": [_artifact(metadata=_PEER_COST)]}}}),
    )
    mgr = _DetachingBgManager()
    monkeypatch.setattr(STATE, "background_mgr", mgr, raising=False)
    tool = _delegate_to()

    handle = ""

    async def turn(_):
        nonlocal handle
        handle = await tool.ainvoke({"target": "orbis", "query": "do the thing", "background": True})
        await mgr.task  # the lead thread works on while the peer answers
        # Proof the billing window was open the whole time: this event, dispatched
        # AFTER the peer answered, still reaches the stream below — so an unsuppressed
        # peer row would have reached it too.
        await adispatch_custom_event("still_streaming", {})
        return handle

    usage: list[dict] = []
    names: list[str] = []
    async for ev in RunnableLambda(turn).astream_events({}, version="v2"):
        if ev["event"] == "on_custom_event":
            names.append(ev["name"])
            if ev["name"] == "usage":
                usage.append(ev["data"])

    assert "job-abc123" in handle  # it really did go down the background path
    assert await mgr.task == "hi from peer"  # …and the peer really did answer, with cost-v1 on it
    assert "still_streaming" in names, "the turn's stream closed early — the test proves nothing"
    assert usage == [], f"a detached delegation must not bill the spawning turn, got {usage}"


# ── the two return paths that deliberately don't bill ─────────────────────────


async def test_a_parked_delegation_bills_nothing(monkeypatch):
    """The HITL park branch returns a resume handle, not an answer, and bills nothing.

    A real park carries no cost-v1 at all (it emits a status message, no terminal
    artifact), so this fixture deliberately puts one where the reader WOULD find it —
    the status-message fallback. Nothing is billed anyway: what decides is the branch,
    not the payload. The peer keeps its own row for that leg (#2943), which is the
    HITL-chain undercount ADR 0006 documents.
    """
    parked = {
        "result": {
            "task": {
                "id": "t1",
                "status": {
                    "state": "TASK_STATE_INPUT_REQUIRED",
                    "message": {"parts": [{"text": "which repo?"}], "metadata": _PEER_COST},
                },
            }
        }
    }
    reply, usage = await _dispatch_capturing_usage(monkeypatch, parked)
    assert str(reply).startswith("⏸") and "t1" in str(reply)
    assert usage == []


async def test_resuming_an_already_finished_task_bills_nothing(monkeypatch):
    """Resuming a task that had already finished reports work an EARLIER dispatch
    caused; billing its cost-v1 here would count the peer's spend into a second turn."""
    finished = {
        "result": {
            "task": {
                "id": "t-old",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [_artifact(metadata=_PEER_COST)],
            }
        }
    }
    reply, usage = await _dispatch_capturing_usage(monkeypatch, finished, resume_task_id="t-old")
    assert "already finished" in str(reply)
    assert usage == []


# ── telemetry never breaks a delegation ───────────────────────────────────────


async def test_a_non_finite_wire_number_is_billed_as_zero(monkeypatch):
    """A peer that puts a non-finite number on the wire must not cost us the answer.

    JSON parses ``Infinity`` — and ``1e400``, which overflows to it — so these values
    arrive from ``json.loads`` intact, and ``int(inf)`` raises ``OverflowError``, which
    is not the ``ValueError`` a coercion guard expects. The delegation still returns the
    peer's answer, the fields that WERE readable are still billed, and every number that
    reaches the accumulator is finite (an infinite ``cost_usd`` would poison the turn's
    sums all the way into the stored row).
    """
    poisoned = {
        "result": {
            "task": {
                "id": "t1",
                "artifacts": [
                    _artifact(
                        metadata={
                            pa.COST_EXT_URI: {
                                "usage": {
                                    "input_tokens": 700,
                                    "output_tokens": float("inf"),
                                    "cache_read_input_tokens": float("nan"),
                                },
                                "costUsd": "1e400",
                            }
                        }
                    )
                ],
            }
        }
    }
    reply, usage = await _dispatch_capturing_usage(monkeypatch, poisoned)

    assert str(reply) == "hi from peer"
    (row,) = usage
    assert row["input_tokens"] == 700
    assert row["output_tokens"] == 0 and row["cache_read_input_tokens"] == 0
    assert row["cost_usd"] == 0.0
    assert all(math.isfinite(v) for v in row.values() if isinstance(v, (int, float)))


async def test_unreadable_peer_telemetry_never_fails_the_delegation(monkeypatch):
    """The #2872 invariant, applied to the half that reads the wire: a payload we cannot
    turn into a row must not turn a delegation that SUCCEEDED into one that failed.

    Left outside the guard, a raise here propagates through ``registry.dispatch``, which
    records the delegate as failing before re-raising — so a bad number would discard an
    answer already in hand, tell the model the delegate broke, and put a red mark on a
    healthy peer in the Delegates panel.
    """
    from plugins.delegates import adapters, status

    def _boom(result, delegate):
        raise ValueError("malformed cost-v1")

    monkeypatch.setattr(adapters, "_peer_usage_row", _boom)
    status.reset()
    inline = {"result": {"task": {"id": "t1", "artifacts": [_artifact(metadata=_PEER_COST)]}}}
    reply, usage = await _dispatch_capturing_usage(monkeypatch, inline)

    assert str(reply) == "hi from peer", "the peer's answer was discarded over a telemetry failure"
    assert usage == []
    assert status.snapshot()["orbis"]["ok"] is True, "a telemetry failure marked a healthy delegate as failing"


# ── the durable outcome: the calling turn's accumulated telemetry ─────────────


_LEAD_USAGE = {
    "input_tokens": 30,
    "output_tokens": 12,
    "cache_read_input_tokens": 5,
    "cache_creation_input_tokens": 2,
    "cost_usd": 0.002,
    "model": "claude-lead",
}
_PEER_ROW = {
    "input_tokens": 700,
    "output_tokens": 55,
    "cache_read_input_tokens": 10,
    "cache_creation_input_tokens": 0,
    "cost_usd": 0.0123,
    "model": "peer:orbis",
    "peer": "orbis",
}


async def test_peer_usage_lands_in_the_calling_turns_outcome():
    """The accumulated outcome — what becomes the durable telemetry row — carries the
    peer's tokens and cost, and names the peer in `models`."""

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("usage", dict(_LEAD_USAGE))
        yield ("usage", dict(_PEER_ROW))
        yield ("done", "answer")

    o = await _run_turn_outcome(stream)
    assert o.state == "completed"
    assert o.usage["input_tokens"] == 730 and o.usage["output_tokens"] == 67
    assert o.usage["cache_read_input_tokens"] == 15
    assert o.cost_usd == pytest.approx(0.0143)
    assert o.models == ["claude-lead", "peer:orbis"]
    # …but the PEER's prompt size is a different agent's context window — it must not
    # move this thread's context-fill reading (the #2872 rule, applied to a peer).
    assert o.context_tokens == 30


async def test_a_peerless_turn_is_unchanged():
    """The degradation path at the accumulator: no peer row, no difference."""

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("usage", dict(_LEAD_USAGE))
        yield ("done", "answer")

    o = await _run_turn_outcome(stream)
    assert o.usage["input_tokens"] == 30 and o.cost_usd == pytest.approx(0.002)
    assert o.models == ["claude-lead"] and o.context_tokens == 30


async def test_peer_spend_rolls_up_into_the_cost_v1_we_emit_but_not_the_context_readout():
    """The chain case (ADR 0055): what a hub bills, its OWN caller can read.

    This instance emits cost-v1 on its terminal artifact from the same accumulator the
    peer row lands in — so a member's spend rides one hop further up and a hub→member→
    member fan-out is countable at the top. The context-v1 readout on the SAME artifact
    must not move, though: that one describes THIS thread's window, and the peer's
    700-token prompt is a different agent's entirely. Both read off the wire, not the
    in-process outcome — the split only counts if it survives serialization.
    """

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("usage", dict(_LEAD_USAGE))
        yield ("usage", dict(_PEER_ROW))
        yield ("done", "answer")

    app = _build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await _send_msg(c)).json()["result"]["task"]
        final = await _poll_terminal(c, task["id"])

    artifact = final["artifacts"][-1]
    payload = pa.parse_cost(artifact["metadata"])
    assert payload, "no cost-v1 on the terminal artifact"
    assert payload["usage"]["input_tokens"] == 730
    assert payload["usage"]["output_tokens"] == 67
    assert payload["costUsd"] == pytest.approx(0.0143)

    # …and the context readout is the LEAD's alone: peak prompt 30, and a next-turn
    # projection of 30+12 anchored to the lead's last call — not 700 / 755.
    _mime, ctx = pa.read_data(artifact["parts"][1])
    assert ctx["contextTokens"] == 30
    assert ctx["projectedTokens"] == 42


async def test_peer_spend_lands_in_the_stored_telemetry_row(tmp_path, monkeypatch):
    """The durable row — the thing anyone actually queries later (ADR 0006 Slice 2).

    A frame on a stream proves nothing if the row it should reach is unchanged, so this
    drives the real terminal chokepoint (``server.a2a._record_a2a_telemetry`` →
    ``record_turn`` → the SQLite store) with a real turn's outcome and reads the row back.
    """
    from observability.telemetry_store import TelemetryStore
    from runtime.state import STATE
    from server.a2a import _record_a2a_telemetry

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("usage", dict(_LEAD_USAGE))
        yield ("usage", dict(_PEER_ROW))
        yield ("done", "answer")

    monkeypatch.setattr(STATE, "telemetry_store", TelemetryStore(str(tmp_path / "turns.db")), raising=False)
    _record_a2a_telemetry(await _run_turn_outcome(stream))

    (row,) = STATE.telemetry_store.recent(limit=5)
    # The peer's spend is IN the row: its cost summed, its tokens summed, and the
    # `peer:` marker in `models` — the only durable trace a delegation leaves.
    assert row["cost_usd"] == pytest.approx(0.0143)
    assert row["models"] == "claude-lead,peer:orbis"
    # …and the prompt split stays disjoint with a peer's tokens in it (#3003): the
    # peer accumulated `input_tokens` cache-INCLUSIVE, the same convention this side
    # subtracts back out, so uncached + cache_read + cache_creation is the true total.
    assert row["cache_read_input_tokens"] == 15 and row["cache_creation_input_tokens"] == 2
    assert row["input_tokens"] == 730 - 15 - 2
    assert row["total_tokens"] == 730 + 67
    # The `model` column must still name a MODEL. A marker names an agent, and a
    # per-model breakdown listing "peer:orbis" would be wrong (#3016).
    assert row["model"] == "claude-lead"


async def test_a_marker_never_becomes_the_rows_primary_model(tmp_path, monkeypatch):
    """The edge the column guard exists for: a lead whose provider reported no usage at
    all yields no frame of its own, leaving the peer marker first in `models`. The row
    falls back to the configured model rather than claiming a delegate name is one — and
    so does the live `turn.usage` bus event the console HUD reads off the same list."""
    import server
    from observability.telemetry_store import TelemetryStore
    from runtime.state import STATE
    from server.turn_telemetry import record_turn

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(server._event_bus, "publish", lambda ev, data: published.append((ev, data)))
    monkeypatch.setattr(STATE, "telemetry_store", TelemetryStore(str(tmp_path / "turns.db")), raising=False)
    record_turn(
        task_id="t-marker",
        session_id="c1",
        state="completed",
        models=["peer:orbis"],
        usage={"input_tokens": 700, "output_tokens": 55},
        cost_usd=0.0123,
    )

    (row,) = STATE.telemetry_store.recent(limit=5)
    assert row["models"] == "peer:orbis"  # still legible as peer spend
    assert not row["model"].startswith("peer:")
    (usage_ev,) = [data for ev, data in published if ev == "turn.usage"]
    assert not usage_ev["model"].startswith("peer:")


async def test_a_marker_never_becomes_the_exported_trace_rows_model(tmp_path, monkeypatch):
    """The third consumer that picks a primary model off the same list: the fleet trace
    export (#1897), whose ``meta.model`` the lab consumes as the row's TEACHER model. A
    delegate name landing there would put an agent in the training data's model field.
    ``meta.models`` keeps the marker — that is where the row records a peer's share."""
    from observability import trace_export
    from tests.test_trace_export import _MESSAGES, _Cfg, _Checkpointer, _Outcome, _read_rows

    trace_export._reset_for_test()
    monkeypatch.setenv("PROTOAGENT_FLEET_TRACE_EXPORT", str(tmp_path))
    trace_export.init()
    assert trace_export.is_enabled()
    try:
        trace_export.export_turn(
            _Outcome(models=["peer:orbis"]), checkpointer=_Checkpointer(_MESSAGES), graph_config=_Cfg()
        )
        (row,) = _read_rows(tmp_path)
    finally:
        trace_export._reset_for_test()

    assert row["meta"]["models"] == ["peer:orbis"]
    assert not row["meta"]["model"].startswith("peer:")


# ── a peer cannot erase the calling turn's telemetry row (#3038) ──────────────


#: A peer's cost-v1 with numbers no real turn produces: ``1e308`` is a 309-digit Python
#: int once ``int()``ed, which is what ``store.record`` rejects with ``OverflowError:
#: Python int too large to convert to SQLite INTEGER``. Both wire spellings are here
#: (proto-JSON float and string) plus a negative, since the executor's accumulator adds
#: whichever survives straight into the turn's sums.
_HOSTILE_COST = {
    pa.COST_EXT_URI: {
        "usage": {
            "input_tokens": 1e308,
            "output_tokens": "1e308",
            "cache_read_input_tokens": -5,
            "cache_creation_input_tokens": 2**70,
        },
        "costUsd": 1e300,
    }
}

#: What the LEAD agent actually spent on the turn that made the delegation — the thing
#: the bug destroys. Realistic, not token-sized: a turn with a delegation in it has a
#: prompt worth of context behind it.
_LEAD_SPEND = {
    "input_tokens": 5000,
    "output_tokens": 900,
    "cache_read_input_tokens": 1200,
    "cache_creation_input_tokens": 300,
    "cost_usd": 0.0475,
    "model": "claude-sonnet-4-6",
}


def _seed_a_month_of_history(store) -> int:
    """A store that is NOT a clean room: a month of the turns a live instance actually
    accumulates — operator turns, coder rows an order of magnitude bigger, both legs of
    a HITL park sharing one ``task_id`` (#3001), earlier honest peer delegations, and
    turns that overlapped.

    The bug is a dropped INSERT, and an empty store cannot tell "the row was written"
    from "the store was already empty" — nor can it show that the loss is confined to
    the one turn rather than taking the history around it too.
    """
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)
    fixtures = [
        ("claude-sonnet-4-6", "claude-sonnet-4-6", 4200, 610, 0.031),
        ("claude-code", "claude-code", 88000, 3100, 0.44),  # a coder row
        ("claude-sonnet-4-6", "claude-sonnet-4-6,peer:orbis", 9100, 720, 0.079),  # an honest peer
        ("gpt-5.2", "gpt-5.2", 2600, 340, 0.012),
    ]
    n = 0
    for day in range(30):
        model, models, inp, out, cost = fixtures[day % len(fixtures)]
        ended = base + timedelta(days=day)
        # Two legs of a HITL park share a task_id; the pair also overlaps in time, which
        # is what concurrent turns on one instance look like in this table.
        for leg in range(2 if day % 7 == 3 else 1):
            n += 1
            store.record(
                {
                    "task_id": f"hist-{day}",
                    "session_id": f"c{day % 5}",
                    "state": "completed",
                    "success": 1,
                    "model": model,
                    "models": models,
                    "input_tokens": inp + leg * 400,
                    "output_tokens": out,
                    "total_tokens": inp + out + leg * 400,
                    "cache_read_input_tokens": inp // 3,
                    "cache_creation_input_tokens": 120,
                    "cost_usd": cost,
                    "duration_ms": 14000 + day * 37,
                    "llm_calls": 3,
                    "tool_calls": 5,
                    "created_at": (ended - timedelta(seconds=14)).isoformat(),
                    "ended_at": (ended + timedelta(seconds=leg)).isoformat(),
                    "soul_rev": "abc1234",
                    "trace_id": f"tr-{day}",
                    "tool_durations": None,
                    "context_tokens": inp,
                }
            )
    return n


async def test_a_hostile_peers_unbounded_number_cannot_erase_the_calling_turns_row(tmp_path, monkeypatch):
    """#3038 — the durable outcome: one hostile delegation must not cost the lead agent
    its own turn.

    A peer replying with ``{"usage": {"input_tokens": 1e308}}`` used to ride the wire
    into the executor's accumulator as a 309-digit int, through ``record_turn``'s
    ``total_tokens``, and into ``store.record``, where SQLite refuses it. ``record_turn``
    swallows the ``OverflowError`` because telemetry is best-effort — correct in
    isolation, and it turned a peer-controlled input into total loss of THAT turn's
    accounting: the lead's own spend gone from cost totals, success rate and latency
    percentiles at a remote party's choosing. ``record_turn`` did log the failure at
    ERROR with a traceback, so the drop was reported; what nothing said was which remote
    party caused it, and no log line brings the row back.

    So this drives the real path end to end — a real ``delegate_to`` dispatch against a
    peer that answers with the poisoned numbers, the resulting usage frame riding the
    same turn as the lead's genuine spend, through the real
    ``server.a2a._record_a2a_telemetry`` chokepoint into a real ``TelemetryStore`` that
    already holds a month of history — and reads the row back out.
    """
    from observability.telemetry_store import TelemetryStore
    from runtime.state import STATE
    from server.a2a import _record_a2a_telemetry

    # 1. The peer's numbers arrive the way they really do: off a live delegation.
    poisoned = {"result": {"task": {"id": "t1", "artifacts": [_artifact(metadata=_HOSTILE_COST)]}}}
    reply, usage = await _dispatch_capturing_usage(monkeypatch, poisoned)
    assert str(reply) == "hi from peer", "the delegation itself must still succeed"
    (peer_row,) = usage
    # The peer double swaps ``httpx.AsyncClient`` globally; the turn below rides an
    # ASGITransport client through the same class, so put the real one back first.
    monkeypatch.undo()

    store = TelemetryStore(str(tmp_path / "turns.db"))
    seeded = _seed_a_month_of_history(store)
    monkeypatch.setattr(STATE, "telemetry_store", store, raising=False)

    # 2. That peer frame rides the SAME turn as the lead agent's real spend.
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("usage", dict(_LEAD_SPEND))
        yield ("usage", dict(peer_row))
        yield ("done", "answer")

    outcome = await _run_turn_outcome(stream)
    _record_a2a_telemetry(outcome)

    # 3. The turn is in the store — the whole point.
    rows = store.recent(limit=200)
    assert len(rows) == seeded + 1, "the hostile peer erased the calling turn's row, the lead's own spend with it"
    (row,) = [r for r in rows if r["task_id"] == outcome.task_id]

    # The lead's genuine numbers are IN the surviving row, not merely a row of zeros.
    assert row["cost_usd"] >= _LEAD_SPEND["cost_usd"]
    assert row["output_tokens"] >= _LEAD_SPEND["output_tokens"]
    assert row["cache_creation_input_tokens"] >= _LEAD_SPEND["cache_creation_input_tokens"]
    assert row["model"] == "claude-sonnet-4-6"
    assert "peer:orbis" in row["models"], "the peer's share stays legible — it is bounded, not erased"

    # Every integer column is storable — SQLite INTEGER is signed 64-bit, and that wall
    # is what the peer was driving the row through — and none went negative on a peer's
    # say-so (a token credit is not a thing a peer gets to issue).
    for col in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "context_tokens",
    ):
        assert 0 <= row[col] < 2**63, f"{col} = {row[col]} is not a storable, non-negative count"
    assert 0.0 <= row["cost_usd"] < 1e12

    # …and the month of history around it is untouched: the aggregate a human reads
    # still sums this turn in with the rest.
    assert store.summary()["turns"] == seeded + 1


async def test_an_out_of_range_peer_value_is_clamped_floored_and_reported(monkeypatch, caplog):
    """The bound itself, at the parse boundary: absurd magnitudes clamp to the ceiling,
    negatives floor at zero, and the first clamp says so out loud.

    The warning is budgeted per PEER (a peer that reports garbage reports it on every
    reply), which means a test asserting it has to clear the budget — otherwise it
    passes or fails on test ORDER, the exact clean-room failure this class of bug keeps
    repeating.
    """
    import logging

    from plugins.delegates import adapters
    from plugins.delegates.adapters import _MAX_WIRE_COST_USD, _MAX_WIRE_TOKENS

    monkeypatch.setattr(adapters, "_clamp_warned", set())
    caplog.set_level(logging.WARNING, logger="protoagent.plugins.delegates")

    poisoned = {"result": {"task": {"id": "t1", "artifacts": [_artifact(metadata=_HOSTILE_COST)]}}}
    _reply, usage = await _dispatch_capturing_usage(monkeypatch, poisoned)
    (row,) = usage

    assert row["input_tokens"] == _MAX_WIRE_TOKENS and isinstance(row["input_tokens"], int)
    assert row["output_tokens"] == _MAX_WIRE_TOKENS, "a string-spelled 1e308 is the same attack"
    assert row["cache_creation_input_tokens"] == _MAX_WIRE_TOKENS
    assert row["cache_read_input_tokens"] == 0, "a negative peer count floors at zero"
    assert row["cost_usd"] == _MAX_WIRE_COST_USD

    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    # `record_turn` already logged the dropped row at ERROR; what it could not name is
    # the peer, because by then the number is one addend inside `total_tokens`.
    assert warned, "a peer sending absurd numbers must be ATTRIBUTABLE to that peer"
    assert "orbis" in warned[0].getMessage(), "the warning must name the peer that sent them"


#: The spelling the first cut of the bound missed (#3038). ``json.loads`` decodes a
#: 401-digit integer LITERAL — the natural way a peer writes an absurd token count — to
#: a Python ``int``, and ``float()`` REFUSES an int wider than a double instead of
#: overflowing to ``inf`` the way the string ``"1e400"`` does. The honest fields sit
#: beside it because the failure was total: the ``OverflowError`` came out of
#: ``_peer_usage_row``, so ``output_tokens`` and ``costUsd`` went down with it.
_WIDE_INT_COST = {
    pa.COST_EXT_URI: {
        "usage": {"input_tokens": int("1" + "0" * 400), "output_tokens": 120},
        "costUsd": 0.51,
    }
}


def _wire_body(cost_meta) -> str:
    """The peer's reply as a RAW JSON body — what actually crosses the wire, decoded on
    the way in by the real ``json.loads`` (see :class:`_Resp`)."""
    return json.dumps({"result": {"task": {"id": "t1", "artifacts": [_artifact(metadata=cost_meta)]}}})


async def test_a_wide_integer_literal_is_bounded_not_dropped(tmp_path, monkeypatch):
    """#3038 — the bound has to hold for the spelling a peer would actually use.

    ``1e308`` is a float literal; a peer writing an absurd count writes an INTEGER, and
    that integer reaches ``_wire_number`` as a Python ``int`` too wide for a double.
    ``float()`` raises ``OverflowError``, which is neither of the ``(TypeError,
    ValueError)`` the coercion guard caught — so the exception left ``_peer_usage_row``
    entirely, ``_bill_peer_usage``'s catch-all swallowed it at DEBUG, and the peer's
    WHOLE cost-v1 was erased rather than bounded: the honest ``output_tokens`` and
    ``costUsd`` beside it gone too, with nothing above DEBUG to say so.

    Driven the whole way: a raw JSON body through the real decoder, a real
    ``delegate_to`` dispatch, the usage frame riding the same turn as the lead agent's
    genuine spend, through ``server.a2a._record_a2a_telemetry`` into a real
    ``TelemetryStore`` that already holds a month of history — then the stored row read
    back out.
    """
    from observability.telemetry_store import TelemetryStore
    from plugins.delegates import adapters
    from plugins.delegates.adapters import _MAX_WIRE_TOKENS
    from runtime.state import STATE
    from server.a2a import _record_a2a_telemetry

    monkeypatch.setattr(adapters, "_clamp_warned", set())
    reply, usage = await _dispatch_capturing_usage(monkeypatch, _wire_body(_WIDE_INT_COST))
    assert str(reply) == "hi from peer", "the delegation itself must still succeed"
    assert len(usage) == 1, "the peer's cost-v1 was erased instead of bounded"
    (peer_row,) = usage

    # Bounded — and the honest fields that used to die with the raise are still there.
    assert peer_row["input_tokens"] == _MAX_WIRE_TOKENS
    assert peer_row["output_tokens"] == 120, "an honest field must survive its neighbour being absurd"
    assert peer_row["cost_usd"] == 0.51

    monkeypatch.undo()  # the peer double swapped httpx.AsyncClient globally

    store = TelemetryStore(str(tmp_path / "turns.db"))
    seeded = _seed_a_month_of_history(store)
    monkeypatch.setattr(STATE, "telemetry_store", store, raising=False)

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("usage", dict(_LEAD_SPEND))
        yield ("usage", dict(peer_row))
        yield ("done", "answer")

    outcome = await _run_turn_outcome(stream)
    _record_a2a_telemetry(outcome)

    rows = store.recent(limit=200)
    assert len(rows) == seeded + 1, "the calling turn's row is gone — the lead's own spend with it"
    (row,) = [r for r in rows if r["task_id"] == outcome.task_id]
    assert row["cost_usd"] >= _LEAD_SPEND["cost_usd"] + 0.51
    assert row["output_tokens"] >= _LEAD_SPEND["output_tokens"] + 120
    assert "peer:orbis" in row["models"], "the peer's share stays legible — bounded, not erased"
    for col in ("input_tokens", "output_tokens", "total_tokens", "cache_read_input_tokens", "context_tokens"):
        assert 0 <= row[col] < 2**63, f"{col} = {row[col]} is not a storable, non-negative count"


async def test_a_non_finite_peer_neither_warns_nor_spends_a_hostile_peers_warning(monkeypatch, caplog):
    """#3038 — the warning has to survive an instance that also has ORDINARY peers on it.

    Two things are being pinned, and both only fail once the process has seen more than
    the one hostile delegation a clean room contains:

    1. A non-finite number is #3016's settled contract — the peer lost track, so it
       bills nothing — and ``json`` really does carry it (``json.dumps(float("nan"))``
       emits a bare ``NaN`` that ``json.loads`` accepts). Reporting it as
       "out-of-range … clamped to sane bounds" is wrong on both counts and names an
       innocent peer for it.
    2. The budget that stops a garbage-reporting peer flooding the log must not be
       spendable BY that ordinary condition, nor by a payload that contributes nothing
       at all — otherwise the one warning an operator ever sees is gone before the
       hostile peer arrives, and the clamp this fix exists for is DEBUG-only on an
       instance where DEBUG is off.
    """
    import logging

    from plugins.delegates import adapters

    monkeypatch.setattr(adapters, "_clamp_warned", set())
    caplog.set_level(logging.DEBUG, logger="protoagent.plugins.delegates")

    def _warnings():
        return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]

    # 1. An ordinary peer whose pricing went NaN. Billed as zero, and NOT called clamped.
    nan_cost = {pa.COST_EXT_URI: {"usage": {"input_tokens": 700, "output_tokens": 55}, "costUsd": float("nan")}}
    _reply, usage = await _dispatch_capturing_usage(monkeypatch, _wire_body(nan_cost), target="peerA")
    (benign,) = usage
    assert benign["cost_usd"] == 0.0 and benign["input_tokens"] == 700
    assert _warnings() == [], "a non-finite value is #3016's contract, not a peer to warn about"

    # 2. A payload that contributes nothing is dropped, as it always was — it must not
    #    spend the warning either.
    empty = {pa.COST_EXT_URI: {"usage": {"input_tokens": -3, "output_tokens": -1}, "costUsd": -2.0}}
    _reply, usage = await _dispatch_capturing_usage(monkeypatch, _wire_body(empty), target="orbis")
    assert usage == [], "an all-negative payload floors to nothing and is not billed"
    assert _warnings() == [], "a row no consumer will ever see must not spend the budget"

    # 3. NOW the hostile peer. This is the one an operator has to see.
    _reply, usage = await _dispatch_capturing_usage(monkeypatch, _wire_body(_HOSTILE_COST), target="orbis")
    assert usage, "the hostile payload is bounded, so it still bills"
    warned = _warnings()
    assert len(warned) == 1, "the hostile clamp must reach WARNING — DEBUG is off in production"
    assert "orbis" in warned[0] and "input_tokens" in warned[0]
    assert "peerA" not in warned[0]

    # 4. …and only once for that peer: it reports garbage on every reply.
    caplog.clear()
    await _dispatch_capturing_usage(monkeypatch, _wire_body(_HOSTILE_COST), target="orbis")
    assert _warnings() == [], "a peer must not set the volume of the log it appears in"
    assert [r for r in caplog.records if r.levelno == logging.DEBUG and "clamped" in r.getMessage()]
