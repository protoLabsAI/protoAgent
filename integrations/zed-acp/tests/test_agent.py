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

    delay = 0.0  # seconds per update: a slow client widens the adopt-vs-finish race

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
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


async def _agent(
    fake: fa.FakeA2A,
    token: str | None = "secret",
    overrides: dict | None = None,
    approve: bool | str | None = True,
    steer_grace: float = 0.0,
):
    client = A2AClient(fake.url, token)
    agent = ProtoAgentACP(client, RootMap(overrides), steer_grace=steer_grace)
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


# ── Send Now → steer ───────────────────────────────────────────────────────────


async def _running(agent, conn, sid, text="long task"):
    running = asyncio.create_task(agent.prompt(prompt=[text_block(text)], session_id=sid))
    for _ in range(200):
        if conn.text():
            return running
        await asyncio.sleep(0.01)
    raise AssertionError("the turn never started streaming")


async def test_send_now_steers_the_running_turn():
    with fa.FakeA2A() as fake:
        fake.script = lambda ctx, msg: [
            fa.task(ctx),
            fa.text(ctx, "Reading A.", append=False),
            fa.after_steer(fake, lambda t: fa.text(ctx, f" Switching to: {t}.", append=True)),
            fa.done(ctx),
        ]
        agent, conn, client = await _agent(fake, steer_grace=2.0)
        sess = await agent.new_session(cwd="/")
        first = await _running(agent, conn, sess.session_id)
        await agent.cancel(session_id=sess.session_id)  # Zed's Send Now: cancel …
        r1 = await asyncio.wait_for(first, 1)  # … answered at once, the turn keeps running
        assert r1.stop_reason == "cancelled"
        r2 = await agent.prompt(prompt=[text_block("look at B instead")], session_id=sess.session_id)  # … then prompt
        await client.aclose()
    assert r2.stop_reason == "end_turn"
    assert fake.cancels == []  # never cancelled for real
    assert len(fake.requests) == 1  # no second A2A turn: the steer rode the running one
    assert fake.steers[0]["session"] == sess.session_id  # /steer is keyed by the A2A contextId
    assert fake.steers[0]["text"] == "look at B instead"
    assert conn.text() == "Reading A.\n\n↪ steering: look at B instead\n\n Switching to: look at B instead."


async def test_stop_without_a_follow_up_cancels_after_the_grace():
    with fa.FakeA2A() as fake:
        fake.script = lambda ctx, msg: [fa.task(ctx, tid="t9"), fa.text(ctx, "working", append=False, tid="t9"), "HOLD"]
        agent, conn, client = await _agent(fake, steer_grace=0.2)
        sess = await agent.new_session(cwd="/")
        first = await _running(agent, conn, sess.session_id)
        await agent.cancel(session_id=sess.session_id)
        assert (await asyncio.wait_for(first, 1)).stop_reason == "cancelled"
        assert fake.cancels == []  # still inside the window
        await asyncio.sleep(0.6)
        await client.aclose()
    assert fake.cancels == ["t9"]


async def test_steer_post_failure_falls_back_to_cancel_and_a_new_turn():
    with fa.FakeA2A() as fake:
        fake.steer_ok = False

        def script(ctx, msg):
            if len(fake.requests) == 1:
                return [fa.task(ctx, tid="t1"), fa.text(ctx, "working", append=False, tid="t1"), "HOLD"]
            return [fa.task(ctx, tid="t2"), fa.text(ctx, "fresh turn", append=False, tid="t2"), fa.done(ctx, tid="t2")]

        fake.script = script
        agent, conn, client = await _agent(fake, steer_grace=2.0)
        sess = await agent.new_session(cwd="/")
        first = await _running(agent, conn, sess.session_id)
        await agent.cancel(session_id=sess.session_id)
        await asyncio.wait_for(first, 1)
        r2 = await agent.prompt(prompt=[text_block("new direction")], session_id=sess.session_id)
        await client.aclose()
    assert fake.cancels == ["t1"] and r2.stop_reason == "end_turn"
    assert fake.requests[1]["parts"][0]["text"] == "new direction"
    assert conn.text().endswith("fresh turn")


async def test_turn_finishing_inside_the_window_makes_the_prompt_a_normal_turn():
    with fa.FakeA2A() as fake:
        release = asyncio.Event()

        def script(ctx, msg):
            if len(fake.requests) == 1:
                return [fa.task(ctx), fa.text(ctx, "almost", append=False), "HOLD", fa.done(ctx)]
            return [fa.task(ctx, tid="t2"), fa.text(ctx, "second", append=False, tid="t2"), fa.done(ctx, tid="t2")]

        fake.script = script
        agent, conn, client = await _agent(fake, steer_grace=5.0)
        sess = await agent.new_session(cwd="/")
        first = await _running(agent, conn, sess.session_id)
        await agent.cancel(session_id=sess.session_id)
        await asyncio.wait_for(first, 1)
        fake.hold.set()  # the turn completes while detached
        for _ in range(100):
            if agent._sessions[sess.session_id].runner is None:
                break
            await asyncio.sleep(0.02)
        release.set()
        r2 = await agent.prompt(prompt=[text_block("next")], session_id=sess.session_id)
        await client.aclose()
    assert r2.stop_reason == "end_turn" and fake.steers == [] and len(fake.requests) == 2
    assert "↪ steering" not in conn.text()


async def test_a_steer_that_arrived_too_late_is_rerun_as_the_next_turn():
    with fa.FakeA2A() as fake:
        def script(ctx, msg):
            if len(fake.requests) == 1:  # the turn ends WITHOUT folding the steer in
                return [fa.task(ctx), fa.text(ctx, "done A", append=False),
                        fa.after_steer(fake, lambda t: fa.done(ctx), fold=False)]
            return [fa.task(ctx, tid="t2"), fa.text(ctx, "now B", append=False, tid="t2"), fa.done(ctx, tid="t2")]

        fake.script = script
        agent, conn, client = await _agent(fake, steer_grace=2.0)
        sess = await agent.new_session(cwd="/")
        first = await _running(agent, conn, sess.session_id)
        await agent.cancel(session_id=sess.session_id)
        await asyncio.wait_for(first, 1)
        r2 = await agent.prompt(prompt=[text_block("do B")], session_id=sess.session_id)
        await client.aclose()
    assert r2.stop_reason == "end_turn"
    assert fake.steer_deleted == [fake.steers[0]["id"]]  # taken back out of the queue …
    assert fake.requests[1]["parts"][0]["text"] == "do B"  # … and sent as the next turn
    assert conn.text().endswith("now B")


async def test_approval_parked_inside_the_window_is_asked_of_the_adopting_prompt():
    with fa.FakeA2A() as fake:
        def script(ctx, msg):
            if (msg.get("metadata") or {}).get("hitl_resume"):
                items = [i for q in fake.steer_queue.values() for i in q]
                fake.steer_queue.clear()  # the resumed turn's next model call folds the steer in
                return [fa.tool(ctx, "c1", "run_command", "completed", result="ok"), fa.steer_consumed(ctx, items),
                        fa.text(ctx, "ran it", append=False), fa.done(ctx)]
            return [
                fa.task(ctx),
                fa.text(ctx, "starting", append=False),
                "HOLD",  # released by the test once the cancel has detached the turn
                fa.tool(ctx, "c1", "run_command", "started", args='{"project": "p", "command": "ls"}'),
                fa.hitl(ctx, SHELL),
            ]

        fake.script = script
        agent, conn, client = await _agent(fake, steer_grace=3.0)
        sess = await agent.new_session(cwd="/")
        first = await _running(agent, conn, sess.session_id)
        await agent.cancel(session_id=sess.session_id)
        await asyncio.wait_for(first, 1)
        fake.hold.set()  # the park lands while nobody owns the turn
        await asyncio.sleep(0.3)
        assert conn.permissions == []  # not asked while detached
        r2 = await agent.prompt(prompt=[text_block("go on")], session_id=sess.session_id)
        await client.aclose()
    assert len(conn.permissions) == 1 and r2.stop_reason == "end_turn"
    assert fake.requests[1]["parts"][0]["text"] == "approved"
    assert conn.text().endswith("ran it")


# ── thread history ─────────────────────────────────────────────────────────────


def _durable_turn(tid, user, answer, *, tools=(), state="TASK_STATE_COMPLETED", preamble=False):
    history = [{"role": "ROLE_USER", "parts": [{"text": (
        "[Context: the operator is talking to you from the Zed editor, opened on your project `p` (/repo). "
        "Paths you read or search there are shown to them in the editor.]\n\n" if preamble else "") + user}]}]
    for call_id, name, args, result in tools:
        history.append({"role": "ROLE_AGENT", "parts": [], "metadata": {fa.TOOL_URI: {"toolCallId": call_id, "name": name, "phase": "started", "args": args}}})
        history.append({"role": "ROLE_AGENT", "parts": [], "metadata": {fa.TOOL_URI: {"toolCallId": call_id, "name": name, "phase": "completed", "result": result}}})
    history.append({"role": "ROLE_USER", "parts": [{"text": "approved"}], "metadata": {"hitl_resume": True}})
    return {"task_id": tid, "state": state, "text": answer, "status": {"state": state}, "artifacts": [], "history": history}


async def test_list_shows_every_chat_and_titles_them():
    with fa.FakeA2A(roots={"p": "/repo"}) as fake:
        fake.sessions = [
            {"session_id": "chat-zed-2-bbb", "last_updated": "2026-09-24T10:00:00", "turn_count": 1},
            {"session_id": "chat-1790294944966-qkf93w", "last_updated": "2026-09-24T09:00:00", "turn_count": 3},
            {"session_id": "chat-zed-1-aaa", "last_updated": "2026-09-23T10:00:00", "turn_count": 2},
            {"session_id": "chat-1790-titled", "last_updated": "2026-09-22T10:00:00", "turn_count": 1, "title": "Server title"},
        ]
        fake.turns["chat-zed-2-bbb"] = [_durable_turn("t1", "Where is the A2A executor?", "In a2a_impl.", preamble=True)]
        fake.turns["chat-1790294944966-qkf93w"] = [_durable_turn("t0", "Plan the release notes", "Sure.")]
        agent, _conn, client = await _agent(fake)
        agent.index.put("chat-zed-1-aaa", cwd="/other/folder", title="Remembered title")
        init = await agent.initialize(protocol_version=1)
        caps = init.agent_capabilities
        assert caps.load_session and caps.session_capabilities.list is not None and caps.session_capabilities.resume is not None
        every = await agent.list_sessions()
        here = await agent.list_sessions(cwd="/repo")
        agent.zed_threads_only = True
        zed_only = await agent.list_sessions()
        await client.aclose()
    by_id = {s.session_id: s for s in every.sessions}
    assert list(by_id) == ["chat-zed-2-bbb", "chat-1790294944966-qkf93w", "chat-zed-1-aaa", "chat-1790-titled"]
    assert by_id["chat-zed-2-bbb"].title == "Where is the A2A executor?"  # preamble stripped
    assert by_id["chat-zed-2-bbb"].updated_at == "2026-09-24T10:00:00Z"
    assert by_id["chat-1790294944966-qkf93w"].title == "Plan the release notes"  # a console chat, titled from its first message
    assert by_id["chat-1790-titled"].title == "Server title"  # a server-side title wins
    assert by_id["chat-zed-1-aaa"].title == "Remembered title" and by_id["chat-zed-1-aaa"].cwd == "/other/folder"
    assert "chat-zed-1-aaa" not in [s.session_id for s in here.sessions]  # opened from /other/folder
    assert "chat-1790294944966-qkf93w" in [s.session_id for s in here.sessions]  # no recorded cwd: listed here
    assert [s.session_id for s in zed_only.sessions] == ["chat-zed-2-bbb", "chat-zed-1-aaa"]  # --zed-threads-only


async def test_load_a_console_chat_and_continue_it():
    with fa.FakeA2A() as fake:
        fake.turns["chat-1790294944966-qkf93w"] = [_durable_turn("t0", "Remember: codeword pineapple", "Noted.")]
        fake.script = lambda ctx, msg: [fa.task(ctx, tid="t1"), fa.text(ctx, "pineapple", append=False, tid="t1"), fa.done(ctx, tid="t1")]
        agent, conn, client = await _agent(fake)
        await agent.load_session(cwd="/", session_id="chat-1790294944966-qkf93w")
        await agent.prompt(prompt=[text_block("what was the codeword?")], session_id="chat-1790294944966-qkf93w")
        agent.zed_threads_only = True
        with pytest.raises(RequestError):
            await agent.load_session(cwd="/", session_id="chat-1790294944966-qkf93w")
        await client.aclose()
    assert conn.updates[0]["content"]["text"] == "Remember: codeword pineapple"
    assert fake.requests[0]["contextId"] == "chat-1790294944966-qkf93w"


async def test_load_replays_the_thread_and_continues_on_the_same_context():
    with fa.FakeA2A(roots={"p": "/repo"}) as fake:
        fake.turns["chat-zed-1-aaa"] = [
            _durable_turn("t0", "Read the README", "It says hello.",
                          tools=[("c1", "read_file", '{"project": "p", "path": "README.md", "offset": 3}', "hello")], preamble=True),
            _durable_turn("t1", "Thanks", "Any time."),
        ]
        fake.script = lambda ctx, msg: [fa.task(ctx, tid="t2"), fa.text(ctx, "Still here.", append=False, tid="t2"), fa.done(ctx, tid="t2")]
        agent, conn, client = await _agent(fake)
        await agent.load_session(cwd="/repo", session_id="chat-zed-1-aaa")
        replay = [(u["sessionUpdate"], u["title"] if u["sessionUpdate"] == "tool_call" else u["content"]["text"]) for u in conn.updates]
        resp = await agent.prompt(prompt=[text_block("what did the README say?")], session_id="chat-zed-1-aaa")
        await client.aclose()
    assert replay == [
        ("user_message_chunk", "Read the README"),
        ("tool_call", "Read p/README.md (from line 3)"),
        ("agent_message_chunk", "It says hello."),
        ("user_message_chunk", "Thanks"),  # the hitl "approved" answer is not replayed as a message
        ("agent_message_chunk", "Any time."),
    ]
    tool = next(u for u in conn.updates if u["sessionUpdate"] == "tool_call")
    assert tool["status"] == "completed" and tool["locations"] == [{"path": "/repo/README.md", "line": 3}]
    assert resp.stop_reason == "end_turn"
    sent = fake.requests[0]
    assert sent["contextId"] == "chat-zed-1-aaa"  # same A2A context: the agent keeps its memory
    assert sent["parts"][0]["text"] == "what did the README say?"  # no re-sent preamble


async def test_resume_registers_without_replay_and_unknown_threads_are_rejected():
    with fa.FakeA2A() as fake:
        fake.turns["chat-zed-1-aaa"] = [_durable_turn("t0", "hi", "hello")]
        fake.script = lambda ctx, msg: [fa.task(ctx), fa.done(ctx)]
        agent, conn, client = await _agent(fake)
        await agent.resume_session(cwd="/", session_id="chat-zed-1-aaa")
        assert conn.updates == []
        await agent.prompt(prompt=[text_block("again")], session_id="chat-zed-1-aaa")
        with pytest.raises(RequestError):
            await agent.load_session(cwd="/", session_id="chat-zed-9-missing")
        with pytest.raises(RequestError):
            await agent.load_session(cwd="/", session_id="chat-1790-console")  # unknown to the server
        await client.aclose()
    assert fake.requests[0]["contextId"] == "chat-zed-1-aaa"


async def test_steer_marker_sits_at_the_fold_even_when_output_raced_the_post():
    """Live navaEngineer: the tail of the pre-steer sentence arrived while the POST was in
    flight. With the steer_consumed boundary the marker lands where the agent READ it."""
    with fa.FakeA2A() as fake:
        fake.script = lambda ctx, msg: [
            fa.task(ctx),
            fa.text(ctx, "Reading A", append=False),
            fa.after_steer(fake, lambda t: fa.text(ctx, " to finish.", append=True), fold=False),
            fa.after_steer(fake, lambda t: fa.text(ctx, "Got it: B.", append=True)),
            fa.done(ctx),
        ]
        agent, conn, client = await _agent(fake, steer_grace=2.0)
        sess = await agent.new_session(cwd="/")
        first = await _running(agent, conn, sess.session_id)
        await agent.cancel(session_id=sess.session_id)
        await asyncio.wait_for(first, 1)
        await agent.prompt(prompt=[text_block("B please")], session_id=sess.session_id)
        await client.aclose()
    assert conn.text() == "Reading A to finish.\n\n↪ steering: B please\n\nGot it: B."


async def test_steer_marker_falls_back_to_turn_end_without_a_boundary_frame():
    with fa.FakeA2A() as fake:
        fake.script = lambda ctx, msg: [
            fa.task(ctx),
            fa.text(ctx, "A.", append=False),
            fa.after_steer(fake, lambda t: fa.text(ctx, " B.", append=True), boundary=False),
            fa.done(ctx),
        ]
        agent, conn, client = await _agent(fake, steer_grace=2.0)
        sess = await agent.new_session(cwd="/")
        first = await _running(agent, conn, sess.session_id)
        await agent.cancel(session_id=sess.session_id)
        await asyncio.wait_for(first, 1)
        await agent.prompt(prompt=[text_block("B")], session_id=sess.session_id)
        await client.aclose()
    assert conn.text().count("↪ steering: B") == 1


async def test_load_replays_a_send_now_steer_as_a_user_message_where_it_was_read():
    turn = _durable_turn("t0", "Survey the folders", "The README says hi.",
                         tools=[("c1", "list_dir", '{"project": "p", "path": "."}', "a/ b/")])
    turn["history"].insert(3, {"role": "ROLE_AGENT", "parts": [{"data": {"items": [{"id": "zed-1", "text": "Just the README"}]},
                                                                  "metadata": {"mimeType": fa.STEER_MIME}}]})
    with fa.FakeA2A(roots={"p": "/repo"}) as fake:
        fake.turns["chat-zed-1-aaa"] = [turn]
        agent, conn, client = await _agent(fake)
        await agent.load_session(cwd="/repo", session_id="chat-zed-1-aaa")
        await client.aclose()
    kinds = [(u["sessionUpdate"], u.get("title") or u["content"]["text"]) for u in conn.updates]
    assert kinds == [
        ("user_message_chunk", "Survey the folders"),
        ("tool_call", "List p/."),
        ("user_message_chunk", "Just the README"),
        ("agent_message_chunk", "The README says hi."),
    ]


async def test_turn_finishing_while_the_steer_is_being_adopted_still_answers_the_prompt():
    """Found by the stdio harness: the adopted turn ended while the adopting prompt was
    still flushing held output (s.runner already cleared) → an internal error."""
    with fa.FakeA2A() as fake:
        fake.script = lambda ctx, msg: [
            fa.task(ctx),
            fa.text(ctx, "A.", append=False),
            fa.after_steer(fake, lambda t: fa.text(ctx, " B.", append=True)),
            fa.done(ctx),
        ]
        agent, conn, client = await _agent(fake, steer_grace=3.0)
        sess = await agent.new_session(cwd="/")
        first = await _running(agent, conn, sess.session_id)
        await agent.cancel(session_id=sess.session_id)
        await asyncio.wait_for(first, 1)
        conn.delay = 0.15  # the whole rest of the turn lands while we flush
        r2 = await agent.prompt(prompt=[text_block("B")], session_id=sess.session_id)
        await client.aclose()
    assert r2.stop_reason == "end_turn"
    assert conn.text() == "A.\n\n↪ steering: B\n\n B."


# ── console → Zed hand-off ──────────────────────────────────────────────────────


async def _new_and_settle(agent, cwd="/Users/me/dev/nava"):
    resp = await agent.new_session(cwd=cwd)
    s = agent._sessions.get(resp.session_id)
    if s is not None and s.handoff is not None:
        await s.handoff
    return resp


async def test_handoff_claim_adopts_the_console_chat_and_replays_it():
    with fa.FakeA2A(roots={"rehearsal": "/Users/me/dev/nava/rehearsal"}) as fake:
        sid = "chat-1790294944966-qkf93w"
        fake.turns[sid] = [_durable_turn("t0", "Why does the assistant stop early?", "The loop exits on the first tool call.",
                                         tools=[("c1", "read_file", '{"project": "rehearsal", "path": "src/agent.ts", "offset": 40}', "…")])]
        fake.handoff = {"session_id": sid, "project": "rehearsal", "path": "src/agent.ts", "line": 42, "title": "Early-stop bug"}
        fake.script = lambda ctx, msg: [fa.task(ctx, tid="t1"), fa.text(ctx, "Fixing it.", append=False, tid="t1"), fa.done(ctx, tid="t1")]
        agent, conn, client = await _agent(fake)
        resp = await _new_and_settle(agent)
        replay = [(u["sessionUpdate"], u.get("title") or u["content"]["text"]) for u in conn.updates]
        await agent.prompt(prompt=[text_block("go ahead and fix it")], session_id=resp.session_id)
        await client.aclose()
    assert resp.session_id == sid  # the console chat's contextId, not a fresh chat-zed-…
    assert fake.claims == [{"cwd": "/Users/me/dev/nava"}]
    assert replay == [
        ("user_message_chunk", "Why does the assistant stop early?"),
        ("tool_call", "Read rehearsal/src/agent.ts (from line 40)"),
        ("agent_message_chunk", "The loop exits on the first tool call."),
        ("tool_call", "Open src/agent.ts"),
        ("agent_message_chunk", "\n\n\u21aa Continuing your console chat \u201cEarly-stop bug\u201d."),
    ]
    open_card = [u for u in conn.updates if u.get("title") == "Open src/agent.ts"][0]
    assert open_card["locations"] == [{"path": "/Users/me/dev/nava/rehearsal/src/agent.ts", "line": 42}]
    assert fake.requests[0]["contextId"] == sid
    assert "Zed editor" not in fake.requests[0]["parts"][0]["text"]  # continuing: no new-thread preamble
    assert agent.index.get(sid) == {"cwd": "/Users/me/dev/nava", "title": "Early-stop bug"}


@pytest.mark.parametrize("case", ["miss", "expired", "server_error", "unknown_session", "old_server"])
async def test_handoff_miss_starts_a_fresh_thread(case):
    with fa.FakeA2A() as fake:
        if case == "expired":
            fake.handoff = {"session_id": "chat-1-x", "title": "t"}
            fake.turns["chat-1-x"] = [_durable_turn("t0", "hi", "hello")]
            fake.handoff_expires = 0  # its 120 s ran out
        elif case == "server_error":
            fake.claim_status = 500
        elif case == "unknown_session":  # claimed, but the chat is gone from the task store
            fake.handoff = {"session_id": "chat-1-gone", "title": "t"}
        elif case == "old_server":
            fake.claim_status = 404
        agent, conn, client = await _agent(fake)
        resp = await _new_and_settle(agent)
        await client.aclose()
    assert resp.session_id.startswith("chat-zed-")
    assert conn.updates == [] and len(fake.claims) == 1


async def test_claim_happens_once_per_session_new():
    with fa.FakeA2A() as fake:
        fake.turns["chat-1-x"] = [_durable_turn("t0", "hi", "hello")]
        fake.handoff = {"session_id": "chat-1-x", "title": "t"}
        agent, _conn, client = await _agent(fake)
        first = await _new_and_settle(agent)
        second = await _new_and_settle(agent)  # the hand-off was one-shot
        await client.aclose()
    assert first.session_id == "chat-1-x" and second.session_id.startswith("chat-zed-")
    assert len(fake.claims) == 2


# ── busy signal: never interleave with a console turn ──────────────────────────


async def test_prompt_waits_while_the_console_is_mid_turn():
    with fa.FakeA2A() as fake:
        sid = "chat-1-x"
        fake.turns[sid] = [_durable_turn("t0", "hi", "hello")]
        fake.active[sid] = [True, True, True, False]  # 1st check + silent re-check, then polls
        fake.script = lambda ctx, msg: [fa.task(ctx), fa.text(ctx, "sent after", append=False), fa.done(ctx)]
        agent, conn, client = await _agent(fake)
        agent.busy_poll = 0.05
        await agent.resume_session(cwd="/", session_id=sid)
        resp = await agent.prompt(prompt=[text_block("next")], session_id=sid)
        await client.aclose()
    assert resp.stop_reason == "end_turn"
    assert conn.text() == "This chat is busy in the console. I'll send when it's free.\n\nsent after"
    assert fake.gets.count(f"/api/chat/sessions/{sid}") == 4 and len(fake.requests) == 1


async def test_busy_too_long_is_a_clear_error_and_nothing_is_sent():
    with fa.FakeA2A() as fake:
        sid = "chat-1-x"
        fake.turns[sid] = [_durable_turn("t0", "hi", "hello")]
        fake.active[sid] = [True]
        agent, _conn, client = await _agent(fake)
        agent.busy_poll, agent.busy_timeout = 0.05, 0.3
        await agent.resume_session(cwd="/", session_id=sid)
        with pytest.raises(RequestError) as ei:
            await agent.prompt(prompt=[text_block("next")], session_id=sid)
        await client.aclose()
    assert "stayed busy" in str(ei.value) and fake.requests == []


async def test_cancel_while_waiting_for_the_console():
    with fa.FakeA2A() as fake:
        sid = "chat-1-x"
        fake.turns[sid] = [_durable_turn("t0", "hi", "hello")]
        fake.active[sid] = [True]
        agent, _conn, client = await _agent(fake)
        agent.busy_poll = 0.05
        await agent.resume_session(cwd="/", session_id=sid)
        waiting = asyncio.create_task(agent.prompt(prompt=[text_block("next")], session_id=sid))
        await asyncio.sleep(0.2)
        await agent.cancel(session_id=sid)
        resp = await asyncio.wait_for(waiting, 2)
        await client.aclose()
    assert resp.stop_reason == "cancelled" and fake.requests == []


async def test_no_busy_signal_on_an_older_server_means_no_wait():
    with fa.FakeA2A() as fake:  # GET /api/chat/sessions/<id> → 405, as on v0.175.0
        fake.script = lambda ctx, msg: [fa.task(ctx), fa.text(ctx, "ok", append=False), fa.done(ctx)]
        agent, conn, client = await _agent(fake)
        sess = await agent.new_session(cwd="/")
        await agent.prompt(prompt=[text_block("x")], session_id=sess.session_id)
        await client.aclose()
    assert conn.text() == "ok"


async def test_a_momentary_busy_right_after_our_own_turn_is_not_announced():
    with fa.FakeA2A() as fake:
        sid = "chat-1-x"
        fake.turns[sid] = [_durable_turn("t0", "hi", "hello")]
        fake.active[sid] = [True, False]  # the tail of our previous turn, then free
        fake.script = lambda ctx, msg: [fa.task(ctx), fa.text(ctx, "ok", append=False), fa.done(ctx)]
        agent, conn, client = await _agent(fake)
        await agent.resume_session(cwd="/", session_id=sid)
        await agent.prompt(prompt=[text_block("x")], session_id=sid)
        await client.aclose()
    assert conn.text() == "ok"


# ── parked chats, running turns, and the replay barrier (integration-test findings) ──

FORM = {
    "kind": "form",
    "question": "Which environment should I deploy to?",
    "steps": [{"schema": {
        "properties": {
            "env": {"title": "Environment", "type": "string", "enum": ["staging", "prod"]},
            "notes": {"title": "Notes", "type": "string", "description": "anything else"},
        },
        "required": ["env"],
    }}],
}


def _parked_turn(tid, user, hitl, text=""):
    t = _durable_turn(tid, user, text, state="TASK_STATE_INPUT_REQUIRED")
    t["status"] = {"state": "TASK_STATE_INPUT_REQUIRED", "message": {"role": "ROLE_AGENT", "parts": [
        {"data": hitl, "metadata": {"mimeType": fa.HITL_MIME}}]}}
    t["history"] = t["history"][:-1]  # no hitl answer yet
    return t


def _texts(conn):
    return [u["content"]["text"] for u in conn.updates if u["sessionUpdate"] == "agent_message_chunk"]


async def test_loaded_form_park_is_shown_and_only_then_answered_by_the_next_prompt():
    with fa.FakeA2A() as fake:
        sid = "chat-1-form"
        fake.turns[sid] = [_parked_turn("t0", "Deploy it", FORM, text="Before I deploy:")]
        fake.active[sid] = [False]
        fake.last_state[sid] = "TASK_STATE_INPUT_REQUIRED"
        fake.script = lambda ctx, msg: [fa.text(ctx, "Deploying to staging.", append=False, tid="t0"), fa.done(ctx, tid="t0")]
        agent, conn, client = await _agent(fake)
        await agent.load_session(cwd="/", session_id=sid)
        shown = "\n".join(_texts(conn))
        await agent.prompt(prompt=[text_block("staging")], session_id=sid)
        await client.aclose()
    assert "**Which environment should I deploy to?**" in shown
    assert "- Environment (one of: staging, prod; required)" in shown and "- Notes (string) — anything else" in shown
    assert shown.endswith("This chat is waiting on a form from the console: Which environment should I deploy to? "
                          "Your next message here will be sent as the answer — or answer it in the console.")
    assert fake.requests[0]["taskId"] == "t0" and fake.requests[0]["metadata"] == {"hitl_resume": True}
    assert f"/api/chat/sessions/{sid}" in fake.gets  # the busy check ran on the resume path too


async def test_a_form_answered_in_the_console_meanwhile_is_not_answered_again():
    with fa.FakeA2A() as fake:
        sid = "chat-1-form"
        fake.turns[sid] = [_parked_turn("t0", "Deploy it", FORM)]
        fake.active[sid] = [False]
        agent, conn, client = await _agent(fake)
        await agent.load_session(cwd="/", session_id=sid)
        # … the operator answers the form in the console; the turn completes there.
        fake.turns[sid] = [_durable_turn("t0", "Deploy it", "Deployed to prod.")]
        fake.last_state[sid] = "TASK_STATE_COMPLETED"
        fake.script = lambda ctx, msg: [fa.task(ctx, tid="t1"), fa.text(ctx, "3 files.", append=False, tid="t1"), fa.done(ctx, tid="t1")]
        await agent.prompt(prompt=[text_block("How many files are in src?")], session_id=sid)
        await client.aclose()
    sent = fake.requests[0]
    assert "taskId" not in sent and not (sent.get("metadata") or {}).get("hitl_resume")  # a NEW turn, not the answer
    assert "Deployed to prod." in _texts(conn)  # the console's outcome shown first


async def test_an_approval_park_is_announced_but_never_answered_from_zed():
    with fa.FakeA2A() as fake:
        sid = "chat-1-appr"
        fake.turns[sid] = [_parked_turn("t0", "clean up", SHELL)]
        fake.script = lambda ctx, msg: [fa.task(ctx, tid="t1"), fa.done(ctx, tid="t1")]
        agent, conn, client = await _agent(fake)
        await agent.load_session(cwd="/", session_id=sid)
        await agent.prompt(prompt=[text_block("unrelated question")], session_id=sid)
        await client.aclose()
    assert any("waiting on an approval in the console" in t for t in _texts(conn))
    assert "taskId" not in fake.requests[0]  # "unrelated question" is not an approval


async def test_an_old_parked_turn_is_history_not_a_pending_question():
    with fa.FakeA2A() as fake:
        sid = "chat-1-old"
        fake.turns[sid] = [_parked_turn("t0", "first", {"question": "Which branch?"}), _durable_turn("t1", "moved on", "ok")]
        fake.script = lambda ctx, msg: [fa.task(ctx, tid="t2"), fa.done(ctx, tid="t2")]
        agent, conn, client = await _agent(fake)
        await agent.load_session(cwd="/", session_id=sid)
        await agent.prompt(prompt=[text_block("next")], session_id=sid)
        await client.aclose()
    assert not any("waiting on" in t for t in _texts(conn))
    assert "taskId" not in fake.requests[0]


async def test_resume_of_a_parked_chat_shows_the_question_after_the_response():
    with fa.FakeA2A() as fake:
        sid = "chat-1-q"
        fake.turns[sid] = [_parked_turn("t0", "go", {"question": "Which branch?"})]
        agent, conn, client = await _agent(fake)
        await agent.resume_session(cwd="/", session_id=sid)
        assert conn.updates == []  # nothing before the response (the barrier)
        await agent._sessions[sid].handoff
        await client.aclose()
    texts = _texts(conn)
    assert texts[0].startswith("**Which branch?**")
    assert "waiting on a question from the console: Which branch? Your next message" in texts[-1]


async def test_claim_of_a_parked_chat_replays_before_the_resumed_turn_streams():
    """The integration test: a prompt sent right after session/new raced the replay — the
    resumed turn's events arrived first. The barrier now holds every path."""
    with fa.FakeA2A() as fake:
        sid = "chat-1790294944966-qkf93w"
        fake.turns[sid] = [_parked_turn("t0", "Summarise briefly.", {"question": "Which file?"})]
        fake.handoff = {"session_id": sid, "title": "Summarise briefly."}
        fake.script = lambda ctx, msg: [fa.text(ctx, "Reading README.md.", append=False, tid="t0"), fa.done(ctx, tid="t0")]
        agent, conn, client = await _agent(fake)
        resp = await agent.new_session(cwd="/")
        await agent.prompt(prompt=[text_block("README.md")], session_id=resp.session_id)  # no settle: race it
        await client.aclose()
    texts = _texts(conn)
    assert texts.index("\n\n\u21aa Continuing your console chat \u201cSummarise briefly.\u201d") < texts.index("Reading README.md.")
    assert any("waiting on a question from the console: Which file" in t for t in texts)
    assert fake.requests[0]["taskId"] == "t0"  # announced, then answered — once


async def test_a_running_console_turn_is_not_replayed_half_written_and_is_finished_before_sending():
    with fa.FakeA2A() as fake:
        sid = "chat-1-run"
        running = _durable_turn("t1", "Explain the loop", "The loop begins by", state="TASK_STATE_WORKING")
        fake.turns[sid] = [_durable_turn("t0", "hi", "hello"), running]
        fake.active[sid] = [True, True, True, False]

        def finish(_sid, active):
            if not active:
                fake.turns[sid] = [_durable_turn("t0", "hi", "hello"),
                                   _durable_turn("t1", "Explain the loop", "The loop begins by reading the queue.")]

        fake.on_summary = finish
        fake.script = lambda ctx, msg: [fa.task(ctx, tid="t2"), fa.text(ctx, "Next answer.", append=False, tid="t2"), fa.done(ctx, tid="t2")]
        agent, conn, client = await _agent(fake)
        agent.busy_poll = 0.05
        await agent.load_session(cwd="/", session_id=sid)
        loaded = _texts(conn)
        await agent.prompt(prompt=[text_block("and then?")], session_id=sid)
        await client.aclose()
    assert loaded == ["hello", "(still running in the console…)"]  # never the half-written text
    texts = _texts(conn)
    finished = texts.index("The loop begins by reading the queue.")
    assert texts.index("This chat is busy in the console. I'll send when it's free.\n\n") < finished < texts.index("Next answer.")
    users = [u["content"]["text"] for u in conn.updates if u["sessionUpdate"] == "user_message_chunk"]
    assert users.count("Explain the loop") == 1  # finishing the turn doesn't repeat its message


async def test_a_console_turn_ending_on_a_new_form_does_not_swallow_the_message():
    with fa.FakeA2A() as fake:
        sid = "chat-1-new-form"
        fake.turns[sid] = [_durable_turn("t0", "hi", "hello")]
        fake.active[sid] = [True, True, True, False]

        def park(_sid, active):
            if not active:
                fake.turns[sid] = [_durable_turn("t0", "hi", "hello"), _parked_turn("t1", "deploy", FORM)]

        fake.on_summary = park
        agent, conn, client = await _agent(fake)
        agent.busy_poll = 0.05
        await agent.resume_session(cwd="/", session_id=sid)
        resp = await agent.prompt(prompt=[text_block("How many files are in src?")], session_id=sid)
        await client.aclose()
    assert resp.stop_reason == "end_turn" and fake.requests == []  # not sent — and not taken as the form's answer
    texts = _texts(conn)
    assert any("waiting on a form from the console" in t for t in texts)
    assert texts[-1].startswith("\n\nYour message wasn't sent")


def test_continuing_notice_never_doubles_the_full_stop():
    from protoagent_acp.agent import continuing_notice

    assert continuing_notice("Summarise the README briefly.") == "\u21aa Continuing your console chat \u201cSummarise the README briefly.\u201d"
    assert continuing_notice("Fix the bug") == "\u21aa Continuing your console chat \u201cFix the bug\u201d."
    assert continuing_notice("Really?") .endswith("\u201d")
    assert continuing_notice("") == "\u21aa Continuing your console chat."
