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

import re
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


async def complete(
    prompt: str, *, system: str | None = None, model_name: str | None = None
) -> str:
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


# ── goal-driven recurring loop (the OODA pattern) ──────────────────────────────────────
# Composing a self-driving "run a tick every N toward a goal until its verifier passes" loop
# means stitching three subsystems by hand: a plugin goal verifier (ADR 0028), the goal
# controller (set a MONITOR goal, ADR 0030), and the scheduler (a recurring prompt, ADR
# 0003/0053). These helpers do it in one call so a plugin doesn't have to know the wiring.

_DURATION = re.compile(r"^\s*(\d+)\s*([mhd])\s*$", re.IGNORECASE)


def _to_cron(every: str) -> str:
    """A 5-field cron passes through; a duration shorthand (``"15m"`` / ``"2h"`` / ``"1d"``)
    is converted to cron. Raises ValueError on anything else."""
    from scheduler.interface import is_cron

    s = (every or "").strip()
    if is_cron(s):
        return s
    m = _DURATION.match(s)
    if not m:
        raise ValueError(f"{every!r} is not a 5-field cron or a duration like '15m'/'2h'/'1d'")
    n, unit = int(m.group(1)), m.group(2).lower()
    if n < 1:
        raise ValueError("duration must be >= 1")
    if unit == "m":
        if n > 59:
            raise ValueError("minutes must be 1–59 (use '1h' for 60)")
        return f"*/{n} * * * *"
    if unit == "h":
        if n > 23:
            raise ValueError("hours must be 1–23 (use '1d' for 24)")
        return f"0 */{n} * * *"
    if n > 31:
        raise ValueError("days must be 1–31")
    return f"0 0 */{n} * *"


def start_goal_loop(*, session_id: str, goal: str, verifier: str, every: str, prompt: str,
                    verifier_args: dict | None = None, mode: str = "monitor",
                    timezone: str | None = None, no_progress_limit: int | None = None,
                    max_iterations: int | None = None, job_id: str | None = None) -> dict:
    """Wire a goal-driven recurring loop in ONE call (the OODA / self-improving pattern):
    set a goal verified by a plugin verifier, and schedule a recurring prompt that drives it
    until the verifier passes — at which point the goal's ``on_achieved`` hook winds the work
    down.

    Register the pieces at ``register()`` time first: the verifier
    (``registry.register_goal_verifier(verifier, fn)``) and usually the hook
    (``registry.register_goal_hook(on_achieved=…)``). Then call this from a tool, passing
    ``session_id`` from your tool's ``InjectedState`` — the goal + the tick are scoped to that
    session (the tick fires back INTO it via ``context_id``, so it drives the right goal).

    Args:
        session_id: the session to scope the goal + tick to (from InjectedState).
        goal: the goal condition text (e.g. "reach 1,000,000 credits").
        verifier: the registered plugin verifier name, ``"<plugin-id>:<name>"``.
        every: how often the tick fires — a 5-field cron (``"0 */6 * * *"``) or a duration
            shorthand ``"15m"`` / ``"2h"`` / ``"1d"``.
        prompt: the recurring tick prompt (e.g. "Run the manage-the-fleet OODA tick …").
        verifier_args: declarative args for the verifier (e.g. ``{"min": 1000000}``).
        mode: ``"monitor"`` (default — an external engine drives the metric; the agent isn't
            re-invoked to *drive* the goal, only the scheduled tick runs) or ``"drive"``.
        timezone: IANA tz for the schedule (e.g. ``"America/Chicago"``); UTC if omitted.
        no_progress_limit, max_iterations: passed through to the goal.
        job_id: a stable id for the tick job (so a re-call replaces it).

    Returns ``{"ok", "goal", "job_id", "schedule", "message"}``; ``ok=False`` with a readable
    message if the goal/scheduler subsystems are absent or the inputs are bad.
    """
    controller = STATE.goal_controller
    scheduler = STATE.scheduler
    if controller is None:
        return {"ok": False, "message": "goal system unavailable (no goal_controller)"}
    if scheduler is None:
        return {"ok": False, "message": "scheduler unavailable"}
    try:
        schedule = _to_cron(every)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    spec = {"type": "plugin", "check": verifier, "args": verifier_args or {}}
    ok, msg = controller.set_goal_safe(session_id, goal, spec, max_iterations=max_iterations,
                                       no_progress_limit=no_progress_limit, mode=mode)
    if not ok:
        return {"ok": False, "message": f"goal not set: {msg}"}
    try:
        job = scheduler.add_job(prompt, schedule, job_id=job_id, timezone=timezone,
                                context_id=session_id)  # tick runs IN the goal's session
    except ValueError as e:
        controller.store.clear(session_id)  # roll back the goal if scheduling failed
        return {"ok": False, "message": f"bad schedule {every!r}: {e}"}
    return {"ok": True, "goal": goal, "job_id": job.id, "schedule": schedule,
            "message": f"goal loop started — {goal} · tick {schedule} · {msg}"}


def stop_goal_loop(*, session_id: str, job_id: str | None = None) -> dict:
    """Tear down a goal loop: clear the goal for ``session_id`` and cancel its tick job
    (call this from an ``on_achieved`` hook, a stop tool, or when winding down)."""
    cleared = False
    if STATE.goal_controller is not None:
        cleared = STATE.goal_controller.store.clear(session_id)
    cancelled = False
    if job_id and STATE.scheduler is not None:
        cancelled = STATE.scheduler.cancel_job(job_id)
    return {"ok": True, "goal_cleared": cleared, "job_cancelled": cancelled}
