"""Goal-mode data types.

A *goal* is a testable outcome the agent self-drives toward: after each turn
the agent "stops" on, a verifier decides whether the goal is met; if not, the
agent is re-invoked with a continuation prompt until it is met, the iteration
budget runs out, or the goal is flagged unachievable.

Unlike protocli's goal system (free-text condition judged by an LLM), the
completion check here is backed by a real verifier (a shell command exit code,
a test run, CI status, or a data assertion) — LLM judgment is only the fallback
verifier type. See ``graph/goals/verifiers.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from time import time

# Goal lifecycle states.
#   active        — being worked toward
#   achieved      — verifier confirmed completion
#   exhausted     — ran out of iteration budget without meeting the goal
#   unachievable  — flagged as not reachable (no-progress streak, or the model
#                   explicitly gave up with a reason)
TERMINAL_STATUSES = ("achieved", "exhausted", "unachievable")


@dataclass
class VerifyResult:
    """Outcome of running a goal's verifier once."""

    met: bool
    reason: str = ""
    evidence: str = ""


@dataclass
class GoalState:
    """Persisted per-session goal record.

    ``verifier`` is a free-form spec dict whose ``type`` selects an entry in
    ``graph/goals/verifiers.VERIFIERS`` and whose other keys are that verifier's
    parameters (e.g. ``{"type": "command", "command": "pytest -q"}``).
    ``checklist`` holds the running plan the agent records with the
    ``update_goal_plan`` tool, carried forward across iterations.
    """

    session_id: str
    condition: str
    verifier: dict = field(default_factory=lambda: {"type": "llm"})
    status: str = "active"
    # Disposition (ADR 0030): "drive" = the agent does the work (bounded
    # continuation loop); "monitor" = an external process drives the metric, the
    # agent only supervises (verifier-only, out-of-band, no exhaustion).
    mode: str = "drive"
    # Fresh-context mode (Ralph loop): each continuation turn starts a NEW
    # LangGraph thread so the model sees a clean slate — no accumulated
    # transcript from prior iterations. Durable state (plan artifact) lives
    # on disk. Opt-in only; short goals benefit from transcript continuity.
    fresh_context: bool = False
    last_checked: float | None = None  # last out-of-band verifier check (monitor)
    checklist: str = ""
    # Set by the agent's ``abandon_goal`` tool mid-turn; ``evaluate`` finishes the goal
    # ``unachievable`` after the verifier runs (retired the ``<goal_unachievable/>`` tag).
    abandon_reason: str = ""
    iteration: int = 0
    max_iterations: int = 8
    # Per-goal patience (ADR 0030 D4); None → the config goal_no_progress_limit.
    no_progress_limit: int | None = None
    no_progress_streak: int = 0
    last_reason: str = ""
    last_evidence: str = ""
    started_at: float = field(default_factory=time)
    finished_at: float | None = None

    @property
    def active(self) -> bool:
        return self.status == "active"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "GoalState":
        # Tolerate unknown/missing keys so older files load forward-compatibly.
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def status_line(self) -> str:
        """One-line human summary for /goal status + continuation footers."""
        vt = self.verifier.get("type", "llm")
        # Monitor goals have no iteration budget — show the disposition instead.
        progress = "monitor" if self.mode == "monitor" else f"iteration {self.iteration}/{self.max_iterations}"
        # mode_tag: only shown when it adds info beyond the progress field.
        # "fresh-context" is always worth noting; "drive" is the default for
        # non-monitor goals (shown for clarity); monitor mode is redundant with
        # the progress label so it's omitted.
        mode_tag = "fresh-context" if self.fresh_context else ("drive" if self.mode == "drive" else "")
        base = f"goal [{self.status}] via {vt}: {self.condition!r} ({progress}{', ' + mode_tag if mode_tag else ''})"
        if self.last_reason:
            base += f" — {self.last_reason}"
        return base
