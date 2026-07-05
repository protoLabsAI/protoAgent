"""System lifecycle dispatch (ADR 0074, extends ADR 0039).

One entry point — :func:`fire` — that every emit site in ``server/`` calls when a
system lifecycle event happens. It does three things, each error-isolated so a bad
hook / webhook / prompt can never break boot or a turn:

1. **Broadcast on the event bus** (ADR 0039) under a dot-namespaced topic, so any
   plugin/console subscriber can react with no wiring.
2. **Fire plugin hooks** (``fire_lifecycle_hook`` — ``registry.register_lifecycle_hook``).
3. **Run config reactions** — the operator-facing half: ``lifecycle_hooks`` entries in
   ``langgraph-config.yaml`` that enqueue a follow-up prompt (``run_in_session``) or POST a
   webhook. **Opt-in**: with no config, nothing fires beyond the bus broadcast.

This module lives in ``graph/`` and imports only ``graph.sdk`` / ``graph.plugins.host`` /
``graph.lifecycle.hooks`` / ``runtime`` (via the SDK) / ``httpx`` — never ``server`` or
``operator_api`` (the import-layering contract). The server emit sites call in.
"""

from __future__ import annotations

import logging

from graph.lifecycle.hooks import fire_lifecycle_hook

log = logging.getLogger(__name__)

# Config-event name (``app_loaded``) → dot-namespaced bus topic (``app.loaded``). The
# ONE place the two naming conventions are bridged: config + slash command speak
# ``app_loaded``; the bus (ADR 0039) speaks ``app.loaded``.
TOPICS: dict[str, str] = {
    "app_loaded": "app.loaded",
    "agent_active": "agent.active",
    "system_wake": "system.wake",
}
# The canonical event names, in emit order. ``system_wake`` is reserved (the bus/seam/
# config accept it now; the desktop emit lands in a follow-up PR).
EVENTS: tuple[str, ...] = ("app_loaded", "agent_active", "system_wake")

# agent.active debounce: a turn fires the chat path constantly, so only emit on the
# FIRST turn since boot or the first turn after this much idle time.
IDLE_THRESHOLD_S = 300.0


def should_emit_active(
    now: float, last_activity_ts: float | None, *, threshold: float = IDLE_THRESHOLD_S
) -> tuple[bool, float, str]:
    """Pure debounce for ``agent.active``. Emit on the FIRST turn since boot
    (``last_activity_ts is None``) or the first turn after an idle gap ≥ ``threshold``.

    Returns ``(emit, idle_seconds, previous_state)`` where ``previous_state`` is
    ``"boot"`` for the first turn or ``"idle"`` otherwise. Pure + side-effect free so
    it's unit-testable without a clock or STATE."""
    if last_activity_ts is None:
        return True, 0.0, "boot"
    idle = max(0.0, now - last_activity_ts)
    return (idle >= threshold), idle, "idle"


def config_reactions(event: str) -> list[dict]:
    """The configured ``lifecycle_hooks`` entries whose ``event`` matches (opt-in;
    empty by default ⇒ nothing fires). Reads the live ``LangGraphConfig`` via the SDK."""
    from graph.sdk import config as _config

    cfg = _config()
    hooks = getattr(cfg, "lifecycle_hooks", None) or []
    return [h for h in hooks if isinstance(h, dict) and h.get("event") == event]


def _publish(event: str, payload: dict) -> None:
    """Broadcast the event on the bus (ADR 0039). Best-effort — a bus hiccup (or no bus
    wired, e.g. headless/tests) must never break the lifecycle path."""
    topic = TOPICS.get(event, event)
    try:
        from graph.plugins.host import HOST

        if HOST.publish:
            HOST.publish(topic, payload)
    except Exception:  # noqa: BLE001
        log.debug("[lifecycle] %s bus emit failed", topic, exc_info=True)


async def _post_webhook(url: str, event: str, payload: dict) -> None:
    """POST the event to a webhook (async, short timeout). Isolated — a slow/broken
    endpoint must never stall boot or a turn."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json={"event": TOPICS.get(event, event), "data": payload})
    except Exception:  # noqa: BLE001 — a bad webhook is the operator's problem, not ours
        log.warning("[lifecycle] %s webhook POST to %s failed", event, url, exc_info=True)


async def _run_reaction(reaction: dict, event: str, payload: dict) -> None:
    """Run one configured reaction — a ``prompt`` (enqueue a follow-up turn via
    ``run_in_session``) and/or a ``webhook`` (POST). Both are optional; both are
    error-isolated."""
    prompt = str(reaction.get("prompt") or "").strip()
    webhook = str(reaction.get("webhook") or "").strip()
    # Prompt reactions run in the configured session, else the event's own session
    # (agent.active carries one; app.loaded/system.wake don't, so those need `session`).
    session = str(reaction.get("session") or payload.get("session_id") or "").strip()
    if prompt:
        try:
            from graph.sdk import run_in_session

            if session:
                run_in_session(session, prompt)
            else:
                log.warning(
                    "[lifecycle] %s prompt reaction has no session (set `session:` on the "
                    "lifecycle_hooks entry) — skipped",
                    event,
                )
        except Exception:  # noqa: BLE001 — a bad reaction must not break the lifecycle path
            log.exception("[lifecycle] %s prompt reaction failed", event)
    if webhook:
        await _post_webhook(webhook, event, payload)


_EVENT_BLURB = {
    "app_loaded": "boot finished — graph, scheduler, surfaces + fleet autostart up",
    "agent_active": "the agent went idle → active (first turn after a quiet gap, debounced)",
    "system_wake": "reserved — the desktop shell woke (emitted in a follow-up PR)",
}


def _reaction_summary(reaction: dict) -> str:
    parts = []
    if str(reaction.get("prompt") or "").strip():
        sess = str(reaction.get("session") or "").strip()
        parts.append(f"prompt→{sess}" if sess else "prompt (own session)")
    if str(reaction.get("webhook") or "").strip():
        parts.append(f"webhook {reaction['webhook']}")
    return " · ".join(parts) or "(no prompt/webhook)"


def describe() -> str:
    """A read-only, human-readable summary of the three lifecycle events, their configured
    ``lifecycle_hooks`` reactions, and registered plugin hooks — backs the ``/lifecycle``
    chat command (AC#3). Listing only; the config file is the source of truth."""
    from graph.lifecycle.hooks import lifecycle_hooks as _plugin_hooks

    lines = [
        "**System lifecycle events** (ADR 0074) — broadcast on the event bus (ADR 0039); "
        "reactions are opt-in via the `lifecycle_hooks:` config list.",
        "",
    ]
    hooks_by_event: dict[str, list[str]] = {e: [] for e in EVENTS}
    for hook in _plugin_hooks():
        pid = hook.get("plugin_id") or "?"
        for event in EVENTS:
            if hook.get(f"on_{event}"):
                hooks_by_event[event].append(pid)
    for event in EVENTS:
        topic = TOPICS[event]
        lines.append(f"- `{topic}` — {_EVENT_BLURB[event]}")
        reactions = config_reactions(event)
        if reactions:
            for r in reactions:
                lines.append(f"    - config reaction: {_reaction_summary(r)}")
        plugins = hooks_by_event.get(event) or []
        if plugins:
            lines.append(f"    - plugin hooks: {', '.join(plugins)}")
        if not reactions and not plugins:
            lines.append("    - no reactions configured")
    return "\n".join(lines)


async def fire(event: str, payload: dict) -> None:
    """Emit a system lifecycle ``event``: broadcast on the bus, fire plugin hooks, run
    configured reactions. Every stage is error-isolated. ``event`` is a canonical name
    (``app_loaded`` / ``agent_active`` / ``system_wake``); ``payload`` carries at least
    ``ts`` + ``previous_state``."""
    _publish(event, payload)
    try:
        await fire_lifecycle_hook(event, payload)
    except Exception:  # noqa: BLE001 — defense in depth (fire_lifecycle_hook already isolates)
        log.exception("[lifecycle] %s hook dispatch failed", event)
    for reaction in config_reactions(event):
        await _run_reaction(reaction, event, payload)
