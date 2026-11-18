"""Durable A2A stores + push-callback SSRF guard for the a2a-sdk wiring.

a2a-sdk owns the task lifecycle and push-config persistence, but its
``DefaultRequestHandler`` defaults to the *in-memory* ``InMemoryTaskStore`` /
``InMemoryPushNotificationConfigStore`` — task and push state are lost on a
restart. This module restores the two capabilities the bespoke
``a2a_task_store.py`` / ``a2a_push_store.py`` provided before the SDK migration:

1. **Durable persistence** — SQLite-backed ``DatabaseTaskStore`` /
   ``DatabasePushNotificationConfigStore`` (via SQLAlchemy + aiosqlite), at the
   same on-disk paths the bespoke stores used (instance-scoped per ADR 0004,
   ``/sandbox`` → ``~/.protoagent`` fallback). The SDK DB stores expose no TTL
   knob; the task store carries a ``last_updated`` column, so a 24h TTL sweep is
   reimplemented here (``sweep_expired_tasks``). The push-config model has no
   timestamp column, so push configs persist without the prior 24h TTL.

2. **SSRF guard** — a client supplies the push-notification callback URL; the
   SDK's ``BasePushNotificationSender`` POSTs to it with no validation hook. The
   bespoke store rejected loopback / RFC1918 / link-local / multicast / reserved
   targets (with a hostname + CIDR allowlist for trusted docker-network agents).
   That policy is restored verbatim and applied at BOTH config set-time
   (``ValidatingPushNotificationConfigStore.set_info``) and send-time
   (``ValidatingPushNotificationSender._dispatch_notification``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from copy import copy
from dataclasses import dataclass
import logging
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import bindparam, delete, insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from a2a.server.context import ServerCallContext
from a2a.server.models import TaskModel
from a2a.server.tasks import (
    BasePushNotificationSender,
    DatabasePushNotificationConfigStore,
    DatabaseTaskStore,
)
from a2a.server.tasks.push_notification_sender import PushNotificationEvent
from a2a.types import Task, TaskPushNotificationConfig

from a2a_impl.conversation_policy import CONVERSATION_WINDOW_DAYS, listed_outside_thread

log = logging.getLogger(__name__)

_DEFAULT_TTL_S = 24 * 60 * 60  # 24h, matching the bespoke stores


# ── SSRF guard for push-notification callback URLs ──────────────────────────────


def _parse_allowlist() -> tuple[frozenset[str], tuple]:
    """Parse the webhook allowlist env vars once per import.

    ``PUSH_NOTIFICATION_ALLOWED_HOSTS`` is a comma-separated list of
    hostnames (e.g. ``automaker-server,internal-hooks``) that bypass the
    SSRF check entirely — trusted internal agents on the docker
    network where every hostname resolves to an RFC1918 address by
    design.

    ``PUSH_NOTIFICATION_ALLOWED_CIDRS`` is a comma-separated list of
    CIDR ranges (e.g. ``10.0.14.0/24``) that bypass the SSRF check
    when the resolved IP falls inside any of them.

    Both are empty by default — the guard stays default-deny for any
    caller the operator hasn't explicitly trusted.
    """
    hosts_raw = os.environ.get("PUSH_NOTIFICATION_ALLOWED_HOSTS", "")
    cidrs_raw = os.environ.get("PUSH_NOTIFICATION_ALLOWED_CIDRS", "")
    hosts = frozenset(h.strip() for h in hosts_raw.split(",") if h.strip())
    cidrs = []
    for c in cidrs_raw.split(","):
        c = c.strip()
        if not c:
            continue
        try:
            cidrs.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            log.warning("[a2a] ignoring malformed CIDR in allowlist: %s", c)
    return hosts, tuple(cidrs)


def is_safe_webhook_url(url: str) -> bool:
    """Reject unsafe webhook targets before we accept or fire a push config.

    Defends against SSRF: a client supplying http://169.254.169.254/... or
    http://10.0.0.1/... as a webhook would have the agent POST task payloads to
    internal cloud metadata, adjacent private services, or the loopback
    device. One-time resolution is not a full defence against DNS rebinding,
    but it closes the trivial "just give it a RFC1918 literal" vector.

    Accepts:
    - http/https URLs to globally-routable IPs.
    - Hostnames in ``PUSH_NOTIFICATION_ALLOWED_HOSTS`` (trusted docker-network
      agents that resolve to RFC1918 by design).
    - Resolved IPs falling inside ``PUSH_NOTIFICATION_ALLOWED_CIDRS``.

    Rejects: non-http(s) schemes, unresolvable hostnames, and anything that
    resolves to loopback / link-local / private / multicast / reserved
    addresses that isn't explicitly allowlisted.

    The allowlist is re-read on each call so an operator can widen trust via
    env without a restart (and so tests can flip it with monkeypatch).

    When the opt-in ``security.callback_allowlist`` (#572) is configured, it
    becomes the policy: allow iff the destination resolves entirely into the
    allowlist. This intentionally overrides the default private-IP denylist (so
    an operator can permit a specific internal/tailnet range) and rejects
    everything outside it. Unset ⇒ the default denylist below.
    """
    from security import policy

    if policy.is_enabled():
        return policy.is_allowed(url)

    allowed_hosts, allowed_cidrs = _parse_allowlist()

    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False

    # Hostname allowlist takes precedence — trusted docker-network agents
    # where the DNS name resolves to an RFC1918 address by design.
    if host in allowed_hosts:
        return True

    # If the hostname is already a literal IP, check it directly; otherwise
    # resolve once and check every returned address (multi-A / AAAA).
    candidates: list[str] = []
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        try:
            # getaddrinfo returns (family, type, proto, canonname, sockaddr);
            # sockaddr[0] is the IP for both AF_INET and AF_INET6.
            candidates = [info[4][0] for info in socket.getaddrinfo(host, None)]
        except socket.gaierror:
            return False

    for addr in candidates:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if allowed_cidrs and any(ip in cidr for cidr in allowed_cidrs):
            continue  # CIDR allowlist bypass — trust this address
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_private
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


# ── Validating wrappers around the SDK push surfaces ────────────────────────────


class ValidatingPushNotificationConfigStore(DatabasePushNotificationConfigStore):
    """Durable push-config store that rejects unsafe callback URLs at set-time.

    Set-time validation gives the caller a synchronous failure (the
    ``set`` JSON-RPC call raises) instead of silently dropping the
    notification later. Send-time validation in
    ``ValidatingPushNotificationSender`` is the defence-in-depth backstop.
    """

    async def set_info(
        self,
        task_id: str,
        notification_config: TaskPushNotificationConfig,
        context: ServerCallContext,
    ) -> None:
        url = notification_config.url
        # is_safe_webhook_url resolves hostnames synchronously (getaddrinfo);
        # run it off the loop so a slow resolver can't stall the whole process.
        if url and not await asyncio.to_thread(is_safe_webhook_url, url):
            log.warning("[a2a] rejected unsafe webhook url at set-time: %s", url)
            raise ValueError(
                f"push-notification callback url is not allowed: {url!r} "
                "(resolves to loopback/private/link-local/multicast/reserved "
                "and is not allowlisted)"
            )
        await super().set_info(task_id, notification_config, context)


class ValidatingPushNotificationSender(BasePushNotificationSender):
    """Push sender that re-validates the callback URL before each POST.

    Backstops the set-time guard: even if a config slipped in (e.g. written
    directly to the store, or a DNS record that changed since set-time), the
    actual outbound POST is gated on the SSRF policy.
    """

    def __init__(self, *args, delivery_store=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._delivery_store = delivery_store

    async def _dispatch_notification(
        self,
        event: PushNotificationEvent,
        push_info: TaskPushNotificationConfig,
        task_id: str,
    ) -> bool:
        if self._delivery_store is not None:
            from a2a_impl.requester_delivery import push_digest

            binding, _ = await self._delivery_store.delivery_data(task_id)
            if binding is not None and (
                binding.get("version") != 1 or binding.get("task_id") != task_id
                or push_digest(push_info) not in binding.get("push", [])
            ):
                return False
        url = push_info.url
        # Re-validate at send-time (DNS may have changed since set-time) — but
        # off the loop: this runs before EVERY push POST, and a synchronous
        # getaddrinfo here would block the event loop for the OS timeout.
        if url and not await asyncio.to_thread(is_safe_webhook_url, url):
            log.warning(
                "[a2a] refusing push delivery to unsafe webhook url for task_id=%s: %s",
                task_id,
                url,
            )
            return False
        return await super()._dispatch_notification(event, push_info, task_id)


# ── Reasoning coalescing (#1710) ────────────────────────────────────────────────

# Duplicated from a2a_impl.executor (the executor imports graph.* at module
# scope; keeping the constant local avoids pulling the whole agent brain into
# the store's import chain). Locked together by test_a2a_stores.
_REASONING_MIME = "application/vnd.protolabs.reasoning-v1+json"


def coalesce_reasoning_history(task: Task) -> int:
    """Collapse each contiguous run of reasoning-v1 Messages in ``task.history``
    into ONE Message, in place. Returns the number of messages removed.

    Reasoning ("thinking") deltas are a STREAMING affordance: the executor emits
    them as reasoning-v1 DataParts on WORKING status frames so the console fills
    the live thinking bubble. But the a2a-sdk ``TaskManager`` moves every status
    frame's message into durable ``history`` — so a token-per-frame reasoning
    stream persisted one near-single-word Message per delta (~700 rows for one
    turn: #1710), bloating the store and making ``GetTask(historyLength=N)``
    return word fragments instead of conversation. The executor now batches the
    wire frames (~24-char granularity, keeping the live bubble); this collapses
    whatever reaches the durable store to one Message per contiguous reasoning
    block, mirroring how the answer text finalizes into a single canonical part.

    Deliberately mutates the task in place: the SDK ``TaskManager`` re-saves its
    ONE in-memory ``Task`` on every event and copies it whole for every
    subscriber frame, so keeping history compact also removes the O(events ×
    history) serialization/copy pressure the flood created (the prime suspect
    for the mid-stream artifact frame loss in #1709). Only agent messages whose
    parts are ALL reasoning DataParts are touched; user messages, tool frames,
    and HITL prompts are never merged, and non-contiguous runs stay separate.
    """
    from google.protobuf import json_format, struct_pb2

    from a2a.types import Part, Role

    def _reasoning_text(part) -> str | None:
        """The part's reasoning text, or None when it isn't a reasoning DataPart."""
        if part.WhichOneof("content") != "data":
            return None
        mime = part.metadata.fields["mimeType"].string_value if "mimeType" in part.metadata.fields else ""
        if mime != _REASONING_MIME:
            return None
        fields = part.data.struct_value.fields
        return fields["text"].string_value if "text" in fields else ""

    def _run_texts(msg) -> list[str] | None:
        """All parts' reasoning texts when the whole message is reasoning, else None."""
        if msg.role != Role.ROLE_AGENT or not msg.parts:
            return None
        texts = [_reasoning_text(p) for p in msg.parts]
        return texts if all(t is not None for t in texts) else None  # type: ignore[return-value]

    merged: list = []
    run_head = None  # first Message of the current contiguous reasoning run
    run_texts: list[str] = []

    def _close_run() -> None:
        nonlocal run_head, run_texts
        if run_head is None:
            return
        # Rewrite the head's parts to a single data part with the run's full text
        # (same shape the executor emits, so parsers see one big reasoning part).
        del run_head.parts[:]
        part = Part()
        value = struct_pb2.Value()
        json_format.ParseDict({"text": "".join(run_texts)}, value.struct_value)
        part.data.CopyFrom(value)
        part.metadata.update({"mimeType": _REASONING_MIME})
        part.media_type = "application/json"
        run_head.parts.append(part)
        merged.append(run_head)
        run_head, run_texts = None, []

    for msg in task.history:
        texts = _run_texts(msg)
        if texts is None:
            _close_run()
            merged.append(msg)
            continue
        if run_head is None:
            head = type(msg)()
            head.CopyFrom(msg)
            run_head = head
        run_texts.extend(texts)
    _close_run()

    removed = len(task.history) - len(merged)
    if removed:
        # `merged` holds references into task.history — copy before clearing.
        kept = []
        for m in merged:
            c = type(m)()
            c.CopyFrom(m)
            kept.append(c)
        del task.history[:]
        task.history.extend(kept)
    return removed


# Duplicated from a2a_impl.executor for the same import-chain reason as above; locked
# together by tests/test_delegate_progress.py.
_DELEGATE_PROGRESS_MIME = "application/vnd.protolabs.delegate-progress-v1+json"


def prune_superseded_progress(task: Task) -> int:
    """Keep only the LATEST delegate-progress Message per delegation in ``task.history``,
    in place. Returns the number of messages removed.

    A coding delegate's live progress (#3979) streams as whole-state snapshots on WORKING
    frames, and the SDK moves every one into durable history — a long coder run would
    persist hundreds of rows that each restate the last. A snapshot supersedes every
    earlier one for the same card (it is the whole state, not a delta), so dropping them
    loses nothing a reattach or a reload needs: it replays the final state. Only agent
    messages whose parts are ALL delegate-progress DataParts are touched."""
    from a2a.types import Role

    def _progress_id(msg) -> str | None:
        if msg.role != Role.ROLE_AGENT or len(msg.parts) != 1:
            return None
        part = msg.parts[0]
        if part.WhichOneof("content") != "data":
            return None
        mime = part.metadata.fields["mimeType"].string_value if "mimeType" in part.metadata.fields else ""
        if mime != _DELEGATE_PROGRESS_MIME:
            return None
        fields = part.data.struct_value.fields
        return fields["id"].string_value if "id" in fields else ""

    ids = [_progress_id(m) for m in task.history]
    if sum(1 for i in ids if i is not None) < 2:
        return 0
    last: dict[str, int] = {}
    for index, pid in enumerate(ids):
        if pid is not None:
            last[pid] = index
    keep = [i for i, pid in enumerate(ids) if pid is None or last[pid] == i]
    removed = len(ids) - len(keep)
    if removed:
        kept = []
        for i in keep:
            c = type(task.history[i])()
            c.CopyFrom(task.history[i])
            kept.append(c)
        del task.history[:]
        task.history.extend(kept)
    return removed


# Duplicated for the same import-chain reason as above; locked together by
# tests/test_stream_args.py.
_TOOL_ARGS_MIME = "application/vnd.protolabs.tool-args-v1+json"


def drop_tool_args_history(task: Task) -> int:
    """Remove every streamed tool-args-v1 Message from ``task.history``, in place.
    Returns the number removed.

    Streamed tool arguments (ADR 0118 D3) are a LIVE-ONLY preview: the executor emits them
    as tool-args-v1 DataParts on WORKING frames so the console can render an artifact as the
    model writes it, but the finished artifact replaces the preview the moment the tool
    runs. The a2a-sdk ``TaskManager`` would otherwise move each one into durable history
    (like reasoning), so a reattach or a reload would replay stale partial args. They carry
    nothing a durable task needs — drop them entirely. Only agent messages whose parts are
    ALL tool-args DataParts are touched; a message mixing one with other parts is kept."""
    from a2a.types import Role

    def _is_tool_args(msg) -> bool:
        if msg.role != Role.ROLE_AGENT or not msg.parts:
            return False
        for part in msg.parts:
            if part.WhichOneof("content") != "data":
                return False
            mime = part.metadata.fields["mimeType"].string_value if "mimeType" in part.metadata.fields else ""
            if mime != _TOOL_ARGS_MIME:
                return False
        return True

    keep = [m for m in task.history if not _is_tool_args(m)]
    removed = len(task.history) - len(keep)
    if removed:
        # `keep` holds references into task.history — copy before clearing.
        kept = []
        for m in keep:
            c = type(m)()
            c.CopyFrom(m)
            kept.append(c)
        del task.history[:]
        task.history.extend(kept)
    return removed


# ── Task owner (who may see a task) ─────────────────────────────────────────────

#: The ordinary-task owner every operator-tier caller saves under (shared bearer, devices, fleet
#: service token, the scheduler's own self-calls). Empty — the owner every row stored before
#: owners existed already carries, so those stay the operator's.
OPERATOR_OWNER = ""
#: New private segments belong to the verified human, independently of trust tier.
HUMAN_OPERATOR_OWNER = "human-operator"
#: The owner a federation-tier caller (ADR 0066) saves under. A federation caller sees and
#: touches only these; an operator-tier caller sees them too.
FEDERATION_OWNER = "federation"
_FEDERATION_CALL = ServerCallContext(state={"trust_tier": "federation"})


def resolve_task_owner(context: ServerCallContext | None) -> str:
    """The a2a-sdk ``owner_resolver``: the owner partition for the caller's tier, read from
    ``state["trust_tier"]`` (stamped from the auth middleware's classification by
    ``TierCallContextBuilder``). Anything but ``federation`` is the operator."""
    state = getattr(context, "state", None) or {}
    return FEDERATION_OWNER if state.get("trust_tier") == "federation" else OPERATOR_OWNER


def _human_operator(context) -> bool:
    from a2a_impl.auth import is_human_operator

    return is_human_operator((getattr(context, "state", None) or {}).get("principal_kind"))


def tier_context_builder():
    """A JSON-RPC ``ServerCallContextBuilder`` that also records the request's classified
    trust tier in the call context's state, where ``resolve_task_owner`` reads it.
    Principal kind travels alongside it without changing owner or tier permissions."""
    from a2a.server.routes.common import DefaultServerCallContextBuilder

    class TierCallContextBuilder(DefaultServerCallContextBuilder):
        def build(self, request):
            context = super().build(request)
            context.state["trust_tier"] = getattr(request.state, "trust_tier", None)
            context.state["principal_kind"] = getattr(request.state, "principal_kind", "open/unknown")
            return context

    return TierCallContextBuilder()


# These columns are deliberately NOT mapped on the SDK TaskModel. SDK serialization,
# merge(), artifacts and history therefore cannot carry the console partition.
_PRIVATE_COLUMNS = {
    "private_version": "INTEGER",
    "private_audience": "TEXT",
    "private_payload": "TEXT",
    "public_visible": "INTEGER",
}
PRIVATE_SCHEMA_VERSION = 1
PRIVATE_AUDIENCE = "human-operator"
#: SDK fields a private turn keeps only inside its partition record (``sdk_task``).
_PARTITIONED_SDK_FIELDS = ("status", "artifacts", "history", "metadata")


@dataclass(frozen=True)
class TaskRecord:
    """Server-only console projection; never pass this envelope to an A2A transport."""

    task: Task
    private_payload: dict | None = None


async def task_public_filter(conn, task_model=TaskModel, *, include_private=False, context=None):
    """Filter BEFORE counts, ranking and limits, including direct SQL history readers.

    Old SDK-only databases have no partition columns and keep their behavior. A partial
    schema or unknown audience/version refuses protected rows, never guessing a format.
    Tombstones hide both partitions of protected records after incomplete deletion.
    Ordinary records retain the SDK's read behavior, including after chat deletion.

    With a request context, exclude human-owned tasks from non-human callers. Without
    one, this is an internal SQL projection for bookkeeping,
    not an authorization boundary: retain all owners. Store reads additionally enforce
    SDK owner scoping even when their context is absent.
    """
    from sqlalchemy import Column, MetaData, String, Table, and_, exists, false, inspect, literal_column, true

    columns = await conn.run_sync(lambda c: {x["name"] for x in inspect(c).get_columns(task_model.__tablename__)})
    present = columns & _PRIVATE_COLUMNS.keys()
    predicate = true()
    if present:
        if present != _PRIVATE_COLUMNS.keys():
            return false()
        c = {name: literal_column(f"{task_model.__tablename__}.{name}") for name in _PRIVATE_COLUMNS}
        ordinary = and_(*(value.is_(None) for value in c.values()))
        supported = and_(
            c["private_version"] == PRIVATE_SCHEMA_VERSION,
            c["private_audience"] == PRIVATE_AUDIENCE,
            c["private_payload"].is_not(None),
            c["public_visible"].in_((0, 1)),
        )
        if not include_private:
            supported = and_(supported, c["public_visible"] == 1)
        if await conn.run_sync(lambda c: inspect(c).has_table(CHAT_TOMBSTONE_TABLE)):
            tombstones = Table(CHAT_TOMBSTONE_TABLE, MetaData(), Column("context_id", String(255)))
            supported = supported & ~exists().where(tombstones.c.context_id == task_model.context_id)
        predicate = or_(ordinary, supported)
    if "requester_binding" in columns:
        from sqlalchemy import func

        binding = literal_column(f"{task_model.__tablename__}.requester_binding")
        principal = (getattr(context, "state", None) or {}).get("requester_principal", "")
        predicate = predicate & or_(
            binding.is_(None),
            and_(
                func.json_extract(binding, "$.version") == 1,
                func.json_extract(binding, "$.principal") == principal,
                func.json_extract(binding, "$.task_id") == task_model.id,
                func.json_extract(binding, "$.context_id") == task_model.context_id,
            ) if principal else false(),
        )
    if "owner" in columns and context is not None and not _human_operator(context):
        predicate &= or_(task_model.owner.is_(None), task_model.owner != HUMAN_OPERATOR_OWNER)
    return predicate


def _recovered_column(task_model, column):
    """``column`` with a human-owned row's partitioned SDK field restored over the plain one."""
    from sqlalchemy import case, func, literal_column, type_coerce

    recovered = func.json_extract(
        literal_column(f"{task_model.__tablename__}.private_payload"), f"$.sdk_task.{column.name}"
    )
    if column.name == "status":
        # Recovery/cancellation may update the public lifecycle state directly.
        recovered = func.json_patch(recovered, column)
    value = case(
        (task_model.owner == HUMAN_OPERATOR_OWNER, func.coalesce(recovered, column)),
        else_=column,
    )
    return type_coerce(value, column.type).label(column.name)


async def _has_partition(conn, task_model) -> bool:
    from sqlalchemy import inspect

    names = await conn.run_sync(lambda c: {x["name"] for x in inspect(c).get_columns(task_model.__tablename__)})
    return _PRIVATE_COLUMNS.keys() <= names


async def task_read_columns(conn, task_model=TaskModel, *, context=None):
    """Recover private SDK fields only for a verified human, after row filtering."""
    columns = list(task_model.__table__.columns)
    if not _human_operator(context) or not await _has_partition(conn, task_model):
        return columns
    return [
        _recovered_column(task_model, column) if column.name in _PARTITIONED_SDK_FIELDS else column
        for column in columns
    ]


async def task_internal_columns(conn, *names, task_model=TaskModel):
    """The named SDK columns with a private turn's partitioned fields restored.

    INTERNAL readers only (model-context recovery, rewind bookkeeping): a private turn's
    reply lives only in the partition, and these readers ran on the plain columns before it
    moved there. Like a context-less ``task_public_filter`` this is not an authorization
    boundary and must never back a caller-facing route — those use ``task_read_columns``
    with the verified request context."""
    by_name = {column.name: column for column in task_model.__table__.columns}
    partitioned = await _has_partition(conn, task_model)
    return [
        _recovered_column(task_model, by_name[name])
        if partitioned and name in _PARTITIONED_SDK_FIELDS else by_name[name]
        for name in names
    ]


async def task_private_inputs(conn, task_ids, task_model=TaskModel) -> dict[str, str]:
    """Private-input text by private-only task id, for the INTERNAL readers above.

    A private turn's history carries only the opaque reference to its input record. Records
    of an unknown format, a tombstoned chat or a missing partition resolve to nothing."""
    from sqlalchemy import func, literal_column, select

    ids = sorted({i for i in task_ids if i})
    if not ids or not await _has_partition(conn, task_model):
        return {}
    text_input = func.json_extract(
        literal_column(f"{task_model.__tablename__}.private_payload"), "$.input"
    )
    rows = await conn.execute(
        select(task_model.id, text_input)
        .where(task_model.id.in_(ids), task_model.owner == HUMAN_OPERATOR_OWNER)
        .where(await task_public_filter(conn, task_model, include_private=True))
    )
    return {task_id: value for task_id, value in rows if isinstance(value, str)}


class ReasoningCoalescingTaskStore(DatabaseTaskStore):
    """Durable task store that coalesces contiguous reasoning-v1 history runs
    into one Message per run on every save (#1710), keeps only the latest
    delegate-progress snapshot per delegation (#3979), and drops live-only
    tool-args-v1 previews entirely (ADR 0118 D3). Streaming frames are
    untouched — this is persistence-shape only, so the wire contract and the
    live thinking bubble are unchanged."""

    async def initialize(self) -> None:
        await super().initialize()
        # create_all cannot add columns to existing SDK tables. No stored task is rewritten.
        from sqlalchemy import inspect

        async with self.engine.begin() as conn:
            columns = await conn.run_sync(
                lambda c: {x["name"] for x in inspect(c).get_columns(self.task_model.__tablename__)}
            )
            table = conn.dialect.identifier_preparer.quote(self.task_model.__tablename__)
            for name, sql_type in {**_PRIVATE_COLUMNS, "requester_binding": "TEXT", "delivery_record": "TEXT"}.items():
                if name not in columns:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))

    async def bind_requester(self, task, binding, context):
        """Dormant acceptance seam. Bind once, before publishing any task handle."""
        principal = (getattr(context, "state", None) or {}).get("requester_principal")
        if binding.get("version") != 1 or not principal or binding.get("principal") != principal:
            raise PermissionError("An enrolled requester principal is required")
        if binding.get("task_id") != task.id or binding.get("context_id") != task.context_id:
            raise PermissionError("Requester binding mismatch")
        if not binding.get("agent_id") or not binding.get("message_id"):
            raise PermissionError("Incomplete requester binding")
        await self._ensure_initialized()
        encoded = json.dumps(binding, sort_keys=True)
        async with self.async_session_maker.begin() as session:
            existing = await session.get(self.task_model, task.id)
            if existing is None:
                await session.merge(self._to_orm(task, self.owner_resolver(context)))
                await session.flush()
            elif existing.owner != self.owner_resolver(context) or existing.context_id != task.context_id:
                raise PermissionError("Requester ownership mismatch")
            row = (await session.execute(text(
                "SELECT requester_binding FROM tasks WHERE id=:id"
            ), {"id": task.id})).first()
            if existing is not None and row.requester_binding != encoded:
                raise PermissionError("Requester binding is immutable")
            await session.execute(text("UPDATE tasks SET requester_binding=:b WHERE id=:id"),
                                  {"b": encoded, "id": task.id})

    async def delivery_data(self, task_id):
        """Server-only binding and sealed delivery; never part of SDK serialization."""
        await self._ensure_initialized()
        async with self.engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT requester_binding, delivery_record FROM tasks WHERE id=:id"
            ), {"id": task_id})).first()
        return tuple(json.loads(v) if v else None for v in row) if row else (None, None)

    async def cas_delivery(self, task_id, binding, previous, record):
        """Seal/claim a delivery before publication; concurrent/restarted retries reuse it."""
        async with self.engine.begin() as conn:
            result = await conn.execute(text(
                "UPDATE tasks SET delivery_record=:record WHERE id=:id "
                "AND requester_binding=:binding AND delivery_record IS :previous"
            ), {"id": task_id, "binding": json.dumps(binding, sort_keys=True),
                "previous": json.dumps(previous, sort_keys=True) if previous else None,
                "record": json.dumps(record, sort_keys=True)})
            return result.rowcount == 1

    async def _reader(self, context, *, human_only=False, sibling_of=None, private=False):
        """An isolated SDK reader preserves SDK pagination and owner semantics."""
        from sqlalchemy import case, false, func
        from sqlalchemy.orm import aliased

        await self._ensure_initialized()
        async with self.engine.connect() as conn:
            visible = await task_public_filter(conn, self.task_model, context=context)
            read_columns = await task_read_columns(conn, self.task_model, context=context)
        human = _human_operator(context)
        if human_only:
            visible &= self.task_model.owner == HUMAN_OPERATOR_OWNER if human else false()
        if sibling_of is not None:
            owner = select(self.task_model.owner).where(self.task_model.id == sibling_of).scalar_subquery()
            visible &= self.task_model.owner == func.coalesce(
                owner, HUMAN_OPERATOR_OWNER if private else self.owner_resolver(context),
            )
        # Project both human-readable operator partitions into the SDK reader's owner
        # without changing stored owners or its pagination/counting behavior.
        columns = [
            case((column == HUMAN_OPERATOR_OWNER, OPERATOR_OWNER), else_=column).label("owner")
            if column.name == "owner" and human else column
            for column in read_columns
        ]
        reader = copy(self)
        reader.task_model = aliased(
            self.task_model, select(*columns).where(visible).subquery(), adapt_on_names=True
        )
        return reader

    async def save_private(
        self, task: Task, payload: dict, context: ServerCallContext, *, public_visible: bool = True,
    ) -> None:
        """Dormant trusted intake: persist an SDK projection and private JSON atomically.

        No route calls this. Private-only turns supply an empty SDK shell; private input
        and output go only in payload. Existing tasks retain their owner and visibility.
        """
        from a2a_impl.auth import is_human_operator

        if not is_human_operator((getattr(context, "state", None) or {}).get("principal_kind")):
            raise PermissionError("Private task access requires a human operator")
        if not isinstance(payload, dict):
            raise ValueError("Private payload must be an object")
        encoded = json.dumps(payload, allow_nan=False)
        if not public_visible and (task.history or task.artifacts or task.metadata.fields or task.status.ByteSize()):
            raise ValueError("Private-only tasks require an empty public shell")
        await self._ensure_initialized()
        async with self.async_session_maker.begin() as session:
            conn = await session.connection()
            await self._check_write(session, conn, task, context, private=True)
            table = conn.dialect.identifier_preparer.quote(self.task_model.__tablename__)
            previous_visibility = (await session.execute(
                text(f"SELECT public_visible FROM {table} WHERE id=:id"), {"id": task.id}
            )).first()
            # NULL partitions are existing public tasks, not new private-only shells.
            if previous_visibility is not None and (
                (1 if previous_visibility.public_visible is None else previous_visibility.public_visible)
                != int(public_visible)
            ):
                raise PermissionError("Task visibility cannot change")
            existing = await session.get(self.task_model, task.id)
            # A public task with an attached private payload remains an ordinary task.
            owner = existing.owner if existing else (
                self.owner_resolver(context) if public_visible else HUMAN_OPERATOR_OWNER
            )
            await session.merge(self._to_orm(task, owner))
            await session.flush()
            await session.execute(text(
                f"UPDATE {table} SET private_version=:version, private_audience=:audience, "
                "private_payload=:payload, public_visible=:visible WHERE id=:id"
            ), {"version": PRIVATE_SCHEMA_VERSION, "audience": PRIVATE_AUDIENCE,
                "payload": encoded, "visible": int(public_visible), "id": task.id})

    async def scheduling_payloads(self, context_id, context):
        """Read the complete private scheduling partition, independent of UI history limits."""
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            return await self._scheduling_payloads(session, context_id, context)

    async def _scheduling_payloads(self, session, context_id, context):
        from a2a_impl.auth import is_human_operator
        from sqlalchemy import literal_column

        if not is_human_operator((getattr(context, "state", None) or {}).get("principal_kind")):
            raise PermissionError("Private task access requires a human operator")
        conn = await session.connection()
        visible = await task_public_filter(conn, self.task_model, include_private=True, context=context)
        owners = [self.owner_resolver(context)]
        if owners == [OPERATOR_OWNER]:
            owners.append(FEDERATION_OWNER)
        if _human_operator(context):
            owners.append(HUMAN_OPERATOR_OWNER)
        rows = (await session.execute(
            select(self.task_model.id, literal_column("private_payload"))
            .where(self.task_model.context_id == context_id, self.task_model.owner.in_(owners),
                   visible, literal_column("public_visible") == 0,
                   literal_column("private_version") == PRIVATE_SCHEMA_VERSION)
        )).all()
        return {key: json.loads(raw) for key, raw in rows}

    async def scheduling_contexts(self, context, *, kind: str, states) -> list[str]:
        """Context ids that hold a private-only scheduling record of ``kind`` in one of ``states``.

        The restart recovery's one enumeration (ADR 0131 P1h.4): which conversations still have
        durable work, without reading any payload out. Operator-only and owner-scoped exactly
        like :meth:`scheduling_payloads`; raises on an unreadable partition (fail closed)."""
        from sqlalchemy import func, literal_column

        if not _human_operator(context):
            raise PermissionError("Private task access requires a human operator")
        await self._ensure_initialized()
        owners = [self.owner_resolver(context), HUMAN_OPERATOR_OWNER]
        if owners[0] == OPERATOR_OWNER:
            owners.append(FEDERATION_OWNER)
        payload = literal_column("private_payload")
        async with self.async_session_maker() as session:
            conn = await session.connection()
            visible = await task_public_filter(conn, self.task_model, include_private=True, context=context)
            rows = (await session.execute(
                select(self.task_model.context_id).distinct()
                .where(self.task_model.owner.in_(owners), visible, literal_column("public_visible") == 0,
                       literal_column("private_version") == PRIVATE_SCHEMA_VERSION,
                       func.json_extract(payload, "$.kind") == kind,
                       func.json_extract(payload, "$.state").in_(list(states)))
                .order_by(self.task_model.context_id)
            )).all()
        return [str(row[0]) for row in rows if row[0]]

    async def update_scheduling_payloads(self, context_id, context, mutate):
        """Commit a synchronous scheduling decision and all its task changes atomically.

        SQLite's writer reservation is acquired BEFORE reading, including on independent
        engines/processes. This covers sequence allocation, dedupe, create-if-absent and
        segment/turn attachment; no process-local lock or merge can lose accepted input.
        This dormant mechanism uses the runtime's SQLite task store only.
        """
        from a2a.types import a2a_pb2

        await self._ensure_initialized()
        if self.engine.dialect.name != "sqlite":
            raise NotImplementedError("Durable scheduling requires the SQLite task store")
        async with self.async_session_maker.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            payloads = await self._scheduling_payloads(session, context_id, context)
            before = {key: json.dumps(value, allow_nan=False) for key, value in payloads.items()}
            result = mutate(payloads)
            conn = await session.connection()
            table = conn.dialect.identifier_preparer.quote(self.task_model.__tablename__)
            for key, value in payloads.items():
                encoded = json.dumps(value, allow_nan=False)
                if before.get(key) == encoded:
                    continue
                task = a2a_pb2.Task(id=key, context_id=context_id)
                await self._check_write(session, conn, task, context, private=True)
                if key not in before:
                    # INSERT, never merge: an unrelated/inaccessible id must fail closed.
                    session.add(self._to_orm(task, HUMAN_OPERATOR_OWNER))
                    await session.flush()
                await session.execute(text(
                    f"UPDATE {table} SET private_version=:version, private_audience=:audience, "
                    "private_payload=:payload, public_visible=0 WHERE id=:id"
                ), {"version": PRIVATE_SCHEMA_VERSION, "audience": PRIVATE_AUDIENCE,
                    "payload": encoded, "id": key})
            return result

    async def cas_private_payload(
        self, task_id: str, *, guard: dict, updates: dict, context: ServerCallContext,
    ) -> bool:
        """Transactional compare-and-set on a private-partition record's JSON payload (ADR
        0131 P1c, dormant). Atomically overwrite the record's payload with
        ``{**current, **updates}`` IFF every key in ``guard`` still equals the stored value —
        a single guarded UPDATE, so a concurrent writer that moved the record on loses
        (``rowcount`` 0, returns False). The guard is the durable ordering token the segment
        scheduler CASes on (a monotonic ``generation``), not a process lock. Operator-only;
        never turns a shell public, never crosses owners. Returns True iff this call committed.
        """
        from a2a_impl.auth import is_human_operator

        if not is_human_operator((getattr(context, "state", None) or {}).get("principal_kind")):
            raise PermissionError("Private task access requires a human operator")
        if not isinstance(updates, dict):
            raise ValueError("CAS updates must be an object")
        # Guard/update keys land in a json_extract path and a payload dict — internal segment
        # fields only; reject anything that isn't a bare identifier so the path stays injection-safe.
        for key in (*guard, *updates):
            if not (isinstance(key, str) and key.isidentifier()):
                raise ValueError(f"Unsupported private payload key: {key!r}")
        await self._ensure_initialized()
        owners = [self.owner_resolver(context)]
        if owners == [OPERATOR_OWNER]:
            owners.append(FEDERATION_OWNER)
        if _human_operator(context):
            owners.append(HUMAN_OPERATOR_OWNER)
        async with self.async_session_maker.begin() as session:
            conn = await session.connection()
            table = conn.dialect.identifier_preparer.quote(self.task_model.__tablename__)
            row = (await session.execute(text(
                f"SELECT private_payload, private_version, owner FROM {table} WHERE id=:id"
            ), {"id": task_id})).first()
            if (
                row is None or row.private_version != PRIVATE_SCHEMA_VERSION
                or row.private_payload is None or row.owner not in owners
            ):
                return False
            payload = json.loads(row.private_payload)
            if not isinstance(payload, dict) or any(payload.get(k) != v for k, v in guard.items()):
                return False
            encoded = json.dumps({**payload, **updates}, allow_nan=False)
            # Single guarded UPDATE: the json_extract predicates re-check the guard atomically,
            # so a writer that changed a guarded field between the read above and here matches
            # no row (rowcount 0) instead of being clobbered — an optimistic CAS, no lost update.
            params: dict = {"p": encoded, "id": task_id, "ver": PRIVATE_SCHEMA_VERSION}
            where = ["id=:id", "private_version=:ver", "private_payload IS NOT NULL"]
            for i, (k, v) in enumerate(guard.items()):
                where.append(f"json_extract(private_payload,'$.{k}')=:g_{i}")
                params[f"g_{i}"] = v
            result = await session.execute(
                text(f"UPDATE {table} SET private_payload=:p WHERE {' AND '.join(where)}"), params
            )
            return (result.rowcount or 0) == 1

    async def _check_write(self, session, conn, task, context, *, private=False):
        # Refuse stale writes to protected/retired rows; never turn a private shell public.
        from sqlalchemy import inspect

        table = conn.dialect.identifier_preparer.quote(self.task_model.__tablename__)
        row = (await session.execute(text(
            f"SELECT owner, context_id, private_version, private_audience, private_payload, public_visible, requester_binding "
            f"FROM {table} WHERE id=:id"
        ), {"id": task.id})).first()
        if row is not None and row.owner == HUMAN_OPERATOR_OWNER and not _human_operator(context):
            raise PermissionError("Private task access requires a human operator")
        protected = row is not None and any(value is not None for value in row[2:6])
        if row is not None and row.requester_binding is not None:
            binding = json.loads(row.requester_binding)
            principal = (getattr(context, "state", None) or {}).get("requester_principal")
            if (binding.get("principal") != principal or row.context_id != task.context_id
                    or row.owner != self.owner_resolver(context)):
                raise PermissionError("Requester ownership mismatch")
        # Detached ordinary turns must still persist their final state after deletion.
        # Private intake and existing protected rows must never revive a deleted chat.
        if not private and not protected:
            return row.owner if row is not None and row.owner == HUMAN_OPERATOR_OWNER else None
        if await conn.run_sync(lambda c: inspect(c).has_table(CHAT_TOMBSTONE_TABLE)):
            context_ids = {task.context_id}
            if row is not None:
                context_ids.add(row.context_id)
            for context_id in context_ids:
                if (await session.execute(text(
                    f"SELECT 1 FROM {CHAT_TOMBSTONE_TABLE} WHERE context_id=:id"
                ), {"id": context_id})).first():
                    raise PermissionError("Conversation unavailable")
        if row is None:
            return
        if protected and (
            row.private_version != PRIVATE_SCHEMA_VERSION or row.private_audience != PRIVATE_AUDIENCE
            or row.private_payload is None or row.public_visible not in (0, 1)
            or row.context_id != task.context_id or (not private and row.public_visible == 0)
        ):
            raise PermissionError("Task unavailable")
        if private and row.owner not in (self.owner_resolver(context), FEDERATION_OWNER, HUMAN_OPERATOR_OWNER):
            raise PermissionError("Task unavailable")
        return row.owner if protected else None

    async def get_record(self, task_id: str, context: ServerCallContext) -> TaskRecord | None:
        """Read the console envelope; all SDK methods always return public Tasks."""
        from a2a_impl.auth import is_human_operator

        from sqlalchemy import literal, literal_column

        human = is_human_operator((getattr(context, "state", None) or {}).get("principal_kind"))

        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            conn = await session.connection()
            visible = await task_public_filter(conn, self.task_model, include_private=human, context=context)
            owners = [self.owner_resolver(context)]
            if owners == [OPERATOR_OWNER]:
                owners.append(FEDERATION_OWNER)
            if human:
                owners.append(HUMAN_OPERATOR_OWNER)
            row = (await session.execute(
                select(self.task_model, literal_column("private_payload") if human else literal(None))
                .where(self.task_model.id == task_id, self.task_model.owner.in_(owners), visible)
            )).first()
            if row is None:
                return None
            model, raw = row
            payload = json.loads(raw) if human and raw is not None else None
            if payload is not None and not isinstance(payload, dict):
                raise ValueError("Unsupported private payload")
            return TaskRecord(self._from_orm(model), payload)

    async def history_records(self, context_id: str, context: ServerCallContext, *, limit: int = 50):
        """Bounded server-only timeline; private turns never enter SDK Task.history."""
        from a2a_impl.auth import is_human_operator

        human = is_human_operator((getattr(context, "state", None) or {}).get("principal_kind"))
        await self._ensure_initialized()
        owners = [self.owner_resolver(context)]
        if owners == [OPERATOR_OWNER]:
            owners.append(FEDERATION_OWNER)
        if _human_operator(context):
            owners.append(HUMAN_OPERATOR_OWNER)
        async with self.engine.connect() as conn:
            visible = await task_public_filter(conn, self.task_model, include_private=human, context=context)
            ids = (await conn.execute(
                select(self.task_model.id)
                .where(self.task_model.context_id == context_id, self.task_model.owner.in_(owners), visible)
                .order_by(*task_newest_first(self.task_model, conn.dialect.name))
                .limit(max(1, min(limit, 200)))
            )).scalars().all()
        records = [await self.get_record(task_id, context) for task_id in reversed(ids)]
        return [record for record in records if record is not None]

    async def save(self, task: Task, context: ServerCallContext) -> None:
        try:
            coalesce_reasoning_history(task)
        except Exception:  # noqa: BLE001 — coalescing must never lose a save
            log.exception("[a2a] reasoning coalescing failed; saving uncoalesced")
        try:
            prune_superseded_progress(task)
        except Exception:  # noqa: BLE001 — pruning must never lose a save
            log.exception("[a2a] delegate-progress pruning failed; saving unpruned")
        try:
            drop_tool_args_history(task)
        except Exception:  # noqa: BLE001 — dropping previews must never lose a save
            log.exception("[a2a] tool-args history drop failed; saving with previews")
        await self._ensure_initialized()
        async with self.async_session_maker.begin() as session:
            protected_owner = await self._check_write(session, await session.connection(), task, context)
            owner = protected_owner if protected_owner is not None else self.owner_resolver(context)
            from a2a_impl.hitl_routing import private_input_ref

            if any(private_input_ref(message) for message in task.history):
                if not _human_operator(context):
                    raise PermissionError("Private task access requires a human operator")
                existing = await session.get(self.task_model, task.id)
                owner = existing.owner if existing is not None else HUMAN_OPERATOR_OWNER
            model = self._to_orm(task, owner)
            if owner == HUMAN_OPERATOR_OWNER:
                if not _human_operator(context):
                    raise PermissionError("Private task access requires a human operator")
                conn = await session.connection()
                table = conn.dialect.identifier_preparer.quote(self.task_model.__tablename__)
                raw = (await session.execute(
                    text(f"SELECT private_payload FROM {table} WHERE id=:id"), {"id": task.id}
                )).scalar_one_or_none()
                payload = json.loads(raw) if raw else {}
                payload["sdk_task"] = {
                    name: getattr(model, "task_metadata" if name == "metadata" else name)
                    for name in ("status", "artifacts", "history", "metadata")
                }
                model.status = {k: v for k, v in model.status.items() if k in ("state", "timestamp")}
                model.artifacts = []
                model.history = [
                    m for m, message in zip(model.history or [], task.history) if private_input_ref(message)
                ]
                model.task_metadata = {}
                await session.merge(model)
                await session.flush()
                await session.execute(text(
                    f"UPDATE {table} SET private_version=:version, private_audience=:audience, "
                    "private_payload=:payload, public_visible=1 WHERE id=:id"
                ), {"version": PRIVATE_SCHEMA_VERSION, "audience": PRIVATE_AUDIENCE,
                    "payload": json.dumps(payload), "id": task.id})
            else:
                await session.merge(model)

    # Owner scoping: the SDK filters every read by the caller's owner. An operator-tier
    # caller also reads federation-owned tasks, as it always could.
    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        reader = await self._reader(context)
        task = await DatabaseTaskStore.get(reader, task_id, context)
        if task is None and self.owner_resolver(context) == OPERATOR_OWNER:
            task = await DatabaseTaskStore.get(reader, task_id, _FEDERATION_CALL)
        return task

    async def list(self, params, context: ServerCallContext):
        reader = await self._reader(context)
        page = await DatabaseTaskStore.list(reader, params, context)
        if (
            not page.tasks
            and params.context_id
            and not params.page_token
            and self.owner_resolver(context) == OPERATOR_OWNER
        ):
            page = await DatabaseTaskStore.list(reader, params, _FEDERATION_CALL)
        return page

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        # Keep ordinary deletion semantics; only a human can remove the new partition.
        await super().delete(task_id, context)
        if _human_operator(context):
            private_store = copy(self)
            private_store.owner_resolver = lambda _: HUMAN_OPERATOR_OWNER
            await DatabaseTaskStore.delete(private_store, task_id, context)

    async def list_owner_scoped(self, params, context: ServerCallContext):
        """List only verified-human private tasks for the guarded resume fallback."""
        reader = await self._reader(context, human_only=True)
        return await DatabaseTaskStore.list(reader, params, context)

    async def list_sibling_tasks(self, params, context: ServerCallContext, task_id: str, *, private=False):
        """Settle only the saved turn's partition, preserving ordinary tier ownership."""
        # The executor can finish before the SDK persists its new task. Use the
        # producer's audience only in that window; a saved owner always wins.
        reader = await self._reader(context, sibling_of=task_id, private=private)
        return await DatabaseTaskStore.list(reader, params, context)

    async def context_foreign(self, context_id: str, context: ServerCallContext) -> bool:
        """Whether ``context_id`` is another owner's conversation, one a federation caller
        must not continue. Never true for an operator-tier caller.

        A conversation belongs to whoever STARTED it: the owner of its first-created task.
        An operator can add turns to a federation peer's thread (an idle steer runs as an
        operator-owned task in that context, ``server/chat.py`` ``_start_steer_turn``). That
        must not take the thread from the peer (#5379). The peer still can't read those
        operator tasks, because every read is owner-scoped. Creation order is the SQLite
        ``rowid`` (:func:`task_newest_first`). Another dialect has no such column, so it keeps
        the stricter rule: any task with another owner makes the context foreign."""
        owner = self.owner_resolver(context)
        if owner == OPERATOR_OWNER or not context_id:
            return False
        await self._ensure_initialized()
        in_context = self.task_model.context_id == context_id
        async with self.async_session_maker() as session:
            in_context = in_context & await task_public_filter(await session.connection(), self.task_model)
            if self.engine.dialect.name == "sqlite":
                from sqlalchemy import literal_column

                first = literal_column(f"{self.task_model.__tablename__}.rowid").asc()
                stmt = select(self.task_model.owner).where(in_context).order_by(first).limit(1)
                starter = (await session.execute(stmt)).scalar_one_or_none()
                return starter is not None and starter != owner
            stmt = select(self.task_model.id).where(in_context, self.task_model.owner != owner)
            return (await session.execute(stmt.limit(1))).first() is not None


# ── Turn order ─────────────────────────────────────────────────────────────────


def task_newest_first(task_model, dialect_name: str) -> tuple:
    """ORDER BY clauses putting a context's most recently CREATED task first (#3963).

    The task row keeps only ``last_updated`` — when it last CHANGED — which is not a turn's
    place in the conversation: a pause a plain message moved to a new task leaves the old
    task to be completed just AFTER the new one parks. On SQLite (the only store this
    runtime builds) the ``rowid`` is the creation order: the SDK inserts a task once and
    only ever merges it afterwards. Any other dialect has no such column, so it falls back
    to last-change order rather than failing the read."""
    if dialect_name == "sqlite":
        from sqlalchemy import literal_column

        return (literal_column(f"{task_model.__tablename__}.rowid").desc(),)
    return (task_model.last_updated.desc().nulls_last(), task_model.id.desc())


# ── Durable store construction (paths match the bespoke stores) ─────────────────


def _resolve_db_path(leaf: str) -> str:
    """Resolve the writable SQLite path for ``leaf`` (e.g. ``a2a-tasks.db``).

    A file directly under the per-instance ``instance_root`` (``store(leaf)`` →
    ``instance_root/<leaf>``), so co-located instances keep disjoint task/push DBs.
    """
    from infra.paths import instance_paths

    path = instance_paths().store(leaf)
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def make_sqlite_engine(db_path: str) -> AsyncEngine:
    """Async SQLAlchemy engine for a local SQLite file (aiosqlite driver)."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(f"sqlite+aiosqlite:///{db_path}")


def build_a2a_stores() -> tuple[
    DatabaseTaskStore,
    ValidatingPushNotificationConfigStore,
    str,
    str,
]:
    """Build the durable task + push-config stores at their on-disk paths.

    Returns ``(task_store, push_config_store, task_db_path, push_db_path)``.
    Each store gets its own engine/file (same split the bespoke stores used:
    ``a2a-tasks.db`` and ``a2a-push.db``). The SDK stores lazy-init their schema
    on first use; ``initialize_a2a_stores`` forces that + a TTL sweep at boot.
    The task store coalesces streamed reasoning runs into one durable history
    Message per run (#1710) — see ``ReasoningCoalescingTaskStore``.
    """
    task_db = _resolve_db_path("a2a-tasks.db")
    push_db = _resolve_db_path("a2a-push.db")
    task_store = ReasoningCoalescingTaskStore(make_sqlite_engine(task_db), owner_resolver=resolve_task_owner)
    push_store = ValidatingPushNotificationConfigStore(make_sqlite_engine(push_db), owner_resolver=resolve_task_owner)
    return task_store, push_store, task_db, push_db


#: The durable chat-deletion marker table, beside the task store in ``a2a-tasks.db``.
#: ``operator_api.chat_routes`` owns its schema and every write (one row per RETIRED console
#: chat, never count-evicted); :func:`tombstoned_chat_sessions` is its synchronous reader.
CHAT_TOMBSTONE_TABLE = "chat_session_tombstones"

#: The durable per-agent chat-session LIST table (ADR 0130 S6), beside the task store in
#: ``a2a-tasks.db``. One row per registered console chat — ``(context_id, created_at, title,
#: closed_at)`` — so the tab list, its titles and its open/closed state are server-held, not
#: device-local. ``server.session_registry`` owns its schema and every write.
CHAT_SESSION_LIST_TABLE = "chat_session_list"
_TOMBSTONE_QUERY_CHUNK = 500  # below SQLite's oldest bound-parameter limit (999)


def tombstoned_chat_sessions(session_ids) -> set[str] | None:
    """Which of ``session_ids`` the operator has DELETED (retired) — read from the chat
    tombstone table, synchronously, with stdlib ``sqlite3``.

    ``None`` means *unknown* (no task DB at this instance's path, or it can't be read): a caller
    deciding whether a chat still exists must then assume it does. A DB without the table is a
    truthful empty set — the table is created on the first retirement, so no chat was ever
    deleted. Synchronous on purpose: its caller (the artifact store's eviction, #4138) runs
    under a cross-process file lock in sync code, sometimes in a process with no task engine
    (the ACP tool process). A plain open, not ``mode=ro``: a read-only URI open fails on a
    live WAL database."""
    ids = sorted({s for s in session_ids if isinstance(s, str) and s})
    if not ids:
        return set()
    import sqlite3

    from infra.paths import instance_paths

    try:
        path = instance_paths().store("a2a-tasks.db")
        if not path.is_file():  # never create it: a reader must not conjure an empty store
            return None
        conn = sqlite3.connect(str(path), timeout=2.0)
    except Exception as exc:  # noqa: BLE001 — unknown, never a guess
        log.warning("[a2a] chat tombstones unreadable: %s", exc)
        return None
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (CHAT_TOMBSTONE_TABLE,)
        ).fetchone()
        if not has_table:
            return set()
        found: set[str] = set()
        for i in range(0, len(ids), _TOMBSTONE_QUERY_CHUNK):
            chunk = ids[i : i + _TOMBSTONE_QUERY_CHUNK]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(f"SELECT context_id FROM {CHAT_TOMBSTONE_TABLE} WHERE context_id IN ({marks})", chunk)
            found.update(r[0] for r in rows)
        return found
    except sqlite3.Error as exc:
        log.warning("[a2a] chat tombstones unreadable: %s", exc)
        return None
    finally:
        conn.close()


def build_push_sender(
    push_config_store: ValidatingPushNotificationConfigStore,
    httpx_client: httpx.AsyncClient,
) -> ValidatingPushNotificationSender:
    """SSRF-guarded push sender wired to the durable config store."""
    return ValidatingPushNotificationSender(httpx_client, push_config_store)


#: Console chat tabs use this ``context_id`` prefix (``operator_api.chat_routes`` indexes them
#: by it too). Their turns are the operator's chat history, which every device rebuilds from.
_CONSOLE_CHAT_PREFIX = "chat-"


async def sweep_expired_tasks(
    engine: AsyncEngine,
    *,
    ttl_s: int = _DEFAULT_TTL_S,
    now: datetime | None = None,
) -> int:
    """Delete task rows older than ``ttl_s`` (24h default), keyed on the SDK's
    ``last_updated`` column. The SDK DB store has no TTL knob, so this restores
    the bespoke store's 24h eviction. Returns the number of rows deleted.

    Console chat history is kept until the chat is deleted (Josh, 2026-11-11). Every device
    rebuilds a chat from these rows (ADR 0104), so sweeping them after 24h left the phone and
    the desktop each with only what it had cached locally. A ``chat-`` row is swept only once
    its chat has a tombstone (the operator deleted it). Outside threads eligible for the
    conversations list keep at least its shared window; jobs, background, Activity and
    palette contexts keep the 24h TTL.

    Restart-spared tasks (ADR 0129 D7, ``restart_spared_task_ids``) keep their pre-crash
    ``last_updated``, so a boot after long downtime — and the hourly prune loop, while a
    recovery is still pending — would sweep exactly the rows the scheduler's recovery is
    about to classify and resume, dropping unresolved work. They are excluded here; once
    recovery finalizes them (``finalize_interrupted_task``) they age normally."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=ttl_s)
    conversation_cutoff = now - timedelta(days=CONVERSATION_WINDOW_DAYS)
    from sqlalchemy import Column, MetaData, String, Table, exists, inspect, not_
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with engine.connect() as conn:
        has_tombstones = await conn.run_sync(lambda c: inspect(c).has_table(CHAT_TOMBSTONE_TABLE))
    is_chat = TaskModel.context_id.like(f"{_CONSOLE_CHAT_PREFIX}%")
    if has_tombstones:
        # Only the key column is read; operator_api.chat_routes owns the full schema.
        tombstones = Table(CHAT_TOMBSTONE_TABLE, MetaData(), Column("context_id", String(255)))
        kept_chat = is_chat & ~exists().where(tombstones.c.context_id == TaskModel.context_id)
    else:
        kept_chat = is_chat  # no chat was ever deleted on this instance

    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        # Preserve resumable HITL/auth pauses (their checkpoint outlives the TTL).
        # A NULL/absent state is not a preserved pause, so still sweep it (the
        # JSON-path is NULL for a stateless row, and ``NOT IN`` wouldn't match it).
        state = TaskModel.status["state"].as_string()
        stmt = (
            delete(TaskModel)
            .where(TaskModel.last_updated < cutoff)
            .where(or_(state.is_(None), state.notin_(_PRESERVED_STATES)))
            .where(not_(kept_chat))
            .where(or_(
                not_(listed_outside_thread(TaskModel.context_id)),
                TaskModel.last_updated < conversation_cutoff,
            ))
        )
        spared = sorted(_RESTART_SPARED_TASK_IDS)
        if spared:
            stmt = stmt.where(TaskModel.id.notin_(spared))
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount or 0


async def sweep_orphaned_push_configs(task_engine: AsyncEngine, push_engine: AsyncEngine) -> int:
    """Delete push-notification configs whose task no longer exists (ADR 0051 Slice 3).

    The SDK push-config store has no timestamp/TTL knob, so a registered webhook config
    persists forever — even after its task is TTL-swept (``sweep_expired_tasks``). Tie the
    config's lifetime to the task: read the live task ids, then drop push rows whose
    ``task_id`` isn't among them. Task + push live in separate SQLite files, so this is a
    two-step read-then-delete (no cross-DB join). Best-effort + SDK-table-tolerant: a
    no-op (returns 0) if the push table is absent or renamed by an SDK upgrade.
    Returns the rows deleted."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    tsm = async_sessionmaker(task_engine, expire_on_commit=False)
    async with tsm() as s:
        live = {r[0] for r in (await s.execute(select(TaskModel.id))).all()}

    psm = async_sessionmaker(push_engine, expire_on_commit=False)
    async with psm() as s:
        try:
            rows = (await s.execute(text("SELECT DISTINCT task_id FROM push_notification_configs"))).all()
        except Exception:  # noqa: BLE001 — table absent/renamed (SDK change): no-op
            return 0
        orphans = [r[0] for r in rows if r[0] not in live]
        if not orphans:
            return 0
        stmt = text("DELETE FROM push_notification_configs WHERE task_id IN :ids").bindparams(
            bindparam("ids", expanding=True)
        )
        res = await s.execute(stmt, {"ids": orphans})
        await s.commit()
        return res.rowcount or len(orphans)


# Non-terminal states a restart leaves dead: a queued (``submitted``) or
# mid-flight (``working``) task can never progress once its LangGraph runner is
# gone. We deliberately do NOT touch ``input_required`` / ``auth_required`` —
# those are HITL / auth *pauses* whose LangGraph checkpoint survives the restart
# and can resume on the next message, so failing them would be wrong.
_INTERRUPTED_STATES = ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING")

# HITL / auth *pauses* whose LangGraph checkpoint survives a restart and resumes on
# the next message — the TTL sweep must NOT delete them (initialize_a2a_stores'
# docstring promises this; sweep_expired_tasks now actually enforces it).
_PRESERVED_STATES = ("TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED")


#: The agent-visible diagnosis a restart-killed task fails with — shared with the
#: scheduler-side D7 recovery (``server/interrupted_turns.py``) so an occurrence an
#: operator must decide on carries the same wording as a blanket-failed chat turn.
RESTART_INTERRUPTED_TEXT = "Task interrupted by an agent restart; its runner did not survive."

# Restart-spared task ids currently awaiting (or undergoing) D7 checkpoint
# classification/resume (ADR 0129). A spared row keeps its pre-crash ``last_updated``,
# so after long downtime BOTH periodic janitors would destroy exactly the rows the
# scheduler's recovery is about to act on: the 24h TTL sweep would delete them, and
# the orphaned-WORKING reaper (#3418 — first pass 45s after boot, 30m idle window)
# would fail them MID-RESUME, after which the resume's final
# ``finalize_interrupted_task`` CAS (on submitted/working) matches nothing and a
# completed turn is recorded as failed. Both janitors skip these ids until recovery
# disposes each task (``finalize_interrupted_task`` releases the id, whatever state it
# wrote), after which the backstops apply again. In-process (every janitor runs in this
# process) and replaced — never accumulated — on each boot.
_RESTART_SPARED_TASK_IDS: set[str] = set()


def mark_restart_spared_tasks(task_ids) -> None:
    """Replace the restart-spared registry with ``task_ids`` (ADR 0129 D7). Called once
    per boot from ``initialize_a2a_stores`` — replacement, not accumulation, so a
    previous generation's leftover ids (a recovery that could not run) never pin rows
    forever across restarts."""
    _RESTART_SPARED_TASK_IDS.clear()
    _RESTART_SPARED_TASK_IDS.update(str(t) for t in task_ids if t)


def restart_spared_task_ids() -> frozenset[str]:
    """The task ids currently exempt from the TTL sweep and the orphaned-WORKING reaper."""
    return frozenset(_RESTART_SPARED_TASK_IDS)

# ``finalize_interrupted_task`` states → proto TaskState names (resolved lazily in
# ``_status_blob`` so the a2a import stays function-local like its siblings).
_FINALIZE_STATES = ("completed", "failed", "input_required")


def _status_blob(now: datetime, text: str, state: str = "failed") -> dict:
    """A serialized ``TaskStatus`` in ``state`` carrying ``text`` as the agent-visible
    message, in the same proto-JSON shape the SDK store writes (``MessageToDict``).
    Shared by the restart reconciler, the orphaned-WORKING-task reaper (#3418) and the
    D7 resume finalizer (ADR 0129)."""
    import uuid

    from google.protobuf.json_format import MessageToDict
    from google.protobuf.timestamp_pb2 import Timestamp

    from a2a.types import Message, Part, Role, TaskState, TaskStatus

    states = {
        "completed": TaskState.TASK_STATE_COMPLETED,
        "failed": TaskState.TASK_STATE_FAILED,
        "input_required": TaskState.TASK_STATE_INPUT_REQUIRED,
        "working": TaskState.TASK_STATE_WORKING,
    }
    ts = Timestamp()
    ts.FromDatetime(now)
    msg = Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_AGENT,
        parts=[Part(text=text)],
    )
    status = TaskStatus(state=states[state], message=msg, timestamp=ts)
    return MessageToDict(status)


def _failed_status_blob(now: datetime, text: str) -> dict:
    """A serialized ``failed`` ``TaskStatus`` carrying ``text`` as the agent-visible
    diagnosis, in the same proto-JSON shape the SDK store writes (``MessageToDict``).
    Shared by the restart reconciler and the orphaned-WORKING-task reaper (#3418)."""
    return _status_blob(now, text, "failed")


def _interrupted_status_blob(now: datetime) -> dict:
    """A serialized ``failed`` ``TaskStatus`` carrying a restart error, in the
    same proto-JSON shape the SDK store writes (``MessageToDict``)."""
    return _failed_status_blob(now, RESTART_INTERRUPTED_TEXT)


async def reconcile_interrupted_tasks(
    engine: AsyncEngine, *, now: datetime | None = None, spare_task_ids=()
) -> int:
    """Fail any task left non-terminal (``submitted`` / ``working``) by a restart.

    The bespoke task store did this; the SDK store doesn't — so an interrupted
    task lingers as fake-active until the 24h TTL silently *deletes* it, never
    surfacing a terminal state. A LangGraph runner doesn't survive a restart, so
    such a task is dead: transition it to ``failed`` with an error a caller can
    react to. The SDK serializes ``status`` as proto-JSON and itself filters on
    ``status['state']`` (in ``list_tasks``), so a dialect-agnostic JSON-path
    UPDATE mirrors ``sweep_expired_tasks``. Returns the rows reconciled. (#486)

    ``spare_task_ids`` (ADR 0129 D7): task ids the scheduler still holds accepted
    dispatch claims for are NOT blanket-failed here — on boot they are classified
    from their LangGraph checkpoint and, when safe, resumed against the original
    thread and task handle (``server/interrupted_turns.py`` + the scheduler's D7
    recovery). Interactive chat turns carry no claim, so they keep this failure.
    """
    now = now or datetime.now(UTC)
    blob = _interrupted_status_blob(now)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    spare = {str(t) for t in spare_task_ids if t}
    stmt = (
        update(TaskModel)
        .where(TaskModel.status["state"].as_string().in_(_INTERRUPTED_STATES))
        .values(status=blob, last_updated=now)
    )
    if spare:
        stmt = stmt.where(TaskModel.id.notin_(sorted(spare)))
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount or 0


async def interrupted_nonterminal_tasks(engine: AsyncEngine, task_ids) -> set[str]:
    """Which of ``task_ids`` are currently non-terminal (``submitted``/``working``) —
    i.e. a restart left them with no surviving runner (ADR 0129 D7). Read at boot,
    BEFORE any reconciliation touches them, so the scheduler's recovery pass knows
    exactly which of its accepted claims were interrupted (a claim whose task already
    reached a terminal state settles through normal reconciliation instead)."""
    ids = sorted({str(t) for t in task_ids if t})
    if not ids:
        return set()
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        rows = await session.execute(
            select(TaskModel.id)
            .where(TaskModel.id.in_(ids))
            .where(TaskModel.status["state"].as_string().in_(_INTERRUPTED_STATES))
        )
        return {r[0] for r in rows}


async def finalize_interrupted_task(
    engine: AsyncEngine, task_id: str, *, state: str, text: str, now: datetime | None = None
) -> bool:
    """Write a terminal (or ``input_required`` pause) status onto a restart-spared task
    row (ADR 0129 D7): the resume driver finishes a turn against the ORIGINAL task
    handle, so the handle must reach the state the turn actually ended in. CAS on the
    interrupted states — a row that moved on (another writer, a TTL sweep recreating
    nothing) is never clobbered. Also releases the id from the restart-spared registry
    (AFTER the write, so the reaper can't race the finalize): the recovery has made its
    disposition, so the TTL/reaper backstops apply to the row again from here on.
    Completion also replaces the interrupted answer artifact: transcript readers
    prefer artifacts over the status message. Failure/pause diagnoses leave partial
    output intact. Completion retries timestamp conflicts against fresh artifacts;
    after three conflicts it returns False and retains the reaper exemption for the
    caller to decide the disposition. Returns True when the row was updated."""
    if state not in _FINALIZE_STATES:
        raise ValueError(f"finalize_interrupted_task: unsupported state {state!r}")
    now = now or datetime.now(UTC)
    blob = _status_blob(now, text, state)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    # Retry only persistence, never the checkpoint drive or its side effects.
    for _attempt in range(3):
        async with session_maker() as session:
            stmt = (
                update(TaskModel)
                .where(TaskModel.id == str(task_id))
                .where(TaskModel.status["state"].as_string().in_(_INTERRUPTED_STATES))
            )
            values = {"status": blob, "last_updated": now}
            if state == "completed":
                row = (await session.execute(
                    select(TaskModel.artifacts, TaskModel.last_updated)
                    .where(TaskModel.id == str(task_id))
                    .where(TaskModel.status["state"].as_string().in_(_INTERRUPTED_STATES))
                )).first()
                if row is None:
                    _RESTART_SPARED_TASK_IDS.discard(str(task_id))
                    return False
                artifacts = list(row.artifacts or [])
                answer_id = f"{task_id}-answer"
                # HITL continuations have separate answer artifacts. Replace only
                # the latest leg, keeping earlier answers and unrelated artifacts.
                index = next((
                    i for i in range(len(artifacts) - 1, -1, -1)
                    if artifacts[i].get("artifactId") == answer_id
                    or artifacts[i].get("artifactId", "").startswith(f"{answer_id}-resumed-")
                ), None)
                if index is None:
                    artifacts.append({"artifactId": answer_id, "parts": [{"text": text}]})
                else:
                    artifact = dict(artifacts[index])
                    artifact["parts"] = [{"text": text}] + [
                        part for part in artifact.get("parts", []) if "text" not in part
                    ]
                    artifacts[index] = artifact
                values["artifacts"] = artifacts
                # Don't overwrite output saved by another writer since the read.
                stmt = stmt.where(TaskModel.last_updated.is_not_distinct_from(row.last_updated))
            result = await session.execute(stmt.values(**values))
            await session.commit()
        if result.rowcount or state != "completed":
            _RESTART_SPARED_TASK_IDS.discard(str(task_id))
            return bool(result.rowcount)
    # Keep the reaper exemption until recovery makes an explicit disposition.
    return False


async def reopen_restart_failed_task(
    engine: AsyncEngine, task_id: str, *, context_id: str = "", now: datetime | None = None
) -> bool:
    """Move a task the D7 disposition failed (:data:`RESTART_INTERRUPTED_TEXT`) back to
    ``working`` for an operator-ordered resume (ADR 0129 D7 S7).

    ``finalize_interrupted_task`` CAS-es on the interrupted states, so without this step
    the resume's final write could never land on a row the disposition already failed —
    a completed operator resume would stay recorded as failed. CAS on ``failed`` + the
    exact restart diagnosis, so an ordinarily-failed task can never be reopened.

    The row may also be GONE: the held occurrence (and its Needs-you card) parks
    indefinitely, while the failed row ages out of the 24h TTL sweep
    (``finalize_interrupted_task`` released it from the spared registry). What the resume
    actually drives is the LangGraph checkpoint — the task row is only the handle the
    outcome lands on — so when ``context_id`` names the session and no row exists at all,
    the handle is re-minted under the ORIGINAL task id instead of silently refusing. Only
    an ABSENT row is re-minted: an existing row in any other state still returns False,
    so an ordinarily-failed/completed task can never be clobbered.

    The id is re-registered as restart-spared so the TTL sweep / orphaned-WORKING reaper
    can't destroy the row mid-drive; the resume's own finalize releases it again. Returns
    True when the row is ``working`` by this call's doing (False: already reopened,
    or not a restart-failed task)."""
    now = now or datetime.now(UTC)
    blob = _status_blob(now, "Resuming after a restart at the operator's request.", "working")
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        result = await session.execute(
            update(TaskModel)
            .where(TaskModel.id == str(task_id))
            .where(TaskModel.status["state"].as_string() == "TASK_STATE_FAILED")
            .where(TaskModel.status["message"]["parts"][0]["text"].as_string() == RESTART_INTERRUPTED_TEXT)
            .values(status=blob, last_updated=now)
        )
        await session.commit()
        reopened = bool(result.rowcount)
        if not reopened and context_id:
            gone = (
                await session.execute(select(TaskModel.id).where(TaskModel.id == str(task_id)))
            ).first() is None
            if gone:
                try:
                    # Shaped like every row the executor saves for a scheduler fire:
                    # operator-tier owner (a NULL owner is invisible to the
                    # reconciler's GetTask, which filters on the caller's partition)
                    # and protocol_version 1.0 (the SDK's proto-vs-legacy
                    # deserialization discriminator — NULL reads as a 0.3 row).
                    await session.execute(
                        insert(TaskModel).values(
                            id=str(task_id), context_id=str(context_id), kind="task",
                            owner=OPERATOR_OWNER, status=blob, last_updated=now,
                            protocol_version="1.0",
                        )
                    )
                    await session.commit()
                    reopened = True
                except IntegrityError:
                    # Re-created between the check and the insert: whatever wrote it
                    # owns its state now — never clobber it.
                    await session.rollback()
    if reopened:
        _RESTART_SPARED_TASK_IDS.add(str(task_id))
    return reopened


# ── Orphaned-WORKING-task reaper (#3418) ─────────────────────────────────────────

# Two discriminated grace windows for ``reap_orphaned_working_tasks``, both keyed on
# the SDK ``last_updated`` column. Deliberately named constants (the store owns the
# defaults; the loop validates/clamps whatever it passes) rather than config fields.
#
# Both defaults sit ABOVE ``model.turn_stall_timeout_seconds`` (900s default) on purpose:
# the executor's own ``_stall_guarded`` owns a live-but-silent stream — including a turn
# that has NOT yet streamed its first frame — and fails it via ``TurnStalled`` after the
# stall window. This reaper is only a backstop for a producer that vanished WITHOUT a live
# stream for the stall guard to trip. Keeping both windows longer than the stall timeout
# guarantees the stall guard gets first crack: an alive task is already terminal (or has
# streamed a frame and bumped ``last_updated``) before either window elapses, so the reaper
# never preempts it. It only reaps a task whose producer is truly gone.
#
#  - BIRTH GRACE — a task still WORKING with no AGENT-authored history AND EMPTY artifacts
#    never produced anything: an orphan-at-birth whose producer died before streaming its
#    first frame. (Its history is not empty: it opens with the operator's prompt.) Kept
#    above the stall window so a genuinely-alive turn that is merely slow to its first
#    frame is failed by the stall guard, not clobbered here. Shorter than IDLE (there is
#    nothing productive to lose) but never shorter than the stall timeout.
#  - IDLE — a task that DID record history/artifacts but has since gone silent. Longer than
#    the stall window for the same reason: the stall guard owns the live stream; this only
#    backstops a producer that vanished WITHOUT tripping it.
_DEFAULT_REAP_BIRTH_GRACE_S = 20 * 60  # 20m — safely above the 900s DEFAULT stall window
_DEFAULT_REAP_IDLE_S = 30 * 60  # 30m
# Both defaults encode a RATIO to the stall window, not two magic numbers: 20m and 30m are
# 1.33x and 2x the 900s default. `turn_stall_timeout_seconds` is configurable, so an
# operator who raises it past 20m would otherwise have this reaper fail turns they had
# explicitly allowed to run — the fixed constants would silently override their setting,
# which is the opposite of a backstop.
_REAP_BIRTH_GRACE_RATIO = _DEFAULT_REAP_BIRTH_GRACE_S / 900
_REAP_IDLE_RATIO = _DEFAULT_REAP_IDLE_S / 900


def reap_thresholds_for(stall_timeout_s: float | None) -> tuple[int, int]:
    """``(birth_grace_s, idle_after_s)`` scaled to the configured stall window.

    The defaults are floors, so a SHORTER configured stall window never shortens the
    reaper below its documented minimum — reaping earlier than 20m/30m is not something
    lowering the stall timeout should buy. A LONGER one does move both up, because the
    invariant that matters is "the stall guard fires first", and only the ratio preserves
    it at any setting.
    """
    if not stall_timeout_s or stall_timeout_s <= 0:
        return _DEFAULT_REAP_BIRTH_GRACE_S, _DEFAULT_REAP_IDLE_S
    return (
        max(_DEFAULT_REAP_BIRTH_GRACE_S, int(stall_timeout_s * _REAP_BIRTH_GRACE_RATIO)),
        max(_DEFAULT_REAP_IDLE_S, int(stall_timeout_s * _REAP_IDLE_RATIO)),
    )


def _produced_output(history, artifacts) -> bool:
    """Whether a stored task row holds anything its PRODUCER emitted.

    Not "any history": the executor opens every task's history with the operator's
    prompt (ADR 0104 — the durable turn must say what was asked), so a producer that
    died before its first frame still leaves that ``ROLE_USER`` message behind. Only an
    artifact, or a history message the agent authored, is output."""
    if artifacts:
        return True
    return any(not (isinstance(m, dict) and m.get("role") == "ROLE_USER") for m in history or ())


def _row_age_anchor(last_updated, status_json) -> datetime | None:
    """The timestamp a WORKING row should be aged against, or None when it has none.

    Prefers the SDK's ``last_updated`` column and falls back to the A2A TaskStatus's own
    ``timestamp``. Both are normalised to aware UTC: SQLite hands back a naive datetime
    and the SDK writes it from a UTC proto timestamp, so a missing tzinfo means UTC.
    """
    if last_updated is not None:
        return last_updated if last_updated.tzinfo else last_updated.replace(tzinfo=UTC)
    raw = (status_json or {}).get("timestamp") if isinstance(status_json, dict) else None
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:  # a malformed timestamp is no timestamp
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def reap_orphaned_working_tasks(
    engine: AsyncEngine,
    *,
    birth_grace_s: int = _DEFAULT_REAP_BIRTH_GRACE_S,
    idle_after_s: int = _DEFAULT_REAP_IDLE_S,
    now: datetime | None = None,
) -> int:
    """Fail WORKING durable tasks whose producer disappeared without a trace (#3418).

    Covers the hole between boot reconciliation (``reconcile_interrupted_tasks`` —
    restart-only) and the executor stall guard (``_stall_guarded`` — an alive-but-
    silent stream): a producer that vanishes without EITHER path firing leaves a task
    stuck in ``TASK_STATE_WORKING``, and the console spinner — which waits indefinitely
    on a non-terminal durable record — never settles.

    The issue's discriminator, keyed on the persisted ``history``/``artifacts`` shape
    and the SDK ``last_updated`` column:

      - a task still WORKING with NO agent-authored history AND NO artifacts is an
        *orphan-at-birth* (the operator's prompt alone is not output — see
        ``_produced_output``); fail it once its age exceeds ``birth_grace_s``. Both
        defaults sit ABOVE the executor stall window so ``_stall_guarded`` fails a
        genuinely-alive slow-first-frame turn first; this only reaps a producer that left
        no live stream to trip the guard.
      - a task that recorded agent history or artifacts is *productive*; fail it only
        once it has been idle longer than ``idle_after_s`` (also longer than the stall
        window).

    Only ``TASK_STATE_WORKING`` rows are considered. Terminal states and the resumable
    ``TASK_STATE_INPUT_REQUIRED`` / ``TASK_STATE_AUTH_REQUIRED`` pauses are never touched.
    Restart-spared rows (ADR 0129 D7, ``restart_spared_task_ids``) are skipped: they keep
    their pre-crash ``last_updated``, so after >idle-window downtime this reaper's first
    post-boot pass would fail them MID-RESUME — and the resume's own finalize (CAS on
    submitted/working) would then silently lose a completed turn to that failed write.
    Recovery releases each id as it disposes the task (``finalize_interrupted_task``),
    restoring this backstop for the row.
    Eligible rows transition to a visible ``failed`` status carrying an actionable
    diagnosis; NOTHING is deleted, so a reaped row stays fetchable for the console's
    terminal watchdog. Idempotent: a reaped row is no longer WORKING, so a second pass
    changes nothing. Each row is classified from a scan and then failed under a
    compare-and-swap on its scanned ``last_updated`` (see below), so a task that revives
    and streams a frame after the scan is never clobbered to FAILED. Returns the number
    of rows actually reaped.

    Bounded input validation: ``birth_grace_s`` / ``idle_after_s`` are clamped to ``>= 0``
    and a non-positive window disables that arm, so a misconfigured value can never reap
    prematurely (a birth grace of 0 would fail every just-created task).
    """
    now = now or datetime.now(UTC)
    birth_grace_s = max(0, int(birth_grace_s))
    idle_after_s = max(0, int(idle_after_s))
    if not birth_grace_s and not idle_after_s:
        return 0
    from sqlalchemy.ext.asyncio import async_sessionmaker

    state = TaskModel.status["state"].as_string()
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    reaped = 0
    spared = frozenset(_RESTART_SPARED_TASK_IDS)
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(
                    TaskModel.id,
                    TaskModel.history,
                    TaskModel.artifacts,
                    TaskModel.last_updated,
                    TaskModel.status,
                ).where(state == "TASK_STATE_WORKING")
            )
        ).all()
        for task_id, history, artifacts, last_updated, status_json in rows:
            if task_id in spared:
                continue  # under D7 recovery — see the docstring; released on finalize
            # A NULL `last_updated` used to be skipped outright, which made this reaper
            # unable to do its own job on exactly the rows most likely to need it: a task
            # whose producer died is not more likely to have written that column. It was
            # left "for restart reconciliation", and a restart is the one event this
            # reaper exists to not depend on — so without a restart the row stayed
            # WORKING forever and the console spinner never settled.
            #
            # The A2A TaskStatus carries its own `timestamp`, written by the SDK whenever
            # the status is set, so it is a real age for the row even when the column is
            # NULL. Only when BOTH are missing is there genuinely nothing to age against,
            # and inventing an age there would risk failing a task created milliseconds
            # ago — so that (now much narrower) case still declines.
            lu = _row_age_anchor(last_updated, status_json)
            if lu is None:
                continue
            age_s = (now - lu).total_seconds()
            if _produced_output(history, artifacts):
                # Productive: the stall guard owns the live stream; only reap a producer
                # that vanished and left the task idle past the (longer) idle threshold.
                if not idle_after_s or age_s < idle_after_s:
                    continue
                reason = (
                    "Task failed: its producer went silent and could not be recovered "
                    f"(no update for {int(age_s)}s, past the {idle_after_s}s idle threshold)."
                )
            else:
                # Orphan-at-birth: WORKING but never persisted a frame or artifact of its own.
                if not birth_grace_s or age_s < birth_grace_s:
                    continue
                reason = (
                    "Task failed: it never produced any output "
                    f"(no frames or artifacts after {int(age_s)}s, past the {birth_grace_s}s "
                    "startup grace) — its producer did not survive."
                )
            # Compare-and-swap against the value we scanned, NOT just "still WORKING":
            # a task that became productive between the scan and here (its producer
            # revived and streamed a frame) is still WORKING but has moved on. The SDK
            # rewrites ``last_updated`` from ``status.timestamp`` on every ``save`` — so
            # appended history/artifacts and any status transition all bump it. Predicating
            # on the exact scanned ``last_updated`` (plus WORKING) fails the UPDATE to zero
            # rows if ANYTHING changed under us, so a re-productive turn is never clobbered
            # to FAILED. ``last_updated`` is the raw scanned value (not the tz-normalized
            # ``lu``) so it round-trips to the same stored representation. None rows are
            # skipped above, so this never compares against NULL.
            res = await session.execute(
                update(TaskModel)
                .where(TaskModel.id == task_id)
                .where(state == "TASK_STATE_WORKING")
                .where(TaskModel.last_updated == last_updated)
                .values(status=_failed_status_blob(now, reason), last_updated=now)
            )
            reaped += res.rowcount or 0
        if reaped:
            await session.commit()
    return reaped


async def drop_legacy_task_table(engine: AsyncEngine) -> bool:
    """Drop a pre-SDK bespoke ``tasks`` table so the SDK store can recreate it.

    The bespoke ``a2a_task_store.py`` (removed in the #443 SDK migration) created
    ``tasks(task_id, state, updated_at, data)``. The SDK's ``DatabaseTaskStore``
    expects ``tasks(id, context_id, status, …)`` and creates it via
    ``Base.metadata.create_all`` — which **skips a table that already exists**. So
    an instance upgraded across the migration keeps the legacy table, and every
    task op 500s with ``no such column: tasks.id`` (the console chat path included).

    Detect the legacy schema (``tasks`` present but no ``id`` column) and drop it
    — task rows are transient, regenerable state — so the subsequent
    ``create_all`` rebuilds the correct schema. No-op when the table is absent
    (fresh db) or already on the SDK schema. Returns True when a drop occurred.
    """
    async with engine.begin() as conn:
        cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info('tasks')"))).fetchall()]
        if not cols or "id" in cols:
            return False  # no table yet, or already the SDK schema — nothing to do
        n = (await conn.execute(text("SELECT count(*) FROM tasks"))).scalar()
        await conn.execute(text("DROP TABLE tasks"))
        log.warning(
            "[a2a] dropped legacy pre-SDK 'tasks' table (%s row(s), columns=%s) so the SDK "
            "DatabaseTaskStore can recreate its schema — fixes 'no such column: tasks.id' on "
            "instances upgraded across the #443 store migration",
            n,
            cols,
        )
        return True


async def initialize_a2a_stores(
    task_store: DatabaseTaskStore,
    push_store: ValidatingPushNotificationConfigStore,
    *,
    spare_task_ids=frozenset(),
) -> set[str]:
    """Create the schemas, reconcile restart-interrupted tasks, and run the TTL
    sweep at boot.

    Restores the bespoke store's restart behavior the #443 migration dropped:
    non-terminal ``submitted`` / ``working`` tasks (dead — their LangGraph runner
    didn't survive) are failed *before* the TTL sweep, so they surface as
    terminal ``failed`` rather than being silently deleted at 24h (#486).
    ``input_required`` / ``auth_required`` pauses are left alone (resumable from
    the checkpoint).

    ``spare_task_ids`` (ADR 0129 D7): the scheduler's unresolved accepted dispatch
    handles. Those of them a restart actually interrupted (still ``submitted`` /
    ``working`` here) are NOT failed — they are returned so the scheduler's boot
    recovery can classify each from its LangGraph checkpoint and resume the safe
    cases against the original thread and task handle. Everything else — interactive
    chat turns included — keeps today's fail-on-boot behavior.
    """
    # Upgrade guard: a legacy bespoke ``tasks`` table would survive create_all and
    # break every task op with "no such column: tasks.id". Drop it first so the
    # SDK rebuilds the correct schema.
    try:
        await drop_legacy_task_table(task_store.engine)
    except Exception:
        log.exception("[a2a] legacy task-table migration check failed; continuing")
    await task_store.initialize()
    await push_store.initialize()
    interrupted_spared: set[str] = set()
    try:
        if spare_task_ids:
            interrupted_spared = await interrupted_nonterminal_tasks(task_store.engine, spare_task_ids)
        r = await reconcile_interrupted_tasks(task_store.engine, spare_task_ids=interrupted_spared)
        if r:
            log.info("[a2a] reconciled %d interrupted task(s) to failed (restart)", r)
        if interrupted_spared:
            log.info(
                "[a2a] %d interrupted scheduler task(s) spared for checkpoint classification (ADR 0129 D7)",
                len(interrupted_spared),
            )
    except Exception:
        log.exception("[a2a] interrupted-task reconciliation failed; continuing")
    # Registered BEFORE the first sweep so neither janitor (TTL sweep here and hourly,
    # orphaned-WORKING reaper 45s in) can destroy a row mid-recovery; always called, so
    # a previous boot's leftovers are cleared even when nothing is spared this time.
    mark_restart_spared_tasks(interrupted_spared)
    try:
        n = await sweep_expired_tasks(task_store.engine)
        if n:
            log.info("[a2a] swept %d expired task record(s) (24h TTL)", n)
    except Exception:
        log.exception("[a2a] task TTL sweep failed; continuing")
    try:
        p = await sweep_orphaned_push_configs(task_store.engine, push_store.engine)
        if p:
            log.info("[a2a] swept %d orphaned push-config(s) (ADR 0051)", p)
    except Exception:
        log.exception("[a2a] push-config sweep failed; continuing")
    return interrupted_spared
