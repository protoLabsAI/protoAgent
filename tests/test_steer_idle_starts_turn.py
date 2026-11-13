"""A steer with no live turn STARTS a turn instead of queueing forever (bd-0ad5v).

A console with a stale 'streaming' bubble steered an idle session; the message sat
``pending`` with nothing left to drain it and the operator couldn't chat. The steer POST
now starts an ordinary operator turn when the session is idle (``started_turn`` in the
reply), and a steer left undrained when its turn ends terminally is restarted as exactly
one fresh turn, with every popped id reported through ``GET …/steer``'s ``started`` map
("" while the start is in flight) so the console attaches to that turn instead of settling
the message as consumed by the one that just ended. The REAL steering queue, busy signal
and per-thread turn lock are used throughout — only the loopback A2A transport is faked
(the turn-start helper, or httpx itself for the wire-shape test).
"""

import importlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph import steering
from runtime import turn_activity
from runtime.state import STATE

# `server.chat` the MODULE — the package re-exports the `chat` function under the same name.
chat_mod = importlib.import_module("server.chat")


def _idle_snapshot():
    return SimpleNamespace(tasks=(), interrupts=())


def _parked_snapshot():
    pend = SimpleNamespace(id="i1", value={"question": "pick one"})
    return SimpleNamespace(tasks=(SimpleNamespace(result=None, interrupts=(pend,)),), interrupts=())


class _Graph:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def aget_state(self, _config):
        return self.snapshot


@pytest.fixture
def client(monkeypatch, tmp_path):
    from operator_api.chat_routes import register_chat_routes

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "instance"))
    steering._reset()
    # A runnable server: a compiled graph with no pending interrupt + an A2A stack.
    monkeypatch.setattr(STATE, "graph", _Graph(_idle_snapshot()), raising=False)
    monkeypatch.setattr(STATE, "a2a_task_engine", object(), raising=False)
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(model_vision=False), raising=False)
    app = FastAPI()
    register_chat_routes(app, ui="none")
    with TestClient(app) as c:
        yield c
    steering._reset()


@pytest.fixture
def starts(monkeypatch):
    """Record every loopback turn start — the ONE faked seam."""
    calls: list[dict] = []

    async def _fake_start(session_id, text, msg_id, *, images=None, incognito=False):
        calls.append({"session_id": session_id, "text": text, "msg_id": msg_id, "images": images, "incognito": incognito})
        return f"task-{len(calls)}"

    monkeypatch.setattr(chat_mod, "_start_steer_turn", _fake_start)
    return calls


def test_idle_steer_starts_exactly_one_turn(client, starts):
    # The bd-0ad5v race, server side: the console checked, believed a turn was live, and
    # POSTed — but the turn had ended. The POST itself must handle the idle session.
    body = client.post("/api/chat/sessions/s1/steer", json={"id": "m1", "text": "hello?"}).json()
    assert body == {"ok": True, "id": "m1", "started_turn": "task-1", "pending": 0}
    assert starts == [{"session_id": "s1", "text": "hello?", "msg_id": "m1", "images": None, "incognito": False}]
    # Nothing pending: the message became a turn, which the queue read also names
    # (`started`), so a console that lost the POST reply still finds its way there.
    assert client.get("/api/chat/sessions/s1/steer").json() == {
        "pending": [],
        "drained": [],
        "started": {"m1": "task-1"},
    }


def test_duplicate_posts_with_same_message_id_start_one_turn(client, starts):
    first = client.post("/api/chat/sessions/s1/steer", json={"id": "m1", "text": "hello?"}).json()
    again = client.post("/api/chat/sessions/s1/steer", json={"id": "m1", "text": "hello?"}).json()
    assert first["started_turn"] == again["started_turn"] == "task-1"
    assert len(starts) == 1  # the duplicate neither started a second turn…
    assert steering.pending("s1") == 0  # …nor was it enqueued into the one it started


def test_steer_during_live_turn_still_queues(client, starts):
    turn_activity.begin("s1")  # the real busy signal every turn driver brackets
    try:
        body = client.post("/api/chat/sessions/s1/steer", json={"id": "m1", "text": "go left"}).json()
    finally:
        turn_activity.end("s1")
    assert body == {"ok": True, "id": "m1", "pending": 1} and "started_turn" not in body
    assert starts == []
    assert steering.pending_items("s1") == [{"id": "m1", "text": "go left"}]


def test_steer_on_a_hitl_parked_thread_still_queues(client, starts):
    # No live driver, but the checkpoint holds an unanswered interrupt (checked under the
    # real thread lock): the message must wait for the form answer, exactly as before.
    STATE.graph.snapshot = _parked_snapshot()
    body = client.post("/api/chat/sessions/s1/steer", json={"id": "m1", "text": "held"}).json()
    assert body == {"ok": True, "id": "m1", "pending": 1}
    assert starts == [] and steering.pending("s1") == 1


def test_failed_start_falls_back_to_the_queue(client, monkeypatch):
    async def _refused(*_a, **_kw):
        return ""

    monkeypatch.setattr(chat_mod, "_start_steer_turn", _refused)
    body = client.post("/api/chat/sessions/s1/steer", json={"id": "m1", "text": "keep me"}).json()
    assert body == {"ok": True, "id": "m1", "pending": 1}  # never dropped
    assert steering.pending_items("s1") == [{"id": "m1", "text": "keep me"}]


@pytest.fixture
def idle_server(monkeypatch):
    steering._reset()
    monkeypatch.setattr(STATE, "graph", _Graph(_idle_snapshot()), raising=False)
    monkeypatch.setattr(STATE, "a2a_task_engine", object(), raising=False)
    yield
    steering._reset()


@pytest.mark.asyncio
async def test_undrained_steers_at_turn_end_become_exactly_one_turn(idle_server, starts):
    steering.enqueue("s9", "first", msg_id="m1")
    steering.enqueue("s9", "second", msg_id="m2")

    task = chat_mod.restart_undrained_steers("s9", "completed")
    assert task is not None
    await task

    assert starts == [{"session_id": "s9", "text": "first\n\nsecond", "msg_id": "m1", "images": None, "incognito": False}]
    assert steering.pending("s9") == 0
    assert steering.drained("s9") == ["m1", "m2"]  # the restart turn reads them like a fold
    # EVERY restarted id names the turn — each is a console bubble that must attach to it.
    assert steering.started_map("s9") == {"m1": "task-1", "m2": "task-1"}


@pytest.mark.asyncio
async def test_restart_marks_items_starting_before_the_loopback_names_a_task(idle_server, monkeypatch):
    # The blocking bd-0ad5v review case: the console's turn-end reconcile reads the queue
    # BETWEEN the pop and the loopback send resolving. It must see each popped id as
    # "starting" (``""`` in the started map), never simply gone — gone reads as consumed
    # by the turn that just ended, and the bubble settles under a reply that never saw it.
    import asyncio

    gate = asyncio.Event()

    async def _gated_start(session_id, text, msg_id, *, images=None, incognito=False):
        await gate.wait()
        return "task-9"

    monkeypatch.setattr(chat_mod, "_start_steer_turn", _gated_start)
    steering.enqueue("s9", "first", msg_id="m1")
    steering.enqueue("s9", "second", msg_id="m2")
    task = chat_mod.restart_undrained_steers("s9", "completed")
    # The pop and the starting mark land in the same synchronous frame.
    assert steering.pending("s9") == 0
    assert steering.started_map("s9") == {"m1": "", "m2": ""}
    gate.set()
    await task
    assert steering.started_map("s9") == {"m1": "task-9", "m2": "task-9"}


@pytest.mark.asyncio
async def test_duplicate_post_during_an_in_flight_start_joins_that_turn(idle_server, monkeypatch):
    # The round-1 race: a duplicate POST lands while the first one's loopback send is in
    # flight. The new turn already looks busy by then, so the old path queued the text
    # into it — the agent reading its own message twice. The duplicate must wait the
    # start out and answer with the same task instead.
    import asyncio

    gate = asyncio.Event()
    starts: list[str] = []

    async def _gated_start(session_id, text, msg_id, *, images=None, incognito=False):
        starts.append(msg_id)
        await gate.wait()
        return "task-1"

    monkeypatch.setattr(chat_mod, "_start_steer_turn", _gated_start)
    first = asyncio.create_task(chat_mod.steer_or_start_turn("s1", "hello?", "m1"))
    await asyncio.sleep(0.01)  # the first POST is now awaiting its loopback send
    dup = asyncio.create_task(chat_mod.steer_or_start_turn("s1", "hello?", "m1"))
    await asyncio.sleep(0.01)
    gate.set()
    r1, r2 = await asyncio.gather(first, dup)
    assert r1["started_turn"] == r2["started_turn"] == "task-1"
    assert starts == ["m1"]  # ONE loopback send…
    assert steering.pending("s1") == 0  # …and the duplicate never queued into its turn


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["input_required", "canceled", "unknown", ""])
async def test_turn_end_restart_only_fires_on_terminal_states(idle_server, starts, state):
    # A park's held messages fold in after the operator answers; a Stop's queued messages
    # are the console's to take back — neither may spawn a surprise turn.
    steering.enqueue("s9", "waiting", msg_id="m1")
    assert chat_mod.restart_undrained_steers("s9", state) is None
    assert starts == [] and steering.pending("s9") == 1


@pytest.mark.asyncio
async def test_turn_end_restart_defers_to_a_running_turn(idle_server, starts):
    steering.enqueue("s9", "waiting", msg_id="m1")
    turn_activity.begin("s9")  # the next queued turn will fold it in itself
    try:
        assert chat_mod.restart_undrained_steers("s9", "completed") is None
    finally:
        turn_activity.end("s9")
    assert starts == [] and steering.pending("s9") == 1


@pytest.mark.asyncio
async def test_failed_restart_requeues_the_items(idle_server, monkeypatch):
    async def _refused(*_a, **_kw):
        return ""

    monkeypatch.setattr(chat_mod, "_start_steer_turn", _refused)
    steering.enqueue("s9", "precious", msg_id="m1")
    task = chat_mod.restart_undrained_steers("s9", "failed")
    await task
    assert steering.pending_items("s9") == [{"id": "m1", "text": "precious"}]
    assert steering.started_map("s9") == {}  # pending again, so no stale "starting" mark


def test_idle_steer_goes_out_as_an_ordinary_a2a_message(monkeypatch, client):
    # Through the route seam (the console's POST), down to the loopback wire: the started
    # turn is a normal operator send — same messageId (dedupe), same part shapes, detached
    # via returnImmediately so the task id answers while the turn runs.
    import httpx

    sent: dict = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"result": {"task": {"id": "task-77"}}}

    class _Client:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            sent.update(url=url, headers=headers, body=json)
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(STATE, "active_port", 7999, raising=False)
    body = client.post(
        "/api/chat/sessions/s1/steer",
        json={
            "id": "m1",
            "text": "hi",
            "images": [["image/png", "data:image/png;base64,AAA="]],
            "incognito": True,
        },
    ).json()
    assert body == {"ok": True, "id": "m1", "started_turn": "task-77", "pending": 0}
    assert sent["url"] == "http://127.0.0.1:7999/a2a"
    assert sent["body"]["params"]["configuration"] == {"returnImmediately": True}  # id now, turn detached
    msg = sent["body"]["params"]["message"]
    assert msg["messageId"] == "m1" and msg["contextId"] == "s1" and msg["role"] == "ROLE_USER"
    assert msg["parts"] == [{"text": "hi"}, {"url": "data:image/png;base64,AAA=", "mediaType": "image/png"}]
    assert msg["metadata"] == {"incognito": True}
