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
        model = ReviewModel(messages=iter([message]))
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
