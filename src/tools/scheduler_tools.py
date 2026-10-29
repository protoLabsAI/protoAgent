"""Scheduler, task-board and watch tools — ``schedule_task`` / ``list_schedules`` /
``cancel_schedule`` / ``wait`` (bound to a ``SchedulerBackend`` by
``_build_scheduler_tools``), ``task_create`` / ``task_list`` / ``task_update`` /
``task_close`` (bound to a ``TaskStore`` by ``_build_task_tools``), and ``create_watch`` /
``list_watches`` / ``update_watch`` / ``clear_watch`` (``_build_watch_tools``, ADR 0067).

Split out of ``tools/lg_tools.py`` (#3830, epic #3804). ``lg_tools`` re-exports the
builders so existing imports — tests, plugins, forks — keep working; ``get_all_tools``
there is still the one assembly point.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from scheduler.interface import is_cron, parse_ttl
from tools.session import _session_id_from

log = logging.getLogger("protoagent.tools")  # same logger lg_tools used


# ── scheduler tools ──────────────────────────────────────────────────────────
#
# Three tools that bind to the local sqlite-backed scheduler — the agent loop
# sees one stable surface over the SchedulerBackend protocol.
#
# Multi-agent safety: the underlying backend is constructed in
# ``server.py`` with the active ``AGENT_NAME`` baked in. add_job /
# list_jobs / cancel_job all filter by that name so two protoAgent
# instances on the same machine never see each other's jobs.


def _humanize_duration(seconds: int) -> str:
    """Turn a raw second count into a short, human phrase (300 -> "5 minutes").

    Keeps the ``wait`` confirmation conversational so the agent can relay it
    naturally instead of parroting a machine-y "300s / ISO timestamp"."""
    s = max(1, int(seconds))
    if s < 60:
        return f"{s} second{'s' if s != 1 else ''}"
    mins, secs = divmod(s, 60)
    if mins < 60:
        parts = [f"{mins} minute{'s' if mins != 1 else ''}"]
        if secs:
            parts.append(f"{secs} second{'s' if secs != 1 else ''}")
        return " ".join(parts)
    hours, mins = divmod(mins, 60)
    parts = [f"{hours} hour{'s' if hours != 1 else ''}"]
    if mins:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
    return " ".join(parts)


def _build_scheduler_tools(scheduler) -> list:
    """Bind scheduler tools to a ``SchedulerBackend``. Returns a list."""

    @tool
    async def schedule_task(
        prompt: str,
        when: str,
        job_id: str | None = None,
        timezone: str | None = None,
        ttl: str | None = None,
        max_fires: int | None = None,
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        # Recurring (cron) schedules get a default 7-day TTL unless ``ttl``/``max_fires`` say
        # otherwise, so a forgotten schedule doesn't run forever; one-shot ISO schedules fire
        # once and are removed. Returns "Scheduled job <id> next at <iso>." or an error string.
        """Schedule a future turn: ``prompt`` arrives as a new turn when it fires (reminders, recurring sweeps, check-ins).

        Args:
            prompt: self-contained instruction — you'll have no memory of scheduling it.
            when: 5-field cron (``"0 9 * * 1-5"``) or ISO-8601 datetime for a one-shot. Compute
                times with ``current_time``.
            job_id: optional id (auto-generated); needed to cancel.
            timezone: IANA zone the cron is evaluated in (DST-aware); default UTC; ignored for ISO.
            ttl: recurring lifetime, e.g. ``"7d"``, ``"24h"``, ``"P7D"``. Cron jobs default to 7d —
                set longer for genuinely long-lived work.
            max_fires: auto-cancel after this many fires.

        For "check back in N seconds" use ``wait``.
        """
        # Validate expiry knobs up front — a clean error string beats a
        # backend-specific failure surfacing later.
        if ttl is not None:
            try:
                parse_ttl(ttl)
            except ValueError as exc:
                return f"Error: {exc}"
        if max_fires is not None and int(max_fires) < 1:
            return f"Error: max_fires must be >= 1, got {max_fires}."
        # Auto-expiry default (#2992): a recurring schedule the caller didn't
        # bound gets 7 days, so forgotten crons don't run forever. One-shots
        # fire once and are removed, so they carry no TTL.
        if ttl is None and is_cron(when):
            ttl = "7d"
        # Dedup guard: don't create a second job identical to an existing active
        # one (same prompt + schedule + timezone). This is the common cause of
        # scheduled-task spam — a loop that re-schedules itself on each run/restart
        # accumulates duplicates that all fire together. (Remote backends may return
        # [] here; then we skip the check and let the backend own dedup.)
        try:
            for j in await asyncio.to_thread(scheduler.list_jobs):
                if (
                    getattr(j, "enabled", True)
                    and (j.prompt or "").strip() == prompt.strip()
                    and j.schedule == when
                    and (getattr(j, "timezone", None) or None) == (timezone or None)
                ):
                    return (
                        f"Already scheduled as {j.id} (next at "
                        f"{j.next_fire or 'managed remotely'}). Not creating a duplicate."
                    )
        except Exception:  # noqa: BLE001 — dedup is best-effort; never block scheduling
            pass
        # Two DISTINCT session stamps derive from the same turn session id:
        #  - ``context_id`` controls WHERE the fire RUNS (ADR 0053). A one-shot resumes
        #    the ORIGINATING chat; a cron deliberately stays context-free so recurring
        #    work lands in Activity, not a chat the operator closed days ago.
        #  - ``origin_session`` is WHERE the RESULT is DELIVERED (#2990): the chat that
        #    created the schedule. Stamped for BOTH crons and one-shots so even a cron
        #    firing into Activity reports back to that conversation as a
        #    ScheduledReportCard. Read from the injected graph state, not the tracing
        #    contextvar (which reads empty in a tool body under LangGraph).
        # A schedule created outside a chat (Activity-origin turn) has ctx=None → no
        # origin, no delivery attempted (backward compatible).
        ctx = _session_id_from(state) or None
        context_id = ctx if not is_cron(when) else None
        try:
            job = await asyncio.to_thread(
                scheduler.add_job,
                prompt,
                when,
                job_id=job_id,
                timezone=timezone,
                context_id=context_id,
                origin_session=ctx,
                ttl=ttl,
                max_fires=max_fires,
            )
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"Error: scheduler add_job failed: {exc}"
        next_fire = job.next_fire or "(managed by remote scheduler)"
        return f"Scheduled job {job.id} next at {next_fire}."

    @tool
    async def list_schedules() -> str:
        """List this agent's scheduled jobs (id, next fire, expiry, prompt preview)."""
        jobs = await asyncio.to_thread(scheduler.list_jobs)
        if not jobs:
            return "No scheduled jobs."
        lines = []
        for j in jobs:
            preview = (j.prompt or "")[:80]
            next_fire = j.next_fire or "(managed remotely)"
            # Expiry state (#2992) — so the agent can see how long a schedule
            # has left before extending or capping it.
            ttl = getattr(j, "ttl", None) or "-"
            max_fires = getattr(j, "max_fires", None)
            fires = getattr(j, "fire_count", 0) or 0
            expiry = f"ttl={ttl}  max_fires={max_fires if max_fires is not None else '-'}  fires={fires}"
            lines.append(f"{j.id}  next={next_fire}  schedule={j.schedule!r}  {expiry}  {preview}")
        return "\n".join(lines)

    @tool
    async def cancel_schedule(job_id: str) -> str:
        """Cancel a scheduled job by the id from ``schedule_task`` / ``list_schedules``."""
        if not job_id or not job_id.strip():
            return "Error: job_id is required."
        try:
            ok = await asyncio.to_thread(scheduler.cancel_job, job_id)
        except Exception as exc:  # noqa: BLE001
            return f"Error: scheduler cancel_job failed: {exc}"
        return f"Canceled {job_id}." if ok else f"Error: cancel failed or no such job {job_id}."

    @tool
    async def wait(seconds: int, then: str, state: Annotated[Any, InjectedState] = None) -> str:
        # ADR 0053: the wake-up resumes in the SAME conversation with history intact.
        """Pause and resume LATER instead of polling: ENDS your turn now and re-invokes you in this conversation after ``seconds``.

        Use whenever you're waiting on something (a build, deploy, cooldown, an ETA a status tool
        gave) — never call a status tool repeatedly to wait it out. For an absolute time or a
        recurring schedule use ``schedule_task``.

        Args:
            seconds: ≥1; use the reported ETA, rounded up a little.
            then: self-contained instruction for when you wake — your only context then, so name
                the exact entities and steps.

        Never paste the returned status line or an ISO timestamp into chat; paraphrase briefly
        (e.g. "I'll check back in about 5 minutes.").
        """
        if not (then or "").strip():
            return "Error: `then` is required — describe what to do on resume."
        try:
            secs = max(1, int(seconds))
            when = (datetime.now(UTC) + timedelta(seconds=secs)).isoformat()
        except (OverflowError, ValueError):
            return "Error: seconds is outside the supported wake-up date range."
        # Resume in the SAME conversation: stamp the originating chat session
        # (== the turn's A2A contextId) onto the job so the scheduler fires the
        # resume into this thread, not the Activity thread — the agent wakes up
        # with the conversation history intact (ADR 0053). Same contextvar the
        # background-subagent path reads. Empty (e.g. an Activity-origin turn) →
        # the scheduler falls back to the Activity thread.
        # Read the originating session from the injected graph state, NOT the
        # tracing contextvar — the contextvar reads empty in a tool body under
        # LangGraph, which silently dropped this resume to the Activity thread.
        ctx = _session_id_from(state) or None
        # ONE pending wait per thread. A stable per-session job id means a new wait
        # SUPERSEDES a still-pending one instead of stacking beside it — the bug behind
        # "old resume messages catching up" (#1702): an agent that under-waited then
        # re-waited scheduled N overlapping wakes that all fired into this thread. The
        # cancel-then-add is safe because waits within a turn are sequential.
        wait_job_id = f"wait:{ctx or 'activity'}"
        superseded = False
        try:
            superseded = await asyncio.to_thread(scheduler.cancel_job, wait_job_id)
        except Exception:  # noqa: BLE001 — a stale/absent prior wait must not block the new one
            pass
        try:
            await asyncio.to_thread(scheduler.add_job, then, when, job_id=wait_job_id, context_id=ctx)
        except Exception as exc:  # noqa: BLE001
            return f"Error: couldn't schedule the wake-up: {exc}"
        # Observability (#1702): every wait is logged with its thread, delay, resume
        # snippet, and whether it replaced a pending wait — so a stacking loop is visible.
        log.info(
            "[wait] thread=%s in %ss%s → resume: %.80s",
            ctx or "activity",
            secs,
            " (superseded a pending wait)" if superseded else "",
            then,
        )
        # Concise, human-friendly summary — the agent should paraphrase this, not
        # echo it (the old ISO timestamp / "re-invoked at" phrasing read as raw
        # system text when parroted into chat). Machine-relevant facts stay intact:
        # the humanized duration and the resume instruction.
        return f"Wait scheduled: {_humanize_duration(secs)}. Will resume to: {then}"

    return [schedule_task, list_schedules, cancel_schedule, wait]


def _build_task_tools(tasks_store) -> list:
    """Bind the tasks issue tracker to a ``TaskStore`` (Sprint B) — the agent's
    in-process planning/task surface. Returns a list."""

    @tool
    def task_create(
        title: str,
        description: str = "",
        priority: int = 2,
        issue_type: str = "task",
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Track a task/issue on your tasks board — your planning surface for
        multi-step work. ``priority`` 0=highest…3=low; ``issue_type`` is one of
        task|bug|feature|chore|epic. Returns the new issue id."""
        # Attribute the task to the session/goal that motivated it (ADR 0079) so a goal's
        # backlog is queryable; from injected graph state (current_session_id() is empty in a
        # tool body). Empty when there's no session context — an instance-global task, as before.
        session_id = _session_id_from(state) or ""
        try:
            i = tasks_store.create(
                title, description=description, priority=priority, issue_type=issue_type, session_id=session_id
            )
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Created {i['id']}: {i['title']} ({i['issue_type']}, p{i['priority']})"

    @tool
    def task_list(include_closed: bool = False) -> str:
        """List issues on your tasks board (open ones by default). Use it to see
        and track outstanding work."""
        items = tasks_store.list(include_closed=include_closed)
        if not items:
            return "No issues on the board."
        return "\n".join(f"[{i['status']}] {i['id']} (p{i['priority']}, {i['issue_type']}) {i['title']}" for i in items)

    @tool
    def task_update(
        issue_id: str,
        status: str = "",
        title: str = "",
        description: str = "",
        priority: int = -1,
        issue_type: str = "",
    ) -> str:
        """Update an issue. ``status`` is open|in_progress|blocked|deferred|closed.
        Leave a field empty (``priority`` -1) to keep it unchanged."""
        fields: dict = {}
        if status:
            fields["status"] = status
        if title:
            fields["title"] = title
        if description:
            fields["description"] = description
        if priority is not None and priority >= 0:
            fields["priority"] = priority
        if issue_type:
            fields["issue_type"] = issue_type
        try:
            i = tasks_store.update(issue_id, **fields)
        except (KeyError, ValueError) as exc:
            return f"Error: {exc}"
        return f"Updated {i['id']}: [{i['status']}] {i['title']}"

    @tool
    def task_close(
        issue_id: str,
        reason: str = "",
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Close an issue (done, or won't-do). Optional ``reason``."""
        try:
            i = tasks_store.close(issue_id, reason=reason or None)
        except (KeyError, ValueError) as exc:
            return f"Error: {exc}"
        session_id = _session_id_from(state)
        try:
            from graph.self_improvement import dispatch_task_review

            dispatch_task_review(i, session_id=session_id, reason=reason)
        except Exception:  # noqa: BLE001 — curation must never break task closure
            log.exception("[self-improvement] task-close review scheduling failed")
        return f"Closed {i['id']}: {i['title']}"

    return [task_create, task_list, task_update, task_close]


def _build_watch_tools():
    """Watch primitive (ADR 0067) — the agent supervises MANY external conditions at once.
    Each watch is polled out-of-band; on met it can run a follow-up prompt back in this
    session. Plugin-verifier only (like set_goal): the agent can't open a shell/eval watch."""

    @tool
    def create_watch(
        condition: str,
        check: str,
        check_args: dict | None = None,
        run_prompt: str = "",
        watch_id: str | None = None,
        interval_s: float | None = None,
        expires_in_s: float | None = None,
        stall_after: int | None = None,
        repeat: bool = False,
        on_change: bool = False,
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        # ADR 0067. Default is a one-shot tripwire. ``repeat`` fires each time the condition
        # BECOMES true again (not once per check while it stays true). ``on_change`` implies
        # repeat. ``interval_s`` is a per-watch floor — never faster than the global cadence.
        # Without interval/expiry/stall knobs a watch polls at the default cadence until cleared.
        """Create a WATCH: poll ``condition`` via the plugin verifier ``check``; when met, run ``run_prompt`` (if given) as a follow-up turn in THIS session.

        One watch per thing (deploy, CI, metric); all poll in parallel. Only plugin verifiers are
        allowed — shell/test/data watches are operator-only. ``watch_id`` defaults to a slug of the
        condition. Fires once by default; ``repeat`` keeps it armed (fires on each re-entry into
        true); ``on_change`` fires whenever the checked value moves and always repeats. A repeating watch runs until its
        deadline or until cleared — set ``expires_in_s`` (seconds from now) unless you mean forever.
        ``interval_s``: seconds between checks (raise for slow things). ``stall_after``: signal after
        N checks with unchanged evidence (watch stays active).
        """
        from runtime.state import STATE

        if STATE.watch_controller is None:
            return "Watch mode is not available."
        session_id = _session_id_from(state)  # injected graph state, not the contextvar
        from graph.goals.verifiers import plugin_verifier_names

        known = plugin_verifier_names()
        if check not in known:
            avail = ", ".join(known) if known else "(none registered — enable a plugin that contributes a verifier)"
            return f"Error: unknown plugin verifier {check!r}. Available verifiers: {avail}."
        from graph.watches.controller import WatchController

        # RELATIVE deadline on the tool surface, absolute in the store. The operator API takes
        # an epoch/ISO `deadline`, but a model has no reliable "now" — asked for an ISO
        # timestamp it guesses, and a guess in the past expires the watch on its very first
        # tick. "Seconds from now" is a duration the model actually knows. The `float | None`
        # annotations already make the tool schema reject a non-numeric span or interval, so
        # the only check left is one the schema can't express: the span must be in the FUTURE.
        deadline = None
        if expires_in_s is not None:
            if expires_in_s <= 0:
                return "Error: expires_in_s must be positive — it's a span from now, not a timestamp."
            from time import time

            deadline = time() + expires_in_s
        ok, msg, _w = STATE.watch_controller.create(
            condition=condition,
            verifier={"type": "plugin", "check": check, "args": check_args or {}},
            watch_id=watch_id,
            interval_s=interval_s,
            deadline=deadline,
            stall_after=WatchController._parse_stall_after(stall_after),
            run_prompt=run_prompt or "",
            run_session=session_id or "",
            trigger="change" if on_change else "met",
            # A change monitor that stopped after one move would be a strange tripwire, so
            # on_change implies repeat rather than silently firing once.
            repeat=bool(repeat or on_change),
            trusted=False,
        )
        return msg

    @tool
    def list_watches() -> str:
        """List every watch for this agent (id · status · condition · verifier). Returns a
        human-readable summary, or a note when there are none."""
        from runtime.state import STATE

        if STATE.watch_controller is None:
            return "Watch mode is not available."
        watches = STATE.watch_controller.list_watches()
        return "\n".join(w.status_line() for w in watches) if watches else "No watches."

    @tool
    async def update_watch(
        watch_id: str,
        condition: str | None = None,
        run_prompt: str | None = None,
        interval_s: float | None = None,
        expires_in_s: float | None = None,
        stall_after: int | None = None,
        clear_deadline: bool = False,
        repeat: bool | None = None,
        on_change: bool | None = None,
    ) -> str:
        # Editing keeps stall history and evidence; clear+create resets them.
        """Adjust an existing watch in place — pass only the fields to change. Prefer this over clear + create (which resets its history).

        ``expires_in_s`` is measured from NOW; ``clear_deadline=true`` removes the expiry. Only
        plugin-verifier watches are editable, and a finished watch can't be — set a new one.
        """
        from runtime.state import STATE

        if STATE.watch_controller is None:
            return "Watch mode is not available."
        from graph.watches.controller import WatchController

        # Only the keys present here are touched — the controller leaves everything else
        # exactly as it was (its UNSET sentinel, not None, marks "not supplied").
        fields: dict = {}
        if condition is not None:
            fields["condition"] = condition
        if run_prompt is not None:
            fields["run_prompt"] = run_prompt
        if interval_s is not None:
            fields["interval_s"] = interval_s
        if stall_after is not None:
            fields["stall_after"] = WatchController._parse_stall_after(stall_after)
        if repeat is not None:
            fields["repeat"] = bool(repeat)
        if on_change is not None:
            fields["trigger"] = "change" if on_change else "met"
            if on_change:
                fields["repeat"] = True
        # Two ways to touch the deadline, because "None" on a tool argument means "not
        # supplied" — there's no way to spell "clear it" with a nullable number alone.
        if clear_deadline:
            if expires_in_s is not None:
                return "Error: pass either expires_in_s or clear_deadline, not both."
            fields["deadline"] = None
        elif expires_in_s is not None:
            if expires_in_s <= 0:
                return "Error: expires_in_s must be positive — it's a span from now, not a timestamp."
            from time import time

            fields["deadline"] = time() + expires_in_s
        if not fields:
            return "Error: nothing to update — pass at least one field to change."
        _ok, msg, _w = await STATE.watch_controller.update(watch_id, trusted=False, **fields)
        return msg

    @tool
    def clear_watch(watch_id: str) -> str:
        """Remove a watch by its id (from list_watches). Returns whether it existed."""
        from runtime.state import STATE

        if STATE.watch_controller is None:
            return "Watch mode is not available."
        cleared = STATE.watch_controller.clear(watch_id)
        return f"Watch {watch_id!r} cleared." if cleared else f"No watch {watch_id!r} to clear."

    return [create_watch, list_watches, update_watch, clear_watch]
