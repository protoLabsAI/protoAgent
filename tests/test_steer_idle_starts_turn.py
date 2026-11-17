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
    # Kept out of ``captured`` on purpose: the value is the private text, which the wire-field
    # assertion below proves is absent from everything the loopback carries.
    at_start: dict = {}

    async def _fake_start(session_id, text, msg_id, *, images=None, incognito=False, settings=None,
                          private_ref=None, delegation=None, hitl_resume=False):
        # Ordering proof: at the instant the start is invoked the partition record ALREADY
        # exists (the steer is recorded BEFORE any turn starts). A record written only after
        # this call returns would read None here and fail the assertion below.
        rec = await store.get_record(private_intake.ref_task_id(private_ref), op)
        at_start["input"] = rec.private_payload["input"] if rec is not None else None
        captured.update(text=text, ref=private_ref, delegation=delegation, images=images, hitl_resume=hitl_resume)
        return "task-1"

    monkeypatch.setattr(chat_mod, "_start_steer_turn", _fake_start)
    body = await chat_mod.steer_or_start_turn(
        "chat-p", "SECRET-STEER", "m1", private=True, private_store=store, private_context=op
    )
    assert body["started_turn"] == "task-1" and body["private"] is True
    ref = body["private_ref"]
    # The loopback carries ONLY the opaque reference + a single-use delegation — never the text,
    # never images (intake assertion). An IDLE session starts a FRESH turn, not a resume.
    assert captured["text"] == "" and captured["ref"] == ref and captured["images"] is None
    assert captured["hitl_resume"] is False
    assert captured["delegation"] and "SECRET-STEER" not in str(captured)
    # The record was already in the partition WHEN the start ran — recorded before the turn.
    assert at_start["input"] == "SECRET-STEER"
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


def test_runner_private_input_prefix_mirrors_the_partition_module():
    # chat.py's runner-gate prefix is a local mirror of the one wire constant; pin it to the
    # canonical so the two copies can never drift (r1 review).
    from a2a_impl import private_intake

    assert chat_mod._PRIVATE_INPUT_PREFIX == private_intake.PRIVATE_INPUT_PREFIX


@pytest.mark.asyncio
async def test_private_parked_steer_rides_the_guarded_resume(tmp_path, monkeypatch, delegation_env):
    # A private steer to a HITL-parked session is recorded, then DELIVERED through the guarded
    # resume path (not silently dropped, r1): a hitl_resume loopback carrying ONLY the opaque
    # reference bound to the single-use delegation — never the text.
    from a2a_impl import private_intake

    steering._reset()
    engine, store = await _partition(tmp_path)
    monkeypatch.setattr(STATE, "graph", _Graph(_parked_snapshot()), raising=False)
    monkeypatch.setattr(STATE, "a2a_task_engine", engine, raising=False)
    op = _operator_ctx()
    captured: dict = {}
    at_start: dict = {}  # the private text — kept out of ``captured`` (the wire-field capture)

    async def _fake_start(session_id, text, msg_id, *, images=None, incognito=False, settings=None,
                          private_ref=None, delegation=None, hitl_resume=False):
        # Ordering proof: the partition record already exists when the resume start is invoked.
        rec = await store.get_record(private_intake.ref_task_id(private_ref), op)
        at_start["input"] = rec.private_payload["input"] if rec is not None else None
        captured.update(text=text, ref=private_ref, delegation=delegation, hitl_resume=hitl_resume)
        return "task-resume"

    monkeypatch.setattr(chat_mod, "_start_steer_turn", _fake_start)
    body = await chat_mod.steer_or_start_turn(
        "chat-parked", "SECRET-PARKED", "m1", private=True, private_store=store, private_context=op
    )
    assert body["started_turn"] == "task-resume" and body["private"] is True
    # The non-idle path rides the guarded RESUME (hitl_resume=True), carrying only the reference.
    assert captured["hitl_resume"] is True
    assert captured["text"] == "" and captured["ref"] == body["private_ref"]
    assert captured["delegation"] and "SECRET-PARKED" not in str(captured)
    assert at_start["input"] == "SECRET-PARKED"  # recorded before the resume start ran
    # The steer + its dedupe key were recorded BEFORE the resume; the console recovers them.
    rec = await store.get_record(private_intake.ref_task_id(body["private_ref"]), op)
    assert rec.private_payload["input"] == "SECRET-PARKED" and rec.private_payload["dedupe"] == "m1"
    await engine.dispose()
    steering._reset()


@pytest.mark.asyncio
async def test_private_steer_is_idempotent_across_a_retry(tmp_path, monkeypatch, delegation_env):
    # The dedupe key persisted in the partition — not only the in-memory start guard — keeps a
    # retried msg_id from minting a second shell or starting a second turn (r2 review).
    from sqlalchemy import text as _sql

    from a2a_impl import private_intake

    steering._reset()
    engine, store = await _partition(tmp_path)
    monkeypatch.setattr(STATE, "graph", _Graph(_idle_snapshot()), raising=False)
    monkeypatch.setattr(STATE, "a2a_task_engine", engine, raising=False)
    op = _operator_ctx()
    starts: list = []

    async def _fake_start(session_id, text, msg_id, *, images=None, incognito=False, settings=None,
                          private_ref=None, delegation=None, hitl_resume=False):
        starts.append(private_ref)
        return f"task-{len(starts)}"

    monkeypatch.setattr(chat_mod, "_start_steer_turn", _fake_start)
    first = await chat_mod.steer_or_start_turn(
        "chat-dup", "SECRET-ONCE", "m1", private=True, private_store=store, private_context=op
    )
    ref = first["private_ref"]
    # Within the process the in-memory start guard already joins a retry to the first turn.
    again = await chat_mod.steer_or_start_turn(
        "chat-dup", "SECRET-ONCE", "m1", private=True, private_store=store, private_context=op
    )
    assert again["started_turn"] == first["started_turn"] and len(starts) == 1

    async def _n_shells() -> int:
        async with store.engine.connect() as conn:
            return (await conn.execute(_sql("SELECT COUNT(*) FROM tasks WHERE public_visible=0"))).scalar()

    # A restart drops the in-memory guard; the persisted dedupe key still reuses the ONE record
    # (same reference, no second shell).
    steering._reset()
    third = await chat_mod.steer_or_start_turn(
        "chat-dup", "SECRET-ONCE", "m1", private=True, private_store=store, private_context=op
    )
    assert third["private_ref"] == ref and await _n_shells() == 1

    # Once the turn has folded its reply into the record, a retry starts NO further turn.
    assert await private_intake.record_private_reply(store, ref, "done, privately", op)
    starts.clear()
    steering._reset()
    replayed = await chat_mod.steer_or_start_turn(
        "chat-dup", "SECRET-ONCE", "m1", private=True, private_store=store, private_context=op
    )
    assert replayed["ok"] is True and "started_turn" not in replayed and replayed["private_ref"] == ref
    assert starts == [] and await _n_shells() == 1
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
@pytest.mark.parametrize("latest_metadata", [None, {}])
async def test_idle_check_resolves_the_latest_turns_thread(monkeypatch, latest_metadata):
    # A later idle start / parked resume must resolve the ORIGINAL turn's checkpointer thread,
    # not one rebuilt from the idle call's empty metadata under a custom resolver (#571).
    from graph import thread_ids

    thread_ids.forget_resolver_metadata("chat-x")
    monkeypatch.setattr(STATE, "thread_id_resolver",
                        lambda md, sid: f"proj:{md.get('project', 'none')}:{sid}", raising=False)
    seen: dict = {}
    snapshots = {}

    class _G:
        async def aget_state(self, config):
            seen["tid"] = config["configurable"]["thread_id"]
            return snapshots.get(seen["tid"], _idle_snapshot())

    monkeypatch.setattr(STATE, "graph", _G(), raising=False)
    monkeypatch.setattr(STATE, "a2a_task_engine", object(), raising=False)
    assert await chat_mod._session_turn_idle("chat-x") is True
    assert seen["tid"] == "proj:none:chat-x"
    thread_ids.remember_resolver_metadata("chat-x", {"project": "atlas"})
    assert await chat_mod._session_turn_idle("chat-x") is True
    assert seen["tid"] == "proj:atlas:chat-x"
    # A subsequent metadata-less turn parks on the unscoped thread. Keeping atlas's
    # inputs would miss that pause and incorrectly allow a fresh idle start.
    thread_ids.remember_resolver_metadata("chat-x", latest_metadata)
    snapshots["proj:none:chat-x"] = _parked_snapshot()
    assert await chat_mod._session_turn_idle("chat-x") is False
    assert seen["tid"] == "proj:none:chat-x"
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
    ok, text, store_got, ctx_got = await chat_mod._resolve_private_turn_input(ref, "chat-r", {DELEGATION_HEADER: token})
    assert ok is True and text == "RUNNER-SECRET" and store_got is not None and ctx_got is not None
    # The resolution ALSO hands back the store + operator context so the runner can fold the
    # turn's reply back into the SAME record — the context it returns may write the partition.
    assert await private_intake.record_private_reply(store_got, ref, "RUNNER-REPLY", ctx_got)
    assert (await store.get_record(private_intake.ref_task_id(ref), op)).private_payload["reply"] == "RUNNER-REPLY"
    # Fail-closed: no delegation header, or one bound to a DIFFERENT reference — no store/context.
    assert await chat_mod._resolve_private_turn_input(ref, "chat-r", {}) == (False, "", None, None)
    other = chat_mod._mint_loopback_delegation(context="chat-r", operation="private-input:other")
    assert await chat_mod._resolve_private_turn_input(ref, "chat-r", {DELEGATION_HEADER: other}) == (False, "", None, None)
    await engine.dispose()
    steering._reset()


@pytest.mark.asyncio
async def test_two_concurrent_private_steers_record_one_shell_and_start_one_turn(tmp_path, monkeypatch, delegation_env):
    # Two concurrent private POSTs of the SAME msg_id must record ONE private shell and start
    # ONE turn: the start is claimed synchronously BEFORE the partition read/write, so the
    # second joins the first instead of both missing the dedupe record and each minting a shell.
    import asyncio

    from sqlalchemy import text as _sql

    steering._reset()
    engine, store = await _partition(tmp_path)
    monkeypatch.setattr(STATE, "graph", _Graph(_idle_snapshot()), raising=False)
    monkeypatch.setattr(STATE, "a2a_task_engine", engine, raising=False)
    op = _operator_ctx()
    gate = asyncio.Event()
    starts: list = []

    async def _gated_start(session_id, text, msg_id, *, images=None, incognito=False, settings=None,
                           private_ref=None, delegation=None, hitl_resume=False):
        starts.append(private_ref)
        await gate.wait()  # hold BOTH callers in flight while the race window is open
        return f"task-{len(starts)}"

    monkeypatch.setattr(chat_mod, "_start_steer_turn", _gated_start)

    async def _steer():
        return await chat_mod.steer_or_start_turn(
            "chat-cc", "SECRET-CONCURRENT", "m1", private=True, private_store=store, private_context=op
        )

    first = asyncio.create_task(_steer())
    await asyncio.sleep(0.02)  # the first claimed the start and is awaiting the loopback send
    dup = asyncio.create_task(_steer())
    await asyncio.sleep(0.02)
    gate.set()
    r1, r2 = await asyncio.gather(first, dup)
    assert r1["started_turn"] == r2["started_turn"]  # both name the ONE turn
    assert len(starts) == 1  # ONE loopback send…

    async def _n_shells() -> int:
        async with store.engine.connect() as conn:
            return (await conn.execute(_sql("SELECT COUNT(*) FROM tasks WHERE public_visible=0"))).scalar()

    assert await _n_shells() == 1  # …and exactly ONE private shell, not two
    await engine.dispose()
    steering._reset()


@pytest.mark.asyncio
async def test_private_turn_resolves_the_original_thread_and_keeps_its_inputs(monkeypatch):
    # A private steer / parked resume carries only its reference + delegation, no resolver
    # inputs: it must resolve the ORIGINAL turn's checkpointer thread from the PERSISTED inputs
    # (so the resume lands on the parked interrupt under a custom resolver), and must NOT
    # overwrite those inputs with its own metadata-less carriage.
    from graph import thread_ids

    thread_ids.forget_resolver_metadata("chat-pt")
    monkeypatch.setattr(STATE, "thread_id_resolver",
                        lambda md, sid: f"proj:{md.get('project', 'none')}:{sid}", raising=False)
    # The original parked turn persisted its resolver inputs.
    thread_ids.remember_resolver_metadata("chat-pt", {"project": "atlas"})
    from graph.fleet.delegation import DELEGATION_HEADER

    carriage = {"private_input": True, DELEGATION_HEADER: "tok"}
    tid = chat_mod._resolve_turn_thread_id(carriage, "chat-pt")
    assert tid == "proj:atlas:chat-pt"  # resolved from the ORIGINAL inputs, not the carriage
    assert thread_ids.resolver_metadata("chat-pt") == {"project": "atlas"}  # untouched
    # An ordinary turn resolves from, and persists, its own metadata (newest request wins).
    assert chat_mod._resolve_turn_thread_id({"project": "zephyr"}, "chat-pt") == "proj:zephyr:chat-pt"
    assert thread_ids.resolver_metadata("chat-pt") == {"project": "zephyr"}
    thread_ids.forget_resolver_metadata("chat-pt")


@pytest.mark.asyncio
async def test_fold_private_reply_frames_records_the_turns_reply(tmp_path):
    # The runner wraps a private turn's frames with this: a completed turn (``done``) folds its
    # reply into the record (console recovery), and the record then reads ``replied`` so a
    # retried steer starts no second turn. Frames pass through untouched.
    from a2a_impl import private_intake

    engine, store = await _partition(tmp_path)
    op = _operator_ctx()
    ref = await private_intake.record_private_steer(
        store, session_id="chat-fold", text="FOLD-SECRET", msg_id="m1", context=op
    )

    async def _frames():
        yield ("tool_start", "working")
        yield ("done", "the private answer")

    seen = [f async for f in chat_mod._fold_private_reply_frames(_frames(), ref, store, op)]
    assert seen == [("tool_start", "working"), ("done", "the private answer")]
    rec = await store.get_record(private_intake.ref_task_id(ref), op)
    assert rec.private_payload["reply"] == "the private answer"
    _r, replied = await private_intake.find_private_steer(store, session_id="chat-fold", msg_id="m1", context=op)
    assert replied is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_fold_private_reply_frames_folds_nothing_when_the_turn_reparks(tmp_path):
    # A private turn that RE-PARKS (no ``done``) folds nothing, so its record stays un-replied
    # and the operator's resume still runs — the reply is only recorded on completion.
    from a2a_impl import private_intake

    engine, store = await _partition(tmp_path)
    op = _operator_ctx()
    ref = await private_intake.record_private_steer(
        store, session_id="chat-park", text="PARK-SECRET", msg_id="m1", context=op
    )

    async def _frames():
        yield ("input_required", {"question": "which one?"})

    _ = [f async for f in chat_mod._fold_private_reply_frames(_frames(), ref, store, op)]
    rec = await store.get_record(private_intake.ref_task_id(ref), op)
    assert rec.private_payload["reply"] is None
    _r, replied = await private_intake.find_private_steer(store, session_id="chat-park", msg_id="m1", context=op)
    assert replied is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_runner_resolves_the_reference_then_folds_the_reply(tmp_path, monkeypatch, delegation_env):
    # The REAL runner (``_chat_langgraph_stream_impl``) resolves the opaque reference to the
    # private text, runs the turn, and folds its ``done`` reply back into the SAME partition
    # record — the production call site the record's ``replied`` idempotency depends on.
    from graph.fleet.delegation import DELEGATION_HEADER

    from a2a_impl import private_intake

    steering._reset()
    engine, store = await _partition(tmp_path)
    monkeypatch.setattr(STATE, "graph", _Graph(_idle_snapshot()), raising=False)
    monkeypatch.setattr(STATE, "a2a_task_engine", engine, raising=False)
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(max_iterations=50), raising=False)
    op = _operator_ctx()
    ref = await private_intake.record_private_steer(
        store, session_id="chat-run", text="RUN-SECRET", msg_id="m1", context=op
    )
    token = chat_mod._mint_loopback_delegation(context="chat-run", operation=ref)

    async def _no_pre(pre, session_id, request_metadata):
        return
        yield  # pragma: no cover — makes this an async generator that yields nothing

    seen: dict = {}

    async def _native(message, session_id, config, **kwargs):
        seen["message"] = message  # the runner resolved the reference to the private text
        yield ("done", "the private reply")

    async def _model(session_id, request_model):
        return "", ""

    monkeypatch.setattr(chat_mod._chat_dispatch, "_pre_turn_dispatch", _no_pre)
    monkeypatch.setattr(chat_mod, "_run_native_turn", _native)
    monkeypatch.setattr(chat_mod._goal_loop, "resolve_turn_model", _model)

    frames = [
        f
        async for f in chat_mod._chat_langgraph_stream_impl(
            ref, "chat-run", request_metadata={"private_input": True, DELEGATION_HEADER: token}
        )
    ]
    assert seen["message"] == "RUN-SECRET"  # resolved privately, never run as the literal reference
    assert frames[-1] == ("done", "the private reply")
    rec = await store.get_record(private_intake.ref_task_id(ref), op)
    assert rec.private_payload["reply"] == "the private reply" and rec.private_payload["input"] == "RUN-SECRET"
    # The record now reads replied, so a later retry of the same steer starts no second turn.
    _r, replied = await private_intake.find_private_steer(store, session_id="chat-run", msg_id="m1", context=op)
    assert replied is True
    await engine.dispose()
    steering._reset()


# ── durable one-audience-per-segment scheduling (ADR 0131 P1c; dormant) ────────────
# Accepted inputs persist as ordered pending turns on the existing task records; the active
# segment is one more durable record carrying a monotonic generation. Every transition is a
# transactional compare-and-set on that generation (a2a_impl.segment_schedule), not a process
# lock, so completion-vs-acceptance is one ordering decision and a real restart recovers the
# queue, audience and order from the reopened store with no tool replay and no public fallback.


async def _open_store(path):
    from a2a_impl import stores

    engine = stores.make_sqlite_engine(str(path))
    store = stores.ReasoningCoalescingTaskStore(engine, owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    return engine, store


@pytest.mark.asyncio
async def test_pending_turns_persist_in_order_with_every_field(tmp_path):
    from a2a_impl import segment_schedule as ss

    engine, store = await _partition(tmp_path)
    op = _operator_ctx()
    peer = await ss.accept_pending_turn(
        store, session_id="chat-s", audience=ss.AUDIENCE_PEER, principal="op-bearer", dedupe="m1",
        checkpoint="proj:atlas:chat-s", interrupt_id="", context=op,
    )
    priv = await ss.accept_pending_turn(
        store, session_id="chat-s", audience=ss.AUDIENCE_PRIVATE, principal="op-bearer", dedupe="m2",
        checkpoint="proj:atlas:chat-s", interrupt_id="i7", input_ref="private-input:abc", context=op,
    )
    assert (peer["seq"], priv["seq"]) == (1, 2)  # monotonic durable acceptance order
    turns = await ss.pending_turns(store, "chat-s", op)
    assert [t["seq"] for t in turns] == [1, 2]
    # Every scheduling field is on the record: sequence, audience, principal, dedupe, checkpoint,
    # interrupt id, input reference and state.
    assert turns[0] == {
        "version": 1, "kind": "pending-turn", "seq": 1, "audience": "peer", "principal": "op-bearer",
        "dedupe": "m1", "checkpoint": "proj:atlas:chat-s", "interrupt_id": "", "input_ref": "", "state": "queued",
    }
    assert turns[1]["audience"] == "private" and turns[1]["input_ref"] == "private-input:abc"
    assert turns[1]["interrupt_id"] == "i7"
    await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_message_id_gives_exactly_one_pending_turn(tmp_path):
    from sqlalchemy import text as _sql

    from a2a_impl import segment_schedule as ss

    engine, store = await _partition(tmp_path)
    op = _operator_ctx()
    first = await ss.accept_pending_turn(
        store, session_id="chat-d", audience=ss.AUDIENCE_PEER, principal="op", dedupe="m1", context=op
    )
    again = await ss.accept_pending_turn(
        store, session_id="chat-d", audience=ss.AUDIENCE_PEER, principal="op", dedupe="m1", context=op
    )
    assert again["seq"] == first["seq"]  # the retry reuses the one turn
    async with store.engine.connect() as conn:
        n = (await conn.execute(_sql("SELECT COUNT(*) FROM tasks WHERE public_visible=0"))).scalar()
    assert n == 1  # one record, not two
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["fold_wins", "complete_wins"])
async def test_completion_versus_acceptance_is_one_transactional_ordering(tmp_path, order):
    # A steer arriving exactly as a turn completes: the single generation CAS decides. Either the
    # fold wins (it bumps the generation, so completion loses and the turn keeps running to drain
    # it — one turn) or completion wins (the fold's CAS then matches nothing, so the steer stays
    # queued for the next segment — one turn). Never both, never neither.
    from a2a_impl import segment_schedule as ss

    engine, store = await _partition(tmp_path)
    op = _operator_ctx()
    head = await ss.accept_pending_turn(
        store, session_id="chat-c", audience=ss.AUDIENCE_PEER, principal="op", dedupe="m1", context=op
    )
    gen = await ss.claim_segment(store, "chat-c", audience=ss.AUDIENCE_PEER, seq=head["seq"], context=op)
    await ss.set_turn_state(store, "chat-c", head["seq"], ss.STATE_RUNNING, op)
    await ss.accept_pending_turn(
        store, session_id="chat-c", audience=ss.AUDIENCE_PEER, principal="op", dedupe="m2", context=op
    )

    if order == "fold_wins":
        gen2 = await ss.try_fold(store, "chat-c", generation=gen, audience=ss.AUDIENCE_PEER, seq=2, context=op)
        assert gen2 == gen + 1  # folded into the running turn
        recovered = await ss.recover_undrained_segments(store, "chat-c", op)
        assert [t["dedupe"] for t in recovered["suspended"]] == ["m1", "m2"]
        assert await ss.complete_segment(store, "chat-c", generation=gen, context=op) is False  # completion lost
        assert await ss.complete_segment(store, "chat-c", generation=gen2, context=op) is True
        assert await ss.next_dispatch(store, "chat-c", op) is None
    else:
        assert await ss.complete_segment(store, "chat-c", generation=gen, context=op) is True  # completion won
        # The fold now matches nothing: the steer is NOT read by the turn that just ended.
        assert await ss.try_fold(store, "chat-c", generation=gen, audience=ss.AUDIENCE_PEER, seq=2, context=op) is None
        # It stays queued; a fresh segment runs it (the next-start half of the same ordering).
        nxt = await ss.next_dispatch(store, "chat-c", op)
        assert nxt["dedupe"] == "m2"
    await engine.dispose()


@pytest.mark.asyncio
async def test_completion_race_resolves_to_exactly_one_outcome_concurrently(tmp_path):
    # The same ordering under a genuine concurrent race on the one SQLite record: fold and
    # completion run together; exactly one commits at the shared generation.
    import asyncio

    from a2a_impl import segment_schedule as ss

    engine, store = await _partition(tmp_path)
    op = _operator_ctx()
    head = await ss.accept_pending_turn(
        store, session_id="chat-r", audience=ss.AUDIENCE_PEER, principal="op", dedupe="m1", context=op
    )
    gen = await ss.claim_segment(store, "chat-r", audience=ss.AUDIENCE_PEER, seq=head["seq"], context=op)
    await ss.accept_pending_turn(
        store, session_id="chat-r", audience=ss.AUDIENCE_PEER, principal="op", dedupe="m2", context=op
    )
    folded, completed = await asyncio.gather(
        ss.try_fold(store, "chat-r", generation=gen, audience=ss.AUDIENCE_PEER, seq=2, context=op),
        ss.complete_segment(store, "chat-r", generation=gen, context=op),
    )
    assert bool(folded) != bool(completed)  # exactly one of the two won the generation CAS
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_cross_audience_steer_queues_behind_the_running_segment(tmp_path):
    # Audience is fixed when the segment is claimed; the other audience queues behind, ordered by
    # acceptance seq, and next_dispatch never crosses audiences. (The model-input proof of BOTH
    # directions is test_no_model_call_mixes_audiences_in_a_real_graph in test_steering.py.)
    from a2a_impl import segment_schedule as ss

    engine, store = await _partition(tmp_path)
    op = _operator_ctx()
    # A private turn is accepted first and claims the runner; a peer steer arrives during it.
    priv = await ss.accept_pending_turn(
        store, session_id="chat-q", audience=ss.AUDIENCE_PRIVATE, principal="op", dedupe="m1",
        input_ref="private-input:x", context=op,
    )
    gen = await ss.claim_segment(store, "chat-q", audience=ss.AUDIENCE_PRIVATE, seq=priv["seq"], context=op)
    await ss.set_turn_state(store, "chat-q", priv["seq"], ss.STATE_RUNNING, op)
    await ss.accept_pending_turn(
        store, session_id="chat-q", audience=ss.AUDIENCE_PEER, principal="op", dedupe="m2", context=op
    )
    # The peer steer must NOT fold into the running private segment, and the runner is reserved.
    assert await ss.try_fold(store, "chat-q", generation=gen, audience=ss.AUDIENCE_PEER, seq=2, context=op) is None
    assert await ss.next_dispatch(store, "chat-q", op) is None  # reserved by the private segment
    # Once the private segment completes, the peer turn is the next dispatch (its own segment).
    assert await ss.complete_segment(store, "chat-q", generation=gen, context=op) is True
    nxt = await ss.next_dispatch(store, "chat-q", op)
    assert nxt["dedupe"] == "m2" and nxt["audience"] == "peer"
    await engine.dispose()


@pytest.mark.asyncio
async def test_parked_segment_reserves_runner_until_its_matching_resume(tmp_path):
    # A parked segment holds the runner; only its matching guarded resume continues it, and the
    # resume may pass queued fresh inputs of its own audience. An ordinary steer does not resume it.
    from a2a_impl import segment_schedule as ss

    engine, store = await _partition(tmp_path)
    op = _operator_ctx()
    head = await ss.accept_pending_turn(
        store, session_id="chat-p", audience=ss.AUDIENCE_PEER, principal="op", dedupe="m1", context=op
    )
    gen = await ss.claim_segment(store, "chat-p", audience=ss.AUDIENCE_PEER, seq=head["seq"], context=op)
    await ss.set_turn_state(store, "chat-p", head["seq"], ss.STATE_RUNNING, op)
    assert await ss.park_segment(store, "chat-p", generation=gen, interrupt_id="i42", context=op) is True
    await ss.set_turn_state(store, "chat-p", head["seq"], ss.STATE_PARKED, op, interrupt_id="i42")
    seg = await ss.read_segment(store, "chat-p", op)
    assert seg["state"] == "parked"  # reserved, not idle
    # A fresh peer steer queues behind; the runner stays reserved (no dispatch).
    await ss.accept_pending_turn(
        store, session_id="chat-p", audience=ss.AUDIENCE_PEER, principal="op", dedupe="m2", context=op
    )
    assert await ss.next_dispatch(store, "chat-p", op) is None
    # A resume for a DIFFERENT interrupt (an ordinary steer) cannot continue it.
    assert await ss.resume_segment(store, "chat-p", generation=seg["generation"], interrupt_id="other", context=op) is None
    # The matching guarded resume continues the SAME segment; the queued fresh input is now
    # available to unblock the interrupt.
    g2 = await ss.resume_segment(store, "chat-p", generation=seg["generation"], interrupt_id="i42", context=op)
    assert g2 == seg["generation"] + 1
    fresh = await ss.pending_turns(store, "chat-p", op, states=(ss.STATE_QUEUED,))
    assert [t["dedupe"] for t in fresh] == ["m2"]
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("head_state", ["queued", "running", "parked"])
async def test_completion_retires_only_attached_turns_durably(tmp_path, head_state):
    from a2a_impl import segment_schedule as ss

    path = tmp_path / "completed.db"
    engine, store = await _open_store(path)
    op = _operator_ctx()
    for dedupe, audience in [
        ("head", ss.AUDIENCE_PEER), ("folded", ss.AUDIENCE_PEER),
        ("later-private", ss.AUDIENCE_PRIVATE), ("later-peer", ss.AUDIENCE_PEER),
    ]:
        await ss.accept_pending_turn(
            store, session_id="chat", audience=audience, principal="op", dedupe=dedupe, context=op,
        )
    generation = await ss.claim_segment(store, "chat", audience=ss.AUDIENCE_PEER, seq=1, context=op)
    await ss.set_turn_state(store, "chat", 1, head_state, op)
    folded_generation = await ss.try_fold(
        store, "chat", generation=generation, audience=ss.AUDIENCE_PEER, seq=2, context=op,
    )
    before = await store.scheduling_payloads("chat", op)
    assert not await ss.complete_segment(store, "chat", generation=generation, context=op)
    assert await store.scheduling_payloads("chat", op) == before
    assert await ss.complete_segment(store, "chat", generation=folded_generation, context=op)
    await engine.dispose()

    engine, store = await _open_store(path)
    try:
        recovered = await ss.recover_undrained_segments(store, "chat", op)
        assert not recovered["reserved"]
        assert recovered["suspended"] == recovered["parked"] == []
        assert [t["dedupe"] for t in recovered["queued"]] == ["later-private", "later-peer"]
        terminal = await ss.pending_turns(store, "chat", op, states=(ss.STATE_TERMINAL,))
        assert [t["dedupe"] for t in terminal] == ["head", "folded"]
        retry = await ss.accept_pending_turn(
            store, session_id="chat", audience=ss.AUDIENCE_PEER, principal="op", dedupe="folded", context=op,
        )
        assert retry == terminal[1]
        next_turn = await ss.next_dispatch(store, "chat", op)
        assert next_turn["dedupe"] == "later-private"
        assert await ss.claim_segment(
            store, "chat", audience=next_turn["audience"], seq=next_turn["seq"], context=op,
        ) is not None
        before = await store.scheduling_payloads("chat", op)
        assert not await ss.complete_segment(store, "chat", generation=folded_generation, context=op)
        assert await store.scheduling_payloads("chat", op) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_restart_after_acceptance_recovers_queue_audience_and_order(tmp_path):
    # A real process restart (the store is disposed and reopened from the same file) after inputs
    # were accepted but before any ran: the durable queue, audience and order are recovered.
    from a2a_impl import segment_schedule as ss

    path = tmp_path / "seg.db"
    op = _operator_ctx()
    engine, store = await _open_store(path)
    await ss.accept_pending_turn(store, session_id="chat-a", audience=ss.AUDIENCE_PEER, principal="op", dedupe="m1", context=op)
    await ss.accept_pending_turn(
        store, session_id="chat-a", audience=ss.AUDIENCE_PRIVATE, principal="op", dedupe="m2",
        input_ref="private-input:x", context=op,
    )
    await engine.dispose()  # == the process restarts

    engine, store = await _open_store(path)
    rec = await ss.recover_undrained_segments(store, "chat-a", op)
    assert rec["reserved"] is False  # no runner was claimed before the crash
    assert [(t["seq"], t["audience"], t["dedupe"]) for t in rec["queued"]] == [
        (1, "peer", "m1"), (2, "private", "m2"),
    ]
    assert rec["audience"] == "peer"  # the head queued turn's audience leads the next dispatch
    await engine.dispose()


@pytest.mark.asyncio
async def test_restart_during_execution_suspends_without_replaying_a_tool_effect(tmp_path):
    # A restart while a turn was RUNNING: its pending turn is reported suspended for
    # reconciliation, never re-dispatched — so a tool effect it may already have fired is not
    # replayed. A private running turn keeps its opaque reference; there is no public fallback.
    from a2a_impl import segment_schedule as ss

    path = tmp_path / "seg.db"
    op = _operator_ctx()
    engine, store = await _open_store(path)
    head = await ss.accept_pending_turn(
        store, session_id="chat-e", audience=ss.AUDIENCE_PRIVATE, principal="op", dedupe="m1",
        input_ref="private-input:secret", context=op,
    )
    await ss.claim_segment(store, "chat-e", audience=ss.AUDIENCE_PRIVATE, seq=head["seq"], context=op)
    await ss.set_turn_state(store, "chat-e", head["seq"], ss.STATE_RUNNING, op)
    await ss.accept_pending_turn(store, session_id="chat-e", audience=ss.AUDIENCE_PEER, principal="op", dedupe="m2", context=op)
    await engine.dispose()  # == a crash mid-execution

    engine, store = await _open_store(path)
    rec = await ss.recover_undrained_segments(store, "chat-e", op)
    assert rec["reserved"] is True and rec["segment"]["state"] == "running"  # the runner stays held
    assert [t["dedupe"] for t in rec["suspended"]] == ["m1"]  # the interrupted turn is suspended…
    assert rec["suspended"][0]["input_ref"] == "private-input:secret"  # …with its private reference intact
    assert rec["suspended"] != rec["queued"]  # NOT re-queued for a fresh dispatch (no replay)
    assert rec["audience"] == "private"  # the reserved audience, never a public fallback
    assert [t["dedupe"] for t in rec["queued"]] == ["m2"]  # the peer input still waits behind it
    # next_dispatch refuses to start anything while the interrupted segment holds the runner.
    assert await ss.next_dispatch(store, "chat-e", op) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_restart_while_parked_keeps_the_reservation(tmp_path):
    # A restart while a segment was PARKED: it stays reserved, with its interrupt id, so only its
    # matching resume continues it after recovery — exactly as before the crash.
    from a2a_impl import segment_schedule as ss

    path = tmp_path / "seg.db"
    op = _operator_ctx()
    engine, store = await _open_store(path)
    head = await ss.accept_pending_turn(store, session_id="chat-k", audience=ss.AUDIENCE_PEER, principal="op", dedupe="m1", context=op)
    gen = await ss.claim_segment(store, "chat-k", audience=ss.AUDIENCE_PEER, seq=head["seq"], context=op)
    await ss.park_segment(store, "chat-k", generation=gen, interrupt_id="i99", context=op)
    await engine.dispose()

    engine, store = await _open_store(path)
    rec = await ss.recover_undrained_segments(store, "chat-k", op)
    assert rec["reserved"] is True and rec["segment"]["state"] == "parked"
    assert rec["segment"]["interrupt_id"] == "i99"
    assert await ss.next_dispatch(store, "chat-k", op) is None  # reserved until the matching resume
    seg = rec["segment"]
    assert await ss.resume_segment(store, "chat-k", generation=seg["generation"], interrupt_id="i99", context=op) == seg["generation"] + 1
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_failed_dispatch_requeues_only_a_proven_refusal(tmp_path, ambiguous):
    # A failed loopback (the dispatch could not start) must not lose or terminalize the accepted
    # input: it stays queued, so recovery re-offers it. It is never replaced by a public fallback.
    from a2a_impl import segment_schedule as ss

    engine, store = await _partition(tmp_path)
    op = _operator_ctx()
    turn = await ss.accept_pending_turn(
        store, session_id="chat-f", audience=ss.AUDIENCE_PRIVATE, principal="op", dedupe="m1",
        input_ref="private-input:keep", context=op,
    )
    import httpx

    attempts = []

    async def loopback(request):
        attempts.append(request)
        if ambiguous:
            raise httpx.ReadTimeout("delivery outcome unknown")
        return httpx.Response(503)

    async def send(pending):
        assert pending["state"] == "running"
        async with httpx.AsyncClient(transport=httpx.MockTransport(loopback)) as client:
            response = await client.post("http://loopback/a2a", json={"input_ref": pending["input_ref"]})
        # This fake receiver explicitly refuses before starting any execution.
        assert response.status_code == 503
        return None

    if ambiguous:
        with pytest.raises(httpx.ReadTimeout):
            await ss.dispatch_pending_turn(store, "chat-f", op, send)
        recovered = await ss.recover_undrained_segments(store, "chat-f", op)
        assert recovered["reserved"] and recovered["queued"] == []
        assert recovered["suspended"][0]["input_ref"] == "private-input:keep"
        assert await ss.next_dispatch(store, "chat-f", op) is None
        await engine.dispose()
        return
    assert await ss.dispatch_pending_turn(store, "chat-f", op, send) is None
    assert len(attempts) == 1
    assert await ss.next_dispatch(store, "chat-f", op) == turn
    still = await ss.pending_turns(store, "chat-f", op, states=(ss.STATE_QUEUED,))
    assert [t["dedupe"] for t in still] == ["m1"]
    assert still[0]["input_ref"] == "private-input:keep"  # its private reference is intact
    assert turn["state"] == "queued"
    await engine.dispose()


@pytest.mark.asyncio
async def test_refusal_after_a_fold_requeues_both_and_never_wedges_the_runner(tmp_path):
    # The round-2 blocker: a same-audience steer FOLDS into the claimed segment while the head's
    # dispatch is still in flight (bumping the generation), and then the loopback PROVES refusal
    # (send returns None). The head never ran, so the folded input never ran either — both must
    # return to the queue and the runner to idle. The segment must NOT be left running at the
    # bumped generation with no completer (a wedged runner that never dispatches again).
    from a2a_impl import segment_schedule as ss

    engine, store = await _partition(tmp_path)
    op = _operator_ctx()
    head = await ss.accept_pending_turn(
        store, session_id="chat-wedge", audience=ss.AUDIENCE_PEER, principal="op", dedupe="m1", context=op
    )

    async def send(pending):
        # In-flight: a second same-audience steer is accepted and folded into the running segment,
        # bumping the generation the dispatch claimed at.
        assert pending["seq"] == head["seq"] and pending["state"] == "running"
        await ss.accept_pending_turn(
            store, session_id="chat-wedge", audience=ss.AUDIENCE_PEER, principal="op", dedupe="m2", context=op
        )
        seg = await ss.read_segment(store, "chat-wedge", op)
        folded = await ss.try_fold(
            store, "chat-wedge", generation=seg["generation"], audience=ss.AUDIENCE_PEER, seq=2, context=op
        )
        assert folded == seg["generation"] + 1  # the fold committed; the segment moved on
        return None  # …but the loopback then PROVES refusal (nothing started)

    assert await ss.dispatch_pending_turn(store, "chat-wedge", op, send) is None
    # Neither input is left running: both are back in the queue in acceptance order, the runner is
    # idle (not stuck at the folded generation), and next_dispatch can cleanly re-claim the head.
    seg = await ss.read_segment(store, "chat-wedge", op)
    assert seg["state"] == "idle" and seg["audience"] == "" and seg["seq"] is None
    queued = await ss.pending_turns(store, "chat-wedge", op, states=(ss.STATE_QUEUED,))
    assert [t["dedupe"] for t in queued] == ["m1", "m2"]
    assert all("segment_seq" not in t for t in queued)  # the fold marker is cleared on requeue
    assert await ss.pending_turns(store, "chat-wedge", op, states=(ss.STATE_RUNNING,)) == []
    nxt = await ss.next_dispatch(store, "chat-wedge", op)
    assert nxt["dedupe"] == "m1"  # the runner re-claims the head cleanly — not wedged
    await engine.dispose()


@pytest.mark.asyncio
async def test_recover_durable_steers_fails_closed_without_a_partition(idle_server):
    # The server-layer recovery seam is fail-closed: with no partition store/context there is
    # nothing durable to recover, so it returns None rather than guessing a public restart.
    assert await chat_mod.recover_durable_steers("chat-x") is None
    assert await chat_mod.recover_durable_steers("chat-x", store=None, context=_operator_ctx()) is None


@pytest.mark.asyncio
async def test_recover_durable_steers_reloads_the_durable_queue(tmp_path):
    # Through the server seam: it reopens the partition and returns the reloaded queue + audience.
    from a2a_impl import segment_schedule as ss

    path = tmp_path / "seg.db"
    op = _operator_ctx()
    engine, store = await _open_store(path)
    await ss.accept_pending_turn(store, session_id="chat-w", audience=ss.AUDIENCE_PEER, principal="op", dedupe="m1", context=op)
    await engine.dispose()

    engine, store = await _open_store(path)
    rec = await chat_mod.recover_durable_steers("chat-w", store=store, context=op)
    assert rec is not None and [t["dedupe"] for t in rec["queued"]] == ["m1"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_accepts_and_claims_preserve_every_input(tmp_path):
    import asyncio
    from a2a_impl import segment_schedule as ss

    path = tmp_path / "concurrent.db"
    engine, store = await _open_store(path)
    other_engine, other = await _open_store(path)
    op = _operator_ctx()
    try:
        accepted = await asyncio.gather(*(
            ss.accept_pending_turn(s, session_id="race", audience="peer", principal="op",
                                   dedupe=mid, context=op)
            for s, mid in [(store, "a"), (other, "b"), (other, "a")]
        ))
        assert len({t["seq"] for t in accepted}) == 2
        assert accepted[0] == accepted[2]
        claims = await asyncio.gather(*(
            ss.claim_segment(s, "race", audience="peer", seq=seq, context=op)
            for s, seq in [(store, 1), (other, 2)]
        ))
        assert sum(g is not None for g in claims) == 1
        seg = await ss.read_segment(store, "race", op)
        assert seg["state"] == "running" and seg["generation"] == 1
    finally:
        await engine.dispose()
        await other_engine.dispose()


@pytest.mark.asyncio
async def test_queue_and_dedupe_survive_more_than_a_history_window(tmp_path):
    from a2a.types import a2a_pb2
    from a2a_impl import segment_schedule as ss

    engine, store = await _partition(tmp_path)
    op = _operator_ctx()
    try:
        first = await ss.accept_pending_turn(store, session_id="long", audience="private",
                                            principal="op", dedupe="old", context=op)
        for i in range(205):
            await store.save(a2a_pb2.Task(id=f"public-{i}", context_id="long"), op)
        again = await ss.accept_pending_turn(store, session_id="long", audience="private",
                                            principal="op", dedupe="old", context=op)
        new = await ss.accept_pending_turn(store, session_id="long", audience="peer",
                                          principal="op", dedupe="new", context=op)
        assert again == first and new["seq"] == 2
        assert await ss.next_dispatch(store, "long", op) == first
        recovered = await ss.recover_undrained_segments(store, "long", op)
        assert recovered["queued"] == [first, new]
    finally:
        await engine.dispose()
