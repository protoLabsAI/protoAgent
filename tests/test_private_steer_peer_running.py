"""Running peer-turn privacy using #5400's Phase 0 projection rig.

The executor, SDK handler, SQLite partition and push serializer are real. As in
Phase 0, trusted scheduling explicitly installs RequesterProjection; the model
stream is scripted. This proves that seam, not default production enablement.
"""

import asyncio

import httpx
import pytest
from a2a.server.tasks import InMemoryPushNotificationConfigStore
from a2a.types import GetTaskRequest, SubscribeToTaskRequest, TaskPushNotificationConfig, TaskState

from a2a_impl import private_intake, stores
from a2a_impl.requester_delivery import RequesterProjection
from tests.test_a2a_hitl_parked_routing import (
    _drain,  # noqa: F401 — autouse cleanup for the shared handler rig
    _handler,
    _hooks,  # noqa: F401 — restore executor hooks and private readers
    _msg,
)
from tests.test_proof_caller_kinds_a2a import principals  # noqa: F401 — real credential authentication

pytestmark = [pytest.mark.asyncio, pytest.mark.platform_sensitive]

SECRET = "RUNNING-PEER-PRIVATE-STEER-CANARY"
PRIVATE_REPLY = f"Private answer containing {SECRET}"
RESULT = "The requested public result."
SESSION = "peer-running-private-steer"


async def _working(handler, task_id, peer, timeout=5.0):
    """The peer's GetTask once the task is stored as working, or the last read at the deadline."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        task = await handler.on_get_task(GetTaskRequest(id=task_id), peer)
        if task.status.state == TaskState.TASK_STATE_WORKING or asyncio.get_running_loop().time() >= deadline:
            return task
        await asyncio.sleep(0.02)


@pytest.mark.parametrize("kind", ["fleet-service", "paired-agent"])
async def test_running_peer_private_steer_retained_handles(tmp_path, principals, kind):  # noqa: F811
    # Phase 0 supplies the enrolled recipient separately from credential classification.
    peer = principals.peer(kind, requester_principal=f"device:{kind}-requester")
    operator = principals.human_operator()
    assert peer.state["principal_kind"] == {
        "fleet-service": "fleet-service", "paired-agent": "agent-device",
    }[kind]
    path = str(tmp_path / "peer-running.db")
    store = stores.ReasoningCoalescingTaskStore(
        stores.make_sqlite_engine(path), owner_resolver=stores.resolve_task_owner,
    )
    await store.initialize()
    started, steered, private_output, finish = (asyncio.Event() for _ in range(4))
    projections, frames, subscriptions, pushes = [], [], [], []
    config = TaskPushNotificationConfig(id="peer-hook", url="https://8.8.8.8/hook")
    push_store = InMemoryPushNotificationConfigStore()
    ref = ""

    async def accept(context, queue):
        config.task_id = context.task_id
        await push_store.set_info(context.task_id, config, peer)
        projection = await RequesterProjection.accept(
            store, context, queue, agent_id="agent:owner", operator=operator, push_configs=[config],
        )
        projections.append(projection)
        return projection

    async def model(*args, **kwargs):
        started.set()
        await steered.wait()
        projection = projections[0]
        await projection.begin_private(ref)
        yield "steer_consumed", {"items": [{"text": SECRET}]}
        yield "text", PRIVATE_REPLY
        yield "tool_end", {"name": "private-tool", "output": SECRET}
        private_output.set()
        await finish.wait()
        delivery = await projection.prepare_result(RESULT)
        yield "deliver_to_requester", delivery
        yield "done", PRIVATE_REPLY

    def push(request):
        pushes.append(request.content.decode())
        return httpx.Response(200)

    async def collect(iterator, target):
        async for event in iterator:
            snapshot = type(event)()
            snapshot.CopyFrom(event)
            target.append(snapshot)

    def clean(handle, value):
        # Keep failures attributable to the leaking peer handle (#5400).
        assert SECRET not in str(value), f"{kind}: private steer leaked via {handle}"
        assert PRIVATE_REPLY not in str(value), f"{kind}: private reply leaked via {handle}"

    client = httpx.AsyncClient(transport=httpx.MockTransport(push))
    sender = stores.ValidatingPushNotificationSender(client, push_store, delivery_store=store)
    handler, router = _handler(
        model, task_store=store, push_config_store=push_store, push_sender=sender,
        requester_projection_factory=accept,
    )
    workers = []
    try:
        workers.append(asyncio.create_task(collect(handler.on_message_send_stream(
            _msg("Please produce a result", mid="peer-request", ctx=SESSION), peer,
        ), frames)))
        await asyncio.wait_for(started.wait(), 5)
        task_id = projections[0].binding["task_id"]
        subscribed = handler.on_subscribe_to_task(SubscribeToTaskRequest(id=task_id), peer)
        initial = await asyncio.wait_for(anext(subscribed), 5)
        snapshot = type(initial)()
        snapshot.CopyFrom(initial)
        subscriptions.append(snapshot)
        workers.append(asyncio.create_task(collect(subscribed, subscriptions)))
        # The model signals `started` before the executor has stored the task as working, so a
        # GetTask taken at once can still read the unset state (seen on loaded CI runners, on
        # Linux and Windows). Wait for the stored state instead of sampling it once.
        before = await _working(handler, task_id, peer)
        assert before.status.state == TaskState.TASK_STATE_WORKING
        assert not workers[0].done() and not workers[1].done()

        # The operator steers only AFTER the peer has all of its retained handles.
        ref = await private_intake.record_private_steer(
            store, session_id=SESSION, text=SECRET, msg_id="operator-steer", context=operator,
        )
        steered.set()
        await asyncio.wait_for(private_output.wait(), 5)
        during = await handler.on_get_task(GetTaskRequest(id=task_id), peer)
        assert during.status.state == TaskState.TASK_STATE_WORKING
        clean("GetTask while private output is running", during)
        finish.set()
        await asyncio.wait_for(asyncio.gather(*workers), 10)
        await router.drain()
        got = await handler.on_get_task(GetTaskRequest(id=task_id), peer)
        assert got.status.state == TaskState.TASK_STATE_COMPLETED
        reply = "".join(part.text for artifact in got.artifacts for part in artifact.parts)
        assert reply == RESULT
        # Positive controls: each transport really carried the released reply.
        for handle, value in (("live stream", frames), ("existing subscription", subscriptions),
                              ("GetTask after completion", got), ("push callbacks", pushes),
                              ("peer reply text", reply)):
            assert RESULT in str(value), f"{handle} never delivered the result"
            clean(handle, value)
        private = await store.get_record(private_intake.ref_task_id(ref), operator)
        assert SECRET in str(private.private_payload)
        assert PRIVATE_REPLY in str(private.private_payload)
    finally:
        steered.set()
        finish.set()
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await handler._active_task_registry.aclose()
        await router.drain()
        await client.aclose()
        await store.engine.dispose()

    reopened = stores.ReasoningCoalescingTaskStore(
        stores.make_sqlite_engine(path), owner_resolver=stores.resolve_task_owner,
    )
    try:
        await reopened.initialize()
        history = await reopened.history_records(SESSION, peer)
        assert RESULT in str(history)
        clean("reopened SQLite history", history)
        clean("reopened task", await reopened.get(task_id, peer))
        private = await reopened.get_record(private_intake.ref_task_id(ref), operator)
        assert SECRET in str(private.private_payload) and PRIVATE_REPLY in str(private.private_payload)
    finally:
        await reopened.engine.dispose()
