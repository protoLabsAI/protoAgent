"""ProtoAgentACP against a scripted A2A server, with a recording ACP client connection."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import fake_a2a as fa
import pytest
from acp import RequestError, text_block

from protoagent_acp.a2a import A2AClient
from protoagent_acp.agent import ProtoAgentACP
from protoagent_acp.roots import RootMap


class RecordingConn:
    def __init__(self, approve: bool = True) -> None:
        self.updates: list[dict] = []
        self.permissions: list[Any] = []
        self.approve = approve

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        self.updates.append(update.model_dump(by_alias=True, exclude_none=True))

    async def request_permission(self, session_id: str, tool_call: Any, options: list[Any], **_: Any) -> Any:
        self.permissions.append(tool_call)
        chosen = "approve" if self.approve else "deny"
        return SimpleNamespace(outcome=SimpleNamespace(outcome="selected", option_id=chosen))

    def of(self, kind: str) -> list[dict]:
        return [u for u in self.updates if u["sessionUpdate"] == kind]

    def text(self) -> str:
        return "".join(u["content"]["text"] for u in self.of("agent_message_chunk"))


async def _agent(fake: fa.FakeA2A, token: str | None = "secret", overrides: dict | None = None, approve: bool = True):
    client = A2AClient(fake.url, token)
    agent = ProtoAgentACP(client, RootMap(overrides))
    conn = RecordingConn(approve)
    agent.on_connect(conn)
    init = await agent.initialize(protocol_version=1)
    assert any(m.type == "terminal" for m in init.auth_methods)  # the registry's requirement
    return agent, conn, client


async def test_text_tools_and_locations_stream_through():
    with fa.FakeA2A(roots={"protoAgent": "/repo"}) as fake:
        fake.script = lambda ctx, msg: [
            fa.task(ctx),
            fa.reasoning(ctx, "Looking for the handler."),
            fa.tool(ctx, "c1", "search_files", "started", args=""),  # early announce: no args
            fa.tool(ctx, "c1", "search_files", "started", args='{"project": "protoAgent", "query": "SendStreamingMessage"}'),
            fa.tool(ctx, "c1", "search_files", "completed", result="server/a2a.py:42: SendStreamingMessage\n"),
            fa.tool(ctx, "c2", "read_file", "started", args='{"project": "protoAgent", "path": "server/a2a.py", "offset": 30, "limit": 20}'),
            fa.tool(ctx, "c2", "read_file", "completed", result="30 | def handle(): ..."),
            fa.text(ctx, "It is in ", append=False),
            fa.text(ctx, "server/a2a.py.", append=True),
            # terminal REPLACE with the full canonical text — must not be re-emitted
            fa.text(ctx, "It is in server/a2a.py.", append=False, usage={"input_tokens": 10, "output_tokens": 5}),
            fa.done(ctx),
        ]
        agent, conn, client = await _agent(fake)
        sess = await agent.new_session(cwd="/repo")
        resp = await agent.prompt(prompt=[text_block("where?")], session_id=sess.session_id)
        await client.aclose()

    assert resp.stop_reason == "end_turn"
    assert resp.usage.input_tokens == 10 and resp.usage.output_tokens == 5
    assert conn.text() == "It is in server/a2a.py."
    assert conn.of("agent_thought_chunk")[0]["content"]["text"] == "Looking for the handler."
    starts = conn.of("tool_call")
    assert [s["toolCallId"] for s in starts] == ["c1", "c2"]
    assert starts[0]["kind"] == "search" and starts[0]["status"] == "in_progress"
    assert starts[1]["locations"] == [{"path": "/repo/server/a2a.py", "line": 30}]
    ups = conn.of("tool_call_update")
    # second announce fills in the search title; completion carries the hit as a location
    assert "SendStreamingMessage" in ups[0]["title"]
    done_c1 = next(u for u in ups if u["toolCallId"] == "c1" and u.get("status") == "completed")
    assert done_c1["locations"] == [{"path": "/repo/server/a2a.py", "line": 42}]
    # the session's contextId is the A2A contextId, and the first prompt says where the operator is
    msg = fake.requests[0]
    assert msg["contextId"] == sess.session_id and sess.session_id.startswith("chat-zed-")
    assert "Zed editor" in msg["parts"][0]["text"] and "`protoAgent`" in msg["parts"][0]["text"]


async def test_roots_fall_back_to_overrides_when_no_endpoint():
    with fa.FakeA2A(roots=None) as fake:
        fake.script = lambda ctx, msg: [
            fa.tool(ctx, "c1", "read_file", "started", args='{"project": "pa", "path": "a.py"}'),
            fa.done(ctx),
        ]
        agent, conn, client = await _agent(fake, overrides={"pa": "/local/pa"})
        sess = await agent.new_session(cwd="/elsewhere")
        await agent.prompt(prompt=[text_block("x")], session_id=sess.session_id)
        await client.aclose()
    assert conn.of("tool_call")[0]["locations"] == [{"path": "/local/pa/a.py"}]
    assert "Zed editor" not in fake.requests[0]["parts"][0]["text"]  # cwd isn't a project


async def test_bad_token_is_auth_required():
    with fa.FakeA2A() as fake:
        agent, _conn, client = await _agent(fake, token="wrong")
        with pytest.raises(RequestError) as ei:
            await agent.new_session(cwd="/")
        await client.aclose()
    assert ei.value.code == RequestError.auth_required().code


@pytest.mark.parametrize("approve", [True, False])
async def test_approval_maps_to_request_permission_and_resumes(approve):
    with fa.FakeA2A() as fake:
        def script(ctx, msg):
            if not (msg.get("metadata") or {}).get("hitl_resume"):
                return [fa.task(ctx), fa.hitl(ctx, {"kind": "approval", "title": "Approve permanent file delete?", "detail": "x.txt"})]
            return [fa.text(ctx, f"resumed with {msg['parts'][0]['text']}", append=False), fa.done(ctx)]

        fake.script = script
        agent, conn, client = await _agent(fake, approve=approve)
        sess = await agent.new_session(cwd="/")
        resp = await agent.prompt(prompt=[text_block("delete x.txt")], session_id=sess.session_id)
        await client.aclose()
    assert resp.stop_reason == "end_turn"
    assert len(conn.permissions) == 1
    word = "approved" if approve else "denied"
    assert fake.requests[1]["taskId"] == "t1" and fake.requests[1]["parts"][0]["text"] == word
    assert conn.text() == f"resumed with {word}"


async def test_question_parks_and_next_prompt_answers_it():
    with fa.FakeA2A() as fake:
        def script(ctx, msg):
            if not (msg.get("metadata") or {}).get("hitl_resume"):
                return [fa.task(ctx), fa.hitl(ctx, {"question": "Which branch?"})]
            return [fa.text(ctx, "ok, " + msg["parts"][0]["text"], append=False), fa.done(ctx)]

        fake.script = script
        agent, conn, client = await _agent(fake)
        sess = await agent.new_session(cwd="/")
        r1 = await agent.prompt(prompt=[text_block("go")], session_id=sess.session_id)
        assert "Which branch?" in conn.text() and r1.stop_reason == "end_turn"
        await agent.prompt(prompt=[text_block("main")], session_id=sess.session_id)
        await client.aclose()
    assert fake.requests[1]["taskId"] == "t1" and fake.requests[1]["metadata"] == {"hitl_resume": True}
    assert conn.text().endswith("ok, main")


async def test_failed_turn_is_reported_as_text():
    with fa.FakeA2A() as fake:
        fake.script = lambda ctx, msg: [fa.task(ctx), fa.done(ctx, state="TASK_STATE_FAILED", reason="gateway down")]
        agent, conn, client = await _agent(fake)
        sess = await agent.new_session(cwd="/")
        resp = await agent.prompt(prompt=[text_block("x")], session_id=sess.session_id)
        await client.aclose()
    assert resp.stop_reason == "end_turn" and "gateway down" in conn.text()


async def test_cancel_sends_cancel_task_and_stops():
    with fa.FakeA2A() as fake:
        fake.script = lambda ctx, msg: [fa.task(ctx, tid="t9"), fa.text(ctx, "working…", append=False, tid="t9"), "HOLD"]
        agent, conn, client = await _agent(fake)
        sess = await agent.new_session(cwd="/")
        running = asyncio.create_task(agent.prompt(prompt=[text_block("long")], session_id=sess.session_id))
        for _ in range(100):
            if conn.text():
                break
            await asyncio.sleep(0.02)
        await agent.cancel(session_id=sess.session_id)
        resp = await asyncio.wait_for(running, 5)
        await client.aclose()
    assert resp.stop_reason == "cancelled"
    assert fake.cancels == ["t9"]


async def test_foreign_context_frames_are_ignored():
    with fa.FakeA2A() as fake:
        fake.script = lambda ctx, msg: [fa.text("someone-else", "leak", append=False), fa.text(ctx, "mine", append=False), fa.done(ctx)]
        agent, conn, client = await _agent(fake)
        sess = await agent.new_session(cwd="/")
        await agent.prompt(prompt=[text_block("x")], session_id=sess.session_id)
        await client.aclose()
    assert conn.text() == "mine"


async def test_leading_paragraph_break_is_not_rendered():
    with fa.FakeA2A() as fake:
        # the live stream opens with "\n\n" and the terminal REPLACE doesn't
        fake.script = lambda ctx, msg: [fa.text(ctx, "\n\nLet", append=False), fa.text(ctx, " me", append=True),
                                        fa.text(ctx, "Let me look.", append=False), fa.done(ctx)]
        agent, conn, client = await _agent(fake)
        sess = await agent.new_session(cwd="/")
        await agent.prompt(prompt=[text_block("x")], session_id=sess.session_id)
        await client.aclose()
    assert conn.text() == "Let me"  # the diverging-whitespace REPLACE adds nothing (can't retract)
