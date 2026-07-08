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

    The running plan (the "orient" world-model the agent records with the
    ``update_goal_plan`` tool) is NOT a field here — it lives in the durable
    ``GoalStore`` plan artifact (``read_plan``/``write_plan``) for EVERY goal, so
    the continuation loop-back and the trace ``orient``/``loop_shape`` signal see
    it uniformly (ADR 0079).
    """

    session_id: str
    condition: str
    verifier: dict = field(default_factory=lambda: {"type": "llm"})
    # --- completion contract (ADR 0073) -----------------------------------
    # A structured layer OVER the verifier (the contract's *verification* stays
    # ``verifier`` — the real, deterministic check). These fields shape the
    # continuation prompt each drive turn; they never decide DONE (the verifier
    # does). All default-empty, so a goal set without a contract is unchanged.
    #   outcome     — the single required end-state, as a human summary
    #                 (falls back to ``condition`` when empty; see ``resolved_outcome``).
    #   constraints — invariants the agent must NOT violate/regress.
    #   boundaries  — the files/dirs/systems in scope (stay inside these).
    #   stop_when   — a condition under which the agent should PAUSE the drive
    #                 loop and ask the operator (v1 = prompt-injected; the agent
    #                 self-parks via the abandon/ask path — no auto-detection).
    outcome: str = ""
    constraints: list[str] = field(default_factory=list)
    boundaries: list[str] = field(default_factory=list)
    stop_when: str = ""
    status: str = "active"
    # Fresh-context mode (Ralph loop): each continuation turn starts a NEW
    # LangGraph thread so the model sees a clean slate — no accumulated
    # transcript from prior iterations. Durable state (plan artifact) lives
    # on disk. Opt-in only; short goals benefit from transcript continuity.
    fresh_context: bool = False
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

    @property
    def resolved_outcome(self) -> str:
        """The contract's required end-state — ``outcome`` when set, else the
        ``condition`` (so a contract-less goal still has one)."""
        return (self.outcome or "").strip() or self.condition

    @property
    def has_contract(self) -> bool:
        """True when the goal carries any contract field beyond the bare condition/verifier."""
        return bool(self.outcome or self.constraints or self.boundaries or self.stop_when)

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
        progress = f"iteration {self.iteration}/{self.max_iterations}"
        tag = ", fresh-context" if self.fresh_context else ""
        if self.has_contract:
            tag += ", contract"
        base = f"goal [{self.status}] via {vt}: {self.condition!r} ({progress}{tag})"
        if self.last_reason:
            base += f" — {self.last_reason}"
        return base
