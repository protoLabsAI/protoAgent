"""Every dispatch funnel writes a durable ledger edge.

These are the tests that matter most. A ledger wired to one funnel and tested only there
is exactly how CLI coding-agent runs stayed invisible to turn telemetry for months
(#3015): the producer was measured, the paths around it were not. So each of the three
funnels gets a test that asserts the DURABLE ROW — never the call that was supposed to
write it, which stays green whether or not anything downstream receives it.

The three funnels, and why there are three rather than one:

- ``graph/agent.py::_run_subagent`` — foreground ``task`` / ``task_batch`` / SDK.
- ``plugins/delegates/registry.py::dispatch`` — every external delegate, and therefore
  ``delegate_to``, the coder ladder, and the board loop.
- ``background/manager.py::spawn`` — background subagent jobs, which do NOT pass through
  ``_run_subagent``: they are fired as a self-directed A2A turn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from observability.ledger_store import LedgerStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    """Point STATE's ledger holder at a throwaway store, as the host does at boot."""
    import runtime.state as rs

    store = LedgerStore(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(rs.STATE, "ledger_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    return store


# --- funnel 1: in-process subagents ----------------------------------------------------


async def test_a_foreground_subagent_delegation_lands_on_the_ledger(ledger_db, monkeypatch):
    import graph.agent as agent

    async def _inner(**_kw):
        return "[researcher completed: dig] -- done"

    monkeypatch.setattr(agent, "_run_subagent_inner", _inner)

    await agent._run_subagent(
        config=None,
        tool_map={},
        available_subagents="researcher",
        description="dig into the thing",
        prompt="go",
        subagent_type="researcher",
        session_id="s1",
        parent_task_id="parent-1",
    )

    rows = ledger_db.recent()
    assert len(rows) == 1
    assert rows[0]["to_kind"] == "subagent"
    assert rows[0]["to_name"] == "researcher"
    assert rows[0]["what"] == "dig into the thing"
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["parent_task_id"] == "parent-1"
    assert rows[0]["outcome"] == "ok"


async def test_a_failed_subagent_delegation_is_still_recorded(ledger_db, monkeypatch):
    # A dispatch that raised still happened and still cost something. A ledger holding
    # only successes answers "what did this fleet do" with a survivor-biased yes.
    import graph.agent as agent

    async def _inner(**_kw):
        raise RuntimeError("subagent exploded")

    monkeypatch.setattr(agent, "_run_subagent_inner", _inner)

    with pytest.raises(RuntimeError):
        await agent._run_subagent(
            config=None,
            tool_map={},
            available_subagents="researcher",
            description="doomed",
            prompt="go",
            subagent_type="researcher",
        )

    row = ledger_db.recent()[0]
    assert row["outcome"] == "failed"
    assert "subagent exploded" in row["error"]


async def test_a_subagent_edge_with_no_reported_usage_records_cost_as_unknown(ledger_db, monkeypatch):
    # No model call reported usage (a native-OAuth path with no usage_metadata, or a run
    # that died before its first call). NULL, never 0: unknown is not free (#3565).
    import graph.agent as agent

    async def _inner(**_kw):
        return "done"

    monkeypatch.setattr(agent, "_run_subagent_inner", _inner)
    await agent._run_subagent(
        config=None,
        tool_map={},
        available_subagents="researcher",
        description="d",
        prompt="p",
        subagent_type="researcher",
    )

    assert ledger_db.recent()[0]["cost_usd"] is None


def _billing_inner(*costs):
    """A sub-graph stand-in that bills one usage row per model call into the sink it is
    handed — the boundary ``_run_subagent_inner`` writes through."""

    async def _inner(*, usage_sink=None, **_kw):
        for cost in costs:
            usage_sink.append({"input_tokens": 10, "output_tokens": 5, "cost_usd": cost, "model": "m"})
        return "done"

    return _inner


async def test_a_workflow_step_subagent_edge_carries_its_priced_spend(ledger_db, monkeypatch):
    # A workflow step reaches _run_subagent through sdk.run_subagent with NO usage_sink —
    # and telemetry's `turns` holds A2A turns only, so before #3565 its cost existed
    # nowhere but Langfuse. The edge now carries the sum of its priced calls.
    import graph.agent as agent

    monkeypatch.setattr(agent, "_run_subagent_inner", _billing_inner(0.0125, 0.0031))
    await agent._run_subagent(
        config=None,
        tool_map={},
        available_subagents="review-finder",
        description="workflow code-review:find",
        prompt="p",
        subagent_type="review-finder",
    )

    assert ledger_db.recent()[0]["cost_usd"] == pytest.approx(0.0156)


async def test_the_parent_turn_is_still_billed_the_same_rows(ledger_db, monkeypatch):
    # The ledger cost is a per-edge view; the parent turn's usage_sink must still get
    # every row (#2872) — collecting locally for the ledger must not swallow them.
    import graph.agent as agent

    monkeypatch.setattr(agent, "_run_subagent_inner", _billing_inner(0.5, 0.25))
    parent_rows: list[dict] = []
    await agent._run_subagent(
        config=None,
        tool_map={},
        available_subagents="researcher",
        description="d",
        prompt="p",
        subagent_type="researcher",
        usage_sink=parent_rows,
    )

    assert [r["cost_usd"] for r in parent_rows] == [0.5, 0.25]
    assert ledger_db.recent()[0]["cost_usd"] == pytest.approx(0.75)


async def test_a_failed_subagent_still_records_what_it_spent(ledger_db, monkeypatch):
    import graph.agent as agent

    async def _inner(*, usage_sink=None, **_kw):
        usage_sink.append({"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.02, "model": "m"})
        raise RuntimeError("died after one call")

    monkeypatch.setattr(agent, "_run_subagent_inner", _inner)
    with pytest.raises(RuntimeError):
        await agent._run_subagent(
            config=None,
            tool_map={},
            available_subagents="researcher",
            description="d",
            prompt="p",
            subagent_type="researcher",
        )

    row = ledger_db.recent()[0]
    assert row["outcome"] == "failed"
    assert row["cost_usd"] == pytest.approx(0.02)


# --- funnel 2: external delegates -------------------------------------------------------


async def test_a_delegate_dispatch_lands_on_the_ledger(ledger_db, monkeypatch):
    from plugins.delegates.adapters import ADAPTERS
    from plugins.delegates.registry import DelegateRegistry

    async def _dispatch(d, query, *, timeout=None, item_id=None, resume_task_id=None):
        return "done"

    monkeypatch.setattr(ADAPTERS["acp"], "dispatch", _dispatch)
    reg = DelegateRegistry([{"name": "coder", "type": "acp", "command": "proto", "workdir": "/tmp"}])

    assert await reg.dispatch("coder", "implement the thing") == "done"

    row = ledger_db.recent()[0]
    assert (row["to_kind"], row["to_name"]) == ("acp", "coder")
    assert row["what"] == "implement the thing"
    assert row["outcome"] == "ok"
    assert row["origin"] == "delegate_to"


async def test_a_failed_delegate_dispatch_is_recorded_as_failed(ledger_db, monkeypatch):
    from plugins.delegates.adapters import ADAPTERS
    from plugins.delegates.registry import DelegateRegistry

    async def _dispatch(d, query, *, timeout=None, item_id=None, resume_task_id=None):
        raise RuntimeError("binary left PATH")

    monkeypatch.setattr(ADAPTERS["acp"], "dispatch", _dispatch)
    reg = DelegateRegistry([{"name": "coder", "type": "acp", "command": "proto", "workdir": "/tmp"}])

    with pytest.raises(Exception):
        await reg.dispatch("coder", "go")

    row = ledger_db.recent()[0]
    assert row["outcome"] == "failed"
    assert "binary left PATH" in row["error"]


async def test_a_cancelled_delegate_dispatch_is_not_recorded_as_a_failure(ledger_db, monkeypatch):
    # An operator hitting stop says nothing about the delegate. Recording it as a failure
    # would put a red mark on a healthy coder every time.
    import asyncio

    from plugins.delegates.adapters import ADAPTERS
    from plugins.delegates.registry import DelegateRegistry

    async def _dispatch(d, query, *, timeout=None, item_id=None, resume_task_id=None):
        raise asyncio.CancelledError()

    monkeypatch.setattr(ADAPTERS["acp"], "dispatch", _dispatch)
    reg = DelegateRegistry([{"name": "coder", "type": "acp", "command": "proto", "workdir": "/tmp"}])

    with pytest.raises(asyncio.CancelledError):
        await reg.dispatch("coder", "go")

    assert ledger_db.recent()[0]["outcome"] == "cancelled"


async def test_an_a2a_delegate_records_the_peer_it_reached(ledger_db, monkeypatch):
    from plugins.delegates.adapters import ADAPTERS
    from plugins.delegates.registry import DelegateRegistry

    async def _dispatch(d, query, *, timeout=None, item_id=None, resume_task_id=None):
        return "ok"

    monkeypatch.setattr(ADAPTERS["a2a"], "dispatch", _dispatch)
    reg = DelegateRegistry([{"name": "hermes", "type": "a2a", "url": "http://127.0.0.1:7903/a2a"}])

    await reg.dispatch("hermes", "ask")

    row = ledger_db.recent()[0]
    assert row["to_kind"] == "a2a"
    assert "7903" in row["to_instance"]


# --- funnel 3: background subagent jobs -------------------------------------------------


async def test_a_background_spawn_lands_on_the_ledger(ledger_db, tmp_path, monkeypatch):
    # The funnel that proves the point: a background job never touches _run_subagent — it
    # is fired as a self-directed A2A turn — so a ledger wired only to the in-process path
    # would miss every backgrounded delegation.
    from background.manager import BackgroundManager
    from background.store import BackgroundStore

    mgr = BackgroundManager(
        agent_name="a",
        invoke_url="http://127.0.0.1:7870",
        store=BackgroundStore(str(Path(tmp_path) / "bg.db")),
        api_key="k",
        bearer_token="b",
    )

    async def _fire(*_a, **_kw):
        return None

    monkeypatch.setattr(mgr, "_fire", _fire)

    job_id = await mgr.spawn(
        origin_session="s1",
        subagent_type="researcher",
        description="dig in the background",
        prompt="go",
    )

    row = ledger_db.recent()[0]
    assert row["to_kind"] == "subagent"
    assert row["to_name"] == "researcher"
    assert row["what"] == "dig in the background"
    assert row["session_id"] == "s1"
    assert row["task_id"] == job_id
    assert row["origin"] == "background"


async def test_a_ledger_failure_cannot_break_a_background_spawn(ledger_db, tmp_path, monkeypatch):
    from background.manager import BackgroundManager
    from background.store import BackgroundStore

    from graph import ledger as ledger_mod

    mgr = BackgroundManager(
        agent_name="a",
        invoke_url="http://127.0.0.1:7870",
        store=BackgroundStore(str(Path(tmp_path) / "bg.db")),
        api_key="k",
        bearer_token="b",
    )

    async def _fire(*_a, **_kw):
        return None

    def _boom(**_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(mgr, "_fire", _fire)
    monkeypatch.setattr(ledger_mod, "record_delegation", _boom)

    # The work matters more than the record of it.
    job_id = await mgr.spawn(
        origin_session="s1", subagent_type="researcher", description="d", prompt="p"
    )
    assert job_id


# --- settling a detached edge -----------------------------------------------------------


async def test_a_background_edge_is_closed_out_when_the_job_lands(ledger_db, tmp_path, monkeypatch):
    """A detached delegation is written twice: at dispatch so in-flight work is visible,
    and again at completion so the edge says what actually happened.

    Recording only the dispatch leaves every background edge reading `ok` with a zero
    duration forever — a claim about an outcome nobody observed.
    """
    from background.manager import BackgroundManager
    from background.store import BackgroundStore

    store = BackgroundStore(str(Path(tmp_path) / "bg.db"))
    mgr = BackgroundManager(
        agent_name="a", invoke_url="http://127.0.0.1:7870", store=store, api_key="k", bearer_token="b"
    )

    async def _fire(*_a, **_kw):
        return None

    monkeypatch.setattr(mgr, "_fire", _fire)
    job_id = await mgr.spawn(
        origin_session="s1", subagent_type="researcher", description="dig", prompt="go"
    )

    # At dispatch: recorded, but the outcome is "dispatched", not "succeeded".
    assert ledger_db.recent()[0]["duration_ms"] == 0

    store.mark_complete(job_id, "completed", "found it")

    row = ledger_db.recent()[0]
    assert row["outcome"] == "ok"
    assert row["duration_ms"] > 0, "a settled edge must carry the work's real duration"


async def test_a_failed_background_job_settles_the_edge_as_failed(ledger_db, tmp_path, monkeypatch):
    from background.manager import BackgroundManager
    from background.store import BackgroundStore

    store = BackgroundStore(str(Path(tmp_path) / "bg.db"))
    mgr = BackgroundManager(
        agent_name="a", invoke_url="http://127.0.0.1:7870", store=store, api_key="k", bearer_token="b"
    )

    async def _fire(*_a, **_kw):
        return None

    monkeypatch.setattr(mgr, "_fire", _fire)
    job_id = await mgr.spawn(
        origin_session="s1", subagent_type="researcher", description="doomed", prompt="go"
    )
    store.mark_complete(job_id, "failed", "the subagent exploded")

    row = ledger_db.recent()[0]
    assert row["outcome"] == "failed"
    assert "exploded" in row["error"]


async def test_a_canceled_background_job_is_not_recorded_as_a_failure(
    ledger_db, tmp_path, monkeypatch
):
    # Same rule the dispatch path follows: an operator stopping work says nothing about
    # the delegate.
    from background.manager import BackgroundManager
    from background.store import BackgroundStore

    store = BackgroundStore(str(Path(tmp_path) / "bg.db"))
    mgr = BackgroundManager(
        agent_name="a", invoke_url="http://127.0.0.1:7870", store=store, api_key="k", bearer_token="b"
    )

    async def _fire(*_a, **_kw):
        return None

    monkeypatch.setattr(mgr, "_fire", _fire)
    job_id = await mgr.spawn(
        origin_session="s1", subagent_type="researcher", description="stopped", prompt="go"
    )
    store.mark_complete(job_id, "canceled", "Canceled.")

    assert ledger_db.recent()[0]["outcome"] == "cancelled"


async def test_a_redundant_settle_cannot_restate_a_recorded_outcome(
    ledger_db, tmp_path, monkeypatch
):
    """`mark_complete` is idempotent and BOTH the manager and the A2A terminal hook call
    it, so a second settle must not overwrite the outcome the first one recorded."""
    from background.manager import BackgroundManager
    from background.store import BackgroundStore

    store = BackgroundStore(str(Path(tmp_path) / "bg.db"))
    mgr = BackgroundManager(
        agent_name="a", invoke_url="http://127.0.0.1:7870", store=store, api_key="k", bearer_token="b"
    )

    async def _fire(*_a, **_kw):
        return None

    monkeypatch.setattr(mgr, "_fire", _fire)
    job_id = await mgr.spawn(
        origin_session="s1", subagent_type="researcher", description="d", prompt="p"
    )
    assert store.mark_complete(job_id, "failed", "real failure") is True
    assert store.mark_complete(job_id, "completed", "late no-op") is False

    row = ledger_db.recent()[0]
    assert row["outcome"] == "failed"
    assert "real failure" in row["error"]


async def test_a_background_edge_settles_with_the_turns_cost(ledger_db, tmp_path, monkeypatch):
    """The A2A terminal hook knows the background turn's priced spend; it lands on the
    edge (#3565). A zero from the executor means no usage was reported — left NULL."""
    from types import SimpleNamespace

    import server.a2a as a2a
    from a2a_impl.executor import TurnOutcome
    from background.manager import BackgroundManager
    from background.store import BackgroundStore
    from runtime.state import STATE

    store = BackgroundStore(str(Path(tmp_path) / "bg.db"))
    mgr = BackgroundManager(
        agent_name="a", invoke_url="http://127.0.0.1:7870", store=store, api_key="k", bearer_token="b"
    )

    async def _fire(*_a, **_kw):
        return None

    monkeypatch.setattr(mgr, "_fire", _fire)
    monkeypatch.setattr(STATE, "background_mgr", mgr, raising=False)
    monkeypatch.setattr(a2a._event_bus, "publish", lambda *_a, **_kw: None)
    monkeypatch.setattr(mgr, "resume_origin", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(a2a, "_spawn_background_wake", lambda *_a, **_kw: None)
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(), raising=False)

    costs = {}
    for cost in (0.42, 0.0):
        job_id = await mgr.spawn(origin_session="s1", subagent_type="researcher", description=f"c{cost}", prompt="go")
        outcome = TurnOutcome(
            task_id="t", context_id=f"background:{job_id}", state="completed", text="done",
            cost_usd=cost, origin="background", trigger=job_id,
        )
        a2a._handle_background_terminal(outcome)
        costs[cost] = next(r["cost_usd"] for r in ledger_db.recent() if r["task_id"] == job_id)

    assert costs[0.42] == pytest.approx(0.42)
    assert costs[0.0] is None
