"""Fleet reverse proxy — detached long-turn /api/chat (#4277 slice 2).

A member agent turn can run far past the proxy's 600s read cap. The hub used to wait the
whole turn on one socket, 504 at 600s, and CLOSE the upstream — and that disconnect cancels
the member's turn (#4205). So a long member turn proxied through the hub could never finish.

The proxy now rewrites a sync ``POST /agents/<slug>/api/chat`` into submit-then-poll
(bd-bas5 / slice 1): it adds ``detach: true``, the member answers 202 ``{turn_id,
session_id}``, and the hub polls ``GET …/turns/{turn_id}`` until the turn is terminal or a
3600s ceiling — answering its caller in the same shape a sync call returns. These tests cover
the injection, the poll-to-completion, the >600s case, the ceiling, a hub-client disconnect
mid-poll (no cancel sent), old-core 200 passthrough, an explicit detach forwarded verbatim,
and the failed/canceled error-status mapping.
"""

from __future__ import annotations

import json

import pytest

from graph.fleet import proxy


@pytest.fixture(autouse=True)
def _clear_cache():
    proxy._slug_cache.clear()
    proxy._remote_slugs.clear()
    yield
    proxy._slug_cache.clear()
    proxy._remote_slugs.clear()


class FakeRequest:
    def __init__(self, method="GET", headers=None, query=None, body=b""):
        self.method = method
        self.headers = headers or {}
        self.query_params = query or {}
        self._body = body
        self._disconnected = False

    async def body(self):
        return self._body

    async def is_disconnected(self):
        return self._disconnected


class FakeResponse:
    """A fully-read (non-streamed) upstream answer — what a poll / submit gets back."""

    def __init__(self, status_code, content=b"", headers=None):
        self.status_code = status_code
        self.content = content if isinstance(content, (bytes, bytearray)) else json.dumps(content).encode()
        self.headers = headers or {"content-type": "application/json"}

    async def aclose(self):
        pass


class FakeClient:
    """Dispatches by method: the POST is the submit, every GET is a poll (the last poll
    response is reused once the script runs out, so an always-"working" member is easy to
    model). Records every built request for assertions on the forwarded body / poll URL."""

    def __init__(self, submit, polls):
        self._submit = submit
        self._polls = list(polls)
        self._poll_i = 0
        self.requests: list[dict] = []

    def build_request(self, method, url, headers=None, content=None, params=None, timeout=None):
        req = {
            "method": method,
            "url": url,
            "headers": headers,
            "content": content,
            "params": params,
            "timeout": timeout,
        }
        self.requests.append(req)
        return req

    async def send(self, req, stream=False):
        if req["method"] == "POST":
            return self._submit
        resp = self._polls[min(self._poll_i, len(self._polls) - 1)]
        self._poll_i += 1
        return resp

    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]

    def gets(self):
        return [r for r in self.requests if r["method"] == "GET"]


def _local(monkeypatch):
    """Resolve the slug to a plain LOCAL peer (empty auth). A FakeRequest with no ``state`` then
    forwards with ``extra={}`` and no drop_params — the auth path isn't what these tests cover."""
    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: ("http://127.0.0.1:7001", {}))


# --- _detach_chat_body -----------------------------------------------------


def test_detach_body_injects_true_and_preserves_the_rest():
    out = proxy._detach_chat_body(b'{"message":"hi","model":"m"}')
    data = json.loads(out)
    assert data == {"message": "hi", "model": "m", "detach": True}


@pytest.mark.parametrize(
    "raw",
    [
        b'{"message":"hi","detach":true}',   # caller already asked to detach
        b'{"message":"hi","detach":false}',  # explicit opt-out
        b"not json",
        b"",
        b"[1,2,3]",                           # a JSON array, not an object
    ],
)
def test_detach_body_returns_none_to_forward_verbatim(raw):
    assert proxy._detach_chat_body(raw) is None


# --- r1: submit → poll → 200 with the member's response --------------------


async def test_detach_sync_post_polls_to_completion(monkeypatch):
    """r1: a 202 then several "working" polls then "completed" returns 200 with the member's
    response, and the forwarded submit body carried detach:true."""
    _local(monkeypatch)
    monkeypatch.setattr(proxy, "_DETACHED_POLL_S", 0)
    submit = FakeResponse(202, {"turn_id": "t1", "session_id": "s1", "state": "working"})
    polls = [
        FakeResponse(200, {"turn_id": "t1", "state": "working"}),
        FakeResponse(200, {"turn_id": "t1", "state": "working"}),
        FakeResponse(
            200,
            {
                "turn_id": "t1",
                "state": "completed",
                "response": "hi there",
                "messages": [{"role": "assistant", "content": "hi there"}],
            },
        ),
    ]
    client = FakeClient(submit, polls)
    monkeypatch.setattr(proxy, "_get_client", lambda: client)

    req = FakeRequest(method="POST", headers={"authorization": "Bearer x"}, body=json.dumps({"message": "hi"}).encode())
    resp = await proxy.forward_to("alice", req, "api/chat")

    assert resp.status_code == 200
    assert json.loads(resp.body) == {
        "response": "hi there",
        "messages": [{"role": "assistant", "content": "hi there"}],
        "session_id": "s1",
    }
    # the forwarded submit body carried detach:true, message preserved
    sent = json.loads(client.posts()[0]["content"])
    assert sent["detach"] is True and sent["message"] == "hi"
    # it polled the member's own turns endpoint for this turn, several times, until terminal
    gets = client.gets()
    assert len(gets) == 3
    assert all(g["url"] == "http://127.0.0.1:7001/api/chat/sessions/s1/turns/t1" for g in gets)
    assert all(g["timeout"] is proxy._READ_TIMEOUT for g in gets)  # each poll on the bounded lane
    # the submit rode the turn lane so an old core isn't cut off at 20s
    assert client.posts()[0]["timeout"] is proxy._TURN_TIMEOUT


# --- r2: a turn longer than the old 600s still returns 200 -----------------


async def test_turn_longer_than_600s_still_returns_200(monkeypatch):
    """r2: with a fake clock jumping past the old 600s cap (but under the 3600s ceiling) the
    turn still completes with 200 — the ceiling is 3600, not 600."""
    _local(monkeypatch)
    monkeypatch.setattr(proxy, "_DETACHED_POLL_S", 0)
    # deadline is set from the first read (0.0 + 3600); later reads report 700s, 1400s elapsed.
    ticks = iter([0.0, 700.0, 1400.0, 2100.0])
    monkeypatch.setattr(proxy, "_now", lambda: next(ticks))
    submit = FakeResponse(202, {"turn_id": "t1", "session_id": "s1"})
    polls = [
        FakeResponse(200, {"state": "working"}),
        FakeResponse(200, {"state": "working"}),
        FakeResponse(200, {"state": "completed", "response": "done", "messages": []}),
    ]
    client = FakeClient(submit, polls)
    monkeypatch.setattr(proxy, "_get_client", lambda: client)

    req = FakeRequest(method="POST", body=json.dumps({"message": "hi"}).encode())
    resp = await proxy.forward_to("alice", req, "api/chat")

    assert resp.status_code == 200  # NOT 504 — a 600s cap would have failed this turn
    assert json.loads(resp.body)["response"] == "done"


# --- r5: the ceiling returns 504 naming the turn, no cancel ----------------


async def test_ceiling_returns_504_naming_turn_and_poll_url(monkeypatch):
    """r5: a turn still working at the ceiling gets a 504 whose detail names the turn id and a
    hub-side poll URL, and the member turn is NOT cancelled."""
    _local(monkeypatch)
    monkeypatch.setattr(proxy, "_DETACHED_POLL_S", 0)
    monkeypatch.setattr(proxy, "_DETACHED_TURN_MAX_S", 5)
    ticks = iter([0.0, 2.0, 6.0])  # deadline=5; the third read trips it
    monkeypatch.setattr(proxy, "_now", lambda: next(ticks))
    submit = FakeResponse(202, {"turn_id": "t9", "session_id": "s9"})
    client = FakeClient(submit, [FakeResponse(200, {"state": "working"})])  # never terminal
    monkeypatch.setattr(proxy, "_get_client", lambda: client)

    req = FakeRequest(method="POST", body=json.dumps({"message": "hi"}).encode())
    resp = await proxy.forward_to("ava", req, "api/chat")

    assert resp.status_code == 504
    body = json.loads(resp.body)
    assert "t9" in body["detail"]
    assert body["turn_id"] == "t9"
    assert body["poll_url"] == "/agents/ava/api/chat/sessions/s9/turns/t9"
    assert body["state"] == "working"
    # the member turn was never cancelled — the hub only ever POSTed (submit) and GET-polled
    assert not any(r["method"] not in ("POST", "GET") for r in client.requests)


# --- r3: a hub-client disconnect mid-poll stops polling, sends no cancel ----


async def test_client_disconnect_stops_polling_and_sends_no_cancel(monkeypatch):
    """r3: when the hub's own client goes away mid-poll, polling stops and NO cancel request is
    sent to the member — the detached turn keeps running."""
    _local(monkeypatch)
    monkeypatch.setattr(proxy, "_DETACHED_POLL_S", 0)
    submit = FakeResponse(202, {"turn_id": "t2", "session_id": "s2"})
    client = FakeClient(submit, [FakeResponse(200, {"state": "working"})])  # never terminal
    monkeypatch.setattr(proxy, "_get_client", lambda: client)

    # connected for the first poll, then the client disconnects before the next.
    disc = iter([False, True, True, True])

    async def gone():
        return next(disc)

    req = FakeRequest(method="POST", body=json.dumps({"message": "hi"}).encode())
    req.is_disconnected = gone
    resp = await proxy.forward_to("bob", req, "api/chat")

    assert resp.status_code == 499  # the body is dropped — the socket is gone — but we stop
    assert len(client.gets()) == 1  # polled once while connected, then stopped
    # NEVER a cancel/stop/delete to the member: only the submit POST + the one poll GET
    assert {r["method"] for r in client.requests} == {"POST", "GET"}


# --- r4: old-core 200 passthrough / explicit detach verbatim ---------------


async def test_old_core_200_is_passed_through_unchanged(monkeypatch):
    """r4: a member that answers 200 (an older core that ignored detach and ran the turn
    inline) has its response passed through unchanged, with no polling."""
    _local(monkeypatch)
    sync_body = {"response": "answer", "messages": [{"role": "assistant", "content": "answer"}], "session_id": "s3"}
    submit = FakeResponse(200, sync_body, headers={"content-type": "application/json"})
    client = FakeClient(submit, [])
    monkeypatch.setattr(proxy, "_get_client", lambda: client)

    req = FakeRequest(method="POST", body=json.dumps({"message": "hi"}).encode())
    resp = await proxy.forward_to("alice", req, "api/chat")

    assert resp.status_code == 200
    assert json.loads(resp.body) == sync_body
    # the hub still added detach:true; the OLD core simply ignored it
    assert json.loads(client.posts()[0]["content"])["detach"] is True
    assert client.gets() == []  # answered inline → no polling


@pytest.mark.parametrize("detach_value", [True, False])
async def test_explicit_detach_is_forwarded_verbatim_with_no_polling(monkeypatch, detach_value):
    """r4: a caller that set ``detach`` itself (true or false) is forwarded verbatim down the
    normal streaming path — the hub does not take over and does not poll."""
    _local(monkeypatch)
    calls = {"base": 0, "detach": 0}

    async def fake_base(base, request, path, extra=None, **kw):
        calls["base"] += 1
        return "FORWARDED"

    async def fake_detach(*a, **k):
        calls["detach"] += 1
        return "POLLED"

    monkeypatch.setattr(proxy, "_forward_to_base", fake_base)
    monkeypatch.setattr(proxy, "_forward_chat_detach", fake_detach)

    req = FakeRequest(method="POST", body=json.dumps({"message": "hi", "detach": detach_value}).encode())
    out = await proxy.forward_to("alice", req, "api/chat")

    assert out == "FORWARDED"
    assert calls["base"] == 1 and calls["detach"] == 0  # normal path, never the poller


# --- r5: failed / canceled map to the sync error status --------------------


async def test_failed_turn_maps_to_sync_error_status(monkeypatch):
    """r5: a failed turn with an upstream 429 maps to 429 (the shared /v1 + /api/chat policy),
    and the Retry-After and message ride in the same detail envelope a sync call raises."""
    _local(monkeypatch)
    monkeypatch.setattr(proxy, "_DETACHED_POLL_S", 0)
    submit = FakeResponse(202, {"turn_id": "t4", "session_id": "s4"})
    polls = [
        FakeResponse(
            200,
            {
                "state": "failed",
                "error": {"type": "rate_limit", "message": "slow down", "upstream_status": 429, "retry_after": 12},
            },
        )
    ]
    client = FakeClient(submit, polls)
    monkeypatch.setattr(proxy, "_get_client", lambda: client)

    req = FakeRequest(method="POST", body=json.dumps({"message": "hi"}).encode())
    resp = await proxy.forward_to("alice", req, "api/chat")

    assert resp.status_code == 429
    assert resp.headers.get("retry-after") == "12"
    detail = json.loads(resp.body)["detail"]
    assert detail["code"] == "rate_limit"
    assert detail["message"] == "slow down"
    assert detail["session_id"] == "s4"
    assert detail["upstream_status"] == 429


async def test_failed_turn_with_no_upstream_is_500(monkeypatch):
    _local(monkeypatch)
    monkeypatch.setattr(proxy, "_DETACHED_POLL_S", 0)
    submit = FakeResponse(202, {"turn_id": "t5", "session_id": "s5"})
    polls = [FakeResponse(200, {"state": "failed", "error": {"type": "server_error", "message": "boom", "upstream_status": None}})]
    client = FakeClient(submit, polls)
    monkeypatch.setattr(proxy, "_get_client", lambda: client)

    req = FakeRequest(method="POST", body=json.dumps({"message": "hi"}).encode())
    resp = await proxy.forward_to("alice", req, "api/chat")
    assert resp.status_code == 500
    assert json.loads(resp.body)["detail"]["message"] == "boom"


async def test_canceled_turn_without_error_bubble_maps_to_409(monkeypatch):
    """A detached turn stopped by the operator (its finished-callback cancel path) records a
    ``canceled`` state with no error bubble; the hub still maps it to 409, like a sync stop."""
    _local(monkeypatch)
    monkeypatch.setattr(proxy, "_DETACHED_POLL_S", 0)
    submit = FakeResponse(202, {"turn_id": "t6", "session_id": "s6"})
    client = FakeClient(submit, [FakeResponse(200, {"state": "canceled"})])
    monkeypatch.setattr(proxy, "_get_client", lambda: client)

    req = FakeRequest(method="POST", body=json.dumps({"message": "hi"}).encode())
    resp = await proxy.forward_to("alice", req, "api/chat")
    assert resp.status_code == 409
    assert json.loads(resp.body)["detail"]["code"] == "turn_cancelled"


# --- a non-200 poll (unknown/expired turn) is surfaced ---------------------


async def test_non_200_poll_is_passed_through(monkeypatch):
    """If the member's turns endpoint 404s (the id expired/unknown), surface it rather than
    spinning to the ceiling."""
    _local(monkeypatch)
    monkeypatch.setattr(proxy, "_DETACHED_POLL_S", 0)
    submit = FakeResponse(202, {"turn_id": "t7", "session_id": "s7"})
    client = FakeClient(submit, [FakeResponse(404, {"detail": {"code": "not_found"}})])
    monkeypatch.setattr(proxy, "_get_client", lambda: client)

    req = FakeRequest(method="POST", body=json.dumps({"message": "hi"}).encode())
    resp = await proxy.forward_to("alice", req, "api/chat")
    assert resp.status_code == 404


# --- a non-POST / non-api-chat request is untouched by the detach lane -------


async def test_get_api_chat_is_not_detached(monkeypatch):
    """Only a POST to api/chat is intercepted; a GET (or any other verb) flows the normal path."""
    _local(monkeypatch)
    seen = {}

    async def fake_base(base, request, path, extra=None, **kw):
        seen["path"] = path
        return "OK"

    async def fake_detach(*a, **k):
        raise AssertionError("detach must not be entered for a GET")

    monkeypatch.setattr(proxy, "_forward_to_base", fake_base)
    monkeypatch.setattr(proxy, "_forward_chat_detach", fake_detach)
    out = await proxy.forward_to("alice", FakeRequest(method="GET"), "api/chat")
    assert out == "OK" and seen["path"] == "api/chat"
