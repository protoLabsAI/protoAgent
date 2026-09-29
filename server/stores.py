"""Agent stores: the checkpointer, the per-agent SQLite stores, the inbox now-recovery
worker, the background manager, and the telemetry / metrics / ledger stores.

Extracted from ``server/agent_init.py`` (#3829, epic #3804). These are the builders
``_init_langgraph_agent`` / ``_reload_langgraph_agent`` call to put durable state on
``runtime.state.STATE``:

- ``_build_checkpointer`` (+ ``_resolve_checkpoint_db``) — chat history.
- ``_agent_store_db`` — the per-agent store path (inbox / background / activity), constant
  so a rename can't orphan a store (#2382).
- ``_build_inbox_store`` + ``recover_pending_now_inbox_items`` /
  ``_start_inbox_now_recovery_once`` — the ADR 0003 inbox and its one-shot boot recovery.
- ``_build_background_manager`` (+ ``_on_work_terminal``) — ADR 0050 background jobs.
- ``_build_activity_log`` / ``_build_telemetry_store`` / ``_build_metrics_store`` /
  ``_build_ledger_store``.

``server.agent_init`` (and ``server/__init__``) re-export these names so existing callers
keep resolving; agent_init's boot/reload path calls the builders by bare name, so a patch
on ``agent_init._build_inbox_store`` (etc.) still intercepts those callers.

**Patch collaborators here, not on ``agent_init``.** What these functions call by bare
name — ``_agent_store_db``, ``_resolve_checkpoint_db``, ``recover_pending_now_inbox_items``,
``_on_work_terminal``, ``instance_paths``, ``agent_name`` — resolves in THIS module's
globals. The boot-recovery latch ``_INBOX_NOW_RECOVERY_STARTED`` lives only here (it is
deliberately NOT re-exported: a re-exported bool is a stale copy).
"""

import asyncio
import logging
import os
import re
from pathlib import Path

from infra.paths import instance_paths
from runtime.state import STATE
from server import agent_name

log = logging.getLogger("protoagent.server")

_INBOX_NOW_RECOVERY_STARTED = False
_INBOX_NOW_RECOVERY_BATCH_LIMIT = 8
_INBOX_NOW_RECOVERY_RETRY_AFTER_S = 60 * 60
# Bounded retries for a page that keeps refusing delivery. A COUNT, not a clock:
# recoverability after a crash must not wait on wall-time (a stale claim is released
# at startup), while a genuinely undeliverable page still stops replaying.
_INBOX_NOW_RECOVERY_MAX_ATTEMPTS = 5


def _resolve_checkpoint_db(configured: str) -> str:
    """The durable checkpoint DB — ``instance_root/checkpoints.db`` (per-instance).

    ``configured`` (``config.checkpoint_db_path``) only gates persistence on/off in
    ``_build_checkpointer``; the path itself is the per-instance store, always
    writable, so there's no /sandbox→~/.protoagent fallback dance any more."""
    path = instance_paths().store("checkpoints.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _build_checkpointer(config):
    """Durable SQLite checkpointer when ``checkpoint_db_path`` is set, else an
    in-memory saver (history cleared on restart). Falls back to in-memory if the
    SQLite saver can't be built so a bad path never blocks boot."""
    if not getattr(config, "checkpoint_db_path", ""):
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()
    try:
        from graph.checkpointer import build_sqlite_checkpointer

        path = _resolve_checkpoint_db(config.checkpoint_db_path)
        saver = build_sqlite_checkpointer(path)
        STATE.checkpoint_path = path
        log.info("[checkpointer] persistent chat history at %s", path)
        return saver
    except Exception:
        log.exception("[checkpointer] SQLite init failed; using in-memory (history won't persist)")
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()



#: Filename for a per-agent store in its OWN instance-private store dir. A constant, because
#: the dir is already the scope (``instance_root/<store>/``) — see ``_agent_store_db``.
_AGENT_DB = "agent.db"


def _agent_store_db(store: str, *, shared_dir: Path | None = None) -> Path:
    """Path for one of this agent's per-agent SQLite stores (inbox / background / activity).

    These were keyed by ``agent_name()`` — the **editable display name** — so renaming an
    agent silently pointed it at a brand-new empty database: the inbox, the background-results
    history and the activity feed all appeared to vanish. Nothing was deleted (both files sit
    side by side on disk, e.g. ``inbox/traderAgent.db`` next to ``inbox/merchantBot.db``); the
    agent just stopped looking at the old one. The name was never the scope in the first place
    — ``instance_paths().store(...)`` is already private to this instance (a fleet member's is
    inside its own workspace dir) — so the filename is now a constant and a rename can't move
    it.

    ``shared_dir`` (only the inbox's configured ``inbox_db_path``) keeps the name-keyed
    filename: there the namespace is load-bearing, because several agents may be pointed at
    one directory on purpose.

    One-time adoption for existing installs: if the constant path doesn't exist yet but the
    private dir holds exactly one ``*.db``, that file IS this agent's store under its old
    name, so it's used as-is — no move, no copy, nothing to interrupt. Two or more (a
    workspace that survived a rename before this fix) is genuinely ambiguous, so it logs both
    and starts clean at the constant path rather than guessing.
    """
    if shared_dir is not None:
        name = re.sub(r"[^a-zA-Z0-9._-]", "_", agent_name()) or "agent"
        shared_dir.mkdir(parents=True, exist_ok=True)
        return shared_dir / f"{name}.db"

    base = instance_paths().store(store)
    base.mkdir(parents=True, exist_ok=True)
    target = base / _AGENT_DB
    if target.exists():
        return target
    legacy = sorted(p for p in base.glob("*.db") if p.name != _AGENT_DB)
    if len(legacy) == 1:
        log.info("[%s] using this agent's existing store %s (name-keyed, pre-#2382)", store, legacy[0].name)
        return legacy[0]
    if legacy:
        log.warning(
            "[%s] %d name-keyed stores in %s (%s) — an earlier rename left more than one and "
            "nothing on disk says which is current, so starting fresh at %s rather than "
            "guessing. To keep one of them, rename it to %s (or move the others aside) while "
            "the agent is stopped.",
            store,
            len(legacy),
            base,
            ", ".join(p.name for p in legacy),
            _AGENT_DB,
            _AGENT_DB,
        )
    return target


def _build_inbox_store(config):
    """Durable inbound inbox (ADR 0003). ``inbox_db_path`` config (a dir) is used verbatim
    and stays namespaced by agent name; else the per-instance ``instance_root/inbox`` store,
    where the dir is already the scope (see ``_agent_store_db``)."""
    from inbox import InboxStore

    configured = getattr(config, "inbox_db_path", "") or ""
    db = _agent_store_db("inbox", shared_dir=Path(configured).expanduser() if configured else None)
    path = str(db)
    try:
        return InboxStore(path)
    except Exception:
        log.exception("[inbox] failed to build store at %s; inbox disabled", path)
        return None


async def recover_pending_now_inbox_items(
    *,
    limit: int = _INBOX_NOW_RECOVERY_BATCH_LIMIT,
    retry_after_s: int = _INBOX_NOW_RECOVERY_RETRY_AFTER_S,
    max_attempts: int = _INBOX_NOW_RECOVERY_MAX_ATTEMPTS,
    now=None,
) -> dict[str, int]:
    """One-shot startup recovery for pre-existing pending priority-``now`` inbox items.

    The inbox store owns the bounded claim/dedup policy: ``claim_now_recovery_batch``
    atomically claims the batch so a concurrent consumer can't double-deliver a
    still-pending page while we await its turn. Delivery flows through
    ``STATE.inbox_now_delivery``, the accepted self-A2A hook registered by ``server.a2a``
    and used by newly posted ``now`` pages. An accepted item is then marked delivered;
    an unfired item is restored to pending with actionable failure evidence so pull
    fallback still works.

    A claimed item that already carries a durable ``recovery_accepted_at`` (delivery was
    accepted on a prior pass but its ``mark_delivered`` write failed) is COMPLETED here —
    marked delivered without re-firing — so a restart cannot turn a recorded mark-failure
    into a duplicate Activity turn.
    """
    import asyncio

    store = STATE.inbox_store
    if store is None:
        return {"claimed": 0, "accepted": 0, "failed": 0}

    # Any recovery claim still set at startup was taken by a process that is gone: the
    # claim is an in-process lease. Left in place it hides the page from recovery AND
    # from the pull fallback until the lease ages out, so a crash between claim and
    # deliver used to cost an operator alert a full retry window — and a replacement
    # process booting inside that window could not reclaim it at all.
    try:
        stale = await asyncio.to_thread(
            store.reset_stale_recovery_claims,
            retry_after_s=retry_after_s,
            now=now,
        )
        if stale:
            log.info("[inbox] now-recovery released %d stale claim(s) from a prior process", stale)
    except Exception:  # noqa: BLE001 — reconciliation must not break boot
        log.exception("[inbox] now-recovery could not release stale claims")

    try:
        items = await asyncio.to_thread(
            store.claim_now_recovery_batch,
            limit=limit,
            retry_after_s=retry_after_s,
            max_attempts=max_attempts,
            now=now,
        )
    except Exception:  # noqa: BLE001 — recovery must not break boot
        log.exception("[inbox] now-recovery claim failed")
        return {"claimed": 0, "accepted": 0, "failed": 0}

    accepted = 0
    failed = 0
    if not items:
        return {"claimed": 0, "accepted": 0, "failed": 0}

    async def _deliver(item: dict) -> bool:
        import time

        guard = getattr(STATE, "storm_guard", None)
        if guard is not None and not guard.allow(time.monotonic()):
            log.warning("[inbox] storm guard suppressed now-recovery for item %s", item.get("id"))
            return False
        delivery = getattr(STATE, "inbox_now_delivery", None)
        if not callable(delivery):
            log.warning(
                "[inbox] now-recovery unavailable for item %s: delivery hook is not registered",
                item.get("id"),
            )
            return False
        return bool(await delivery(item))

    async def _restore(item_id: int, reason: str, claimed_at: str | None) -> None:
        """Best-effort: hand a claimed-but-unfired item back to pending with evidence.

        ``claimed_at`` scopes the restore to THIS recovery's claim. If a concurrent
        owner already delivered the page, the restore is a no-op: a late failure never
        un-delivers a delivered item."""
        try:
            await asyncio.to_thread(
                store.restore_recovery_failure,
                item_id,
                reason,
                claimed_at=claimed_at,
            )
        except Exception:  # noqa: BLE001 — restore is best-effort; never break the batch
            log.warning("[inbox] now-recovery could not restore item %s to pending (%s)", item_id, reason)

    async def _record_accepted_mark_failure(item_id: int, reason: str, claimed_at: str | None) -> None:
        """Best-effort: record accepted delivery whose delivered mark failed.

        This persists a durable ``recovery_accepted_at`` marker (see
        ``record_recovery_mark_delivered_failure``): the page stays undelivered but hidden
        from the pull fallback, and the next recovery pass COMPLETES it (marks delivered)
        without re-firing — so a restart can't turn this recorded mark-failure into a
        duplicate Activity turn. The recovery claim is left in place too, hiding the page
        within the current process before any restart.
        """
        try:
            await asyncio.to_thread(
                store.record_recovery_mark_delivered_failure,
                item_id,
                reason,
                claimed_at=claimed_at,
            )
        except Exception:  # noqa: BLE001 — evidence is best-effort; never break the batch
            log.warning(
                "[inbox] now-recovery could not record mark-delivered failure for item %s",
                item_id,
            )

    async def _restore_remaining(remaining: list[dict], reason: str) -> None:
        """Restore EVERY still-claimed item in ``remaining`` back to pending.

        ``claim_now_recovery_batch`` claimed the whole batch up front, so on
        cancellation the current item AND all later, not-yet-processed items still
        bear this recovery's claim. Restoring only the current one would leave the
        rest hidden from pull fallback until the recovery lease expires. Each restore
        is scoped to its own ``claimed_at``, so an item another owner has since
        delivered is left untouched."""
        for it in remaining:
            await _restore(int(it["id"]), reason, it.get("recovery_claimed_at"))

    # Every item in ``items`` was claimed by the atomic claim above, so no concurrent
    # consumer can double-deliver it while we await its fire. Mark accepted delivery
    # explicitly; restore the claim to pending (with evidence) otherwise.
    for idx, item in enumerate(items):
        item_id = int(item["id"])
        claimed_at = item.get("recovery_claimed_at")
        # A page carrying a durable recovery_accepted_at was already accepted-delivered on a
        # prior pass whose mark_delivered write failed (#3351 review). Re-firing it would
        # double-deliver, so COMPLETE it — mark delivered only, never fire again — then fall
        # through to the shared mark path below.
        already_accepted = bool(item.get("recovery_accepted_at"))
        try:
            delivered = True if already_accepted else await _deliver(item)
        except asyncio.CancelledError:
            # Restore the current item and every later item the batch already claimed,
            # otherwise the un-processed tail stays reserved until the recovery lease expires.
            await _restore_remaining(items[idx:], "delivery was cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — keep processing the rest of the bounded batch
            failed += 1
            log.exception("[inbox] now-recovery delivery raised for item %s", item_id)
            await _restore(item_id, f"delivery raised: {exc}", claimed_at)
            continue

        if delivered:
            try:
                await asyncio.to_thread(store.mark_delivered, [item_id])
            except Exception as exc:  # noqa: BLE001 — keep processing the rest of the bounded batch
                failed += 1
                log.exception("[inbox] now-recovery accepted item %s but could not mark delivered", item_id)
                await _record_accepted_mark_failure(
                    item_id,
                    f"accepted delivery could not be marked delivered: {exc}",
                    claimed_at,
                )
                continue
            accepted += 1
            continue

        failed += 1
        log.warning("[inbox] now-recovery not accepted for item %s; restoring pending fallback", item_id)
        await _restore(item_id, "delivery was not accepted", claimed_at)

    log.info(
        "[inbox] now-recovery claimed=%d accepted=%d failed=%d",
        len(items),
        accepted,
        failed,
    )
    return {"claimed": len(items), "accepted": accepted, "failed": failed}


def _start_inbox_now_recovery_once() -> None:
    """Start the one-shot boot recovery worker from the FastAPI startup lifecycle.

    The accepted delivery path is self-A2A through the mounted FastAPI app, so this
    must run after startup has established the app, graph, surfaces, and delivery
    hook. This is not a scheduler: it exits after one bounded recovery pass.
    """
    global _INBOX_NOW_RECOVERY_STARTED
    if _INBOX_NOW_RECOVERY_STARTED:
        return
    if STATE.inbox_store is None:
        return
    if not callable(getattr(STATE, "inbox_now_delivery", None)):
        log.warning("[inbox] now-recovery skipped: delivery hook is not registered")
        return

    async def _run() -> None:
        try:
            await recover_pending_now_inbox_items()
        except Exception:  # noqa: BLE001 — boot worker must never crash the process
            log.exception("[inbox] now-recovery worker failed")

    loop = getattr(STATE, "main_loop", None)
    if loop is not None and loop.is_running():
        _INBOX_NOW_RECOVERY_STARTED = True
        loop.create_task(_run())
        return

    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("[inbox] now-recovery skipped: no running startup event loop")
        return
    _INBOX_NOW_RECOVERY_STARTED = True
    running_loop.create_task(_run())


def _on_work_terminal(job) -> None:
    """Deliver a deterministic ``spawn_work`` job's completion (``knowledge_ingest``,
    ``delegate_to``) the SAME way the A2A terminal hook delivers a subagent-turn job
    (``server.a2a._handle_background_terminal``, ADR 0050 Phase 2 / ADR 0070 D1):
    push-resume the job's ORIGIN session when the D1 guards allow it (not disabled,
    not canceled, has a real chat origin, not incognito), so the origin agent runs a
    turn there and briefs the operator; fall back to the Activity-thread idle-wake
    otherwise. Module-level (not a nested closure) so it's independently testable.

    Before this, a ``spawn_work`` job only ever fired the Activity-thread wake — never
    a turn in the chat session that actually requested it — so a promise like
    ``knowledge_ingest``'s "I'll report back when it's ready" never fired (#1840).
    Lazy import keeps the manager off ``server`` and avoids a server.a2a ↔ agent_init
    import cycle. Best-effort — never raises into the manager's terminal callback."""
    try:
        from server.a2a import (
            _background_wake_enabled,
            _should_auto_resume,
            _spawn_background_resume,
            _spawn_background_wake,
        )

        mgr = getattr(STATE, "background_mgr", None)
        if mgr is not None and _should_auto_resume(job):
            _spawn_background_resume(mgr, job)
        elif _background_wake_enabled():
            _spawn_background_wake(job)
    except Exception:  # noqa: BLE001 — the wake/resume is best-effort
        log.exception("[background] work-job terminal hook failed for %s", getattr(job, "id", "?"))


def _build_background_manager(config):
    """Background subagent manager (ADR 0050). Fires detached jobs as self-POSTed A2A
    turns, so it derives the invoke URL + auth exactly like ``_build_scheduler`` (so a
    wizard rename can't break self-invocation). The store is the per-instance
    ``instance_root/background`` store (see ``_agent_store_db``). Reconciles any
    job left ``running`` by a prior crash on startup. Returns ``None`` when disabled or
    the store can't be built (the ``task`` tool then falls back to synchronous execution)."""
    if os.environ.get("BACKGROUND_DISABLED", "").lower() in ("1", "true", "yes"):
        log.info("[background] disabled via BACKGROUND_DISABLED env")
        return None
    from background import BackgroundManager, BackgroundStore

    path = str(_agent_store_db("background"))
    try:
        store = BackgroundStore(path)
    except Exception:
        log.exception("[background] failed to build store at %s; background disabled", path)
        return None
    try:
        reconciled = store.reconcile_interrupted()
        if reconciled:
            log.info("[background] reconciled %d interrupted job(s) on startup", reconciled)
    except Exception:
        log.exception("[background] startup reconcile failed")

    invoke_url = os.environ.get(
        "SCHEDULER_INVOKE_URL",
        f"http://127.0.0.1:{STATE.active_port}",
    )
    # One source for "what does this agent require inbound" — the guard that enforces it.
    # Re-deriving it per call site (this was config-or-env; the console used env-or-config)
    # is what let the card drift from enforcement (#2620).
    from a2a_impl.auth import inbound_credentials

    bearer, api_key = inbound_credentials()
    # The event bus → a still-open spawning chat gets a live ``background.started``
    # push (completion is published by the terminal hook). Imported lazily to keep
    # this builder import-cheap; tolerate its absence.
    try:
        from server import _event_bus

        publish = _event_bus.publish
    except Exception:  # noqa: BLE001
        publish = None

    try:
        return BackgroundManager(
            # Recorded on each job row as descriptive metadata — never used to scope a query
            # (unlike the scheduler's), so the live display name is the right value here.
            agent_name=agent_name(),
            invoke_url=invoke_url,
            store=store,
            api_key=api_key,
            bearer_token=bearer,
            event_publish=publish,
            on_terminal=_on_work_terminal,
        )
    except Exception:
        log.exception("[background] manager init failed; background disabled")
        return None


def _build_activity_log(config):
    """Provenance feed store (ADR 0022) — the per-instance ``instance_root/activity``
    store (see ``_agent_store_db``)."""
    from activity import ActivityLog

    path = str(_agent_store_db("activity"))
    try:
        return ActivityLog(path)
    except Exception:
        log.exception("[activity] failed to build log at %s; feed disabled", path)
        return None


def _build_telemetry_store(config):
    """Local per-turn telemetry store (ADR 0006 Slice 2). ``telemetry.db_path`` config
    is used verbatim when an operator overrides it; the legacy ``/sandbox`` default maps
    to the per-instance ``instance_root/telemetry.db`` store. Off when ``telemetry.enabled``
    is false; best-effort otherwise."""
    if not getattr(config, "telemetry_enabled", True):
        return None
    from observability.telemetry_store import TelemetryStore

    configured = getattr(config, "telemetry_db_path", "") or ""
    if configured and not str(configured).startswith("/sandbox"):
        db = Path(configured).expanduser()
    else:
        db = instance_paths().store("telemetry.db")
    db.parent.mkdir(parents=True, exist_ok=True)
    path = str(db)
    try:
        store = TelemetryStore(path)
        log.info("[telemetry] store ready at %s", path)
        return store
    except Exception:
        log.exception("[telemetry] failed to build store at %s; telemetry disabled", path)
        return None


def _build_metrics_store():
    """Plugin metric timeseries store (#1632) — behind ``sdk.record_metric`` /
    ``metric_history`` / ``metric_last``. Always on, no config gate: unlike the turn
    telemetry store (an observability preference), metric series are *functional*
    plugin state — history-dependent watch verifiers go blind without them — so they
    get their own per-instance ``metrics.db`` (retention is capped per series inside
    the store). Best-effort: a build failure degrades the SDK calls to no-ops."""
    from observability.metrics_store import MetricsStore

    db = instance_paths().store("metrics.db")
    try:
        store = MetricsStore(str(db))
        log.info("[metrics] plugin metric store ready at %s", db)
        return store
    except Exception:
        log.exception("[metrics] failed to build plugin metric store at %s; sdk metrics disabled", db)
        return None


def _build_ledger_store():
    """Per-instance delegation ledger (``ledger.db``).

    Always on, no config gate. It records the EDGE — which agent handed work to which
    delegate, and how that turned out — which nothing else stores: turn telemetry has no
    actor column, the in-flight delegation registry is an in-memory dict, and orgChart's
    topology is a live crawl that persists nothing. Without it "what did this fleet
    actually do" has no answer at all, so it is not something to make optional.
    """
    from observability.ledger_store import LedgerStore

    db = instance_paths().store("ledger.db")
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        store = LedgerStore(str(db))
        log.info("[ledger] store ready at %s", db)
        return store
    except Exception:
        # Best-effort, exactly like the writer: a fleet that cannot record its delegations
        # must still be able to make them.
        log.exception("[ledger] failed to build store at %s; delegation ledger disabled", db)
        return None
