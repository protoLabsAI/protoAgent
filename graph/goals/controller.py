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
from dataclasses import dataclass

from graph.goals.store import GoalStore
from graph.goals.types import GoalState
from graph.goals.verifiers import VerifyContext, run_verifier

log = logging.getLogger(__name__)

CLEAR_ALIASES = {"clear", "stop", "off", "reset", "none", "cancel"}


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
        spec, condition, max_iters, no_progress, mode, fresh_context, deadline, stall_after = self._parse_set(rest)
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
            mode=mode,  # "drive" (default) | "monitor" (ADR 0030)
            fresh_context=fresh_context,
            max_iterations=max_iters or getattr(self._config, "goal_max_iterations", 8),
            no_progress_limit=no_progress,  # per-goal patience (ADR 0030 D4); None → config
            deadline=deadline,  # monitor deadline → expired (ADR 0030 D5)
            stall_after=stall_after,  # monitor stall signal → on_stalled (ADR 0030 D5)
        )
        self._store.set(state)
        return f"Goal set. {state.status_line()}"

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
        mode: str = "drive",
        deadline: float | None = None,
        stall_after: int | None = None,
    ) -> tuple[bool, str]:
        """Set a goal from a NON-operator caller (an agent tool, a plugin, REST).
        Accepts ONLY a `plugin` verifier — refuses command/test/ci/data/llm so a
        programmatic set can never reach a shell or `eval` sink (ADR 0028 D3). The
        operator `/goal` path keeps full access. Returns (ok, message)."""
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
            mode=("monitor" if mode == "monitor" else "drive"),  # ADR 0030 (still plugin-gated)
            max_iterations=max_iterations or getattr(self._config, "goal_max_iterations", 8),
            no_progress_limit=no_progress_limit,  # per-goal patience (ADR 0030 D4)
            deadline=deadline,  # monitor deadline → expired (ADR 0030 D5)
            stall_after=stall_after,  # monitor stall signal → on_stalled (ADR 0030 D5)
        )
        self._store.set(state)
        return (True, f"Goal set. {state.status_line()}")

    # --- agent goal-loop tools (retired the <goal_plan>/<goal_unachievable> XML) ---

    def record_plan(self, session_id: str, plan: str) -> tuple[bool, str]:
        """Persist the agent's running plan for its active goal — called by the
        ``update_goal_plan`` tool DURING a turn (replaces the old ``<goal_plan>`` tag).
        Fresh-context goals write the durable plan artifact; same-session goals carry it
        on the goal state. The next continuation prompt feeds it back. Returns (ok, msg)."""
        state = self.active_goal(session_id)
        if state is None:
            return (False, "no active goal for this session.")
        plan = (plan or "").strip()
        if not plan:
            return (False, "a plan is required.")
        if state.fresh_context:
            self._store.write_plan(state.session_id, plan)
        else:
            state.checklist = plan
            self._store.set(state)
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
        mode, fresh_context, deadline|None, stall_after|None)."""
        if rest.lstrip().startswith("{"):
            try:
                data = json.loads(rest)
            except json.JSONDecodeError:
                return ({}, None, None, None, "drive", False, None, None)
            condition = data.get("condition")
            if not condition:
                return ({}, None, None, None, "drive", False, None, None)
            verifier = data.get("verifier") or {"type": "llm"}
            if "type" not in verifier:
                verifier["type"] = "llm"
            mode = "monitor" if data.get("mode") == "monitor" else "drive"
            fresh_context = bool(data.get("fresh_context", False))
            # Monitor termination + stall (ADR 0030 D5); plain data (not verifiers), so the
            # Phase 1 trust-gate is unaffected.
            deadline = self._parse_deadline(data.get("deadline"))
            stall_after = self._parse_stall_after(data.get("stall_after"))
            return (
                verifier,
                condition,
                data.get("max_iterations"),
                data.get("no_progress_limit"),
                mode,
                fresh_context,
                deadline,
                stall_after,
            )
        # plain text → fuzzy goal judged by the llm verifier
        return ({"type": "llm"}, rest, None, None, "drive", False, None, None)

    @staticmethod
    def _parse_deadline(value) -> float | None:
        """A monitor-goal deadline: a number = epoch seconds, or an ISO-8601 string
        (``datetime.fromisoformat``) → epoch seconds. Unparseable → None (no deadline)."""
        if value is None:
            return None
        if isinstance(value, bool):  # bool is an int subclass — reject it explicitly
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            from datetime import datetime

            try:
                return datetime.fromisoformat(value.strip()).timestamp()
            except ValueError:
                return None
        return None

    @staticmethod
    def _parse_stall_after(value) -> int | None:
        """A monitor-goal stall threshold: a positive int (checks) or None."""
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

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
        )
        result = await run_verifier(state.verifier, ctx)

        if result.met:
            return await self._finish(state, "achieved", result.reason or "verifier passed", evidence=result.evidence)

        # Monitor goals (ADR 0030): an external process drives the metric, not the
        # agent's turns — so on not-met there's nothing for the agent to do. Record
        # the check and wait for the next one; no continuation, no iteration/no-
        # progress bookkeeping, no exhaustion. It ends only on achieved / cleared /
        # a deadline (→ expired), with an optional stall signal. This is what closes
        # ADR-0028 D6 (and the ADR 0030 D5 slice).
        if state.mode == "monitor":
            from time import time

            from graph.goals.hooks import fire_stall_hook

            # (a) Deadline (ADR 0030 D5): a monitor goal that hasn't been met by its
            # deadline finishes `expired` — a NON-achieved terminal, so it fires on_failed
            # + the goal.failed bus event like exhausted/unachievable.
            if state.deadline is not None and time() >= state.deadline:
                return await self._finish(
                    state, "expired", "deadline passed before the goal was met", evidence=result.evidence
                )

            # (b) Stall signal (ADR 0030 D5): after `stall_after` consecutive checks whose
            # verifier reason+evidence didn't change, fire the on_stalled hook ONCE per stall
            # episode — WITHOUT ending the goal (the external engine stopped moving, but the
            # objective lives). Re-arm when the evidence changes.
            unchanged = result.reason == state.last_reason and result.evidence == state.last_evidence
            state.stall_streak = (state.stall_streak + 1) if unchanged else 0
            if not unchanged:
                state.stalled_notified = False
            if state.stall_after and state.stall_streak >= state.stall_after and not state.stalled_notified:
                state.stalled_notified = True
                await fire_stall_hook(state)
                # Best-effort bus signal (mirrors the goal.iteration publish below) so a
                # console/plugin can react to a stalled monitor goal without a hook.
                try:
                    from graph.plugins.host import HOST

                    if HOST.publish:
                        HOST.publish(
                            "goal.stalled",
                            {
                                "session_id": getattr(state, "session_id", "") or "",
                                "condition": getattr(state, "condition", "") or "",
                                "stall_streak": state.stall_streak,
                                "reason": result.reason,
                            },
                        )
                except Exception:  # noqa: BLE001 — a bus hiccup must never break the goal loop
                    pass

            # (c) Record the check and wait for the next one.
            state.last_reason = result.reason
            state.last_evidence = result.evidence
            state.last_checked = time()
            self._store.set(state)
            return None

        # 2. Verifier not met — honour an explicit give-up. The agent records it
        # DURING its turn via the `abandon_goal` tool (persisted to the goal state); we
        # read it here, AFTER the verifier, so ground truth still wins over give-up.
        if state.abandon_reason:
            return await self._finish(state, "unachievable", state.abandon_reason)

        # 3. Not met — track progress, decide continue vs stop. The running plan is
        # maintained by the agent's `update_goal_plan` tool (already persisted to the goal
        # state / plan artifact), so there is nothing to extract from the text here.
        signature_unchanged = result.reason == state.last_reason and result.evidence == state.last_evidence
        state.no_progress_streak = (state.no_progress_streak + 1) if signature_unchanged else 0
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

    async def evaluate_now(self, session_id: str) -> Decision | None:
        """Run the active goal's verifier immediately — no agent turn, no drive
        bookkeeping (ADR 0030 D2.2). A plugin calls this from its own state-change
        path (e.g. right after a sale clears) so achievement is caught promptly
        instead of at the next monitor tick. Met → finish (hooks fire); not-met →
        record evidence + return None (iteration/no-progress untouched)."""
        state = self.active_goal(session_id)
        if state is None:
            return None
        ctx = VerifyContext(
            config=self._config,
            condition=state.condition,
            last_text="",
            tool_summary="",
            cwd=os.getcwd(),
        )
        result = await run_verifier(state.verifier, ctx)
        if result.met:
            return await self._finish(state, "achieved", result.reason or "verifier passed", evidence=result.evidence)
        from time import time

        state.last_reason = result.reason
        state.last_evidence = result.evidence
        state.last_checked = time()
        self._store.set(state)
        return None

    async def tick_monitor_goals(self) -> int:
        """Evaluate every active monitor goal out-of-band — verifier-only, no agent
        turn (ADR 0030 D2.1). The server runs this on a cadence so a met goal
        doesn't sit ``active`` until the next session turn. Returns how many reached
        a terminal state this tick."""
        finished = 0
        for state in list(self._store.all()):
            if not (state.active and state.mode == "monitor"):
                continue
            try:
                decision = await self.evaluate(state.session_id, last_text="")
            except Exception:  # noqa: BLE001 — one bad goal must not stop the tick
                log.exception("[goal] monitor tick failed for %s", state.session_id)
                continue
            if decision is not None and decision.action == "done":
                finished += 1
        return finished

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
                        "mode": state.mode,
                    },
                )
        except Exception:  # noqa: BLE001
            log.debug("[goals] goal.* bus emit failed", exc_info=True)
        glyph = {"achieved": "✓", "exhausted": "⏳", "unachievable": "✗", "expired": "⌛"}.get(status, "•")
        return Decision(action="done", state=state, note=f"{glyph} goal {status}: {reason}")

    def _continuation(self, state: GoalState, result) -> str:
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
        plan_block = state.checklist.strip() or "(no plan yet — create one)"
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
