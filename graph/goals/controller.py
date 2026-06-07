"""GoalController — control-message parsing + the goal decision loop.

Two responsibilities, both pure of any graph calls so they're unit-testable:

1. ``parse_control`` — interpret a ``/goal`` control message (set / status /
   clear) and mutate the store. Returns a reply string when the message *was* a
   command (the caller short-circuits the turn), else ``None``.

2. ``evaluate`` — run after the agent "stops" (terminal turn). Runs the goal's
   verifier and returns a ``Decision``: keep going with a continuation prompt,
   or finish (achieved / exhausted / unachievable).

The server invocation paths own the actual re-invocation loop; this class only
decides what should happen next.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

from graph.goals.store import GoalStore
from graph.goals.types import GoalState
from graph.goals.verifiers import VerifyContext, run_verifier

log = logging.getLogger(__name__)

CLEAR_ALIASES = {"clear", "stop", "off", "reset", "none", "cancel"}

_GOAL_PLAN_RE = re.compile(r"<goal_plan>(.*?)</goal_plan>", re.IGNORECASE | re.DOTALL)
_GIVEUP_RE = re.compile(
    r"<goal_unachievable(?:\s+reason=\"([^\"]*)\")?\s*/?>", re.IGNORECASE
)


@dataclass
class Decision:
    action: str               # "continue" | "done"
    state: GoalState | None = None
    message: str | None = None   # continuation prompt (action == "continue")
    note: str = ""               # human-readable status note


class GoalController:
    def __init__(self, config, store: GoalStore | None = None):
        self._config = config
        self._store = store or GoalStore()

    @property
    def store(self) -> GoalStore:
        return self._store

    def active_goal(self, session_id: str) -> GoalState | None:
        state = self._store.get(session_id)
        return state if state and state.active else None

    # --- control messages --------------------------------------------------

    async def parse_control(self, message: str, session_id: str) -> str | None:
        if not isinstance(message, str):
            return None
        stripped = message.strip()
        if not (stripped == "/goal" or stripped.lower().startswith("/goal ")
                or stripped.lower().startswith("/goal\n")):
            return None
        rest = stripped[len("/goal"):].strip()

        # /goal  → status
        if not rest:
            state = self._store.get(session_id)
            return state.status_line() if state else "No active goal for this session."

        # /goal clear|stop|...  → clear
        if rest.lower() in CLEAR_ALIASES:
            existed = self._store.clear(session_id)
            return "Goal cleared." if existed else "No active goal to clear."

        # /goal {json}  or  /goal <free text>  → set
        spec, condition, max_iters = self._parse_set(rest)
        if condition is None:
            return ("Could not parse goal. Use `/goal <text>` or "
                    '`/goal {"condition": "...", "verifier": {"type": "command", '
                    '"command": "pytest -q"}}`.')
        state = GoalState(
            session_id=session_id,
            condition=condition,
            verifier=spec,
            max_iterations=max_iters or getattr(self._config, "goal_max_iterations", 8),
        )
        self._store.set(state)
        return f"Goal set. {state.status_line()}"

    # Verifier types safe to set PROGRAMMATICALLY (agent / plugin / REST). Only
    # `plugin` qualifies (ADR 0028 D3): command/test/ci shell out, and `data`
    # eval()s a spec expr — all code-exec sinks that stay operator-only (/goal).
    SAFE_PROGRAMMATIC_VERIFIERS = frozenset({"plugin"})

    def set_goal_safe(self, session_id: str, condition: str, verifier: dict,
                      max_iterations: int | None = None) -> tuple[bool, str]:
        """Set a goal from a NON-operator caller (an agent tool, a plugin, REST).
        Accepts ONLY a `plugin` verifier — refuses command/test/ci/data/llm so a
        programmatic set can never reach a shell or `eval` sink (ADR 0028 D3). The
        operator `/goal` path keeps full access. Returns (ok, message)."""
        vtype = (verifier or {}).get("type")
        if vtype not in self.SAFE_PROGRAMMATIC_VERIFIERS:
            return (False, f"programmatic goals must use a 'plugin' verifier (got {vtype!r}); "
                    "command/test/ci/data verifiers are operator-only — set them with /goal.")
        if not condition:
            return (False, "a goal condition is required.")
        if not (verifier.get("check")):
            return (False, "a plugin verifier needs a 'check' (the <plugin-id>:<name>).")
        state = GoalState(
            session_id=session_id, condition=condition, verifier=verifier,
            max_iterations=max_iterations or getattr(self._config, "goal_max_iterations", 8),
        )
        self._store.set(state)
        return (True, f"Goal set. {state.status_line()}")

    def _parse_set(self, rest: str):
        """Return (verifier_spec, condition, max_iterations|None)."""
        if rest.lstrip().startswith("{"):
            try:
                data = json.loads(rest)
            except json.JSONDecodeError:
                return ({}, None, None)
            condition = data.get("condition")
            if not condition:
                return ({}, None, None)
            verifier = data.get("verifier") or {"type": "llm"}
            if "type" not in verifier:
                verifier["type"] = "llm"
            return (verifier, condition, data.get("max_iterations"))
        # plain text → fuzzy goal judged by the llm verifier
        return ({"type": "llm"}, rest, None)

    # --- evaluation --------------------------------------------------------

    async def evaluate(self, session_id: str, *, last_text: str, tool_summary: str = "") -> Decision | None:
        state = self.active_goal(session_id)
        if state is None:
            return None

        # 1. Run the verifier first — ground truth overrides the model's
        # self-assessment. If the external world already satisfies the goal,
        # a same-turn <goal_unachievable> give-up must not mask that.
        ctx = VerifyContext(
            config=self._config,
            condition=state.condition,
            last_text=last_text or "",
            tool_summary=tool_summary or "",
            cwd=os.getcwd(),
        )
        result = await run_verifier(state.verifier, ctx)

        if result.met:
            return await self._finish(state, "achieved", result.reason or "verifier passed",
                                evidence=result.evidence)

        # 2. Verifier not met — honour an explicit give-up from the agent.
        giveup = _GIVEUP_RE.search(last_text or "")
        if giveup:
            reason = (giveup.group(1) or "agent flagged the goal unachievable").strip()
            return await self._finish(state, "unachievable", reason)

        # 3. Not met — refresh checklist, track progress, decide continue vs stop.
        plan = _GOAL_PLAN_RE.search(last_text or "")
        if plan:
            state.checklist = plan.group(1).strip()

        signature_unchanged = (
            result.reason == state.last_reason and result.evidence == state.last_evidence
        )
        state.no_progress_streak = (state.no_progress_streak + 1) if signature_unchanged else 0
        state.last_reason = result.reason
        state.last_evidence = result.evidence
        state.iteration += 1

        limit = getattr(self._config, "goal_no_progress_limit", 3)
        if state.iteration >= state.max_iterations:
            return await self._finish(state, "exhausted",
                                f"ran out of iteration budget ({state.max_iterations})",
                                evidence=result.evidence)
        if state.no_progress_streak >= limit:
            return await self._finish(state, "unachievable",
                                f"no progress after {state.no_progress_streak} attempts: {result.reason}",
                                evidence=result.evidence)

        self._store.set(state)
        return Decision(
            action="continue",
            state=state,
            message=self._continuation(state, result),
            note=f"goal not met (iteration {state.iteration}/{state.max_iterations}): {result.reason}",
        )

    async def _finish(self, state: GoalState, status: str, reason: str, *, evidence: str = "") -> Decision:
        from time import time
        from graph.goals.hooks import fire_goal_hooks
        state.status = status
        state.last_reason = reason
        if evidence:
            state.last_evidence = evidence
        state.finished_at = time()
        self._store.set(state)
        # Plugin lifecycle reactions (ADR 0028 D4) — notify / record / set next goal.
        await fire_goal_hooks(status, state)
        glyph = {"achieved": "✓", "exhausted": "⏳", "unachievable": "✗"}.get(status, "•")
        return Decision(action="done", state=state, note=f"{glyph} goal {status}: {reason}")

    def _continuation(self, state: GoalState, result) -> str:
        evidence = (result.evidence or "").strip()
        evidence_block = f"\nEvidence:\n{evidence}\n" if evidence else "\n"
        plan_block = state.checklist.strip() or "(no plan yet — create one)"
        vtype = state.verifier.get("type", "llm")
        return (
            f"[goal continuation {state.iteration}/{state.max_iterations}]\n"
            f"The goal is NOT yet met.\n"
            f"Verifier ({vtype}): {result.reason}"
            f"{evidence_block}\n"
            f"Current plan:\n{plan_block}\n\n"
            f'Keep working toward the goal: "{state.condition}".\n'
            f"Maintain a running checklist inside a <goal_plan>...</goal_plan> block "
            f"(update it every turn). If you determine the goal is impossible or out "
            f'of scope, emit <goal_unachievable reason="..."/> and stop. '
            f"Otherwise take the next concrete step now."
        )
