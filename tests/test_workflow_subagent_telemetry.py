"""Workflow delegations bill turns exactly once, using the shared turn writer."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from graph import agent, delegation_usage, ledger
from graph.config import LangGraphConfig
from graph.providers import oauth
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
from observability import pricing
from observability.ledger_store import LedgerStore
from observability.telemetry_store import TelemetryStore
from runtime.state import STATE


class ReviewModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def harness(tmp_path, monkeypatch):
    config = LangGraphConfig.from_dict({"model": {"provider": "openai", "name": "lead"}})
    store = TelemetryStore(str(tmp_path / "turns.db"))
    edges = LedgerStore(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(STATE, "telemetry_store", store)
    monkeypatch.setattr(STATE, "ledger_store", edges, raising=False)
    monkeypatch.setattr(STATE, "graph_config", config)
    monkeypatch.setattr(oauth, "peek_codex_access_token", lambda: SimpleNamespace(account_id="review-account"))
    monkeypatch.setitem(SUBAGENT_REGISTRY, "review-test", SubagentConfig(
        name="review-test", description="review", system_prompt="Review.",
        tools=[], model="openai-codex:gpt-5", max_turns=2,
    ))
    monkeypatch.setattr(agent, "build_subagent_prompt", lambda *_a, **_kw: "Review.")

    async def run(*, usage=True, **kwargs):
        message = AIMessage(
            content="Review complete.",
            response_metadata={"model_name": "gpt-5"},
            usage_metadata={
                "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                "input_token_details": {"cache_read": 30, "cache_creation": 10},
            } if usage else None,
        )
        model = ReviewModel(messages=iter([message]), disable_streaming=True)
        monkeypatch.setattr(agent, "create_llm", lambda *_a, **_kw: model)
        return await agent._run_subagent(
            config=config, tool_map={}, available_subagents="review-test",
            description="review gate", prompt="Review this.", subagent_type="review-test",
            session_id="workflow-session", **kwargs,
        )

    return run, store, edges


async def test_orphan_records_one_attributed_turn(harness):
    run, store, edges = harness
    before = datetime.now(UTC) - timedelta(seconds=1)
    with ledger.run_scope(run_id="workflow-run"):
        assert "Review complete" in await run()
    rows = store.recent()
    assert len(rows) == 1
    row = rows[0]
    assert row["total_tokens"] == 120
    assert (row["input_tokens"], row["cache_read_input_tokens"], row["cache_creation_input_tokens"]) == (60, 30, 10)
    assert row["model"] == "gpt-5"
    assert row["account_key"] == "review-account"
    assert row["provider_id"] == "openai-codex"
    assert row["session_id"] == "workflow-session"
    assert row["run_id"] == "workflow-run"
    assert row["task_id"] == edges.recent()[0]["edge_uuid"]
    assert row["cost_usd"] == edges.recent()[0]["cost_usd"]
    assert row["llm_calls"] == 1
    assert store.attributed_tokens("review-account", before.isoformat(),
                                   (datetime.now(UTC) + timedelta(seconds=1)).isoformat()) == 120


@pytest.mark.parametrize("owner", ["sink", "collector", "both"])
async def test_parent_owned_usage_does_not_write_turn(harness, owner):
    run, store, _ = harness
    sink = []
    kwargs = {"usage_sink": sink} if owner in ("sink", "both") else {}
    if owner in ("collector", "both"):
        with delegation_usage.collect() as collected:
            assert delegation_usage.is_collecting()
            await run(**kwargs)
            assert len(collected) == 1
    else:
        await run(**kwargs)
    if kwargs:
        assert len(sink) == 1
    assert not delegation_usage.is_collecting()
    assert store.recent() == []


async def test_no_usage_leaves_cost_unknown(harness):
    run, store, edges = harness
    await run(usage=False)
    assert store.recent() == []
    assert edges.recent()[0]["cost_usd"] is None


async def test_unknown_price_stays_null(harness, monkeypatch):
    run, store, edges = harness
    monkeypatch.setattr(pricing, "cost_usd", lambda *_a, **_kw: None)
    await run()
    assert store.recent()[0]["cost_usd"] is None
    assert edges.recent()[0]["cost_usd"] is None


async def test_failed_write_is_logged_without_breaking_delegation(harness, monkeypatch, caplog):
    run, store, edges = harness

    def fail(_row):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(store, "record", fail)
    assert "Review complete" in await run()
    assert "failed to record turn" in caplog.text
    assert edges.recent()[0]["outcome"] == "ok"


async def test_multiple_calls_and_failed_delegation_keep_spend(harness, monkeypatch):
    _, store, edges = harness

    async def fail(**kwargs):
        kwargs["usage_sink"].extend([
            {"model": "gpt-5", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.1},
            {"model": "gpt-5", "input_tokens": 20, "output_tokens": 7, "cost_usd": 0.2},
        ])
        raise RuntimeError("review failed")

    monkeypatch.setattr(agent, "_run_subagent_inner", fail)
    with pytest.raises(RuntimeError, match="review failed"):
        await agent._run_subagent(config=STATE.graph_config, tool_map={}, available_subagents="review-test",
                                  description="review", prompt="go", subagent_type="review-test")
    rows = store.recent()
    assert len(rows) == 1
    assert rows[0]["total_tokens"] == 42
    assert rows[0]["cost_usd"] == pytest.approx(0.3)
    assert rows[0]["state"] == "failed"
    assert rows[0]["run_id"] == edges.recent()[0]["run_id"]


async def test_writer_setup_failure_is_best_effort(harness, monkeypatch, caplog):
    from observability import turn_telemetry

    run, store, edges = harness

    def fail(**kwargs):
        raise RuntimeError("writer setup failed")

    monkeypatch.setattr(turn_telemetry, "record_turn", fail)
    assert "Review complete" in await run()
    assert "failed to record subagent" in caplog.text
    assert store.recent() == []
    assert edges.recent()[0]["cost_usd"] is not None


@pytest.mark.parametrize("driver", ["stream", "sync", "sync_continuation"])
@pytest.mark.parametrize("runner_task", [False, True])
async def test_in_turn_tool_delegation_is_billed_only_by_parent(harness, monkeypatch, driver, runner_task):
    """Plugin tools and workflow runner tasks inherit the lead's billing owner."""
    import asyncio
    from unittest.mock import AsyncMock

    from langchain_core.runnables import RunnableLambda
    from langchain_core.tools import tool
    from observability.turn_telemetry import fold_usage_rows, record_turn, telemetry_usage, make_usage_callback
    from server import turn_stream, turn_sync

    run, store, edges = harness

    @tool
    async def review_gate() -> str:
        """Run the workflow review gate without a sink or collector."""
        assert not delegation_usage.is_collecting()
        if runner_task:
            return await asyncio.create_task(run())
        return await run()

    async def lead(_input):
        answer = await review_gate.ainvoke({})
        return {"messages": [AIMessage(content=answer)]}

    monkeypatch.setattr(STATE, "graph", RunnableLambda(lead))
    monkeypatch.setattr(STATE, "goal_controller", None)
    chat = turn_stream._chat()
    monkeypatch.setattr(chat, "_pending_interrupt_value", AsyncMock(return_value=None))
    monkeypatch.setattr(chat, "_drain_background", lambda *_a: ([], []))
    monkeypatch.setattr(turn_sync._turn_control, "_hold_if_hitl_pending", AsyncMock(return_value=None))
    calls = 2 if driver == "sync_continuation" else 1
    if driver == "sync_continuation":
        async def steps(self):
            yield turn_sync._goal_loop.GoalContinuation(
                message="Review again", config={"configurable": {"thread_id": "fresh-goal-thread"}},
            )

        monkeypatch.setattr(turn_sync._goal_loop.GoalDrive, "steps", steps)
        monkeypatch.setattr(turn_stream, "_carried_fence", AsyncMock(return_value=[]))
    config = {"configurable": {"thread_id": "parent"}, "metadata": {"trace_tag": "keep"}}
    if driver == "stream":
        frames = [frame async for frame in turn_stream._run_turn_stream("review", "parent", config)]
        usage_rows = [payload for kind, payload in frames if kind == "usage"]
        models, usage, cost = fold_usage_rows(usage_rows)
        assert len(usage_rows) == 1
    else:
        callback = make_usage_callback()
        await turn_sync._native_turn(
            "review", None, session_id="parent", config={**config, "callbacks": [callback]},
            usage_cb=callback, state_extra={}, tool_fence=None, hitl_resume=False, incognito=False,
        )
        models, usage, cost = telemetry_usage(callback.usage_metadata)
        assert callback.llm_calls == calls
    assert store.recent() == []
    assert len(edges.recent()) == calls
    assert usage["input_tokens"] + usage["output_tokens"] == 120 * calls
    record_turn(task_id="parent", session_id="parent", state="completed", models=models,
                usage=usage, cost_usd=cost, requested_model="openai-codex:gpt-5")
    assert [row["total_tokens"] for row in store.recent()] == [120 * calls]
    assert config["metadata"] == {"trace_tag": "keep"}
    # Billing ownership must not escape the graph's runnable context.
    assert not delegation_usage.is_parent_billed()
    await run()
    assert len(store.recent()) == 2


async def test_unbilled_runnable_callbacks_do_not_hide_orphan_usage(harness):
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.runnables import RunnableLambda

    run, store, _ = harness

    async def workflow(_input):
        return await run()

    await RunnableLambda(workflow).ainvoke({}, config={"callbacks": [BaseCallbackHandler()]})
    assert [row["total_tokens"] for row in store.recent()] == [120]
