"""Streaming / non-streaming turn-driver parity (#3805).

``server/chat.py`` has two turn entry points — the streaming
``_chat_langgraph_stream_impl`` (A2A / console) and the non-streaming
``_chat_langgraph_impl`` behind ``chat()`` (OpenAI-compat ``/v1``, ``/api/chat``,
plugin surfaces). Each used to carry its own copy of the pre-turn dispatch chain and
the error handling, and the non-streaming copy drifted: it never learned the
context-overflow compact-and-retry or the ``/<subagent>`` slash command. Both now run
ONE shared chain (``_pre_turn_dispatch``) and ONE failure classifier; these pin the
behaviours the non-streaming path was missing, through ``chat()`` itself.
"""

from __future__ import annotations

import importlib

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from graph.config import LangGraphConfig

# The module, not the `chat` function `server/__init__` re-exports under the same name.
chat_mod = importlib.import_module("server.chat")
# The room exchange lives in server.chat_rooms (#3838) — patch it there.
rooms_mod = importlib.import_module("server.chat_rooms")
# The HITL hold lives in server.turn_control (#3847) — patch it there.
turn_control = importlib.import_module("server.turn_control")

_OVERFLOW = "Error code: 400 - This model's maximum context length is 128000 tokens."


class _FakeGraph:
    """Just the graph surface the non-streaming driver touches. ``outcomes`` is
    consumed one per ``ainvoke``: an exception is raised, anything else returned."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.inputs: list = []
        self.recorded: list = []

    async def ainvoke(self, graph_input, config=None):
        self.inputs.append(graph_input)
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    async def aupdate_state(self, config, values):
        self.recorded.append(values)

    async def aget_state(self, config):  # pragma: no cover — no interrupt is ever pending here
        raise AssertionError("not expected")


def _answer(text: str) -> dict:
    return {"messages": [HumanMessage(content="q"), AIMessage(content=text)]}


@pytest.fixture
def graph(monkeypatch):
    def _install(outcomes):
        g = _FakeGraph(outcomes)
        monkeypatch.setattr(chat_mod.STATE, "graph", g, raising=False)
        monkeypatch.setattr(chat_mod.STATE, "goal_controller", None, raising=False)
        monkeypatch.setattr(chat_mod.STATE, "graph_config", LangGraphConfig(), raising=False)

        async def _no_hold(*a, **k):
            return None

        monkeypatch.setattr(turn_control, "_hold_if_hitl_pending", _no_hold)
        return g

    return _install


@pytest.fixture
def compaction(monkeypatch):
    """Spy on the force-compaction; ``result`` is what it reports (did it shrink?)."""
    calls: list[tuple[str, str]] = []
    state = {"result": True}

    async def _fake(thread_id, session_id):
        calls.append((thread_id, session_id))
        return state["result"]

    monkeypatch.setattr(chat_mod, "_force_compact_for_overflow", _fake)
    return calls, state


@pytest.mark.asyncio
async def test_nonstreaming_overflow_compacts_and_retries_once(graph, compaction):
    calls, _ = compaction
    g = graph([ValueError(_OVERFLOW), _answer("recovered")])

    out = await chat_mod.chat("hello", "s-overflow")

    assert out[0]["content"] == "recovered"
    assert "error" not in out[0]
    assert calls == [(chat_mod._resolve_thread_id(None, "s-overflow"), "s-overflow")]
    assert len(g.inputs) == 2
    # The retry runs the recovery prompt, not the operator's (already-checkpointed) message.
    assert g.inputs[1]["messages"][0].content == chat_mod._OVERFLOW_RETRY_PROMPT
    assert g.recorded == []  # a recovered turn is not a failed one


@pytest.mark.asyncio
async def test_nonstreaming_overflow_retries_only_once(graph, compaction):
    calls, _ = compaction
    g = graph([ValueError(_OVERFLOW), ValueError(_OVERFLOW + " (again)")])

    out = await chat_mod.chat("hello", "s-twice")

    assert len(calls) == 1 and len(g.inputs) == 2
    assert out[0]["content"].startswith("**Error:**") and "(again)" in out[0]["content"]
    assert out[0]["error"]["exception"] == "ValueError"
    assert len(g.recorded) == 1  # the SECOND failure is the one recorded (#2593)


@pytest.mark.asyncio
async def test_nonstreaming_overflow_without_a_shrink_does_not_retry(graph, compaction):
    calls, state = compaction
    state["result"] = False
    g = graph([ValueError(_OVERFLOW)])

    out = await chat_mod.chat("hello", "s-noshrink")

    assert len(calls) == 1 and len(g.inputs) == 1
    assert "maximum context length" in out[0]["content"]


@pytest.mark.asyncio
async def test_nonstreaming_other_errors_never_compact(graph, compaction):
    calls, _ = compaction
    graph([ValueError("invalid api key")])

    out = await chat_mod.chat("hello", "s-other")

    assert calls == []
    assert out[0]["content"] == "**Error:** invalid api key"


@pytest.mark.asyncio
async def test_subagent_slash_command_runs_via_chat(graph, monkeypatch):
    g = graph([])
    ran: list[tuple] = []

    async def _fake_run(sub_type, prompt, *, session_id=""):
        ran.append((sub_type, prompt, session_id))
        return "worker output"

    monkeypatch.setattr(chat_mod, "_run_parsed_subagent", _fake_run)

    out = await chat_mod.chat("/researcher find the latest on X", "s-sub")

    assert out == [{"role": "assistant", "content": "worker output"}]
    assert ran == [("researcher", "find the latest on X", "s-sub")]
    assert g.inputs == []  # short-circuited: no lead-agent turn on the raw command


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["/researcher find the latest on X", "/self-improve rewrite your soul", "/goal take over", "/no-such-command"],
)
async def test_a_fenced_turn_runs_no_short_circuit(graph, monkeypatch, message):
    """A ``tool_fence`` (#2972) is only enforced on the lead turn, so a fenced turn
    must not reach a short-circuit that works outside it — the text runs as a fenced
    lead turn instead."""
    g = graph([_answer("fenced answer")])

    async def _never(*a, **k):  # pragma: no cover — must not escape the fence
        raise AssertionError("a fenced turn ran a subagent")

    monkeypatch.setattr(chat_mod, "_run_parsed_subagent", _never)

    out = await chat_mod.chat(message, "s-fenced", tool_fence=["discord_read"], origin="plugin")

    assert out[0]["content"] == "fenced answer"
    assert len(g.inputs) == 1
    assert g.inputs[0]["subagent_fence"] == ["discord_read"]


@pytest.mark.asyncio
async def test_bare_subagent_command_via_chat_returns_usage(graph, monkeypatch):
    g = graph([])

    async def _never(*a, **k):  # pragma: no cover — must not run without a prompt
        raise AssertionError("ran a subagent with no prompt")

    monkeypatch.setattr(chat_mod, "_run_parsed_subagent", _never)

    out = await chat_mod.chat("/researcher", "s-sub-usage")

    assert out[0]["content"].startswith("Usage: `/researcher <prompt>`")
    assert g.inputs == []


@pytest.mark.asyncio
async def test_provider_stream_drop_leaves_the_same_record_on_both_surfaces(graph, monkeypatch):
    """One classifier: a dropped provider stream is described and recorded with the
    SAME text whichever driver ran the turn (#2593's "same transcript" contract)."""
    import httpx

    drop = httpx.ReadError("stream closed")  # one of graph.llm.RETRYABLE_STREAM_ERRORS

    g = graph([drop])
    out = await chat_mod.chat("hello", "s-drop")
    assert out[0]["content"] == f"**Error:** {chat_mod._PROVIDER_CLOSED_MSG}"
    nonstream_record = g.recorded[-1]["messages"][0].content

    async def _dropping_turn(*a, **k):
        raise drop
        yield  # pragma: no cover — make this an async generator

    monkeypatch.setattr(chat_mod, "_run_native_turn", _dropping_turn)
    frames = [f async for f in chat_mod._chat_langgraph_stream_impl("hello", "s-drop")]
    assert frames[-1] == ("error", chat_mod._PROVIDER_CLOSED_MSG)
    assert g.recorded[-1]["messages"][0].content == nonstream_record


@pytest.mark.asyncio
async def test_streaming_overflow_before_the_native_turn_does_not_compact(graph, compaction, monkeypatch):
    """The recovery compacts the thread the NATIVE turn used. A failure in the
    pre-turn chain never touched it — and used to reach an unassigned `_tid` in the
    handler (UnboundLocalError) instead of reporting the real error."""
    calls, _ = compaction
    graph([])

    async def _exploding_exchange(*a, **k):
        raise ValueError(_OVERFLOW)

    monkeypatch.setattr(rooms_mod, "_at_delegate_exchange", _exploding_exchange)

    frames = [f async for f in chat_mod._chat_langgraph_stream_impl("hello", "s-pre")]

    assert calls == []
    assert frames[-1] == ("error", _OVERFLOW)


@pytest.mark.asyncio
async def test_a_fenced_turn_on_an_acp_runtime_is_refused_not_run(graph, monkeypatch):
    """A ``tool_fence`` (#2972) names THIS agent's tools and is enforced only on the native
    lead turn. An ACP runtime (claude-code, codex, …) runs its own full toolset, which the
    fence can't restrict — so the untrusted text must not reach it. Fail closed: refuse."""
    import runtime.acp_runtime as acp_rt

    g = graph([_answer("native answer")])
    monkeypatch.setattr(acp_rt, "is_acp_runtime", lambda cfg: True)
    ran = []

    async def _acp_never(session_id, message):  # pragma: no cover — must not run
        ran.append(message)
        return [{"role": "assistant", "content": "acp ran it"}]

    monkeypatch.setattr(chat_mod._chat_acp, "_acp_turn_collected", _acp_never)

    out = await chat_mod.chat("delete the repo", "s-fenced-acp", tool_fence=["discord_read"], origin="plugin")

    assert ran == []  # the external agent never saw the untrusted text (the hole)
    assert g.inputs == []  # nor did the native loop run it unfenced
    assert out[0]["content"] == chat_mod._FENCED_ACP_REFUSAL


@pytest.mark.asyncio
async def test_an_unfenced_turn_on_an_acp_runtime_still_runs_there(graph, monkeypatch):
    import runtime.acp_runtime as acp_rt

    graph([_answer("native answer")])
    monkeypatch.setattr(acp_rt, "is_acp_runtime", lambda cfg: True)
    ran = []

    async def _acp(session_id, message):
        ran.append(message)
        return [{"role": "assistant", "content": "acp ran it"}]

    monkeypatch.setattr(chat_mod._chat_acp, "_acp_turn_collected", _acp)

    out = await chat_mod.chat("hello", "s-acp", origin="plugin")

    assert out[0]["content"] == "acp ran it" and ran == ["hello"]
