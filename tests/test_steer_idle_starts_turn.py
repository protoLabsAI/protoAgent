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

    async def _fake_start(session_id, text, msg_id, *, images=None, incognito=False, settings=None):
        calls.append(
            {
                "session_id": session_id,
                "text": text,
                "msg_id": msg_id,
                "images": images,
                "incognito": incognito,
                "settings": settings,
            }
        )
        return f"task-{len(calls)}"

    monkeypatch.setattr(chat_mod, "_start_steer_turn", _fake_start)
    return calls


def test_idle_steer_starts_exactly_one_turn(client, starts):
    # The bd-0ad5v race, server side: the console checked, believed a turn was live, and
    # POSTed — but the turn had ended. The POST itself must handle the idle session.
    body = client.post("/api/chat/sessions/s1/steer", json={"id": "m1", "text": "hello?"}).json()
    assert body == {"ok": True, "id": "m1", "started_turn": "task-1", "pending": 0}
    assert starts == [
        {"session_id": "s1", "text": "hello?", "msg_id": "m1", "images": None, "incognito": False, "settings": None}
    ]
    # Nothing pending: the message became a turn, which the queue read also names
    # (`started`), so a console that lost the POST reply still finds its way there.
    assert client.get("/api/chat/sessions/s1/steer").json() == {
        "pending": [],
        "drained": [],
        "started": {"m1": "task-1"},
    }


def test_idle_steer_carries_the_tabs_turn_settings(client, starts):
    # The round-2 bd-0ad5v review: every console send stamps model/reasoning_effort/
    # bypass_permissions, so the turn a steer starts must run under them too — not under
    # the configured defaults. Unknown body keys never ride (the settings reach a
    # loopback A2A send).
    body = client.post(
        "/api/chat/sessions/s1/steer",
        json={"id": "m1", "text": "hi", "model": "gw-sonnet", "reasoning_effort": "high",
              "bypass_permissions": True, "sneaky": "no"},
    ).json()
    assert body["started_turn"] == "task-1"
    assert starts[0]["settings"] == {"model": "gw-sonnet", "reasoning_effort": "high", "bypass_permissions": True}


def test_queued_steer_remembers_settings_for_the_turn_end_restart(client, starts):
    # Queued into a live turn, the settings ride the ITEM: the restart at turn-end is the
    # send that uses them (the fold itself inherits the running turn's).
    turn_activity.begin("s1")
    try:
        client.post(
            "/api/chat/sessions/s1/steer",
            json={"id": "m1", "text": "go", "model": "gw-sonnet", "reasoning_effort": "low"},
        )
    finally:
        turn_activity.end("s1")
    assert steering.pending_items("s1") == [
        {"id": "m1", "text": "go", "settings": {"model": "gw-sonnet", "reasoning_effort": "low"}}
    ]


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

    assert starts == [
        {
            "session_id": "s9",
            "text": "first\n\nsecond",
            "msg_id": "m1",
            "images": None,
            "incognito": False,
            "settings": None,
        }
    ]
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

    async def _gated_start(session_id, text, msg_id, *, images=None, incognito=False, settings=None):
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

    async def _gated_start(session_id, text, msg_id, *, images=None, incognito=False, settings=None):
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
async def test_turn_end_restart_carries_the_items_settings(idle_server, starts):
    # The restarted turn is ONE send, so the items' per-message settings merge in queue
    # order (newest write per key wins — the sender's latest tab state).
    steering.enqueue("s9", "first", msg_id="m1", settings={"model": "gw-a", "bypass_permissions": True})
    steering.enqueue("s9", "second", msg_id="m2", settings={"model": "gw-b", "reasoning_effort": "high"})
    await chat_mod.restart_undrained_steers("s9", "completed")
    assert starts[0]["settings"] == {"model": "gw-b", "reasoning_effort": "high", "bypass_permissions": True}


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
    steering.enqueue("s9", "precious", msg_id="m1", settings={"model": "gw-a"})
    task = chat_mod.restart_undrained_steers("s9", "failed")
    await task
    assert steering.pending_items("s9") == [{"id": "m1", "text": "precious", "settings": {"model": "gw-a"}}]
    assert steering.started_map("s9") == {}  # pending again, so no stale "starting" mark
    # The pop logged m1 as drained (the restart would have read it like a fold); requeued,
    # that row would call it pending AND read at once — a console settles the bubble while
    # the message sits queued (the round-2 bd-0ad5v finding). Un-said on requeue.
    assert steering.drained("s9") == []


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
            "model": "gw-sonnet",
            "reasoning_effort": "high",
            "bypass_permissions": True,
        },
    ).json()
    assert body == {"ok": True, "id": "m1", "started_turn": "task-77", "pending": 0}
    assert sent["url"] == "http://127.0.0.1:7999/a2a"
    assert sent["body"]["params"]["configuration"] == {"returnImmediately": True}  # id now, turn detached
    msg = sent["body"]["params"]["message"]
    assert msg["messageId"] == "m1" and msg["contextId"] == "s1" and msg["role"] == "ROLE_USER"
    assert msg["parts"] == [{"text": "hi"}, {"url": "data:image/png;base64,AAA=", "mediaType": "image/png"}]
    # The tab's per-message settings land on the metadata exactly where a console send
    # puts them, beside incognito — the fresh turn runs under them, not the defaults.
    assert msg["metadata"] == {
        "incognito": True,
        "model": "gw-sonnet",
        "reasoning_effort": "high",
        "bypass_permissions": True,
    }


# ── private idle intake (ADR 0131 P1b.2; dormant) ─────────────────────────────────
# A private steer is recorded into the P1b.1 partition BEFORE any turn starts; the loopback
# carries only an opaque reference bound to a single-use delegation, never the private text.


async def _partition(tmp_path):
    from a2a_impl import stores

    engine = stores.make_sqlite_engine(str(tmp_path / "private.db"))
    store = stores.ReasoningCoalescingTaskStore(engine, owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    return engine, store


def _operator_ctx():
    from a2a.server.context import ServerCallContext

    return ServerCallContext(state={"principal_kind": "shared-operator-bearer"})


@pytest.mark.asyncio
async def test_private_idle_steer_records_before_start_and_sends_only_a_reference(tmp_path, monkeypatch, delegation_env):
    from a2a.server.context import ServerCallContext

    from a2a_impl import private_intake

    steering._reset()
    engine, store = await _partition(tmp_path)
    monkeypatch.setattr(STATE, "graph", _Graph(_idle_snapshot()), raising=False)
    monkeypatch.setattr(STATE, "a2a_task_engine", engine, raising=False)
    op = _operator_ctx()
    captured: dict = {}

    async def _fake_start(session_id, text, msg_id, *, images=None, incognito=False, settings=None,
                          private_ref=None, delegation=None):
        captured.update(text=text, ref=private_ref, delegation=delegation, images=images)
        return "task-1"

    monkeypatch.setattr(chat_mod, "_start_steer_turn", _fake_start)
    body = await chat_mod.steer_or_start_turn(
        "chat-p", "SECRET-STEER", "m1", private=True, private_store=store, private_context=op
    )
    assert body["started_turn"] == "task-1" and body["private"] is True
    ref = body["private_ref"]
    # The loopback carries ONLY the opaque reference + a single-use delegation — never the text,
    # never images (intake assertion).
    assert captured["text"] == "" and captured["ref"] == ref and captured["images"] is None
    assert captured["delegation"] and "SECRET-STEER" not in str(captured)
    # The steer + its dedupe key were recorded into the partition BEFORE the start; the console
    # recovers the input (and later the reply) from there.
    rec = await store.get_record(private_intake.ref_task_id(ref), op)
    assert rec.private_payload["input"] == "SECRET-STEER" and rec.private_payload["dedupe"] == "m1"
    assert await private_intake.record_private_reply(store, ref, "done, privately", op)
    assert (await store.get_record(private_intake.ref_task_id(ref), op)).private_payload["reply"] == "done, privately"
    # A peer sees no task for the private-only shell, and the resolver round-trips with the
    # delegation it was bound to but fails closed for an unbound one.
    peer = ServerCallContext(state={"principal_kind": "federation", "trust_tier": "federation"})
    assert await store.get(private_intake.ref_task_id(ref), peer) is None
    deleg = delegation_env.redeem_delegation(
        captured["delegation"], audience=chat_mod._LOOPBACK_AUDIENCE, context="chat-p",
        operation=ref, idempotent=True, allowed_issuers=frozenset({delegation_env.ISSUER_LOOPBACK}),
    )
    assert await private_intake.resolve_private_input(store, ref, delegation=deleg, context=op) == "SECRET-STEER"
    assert await private_intake.resolve_private_input(store, ref, delegation=None, context=op) is None
    await engine.dispose()
    steering._reset()


@pytest.mark.asyncio
async def test_private_idle_steer_fails_closed_without_a_partition(monkeypatch):
    steering._reset()
    monkeypatch.setattr(STATE, "graph", _Graph(_idle_snapshot()), raising=False)
    monkeypatch.setattr(STATE, "a2a_task_engine", object(), raising=False)
    calls: list = []

    async def _fake_start(*a, **k):
        calls.append(1)
        return "task-x"

    monkeypatch.setattr(chat_mod, "_start_steer_turn", _fake_start)
    body = await chat_mod.steer_or_start_turn("chat-p", "SECRET", "m1", private=True)
    assert body["ok"] is False and "started_turn" not in body and calls == []
    steering._reset()


@pytest.mark.asyncio
async def test_private_idle_steer_fails_closed_when_the_record_cannot_be_written(tmp_path, monkeypatch):
    steering._reset()
    engine, store = await _partition(tmp_path)
    monkeypatch.setattr(STATE, "graph", _Graph(_idle_snapshot()), raising=False)
    monkeypatch.setattr(STATE, "a2a_task_engine", engine, raising=False)
    calls: list = []

    async def _fake_start(*a, **k):
        calls.append(1)
        return "task-x"

    async def _boom(*a, **k):
        raise RuntimeError("partition down")

    monkeypatch.setattr(chat_mod, "_start_steer_turn", _fake_start)
    monkeypatch.setattr(store, "save_private", _boom)
    body = await chat_mod.steer_or_start_turn(
        "chat-p", "SECRET", "m1", private=True, private_store=store, private_context=_operator_ctx()
    )
    # Fail-closed: no turn started, no ordinary SendMessage fallback.
    assert body["ok"] is False and "started_turn" not in body and calls == []
    await engine.dispose()
    steering._reset()


@pytest.mark.asyncio
async def test_idle_check_resolves_the_original_turns_thread(monkeypatch):
    # A later idle start / parked resume must resolve the ORIGINAL turn's checkpointer thread,
    # not one rebuilt from the idle call's empty metadata under a custom resolver (#571).
    from graph import thread_ids

    thread_ids.forget_resolver_metadata("chat-x")
    monkeypatch.setattr(STATE, "thread_id_resolver",
                        lambda md, sid: f"proj:{md.get('project', 'none')}:{sid}", raising=False)
    seen: dict = {}

    class _G:
        async def aget_state(self, config):
            seen["tid"] = config["configurable"]["thread_id"]
            return _idle_snapshot()

    monkeypatch.setattr(STATE, "graph", _G(), raising=False)
    monkeypatch.setattr(STATE, "a2a_task_engine", object(), raising=False)
    assert await chat_mod._session_turn_idle("chat-x") is True
    assert seen["tid"] == "proj:none:chat-x"
    thread_ids.remember_resolver_metadata("chat-x", {"project": "atlas"})
    assert await chat_mod._session_turn_idle("chat-x") is True
    assert seen["tid"] == "proj:atlas:chat-x"
    thread_ids.forget_resolver_metadata("chat-x")


@pytest.mark.asyncio
async def test_trusted_runner_resolves_the_reference_and_fails_closed(tmp_path, monkeypatch, delegation_env):
    # The trusted runner redeems the delegation from the request metadata and resolves the
    # opaque reference to the private text — fail-closed, never running the reference literally.
    from graph.fleet.delegation import DELEGATION_HEADER

    from a2a_impl import private_intake

    steering._reset()
    engine, store = await _partition(tmp_path)
    monkeypatch.setattr(STATE, "a2a_task_engine", engine, raising=False)
    op = _operator_ctx()
    ref = await private_intake.record_private_steer(
        store, session_id="chat-r", text="RUNNER-SECRET", msg_id="m1", context=op
    )
    token = chat_mod._mint_loopback_delegation(context="chat-r", operation=ref)
    assert await chat_mod._resolve_private_turn_input(ref, "chat-r", {DELEGATION_HEADER: token}) == (True, "RUNNER-SECRET")
    # Fail-closed: no delegation header, or one bound to a DIFFERENT reference.
    assert await chat_mod._resolve_private_turn_input(ref, "chat-r", {}) == (False, "")
    other = chat_mod._mint_loopback_delegation(context="chat-r", operation="private-input:other")
    assert await chat_mod._resolve_private_turn_input(ref, "chat-r", {DELEGATION_HEADER: other}) == (False, "")
    await engine.dispose()
    steering._reset()
