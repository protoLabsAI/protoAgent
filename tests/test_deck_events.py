"""deck.events — member event streams and the fleet fan-in (#3470)."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from deck import events
from deck import hub as deckhub


def _sse(*frames, ids=True, extra=""):
    body = ": connected\n\n"
    for i, f in enumerate(frames, start=1):
        if ids:
            body += f"id: {f.get('seq', i)}\n"
        body += f"data: {json.dumps(f)}\n\n"
    return body + extra


def test_parse_sse_reads_id_lines_multiline_data_and_skips_comments():
    body = "id: 7\ndata: {\"topic\": \"turn.usage\",\ndata: \"data\": {\"cost_usd\": 0.1}, \"seq\": 7}\n\n: keepalive\n\ndata: not json\n\ndata: {\"topic\": \"x\", \"data\": {}, \"seq\": 9}\n\n"
    got = list(events.parse_sse(iter(body.splitlines())))
    assert got[0][0] == 7 and got[0][1]["topic"] == "turn.usage"
    assert got[1] == (9, {"topic": "x", "data": {}, "seq": 9})  # seq from the payload when no id: line


def test_member_events_yields_events_sends_bearer_and_replays_since():
    seen: list[dict] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        seen.append({"auth": request.headers.get("Authorization"), "path": request.url.path, "since": request.url.params.get("since"), "leid": request.headers.get("Last-Event-ID")})
        if calls["n"] == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_sse({"topic": "turn.usage", "data": {"cost_usd": 0.5}, "seq": 41}, {"topic": "chat.progress", "data": {"phase": "text", "session_id": "s", "text": "hi"}, "seq": 42}))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_sse({"topic": "turn.finished", "data": {"session_id": "s"}, "seq": 43}))

    m = events.MemberEvents("http://127.0.0.1:7870", "tok", "protoEngineer-ba4c", transport=httpx.MockTransport(handler))
    out: list[events.Event] = []
    for ev in m.events():
        out.append(ev)
        if len(out) == 3:
            m.stop()
    assert [e.topic for e in out] == ["turn.usage", "chat.progress", "turn.finished"]
    assert out[0].slug == "protoEngineer-ba4c" and out[0].seq == 41 and out[1].data["text"] == "hi"
    assert seen[0] == {"auth": "Bearer tok", "path": "/agents/protoEngineer-ba4c/api/events", "since": None, "leid": None}
    # the reconnect (after the first stream closed cleanly) replayed from the last seq
    assert seen[1]["since"] == "42" and seen[1]["leid"] == "42"
    m.close()


def test_member_events_backs_off_on_transport_errors_and_stops_on_401(monkeypatch):
    monkeypatch.setattr(events, "_BACKOFF_S", (0.01, 0.01))
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_sse({"topic": "turn.usage", "data": {}, "seq": 1}))

    m = events.MemberEvents("http://127.0.0.1:7870", "tok", "x", transport=httpx.MockTransport(flaky))
    got = []
    for ev in m.events():
        got.append(ev)
        m.stop()
    assert calls["n"] == 3 and len(got) == 1 and "refused" in m.last_error
    m.close()

    m = events.MemberEvents("http://127.0.0.1:7870", "tok", "roxy-e815", transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    assert list(m.events()) == []
    assert "roxy-e815" in m.last_error and "rejected" in m.last_error  # final: no retry loop
    m.close()


def test_member_events_refuses_a_credential_over_plain_http_off_box():
    with pytest.raises(deckhub.InsecureHub):
        events.MemberEvents("http://ava.tail:7870", "tok", "x")


def test_stop_wakes_a_reader_blocked_on_a_real_socket():
    """``stop()`` shuts the socket and ONLY that: closing the response (the fd) behind it
    from the stopping thread races the wake-up on macOS and the reader sleeps out the whole
    read window (~1 in 14). Many stops against a chunked loopback stream — as uvicorn
    streams — so a regression fails with ~90% probability."""
    import socketserver
    from http.server import BaseHTTPRequestHandler

    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            frame = b'id: 1\ndata: {"topic": "turn.usage", "data": {}, "seq": 1}\n\n'
            self.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
            self.wfile.flush()
            release.wait(8.0)

        def log_message(self, *a):
            pass

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for attempt in range(30):
            m = events.MemberEvents(f"http://127.0.0.1:{srv.server_address[1]}", None, "host")
            got: list = []
            t = threading.Thread(target=lambda m=m, got=got: got.extend(m.events()))
            t.start()
            deadline = time.monotonic() + 3.0
            while not got and time.monotonic() < deadline:
                time.sleep(0.005)
            assert got and got[0].topic == "turn.usage", f"attempt {attempt}"
            t0 = time.monotonic()
            m.stop()
            t.join(3.0)
            assert not t.is_alive() and time.monotonic() - t0 < 2.0, f"reader still blocked after stop() (attempt {attempt})"
            m.close()
    finally:
        release.set()
        srv.shutdown()
        srv.server_close()


def test_fleet_events_fans_in_and_reconciles_the_watched_set():
    def handler(request: httpx.Request) -> httpx.Response:
        slug = request.url.path.split("/")[2] if request.url.path.startswith("/agents/") else "host"
        if slug == "dead":
            raise httpx.ConnectError("refused", request=request)
        body = _sse({"topic": "turn.usage", "data": {"who": slug}, "seq": 1})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    hub = deckhub.HubClient("http://127.0.0.1:7870", "tok", transport=httpx.MockTransport(handler))
    fe = events.FleetEvents(hub, transport=httpx.MockTransport(handler))
    fe.watch(["host", "a", "b"])
    deadline = time.monotonic() + 3.0
    got: list[events.Event] = []
    while len(got) < 3 and time.monotonic() < deadline:
        got.extend(fe.drain())
        time.sleep(0.02)
    assert sorted(e.data["who"] for e in got) == ["a", "b", "host"]
    fe.watch(["a", "dead"])  # host and b stopped, dead spawned (and keeps backing off)
    st = fe.status()
    assert set(st) == {"a", "dead"}
    fe.close()
    assert fe.status() == {}


def test_parse_progress_mirrors_the_console():
    base = {"session_id": "s", "task_id": "t"}
    assert events.parse_progress({}) is None
    p = events.parse_progress({**base, "phase": "tool_start", "tool": "run_command", "tool_call_id": "c1", "control": {"operator_controllable": True}})
    assert (p.kind, p.name, p.tool_id, p.done, p.control["operator_controllable"]) == ("tool", "run_command", "c1", False, True)
    p = events.parse_progress({**base, "phase": "tool_end", "tool": "run_command", "tool_call_id": "c1", "output": "3 open", "error": True})
    assert (p.done, p.output, p.error) == (True, "3 open", True)
    assert events.parse_progress({**base, "phase": "tool_end", "tool": "x"}) is None  # no id → can't pair
    assert events.parse_progress({**base, "phase": "text", "text": ""}) is None
    assert events.parse_progress({**base, "phase": "text", "text": "hi"}).text == "hi"
    r = events.parse_progress({**base, "phase": "room_reply", "message_id": "m1", "author": "sonnet", "text": "1 replied", "ok": False})
    assert (r.kind, r.author, r.ok) == ("room", "sonnet", False)
    a = events.parse_progress({**base, "phase": "room_reply", "message_id": "m2", "addressed_to": "sonnet", "text": "report"})
    assert (a.kind, a.addressed_to) == ("ask", "sonnet")
    assert events.parse_progress({**base, "phase": "room_reply", "message_id": "m3"}) is None
    s = events.parse_progress({**base, "phase": "steer_consumed", "items": [{"id": "s1", "text": "go"}, {"id": "", "text": "x"}]})
    assert s.kind == "steer" and s.items == [{"id": "s1", "text": "go"}]
    assert events.parse_progress({**base, "phase": "steer_consumed", "items": []}) is None
    assert events.parse_progress({**base, "phase": "turn_started"}) is None


def test_a_redirect_or_an_error_body_is_an_error_not_a_clean_empty_stream(monkeypatch):
    """Reviewer findings: a 3xx (an http→https front) read as a clean close and reconnected
    every second forever; a 4xx raised ResponseNotRead from `r.text` on the unread stream,
    hiding the real status."""
    monkeypatch.setattr(events, "_BACKOFF_S", (0.01, 0.01))
    calls = {"n": 0}

    def redirect_then_ok(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(302, headers={"location": "https://hub/agents/x/api/events"})
        if calls["n"] == 2:
            return httpx.Response(409, json={"detail": "agent is not running"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_sse({"topic": "turn.usage", "data": {}, "seq": 1}))

    m = events.MemberEvents("http://127.0.0.1:7870", "tok", "x", transport=httpx.MockTransport(redirect_then_ok))
    got = []
    for ev in m.events():
        got.append(ev)
        m.stop()
    assert calls["n"] == 3 and len(got) == 1
    assert "409" in m.last_error and "agent is not running" in m.last_error and not m.gave_up
    m.close()


def test_attendance_gives_up_on_a_member_without_the_route_and_holds_otherwise():
    calls = {"n": 0}

    def missing(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path == "/agents/x/api/chat/attend" and request.url.params.get("session") == "chat-1"
        assert request.headers.get("Accept") == "text/event-stream" and request.headers.get("Authorization") == "Bearer tok"
        return httpx.Response(404, json={"detail": "Not Found"})

    a = events.Attendance("http://127.0.0.1:7870", "tok", "x", "chat-1", transport=httpx.MockTransport(missing)).start()
    a._thread.join(3.0)
    assert not a._thread.is_alive() and calls["n"] == 1 and a.gave_up and "404" in a.last_error
    a.close()


def test_replayed_frames_are_flagged_and_a_member_stamp_is_carried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:  # first connect: no replay
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_sse({"topic": "turn.usage", "data": {}, "seq": 5, "ts": 1700000000.5}))
        # the reconnect with ?since= replays the ring at once
        assert request.url.params.get("since") == "5"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_sse({"topic": "turn.usage", "data": {}, "seq": 6}, {"topic": "turn.usage", "data": {}, "seq": 7, "ts": "not-a-number"}))

    m = events.MemberEvents("http://127.0.0.1:7870", "tok", "x", transport=httpx.MockTransport(handler))
    out = []
    for ev in m.events():
        out.append(ev)
        if len(out) == 3:
            m.stop()
    assert (out[0].replayed, out[0].ts) == (False, 1700000000.5)
    assert out[1].replayed and out[1].ts is None and out[2].replayed and out[2].ts is None
    m.close()
