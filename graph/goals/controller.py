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

import hashlib
import json
import logging
import os
from dataclasses import dataclass

from graph.goals.store import GoalStore
from graph.goals.types import GoalState
from graph.goals.verifiers import VerifierInvoker, VerifyContext, run_verifier

log = logging.getLogger(__name__)

CLEAR_ALIASES = {"clear", "stop", "off", "reset", "none", "cancel"}

# The exact prefix a successful ``/goal`` SET replies with. The chat runners match on it
# to decide whether to KICK an initial goal-driven turn (#1910) — see ``is_set_ack``.
SET_ACK_PREFIX = "Goal set. "

# Appended to every kickoff + continuation prompt (ADR 0079): point the drive loop at the full
# toolkit so it composes goals with tasks/watches/scheduling instead of spinning. The system
# prompt carries the full operating model; this is the goal-loop-specific reminder.
_DRIVE_TACTIC = (
    "Decompose this into `task_create` items and drive them down. If your next step waits on "
    "async or delegated work (a build, a peer agent, CI, a review), set a `create_watch` on that "
    "condition (or a `schedule_task`) and END the turn — you'll be resumed when it's ready. Don't "
    "spin polling to the iteration cap."
)


def _coerce_str_list(value) -> list[str]:
    """Normalize a contract list field (``constraints``/``boundaries``) to ``list[str]``.

    A bare string becomes a 1-element list; ``None``/empty → ``[]``; a list/tuple is
    stringified element-wise (blank entries dropped); anything else → ``[]``. Keeps
    the operator-set seam forgiving of a JSON body that sent a single string where a
    list was expected (ADR 0073)."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return []


@dataclass
class Decision:
    action: str  # "continue" | "done"
    state: GoalState | None = None
    message: str | None = None  # continuation prompt (action == "continue")
    note: str = ""  # human-readable status note


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

    async def parse_control(self, message: str, session_id: str, *, trusted: bool = True) -> str | None:
        # `trusted` gates which verifier types a SET may use (Phase 1 trust-gate, #1407).
        # The server's chat entry points MUST pass trusted=False; the default stays True
        # for the operator/programmatic path (and backward-compat) that Phase 2 re-enables.
        if not isinstance(message, str):
            return None
        stripped = message.strip()
        if not (stripped == "/goal" or stripped.lower().startswith("/goal ") or stripped.lower().startswith("/goal\n")):
            return None
        rest = stripped[len("/goal") :].strip()

        # /goal  → status
        if not rest:
            state = self._store.get(session_id)
            return state.status_line() if state else "No active goal for this session."

        # /goal clear|stop|...  → clear
        if rest.lower() in CLEAR_ALIASES:
            existed = self._store.clear(session_id)
            return "Goal cleared." if existed else "No active goal to clear."

        # /goal {json}  or  /goal <free text>  → set
        spec, condition, max_iters, no_progress, fresh_context, contract = self._parse_set(rest)
        if condition is None:
            return (
                "Could not parse goal. Use `/goal <text>` or "
                '`/goal {"condition": "...", "verifier": {"type": "command", '
                '"command": "pytest -q"}}`.'
            )
        # Phase 1 trust-gate (#1407): a /goal CHAT message is untrusted — both server call
        # sites pass trusted=False, because a federation peer / API client shares the
        # operator bearer today, so we can't tell them apart. Refuse the code-exec verifiers
        # from chat for EVERYONE: command/test/ci shell out on the host, and a `data` `expr`
        # is a restricted-eval sink + arbitrary file read (ADR 0028 D3). Only the declarative
        # types pass — `plugin`, `llm` (fuzzy), and `data` with a plain `contains`.
        if not trusted and not self._chat_verifier_allowed(spec):
            return (
                "For safety, a `command`, `test`, `ci`, or `data`+`expr` verifier can't be "
                "set from a chat message. Use a fuzzy goal (`/goal <text>`), a `plugin` "
                "verifier, or a `data` verifier with `contains`. (Shell/eval verifiers are "
                "operator-only.)"
            )
        state = GoalState(
            session_id=session_id,
            condition=condition,
            verifier=spec,
            fresh_context=fresh_context,
            max_iterations=max_iters or getattr(self._config, "goal_max_iterations", 8),
            no_progress_limit=no_progress,  # per-goal patience; None → config
            outcome=contract.get("outcome", ""),
            constraints=list(contract.get("constraints") or []),
            boundaries=list(contract.get("boundaries") or []),
            stop_when=contract.get("stop_when", ""),
        )
        self._store.set(state)
        return f"{SET_ACK_PREFIX}{state.status_line()}"

    @staticmethod
    def is_set_ack(reply: str | None) -> bool:
        """True when a ``parse_control`` reply is the ack for a *successful* goal SET
        (as opposed to a status / clear / parse-error reply). The chat runners use this to
        decide whether to KICK an initial goal-driven turn (#1910): a SET should drive the
        agent immediately, whereas /goal status or /goal clear just reply and stop."""
        return isinstance(reply, str) and reply.startswith(SET_ACK_PREFIX)

    def kickoff_prompt(self, state: GoalState, user_message: str = "") -> str:
        """The initial goal-driven turn's prompt (#1910). The re-invoke iterations receive
        the goal via ``_continuation``; the *first* turn had nothing — it fell through to a
        bare user message, so the agent never learned its own active goal and asked
        "what goal?", parking at INPUT_REQUIRED before the drive loop could run. This
        injects the goal condition (and completion contract, if any) so the agent BEGINS on
        turn 1. Mirrors the continuation framing for iteration 0 (no verifier result yet)."""
        if state.fresh_context:
            lead = (
                f"[goal kickoff — 0/{state.max_iterations}, fresh context]\n"
                f"Goal: {state.condition}\n\n"
                "Take ONE concrete step toward the goal now. Record your running plan by "
                "calling the `update_goal_plan` tool (it is persisted for the next "
                "iteration). If you determine the goal is impossible or out of scope, call "
                "the `abandon_goal` tool with a reason and stop."
            )
        else:
            lead = (
                f"[goal kickoff — 0/{state.max_iterations}]\n"
                f"You have an active goal for this session: {state.condition}\n\n"
                "Begin working toward it now — take concrete action; do not ask which goal "
                "it is. Record your running plan by calling the `update_goal_plan` tool (it "
                "is persisted and fed back to you each iteration). If it is impossible or out "
                "of scope, call the `abandon_goal` tool with a reason and stop."
            )
        contract = self._contract_prompt(state)
        body = f"{lead}\n\n{contract}" if contract else lead
        body = f"{body}\n\n{_DRIVE_TACTIC}"
        # A plain inbound message that arrived alongside an already-active goal (not a
        # /goal command) is folded in so the operator's words aren't lost.
        extra = (user_message or "").strip()
        if extra and not extra.lower().startswith("/goal"):
            body = f"{body}\n\nThe operator also said: {extra}"
        return body

    @staticmethod
    def _chat_verifier_allowed(verifier: dict) -> bool:
        """Allow-list for a verifier set from an (untrusted) /goal CHAT message (Phase 1,
        #1407). Gate by the complement (R2): allow only the declarative, no-code-exec
        types — `plugin`, `llm`, and `data` restricted to a plain `contains` substring.
        Everything else (command/test/ci, and `data` carrying an `expr`) shells out or hits
        a restricted-eval sink and stays operator-only."""
        vtype = (verifier or {}).get("type", "llm")
        if vtype in ("plugin", "llm"):
            return True
        if vtype == "data":
            return "expr" not in verifier and "contains" in verifier
        return False

    # Verifier types safe to set PROGRAMMATICALLY (agent / plugin / REST). Only
    # `plugin` qualifies (ADR 0028 D3): command/test/ci shell out, and `data`
    # eval()s a spec expr — all code-exec sinks that stay operator-only (/goal).
    SAFE_PROGRAMMATIC_VERIFIERS = frozenset({"plugin"})

    def set_goal_safe(
        self,
        session_id: str,
        condition: str,
        verifier: dict,
        max_iterations: int | None = None,
        no_progress_limit: int | None = None,
        *,
        outcome: str = "",
        constraints: list[str] | None = None,
        boundaries: list[str] | None = None,
        stop_when: str = "",
    ) -> tuple[bool, str]:
        """Set a goal from a NON-operator caller (an agent tool, a plugin, REST).
        Accepts ONLY a `plugin` verifier — refuses command/test/ci/data/llm so a
        programmatic set can never reach a shell or `eval` sink (ADR 0028 D3). The
        operator `/goal` path keeps full access. The optional completion-contract
        fields (ADR 0073) shape the continuation prompt only — the verifier still
        decides DONE. Returns (ok, message)."""
        vtype = (verifier or {}).get("type")
        if vtype not in self.SAFE_PROGRAMMATIC_VERIFIERS:
            return (
                False,
                f"programmatic goals must use a 'plugin' verifier (got {vtype!r}); "
                "command/test/ci/data verifiers are operator-only — set them with /goal.",
            )
        if not condition:
            return (False, "a goal condition is required.")
        if not (verifier.get("check")):
            return (False, "a plugin verifier needs a 'check' (the <plugin-id>:<name>).")
        state = GoalState(
            session_id=session_id,
            condition=condition,
            verifier=verifier,
            max_iterations=max_iterations or getattr(self._config, "goal_max_iterations", 8),
            no_progress_limit=no_progress_limit,
            outcome=outcome or "",
            constraints=_coerce_str_list(constraints),
            boundaries=_coerce_str_list(boundaries),
            stop_when=stop_when or "",
        )
        self._store.set(state)
        return (True, f"Goal set. {state.status_line()}")

    def set_goal_operator(
        self,
        session_id: str,
        condition: str,
        verifier: dict,
        max_iterations: int | None = None,
        no_progress_limit: int | None = None,
        *,
        outcome: str = "",
        constraints: list[str] | None = None,
        boundaries: list[str] | None = None,
        stop_when: str = "",
    ) -> tuple[bool, str]:
        """Set a goal from the TRUSTED OPERATOR surface — ``POST /api/goals``, gated to
        operator-tier by the ADR 0066 ``/api`` path ceiling. Unlike ``set_goal_safe``
        (agent/plugin/programmatic → ``plugin``-verifier only), this accepts ANY verifier
        type (command/test/ci/data included) because the caller is the authenticated
        operator — the same power the operator ``/goal`` chat path had before Phase 1.
        The optional completion-contract fields (ADR 0073) shape the continuation prompt
        only — the verifier still decides DONE. Returns (ok, message)."""
        from graph.goals.verifiers import VERIFIERS

        if not condition:
            return (False, "a goal condition is required.")
        verifier = verifier or {"type": "llm"}
        vtype = verifier.get("type", "llm")
        if vtype not in VERIFIERS:
            return (False, f"unknown verifier type {vtype!r}; known: {', '.join(sorted(VERIFIERS))}.")
        state = GoalState(
            session_id=session_id,
            condition=condition,
            verifier=verifier,
            max_iterations=max_iterations or getattr(self._config, "goal_max_iterations", 8),
            no_progress_limit=no_progress_limit,
            outcome=outcome or "",
            constraints=_coerce_str_list(constraints),
            boundaries=_coerce_str_list(boundaries),
            stop_when=stop_when or "",
        )
        self._store.set(state)
        return (True, f"Goal set. {state.status_line()}")

    # --- agent goal-loop tools (retired the <goal_plan>/<goal_unachievable> XML) ---

    def record_plan(self, session_id: str, plan: str) -> tuple[bool, str]:
        """Persist the agent's running plan (its "orient" world-model) for the active goal —
        called by the ``update_goal_plan`` tool DURING a turn. Writes the durable ``GoalStore``
        plan artifact for EVERY goal (ADR 0079 — no fresh-context fork), so the next continuation
        prompt feeds it back and the trace ``orient``/``loop_shape`` signal sees it uniformly.
        Returns (ok, msg)."""
        state = self.active_goal(session_id)
        if state is None:
            return (False, "no active goal for this session.")
        plan = (plan or "").strip()
        if not plan:
            return (False, "a plan is required.")
        self._store.write_plan(state.session_id, plan)
        return (True, "plan recorded.")

    def request_abandon(self, session_id: str, reason: str) -> tuple[bool, str]:
        """Flag the active goal as unachievable at the agent's request — called by the
        ``abandon_goal`` tool DURING a turn (replaces the old ``<goal_unachievable/>``
        tag). Recorded on the goal state; the post-turn ``evaluate`` honours it AFTER the
        verifier, so a goal the world already satisfies still finishes ``achieved``.
        Returns (ok, msg)."""
        state = self.active_goal(session_id)
        if state is None:
            return (False, "no active goal for this session.")
        state.abandon_reason = (reason or "").strip() or "agent flagged the goal unachievable"
        self._store.set(state)
        return (True, "goal will stop after this turn (flagged unachievable).")

    def _parse_set(self, rest: str):
        """Return (verifier_spec, condition, max_iterations|None, no_progress_limit|None,
        fresh_context, contract) — ``contract`` carries the optional ADR 0073 fields
        (outcome/constraints/boundaries/stop_when) from a JSON spec; ``{}`` for free text.
        The contract is directive prose (no code-exec), so it's safe from an untrusted chat
        set — no trust-gate, unlike the verifier."""
        if rest.lstrip().startswith("{"):
            try:
                data = json.loads(rest)
            except json.JSONDecodeError:
                return ({}, None, None, None, False, {})
            condition = data.get("condition")
            if not condition:
                return ({}, None, None, None, False, {})
            verifier = data.get("verifier") or {"type": "llm"}
            if "type" not in verifier:
                verifier["type"] = "llm"
            fresh_context = bool(data.get("fresh_context", False))
            contract = {
                "outcome": data.get("outcome") or "",
                "constraints": _coerce_str_list(data.get("constraints")),
                "boundaries": _coerce_str_list(data.get("boundaries")),
                "stop_when": data.get("stop_when") or "",
            }
            return (verifier, condition, data.get("max_iterations"), data.get("no_progress_limit"), fresh_context, contract)
        # plain text → fuzzy goal judged by the llm verifier
        return ({"type": "llm"}, rest, None, None, False, {})

    # --- evaluation --------------------------------------------------------

    async def evaluate(self, session_id: str, *, last_text: str, tool_summary: str = "") -> Decision | None:
        state = self.active_goal(session_id)
        if state is None:
            return None

        # 1. Run the verifier first — ground truth overrides the model's
        # self-assessment. If the external world already satisfies the goal,
        # a same-turn abandon_goal give-up must not mask that.
        ctx = VerifyContext(
            config=self._config,
            condition=state.condition,
            last_text=last_text or "",
            tool_summary=tool_summary or "",
            cwd=os.getcwd(),
            # A goal is keyed by its session (one per session), so id == session_id (#1641).
            invoker=VerifierInvoker(kind="goal", id=state.session_id, session_id=state.session_id),
        )
        result = await run_verifier(state.verifier, ctx)

        if result.met:
            return await self._finish(state, "achieved", result.reason or "verifier passed", evidence=result.evidence)

        # 2. Verifier not met — honour an explicit give-up. The agent records it
        # DURING its turn via the `abandon_goal` tool (persisted to the goal state); we
        # read it here, AFTER the verifier, so ground truth still wins over give-up.
        if state.abandon_reason:
            return await self._finish(state, "unachievable", state.abandon_reason)

        # 3. Not met — track progress, decide continue vs stop. No-progress detection needs a
        # per-iteration fingerprint that is stable ⟺ the agent made no real progress:
        #   • DETERMINISTIC verifiers (command/test/ci/data/plugin): the verifier's own
        #     (reason, evidence) IS that fingerprint — an identical failure repeated is a stall.
        #   • The fuzzy `llm` verifier: `reason` is model free-text that varies every call and
        #     `evidence` is always "" (see verifiers._verify_llm), so that fingerprint would
        #     NEVER repeat — no_progress_limit could never fire and a fuzzy goal would only ever
        #     stop at the iteration cap. Fingerprint the agent's PLAN artifact instead: an llm
        #     goal whose recorded plan is unchanged across turns is spinning; while the agent
        #     keeps recording new progress (updating the plan) it runs to the cap as before.
        signature = self._progress_signature(state, result)
        signature_unchanged = signature == state.last_progress_signature
        state.no_progress_streak = (state.no_progress_streak + 1) if signature_unchanged else 0
        state.last_progress_signature = signature
        state.last_reason = result.reason
        state.last_evidence = result.evidence
        state.iteration += 1

        limit = state.no_progress_limit or getattr(self._config, "goal_no_progress_limit", 3)
        if state.iteration >= state.max_iterations:
            return await self._finish(
                state, "exhausted", f"ran out of iteration budget ({state.max_iterations})", evidence=result.evidence
            )
        if state.no_progress_streak >= limit:
            return await self._finish(
                state,
                "unachievable",
                f"no progress after {state.no_progress_streak} attempts: {result.reason}",
                evidence=result.evidence,
            )

        self._store.set(state)
        # Realtime goal-loop progress (ADR 0051 Slice 3) — only goal.achieved/failed were
        # on the bus; surface each continuation too so a console can show the loop working.
        # Best-effort, same channel as the terminal events.
        try:
            from graph.plugins.host import HOST

            if HOST.publish:
                HOST.publish(
                    "goal.iteration",
                    {
                        "session_id": getattr(state, "session_id", "") or "",
                        "condition": getattr(state, "condition", "") or "",
                        "iteration": state.iteration,
                        "max_iterations": state.max_iterations,
                        "reason": result.reason,
                    },
                )
        except Exception:  # noqa: BLE001 — a bus hiccup must never break the goal loop
            pass
        return Decision(
            action="continue",
            state=state,
            message=self._continuation(state, result),
            note=f"goal not met (iteration {state.iteration}/{state.max_iterations}): {result.reason}",
        )

    def _progress_signature(self, state: GoalState, result) -> str:
        """A per-iteration fingerprint that stays constant ⟺ the agent made no progress
        (drives ``no_progress_streak``). Verifier-type-aware: the fuzzy ``llm`` verifier's
        free-text reason can't serve as a stall signal (it varies every call, evidence is
        always ""), so a fuzzy goal fingerprints its PLAN artifact; every other
        (deterministic) verifier fingerprints its own ``(reason, evidence)``. Never returns
        "" for a real computation, so the default-"" baseline can't false-match on turn 1."""
        if state.verifier.get("type", "llm") == "llm":
            plan = self._store.read_plan(state.session_id)
            return "plan:" + hashlib.sha256(plan.encode("utf-8")).hexdigest()
        return f"rv:{result.reason}\x00{result.evidence}"

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
        # Broadcast on the event bus (ADR 0039) so ANY plugin or the console can react to a terminal
        # goal — no goal_hook plugin required, no cross-dependency. `goal.achieved` on success;
        # `goal.failed` on exhausted/unachievable. Best-effort: a bus hiccup must never break finish.
        try:
            from graph.plugins.host import HOST

            if HOST.publish:
                HOST.publish(
                    "goal.achieved" if status == "achieved" else "goal.failed",
                    {
                        "session_id": state.session_id,
                        "condition": state.condition,
                        "status": status,
                        "reason": reason,
                        "evidence": evidence or state.last_evidence or "",
                    },
                )
        except Exception:  # noqa: BLE001
            log.debug("[goals] goal.* bus emit failed", exc_info=True)
        glyph = {"achieved": "✓", "exhausted": "⏳", "unachievable": "✗", "expired": "⌛"}.get(status, "•")
        return Decision(action="done", state=state, note=f"{glyph} goal {status}: {reason}")

    def _continuation(self, state: GoalState, result) -> str:
        # Re-state the completion contract every drive turn (ADR 0073) — mirrors
        # Hermes' contract-directed continuation. It is directive ONLY: the DONE
        # decision still comes from the verifier (see `evaluate`). A goal with no
        # contract appends nothing, so its continuation prompt is byte-for-byte
        # unchanged from before contracts existed (backward-compat).
        base = self._continuation_base(state, result)
        contract = self._contract_prompt(state)
        body = f"{base}\n\n{contract}" if contract else base
        return f"{body}\n\n{_DRIVE_TACTIC}"

    @staticmethod
    def _verifier_summary(verifier: dict) -> str:
        """Compact human summary of a verifier spec for the contract directive, e.g.
        ``command: pytest -q``, ``ci PR #12``, ``plugin demo:probe``, ``llm judgment``."""
        v = verifier or {}
        vt = v.get("type", "llm")
        if vt in ("command", "test"):
            cmd = (v.get("command") or "").strip()
            return f"{vt}: {cmd}" if cmd else vt
        if vt == "ci":
            if v.get("pr") is not None:
                return f"ci PR #{v.get('pr')}"
            if v.get("branch"):
                return f"ci branch {v.get('branch')}"
            return "ci"
        if vt == "data":
            return f"data check on {v.get('path')}" if v.get("path") else "data check"
        if vt == "plugin":
            return f"plugin {v.get('check')}" if v.get("check") else "plugin verifier"
        if vt == "llm":
            return "llm judgment"
        return vt

    def _contract_prompt(self, state: GoalState) -> str:
        """Render the goal's completion contract (ADR 0073) into one compact directive
        block for the continuation prompt. Only the non-empty fields appear; a goal with
        no contract renders to ``""`` (so its continuation is unchanged). This is
        directive text — the verifier, NOT this block, decides DONE."""
        if not state.has_contract:
            return ""
        parts = [
            f"Contract for this goal: it is DONE only when the verifier passes "
            f"({self._verifier_summary(state.verifier)})."
        ]
        outcome = (state.outcome or "").strip()
        if outcome:
            parts.append(f"Required outcome: {outcome}.")
        constraints = [c for c in state.constraints if str(c).strip()]
        if constraints:
            parts.append("Constraints (do NOT violate): " + "; ".join(constraints) + ".")
        boundaries = [b for b in state.boundaries if str(b).strip()]
        if boundaries:
            parts.append("Stay within these boundaries: " + "; ".join(boundaries) + ".")
        stop_when = (state.stop_when or "").strip()
        if stop_when:
            # follow-up (ADR 0073 D4): stop_when is prompt-injected in v1 — the agent
            # self-parks via abandon_goal when it recognizes the condition. A future
            # option is a `stop_when`-as-verifier auto-check that could park the drive
            # loop DETERMINISTICALLY (reusing the verifier surface) when the stop
            # condition is mechanically checkable.
            parts.append(
                f"If {stop_when}, STOP and ask the operator instead of continuing "
                "(call the `abandon_goal` tool with that reason to park the goal)."
            )
        return " ".join(parts)

    def _continuation_base(self, state: GoalState, result) -> str:
        if state.fresh_context:
            plan = self._store.read_plan(state.session_id) or "(no plan yet — create one)"
            evidence = (result.evidence or "").strip()
            evidence_block = f"Evidence:\n{evidence}\n" if evidence else ""
            vtype = state.verifier.get("type", "llm")
            return (
                f"[goal continuation {state.iteration}/{state.max_iterations} — fresh context]\n"
                f"Goal: {state.condition}\n"
                f"Verifier ({vtype}) last result: {result.reason}\n"
                + (evidence_block + "\n" if evidence_block else "\n")
                + f"Plan from last iteration:\n{plan}\n\n"
                f"Take ONE concrete step toward the goal. Read the plan — it records what's "
                f"been tried, what's next, and what failed. Record your updated running plan "
                f"by calling the `update_goal_plan` tool (it is persisted for the next "
                f"iteration). If you determine the goal is impossible or out of scope, call "
                f"the `abandon_goal` tool with a reason and stop."
            )
        evidence = (result.evidence or "").strip()
        evidence_block = f"\nEvidence:\n{evidence}\n" if evidence else "\n"
        plan_block = self._store.read_plan(state.session_id).strip() or "(no plan yet — create one)"
        vtype = state.verifier.get("type", "llm")
        return (
            f"[goal continuation {state.iteration}/{state.max_iterations}]\n"
            f"The goal is NOT yet met.\n"
            f"Verifier ({vtype}): {result.reason}"
            f"{evidence_block}\n"
            f"Current plan:\n{plan_block}\n\n"
            f'Keep working toward the goal: "{state.condition}".\n'
            f"Record your running plan by calling the `update_goal_plan` tool (update it "
            f"each turn — it is fed back to you here). If you determine the goal is "
            f"impossible or out of scope, call the `abandon_goal` tool with a reason and "
            f"stop. Otherwise take the next concrete step now."
        )
