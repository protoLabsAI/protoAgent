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
import re
import secrets
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from infra.publish import list_published_links
from runtime.state import STATE

log = logging.getLogger("protoagent.server")
from server import agent_name
from server.agent_init import _retire_thread
from server.chat import (
    _resolve_thread_id,
    aside_session,
    chat,
    compact_session,
    export_session,
    fork_session,
    publish_preview,
    publish_session,
    revoke_published_link,
    rewind_session,
)


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


def _has_unresolved_tool_calls(msg) -> bool:
    """True when ``msg`` is an ``AIMessage`` still carrying ``tool_calls`` — the
    model requested tools but no tool ever answered, i.e. the turn was cut off
    between the request and the tool node running. Both real captures in #2234
    end in exactly this shape: a narration-bearing ``AIMessage`` with unresolved
    ``tool_calls`` as the thread's last checkpointed message."""
    from langchain_core.messages import AIMessage

    return isinstance(msg, AIMessage) and bool(getattr(msg, "tool_calls", None))


#: Caller-supplied session keys are pinned to this alphabet. A session id reaches
#: filesystem paths (``graph/middleware/memory.py`` still resolves a LEGACY raw-name read
#: path beside the encoded one), so an unsanitized `../` from a /v1 caller would escape
#: the memory dir. Sanitizing at the boundary closes that for this surface regardless of
#: what any downstream consumer does with the value.
_V1_KEY_OK = re.compile(r"[^A-Za-z0-9._-]")
_V1_KEY_MAX = 96


def _v1_session_id(req: dict, request=None) -> str:
    """The session this ``/v1`` turn continues (#2119).

    Every request used to mint ``openai-compat-<unix seconds>``, so a multi-turn
    workflow — plan, review, execute, the LE archetype's core loop — was amnesiac: turn 2
    re-scouted everything turn 1 had already read, and the caller had to paste turn 1's
    output back in to make progress. (The second-resolution clock was also a latent
    *collision*: two unrelated callers landing in the same second silently shared one
    session.)

    A caller can now pin the session, in precedence order:

    1. ``session_id`` in the body — explicit intent, and what an OpenAI client passes
       through ``extra_body``.
    2. ``X-Session-Id`` header — for clients that can't reach the body schema.
    3. ``user`` — the OpenAI-standard field, honoured as the issue proposed.

    With none of them the behaviour is unchanged in spirit but no longer collides: a
    fresh, unique session per request. Continuity is opt-in, so an existing stateless
    caller sees exactly what it saw before.
    """
    body = req or {}
    header = (request.headers.get("X-Session-Id") or "").strip() if request is not None else ""
    raw = (
        str(body.get("session_id") or "").strip()
        or header
        or str(body.get("user") or "").strip()
    )
    if not raw:
        # uuid4, not the wall clock: unique per request AND collision-free.
        return f"openai-compat-{uuid.uuid4().hex}"
    # Strip separators first, then collapse any run of dots. Dots are allowed (a `user`
    # is plausibly "first.last") but `..` never needs to survive into something that
    # becomes a path component — belt-and-braces, since removing `/` already defangs it.
    safe = re.sub(r"\.{2,}", "_", _V1_KEY_OK.sub("_", raw))
    return f"openai-compat-{safe[:_V1_KEY_MAX]}"


async def _run_v1_turn(prompt: str, session_id: str, **kw):
    """Run one ``/v1`` turn with **defined** disconnect semantics (#2119): the turn is
    NOT cancelled when the HTTP client goes away.

    Previously this was simply undefined from the outside — a client-side read timeout
    left the caller unable to tell whether the work had been abandoned or was still
    running, and in the reported case a 15-minute turn's entire result was unreachable.

    Run-to-completion is the right default for an agent turn: the expensive part is the
    tool work, it is already checkpointed against the session, and killing it at the
    transport layer throws that away for a reason (a client's socket) that has nothing to
    do with whether the work is still wanted. Paired with a pinned ``session_id``, a
    caller that timed out reconnects to the SAME session and finds the finished work in
    context instead of re-running it.

    The turn is shielded rather than fire-and-forget: if the client is still there it
    still awaits normally. The done-callback exists to retrieve the exception from an
    orphaned turn — without it a failure after the caller left surfaces as a bare
    "exception was never retrieved" at GC time, with no session context attached.
    """
    turn = asyncio.ensure_future(chat(prompt, session_id, **kw))

    def _drain(fut: asyncio.Future) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            log.warning("[v1] turn for session %s failed after the client left: %r", session_id, exc)

    turn.add_done_callback(_drain)
    return await asyncio.shield(turn)


def _v1_error_response(err: dict) -> JSONResponse:
    """Map a failed turn to an OpenAI-shaped HTTP error instead of a 200 (#2578).

    Status choice, which is the whole point of the endpoint being honest:

    - **429 is mirrored.** Every OpenAI client's backoff keys on it, and a 429 from us
      means "rate limited" no matter which hop produced it.
    - **Any other upstream HTTP failure becomes 502**, deliberately NOT the upstream
      status. A 401 from this endpoint already means "your protoAgent bearer is bad"
      (``a2a_impl.auth``); echoing an upstream 401 would send callers to re-check the
      one credential that is fine. 502 says "the hop behind me failed" and the body
      names it — ``upstream_status`` carries the original.
    - **No HTTP status at all ⇒ 500** — that's a fault in our own turn, not a proxy hop.
    """
    upstream = err.get("upstream_status")
    if upstream == 429:
        status = 429
    elif isinstance(upstream, int):
        status = 502
    else:
        status = 500
    return JSONResponse(
        {
            "error": {
                "message": err.get("message") or "the turn failed",
                "type": err.get("type") or "server_error",
                "param": None,
                "code": str(upstream) if isinstance(upstream, int) else None,
                # Non-standard but additive: which hop actually failed, for operators
                # staring at a 502 wondering whose credential expired.
                "upstream_status": upstream,
            }
        },
        status_code=status,
    )


async def _v1_finish_reason(session_id: str) -> str:
    """How a non-streaming /v1 turn terminated, as an OpenAI ``finish_reason`` (#2234).

    ``"length"`` (the OpenAI value for "ran into a limit") when the thread's
    checkpointed state shows a hard-stop — the turn ended mid-execution
    (LangGraph ``recursion_limit`` / tool-loop ``max_iterations``) rather than
    at a natural synthesis point:

    - the last checkpointed message is a ``ToolMessage`` — the loop stopped
      right after a tool resolved, before the model synthesized; or
    - the last message is an ``AIMessage`` with unresolved ``tool_calls``
      (``_has_unresolved_tool_calls``) — cut off after the model requested
      tools but before the tool node ran.

    ``"stop"`` for a clean synthesis, and as the FAIL-SAFE whenever the state
    can't be read (no graph / no ``aget_state`` / snapshot error / empty
    thread) — introspection must never fail or misflag a good response."""
    from langchain_core.messages import ToolMessage

    aget_state = getattr(STATE.graph, "aget_state", None)
    if aget_state is None:
        return "stop"
    try:
        config = {"configurable": {"thread_id": _resolve_thread_id(None, session_id)}}
        snapshot = await aget_state(config)
        messages = (getattr(snapshot, "values", None) or {}).get("messages") or []
    except Exception:  # noqa: BLE001 — fail-safe; see docstring
        return "stop"
    if not messages:
        return "stop"
    last = messages[-1]
    if isinstance(last, ToolMessage):
        return "length"
    if _has_unresolved_tool_calls(last):
        return "length"
    return "stop"


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
        # Prompt snapshots are conversation-scoped forensics (#2243) — purge them
        # here so a deleted chat's prompts never outlive the conversation.
        try:
            from observability.prompt_snapshots import prompt_snapshots

            await asyncio.to_thread(prompt_snapshots().purge_session, session_id)
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
            log.warning("[chat] prompt-snapshot cleanup failed for %s: %s", session_id, exc)
        # The session-summary memory (#2482) — without this, a digest of the
        # deleted conversation kept riding <prior_sessions> into future prompts,
        # violating the delete dialog's "its history will be removed".
        try:
            from graph.middleware.memory import delete_session_summary

            await asyncio.to_thread(delete_session_summary, session_id)
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
            log.warning("[chat] session-summary cleanup failed for %s: %s", session_id, exc)
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

        Generally available since #2785 (ADR 0101 D5): the ``chat.compact`` dev
        flag expired past its own remove_by — and gating the LOSSLESS path while
        lossy auto-compaction ran by default was exactly backwards."""
        return await compact_session(session_id)

    @app.get("/api/chat/sessions/{session_id}/export")
    async def _api_export_session(session_id: str, title: str | None = None):
        """Export a chat session's conversation as Markdown (#2158 P1) — the
        "share this thread" gesture.

        **Read-only**: unlike its compact/rewind siblings this never touches the
        checkpoint, so it needs no developer-flag gate. Returns
        ``{found, markdown, message_count, redactions, reason, message}``.

        Secrets are scrubbed before the Markdown is produced (see
        ``graph.export_op``) and the kinds found are reported in ``redactions``
        AND disclosed in the document itself — an export is meant to leave the
        machine, so the operator reviews rather than trusting a silent filter.
        Redaction is a safety net, not a guarantee."""
        return await export_session(session_id, title=title)

    @app.get("/api/chat/sessions/{session_id}/publish/preview")
    async def _api_publish_preview(session_id: str, title: str | None = None):
        """Build the structured chat-bundle for the pre-publish review (#2179 P2, #2682)
        — **read-only**, sends nothing anywhere. The operator reviews this before
        deciding to publish. Returns
        ``{found, manifest, message_count, redactions, reason, message}``.

        Pre-release: behind the ``chat.publish`` developer flag (ADR 0068) — the hosted
        service this feeds (#2685) doesn't exist yet."""
        from fastapi import HTTPException

        from runtime.flags import flag_enabled

        if not flag_enabled("chat.publish"):
            raise HTTPException(
                status_code=403,
                detail="/publish is pre-release — enable the chat.publish developer flag (ADR 0068)",
            )
        return await publish_preview(session_id, title=title)

    @app.post("/api/chat/sessions/{session_id}/publish")
    async def _api_publish_session(session_id: str, body: dict | None = None):
        """Publish a chat thread to the hosted viewer (#2179 P2, #2683).

        Builds the bundle server-side, fresh — never accepts a client-supplied bundle —
        then POSTs it to the configured ``publish.endpoint_url``. Returns
        ``{published, public_url, revoke_token, expires_at, redactions, artifact_notes,
        reason, message}``; a false ``published`` carries a ``reason`` (e.g.
        ``not_configured`` when no hosted endpoint is set) rather than an error status,
        since "not configured yet" is an expected state, not a failure.

        Pre-release: behind the ``chat.publish`` developer flag (ADR 0068)."""
        from fastapi import HTTPException

        from runtime.flags import flag_enabled

        if not flag_enabled("chat.publish"):
            raise HTTPException(
                status_code=403,
                detail="/publish is pre-release — enable the chat.publish developer flag (ADR 0068)",
            )
        title = (body or {}).get("title")
        return await publish_session(session_id, title=title)

    @app.get("/api/chat/publish/links")
    async def _api_list_published_links():
        """Everything this instance has published to the hosted viewer (#2684) —
        title, public URL, timestamps, revoked state. Never includes the revoke
        token — that's server-internal only, presented to the hosted service by
        the revoke route below, never sent back to the browser after the initial
        publish. Not per-session: a link outlives the tab it was published from.

        Pre-release: behind the ``chat.publish`` developer flag (ADR 0068)."""
        from fastapi import HTTPException

        from runtime.flags import flag_enabled

        if not flag_enabled("chat.publish"):
            raise HTTPException(
                status_code=403,
                detail="/publish is pre-release — enable the chat.publish developer flag (ADR 0068)",
            )
        return {"links": list_published_links()}

    @app.post("/api/chat/publish/links/{link_id}/revoke")
    async def _api_revoke_published_link(link_id: str):
        """Un-share a previously published thread (#2684). Looks up the link's stored
        revoke_token and presents it to the configured ``publish.revoke_endpoint_url`` —
        marks the link revoked LOCALLY only once the hosted service confirms, never
        before (a local-only revoke would tell the operator a link is dead while it's
        still live). Returns ``{ok, error?}``: ``ok: false`` with
        ``reason: "not_configured"`` when no revoke endpoint is set, same honest-state
        shape as publish itself.

        Pre-release: behind the ``chat.publish`` developer flag (ADR 0068)."""
        from fastapi import HTTPException

        from runtime.flags import flag_enabled

        if not flag_enabled("chat.publish"):
            raise HTTPException(
                status_code=403,
                detail="/publish is pre-release — enable the chat.publish developer flag (ADR 0068)",
            )
        return await revoke_published_link(link_id)

    @app.post("/api/chat/sessions/{session_id}/aside")
    async def _api_aside_session(session_id: str, body: dict | None = None):
        """`/btw` (#2180) — answer a side question about this session's context WITHOUT
        touching it. The turn runs incognito on a fresh EPHEMERAL thread seeded with the
        main thread's messages; the main thread's checkpoint is never written (the
        isolation is structural — see graph/aside_op). Returns
        ``{found, answer, reason, message}``. Body: ``{"question": "..."}``."""
        question = str((body or {}).get("question") or "")
        return await aside_session(session_id, question)

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
            # Exclusive cut (#2491): Regenerate discards the last user+assistant
            # pair so its resend REPLACES the turn instead of appending a duplicate.
            before=bool(body.get("before", False)),
        )

    @app.post("/api/chat/sessions/{session_id}/fork")
    async def _api_fork_session(session_id: str, body: dict | None = None):
        """Fork a chat session at a target message (#2803): copy the checkpoint
        prefix through the target onto ``new_session_id``'s (fresh) thread. The
        SOURCE is untouched — this is rewind's non-destructive sibling, and it is
        what makes "Fork from here" real memory instead of a display copy the
        agent can't see. Body: ``new_session_id`` (required) + the same target
        spec as /rewind (``message_id`` / ``content``+``occurrence`` / ``index``)."""
        body = body or {}
        idx = body.get("index")
        occ = body.get("occurrence")
        return await fork_session(
            session_id,
            str(body.get("new_session_id") or ""),
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
    async def _openai_chat_completions(req: dict, request: Request):
        """OpenAI-compatible chat completion over one agent turn.

        Non-streaming ``finish_reason`` semantics (#2234): ``"stop"`` means the
        turn ended at its natural synthesis point and the message is the final
        answer; ``"length"`` (the OpenAI value for "ran into a limit") means the
        turn was cut off by a hard-stop — LangGraph ``recursion_limit`` or the
        tool loop's ``max_iterations`` — so the content is the last thing said
        mid-turn, NOT a completed answer, and a stateless driver must not treat
        it as one. Detection reads the thread's checkpointed state (see
        ``_v1_finish_reason``). The non-streaming message carries only the LAST
        assistant message of the turn — intermediate narrations between tool
        calls are never joined in. The streaming path is unchanged.

        **Session continuity (#2119).** Pin a session with ``session_id`` in the
        body, an ``X-Session-Id`` header, or the OpenAI ``user`` field (that
        precedence) and consecutive requests resume the same context — so a
        multi-turn workflow (plan → review → execute) doesn't re-scout from
        scratch every turn. Omit all three and you get a fresh, unique session
        per request, as before. See ``_v1_session_id``.

        **Disconnect semantics (#2119).** Defined, where they used to be
        undefined: the turn is NOT cancelled when the HTTP client goes away. It
        runs to completion server-side and is checkpointed against its session
        (see ``_run_v1_turn``). So a caller whose read timeout fired reconnects
        with the SAME session key and finds the finished work already in
        context, instead of re-running it — which only works if the session was
        pinned, the other half of why continuity matters here.

        **Failure semantics (#2578).** A turn that raises returns an OpenAI-shaped
        ``{"error": {...}}`` body with a non-2xx status — 429 mirrored, any other
        upstream HTTP failure as 502, an internal fault as 500 (see
        ``_v1_error_response``). It used to answer 200 with the exception text as the
        assistant's content and ``finish_reason: "stop"``, so an SDK client counted a
        hard auth failure as a successful completion."""
        messages = req.get("messages", [])
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if not user_msgs:
            return {"error": "No user message provided"}, 400
        prompt, images = _split_openai_content(user_msgs[-1].get("content", ""))
        session_id = _v1_session_id(req, request)
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

        result = await _run_v1_turn(prompt, session_id, model=model, incognito=incognito, images=images or None)

        # A turn that raised comes back as an assistant bubble carrying a structured
        # `error` (server.chat.turn_error). Answering 200 with that text as the content
        # made an upstream 401/429 indistinguishable from a real answer (#2578). Checked
        # before the stream branch — nothing has been sent yet, so BOTH modes can still
        # return a proper status rather than an SSE frame the client reads as success.
        turn_err = next((m["error"] for m in result if isinstance(m.get("error"), dict)), None)
        if turn_err:
            return _v1_error_response(turn_err)

        # Joined parts feed the streaming path only (its historical shape); the
        # non-streaming body is rebuilt below from the LAST assistant message.
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

        # Non-streaming (#2234): the turn's answer is the LAST assistant message
        # ONLY. A limit-terminated turn returns several assistant messages — the
        # earlier ones are mid-loop narrations between tool calls, and joining
        # them presented a fragment as if it were the completed answer.
        final = next((m for m in reversed(result) if m.get("role") == "assistant" and m.get("content")), None)
        content = str(final["content"]) if final else ""
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": agent_name(),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    # "length" when the turn hit a limit mid-execution; "stop"
                    # only for a clean synthesis (docstring above).
                    "finish_reason": await _v1_finish_reason(session_id),
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
