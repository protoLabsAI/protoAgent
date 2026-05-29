"""FastAPI route registration for the React operator console contracts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException
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
    project_path: str
    workspace: dict[str, Any]


class BeadsInitRequest(BaseModel):
    project_path: str
    prefix: str | None = None


class BeadsCreateRequest(BaseModel):
    project_path: str
    title: str
    type: str = "task"
    priority: int = 2
    description: str | None = None
    assignee: str | None = None


class BeadsUpdateRequest(BaseModel):
    project_path: str
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: int | None = None
    type: str | None = None
    assignee: str | None = None


class BeadsCloseRequest(BaseModel):
    project_path: str
    reason: str | None = None


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


def register_operator_routes(
    app,
    *,
    runtime_status: Callable[[], dict[str, Any]],
    subagent_list: Callable[[], list[dict[str, Any]]],
    subagent_run: Callable[[dict[str, Any]], Awaitable[str]],
    subagent_batch: Callable[[dict[str, Any]], Awaitable[str]],
    beads_service: BeadsService | None = None,
    notes_service: NotesService | None = None,
    allowed_dirs: Callable[[], list[str]] | None = None,
) -> None:
    """Register React operator-console routes on a FastAPI app.

    ``allowed_dirs`` is an accessor returning the directories the operator
    console may read/write (beads + notes). It's a callable, not a static
    list, so it re-reads live config after a settings reload. Injected
    services keep their own allowlist; it only wires the defaults.
    """
    beads = beads_service or BeadsService(allowed_dirs=allowed_dirs)
    notes = notes_service or NotesService(allowed_dirs=allowed_dirs)

    @app.get("/api/runtime/status")
    async def _runtime_status():
        return runtime_status()

    @app.get("/api/subagents")
    async def _subagents():
        return {"subagents": subagent_list()}

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
    async def _notes_get(project_path: str):
        try:
            workspace = await asyncio.to_thread(notes.load_workspace, project_path)
            return {"workspace": workspace}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/notes/workspace")
    async def _notes_save(req: NotesSaveRequest):
        try:
            await asyncio.to_thread(notes.save_workspace, req.project_path, req.workspace)
            return {"ok": True}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/beads/status")
    async def _beads_status(project_path: str):
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
    async def _beads_list(project_path: str):
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
    async def _beads_delete(issue_id: str, project_path: str):
        try:
            return await asyncio.to_thread(beads.delete, project_path, issue_id)
        except Exception as exc:
            raise _http_error(exc) from exc
