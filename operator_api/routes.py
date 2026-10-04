"""FastAPI route registration for the React operator console contracts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import Body, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from graph.goals.types import MAX_GOAL_ITERATIONS, MAX_GOAL_NO_PROGRESS_LIMIT
from runtime.session_ids import SessionId, optional_session_id

log = logging.getLogger(__name__)

# The most tasks one ``POST /api/subagents/batch`` may fan out (#3973). Each task is a
# full subagent run (its own model calls and tool loop); the semaphore bounds how many
# run AT ONCE, but nothing bounded how many a single request could queue. 20 is well
# above any console use (the batch form sends a handful) and far below "a request that
# ties the agent up for hours". A longer list is a 422, not a silent truncation.
MAX_BATCH_TASKS = 20

# Background job ids are strictly ``bg-<12 hex>`` (background/store.py). Every
# ``/api/background/{job_id}`` route checks this before the id reaches the store.
_BG_JOB_ID = re.compile(r"bg-[a-f0-9]{12}")


class NotFoundError(LookupError):
    """The resource a route names does not exist — ``_http_error`` answers 404.

    Raised by the console handlers (a scheduler job that isn't there) and the task-store
    adapter (an issue id the store doesn't know), so a missing id is a 404 rather than a
    400 or an AttributeError/KeyError 500 (#3973)."""


class SubagentRunRequest(BaseModel):
    session_id: str = "manual-subagent"
    type: str = "researcher"
    description: str = ""
    prompt: str

    # A caller-chosen session id: same shape rule as every chat entry point. Blank is
    # passed through unchanged (it has always meant "no parent session").
    _check_session_id = field_validator("session_id")(optional_session_id)


class SubagentBatchTask(BaseModel):
    """One task of a manual subagent batch — the same keys the lead agent's ``task_batch``
    tool takes. ``type`` and ``subagent_type`` are aliases (``subagent_type`` wins)."""

    prompt: str = Field(min_length=1)
    description: str = ""
    type: str | None = None
    subagent_type: str | None = None


class SubagentBatchRequest(BaseModel):
    session_id: str = "manual-subagent"
    # At most MAX_BATCH_TASKS tasks (#3973) — see MAX_BATCH_TASKS for the cap's reasoning.
    # An empty list still reaches the batch runner, which answers its own 400.
    tasks: list[SubagentBatchTask] = Field(max_length=MAX_BATCH_TASKS)

    _check_session_id = field_validator("session_id")(optional_session_id)

    def payload(self) -> dict[str, Any]:
        """The handler's dict: unset per-task keys are dropped, so the batch runner's
        ``spec.get("type", "researcher")`` default still applies to a task without one."""
        return {
            "session_id": self.session_id,
            "tasks": [t.model_dump(exclude_none=True) for t in self.tasks],
        }


class ScheduleAddRequest(BaseModel):
    prompt: str
    schedule: str  # 5-field cron expression OR an ISO-8601 datetime
    job_id: str | None = None
    timezone: str | None = None  # IANA tz for cron eval (None = UTC)


def _schedule_problem(schedule: str | None, timezone: str | None) -> str | None:
    """Why a schedule / timezone a scheduler route was sent is malformed, or ``None``
    (#3973). A format check at the route, so the caller gets a clear 400 naming the
    field before anything reaches the backend; the backend still validates semantics (and
    normalises its own errors to 400, #3969). ``None`` means "not sent" — the update
    route's partial edit leaves that field alone."""
    if timezone:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(timezone)
        except Exception:  # noqa: BLE001 — ZoneInfoNotFoundError, ValueError, a bad path …
            return f"invalid timezone {timezone!r}: expected an IANA name like 'America/Chicago'"
    if schedule is None:
        return None
    text = schedule.strip()
    if not text:
        return "schedule cannot be empty"
    from scheduler.interface import is_cron, parse_iso_to_utc

    bad = f"invalid schedule {text!r} (malformed): expected a 5-field cron expression or an ISO-8601 datetime"
    if is_cron(text):
        from croniter import croniter

        return None if croniter.is_valid(text) else bad
    try:
        parse_iso_to_utc(text)
    except (ValueError, OverflowError):
        return bad
    return None


class GoalSetRequest(BaseModel):
    """``POST /api/goals`` (ADR 0066/0073). The wire shape is the one the route has always
    taken as a bare dict, now typed (#3973): every field is optional here because the
    handler owns the "required" answers (a 400 with its own message, as before), and
    unknown keys are ignored as they always were. What changes is that a WRONG type — a
    string ``max_iterations``, a list ``verifier`` — is a 422 at the door instead of a
    stored goal that breaks on its next turn."""

    session_id: str = ""
    condition: str | None = None
    verifier: dict[str, Any] | None = None
    max_iterations: int | None = Field(default=None, ge=1, le=MAX_GOAL_ITERATIONS, strict=True)
    no_progress_limit: int | None = Field(default=None, ge=1, le=MAX_GOAL_NO_PROGRESS_LIMIT, strict=True)
    outcome: str | None = None
    # A single string is still accepted and coerced to a 1-element list by the handler.
    constraints: list[str] | str | None = None
    boundaries: list[str] | str | None = None
    stop_when: str | None = None
    kick: bool = True


class ScheduleUpdateRequest(BaseModel):
    """A PARTIAL edit (#3957): every field is optional and an omitted one keeps the
    job's current value — so ``{"schedule": "0 17 * * *"}`` reschedules without
    restating the prompt. The route forwards only the fields the caller actually
    sent (``exclude_unset``), which is how an explicit ``"timezone": null`` (back to
    UTC) stays distinguishable from leaving the timezone alone."""

    prompt: str | None = None
    schedule: str | None = None  # 5-field cron expression OR an ISO-8601 datetime
    timezone: str | None = None  # IANA tz for cron eval (None = UTC)


class InboxAddRequest(BaseModel):
    text: str
    priority: str = "next"  # now | next | later
    source: str = ""
    dedup_key: str = ""


class TaskInitRequest(BaseModel):
    project_path: str = ""  # ignored — the tasks store is agent-global
    prefix: str | None = None


class TaskCreateRequest(BaseModel):
    project_path: str = ""  # ignored — the tasks store is agent-global
    title: str
    type: str = "task"
    priority: int = 2
    description: str | None = None
    assignee: str | None = None


class TaskUpdateRequest(BaseModel):
    project_path: str = ""  # ignored — the tasks store is agent-global
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: int | None = None
    type: str | None = None
    assignee: str | None = None


class TaskCloseRequest(BaseModel):
    project_path: str = ""  # ignored — the tasks store is agent-global
    reason: str | None = None


class ChatFormSubmitRequest(BaseModel):
    # A plugin composer-form's answers routed back to the plugin's on_submit (#1701 S2).
    callback_id: str
    session_id: str = ""
    answers: dict = {}


async def _sse_event_stream(
    subscribe: Callable[..., AsyncIterator[dict[str, Any]]],
    *,
    since: int | None = None,
    keepalive_s: float = 15.0,
) -> AsyncIterator[str]:
    """Frame bus events as SSE text for the ``/api/events`` response.

    Emits a ``: connected`` comment up front (so the client's ``onopen`` fires),
    then one ``id:``/``event:``/``data:`` frame per published event, with periodic
    ``: keepalive`` comments to hold the connection open through idle stretches. The
    ``id:`` is the bus seq — a reconnecting client passes it back as ``?since=`` to
    replay events it missed from the ring buffer (ADR 0039).
    """
    yield ": connected\n\n"
    agen = subscribe(since) if since is not None else subscribe()
    # The pending read OUTLIVES an idle window. ``wait_for(agen.__anext__())`` would cancel
    # it on timeout, and cancelling an async generator's step closes the generator: the bus
    # subscription ended at the first keepalive, so every console connection dropped after
    # 15s idle and reconnected — re-hydrating every store that refetches on reconnect, and
    # blinking a background delegation's progress card out each time.
    pending: asyncio.Future | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(agen.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=keepalive_s)
            if not done:
                yield ": keepalive\n\n"
                continue
            step, pending = pending, None
            try:
                evt = step.result()
            except StopAsyncIteration:
                break
            # One malformed bus event (no `event` key, a non-dict, a payload json can't
            # encode) must not end the console's only push channel (#3973): it is logged
            # and skipped, and the stream carries on with the next event.
            try:
                text = _sse_frame(evt)
            except Exception:  # noqa: BLE001 — per-event guard; see above
                log.warning("[events] skipping a malformed bus event: %r", evt, exc_info=True)
                continue
            yield text
    finally:
        if pending is not None:
            # The client left mid-wait: stop the read before closing the subscription
            # (closing a generator whose step is still running raises).
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
        await agen.aclose()


def _sse_frame(evt: dict[str, Any]) -> str:
    """One bus event as SSE text. Raises on a malformed event — the caller skips it."""
    seq = evt.get("seq")
    prefix = f"id: {seq}\n" if seq is not None else ""
    # Default (unnamed) SSE frame carrying the topic in the payload, so the client
    # routes by topic with wildcard matching (ADR 0039) — one catch-all `onmessage`
    # instead of per-name listeners. The `id:` lets EventSource auto-send Last-Event-ID
    # on reconnect → the route replays missed events from the ring buffer.
    frame = {"topic": evt["event"], "data": evt["data"]}
    if seq is not None:
        frame["seq"] = seq
    if isinstance(evt.get("ts"), (int, float)):
        frame["ts"] = evt["ts"]  # when it happened — a replaying client must not stamp it "now"
    return f"{prefix}data: {json.dumps(frame)}\n\n"


def _http_error(exc: Exception) -> HTTPException:
    """Map a handler's exception to an HTTP error.

    ``ValueError`` (the caller's input) is a 400 and ``NotFoundError`` a 404, both with the
    handler's own message; a "not loaded" ``RuntimeError`` is a 409. Anything else is a
    fault in our code: it is logged here with its traceback under a short error id, and the
    client gets a generic message carrying that id (#3973) — never ``str(exc)``, which
    leaked file paths and library internals to whoever made the request."""
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RuntimeError) and "not loaded" in str(exc).lower():
        return HTTPException(status_code=409, detail=str(exc))
    error_id = uuid.uuid4().hex[:8]
    log.error("[operator-api] request failed (error id %s)", error_id, exc_info=exc)
    return HTTPException(
        status_code=500,
        detail=f"Internal server error (error id {error_id}); the details are in the server log.",
    )


def _subagent_http_error(exc: Exception, session_id: str | None = None) -> HTTPException:
    """``_http_error`` for a subagent run, which fails on the MODEL hop more than anywhere
    else (#3957). A provider 429 used to come back as a 500 — "protoAgent is broken" —
    when it means "back off and retry"; a client's backoff only keys on the real status.
    Same mapping as ``/v1``: 429 mirrored (with ``Retry-After`` when the provider sent
    one), any other upstream failure or an unreachable gateway a 502; everything else
    keeps ``_http_error``'s 400/409/500.

    The upstream 429/502 carry ``POST /api/chat``'s OBJECT detail — ``{code, message,
    upstream_status, session_id, error_id}`` (#3991). A plain-string 502 is what the hub
    proxy answers for an agent that is not up yet, and the console retries THAT as a cold
    start (``isColdStart``): a string detail here made a subagent's model failure look
    like a booting agent, retried up to 25 times."""
    from graph.upstream_errors import upstream_error_type, upstream_http_status, upstream_status_in_chain

    status = upstream_http_status(exc)
    if status is None:
        return _http_error(exc)
    upstream = upstream_status_in_chain(exc)
    if status == 429:
        message = f"The model provider rate-limited this subagent run (upstream HTTP 429) — retry later. {exc}"
    elif upstream is not None:
        message = f"The model provider rejected this subagent run (upstream HTTP {upstream}). {exc}"
    else:
        message = f"The model gateway could not be reached for this subagent run. {exc}"
    error_id = uuid.uuid4().hex[:8]
    log.warning("[operator-api] subagent run failed with HTTP %s (error id %s): %s", status, error_id, message)
    headers = None
    retry_after = _retry_after(exc) if status == 429 else None
    if retry_after:
        headers = {"Retry-After": retry_after}
    return HTTPException(
        status_code=status,
        detail={
            "code": upstream_error_type(upstream),
            "message": message,
            "upstream_status": upstream,
            "session_id": session_id,
            "error_id": error_id,
        },
        headers=headers,
    )


def _retry_after(exc: BaseException | None) -> str | None:
    """The provider's ``Retry-After`` header off the first exception in the explicit
    ``__cause__`` chain whose ``response`` carries one, else ``None``. Best-effort."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen and len(seen) < 16:
        try:
            value = getattr(getattr(exc, "response", None), "headers", {}).get("retry-after")
        except Exception:  # noqa: BLE001 — a header we can't read is just absent
            value = None
        if value:
            return str(value)
        seen.add(id(exc))
        exc = exc.__cause__
    return None


def _model_payload(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


class _TaskStoreAdapter:
    """Adapts the in-process ``TaskStore`` to the method shape the task routes
    call. ``project_path`` is ignored — the store is a single instance-scoped
    board the agent + console share."""

    def __init__(self, store: Any):
        self._s = store

    @property
    def enabled(self) -> bool:
        return self._s is not None

    def status(self, project_path: str) -> dict[str, bool]:
        # Honest when no store is wired (#3973): the routes are still registered (they
        # answer 503), but the board is NOT initialized.
        return {"initialized": self.enabled}

    def init(self, project_path: str, prefix: str | None = None) -> dict[str, bool]:
        return {"initialized": True, "already_initialized": True}

    def list(self, project_path: str) -> list[dict[str, Any]]:
        return self._s.list()

    def create(self, project_path: str, issue: dict[str, Any]) -> dict[str, Any]:
        return self._s.create(
            str(issue.get("title", "")),
            description=issue.get("description") or "",
            priority=issue.get("priority") if issue.get("priority") is not None else 2,
            issue_type=issue.get("type") or issue.get("issue_type") or "task",
            assignee=issue.get("assignee") or "",
        )

    def update(self, project_path: str, issue_id: str, update: dict[str, Any]) -> dict[str, Any]:
        fields = {
            k: v
            for k, v in update.items()
            if k in ("title", "description", "status", "priority", "issue_type", "type", "assignee") and v is not None
        }
        try:
            return self._s.update(issue_id, **fields)
        except KeyError as exc:  # TaskStore's "unknown issue" — a 404, not a 500 (#3973)
            raise NotFoundError(f"No task {issue_id!r}.") from exc

    def close(self, project_path: str, issue_id: str, reason: str | None = None) -> dict[str, Any]:
        try:
            return self._s.close(issue_id, reason=reason)
        except KeyError as exc:
            raise NotFoundError(f"No task {issue_id!r}.") from exc

    def delete(self, project_path: str, issue_id: str) -> dict[str, Any]:
        # The store answers False for an id it doesn't have; that used to be a 200
        # `{deleted: false}`, indistinguishable from success to a client that reads the
        # status (#3973). The success body is unchanged.
        if not self._s.delete(issue_id):
            raise NotFoundError(f"No task {issue_id!r}.")
        return {"deleted": True}


def _check_bg_job_id(job_id: str) -> None:
    if not _BG_JOB_ID.fullmatch(job_id or ""):
        raise HTTPException(status_code=400, detail="Invalid background job id.")


def register_operator_routes(
    app,
    *,
    runtime_status: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]],
    subagent_list: Callable[[], list[dict[str, Any]]],
    tools_list: Callable[[], dict[str, Any]] = lambda: {"tools": [], "count": 0},
    subagent_run: Callable[[dict[str, Any]], Awaitable[str]],
    subagent_batch: Callable[[dict[str, Any]], Awaitable[str]],
    tasks_store: Any | None = None,
    allowed_dirs: Callable[[], list[str]] | None = None,
    scheduler_list: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    scheduler_add: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    scheduler_cancel: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    scheduler_update: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    goal_list: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    goal_clear: Callable[[str, bool], Awaitable[dict[str, Any]]] | None = None,
    goal_set: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    goal_rearm: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    goal_resume: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    watch_list: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    watch_clear: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    watch_set: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    watch_update: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    verifier_catalog: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    chat_commands: Callable[[], dict[str, Any]] | None = None,
    form_task_settle: Callable[[str], Awaitable[bool]] | None = None,
    events_subscribe: Callable[..., AsyncIterator[dict[str, Any]]] | None = None,
    events_publish: Callable[[str, dict[str, Any]], None] | None = None,
    activity_list: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    inbox_add: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    inbox_authorized: Callable[[str | None], bool] | None = None,
    inbox_list: Callable[[str, bool], Awaitable[dict[str, Any]]] | None = None,
    inbox_deliver: Callable[[int], Awaitable[dict[str, Any]]] | None = None,
) -> None:
    """Register React operator-console routes on a FastAPI app.

    ``allowed_dirs`` is DEAD — accepted so callers (and forks) that still pass it keep
    working, but no route reads it. It used to fence the tasks/notes path arguments via
    ``operator_api.paths.resolve_project_path``; tasks then became one instance-scoped
    store that ignores ``project_path`` entirely, and notes moved to a plugin. The
    agent's filesystem fence is ``filesystem.projects`` (ADR 0007), not this.
    """
    # The agent + console share one instance-scoped task board (in-process store).
    task_svc = _TaskStoreAdapter(tasks_store)

    def _require_tasks() -> None:
        # No store wired (#3973): the task routes stay registered — so the console gets
        # a clear answer, not a 404 that reads as "wrong server" — but every data route
        # answers 503 instead of an AttributeError 500, and /api/tasks/status reports
        # `initialized: false`.
        if not task_svc.enabled:
            raise HTTPException(status_code=503, detail="tasks not enabled")

    @app.get("/api/runtime/status")
    async def _runtime_status():
        # The console handler is async (it offloads the per-poll `ps` co-location
        # probe off the loop, #875); accept a plain dict too so sync test doubles
        # and forks that wire a sync accessor keep working.
        try:
            res = runtime_status()
            return await res if asyncio.iscoroutine(res) else res
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/subagents")
    async def _subagents():
        try:
            return {"subagents": subagent_list()}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/tools")
    async def _tools():
        return tools_list()

    @app.get("/api/background")
    async def _background_jobs(session: str = "", status: str = "", limit: int = 100):
        """Background subagent jobs (ADR 0050) — read-only list for the console.

        Filters by ``session`` (originating chat session) and/or ``status``
        (running|completed|failed|canceled — the store's ``STATUSES``; anything else is a
        400, #3973, where it used to quietly match nothing). Returns
        ``{"jobs": [...], "enabled": bool}``."""
        from background.store import STATUSES
        from runtime.state import STATE

        if status and status not in STATUSES:
            raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(STATUSES)}.")
        mgr = getattr(STATE, "background_mgr", None)
        if mgr is None:
            return {"jobs": [], "enabled": False}
        try:
            jobs = await asyncio.to_thread(
                mgr.store.list,
                origin_session=session or None,
                status=status or None,
                limit=max(1, min(int(limit), 500)),
            )
            return {"jobs": [j.to_dict() for j in jobs], "enabled": True}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/background/{job_id}")
    async def _background_job(job_id: str):
        """One background job's full row by id (ADR 0070 D4) — the console report
        card fetches this instead of list-and-filter, and it carries the FULL result
        (the ``background.completed`` bus event only carries a preview). Still resolves
        after the job is dismissed from the panel (#1808): dismiss is a soft flag, so the
        row + report are retained and the card can always reopen the full report. Job ids
        are strictly ``bg-<12 hex>`` (background/store.py), so anything else is rejected
        before it reaches the store."""
        from runtime.state import STATE

        _check_bg_job_id(job_id)
        mgr = getattr(STATE, "background_mgr", None)
        if mgr is None:
            raise HTTPException(status_code=404, detail="Background jobs are not available.")
        try:
            job = await asyncio.to_thread(mgr.store.get, job_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        if job is None:
            raise HTTPException(status_code=404, detail=f"No background job {job_id}.")
        return job.to_dict()

    @app.post("/api/background/{job_id}/cancel")
    async def _background_cancel(job_id: str):
        """Stop a running background job (ADR 0051) — cancels its detached A2A turn."""
        from runtime.state import STATE

        _check_bg_job_id(job_id)
        mgr = getattr(STATE, "background_mgr", None)
        if mgr is None:
            return {"ok": False, "detail": "Background jobs are not available."}
        try:
            return await mgr.cancel(job_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.delete("/api/background/{job_id}")
    async def _background_delete(job_id: str):
        """Dismiss a FINISHED background job from the panel (#1808). A SOFT dismiss, not a
        hard delete: the row + report are retained so the chat report card can still open the
        full report by id (ADR 0070 — the report outlives the disposable worker). Running jobs
        are kept — cancel them first. Returns ``{ok, deleted}`` (``deleted`` = a row was
        newly dismissed; the key is kept for API compatibility)."""
        from runtime.state import STATE

        _check_bg_job_id(job_id)
        mgr = getattr(STATE, "background_mgr", None)
        if mgr is None:
            return {"ok": False, "detail": "Background jobs are not available."}
        try:
            dismissed = await asyncio.to_thread(mgr.store.dismiss, job_id)
            return {"ok": True, "deleted": bool(dismissed)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/background/clear")
    async def _background_clear(session: str = ""):
        """Dismiss all FINISHED background jobs from the panel (#1808), optionally scoped to an
        originating ``session``. Soft, like the single dismiss — rows + reports are retained so
        they stay openable by id. Running jobs are kept. Returns ``{ok, cleared}``."""
        from runtime.state import STATE

        mgr = getattr(STATE, "background_mgr", None)
        if mgr is None:
            return {"ok": False, "detail": "Background jobs are not available."}
        try:
            cleared = await asyncio.to_thread(mgr.store.dismiss_finished, session or None)
            return {"ok": True, "cleared": int(cleared)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/subagents/run")
    async def _subagent_run(req: SubagentRunRequest):
        try:
            output = await subagent_run(_model_payload(req))
            return {"ok": True, "session_id": req.session_id, "output": output}
        except Exception as exc:
            raise _subagent_http_error(exc, req.session_id) from exc

    @app.post("/api/subagents/batch")
    async def _subagent_batch(req: SubagentBatchRequest):
        try:
            output = await subagent_batch(req.payload())
            return {"ok": True, "session_id": req.session_id, "output": output}
        except Exception as exc:
            raise _subagent_http_error(exc, req.session_id) from exc

    @app.get("/api/tasks/status")
    async def _tasks_status(project_path: str = ""):
        try:
            return await asyncio.to_thread(task_svc.status, project_path)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/tasks/init")
    async def _tasks_init(req: TaskInitRequest):
        _require_tasks()
        try:
            return await asyncio.to_thread(task_svc.init, req.project_path, req.prefix)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/tasks/issues")
    async def _tasks_list(project_path: str = ""):
        _require_tasks()
        try:
            issues = await asyncio.to_thread(task_svc.list, project_path)
            return {"issues": issues}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/tasks/issues")
    async def _tasks_create(req: TaskCreateRequest):
        _require_tasks()
        try:
            issue = await asyncio.to_thread(task_svc.create, req.project_path, _model_payload(req))
            return {"issue": issue}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.patch("/api/tasks/issues/{issue_id}")
    async def _tasks_update(issue_id: str, req: TaskUpdateRequest):
        _require_tasks()
        try:
            issue = await asyncio.to_thread(task_svc.update, req.project_path, issue_id, _model_payload(req))
            return {"issue": issue}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/tasks/issues/{issue_id}/close")
    async def _tasks_close(issue_id: str, req: TaskCloseRequest):
        _require_tasks()
        try:
            issue = await asyncio.to_thread(task_svc.close, req.project_path, issue_id, req.reason)
            try:
                from graph.self_improvement import dispatch_task_review

                dispatch_task_review(issue, reason=req.reason or "")
            except Exception:  # noqa: BLE001 — curation must never turn a successful close into a 500
                log.exception("[self-improvement] task-close review scheduling failed")
            return {"issue": issue}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.delete("/api/tasks/issues/{issue_id}")
    async def _tasks_delete(issue_id: str, project_path: str = ""):
        _require_tasks()
        try:
            return await asyncio.to_thread(task_svc.delete, project_path, issue_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    # --- Scheduler -----------------------------------------------------------
    # Registered only when the accessors are wired (server.py passes them over
    # the live scheduler backend). Lets the console list/create/cancel the jobs
    # the agent would otherwise only reach through its schedule_* tools.
    if scheduler_list is not None:

        @app.get("/api/scheduler/jobs")
        async def _scheduler_jobs():
            try:
                return await scheduler_list()
            except Exception as exc:
                raise _http_error(exc) from exc

    if scheduler_add is not None:

        @app.post("/api/scheduler/jobs")
        async def _scheduler_add(req: ScheduleAddRequest):
            if problem := _schedule_problem(req.schedule, req.timezone):
                raise HTTPException(status_code=400, detail=problem)
            try:
                return {"job": await scheduler_add(_model_payload(req))}
            except Exception as exc:
                raise _http_error(exc) from exc

    if scheduler_update is not None:

        @app.put("/api/scheduler/jobs/{job_id}")
        async def _scheduler_update(job_id: str, req: ScheduleUpdateRequest):
            if problem := _schedule_problem(req.schedule, req.timezone):
                raise HTTPException(status_code=400, detail=problem)
            try:
                # Only the fields the caller sent — the handler keeps the rest (#3957).
                return {"job": await scheduler_update(job_id, req.model_dump(exclude_unset=True))}
            except Exception as exc:
                raise _http_error(exc) from exc

    if scheduler_cancel is not None:

        @app.delete("/api/scheduler/jobs/{job_id}")
        async def _scheduler_cancel(job_id: str):
            try:
                res = await scheduler_cancel(job_id)
            except Exception as exc:
                raise _http_error(exc) from exc
            # Nothing was canceled because there was no such job → 404 (#3973). It used to
            # be a 200 `{canceled: false}`, which a client reading the status took as done.
            if isinstance(res, dict) and res.get("canceled") is False:
                raise HTTPException(status_code=404, detail=f"No scheduled job {job_id!r}.")
            return res

    # --- Goals ---------------------------------------------------------------
    # List goals across sessions + clear one. Goals are *set* in chat (`/goal`);
    # the console surface is a read + clear view.
    if goal_list is not None:

        @app.get("/api/goals")
        async def _goals():
            try:
                return await goal_list()
            except Exception as exc:
                raise _http_error(exc) from exc

    if goal_clear is not None:

        # `?close_tasks=true` also closes the goal's session-scoped task backlog (ADR 0079) —
        # used by the "Stop goal" action so a stopped goal leaves no orphaned open tasks.
        @app.delete("/api/goals/{session_id}")
        async def _goal_clear(session_id: SessionId, close_tasks: bool = False):
            try:
                return await goal_clear(session_id, close_tasks)
            except Exception as exc:
                raise _http_error(exc) from exc

    # One session's goal status + its durable plan artifact under the canonical plural
    # shape (D4 dedupe, ADR 0075) — replaces the retired singular `/api/goal/{session_id}`.
    # Reads the controller directly, so it degrades to {enabled: False} when goals are off;
    # no injected fn. `plan` is the `.plan.md` the agent maintains with `update_goal_plan`
    # (its "orient" world-model, ADR 0079) — "" when the goal hasn't recorded one. Powers
    # the console goal detail drawer; additive, so pre-existing callers are unaffected.
    @app.get("/api/goals/{session_id}")
    async def _goal_status(session_id: SessionId):
        from runtime.state import STATE

        if STATE.goal_controller is None:
            return {"enabled": False, "goal": None, "plan": ""}
        try:
            store = STATE.goal_controller.store
            state = await asyncio.to_thread(store.get, session_id)
            plan = await asyncio.to_thread(store.read_plan, session_id) if state else ""
            return {"enabled": True, "goal": state.to_dict() if state else None, "plan": plan or ""}
        except Exception as exc:
            raise _http_error(exc) from exc

    # Programmatic goal-set (ADR 0028 D3) — accepts ONLY a `plugin` verifier;
    # command/test/ci/data stay operator-only (/goal). 400 on a rejected verifier.
    if goal_set is not None:

        @app.post("/api/goals")
        async def _goal_set(req: GoalSetRequest):
            try:
                # Only the keys the caller sent — the handler's defaults (kick=True, …)
                # apply to the rest exactly as they did for the bare-dict body.
                res = await goal_set(req.model_dump(exclude_unset=True))
            except Exception as exc:
                raise _http_error(exc) from exc
            if not res.get("ok"):
                raise HTTPException(status_code=400, detail=res.get("error") or res.get("message"))
            return res

    # Goal lifecycle (ADR 0079) — re-arm: extend an active goal's budget, or reactivate a
    # terminal one and kick a fresh drive turn. Operator-tier by the `/api` ceiling. 400 on a
    # no-op (e.g. an active goal with no added iterations).
    if goal_rearm is not None:

        @app.post("/api/goals/{session_id}/rearm")
        async def _goal_rearm(session_id: SessionId, body: dict | None = Body(default=None)):
            try:
                res = await goal_rearm(session_id, body or {})
            except Exception as exc:
                raise _http_error(exc) from exc
            if not res.get("ok"):
                raise HTTPException(status_code=400, detail=res.get("error") or res.get("message"))
            return res

    # Detach-continue (ADR 0079): kick a headless continuation for an ACTIVE goal so it keeps
    # driving after the chat tab that was streaming it is closed. 400 when nothing is active.
    if goal_resume is not None:

        @app.post("/api/goals/{session_id}/resume")
        async def _goal_resume(session_id: SessionId):
            try:
                res = await goal_resume(session_id)
            except Exception as exc:
                raise _http_error(exc) from exc
            if not res.get("ok"):
                raise HTTPException(status_code=400, detail=res.get("error") or res.get("message"))
            return res

    # Watch surface (ADR 0067): read + clear + operator create/edit. POST and PATCH accept
    # ANY verifier — safe because /api is operator-tier by the ADR 0066 path ceiling.
    if watch_list is not None:

        @app.get("/api/watches")
        async def _watches():
            try:
                return await watch_list()
            except Exception as exc:
                raise _http_error(exc) from exc

    if watch_clear is not None:

        @app.delete("/api/watches/{watch_id}")
        async def _watch_clear(watch_id: str):
            try:
                return await watch_clear(watch_id)
            except Exception as exc:
                raise _http_error(exc) from exc

    if watch_set is not None:

        @app.post("/api/watches")
        async def _watch_set(body: dict):
            try:
                res = await watch_set(body or {})
            except Exception as exc:
                raise _http_error(exc) from exc
            if not res.get("ok"):
                raise HTTPException(status_code=400, detail=res.get("error") or res.get("message"))
            return res

    if watch_update is not None:

        @app.patch("/api/watches/{watch_id}")
        async def _watch_update(watch_id: str, body: dict):
            # PATCH, not PUT: only the keys present in the body change. An explicit `null`
            # clears a field (drop the deadline); an absent key leaves it alone.
            try:
                res = await watch_update(watch_id, body or {})
            except Exception as exc:
                raise _http_error(exc) from exc
            if not res.get("ok"):
                raise HTTPException(status_code=400, detail=res.get("error") or res.get("message"))
            return res

    # Verifier catalog (ADR 0028/0067) — what a goal or watch can be checked WITH, from
    # every source. Read-only; the console's creators build their pickers from it instead of
    # hardcoding a list that drifts from the registry.
    if verifier_catalog is not None:

        @app.get("/api/verifiers")
        async def _verifiers():
            try:
                return await verifier_catalog()
            except Exception as exc:
                raise _http_error(exc) from exc

    # --- Slash commands ------------------------------------------------------
    # The chat console fetches the registered `/`-commands the server handles
    # (e.g. `/goal`) to drive its autocomplete. Static per server config.
    if chat_commands is not None:

        @app.get("/api/chat/commands")
        async def _chat_commands():
            try:
                return chat_commands()
            except Exception as exc:
                raise _http_error(exc) from exc

    # --- Mentions ------------------------------------------------------------
    # The composer's `@` autocomplete — who the operator can address directly (#3042).
    # Served from the SAME resolver the chat dispatcher routes with (``graph.mentions``,
    # which `server.chat_rooms._parse_at_delegate` also reads), so the roster offered can't drift from the
    # roster reached. Not gated on `chat_commands`: `@` addressing is independent of `/`
    # commands, and the roster is live config (delegates hot-reload), so it's read per
    # request.
    @app.get("/api/chat/mentions")
    async def _chat_mentions():
        from graph.mentions import resolve_mentions

        try:
            return {"mentions": resolve_mentions()}
        except Exception as exc:
            raise _http_error(exc) from exc

    # A plugin composer-form's answers route back to the plugin's on_submit here
    # (#1701 Slice 2) — the form itself rode the input_required frame with a
    # `plugin_callback_id`; the console POSTs the field values to redeem it. Not gated
    # on `chat_commands` (a plugin can open a form even if no static commands exist).
    @app.post("/api/chat/commands/submit")
    async def _chat_command_submit(req: ChatFormSubmitRequest):
        from graph.slash_commands import PluginFormRequest, submit_plugin_form

        try:
            result = await submit_plugin_form(req.callback_id, req.answers or {}, req.session_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        # A multi-step wizard returns the next form; anything else is a reply note.
        if isinstance(result, PluginFormRequest):
            return {"form": result.form, "callback_id": result.callback_id}
        if form_task_settle is not None and req.session_id:
            # the form's A2A task parked in input_required to deliver the card; the wizard
            # is done, so end it — or the session reads "waiting on the operator" forever
            try:
                await form_task_settle(req.session_id)
            except Exception:  # noqa: BLE001 — bookkeeping never fails the redeem
                pass
        return {"reply": result if isinstance(result, str) else None}

    # --- Workflows -----------------------------------------------------------
    # Workflows are an opt-in plugin (plugins/workflows) — it self-registers its
    # /api/plugins/workflows router; core no longer serves /api/workflows.

    # --- Activity thread -----------------------------------------------------
    # The durable Activity thread's history (ADR 0003). Agent-initiated turns
    # (scheduled fires, inbox items) land here; the console loads this when the
    # Activity surface opens and appends live via the `activity.message` event.
    if activity_list is not None:

        @app.get("/api/activity")
        async def _activity():
            try:
                return await activity_list()
            except Exception as exc:
                raise _http_error(exc) from exc

    # --- Inbound inbox -------------------------------------------------------
    # Authenticated intake for external stimuli (ADR 0003) — webhooks, scripts,
    # sister agents POST here. now-priority items fire an Activity turn; the
    # rest queue for the agent's check_inbox tool. Authed because an inbound
    # item can initiate an agent turn (and tool use).
    if inbox_add is not None:

        @app.post("/api/inbox")
        async def _inbox(req: InboxAddRequest, request: Request):
            if inbox_authorized is not None:
                header = request.headers.get("Authorization", "")
                token = header[7:].strip() if header[:7].lower() == "bearer " else None
                if not inbox_authorized(token):
                    raise HTTPException(status_code=401, detail="invalid or missing bearer token")
            try:
                return await inbox_add(_model_payload(req))
            except Exception as exc:
                raise _http_error(exc) from exc

    # Console-side inbox views (read + dismiss). Unauthenticated like the other
    # operator routes — only POST /api/inbox (external intake) is token-gated.
    if inbox_list is not None:

        @app.get("/api/inbox")
        async def _inbox_get(floor: str = "later", include_delivered: bool = False):
            try:
                return await inbox_list(floor, include_delivered)
            except Exception as exc:
                raise _http_error(exc) from exc

    if inbox_deliver is not None:

        @app.post("/api/inbox/{item_id}/deliver")
        async def _inbox_deliver(item_id: int):
            try:
                return await inbox_deliver(item_id)
            except Exception as exc:
                raise _http_error(exc) from exc

    # --- Event stream --------------------------------------------------------
    # Server→client SSE push channel (ADR 0003). The console keeps one of these
    # open for the app's lifetime; the server pushes unsolicited events
    # (activity messages, inbox items) the request-scoped chat stream can't.
    if events_subscribe is not None:

        @app.get("/api/events")
        async def _events(request: Request):
            # ?since=<seq> (or the SSE Last-Event-ID header) replays missed events
            # from the ring buffer on reconnect (ADR 0039).
            raw = request.query_params.get("since") or request.headers.get("last-event-id")
            try:
                since = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                since = None
            return StreamingResponse(_sse_event_stream(events_subscribe, since=since), media_type="text/event-stream")

    if events_publish is not None:

        @app.post("/api/events/publish")
        async def _events_publish(body: dict = Body(...)):
            """Publish an event to the bus from a client / plugin iframe (ADR 0039).

            The console relays sandboxed-iframe ``protoagent:publish`` messages here. Light
            guard (the no-cross-dependency clause): the topic must be namespaced (``<plugin>.<event>``)
            and must not contain subscription wildcards; payloads are size-capped. Bearer-gated
            like all of ``/api/*``."""
            topic = str(body.get("topic", "")).strip()
            data = body.get("data") or {}
            if not topic or "." not in topic:
                raise HTTPException(status_code=400, detail="topic must be namespaced as <plugin>.<event>")
            if "*" in topic or "#" in topic:
                raise HTTPException(status_code=400, detail="published topic cannot contain wildcards")
            if not isinstance(data, dict):
                raise HTTPException(status_code=400, detail="data must be an object")
            if len(json.dumps(data)) > 64 * 1024:
                raise HTTPException(status_code=413, detail="event payload too large (64KB cap)")
            events_publish(topic, data)
            return {"ok": True}
