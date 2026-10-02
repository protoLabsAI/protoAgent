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

# A headless goal drive turn (the operator-API kick, a detach-resume, a re-arm) is a
# one-shot scheduler job. Its id carries this prefix so the fire is recognisable as a GOAL
# RUN end to end: the scheduler gives it a goal wake header, and the console — which sees
# the job id as the turn's ``trigger`` on ``turn.started`` / ``chat.resumed`` — streams it as
# a goal run instead of folding it into a collapsed "Scheduled task" result card. The
# scheduler matches the literal prefix (it doesn't import ``graph``); keep them in step.
GOAL_RUN_JOB_PREFIX = "goal-run:"


def goal_run_job_id(session_id: str) -> str:
    """The stable one-shot job id for ``session_id``'s goal drive turn. Stable on purpose:
    ``run_in_session`` REPLACES a pending job with the same id, so a resume clicked twice
    queues one drive turn, not two."""
    return f"{GOAL_RUN_JOB_PREFIX}{session_id}"

# Accepted ranges for a goal's per-goal budgets when a caller sets them (#3973). Unset
# (``None``) means "use the config default". Outside the range is refused, not clamped:
# a string, a float, a bool, zero or a negative would otherwise reach ``GoalState`` and
# break the drive loop's ``iteration >= max_iterations`` comparison later.
MAX_GOAL_ITERATIONS = 1000
MAX_GOAL_NO_PROGRESS_LIMIT = 100


def goal_budget_problem(max_iterations: object, no_progress_limit: object) -> str | None:
    """Why a caller-supplied ``max_iterations`` / ``no_progress_limit`` is unusable, or
    ``None`` when both are acceptable. Each must be absent (``None``) or a whole number
    in ``1..MAX``. ``bool`` is refused even though it subclasses ``int``."""
    for name, value, hi in (
        ("max_iterations", max_iterations, MAX_GOAL_ITERATIONS),
        ("no_progress_limit", no_progress_limit, MAX_GOAL_NO_PROGRESS_LIMIT),
    ):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{name} must be a whole number, got {value!r}."
        if not 1 <= value <= hi:
            return f"{name} must be between 1 and {hi}, got {value}."
    return None


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
    # The previous iteration's progress fingerprint — stable ⟺ no real progress, so the
    # no_progress_streak counts consecutive matches. It is verifier-type-aware (see
    # GoalController.evaluate): a deterministic verifier's (reason, evidence) for
    # command/test/ci/data/plugin; the PLAN artifact hash for the fuzzy `llm` verifier
    # (whose free-text reason varies every call and so can't serve as a stall signal).
    last_progress_signature: str = ""
    last_reason: str = ""
    last_evidence: str = ""
    # Per-iteration verifier trail (the drive-loop timeline), newest appended last and
    # capped to the most recent entries. Each event: {iteration, at (epoch), status
    # ("continue" | terminal), reason, evidence}. Survives re-arms so the timeline shows
    # the whole journey. Plain dicts (not a dataclass) so to_dict/from_dict round-trip it.
    history: list[dict] = field(default_factory=list)
    # The tool fence of the turn that set the goal (#1639/#2972) — its completion hooks
    # (plugin ``on_achieved``/``on_failed`` reactions, the self-improvement review) run
    # under it, so a goal a fenced turn set never reacts unfenced. ``[]`` = unfenced.
    fence: list[str] = field(default_factory=list)
    # The model override of the turn that set the goal (#3957), ``""`` for none. A turn
    # that drives the goal without a pick of its own (a watch / schedule fire, a
    # background nudge) runs on it — the pick is not inherited from the thread any more.
    model: str = ""
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
        fields = {k: v for k, v in data.items() if k in known}
        if "fence" in fields:
            # A missing key (a pre-fence file) is unfenced; a PRESENT fence that isn't a
            # list (``null``, a hand-edit) fails CLOSED — deny-all, never unfenced.
            fields["fence"] = _stored_fence(fields["fence"])
        return cls(**fields)

    def status_line(self) -> str:
        """One-line human summary for /goal status + continuation footers."""
        vt = self.verifier.get("type", "llm")
        progress = f"iteration {self.iteration}/{self.max_iterations}"
        tag = ", fresh-context" if self.fresh_context else ""
        if self.has_contract:
            tag += ", contract"
        if self.fence:
            # Set by a turn with a tool fence: every turn that drives it runs under it.
            tag += ", restricted tool scope"
        base = f"goal [{self.status}] via {vt}: {self.condition!r} ({progress}{tag})"
        if self.last_reason:
            base += f" — {self.last_reason}"
        return base


def _stored_fence(value) -> list[str]:
    """A persisted ``fence`` → list of tool names; anything but a list → deny-all."""
    if isinstance(value, list):
        return [str(t) for t in value]
    from graph.middleware.subagent_fence import FENCE_DENY_ALL

    return [FENCE_DENY_ALL]
