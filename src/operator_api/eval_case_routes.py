"""Operator-only, explicit draft/save promotion of turns (ADR 0126 D5)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from filelock import FileLock
from fastapi import HTTPException
from pydantic import BaseModel, Field

from evals.compare import resolve_tier
from evals.runner import INSTANCE_ID_PREFIX, instance_cases_path, load_instance_cases
from infra.paths import atomic_write, instance_paths
from runtime.state import STATE


class DraftRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=256)
    trace_id: str = Field(min_length=1, max_length=256)
    incognito: bool = False


class SaveRequest(BaseModel):
    case: dict


def _friction(session_id: str, trace_id: str) -> list[str]:
    # Do not call the plugin's reader: resolving its path can migrate a legacy
    # ledger. Drafting must be read-only, including on a fresh instance.
    path = (
        Path(os.environ["FRICTION_LOG"])
        if os.environ.get("FRICTION_LOG")
        else (instance_paths().store("friction") / "friction.jsonl")
    )
    summaries = []
    if path.is_file():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("session_id") == session_id and row.get("trace_id") == trace_id:
                    summaries.append(row.get("summary") or "Friction recorded")
    return summaries


async def _prompt(session_id: str, task_id: str) -> str:
    # The durable task transcript preserves the user's bubble, unlike the
    # model checkpoint which can contain attachment instructions or compaction.
    from a2a.server.tasks.database_task_store import TaskModel
    from sqlalchemy import select

    if STATE.a2a_task_engine is None:
        raise HTTPException(404, "Stored turn messages are unavailable")
    async with STATE.a2a_task_engine.connect() as conn:
        history = (
            await conn.execute(
                select(TaskModel.history).where(TaskModel.id == task_id, TaskModel.context_id == session_id)
            )
        ).scalar_one_or_none()
    for message in history or []:
        if (message.get("metadata") or {}).get("incognito"):
            raise HTTPException(409, "Incognito sessions cannot become eval cases")
    for message in history or []:
        if message.get("role") in ("ROLE_USER", "user"):
            texts = [p["text"] for p in message.get("parts", []) if isinstance(p.get("text"), str)]
            if texts and any(t.strip() for t in texts):
                return "".join(texts)
            break
    raise HTTPException(404, "The turn has no stored user prompt")


def _validate(case: dict) -> None:
    for key in ("id", "name", "category", "prompt"):
        if not isinstance(case.get(key), str) or not case[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if case.get("kind", "ask") not in ("ask", "stream"):
        raise ValueError("Promoted cases must have kind ask or stream")
    resolve_tier(case)
    asserted = False
    for key in ("expected_tools", "expected_any_tools", "forbidden_tools", "expected_patterns", "forbidden_patterns"):
        if key not in case:
            continue
        value = case[key]
        if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
            raise ValueError(f"{key} must be a list of non-empty strings")
        # An explicit [] is the runner's no-tools assertion.
        asserted |= bool(value) or key == "expected_tools"
    if "verify_rubric" in case:
        rubric = case["verify_rubric"]
        if not isinstance(rubric, dict) or not isinstance(rubric.get("criteria"), list) or not rubric["criteria"]:
            raise ValueError("verify_rubric needs non-empty criteria")
        if any(not isinstance(c, str) or not c.strip() for c in rubric["criteria"]):
            raise ValueError("Rubric criteria must be non-empty strings")
        threshold = rubric.get("threshold", 0.7)
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold <= 1:
            raise ValueError("Rubric threshold must be greater than zero and at most one")
        asserted = True
    if not asserted:
        raise ValueError("Case must assert something: tools, reply patterns, or a rubric")


def _save(case: dict) -> None:
    try:
        _validate(case)
        json.dumps(case, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    path = instance_cases_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock"):
        try:
            existing = load_instance_cases(path)
        except ValueError as exc:
            raise HTTPException(409, "Existing instance cases are malformed; repair them before saving") from exc
        if any(row["id"] == f"{INSTANCE_ID_PREFIX}{case['id']}" for row in existing):
            raise HTTPException(409, "A case with this id already exists")
        # Read raw records after validation to preserve their ids and all fields.
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        atomic_write(path, json.dumps([*raw, case], indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def register_eval_case_routes(app) -> None:
    """Inherit the server's default-deny operator auth, like feedback routes."""

    @app.post("/api/evals/cases/draft")
    async def draft(req: DraftRequest):
        if req.incognito:
            raise HTTPException(409, "Incognito sessions cannot become eval cases")
        store = STATE.telemetry_store
        turns = await asyncio.to_thread(store.session_feedback, req.session_id) if store else []
        matches = [t for t in turns if t["trace_id"] == req.trace_id]
        if len(matches) != 1:
            raise HTTPException(404, "No unambiguous stored turn matches session_id and trace_id")
        turn = matches[0]
        prompt = await _prompt(req.session_id, turn["task_id"])
        summaries = await asyncio.to_thread(_friction, req.session_id, req.trace_id)
        if turn["rating"] is None and not summaries:
            raise HTTPException(409, "The turn must be rated or carry friction")
        case = dict(
            id=f"turn-{uuid4().hex}",
            name="Promoted turn",
            category="instance",
            kind="ask",
            prompt=prompt,
            source_trace_id=req.trace_id,
            tier="gate",
        )
        if turn["rating"] == 1 and not summaries:
            from evals.verify import audit_entries_since

            entries = await asyncio.to_thread(audit_entries_since, "")
            case["expected_tools"] = sorted(
                {
                    e["tool"]
                    for e in entries
                    if e.get("trace_id") == req.trace_id and e.get("session_id") == req.session_id and e.get("tool")
                }
            )
        else:
            case["comment"] = "\n".join([*([turn["note"]] if turn["note"] else []), *summaries])
        return case

    @app.post("/api/evals/cases", status_code=201)
    async def save(req: SaveRequest):
        await asyncio.to_thread(_save, req.case)
        return {"ok": True, "case": req.case}
