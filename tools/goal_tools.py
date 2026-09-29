"""Goal tools (ADR 0028 + the goal loop) — ``set_goal``, ``list_verifiers``,
``update_goal_plan`` and ``abandon_goal``, each built by its own ``_build_*_tool``.

Split out of ``tools/lg_tools.py`` (#3830, epic #3804). ``lg_tools`` re-exports the
builders so existing imports — tests, plugins, forks — keep working; ``get_all_tools``
there is still the one assembly point.
"""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from tools.session import _session_id_from


def _build_set_goal_tool():
    """The lead agent sets its OWN standing goal — verified by a plugin verifier
    only (ADR 0028). The agent literally can't open a shell/eval goal here: the
    tool hardcodes ``type="plugin"`` and routes through ``set_goal_safe``."""

    @tool
    def set_goal(
        condition: str,
        check: str,
        check_args: dict | None = None,
        max_iterations: int | None = None,
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Set a standing goal for THIS session, ground-truthed by a plugin verifier.

        You'll be re-invoked toward `condition` until the plugin verifier named by
        `check` (a registered "<plugin-id>:<name>" verifier) passes. `check_args` is
        declarative data the verifier reads (e.g. {"min": 1000000}). Only plugin
        verifiers are allowed — shell/test/data goals are operator-only via /goal.
        Returns the goal status, or an error if goal mode is off / `check` is unknown.
        """
        from runtime.state import STATE

        if STATE.goal_controller is None:
            return "Goal mode is not enabled."
        session_id = _session_id_from(state)  # injected graph state, not the contextvar
        if not session_id:
            return "No active session — set_goal can only run during a turn."
        # Reject an unknown verifier up front. Otherwise the goal is created but can
        # never pass — it just spins to the iteration cap, flagged 'unachievable'
        # (the live failure mode). List the registered ones so the agent can choose.
        from graph.goals.verifiers import plugin_verifier_names

        known = plugin_verifier_names()
        if check not in known:
            avail = ", ".join(known) if known else "(none registered — enable a plugin that contributes a verifier)"
            return f"Error: unknown plugin verifier {check!r}. Available verifiers: {avail}."
        verifier = {"type": "plugin", "check": check, "args": check_args or {}}
        _ok, msg = STATE.goal_controller.set_goal_safe(
            session_id,
            condition,
            verifier,
            max_iterations,
        )
        return msg

    return set_goal


def _build_list_verifiers_tool():
    """Catalog every goal/watch verifier available on this instance."""

    @tool
    async def list_verifiers() -> str:
        """List all verifier types and plugin checks registered on this instance.

        Returns core verifier types (command, test, ci, data, llm, plugin) for your
        AWARENESS — they exist and an operator can use any of them — then any
        plugin-contributed checks with their <plugin-id>:<name> identifier and
        description. Only the plugin-contributed checks are valid for YOUR set_goal
        or create_watch calls; both tools accept a plugin verifier name only, never
        a core type directly. If no plugin verifiers are registered, set_goal and
        create_watch have nothing they can use yet — say so rather than trying one
        of the core types, which will fail with "unknown plugin verifier".
        """
        from graph.goals.verifiers import verifier_catalog

        catalog = verifier_catalog()
        lines: list[str] = ["Core verifier types (informational — operator-only, not usable by your set_goal/create_watch):"]
        for t in catalog["types"]:
            lines.append(f"  {t['value']}: {t['description']}")
        lines.append("")
        checks = catalog["plugin_checks"]
        if checks:
            lines.append("Plugin verifiers (usable by your set_goal/create_watch):")
            for c in checks:
                lines.append(f"  {c['name']}: {c['description']}")
        else:
            lines.append(
                "No plugin verifiers registered. set_goal and create_watch have nothing they "
                "can use right now — don't pass a core type (e.g. 'ci') to either; it will fail."
            )
        return "\n".join(lines)

    return list_verifiers


def _build_goal_plan_tool():
    """The agent records its running plan for the active goal (goal loop). Replaces the
    retired ``<goal_plan>`` continuation tag — the plan is persisted to the goal state /
    plan artifact and fed back into the next continuation prompt."""

    @tool
    def update_goal_plan(
        plan: str,
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Record/refresh your running plan for THIS session's active goal.

        Call this during a goal continuation turn to carry a coherent plan across
        iterations (what's done, what's next, what failed) — it is persisted and fed
        back to you in the next continuation. Returns a message; it's a harmless no-op
        when goal mode is off or no goal is active.
        """
        from runtime.state import STATE

        if STATE.goal_controller is None:
            return "Goal mode is not enabled."
        session_id = _session_id_from(state)  # injected graph state, not the contextvar
        if not session_id:
            return "No active session — update_goal_plan can only run during a turn."
        _ok, msg = STATE.goal_controller.record_plan(session_id, plan)
        return msg

    return update_goal_plan


def _build_abandon_goal_tool():
    """The agent explicitly gives up on the active goal (goal loop). Replaces the retired
    ``<goal_unachievable/>`` tag — recorded on the goal state and honoured AFTER the
    verifier runs, so a goal the world already satisfies still finishes ``achieved``."""

    @tool
    def abandon_goal(
        reason: str,
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Flag THIS session's active goal as unachievable and stop the goal loop.

        Call this only when you determine the goal is impossible or out of scope, with a
        one-line `reason`. The goal is finished ``unachievable`` after your turn — unless
        the verifier finds it already met, which wins. Returns a message; it's a harmless
        no-op when goal mode is off or no goal is active.
        """
        from runtime.state import STATE

        if STATE.goal_controller is None:
            return "Goal mode is not enabled."
        session_id = _session_id_from(state)  # injected graph state, not the contextvar
        if not session_id:
            return "No active session — abandon_goal can only run during a turn."
        _ok, msg = STATE.goal_controller.request_abandon(session_id, reason)
        return msg

    return abandon_goal
