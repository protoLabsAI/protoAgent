"""Work in flight is visible while it happens, not only after it lands.

The ledger records an edge when a dispatch ENDS; the org chart's live view needs it while it
runs. So the same funnels open the edge first (``graph/ledger.py``), announce it on the bus
(``delegation.started`` → ``.progress`` → ``.finished``, all live-only), and keep it in
``live_edges()`` / ``GET /api/ledger/live`` for a view that opens mid-run.
"""

from __future__ import annotations

import asyncio

import pytest

from graph import delegate_progress as dp
from graph import ledger

pytestmark = pytest.mark.asyncio


@pytest.fixture
def bus(monkeypatch):
    from graph.plugins.host import HOST

    sent: list[tuple[str, dict, dict]] = []
    monkeypatch.setattr(HOST, "publish", lambda topic, data, **kw: sent.append((topic, data, kw)))
    monkeypatch.setattr(ledger, "_LIVE", {})
    monkeypatch.setattr(ledger, "_LIVE_BY_TASK", {})
    return sent


def _topics(sent):
    return [t for t, _, _ in sent]


async def test_a_dispatch_is_live_while_it_runs_and_announced_both_ends(bus):
    with ledger.dispatch(to_kind="acp", to_name="claude-code", what="add clamp", origin="delegate_to") as edge:
        live = ledger.live_edges()
        assert [e["to_name"] for e in live] == ["claude-code"]
        assert live[0]["id"] == edge.id and live[0]["what"] == "add clamp"
        edge.cost_usd = 0.12
    assert ledger.live_edges() == []
    assert _topics(bus) == ["delegation.started", "delegation.finished"]
    finished = bus[-1][1]
    assert finished["id"] == edge.id and finished["outcome"] == "ok" and finished["cost_usd"] == 0.12
    # Live-only: a replayed "started" for work that ended long ago is worse than none.
    assert all(kw.get("retain") is False for _, _, kw in bus)


async def test_a_nested_dispatch_names_its_parent(bus):
    with ledger.dispatch(to_kind="subagent", to_name="engineer") as outer:
        with ledger.dispatch(to_kind="acp", to_name="claude-code") as inner:
            pass
    started = {d["to_name"]: d for t, d, _ in bus if t == "delegation.started"}
    assert started["engineer"]["parent_id"] == ""
    assert started["claude-code"]["parent_id"] == outer.id
    assert inner.id != outer.id


async def test_failure_and_cancellation_close_the_edge_with_their_outcome(bus):
    with pytest.raises(RuntimeError):
        with ledger.dispatch(to_kind="a2a", to_name="peer"):
            raise RuntimeError("peer down")
    with pytest.raises(asyncio.CancelledError):
        with ledger.dispatch(to_kind="a2a", to_name="peer"):
            raise asyncio.CancelledError()
    outcomes = [d["outcome"] for t, d, _ in bus if t == "delegation.finished"]
    assert outcomes == ["failed", "cancelled"]
    assert ledger.live_edges() == []


async def test_a_background_job_stays_live_until_it_settles(bus):
    edge_id = ledger.open_edge(to_kind="subagent", to_name="researcher", task_id="bg-1", origin="background")
    assert [e["id"] for e in ledger.live_edges()] == [edge_id]
    ledger.settle_delegation("bg-1", outcome="failed", duration_ms=900, error="boom")
    assert ledger.live_edges() == []
    finished = bus[-1][1]
    assert bus[-1][0] == "delegation.finished" and finished["outcome"] == "failed" and finished["duration_ms"] == 900


async def test_a_coder_reports_progress_onto_its_open_edge_even_with_no_card(bus):
    with ledger.dispatch(to_kind="acp", to_name="claude-code") as edge:
        sink = dp.current_sink()
        assert sink is not None  # no card bound, but the edge wants the live work
        t = dp.DelegateProgress("claude-code", sink, min_interval=0)
        await t.on_tool({"phase": "start", "id": "1", "name": "Read calc.py", "kind": "read"})
        await t.on_text("Reading.")
        await t.finish(ok=True)
        live = ledger.live_edges()[0]
        assert live["progress"]["current_tool"]["name"] == "Read calc.py"
        assert "timeline" not in live["progress"]
    progress = [d for t, d, _ in bus if t == "delegation.progress"]
    assert progress and progress[-1]["id"] == edge.id and progress[-1]["progress"]["done"] is True
    # Outside any edge and with no card, nothing is tracked — as before.
    assert dp.current_sink() is None


async def test_free_text_is_redacted_and_clipped(bus):
    token = "ghp_" + "abcdefghij" * 4  # assembled at runtime so the secret scanner stays quiet
    ledger.open_edge(to_kind="acp", to_name="c", what=f"use {token} " + "x" * 600)
    started = bus[-1][1]
    assert "ghp_abc" not in started["what"] and len(started["what"]) <= ledger._WHAT_MAX


async def test_the_live_registry_is_bounded(bus, monkeypatch):
    monkeypatch.setattr(ledger, "_LIVE_MAX", 3)
    ids = [ledger.open_edge(to_kind="acp", to_name=f"c{i}") for i in range(5)]
    assert [e["id"] for e in ledger.live_edges()] == ids[-3:]


async def test_the_live_endpoint_returns_what_is_in_flight(bus):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from operator_api.telemetry_routes import register_telemetry_routes

    app = FastAPI()
    register_telemetry_routes(app)
    edge_id = ledger.open_edge(to_kind="acp", to_name="claude-code", what="go")
    body = TestClient(app).get("/api/ledger/live").json()
    assert [e["id"] for e in body["edges"]] == [edge_id] and body["now"] > 0


async def test_a_wrapped_ask_is_recorded_as_what_was_asked(bus, tmp_path, monkeypatch):
    import runtime.state as rs
    from observability.ledger_store import LedgerStore

    store = LedgerStore(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(rs.STATE, "ledger_store", store, raising=False)
    with ledger.describing("add clamp"):
        with ledger.dispatch(to_kind="acp", to_name="claude-code", what="You are taking part in a group chat…\n\nadd clamp"):
            with ledger.dispatch(to_kind="subagent", to_name="inner", what="inner ask"):
                pass
    started = {d["to_name"]: d["what"] for t, d, _ in bus if t == "delegation.started"}
    assert started == {"claude-code": "add clamp", "inner": "inner ask"}
    assert {r["to_name"]: r["what"] for r in store.recent()} == {"claude-code": "add clamp", "inner": "inner ask"}


async def test_a_ledger_only_sink_is_not_a_card(bus):
    async def card(_s):
        pass

    assert dp.is_card_sink(card) and not dp.is_card_sink(None)
    with ledger.dispatch(to_kind="acp", to_name="c"):
        assert not dp.is_card_sink(dp.current_sink())


async def test_an_evicted_edge_is_announced_as_gone(bus, monkeypatch):
    monkeypatch.setattr(ledger, "_LIVE_MAX", 1)
    first = ledger.open_edge(to_kind="acp", to_name="old")
    ledger.open_edge(to_kind="acp", to_name="new")
    gone = [d for t, d, _ in bus if t == "delegation.finished"]
    assert [d["id"] for d in gone] == [first] and gone[0]["outcome"] == "cancelled"


async def test_a_failing_ledger_note_still_reaches_the_card(bus, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(ledger, "note_progress", boom)
    frames: list[dict] = []

    async def card(snapshot):
        frames.append(snapshot)

    with ledger.dispatch(to_kind="acp", to_name="claude-code"):
        t = dp.DelegateProgress("claude-code", card, min_interval=0)
        await t.on_text("Reading.")
        await t.finish(ok=True)
    assert frames and frames[-1]["done"] is True
