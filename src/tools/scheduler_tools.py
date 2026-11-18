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

from scheduler.cron_validation import cron_ambiguity_problem
from scheduler.interface import is_cron, parse_ttl
from tools.session import _origin_session_from, _session_id_from

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


def _publish_scheduler_changed(job_id: str, action: str) -> None:
    """Best-effort bus push mirroring the console's cancel publish
    (``operator_api.console_handlers._publish_scheduler_changed``), so an agent-side
    ``cancel_schedule`` reaches the same subscribers — the console's Schedules refresh and
    the directive link auto-fulfil (``graph.directive_autofulfil``), which without it never
    learned a linked job was canceled. No-op when the host hasn't wired a publisher (unit
    tests / standalone use); a bus hiccup must never break the cancel itself."""
    try:
        from graph.plugins.host import HOST

        if HOST.publish:
            HOST.publish("scheduler.changed", {"job_id": job_id, "action": action})
    except Exception:  # noqa: BLE001
        pass


def _plugin_job_owner(job_id: str) -> str | None:
    """The plugin id owning ``job_id``, or ``None`` for an agent-created job (#5313).

    Plugin-owned jobs carry ``plugin:<plugin_id>:<job>`` ids (``graph.sdk.plugin_job_prefix``)
    and are re-armed by the plugin's idempotent ``schedule_recurring`` on its next register,
    so an agent-side cancel can't stick. The owner is the segment between the first and
    second colon — ``""`` for a malformed ``plugin:`` id, which is still not the agent's."""
    from graph.sdk import _PLUGIN_JOB_PREFIX  # the one literal plugin_job_prefix builds from

    jid = (job_id or "").strip()
    if not jid.startswith(_PLUGIN_JOB_PREFIX):
        return None
    return jid[len(_PLUGIN_JOB_PREFIX) :].split(":", 1)[0]


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


# Turn-ending guidance for the SUCCESS results of ``schedule_task`` (#4651). Seen in QA:
# after image tool calls failed with gateway 524s the agent scheduled a retry and its ENTIRE
# reply was "I'll pick this back up in 5 minutes" — the user was never told anything had
# failed. These lines nudge the model to say what happened and what will run before the turn
# ends, instead of yielding silently.
#
# They go on the RESULT of ``schedule_task`` because that tool does NOT yield the turn: the
# model is re-invoked after it runs and reads the result, so the guidance reaches it. (``wait``
# is different — ``WaitYieldMiddleware`` jumps the turn to ``end`` the moment a successful wait
# ToolMessage lands, so the model never sees the wait result that turn; its guidance therefore
# lives in the ``wait`` DOCSTRING, which the model reads before it calls the tool.)
#
# Appended AFTER the machine fact on its own line so the "Scheduled job … next at …" prefix
# that callers key off is untouched, and so the user-facing silent-turn fallback can strip it
# back to the clean first line (``schedule_turn_reply`` in graph/middleware/wait_yield.py) —
# these are model-only instructions and must never surface to the operator. Cron schedules
# carry a lighter version — a recurring sweep usually isn't a retry-after-failure, so it only
# needs "say what was scheduled".
_ONESHOT_END_TURN_GUIDANCE = (
    "\nBefore ending your turn, tell the user in your reply what happened (e.g. what failed) "
    'and what will run when this fires — never reply with only "I\'ll pick this back up".'
)
_CRON_END_TURN_GUIDANCE = "\nBefore ending your turn, tell the user what was scheduled and when it will run."


def _schedule_expiry_description(job) -> str:
    from scheduler.interface import job_expires_at

    expiry = job_expires_at(job)
    description = f" Expires at {expiry.isoformat()}." if expiry else " No time expiry."
    if getattr(job, "max_fires", None) is not None:
        description += f" Auto-cancels after {job.max_fires} fires."
    return description


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
        # once and are removed. Returns "Scheduled job <id> next at <iso>." (plus a line of
        # turn-ending guidance, #4651) or an error string.
        """Schedule a future turn: ``prompt`` arrives as a new turn when it fires (reminders, recurring sweeps, check-ins).

        Args:
            prompt: self-contained instruction — you'll have no memory of scheduling it.
                For a recurring check, tell it to end the reply with a final line ``NO_UPDATE``
                when nothing changed: such a run posts no result card back to this chat (it is
                still recorded in Activity), and a run repeating the last delivered result is
                not re-posted either.
            when: 5-field cron (``"0 9 * * 1-5"``) or ISO-8601 datetime for a one-shot. Compute
                times with ``current_time``.
            job_id: optional id (auto-generated); needed to cancel.
            timezone: IANA zone the cron is evaluated in (DST-aware); default UTC; ignored for ISO.
            ttl: recurring lifetime, e.g. ``"7d"``, ``"24h"``, ``"P7D"``. Cron jobs default to 7d —
                set longer for long-lived work, or ``"none"`` for no TTL only in an operator chat turn.
            max_fires: auto-cancel after this many fires.

        For "check back in N seconds" use ``wait``.

        Before you end the turn, tell the user what you scheduled — and if it follows a failure,
        what failed and what will run when it fires; never reply with only "I'll pick this back up".
        """
        if problem := cron_ambiguity_problem(when):
            return f"Error: {problem}"
        # Validate expiry knobs up front — a clean error string beats a
        # backend-specific failure surfacing later.
        no_expiry = isinstance(ttl, str) and ttl.strip().lower() == "none"
        if no_expiry and (
            not isinstance(state, dict)
            or state.get("operator_chat_turn") is not True
            or state.get("subagent_fence")
        ):
            return 'Error: ttl="none" requires an operator chat turn without a subagent fence.'
        if no_expiry:
            ttl = None  # The existing nullable TTL column represents no expiry.
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
        if ttl is None and not no_expiry and is_cron(when):
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
                        + _schedule_expiry_description(j)
                    )
        except Exception:  # noqa: BLE001 — dedup is best-effort; never block scheduling
            pass
        # Two DISTINCT session stamps derive from the turn:
        #  - ``context_id`` controls WHERE the fire RUNS (ADR 0053). A one-shot resumes
        #    the ORIGINATING chat; a cron deliberately passes none, and the scheduler's
        #    ``job_sessions`` assigns it a durable isolated ``job:`` execution owner
        #    (ADR 0129) — recurring work runs neither in a chat the operator closed
        #    days ago nor on the shared Activity session.
        #  - ``origin_session`` is WHERE the RESULT is DELIVERED (#2990): the chat that
        #    created the schedule. Stamped for BOTH crons and one-shots so even a cron
        #    running in its own job session reports back to that conversation as a
        #    ScheduledReportCard. Read from the injected graph state, not the tracing
        #    contextvar (which reads empty in a tool body under LangGraph).
        # A schedule created outside a chat (Activity-origin turn) has ctx=None → no
        # origin, no delivery attempted (backward compatible).
        ctx = _session_id_from(state) or None
        context_id = ctx if not is_cron(when) else None
        # Server turns inherit delivery provenance without changing where the fire runs.
        origin_session = ctx
        if ctx and ctx.startswith(("job:", "background:")):
            origin_session = _origin_session_from(state) or None
        try:
            job = await asyncio.to_thread(
                scheduler.add_job,
                prompt,
                when,
                job_id=job_id,
                timezone=timezone,
                context_id=context_id,
                origin_session=origin_session,
                ttl=ttl,
                max_fires=max_fires,
            )
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"Error: scheduler add_job failed: {exc}"
        next_fire = job.next_fire or "(managed by remote scheduler)"
        # #4651: append turn-ending guidance so the model — re-invoked after this tool, which
        # unlike ``wait`` does NOT yield — reports back to the user instead of ending on only
        # "I'll pick this back up". Cron gets the lighter wording. The guidance is model-only; a
        # silent turn's user-facing fallback strips it via ``schedule_turn_reply``.
        guidance = _CRON_END_TURN_GUIDANCE if is_cron(when) else _ONESHOT_END_TURN_GUIDANCE
        expiry = _schedule_expiry_description(job) if is_cron(when) or ttl or max_fires else ""
        return f"Scheduled job {job.id} next at {next_fire}.{expiry}{guidance}"

    @tool
    async def list_schedules() -> str:
        """List this agent's scheduled jobs (id, next fire, expiry, prompt preview).

        A row ending ``(plugin-owned: <plugin_id>)`` belongs to that plugin: its cadence is
        changed in the plugin's config section, and ``cancel_schedule`` refuses it.
        """
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
            line = f"{j.id}  next={next_fire}  schedule={j.schedule!r}  {expiry}  {preview}"
            # #5313: flag plugin-owned rows so the agent knows BEFORE it tries that
            # ``cancel_schedule`` will refuse them — the plugin's config owns the cadence.
            owner = _plugin_job_owner(str(j.id))
            if owner is not None:
                line += f"  (plugin-owned: {owner})" if owner else "  (plugin-owned)"
            lines.append(line)
        return "\n".join(lines)

    @tool
    async def cancel_schedule(job_id: str) -> str:
        """Cancel a scheduled job by the id from ``schedule_task`` / ``list_schedules``.

        Plugin-owned jobs (``plugin:<plugin_id>:…`` ids) are refused: the plugin re-arms them,
        so change the cadence in that plugin's config section instead.
        """
        if not job_id or not job_id.strip():
            return "Error: job_id is required."
        # #5313: a plugin-owned job is re-armed by its plugin's next register (an idempotent
        # replace), so canceling here reported "Canceled" for a job that kept firing. Refuse
        # before touching the scheduler — no cancel, no scheduler.changed event.
        owner = _plugin_job_owner(job_id)
        if owner is not None:
            who = f"the `{owner}` plugin" if owner else "a plugin"
            where = f"Settings, plugin `{owner}`" if owner else "Settings"
            return (
                f"Error: {job_id.strip()} is owned by {who}, which controls this job's cadence and "
                "re-creates it whenever it registers — it was NOT canceled and will keep firing. "
                f"Change or disable it in that plugin's config section ({where}), not with "
                "cancel_schedule; tell the operator that is where the cadence changes."
            )
        try:
            ok = await asyncio.to_thread(scheduler.cancel_job, job_id)
        except Exception as exc:  # noqa: BLE001
            return f"Error: scheduler cancel_job failed: {exc}"
        if ok:
            # The same publish the console cancel makes: a canceled job never fires
            # `turn.finished` again, so this event is the only way its subscribers —
            # the Schedules view, a directive linking this job — ever learn it ended.
            _publish_scheduler_changed(job_id, "canceled")
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

        Never paste the returned status line or an ISO timestamp into chat; paraphrase briefly,
        and say WHY you're waiting and what happens on resume — never reply with only "I'll pick
        this back up".
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
        # resume into this thread — the agent wakes up with the conversation
        # history intact (ADR 0053). Empty (e.g. an Activity/background-origin
        # turn) → the scheduler's ``job_sessions`` assigns the wait its own
        # isolated ``job:`` execution owner (ADR 0129); it never lands on the
        # shared Activity session.
        # Read the originating session from the injected graph state, NOT the
        # tracing contextvar — the contextvar reads empty in a tool body under
        # LangGraph, which silently dropped this resume out of its chat.
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
        # #4651: NO turn-ending guidance is appended here. ``wait`` yields the turn —
        # ``WaitYieldMiddleware`` jumps to ``end`` as soon as this successful result lands, so
        # the model never reads it this turn. The "say what failed / what resumes" guidance
        # that fixes #4651 lives in this tool's DOCSTRING instead (the model reads it before it
        # calls ``wait``, and emits its reply text alongside the tool call). Keeping the result
        # clean also means the silent-turn fallback (``wait_turn_reply``) has nothing to strip.
        return f"Wait scheduled: {_humanize_duration(secs)}. Will resume to: {then}"

    return [schedule_task, list_schedules, cancel_schedule, wait]


# ADR 0129 S13: the one appended line when a task is set ``in_progress`` with nothing
# driving it — no unresolved link, no pending follow-up / watch / background job / running
# turn in its chat (D9: ``task-59`` sat ``in_progress`` for an afternoon with nothing that
# would ever bring the agent back to it). Keeps the model honest about leaving work parked:
# either link the work, schedule a follow-up, or park it ``waiting`` with an ask.
_UNDRIVEN_IN_PROGRESS_NOTE = (
    "\nNote: nothing is driving this task (no linked work running, no follow-up scheduled "
    "in its chat). Link the card/PR/job doing it, schedule a follow-up (wait or "
    "schedule_task), or set it waiting with an ask; otherwise a stall reminder fires after "
    "30 min."
)


def _undriven_in_progress_note(task: dict) -> str:
    """The stall-reminder note (ADR 0129 S13) when ``task`` is undriven, else ``""``.

    Uses the shared driver test (:func:`tasks.stall.is_driven` / :func:`default_sources`) so
    the warning never disagrees with the stall sweep or the Work view. Fail-quiet: a driver
    check that raises returns ``""`` — a best-effort live read that failed must never change
    a successful update or its result. Imported lazily so the host-free tasks module loads
    only when a task actually reaches ``in_progress``."""
    try:
        from tasks.stall import default_sources, is_driven

        if is_driven(task, sources=default_sources()):
            return ""
    except Exception:  # noqa: BLE001 — a failing driver read must not change the update
        return ""
    return _UNDRIVEN_IN_PROGRESS_NOTE


def _build_task_tools(tasks_store) -> list:
    """Bind the tasks issue tracker to a ``TaskStore`` (Sprint B) — the agent's
    in-process planning/task surface. Returns a list."""

    @tool
    def task_create(
        title: str,
        description: str = "",
        priority: int = 2,
        issue_type: str = "task",
        links: list[str] | None = None,
        state: Annotated[Any, InjectedState] = None,
        job_key: str = "",
    ) -> str:
        """Track a task/issue on your tasks board — your planning surface for
        multi-step work. ``priority`` 0=highest…3=low; ``issue_type`` is one of
        task|bug|feature|chore|epic. Returns the new or reused issue id.

        ``job_key``: a short stable name for this distinct job within the current
        request (for example "fix-ci" or "write-docs"). Reuse the same job_key when
        retrying the same job; leave it empty when not handling a request. Keys are
        stripped, lowercased, whitespace collapsed to "-", and limited to 80 chars.

        ``links``: ids/URLs of the work that delivers this task — board card ids
        (``bd-…``), scheduler job ids, a directive id, PR URLs. Link the cards/jobs
        that do the work so the task closes itself when every linked item finishes
        (with an attributed reason, e.g. "bd-c8r5h merged (#5146)"). When the next
        step is the operator's call, set status ``waiting`` via ``task_update``."""
        # Attribute the task to the session/goal that motivated it (ADR 0079) so a goal's
        # backlog is queryable; from injected graph state (current_session_id() is empty in a
        # tool body). Empty when there's no session context — an instance-global task, as before.
        session_id = _session_id_from(state) or ""
        correlation = {}
        if session_id.startswith("job:"):
            try:
                from runtime.state import STATE

                if STATE.inbox_store is not None:
                    correlation = STATE.inbox_store.request_for_context(session_id)
            except Exception:  # noqa: BLE001 — unavailable provenance must not prevent task creation
                pass
        if session_id.startswith(("job:", "background:")):
            session_id = _origin_session_from(state)
        normalized_job_key = "-".join(job_key.strip().lower().split())[:80]
        # Scope the promotion key to the inbox ITEM (the durable request), never to the
        # execution's message_id: a2a retires the execution identity on a turn that ends
        # failed/canceled (server/a2a.py _retire_inbox_execution → InboxStore.retire_execution),
        # so the item's next delivery mints a FRESH message_id for the same request. Keying on
        # message_id would therefore miss the row this job already has and create a duplicate on
        # a failed-then-retried job. item_id is the inbox primary key — stable across both a
        # restart (the inbox_executions row survives) and that retirement — so the same job
        # retried under either path reuses its one row. Never derived from title/time/origin.
        request = correlation.get("request") or {}
        item_id = request.get("item_id")
        promotion_key = f"inbox:{item_id}:{normalized_job_key}" if item_id and normalized_job_key else ""
        try:
            i = tasks_store.create(
                title,
                description=description,
                priority=priority,
                issue_type=issue_type,
                session_id=session_id,
                links=links,
                request=correlation.get("request"),
                requester=correlation.get("requester"),
                promotion_key=promotion_key,
            )
        except ValueError as exc:
            return f"Error: {exc}"
        if i.get("promoted_existing"):
            return f"Reused existing task {i['id']} for this job: {i['title']} ({i['issue_type']}, p{i['priority']})"
        return f"Created {i['id']}: {i['title']} ({i['issue_type']}, p{i['priority']})"

    @tool
    def task_list(include_closed: bool = False) -> str:
        """List issues on your tasks board (open ones by default). Use it to see
        and track outstanding work."""
        from tasks.store import parse_links

        items = tasks_store.list(include_closed=include_closed)
        if not items:
            return "No issues on the board."
        lines = []
        for i in items:
            line = f"[{i['status']}] {i['id']} (p{i['priority']}, {i['issue_type']}) {i['title']}"
            # Linked work (ADR 0129 Q1), with the already-finished refs ticked.
            refs = [f"{e['ref']}✓" if e.get("state") == "done" else e["ref"] for e in parse_links(i)]
            if refs:
                line += f"  links={','.join(refs)}"
            lines.append(line)
        return "\n".join(lines)

    @tool
    def task_get(issue_id: str) -> str:
        """Read a task's full description, linked work, and origin by its issue id."""
        from tasks.store import parse_links

        issue_id = issue_id.strip()
        if not issue_id:
            return "Error: issue_id is required."
        i = tasks_store.get(issue_id)
        if i is None:
            return f"Error: no task {issue_id}."
        lines = [
            f"Title: {i['title']}",
            f"Id: {i['id']}",
            f"Status: {i['status']}",
            f"Priority: {i['priority']}",
            f"Issue type: {i['issue_type']}",
            f"Description: {i.get('description') or ''}",
        ]
        if i.get("ask"):
            lines.append(f"Ask: {i['ask']}")
        refs = [f"{e['ref']}✓" if e.get("state") == "done" else e["ref"] for e in parse_links(i)]
        if refs:
            lines.append(f"Links: {', '.join(refs)}")
        for key, label in (("session_id", "Origin session id"), ("created_at", "Created"), ("updated_at", "Updated")):
            if i.get(key):
                lines.append(f"{label}: {i[key]}")
        return "\n".join(lines)

    @tool
    def task_update(
        issue_id: str,
        status: str = "",
        title: str = "",
        description: str = "",
        priority: int = -1,
        issue_type: str = "",
        links: list[str] | None = None,
        ask: str = "",
    ) -> str:
        """Update an issue. ``status`` is open|in_progress|blocked|waiting|deferred|closed.
        Leave a field empty (``priority`` -1, ``links`` omitted) to keep it unchanged.

        An ``in_progress`` task should be *driven* — have a linked card/PR/job in flight or
        a scheduled follow-up (``wait``/``schedule_task``) in its chat — or the result warns
        you (an undriven task gets a stall reminder after 30 min).

        Set ``waiting`` when the next step is the operator's call — don't leave a task
        ``in_progress`` while you wait on them. Supply ``ask``: a short, self-contained
        question or action for the operator, shown on the Needs you card so they can
        answer without opening the source chat. Required for ``waiting`` unless the
        task already has an ask. ``links`` REPLACES the task's linked
        work items (board card ids ``bd-…``, scheduler job ids, a directive id, PR
        URLs): link the cards/jobs that do the work so the task closes itself when
        they all finish. A ref the task already resolved stays finished through a
        replacement — and if every ref you keep has already finished, the task closes
        now with its attributed reason."""
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
        if links is not None:
            fields["links"] = links
        if ask.strip():
            fields["ask"] = ask.strip()
        try:
            if status.strip().lower() == "waiting" and not ask.strip():
                existing = tasks_store.get(issue_id)
                if existing is None:
                    raise KeyError(f"unknown issue {issue_id!r}")
                if not (existing.get("ask") or "").strip():
                    return "Error: provide an ask for the operator before setting status to waiting."
            i = tasks_store.update(issue_id, **fields)
        except (KeyError, ValueError) as exc:
            return f"Error: {exc}"
        result = f"Updated {i['id']}: [{i['status']}] {i['title']}"
        # ADR 0129 S13: a task now ``in_progress`` with nothing driving it earns a stall
        # warning so it isn't silently parked. The update already succeeded — this only
        # appends a line, and never raises (helper is fail-quiet).
        if (i.get("status") or "") == "in_progress":
            result += _undriven_in_progress_note(i)
        return result

    @tool
    def task_close(issue_id: str, reason: str = "") -> str:
        """Close an issue (done, or won't-do). Optional ``reason``."""
        try:
            i = tasks_store.close(issue_id, reason=reason or None)
        except (KeyError, ValueError) as exc:
            return f"Error: {exc}"
        # The self-improvement review is queued from the store's ``task.changed`` close
        # event (``tasks.autoclose``), the one path every close takes.
        return f"Closed {i['id']}: {i['title']}"

    return [task_create, task_list, task_get, task_update, task_close]


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
        N checks with unchanged evidence (watch stays active). A reaction replies as a visible turn
        in THIS chat — not a result card — so do NOT tell ``run_prompt`` to end with the
        ``NO_UPDATE`` quiet-reply line: nothing strips it here, and it would land verbatim in the
        chat. That convention is for recurring ``schedule_task`` runs, whose results post as cards.
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
