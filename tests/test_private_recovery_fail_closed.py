"""A private turn runs only on the checkpoint it was bound to at acceptance (ADR 0131): the
restart recovery still runs a brand-new conversation's first private turn, and the live stream
refuses one whose thread no longer resolves to its binding. On the S9 proof rig (real route,
loopback, executor, stream runner, SQLite partition and checkpointer; the model is scripted)."""
import importlib

import pytest

from a2a_impl import segment_schedule as ss
from graph import steering, thread_ids
from runtime.state import STATE
from tests._a2a_app import poll_terminal
from tests.test_private_steer_route import SECRET
from tests.test_proof_missing_state import OP, _assert_never_public_and_never_peer_readable, _atlas, rig, world  # noqa: F401

chat_mod = importlib.import_module("server.chat")
pytestmark = [pytest.mark.platform_sensitive, pytest.mark.asyncio]


async def _accepted_but_never_started(world, monkeypatch, session):  # noqa: F811
    """A live private steer whose loopback is refused: durably accepted, still queued."""
    async def _refused(*_args, **_kwargs):
        return ""

    with monkeypatch.context() as patch:
        patch.setattr(chat_mod, "_start_steer_turn", _refused)
        refused = await world.steer("shared-bearer", session, SECRET, "m1", private=True)
    assert "started_turn" not in refused.text and world.runs == []
    (turn,) = await ss.pending_turns(chat_mod._private_partition_store(), session, OP)
    return turn["input_ref"], turn


async def test_first_private_turn_of_a_new_session_is_recovered_and_run(world, monkeypatch):  # noqa: F811
    session, thread = "chat-new", "a2a:chat-new"
    ref, turn = await _accepted_but_never_started(world, monkeypatch, session)
    assert (turn["checkpoint"], turn["checkpoint_existed"], turn["state"]) == (thread, False, ss.STATE_QUEUED)
    # The restart: every in-memory guard is gone; only the durable records remain.
    steering._reset()
    thread_ids.forget_resolver_metadata(session)
    chat_mod._RECOVERED_PRIVATE_TURNS.clear()
    chat_mod._PRIVATE_RUNS.clear()
    out = await chat_mod.recover_private_pending_turns()
    await out["dispatch"]
    task_id = next(row.id for row in await world.rows() if ref in str(row.history))
    assert (await poll_terminal(world.op, task_id))["status"]["state"] == "TASK_STATE_COMPLETED"
    assert world.runs == [{"text": SECRET, "resume": False, "thread": thread, "fresh": True}]
    assert await ss.pending_turns(chat_mod._private_partition_store(), session, OP) == []


async def test_live_stream_refuses_a_private_turn_off_its_bound_checkpoint(world, monkeypatch):  # noqa: F811
    session = "chat-moved"
    monkeypatch.setattr(STATE, "thread_id_resolver", _atlas, raising=False)
    chat_mod._resolve_turn_thread_id({"project": "atlas"}, session)  # the conversation's original turn
    ref, turn = await _accepted_but_never_started(world, monkeypatch, session)
    assert turn["checkpoint"] == f"proj:atlas:{session}"
    monkeypatch.setattr(STATE, "thread_id_resolver", None, raising=False)  # now resolves a2a:<session>
    retry = (await world.steer("shared-bearer", session, SECRET, "m1", private=True)).json()
    task = await poll_terminal(world.op, retry["started_turn"])
    assert task["status"]["state"] == "TASK_STATE_FAILED" and SECRET not in str(task)
    assert world.runs == []  # the graph never ran, on any thread
    store = chat_mod._private_partition_store()
    assert await ss.pending_turns(store, session, OP) == []  # closed, not left to be retried
    assert (await ss.read_segment(store, session, OP))["state"] == ss.SEGMENT_IDLE
    await _assert_never_public_and_never_peer_readable(world, session, ref)
