"""Caller-supplied chat session ids get one consistent shape check at every entry point,
and the session-memory store answers "not under the base" for any id it cannot resolve.

The rule (``runtime.session_ids``) is shape-only: every id a first-party client mints —
console ``chat-<ms>-<rand>``, Zed ``chat-zed-...``, minted ``api-...``, A2A context ids
(UUIDs, ``:``-bearing ids), ``/v1``'s ``openai-compat-...`` — passes unchanged; ids no
client produces (separators, control characters, ``%``, dot-only, over-long) are refused.
"""

from __future__ import annotations

import os
import re
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from runtime.session_ids import MAX_SESSION_ID_LEN, is_valid_session_id, session_id_problem

# Ids real clients send today — none may be refused or rewritten.
LEGIT_IDS = [
    "chat-1727712345678-k3j9x2",  # console tab (apps/web chat-store id())
    "chat-zed-1727712345678-ab12",  # Zed ACP shim
    "api-1727712345678-q8w7e6",  # minted by POST /api/chat
    "openai-compat-first.last",  # /v1 pinned session
    str(uuid.uuid4()),  # A2A SDK default contextId
    "a2a:peer-agent:ctx-1",  # ':'-bearing A2A / room ids (memory encodes ':' as %3A)
    "system:activity",
    "tab-1",
    "first.last@example.com",
    "x" * MAX_SESSION_ID_LEN,
]

REFUSED_IDS = [
    "",
    ".",
    "..",
    "../x",
    "a/b",
    "/abs",
    "..\\x",
    "a\\b",
    "x\x00y",
    "x\ny",
    "a%3Ab",  # would alias the encoded filename of "a:b"
    "a?b",
    "a*b",
    'a"b',
    "a|b",
    "a<b>",
    "x" * (MAX_SESSION_ID_LEN + 1),
]


# --- the shared rule ---------------------------------------------------------------


@pytest.mark.parametrize("sid", LEGIT_IDS)
def test_first_party_session_ids_are_accepted(sid):
    assert session_id_problem(sid) is None
    assert is_valid_session_id(sid)


@pytest.mark.parametrize("sid", REFUSED_IDS)
def test_unusable_session_ids_are_refused_with_a_reason(sid):
    problem = session_id_problem(sid)
    assert isinstance(problem, str) and problem


def test_v1_normalized_ids_always_satisfy_the_shared_rule():
    from operator_api.chat_routes import _v1_session_id

    for raw in [*REFUSED_IDS[3:], "user name with spaces", "a:b", "é" * 400]:
        sid = _v1_session_id({"session_id": raw})
        assert is_valid_session_id(sid), (raw, sid)


# --- POST /api/chat -----------------------------------------------------------------


def _chat_client(monkeypatch, seen: list):
    import operator_api.chat_routes as cr
    import runtime.state as rs

    async def _fake_chat(message, session_id, **_kw):
        seen.append(session_id)
        return [{"role": "assistant", "content": "ok"}]

    monkeypatch.setattr(cr, "chat", _fake_chat)
    monkeypatch.setattr(cr, "agent_name", lambda: "protoagent")
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    monkeypatch.setattr(rs.STATE, "a2a_task_engine", None, raising=False)
    app = FastAPI()
    cr.register_chat_routes(app, ui="none")
    return TestClient(app)


@pytest.mark.parametrize("sid", ["../../outside", "a/b", "x\x00y", "..", "a%2Fb", "a\\b"])
def test_api_chat_refuses_an_unusable_session_id_before_the_turn(monkeypatch, sid):
    seen: list = []
    c = _chat_client(monkeypatch, seen)
    r = c.post("/api/chat", json={"message": "hi", "session_id": sid})
    assert r.status_code == 422, r.text
    assert seen == []  # the turn never ran under that id


@pytest.mark.parametrize("sid", LEGIT_IDS)
def test_api_chat_passes_first_party_session_ids_through_unchanged(monkeypatch, sid):
    seen: list = []
    c = _chat_client(monkeypatch, seen)
    body = c.post("/api/chat", json={"message": "hi", "session_id": f"  {sid}  "}).json()
    assert body["session_id"] == sid
    assert seen == [sid]


def test_api_chat_blank_session_id_still_mints_one(monkeypatch):
    seen: list = []
    c = _chat_client(monkeypatch, seen)
    body = c.post("/api/chat", json={"message": "hi", "session_id": "   "}).json()
    assert re.fullmatch(r"api-\d{13,}-[a-z0-9]{6}", body["session_id"])


# --- /api/chat/sessions/{session_id}/... ---------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", ""),
        ("delete", ""),
        ("get", "/turns"),
        ("get", "/export"),
        ("post", "/compact"),
        ("post", "/steer"),
        ("get", "/steer"),
        ("get", "/delegations"),
    ],
)
@pytest.mark.parametrize("encoded", ["..%5C..%5Cx", "x%00y", "a%25b", "%2E%2E"])
def test_session_path_routes_refuse_an_unusable_id(monkeypatch, method, suffix, encoded):
    c = _chat_client(monkeypatch, [])
    url = f"/api/chat/sessions/{encoded}{suffix}"
    r = getattr(c, method)(url, **({"json": {"text": "x"}} if method == "post" else {}))
    assert r.status_code == 422, (url, r.status_code, r.text)


def test_fork_refuses_an_unusable_target_session_id(monkeypatch):
    import operator_api.chat_routes as cr

    calls: list = []

    async def _fake_fork(src, dst, **kw):
        calls.append((src, dst))
        return {"forked": True}

    c = _chat_client(monkeypatch, [])
    monkeypatch.setattr(cr, "fork_session", _fake_fork)
    r = c.post("/api/chat/sessions/chat-1/fork", json={"new_session_id": "../chat-2", "index": 0})
    assert r.status_code == 422
    assert calls == []
    ok = c.post("/api/chat/sessions/chat-1/fork", json={"new_session_id": "chat-2", "index": 0})
    assert ok.status_code == 200 and calls == [("chat-1", "chat-2")]


# --- goal routes ----------------------------------------------------------------------


def test_goal_routes_refuse_an_unusable_id(monkeypatch):
    import runtime.state as rs
    from operator_api.routes import register_operator_routes

    cleared: list = []

    async def _clear(sid, close_tasks):
        cleared.append(sid)
        return {"cleared": True}

    async def _resume(sid):
        return {"ok": True}

    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda r: None,
        subagent_batch=lambda r: None,
        goal_clear=_clear,
        goal_resume=_resume,
    )
    c = TestClient(app)
    assert c.get("/api/goals/x%00y").status_code == 422
    assert c.delete("/api/goals/..%5Cx").status_code == 422
    assert c.post("/api/goals/a%25b/resume").status_code == 422
    assert cleared == []
    assert c.get("/api/goals/chat-1").json() == {"enabled": False, "goal": None, "plan": ""}
    assert c.delete("/api/goals/a2a:x").json() == {"cleared": True} and cleared == ["a2a:x"]


def test_operator_goal_set_refuses_an_unusable_session_id(monkeypatch):
    import asyncio

    import runtime.state as rs
    from operator_api import console_handlers

    set_calls: list = []

    class _Ctl:
        def set_goal_operator(self, sid, *a, **kw):
            set_calls.append(sid)
            return True, "ok"

    monkeypatch.setattr(rs.STATE, "goal_controller", _Ctl(), raising=False)
    res = asyncio.run(console_handlers._operator_goals_set({"session_id": "../x", "condition": "c"}))
    assert res["ok"] is False and set_calls == []


# --- A2A contextId ----------------------------------------------------------------------


def _a2a_handler(calls: list):
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.tasks import InMemoryPushNotificationConfigStore, InMemoryTaskStore
    from a2a.types import AgentSkill

    import protolabs_a2a as pa
    from a2a_impl.executor import ProtoAgentExecutor

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        calls.append(ctx)
        yield ("done", "ok")

    card = pa.build_agent_card(
        name="t",
        description="d",
        url="http://t/a2a",
        version="0.0.0",
        skills=[AgentSkill(id="chat", name="chat", description="d", tags=["chat"])],
        bearer=False,
    )
    return DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(stream),
        task_store=InMemoryTaskStore(),
        agent_card=card,
        push_config_store=InMemoryPushNotificationConfigStore(),
    )


def _a2a_msg(ctx: str):
    from a2a.types import Message, Part, Role, SendMessageRequest

    return SendMessageRequest(
        message=Message(message_id=uuid.uuid4().hex, context_id=ctx, role=Role.ROLE_USER, parts=[Part(text="hi")])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ctx", ["../../outside", "a\\b", "x\x00y", "a%3Ab"])
async def test_a2a_refuses_an_unusable_context_id_before_the_turn(ctx):
    from a2a.server.context import ServerCallContext
    from a2a.utils.errors import InvalidParamsError

    from a2a_impl.executor import set_progress_hook, set_terminal_hook

    set_terminal_hook(None)
    set_progress_hook(None)
    calls: list = []
    handler = _a2a_handler(calls)
    with pytest.raises(InvalidParamsError):
        await handler.on_message_send(_a2a_msg(ctx), ServerCallContext())
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ctx", ["chat-1727712345678-k3j9x2", "a2a:peer-agent:ctx-1", str(uuid.uuid4())])
async def test_a2a_first_party_context_ids_still_run(ctx):
    from a2a.server.context import ServerCallContext
    from a2a.types import TaskState

    from a2a_impl.executor import set_progress_hook, set_terminal_hook

    set_terminal_hook(None)
    set_progress_hook(None)
    calls: list = []
    handler = _a2a_handler(calls)
    task = await handler.on_message_send(_a2a_msg(ctx), ServerCallContext())
    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert calls == [ctx]


# --- session-memory store: resolution never fails, never leaves the base -------------


def test_contained_in_answers_false_for_an_unresolvable_path(tmp_path):
    from graph.middleware.memory import contained_in

    assert contained_in(str(tmp_path), os.path.join(str(tmp_path), "x\x00y.json")) is False
    assert contained_in(str(tmp_path), os.path.join(str(tmp_path), "ok.json")) is True


def test_session_summary_store_ignores_an_unresolvable_id(tmp_path):
    from graph.middleware.memory import delete_session_summary, session_file_candidates

    assert session_file_candidates("x\x00y", base=str(tmp_path)) == []
    assert delete_session_summary("x\x00y", base=str(tmp_path)) is False


def test_persist_never_fails_the_turn_and_writes_nothing_for_an_unresolvable_id(monkeypatch, tmp_path):
    from langchain_core.messages import AIMessage, HumanMessage

    import graph.middleware.memory as mem

    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
    monkeypatch.setattr(mem, "_PERSISTENCE_DISABLED", False)
    state = {"session_id": "x\x00y", "messages": [HumanMessage("hi"), AIMessage("hello")]}
    mem._persist_session(state, "trace-1")  # must not raise
    assert [p for p in os.listdir(tmp_path) if not p.startswith(".")] == []


def test_persist_keeps_writing_first_party_ids(monkeypatch, tmp_path):
    from langchain_core.messages import AIMessage, HumanMessage

    import graph.middleware.memory as mem

    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
    monkeypatch.setattr(mem, "_PERSISTENCE_DISABLED", False)
    for sid in ("chat-1727712345678-k3j9x2", "a2a:peer:1"):
        mem._persist_session({"session_id": sid, "messages": [HumanMessage("hi"), AIMessage("yo")]}, "t")
    assert sorted(os.listdir(tmp_path)) == ["a2a%3Apeer%3A1.json", "chat-1727712345678-k3j9x2.json"]
