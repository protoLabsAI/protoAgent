"""Server-start recovery of private pending turns (ADR 0131 P1h.4), on the P1h.3 rig: the real
auth middleware, loopback ``SendMessage``, SDK handler, executor, stream runner and SQLite
partition. "Restart" is the durable records alone — every in-memory guard is reset."""

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
