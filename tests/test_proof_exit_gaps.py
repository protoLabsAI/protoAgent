"""G1 proof at the dormant projection seam, with real SDK/store/serializer paths.

Model output and an outbound HTTP service are deterministic fakes. This does not
claim production intake wiring, browser rendering, or process-restart acceptance.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from a2a.server.context import ServerCallContext
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.utils.errors import InvalidParamsError
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryPushNotificationConfigStore
from a2a.types import (
    AgentSkill, CancelTaskRequest, GetTaskRequest, Message, Part, Role,
    SendMessageRequest, SubscribeToTaskRequest, TaskPushNotificationConfig, TaskState,
)
from google.protobuf.json_format import MessageToDict
from langchain_core.tools import tool
from starlette.requests import Request
from security import devices

import protolabs_a2a as pa
from a2a_impl import auth, hitl_routing, private_intake, stores
from a2a_impl.executor import ProtoAgentExecutor, set_progress_hook, set_terminal_hook
from a2a_impl.requester_delivery import RequesterProjection
from tests._virtual_clock import VirtualClock

CTX = "proof-g1"
SECRET, REPLY, OUTPUT = "PRIVATE-INPUT-G1", "PRIVATE-REPLY-G1", "PRIVATE-TOOL-OUTPUT-G1"
RESULT = "RESULT-VALUE-G1"
SELECTED = f"Selected value: {RESULT}"
KINDS = ["federation", "fleet-service", "paired-agent", "enrolled-console"]


class MissingPrivateProgress(AssertionError):
    pass


class OpenDelivery(AssertionError):
    pass


class UnselectedEffect(AssertionError):
    pass


def _snapshot(event):
    copy = type(event)()
    copy.CopyFrom(event)
    return MessageToDict(copy)


async def _scenario(tmp_path, monkeypatch, kind, exit_kind):
    """Retain stream/subscription/poll/push handles BEFORE private work starts."""
    from infra import media

    monkeypatch.setattr(media, "_PRIVATE_READERS", {})
    clock = VirtualClock().install(monkeypatch)
    path = str(tmp_path / "proof.db")
    store = stores.ReasoningCoalescingTaskStore(stores.make_sqlite_engine(path),
                                              owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    # Classify real credentials: owner tier alone is not requester identity.
    monkeypatch.setattr(auth, "_BEARER", ["owner-token"])
    monkeypatch.setattr(auth, "_FEDERATION", ["federation-token"])
    monkeypatch.setattr(auth, "_FLEET", ["fleet-token"])
    monkeypatch.setattr(devices, "_registry_path", lambda: tmp_path / "devices.json")
    token = {"federation": "federation-token", "fleet-service": "fleet-token"}.get(kind)
    if token is None:
        code, _ = devices.start_pairing(kind="agent" if kind == "paired-agent" else "device")
        _, token = devices.claim_pairing(code, "proof requester")
    credential = auth._classify_token(token)
    expected = {"paired-agent": "agent-device", "enrolled-console": "device"}.get(kind, kind)
    assert credential.principal_kind == expected
    request = Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())],
                       "state": {"principal_kind": credential.principal_kind, "trust_tier": credential.tier}})
    peer = stores.tier_context_builder().build(request)
    peer.state["requester_principal"] = credential.device_id or kind
    op = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})
    ref = await private_intake.record_private_steer(
        store, session_id=CTX, text=f"{SECRET}; candidate {RESULT}", msg_id="steer", context=op)
    ready, proceed, private_ready, finish, stopped = (asyncio.Event() for _ in range(5))
    projections, progress, pushes, requests, calls = [], [], [], [], []
    frames, subscriptions = [], []
    push_store = InMemoryPushNotificationConfigStore()
    config = TaskPushNotificationConfig(id="original", url="https://8.8.8.8/hook")

    def sink(request):
        target = pushes if request.url.path == "/hook" else requests
        target.append(request.content.decode())
        return httpx.Response(200, json={"result": OUTPUT})

    client = httpx.AsyncClient(transport=httpx.MockTransport(sink))

    @tool
    async def outbound(value: str) -> str:
        """Send a value to the external service."""
        response = await client.post("https://8.8.4.4/effect", json={"value": value})
        return response.json()["result"]

    async def factory(context, queue):
        config.task_id = context.task_id
        await push_store.set_info(context.task_id, config, peer)
        projection = await RequesterProjection.accept(
            store, context, queue, agent_id="owner", operator=op, push_configs=[config])
        projections.append(projection)
        return projection

    async def stream(text, ctx, **kwargs):
        calls.append(text)
        metadata = kwargs.get("request_metadata") or {}
        assert ("scheduler_job_id" in metadata) is (kind != "federation")
        try:
            ready.set()
            await proceed.wait()
            projection = projections[0]
            await projection.begin_private(ref)
            yield "steer_consumed", {"items": [{"text": SECRET}]}
            yield "text", REPLY
            value = RESULT if exit_kind in {"outbound_selected", "outbound_selected_stop"} else SECRET
            yield "tool_start", {"id": "call", "name": "outbound", "input": value}
            output = await outbound.ainvoke({"value": value}) if exit_kind.startswith("outbound") else OUTPUT
            yield "tool_end", {"id": "call", "name": "outbound", "output": output, "duration_ms": 1}
            # An exhausted stream with no selected result must not leave a live delivery.
            if exit_kind != "implicit":
                assert await projection.prepare_result(SELECTED)
            private_ready.set()
            await finish.wait()
            if exit_kind == "stall":
                await clock.sleep(31)
                await asyncio.Event().wait()
            elif exit_kind == "implicit":
                return
            else:
                yield "done", REPLY
        finally:
            stopped.set()

    sender = stores.ValidatingPushNotificationSender(client, push_store, delivery_store=store)
    card = pa.build_agent_card(name="t", description="d", url="http://t/a2a", version="0.0.0",
                               skills=[AgentSkill(id="chat", name="chat", description="d", tags=["chat"])],
                               bearer=False)
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(stream, requester_projection_factory=factory,
                                          stall_timeout_provider=lambda: 30,
                                          server_fired_origin=lambda origin: origin == "scheduler"),
        task_store=store, agent_card=card, push_config_store=push_store, push_sender=sender)
    router = hitl_routing.install_parked_task_routing(handler)
    set_progress_hook(lambda *args: progress.append(args))
    set_terminal_hook(None)

    async def collect(iterator, target):
        async for event in iterator:
            target.append(_snapshot(event))

    request = SendMessageRequest(message=Message(message_id="request", context_id=CTX,
                                                role=Role.ROLE_USER, parts=[Part(text="request")],
                                                metadata={"origin": "scheduler", "scheduler_job_id": "forged"}))
    tasks = [asyncio.create_task(collect(handler.on_message_send_stream(request, peer), frames))]
    try:
        if kind == "federation":
            reserved_request = SendMessageRequest()
            reserved_request.CopyFrom(request)
            reserved_request.message.context_id = "background:reserved"
            reserved = RequestContext(call_context=peer, request=reserved_request, task_id="reserved",
                                      context_id="background:reserved")
            with pytest.raises(InvalidParamsError, match="reserved"):
                await ProtoAgentExecutor(stream).execute(reserved, EventQueue())
        await asyncio.wait_for(ready.wait(), 5)
        projection = projections[0]
        task_id = projection.binding["task_id"]
        subscribed = handler.on_subscribe_to_task(SubscribeToTaskRequest(id=task_id), peer)
        subscriptions.append(_snapshot(await asyncio.wait_for(anext(subscribed), 5)))
        tasks.append(asyncio.create_task(collect(subscribed, subscriptions)))
        assert (await handler.on_get_task(GetTaskRequest(id=task_id), peer)).context_id == CTX
        proceed.set()
        await asyncio.wait_for(private_ready.wait(), 5)
        during = _snapshot(await handler.on_get_task(GetTaskRequest(id=task_id), peer))
        assert RESULT not in str(during)
        private_progress = list(progress)
        started = next(frame for _, _, frame in progress if frame.get("phase") == "turn_started")
        assert started["origin"] == ("" if kind == "federation" else "scheduler")
        assert projection.binding["principal"] == (credential.device_id or kind)
        if exit_kind.endswith("_stop"):
            cancelled = await handler.on_cancel_task(CancelTaskRequest(id=task_id), peer)
            assert cancelled.status.state == TaskState.TASK_STATE_CANCELED
            # Releasing the blocked producer after Stop must not restart cancelled input.
            finish.set()
        else:
            finish.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        await asyncio.wait_for(stopped.wait(), 5)
        await router.drain()
        cleanup = set(getattr(handler._active_task_registry, "_cleanup_tasks", ()) or ())
        if cleanup:
            await asyncio.wait_for(asyncio.gather(*cleanup), 5)
        got = await handler.on_get_task(GetTaskRequest(id=task_id), peer)
        terminal = {"stall": TaskState.TASK_STATE_FAILED, "outbound_stop": TaskState.TASK_STATE_CANCELED,
                    "outbound_selected_stop": TaskState.TASK_STATE_CANCELED}
        assert got.status.state == terminal.get(exit_kind, TaskState.TASK_STATE_COMPLETED)
        assert got.context_id == CTX
        _, delivery = await store.delivery_data(task_id)
        delivered = exit_kind in {"progress", "outbound_complete", "outbound_selected"}
        assert [p.text for a in got.artifacts for p in a.parts] == ([SELECTED] if delivered else [])
        assert sum(SELECTED in body for body in pushes) == int(delivered)
        for surface in (frames, subscriptions):
            assert sum(SELECTED in str(frame) for frame in surface) == int(delivered)
        await store.engine.dispose()
        reopened = stores.ReasoningCoalescingTaskStore(stores.make_sqlite_engine(path),
                                                      owner_resolver=stores.resolve_task_owner)
        try:
            # SDK history stays public for human devices too; console recovery is separate.
            history = _snapshot(await reopened.get(task_id, peer))
            console_records = await reopened.history_records(CTX, peer)
            assert (SECRET in str(console_records)) is (kind == "enrolled-console")
            surfaces = {"stream": frames, "subscription": subscriptions, "during": during,
                        "poll": _snapshot(got), "push": pushes, "reopened history": history}
            for name, surface in surfaces.items():
                for canary in (SECRET, REPLY, OUTPUT):
                    assert canary not in str(surface), (name, canary)
                if not delivered:
                    assert RESULT not in str(surface), name
            assert (SELECTED in str(history)) is delivered
            assert (await reopened.delivery_data(task_id))[1] == delivery
            recovered = RequesterProjection(reopened, projection.context, projection.queue, projection.binding, op)
            recovered.private_ref = ref
            assert not await recovered.deliver_to_requester(delivery["id"])
            private = await reopened.get_record(private_intake.ref_task_id(ref), op)
            assert all(c in str(private.private_payload) for c in (SECRET, REPLY, OUTPUT))
        finally:
            await reopened.engine.dispose()
        assert calls == ["request"]
        # A future effect guard may suppress a forbidden request. Do not require the leak.
        assert len(requests) <= int(exit_kind.startswith("outbound"))
        if exit_kind == "outbound_selected":
            assert [json.loads(body) for body in requests] == [{"value": RESULT}]
        return delivery, private_progress, requests
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await router.drain()
        cleanup = set(getattr(handler._active_task_registry, "_cleanup_tasks", ()) or ())
        if cleanup:
            await asyncio.wait_for(asyncio.gather(*cleanup), 5)
        set_progress_hook(None)
        set_terminal_hook(None)
        hitl_routing._ROUTER[0] = None
        await client.aclose()
        await store.engine.dispose()


@pytest.mark.platform_sensitive
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.xfail(strict=True, raises=MissingPrivateProgress,
                   reason="PROOF FAILURE: lifecycle-only operator hook cannot show private tool progress required by G1")
async def test_preexisting_handles_keep_operator_private_progress(tmp_path, monkeypatch, kind):
    delivery, progress, _ = await _scenario(tmp_path, monkeypatch, kind, "progress")
    assert delivery["state"] == "delivered"
    tool_frames = [frame for ctx, task, frame in progress if ctx == CTX and frame.get("phase") == "tool_end"]
    assert any(frame.get("phase") == "turn_started" for _, _, frame in progress)
    if not any(OUTPUT in str(frame) for frame in tool_frames):
        raise MissingPrivateProgress("operator hook lost private tool progress")


@pytest.mark.platform_sensitive
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("exit_kind", [
    "stall", "outbound_selected",
    pytest.param("implicit", marks=pytest.mark.xfail(strict=True, raises=OpenDelivery,
                 reason="PROOF FAILURE: implicit stream end leaves an unprepared private delivery open")),
    *[pytest.param(case, marks=pytest.mark.xfail(strict=True, raises=UnselectedEffect,
                   reason="PROOF FAILURE: outbound tool sends private values absent from the delivered result"))
      for case in ("outbound_complete", "outbound_stop", "outbound_selected_stop")],
])
async def test_private_exit_closes_without_unselected_effects(tmp_path, monkeypatch, kind, exit_kind):
    delivery, _, requests = await _scenario(tmp_path, monkeypatch, kind, exit_kind)
    if exit_kind == "implicit" and delivery["state"] == "private":
        raise OpenDelivery("completed task retained a live private delivery")
    assert delivery["state"] == ("delivered" if exit_kind in {"outbound_complete", "outbound_selected"} else "closed")
    if exit_kind not in {"outbound_complete", "outbound_selected"}:
        assert delivery["payload"] is None and delivery["digest"] is None
    for body in requests:
        # Only a value selected into the delivered result may cross an outbound boundary.
        delivered = delivery["payload"] if delivery["state"] == "delivered" else ""
        for value in (SECRET, REPLY, OUTPUT, RESULT):
            if value in body and value not in delivered:
                raise UnselectedEffect(f"undelivered private value escaped: {value}")
