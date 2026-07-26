"""Watch-mode data types (ADR 0067).

A *watch* is a passive, out-of-band objective: poll a condition on a cadence, and when it
trips, react (run a follow-up agent turn and/or fire hooks). Unlike a *goal* — which the
agent DRIVES via a bounded continuation loop, one per session — a watch is verifier-only and
you can hold MANY at once (keyed by its own id, not the session). It's the parallel-
supervision counterpart to goal-drive.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import time

# Watch lifecycle:
#   active   — being polled on a cadence
#   met      — the verifier passed (reaction fired)
#   expired  — a deadline passed before it met (on_expired fired)
# There is no "cleared" status: clearing UNLINKS the file, so a cleared watch is an absent
# watch, never a stored one. The constant used to list it, which is why a console filter
# (`workOverview.visibleWatches`) shipped a dead `status !== "cleared"` guard. Terminal ==
# "finished, subject to `watches.keep_terminal_h` retention" — the definition the store,
# the tick and `Watch.active` all agree on.
TERMINAL_STATUSES = ("met", "expired")

# What makes a watch FIRE its reaction.
#   met     — the verifier passed. A tripwire: "when the deploy finishes, run the smoke test."
#   change  — the verifier's EVIDENCE differs from last check, whatever the predicate says. A
#             monitor: "tell me whenever the treasury moves." The first check only establishes
#             the baseline; it never fires.
TRIGGERS = ("met", "change")

# Default global poll cadence, in seconds. THE source of truth: ``LangGraphConfig``
# imports it as the default for its ``watch_interval`` field (``watches.interval``), and
# the controller / server loop fall back to it when handed a config object that has no
# such attribute (a duck-typed stub in tests). Keep it here rather than as a literal at
# each read — the reads used to be three unrelated ``getattr(cfg, "watch_interval", 30)``
# calls against a field that did not exist.
DEFAULT_WATCH_INTERVAL_S = 30.0

# Floor the server enforces on the configured cadence — a sub-5s tick would re-run every
# verifier (which can shell out) faster than they plausibly finish.
MIN_WATCH_INTERVAL_S = 5.0

# How long a TERMINAL watch is kept before the tick retires it, in hours.
# ``LangGraphConfig.watch_keep_terminal_h`` (``watches.keep_terminal_h``) mirrors it; <= 0
# disables pruning, the repo's convention for a retention knob. 24h matches the Overview
# card's "met today" pulse, so a trip stays visible for exactly as long as the console
# claims to report it.
DEFAULT_KEEP_TERMINAL_H = 24.0

# Consecutive fires that mark a repeating watch as FLAPPING. A healthy repeating watch fires
# on edges, which are rare; one that fires on back-to-back checks is either oscillating or
# monitoring a value that moves every poll. Either way each fire can enqueue an agent turn, so
# it costs far more than intended — worth a loud, deduped warning and a metric.
FLAP_WARN_CONSECUTIVE_FIRES = 5


@dataclass
class Watch:
    """A persisted watch, keyed by ``id`` (many per instance).

    ``verifier`` is the same free-form spec dict goals use (``type`` selects an entry in
    ``graph/goals/verifiers.VERIFIERS``). On *met*, the optional ``run_prompt`` is enqueued
    as a one-shot agent turn in ``run_session`` (via ``sdk.run_in_session``); registered
    hooks fire regardless.
    """

    id: str
    condition: str
    verifier: dict = field(default_factory=lambda: {"type": "llm"})
    status: str = "active"
    interval_s: float | None = None  # per-watch cadence override; None → config watch_interval
    deadline: float | None = None  # epoch seconds; past → expired (fires on_expired)
    stall_after: int | None = None  # N unchanged checks → on_stalled (watch stays active)
    # What fires the reaction (see TRIGGERS) and whether firing ENDS the watch. The two are
    # orthogonal: `trigger` picks the event, `repeat` picks the lifetime.
    #   met   + repeat=False → the original one-shot tripwire (the default)
    #   met   + repeat=True  → fires on every rising EDGE, stays active
    #   change+ repeat=True  → fires on every value change, stays active
    trigger: str = "met"
    repeat: bool = False
    # Edge state for a repeating `met` watch: was the predicate satisfied at the LAST check?
    # Without this a repeating watch is level-triggered — a predicate that stays true (e.g.
    # "credits >= 1,000,000", true forever once crossed) would re-fire every single tick.
    was_met: bool = False
    # Observability for the repeating/monitor dispositions (see FLAP_WARN_CONSECUTIVE_FIRES).
    # `check_count`/`fire_count` give a lifetime fire RATE straight off GET /api/watches;
    # `consecutive_fires` is the burst signal — it resets on any check that doesn't fire.
    check_count: int = 0
    fire_count: int = 0
    consecutive_fires: int = 0
    flap_warned: bool = False
    # Reaction (ADR 0067 D3): on met, enqueue this prompt as a one-shot agent turn in
    # ``run_session`` via sdk.run_in_session. Both empty → the watch reacts via hooks only.
    run_prompt: str = ""
    run_session: str = ""
    created_at: float = field(default_factory=time)
    last_checked: float | None = None
    last_reason: str = ""
    last_evidence: str = ""
    stall_streak: int = 0
    stalled_notified: bool = False
    finished_at: float | None = None

    @property
    def active(self) -> bool:
        return self.status == "active"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Watch":
        # Tolerate unknown/missing keys so older files load forward-compatibly.
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def repeating(self) -> bool:
        """Whether firing leaves the watch active. A repeating watch only ends on its
        deadline or an explicit clear — give it an expiry if it shouldn't run forever."""
        return bool(self.repeat)

    def status_line(self) -> str:
        """One-line human summary for list output + status.

        Includes the lifetime knobs for an ACTIVE watch, because this line is what the agent
        reads back — from ``list_watches`` and from ``<working_state>``'s ACTIVE WATCHES
        block. An agent that can set an expiry has to be able to see one, or it can't tell a
        watch that's about to lapse from one that will poll indefinitely."""
        vt = self.verifier.get("type", "llm")
        base = f"watch [{self.status}] ({self.id}) via {vt}: {self.condition!r}"
        if self.active:
            base += self._lifetime_suffix()
        if self.last_reason:
            base += f" — {self.last_reason}"
        return base

    def _lifetime_suffix(self) -> str:
        """`` (every 30m, expires in 2h, stall after 3)`` — only the parts that are set."""
        parts = []
        if self.trigger == "change":
            parts.append("on change")
        if self.repeat:
            parts.append("repeating")
        if self.interval_s:
            parts.append(f"every {_duration(self.interval_s)}")
        if self.deadline is not None:
            left = self.deadline - time()
            parts.append(f"expires in {_duration(left)}" if left > 0 else "past its deadline")
        if self.stall_after:
            parts.append(f"stall after {self.stall_after}")
        return f" ({', '.join(parts)})" if parts else ""


def _duration(seconds: float) -> str:
    """A compact, agent-readable span: ``45s`` / ``30m`` / ``2h`` / ``3d``. Rounded — this
    feeds prose the model reads, not arithmetic it does."""
    seconds = max(0.0, float(seconds))
    for limit, div, unit in ((90, 1, "s"), (5400, 60, "m"), (172800, 3600, "h")):
        if seconds < limit:
            return f"{round(seconds / div)}{unit}"
    return f"{round(seconds / 86400)}d"
