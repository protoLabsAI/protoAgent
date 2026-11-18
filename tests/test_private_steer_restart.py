"""Server-start recovery of private pending turns (ADR 0131 P1h.4), on the P1h.3 rig: the real
auth middleware, loopback ``SendMessage``, SDK handler, executor, stream runner and SQLite
partition. "Restart" is the durable records alone — every in-memory guard is reset."""

import asyncio
import importlib
import logging

import pytest

from a2a_impl import private_intake as pi
from a2a_impl import segment_schedule as ss
from graph import steering
from tests._a2a_app import poll_terminal
from tests.test_private_steer_route import REPLY, SECRET, _assert_private_only, rig  # noqa: F401 — fixture
from tests.test_steer_idle_starts_turn import _operator_ctx

chat_mod = importlib.import_module("server.chat")
pytestmark = [pytest.mark.platform_sensitive, pytest.mark.asyncio]
OP = _operator_ctx()


async def _crashed(session, head_state="queued", *, replied=False):
    """Records a process left behind: one private head in ``head_state`` plus a queued private
    follower, then the in-memory state a restart loses. Returns (store, head reference)."""
    store = chat_mod._private_partition_store()
    refs = [await pi.record_private_steer(store, session_id=session, text=f"{SECRET} {n}", msg_id=f"m{n}", context=OP)
            for n in (1, 2)]
    for n, ref in enumerate(refs, 1):
        await ss.accept_pending_turn(store, session_id=session, audience=ss.AUDIENCE_PRIVATE, principal="op",
                                     dedupe=f"m{n}", input_ref=ref, context=OP)
    if replied:
        await pi.record_private_reply(store, refs[0], "already answered", OP)
    if head_state != "queued":
        async def claimed(_turn):
            return "task-before-the-crash"

        await ss.dispatch_pending_turn(store, session, OP, claimed)
    if head_state == "parked":
        seg = await ss.read_segment(store, session, OP)
        assert await ss.park_segment(store, session, generation=seg["generation"], interrupt_id="i1", context=OP)
        await ss.set_turn_state(store, session, 1, ss.STATE_PARKED, OP, interrupt_id="i1")
    steering._reset()
    chat_mod._RECOVERED_PRIVATE_TURNS.clear()
    chat_mod._PRIVATE_RUNS.clear()
    return store, refs[0]


@pytest.mark.parametrize("replied", [False, True])
async def test_restarted_private_turns_drain_followers_through_the_private_path(rig, replied):  # noqa: F811
    store, ref = await _crashed("chat-q", replied=replied)
    out = await chat_mod.recover_private_pending_turns()
    assert [t["dedupe"] for t in out["recovered"]["chat-q"]["queued"]] == ["m1", "m2"]
    await out["dispatch"]
    # The head that already holds a reply ran before the crash: closed, never run a second time.
    for n in ([2] if replied else [1, 2]):
        task_id = next(row.id for row in await rig.rows()
                       if "private-input:" in str(row.history) and f'"messageId": "m{n}"' in row.history)
        task = await poll_terminal(rig.op, task_id)
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        # Each completion dispatches its follower before publishing the done frame.
        (part,) = task["history"][0]["parts"]
        assert pi.is_private_ref(part["text"]) and (n != 1 or part["text"] == ref)
        await _assert_private_only(rig, "chat-q", task_id)
    assert rig.seen == [(f"{SECRET} {n}", False) for n in ([2] if replied else [1, 2])]
    assert await ss.pending_turns(store, "chat-q", OP) == []
    assert (await ss.read_segment(store, "chat-q", OP))["state"] == ss.SEGMENT_IDLE  # the runner is released
    # The loopback carried only the opaque reference; the reply was folded back privately.
    assert any(f'"reply": "{REPLY}"' in (row.private_payload or "") for row in await rig.rows())
    assert chat_mod._RECOVERED_PRIVATE_TURNS == {}


@pytest.mark.parametrize("head_state", ["running", "parked"])
async def test_an_interrupted_or_parked_private_turn_stays_reserved(rig, head_state):  # noqa: F811
    store, _ref = await _crashed("chat-r", head_state)
    before = await store.scheduling_payloads("chat-r", OP)
    out = await chat_mod.recover_private_pending_turns()
    view = out["recovered"]["chat-r"]
    assert out["dispatch"] is None and view["reserved"] and view["audience"] == ss.AUDIENCE_PRIVATE
    assert [t["seq"] for t in view["suspended" if head_state == "running" else "parked"]] == [1]
    assert [t["seq"] for t in view["queued"]] == [2]
    # Nothing was sent, re-run or rewritten — not the head, and not the turn queued behind it.
    assert rig.seen == [] and await store.scheduling_payloads("chat-r", OP) == before
    assert await chat_mod._dispatch_recovered_private_turn("chat-r", store, OP) is None and rig.seen == []
    if head_state == "parked":  # only the matching guarded resume continues the parked segment
        gen = view["segment"]["generation"]
        assert await ss.resume_segment(store, "chat-r", generation=gen, interrupt_id="wrong", context=OP) is None
        assert await ss.resume_segment(store, "chat-r", generation=gen, interrupt_id="i1", context=OP) == gen + 1


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_follower_delivery_failure_preserves_work_without_failing_completed_head(rig, monkeypatch, ambiguous):  # noqa: F811
    store, _ref = await _crashed("chat-follow")
    start = chat_mod._start_steer_turn

    async def _send(session_id, text, msg_id, **kwargs):
        if msg_id == "m2":
            if ambiguous:
                raise RuntimeError("delivery outcome unknown")
            return ""
        return await start(session_id, text, msg_id, **kwargs)

    monkeypatch.setattr(chat_mod, "_start_steer_turn", _send)
    out = await chat_mod.recover_private_pending_turns()
    await out["dispatch"]
    task_id = next(row.id for row in await rig.rows() if "private-input:" in str(row.history))
    assert (await poll_terminal(rig.op, task_id))["status"]["state"] == "TASK_STATE_COMPLETED"
    assert rig.seen == [(f"{SECRET} 1", False)]
    (follower,) = await ss.pending_turns(store, "chat-follow", OP)
    assert follower["dedupe"] == "m2"
    assert follower["state"] == (ss.STATE_RUNNING if ambiguous else ss.STATE_QUEUED)
    assert (await ss.read_segment(store, "chat-follow", OP))["state"] == (
        ss.STATE_RUNNING if ambiguous else ss.SEGMENT_IDLE
    )


async def test_flag_off_startup_never_touches_the_partition(rig, monkeypatch):  # noqa: F811
    await _crashed("chat-off")
    before = await rig.rows()
    rig.config.private_steering_enabled = False

    def _no_partition():
        raise AssertionError("flag off: the partition must not be opened")

    monkeypatch.setattr(chat_mod, "_private_partition_store", _no_partition)
    assert await chat_mod.recover_private_pending_turns() is None
    assert await rig.rows() == before and rig.seen == [] and not chat_mod._STEER_RESTARTS


@pytest.mark.parametrize("broken", ["list", "reload", "refused", "transport"])
async def test_a_recovery_failure_is_logged_and_never_raises(rig, monkeypatch, caplog, broken):  # noqa: F811
    store, _ref = await _crashed("chat-e")

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("partition unreadable")

    async def _refused(*_args, **_kwargs):
        return ""

    target, replacement = {
        "list": (ss, ("sessions_with_pending_turns", _boom)),
        "reload": (ss, ("recover_undrained_segments", _boom)),
        "refused": (chat_mod, ("_start_steer_turn", _refused)),
        "transport": (chat_mod, ("_start_steer_turn", _boom)),
    }[broken]
    monkeypatch.setattr(target, *replacement)
    before = await ss.pending_turns(store, "chat-e", OP)
    with caplog.at_level(logging.WARNING, logger="protoagent.server"):
        out = await chat_mod.recover_private_pending_turns()
        if out and out["dispatch"]:
            await out["dispatch"]
    assert rig.seen == [] and (out is None) == (broken in ("list", "reload"))
    turns = await ss.pending_turns(store, "chat-e", OP)
    if broken == "transport":  # ambiguous delivery: reserved for reconciliation, never retried
        assert [t["state"] for t in turns] == ["running", "queued"] and "left reserved" in caplog.text
        assert await chat_mod._dispatch_recovered_private_turn("chat-e", store, OP) is None
    else:  # the records are exactly what the crash left
        assert turns == before
        assert broken == "refused" or "recovery" in caplog.text


@pytest.mark.parametrize("running", [False, True])
async def test_route_private_steer_survives_restart(rig, monkeypatch, running):  # noqa: F811
    # #5504: acceptance comes from the live route, never a hand-seeded queue.
    session = "chat-route-restart"
    await rig.steer("shared-bearer", session, "hold public", "public")
    await rig.running.wait()
    body = (await rig.steer("shared-bearer", session, SECRET + " hold", "private", private=True)).json()
    retry = (await rig.steer("shared-bearer", session, SECRET + " hold", "private", private=True)).json()
    assert retry["started_turn"] == body["started_turn"]
    if running:
        rig.running.clear()
        original = chat_mod._run_native_turn

        async def held(*args, **kwargs):
            rig.release.clear()
            async for frame in original(*args, **kwargs):
                yield frame

        monkeypatch.setattr(chat_mod, "_run_native_turn", held)
        rig.release.set()
        await asyncio.wait_for(rig.running.wait(), 5)
        monkeypatch.setattr(chat_mod, "_run_native_turn", original)
    await rig.restart()
    out = await chat_mod.recover_private_pending_turns()
    assert out is not None
    view = out["recovered"][session]
    (turn,) = view["suspended" if running else "queued"]
    assert turn["principal"] == "shared-operator-bearer" and turn["checkpoint"] == f"a2a:{session}"
    assert [t["dedupe"] for t in view["suspended" if running else "queued"]] == ["private"]
    if running:
        assert out["dispatch"] is None
    else:
        rig.release.set()
        await out["dispatch"]
        task_id = next(row.id for row in await rig.rows()
                       if "private-input:" in str(row.history) and row.id != body["started_turn"])
        await poll_terminal(rig.op, task_id)
    await _assert_private_only(rig, session, body["started_turn"])
    for peer in ("fleet-service", "paired-agent"):
        assert SECRET not in (await rig.clients[peer].get(f"/api/chat/sessions/{session}/turns")).text
    assert sum(SECRET in message for message, _ in rig.seen) == 1


async def test_pending_write_failure_refuses_before_send(rig, monkeypatch):  # noqa: F811
    async def fail(*args, **kwargs):
        raise RuntimeError("write unavailable")

    monkeypatch.setattr(ss, "accept_pending_turn", fail)
    response = await rig.steer("shared-bearer", "chat-write", SECRET, "m1", private=True)
    assert response.status_code == 503 and rig.seen == []
    assert not steering.starting("chat-write", "m1")
    assert not steering.pending_items("chat-write")
    assert not any("private-input:" in str(row.history) for row in await rig.rows())


async def test_retry_after_refused_delivery_reuses_durable_acceptance(rig, monkeypatch):  # noqa: F811
    async def refused(*args, **kwargs):
        return ""

    monkeypatch.setattr(chat_mod, "_start_steer_turn", refused)
    for _ in range(2):
        response = await rig.steer("shared-bearer", "chat-retry", SECRET, "m1", private=True)
        assert response.status_code == 503
        steering._reset()
    (turn,) = await ss.pending_turns(chat_mod._private_partition_store(), "chat-retry", OP)
    assert turn["dedupe"] == "m1" and turn["seq"] == 1 and turn["state"] == ss.STATE_QUEUED
    assert sum(SECRET in (row.private_payload or "") for row in await rig.rows()) == 1


@pytest.mark.parametrize("ending", ["error", "exception", "disconnect", "cancel", "exhausted", "retry"])
@pytest.mark.parametrize("recovered", [False, True])
async def test_abnormal_private_turn_releases_runner(rig, monkeypatch, ending, recovered):  # noqa: F811
    # #5504: a known-ended execution must not wedge every later private steer.
    from graph.fleet.delegation import DELEGATION_HEADER

    session = "chat-ended"
    carriage = {}
    start = chat_mod._start_steer_turn
    native = chat_mod._run_native_turn

    async def capture(*args, **kwargs):
        carriage.update(kwargs)
        return "captured"

    monkeypatch.setattr(chat_mod, "_start_steer_turn", capture)
    response = await rig.steer("shared-bearer", session, SECRET, "m1", private=True)
    assert response.status_code == 200
    if recovered:
        steering._reset()
        chat_mod._PRIVATE_RUNS.clear()  # a restart forgets the in-process order too
        out = await chat_mod.recover_private_pending_turns()
        await out["dispatch"]

    async def failing(*args, **kwargs):
        if kwargs.get("overflow_retry"):
            segment = await ss.read_segment(chat_mod._private_partition_store(), session, OP)
            assert segment["state"] == ss.STATE_RUNNING
            yield ("done", REPLY)
            return
        yield ("text", "partial")
        if ending == "retry":
            import httpx

            raise httpx.ReadError("provider stream dropped")
        if ending == "exception":
            raise ValueError("native failed")
        if ending == "cancel":
            raise asyncio.CancelledError()
        if ending == "error":
            yield ("error", "native failed")

    monkeypatch.setattr(chat_mod, "_run_native_turn", failing)
    stream = chat_mod._chat_langgraph_stream(
        carriage["private_ref"], session,
        request_metadata={"private_input": True, DELEGATION_HEADER: carriage["delegation"]},
    )
    try:
        assert await anext(stream) == ("text", "partial")
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await anext(stream)
        elif ending != "disconnect":
            frames = []
            async for frame in stream:
                frames.append(frame)
                if frame[0] == "error":
                    break  # the executor closes the stream at its terminal frame
            if ending == "retry":
                assert frames[-1] == ("done", REPLY)
    finally:
        await stream.aclose()
    store = chat_mod._private_partition_store()
    assert await ss.pending_turns(store, session, OP) == []
    assert (await ss.read_segment(store, session, OP))["state"] == ss.SEGMENT_IDLE
    assert not chat_mod._RECOVERED_PRIVATE_TURNS
    monkeypatch.setattr(chat_mod, "_start_steer_turn", start)
    monkeypatch.setattr(chat_mod, "_run_native_turn", native)
    follow = (await rig.steer("shared-bearer", session, SECRET, "m2", private=True)).json()
    assert (await poll_terminal(rig.op, follow["started_turn"]))["status"]["state"] == "TASK_STATE_COMPLETED"


async def _drained(session):
    store = chat_mod._private_partition_store()
    for _ in range(100):  # the task is terminal at ``done``; the stream's own exit follows it
        if not chat_mod._PRIVATE_RUNS:
            break
        await asyncio.sleep(0.02)
    assert await ss.pending_turns(store, session, OP) == []
    assert (await ss.read_segment(store, session, OP))["state"] == ss.SEGMENT_IDLE
    assert not chat_mod._PRIVATE_RUNS and not chat_mod._RECOVERED_PRIVATE_TURNS


async def test_a_failed_private_turn_settles_before_the_next_private_steer_claims(rig, monkeypatch):  # noqa: F811
    # The thread lock is released before the failed turn's segment is settled: the private
    # steer queued behind it must wait for that settlement, not be refused in the window.
    session = "chat-settle"
    native, finish = chat_mod._run_native_turn, chat_mod._finish_private_pending_turn

    async def failing(message, *args, **kwargs):
        if "hold" in message:
            rig.running.set()
            await rig.release.wait()
            raise ValueError("native failed")
        async for frame in native(message, *args, **kwargs):
            yield frame

    async def slow_finish(*args, **kwargs):
        await asyncio.sleep(0.2)  # the window after the lock is released
        return await finish(*args, **kwargs)

    monkeypatch.setattr(chat_mod, "_run_native_turn", failing)
    monkeypatch.setattr(chat_mod, "_finish_private_pending_turn", slow_finish)
    first = (await rig.steer("shared-bearer", session, f"{SECRET} hold", "m1", private=True)).json()
    await rig.running.wait()
    second = (await rig.steer("shared-bearer", session, f"{SECRET} next", "m2", private=True)).json()
    rig.release.set()
    assert (await poll_terminal(rig.op, first["started_turn"]))["status"]["state"] == "TASK_STATE_FAILED"
    assert (await poll_terminal(rig.op, second["started_turn"]))["status"]["state"] == "TASK_STATE_COMPLETED"
    assert rig.seen == [(f"{SECRET} next", False)]
    await _drained(session)


async def test_an_older_queued_private_turn_runs_before_a_later_steer(rig, monkeypatch):  # noqa: F811
    # A steer whose delivery was refused stays queued with no runner: the next private steer
    # starts it first, in acceptance order, instead of running ahead and stranding it.
    session, start = "chat-order", chat_mod._start_steer_turn

    async def refused(*args, **kwargs):
        return ""

    monkeypatch.setattr(chat_mod, "_start_steer_turn", refused)
    assert (await rig.steer("shared-bearer", session, f"{SECRET} one", "m1", private=True)).status_code == 503
    monkeypatch.setattr(chat_mod, "_start_steer_turn", start)
    body = (await rig.steer("shared-bearer", session, f"{SECRET} two", "m2", private=True)).json()
    assert (await poll_terminal(rig.op, body["started_turn"]))["status"]["state"] == "TASK_STATE_COMPLETED"
    assert rig.seen == [(f"{SECRET} one", False), (f"{SECRET} two", False)]
    await _drained(session)
    await _assert_private_only(rig, session, body["started_turn"])


async def test_a_private_steer_answered_before_the_runner_is_not_left_queued(rig, monkeypatch):  # noqa: F811
    async def handled(pre, session_id, request_metadata):
        pre.handled = True
        yield ("done", "handled")

    monkeypatch.setattr(chat_mod._chat_dispatch, "_pre_turn_dispatch", handled)
    body = (await rig.steer("shared-bearer", "chat-pre", SECRET, "m1", private=True)).json()
    assert (await poll_terminal(rig.op, body["started_turn"]))["status"]["state"] == "TASK_STATE_COMPLETED"
    assert rig.seen == []
    await _drained("chat-pre")
