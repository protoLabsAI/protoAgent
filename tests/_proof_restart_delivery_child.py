"""P1d process worker: durable stores, real HTTP push, and parent-controlled kill barriers."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
from a2a.types import Message, Part, Role, TaskPushNotificationConfig
from a2a_impl.requester_delivery import RequesterProjection
from tests._proof_restart_child import SESSION, SECRET, context, identities, pi, stores

RESULT = "The sealed restart result."
TASK = "delivery-restart-task"


async def run(base, point, caller, recover, sink):
    # R1 exposes the store module/factory rather than a standalone open-store helper.
    engine = stores.make_sqlite_engine(str(base / "tasks.db"))
    store = stores.ReasoningCoalescingTaskStore(engine, owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    push_store = stores.ValidatingPushNotificationConfigStore(engine)
    await push_store.initialize()
    tokens = identities()
    requester, operator = context(caller, tokens), context("enrolled-console", tokens)

    async def ready():
        _, record = await store.delivery_data(TASK)
        (base / "ready.tmp").write_text(json.dumps({"pid": os.getpid(), "record": record}))
        (base / "ready.tmp").replace(base / "ready")
        await asyncio.Event().wait()

    async with httpx.AsyncClient(trust_env=False) as client:
        sender = stores.ValidatingPushNotificationSender(client, push_store, delivery_store=store)

        class PushQueue:
            # Exercise the production projection and sender synchronously at their seam;
            # the proof does not claim executor/router startup recovery is wired in production.
            async def enqueue_event(self, event):
                await sender.send_notification(TASK, event)
                if not recover and point == "sent":
                    await ready()  # HTTP sink fsynced the body; delivery CAS acknowledgment has not run.

        queue = PushQueue()
        if not recover:
            config = TaskPushNotificationConfig(id="original", task_id=TASK, url=sink + "/original")
            ctx = SimpleNamespace(task_id=TASK, context_id=SESSION, current_task=None, call_context=requester,
                message=Message(message_id="original", context_id=SESSION, role=Role.ROLE_USER,
                                parts=[Part(text="public request")]))
            projection = await RequesterProjection.accept(store, ctx, queue, agent_id="owner",
                                                          operator=operator, push_configs=[config])
            await push_store.set_info(TASK, config, requester)
            await push_store.set_info(TASK, TaskPushNotificationConfig(
                id="other", task_id=TASK, url=sink + "/other"), requester)
            ref = await pi.record_private_steer(store, session_id=SESSION, text=SECRET,
                                                msg_id="private", context=operator)
            await pi.record_private_reply(store, ref, "PRIVATE-REPLY-CANARY", operator)
            await projection.begin_private(ref)
            await projection.retain("tool", "PRIVATE-TOOL-CANARY")
            delivery_id = await projection.prepare_result(RESULT)
            if point == "committed":
                await ready()  # prepare_result's transaction committed, no publication yet.
            await projection.deliver_to_requester(delivery_id)
        else:
            binding, record = await store.delivery_data(TASK)
            projection = RequesterProjection(store, None, queue, binding, operator)
            await projection.begin_private(pi.private_ref(record["private_turn_id"]))
            outcomes = [await projection.deliver_to_requester(record["id"]) for _ in range(2)]
            final_binding, final_record = await store.delivery_data(TASK)
            (base / "report.json").write_text(json.dumps({"pid": os.getpid(), "before": record,
                "record": final_record, "binding": final_binding, "original_binding": binding,
                "outcomes": outcomes, "kind": requester.state["principal_kind"]}))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4] == "recover", sys.argv[5]))
