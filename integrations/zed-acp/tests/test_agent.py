"""ProtoAgentACP against a scripted A2A server, with a recording ACP client connection."""

from __future__ import annotations

import asyncio
from typing import Any

import fake_a2a as fa
import pytest
from acp import RequestError, text_block
from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

from protoagent_acp.a2a import A2AClient
from protoagent_acp.agent import ProtoAgentACP
from protoagent_acp.roots import RootMap


class RecordingConn:
    def __init__(self, approve: bool | str | None = True) -> None:
        self.updates: list[dict] = []
        self.permissions: list[Any] = []
        self.options: list[list[tuple[str, str]]] = []
        self.approve = approve

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        self.updates.append(update.model_dump(by_alias=True, exclude_none=True))

    async def request_permission(self, session_id: str, tool_call: Any, options: list[Any], **_: Any) -> Any:
        self.permissions.append(tool_call)
        self.options.append([(o.option_id, o.kind) for o in options])
        # The real schema shapes a client sends: picking any option is AllowedOutcome
        # ("selected"); dismissing the prompt is DeniedOutcome ("cancelled").
        if self.approve is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        if self.approve == "always":
            ids = [o.option_id for o in options if o.kind == "allow_always"] or [o.option_id for o in options if o.kind == "allow_once"]
            return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=ids[0]))
        chosen = "approve" if self.approve else "deny"
        return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=chosen))

    def of(self, kind: str) -> list[dict]:
        return [u for u in self.updates if u["sessionUpdate"] == kind]

    def text(self) -> str:
        return "".join(u["content"]["text"] for u in self.of("agent_message_chunk"))


async def _agent(fake: fa.FakeA2A, token: str | None = "secret", overrides: dict | None = None, approve: bool | str | None = True):
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


@pytest.mark.parametrize("approve", [True, False, None])
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
    word = "approved" if approve else "denied"  # a dismissed prompt (None) fails closed
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


# What protoAgent really emits when the model call dies (reproduced against a live
# instance whose gateway answers 429 usage_limit_reached, and read back from the flagship
# navaEngineer's durable task store): SUBMITTED → WORKING → FAILED, with the exception text
# as the FAILED status message's only part. No artifact, no cost metadata.
RATE_LIMIT = (
    "Error code: 429 - {'error': {'type': 'usage_limit_reached', 'message': 'The usage limit has been "
    "reached', 'plan_type': 'prolite', 'resets_at': 1790758625, 'eligible_promo': None}}"
)


async def _prompt_expecting_error(fake, text="x"):
    agent, conn, client = await _agent(fake)
    sess = await agent.new_session(cwd="/")
    with pytest.raises(RequestError) as ei:
        await agent.prompt(prompt=[text_block(text)], session_id=sess.session_id)
    await client.aclose()
    return ei.value, conn


async def test_failed_turn_is_an_error_and_a_visible_message():
    with fa.FakeA2A() as fake:
        fake.script = lambda ctx, msg: [
            fa.task(ctx),
            fa.status(ctx),
            fa.done(ctx, state="TASK_STATE_FAILED", reason=RATE_LIMIT),
        ]
        err, conn = await _prompt_expecting_error(fake)
    assert err.code == -32603
    assert str(err) == "protoAgent: The usage limit has been reached (HTTP 429, usage_limit_reached)"
    assert err.data["state"] == "failed" and err.data["taskId"] == "t1" and "prolite" in err.data["detail"]
    assert conn.text() == "⚠️ protoAgent error: The usage limit has been reached (HTTP 429, usage_limit_reached)"


async def test_stream_closing_without_terminal_state_reads_back_the_failure():
    with fa.FakeA2A() as fake:
        fake.script = lambda ctx, msg: [fa.task(ctx, tid="t2"), fa.status(ctx, tid="t2")]  # then the socket closes
        fake.tasks["t2"] = fa.durable("t2", "c", "TASK_STATE_FAILED", message=RATE_LIMIT)
        err, conn = await _prompt_expecting_error(fake)
    assert "usage limit" in str(err) and "⚠️ protoAgent error" in conn.text()


async def test_stream_closing_early_recovers_a_completed_answer():
    with fa.FakeA2A() as fake:
        fake.script = lambda ctx, msg: [fa.task(ctx, tid="t3"), fa.text(ctx, "Half", append=False, tid="t3")]
        fake.tasks["t3"] = fa.durable("t3", "c", "TASK_STATE_COMPLETED", answer="Half and the rest.")
        agent, conn, client = await _agent(fake)
        sess = await agent.new_session(cwd="/")
        resp = await agent.prompt(prompt=[text_block("x")], session_id=sess.session_id)
        await client.aclose()
    assert resp.stop_reason == "end_turn" and conn.text() == "Half and the rest."


async def test_stream_closing_while_task_still_runs_is_not_success():
    with fa.FakeA2A() as fake:
        fake.script = lambda ctx, msg: [fa.task(ctx, tid="t4"), fa.status(ctx, tid="t4")]
        fake.tasks["t4"] = fa.durable("t4", "c", "TASK_STATE_WORKING")
        err, _conn = await _prompt_expecting_error(fake)
    assert "still working" in str(err) and err.data["state"] == "working"


async def test_stream_closing_with_nothing_readable_is_not_success():
    with fa.FakeA2A() as fake:
        fake.script = lambda ctx, msg: []  # 200 + event-stream, zero frames
        err, conn = await _prompt_expecting_error(fake)
    assert "without a result" in str(err) and conn.text().startswith("⚠️ protoAgent error")


async def test_json_rpc_error_frame_is_an_error():
    with fa.FakeA2A() as fake:
        fake.script = lambda ctx, msg: [{"jsonrpc": "2.0", "id": "1", "error": {"code": -32603, "message": "boom"}}]
        err, _conn = await _prompt_expecting_error(fake)
    assert str(err) == "protoAgent: boom"


def test_friendly_error_passthrough():
    from protoagent_acp.agent import friendly_error

    assert friendly_error("gateway down") == "gateway down"
    assert friendly_error('Error code: 500 - {"error": {"message": "upstream exploded"}}') == "upstream exploded (HTTP 500)"


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


async def test_project_onboarded_mid_session_gets_locations():
    """navaEngineer rehearsal: the agent registered a project partway through the session;
    every later read/edit on it had a title but no location because the roots were only
    fetched at session/new."""
    with fa.FakeA2A(roots={"protoAgent": "/repo"}) as fake:
        def script(ctx, msg):
            fake.roots["rehearsal"] = "/Users/me/dev/nava/rehearsal"  # onboard_project, mid-turn
            return [
                fa.tool(ctx, "c1", "onboard_project", "started", args='{"name": "rehearsal"}'),
                fa.tool(ctx, "c1", "onboard_project", "completed", result="registered"),
                fa.tool(ctx, "c2", "read_file", "started", args='{"project": "rehearsal", "path": "src/assistant.ts"}'),
                fa.tool(ctx, "c3", "edit_file", "started", args='{"project": "rehearsal", "path": "src/assistant.ts", "old": "a", "new": "b"}'),
                fa.done(ctx),
            ]

        fake.script = script
        agent, conn, client = await _agent(fake)
        sess = await agent.new_session(cwd="/repo")
        await agent.prompt(prompt=[text_block("x")], session_id=sess.session_id)
        await client.aclose()
    starts = {u["toolCallId"]: u for u in conn.of("tool_call")}
    assert starts["c2"]["locations"] == [{"path": "/Users/me/dev/nava/rehearsal/src/assistant.ts"}]
    assert starts["c3"]["locations"] == [{"path": "/Users/me/dev/nava/rehearsal/src/assistant.ts"}]
    assert starts["c3"]["kind"] == "edit"
    assert fake.gets.count("/api/fs/roots") == 2  # session/new + ONE refresh for the new name


async def test_unknown_project_refresh_is_rate_limited():
    with fa.FakeA2A(roots={"protoAgent": "/repo"}) as fake:
        fake.script = lambda ctx, msg: [
            fa.tool(ctx, f"c{i}", "read_file", "started", args='{"project": "ghost", "path": "a.py"}') for i in range(5)
        ] + [fa.done(ctx)]
        agent, conn, client = await _agent(fake)
        sess = await agent.new_session(cwd="/")
        await agent.prompt(prompt=[text_block("x")], session_id=sess.session_id)
        await client.aclose()
    assert all("locations" not in u for u in conn.of("tool_call"))
    assert fake.gets.count("/api/fs/roots") == 2  # session/new + one refresh for five misses


# ── "Allow for this session" ────────────────────────────────────────────────

SHELL = {"kind": "approval", "title": "Approve shell command?", "detail": "npm test\n\nruns via: /bin/sh -c", "project": "p"}
DELETE = {"kind": "approval", "title": "Approve permanent file delete?", "detail": "old.txt", "project": "p"}


def _run_command_park(ctx, call_id, hitl=SHELL, tid="t1"):
    return [
        fa.tool(ctx, call_id, "run_command", "started", args='{"project": "p", "command": "npm test"}', tid=tid),
        fa.hitl(ctx, hitl, tid=tid),
    ]


def _bypassing(msg) -> bool:
    return bool((msg.get("metadata") or {}).get("bypass_permissions"))


async def test_allow_for_session_approves_now_this_turn_and_bypasses_later_turns():
    with fa.FakeA2A() as fake:
        def script(ctx, msg):
            n = len(fake.requests)
            if n == 1:  # turn 1: a command parks
                return [fa.task(ctx), *_run_command_park(ctx, "c1")]
            if n == 2:  # resume → the tool finishes, a SECOND command parks in the same turn
                return [fa.tool(ctx, "c1", "run_command", "completed", result="ok"), *_run_command_park(ctx, "c2")]
            if n == 3:  # resume of the second park → turn ends
                return [fa.tool(ctx, "c2", "run_command", "completed", result="ok"), fa.text(ctx, "done", append=False), fa.done(ctx)]
            # turn 2: the server honours bypass_permissions and never parks
            assert _bypassing(msg)
            return [fa.task(ctx, tid="t2"), fa.text(ctx, "ran without asking", append=False, tid="t2"), fa.done(ctx, tid="t2")]

        fake.script = script
        agent, conn, client = await _agent(fake, approve="always")
        sess = await agent.new_session(cwd="/")
        await agent.prompt(prompt=[text_block("run the tests twice")], session_id=sess.session_id)
        await agent.prompt(prompt=[text_block("again")], session_id=sess.session_id)
        await client.aclose()
    assert len(conn.permissions) == 1  # the second park in the same turn was NOT asked
    assert ("approve_session", "allow_always") in conn.options[0]
    assert [m["parts"][0]["text"] for m in fake.requests[1:3]] == ["approved", "approved"]
    assert not _bypassing(fake.requests[0])  # before the choice
    assert all(_bypassing(m) for m in fake.requests[1:])  # every later A2A message
    assert fake.requests[1]["metadata"] == {"hitl_resume": True, "bypass_permissions": True}
    assert "Commands will run without asking for the rest of this thread." in conn.text()
    auto = [u for u in conn.of("tool_call") if u["title"].startswith("Allowed for this session")]
    assert len(auto) == 1 and auto[0]["status"] == "completed"


async def test_delete_is_never_session_allowed_or_auto_approved():
    with fa.FakeA2A() as fake:
        def script(ctx, msg):
            n = len(fake.requests)
            if n == 1:
                return [fa.task(ctx), *_run_command_park(ctx, "c1")]
            if n == 2:  # after allow-for-session, the same turn asks to delete a file
                return [
                    fa.tool(ctx, "c1", "run_command", "completed", result="ok"),
                    fa.tool(ctx, "d1", "delete_file", "started", args='{"project": "p", "path": "old.txt"}'),
                    fa.hitl(ctx, DELETE),
                ]
            return [fa.tool(ctx, "d1", "delete_file", "completed", result="deleted"), fa.done(ctx)]

        fake.script = script
        agent, conn, client = await _agent(fake, approve="always")
        sess = await agent.new_session(cwd="/")
        await agent.prompt(prompt=[text_block("x")], session_id=sess.session_id)
        await client.aclose()
    assert len(conn.permissions) == 2  # the delete WAS asked, despite allow-all
    assert [k for _, k in conn.options[1]] == ["allow_once", "reject_once"]  # no allow_always offered
    delete_card = next(u for u in conn.of("tool_call") if u["toolCallId"].startswith("approval-") and "delete" in u["title"])
    assert delete_card["kind"] == "delete" and delete_card["status"] == "pending"


async def test_server_refusing_bypass_still_prompts_and_says_so_once():
    """filesystem.bypass_allowed: false — the server parks even with bypass_permissions set.
    The shim must not work around it: it asks, every time."""
    with fa.FakeA2A() as fake:
        def script(ctx, msg):
            n = len(fake.requests)
            if msg.get("metadata", {}).get("hitl_resume"):
                return [fa.tool(ctx, f"c{n}", "run_command", "completed", result="ok", tid=msg["taskId"]), fa.done(ctx, tid=msg["taskId"])]
            tid = f"t{n}"
            return [fa.task(ctx, tid=tid), *_run_command_park(ctx, f"c{n}", tid=tid)]  # parks regardless of bypass

        fake.script = script
        agent, conn, client = await _agent(fake, approve="always")
        sess = await agent.new_session(cwd="/")
        for _ in range(3):
            await agent.prompt(prompt=[text_block("run")], session_id=sess.session_id)
        await client.aclose()
    assert len(conn.permissions) == 3  # asked on every turn after the first
    assert conn.text().count("doesn't allow skipping command approval") == 1


async def test_allow_for_session_is_per_session():
    with fa.FakeA2A() as fake:
        def script(ctx, msg):
            if msg.get("metadata", {}).get("hitl_resume"):
                return [fa.tool(ctx, "c1", "run_command", "completed", result="ok"), fa.done(ctx)]
            return [fa.task(ctx), *_run_command_park(ctx, "c1")]

        fake.script = script
        agent, conn, client = await _agent(fake, approve="always")
        a = await agent.new_session(cwd="/")
        await agent.prompt(prompt=[text_block("x")], session_id=a.session_id)
        b = await agent.new_session(cwd="/")  # a new Zed thread
        await agent.prompt(prompt=[text_block("x")], session_id=b.session_id)
        await client.aclose()
    first_b = next(m for m in fake.requests if m["contextId"] == b.session_id)
    assert not _bypassing(first_b)
    assert len(conn.permissions) == 2  # thread B asked again
