"""System-prompt snapshot routes (#2243).

The read surface over ``observability/prompt_snapshots.py`` — what the console's
"View prompt" dialog and the ``/prompt`` client command fetch to answer "what
did the model ACTUALLY receive on this turn?". Registrar-style
(``register_prompt_routes(app)``), matching ``register_injection_routes``.

Operator ``/api`` surface only — never ``/v1`` or A2A, and structurally outside
``/export`` (system messages are already excluded there).
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.server")


def _capture_enabled() -> bool:
    """The live ``prompts.capture`` setting. Off flips every route to
    ``{enabled: false}`` — same contract as the telemetry routes — so a
    restricted console that locks the key gets an honest, quiet surface."""
    from runtime.state import STATE

    return bool(getattr(STATE.graph_config, "prompt_capture_enabled", True))


def _sections(row: dict) -> list[dict]:
    """The call's labeled sections in prompt order — stable prefix first, then
    the dynamic tail — as ``{label, chars, approx_tokens, scope}`` rows (#2243
    P2). ``approx_tokens`` uses the chars/4 estimator (the injection-log
    precedent). Empty when the call was captured unsegmented (pre-P2 rows)."""
    out: list[dict] = []
    for scope, col in (("stable", "stable_sections"), ("context", "context_sections")):
        for s in row.get(col) or []:
            if not isinstance(s, dict):
                continue
            chars = int(s.get("chars") or 0)
            out.append(
                {
                    "label": str(s.get("label") or ""),
                    "chars": chars,
                    "approx_tokens": chars // 4,
                    "scope": scope,
                }
            )
    return out


def _shape(row: dict) -> dict:
    """One store row → the wire shape the dialog renders: the prompt split as
    ``system.stable`` / ``system.context`` (their concatenation is byte-for-byte
    what the model received), the per-section budget rows, and the call's real
    token usage."""
    return {
        "call_index": int(row.get("call_index") or 0),
        "ts": row.get("ts") or "",
        "model": row.get("model") or "",
        "system": {
            "stable": row.get("stable_text") or "",
            "context": row.get("context_text") or "",
        },
        "sections": _sections(row),
        "usage": {
            "input_tokens": int(row.get("input_tokens") or 0),
            "output_tokens": int(row.get("output_tokens") or 0),
            "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
            "cache_creation_tokens": int(row.get("cache_creation_tokens") or 0),
        },
    }


def register_prompt_routes(app) -> None:
    """Register the ``/api/prompts/*`` read-only routes on ``app``."""

    # NOTE: /last is registered before /{task_id} — FastAPI matches in
    # registration order, and "last" must not be swallowed by the path param.
    @app.get("/api/prompts/last")
    async def _api_prompts_last(session_id: str = ""):
        """The most recent captured model call — of one session when
        ``session_id`` is given, else across all sessions. Backs the client's
        ``/prompt`` command ("the prompt as of the last call" — exact and
        cheap; a true next-call preview would need speculative retrieval).
        ``call`` is null when nothing has been captured yet."""
        import asyncio

        from observability.prompt_snapshots import prompt_snapshots

        if not _capture_enabled():
            return {"enabled": False, "call": None}
        row = await asyncio.to_thread(prompt_snapshots().last_for_session, session_id.strip())
        return {"enabled": True, "call": _shape(row) if row else None}

    @app.get("/api/prompts/{task_id}")
    async def _api_prompts_for_task(task_id: str):
        """Every captured model call of one A2A turn, in call order — the
        "View prompt" dialog's payload (one tab per call). 404 when the task
        has no snapshots (never captured, trimmed by retention, or purged)."""
        import asyncio

        from fastapi.responses import JSONResponse

        from observability.prompt_snapshots import prompt_snapshots

        if not _capture_enabled():
            return {"enabled": False, "calls": []}
        rows = await asyncio.to_thread(prompt_snapshots().calls_for_task, task_id)
        if not rows:
            return JSONResponse({"detail": "no prompt snapshots for that task"}, status_code=404)
        return {"enabled": True, "calls": [_shape(r) for r in rows]}
