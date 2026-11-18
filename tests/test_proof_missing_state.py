"""P1 proof S9, "Missing/stale state": a private turn whose LangGraph checkpoint is gone, or
whose thread-id resolver is no longer supported, must fail closed (ADR 0131 proof matrix).

Real: the P1h.3 rig (auth middleware for every caller kind, steer route, loopback ``SendMessage``,
SDK handler, executor, stream runner, SQLite partition), a compiled LangGraph over the SQLite
checkpointer, and, for the restart cases, a second interpreter that accepts the private turn
and is SIGKILLed (this file is that worker when run as a module). Only the model turn is scripted:
it records the thread each run landed on and whether that thread still had a checkpoint.
"""
import asyncio
import importlib
import os
import signal
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
from a2a.server.context import ServerCallContext
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

from a2a_impl import private_intake as pi, segment_schedule as ss, stores
from graph import steering
from graph.checkpointer import build_sqlite_checkpointer
from runtime.state import STATE
from tests import test_private_steer_route as private_routes
from tests._a2a_app import poll_terminal
from tests.test_private_steer_route import REPLY, SECRET

rig = private_routes.rig
chat_mod = importlib.import_module("server.chat")
pytestmark = [pytest.mark.platform_sensitive, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[1]
OP = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})
CALLERS = ("federation", "fleet-service", "paired-agent", "enrolled-console")
# A device-kind pairing IS the human operator (proposal, "Identity lifecycle"): it may recover the
# REPLY of its own private turn, and a private steer from it is an action, not a read. The private
# INPUT canary is asserted absent for all four; the other three are peers on every surface.
HUMAN = "enrolled-console"

class FailedOpen(AssertionError):
    """The private turn ran where it must not: the one assertion a strict xfail may absorb."""

def _proof_failure(what):
    return pytest.mark.xfail(strict=True, raises=FailedOpen, reason=f"PROOF FAILURE: {what}")

def _atlas(metadata, session_id):
    return f"proj:{metadata.get('project', 'none')}:{session_id}"

def _raising(metadata, session_id):
    raise RuntimeError("resolver plugin failed to load")


def _graph(path):
    """A compiled graph over the real SQLite checkpointer; a "park" message pauses on a HITL interrupt."""
    async def ask(state):
        if state["messages"][-1].content == "park":
            interrupt({"question": "Which one?"})
        return {"messages": []}

    saver = build_sqlite_checkpointer(str(path))
    builder = StateGraph(MessagesState)
    builder.add_node("ask", ask)
    builder.add_edge(START, "ask")
    builder.add_edge("ask", END)
    return builder.compile(checkpointer=saver), saver


async def _pending(graph, thread):
    """(has a checkpoint, pending interrupt count) for ``thread``."""
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread}})
    return bool(snapshot.created_at), sum(len(task.interrupts) for task in snapshot.tasks)


@pytest.fixture
async def world(rig, monkeypatch, tmp_path):
    """The rig over a real checkpointer; ``rig.runs`` is every scripted model turn, in order."""
    graph, saver = _graph(tmp_path / "checkpoints.db")
    rig.graph, rig.saver, rig.runs, rig.base = graph, saver, [], tmp_path

    async def _native(message, session_id, config, *, resume=False, **_kwargs):
        thread = config["configurable"]["thread_id"]
        had_checkpoint, _ = await _pending(graph, thread)
        rig.runs.append({"text": message, "resume": resume, "thread": thread, "fresh": not had_checkpoint})
        if message.endswith("park") and not resume:
            await graph.ainvoke({"messages": [HumanMessage("park")]}, config)
            yield ("input_required", {"question": "Which one?"})
            return
        yield ("done", REPLY)

    monkeypatch.setattr(STATE, "graph", graph)
    monkeypatch.setattr(chat_mod, "_run_native_turn", _native)
    with closing(saver.conn):
        yield rig


async def _settle(world, ref):
    """Wait out every turn that carried the opaque reference, so the assertions read a quiet store."""
    for row in await world.rows():
        if ref in str(row.history):
            await poll_terminal(world.op, row.id)


async def _assert_never_public_and_never_peer_readable(world, session, ref):
    """What holds whether or not the turn ran: no public fallback, the record intact, no peer read."""
    rows = await world.rows()
    assert all(SECRET not in str(tuple(row)[:6]) and REPLY not in str(tuple(row)[:6]) for row in rows)
    assert steering.pending_items(session) == []  # never queued as ordinary, foldable text
    record = await chat_mod._private_partition_store().get_record(pi.ref_task_id(ref), OP)
    assert record.private_payload["input"].startswith(SECRET) and record.private_payload["version"]
    for caller in CALLERS:
        reads = {"ListTasks": str(await world.rpc(caller, "ListTasks", {"contextId": session, "includeArtifacts": True})),
                 "GetTask": "".join([str(await world.rpc(caller, "GetTask", {"id": row.id})) for row in rows]),
                 "turns": (await world.clients[caller].get(f"/api/chat/sessions/{session}/turns")).text,
                 "steer": (await world.clients[caller].get(f"/api/chat/sessions/{session}/steer")).text}
        if caller != HUMAN:
            reads["steer-post"] = (await world.steer(caller, session, "retry", "m1", private=True)).text
        for surface, body in reads.items():
            assert SECRET not in body and (caller == HUMAN or REPLY not in body), (caller, surface)


# ── case 1: a parked private segment whose checkpoint is deleted ─────────────────

# Fails closed since #5504: the live route's durable segment stays parked on the ORIGINAL interrupt,
# and a resume that no longer carries that interrupt is refused at the claim, never run fresh.
async def test_parked_private_segment_with_a_deleted_checkpoint_fails_closed(world):
    session, thread = "chat-gone", "a2a:chat-gone"
    first = (await world.steer("shared-bearer", session, f"{SECRET} park", "m1", private=True)).json()
    assert (await poll_terminal(world.op, first["started_turn"]))["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert [row.owner for row in await world.rows() if row.id == first["started_turn"]] == ["human-operator"]
    assert await _pending(world.graph, thread) == (True, 1)
    await world.saver.adelete_thread(thread)
    assert await _pending(world.graph, thread) == (False, 0)
    answer = await world.steer("shared-bearer", session, f"{SECRET} answer", "m2", private=True)
    assert SECRET not in answer.text
    await _settle(world, answer.json().get("private_ref") or first["private_ref"])
    await _assert_never_public_and_never_peer_readable(world, session, first["private_ref"])
    await world.rpc("shared-bearer", "CancelTask", {"id": first["started_turn"]})  # release the parked stream
    if len(world.runs) > 1:
        raise FailedOpen(f"private input ran on an empty thread: {world.runs[1:]}")


# ── cases 2 and 3: accepted in a process that is then killed ─────────────────────

async def _accept_then_wait_for_kill(base, session, custom, parked):
    """The killed half: accept one private turn bound to its checkpoint, then never return."""
    engine = stores.make_sqlite_engine(str(base / "private.db"))
    store = stores.ReasoningCoalescingTaskStore(engine, owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    STATE.thread_id_resolver = _atlas if custom else None
    thread = chat_mod._resolve_turn_thread_id({"project": "atlas"}, session)
    config = {"configurable": {"thread_id": thread}}
    graph, _saver = _graph(base / "checkpoints.db")
    await graph.ainvoke({"messages": [HumanMessage("an earlier turn")]}, config)
    ref = await pi.record_private_steer(store, session_id=session, text=SECRET, msg_id="m1", context=OP)
    await ss.accept_pending_turn(store, session_id=session, audience=ss.AUDIENCE_PRIVATE, principal="op",
                                 dedupe="m1", checkpoint=thread, input_ref=ref, context=OP)
    if parked:
        async def send(_turn):
            await graph.ainvoke({"messages": [HumanMessage("park")]}, config)
            return "task-before-the-kill"

        await ss.dispatch_pending_turn(store, session, OP, send)
        ((pause,),) = [task.interrupts for task in (await graph.aget_state(config)).tasks]
        segment = await ss.read_segment(store, session, OP)
        assert await ss.park_segment(store, session, generation=segment["generation"], interrupt_id=pause.id, context=OP)
        await ss.set_turn_state(store, session, 1, ss.STATE_PARKED, OP, interrupt_id=pause.id)
    (base / "ready").write_text(f"{thread}\n{ref}", encoding="utf-8")
    await asyncio.Event().wait()  # the parent kills us with the stores still open


def _kill_after_accept(base, session, custom, parked):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT))),
           "PROTOAGENT_HOME": str(base / "child-home"), "PROTOAGENT_BOX_ROOT": str(base / "child-box")}
    cmd = [sys.executable, "-m", "tests.test_proof_missing_state", str(base), session, str(int(custom)), str(int(parked))]
    with (base / "child.log").open("w", encoding="utf-8") as log:
        child = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 45
            while not (base / "ready").exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert (base / "ready").exists() and child.poll() is None, (base / "child.log").read_text()
            child.send_signal(signal.SIGKILL)
            assert child.wait(timeout=10) == -signal.SIGKILL
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    return (base / "ready").read_text(encoding="utf-8").split("\n")


RESTARTS = {  # id: (resolver at acceptance, resolver after restart, checkpoint deleted, parked resume)
    "control-same-resolver": (True, _atlas, False, False),
    "missing-checkpoint": (False, None, True, False),
    "unregistered-resolver-recovery": (True, None, False, False),
    "raising-resolver-recovery": (True, _raising, False, False),
    "unregistered-resolver-resume": (True, None, False, True),
    "raising-resolver-resume": (True, _raising, False, True),
}
FAILURES = {  # the two parked-resume cases fail closed since #5504 (the claim refuses a non-matching resume)
    "missing-checkpoint": "restart recovery dispatches a private pending turn whose bound checkpoint is gone onto a fresh thread",
    "unregistered-resolver-recovery": "with the resolver unregistered, restart recovery runs the private turn on the default a2a:<session> thread",
    "raising-resolver-recovery": "with a raising resolver, restart recovery runs the private turn on the default a2a:<session> thread",
}


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX SIGKILL semantics")
@pytest.mark.parametrize("case", [pytest.param(case, id=case, marks=[_proof_failure(FAILURES[case])] if case in FAILURES else [])
                                  for case in RESTARTS])
async def test_private_turn_after_a_real_kill_fails_closed(world, monkeypatch, case):
    custom, resolver, delete_checkpoint, parked = RESTARTS[case]
    session = "chat-killed"
    thread, ref = await asyncio.to_thread(_kill_after_accept, world.base, session, custom, parked)
    assert thread == (f"proj:atlas:{session}" if custom else f"a2a:{session}")
    assert await _pending(world.graph, thread) == (True, int(parked))
    if delete_checkpoint:
        await world.saver.adelete_thread(thread)
        assert await _pending(world.graph, thread) == (False, 0)
    # The restart: this process never saw the turn, and the resolver it was accepted under is gone.
    monkeypatch.setattr(STATE, "thread_id_resolver", resolver, raising=False)
    out = await chat_mod.recover_private_pending_turns()
    view = out["recovered"][session]
    assert [(t["dedupe"], t["input_ref"], t["checkpoint"]) for t in view["parked" if parked else "queued"]] == [("m1", ref, thread)]
    if parked:  # a parked segment stays reserved by recovery; only the operator's resume may continue it
        assert out["dispatch"] is None and view["reserved"] and world.runs == []
        answer = await world.steer("shared-bearer", session, f"{SECRET} answer", "m2", private=True)
        assert SECRET not in answer.text
        await _settle(world, answer.json().get("private_ref") or ref)
    elif out["dispatch"]:
        await out["dispatch"]
        await _settle(world, ref)
    await _assert_never_public_and_never_peer_readable(world, session, ref)
    strayed = [run for run in world.runs if run["thread"] != thread or run["fresh"]]
    if strayed:
        raise FailedOpen(f"the private turn left its bound checkpoint {thread!r}: {strayed}")
    if case.startswith("control"):  # the harness can pass: same resolver, same thread, the private text
        assert world.runs == [{"text": SECRET, "resume": False, "thread": thread, "fresh": False}]


if __name__ == "__main__":
    asyncio.run(_accept_then_wait_for_kill(Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "1", sys.argv[4] == "1"))
