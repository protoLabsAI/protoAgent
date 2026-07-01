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
#   cleared  — removed by an operator/agent/plugin
TERMINAL_STATUSES = ("met", "expired", "cleared")


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

    def status_line(self) -> str:
        """One-line human summary for list output + status."""
        vt = self.verifier.get("type", "llm")
        base = f"watch [{self.status}] ({self.id}) via {vt}: {self.condition!r}"
        if self.last_reason:
            base += f" — {self.last_reason}"
        return base
