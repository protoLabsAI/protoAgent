"""deck.a2a — the deck's A2A client and frame reducer (#3469).

The reducer cases are ported from the console (api.test.ts / reattach.test.ts /
parts.test.ts / turnReducers): each is a wire fact that bit the browser once.
"""

from __future__ import annotations

import json

import httpx
import pytest

from deck import a2a
from deck import hub as deckhub

TOOL = a2a.TOOL_CALL_EXT_URI
COST = a2a.COST_EXT_URI
CID = "chat-1789169255449-mw1pz8"


# ── frame builders ────────────────────────────────────────────────────────────


def status(state="TASK_STATE_WORKING", *, parts=None, metadata=None, final=False, task_id="t1", cid=CID):
    msg: dict = {"role": "ROLE_AGENT", "messageId": "m", "parts": parts or []}
    if metadata:
        msg["metadata"] = metadata
    return {"result": {"statusUpdate": {"taskId": task_id, "contextId": cid, "status": {"state": state, "message": msg}, "final": final}}}


def artifact(text, *, append=None, last=False, metadata=None, parts=None, cid=CID):
    art: dict = {"artifactId": "t1-answer", "parts": parts if parts is not None else [{"text": text}]}
    if metadata:
        art["metadata"] = metadata
    upd: dict = {"taskId": "t1", "contextId": cid, "artifact": art}
    if append is not None:
        upd["append"] = append
    if last:
        upd["lastChunk"] = True
    return {"result": {"artifactUpdate": upd}}


def tool(phase, tid="c1", name="run_command", **extra):
    return {TOOL: {"toolCallId": tid, "name": name, "phase": phase, **extra}}


def data_part(mime, payload, encoding="flat"):
    if encoding == "flat":
        return {"data": payload, "metadata": {"mimeType": mime}}
    if encoding == "case":
        return {"content": {"$case": "data", "value": payload}, "metadata": {"mimeType": mime}}
    return {"kind": "data", "data": payload, "metadata": {"mimeType": mime}}


def fold(*frames, cid=CID):
    turn = a2a.Turn(context_id=cid)
    for f in frames:
        a2a.apply_frame(turn, f)
    return turn


# ── states / ids ──────────────────────────────────────────────────────────────


def test_state_normalization_and_terminal_set():
    assert a2a.norm_state("TASK_STATE_INPUT_REQUIRED") == "input-required"
    assert a2a.norm_state("input_required") == "input-required"
    assert a2a.is_terminal("TASK_STATE_COMPLETED") and a2a.is_terminal("canceled") and a2a.is_terminal("TASK_STATE_FAILED")
    assert not a2a.is_terminal("TASK_STATE_WORKING") and not a2a.is_terminal("input-required")
    assert a2a.is_paused("TASK_STATE_INPUT_REQUIRED") and a2a.is_paused("auth_required")


def test_session_id_matches_the_console_shape():
    sid = a2a.new_session_id()
    assert sid.startswith("chat-") and len(sid.split("-")) == 3 and len(sid.split("-")[2]) == 6


# ── text: append only on explicit true; terminal replace does not double ──────


def test_text_appends_only_on_explicit_true_and_terminal_replace_is_idempotent():
    t = fold(artifact("Hello", append=False), artifact(" world", append=True), artifact("Hello world", last=True))
    assert t.content == "Hello world"
    assert a2a.text_runs(t.parts) == ["Hello world"]  # the replace was a no-op: no doubling
    # absence of `append` (proto3 default) is a REPLACE, never an append
    t = fold(artifact("A", append=True), artifact("A"), artifact("A"))
    assert t.content == "A" and a2a.text_runs(t.parts) == ["A"]


def test_replace_keeps_interleaving_when_parts_already_render_the_text():
    t = fold(
        artifact("Let me check.", append=True),
        status(metadata=tool("started")),
        status(metadata=tool("completed", result="ok")),
        artifact("\n\nDone.", append=True),
        artifact("Let me check.\n\nDone.", last=True),
    )
    kinds = [p.kind for p in t.parts]
    assert kinds == ["text", "tools", "text"]  # preamble → cards → answer, preserved
    assert a2a.text_runs(t.parts) == ["Let me check.", "Done."]


def test_replace_on_real_divergence_rebuilds_once():
    t = fold(artifact("partial", append=True), status(metadata=tool("started")), artifact("the full canonical answer", last=True))
    assert a2a.text_runs(t.parts) == ["the full canonical answer"]
    assert [p.kind for p in t.parts] == ["tools", "text"]
    assert t.content == "the full canonical answer"


def test_whitespace_only_delta_between_tool_calls_does_not_split_the_group():
    t = fold(status(metadata=tool("started", "a")), artifact("\n", append=True), status(metadata=tool("started", "b")))
    assert [p.kind for p in t.parts] == ["tools"] and t.parts[0].ids == ["a", "b"]


# ── tool calls ────────────────────────────────────────────────────────────────


def test_tool_events_pair_start_and_end_with_duration_and_failure():
    t = fold(
        status(metadata=tool("started", args='{"cmd": "ls"}')),
        status(metadata=tool("completed", result="file.txt", outputChars=1234.0)),
        status(metadata=tool("started", "c2", "fetch_url")),
        status(metadata=tool("failed", "c2", "fetch_url", error="timeout")),
    )
    c1, c2 = t.tool_calls
    assert (c1.name, c1.input, c1.output, c1.status, c1.output_chars) == ("run_command", '{"cmd": "ls"}', "file.txt", "done", 1234)
    assert c1.duration_ms is not None and c1.duration_ms >= 0
    assert (c2.status, c2.output) == ("error", "timeout")
    assert [p.kind for p in t.parts] == ["tools"] and t.parts[0].ids == ["c1", "c2"]


def test_subagent_tool_nests_under_its_task_by_parent_id_and_by_open_task_fallback():
    t = fold(
        status(metadata=tool("started", "task1", "task")),
        status(metadata=tool("started", "child1", "read_file", parentToolCallId="task1")),
        status(metadata=tool("started", "child2", "search_files")),  # older server: no parent id → last open task
        status(metadata=tool("completed", "child1", "read_file", result="…")),
        status(metadata=tool("completed", "task1", "task", result="done")),
    )
    assert t.tool("child1").parent_id == "task1" and t.tool("child2").parent_id == "task1"
    assert [c.id for c in t.top_level_tools()] == ["task1"]
    assert [c.id for c in t.children_of("task1")] == ["child1", "child2"]
    assert t.parts[0].ids == ["task1"]  # children never open their own block


def test_missed_start_still_renders_on_end():
    t = fold(status(metadata=tool("completed", "ghost", "web_search", result="r")))
    assert t.tool("ghost").status == "done" and t.parts[0].ids == ["ghost"]


# ── reasoning, components, room replies, steers ───────────────────────────────


@pytest.mark.parametrize("encoding", ["flat", "case", "legacy"])
def test_reasoning_streams_inline_in_every_datapart_encoding(encoding):
    t = fold(
        status(parts=[data_part(a2a.REASONING_MIME, {"text": "thinking "}, encoding)]),
        status(parts=[data_part(a2a.REASONING_MIME, {"text": "hard"}, encoding)]),
        status(metadata=tool("started")),
        status(parts=[data_part(a2a.REASONING_MIME, {"text": "more"}, encoding)]),
    )
    assert t.reasoning == "thinking hardmore"
    assert [p.kind for p in t.parts] == ["reasoning", "tools", "reasoning"]
    assert t.status_text == "working"  # a reasoning-only frame never clobbers the status line with itself


def test_component_room_reply_and_consumed_steers_are_captured():
    t = fold(
        status(parts=[data_part(a2a.COMPONENT_MIME, {"component": "table", "props": {"rows": []}})]),
        status(parts=[data_part(a2a.ROOM_MIME, {"author": "sonnet", "text": "1 replied"})]),
        status(parts=[data_part(a2a.STEER_CONSUMED_MIME, {"items": [{"id": "s1", "text": "switch to jellyfish"}, {"bad": 1}]})]),
    )
    assert t.components == [{"component": "table", "props": {"rows": []}}] and t.parts[0].kind == "component"
    assert t.room_replies[0]["author"] == "sonnet"
    assert t.consumed_steers == [{"id": "s1", "text": "switch to jellyfish"}]


# ── HITL, failure, cost, context, terminal ────────────────────────────────────


def test_input_required_surfaces_the_hitl_payload_and_is_not_terminal():
    form = {"kind": "form", "title": "Which store?", "steps": [{"schema": {"type": "object", "properties": {}}}]}
    t = fold(status("TASK_STATE_INPUT_REQUIRED", parts=[{"text": "Which store?"}, data_part(a2a.HITL_MIME, form)]))
    assert t.hitl == form and t.state == "input-required" and not t.done
    # a plain ask_human pause without the DataPart degrades to the prompt text
    t = fold(status("input-required", parts=[{"text": "Merge this PR?"}]))
    assert t.hitl == {"question": "Merge this PR?"}


def test_failed_state_and_cost_and_context_and_done():
    meta = {COST: {"usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 800}, "costUsd": 0.0123, "durationMs": 4200}}
    ctx = data_part(a2a.CONTEXT_MIME, {"contextTokens": 5400, "maxTokens": 200000})
    t = fold(
        artifact("answer", append=True),
        artifact("answer", last=True, metadata=meta, parts=[{"text": "answer"}, ctx]),
        status("TASK_STATE_COMPLETED", final=True),
    )
    assert t.done and t.state == "completed"
    assert (t.usage.input_tokens, t.usage.output_tokens, t.usage.cache_read_tokens, t.usage.cost_usd, t.usage.duration_ms) == (1200, 300, 800, 0.0123, 4200)
    assert t.usage.total_tokens == 1500 and t.context["contextTokens"] == 5400
    f = fold(status("TASK_STATE_FAILED", parts=[{"text": "boom"}]))
    assert f.done and f.failed == "boom"


def test_json_rpc_error_frame_raises():
    with pytest.raises(a2a.TurnError, match="nope"):
        fold({"error": {"message": "nope"}})


# ── foreign frames and 0.3 shapes ─────────────────────────────────────────────


def test_foreign_context_frames_are_dropped_but_unstamped_ones_are_not():
    t = fold(artifact("mine", append=True), artifact("theirs", append=True, cid="chat-other"))
    assert t.content == "mine"
    flat03 = {"result": {"kind": "artifact-update", "taskId": "t1", "artifact": {"parts": [{"text": " more"}]}, "append": True}}
    t = fold(artifact("mine", append=True), flat03)
    assert t.content == "mine more"
    su03 = {"result": {"kind": "status-update", "taskId": "t1", "status": {"state": "completed"}, "final": True}}
    assert fold(su03).done


# ── snapshot replay (resubscribe / GetTask / durable turns) ───────────────────


def test_task_snapshot_replays_history_then_text_and_a_parked_state():
    snap = {
        "result": {
            "task": {
                "id": "t9",
                "contextId": CID,
                "status": {"state": "TASK_STATE_INPUT_REQUIRED", "message": {"parts": [{"text": "Approve?"}, data_part(a2a.HITL_MIME, {"kind": "approval", "title": "Approve shell command?", "detail": "rm -rf build"})]}},
                "history": [
                    {"role": "ROLE_USER", "parts": [{"text": "clean the build dir"}]},
                    {"role": "ROLE_AGENT", "parts": [data_part(a2a.REASONING_MIME, {"text": "ok"})]},
                    {"role": "ROLE_AGENT", "parts": [], "metadata": tool("started", "c1", "run_command", args="rm -rf build")},
                ],
                "artifacts": [{"artifactId": "t9-answer", "parts": [{"text": "Cleaning now."}]}],
            }
        }
    }
    t = fold(snap)
    assert t.task_id == "t9" and t.state == "input-required" and not t.done
    assert t.hitl["kind"] == "approval" and t.hitl["detail"] == "rm -rf build"
    assert [p.kind for p in t.parts] == ["reasoning", "tools", "text"]
    assert t.tool("c1").status == "running" and t.content == "Cleaning now."


def test_turn_from_durable_replays_a_turns_row_with_tool_cards():
    """The exact shape GET /api/chat/sessions/<id>/turns returns (verified live on
    protoEngineer 2026-09-11: an @sonnet mention started then completed)."""
    row = {
        "task_id": "31373db0-a53",
        "state": "TASK_STATE_COMPLETED",
        "status": {"state": "TASK_STATE_COMPLETED"},
        "text": "Let me gather the current state.",
        "artifacts": [{"artifactId": "x", "parts": [{"text": "Let me gather the current state."}], "metadata": {COST: {"usage": {"input_tokens": 10, "output_tokens": 2}}}}],
        "history": [
            {"role": "ROLE_USER", "parts": [{"text": "status report please"}]},
            {"role": "ROLE_AGENT", "parts": [], "metadata": {TOOL: {"phase": "started", "name": "@sonnet", "toolCallId": "mention:sonnet", "args": "report your status"}}},
            {"role": "ROLE_AGENT", "parts": [], "metadata": {TOOL: {"phase": "completed", "result": "1 replied", "toolCallId": "mention:sonnet", "name": "@sonnet"}}},
        ],
    }
    t = a2a.turn_from_durable(CID, row)
    assert t.done and t.task_id == "31373db0-a53"
    assert t.tool("mention:sonnet").status == "done" and t.tool("mention:sonnet").output == "1 replied"
    assert t.content == "Let me gather the current state." and t.usage.input_tokens == 10
    assert a2a.user_text_from_durable(row) == "status report please"


# ── the client ────────────────────────────────────────────────────────────────


def _sse(*frames: dict, extra: str = "") -> str:
    body = ": connected\n\n"
    for f in frames:
        body += f"data: {json.dumps(f)}\n\n"
    return body + extra


def test_stream_sends_the_1_0_wire_and_yields_frames():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_sse(status(), artifact("hi", append=True), status("TASK_STATE_COMPLETED", final=True), extra=": keepalive\n\n"))

    c = a2a.A2AClient("http://127.0.0.1:7870", "tok", slug="protoEngineer-ba4c", transport=httpx.MockTransport(handler))
    frames = list(c.stream("hello", context_id=CID, metadata={"hitl_resume": True}, task_id="t1"))
    assert len(frames) == 3
    assert seen["path"] == "/agents/protoEngineer-ba4c/a2a"
    assert seen["headers"]["a2a-version"] == "1.0" and seen["headers"]["authorization"] == "Bearer tok"
    msg = seen["body"]["params"]["message"]
    assert seen["body"]["method"] == "SendStreamingMessage"
    assert (msg["role"], msg["parts"], msg["contextId"], msg["taskId"], msg["metadata"]) == ("ROLE_USER", [{"text": "hello"}], CID, "t1", {"hitl_resume": True})
    t = a2a.Turn(context_id=CID)
    for f in frames:
        a2a.apply_frame(t, f)
    assert t.done and t.content == "hi"


def test_sse_parser_handles_multiline_data_and_ignores_comments():
    body = ": connected\n\ndata: {\"a\":\ndata: 1}\n\n: keepalive\n\ndata: not json\n\ndata: {\"b\": 2}\n\n"
    assert list(a2a._parse_sse(iter(body.splitlines()))) == [{"a": 1}, {"b": 2}]


def test_for_member_routes_through_the_hub_proxy_and_the_host_directly():
    hub = deckhub.HubClient("http://127.0.0.1:7870", "tok", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert a2a.A2AClient.for_member(hub, "protoEngineer-ba4c").endpoint == "http://127.0.0.1:7870/agents/protoEngineer-ba4c/a2a"
    assert a2a.A2AClient.for_member(hub, "host").endpoint == "http://127.0.0.1:7870/a2a"


def test_member_401_on_the_proxy_is_the_members_and_a_5xx_is_a_request_error():
    c = a2a.A2AClient("http://127.0.0.1:7870", "tok", slug="roxy-e815", transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(deckhub.MemberUnauthorized) as ei:
        list(c.stream("x", context_id=CID))
    assert ei.value.slug == "roxy-e815"
    c = a2a.A2AClient("http://127.0.0.1:7870", "tok", transport=httpx.MockTransport(lambda r: httpx.Response(503, text="down")))
    with pytest.raises(deckhub.HubRequestError):
        c.get_task("t1")


def test_get_task_reads_the_flat_1_0_result_and_cancel_posts_cancel_task():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "GetTask":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": "x", "result": {"id": "t1", "status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": []}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "x", "result": {"id": "t1", "status": {"state": "TASK_STATE_CANCELED"}}})

    c = a2a.A2AClient("http://127.0.0.1:7870", transport=httpx.MockTransport(handler))
    assert c.get_task("t1")["status"]["state"] == "TASK_STATE_COMPLETED"
    c.cancel("t1")
    assert calls == ["GetTask", "CancelTask"]


def test_read_timeout_on_the_stream_is_stream_stalled_and_abort_closes_the_response():
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no frame", request=request)

    c = a2a.A2AClient("http://127.0.0.1:7870", transport=httpx.MockTransport(slow))
    with pytest.raises(a2a.StreamStalled):
        list(c.stream("x", context_id=CID))
    assert c._active is None
    c.abort()  # nothing open → no-op, never raises
    assert a2a.STREAM_READ_S >= 45


def test_abort_wakes_a_reader_blocked_on_a_real_socket():
    """Round-2 blocker: Response.close() from another thread does not wake a reader in
    recv(); shutting the socket does. And ONLY the shutdown: closing the response (the fd)
    right behind it from the aborting thread races the wake-up on macOS — the poller finds
    its fd gone and sleeps out the whole read timeout — about 1 abort in 14. So this runs
    the abort many times against a real loopback server that sends one frame then sleeps;
    a regression to shutdown+close fails it with ~90% probability."""
    import socketserver
    import threading
    import time
    from http.server import BaseHTTPRequestHandler

    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")  # as uvicorn streams: EOF mid-body is an error, not a clean end
            self.end_headers()
            frame = b'data: {"result": {"task": {"id": "t1", "contextId": "s"}}}\n\n'
            self.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
            self.wfile.flush()
            release.wait(8.0)  # silence — the client must not have to wait for this

        def log_message(self, *a):  # quiet
            pass

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = Server(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for attempt in range(30):
            c = a2a.A2AClient(f"http://127.0.0.1:{port}")
            got: list = []
            err: list = []

            def reader(c=c, got=got, err=err):
                try:
                    for f in c.stream("x", context_id="s"):
                        got.append(f)
                except Exception as exc:  # noqa: BLE001
                    err.append(exc)

            t = threading.Thread(target=reader)
            t.start()
            deadline = time.monotonic() + 3.0
            while not got and time.monotonic() < deadline:
                time.sleep(0.005)
            assert got, f"first frame never arrived (attempt {attempt})"
            t0 = time.monotonic()
            c.abort()
            t.join(3.0)
            assert not t.is_alive(), f"reader still blocked after abort() (attempt {attempt})"
            assert time.monotonic() - t0 < 2.0
            assert err and isinstance(err[0], deckhub.HubUnreachable)
            c.close()
    finally:
        release.set()
        srv.shutdown()
        srv.server_close()


def test_for_member_owns_its_transport_so_close_never_drains_the_hubs_pool():
    hub = deckhub.HubClient("http://127.0.0.1:7870", "tok", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    c = a2a.A2AClient.for_member(hub, "protoEngineer-ba4c")
    assert c._client._transport is not hub._client._transport
    c.close()  # the hub's transport is untouched
    assert hub.agent_card() is not None or hub._client._transport is not None  # the hub client still works


def test_rpc_error_body_raises_turn_error():
    c = a2a.A2AClient("http://127.0.0.1:7870", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"error": {"code": -32001, "message": "TaskNotFound"}})))
    with pytest.raises(a2a.TurnError, match="TaskNotFound"):
        c.get_task("nope")


def test_credential_over_plain_http_off_box_is_refused_for_a2a_too():
    with pytest.raises(deckhub.InsecureHub):
        a2a.A2AClient("http://ava.tail:7870", "tok", slug="x", transport=httpx.MockTransport(lambda r: httpx.Response(200)))


# ── the stall watchdog ────────────────────────────────────────────────────────


def test_watchdog_finalizes_only_a_terminal_task_after_the_idle_window():
    t = a2a.Turn(context_id=CID, task_id="t1")
    t.last_frame_at = 100.0
    assert a2a.stalled_turn_is_terminal(t, lambda tid: {"status": {"state": "TASK_STATE_COMPLETED"}}, now=120.0) is None  # not idle yet
    assert a2a.stalled_turn_is_terminal(t, lambda tid: {"status": {"state": "TASK_STATE_WORKING"}}, now=200.0) is None  # legitimately quiet
    assert a2a.stalled_turn_is_terminal(t, lambda tid: {"status": {"state": "TASK_STATE_INPUT_REQUIRED"}}, now=200.0) is None  # paused ≠ terminal
    assert a2a.stalled_turn_is_terminal(t, lambda tid: {"status": {"state": "TASK_STATE_COMPLETED"}}, now=200.0)["status"]["state"] == "TASK_STATE_COMPLETED"
    assert a2a.stalled_turn_is_terminal(t, lambda tid: {}, now=200.0) == {}  # gone from the store → un-stick

    def boom(tid):
        raise RuntimeError("unreachable")

    assert a2a.stalled_turn_is_terminal(t, boom, now=200.0) is None  # unknown → re-arm
    t.done = True
    assert a2a.stalled_turn_is_terminal(t, lambda tid: {"status": {"state": "TASK_STATE_COMPLETED"}}, now=999.0) is None
