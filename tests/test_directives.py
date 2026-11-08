"""The per-chat operator directive ledger (ADR 0127 D6).

The incident behind it: an operator approved a release plan with conditions and a stop
rule; a later compaction summarised the approval away; the agent, no longer seeing it,
decided it had acted without approval. The ledger keeps the operator's words outside the
transcript and re-injects them every model call, so a compaction can't erase them.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import runtime.state as rs
from graph.directives import (
    MAX_QUOTE_CHARS,
    STATUS_FULFILLED,
    STATUS_IN_FORCE,
    STATUS_REVOKED,
    WS_HEADING,
    DirectiveClosed,
    DirectiveError,
    DirectiveNotFound,
    DirectiveStore,
    directive_store,
    directives_command_reply,
)

APPROVAL = (
    "once #4801 and #4808 part 1 have both merged, cut the release … if the review panel gives "
    "the release PR a WARN or FAIL, come back to me instead of merging."
)


@pytest.fixture
def store(tmp_path, monkeypatch) -> DirectiveStore:
    s = DirectiveStore(tmp_path / "directives")
    monkeypatch.setattr(rs.STATE, "directive_store", s, raising=False)
    return s


@pytest.fixture
def bare_state(store, monkeypatch):
    """Only the ledger contributes to <working_state>."""
    for attr in ("goal_controller", "tasks_store", "watch_controller", "scheduler"):
        monkeypatch.setattr(rs.STATE, attr, None, raising=False)
    return store


# ── store ─────────────────────────────────────────────────────────────────────────


def test_record_list_and_close_round_trip(store):
    d = store.record("chat-a", f"  {APPROVAL}\n", conditions="both merged; stop on WARN/FAIL", scope="v0.231.0")
    assert d.id.startswith("dir-") and d.status == STATUS_IN_FORCE and d.created_at
    assert d.quote == APPROVAL  # verbatim — only the ends trimmed

    (got,) = store.list("chat-a")
    assert got == d
    assert store.get("chat-a", d.id) == d

    closed = store.close("chat-a", d.id, status=STATUS_FULFILLED, reason="v0.231.0 tagged", closed_by="agent")
    assert closed.status == STATUS_FULFILLED and closed.closed_by == "agent" and closed.closed_at
    assert store.in_force("chat-a") == []
    assert [x.status for x in store.list("chat-a")] == [STATUS_FULFILLED]


def test_ledger_survives_a_new_store_object(store, tmp_path):
    d = store.record("a2a:ctx-1", APPROVAL)
    again = DirectiveStore(tmp_path / "directives")
    assert [x.quote for x in again.in_force("a2a:ctx-1")] == [APPROVAL]
    assert again.get("a2a:ctx-1", d.id) is not None


def test_directives_are_scoped_to_their_chat(store):
    a = store.record("chat-a", "only in A")
    store.record("chat-b", "only in B")
    # Ids that a sanitising file-name mapper would alias stay separate.
    store.record("chat:x", "colon")
    store.record("chat_x", "underscore")

    assert [d.quote for d in store.list("chat-a")] == ["only in A"]
    assert [d.quote for d in store.list("chat:x")] == ["colon"]
    assert [d.quote for d in store.list("chat_x")] == ["underscore"]
    with pytest.raises(DirectiveNotFound):
        store.close("chat-b", a.id, status=STATUS_REVOKED, closed_by="operator")
    assert store.get("chat-a", a.id).in_force  # the other chat's attempt changed nothing


def test_closing_twice_and_bad_input_are_refused(store):
    d = store.record("s", "do it")
    store.close("s", d.id, status=STATUS_REVOKED, reason="changed my mind", closed_by="operator")
    with pytest.raises(DirectiveClosed):
        store.close("s", d.id, status=STATUS_FULFILLED)
    with pytest.raises(DirectiveError):
        store.record("s", "   ")
    with pytest.raises(DirectiveError):
        store.record("", "no chat")
    with pytest.raises(DirectiveError):
        store.record("s", "x" * (MAX_QUOTE_CHARS + 1))
    with pytest.raises(DirectiveError):
        store.close("s", d.id, status="in_force")


def test_a_file_belonging_to_another_session_is_ignored(store):
    d = store.record("s", "mine")
    path = store._path("s")
    data = json.loads(path.read_text())
    data["session_id"] = "someone-else"
    path.write_text(json.dumps(data))
    assert store.list("s") == []
    assert store.get("s", d.id) is None


def test_forget_drops_the_chat_ledger(store):
    store.record("s", "one")
    store.record("t", "keep")
    assert store.forget("s") is True
    assert store.list("s") == [] and len(store.list("t")) == 1
    assert store.forget("s") is False


def test_directive_store_falls_back_to_the_instance_store(monkeypatch):
    from infra.paths import instance_paths

    monkeypatch.setattr(rs.STATE, "directive_store", None, raising=False)
    s = directive_store()
    assert s.path == instance_paths().store("directives")


# ── tools ─────────────────────────────────────────────────────────────────────────


def _tools() -> dict:
    from tools.directive_tools import build_directive_tools

    return {t.name: t for t in build_directive_tools()}


def _call(name: str, args: dict, session_id: str = "chat-a") -> str:
    tool = _tools()[name]
    return tool.func(**args, state={"session_id": session_id, "operator_chat_turn": True})


def test_tools_record_list_fulfil_revoke(store):
    out = _call("record_directive", {"quote": APPROVAL, "conditions": "stop on WARN/FAIL"})
    assert out.startswith("Recorded directive dir-")
    did = out.split()[2].rstrip(".")

    listing = _call("list_directives", {})
    assert did in listing and APPROVAL in listing and "stop on WARN/FAIL" in listing
    assert _call("list_directives", {}, session_id="chat-b") == "No directives in force in this chat."

    assert _call("fulfil_directive", {"directive_id": did, "reason": ""}).startswith("Error:")
    assert "fulfilled" in _call("fulfil_directive", {"directive_id": did, "reason": "released"})
    assert "already fulfilled" in _call("revoke_directive", {"directive_id": did, "reason": "x"})
    assert "released" in _call("list_directives", {"include_closed": True})

    other = _call("record_directive", {"quote": "hold prod"}).split()[2].rstrip(".")
    # Another chat can't close this chat's directive.
    assert "no directive" in _call("revoke_directive", {"directive_id": other, "reason": "x"}, session_id="chat-b")
    assert "revoked" in _call("revoke_directive", {"directive_id": other, "reason": 'operator: "never mind"'})
    assert store.list("chat-a")[1].closed_by == "agent"


@pytest.mark.parametrize(
    "name,args",
    [
        ("record_directive", {"quote": "x"}),
        ("list_directives", {}),
        ("fulfil_directive", {"directive_id": "dir-1", "reason": "r"}),
        ("revoke_directive", {"directive_id": "dir-1", "reason": "r"}),
    ],
)
def test_tools_refuse_without_a_chat(store, name, args):
    out = _call(name, args, session_id="")
    assert out.startswith("No active chat") and name in out
    assert not store.path.exists() or not list(store.path.glob("*.json"))


def test_tools_take_the_session_from_injected_state_not_the_contextvar(store, monkeypatch):
    from observability import tracing

    monkeypatch.setattr(tracing, "current_session_id", lambda: "")  # what a tool body sees
    _call("record_directive", {"quote": "from state"}, session_id="chat-z")
    assert [d.quote for d in store.list("chat-z")] == ["from state"]


def test_directive_tools_are_bound_by_default_and_never_deferred():
    from tools.lg_tools import DEFERRED_BASE_TOOL_NAMES, get_all_tools
    from tools.directive_tools import DIRECTIVE_TOOL_NAMES

    names = {t.name for t in get_all_tools()}
    assert DIRECTIVE_TOOL_NAMES <= names
    assert DIRECTIVE_TOOL_NAMES <= DEFERRED_BASE_TOOL_NAMES


def test_console_groups_the_tools_under_directives():
    from operator_api.console_handlers import _tool_category
    from tools.directive_tools import DIRECTIVE_TOOL_NAMES

    assert {_tool_category(n, "core") for n in DIRECTIVE_TOOL_NAMES} == {"Directives"}


def test_operating_model_tells_the_agent_to_record_and_consult():
    from graph.prompts import _build_operating_model

    text = _build_operating_model(frozenset({"record_directive", "fulfil_directive", "revoke_directive"}))
    assert "record_directive" in text and "STANDING DIRECTIVES" in text
    assert "lack approval" in text and "fulfil_directive" in text
    assert "Directives" not in _build_operating_model(frozenset({"memory_save"}))


# ── <working_state> injection ─────────────────────────────────────────────────────


def _ws(session_id: str) -> str:
    from graph.projection import working_state_block

    return working_state_block({"session_id": session_id})


def test_in_force_directives_render_verbatim_in_working_state(bare_state):
    d = bare_state.record("chat-a", APPROVAL, conditions="both merged first", scope="release v0.231.0")
    done = bare_state.record("chat-a", "rename the branch")
    bare_state.close("chat-a", done.id, status=STATUS_FULFILLED, reason="renamed")
    bare_state.record("chat-b", "other chat's approval")

    block = _ws("chat-a")
    assert block.startswith("<working_state>")
    assert f"{WS_HEADING}:" in block
    assert d.id in block and APPROVAL in block
    assert "Conditions: both merged first" in block and "Scope: release v0.231.0" in block
    assert "rename the branch" not in block  # closed ones don't ride the turn
    assert "other chat's approval" not in block  # another chat's never do
    assert _ws("chat-c") == ""  # nothing in force → no block at all


def test_directives_lead_the_working_state(bare_state, monkeypatch):
    class _Tasks:
        def list(self, *, include_closed=False):
            return [{"status": "open", "id": "task-1", "priority": 1, "title": "t"}]

    monkeypatch.setattr(rs.STATE, "tasks_store", _Tasks(), raising=False)
    bare_state.record("s", "keep the API stable")
    block = _ws("s")
    assert block.index(WS_HEADING) < block.index("OPEN TASKS")


def test_a_ledger_read_failure_skips_the_section(bare_state, monkeypatch):
    def _boom(session_id):
        raise OSError("disk gone")

    monkeypatch.setattr(bare_state, "in_force", _boom)
    assert _ws("s") == ""


def test_many_directives_are_capped_with_a_pointer(bare_state):
    from graph.directives import WS_DIRECTIVE_CAP

    for i in range(WS_DIRECTIVE_CAP + 3):
        bare_state.record("s", f"rule {i}")
    block = _ws("s")
    assert f"rule {WS_DIRECTIVE_CAP - 1}" in block and f"rule {WS_DIRECTIVE_CAP}" not in block
    assert "…+3 more (list_directives shows them all)" in block


# ── survives compaction: the incident ─────────────────────────────────────────────


def test_a_recorded_approval_is_visible_after_compaction_replaces_the_messages(bare_state):
    """Turn 1: the operator approves with conditions; the agent records it. Then a
    compaction replaces the whole transcript with a summary that never mentions the
    approval (the incident's summary kept only the later small talk). Turn 2: the model
    is still shown the approval, verbatim, under STANDING DIRECTIVES."""
    from langchain.agents import create_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    from graph.context_frame import is_context_frame
    from graph.middleware.knowledge import KnowledgeMiddleware
    from graph.state import ProtoAgentState
    from tools.directive_tools import build_directive_tools

    class _Model(BaseChatModel):
        frames: Any = None
        transcripts: Any = None

        @property
        def _llm_type(self) -> str:
            return "directive-fake"

        def bind_tools(self, tools, **kwargs):
            return self

        def _reply(self, messages) -> AIMessage:
            self.frames.append("\n".join(str(m.content) for m in messages if is_context_frame(m)))
            self.transcripts.append("\n".join(str(m.content) for m in messages if not is_context_frame(m)))
            last_human = next(m for m in reversed(messages) if isinstance(m, HumanMessage) and not is_context_frame(m))
            if "cut the release" in str(last_human.content) and not any(isinstance(m, ToolMessage) for m in messages):
                call = {
                    "name": "record_directive",
                    "args": {"quote": APPROVAL, "conditions": "both merged; WARN/FAIL → come back, don't merge"},
                    "id": "c1",
                    "type": "tool_call",
                }
                return AIMessage(content="", tool_calls=[call])
            return AIMessage(content="ok")

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=self._reply(messages))])

    frames: list[str] = []
    transcripts: list[str] = []
    knowledge = KnowledgeMiddleware(None, prior_sessions_enabled=False)
    knowledge._prior_sessions_cache = ""
    graph = create_agent(
        model=_Model(frames=frames, transcripts=transcripts),
        tools=build_directive_tools(),
        middleware=[knowledge],
        system_prompt="sys",
        state_schema=ProtoAgentState,
        checkpointer=InMemorySaver(),
    )
    cfg = {"configurable": {"thread_id": "a2a:chat-a"}}
    graph.invoke({"messages": [HumanMessage(APPROVAL)], "session_id": "chat-a", "operator_chat_turn": True}, cfg)
    assert [d.quote for d in bare_state.in_force("chat-a")] == [APPROVAL]

    # The compaction: every message replaced by a summary that lost the approval.
    summary = HumanMessage("[Summary of earlier conversation] The operator said work is wrapped up.")
    graph.update_state(cfg, {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary]})
    graph.invoke({"messages": [HumanMessage("what are you doing?")], "session_id": "chat-a", "operator_chat_turn": True}, cfg)

    after_frame, after_transcript = frames[-1], transcripts[-1]
    assert APPROVAL not in after_transcript  # the transcript lost it …
    assert WS_HEADING in after_frame and APPROVAL in after_frame  # … the model still sees it
    assert "WARN/FAIL → come back, don't merge" in after_frame


# ── operator API ──────────────────────────────────────────────────────────────────


def _client(monkeypatch, *, auth=False) -> TestClient:
    import operator_api.chat_routes as cr

    monkeypatch.setattr(cr, "agent_name", lambda: "protoagent")
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    app = FastAPI()
    if auth:
        from a2a_impl import auth as a2a_auth

        monkeypatch.setattr(a2a_auth, "_BEARER", ["test-operator"])
        monkeypatch.setattr(a2a_auth, "_FEDERATION", ["test-federation"])
        monkeypatch.setattr(a2a_auth, "_FLEET", [None])
        app.add_middleware(a2a_auth.A2AAuthMiddleware)
    cr.register_chat_routes(app, ui="none")
    return TestClient(app)


def test_api_lists_a_chats_directives(store, monkeypatch):
    c = _client(monkeypatch)
    a = store.record("chat-a", APPROVAL, conditions="stop on WARN")
    b = store.record("chat-a", "second")
    store.close("chat-a", b.id, status=STATUS_FULFILLED, reason="done")
    store.record("chat-b", "not yours")

    body = c.get("/api/chat/sessions/chat-a/directives").json()
    assert body["in_force"] == 1
    assert [d["id"] for d in body["directives"]] == [a.id, b.id]
    assert body["directives"][0]["quote"] == APPROVAL and body["directives"][0]["conditions"] == "stop on WARN"

    live = c.get("/api/chat/sessions/chat-a/directives?status=in_force").json()
    assert [d["id"] for d in live["directives"]] == [a.id]
    assert c.get("/api/chat/sessions/chat-a/directives?status=bogus").status_code == 400
    assert c.get("/api/chat/sessions/chat-new/directives").json() == {"directives": [], "in_force": 0}


def test_api_revoke_by_operator_and_isolation(store, monkeypatch):
    c = _client(monkeypatch)
    a = store.record("chat-a", APPROVAL)

    # Another chat's path can't reach it.
    r = c.delete(f"/api/chat/sessions/chat-b/directives/{a.id}")
    assert r.status_code == 404
    assert store.get("chat-a", a.id).in_force
    assert c.delete("/api/chat/sessions/chat-a/directives/dir-nope").status_code == 404

    r = c.delete(f"/api/chat/sessions/chat-a/directives/{a.id}?reason=plan changed")
    assert r.status_code == 200
    got = r.json()["directive"]
    assert got["status"] == STATUS_REVOKED and got["closed_by"] == "operator" and got["close_reason"] == "plan changed"
    assert c.delete(f"/api/chat/sessions/chat-a/directives/{a.id}").status_code == 409


def test_api_rejects_a_malformed_session_id(store, monkeypatch):
    c = _client(monkeypatch)
    assert c.get("/api/chat/sessions/bad%25id/directives").status_code in (400, 422)


def test_api_requires_an_operator_credential(store, monkeypatch):
    c = _client(monkeypatch, auth=True)
    a = store.record("chat-a", "x")
    paths = [
        (c.get, "/api/chat/sessions/chat-a/directives"),
        (c.delete, f"/api/chat/sessions/chat-a/directives/{a.id}"),
    ]
    for method, path in paths:
        assert method(path).status_code == 401
        assert method(path, headers={"Authorization": "Bearer test-federation"}).status_code in (401, 403)
    assert store.get("chat-a", a.id).in_force  # nothing got through
    ok = {"Authorization": "Bearer test-operator"}
    assert c.get("/api/chat/sessions/chat-a/directives", headers=ok).status_code == 200
    assert c.delete(f"/api/chat/sessions/chat-a/directives/{a.id}", headers=ok).status_code == 200


# ── /directives chat command ──────────────────────────────────────────────────────


def test_directives_is_a_reserved_slash_command():
    from graph.slash_commands import slash_kind
    from server.chat_dispatch import _unknown_slash_command_reply

    assert slash_kind("directives") == "directives"
    assert _unknown_slash_command_reply("/directives") is None


@pytest.fixture
def operator_tier():
    from observability.tracing import reset_trust_tier, set_trust_tier

    token = set_trust_tier("operator")
    yield
    reset_trust_tier(token)


def test_slash_lists_and_revokes(store, operator_tier):
    assert directives_command_reply("chat-a", "") == "No directives recorded in this chat."
    a = store.record("chat-a", APPROVAL, conditions="stop on WARN")
    b = store.record("chat-a", "old rule")
    store.close("chat-a", b.id, status=STATUS_FULFILLED, reason="shipped")

    listing = directives_command_reply("chat-a", "")
    assert "In force:" in listing and a.id in listing and APPROVAL in listing
    assert "Closed:" in listing and "Fulfilled by the agent" in listing and "shipped" in listing

    assert directives_command_reply("chat-a", "frobnicate").startswith("Usage:")
    assert "no directive" in directives_command_reply("chat-b", f"revoke {a.id}")
    reply = directives_command_reply("chat-a", f"revoke {a.id} we're not releasing today")
    assert reply.startswith(f"Revoked {a.id}")
    got = store.get("chat-a", a.id)
    assert got.status == STATUS_REVOKED and got.closed_by == "operator"
    assert got.close_reason == "we're not releasing today"


def test_slash_runs_in_the_pre_turn_dispatch(store, monkeypatch):
    from server import chat_dispatch

    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    store.record("chat-a", APPROVAL)

    async def _drain():
        pre = chat_dispatch._PreTurn(message="/directives")
        frames = [f async for f in chat_dispatch._pre_turn_dispatch(pre, "chat-a", None)]
        return pre, frames

    pre, frames = asyncio.run(_drain())
    assert pre.handled
    kind, payload = frames[-1]
    assert kind == "done" and APPROVAL in payload


def test_palette_lists_directives():
    from operator_api.console_handlers import _operator_chat_commands

    names = {c["name"]: c for c in _operator_chat_commands()["commands"]}
    assert names["directives"]["kind"] == "control"


def test_deleting_a_chat_drops_its_ledger(store, monkeypatch):
    import operator_api.chat_routes as cr

    async def _retire(*a, **k):
        return None

    monkeypatch.setattr(cr, "_retire_thread", _retire)
    monkeypatch.setattr(rs.STATE, "knowledge_store", None, raising=False)
    monkeypatch.setattr(rs.STATE, "a2a_task_engine", None, raising=False)
    c = _client(monkeypatch)
    store.record("chat-a", "x")
    store.record("chat-b", "y")
    assert c.delete("/api/chat/sessions/chat-a?retire=false").status_code == 200
    assert store.list("chat-a") == [] and len(store.list("chat-b")) == 1
