"""`sdk.run_subagent(session_id=…)` stamps the delegation's ledger edge, so a run driven
through the SDK (the workflows plugin's per-step runner) joins back to the caller it
belongs to instead of landing with an empty ``session_id`` (#4612 part 2).

A fake chat model drives the real graph; its input state and ledger rows verify
that attribution stays separate from the session exposed to tools.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

import graph.agent as agent
import graph.sdk as sdk
import runtime.state as rs
from graph import ledger
from graph.config import LangGraphConfig
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
from observability.ledger_store import LedgerStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def graph_sessions():
    return []


@pytest.fixture
def ledger_db(tmp_path, monkeypatch, graph_sessions):
    """A throwaway ledger + a real (filesystem-off) config on STATE, as the host wires at
    boot — ``sdk.run_subagent`` reads ``STATE.graph_config`` and records onto
    ``STATE.ledger_store``."""
    store = LedgerStore(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(rs.STATE, "ledger_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(filesystem_enabled=False), raising=False)

    class FakeModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    monkeypatch.setattr(agent, "create_llm", lambda *_a, **_kw: FakeModel(
        messages=iter([AIMessage(content="Done.")]), disable_streaming=True,
    ))
    monkeypatch.setattr(agent, "get_all_tools", lambda *_a, **_kw: [])

    monkeypatch.setitem(SUBAGENT_REGISTRY, "researcher", SubagentConfig(
        name="researcher", description="research", system_prompt="Research.", tools=[],
    ))
    monkeypatch.setattr(agent, "build_subagent_prompt", lambda *_a, **_kw: "Research.")

    create_agent = agent.create_agent

    def capture_graph(**kwargs):
        graph = create_agent(**kwargs)
        stream = graph.astream

        async def astream(inputs, **stream_kwargs):
            graph_sessions.append(inputs["session_id"])
            async for state in stream(inputs, **stream_kwargs):
                yield state

        monkeypatch.setattr(graph, "astream", astream)
        return graph

    monkeypatch.setattr(agent, "create_agent", capture_graph)
    return store


async def test_session_id_lands_on_the_delegation_edge(ledger_db, graph_sessions):
    # r1: an explicit session_id is stamped on the row the SDK run writes.
    await sdk.run_subagent(
        "researcher",
        "go",
        description="dig into the thing",
        extra_tools=[],
        check_prompt_tools=False,
        session_id="wf-123",
    )

    rows = ledger_db.recent()
    assert len(rows) == 1
    assert rows[0]["to_kind"] == "subagent"
    assert rows[0]["to_name"] == "researcher"
    assert rows[0]["session_id"] == "wf-123"
    assert rows[0]["outcome"] == "ok"
    assert graph_sessions == [""]


async def test_default_call_leaves_the_session_id_empty(ledger_db):
    # r2: the default keeps the prior behaviour — no session_id, empty edge.
    await sdk.run_subagent(
        "researcher",
        "go",
        description="dig into the thing",
        extra_tools=[],
        check_prompt_tools=False,
    )

    assert ledger_db.recent()[0]["session_id"] == ""


@pytest.mark.parametrize("outer", ["none", "scope", "edge"])
@pytest.mark.parametrize("resume", [False, True])
async def test_workflow_step_records_its_run_identity(ledger_db, graph_sessions, tmp_path, monkeypatch, outer, resume):
    from contextlib import nullcontext

    import plugins.workflows as wf
    from plugins.workflows.run_state import WorkflowRunStore
    from tests.test_workflows import _GatedReg, _patch_sdk

    _patch_sdk(monkeypatch, sdk.run_subagent)
    store = WorkflowRunStore(tmp_path / "runs")
    context = {
        "none": nullcontext(),
        "scope": ledger.run_scope(session_id="chat-session", run_id="chat-turn"),
        "edge": ledger.dispatch(to_kind="workflow", to_name="outer", session_id="chat-session"),
    }[outer]
    with context:
        await wf._execute(_GatedReg(), "gated", {"topic": "ai"}, run_store=store)
        if resume:
            await wf._resume(_GatedReg(), store.run_id, "approve", run_store=store)

    expected = f"workflow:{store.run_id}" if outer == "none" else "chat-session"
    rows = ledger_db.recent(session_id=expected)
    steps = [row for row in rows if row["to_kind"] == "subagent"]
    assert len(steps) == (2 if resume else 1)
    assert all(row["session_id"] == expected for row in steps)
    assert graph_sessions == [""] * len(steps)
    if outer != "none":
        workflow = next(row for row in rows if row["task_id"] == store.run_id)
        assert any(row["parent_uuid"] == workflow["edge_uuid"] for row in steps)


async def test_default_inherits_scope_without_changing_tool_state(ledger_db, graph_sessions):
    with ledger.run_scope(session_id="chat-session"):
        await sdk.run_subagent("researcher", "go", description="dig", extra_tools=[])
    assert ledger_db.recent()[0]["session_id"] == "chat-session"
    assert graph_sessions == [""]


async def test_manual_session_still_reaches_tool_state(ledger_db, graph_sessions):
    await agent.run_manual_subagent(
        rs.STATE.graph_config, subagent_type="researcher", prompt="go", description="dig",
        session_id="real-chat", extra_tools=[],
    )
    assert ledger_db.recent()[0]["session_id"] == "real-chat"
    assert graph_sessions == ["real-chat"]
