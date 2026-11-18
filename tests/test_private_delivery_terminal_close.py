"""Terminal close of private deliveries leaves ordinary tasks and live sends alone."""
import asyncio

import httpx
import pytest
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryPushNotificationConfigStore
from a2a.types import (
    AgentSkill, GetTaskRequest, Message, Part, Role, SendMessageRequest,
    TaskPushNotificationConfig, TaskState,
)
from google.protobuf.json_format import MessageToDict

import protolabs_a2a as pa
from a2a_impl import requester_delivery, stores
from a2a_impl.executor import ProtoAgentExecutor
from a2a_impl.requester_delivery import RequesterProjection

pytestmark = pytest.mark.asyncio
ANSWER = "ORDINARY-PUBLIC-ANSWER"


async def test_ordinary_task_completion_and_push_are_unchanged(tmp_path):
    store = stores.ReasoningCoalescingTaskStore(stores.make_sqlite_engine(str(tmp_path / "t.db")),
                                              owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    peer = ServerCallContext(state={"requester_principal": "peer"})
    op = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})
    pushes, frames, projections = [], [], []
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: pushes.append(request.content.decode()) or httpx.Response(200)))
    push_store = InMemoryPushNotificationConfigStore()
    config = TaskPushNotificationConfig(id="original", url="https://8.8.8.8/hook")

    async def factory(context, queue):
        config.task_id = context.task_id
        await push_store.set_info(context.task_id, config, peer)
        projections.append(await RequesterProjection.accept(
            store, context, queue, agent_id="owner", operator=op, push_configs=[config]))
        return projections[0]

    async def stream(text, ctx, **kwargs):
        yield "done", ANSWER

    card = pa.build_agent_card(name="t", description="d", url="http://t/a2a", version="0.0.0",
                               skills=[AgentSkill(id="chat", name="chat", description="d", tags=["chat"])], bearer=False)
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(stream, requester_projection_factory=factory),
        task_store=store, agent_card=card, push_config_store=push_store,
        push_sender=stores.ValidatingPushNotificationSender(client, push_store, delivery_store=store))
    request = SendMessageRequest(message=Message(message_id="m", context_id="ordinary",
                                                role=Role.ROLE_USER, parts=[Part(text="hi")]))
    try:
        async for event in handler.on_message_send_stream(request, peer):
            frames.append(MessageToDict(event))
        cleanup = set(getattr(handler._active_task_registry, "_cleanup_tasks", ()) or ())
        if cleanup:
            await asyncio.wait_for(asyncio.gather(*cleanup), 5)
        projection = projections[0]
        task_id = projection.binding["task_id"]
        got = await handler.on_get_task(GetTaskRequest(id=task_id), peer)
        assert got.status.state == TaskState.TASK_STATE_COMPLETED
        assert [p.text for a in got.artifacts for p in a.parts if p.text] == [ANSWER]
        assert sum(ANSWER in str(frame) for frame in frames) == 1
        assert sum(ANSWER in body for body in pushes) == 1
        # No private segment: there is no delivery record, and close/recover never make one.
        await projection.close()
        assert await projection.recover() is False
        assert (await store.delivery_data(task_id))[1] is None
    finally:
        await client.aclose()
        await store.engine.dispose()


class _Store:
    def __init__(self, record):
        self.record = record

    async def delivery_data(self, task_id):
        return {"task_id": "t"}, self.record

    async def cas_delivery(self, task_id, binding, previous, record):
        self.record = record
        return True


@pytest.mark.parametrize("state", ["private", "prepared", "delivered", "closed"])
async def test_recover_leaves_other_states_alone(state):
    store = _Store({"version": 1, "id": "d", "private_turn_id": "turn", "state": state, "payload": "p"})
    projection = RequesterProjection(store, None, None, {"task_id": "t"}, None)
    projection.private_ref = requester_delivery.private_ref("turn")
    assert await projection.recover() is False and store.record["state"] == state


async def test_recover_closes_a_stale_claim_but_not_a_live_one(monkeypatch):
    record = {"version": 1, "id": "d", "private_turn_id": "turn", "state": "dispatching",
              "payload": "sealed", "digest": "x"}
    store = _Store(record)
    projection = RequesterProjection(store, None, None, {"task_id": "t"}, None)
    projection.private_ref = requester_delivery.private_ref("turn")
    monkeypatch.setattr(requester_delivery, "_IN_FLIGHT", {("t", "d")})
    assert await projection.recover() is False and store.record == record
    monkeypatch.setattr(requester_delivery, "_IN_FLIGHT", set())
    assert await projection.recover() is True
    assert store.record == {**record, "state": "closed"}
    assert await projection.recover() is False
