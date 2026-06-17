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
import time

from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from runtime.state import STATE

log = logging.getLogger("protoagent.server")
from server import agent_name
from server.agent_init import _retire_thread
from server.chat import chat


class ChatRequest(BaseModel):
    message: str
    session_id: str = "api-default"
    model: str | None = None  # per-tab model override; None → configured default


def register_chat_routes(app, ui: str) -> None:
    """Register the chat / goal / health / OpenAI-compat routes on ``app``.

    ``ui`` is the active deployment tier (full/console/none); ``/healthz`` echoes
    it so probes can see which surface is running.
    """

    # --- Chat API -----------------------------------------------------------
    @app.post("/api/chat")
    async def _api_chat(req: ChatRequest):
        result = await chat(req.message, req.session_id, model=req.model)
        parts = [m["content"] for m in result if m.get("role") == "assistant" and m.get("content")]
        return {"response": "\n\n".join(parts), "messages": result}

    @app.delete("/api/chat/sessions/{session_id}")
    async def _api_delete_session(session_id: str, harvest: bool = False):
        """Retire a chat session: purge its checkpoints for both the A2A and
        chat prefix, optionally harvesting the conversation into the knowledge
        base first. Called when the operator deletes a chat tab.

        Harvest is OPT-IN (``?harvest=true`` — the delete dialog's checkbox):
        deleting a chat must not silently copy it into searchable memory; the
        operator may be deleting it precisely to get rid of it. The TTL prune
        sweep keeps its own config-driven default (``checkpoint_harvest_enabled``).

        Both ``a2a:{session_id}`` and ``chat:{session_id}`` threads are retired
        with cascade so goal-mode ``:goal-iter-N`` sub-threads are not orphaned."""
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

    # --- Goal mode API ------------------------------------------------------
    # Programmatic status/clear for a session's goal (setting is done via the
    # `/goal ...` control message through chat/A2A). Returns 404-style payloads
    # as plain JSON to keep the surface dependency-free.
    @app.get("/api/goal/{session_id}")
    async def _api_goal_status(session_id: str):
        if STATE.goal_controller is None:
            return {"enabled": False, "goal": None}
        state = STATE.goal_controller.store.get(session_id)
        return {"enabled": True, "goal": state.to_dict() if state else None}

    @app.delete("/api/goal/{session_id}")
    async def _api_goal_clear(session_id: str):
        if STATE.goal_controller is None:
            return {"enabled": False, "cleared": False}
        return {"enabled": True, "cleared": STATE.goal_controller.store.clear(session_id)}

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
        prompt = user_msgs[-1].get("content", "")
        session_id = f"openai-compat-{int(time.time())}"
        stream = req.get("stream", False)

        # Honor the OpenAI `model` field as a per-request override — unless it's
        # this agent's own advertised id (the default model from /v1/models), in
        # which case use the configured default. Lets an OpenAI client target a
        # specific gateway model.
        req_model = (req.get("model") or "").strip()
        model = req_model if req_model and req_model != agent_name() else None

        result = await chat(prompt, session_id, model=model)
        parts = [m["content"] for m in result if m.get("role") == "assistant" and m.get("content")]
        content = "\n\n".join(parts)
        created = int(time.time())
        completion_id = f"{agent_name()}-{session_id}"

        if stream:

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
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    @app.get("/v1/models")
    async def _openai_models():
        return {
            "object": "list",
            "data": [{"id": agent_name(), "object": "model", "created": 1774600000, "owned_by": "protolabs"}],
        }
