"""Plugin-contributed work providers — the extension point for ADR 0079's *Observe* step.

The agent's durable working state is ``{active goal + plan · open tasks · live watches ·
pending schedules}``, injected on every turn by
``graph.middleware.knowledge.KnowledgeMiddleware._working_state_block`` so the agent
OBSERVES its own commitments instead of having to poll for them.

Those four sections read four CORE stores, and there was no way in. A plugin owning a work
queue of its own — a project board, a review lane, an inbox — was therefore invisible to
the agent's working state no matter how central that queue is to what the agent actually
does. The failure this fixes was observed in production: an agent whose entire job lives on
a plugin-owned board reported itself idle for hours while one of its own cards sat stalled,
because the block it is taught to treat as "your live commitments" could not see the board.

A provider closes that by **projection, not replication**: the plugin stays the system of
record and the host reads a bounded snapshot at turn-composition time. Nothing is copied
into a core store, so there is no second source of truth to reconcile and no sync to drift.

**A provider must be cheap and non-blocking.** It is called inline on EVERY turn. Return an
in-memory snapshot — never shell out, hit a network, or take a lock. A provider slower than
``SLOW_PROVIDER_S`` is logged once per name, the same posture ``graph.plugins.host`` takes
for a slow lifecycle stage. A provider that raises is skipped and never breaks the turn.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# A provider crossing this drags EVERY turn's context assembly, so say so — once per name,
# because a slow provider is slow on every turn and would otherwise flood the log.
SLOW_PROVIDER_S = 0.25

# Longest a single rendered line may run before it is trimmed. Keeps one pathological title
# from eating the working-state budget the four core sections share.
_LINE_CAP = 160

# name -> () -> list[dict]. Replaced wholesale by ``set_plugin_work_providers`` at build and
# on every config reload, exactly like the plugin verifier registry (#1752: a stale registry
# is worse than an empty one — it silently serves the PRE-reload mapping).
_PROVIDERS: dict = {}

# name -> {"plugin_id", "label"} — parallel to _PROVIDERS rather than folded into it so the
# value of _PROVIDERS stays THE callable, matching every other registry in this codebase.
_PROVIDER_META: dict[str, dict] = {}

# (name, kind) already warned about, so a recurring problem logs once per PROBLEM rather
# than once per provider. Keying on the name alone silently swallowed later, different
# failures: a provider warned once for being SLOW was thereafter permanently mute about
# RAISING, because both branches consulted the same set — and an exception is the one thing
# you most need to hear about.
_WARNED: set[tuple[str, str]] = set()


def _warn_once(name: str, kind: str, msg: str, *args) -> None:
    """Log this provider's problem the first time THIS KIND of problem is seen for it."""
    if (name, kind) in _WARNED:
        return
    _WARNED.add((name, kind))
    log.warning(msg, *args)


def set_plugin_work_providers(mapping: dict | None, meta: dict | None = None) -> None:
    """Replace the registered work-provider set (called at build + on config reload).

    Wholesale replacement, not a merge: a reload that drops a plugin must drop its provider
    too, or the working state keeps rendering a queue nothing is maintaining any more."""
    global _PROVIDERS, _PROVIDER_META
    _PROVIDERS = dict(mapping or {})
    _PROVIDER_META = dict(meta or {})
    # A provider that was slow or malformed under the OLD mapping may be fixed under the new
    # one — reset the dedup so a still-broken provider warns again instead of going quiet.
    _WARNED.clear()


def work_provider_names() -> list[str]:
    """Registered provider names, sorted — the introspection half, for a status surface."""
    return sorted(_PROVIDERS)


def _render(item, name: str) -> str:
    """One work item → one working-state line, or "" if the item is unusable.

    Accepts a plain string (rendered verbatim) or a dict of ``{id, title, state, hint}`` —
    every field optional, because a provider should not have to synthesize an id it does not
    have. Mirrors the OPEN TASKS line shape so the agent reads one vocabulary, not two."""
    if isinstance(item, str):
        line = item.strip()
        return f"- {line[:_LINE_CAP]}" if line else ""
    if not isinstance(item, dict):
        return ""
    state = str(item.get("state") or "").strip()
    fid = str(item.get("id") or "").strip()
    title = str(item.get("title") or "").strip()
    hint = str(item.get("hint") or "").strip()
    if not (fid or title):
        return ""  # nothing addressable and nothing to read — not worth a line
    parts = []
    if state:
        parts.append(f"[{state}]")
    if fid:
        parts.append(fid)
    if title:
        parts.append(title)
    line = " ".join(parts)
    if hint:
        line += f" — {hint}"
    if len(line) > _LINE_CAP:
        line = line[: _LINE_CAP - 1] + "…"
    return f"- {line}"


def _label_for(name: str) -> str:
    """The section heading for a provider. A plugin may set its own (``label=``); otherwise
    derive one from the name so an unlabelled provider still reads as a heading rather than
    as a raw registry key."""
    label = str((_PROVIDER_META.get(name) or {}).get("label") or "").strip()
    if label:
        return label.upper()
    return f"OPEN WORK ({name})"


def collect_work_sections(cap: int) -> list[tuple[str, list[str]]]:
    """``[(heading, [line, …]), …]`` for every provider that currently has work.

    ``cap`` bounds the items taken FROM EACH provider — the working state is injected on
    every turn, so an unbounded board would quietly become the largest thing in the prompt.
    Providers are visited in sorted-name order so the block is stable turn to turn (an
    unstable block would invalidate the prompt cache on every turn for no benefit).

    Best-effort throughout: a provider that raises, returns a non-list, or yields nothing
    usable is skipped without disturbing the others or the turn."""
    sections: list[tuple[str, list[str]]] = []
    for name in sorted(_PROVIDERS):
        fn = _PROVIDERS[name]
        try:
            started = time.monotonic()
            items = fn()
            elapsed = time.monotonic() - started
        except Exception as exc:  # noqa: BLE001 — a bad provider must never break the turn
            _warn_once(name, "raised", "[work_providers] %s raised (skipped, once per name): %s", name, exc)
            continue
        if elapsed > SLOW_PROVIDER_S:
            _warn_once(
                name,
                "slow",
                "[work_providers] %s took %.2fs — it runs on EVERY turn and drags each one. "
                "A provider must return an in-memory snapshot, not do I/O.",
                name,
                elapsed,
            )
        if not isinstance(items, (list, tuple)):
            if items:
                _warn_once(
                    name,
                    "not-a-list",
                    "[work_providers] %s returned %s, expected a list — skipped",
                    name,
                    type(items).__name__,
                )
            continue
        lines = [rendered for rendered in (_render(i, name) for i in items[:cap]) if rendered]
        if lines:
            sections.append((_label_for(name), lines))
    return sections
