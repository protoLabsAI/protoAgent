"""End-to-end: a federation-token /a2a request can't approve or bypass ``run_command`` (#5162 item 4).

#5138 tied approval authority to the caller's trust tier along a three-link chain: the real A2A
auth middleware classifies the credential (``request.state.trust_tier`` → tracing contextvar); the
streaming driver binds it (``server/chat.py``: ``request_metadata_scope(..., may_approve=
turn_caller_may_approve())`` — ``False`` for the federation tier, and it *drops* a caller-supplied
``bypass_permissions``); and the gate reads what that bound (``tools/fs_tools.py``:
``caller_may_approve()`` / ``_bypass_requested()``), never the caller's own metadata.

Existing tests pin each link in isolation. None drove a real ``/a2a`` request into the driver and
checked the gate. Both cases here POST a real ``SendMessage`` through the real ``A2AAuthMiddleware``
and ``ProtoAgentExecutor`` → ``_chat_langgraph_stream`` → a real ``create_agent`` graph, with the
model faked only at the gateway boundary (``graph.agent.create_llm``) and scripted to call
``run_command`` with a state-changing command while the turn carries ``bypass_permissions``:
a federation token → refused, never auto-approved (``caller_may_approve() is False``) and the state
change never happens; the operator credential → the same request runs past the gate (the control).

Only the streaming path is reachable from ``/a2a`` (the executor always drives
``_chat_langgraph_stream``). The sync path ``server/turn_sync.py`` serves ``/v1`` and ``/api/chat``,
not ``/a2a``, so it is not exercised here (it binds the same tier via ``approval_authority_scope``).
"""

from __future__ import annotations

import asyncio
import importlib
import json
from unittest.mock import patch

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage

from a2a_impl import auth
from runtime.state import STATE
from tests._a2a_app import A2A_HEADERS, build_app, drain_handlers

# `server.chat` the attribute is shadowed by the re-exported `chat` function in
# server/__init__.py, so resolve the actual submodule from sys.modules.
chat_mod = importlib.import_module("server.chat")

# run_command spawns a real subprocess in the fenced project dir.
pytestmark = pytest.mark.platform_sensitive

_OP_SECRET = "op-secret"
_FED_SECRET = "fed-secret"

# A state-changing command: it creates a marker file in the fenced project dir. `echo … > file`
# is valid under both /bin/sh and cmd.exe, so the marker's presence/absence is a direct, cross-
# platform read of whether the gate let the command run.
_MARKER = "ran_marker.txt"
_RUN_CMD = f"echo ran > {_MARKER}"


class _ToolFake(GenericFakeChatModel):
    """Fake chat model that supports ``bind_tools`` (returns itself) so it drops into
    ``create_agent`` and replays preset ``AIMessage``s, tool calls included.

    The streaming driver consumes the model via ``astream_events``; the stock
    ``GenericFakeChatModel._stream`` drops ``tool_calls`` (and yields nothing for an
    empty-content tool-call message). Override ``_astream`` to emit one chunk carrying the
    full message. (Mirrors ``tests/test_hitl_hold.py``'s fake.)"""

    def bind_tools(self, tools, **kwargs):
        return self

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
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


def _script() -> list[AIMessage]:
    """Fresh per install (the model consumes a one-shot iterator): the turn's first model call
    asks for the gated ``run_command``; the next returns a plain final answer. Spare answers
    guard against any extra model call (e.g. a middleware) overrunning the iterator."""
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "run_command",
                "args": {"project": "work", "command": _RUN_CMD},
                "id": "c1",
                "type": "tool_call",
            }
        ],
    )
    return [call, AIMessage(content="done."), AIMessage(content="ok"), AIMessage(content="ok")]


def _install_graph(monkeypatch, project_dir):
    """A real ``create_agent`` graph over a single run-enabled, approval-gated, bypass-allowed
    filesystem project, with the gateway model faked. The gate is live; only the LLM is a stub."""
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver

    cfg = LangGraphConfig(
        filesystem_enabled=True,
        filesystem_allow_run=True,
        filesystem_run_requires_approval=True,
        filesystem_bypass_allowed=True,
        filesystem_projects=[{"name": "work", "path": str(project_dir), "write": True}],
    )
    fake = _ToolFake(messages=iter(_script()))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        g = create_agent_graph(cfg, include_subagents=False, checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    return g


def _app_with_auth():
    """The real streaming driver behind the real A2A auth middleware, configured two-tier:
    ``op-secret`` → operator, ``fed-secret`` → federation (ADR 0066)."""
    app = build_app(chat_mod._chat_langgraph_stream)  # no bearer kwarg → build_app adds no middleware
    auth.install(
        app,
        bearer_token=_OP_SECRET,
        api_key="",
        allowed_origins_raw="",
        federation_token=_FED_SECRET,
    )
    return app


def _send(client, *, token, context_id):
    """A ``SendMessage`` carrying a per-turn ``bypass_permissions`` request (the operator's
    ``/bypass`` toggle shape) — honored only for a caller that may approve, never from metadata."""
    return client.post(
        "/a2a",
        headers={**A2A_HEADERS, "Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": "r1",
            "method": "SendMessage",
            "params": {
                # request-level AND message-level, so whichever the SDK surfaces carries the flag.
                "metadata": {"bypass_permissions": True},
                "message": {
                    "messageId": "m1",
                    "role": "ROLE_USER",
                    "contextId": context_id,
                    "parts": [{"text": "run the command"}],
                    "metadata": {"bypass_permissions": True},
                },
            },
        },
    )


async def _poll_terminal(client, task_id, *, token, tries=100):
    hdrs = {**A2A_HEADERS, "Authorization": f"Bearer {token}"}
    for _ in range(tries):
        g = await client.post(
            "/a2a",
            headers=hdrs,
            json={"jsonrpc": "2.0", "id": "g", "method": "GetTask", "params": {"id": task_id}},
        )
        t = g.json()["result"]
        if t["status"]["state"] in (
            "TASK_STATE_COMPLETED",
            "TASK_STATE_FAILED",
            "TASK_STATE_CANCELED",
            "TASK_STATE_INPUT_REQUIRED",
        ):
            return t
        await asyncio.sleep(0.03)
    raise AssertionError(f"task {task_id} never reached terminal")


async def _run_command_result(context_id) -> str:
    """The ``run_command`` tool result from the committed thread (thread_id ``a2a:{context_id}``,
    as the streaming driver resolves it)."""
    snap = await STATE.graph.aget_state({"configurable": {"thread_id": f"a2a:{context_id}"}})
    tool_contents = [str(m.content) for m in snap.values.get("messages", []) if isinstance(m, ToolMessage)]
    assert tool_contents, "no tool result was recorded on the thread"
    return "\n".join(tool_contents)


async def _drive(token, context_id):
    """POST the SendMessage over HTTP and wait for the turn to finish; return the task's
    context id (what the thread is keyed under)."""
    app = _app_with_auth()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=10
        ) as c:
            resp = await _send(c, token=token, context_id=context_id)
            assert resp.status_code == 200, resp.text
            task = resp.json()["result"]["task"]
            final = await _poll_terminal(c, task["id"], token=token)
            assert final["status"]["state"] == "TASK_STATE_COMPLETED", final["status"]
            return final.get("contextId") or task.get("contextId") or context_id
    finally:
        await drain_handlers()


@pytest.mark.asyncio
async def test_a_federation_token_cannot_approve_or_bypass_run_command_over_a2a(tmp_path, monkeypatch):
    """r1: a real ``/a2a`` turn authenticated with a federation token is refused the gated
    command. The driver binds ``may_approve=False`` from the tier, so the gate sees
    ``caller_may_approve() is False`` — the command is refused outright (no card to self-answer,
    no bypass) and the state change never happens."""
    proj = tmp_path / "work"
    proj.mkdir()
    _install_graph(monkeypatch, proj)

    ctx = await _drive(_FED_SECRET, "fed-session")

    # The command did NOT run: the marker file was never created.
    assert not (proj / _MARKER).exists()
    # …and the tool result says exactly why: the gate refused a caller that can't approve.
    assert "this caller can't give it" in await _run_command_result(ctx)


@pytest.mark.asyncio
async def test_the_operator_credential_is_allowed_past_the_approval_gate_over_a2a(tmp_path, monkeypatch):
    """r2 (control): the SAME request with the operator bearer is allowed past the gate. The
    driver binds ``may_approve=True``, so the honored ``bypass_permissions`` skips the approval
    gate and the command runs — the marker file is created, and no refusal was recorded."""
    proj = tmp_path / "work"
    proj.mkdir()
    _install_graph(monkeypatch, proj)

    ctx = await _drive(_OP_SECRET, "op-session")

    # Allowed past the gate: the command ran and made its state change.
    assert (proj / _MARKER).exists()
    assert "this caller can't give it" not in await _run_command_result(ctx)
