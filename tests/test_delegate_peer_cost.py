"""A2A peer cost-v1 → the calling turn's telemetry (#3016).

A protoAgent peer measures the turn it runs for us and ships the numbers back on the
terminal artifact's cost-v1 extension metadata. The delegates a2a adapter used to read
only ``_extract_text`` and drop them, so a hub that fanned work out to members reported
only its own thinking. These tests cover the whole path:

  - reading the payload off the EXACT shape our own executor emits (the fixture is a
    real turn through the real a2a-sdk app, not a hand-written envelope);
  - the adapter emitting one tagged ``usage`` custom event per delegation, on both the
    inline and the polled terminal path;
  - the durable outcomes — the peer's tokens and cost in the calling turn's accumulator,
    in the STORED telemetry row, and in the cost-v1 this instance itself puts back on
    the wire (so a hub→member→member chain rolls up) — while the peer's prompt size
    stays out of the lead thread's context fill;
  - silent degradation for a peer that emits no cost-v1.
"""

from __future__ import annotations

import httpx
import protolabs_a2a as pa
import pytest

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
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200
        self.text = str(payload)

    def json(self):
        return self._p


def _delegate_to(peer_url="https://peer/a2a", name="orbis"):
    from plugins.delegates import _build_delegate_to

    return _build_delegate_to(DelegateRegistry([{"name": name, "type": "a2a", "url": peer_url}]))


async def _dispatch_capturing_usage(monkeypatch, *responses, target="orbis"):
    """Run ``delegate_to`` under ``astream_events`` — the same consumer
    ``server/chat.py::_run_turn_stream`` is — and return (reply, usage payloads)."""
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _PeerClient(*responses))
    tool = _delegate_to(name=target)
    usage: list[dict] = []
    reply = ""
    async for ev in tool.astream_events({"target": target, "query": "do the thing"}, version="v2"):
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
    """A background delegation (ADR 0050) runs detached from the turn's callback
    context, so there is nowhere to dispatch the event. Telemetry must never break a
    delegation: the reply still comes back."""
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _PeerClient({"result": {"task": {"id": "t1", "artifacts": [_artifact(metadata=_PEER_COST)]}}}),
    )
    d = ADAPTERS["a2a"].parse({"name": "orbis", "type": "a2a", "url": "https://peer/a2a"})
    assert await ADAPTERS["a2a"].dispatch(d, "q") == "hi from peer"


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
    falls back to the configured model rather than claiming a delegate name is one."""
    from observability.telemetry_store import TelemetryStore
    from runtime.state import STATE
    from server.turn_telemetry import record_turn

    monkeypatch.setattr(STATE, "telemetry_store", TelemetryStore(str(tmp_path / "turns.db")), raising=False)
    record_turn(
        task_id="t-marker",
        session_id="c1",
        state="completed",
        models=["peer:orbis"],
        usage={"input_tokens": 700, "output_tokens": 55},
        cost_usd=0.0123,
        publish_usage_event=False,
    )

    (row,) = STATE.telemetry_store.recent(limit=5)
    assert row["models"] == "peer:orbis"  # still legible as peer spend
    assert not row["model"].startswith("peer:")
