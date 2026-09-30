"""HITL hold (#1560) — while a form/question/approval interrupt is pending, fresh
operator messages are HELD (queued, not delivered) until the form resolves.

Why the hold lives at TURN ENTRY (server.chat) and not in SteeringMiddleware: while
the graph is parked at an ``interrupt()`` it makes no model calls, so the fold seam
never runs — the interleaving actually happened when a fresh message invoked the
parked thread, which LangGraph treats as "abandon the interrupt and continue"
(dangling tool_call, form unresolvable, message seen BEFORE the form answer).
``_hold_if_hitl_pending`` intercepts that: unmarked messages are parked in the
steering queue and the turn re-parks on the same payload; the marked answer
(``hitl_resume``) becomes a real ``Command(resume=…)``. Held messages then fold in
via ``SteeringMiddleware`` at the first model call after the resume — i.e.
immediately AFTER the form response, in arrival order.
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph import steering
from runtime.state import STATE

# `server.chat` the attribute is shadowed by the re-exported `chat` function in
# server/__init__.py, so resolve the actual submodule from sys.modules.
chat_mod = importlib.import_module("server.chat")


class _ToolFake(GenericFakeChatModel):
    """Fake chat model that supports bind_tools (returns itself) so it drops into
    create_agent and replays preset AIMessages, including tool calls.

    The streaming turn driver consumes the model via ``astream_events``, but the
    stock ``GenericFakeChatModel._stream`` drops ``tool_calls`` (it only chunks
    content / additional_kwargs) and yields NOTHING for an empty-content tool-call
    message ("No generations found in stream"). Override ``_astream`` to emit one
    chunk carrying the full message, tool calls included."""

    def bind_tools(self, tools, **kwargs):
        return self

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        import json

        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk

        message = next(self.messages)
        tool_call_chunks = [
            {
                "name": tc["name"],
                "args": json.dumps(tc["args"]),
                "id": tc["id"],
                "index": i,
                "type": "tool_call_chunk",
            }
            for i, tc in enumerate(getattr(message, "tool_calls", []) or [])
        ]
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=message.content or "", tool_call_chunks=tool_call_chunks)
        )


_FORM_STEPS = [{"schema": {"type": "object", "properties": {"env": {"type": "string"}}, "required": ["env"]}}]


def _form_call(call_id: str = "c1") -> AIMessage:
    """An assistant turn that opens the real ``request_user_input`` form (which
    parks the graph at a LangGraph interrupt)."""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "request_user_input",
                "args": {"title": "Pick env", "steps": _FORM_STEPS},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _install_graph(monkeypatch, messages):
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver

    fake = _ToolFake(messages=iter(messages))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        g = create_agent_graph(LangGraphConfig(), include_subagents=False, checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)
    return g


def _cfg(session_id: str) -> dict:
    return {"configurable": {"thread_id": f"a2a:{session_id}"}}


async def _frames(message: str, session_id: str, *, request_metadata=None):
    return [
        frame
        async for frame in chat_mod._chat_langgraph_stream(message, session_id, request_metadata=request_metadata)
    ]


async def _history(session_id: str) -> list:
    snap = await STATE.graph.aget_state(_cfg(session_id))
    return list(snap.values.get("messages", []))


@pytest.fixture(autouse=True)
def _clear_queue():
    steering._QUEUES.clear()
    yield
    steering._QUEUES.clear()


# ── held while the form is pending ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_message_held_while_form_pending(monkeypatch):
    sid = "hold-1"
    _install_graph(monkeypatch, [_form_call(), AIMessage(content="unused")])

    frames = await _frames("deploy the service", sid)
    assert frames[-1][0] == "input_required"
    form = frames[-1][1]
    assert form.get("kind") == "form" and form.get("title") == "Pick env"

    # A message typed while the form is open: NOT delivered — held in the steering
    # queue, and the turn re-parks on the SAME form payload (no model call, no text
    # frames, nothing folded into the thread).
    held = await _frames("also make it blue", sid)
    assert held == [("input_required", form)]
    assert steering.pending(sid) == 1

    history = await _history(sid)
    assert not any(isinstance(m, HumanMessage) and "also make it blue" in str(m.content) for m in history)
    # The interrupt is still pending — the form can still be answered.
    assert await chat_mod._pending_interrupt_value(_cfg(sid)) is not None


# ── released in order, AFTER the form response, on submit ─────────────────────


@pytest.mark.asyncio
async def test_held_messages_fold_in_order_after_submit(monkeypatch):
    sid = "hold-2"
    _install_graph(monkeypatch, [_form_call(), AIMessage(content="Deployed to staging.")])

    await _frames("deploy the service", sid)
    await _frames("first note while form open", sid)
    await _frames("second note while form open", sid)
    assert steering.pending(sid) == 2

    # The operator submits the form: the console marks the answer with hitl_resume,
    # which resumes the parked interrupt (a real Command resume — the tool RETURNS).
    frames = await _frames('{"env": "staging"}', sid, request_metadata={"hitl_resume": True})
    assert any(kind == "done" for kind, _ in frames)
    assert steering.pending(sid) == 0  # released — nothing left queued

    history = await _history(sid)
    tool_idx = next(
        i for i, m in enumerate(history) if isinstance(m, ToolMessage) and "staging" in str(m.content)
    )
    fold = next(
        (i, m)
        for i, m in enumerate(history)
        if isinstance(m, HumanMessage) and "first note while form open" in str(m.content)
    )
    fold_idx, fold_msg = fold
    final_idx = max(i for i, m in enumerate(history) if isinstance(m, AIMessage) and m.content)

    # The form response (the tool result) comes FIRST; the held messages fold in
    # right after it, before the final answer — and keep their arrival order.
    assert tool_idx < fold_idx < final_idx
    content = str(fold_msg.content)
    assert "second note while form open" in content
    assert content.index("first note while form open") < content.index("second note while form open")


# ── released on cancel/dismiss (no deadlock, nothing dropped) ─────────────────


@pytest.mark.asyncio
async def test_held_messages_released_on_dismiss(monkeypatch):
    sid = "hold-3"
    _install_graph(monkeypatch, [_form_call(), AIMessage(content="Proceeding without the form.")])

    await _frames("deploy the service", sid)
    await _frames("note sent while form open", sid)
    assert steering.pending(sid) == 1

    # The operator DISMISSES the form (the console's ✕): also a marked resume — the
    # tool returns the dismissal sentinel and the turn completes; held messages are
    # released right after it. Nothing is dropped, nothing deadlocks.
    dismissal = "[dismissed] The operator dismissed this request without providing input."
    frames = await _frames(dismissal, sid, request_metadata={"hitl_resume": True})
    assert any(kind == "done" for kind, _ in frames)
    assert steering.pending(sid) == 0

    history = await _history(sid)
    tool_idx = next(i for i, m in enumerate(history) if isinstance(m, ToolMessage) and "[dismissed]" in str(m.content))
    fold_idx = next(
        i for i, m in enumerate(history) if isinstance(m, HumanMessage) and "note sent while form open" in str(m.content)
    )
    assert tool_idx < fold_idx
    assert await chat_mod._pending_interrupt_value(_cfg(sid)) is None  # the pause is resolved


# ── no pending form ⇒ behavior unchanged ──────────────────────────────────────


@pytest.mark.asyncio
async def test_no_pending_form_leaves_turns_untouched(monkeypatch):
    sid = "hold-4"
    _install_graph(monkeypatch, [AIMessage(content="plain answer"), AIMessage(content="second answer")])

    frames = await _frames("hello", sid)
    assert not any(kind == "input_required" for kind, _ in frames)
    assert any(kind == "done" for kind, _ in frames)
    assert steering.pending(sid) == 0  # nothing was queued

    # A stray hitl_resume marker with NO pending interrupt degrades to a normal
    # fresh turn (never an error, never held).
    frames = await _frames("hello again", sid, request_metadata={"hitl_resume": True})
    assert any(kind == "done" for kind, _ in frames)
    assert any(isinstance(m, HumanMessage) and "hello again" in str(m.content) for m in await _history(sid))


# ── restart with a pending form can't strand the flow ─────────────────────────


@pytest.mark.asyncio
async def test_restart_with_pending_form_still_resumes(monkeypatch):
    sid = "hold-5"
    _install_graph(monkeypatch, [_form_call(), AIMessage(content="Deployed.")])

    await _frames("deploy the service", sid)
    await _frames("note before the restart", sid)

    # Simulated restart: the in-memory steering queue is gone; the pending-form
    # state lives in the DURABLE checkpoint (re-read on every turn), so the hold
    # cannot latch shut — the form still resumes and the thread completes.
    steering._QUEUES.clear()
    frames = await _frames('{"env": "prod"}', sid, request_metadata={"hitl_resume": True})
    assert any(kind == "done" for kind, _ in frames)
    assert await chat_mod._pending_interrupt_value(_cfg(sid)) is None


# ── the non-streaming path (/api/chat desktop fallback) mirrors the contract ──


@pytest.mark.asyncio
async def test_nonstreaming_chat_holds_and_resumes(monkeypatch):
    sid = "hold-6"
    _install_graph(monkeypatch, [_form_call(), AIMessage(content="Deployed to prod.")])

    out = await chat_mod.chat("deploy the service", sid)
    assert "Input needed" in out[0]["content"]  # parked on the form

    out = await chat_mod.chat("typed while form open", sid)
    assert "queued" in out[0]["content"]  # held, with an honest ack
    assert steering.pending(sid) == 1
    assert await chat_mod._pending_interrupt_value(_cfg(sid)) is not None  # form untouched

    out = await chat_mod.chat('{"env": "prod"}', sid, hitl_resume=True)
    assert out[0]["content"] == "Deployed to prod."
    assert steering.pending(sid) == 0
    history = await _history(sid)
    tool_idx = next(i for i, m in enumerate(history) if isinstance(m, ToolMessage) and "prod" in str(m.content))
    fold_idx = next(
        i for i, m in enumerate(history) if isinstance(m, HumanMessage) and "typed while form open" in str(m.content)
    )
    assert tool_idx < fold_idx


# ── parallel gated tool calls: several interrupts pend at once, drain by id ───


def _two_form_calls() -> AIMessage:
    """One assistant turn calling ``request_user_input`` TWICE — the tool node runs a
    turn's tool calls concurrently, so BOTH interrupts pend at once. Resuming bare
    (no interrupt id) in that state is a hard LangGraph RuntimeError."""

    def call(cid: str, title: str) -> dict:
        return {
            "name": "request_user_input",
            "args": {"title": title, "steps": _FORM_STEPS},
            "id": cid,
            "type": "tool_call",
        }

    return AIMessage(content="", tool_calls=[call("c1", "Pick env"), call("c2", "Pick region")])


async def _pending_count(session_id: str) -> int:
    snap = await STATE.graph.aget_state(_cfg(session_id))
    pend = list(getattr(snap, "interrupts", None) or [])
    if not pend:
        for t in getattr(snap, "tasks", ()) or ():
            pend.extend(getattr(t, "interrupts", ()) or ())
    return len(pend)


@pytest.mark.asyncio
async def test_parallel_interrupts_drain_one_at_a_time_by_id(monkeypatch):
    sid = "multi-1"
    _install_graph(monkeypatch, [_two_form_calls(), AIMessage(content="Both picked.")])

    # Turn 1: both gated tools interrupt concurrently — TWO pending; the first surfaces.
    frames = await _frames("configure the deploy", sid)
    assert frames[-1][0] == "input_required"
    assert frames[-1][1].get("title") == "Pick env"
    assert await _pending_count(sid) == 2

    # Answer 1 resumes exactly the surfaced interrupt BY ID — a bare resume here is
    # "RuntimeError: When there are multiple pending interrupts, you must specify the
    # interrupt id". The still-unanswered second interrupt surfaces on the next pass.
    frames = await _frames('{"env": "prod"}', sid, request_metadata={"hitl_resume": True})
    assert frames[-1][0] == "input_required"
    assert frames[-1][1].get("title") == "Pick region"

    # Answer 2 drains the last interrupt; the turn completes normally.
    frames = await _frames('{"env": "us-east-1"}', sid, request_metadata={"hitl_resume": True})
    assert any(kind == "done" for kind, _ in frames)
    assert await chat_mod._pending_interrupt_value(_cfg(sid)) is None


# ── #3930 M2: continuing an input-required TASK whose interrupt is already gone ──


@pytest.mark.asyncio
async def test_resume_with_no_pending_interrupt_runs_as_a_fresh_message(monkeypatch):
    """A message continuing an input-required task arrives with resume=True (the
    executor's reading of the TASK). If the THREAD's interrupt was already answered
    elsewhere — the /api/chat fallback, or a fresh task before #3930 — a
    ``Command(resume=…)`` is a silent LangGraph no-op: the operator's text vanished and
    the task completed empty. It must run as the fresh message it now is."""
    sid = "orphan-1"
    _install_graph(
        monkeypatch, [_form_call(), AIMessage(content="Deployed to prod."), AIMessage(content="You said banana.")]
    )
    await _frames("deploy the service", sid)
    out = await chat_mod.chat('{"env": "prod"}', sid, hitl_resume=True)  # answered elsewhere
    assert out[0]["content"] == "Deployed to prod."
    assert await chat_mod._pending_interrupt_value(_cfg(sid)) is None

    frames = [
        frame
        async for frame in chat_mod._chat_langgraph_stream(
            "banana", sid, resume=True, request_metadata={"hitl_resume": True}
        )
    ]
    assert ("done", "You said banana.") in frames
    assert any(isinstance(m, HumanMessage) and "banana" in str(m.content) for m in await _history(sid))


@pytest.mark.asyncio
async def test_answer_to_an_orphaned_input_required_task_reaches_the_agent(monkeypatch):
    """The same through the real A2A executor + SDK: a task parked, its interrupt was then
    answered by the non-streaming fallback (leaving the TASK input-required), and the
    console answers the task by id. The text reaches the agent and settles the task."""
    from a2a.server.context import ServerCallContext
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.tasks import InMemoryPushNotificationConfigStore, InMemoryTaskStore
    from a2a.types import AgentSkill, Message, Part, Role, SendMessageRequest, TaskState

    import protolabs_a2a as pa
    from a2a_impl import hitl_routing
    from a2a_impl.executor import ProtoAgentExecutor

    sid = "orphan-2"
    _install_graph(
        monkeypatch, [_form_call(), AIMessage(content="Deployed to prod."), AIMessage(content="You said banana.")]
    )
    card = pa.build_agent_card(
        name="t", description="d", url="http://t/a2a", version="0.0.0",
        skills=[AgentSkill(id="chat", name="chat", description="d", tags=["chat"])], bearer=False,
    )
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(chat_mod._chat_langgraph_stream),
        task_store=InMemoryTaskStore(),
        agent_card=card,
        push_config_store=InMemoryPushNotificationConfigStore(),
    )
    router = hitl_routing.install_parked_task_routing(handler)
    call = ServerCallContext()

    def msg(text, mid, task_id=""):
        m = Message(message_id=mid, context_id=sid, role=Role.ROLE_USER, parts=[Part(text=text)])
        if task_id:
            m.task_id = task_id
        m.metadata.update({"hitl_resume": True} if task_id else {})
        return SendMessageRequest(message=m)

    try:
        parked = await handler.on_message_send(msg("deploy the service", "m1"), call)
        assert parked.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        await chat_mod.chat('{"env": "prod"}', sid, hitl_resume=True)  # the fallback answered it

        answer = await handler.on_message_send(msg("banana", "m2", task_id=parked.id), call)
        await router.drain()
        assert answer.id == parked.id
        assert answer.status.state == TaskState.TASK_STATE_COMPLETED
        text = "".join(p.text for a in answer.artifacts for p in a.parts)
        assert "You said banana." in text
        assert any(isinstance(m, HumanMessage) and "banana" in str(m.content) for m in await _history(sid))
    finally:
        hitl_routing._ROUTER[0] = None
