"""#4004 — an ``@``-addressed turn's telemetry row.

An ``@name`` message short-circuits the lead graph (``server.chat_dispatch`` STEP 0): the
addressed delegate answers, the lead model never runs. The hub's zero-usage rows on the
``openai-codex`` and ``anthropic-oauth`` lanes were these turns — one tool call (the
mention card), no model generation in their Langfuse traces, only an ``a2a:<peer>`` span.

1. A protoAgent peer reports its own spend (cost-v1). ``delegate_to`` bills it to the
   calling turn through a LangChain custom event (#3016), but the ``@`` exchange runs
   outside any LangChain run, so the event had nowhere to go and the spend was dropped.
   It now reaches the row through the same collector a ``/<subagent>`` short-circuit
   uses (#3957).
2. A turn on which no model of this agent ran still carries a model label (the
   requested or configured model, #3957), so it was counted as a sample of that lane.
   ``summary()["by_model"]`` — the per-lane cost/cache breakdown — now leaves it out,
   and ``no_model_turns`` says how many it left out.

Driven through the real turn drivers (``chat()`` and the A2A executor over
``_chat_langgraph_stream``), the real ``@`` exchange and the real A2A delegate adapter;
only the peer's HTTP endpoint is faked.
"""

from __future__ import annotations

import importlib

import httpx
import pytest

from observability.telemetry_store import TelemetryStore
from plugins.delegates.registry import DelegateRegistry
from tests._turn_driver_fakes import ScriptedGraph
from tests.test_delegate_peer_cost import _PEER_COST, _artifact, _PeerClient
from tests.test_turn_telemetry_3957 import _execute

chat_mod = importlib.import_module("server.chat")

_WITH_COST = {
    "result": {"task": {"id": "t1", "status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [_artifact(_PEER_COST)]}}
}
_NO_COST = {"result": {"task": {"id": "t1", "status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [_artifact()]}}}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Real telemetry store + a real a2a delegate registry; only the peer is faked."""
    from graph.config import LangGraphConfig
    from security import policy

    import runtime.state as rs

    monkeypatch.setattr(policy, "check_url", lambda url: None)
    store = TelemetryStore(str(tmp_path / "telemetry.db"))
    cfg = LangGraphConfig()
    cfg.model_name = "configured-default-model"
    for attr, val in {
        "telemetry_store": store,
        "goal_controller": None,
        "background_mgr": None,
        "watch_controller": None,
        "scheduler": None,
        "graph_auth_error": None,
        "thread_id_resolver": None,
        "checkpointer": None,
        "knowledge_store": None,
        "graph_config": cfg,
        # Takes the mention record and the lead never runs — any model call would raise.
        "graph": ScriptedGraph(),
        "delegate_registry": DelegateRegistry([{"name": "orbis", "type": "a2a", "url": "https://peer/a2a"}]),
    }.items():
        monkeypatch.setattr(rs.STATE, attr, val, raising=False)

    def peer(response):
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _PeerClient(response))

    store.peer = peer
    return store


# ── 1. a peer's reported spend reaches the row ─────────────────────────────────────


async def test_a_streamed_mention_bills_the_peers_reported_spend(env):
    env.peer(_WITH_COST)

    (outcome,) = await _execute("@orbis do the thing")

    assert outcome.state == "completed"
    assert outcome.llm_calls == 1  # was 0
    assert outcome.models == ["peer:orbis"]
    assert outcome.usage["input_tokens"] == 700 and outcome.usage["output_tokens"] == 55
    assert outcome.usage["cache_read_input_tokens"] == 10
    assert outcome.cost_usd == pytest.approx(0.0123)
    # A peer's prompt is another agent's context window, never the lead thread's.
    assert outcome.context_tokens == 0


async def test_a_sync_mention_bills_the_peers_reported_spend(env):
    env.peer(_WITH_COST)

    out = await chat_mod.chat("@orbis do the thing", "s-sync", origin="v1")

    assert out[0]["content"]
    (row,) = env.recent()
    assert row["state"] == "completed"
    assert row["llm_calls"] == 1  # was 0
    assert row["models"] == "peer:orbis"
    assert row["output_tokens"] == 55 and row["cost_usd"] == pytest.approx(0.0123)
    assert not row["model"].startswith("peer:")


async def test_a_mention_to_a_peer_without_cost_v1_still_bills_nothing(env):
    """The hub's 09-27 rows: Hermes is not a protoAgent and reports no cost-v1."""
    env.peer(_NO_COST)

    (outcome,) = await _execute("@orbis do the thing")

    assert outcome.state == "completed"
    assert outcome.llm_calls == 0 and outcome.models == [] and outcome.cost_usd == 0


# ── 2. a turn no model of this agent ran is not a sample of any lane ───────────────


def _row(store, task_id, **kw):
    base = {
        "task_id": task_id,
        "session_id": "s",
        "state": "completed",
        "success": 1,
        "model": "claude-opus-5-5",
        "models": "claude-opus-5-5",
        "input_tokens": 100,
        "output_tokens": 10,
        "total_tokens": 1110,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.01,
        "duration_ms": 2000,
        "llm_calls": 1,
        "tool_calls": 0,
        "created_at": "2026-09-27T06:00:00+00:00",
        "ended_at": "2026-09-27T06:00:02+00:00",
    }
    store.record({**base, **kw})


def test_summary_by_model_leaves_out_turns_no_model_ran(env):
    _row(env, "metered")
    # An @-address to a peer without cost-v1: labelled, but nothing ran on the lane.
    _row(env, "mention", models="", llm_calls=0, tool_calls=1, input_tokens=0, output_tokens=0,
         total_tokens=0, cache_read_input_tokens=0, cost_usd=0.0, duration_ms=300_000)
    # An @-address to a protoAgent peer: the spend is the peer's, not the lane's.
    _row(env, "peer-only", models="peer:orbis", input_tokens=700, cache_read_input_tokens=0,
         total_tokens=755, output_tokens=55, cost_usd=0.0123, duration_ms=90_000)
    # A lead turn that also delegated keeps its place in its lane.
    _row(env, "lead+peer", models="claude-opus-5-5,peer:orbis")

    s = env.summary()

    (lane,) = s["by_model"]
    assert lane["model"] == "claude-opus-5-5"
    assert lane["turns"] == 2  # metered + lead+peer
    assert lane["p95_duration_ms"] == 2000  # the 300 s mention wait is not this model's latency
    assert lane["cache_hit_ratio"] == round(1000 / 1100, 4)
    assert s["no_model_turns"] == 2
    # The whole-store totals still count every turn and every dollar.
    assert s["turns"] == 4
    assert s["cost_usd"] == pytest.approx(0.0323)


def test_a_failed_first_call_stays_in_its_lane(env):
    """A turn whose first call was rejected (#3957) did try that model — it is the lane's."""
    _row(env, "rejected", state="failed", success=0, models="", llm_calls=0, input_tokens=0,
         output_tokens=0, total_tokens=0, cache_read_input_tokens=0, cost_usd=0.0)

    s = env.summary()

    assert [m["model"] for m in s["by_model"]] == ["claude-opus-5-5"]
    assert s["no_model_turns"] == 0
