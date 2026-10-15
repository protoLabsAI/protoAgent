"""Operational projection survives disabling knowledge and skills in a real graph."""
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from graph.context_frame import is_context_frame
from graph.middleware.knowledge import KnowledgeMiddleware, TURN_PROJECTION_KEY

pytestmark = pytest.mark.platform_sensitive


@pytest.mark.asyncio
@pytest.mark.parametrize("incognito", [False, True])
@pytest.mark.parametrize("knowledge_enabled", [False, True])
async def test_real_graph_delivers_one_live_state_without_checkpointing(monkeypatch, tmp_path, incognito, knowledge_enabled):
    import graph.agent as agent
    from graph.config import LangGraphConfig
    from runtime.state import STATE

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    goal = SimpleNamespace(status="active", iteration=1, max_iterations=4, condition="goal-fixture")
    monkeypatch.setattr(STATE, "goal_controller", SimpleNamespace(
        active_goal=lambda sid: goal,
        _store=SimpleNamespace(read_plan=lambda sid: "plan-fixture")))
    monkeypatch.setattr(STATE, "tasks_store", SimpleNamespace(list=lambda **kw: [
        dict(id="task-fixture", status="open", priority=1, title="task-title")]))
    monkeypatch.setattr(STATE, "watch_controller", SimpleNamespace(list_watches=lambda: [
        SimpleNamespace(status="active", status_line=lambda: "watch-fixture")]))
    monkeypatch.setattr(STATE, "scheduler", SimpleNamespace(list_jobs=lambda: [
        SimpleNamespace(id="schedule-fixture", next_fire="2099-01-01", prompt="scheduled-work")]))
    monkeypatch.setattr("graph.work_providers.collect_work_sections", lambda cap: [("PLUGIN WORK", ["plugin-fixture"])])
    # No prior-session recall may be activated by the newly installed middleware.
    digest_reads = []
    def forbidden(*a, **kw):
        digest_reads.append(True)
        raise AssertionError("prior sessions read with knowledge and skills disabled")
    if not knowledge_enabled:
        monkeypatch.setattr(KnowledgeMiddleware, "_cached_digest", forbidden)

    seen = []
    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self
        async def _agenerate(self, messages, **kwargs):
            seen.append(messages)
            return await super()._agenerate(messages, **kwargs)

    monkeypatch.setattr(agent, "create_llm", lambda *a, **kw: Model(
        messages=iter([AIMessage(content="done"), AIMessage(content="done")]), disable_streaming=True))
    cfg = LangGraphConfig(knowledge_middleware=knowledge_enabled, skills_enabled=False,
                          memory_middleware=False, compaction_enabled=False, audit_middleware=False,
                          context_prior_sessions="off" if knowledge_enabled else "newest")
    store = SimpleNamespace(get_hot_memory=lambda **kw: "memory-fixture", search=lambda *a, **kw: [])
    graph = agent.create_agent_graph(cfg, knowledge_store=store, checkpointer=InMemorySaver())
    assert graph.config["recursion_limit"] == cfg.max_iterations
    run = {"configurable": {"thread_id": "fixture"}}
    for _ in range(2):
        await graph.ainvoke({"messages": [HumanMessage(content="status")], "session_id": "fixture", "incognito": incognito}, run)
    for messages in seen:
        frames = [m for m in messages if is_context_frame(m) and "<working_state>" in str(m.content)]
        assert len(frames) == 1
        text = str(frames[0].content)
        assert text.count("<working_state>") == 1
        for marker in ("goal-fixture", "plan-fixture", "task-fixture", "watch-fixture", "schedule-fixture", "plugin-fixture"):
            assert marker in text
        assert ("memory-fixture" in text) == (knowledge_enabled and not incognito)
    assert digest_reads == []
    assert [m.content for m in seen[0] if m.type == "system"] == [m.content for m in seen[1] if m.type == "system"]
    checkpoint = await graph.aget_state(run)
    assert TURN_PROJECTION_KEY not in checkpoint.values
    assert not any(is_context_frame(m) for m in checkpoint.values["messages"])
    assert all("working_state" not in text for _, text in graph.system_prompt_parts)
    from langgraph.errors import GraphRecursionError
    with pytest.raises(GraphRecursionError):
        await graph.ainvoke({"messages": [HumanMessage(content="bounded")], "session_id": "bounded"},
                            {"configurable": {"thread_id": "bounded"}, "recursion_limit": 1})


def test_model_override_cannot_reenable_prior_recall():
    from graph.config import LangGraphConfig
    from graph.projection import ProjectionOptions
    cfg = LangGraphConfig(context_prior_sessions="newest")
    mw = KnowledgeMiddleware(None, prior_sessions_enabled=False, config=cfg,
                             options=ProjectionOptions.from_config(cfg))
    for state in ({}, {"model": "protolabs:protolabs/fast"}):
        assert mw._with_bound_tools(mw._options(state)).prior_sessions_policy == "off"
