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
    """One store row → the shape the dialog renders: the COMPOSED prompt split as
    ``system.stable`` / ``system.context``, the per-section budget rows, and the
    call's real token usage. ``system.wire_differs``/``system.wire`` (#2527)
    report what the wire ACTUALLY carried when a provider transform changed it —
    the composed concatenation is only byte-for-byte what the model received
    when ``wire_differs`` is false (that claim used to be unconditional, and
    #2519 proved it wrong)."""
    wire = row.get("wire_text")
    return {
        "call_index": int(row.get("call_index") or 0),
        "ts": row.get("ts") or "",
        "model": row.get("model") or "",
        "system": {
            "stable": row.get("stable_text") or "",
            "context": row.get("context_text") or "",
            "wire_differs": wire is not None,
            "wire": wire or "",
        },
        "sections": _sections(row),
        "subagent_type": row.get("subagent_type") or "",
        "usage": {
            "input_tokens": int(row.get("input_tokens") or 0),
            "output_tokens": int(row.get("output_tokens") or 0),
            "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
            "cache_creation_tokens": int(row.get("cache_creation_tokens") or 0),
        },
    }


def register_prompt_routes(app) -> None:
    """Register the ``/api/prompts/*`` read-only routes on ``app``."""

    # NOTE: the static paths (/last, /preview, /breakdown) are registered before
    # /{task_id} — FastAPI matches in registration order, and none of them must
    # be swallowed by the path param.
    @app.get("/api/prompts/breakdown")
    async def _api_prompts_breakdown(session_id: str = ""):
        """What the session's HISTORY is made of (#2843's audit, console-reachable):
        sizes the thread's checkpoint messages into categories — assistant text,
        tool-call args (counted exactly once, the graph.message_blocks contract),
        tool results, injected memory frames — plus per-tool totals and the biggest
        blocks. MOSTLY independent of ``prompts.capture``: it reads the checkpoint,
        not the snapshot store, so the SIZES (pure metadata) answer even with
        capture off — but the ``top_blocks`` previews are content slices, so a
        capture-locked console gets them redacted (the locked contract every
        sibling route honors, applied to the one content-bearing field).
        Cheap (one aget_state + string sums) — no speculative retrieval."""
        import asyncio

        from runtime.state import STATE

        sid = session_id.strip()
        if not sid:
            return {"found": False, "reason": "session_id required"}
        graph = STATE.graph
        aget_state = getattr(graph, "aget_state", None)
        if graph is None or aget_state is None:
            return {"found": False, "reason": "no live agent"}
        from operator_api.chat_routes import _resolve_thread_id

        try:
            snapshot = await aget_state({"configurable": {"thread_id": _resolve_thread_id(None, sid)}})
        except Exception:  # noqa: BLE001 — an unreadable thread is an answer, not a 500
            log.debug("[prompts] breakdown aget_state failed", exc_info=True)
            return {"found": False, "reason": "thread unreadable"}
        messages = list((getattr(snapshot, "values", None) or {}).get("messages") or [])
        if not messages:
            return {"found": False, "reason": "no history yet"}
        from graph.context_audit_op import audit_messages

        report = await asyncio.to_thread(audit_messages, messages, top_n=8)
        if not _capture_enabled():
            for block in report.get("top_blocks") or []:
                block["preview"] = ""
            report["previews_redacted"] = True
        return {"found": True, "breakdown": report}

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

    @app.get("/api/prompts/preview")
    async def _api_prompts_preview(session_id: str = ""):
        """The TRUE next-call preview (#2388 P3) — speculatively runs the dynamic
        layer (digest, hot memory, RAG keyed on the session's latest human message,
        skill index, working state) via ``KnowledgeMiddleware.compose_context``
        with ``record=False``, so previewing never writes an injection-log record.
        A distinct, explicit route rather than a ``/last`` default because the
        speculative retrieval is not free. ``call`` is null with a ``reason``
        when the live graph can't preview (no graph, no stamps, no session)."""
        import asyncio

        from runtime.state import STATE

        if not _capture_enabled():
            return {"enabled": False, "call": None, "reason": "capture disabled"}
        graph = STATE.graph
        parts = list(getattr(graph, "system_prompt_parts", None) or [])
        km = getattr(graph, "knowledge_middleware", None)
        if graph is None or not parts:
            return {"enabled": True, "call": None, "reason": "live graph has no prompt stamps"}

        state: dict = {"messages": [], "incognito": False}
        sid = session_id.strip()
        if sid:
            aget_state = getattr(graph, "aget_state", None)
            if aget_state is not None:
                try:
                    from operator_api.chat_routes import _resolve_thread_id

                    snapshot = await aget_state({"configurable": {"thread_id": _resolve_thread_id(None, sid)}})
                    state["messages"] = (getattr(snapshot, "values", None) or {}).get("messages") or []
                except Exception:  # noqa: BLE001 — a fresh/unreadable session previews without history
                    pass

        ctx = None
        if km is not None:
            try:
                ctx = await asyncio.to_thread(km.compose_context, state, None, record=False)
            except Exception:  # noqa: BLE001 — preview degrades to the stable half, honestly flagged
                log.debug("[prompts] speculative compose failed", exc_info=True)
        stable_text = "\n\n".join(text for _label, text in parts)
        row = {
            "call_index": -1,
            "ts": "",
            "model": "",
            "stable_text": stable_text,
            "context_text": (ctx or {}).get("context") or "",
            "stable_sections": [{"label": label, "chars": len(text)} for label, text in parts],
            "context_sections": (ctx or {}).get("context_sections"),
        }
        shaped = _shape(row)
        shaped["preview"] = True
        # Delivery note (#2527): the preview is a composed reconstruction — say how
        # the native-OAuth providers actually ship it so the surface doesn't imply
        # a system-role message that never exists on that wire.
        provider = (getattr(STATE.graph_config, "model_provider", "") or "").strip().lower()
        if provider == "openai-codex":
            shaped["delivery"] = "Delivered via the Responses `instructions` field (openai-codex)."
        elif provider == "anthropic-oauth":
            shaped["delivery"] = (
                "Delivered as the system message with the Claude Code identity line prepended (anthropic-oauth)."
            )
        return {"enabled": True, "call": shaped, "reason": ""}

    @app.get("/api/prompts/{task_id}")
    async def _api_prompts_for_task(task_id: str):
        """Every captured model call of one A2A turn, in call order — the
        "View prompt" dialog's payload (one tab per call). 404 when the task
        has no snapshots (never captured, trimmed by retention, or purged).

        #2388 P3 additions, both additive: ``subagents`` — calls captured under
        this turn's delegating tool-call ids (nested tabs); ``prev`` — the
        previous turn's last main-loop call in the same session (the diff
        anchor), null when there is none (first turn, incognito gap, retention)."""
        import asyncio

        from fastapi.responses import JSONResponse

        from observability.prompt_snapshots import prompt_snapshots

        if not _capture_enabled():
            return {"enabled": False, "calls": []}
        store = prompt_snapshots()
        rows = await asyncio.to_thread(store.calls_for_task, task_id)
        if not rows:
            return JSONResponse({"detail": "no prompt snapshots for that task"}, status_code=404)
        sub_rows = await asyncio.to_thread(store.calls_for_parent, task_id)
        prev = await asyncio.to_thread(
            store.previous_main_call, rows[0].get("session_id") or "", rows[0].get("ts") or ""
        )
        return {
            "enabled": True,
            "calls": [_shape(r) for r in rows],
            "subagents": [_shape(r) for r in sub_rows],
            "prev": _shape(prev) if prev else None,
        }
