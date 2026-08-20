"""Telemetry tests.

Two surfaces share this file:

- The plugin telemetry + decision-log kit (``graph.telemetry``): a decision-log
  ring buffer, the standard telemetry envelope, and a themed HTML panel. Pure
  stdlib — tested directly.
- Subagent usage propagation into the parent turn's accumulator (#2872): the
  ``task``/``task_batch`` tools bill each delegation's model calls (tokens, cost,
  model name) into the turn's telemetry via custom ``usage`` events, and the
  executor keeps the tagged rows out of the LEAD thread's context-fill tracking.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph.sdk import DecisionLog as SdkDecisionLog  # re-exported on the plugin SDK surface
from graph.telemetry import DecisionLog, render_html, telemetry


def test_exported_on_the_sdk():
    assert SdkDecisionLog is DecisionLog


# ── DecisionLog ──────────────────────────────────────────────────────────────────────
def test_decision_log_records_action_detail_and_extra_fields():
    log = DecisionLog()
    e = log.record("tune", "min_margin: 30 → 15", reason="cr/hr falling")
    assert e == {"action": "tune", "detail": "min_margin: 30 → 15", "reason": "cr/hr falling"}
    assert log.entries() == [e] and len(log) == 1


def test_decision_log_is_capped_newest_kept():
    log = DecisionLog(cap=3)
    for i in range(5):
        log.record("tune", f"k{i}")
    details = [e["detail"] for e in log.entries()]
    assert details == ["k2", "k3", "k4"]  # oldest two fell off, newest last


def test_decision_log_entries_n_and_clear():
    log = DecisionLog()
    for i in range(4):
        log.record("a", str(i))
    assert [e["detail"] for e in log.entries(2)] == ["2", "3"]
    log.clear()
    assert log.entries() == [] and len(log) == 0


# ── telemetry envelope ───────────────────────────────────────────────────────────────
def test_telemetry_envelope_has_the_standard_shape():
    env = telemetry(status="running", metrics={"credits": 1000}, hints=["reinvest"])
    assert env["status"] == "running"
    assert env["metrics"] == {"credits": 1000}
    assert env["hints"] == ["reinvest"]
    assert env["decisions"] == [] and env["sections"] == []


def test_telemetry_accepts_a_decisionlog_or_a_list():
    log = DecisionLog()
    log.record("strategy", "→ trade-max")
    assert telemetry(decisions=log)["decisions"] == [{"action": "strategy", "detail": "→ trade-max"}]
    raw = [{"action": "tune", "detail": "x"}]
    assert telemetry(decisions=raw)["decisions"] == raw


def test_telemetry_passes_extra_keys_through():
    env = telemetry(status="ok", credits=1234, per_hour=42)
    assert env["credits"] == 1234 and env["per_hour"] == 42


# ── render_html ──────────────────────────────────────────────────────────────────────
def test_render_html_includes_metrics_decisions_and_hints():
    log = DecisionLog()
    log.record("tune", "min_margin 30→15")
    env = telemetry(
        status="running · 1,000,000 cr",
        metrics={"credits": 1_000_000},
        hints=["idle capital — reinvest"],
        decisions=log,
    )
    out = render_html(env, title="Fleet")
    assert "<section" in out and "pl-tele" in out
    assert "Fleet" in out and "running · 1,000,000 cr" in out
    assert "1,000,000" in out  # int metric comma-formatted
    assert "min_margin 30→15" in out and "idle capital — reinvest" in out
    assert "--pl-color-fg" in out  # themed via DS tokens (with fallbacks)


def test_render_html_renders_section_tables():
    env = telemetry(
        sections=[
            {"title": "Fleet", "columns": ["ship", "role"], "rows": [["DRONE-1", "miner"], ["HAULER-1", "trader"]]}
        ]
    )
    out = render_html(env)
    assert "<th>ship</th>" in out and "<td>DRONE-1</td>" in out and "<td>miner</td>" in out


def test_render_html_escapes_values():
    env = telemetry(status="<script>alert(1)</script>", hints=["a & b <c>"])
    out = render_html(env)
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out and "a &amp; b &lt;c&gt;" in out


def test_render_html_handles_empty_envelope():
    out = render_html(telemetry())
    assert "<section" in out and "</section>" in out  # no metrics/decisions/hints → still valid


def test_decisions_render_newest_first():
    log = DecisionLog()
    log.record("a", "first")
    log.record("b", "second")
    out = render_html(telemetry(decisions=log))
    assert out.index("second") < out.index("first")  # newest at the top of the table


# ── Subagent usage → parent turn telemetry (#2872) ───────────────────────────────────
#
# The `task`/`task_batch` tools extract each delegation's per-model-call usage from
# the sub-graph's final state and dispatch it as custom `usage` events; the chat
# stream forwards them as `("usage", …)` frames; the executor's accumulator sums
# them into the turn's tokens/cost/models — tagged rows stay out of the LEAD
# thread's context-window fill.


def test_extract_subagent_usage_rows_shape_tagging_and_model_fallback():
    from graph.agent import _extract_subagent_usage
    from observability import pricing

    messages = [
        HumanMessage(content="go"),
        AIMessage(
            content="draft",
            usage_metadata={
                "input_tokens": 120,
                "output_tokens": 30,
                "total_tokens": 150,
                "input_token_details": {"cache_read": 100, "cache_creation": 7},
            },
        ),
        ToolMessage(content="tool result", tool_call_id="t1"),
        AIMessage(
            content="routed",
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            response_metadata={"model_name": "claude-haiku-4-5"},
        ),
        AIMessage(content="no usage_metadata -> skipped"),
    ]
    rows = _extract_subagent_usage(messages, subagent_type="creative", fallback_model="protolabs/creative")
    assert len(rows) == 2
    first, second = rows
    assert first == {
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 7,
        "cost_usd": pricing.cost_usd("protolabs/creative", {"input_tokens": 120, "output_tokens": 30}),
        "model": "protolabs/creative",  # the pinned/aux fallback — a model-pinned subagent bills to ITS model
        "subagent_type": "creative",
    }
    # A gateway-reported per-reply model name wins over the fallback.
    assert second["model"] == "claude-haiku-4-5"
    assert second["input_tokens"] == 10 and second["output_tokens"] == 5
    assert second["subagent_type"] == "creative"


def test_extract_subagent_usage_handles_empty_and_usage_less_lists():
    from graph.agent import _extract_subagent_usage

    assert _extract_subagent_usage([], subagent_type="researcher") == []
    assert _extract_subagent_usage([AIMessage(content="hi")], subagent_type="researcher") == []


@pytest.mark.asyncio
async def test_emit_subagent_usage_without_run_context_is_a_noop():
    """Outside a graph run (unit tests, the manual console runner) there is no
    callback context to dispatch into — the emitter must swallow that, never raise."""
    from graph.agent import _emit_subagent_usage

    await _emit_subagent_usage([])
    await _emit_subagent_usage([{"input_tokens": 1, "output_tokens": 1, "model": "m", "subagent_type": "researcher"}])


# ── executor accumulator: tagged usage frames ────────────────────────────────────────


def _mini_a2a_app(stream_fn):
    """A real a2a-sdk app driven by ProtoAgentExecutor(stream_fn) — the same wiring
    tests/test_a2a_handler.py uses, trimmed to what the accumulator tests need."""
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes.agent_card_routes import create_agent_card_routes
    from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
    from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
    from a2a.server.tasks import InMemoryPushNotificationConfigStore, InMemoryTaskStore
    from a2a.types import AgentSkill
    from fastapi import FastAPI

    import protolabs_a2a as pa
    from a2a_impl.executor import ProtoAgentExecutor

    card = pa.build_agent_card(
        name="telemetry-test",
        description="d",
        url="http://test/a2a",
        version="0.0.0",
        skills=[AgentSkill(id="chat", name="Chat", description="general chat", tags=["t"])],
    )
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(stream_fn),
        task_store=InMemoryTaskStore(),
        agent_card=card,
        push_config_store=InMemoryPushNotificationConfigStore(),
    )
    app = FastAPI()
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/a2a"),
    )
    return app


async def _run_turn_outcome(stream_fn):
    """One SendMessage through the mini app; returns the terminal TurnOutcome."""
    from a2a_impl.executor import set_terminal_hook

    outcomes = []
    set_terminal_hook(outcomes.append)
    try:
        app = _mini_a2a_app(stream_fn)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10
        ) as c:
            resp = await c.post(
                "/a2a",
                headers={"A2A-Version": "1.0"},
                json={
                    "jsonrpc": "2.0",
                    "id": "r1",
                    "method": "SendMessage",
                    "params": {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "hi"}]}},
                },
            )
            assert resp.status_code == 200, resp.text
            for _ in range(200):  # the hook fires inside execute(); poll the hook, not the task
                if outcomes:
                    break
                await asyncio.sleep(0.02)
    finally:
        set_terminal_hook(None)
    assert outcomes, "turn never reached a terminal outcome"
    return outcomes[0]


_LEAD_USAGE = {
    "input_tokens": 30,
    "output_tokens": 12,
    "cache_read_input_tokens": 5,
    "cache_creation_input_tokens": 2,
    "cost_usd": 0.002,
    "model": "claude-lead",
}
_SUB_USAGE = {
    "input_tokens": 700,
    "output_tokens": 55,
    "cache_read_input_tokens": 10,
    "cache_creation_input_tokens": 0,
    "cost_usd": 0.01,
    "model": "protolabs/creative",
    "subagent_type": "researcher",
}


@pytest.mark.asyncio
async def test_turn_accumulator_sums_subagent_usage_into_totals_models_and_cost():
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("usage", dict(_LEAD_USAGE))
        yield ("usage", dict(_SUB_USAGE))
        yield ("done", "answer")

    o = await _run_turn_outcome(stream)
    assert o.state == "completed"
    # Token totals and cost include the delegated call's contribution.
    assert o.usage["input_tokens"] == 730 and o.usage["output_tokens"] == 67
    assert o.usage["cache_read_input_tokens"] == 15
    assert o.cost_usd == pytest.approx(0.012)
    # The subagent's (pinned) model lands in the turn's models list.
    assert o.models == ["claude-lead", "protolabs/creative"]
    assert o.llm_calls == 2
    # …but the delegated prompt must NOT count as the lead thread's context fill.
    assert o.context_tokens == 30


@pytest.mark.asyncio
async def test_turn_accumulator_unchanged_without_subagent_usage():
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("usage", dict(_LEAD_USAGE))
        yield ("done", "answer")

    o = await _run_turn_outcome(stream)
    assert o.usage["input_tokens"] == 30 and o.usage["output_tokens"] == 12
    assert o.cost_usd == pytest.approx(0.002)
    assert o.models == ["claude-lead"] and o.llm_calls == 1
    assert o.context_tokens == 30  # untagged usage still tracks context fill


# ── task() end-to-end: the delegation's usage reaches the parent turn's stream ──────


def _delegation_script():
    """lead delegates → subagent answers (with usage) → lead answers."""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {"description": "research", "prompt": "go", "subagent_type": "researcher"},
                    "id": "t1",
                    "type": "tool_call",
                }
            ],
        ),
        # The subagent's single (mock) model call — this usage must bill to the parent turn.
        AIMessage(
            content="sub answer",
            usage_metadata={"input_tokens": 700, "output_tokens": 55, "total_tokens": 755},
        ),
        AIMessage(content="lead answer"),
    ]


def _install_graph(monkeypatch, messages):
    """Mirror tests/test_subagent_stream_isolation.py's harness, with a fake model
    that preserves usage_metadata on its chunks (so the subagent's final state
    carries the usage the task tool must extract)."""
    import itertools
    import json

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk
    from langgraph.checkpoint.memory import MemorySaver

    import runtime.state as rs
    from graph.config import LangGraphConfig

    class _UsageToolFake(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _chunk(self):
            msg = next(self.messages)
            return ChatGenerationChunk(
                message=AIMessageChunk(
                    content=msg.content,
                    usage_metadata=getattr(msg, "usage_metadata", None),
                    tool_call_chunks=[
                        {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}
                        for i, tc in enumerate(getattr(msg, "tool_calls", None) or [])
                    ],
                )
            )

        def _stream(self, messages, stop=None, run_manager=None, **kwargs):
            yield self._chunk()

        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            await asyncio.sleep(0)
            chunk = self._chunk()
            await asyncio.sleep(0)
            yield chunk

    stream = itertools.chain(iter(messages), itertools.repeat(AIMessage(content="done")))
    fake = _UsageToolFake(messages=stream)
    monkeypatch.setattr("graph.agent.create_llm", lambda *a, **k: fake)
    from graph.agent import create_agent_graph

    g = create_agent_graph(LangGraphConfig(), include_subagents=True, checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)


@pytest.mark.asyncio
async def test_task_delegation_bills_subagent_usage_to_parent_turn(monkeypatch):
    """A `task()` invocation with a mock subagent model call surfaces that call's
    usage — tokens, the PINNED model name, a `subagent_type` tag — as exactly one
    `usage` frame on the parent turn's stream (what the executor accumulates)."""
    import dataclasses

    from graph.subagents.config import SUBAGENT_REGISTRY
    from server.chat import _run_turn_stream

    # Model-pin the subagent so the frame must attribute to ITS model, not the lead's.
    monkeypatch.setitem(
        SUBAGENT_REGISTRY,
        "researcher",
        dataclasses.replace(SUBAGENT_REGISTRY["researcher"], model="protolabs/creative"),
    )
    _install_graph(monkeypatch, _delegation_script())

    usage_frames = []
    async for kind, payload in _run_turn_stream(
        "delegate then answer", "tele1", {"configurable": {"thread_id": "tele1"}}
    ):
        if kind == "usage":
            usage_frames.append(payload)

    tagged = [f for f in usage_frames if f.get("subagent_type")]
    assert len(tagged) == 1, f"expected exactly one subagent usage frame, got {usage_frames}"
    row = tagged[0]
    assert row["subagent_type"] == "researcher"
    assert row["model"] == "protolabs/creative"
    assert row["input_tokens"] == 700 and row["output_tokens"] == 55
    assert "cost_usd" in row
    # No untagged duplicate of the same call — the double-billing guard: the
    # subagent's bubbled on_chat_model_end no longer yields its own frame.
    assert not [f for f in usage_frames if not f.get("subagent_type") and f.get("input_tokens") == 700]
