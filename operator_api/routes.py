"""FastAPI route registration for the React operator console contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from operator_api.beads import BeadsCommandError, BeadsService
from operator_api.notes import NotesService


class SubagentRunRequest(BaseModel):
    session_id: str = "manual-subagent"
    type: str = "researcher"
    description: str = ""
    prompt: str
    emit_skill: bool = False


class SubagentBatchRequest(BaseModel):
    session_id: str = "manual-subagent"
    tasks: list[dict[str, Any]]


class NotesSaveRequest(BaseModel):
    workspace: dict[str, Any]
    project_path: str = ""  # ignored — notes are agent-global; kept for back-compat


class ScheduleAddRequest(BaseModel):
    prompt: str
    schedule: str  # 5-field cron expression OR an ISO-8601 datetime
    job_id: str | None = None


class WorkflowRunRequest(BaseModel):
    inputs: dict[str, Any] = {}


class InboxAddRequest(BaseModel):
    text: str
    priority: str = "next"  # now | next | later
    source: str = ""
    dedup_key: str = ""


class BeadsInitRequest(BaseModel):
    project_path: str = ""  # ignored — the beads store is agent-global
    prefix: str | None = None


class BeadsCreateRequest(BaseModel):
    project_path: str = ""  # ignored — the beads store is agent-global
    title: str
    type: str = "task"
    priority: int = 2
    description: str | None = None
    assignee: str | None = None


class BeadsUpdateRequest(BaseModel):
    project_path: str = ""  # ignored — the beads store is agent-global
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: int | None = None
    type: str | None = None
    assignee: str | None = None


class BeadsCloseRequest(BaseModel):
    project_path: str = ""  # ignored — the beads store is agent-global
    reason: str | None = None


async def _sse_event_stream(
    subscribe: Callable[[], AsyncIterator[dict[str, Any]]],
    *,
    keepalive_s: float = 15.0,
) -> AsyncIterator[str]:
    """Frame bus events as SSE text for the ``/api/events`` response.

    Emits a ``: connected`` comment up front (so the client's ``onopen`` fires),
    then one ``event:``/``data:`` frame per published event, with periodic
    ``: keepalive`` comments to hold the connection open through idle stretches.
    """
    yield ": connected\n\n"
    agen = subscribe()
    try:
        while True:
            try:
                evt = await asyncio.wait_for(agen.__anext__(), timeout=keepalive_s)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            except StopAsyncIteration:
                break
            yield f"event: {evt['event']}\ndata: {json.dumps(evt['data'])}\n\n"
    finally:
        await agen.aclose()


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, RuntimeError) and "not loaded" in str(exc).lower():
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, BeadsCommandError):
        return HTTPException(status_code=500, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _model_payload(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


class _BeadsStoreAdapter:
    """Adapts the in-process ``BeadsStore`` (Sprint B) to the BeadsService method
    shape the beads routes call. ``project_path`` is ignored — the store is a
    single instance-scoped board the agent + console share (no `br` CLI, no
    per-project ``.beads/``)."""

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
            if k in ("title", "description", "status", "priority", "issue_type", "type", "assignee")
            and v is not None
        }
        return self._s.update(issue_id, **fields)

    def close(self, project_path: str, issue_id: str, reason: str | None = None) -> dict[str, Any]:
        return self._s.close(issue_id, reason=reason)

    def delete(self, project_path: str, issue_id: str) -> dict[str, Any]:
        return {"deleted": self._s.delete(issue_id)}


def register_operator_routes(
    app,
    *,
    runtime_status: Callable[[], dict[str, Any]],
    subagent_list: Callable[[], list[dict[str, Any]]],
    tools_list: Callable[[], dict[str, Any]] = lambda: {"tools": [], "count": 0},
    subagent_run: Callable[[dict[str, Any]], Awaitable[str]],
    subagent_batch: Callable[[dict[str, Any]], Awaitable[str]],
    beads_service: BeadsService | None = None,
    beads_store: Any | None = None,
    notes_service: NotesService | None = None,
    allowed_dirs: Callable[[], list[str]] | None = None,
    scheduler_list: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    scheduler_add: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    scheduler_cancel: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    goal_list: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    goal_clear: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    goal_set: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    chat_commands: Callable[[], dict[str, Any]] | None = None,
    workflows_list: Callable[[], dict[str, Any]] | None = None,
    workflows_run: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    workflows_save: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    workflows_delete: Callable[[str], dict[str, Any]] | None = None,
    events_subscribe: Callable[[], AsyncIterator[dict[str, Any]]] | None = None,
    activity_list: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    inbox_add: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    inbox_authorized: Callable[[str | None], bool] | None = None,
    inbox_list: Callable[[str, bool], Awaitable[dict[str, Any]]] | None = None,
    inbox_deliver: Callable[[int], Awaitable[dict[str, Any]]] | None = None,
) -> None:
    """Register React operator-console routes on a FastAPI app.

    ``allowed_dirs`` is an accessor returning the directories the operator
    console may read/write (beads + notes). It's a callable, not a static
    list, so it re-reads live config after a settings reload. Injected
    services keep their own allowlist; it only wires the defaults.
    """
    # Prefer the in-process store (Sprint B) — the agent + console share one
    # instance-scoped board; the `br` CLI service stays as a fallback for forks.
    beads = (
        _BeadsStoreAdapter(beads_store)
        if beads_store is not None
        else (beads_service or BeadsService(allowed_dirs=allowed_dirs))
    )
    notes = notes_service or NotesService(allowed_dirs=allowed_dirs)

    @app.get("/api/runtime/status")
    async def _runtime_status():
        return runtime_status()

    @app.get("/api/subagents")
    async def _subagents():
        return {"subagents": subagent_list()}

    @app.get("/api/tools")
    async def _tools():
        return tools_list()

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

    @app.get("/api/notes/workspace")
    async def _notes_get():
        try:
            workspace = await asyncio.to_thread(notes.load_workspace)
            return {"workspace": workspace}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/notes/workspace")
    async def _notes_save(req: NotesSaveRequest):
        try:
            await asyncio.to_thread(notes.save_workspace, req.workspace)
            return {"ok": True}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/beads/status")
    async def _beads_status(project_path: str = ""):
        try:
            return await asyncio.to_thread(beads.status, project_path)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/beads/init")
    async def _beads_init(req: BeadsInitRequest):
        try:
            return await asyncio.to_thread(beads.init, req.project_path, req.prefix)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/beads/issues")
    async def _beads_list(project_path: str = ""):
        try:
            issues = await asyncio.to_thread(beads.list, project_path)
            return {"issues": issues}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/beads/issues")
    async def _beads_create(req: BeadsCreateRequest):
        try:
            issue = await asyncio.to_thread(beads.create, req.project_path, _model_payload(req))
            return {"issue": issue}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.patch("/api/beads/issues/{issue_id}")
    async def _beads_update(issue_id: str, req: BeadsUpdateRequest):
        try:
            issue = await asyncio.to_thread(
                beads.update, req.project_path, issue_id, _model_payload(req)
            )
            return {"issue": issue}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/beads/issues/{issue_id}/close")
    async def _beads_close(issue_id: str, req: BeadsCloseRequest):
        try:
            issue = await asyncio.to_thread(beads.close, req.project_path, issue_id, req.reason)
            return {"issue": issue}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.delete("/api/beads/issues/{issue_id}")
    async def _beads_delete(issue_id: str, project_path: str = ""):
        try:
            return await asyncio.to_thread(beads.delete, project_path, issue_id)
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
    # List the registered workflow recipes and run one with inputs (ADR 0002).
    if workflows_list is not None:

        @app.get("/api/workflows")
        async def _workflows():
            try:
                return workflows_list()
            except Exception as exc:
                raise _http_error(exc) from exc

    if workflows_run is not None:

        @app.post("/api/workflows/{name}/run")
        async def _workflow_run(name: str, req: WorkflowRunRequest):
            try:
                return await workflows_run(name, req.inputs)
            except Exception as exc:
                raise _http_error(exc) from exc

    # Author a workflow from the console (validates, then saves to the writable
    # workflows dir; immediately runnable). The body is the recipe dict.
    if workflows_save is not None:

        @app.post("/api/workflows")
        async def _workflow_save(recipe: dict[str, Any]):
            try:
                return workflows_save(recipe)
            except Exception as exc:
                raise _http_error(exc) from exc

    if workflows_delete is not None:

        @app.delete("/api/workflows/{name}")
        async def _workflow_delete(name: str):
            try:
                return workflows_delete(name)
            except Exception as exc:
                raise _http_error(exc) from exc

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
        async def _events():
            return StreamingResponse(
                _sse_event_stream(events_subscribe), media_type="text/event-stream"
            )
