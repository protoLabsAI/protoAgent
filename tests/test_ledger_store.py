"""Tests for the delegation ledger — the durable record of who handed work to whom.

Every assertion reads the DURABLE ROW, never the call that was supposed to write it. That
is the discipline the 2026-08 telemetry audit was built on: five defects survived because
a test asserted the dict the producer had just constructed, which stays green whether or
not anything downstream receives it. It proves construction, not delivery.
"""

from __future__ import annotations

import asyncio

import pytest

from observability.ledger_store import LedgerStore


@pytest.fixture
def store(tmp_path):
    return LedgerStore(str(tmp_path / "ledger.db"))


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point STATE's ledger holder at a throwaway store, as the host does at boot."""
    import runtime.state as rs

    store = LedgerStore(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(rs.STATE, "ledger_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    return store


def _edge(store, **over):
    base = dict(
        from_agent="gina",
        to_kind="acp",
        to_name="protoCoder",
        what="implement the thing",
        session_id="s1",
        task_id="t1",
        outcome="ok",
    )
    base.update(over)
    return store.record(**base)


# --- the store ------------------------------------------------------------------------


def test_an_edge_survives_as_a_readable_row(store):
    _edge(store, what="implement the thing")

    rows = store.recent()
    assert len(rows) == 1
    assert rows[0]["to_name"] == "protoCoder"
    assert rows[0]["what"] == "implement the thing"
    assert rows[0]["outcome"] == "ok"


def test_several_edges_to_one_target_are_all_kept(store):
    # The identity is a surrogate on purpose. Keying on task_id would silently overwrite
    # edges — a HITL park/resume shares one across legs, and a fan-out shares a parent.
    # That is the exact defect the telemetry store had to migrate away from (#3001).
    for _ in range(3):
        _edge(store, task_id="same-task")

    rows = store.recent()
    assert len(rows) == 3
    assert len({r["edge_id"] for r in rows}) == 3


def test_an_unknown_cost_is_stored_as_null_not_as_zero(store):
    # A confident zero makes an unmeasured coder look free and silently understates every
    # rollup built on the column. Unknown and free are different claims.
    _edge(store, to_name="unmeasured", cost_usd=None)
    _edge(store, to_name="measured", cost_usd=0.42)
    _edge(store, to_name="genuinely-free", cost_usd=0.0)

    by_name = {r["to_name"]: r["cost_usd"] for r in store.recent()}
    assert by_name["unmeasured"] is None
    assert by_name["measured"] == pytest.approx(0.42)
    assert by_name["genuinely-free"] == 0.0


def test_aggregate_separates_priced_rows_from_the_unmeasured_ones(store):
    # Without `priced`, a partial sum reads as a total and "cheap" is indistinguishable
    # from "mostly unmeasured".
    _edge(store, to_name="coder", cost_usd=1.0)
    _edge(store, to_name="coder", cost_usd=None)
    _edge(store, to_name="coder", cost_usd=None)

    edge = store.edges()[0]
    assert edge["dispatches"] == 3
    assert edge["priced"] == 1
    assert edge["cost_usd"] == pytest.approx(1.0)


def test_aggregate_counts_outcomes_separately(store):
    # A target that is reachable but failing every call must be distinguishable from an
    # idle one — the distinction a health dot alone cannot make.
    _edge(store, to_name="flaky", outcome="ok")
    _edge(store, to_name="flaky", outcome="failed", error="boom")
    _edge(store, to_name="flaky", outcome="cancelled")

    edge = store.edges()[0]
    assert (edge["ok"], edge["failed"], edge["cancelled"]) == (1, 1, 1)
    assert edge["dispatches"] == 3


def test_aggregate_groups_by_target_not_by_dispatch(store):
    _edge(store, to_kind="acp", to_name="protoCoder")
    _edge(store, to_kind="acp", to_name="protoCoder")
    _edge(store, to_kind="a2a", to_name="hermes")

    edges = {(e["to_kind"], e["to_name"]): e for e in store.edges()}
    assert edges[("acp", "protoCoder")]["dispatches"] == 2
    assert edges[("a2a", "hermes")]["dispatches"] == 1


def test_free_text_is_bounded_so_one_prompt_cannot_dominate_the_table(store):
    _edge(store, what="x" * 5000, error="y" * 5000, outcome="failed")

    row = store.recent()[0]
    assert len(row["what"]) <= 500
    assert len(row["error"]) <= 500


def test_recent_can_scope_to_one_session(store):
    _edge(store, session_id="s1")
    _edge(store, session_id="s2")

    assert len(store.recent(session_id="s1")) == 1
    assert len(store.recent()) == 2


def test_recent_returns_newest_first(store):
    _edge(store, what="first")
    _edge(store, what="second")

    assert [r["what"] for r in store.recent()] == ["second", "first"]


def test_prune_drops_only_the_old_edges(store):
    _edge(store, what="ancient", at="2020-01-01T00:00:00+00:00")
    _edge(store, what="fresh")

    assert store.prune(keep_days=30) == 1
    assert [r["what"] for r in store.recent()] == ["fresh"]


# --- the writer -----------------------------------------------------------------------


def test_the_writer_persists_through_state(wired):
    from graph import ledger

    ledger.record_delegation(to_kind="a2a", to_name="hermes", what="ask a peer")

    rows = wired.recent()
    assert len(rows) == 1
    assert rows[0]["to_name"] == "hermes"


def test_the_writer_is_a_no_op_when_no_store_is_wired(monkeypatch):
    # Host-free tooling and early boot have no store; recording must degrade to nothing
    # rather than raising into a dispatch.
    import runtime.state as rs

    from graph import ledger

    monkeypatch.setattr(rs.STATE, "ledger_store", None, raising=False)
    assert ledger.record_delegation(to_kind="a2a", to_name="hermes") is None


def test_a_ledger_failure_never_breaks_the_caller(wired, monkeypatch):
    # The work matters more than the record of it.
    from graph import ledger

    def _boom(**_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(wired, "record", _boom)
    assert ledger.record_delegation(to_kind="acp", to_name="protoCoder") is None


def test_the_context_manager_records_a_successful_dispatch(wired):
    from graph import ledger

    with ledger.dispatch(to_kind="acp", to_name="protoCoder", what="build it") as edge:
        edge.cost_usd = 1.25

    row = wired.recent()[0]
    assert row["outcome"] == "ok"
    assert row["cost_usd"] == pytest.approx(1.25)
    assert row["what"] == "build it"


def test_the_context_manager_records_a_failed_dispatch_and_reraises(wired):
    # A dispatch that raises still happened and still cost something. A ledger that only
    # records successes answers "what did this fleet do" with a survivor-biased yes.
    from graph import ledger

    with pytest.raises(RuntimeError):
        with ledger.dispatch(to_kind="acp", to_name="protoCoder"):
            raise RuntimeError("coder exploded")

    row = wired.recent()[0]
    assert row["outcome"] == "failed"
    assert "coder exploded" in row["error"]


def test_a_cancellation_is_not_recorded_as_a_failure(wired):
    # An operator stopping a turn says nothing about the delegate; collapsing the two puts
    # a red mark on a healthy coder every time someone hits stop.
    from graph import ledger

    with pytest.raises(asyncio.CancelledError):
        with ledger.dispatch(to_kind="acp", to_name="protoCoder"):
            raise asyncio.CancelledError()

    row = wired.recent()[0]
    assert row["outcome"] == "cancelled"
    assert row["error"] == ""


def test_the_context_manager_times_the_dispatch(wired):
    from graph import ledger

    with ledger.dispatch(to_kind="subagent", to_name="researcher"):
        pass

    assert wired.recent()[0]["duration_ms"] >= 0


def test_the_context_manager_takes_ids_learned_during_the_dispatch(wired):
    # An a2a task id only exists once the peer has accepted the work.
    from graph import ledger

    with ledger.dispatch(to_kind="a2a", to_name="hermes") as edge:
        edge.task_id = "task-from-peer"
        edge.to_instance = "http://127.0.0.1:7903"

    row = wired.recent()[0]
    assert row["task_id"] == "task-from-peer"
    assert row["to_instance"] == "http://127.0.0.1:7903"


def test_the_writer_stamps_this_agent_as_the_source(wired, monkeypatch):
    import runtime.state as rs

    from graph import ledger

    monkeypatch.setattr(
        rs.STATE, "graph_config", type("C", (), {"identity_name": "gina"})(), raising=False
    )
    ledger.record_delegation(to_kind="a2a", to_name="hermes")

    assert wired.recent()[0]["from_agent"] == "gina"


# --- the plugin seam ------------------------------------------------------------------


def test_the_sdk_seam_writes_a_durable_row(wired):
    """`sdk.record_delegation` is the PUBLIC plugin API — the seam a plugin uses to put
    its own dispatches on the org's record — so it needs its own test rather than
    inheriting confidence from `graph.ledger`.

    A thin passthrough is exactly the kind of thing that breaks silently: one renamed
    keyword and every plugin's edges vanish with no error, because the writer is
    best-effort by design and swallows the failure.
    """
    from graph import sdk

    edge_id = sdk.record_delegation(
        to_kind="a2a",
        to_name="hermes",
        to_instance="http://127.0.0.1:7903",
        what="ask a peer",
        session_id="s1",
        parent_task_id="p1",
        task_id="t1",
        outcome="ok",
        duration_ms=42,
        cost_usd=0.5,
        origin="my-plugin",
    )

    assert edge_id is not None
    row = wired.recent()[0]
    # Every field the seam accepts must survive to the row — a passthrough that drops one
    # silently is the failure this guards.
    assert row["to_kind"] == "a2a"
    assert row["to_name"] == "hermes"
    assert row["to_instance"] == "http://127.0.0.1:7903"
    assert row["what"] == "ask a peer"
    assert row["session_id"] == "s1"
    assert row["parent_task_id"] == "p1"
    assert row["task_id"] == "t1"
    assert row["duration_ms"] == 42
    assert row["cost_usd"] == 0.5
    assert row["origin"] == "my-plugin"


def test_the_sdk_seam_records_a_failed_dispatch(wired):
    from graph import sdk

    sdk.record_delegation(to_kind="acp", to_name="coder", outcome="failed", error="boom")

    row = wired.recent()[0]
    assert row["outcome"] == "failed"
    assert "boom" in row["error"]


def test_the_sdk_seam_never_raises_into_a_plugin(monkeypatch):
    # A plugin's dispatch must not die because the ledger did.
    import runtime.state as rs

    from graph import sdk

    monkeypatch.setattr(rs.STATE, "ledger_store", None, raising=False)
    assert sdk.record_delegation(to_kind="a2a", to_name="hermes") is None
