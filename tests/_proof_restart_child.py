"""Killed-process proof worker; importable by pytest and launched as a module."""
from __future__ import annotations

import asyncio
import json
import importlib
import os
import sys
from contextlib import closing
from pathlib import Path

from a2a.server.context import ServerCallContext
from langchain_core.tools import tool
from a2a.types import GetTaskRequest
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

from a2a_impl import private_intake as pi, segment_schedule as ss, stores
from graph import thread_ids
from graph.checkpointer import build_sqlite_checkpointer
from runtime.state import STATE
from tests.test_a2a_hitl_parked_routing import _handler, _msg

chat = importlib.import_module("server.chat")
SESSION = "restart-proof"
THREAD = "proj:atlas:restart-proof"
SECRET = "PRIVATE-RESTART-CANARY"
KINDS = {"federation": "federation", "fleet-service": "fleet-service",
         "paired-agent": "agent-device", "enrolled-console": "device"}


def context(caller):
    return ServerCallContext(state={"principal_kind": KINDS[caller],
                                   "trust_tier": "federation" if caller == "federation" else "operator",
                                   "requester_principal": f"device:{caller}"})


def append(path, value):
    with path.open("a", encoding="utf-8") as out:
        out.write(value + "\n")
        out.flush()
        os.fsync(out.fileno())


async def run(base, point, caller, recover):
    engine = stores.make_sqlite_engine(str(base / "tasks.db"))
    store = stores.ReasoningCoalescingTaskStore(engine, owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    human, requester = context("enrolled-console"), context(caller)
    STATE.thread_id_resolver = lambda md, sid: f"proj:{md.get('project', 'none')}:{sid}"
    cfg = {"configurable": {"thread_id": THREAD}}

    async def ready():
        (base / "ready").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        await asyncio.Event().wait()  # parent kills us with stores and execution still open

    @tool
    async def effect() -> str:
        """Record a durable external effect before returning."""
        append(base / "effects", "effect")
        if not recover and point == "claimed":
            await ready()
        return "recorded"

    async def node(state):
        if point == "parked":
            interrupt("approve continuation")
        await effect.ainvoke({})
        return {"messages": []}

    saver = build_sqlite_checkpointer(str(base / "checkpoints.db"))
    with closing(saver.conn):
        builder = StateGraph(MessagesState)
        builder.add_node("effect", node)
        builder.add_edge(START, "effect")
        builder.add_edge("effect", END)
        graph = builder.compile(checkpointer=saver)

        async def stream(text, ctx, **kwargs):
            yield "done", "public receipt"

        handler, router = _handler(stream, task_store=store)
        if not recover:
            # Original caller enters through the SDK/executor, independently of the human
            # who subsequently contributes private input to this conversation.
            chat._resolve_turn_thread_id({"project": "atlas"}, SESSION)
            original = await handler.on_message_send(_msg("public request", mid="original", ctx=SESSION), requester)
            await router.drain()
            await graph.aupdate_state(cfg, {"messages": []}, as_node="__start__")
            ref = await pi.record_private_steer(store, session_id=SESSION, text=SECRET,
                                               msg_id="private", context=human)
            await pi.record_private_reply(store, ref, "PRIVATE-REPLY-CANARY", human)
            for dedupe, audience, principal, input_ref in (
                ("private", "private", "device:enrolled-console", ref),
                ("peer-1", "peer", f"device:{caller}", ""),
                ("peer-2", "peer", f"device:{caller}", ""),
            ):
                await ss.accept_pending_turn(store, session_id=SESSION, audience=audience,
                    principal=principal, dedupe=dedupe, checkpoint=THREAD, input_ref=input_ref, context=human)
            (base / "ids.json").write_text(json.dumps({"ref": ref, "task": original.id}), encoding="utf-8")
            if point == "accepted":
                await ready()

            async def send(turn):
                append(base / "sends", turn["input_ref"])
                await graph.ainvoke(None, cfg)
                snapshot = await graph.aget_state(cfg)
                iid = snapshot.tasks[0].interrupts[0].id
                seg = await ss.read_segment(store, SESSION, human)
                assert await ss.park_segment(store, SESSION, generation=seg["generation"], interrupt_id=iid, context=human)
                await ss.set_turn_state(store, SESSION, turn["seq"], "parked", human, interrupt_id=iid)
                await ready()

            await ss.dispatch_pending_turn(store, SESSION, human, send)
        else:
            ids = json.loads((base / "ids.json").read_text(encoding="utf-8"))
            ref = ids["ref"]
            recovered = await ss.recover_undrained_segments(store, SESSION, human)
            repeated = await ss.recover_undrained_segments(store, SESSION, human)
            record = await store.get_record(pi.ref_task_id(ref), human)
            visible = await store.get_record(pi.ref_task_id(ref), requester)
            public = await store.get(pi.ref_task_id(ref), requester)
            snapshot = await graph.aget_state(cfg)
            sends = []

            async def unexpected_send(turn):
                sends.append(turn)
                await effect.ainvoke({})
                return "unexpected"

            if point != "accepted":
                await ss.dispatch_pending_turn(store, SESSION, human, unexpected_send)
            denied = False
            if caller != "enrolled-console":
                try:
                    await pi.record_private_steer(store, session_id=SESSION, text="unauthorized",
                                                   msg_id="attack", context=requester)
                except PermissionError:
                    denied = True
                try:
                    await ss.dispatch_pending_turn(store, SESSION, requester, unexpected_send)
                except PermissionError:
                    pass
            # Missing reference/delegation must remain unresolved, never public text.
            unresolved = await pi.resolve_private_input(store, ref, delegation=None, context=human)
            report = dict(pid=os.getpid(), recovery=recovered, repeated=repeated,
                peer_task=str(await handler.on_get_task(GetTaskRequest(id=ids["task"]), requester)),
                next=await ss.next_dispatch(store, SESSION, human), transcript=record.private_payload,
                visible=visible.private_payload if visible else None, public=public is not None,
                denied=denied, sends=sends, unresolved=unresolved,
                metadata=thread_ids.resolver_metadata(SESSION),
                resolved=chat._resolve_turn_thread_id({"private_input": True}, SESSION),
                checkpoint=snapshot.config["configurable"]["thread_id"],
                interrupts=[i.id for t in snapshot.tasks for i in t.interrupts])
            if point == "parked":
                seg = recovered["segment"]
                report["wrong_resume"] = await ss.resume_segment(store, SESSION,
                    generation=seg["generation"], interrupt_id="wrong", context=human)
                report["matching_resume"] = await ss.resume_segment(store, SESSION,
                    generation=seg["generation"], interrupt_id=seg["interrupt_id"], context=human)
            (base / "report.json").write_text(json.dumps(report), encoding="utf-8")
            await router.drain()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4] == "recover"))
