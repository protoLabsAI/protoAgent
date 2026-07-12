"""Chat / goal / health / OpenAI-compat routes.

The non-A2A HTTP chat surface: the operator console's `/api/chat`, session
retirement, goal-mode status/clear, the `/healthz` readiness probe (ADR 0010),
and the OpenAI-compatible `/v1/chat/completions` + `/v1/models` endpoints that
let this agent register as a model in the LiteLLM gateway / OpenWebUI. Extracted
from ``server._main`` (ADR 0023 phase 3) into a ``register_chat_routes(app, ui)``
registrar.

The turn logic lives in ``server.chat`` (``chat``); these handlers are the thin
HTTP layer over it. ``ui`` (the deployment tier) is passed in because
``/healthz`` echoes it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time

from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from runtime.state import STATE

log = logging.getLogger("protoagent.server")
from server import agent_name
from server.agent_init import _retire_thread
from server.chat import chat, compact_session, rewind_session


class ChatRequest(BaseModel):
    # Omitted/blank session_id → a unique per-call id is minted (ADR 0069 D4).
    # The old literal "api-default" pooled every anonymous caller into ONE
    # checkpointer thread and ONE session-memory file.
    message: str
    session_id: str = ""
    model: str | None = None  # per-tab model override; None → configured default
    # Incognito thread (ADR 0069 D3b): no session-memory persistence and no
    # memory injection for this turn. Additive, default False — existing
    # callers are unaffected.
    incognito: bool = False
    # This message answers a pending HITL form/question/approval (#1560): resume
    # the parked interrupt instead of running a fresh turn. Set by the console's
    # desktop /api/chat fallback — the streaming path carries the same flag as
    # A2A message metadata (`hitl_resume`). Additive, default False.
    hitl_resume: bool = False


_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _mint_session_id() -> str:
    """Unique per-call session id — ``api-<epoch-ms>-<6 base36>``, mirroring the
    console's ``chat-<ts>-<rand>`` shape (apps/web chat-store ``id()``)."""
    rand = "".join(secrets.choice(_B36) for _ in range(6))
    return f"api-{int(time.time() * 1000)}-{rand}"


def _split_openai_content(content) -> tuple[str, list[tuple[str, str]]]:
    """OpenAI ``message.content`` → ``(text, [(media_type, url)])`` (#1943).

    OpenAI multimodal messages carry ``content`` as a list of typed parts
    (``{"type": "text"}`` / ``{"type": "image_url"}``); assuming a plain string
    made ``/v1`` reject any image-attaching client with a ``.strip()`` crash.
    Text parts join with newlines; image parts keep the URL as-is (``data:`` or
    remote — both are what the turn layer forwards to a vision model). The
    media_type is parsed off a ``data:`` URI and empty otherwise, matching the
    ``[(media_type, uri)]`` shape of ``a2a_impl``'s ``_extract_image_parts``.
    """
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    images: list[tuple[str, str]] = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            texts.append(str(part.get("text", "")))
        elif kind == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if isinstance(url, str) and url:
                mt = ""
                if url.startswith("data:"):
                    mt = url[5:].split(";", 1)[0].split(",", 1)[0]
                images.append((mt, url))
    return "\n".join(texts), images


def register_chat_routes(app, ui: str) -> None:
    """Register the chat / goal / health / OpenAI-compat routes on ``app``.

    ``ui`` is the active deployment tier (full/console/none); ``/healthz`` echoes
    it so probes can see which surface is running.
    """

    # --- Chat API -----------------------------------------------------------
    @app.post("/api/chat")
    async def _api_chat(req: ChatRequest):
        # Echo the (possibly minted) session_id so callers can continue the
        # session — additive key, existing consumers unaffected.
        session_id = req.session_id.strip() or _mint_session_id()
        result = await chat(
            req.message, session_id, model=req.model, incognito=req.incognito, hitl_resume=req.hitl_resume
        )
        parts = [m["content"] for m in result if m.get("role") == "assistant" and m.get("content")]
        return {"response": "\n\n".join(parts), "messages": result, "session_id": session_id}

    @app.delete("/api/chat/sessions/{session_id}")
    async def _api_delete_session(session_id: str, harvest: bool = False):
        """Retire a chat session: purge its checkpoints for both the A2A and
        chat prefix, optionally harvesting the conversation into the knowledge
        base first. Called when the operator deletes a chat tab.

        Harvest is OPT-IN (``?harvest=true`` — the delete dialog's checkbox):
        deleting a chat must not silently copy it into searchable memory; the
        operator may be deleting it precisely to get rid of it. The TTL prune
        sweep keeps its own config-driven default (``checkpoint_harvest_enabled``).

        Both ``a2a:{session_id}`` and the legacy ``chat:{session_id}`` threads are
        retired (non-streaming turns keyed ``chat:`` before ADR 0069 unified the
        prefix) with cascade so goal-mode ``:goal-iter-N`` sub-threads are not
        orphaned."""
        chunk_id = await _retire_thread(f"a2a:{session_id}", harvest=harvest, cascade=True)
        await _retire_thread(f"chat:{session_id}", harvest=False, cascade=True)  # only harvest once
        # Ephemeral chat attachments are session-scoped (ADR 0021) — drop them so a
        # deleted chat leaves nothing indexed behind.
        store = STATE.knowledge_store
        if store is not None and hasattr(store, "delete_by_namespace"):
            try:
                await asyncio.to_thread(store.delete_by_namespace, f"attach:{session_id}")
            except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                log.warning("[chat] attachment cleanup failed for %s: %s", session_id, exc)
        return {"deleted": True, "harvested": chunk_id is not None}

    @app.post("/api/chat/sessions/{session_id}/compact")
    async def _api_compact_session(session_id: str):
        """Compact a chat session's live context (#1527): archive the raw history
        into searchable memory, summarize it, and rewrite the LangGraph checkpoint
        to ``[summary, recent tail]`` so the agent keeps context at lower token
        cost. Runs SERVER-SIDE — the checkpoint is the agent's real context, so a
        client-only compaction would do nothing.

        Never-lossy: if there's no store or the archive write yields nothing, the
        checkpoint is left untouched and ``refused`` is true (the console then
        keeps the full thread rather than dropping anything).

        Pre-release: behind the ``chat.compact`` developer flag (ADR 0068)."""
        from fastapi import HTTPException

        from runtime.flags import flag_enabled

        if not flag_enabled("chat.compact"):
            raise HTTPException(
                status_code=403,
                detail="/compact is pre-release — enable the chat.compact developer flag (ADR 0068)",
            )
        return await compact_session(session_id)

    @app.post("/api/chat/sessions/{session_id}/rewind")
    async def _api_rewind_session(session_id: str, body: dict | None = None):
        """Rewind a chat session to a target message (#1535): discard everything
        AFTER it and rewrite the LangGraph checkpoint IN PLACE. Runs SERVER-SIDE —
        the checkpoint is the agent's real context, so a client-only truncate would
        leave the agent's memory intact.

        The body carries the target: ``message_id`` and/or ``content`` (the console
        sends the visible bubble's text, since its client-side message ids never
        appear in the checkpoint), or an explicit ``index``. Intentionally
        DESTRUCTIVE (no archive) but never corrupting — the kept prefix is trimmed
        to a safe tool-call boundary so no orphaned tool_call is left behind."""
        body = body or {}
        idx = body.get("index")
        occ = body.get("occurrence")
        return await rewind_session(
            session_id,
            message_id=body.get("message_id"),
            index=int(idx) if idx is not None else None,
            content=body.get("content"),
            occurrence=int(occ) if occ is not None else None,
        )

    @app.post("/api/chat/sessions/{session_id}/steer")
    async def _api_steer(session_id: str, body: dict | None = None):
        """Queue a user message into a RUNNING turn (mid-turn steering).

        The next model call folds it in via ``SteeringMiddleware``, so the user
        can redirect or reset ongoing work without stopping the live stream. This
        does NOT start a turn — it only enqueues; the in-flight turn picks it up at
        its next model call (i.e. after the current tool finishes). The client may
        pass its own ``id`` so it can reconcile at turn-end."""
        from fastapi import HTTPException

        from graph import steering

        text = str((body or {}).get("text", "")).strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        msg_id = str((body or {}).get("id", "")).strip() or None
        mid = steering.enqueue(session_id, text, msg_id=msg_id)
        return {"ok": True, "id": mid, "pending": steering.pending(session_id)}

    @app.get("/api/chat/sessions/{session_id}/steer")
    async def _api_steer_pending(session_id: str):
        """Items still queued for ``session_id`` — i.e. steering messages that
        arrived after the turn's last model call and weren't folded in. The
        console reads this at turn-end: it settles the consumed ones into the
        thread and re-sends these un-consumed ones as a fresh turn."""
        from graph import steering

        return {"pending": steering.pending_items(session_id)}

    @app.delete("/api/chat/sessions/{session_id}/steer/{msg_id}")
    async def _api_steer_cancel(session_id: str, msg_id: str):
        """Cancel a still-queued steer before it folds into the turn (the ✕ on a
        pending bubble). ``removed: true`` means it was dropped from the queue and
        the agent never sees it; ``removed: false`` means it had already been
        drained into the running turn (too late — the agent will still act on it)
        or was never queued. The console settles a not-removed steer into the
        thread instead of pretending it never happened."""
        from graph import steering

        removed = steering.dequeue(session_id, msg_id)
        return {"removed": removed, "pending": steering.pending(session_id)}

    @app.get("/api/chat/sessions/{session_id}/delegations")
    async def _api_delegations(session_id: str):
        """In-flight foreground subagent delegations for ``session_id`` —
        ``[{"id", "label"}]``. ``id`` is the running ``task`` tool-call id; the
        console surfaces a Cancel affordance on each running ``task`` card and this
        is the authoritative list that affordance acts on."""
        from graph import delegations

        return {"running": delegations.running_items(session_id)}

    @app.post("/api/chat/sessions/{session_id}/delegations/{delegation_id}/cancel")
    async def _api_delegation_cancel(session_id: str, delegation_id: str):
        """Abort ONE running foreground delegation (the Stop on a running ``task``
        card) — cancels just that subagent, NOT the whole turn: the lead continues
        with a 'cancelled' result. Contrast the composer Stop, which A2A-CancelTasks
        the entire turn. ``cancelled: false`` means the delegation already finished,
        was already cancelling, or was never running (too late / nothing to do)."""
        from graph import delegations

        cancelled = delegations.cancel(session_id, delegation_id)
        return {"cancelled": cancelled, "running": delegations.running(session_id)}

    # Goal-mode read/clear moved to the canonical plural `/api/goals*` in
    # operator_api/routes.py (D4 dedupe, ADR 0075): `GET /api/goals` (list),
    # `GET /api/goals/{session_id}` (one), `DELETE /api/goals/{session_id}` (clear).
    # The singular `/api/goal/{session_id}` duplicates were retired here.

    # --- Health / readiness (ADR 0010) -------------------------------------
    # Reflects whether the graph actually compiled — the only readiness signal
    # in the 'none' tier (no UI to eyeball). 503 until ready, for k8s probes.
    @app.get("/healthz", include_in_schema=False)
    async def _healthz():
        from graph.config_io import is_setup_complete

        ready = STATE.graph is not None
        return JSONResponse(
            {
                "ok": ready,
                "graph_compiled": ready,
                "setup_complete": is_setup_complete(),
                "ui": ui,
                # Surface the active model so eval reports can be tagged with the
                # model under test without guessing (evals.runner auto-detects).
                "model": STATE.graph_config.model_name if STATE.graph_config else None,
            },
            status_code=200 if ready else 503,
        )

    # --- OpenAI-compatible chat completions --------------------------------
    # Lets this agent be registered as a model in the LiteLLM gateway /
    # OpenWebUI without any protocol adapter.
    @app.post("/v1/chat/completions")
    async def _openai_chat_completions(req: dict):
        messages = req.get("messages", [])
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if not user_msgs:
            return {"error": "No user message provided"}, 400
        prompt, images = _split_openai_content(user_msgs[-1].get("content", ""))
        session_id = f"openai-compat-{int(time.time())}"
        stream = req.get("stream", False)

        # Honor the OpenAI `model` field as a per-request override — unless it's
        # this agent's own advertised id (the default model from /v1/models), in
        # which case use the configured default. Lets an OpenAI client target a
        # specific gateway model.
        req_model = (req.get("model") or "").strip()
        model = req_model if req_model and req_model != agent_name() else None

        # Incognito (ADR 0069): opt-in per request via an `incognito` field (OpenAI
        # clients pass it through `extra_body`). Off by default — unchanged behavior —
        # so a programmatic caller (e.g. an eval/benchmark harness) can run a turn with
        # no memory injection, persistence, or harvest, for reproducible, uncontaminated
        # runs. A2A and /api/chat already expose this; /v1 was the gap.
        incognito = bool(req.get("incognito", False))

        result = await chat(prompt, session_id, model=model, incognito=incognito, images=images or None)
        parts = [m["content"] for m in result if m.get("role") == "assistant" and m.get("content")]
        content = "\n\n".join(parts)
        created = int(time.time())
        completion_id = f"{agent_name()}-{session_id}"

        # Real token usage, summed from the assistant turn(s) (server.chat._sum_usage
        # already folds every model call — lead + goal continuations + subagents — into the
        # OpenAI `{prompt,completion,total}_tokens` shape). Absent (a short-circuit / older
        # reply) ⇒ zeros, same as before (ADR 0075 D4).
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for _m in result:
            _u = _m.get("usage")
            if isinstance(_u, dict):
                for _k in usage:
                    usage[_k] += int(_u.get(_k, 0) or 0)

        if stream:
            # OpenAI streams usage only when the client opts in, in a final chunk with
            # empty `choices` (stream_options.include_usage).
            include_usage = bool((req.get("stream_options") or {}).get("include_usage"))

            async def _stream():
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": agent_name(),
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                done_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": agent_name(),
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(done_chunk)}\n\n"
                if include_usage:
                    usage_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": agent_name(),
                        "choices": [],
                        "usage": usage,
                    }
                    yield f"data: {json.dumps(usage_chunk)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(_stream(), media_type="text/event-stream")

        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": agent_name(),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }

    @app.get("/v1/models")
    async def _openai_models():
        return {
            "object": "list",
            "data": [{"id": agent_name(), "object": "model", "created": 1774600000, "owned_by": "protolabs"}],
        }
