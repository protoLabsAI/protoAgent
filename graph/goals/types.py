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
    ``checklist`` holds the model-authored ``<goal_plan>`` text, carried forward
    across iterations so the agent keeps a running plan.
    """
    session_id: str
    condition: str
    verifier: dict = field(default_factory=lambda: {"type": "llm"})
    status: str = "active"
    checklist: str = ""
    iteration: int = 0
    max_iterations: int = 8
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
        base = (
            f"goal [{self.status}] via {vt}: {self.condition!r} "
            f"(iteration {self.iteration}/{self.max_iterations})"
        )
        if self.last_reason:
            base += f" — {self.last_reason}"
        return base
