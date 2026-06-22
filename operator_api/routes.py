"""FastAPI route registration for the React operator console contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import Body, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


class SubagentRunRequest(BaseModel):
    session_id: str = "manual-subagent"
    type: str = "researcher"
    description: str = ""
    prompt: str


class SubagentBatchRequest(BaseModel):
    session_id: str = "manual-subagent"
    tasks: list[dict[str, Any]]


class ScheduleAddRequest(BaseModel):
    prompt: str
    schedule: str  # 5-field cron expression OR an ISO-8601 datetime
    job_id: str | None = None
    timezone: str | None = None  # IANA tz for cron eval (None = UTC)


class ScheduleUpdateRequest(BaseModel):
    prompt: str
    schedule: str  # 5-field cron expression OR an ISO-8601 datetime
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
    try:
        while True:
            try:
                evt = await asyncio.wait_for(agen.__anext__(), timeout=keepalive_s)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            except StopAsyncIteration:
                break
            seq = evt.get("seq")
            prefix = f"id: {seq}\n" if seq is not None else ""
            # Default (unnamed) SSE frame carrying the topic in the payload, so the client
            # routes by topic with wildcard matching (ADR 0039) — one catch-all `onmessage`
            # instead of per-name listeners. The `id:` lets EventSource auto-send Last-Event-ID
            # on reconnect → the route replays missed events from the ring buffer.
            frame = {"topic": evt["event"], "data": evt["data"]}
            if seq is not None:
                frame["seq"] = seq
            yield f"{prefix}data: {json.dumps(frame)}\n\n"
    finally:
        await agen.aclose()


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, RuntimeError) and "not loaded" in str(exc).lower():
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


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

    def status(self, project_path: str) -> dict[str, bool]:
        return {"initialized": True}

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
        return self._s.update(issue_id, **fields)

    def close(self, project_path: str, issue_id: str, reason: str | None = None) -> dict[str, Any]:
        return self._s.close(issue_id, reason=reason)

    def delete(self, project_path: str, issue_id: str) -> dict[str, Any]:
        return {"deleted": self._s.delete(issue_id)}


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
    goal_clear: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    goal_set: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    chat_commands: Callable[[], dict[str, Any]] | None = None,
    events_subscribe: Callable[..., AsyncIterator[dict[str, Any]]] | None = None,
    events_publish: Callable[[str, dict[str, Any]], None] | None = None,
    activity_list: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    inbox_add: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    inbox_authorized: Callable[[str | None], bool] | None = None,
    inbox_list: Callable[[str, bool], Awaitable[dict[str, Any]]] | None = None,
    inbox_deliver: Callable[[int], Awaitable[dict[str, Any]]] | None = None,
) -> None:
    """Register React operator-console routes on a FastAPI app.

    ``allowed_dirs`` is an accessor returning the directories the operator
    console may read/write (tasks + notes). It's a callable, not a static
    list, so it re-reads live config after a settings reload. Injected
    services keep their own allowlist; it only wires the defaults.
    """
    # The agent + console share one instance-scoped task board (in-process store).
    task_svc = _TaskStoreAdapter(tasks_store)

    @app.get("/api/runtime/status")
    async def _runtime_status():
        # The console handler is async (it offloads the per-poll `ps` co-location
        # probe off the loop, #875); accept a plain dict too so sync test doubles
        # and forks that wire a sync accessor keep working.
        res = runtime_status()
        return await res if asyncio.iscoroutine(res) else res

    @app.get("/api/subagents")
    async def _subagents():
        return {"subagents": subagent_list()}

    @app.get("/api/tools")
    async def _tools():
        return tools_list()

    @app.get("/api/background")
    async def _background_jobs(session: str = "", status: str = "", limit: int = 100):
        """Background subagent jobs (ADR 0050) — read-only list for the console.

        Filters by ``session`` (originating chat session) and/or ``status``
        (running|completed|failed). Returns ``{"jobs": [...], "enabled": bool}``."""
        from runtime.state import STATE

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

    @app.post("/api/background/{job_id}/cancel")
    async def _background_cancel(job_id: str):
        """Stop a running background job (ADR 0051) — cancels its detached A2A turn."""
        from runtime.state import STATE

        mgr = getattr(STATE, "background_mgr", None)
        if mgr is None:
            return {"ok": False, "detail": "Background jobs are not available."}
        try:
            return await mgr.cancel(job_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.delete("/api/background/{job_id}")
    async def _background_delete(job_id: str):
        """Delete a FINISHED background job's entry (housekeeping). Running jobs are kept —
        cancel them first. Returns ``{ok, deleted}``."""
        from runtime.state import STATE

        mgr = getattr(STATE, "background_mgr", None)
        if mgr is None:
            return {"ok": False, "detail": "Background jobs are not available."}
        try:
            deleted = await asyncio.to_thread(mgr.store.delete, job_id)
            return {"ok": True, "deleted": bool(deleted)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/background/clear")
    async def _background_clear(session: str = ""):
        """Delete all FINISHED background jobs (optionally scoped to an originating
        ``session``). Running jobs are kept. Returns ``{ok, cleared}``."""
        from runtime.state import STATE

        mgr = getattr(STATE, "background_mgr", None)
        if mgr is None:
            return {"ok": False, "detail": "Background jobs are not available."}
        try:
            cleared = await asyncio.to_thread(mgr.store.clear_finished, session or None)
            return {"ok": True, "cleared": int(cleared)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/subagents/run")
    async def _subagent_run(req: SubagentRunRequest):
        try:
            output = await subagent_run(_model_payload(req))
            return {"ok": True, "session_id": req.session_id, "output": output}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/subagents/batch")
    async def _subagent_batch(req: SubagentBatchRequest):
        try:
            output = await subagent_batch(_model_payload(req))
            return {"ok": True, "session_id": req.session_id, "output": output}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/tasks/status")
    async def _tasks_status(project_path: str = ""):
        try:
            return await asyncio.to_thread(task_svc.status, project_path)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/tasks/init")
    async def _tasks_init(req: TaskInitRequest):
        try:
            return await asyncio.to_thread(task_svc.init, req.project_path, req.prefix)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/tasks/issues")
    async def _tasks_list(project_path: str = ""):
        try:
            issues = await asyncio.to_thread(task_svc.list, project_path)
            return {"issues": issues}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/tasks/issues")
    async def _tasks_create(req: TaskCreateRequest):
        try:
            issue = await asyncio.to_thread(task_svc.create, req.project_path, _model_payload(req))
            return {"issue": issue}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.patch("/api/tasks/issues/{issue_id}")
    async def _tasks_update(issue_id: str, req: TaskUpdateRequest):
        try:
            issue = await asyncio.to_thread(task_svc.update, req.project_path, issue_id, _model_payload(req))
            return {"issue": issue}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/tasks/issues/{issue_id}/close")
    async def _tasks_close(issue_id: str, req: TaskCloseRequest):
        try:
            issue = await asyncio.to_thread(task_svc.close, req.project_path, issue_id, req.reason)
            return {"issue": issue}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.delete("/api/tasks/issues/{issue_id}")
    async def _tasks_delete(issue_id: str, project_path: str = ""):
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
            try:
                return {"job": await scheduler_add(_model_payload(req))}
            except Exception as exc:
                raise _http_error(exc) from exc

    if scheduler_update is not None:

        @app.put("/api/scheduler/jobs/{job_id}")
        async def _scheduler_update(job_id: str, req: ScheduleUpdateRequest):
            try:
                return {"job": await scheduler_update(job_id, _model_payload(req))}
            except Exception as exc:
                raise _http_error(exc) from exc

    if scheduler_cancel is not None:

        @app.delete("/api/scheduler/jobs/{job_id}")
        async def _scheduler_cancel(job_id: str):
            try:
                return await scheduler_cancel(job_id)
            except Exception as exc:
                raise _http_error(exc) from exc

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

        @app.delete("/api/goals/{session_id}")
        async def _goal_clear(session_id: str):
            try:
                return await goal_clear(session_id)
            except Exception as exc:
                raise _http_error(exc) from exc

    # Programmatic goal-set (ADR 0028 D3) — accepts ONLY a `plugin` verifier;
    # command/test/ci/data stay operator-only (/goal). 400 on a rejected verifier.
    if goal_set is not None:

        @app.post("/api/goals")
        async def _goal_set(body: dict):
            try:
                res = await goal_set(body or {})
            except Exception as exc:
                raise _http_error(exc) from exc
            if not res.get("ok"):
                raise HTTPException(status_code=400, detail=res.get("error") or res.get("message"))
            return res

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
