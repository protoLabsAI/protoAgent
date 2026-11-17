"""Dormant private-steer intake over the P1b.1 partition (ADR 0131 P1b.2; Refs #5401).

A private steer never travels as text. The console-authenticated sender records the steer
— and its deduplication key — into the private partition (``stores.save_private``, a
``public_visible=False`` shell) BEFORE any turn starts, and the loopback self-POST carries
only an OPAQUE reference bound to a single-use delegation (``graph.fleet.delegation``). The
trusted runner redeems that delegation and resolves the reference back to the text, and the
console recovers both the input and the reply from the same partition record
(``stores.get_record`` / ``history_records`` — operator-only). None of it ever enters an SDK
``Task``'s history, artifacts or status message, so a peer read (stream, poll, push, reopened
history) sees nothing.

**Fail closed (r3).** A missing record, an unknown ``version``, or a delegation that is not
bound to THIS reference stops the continuation — :func:`resolve_private_input` returns ``None``
(never the text) and the caller must refuse the turn rather than fall back to an ordinary
SendMessage.

**Dormant.** No live route sets ``private`` and nothing in the request path grants private
access off a delegation yet; only the P1b.2 proof tests reach this. It depends only on neutral
modules (``a2a_impl.stores`` + the ``graph.fleet.delegation`` shape) — never ``server`` — so the
import layering holds.
"""

from __future__ import annotations

import logging
import uuid

from a2a.types import a2a_pb2

from a2a_impl.stores import PRIVATE_SCHEMA_VERSION

log = logging.getLogger(__name__)

#: Scheme marking the opaque reference the loopback carries in place of the private text.
PRIVATE_INPUT_PREFIX = "private-input:"
#: The payload ``kind`` the dormant steer records under.
PRIVATE_KIND_STEER = "steer"


def private_ref(task_id: str) -> str:
    """The opaque reference for a stored private-input record (its private-only task id)."""
    return f"{PRIVATE_INPUT_PREFIX}{task_id}"


def is_private_ref(text: str | None) -> bool:
    """Whether ``text`` is a private-input reference (and nothing else)."""
    return isinstance(text, str) and text.startswith(PRIVATE_INPUT_PREFIX) and len(text) > len(PRIVATE_INPUT_PREFIX)


def ref_task_id(ref: str | None) -> str:
    """The private-only task id a reference points at, or ``""`` when ``ref`` is not one."""
    return ref[len(PRIVATE_INPUT_PREFIX) :] if is_private_ref(ref) else ""


def _private_task_id(session_id: str) -> str:
    return f"private:{session_id}:{uuid.uuid4().hex}"


async def record_private_steer(task_store, *, session_id, text, msg_id, context, kind=PRIVATE_KIND_STEER) -> str:
    """Persist a private steer and its dedupe key into the partition, transactionally, and
    return the opaque reference bound to it.

    The record is a ``public_visible=False`` shell — an EMPTY SDK ``Task`` plus the private
    JSON — so no peer-readable projection is ever exposed (``stores.save_private``). Raises on
    any failure; the caller must then NOT send an ordinary SendMessage (r3 fail-closed)."""
    task_id = _private_task_id(session_id)
    shell = a2a_pb2.Task(id=task_id, context_id=session_id)
    payload = {
        "version": PRIVATE_SCHEMA_VERSION,
        "kind": kind,
        "input": text,
        "dedupe": str(msg_id or ""),
        "reply": None,
    }
    await task_store.save_private(shell, payload, context, public_visible=False)
    return private_ref(task_id)


async def record_private_reply(task_store, ref, reply, context) -> bool:
    """Fold a turn's reply into its private-input record so the console can recover it.

    Re-saves the same ``public_visible=False`` shell with the reply filled in; the public
    projection stays empty. Best-effort: returns ``False`` when the record is gone, unknown
    or unreadable (never widens anything)."""
    task_id = ref_task_id(ref)
    if not task_id:
        return False
    record = await task_store.get_record(task_id, context)
    payload = getattr(record, "private_payload", None) if record is not None else None
    if not isinstance(payload, dict) or payload.get("version") != PRIVATE_SCHEMA_VERSION:
        return False
    payload = {**payload, "reply": reply}
    shell = a2a_pb2.Task(id=task_id, context_id=record.task.context_id)
    await task_store.save_private(shell, payload, context, public_visible=False)
    return True


async def resolve_private_input(task_store, ref, *, delegation, context) -> str | None:
    """Resolve an opaque reference to its private text, inside the trusted runner.

    Fail-closed (r3): returns ``None`` — never the text, never a fallback — unless the
    delegation is bound to THIS reference, the record exists for an operator who may read it,
    its ``version`` matches, and it carries private input. The store itself already refuses a
    non-human-operator ``context`` and an unknown partition format; this adds the delegation
    binding and the version re-check the runner must not continue without."""
    task_id = ref_task_id(ref)
    if not task_id:
        return None
    if delegation is None or getattr(delegation, "operation", None) != ref:
        return None  # binding missing or not for this reference
    try:
        record = await task_store.get_record(task_id, context)
    except Exception:  # noqa: BLE001 — an unreadable record is "missing", and fails closed
        log.warning("[private] private-input record unreadable for a resolve", exc_info=True)
        return None
    payload = getattr(record, "private_payload", None) if record is not None else None
    if not isinstance(payload, dict) or payload.get("version") != PRIVATE_SCHEMA_VERSION or "input" not in payload:
        return None
    return str(payload["input"])
