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
from typing import Any

from runtime.state import STATE

# Re-export the supervised background-task helper as part of the consumption surface, so a
# plugin writes `from graph.sdk import supervise` for a self-perpetuating, watchdog-backed
# engine instead of hand-rolling task/restart machinery (graph/supervisor.py is host-free).
from graph.supervisor import Supervisor, supervise  # noqa: F401

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
    """
    from graph.agent import run_manual_subagent

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
# two thin accessors close that: the coding loop reads distilled lessons to inject into
# a coder's prompt; the loop-retro writes recurring failures back as searchable chunks.
# Both degrade to a no-op ([] / None) when no store is configured, and run the
# (HTTP-embedding) store call off the event loop.


async def knowledge_search(query: str, *, k: int = 5, domain: str | None = None) -> list[dict]:
    """Search the agent's knowledge graph (hybrid FTS5 + embeddings); return the top-``k``
    matching chunks (each a dict with ``preview``/``content``, ``domain``, ``score`` …),
    or ``[]`` when no store is configured. ``domain`` scopes to one bucket
    (e.g. ``"loop-lessons"``)."""
    store = getattr(STATE, "knowledge_store", None)
    if store is None:
        return []
    return await asyncio.to_thread(store.search, query, k=k, domain=domain)


async def knowledge_add(content: str, *, domain: str = "general", heading: str | None = None) -> int | None:
    """Add one chunk to the agent's knowledge graph; return its id, or ``None`` when no
    store is configured / it was a no-op. ``domain`` is the bucket, ``heading`` an
    optional title — e.g. ``knowledge_add(lesson, domain="loop-lessons", heading=cls)``."""
    store = getattr(STATE, "knowledge_store", None)
    if store is None:
        return None
    return await asyncio.to_thread(store.add_chunk, content, domain=domain, heading=heading)


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
