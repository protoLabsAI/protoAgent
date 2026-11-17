"""Dormant Model B projection. Only proof tests install this executor seam.

Trusted acceptance supplies an enrolled principal (not an owner tier or display
origin). Private output is quarantined; only a sealed delivery ID can publish.
This isolates transports, not the model's wording inside an intended result.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from a2a.types import Artifact, Part, Task, TaskArtifactUpdateEvent, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict

from a2a_impl.private_intake import private_ref, ref_task_id


def push_digest(config):
    return hashlib.sha256(config.SerializeToString(deterministic=True)).hexdigest()


class RequesterProjection:
    def __init__(self, store, context, queue, binding, operator):
        self.store, self.context, self.queue = store, context, queue
        self.binding, self.operator = binding, operator
        self.private_ref = ""

    @classmethod
    async def accept(cls, store, context, queue, *, agent_id, operator, push_configs=()):
        binding = {
            "version": 1,
            "principal": context.call_context.state.get("requester_principal"),
            "agent_id": agent_id, "context_id": context.context_id,
            "message_id": context.message.message_id, "task_id": context.task_id,
            "push": sorted(push_digest(c) for c in push_configs),
        }
        if context.current_task is not None:
            original, _ = await store.delivery_data(context.task_id)
            if not original or any(original.get(k) != binding[k] for k in
                                   ("principal", "agent_id", "context_id", "task_id")):
                raise PermissionError("Requester continuation mismatch")
            binding = original
        await store.bind_requester(
            context.current_task or Task(id=context.task_id, context_id=context.context_id,
                                         history=[context.message]),
            binding, context.call_context,
        )
        projection = cls(store, context, queue, binding, operator)
        _, delivery = await store.delivery_data(context.task_id)
        if delivery is not None:
            await projection.begin_private(private_ref(delivery["private_turn_id"]))
        return projection

    async def begin_private(self, ref):
        # Trusted scheduling calls this BEFORE yielding any private boundary or output.
        record = await self.store.get_record(ref_task_id(ref), self.operator)
        if (record is None or record.task.context_id != self.binding["context_id"]
                or not record.private_payload or record.private_payload.get("version") != 1
                or record.private_payload.get("kind") != "steer"):
            raise PermissionError("Missing private segment")
        binding, previous = await self.store.delivery_data(self.binding["task_id"])
        if binding != self.binding:
            raise PermissionError("Requester binding changed")
        if previous is not None:
            if previous.get("version") != 1 or previous.get("private_turn_id") != ref_task_id(ref):
                raise PermissionError("Private segment mismatch")
        else:
            identity = uuid.uuid4().hex
            pending = {"version": 1, "id": identity, "idempotency_key": identity,
                       "private_turn_id": ref_task_id(ref), "state": "private",
                       "digest": None, "payload": None}
            if not await self.store.cas_delivery(binding["task_id"], binding, None, pending):
                return await self.begin_private(ref)
        self.private_ref = ref

    async def retain(self, event, payload):
        record = await self.store.get_record(ref_task_id(self.private_ref), self.operator)
        if record is None:
            raise PermissionError("Private partition disappeared")
        data = dict(record.private_payload)
        data["output"] = [*data.get("output", []), {"event": event, "payload": payload}]
        await self.store.save_private(record.task, data, self.operator, public_visible=False)

    async def prepare_result(self, result):
        """Trusted agent action seals intended bytes, without an operator release step."""
        if not self.private_ref or not isinstance(result, str):
            raise PermissionError("No private segment")
        binding, previous = await self.store.delivery_data(self.binding["task_id"])
        if binding != self.binding:
            raise PermissionError("Requester binding changed")
        digest = hashlib.sha256(result.encode()).hexdigest()
        if not previous or previous.get("private_turn_id") != ref_task_id(self.private_ref):
            raise PermissionError("Missing private segment binding")
        if previous["state"] != "private":
            if previous["digest"] != digest:
                raise PermissionError("Delivery is immutable")
            return previous["id"]
        record = {**previous, "digest": digest, "payload": result, "state": "prepared"}
        if not await self.store.cas_delivery(binding["task_id"], binding, previous, record):
            return await self.prepare_result(result)
        return record["id"]

    async def deliver_to_requester(self, delivery_id):
        binding, record = await self.store.delivery_data(self.binding["task_id"])
        if (binding != self.binding or not record or record.get("version") != 1
                or record.get("id") != delivery_id
                or record.get("state") != "prepared" or not self.private_ref
                or record.get("private_turn_id") != ref_task_id(self.private_ref)
                or hashlib.sha256(record["payload"].encode()).hexdigest() != record["digest"]):
            return False
        private = await self.store.get_record(record["private_turn_id"], self.operator)
        if (private is None or private.task.context_id != binding["context_id"]
                or not private.private_payload or private.private_payload.get("version") != 1
                or private.private_payload.get("kind") != "steer"):
            return False
        # A crash after this claim is ambiguous: do not resend or invent a new identity.
        claimed = {**record, "state": "dispatching"}
        if not await self.store.cas_delivery(binding["task_id"], binding, record, claimed):
            return False
        await self.queue.enqueue_event(TaskArtifactUpdateEvent(
            task_id=binding["task_id"], context_id=binding["context_id"],
            artifact=Artifact(artifact_id=record["id"], parts=[Part(text=record["payload"])]),
            append=False, last_chunk=True,
        ))
        await self.store.cas_delivery(binding["task_id"], binding, claimed, {**claimed, "state": "delivered"})
        return True

    async def enqueue_event(self, event):
        if not self.private_ref:
            await self.queue.enqueue_event(event)
            return
        await self.retain("transport", MessageToDict(event))
        # Keep lifecycle signals content-free so existing stream handles can terminate.
        if isinstance(event, TaskStatusUpdateEvent):
            clean = TaskStatusUpdateEvent()
            clean.CopyFrom(event)
            clean.status.ClearField("message")
            clean.ClearField("metadata")
            await self.queue.enqueue_event(clean)

    async def consume(self, event, payload):
        if event == "deliver_to_requester":
            # A label or a raw B event is never a delivery record.
            if isinstance(payload, str):
                await self.deliver_to_requester(payload)
            return True
        if self.private_ref:
            await self.retain(event, payload if isinstance(payload, (dict, list, str, int, float, bool, type(None)))
                              else json.dumps(str(type(payload))))
            return event not in ("done", "error", "input_required")
        return False
