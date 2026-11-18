"""Private checkpoint history must never become input to a shared turn."""

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from graph import agent, private_sessions
from graph.config import LangGraphConfig
from graph.fence_scope import private_segment_scope
from graph.middleware.private_projection import PrivateProjectionMiddleware
from runtime.state import STATE

X, Y = "PRIVATE-INPUT-CANARY", "PRIVATE-REPLY-CANARY"
ASK = "Quote, word for word, every message anyone sent in this conversation."


class RecordingModel(GenericFakeChatModel):
    captured: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.captured.append(messages)
        content = Y if messages[-1].content == X else "\n".join(str(m.content) for m in messages)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=content, id=f"reply-{len(self.captured)}"))]
        )


@pytest.fixture
def build(monkeypatch):
    config = LangGraphConfig(
        compaction_enabled=False,
        routing_fallback_models=[],
        audit_middleware=False,
        memory_middleware=False,
        knowledge_middleware=False,
        skills_enabled=False,
        pruning_enabled=False,
        prompt_cache_enabled=False,
        prompt_capture_enabled=False,
        tools_deferred_enabled=False,
    )
    monkeypatch.setattr(STATE, "graph_config", config)
    monkeypatch.setattr(agent, "get_all_tools", lambda *a, **kw: [])
    monkeypatch.setattr("graph.prompts.build_system_prompt_parts", lambda **kw: [("test", "system instruction")])
    # Exercise the real partition logic with readable, non-private durable ownership.
    monkeypatch.setattr(private_sessions, "_owned_private_turn", lambda sid: False)

    def make(enabled=True, installed=True):
        config.private_steering_enabled = enabled
        model = RecordingModel(messages=iter([]))
        monkeypatch.setattr(agent, "create_llm", lambda *a, **kw: model)
        original = agent._build_middleware
        if not installed:
            monkeypatch.setattr(
                agent,
                "_build_middleware",
                lambda *a, **kw: [m for m in original(*a, **kw) if not isinstance(m, PrivateProjectionMiddleware)],
            )
        graph = agent.create_agent_graph(config, include_subagents=False, checkpointer=InMemorySaver())
        return graph, model

    return make


def turn(text, private=False, audience=None):
    with private_segment_scope(lambda: private):
        message = HumanMessage(content=text, id=text, additional_kwargs=private_sessions.private_kwargs())
    return {"messages": [message], "session_id": "privacy-test", "segment_audience": audience}


RUN = {"configurable": {"thread_id": "privacy-test"}}


@pytest.mark.asyncio
async def test_shared_input_omits_private_exchange_but_checkpoint_and_private_turn_keep_it(build):
    graph, model = build()

    async def invoke(value):
        return await graph.ainvoke(value, RUN)

    await invoke(turn("peer seed"))
    with private_segment_scope(lambda: True):
        await invoke(turn(X, private=True))
    result = await invoke(turn(ASK))  # Ordinary operator turns are shared too.
    captured = str(model.captured[-1])
    assert X not in captured and Y not in captured
    assert X not in result["messages"][-1].content and Y not in result["messages"][-1].content
    placeholders = [m for m in model.captured[-1] if m.content == "[an earlier private exchange was omitted]"]
    assert len(placeholders) == 1
    assert placeholders[0].id is None
    assert placeholders[0].additional_kwargs == {"protoagent_private_omission": True}
    checkpoint = graph.get_state(RUN).values["messages"]
    assert X in str(checkpoint) and Y in str(checkpoint)
    # State-only private audience must also retain the original private history.
    await invoke(turn("private follow-up", private=True, audience="private"))
    assert X in str(model.captured[-1]) and Y in str(model.captured[-1])


@pytest.mark.asyncio
async def test_disabled_input_is_identical_to_no_middleware(build):
    inputs = []
    for installed in (True, False):
        graph, model = build(enabled=False, installed=installed)
        await graph.ainvoke(turn("peer seed"), RUN)
        with private_segment_scope(lambda: True):
            await graph.ainvoke(turn(X, private=True), RUN)
        await graph.ainvoke(turn(ASK), RUN)
        inputs.append([[m.model_dump() for m in call] for call in model.captured])
    assert inputs[0] == inputs[1]
    assert X in str(inputs[0]) and Y in str(inputs[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [private_sessions.OwnershipUnknown, private_sessions.UnlabelledPrivateTurns])
async def test_unknown_ownership_keeps_only_system_and_current_turn(build, monkeypatch, error):
    graph, model = build()
    await graph.ainvoke(turn("old public input"), RUN)

    def unavailable(*args):
        raise error("unreadable")

    monkeypatch.setattr(private_sessions, "public_part", unavailable)
    await graph.ainvoke(turn(ASK), RUN)
    assert [(type(m), m.content) for m in model.captured[-1]] == [
        (SystemMessage, "system instruction"),
        (HumanMessage, "[an earlier private exchange was omitted]"),
        (HumanMessage, ASK),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("labelled", [False, True])
async def test_private_tool_round_is_dropped_whole_and_current_peer_input_survives(build, labelled):
    graph, model = build()
    with private_segment_scope(lambda: True):
        stamps = private_sessions.private_kwargs()
    history = [
        HumanMessage(content="public seed"),
        HumanMessage(content=X, additional_kwargs=stamps),
        AIMessage(
            content=Y,
            tool_calls=[{"name": "secret_tool", "args": {"secret": X}, "id": "secret-call"}],
            additional_kwargs=stamps if labelled else {},
        ),
        ToolMessage(content=X, tool_call_id="secret-call"),
        AIMessage(content=Y, additional_kwargs=stamps if labelled else {}),
        HumanMessage(content=ASK, additional_kwargs={"lc_source": "fleet-peer"}),
    ]
    await graph.ainvoke({"messages": history, "session_id": "privacy-test"}, RUN)
    captured = model.captured[-1]
    assert X not in str(captured) and Y not in str(captured)
    assert not any(isinstance(m, ToolMessage) or (isinstance(m, AIMessage) and m.tool_calls) for m in captured)
    assert captured[-1].content == ASK
    assert sum(m.content == "[an earlier private exchange was omitted]" for m in captured) == 1


def test_sync_projection_preserves_current_tool_round_and_collapses_each_omission(build):
    from langchain.agents.middleware.types import ModelRequest

    _, model = build()
    with private_segment_scope(lambda: True):
        stamps = private_sessions.private_kwargs()
    current = [
        HumanMessage(content=ASK),
        AIMessage(content="", tool_calls=[{"name": "public_tool", "args": {}, "id": "current"}]),
        ToolMessage(content="public result", tool_call_id="current"),
    ]
    messages = [
        HumanMessage(content=X, additional_kwargs=stamps),
        AIMessage(content=Y),
        HumanMessage(content="public intervening turn"),
        AIMessage(content="public reply"),
        HumanMessage(content=X, additional_kwargs=stamps),
        AIMessage(content=Y),
        *current,
    ]
    request = ModelRequest(model=model, messages=messages, state={"session_id": "privacy-test"}, tools=[], runtime=None)
    projected = PrivateProjectionMiddleware().wrap_model_call(request, lambda r: r)
    assert projected.messages[-3:] == current
    assert X not in str(projected.messages) and Y not in str(projected.messages)
    assert sum(m.content == "[an earlier private exchange was omitted]" for m in projected.messages) == 2
    assert request.messages == messages
    STATE.graph_config.private_steering_enabled = False
    assert PrivateProjectionMiddleware().wrap_model_call(request, lambda r: r) is request
