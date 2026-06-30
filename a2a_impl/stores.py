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
import logging
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import bindparam, delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from a2a.server.context import ServerCallContext
from a2a.server.models import TaskModel
from a2a.server.tasks import (
    BasePushNotificationSender,
    DatabasePushNotificationConfigStore,
    DatabaseTaskStore,
)
from a2a.server.tasks.push_notification_sender import PushNotificationEvent
from a2a.types import TaskPushNotificationConfig

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

    async def _dispatch_notification(
        self,
        event: PushNotificationEvent,
        push_info: TaskPushNotificationConfig,
        task_id: str,
    ) -> bool:
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
    """
    task_db = _resolve_db_path("a2a-tasks.db")
    push_db = _resolve_db_path("a2a-push.db")
    task_store = DatabaseTaskStore(make_sqlite_engine(task_db))
    push_store = ValidatingPushNotificationConfigStore(make_sqlite_engine(push_db))
    return task_store, push_store, task_db, push_db


def build_push_sender(
    push_config_store: ValidatingPushNotificationConfigStore,
    httpx_client: httpx.AsyncClient,
) -> ValidatingPushNotificationSender:
    """SSRF-guarded push sender wired to the durable config store."""
    return ValidatingPushNotificationSender(httpx_client, push_config_store)


async def sweep_expired_tasks(engine: AsyncEngine, *, ttl_s: int = _DEFAULT_TTL_S, now: datetime | None = None) -> int:
    """Delete task rows older than ``ttl_s`` (24h default), keyed on the SDK's
    ``last_updated`` column. The SDK DB store has no TTL knob, so this restores
    the bespoke store's 24h eviction. Returns the number of rows deleted."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=ttl_s)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        # Preserve resumable HITL/auth pauses (their checkpoint outlives the TTL).
        # A NULL/absent state is not a preserved pause, so still sweep it (the
        # JSON-path is NULL for a stateless row, and ``NOT IN`` wouldn't match it).
        state = TaskModel.status["state"].as_string()
        result = await session.execute(
            delete(TaskModel)
            .where(TaskModel.last_updated < cutoff)
            .where(or_(state.is_(None), state.notin_(_PRESERVED_STATES)))
        )
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


def _interrupted_status_blob(now: datetime) -> dict:
    """A serialized ``failed`` ``TaskStatus`` carrying a restart error, in the
    same proto-JSON shape the SDK store writes (``MessageToDict``)."""
    import uuid

    from google.protobuf.json_format import MessageToDict
    from google.protobuf.timestamp_pb2 import Timestamp

    from a2a.types import Message, Part, Role, TaskState, TaskStatus

    ts = Timestamp()
    ts.FromDatetime(now)
    msg = Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_AGENT,
        parts=[Part(text="Task interrupted by an agent restart; its runner did not survive.")],
    )
    status = TaskStatus(state=TaskState.TASK_STATE_FAILED, message=msg, timestamp=ts)
    return MessageToDict(status)


async def reconcile_interrupted_tasks(engine: AsyncEngine, *, now: datetime | None = None) -> int:
    """Fail any task left non-terminal (``submitted`` / ``working``) by a restart.

    The bespoke task store did this; the SDK store doesn't — so an interrupted
    task lingers as fake-active until the 24h TTL silently *deletes* it, never
    surfacing a terminal state. A LangGraph runner doesn't survive a restart, so
    such a task is dead: transition it to ``failed`` with an error a caller can
    react to. The SDK serializes ``status`` as proto-JSON and itself filters on
    ``status['state']`` (in ``list_tasks``), so a dialect-agnostic JSON-path
    UPDATE mirrors ``sweep_expired_tasks``. Returns the rows reconciled. (#486)
    """
    now = now or datetime.now(UTC)
    blob = _interrupted_status_blob(now)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        result = await session.execute(
            update(TaskModel)
            .where(TaskModel.status["state"].as_string().in_(_INTERRUPTED_STATES))
            .values(status=blob, last_updated=now)
        )
        await session.commit()
        return result.rowcount or 0


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
) -> None:
    """Create the schemas, reconcile restart-interrupted tasks, and run the TTL
    sweep at boot.

    Restores the bespoke store's restart behavior the #443 migration dropped:
    non-terminal ``submitted`` / ``working`` tasks (dead — their LangGraph runner
    didn't survive) are failed *before* the TTL sweep, so they surface as
    terminal ``failed`` rather than being silently deleted at 24h (#486).
    ``input_required`` / ``auth_required`` pauses are left alone (resumable from
    the checkpoint).
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
    try:
        r = await reconcile_interrupted_tasks(task_store.engine)
        if r:
            log.info("[a2a] reconciled %d interrupted task(s) to failed (restart)", r)
    except Exception:
        log.exception("[a2a] interrupted-task reconciliation failed; continuing")
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
