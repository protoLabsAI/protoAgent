"""Plugin SDK — the stable surface a plugin uses to TAP CORE capabilities.

The plugin contract has two halves:

  • Contribution — ``PluginRegistry.register_*`` (tools, routers, recipe dirs, goal
    verifiers, …): what a plugin ADDS to the host.
  • Consumption — THIS module: what a plugin CALLS back into the host (run a subagent,
    read the live config, …).

Plugins import ``from graph.sdk import …`` rather than reaching into ``graph.agent`` /
``runtime.state`` internals, so core can refactor underneath them without breaking
plugins. Keep this surface **small, stable, and deliberate** — it's the seam we lean on
as plugins tap core more aggressively (the workflows plugin is the first real consumer:
its engine injects ``run_subagent`` as the per-step runner).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from events import ACTIVITY_CONTEXT
from runtime.state import STATE

log = logging.getLogger(__name__)

# Re-export the supervised background-task helper as part of the consumption surface, so a
# plugin writes `from graph.sdk import supervise` for a self-perpetuating, watchdog-backed
# engine instead of hand-rolling task/restart machinery (graph/supervisor.py is host-free).
from graph.supervisor import RetryAfter, Supervisor, supervise  # noqa: F401

# Re-export the telemetry + decision-log kit, so a plugin writes
# `from graph.sdk import DecisionLog, telemetry, render_html` for a standard observability
# surface (audit trail + envelope + themed panel). graph/telemetry.py is host-free.
from graph.telemetry import DecisionLog, render_html, telemetry  # noqa: F401

# Re-export the runtime-knobs + presets control surface, so a plugin writes
# `from graph.sdk import Knobs, make_knob_tools` for a bounded, reversible set of tunable
# engine knobs + presets + auto-generated agent tools (graph/knobs.py is host-free).
from graph.knobs import Knobs, make_knob_tools  # noqa: F401


def config() -> Any:
    """The live runtime ``LangGraphConfig``."""
    return STATE.graph_config


def gateway_client(*, timeout: float | None = None) -> Any:
    """An ``httpx.AsyncClient`` pre-configured for the **model gateway** (#1931):
    ``base_url`` = the configured ``api_base``, bearer auth, the allowlisted
    User-Agent (the gateway's WAF 403s default SDK UAs), and a sane timeout.

    For OpenAI-compatible endpoints the chat model doesn't cover —
    ``/images/generations``, ``/images/edits``, ``/audio/*`` (core's own
    transcription rides the same client). Request relative paths and use it per
    call::

        async with sdk.gateway_client(timeout=300) as client:
            resp = await client.post("/images/generations", json={...})
            resp.raise_for_status()

    Call gateway endpoints through this — never a provider backend directly: the
    ``api_base`` host is auto-trusted by the egress guard + OpenShell network
    policy (ADR 0008); other hosts are not (a private backend IP is denied
    outright). ``timeout=None`` keeps the factory default."""
    from graph.config import LangGraphConfig
    from graph.llm import gateway_client as _factory

    cfg = STATE.graph_config or LangGraphConfig()
    return _factory(cfg, **({} if timeout is None else {"timeout": timeout}))


def subagent_types() -> set[str]:
    """Ids of the configured subagents — for validating/listing recipe steps."""
    from graph.subagents.config import SUBAGENT_REGISTRY

    return set(SUBAGENT_REGISTRY)


async def run_subagent(
    subagent_type: str,
    prompt: str,
    *,
    description: str,
    extra_tools: Any = None,
    truncate: int | None = None,
) -> str:
    """Run a subagent to completion and return its text output.

    Pulls the config + knowledge store + scheduler from runtime state, so a plugin
    tool only supplies the subagent + prompt. This is the capability the workflows
    plugin's engine injects as its per-step ``run_step``.

    ``extra_tools`` defaults to the host's plugin + MCP tools (the same set the
    lead graph and the console fan-out expose) — a subagent whose allowlist names
    a plugin tool (the review-finder's ``github_pr_diff``, a finance backtester)
    must see it here too, or every SDK-driven workflow step silently degrades to
    "No tools available". Pass an explicit list (even ``[]``) to override.
    """
    from graph.agent import run_manual_subagent

    if extra_tools is None:
        extra_tools = list(getattr(STATE, "plugin_tools", None) or []) + list(getattr(STATE, "mcp_tools", None) or [])

    return await run_manual_subagent(
        STATE.graph_config,
        knowledge_store=getattr(STATE, "knowledge_store", None),
        scheduler=getattr(STATE, "scheduler", None),
        description=description,
        prompt=prompt,
        subagent_type=subagent_type,
        extra_tools=extra_tools,
        truncate=truncate,
    )


async def complete(prompt: str, *, system: str | None = None, model_name: str | None = None) -> str:
    """Run a single **bare** LLM completion and return the text — no tools, no agent
    loop, no persona, no memory. The clean primitive for a plugin that just needs the
    model to answer a prompt (e.g. an interactive artifact calling back to the agent,
    a one-shot classifier/summarizer). Distinct from :func:`run_subagent`, which runs a
    full tool-using subagent. Uses the live config's model through the gateway; pass
    ``model_name`` to target a different model on the same gateway, ``system`` for a
    system instruction.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from graph.llm import create_llm

    llm = create_llm(STATE.graph_config, model_name=model_name)
    messages: list[Any] = []
    if system:
        messages.append(SystemMessage(system))
    messages.append(HumanMessage(prompt))
    resp = await llm.ainvoke(messages)
    content = getattr(resp, "content", resp)
    return content if isinstance(content, str) else str(content)


# ── knowledge graph (the plugin↔knowledge channel, ADR 0043 — "shared knowledge") ──
# The consumption SDK exposed run_subagent/complete but not the knowledge store, so a
# plugin couldn't ground its work in (or contribute to) what the agent knows. These
# thin accessors close that: the coding loop reads distilled lessons to inject into
# a coder's prompt; the loop-retro writes recurring failures back as searchable chunks.
# knowledge_purge + the epoch tag (#1634) are the LIFECYCLE half — a long-running
# plugin's knowledge can become actively wrong (spacetraders: weekly universe wipes),
# so it needs a way to retire a bucket (purge) or scope retrieval to the current era
# (epoch) without core knowing the plugin's reset semantics. All degrade to a no-op
# ([] / None / 0) when no store is configured, and run the (HTTP-embedding) store
# call off the event loop.


async def knowledge_search(
    query: str, *, k: int = 5, domain: str | None = None, epoch: str | None = None
) -> list[dict]:
    """Search the agent's knowledge graph (hybrid FTS5 + embeddings); return the top-``k``
    matching chunks (each a dict with ``preview``/``content``, ``domain``, ``score`` …),
    or ``[]`` when no store is configured. ``domain`` scopes to one bucket
    (e.g. ``"loop-lessons"``); ``epoch`` (#1634) scopes to chunks tagged with exactly
    that era via ``knowledge_add(..., epoch=...)`` — out-of-era and untagged chunks
    don't match (both search modes filter). ``None`` = unfiltered."""
    store = getattr(STATE, "knowledge_store", None)
    if store is None:
        return []
    if epoch is None:
        # Only pass epoch when set — an ADR 0031 plugin backend predating the epoch
        # kwarg keeps working untouched on the common unfiltered path.
        return await asyncio.to_thread(store.search, query, k=k, domain=domain)
    return await asyncio.to_thread(store.search, query, k=k, domain=domain, epoch=epoch)


async def knowledge_add(
    content: str, *, domain: str = "general", heading: str | None = None, epoch: str | None = None
) -> int | None:
    """Add one chunk to the agent's knowledge graph; return its id, or ``None`` when no
    store is configured / it was a no-op. ``domain`` is the bucket, ``heading`` an
    optional title — e.g. ``knowledge_add(lesson, domain="loop-lessons", heading=cls)``.
    ``epoch`` (#1634) tags the chunk with the era it was learned in — an opaque string,
    typically a reset date (``epoch="2026-06-29"``). On the next wipe the plugin just
    searches with the NEW epoch: old lessons stay for post-mortems but stop matching."""
    store = getattr(STATE, "knowledge_store", None)
    if store is None:
        return None
    if epoch is None:
        return await asyncio.to_thread(store.add_chunk, content, domain=domain, heading=heading)
    return await asyncio.to_thread(store.add_chunk, content, domain=domain, heading=heading, epoch=epoch)


async def knowledge_purge(domain: str, *, before: str | None = None) -> int:
    """HARD-delete every chunk in ``domain`` — optionally only those created strictly
    before ``before`` (an ISO-8601 timestamp) — and return how many were removed.

    The knowledge-lifecycle primitive (#1634): retire a bucket of now-wrong lessons
    (``knowledge_purge("st-routes")``) or expire just the stale tail
    (``knowledge_purge("st-routes", before="2026-06-01")``). Deletes consistently from
    every index (main rows, FTS, vectors); on a layered store only the PRIVATE tier is
    purged — the shared commons is curated, never bulk-deleted. Keep-for-audit
    retirement is the ``epoch`` tag instead (see :func:`knowledge_add`). Returns 0 when
    no store is configured (or the backend has no ``purge_domain``), when ``domain`` is
    empty, or when ``before`` is unparseable — it refuses rather than risk deleting the
    wrong rows."""
    store = getattr(STATE, "knowledge_store", None)
    purge = getattr(store, "purge_domain", None)
    if store is None or not callable(purge):
        return 0
    return await asyncio.to_thread(purge, domain, before=before)


# ── goal-driven recurring loop (the OODA pattern) ──────────────────────────────────────
# Composing a self-driving "run a tick every N toward a goal until its verifier passes" loop
# means stitching three subsystems by hand: a plugin goal verifier (ADR 0028), the goal
# controller (set a MONITOR goal, ADR 0030), and the scheduler (a recurring prompt, ADR
# 0003/0053). These helpers do it in one call so a plugin doesn't have to know the wiring.


def run_in_session(
    session_id: str,
    prompt: str,
    *,
    delay_seconds: float = 0.0,
    job_id: str | None = None,
) -> dict:
    """Enqueue ``prompt`` as a one-shot agent turn in ``session_id`` — non-blocking.

    This is the primitive behind "when a goal fires, prompt the agent." Call it from a
    goal ``on_achieved`` / ``on_failed`` hook (``registry.register_goal_hook(...)``) — or
    any plugin event handler — with a prompt built from the terminal ``GoalState`` (its
    ``condition`` / ``last_reason`` / ``last_evidence``), and the agent runs a follow-up
    turn (with that session's memory and full tool set) reacting to what just happened::

        async def on_achieved(goal):
            sdk.run_in_session(
                goal.session_id,
                f"The goal '{goal.condition}' just completed. Evidence: {goal.last_evidence}. "
                f"Write up a summary and open the follow-up PR.",
            )
        registry.register_goal_hook(on_achieved=on_achieved)

    Mechanics: it schedules a **one-shot** job (an ISO fire time, not a cron) into the
    session's context via the scheduler, so the turn runs on the normal fire path (the
    same loopback A2A call cron ticks use) and the caller returns immediately. It NEVER
    runs the turn inline, so it is safe to call from a goal hook / monitor tick without
    blocking it.

    Args:
        session_id: the A2A contextId to run the turn in (e.g. ``goal.session_id``).
        prompt: the message the agent processes as a turn.
        delay_seconds: fire at now + this delay (default 0 → the next poll tick, ~1s).
        job_id: a stable id so a re-call REPLACES the pending one-shot (idempotent).

    Returns ``{"ok", "job_id", "fires_at", "message"}``; ``ok=False`` with a readable
    message when the scheduler is unavailable or the inputs are bad.
    """
    scheduler = STATE.scheduler
    if scheduler is None:
        return {"ok": False, "message": "scheduler unavailable — cannot enqueue a turn"}
    if not (session_id or "").strip():
        return {"ok": False, "message": "session_id is required"}
    if not (prompt or "").strip():
        return {"ok": False, "message": "prompt is required"}
    from datetime import UTC, datetime, timedelta

    fires_at = (datetime.now(UTC) + timedelta(seconds=max(0.0, delay_seconds))).isoformat()
    # Idempotent replace: add_job RAISES on a duplicate id (it never overwrites), so drop
    # any pending one-shot with this id first — a re-call re-arms rather than colliding.
    if job_id:
        scheduler.cancel_job(job_id)
    try:
        job = scheduler.add_job(prompt, fires_at, job_id=job_id, context_id=session_id)
    except ValueError as e:
        return {"ok": False, "message": f"could not enqueue turn: {e}"}
    return {
        "ok": True,
        "job_id": job.id,
        "fires_at": fires_at,
        "message": f"turn enqueued in session {session_id!r} (fires {'now' if delay_seconds <= 0 else f'+{delay_seconds:g}s'})",
    }


def create_watch(
    *,
    condition: str,
    verifier: str,
    verifier_args: dict | None = None,
    watch_id: str | None = None,
    interval_s: float | None = None,
    deadline: float | None = None,
    stall_after: int | None = None,
    run_prompt: str = "",
    run_session: str = "",
) -> dict:
    """Register a WATCH from a plugin (ADR 0067): poll ``condition`` — ground-truthed by the
    plugin verifier named ``verifier`` (``"<plugin-id>:<name>"``) — on a cadence, and when it
    trips run ``run_prompt`` as a follow-up turn in ``run_session`` (via :func:`run_in_session`)
    and fire ``on_met`` hooks. Plugin-verifier only (like a set_goal-tool goal); hold as MANY as
    you like (unlike a monitor goal, which is one-per-session). Returns ``{"ok", "watch_id",
    "message"}`` — ok=False with a readable message if the subsystem is off or the verifier is
    rejected."""
    controller = STATE.watch_controller
    if controller is None:
        return {"ok": False, "watch_id": None, "message": "watch system unavailable (no watch_controller)"}
    ok, msg, watch = controller.create(
        condition=condition,
        verifier={"type": "plugin", "check": verifier, "args": verifier_args or {}},
        watch_id=watch_id,
        interval_s=interval_s,
        deadline=deadline,
        stall_after=stall_after,
        run_prompt=run_prompt,
        run_session=run_session,
        trusted=False,
    )
    return {"ok": ok, "watch_id": watch.id if watch else None, "message": msg}


def list_watches(prefix: str = "") -> list[dict]:
    """List the registered watches — each ``{"id", "condition", "status", "verifier"}`` —
    optionally filtered to ids starting with ``prefix``. This is the read half
    :func:`create_watch` was missing (#1638): a plugin that arms a watch *suite* under
    stable ids (``st-credits``, ``st-contract`` …) lists its own with
    ``list_watches("st-")`` to verify the suite or render it on a dashboard, and —
    paired with :func:`clear_watch` — reconciles on upgrade: clear the ids no longer in
    its spec set, then create/replace the rest (stable-id replace alone only heals specs
    that still exist; a renamed/dropped spec would keep polling forever). Returns ``[]``
    when the watch system is unavailable."""
    from copy import deepcopy

    controller = STATE.watch_controller
    if controller is None:
        return []
    return [
        # deepcopy: the verifier spec nests dicts (``args``) — hand the caller a snapshot,
        # not a mutable reference into the stored watch.
        {"id": w.id, "condition": w.condition, "status": w.status, "verifier": deepcopy(w.verifier)}
        for w in controller.list_watches()
        if w.id.startswith(prefix)
    ]


def clear_watch(watch_id: str) -> bool:
    """Remove the watch ``watch_id`` (it stops being polled; its state is deleted).
    Returns ``True`` if it existed, ``False`` when it didn't — or when the watch system
    is unavailable. The remove half of the :func:`list_watches` reconcile pattern."""
    controller = STATE.watch_controller
    if controller is None:
        return False
    return controller.clear(watch_id)


# ── background jobs (ADR 0050 spawn + ADR 0070 results pipeline) ───────────────────────
# Detached campaign work — "chart the frontier and report back" — is naturally a
# background subagent job, but the manager only had a tool-path consumer: a plugin had to
# reach into ``runtime.state.STATE.background_mgr`` and mirror what the ``task`` tool
# does. This thin pair closes that hole. A job spawned here rides the FULL ADR 0070
# results pipeline for free: push-resume nudge into ``origin_session`` at completion,
# KB-indexed report (``source_type="background_report"``), the console report card, and
# the ``GET /api/background/{id}`` route.


async def spawn_background(
    prompt: str,
    *,
    subagent_type: str,
    origin_session: str,
    label: str | None = None,
) -> dict:
    """Spawn a detached background subagent job (ADR 0050) and return immediately.

    The job runs as its own detached A2A turn under the ``subagent_type`` role; when it
    finishes, the ADR 0070 pipeline delivers the report — a push-resume nudge into
    ``origin_session``, the notified-gated ``<task-notification>`` drain, knowledge-store
    indexing, and the console report card. Poll in between with
    :func:`background_status` (e.g. to render campaign progress on a plugin dashboard).

    Args:
        prompt: detailed instructions for the background worker.
        subagent_type: which subagent role runs the job — one of the registered roster
            (:func:`subagent_types`), plugin-contributed subagents included.
        origin_session: the chat session the report drains back into (and gets the
            completion nudge). Required — a job with no origin has nowhere to report.
        label: short human description for the job card / report heading. Defaults to
            the first line of ``prompt`` (clipped).

    Returns ``{"ok", "task_id", "message"}`` — ``task_id`` is the ``bg-…`` job id (the
    handle for :func:`background_status`, cancel, and the by-id API route); ``ok=False``
    with a readable message when the background subsystem is off or the inputs are bad.
    """
    mgr = STATE.background_mgr
    if mgr is None:
        return {"ok": False, "task_id": None, "message": "background subsystem unavailable (no background_mgr)"}
    if not (prompt or "").strip():
        return {"ok": False, "task_id": None, "message": "prompt is required"}
    if not (origin_session or "").strip():
        return {"ok": False, "task_id": None, "message": "origin_session is required"}
    from graph.subagents.config import SUBAGENT_REGISTRY

    if subagent_type not in SUBAGENT_REGISTRY:
        available = ", ".join(sorted(SUBAGENT_REGISTRY)) or "(none configured)"
        return {"ok": False, "task_id": None, "message": f"unknown subagent {subagent_type!r} — available: {available}"}
    description = (label or "").strip() or prompt.strip().splitlines()[0][:80]
    task_id = await mgr.spawn(
        origin_session=origin_session,
        subagent_type=subagent_type,
        description=description,
        prompt=prompt,
    )
    return {
        "ok": True,
        "task_id": task_id,
        "message": (
            f"background job {task_id} spawned ({subagent_type}: {description}) — it runs detached; "
            f"the report is delivered to session {origin_session!r} on completion"
        ),
    }


def background_status(task_id: str) -> dict:
    """Look up a background job by its ``bg-…`` id — the status-query companion to
    :func:`spawn_background`, so a plugin can render campaign progress on its own
    surface instead of being blind between launch and the ADR 0070 completion nudge.

    Returns ``{"ok", "task_id", "status", "subagent_type", "description", "created_at",
    "completed_at", "message"}`` plus — only once the job is terminal
    (completed/failed/canceled) — ``"report"`` with the full result text. An unknown id
    (or the subsystem being off) returns ``ok=False`` with ``status="unknown"`` and a
    readable message. Reads the durable jobs store directly (cheap local SQLite).
    """
    mgr = STATE.background_mgr
    if mgr is None:
        return {"ok": False, "status": "unknown", "message": "background subsystem unavailable (no background_mgr)"}
    job = mgr.store.get((task_id or "").strip())
    if job is None:
        return {"ok": False, "status": "unknown", "message": f"no background job {task_id!r}"}
    out = {
        "ok": True,
        "task_id": job.id,
        "status": job.status,
        "subagent_type": job.subagent_type,
        "description": job.description,
        "created_at": job.created_at,
        "completed_at": job.completed_at,
        "message": f"background job {job.id} is {job.status}",
    }
    if job.status != "running":
        out["report"] = job.result
    return out


# ── reactive rules (ADR 0039 events → one-shot turns) ──────────────────────────────────
# The canonical reactive composition is ``registry.on(topic, handler)`` →
# :func:`run_in_session` — and every plugin that wants "when X happens, have the agent
# react" writes the same glue: guard for a missing host, build the prompt from the event
# payload, pick an idempotent job_id, debounce bursts so ten events don't enqueue ten
# turns. ``react_on`` is that glue, once (#1633). Pure composition of
# ``EventBus.subscribe_handler`` (via the plugin host seam) + ``run_in_session`` —
# no new persistent state.


def react_on(
    topic: str,
    *,
    prompt: Callable[[dict], str | None],
    job_id: str,
    session: str = ACTIVITY_CONTEXT,
    debounce_s: float = 0.0,
) -> Callable[[], None]:
    """When a bus event matching ``topic`` fires, enqueue a follow-up agent turn.

    ``prompt`` is called with the full event payload (``{"event", "data", "seq"}``)
    at delivery time; return the turn's prompt text, or ``None``/empty to **skip**
    that event (cheap filtering). The turn is enqueued via :func:`run_in_session`
    with ``job_id``, so a rule re-fires idempotently (a pending turn is REPLACED,
    never duplicated)::

        unsub = sdk.react_on(
            "spacetraders.opportunity",
            prompt=lambda ev: f"A {ev['data']['margin']}% route appeared. Evaluate it.",
            job_id="spacetraders-opportunity",
            debounce_s=30,
        )

    Args:
        topic: bus topic pattern (``*`` = one segment, ``#`` = tail — any namespace;
            subscribing is read-only, like ``registry.on``).
        prompt: ``(payload) -> str | None`` — the prompt builder / filter.
        job_id: stable id for the enqueued turn (``run_in_session``'s
            idempotent-replace key). Required — it's what keeps a chatty rule from
            stacking turns.
        session: the session the turn runs in; defaults to the durable Activity
            thread (``ACTIVITY_CONTEXT``).
        debounce_s: > 0 coalesces a burst into ONE turn — trailing-edge: the timer
            re-arms on every qualifying event, fires ``debounce_s`` after the LAST
            one, and that last event's prompt text wins. Skipped events (``prompt``
            returned ``None``/empty) neither fire nor extend the window. Note a
            sustained stream arriving faster than ``debounce_s`` keeps deferring
            the turn (classic debounce). Thread-safe — the bus may deliver from
            worker threads.

    Returns an **unsubscribe** callable (mirroring ``registry.on``'s seam): it stops
    delivery and cancels any pending debounce timer. When no host bus is wired
    (tests, headless), logs a warning and returns a no-op unsubscribe.
    """
    import threading

    from graph.plugins.host import HOST

    if not callable(prompt):
        raise TypeError("react_on: prompt must be a callable (event payload) -> str | None")
    if not (job_id or "").strip():
        raise ValueError("react_on: a stable job_id is required (the idempotent-replace key)")
    subscribe = HOST.on
    if subscribe is None:
        log.warning("[sdk] react_on(%r) dropped — no event bus wired (non-server context)", topic)
        return lambda: None

    # Debounce state — guarded by a lock because the bus publish path is threadsafe
    # (handlers may be delivered from worker threads, e.g. a sync middleware hook).
    lock = threading.Lock()
    pending: dict[str, Any] = {"timer": None, "text": None}

    def _enqueue(text: str) -> None:
        res = run_in_session(session, text, job_id=job_id)
        if not res.get("ok"):
            log.warning("[sdk] react_on(%r): could not enqueue turn — %s", topic, res.get("message"))

    def _fire() -> None:  # timer thread — never let an exception die silently there
        with lock:
            text = pending["text"]
            pending["timer"] = None
            pending["text"] = None
        if not text:
            return
        try:
            _enqueue(text)
        except Exception:  # noqa: BLE001
            log.exception("[sdk] react_on(%r): debounced enqueue failed", topic)

    def _handler(payload: dict) -> None:
        text = prompt(payload)
        if not (text or "").strip():
            return  # filtered — a skipped event doesn't extend the debounce window
        if debounce_s <= 0:
            _enqueue(text)
            return
        with lock:
            pending["text"] = text  # last event in the burst wins
            timer = pending["timer"]
            if timer is not None:
                timer.cancel()
            timer = threading.Timer(debounce_s, _fire)
            timer.daemon = True
            pending["timer"] = timer
            timer.start()

    unsubscribe = subscribe(topic, _handler)

    def _unsubscribe() -> None:
        if callable(unsubscribe):
            unsubscribe()
        with lock:
            timer = pending["timer"]
            pending["timer"] = None
            pending["text"] = None
        if timer is not None:
            timer.cancel()

    return _unsubscribe


# ── plugin-owned recurring jobs (#1642) ─────────────────────────────────────────────
# run_in_session covers one-shot turns; a RECURRING cadence (spacetraders' daily
# strategist tick) had no plugin-owned path — the operator wired a cron job manually, and
# nothing tied it to the plugin, so uninstall/disable left an orphan job firing prompts
# about a plugin that's gone. These helpers namespace the job id `plugin:<plugin_id>:<job_id>`
# so the lifecycle hooks (loader disable sweep + installer uninstall) can cancel exactly the
# plugin's jobs. The ADR 0004 `agent_name` scoping is untouched — the scheduler still
# namespaces per instance underneath; this adds a *plugin* ownership dimension on the id.

_PLUGIN_JOB_PREFIX = "plugin:"


def plugin_job_prefix(plugin_id: str) -> str:
    """The id prefix every scheduler job owned by ``plugin_id`` carries."""
    return f"{_PLUGIN_JOB_PREFIX}{plugin_id}:"


def schedule_recurring(
    prompt: str,
    cron: str,
    *,
    plugin_id: str,
    job_id: str,
    session: str = "",
    timezone: str | None = None,
) -> dict:
    """Schedule ``prompt`` as a RECURRING agent turn on a ``cron`` cadence, owned by
    ``plugin_id``.

    Thin over ``STATE.scheduler.add_job`` with the id namespaced
    ``plugin:<plugin_id>:<job_id>`` — that ownership tag is what lets the host cancel
    the plugin's jobs on disable/uninstall (and :func:`cancel_scheduled` /
    :func:`cancel_plugin_jobs` find them). Idempotent by id: re-calling with the same
    ``job_id`` REPLACES the pending job, so a plugin re-arms its cadence in
    ``register()`` (or when a cadence knob changes) without colliding.

    There is no ambient plugin identity in the SDK (functions are plain imports, not
    registry-bound), so ``plugin_id`` is an explicit required kwarg — pass
    ``registry.plugin_id``. Note: a *disable* cancels the plugin's jobs; re-enabling
    relies on the plugin re-arming in ``register()``, which runs after the scheduler
    is wired on both boot and hot-reload.

    Args:
        prompt: the message the agent processes each fire.
        cron: a 5-field cron expression (e.g. ``"0 9 * * *"``). One-shot turns belong
            to :func:`run_in_session`, so an ISO datetime is rejected here.
        plugin_id: the owning plugin's id (``registry.plugin_id``).
        job_id: a stable plugin-local id for this cadence (e.g. ``"strategist-tick"``).
        session: the A2A contextId to fire into; empty → the durable Activity thread
            (the default for scheduled work).
        timezone: IANA name the cron is evaluated in (None = UTC).

    Returns ``{"ok", "job_id", "next_fire", "message"}`` — ``job_id`` is the full
    namespaced id; ``ok=False`` with a readable message when the scheduler is
    unavailable or the inputs are bad.
    """
    from scheduler.interface import is_cron

    scheduler = STATE.scheduler
    if scheduler is None:
        return {"ok": False, "job_id": None, "message": "scheduler unavailable — cannot schedule a recurring job"}
    plugin_id = (plugin_id or "").strip()
    if not plugin_id or ":" in plugin_id:
        return {"ok": False, "job_id": None, "message": "plugin_id is required (and must not contain ':')"}
    if not (job_id or "").strip():
        return {"ok": False, "job_id": None, "message": "job_id is required"}
    if not (prompt or "").strip():
        return {"ok": False, "job_id": None, "message": "prompt is required"}
    cron = (cron or "").strip()
    if not is_cron(cron):
        return {
            "ok": False,
            "job_id": None,
            "message": f"schedule {cron!r} is not a 5-field cron expression — "
            "for a one-shot turn use run_in_session",
        }
    full_id = f"{plugin_job_prefix(plugin_id)}{job_id.strip()}"
    # Idempotent replace: add_job RAISES on a duplicate id, so drop any pending job
    # with this id first — a re-arm (register() re-run, cadence knob change) updates
    # rather than collides (mirrors run_in_session).
    scheduler.cancel_job(full_id)
    try:
        job = scheduler.add_job(
            prompt, cron, job_id=full_id, timezone=timezone, context_id=(session or "").strip() or None
        )
    except ValueError as e:
        return {"ok": False, "job_id": full_id, "message": f"could not schedule: {e}"}
    return {
        "ok": True,
        "job_id": job.id,
        "next_fire": getattr(job, "next_fire", None),
        "message": f"recurring job {job.id!r} scheduled ({cron!r})",
    }


def cancel_scheduled(job_id: str, *, plugin_id: str) -> bool:
    """Cancel the plugin-owned recurring job ``job_id`` (the same plugin-local id passed
    to :func:`schedule_recurring` — namespacing is applied here, never by the caller).
    Returns ``True`` if a job was removed; ``False`` when there was none — or when the
    scheduler is unavailable."""
    scheduler = STATE.scheduler
    if scheduler is None:
        return False
    plugin_id = (plugin_id or "").strip()
    # Same ':' guard as schedule_recurring — plugin "a:b" must not reach into "a"'s namespace.
    if not plugin_id or ":" in plugin_id or not (job_id or "").strip():
        return False
    return bool(scheduler.cancel_job(f"{plugin_job_prefix(plugin_id)}{job_id.strip()}"))


def cancel_plugin_jobs(plugin_id: str) -> int:
    """Cancel EVERY scheduler job owned by ``plugin_id`` (ids ``plugin:<plugin_id>:*``).
    Returns how many were cancelled (0 when the scheduler is unavailable).

    This is the lifecycle hygiene hook (#1642): the loader sweeps a disabled plugin's
    jobs on (re)load and the installer sweeps on uninstall, so an orphan cadence can't
    keep firing prompts about a plugin that's gone. Only jobs under this instance's
    ``agent_name`` are visible (``list_jobs`` filters), so the ADR 0004 scoping holds.
    Also useful to a plugin that wants to tear down its whole cadence at once."""
    scheduler = STATE.scheduler
    plugin_id = (plugin_id or "").strip()
    if scheduler is None or not plugin_id:
        return 0
    prefix = plugin_job_prefix(plugin_id)
    cancelled = 0
    for job in scheduler.list_jobs():
        jid = getattr(job, "id", "") or ""
        if jid.startswith(prefix) and scheduler.cancel_job(jid):
            cancelled += 1
    return cancelled


# ── plugin metric timeseries (#1632) ────────────────────────────────────────────────
# History-dependent watch verifiers (ADR 0067: drawdown-vs-high-water, flatline
# detection) and dashboard sparklines need PRIOR values — but a verifier only sees live
# state and sdk.telemetry() is a point-in-time envelope, so every plugin with a
# background engine hand-rolled its own persistence (a knobs JSON here, a private
# sqlite there). These three calls are that store, once: small named numeric series,
# namespaced `<plugin_id>:<name>`, SQLite-backed in the instance dir
# (observability/metrics_store.py), retention-capped per series (90d / 10k points).


def _metric_series(name: str, plugin_id: str) -> str | None:
    """The namespaced metric series key ``<plugin_id>:<name>``, or ``None`` on bad
    input. Same ``':'`` guard as :func:`schedule_recurring` — plugin ``a:b`` must not
    be able to reach into plugin ``a``'s namespace. (The SDK has no ambient plugin
    identity — functions are plain imports, not registry-bound — so ``plugin_id`` is an
    explicit required kwarg on every metric call; pass ``registry.plugin_id``.)"""
    plugin_id = (plugin_id or "").strip()
    name = (name or "").strip()
    if not plugin_id or ":" in plugin_id or not name:
        return None
    return f"{plugin_id}:{name}"


def record_metric(name: str, value: float, *, ts: float | None = None, plugin_id: str) -> dict:
    """Append one sample to the plugin metric timeseries ``name`` (#1632).

    Small named numeric series — treasury, net worth, fleet size — are what
    history-dependent watch verifiers (ADR 0067 drawdown-vs-high-water, flatline
    detection) and dashboard sparklines need, and a verifier can only see *live* state
    (``sdk.telemetry()`` is point-in-time). Series are namespaced
    ``<plugin_id>:<name>``, SQLite-backed in the instance dir
    (``observability/metrics_store.py``), and retention-capped per series (90 days /
    10k points, trimmed on write) — record freely from an engine tick.

    Args:
        name: the plugin-local series name (e.g. ``"credits"``). Namespacing is
            applied here, never by the caller.
        value: the sample — any real number (NaN/inf are rejected; they poison
            drawdown math downstream).
        ts: Unix epoch seconds for the sample; ``None`` → now. Useful for backfill
            and deterministic tests.
        plugin_id: the owning plugin's id (``registry.plugin_id``) — explicit, like
            :func:`schedule_recurring`; ``':'`` is rejected.

    Returns ``{"ok", "series", "message"}`` — ``ok=False`` with a readable message
    when the store is unavailable (non-server context) or the inputs are bad; a write
    failure never raises into an engine loop.
    """
    import math

    store = getattr(STATE, "metrics_store", None)
    if store is None:
        return {"ok": False, "series": None, "message": "metrics store unavailable (non-server context)"}
    series = _metric_series(name, plugin_id)
    if series is None:
        return {
            "ok": False,
            "series": None,
            "message": "name and plugin_id are required (and plugin_id must not contain ':')",
        }
    try:
        value = float(value)
    except (TypeError, ValueError):
        return {"ok": False, "series": series, "message": f"value must be numeric, got {value!r}"}
    if not math.isfinite(value):
        return {"ok": False, "series": series, "message": f"value must be finite, got {value!r}"}
    if ts is not None:
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            return {"ok": False, "series": series, "message": f"ts must be Unix epoch seconds, got {ts!r}"}
        if not math.isfinite(ts):
            return {"ok": False, "series": series, "message": f"ts must be finite, got {ts!r}"}
    try:
        store.record(series, value, ts=ts)
    except Exception as e:  # noqa: BLE001 — best-effort: a disk hiccup must not kill an engine tick
        log.exception("[sdk] record_metric(%r) failed", series)
        return {"ok": False, "series": series, "message": f"could not record: {e}"}
    return {"ok": True, "series": series, "message": f"recorded {series}={value:g}"}


def metric_history(
    name: str, *, since: float | None = None, limit: int = 500, plugin_id: str
) -> list[tuple[float, float]]:
    """The newest ``limit`` samples of the plugin metric series ``name`` — at/after
    ``since`` (Unix epoch seconds) when given — returned **oldest→newest** as
    ``(ts, value)`` tuples: chronological order, ready for verifier math
    (``high_water = max(v for _, v in points)``) or a sparkline. Returns ``[]`` when
    the store is unavailable, ``name``/``plugin_id`` are invalid, or the series has no
    samples. Same namespacing + ``plugin_id`` contract as :func:`record_metric`."""
    store = getattr(STATE, "metrics_store", None)
    series = _metric_series(name, plugin_id)
    if store is None or series is None:
        return []
    return store.history(series, since=since, limit=limit)


def metric_last(name: str, *, plugin_id: str) -> tuple[float, float] | None:
    """The most recent ``(ts, value)`` of the plugin metric series ``name``, or
    ``None`` when there is no sample (never recorded / fully aged out / store
    unavailable). The cheap read for "what did I last see?" checks — e.g. a verifier
    comparing the live reading against the last recorded one. Same namespacing +
    ``plugin_id`` contract as :func:`record_metric`."""
    store = getattr(STATE, "metrics_store", None)
    series = _metric_series(name, plugin_id)
    if store is None or series is None:
        return None
    return store.last(series)
