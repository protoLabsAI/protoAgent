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

    async def _fake_chat(message, session_id, *, model=None, incognito=False, hitl_resume=False, images=None, **_kw):
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

    async def _fake_chat(message, session_id, *, model=None, incognito=False, hitl_resume=False, images=None, **_kw):
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

    async def _fake_chat(message, session_id, *, model=None, incognito=False, hitl_resume=False, images=None, **_kw):
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

    async def _fake_chat(message, session_id, *, model=None, incognito=False, hitl_resume=False, images=None, **_kw):
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


# ---------------------------------------------------------------------------
# A failed turn is an HTTP error, not a 200 with the exception as the answer (#2578)
# ---------------------------------------------------------------------------


def _raw(c, body=None):
    return c.post("/v1/chat/completions", json=body or {"messages": [{"role": "user", "content": "go"}]})


def _err_reply(exc, message=None):
    """What `chat()` hands back for a turn that raised — the rendered bubble plus
    the structured companion the /v1 seam reads."""
    from server.chat import turn_error

    text = message or str(exc)
    return [{"role": "assistant", "content": f"**Error:** {text}", "error": turn_error(exc, message)}]


class _UpstreamError(Exception):
    """Stands in for openai.AuthenticationError / RateLimitError — the SDK exceptions
    carry the provider's HTTP status on `status_code`, which is all the seam reads."""

    def __init__(self, status, msg):
        super().__init__(msg)
        self.status_code = status


def test_v1_upstream_auth_failure_is_not_a_successful_completion(monkeypatch):
    """The reported bug: the gateway rejected the key, and /v1 answered HTTP 200 with
    the traceback text as `content` and finish_reason "stop"."""
    exc = _UpstreamError(
        401,
        "Error code: 401 - {'error': {'message': \"Authentication Error, LiteLLM Virtual Key expected.\"}}",
    )
    c = _client(monkeypatch, graph=_FakeGraph([AIMessage(content="x")]), chat_reply=_err_reply(exc))

    r = _raw(c)

    assert r.status_code == 502  # NOT 200, and NOT 401 (that means "your protoAgent bearer is bad")
    body = r.json()
    assert body["error"]["type"] == "authentication_error"
    assert body["error"]["upstream_status"] == 401
    assert "Authentication Error" in body["error"]["message"]
    assert "choices" not in body  # nothing that looks like an answer


def test_v1_rate_limit_is_mirrored_so_client_backoff_works(monkeypatch):
    exc = _UpstreamError(429, "Error code: 429 - rate limit exceeded")
    c = _client(monkeypatch, graph=_FakeGraph([AIMessage(content="x")]), chat_reply=_err_reply(exc))

    r = _raw(c)

    assert r.status_code == 429  # every OpenAI client's retry logic keys on this
    assert r.json()["error"]["type"] == "rate_limit_error"


def test_v1_internal_fault_is_500_not_502(monkeypatch):
    """No upstream status ⇒ the fault is ours, not a proxy hop's."""
    c = _client(monkeypatch, graph=_FakeGraph([AIMessage(content="x")]), chat_reply=_err_reply(ValueError("boom")))

    r = _raw(c)

    assert r.status_code == 500
    body = r.json()
    assert body["error"]["type"] == "server_error"
    assert body["error"]["upstream_status"] is None


def test_v1_streaming_failure_is_an_http_error_not_an_sse_frame(monkeypatch):
    """The turn completes before the stream opens, so a failure can still be a real
    status instead of a delta the client reads as a successful answer."""
    exc = _UpstreamError(401, "bad key")
    c = _client(monkeypatch, graph=_FakeGraph([AIMessage(content="x")]), chat_reply=_err_reply(exc))

    r = _raw(c, {"messages": [{"role": "user", "content": "go"}], "stream": True})

    assert r.status_code == 502
    assert "text/event-stream" not in r.headers.get("content-type", "")
    assert "data:" not in r.text


def test_v1_successful_turn_is_untouched(monkeypatch):
    """The guard must only fire on a structured error — a normal turn still 200s."""
    c = _client(monkeypatch, graph=_FakeGraph([AIMessage(content="fine")]))

    r = _raw(c)

    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "echo:go"


def test_v1_content_mentioning_error_is_not_hijacked(monkeypatch):
    """A turn whose ANSWER happens to discuss an error is a success. The seam keys on
    the structured field, never on the `**Error:**` prefix."""
    reply = [{"role": "assistant", "content": "**Error:** is how that log line starts — here's why."}]
    c = _client(monkeypatch, graph=_FakeGraph([AIMessage(content="x")]), chat_reply=reply)

    r = _raw(c)

    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"].startswith("**Error:**")
