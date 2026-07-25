"""Non-streaming /v1/chat/completions termination semantics (#2234).

The bug: a turn that hit LangGraph's ``recursion_limit`` / the tool loop's
``max_iterations`` returned with the assistant body truncated mid-thought — the
handler joined EVERY assistant message from ``chat()`` (including mid-loop
narrations between tool calls) and always claimed ``finish_reason: "stop"``, so
a stateless programmatic driver acted on a fragment as if it were the answer.

The fix (both at the HTTP seam in ``operator_api.chat_routes``): the
non-streaming body carries only the LAST assistant message, and
``finish_reason`` is read off the thread's checkpointed state —
``"length"`` for a hard-stop (thread ends in a ``ToolMessage``, or in an
``AIMessage`` with unresolved ``tool_calls``), ``"stop"`` only for a clean
synthesis. Streaming is untouched.
"""

import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


class _FakeGraph:
    """Just enough graph for the /v1 seam: ``aget_state(config)`` returns a
    snapshot whose ``.values["messages"]`` is the checkpointed thread. The
    ``messages`` list is shared, so a fake ``chat()`` can append to it and the
    handler's post-turn inspection sees what the turn 'checkpointed'."""

    def __init__(self, messages=()):
        self.messages = list(messages)
        self.seen: list[dict] = []

    async def aget_state(self, config):
        self.seen.append(config)
        return SimpleNamespace(values={"messages": list(self.messages)})


def _tc(name: str, call_id: str) -> dict:
    """A LangChain tool_call entry as it appears on an AIMessage."""
    return {"name": name, "args": {}, "id": call_id, "type": "tool_call"}


def _client(monkeypatch, *, graph, chat_reply=None):
    import operator_api.chat_routes as cr
    import runtime.state as rs

    async def _fake_chat(message, session_id, *, model=None, incognito=False, hitl_resume=False, images=None):
        return chat_reply or [{"role": "assistant", "content": f"echo:{message}"}]

    monkeypatch.setattr(cr, "chat", _fake_chat)
    monkeypatch.setattr(cr, "agent_name", lambda: "protoagent")
    monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    monkeypatch.setattr(rs.STATE, "thread_id_resolver", None, raising=False)
    app = FastAPI()
    cr.register_chat_routes(app, ui="none")
    return TestClient(app)


def _post(c, prompt="go"):
    return c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": prompt}]}).json()


# ---------------------------------------------------------------------------
# Last-assistant-message-only body
# ---------------------------------------------------------------------------


def test_v1_returns_only_last_assistant_message(monkeypatch):
    # A limit-terminated turn hands back several assistant messages; the /v1
    # body must be the FINAL one only — never a "\n\n" join that presents
    # mid-loop narration as part of the answer.
    reply = [
        {"role": "assistant", "content": "Let me create those features first…"},
        {"role": "assistant", "content": "Now marking them ready…"},
        {"role": "assistant", "content": "Done: created A and B, both marked ready."},
    ]
    graph = _FakeGraph([AIMessage(content="Done: created A and B, both marked ready.")])
    c = _client(monkeypatch, graph=graph, chat_reply=reply)
    body = _post(c)
    assert len(body["choices"]) == 1
    msg = body["choices"][0]["message"]
    assert msg == {"role": "assistant", "content": "Done: created A and B, both marked ready."}
    assert "Let me create" not in msg["content"] and "Now marking" not in msg["content"]
    assert body["choices"][0]["finish_reason"] == "stop"


def test_v1_multi_tool_board_batch_regression(monkeypatch):
    # The #2234 reproduction shape — a board batch op: two board_create_feature
    # calls then two board_mark_ready calls in one turn. The response body must
    # only exist once the LAST tool call resolved, and it must carry exactly ONE
    # assistant message (the final synthesis), with no intermediate narration.
    import operator_api.chat_routes as cr

    graph = _FakeGraph()
    events: list[str] = []
    script = [
        ("board_create_feature", "Creating feature A…"),
        ("board_create_feature", "Creating feature B…"),
        ("board_mark_ready", "Marking feature A ready…"),
        ("board_mark_ready", "Marking feature B ready…"),
    ]

    async def _fake_chat(message, session_id, *, model=None, incognito=False, hitl_resume=False, images=None):
        graph.messages.append(HumanMessage(content=message))
        narrations = []
        for i, (tool, narration) in enumerate(script):
            graph.messages.append(AIMessage(content=narration, tool_calls=[_tc(tool, f"call_{i}")]))
            graph.messages.append(ToolMessage(content="ok", tool_call_id=f"call_{i}"))
            events.append(f"resolved:{tool}")
            narrations.append(narration)
        graph.messages.append(AIMessage(content="All 4 board ops done."))
        events.append("chat-returned")
        return [{"role": "assistant", "content": n} for n in narrations] + [
            {"role": "assistant", "content": "All 4 board ops done."}
        ]

    c = _client(monkeypatch, graph=graph)
    monkeypatch.setattr(cr, "chat", _fake_chat)
    body = _post(c)

    # The body arrived only after every tool call resolved.
    assert events == [
        "resolved:board_create_feature",
        "resolved:board_create_feature",
        "resolved:board_mark_ready",
        "resolved:board_mark_ready",
        "chat-returned",
    ]
    # Exactly one assistant message — the final synthesis, no narration joined in.
    assert len(body["choices"]) == 1
    assert body["choices"][0]["message"]["content"] == "All 4 board ops done."
    for _, narration in script:
        assert narration not in body["choices"][0]["message"]["content"]
    assert body["choices"][0]["finish_reason"] == "stop"


# ---------------------------------------------------------------------------
# finish_reason honesty
# ---------------------------------------------------------------------------


def test_v1_finish_reason_length_when_thread_ends_in_toolmessage(monkeypatch):
    # Thread checkpointed a ToolMessage last → the loop stopped right after a
    # tool ran, before any synthesis: an interrupted turn, not an answer.
    graph = _FakeGraph(
        [
            AIMessage(content="Creating feature A…", tool_calls=[_tc("board_create_feature", "call_0")]),
            ToolMessage(content="ok", tool_call_id="call_0"),
        ]
    )
    c = _client(monkeypatch, graph=graph, chat_reply=[{"role": "assistant", "content": "Creating feature A…"}])
    body = _post(c)
    assert body["choices"][0]["finish_reason"] == "length"


def test_v1_finish_reason_length_on_unresolved_tool_calls(monkeypatch):
    # A thread ending in AIMessage(content=<narration>, tool_calls=[…]) must
    # report "length": the model requested tools but the tool node never ran —
    # the turn was cut off between the request and the execution. BOTH real
    # captures attached to #2234 end in exactly this shape, so this is the
    # reported bug, not just the ToolMessage shape the fix also covers.
    graph = _FakeGraph(
        [
            AIMessage(
                content="Now let me mark those features ready…",
                tool_calls=[_tc("board_mark_ready", "call_2"), _tc("board_mark_ready", "call_3")],
            ),
        ]
    )
    c = _client(
        monkeypatch,
        graph=graph,
        chat_reply=[{"role": "assistant", "content": "Now let me mark those features ready…"}],
    )
    body = _post(c)
    assert body["choices"][0]["finish_reason"] == "length"


def test_v1_max_iterations_cutoff_reports_length(monkeypatch):
    # The max-iterations edge: a 4-tool turn driven with max_iterations=3 stops
    # after the 3rd tool resolves, with the model's 4th request left unresolved
    # on the thread. finish_reason must be "length" — NOT "stop" — and the body
    # must be the last narration alone, not a join of all of them.
    import operator_api.chat_routes as cr

    max_iterations = 3
    graph = _FakeGraph()
    resolved: list[str] = []
    script = [
        ("board_create_feature", "Creating feature A…"),
        ("board_create_feature", "Creating feature B…"),
        ("board_mark_ready", "Marking feature A ready…"),
        ("board_mark_ready", "Marking feature B ready…"),
    ]

    async def _fake_chat(message, session_id, *, model=None, incognito=False, hitl_resume=False, images=None):
        narrations = []
        for i, (tool, narration) in enumerate(script):
            narrations.append(narration)
            if i >= max_iterations:
                # Cap hit: the request is checkpointed but the tool never runs.
                graph.messages.append(AIMessage(content=narration, tool_calls=[_tc(tool, f"call_{i}")]))
                break
            graph.messages.append(AIMessage(content=narration, tool_calls=[_tc(tool, f"call_{i}")]))
            graph.messages.append(ToolMessage(content="ok", tool_call_id=f"call_{i}"))
            resolved.append(tool)
        return [{"role": "assistant", "content": n} for n in narrations]

    c = _client(monkeypatch, graph=graph)
    monkeypatch.setattr(cr, "chat", _fake_chat)
    body = _post(c)

    assert len(resolved) == max_iterations  # the cap actually bit
    assert body["choices"][0]["finish_reason"] == "length"
    assert body["choices"][0]["message"]["content"] == "Marking feature B ready…"
    assert "Creating feature A…" not in body["choices"][0]["message"]["content"]


def test_v1_finish_reason_inspects_the_turns_own_thread(monkeypatch):
    # The state inspection must target the SAME checkpointer thread chat() wrote
    # (the default a2a:<session_id> keying) — not some other session's state.
    import operator_api.chat_routes as cr

    graph = _FakeGraph([AIMessage(content="done")])
    seen_sessions: list[str] = []

    async def _fake_chat(message, session_id, *, model=None, incognito=False, hitl_resume=False, images=None):
        seen_sessions.append(session_id)
        return [{"role": "assistant", "content": "done"}]

    c = _client(monkeypatch, graph=graph)
    monkeypatch.setattr(cr, "chat", _fake_chat)
    _post(c)
    assert len(seen_sessions) == 1 and len(graph.seen) == 1
    assert graph.seen[0]["configurable"]["thread_id"] == f"a2a:{seen_sessions[0]}"


async def test_v1_finish_reason_fail_safe_is_stop(monkeypatch):
    # An unreadable snapshot must never fail (or misflag) the response: no
    # aget_state on the graph, a raising aget_state, and an empty thread all
    # degrade to the historical "stop".
    import runtime.state as rs
    from operator_api.chat_routes import _v1_finish_reason

    monkeypatch.setattr(rs.STATE, "thread_id_resolver", None, raising=False)

    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)  # no aget_state
    assert await _v1_finish_reason("s1") == "stop"

    class _Boom:
        async def aget_state(self, config):
            raise RuntimeError("checkpointer unavailable")

    monkeypatch.setattr(rs.STATE, "graph", _Boom(), raising=False)
    assert await _v1_finish_reason("s1") == "stop"

    monkeypatch.setattr(rs.STATE, "graph", _FakeGraph([]), raising=False)
    assert await _v1_finish_reason("s1") == "stop"


def test_has_unresolved_tool_calls_predicate():
    from operator_api.chat_routes import _has_unresolved_tool_calls

    assert _has_unresolved_tool_calls(AIMessage(content="on it", tool_calls=[_tc("board_mark_ready", "c1")]))
    assert not _has_unresolved_tool_calls(AIMessage(content="done"))  # clean synthesis
    assert not _has_unresolved_tool_calls(ToolMessage(content="ok", tool_call_id="c1"))
    assert not _has_unresolved_tool_calls(HumanMessage(content="hi"))


# ---------------------------------------------------------------------------
# Streaming is untouched
# ---------------------------------------------------------------------------


def test_v1_streaming_unchanged(monkeypatch):
    # The stream path keeps its historical shape even for a limit-terminated
    # multi-message turn: joined parts in one chunk, finish_reason "stop" on the
    # done chunk. (#2234 is a non-streaming fix only.)
    reply = [
        {"role": "assistant", "content": "narration"},
        {"role": "assistant", "content": "final"},
    ]
    graph = _FakeGraph([ToolMessage(content="ok", tool_call_id="c1")])  # would be "length" if consulted
    c = _client(monkeypatch, graph=graph, chat_reply=reply)
    r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "go"}], "stream": True})
    frames = [json.loads(ln[len("data: ") :]) for ln in r.text.splitlines() if ln.startswith("data: ") and "[DONE]" not in ln]
    assert frames[0]["choices"][0]["delta"]["content"] == "narration\n\nfinal"
    assert frames[1]["choices"][0]["finish_reason"] == "stop"
