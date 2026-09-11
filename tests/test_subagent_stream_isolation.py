"""Regression: a subagent's CONTENT (and reasoning) tokens must NOT stream into the
lead turn's answer.

LangChain propagates the parent run's callbacks into the nested subagent ``ainvoke``,
so the subagent's ``on_chat_model_stream`` events bubble up onto the lead's
``astream_events`` loop in ``_run_turn_stream``. Without a guard the loop forwarded them
as ``("text"/"reasoning")`` frames — streaming the subagent's internals into the lead
answer (polluting ``accumulated_raw``) and, under ``task_batch``, interleaving every
concurrent subagent's tokens character-by-character (the garbled-output bug).

The fix suppresses forwarding for any chat-model-stream event carrying ``parent_task_id``
(a subagent run). The subagent's result still comes back via the ``task`` tool's
ToolMessage (a ``tool_end`` frame → the delegation card), which is the correct handoff —
so the secret below is expected on the tool card, just never on the lead's text stream.

Drives the real ``_run_turn_stream`` frame emitter with a fake model scripting
lead → task → subagent → lead-answer, mirroring ``test_subagent_nesting_stream``.
"""

from __future__ import annotations

import itertools
import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk


class _ToolFake(GenericFakeChatModel):
    """Replays preset AIMessages (incl. tool calls) over the STREAMING path so it drops
    into create_agent for BOTH the lead and the subagent (same patched create_llm). Emits
    one chunk carrying any tool calls as ``tool_call_chunks`` (the wire shape the agent
    re-aggregates) so an empty-content tool-call message still yields a stream event."""

    def bind_tools(self, tools, **kwargs):
        return self

    def _chunk(self):
        msg = next(self.messages)
        return ChatGenerationChunk(
            message=AIMessageChunk(
                content=msg.content,
                tool_call_chunks=[
                    {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}
                    for i, tc in enumerate(getattr(msg, "tool_calls", None) or [])
                ],
            )
        )

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        yield self._chunk()

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        # Yield control like a real model's network I/O would, so astream_events flushes
        # the detached subagent's events in real-time order (not a sync burst).
        import asyncio

        await asyncio.sleep(0)
        chunk = self._chunk()
        await asyncio.sleep(0)
        yield chunk


def _install(monkeypatch, messages):
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver

    # Pad with a no-tool-call finisher forever so an extra lead/subagent step ends cleanly.
    stream = itertools.chain(iter(messages), itertools.repeat(AIMessage(content="<output>done</output>")))
    fake = _ToolFake(messages=stream)
    # Persist the fake for the whole turn — the subagent builds ITS model lazily in
    # _run_subagent, so a patch that exits after construction would miss it.
    monkeypatch.setattr("graph.agent.create_llm", lambda *a, **k: fake)
    from graph.agent import create_agent_graph

    g = create_agent_graph(LangGraphConfig(), include_subagents=True, checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)
    return g


def _delegate(**args):
    return AIMessage(
        content="",
        tool_calls=[{"name": "task", "args": args, "id": "t1", "type": "tool_call"}],
    )


@pytest.mark.asyncio
async def test_subagent_content_does_not_leak_into_lead_stream(monkeypatch):
    from server.chat import _run_turn_stream

    sub_secret = "SUBAGENT_INTERNAL_DRAFT_XYZ"
    lead_answer = "LEAD_FINAL_ANSWER_ABC"
    _install(
        monkeypatch,
        [
            _delegate(description="research a topic", prompt="go research", subagent_type="researcher"),
            # subagent's only turn → produces content (its draft/answer). THIS is what
            # leaked into the lead view in the bug — it must stay out of the text stream.
            AIMessage(content=sub_secret),
            # lead's turn 2 → its real answer (streams as normal).
            AIMessage(content=lead_answer),
        ],
    )

    text_frames: list[str] = []
    reasoning_frames: list[str] = []
    tool_outputs: list[str] = []
    async for kind, payload in _run_turn_stream(
        "delegate then answer", "iso1", {"configurable": {"thread_id": "iso1"}}
    ):
        if kind == "text":
            text_frames.append(payload)
        elif kind == "reasoning":
            reasoning_frames.append(payload)
        elif kind == "tool_end":
            tool_outputs.append(str(payload.get("output", "")))

    streamed = "".join(text_frames)
    # The lead's own answer still streams to the user.
    assert lead_answer in streamed, f"lead answer should stream; saw {streamed!r}"
    # …but the subagent's internal content must NOT appear in the lead's text or
    # reasoning stream (this assertion fails on the pre-fix code).
    assert sub_secret not in streamed, f"subagent content leaked into the lead text stream: {streamed!r}"
    assert sub_secret not in "".join(reasoning_frames), "subagent content leaked into the lead reasoning stream"
    # The subagent's result is still delivered — as the `task` tool result (delegation
    # card), which is the correct handoff path, not the lead's answer stream.
    assert any(sub_secret in out for out in tool_outputs), (
        f"subagent result should return via the task tool card; tool outputs: {tool_outputs}"
    )


# ── A model call a TOOL makes — detached or not — never speaks for the lead (#3439) ──
#
# Work a tool spawns runs in a copy of the tool's context (plugins/delegates, #3016:
# "LangChain run context and all"), so its model calls — a background ingest's
# describe/enrich passes, a plugin's spawn_work running sdk.complete — keep reporting
# into the spawning turn's astream_events while the lead is still answering. Their
# events run in the TOOL node. Two ways that reached the answer:
#   - a streaming one leaked its tokens mid-sentence ("I started the
#     IMAGE-DESCRIPTIONingest…") — pre-existing;
#   - a quiet one merely STARTING made the lead's next delta open a paragraph break
#     mid-sentence, once #3439 separated each model call's narration.

_ANSWER = "I started the ingest and it will be searchable in a minute or two once indexed."


class _SlowLead(GenericFakeChatModel):
    """The lead: a tool call as one chunk, then its answer word by word, slowly — so a
    detached call lands mid-sentence."""

    def bind_tools(self, tools, **kwargs):
        return self

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        import asyncio

        msg = next(self.messages)
        calls = getattr(msg, "tool_calls", None) or []
        if calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}
                        for i, tc in enumerate(calls)
                    ],
                )
            )
            return
        for i, word in enumerate(msg.content.split(" ")):
            await asyncio.sleep(0.05)
            yield ChatGenerationChunk(message=AIMessageChunk(content=word if i == 0 else f" {word}"))


def _aux_model(*, streams: bool):
    """An in-process helper model (describe/enrich/complete), quiet or token-streaming."""
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult

    class _Aux(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "aux-fake"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="IMAGE-DESCRIPTION"))])

    class _StreamingAux(_Aux):
        def _stream(self, messages, stop=None, run_manager=None, **kwargs):
            for token in ("IMAGE-", "DESCRIPTION"):
                chunk = ChatGenerationChunk(message=AIMessageChunk(content=token))
                if run_manager:
                    run_manager.on_llm_new_token(token, chunk=chunk)
                yield chunk

    return _StreamingAux() if streams else _Aux()


@pytest.mark.parametrize(
    ("how", "streams"),
    [("on-the-loop", False), ("in-a-thread", False), ("in-a-thread", True)],
)
@pytest.mark.asyncio
async def test_a_model_call_detached_from_a_tool_never_enters_the_lead_answer(monkeypatch, how, streams):
    import asyncio

    import runtime.state as rs
    from graph.config import LangGraphConfig
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import MemorySaver

    from server.chat import _run_turn_stream

    aux = _aux_model(streams=streams)
    spawned: list = []

    @tool
    async def ingest_in_background(source: str) -> str:
        """Start a background ingest."""

        async def _work():
            await asyncio.sleep(0.2)  # lands while the lead is mid-sentence
            if how == "on-the-loop":
                await aux.ainvoke([HumanMessage("situate this chunk")])
            else:
                await asyncio.to_thread(aux.invoke, [HumanMessage("situate this chunk")])

        spawned.append(asyncio.create_task(_work()))  # the spawn_work shape
        return "Ingest started in the background."

    lead = _SlowLead(
        messages=itertools.chain(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "ingest_in_background", "args": {"source": "x"}, "id": "c1", "type": "tool_call"}],
                ),
                AIMessage(content=_ANSWER),
            ],
            itertools.repeat(AIMessage(content="(extra step)")),
        )
    )
    monkeypatch.setattr("graph.agent.create_llm", lambda *a, **k: lead)
    from graph.agent import create_agent_graph

    graph = create_agent_graph(
        LangGraphConfig(), include_subagents=False, checkpointer=MemorySaver(), extra_tools=[ingest_in_background]
    )
    monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)

    streamed, raw = "", None
    async for kind, payload in _run_turn_stream("ingest x", f"det-{how}", {"configurable": {"thread_id": f"det-{how}"}}):
        if kind == "text":
            streamed += payload
        elif kind == "__raw__":
            raw = payload
    await asyncio.gather(*spawned)
    assert raw == _ANSWER, f"the lead's answer was altered by a call a tool made: {raw!r}"
    assert streamed == _ANSWER


def _word_streamer(messages, *, delay=0.0):
    """A scripted model that streams each reply word by word AND reports every token to its
    callbacks — the shape a graph run from inside a tool needs for its tokens to surface on
    the lead's astream_events (the leak these tests pin shut)."""

    class _Streamer(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            import asyncio

            msg = next(self.messages)
            calls = getattr(msg, "tool_calls", None) or []
            for i, word in enumerate(msg.content.split(" ") if msg.content else []):
                await asyncio.sleep(delay)
                token = word if i == 0 else f" {word}"
                chunk = ChatGenerationChunk(message=AIMessageChunk(content=token))
                if run_manager:
                    await run_manager.on_llm_new_token(token, chunk=chunk)
                yield chunk
            if calls:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        tool_call_chunks=[
                            {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}
                            for i, tc in enumerate(calls)
                        ],
                    )
                )

    return _Streamer(messages=itertools.chain(iter(messages), itertools.repeat(AIMessage(content="(extra step)"))))


def _install_lead(monkeypatch, lead, *, tools=(), config=None):
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver

    cfg = config or LangGraphConfig()
    monkeypatch.setattr("graph.agent.create_llm", lambda *a, **k: lead)
    from graph.agent import create_agent_graph

    graph = create_agent_graph(cfg, include_subagents=False, checkpointer=MemorySaver(), extra_tools=list(tools))
    monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)


async def _raw_answer(message, session):
    from server.chat import _run_turn_stream

    streamed, raw = "", None
    async for kind, payload in _run_turn_stream(message, session, {"configurable": {"thread_id": session}}):
        if kind == "text":
            streamed += payload
        elif kind == "__raw__":
            raw = payload
    assert streamed == raw, "the stream and the canonical text must be one string"
    return raw


def _call(tool_name, **args):
    return AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": "c1", "type": "tool_call"}])


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.asyncio
async def test_a_graph_a_tool_runs_never_enters_the_lead_answer(monkeypatch, detached):
    """A graph a tool runs reports its OWN node ("model") and carries no parent_task_id —
    only the checkpoint namespace (``tools:<id>|model:<id>``) says it ran under a tool."""
    import asyncio

    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import tool

    inner = create_agent(model=_word_streamer([AIMessage(content="STEP-INTERNAL-NOTES")], delay=0.03), tools=[])
    spawned: list = []

    @tool
    async def run_steps(goal: str) -> str:
        """Run a recipe over an inner agent."""
        if not detached:
            result = await inner.ainvoke({"messages": [HumanMessage(goal)]})
            return str(result["messages"][-1].content)

        async def _work():
            await asyncio.sleep(0.2)  # lands while the lead is mid-sentence
            await inner.ainvoke({"messages": [HumanMessage(goal)]})

        spawned.append(asyncio.create_task(_work()))
        return "started"

    _install_lead(
        monkeypatch,
        _SlowLead(messages=itertools.chain([_call("run_steps", goal="g"), AIMessage(content=_ANSWER)], itertools.repeat(AIMessage(content="(extra)")))),
        tools=[run_steps],
    )
    raw = await _raw_answer("go", f"nested-{detached}")
    await asyncio.gather(*spawned)
    assert raw == _ANSWER


@pytest.mark.asyncio
async def test_parallel_workflow_steps_never_enter_the_lead_answer(monkeypatch):
    """The workflows plugin's shape: a tool runs steps through ``sdk.run_subagent`` (no
    parent_task_id), two of them concurrently. Their tokens used to interleave into the
    answer — and, with a paragraph per model run, every alternation opened a paragraph."""
    import asyncio

    from graph import agent as agent_mod
    from graph import sdk
    from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
    from langchain_core.tools import tool

    monkeypatch.setitem(
        SUBAGENT_REGISTRY, "wfstep", SubagentConfig(name="wfstep", description="a step", system_prompt="Do it.", tools=[])
    )

    @tool
    async def run_workflow(name: str) -> str:
        """Run a two-step parallel workflow."""
        a, b = await asyncio.gather(
            sdk.run_subagent("wfstep", "step a", description=f"{name}:a", extra_tools=[]),
            sdk.run_subagent("wfstep", "step b", description=f"{name}:b", extra_tools=[]),
        )
        return f"{a}\n{b}"

    _install_lead(
        monkeypatch,
        _word_streamer([_call("run_workflow", name="w"), AIMessage(content="The workflow finished.")], delay=0.001),
        tools=[run_workflow],
    )
    monkeypatch.setattr(agent_mod, "get_all_tools", lambda *a, **k: [])
    steps = iter(
        [
            _word_streamer([AIMessage(content="AAA1 AAA2 AAA3 AAA4 AAA5")], delay=0.01),
            _word_streamer([AIMessage(content="BBB1 BBB2 BBB3 BBB4 BBB5")], delay=0.01),
        ]
    )
    monkeypatch.setattr(agent_mod, "create_llm", lambda *a, **k: next(steps))
    assert await _raw_answer("run it", "wf-steps") == "The workflow finished."


@pytest.mark.asyncio
async def test_the_compaction_summary_never_enters_the_answer(monkeypatch):
    """Compaction (on by default) summarizes old history with its own model call, in a
    middleware node — neither a tool nor a subagent. langchain marks it internal; its text
    used to stream into the answer and the stored turn."""
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig(compaction_enabled=True, compaction_trigger="messages:3", compaction_keep_messages=1)
    shared = _word_streamer(
        [AIMessage(content="First answer."), AIMessage(content="SUMMARY-OF-OLD-HISTORY"), AIMessage(content="Second answer.")]
    )
    _install_lead(monkeypatch, shared, config=cfg)
    assert await _raw_answer("hello", "compact") == "First answer."
    assert await _raw_answer("again", "compact") == "Second answer."
