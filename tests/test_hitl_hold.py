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
