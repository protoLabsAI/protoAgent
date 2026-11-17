"""Dormant Model B projection. Only proof tests install this executor seam.

Trusted acceptance supplies an enrolled principal (not an owner tier or display
origin). Private output is quarantined; only a sealed delivery ID can publish.
This isolates transports, not the model's wording inside an intended result.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from a2a.types import Artifact, Part, Task, TaskArtifactUpdateEvent, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict, ParseDict

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
        self.bind_files()

    @property
    def file_owner(self):
        return ref_task_id(self.private_ref) if self.private_ref else ""

    def bind_files(self):
        if self.private_ref:
            from infra.media import bind_private_files

            bind_private_files(ref_task_id(self.private_ref), self.file_allowed)

    async def file_allowed(self, name, principal, digest, mime):
        binding, record = await self.store.delivery_data(self.binding["task_id"])
        if (binding != self.binding or binding.get("principal") != principal
                or not record or record.get("version") != 1
                or record.get("state") != "delivered" or record.get("format") != "artifact-v1"
                or record.get("private_turn_id") != ref_task_id(self.private_ref)
                or hashlib.sha256(record["payload"].encode()).hexdigest() != record["digest"]):
            return False
        private = await self.store.get_record(record["private_turn_id"], self.operator)
        if (private is None or not private.private_payload
                or private.private_payload.get("version") != 1
                or private.private_payload.get("kind") != "steer"
                or private.task.context_id != binding["context_id"]):
            return False
        envelope = json.loads(record["payload"])
        return envelope.get("version") == 1 and envelope["files"].get(name) == {"sha256": digest, "mime": mime}

    async def finalize(self, artifact):
        """Quarantine the assembled candidate; publish only the already selected result.

        Structured finalizers and attachment collection cannot extend a sealed result.
        Selection is a trusted agent action (prepare_result), never a producer label.
        """
        await self.retain("final-envelope", MessageToDict(artifact))
        _, record = await self.store.delivery_data(self.binding["task_id"])
        if record and record.get("state") == "prepared":
            await self.deliver_to_requester(record["id"])

    async def close(self):
        """Seal the delivery terminally on an abnormal turn exit (error, cancel, timeout).

        The executor only ever ``finalize``s — the single publish path — on a clean
        completion; an error, cancel or timeout never reaches it, so an already-sealed
        ``prepared`` result must not be shippable afterwards by a crash-recovery
        ``deliver_to_requester``. This drops the unsent candidate and moves the record to a
        terminal ``closed`` state so nothing private can publish after the turn gave up —
        the peer keeps only the content-free terminal the executor already sent.

        Idempotent and best-effort: a delivery already ``dispatching``/``delivered`` is left
        intact (never resent, never reopened), and a lost CAS just means the record already
        moved on. Nothing private is published here."""
        if not self.private_ref:
            return
        binding, previous = await self.store.delivery_data(self.binding["task_id"])
        if (binding != self.binding or not previous or previous.get("version") != 1
                or previous.get("private_turn_id") != ref_task_id(self.private_ref)
                or previous.get("state") in ("dispatching", "delivered", "closed")):
            return
        closed = {**previous, "state": "closed", "payload": None, "digest": None}
        await self.store.cas_delivery(binding["task_id"], binding, previous, closed)

    async def retain(self, event, payload):
        record = await self.store.get_record(ref_task_id(self.private_ref), self.operator)
        if record is None:
            raise PermissionError("Private partition disappeared")
        data = dict(record.private_payload)
        data["output"] = [*data.get("output", []), {"event": event, "payload": payload}]
        await self.store.save_private(record.task, data, self.operator, public_visible=False)

    async def prepare_result(self, result):
        """Trusted agent action seals intended bytes, without an operator release step."""
        if not self.private_ref or not isinstance(result, (str, Artifact)):
            raise PermissionError("No private segment")
        binding, previous = await self.store.delivery_data(self.binding["task_id"])
        if binding != self.binding:
            raise PermissionError("Requester binding changed")
        if (not previous or previous.get("version") != 1
                or previous.get("private_turn_id") != ref_task_id(self.private_ref)):
            raise PermissionError("Missing private segment binding")
        candidate = result
        format_name = "text"
        if isinstance(result, Artifact):
            from infra.media import is_private_media, private_owner, private_file_digest, resolve_media

            artifact = Artifact()
            artifact.CopyFrom(result)
            artifact.artifact_id = previous["id"]
            files = {}
            # Include links in text/DataParts as well as FileParts: previews are content.
            names = re.findall(r"/media/(private-[A-Za-z0-9._-]+)", json.dumps(MessageToDict(artifact)))
            for name in names:
                if (not is_private_media(name)
                        or private_owner(name) != ref_task_id(self.private_ref)):
                    raise PermissionError("Result file is outside the private segment")
                files[name] = {"sha256": private_file_digest(name),
                               "mime": resolve_media(name, private_authorized=True)[1]}
            result = json.dumps({"version": 1, "artifact": MessageToDict(artifact), "files": files},
                                sort_keys=True, separators=(",", ":"), allow_nan=False)
            format_name = "artifact-v1"
        digest = hashlib.sha256(result.encode()).hexdigest()
        if previous["state"] != "private":
            if previous["digest"] != digest:
                raise PermissionError("Delivery is immutable")
            return previous["id"]
        record = {**previous, "digest": digest, "payload": result, "state": "prepared", "format": format_name}
        if not await self.store.cas_delivery(binding["task_id"], binding, previous, record):
            return await self.prepare_result(candidate)
        return record["id"]

    async def deliver_to_requester(self, delivery_id):
        binding, record = await self.store.delivery_data(self.binding["task_id"])
        if (binding != self.binding or not record or record.get("version") != 1
                or record.get("format", "text") not in {"text", "artifact-v1"}
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
        if record.get("format") == "artifact-v1":
            from infra.media import private_file_digest, private_owner, resolve_media

            envelope = json.loads(record["payload"])
            if envelope.get("version") != 1:
                return False
            try:
                for name, resource in envelope["files"].items():
                    if (private_owner(name) != record["private_turn_id"]
                            or private_file_digest(name) != resource["sha256"]
                            or resolve_media(name, private_authorized=True)[1] != resource["mime"]):
                        return False
            except (OSError, ValueError, KeyError, TypeError):
                return False
        # A crash after this claim is ambiguous: do not resend or invent a new identity.
        claimed = {**record, "state": "dispatching"}
        if not await self.store.cas_delivery(binding["task_id"], binding, record, claimed):
            return False
        artifact = Artifact(artifact_id=record["id"], parts=[Part(text=record["payload"])])
        if record.get("format") == "artifact-v1":
            envelope = json.loads(record["payload"])
            artifact = ParseDict(envelope["artifact"], Artifact())
        await self.queue.enqueue_event(TaskArtifactUpdateEvent(
            task_id=binding["task_id"], context_id=binding["context_id"],
            artifact=artifact,
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

    # Frames the executor still PROCESSES for a privately-steered turn. Its usage/cost/
    # llm_calls, tool_calls/tool-duration and delta accounting must run or the turn's row
    # records zero spend; the terminals let it finish or park. Only the CONTENT frames
    # (text, reasoning, steer_consumed, …) are quarantined — telemetry is not isolated.
    # The accounted frames' peer-facing output is suppressed downstream in enqueue_event
    # and the executor's private-mode progress wrapper, so processing them here leaks
    # nothing; a content frame carrying a forged audience label is swallowed, not published.
    _PASS_THROUGH = frozenset(
        {"usage", "delta", "tool_start", "tool_end", "done", "error", "input_required"}
    )

    async def consume(self, event, payload):
        if event == "deliver_to_requester":
            # A label or a raw B event is never a delivery record.
            if isinstance(payload, str):
                await self.deliver_to_requester(payload)
            return True
        if self.private_ref:
            await self.retain(event, payload if isinstance(payload, (dict, list, str, int, float, bool, type(None)))
                              else json.dumps(str(type(payload))))
            return event not in self._PASS_THROUGH
        return False
