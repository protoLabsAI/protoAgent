"""Chat backend — the LangGraph turn loop behind every entry point.

Extracted from ``server/__init__.py`` (ADR 0023, phase 2). This module owns the
non-streaming ``chat`` (the console + OpenAI-compat) and streaming
``_chat_langgraph_stream`` (the A2A handler) turn drivers, the shared
``_run_turn_stream`` event loop, tool-preview/interrupt shaping, and slash-command
parsing + execution for workflows and subagents.

The out-of-turn session gestures (``/compact``, export, publish, ``/btw``, rewind,
fork) live in ``server/chat_session_ops.py`` and the non-streaming usage/telemetry
helpers in ``server/turn_telemetry.py`` (#3810); the ACP runtime registry + turn
driving live in ``server/chat_acp.py`` (#3828) — the ``pre.acp`` switch stays here in
``_pre_turn_dispatch``; the @-delegate room exchange lives in ``server/chat_rooms.py``
(#3838) — its call site (under the per-thread lock) stays in ``_pre_turn_dispatch``.
All four are re-exported below.

It depends only on neutral modules (``runtime.state``, ``graph.output_format``)
plus function-local imports — nothing from ``server/__init__``, so there is no
import cycle. ``server/__init__.py`` re-exports every public name so
``server.<symbol>`` keeps resolving for the OpenAI-compat / A2A wiring in
``_main`` and for the test suite.
"""

import asyncio
import contextlib
import functools
import json
import logging
import re
import time
import weakref
from dataclasses import dataclass, field
from typing import Any

from graph.middleware.redaction import redact as _redact
from graph.output_format import extract_output
from runtime import turn_activity as _turn_activity
from runtime.state import STATE
from server import turn_telemetry as _turn_telemetry

# The ACP runtime registry + turn driving moved to server/chat_acp.py (#3828). The
# drivers below call it through the module (``_chat_acp.<name>``) so a patch there
# intercepts; these re-exports are COPIES of the bindings — patch/mutate
# server.chat_acp, never these names (tests/test_chat_acp_seam.py guards it).
from server import chat_acp as _chat_acp
from server.chat_acp import (  # noqa: F401 — re-export
    _ACP_BUSY,
    _ACP_IDLE_TTL_S,
    _ACP_LOCK,
    _ACP_MAX_RUNTIMES,
    _ACP_RUNTIME_ACCESS,
    _ACP_RUNTIMES,
    _acp_acquire,
    _acp_drive_turn,
    _acp_release,
    _acp_turn_collected,
    _evict_acp_runtimes,
    _get_acp_runtime,
    _get_acp_runtime_locked,
    acp_sessions_snapshot,
)

# The @-delegate room exchange moved to server/chat_rooms.py (#3838).
# ``_pre_turn_dispatch`` calls it through the module (``_chat_rooms.<name>``) so a patch
# there intercepts; these re-exports are COPIES of the bindings — patch server.chat_rooms,
# never these names (tests/test_chat_rooms_seam.py guards it).
from server import chat_rooms as _chat_rooms
from server.chat_rooms import (  # noqa: F401 — re-export
    _all_mentions_are_startable_unreachable,
    _at_delegate_exchange,
    _at_delegate_reply,
    _covered_by_a_bubble,
    _delegate_unavailable_msg,
    _parse_at_delegate,
    _parse_at_delegates,
    _room_note,
    _with_room_notes,
)

# Session ops (/compact, export, publish, /btw, rewind, fork) moved to
# server/chat_session_ops.py and the non-streaming usage/telemetry helpers to
# server/turn_telemetry.py (#3810). Re-exported so ``from server.chat import …`` and
# operator_api.chat_routes keep resolving. These are COPIES of the bindings: patch the
# defining module, never these names (tests/test_chat_session_ops_seam.py guards it).
from server.chat_session_ops import (  # noqa: F401 — re-export
    _artifact_resolver,
    _build_bundle,
    _compaction_message,
    _export_message,
    _fork_message,
    _publish_message,
    _publish_preview_message,
    _rewind_message,
    aside_session,
    compact_session,
    export_session,
    forget_delegate_conversations,
    forget_delegate_conversations_for_session,
    fork_session,
    publish_preview,
    publish_session,
    revoke_published_link,
    rewind_session,
)
from server.turn_telemetry import (  # noqa: F401 — re-export under the historical private names
    make_usage_callback as _make_usage_callback,
    record_local_turn as _record_local_turn,
    sum_usage as _sum_usage,
    telemetry_usage as _telemetry_usage,
)

log = logging.getLogger("protoagent.server")


# Chars of a background *report* injected into the spawning turn. Shrunk 6000 → 3000
# (ADR 0070 D2): a substantial report is now ALSO indexed into the knowledge store at
# completion, so the notification carries a summary-sized excerpt plus a pointer to
# the searchable full text instead of a third of the context window.
#
# This applies to ``spawn`` jobs only — an LLM subagent turn whose result is a REPORT an
# autonomous worker wrote. A ``spawn_work`` job (``deterministic``; ``delegate_to``,
# ``knowledge_ingest``) is a different thing: its result is the DELIVERABLE the caller
# dispatched and is waiting on, and truncating that destroys the work product rather than
# summarizing it (#2363, from #2352 — a delegate's reply arriving chopped at 3000 chars,
# with the operator hand-copying the rest out of the console). Those are delivered whole.
#
# Foreground ``delegate_to`` was never capped, so capping the background path made the
# SAME reply arrive differently depending on whether the orchestrator held its turn open —
# background vs foreground is a transport choice, not a content policy.
_BG_RESULT_CAP = 3000


def _drain_background(session_id: str) -> tuple[list, list[dict]]:
    """Pull completed background jobs for this session (ADR 0050) and render model
    messages to prepend to the turn's input.

    Drains exactly-once (the store flips ``notified`` atomically), so a completion is
    announced to the model on the spawning session's next turn and never again. Returns
    ``(messages, room_replies)``; both are empty on a normal turn. A background
    delegate contributes one authored room frame in honest completion order (#3051).

    Two delivery shapes, keyed on what the result IS (#2363):

    * a ``spawn`` job's result is a **report** an autonomous subagent wrote — excerpt it at
      ``_BG_RESULT_CAP`` and point at the knowledge-store copy (ADR 0070 D2);
    * a ``spawn_work`` job's result (``deterministic``) is the **deliverable** the caller
      dispatched — deliver it whole.
    * a background delegate with ``result_author`` is that participant's own reply —
      persist a ``<room-message>`` and emit its matching live authorship frame (#3051).

    The excerpt path's ``memory_recall`` pointer is only ever emitted for jobs that were
    actually indexed: ``_spawn_report_index`` runs from the A2A terminal hook, i.e. for
    ``spawn`` jobs, which are exactly the ones that reach the truncation branch. Keep those
    two in step — a pointer at an index that never ran sends the model to an empty search
    (#2362), which is worse than the truncation it is trying to soften.
    """
    mgr = getattr(STATE, "background_mgr", None)
    if mgr is None or not session_id:
        return [], []
    try:
        jobs = mgr.store.drain_pending(session_id)
    except Exception:  # noqa: BLE001 — never break a turn over the drain
        log.exception("[background] drain failed for session %s", session_id)
        return [], []
    if not jobs:
        return [], []
    from langchain_core.messages import HumanMessage

    msgs = []
    room_replies: list[dict] = []
    for j in jobs:
        result = j.result or ""
        author = str(getattr(j, "result_author", "") or "")
        if author:
            # Persist the same envelope as foreground/direct addressing so the lead,
            # history reload, later participant catch-up, and live console all agree
            # that these are the delegate's own words. The lead turn continues after
            # these frames and still provides its synthesis.
            from graph.mention_op import _envelope

            ok = j.status == "completed"
            text = result.strip() or ("(replied with nothing)" if ok else f"({j.status})")
            msgs.append(
                HumanMessage(
                    content=_envelope(author, text),
                    additional_kwargs={
                        "lc_source": "room",
                        "room": {"from": author, **({} if ok else {"failed": True})},
                    },
                )
            )
            room_replies.append(
                {"id": j.id, "author": author, "from": "assistant", "text": text, "ok": ok}
            )
            continue
        # A deterministic work job's result is the deliverable — deliver it whole (#2363).
        if not getattr(j, "deterministic", False) and len(result) > _BG_RESULT_CAP:
            # Completed, non-incognito reports this size were indexed at completion
            # (ADR 0070 D2) — say so; incognito/failed/chained (background-origin)
            # ones only live in the jobs DB.
            searchable = (
                " — the full report is indexed and searchable via memory_recall"
                if (
                    j.status == "completed"
                    and not getattr(j, "origin_incognito", False)
                    and not (j.origin_session or "").startswith("background:")
                )
                else ""
            )
            result = result[:_BG_RESULT_CAP] + (
                f"\n\n…[truncated to {_BG_RESULT_CAP} chars{searchable}; the operator can open "
                f"the full text from the console background report card (job id {j.id})]"
            )
        body = (
            "<task-notification>\n"
            "A background agent finished a task you delegated earlier:\n"
            f"<job-id>{j.id}</job-id>\n"
            f"<subagent>{j.subagent_type}</subagent>\n"
            f"<description>{j.description}</description>\n"
            f"<status>{j.status}</status>\n"
            "<result>\n"
            f"{result}\n"
            "</result>\n"
            "</task-notification>"
        )
        msgs.append(HumanMessage(content=body))
    log.info("[background] drained %d completion(s) into session %s", len(msgs), session_id)
    return msgs, room_replies


def _drain_background_messages(session_id: str) -> list:
    """Compatibility view of :func:`_drain_background` for model-facing callers/tests."""
    return _drain_background(session_id)[0]


# Tool bodies need the same thread-id rule without importing ``server`` (which would
# violate the graph/plugin import boundary). Keep this private alias for existing callers.
from graph.thread_ids import resolve_thread_id as _resolve_thread_id  # noqa: E402


def _goal_continuation_config(config: dict, goal_state) -> dict:
    """The LangGraph config for one goal *continuation* turn.

    Same-session goals reuse ``config`` (the checkpointer keeps the transcript so the model
    sees prior iterations). Fresh-context goals (Ralph loop) get a scoped, per-iteration
    ``thread_id`` so the checkpointer starts clean each turn — derived from the CURRENT
    ``config`` thread_id so the streaming and non-streaming drive loops (both ``a2a:…``
    since ADR 0069 unified the prefix) build it identically instead of each hand-rolling
    it. (They had drifted: the two paths re-derived the base thread_id differently and
    only the streaming one set ``recursion_limit`` — this unifies both.) Durable state
    lives in the goal's plan artifact on disk, not the thread.
    """
    if not (goal_state and getattr(goal_state, "fresh_context", False)):
        return config
    base_tid = (config.get("configurable") or {}).get("thread_id") or "goal"
    return {
        "configurable": {"thread_id": f"{base_tid}:goal-iter-{goal_state.iteration}"},
        "recursion_limit": getattr(STATE.graph_config, "max_iterations", 200),
    }


def _setup_required_message() -> list[dict[str, Any]]:
    """Returned by chat endpoints when there is no compiled graph.

    The console hides the chat pane until setup completes, but the
    HTTP /api/chat, OpenAI-compat, and A2A endpoints don't know the
    UI state — so they emit a plain-text message instead of 500ing on
    ``STATE.graph is None``. Two graphless states, two messages: the
    wizard hasn't run, or a native OAuth provider is signed out
    (#2458) and needs an in-console reconnect — telling that user to
    "finish setup" points them at a wizard that IS complete.
    """
    auth_err = getattr(STATE, "graph_auth_error", None)
    if auth_err:
        return [
            {
                "role": "assistant",
                "content": (
                    f"**Signed out.** {auth_err.get('message') or 'The model provider is disconnected.'} "
                    "Chat is disabled until the provider is reconnected from the console."
                ),
            }
        ]
    return [
        {
            "role": "assistant",
            "content": (
                "**Setup required.** The setup wizard has not been completed. "
                "Open the UI and finish the wizard, or POST the completed config "
                "to `/api/config/setup` before calling chat endpoints."
            ),
        }
    ]


# ---------------------------------------------------------------------------
# Chat backend — called by the A2A handler + OpenAI-compat endpoint
# ---------------------------------------------------------------------------


async def chat(
    message: str,
    session_id: str,
    *,
    model: str | None = None,
    incognito: bool = False,
    hitl_resume: bool = False,
    images: list[tuple[str, str]] | None = None,
    tool_fence: list[str] | None = None,
    origin: str = "local",
) -> list[dict[str, Any]]:
    """Route a user message through LangGraph and return the final assistant
    response as a list of ``{"role": "assistant", "content": ...}`` dicts.

    This is the non-streaming entry point used by the console + the OpenAI-compat
    endpoint. The A2A handler uses ``_chat_langgraph_stream`` instead to
    capture tool events and emit the cost-v1 DataPart on the terminal
    artifact. ``model`` overrides the lead model for this turn (per-tab / per
    OpenAI request); unset → the configured default. ``incognito`` (ADR 0069
    D3b) marks the turn as leaving no memory trail: no session-summary
    persistence, no memory injection. ``hitl_resume`` marks the message as the
    operator's answer to a pending HITL interrupt (#1560 — the desktop /api/chat
    fallback's analogue of the streaming path's ``hitl_resume`` metadata).
    ``images`` (#1943) carries inbound vision parts as ``[(media_type, uri)]``,
    same shape and gating as the streaming path's — forwarded to the model only
    when it's vision-capable. ``tool_fence`` (#2972) is a per-turn tool allowlist
    for a turn that originated with an untrusted party (a plugin surface relaying
    another operator's agent, say): it rides the state as ``subagent_fence`` — the
    same channel a detached background subagent run uses (#1639) — so
    ``SubagentFenceMiddleware`` blocks any tool call outside it. Unset → no fence.
    ``origin`` (#3000) names the surface this turn came from — ``v1``, ``api-chat``,
    ``plugin`` — and prefixes the telemetry row's key, since these turns have no A2A
    task to name them. Without it a row from the OpenAI-compat endpoint is
    indistinguishable from one the console produced, and "which surface is spending
    this" is the question those rows exist to answer.
    """
    if STATE.graph is None:
        return _setup_required_message()
    return await _chat_langgraph(
        message,
        session_id,
        model=model,
        incognito=incognito,
        hitl_resume=hitl_resume,
        images=images,
        tool_fence=tool_fence,
        origin=origin,
    )


# Cap tool input/output previews so a single frame stays small on the wire.
_TOOL_PREVIEW_CHARS = 800


def _coerce_tool_value(value) -> str:
    """Render a tool input/output for a tool-call card.

    Structured values (dict/list) become compact JSON with double quotes so
    the console can pretty-print them — Python's ``str()`` would emit a repr
    with single quotes that no JSON parser accepts. Everything else is
    stringified. Always truncated to keep the SSE frame small.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)[:_TOOL_PREVIEW_CHARS]
        except (TypeError, ValueError):
            pass
    return str(value)[:_TOOL_PREVIEW_CHARS]


def _tool_payload(value):
    """The payload of a tool result, whatever shape the tool returned.

    A tool may return a plain value, a ``ToolMessage``, or — since #3102 — a
    ``Command(update={"messages": [...]})`` that writes conversation state and carries
    its own terminating ``ToolMessage``. A ``Command`` has no ``.content``, so the old
    ``getattr(value, "content", value)`` fell straight through to ``str()`` and rendered
    the whole `Command(update={...})` repr — envelopes, tool_call_ids and all — into the
    chat as the delegate's reply.

    The terminator is what the tool actually returned to the model, so that is the
    payload: take the LAST ToolMessage in the update, which is where ToolNode requires it.
    """
    update = getattr(value, "update", None)
    if isinstance(update, dict):
        messages = update.get("messages")
        if isinstance(messages, list):
            for msg in reversed(messages):
                if getattr(msg, "type", "") == "tool" or type(msg).__name__ == "ToolMessage":
                    return getattr(msg, "content", msg)
            # A Command that updates state without a terminator has no payload to show;
            # its repr is never the right answer.
            return ""
    return getattr(value, "content", value)


def _coerce_tool_output(value) -> str:
    """Unwrap a tool result to its payload.

    ``on_tool_end`` hands back the LangChain ``ToolMessage``, whose ``str()``
    leaks ``name=``/``tool_call_id=`` noise — the card wants the actual
    ``.content``. Falls back to the raw value for plain returns.
    """
    return _coerce_tool_value(_tool_payload(value))


# The job handle a background `delegate_to` returns in its receipt ("… (job `bg-…`) …").
_BG_JOB_ID = re.compile(r"\(job `(bg-[a-f0-9]{12})`\)")
# The ONE refusal a background dispatch answers with instead of a job handle. Matched
# exactly: a bare `startswith("Error")` also catches a delegate whose own reply opens with
# that word (the no-manager inline fallback returns the reply here), and calling that a
# failed dispatch would hide the answer behind an error row.
_BG_DISPATCH_REFUSED = re.compile(r"^Error: unknown delegate\b")


def _delegation_summary(summary: object, query: str) -> str:
    """The one line the console shows for a delegation instead of its full query.

    The agent's own ``summary`` argument when it wrote one; else the query's first sentence
    — a delegation prompt restates everything the delegate needs, so the whole thing is a
    wall of text the operator didn't write and rarely needs to read. Same fallback the job's
    title uses (``infra.text.first_sentence``), so the row and the Background panel agree."""
    from infra.text import first_sentence

    line = " ".join(str(summary or "").split())
    if not line:
        return first_sentence(query)
    return line if len(line) <= 120 else f"{line[:119].rstrip()}…"


def _coerce_room_text(value) -> str:
    """A delegate's reply/query as a chat MESSAGE — full text, never preview-capped.

    ``_coerce_tool_output`` truncates at ``_TOOL_PREVIEW_CHARS`` (800) because a tool CARD
    is a preview. A room bubble is the participant's actual message, so it must carry the
    whole thing — the same as the operator-`@` path, which returns the delegate's reply
    uncapped. Unwraps the ToolMessage and renders structured content as JSON, minus the
    cap. (#3042 truncation: a long review came through cut at ~800 chars.)
    """
    v = _tool_payload(value)
    if v is None or v == "":
        return ""
    if isinstance(v, (dict, list)):
        import json as _json

        try:
            return _json.dumps(v, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            pass
    return str(v)


def _tool_output_chars(value) -> int:
    """True size of a tool result BEFORE the preview truncation (#2775).

    The SSE frame's ``output`` is capped at ``_TOOL_PREVIEW_CHARS`` (800 ≈ 200
    tokens), which is below the console cost chip's own 250-token display floor
    — estimating from the preview made the #2282 chip mathematically dead. Same
    coercion as the preview (ToolMessage unwrap, dict/list → JSON), minus the cap.
    """
    v = _tool_payload(value)
    if v is None or v == "":
        return 0
    if isinstance(v, (dict, list)):
        try:
            return len(json.dumps(v, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            pass
    return len(str(v))


def _interrupt_payload(val) -> dict:
    """Shape a LangGraph interrupt value into the ``input-required`` payload the
    A2A layer parks and the console renders. Richer HITL shapes pass through:
    ``ask_human`` → ``{"question": …}``; ``request_user_input`` → ``{"kind":"form",
    "title", "description", "steps":[…]}``; ``run_command`` approval →
    ``{"kind":"approval", "title", "detail", …}``. Anything else degrades to a
    question with the stringified value. The console renders by shape (prompt vs
    JSON-schema form vs Approve/Deny); the resume value is a string for a
    question, a dict for a form, and a decision for an approval."""
    if isinstance(val, dict) and (val.get("question") or val.get("kind") in ("form", "approval")):
        return val
    return {"question": (str(val) if val is not None else "Input required.")}


async def _pending_interrupt(config: dict):
    """The FIRST pending LangGraph interrupt for this thread as ``(interrupt_id, value)``,
    or ``None``. Both chat paths read the same snapshot to detect a turn that paused for
    human input instead of producing a final answer.

    A turn can pend SEVERAL interrupts at once: the tool node runs an assistant turn's
    tool calls concurrently, so two approval-gated tools called in one turn both hit
    ``interrupt()`` before either resolves. They drain one at a time — the first is
    surfaced, the operator's answer resumes exactly that one BY ID (a bare resume with
    more than one pending is a hard LangGraph error), and the graph re-pauses on the
    next still-unanswered interrupt, which surfaces on the following pass."""
    try:
        snapshot = await STATE.graph.aget_state(config)
    except Exception:
        return None
    # Task-level first, SKIPPING completed tasks: a super-step doesn't commit until all
    # its parallel tasks finish, so an already-ANSWERED interrupt stays in the snapshot
    # (its task carries a ``result``) alongside the still-unanswered ones. Surfacing it
    # again would loop the operator on a question they already answered.
    pending = []
    for t in getattr(snapshot, "tasks", ()) or ():
        if getattr(t, "result", None) is not None:
            continue
        pending.extend(getattr(t, "interrupts", ()) or ())
    if not pending:
        # Fallback for snapshot shapes without task-level interrupts.
        pending = list(getattr(snapshot, "interrupts", None) or [])
    if not pending:
        return None
    if len(pending) > 1:
        log.info(
            "[hitl] %d interrupts pending at once (parallel gated tool calls) — surfacing "
            "one at a time; each resume targets its interrupt id.",
            len(pending),
        )
    first = pending[0]
    return (getattr(first, "id", None), getattr(first, "value", first))


async def _pending_interrupt_value(config: dict):
    """Just the pending interrupt's value (the payload the console renders), or ``None``."""
    found = await _pending_interrupt(config)
    return None if found is None else found[1]


async def _resume_payload(config: dict, resume_value):
    """What ``Command(resume=…)`` should carry: the id-keyed form ``{interrupt_id: value}``
    answering exactly the surfaced (first-pending) interrupt. The bare value is only a
    fallback for an unreadable snapshot / an id-less interrupt — safe there, since with a
    single pending interrupt both forms are equivalent, and multi-pending implies modern
    LangGraph whose interrupts always carry ids."""
    found = await _pending_interrupt(config)
    iid = found[0] if found else None
    return {iid: resume_value} if iid else resume_value


async def _clear_pending_interrupt(config: dict) -> None:
    """Discard a pending (un-resumed) LangGraph interrupt so the thread is left in a clean,
    non-interrupted state. Used when an autonomous turn gives up on a HITL pause it can't
    answer (below): ``aupdate_state(config, None)`` advances the checkpoint past the interrupt
    WITHOUT running the model — verified to clear ``snapshot.interrupts`` and let the next
    fresh-input turn run clean. Best-effort: a failure here must never break the turn, and the
    next fresh turn supersedes a stray interrupt anyway."""
    try:
        await STATE.graph.aupdate_state(config, None)
    except Exception:  # noqa: BLE001 — clearing is defensive; never break the turn
        log.debug("[hitl] could not clear pending interrupt on autonomous give-up", exc_info=True)


def this_turn_messages(result) -> list:
    """The messages THIS turn produced — everything after the last ``HumanMessage``.

    ``graph.ainvoke`` returns the whole accumulated conversation from the
    checkpointer, not just the turn's additions. Scanning all of it for "the last
    assistant message" is only correct while every turn actually produces one:
    a turn whose model call dies mid-stream appends its HumanMessage and nothing
    else, so an unbounded reverse scan walks straight past it and returns the
    PREVIOUS turn's answer — a complete, coherent, plausible reply to the wrong
    question, with no error to give it away (#2300).

    The last HumanMessage is the boundary: the turn's own input. A ``hitl_resume``
    turn adds no new HumanMessage (it resumes via ``Command``), so the slice
    correctly still covers the interrupted turn's work, which is this turn's too.
    """
    messages = (result or {}).get("messages", []) or []
    from langchain_core.messages import HumanMessage

    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return list(messages[i + 1 :])
    return list(messages)


def _last_tool_text(result) -> str:
    """The last tool result's text in THIS turn — the fallback when a turn produced
    no assistant text (e.g. a ``wait`` yield, whose 'Wait scheduled…' confirmation
    is a ToolMessage, not an AIMessage). Bounded to the current turn for the same
    reason as ``this_turn_messages`` (#2300): an unbounded scan would answer with a
    prior turn's tool output."""
    from langchain_core.messages import ToolMessage

    for msg in reversed(this_turn_messages(result)):
        if isinstance(msg, ToolMessage) and msg.content:
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


# Byte-matches the protobanana plugin's middleware marker: the plugin skips any
# message whose text already carries it, so core bridging and the plugin's
# (pre-min-host-version) middleware never double-save an attachment.
_ATTACHMENT_REFS_MARKER = "[attached-image refs]"


def _bridge_attachment_ids(images: list[tuple[str, str]] | None, session_id: str | None = None) -> str:
    """Persist ``data:`` image attachments to the media store; return the id note.

    The tools side of native vision (#1969): the model can SEE an attachment but
    cannot echo megabytes of base64 out of its vision context into a tool
    argument — so each image is saved once at turn entry (before the vision
    gate, so text-only models get the refs too) and named by media id in a text
    note any media-ref-taking tool can act on.

    Remote http(s) image URLs are left alone — no server-side fetch (SSRF).
    A failed save degrades to today's behavior (no note); it never breaks the turn.
    """
    import base64

    from infra.media import save_media

    ids: list[str] = []
    for mime, uri in images or []:
        if not uri.startswith("data:image/") or "," not in uri:
            continue
        try:
            _header, b64 = uri.split(",", 1)
            meta = {"source": "user_attachment"}
            if session_id:
                meta["session_id"] = session_id
            ref = save_media(base64.b64decode(b64), mime or "image/png", meta)
            ids.append(ref.id)
        except Exception:  # noqa: BLE001 — one bad part must not kill the turn
            log.warning("[media] failed to persist an attached image", exc_info=True)
    if not ids:
        return ""
    listing = ", ".join(f"image {i} = `{mid}`" for i, mid in enumerate(ids, start=1))
    return (
        f"{_ATTACHMENT_REFS_MARKER} the user's attached image(s) are saved and can be "
        f"passed to image tools by media id: {listing}"
    )


def _vision_human_message(
    message: str,
    images: list[tuple[str, str]] | None = None,
    *,
    session_id: str | None = None,
    incognito: bool = False,
):
    """The turn's user message, with native vision (ADR 0021): when the model is
    vision-capable and the turn carried image parts, a multimodal content list
    (text + image_url blocks) the model sees directly — not piped through
    extraction. Non-vision models get plain text (image blocks dropped), the
    same gating on both the streaming and non-streaming (#1943) paths.

    Attachments are also bridged into the media store (#1969) so tools can
    reference them by id — on vision AND non-vision models — except on
    incognito turns (ADR 0069: no persistence)."""
    from langchain_core.messages import HumanMessage

    note = "" if incognito else _bridge_attachment_ids(images, session_id)
    if images and getattr(STATE.graph_config, "model_vision", False):
        blocks: list[dict] = [{"type": "text", "text": message}] if message else []
        blocks += [{"type": "image_url", "image_url": {"url": uri}} for _mt, uri in images]
        if note:
            blocks.append({"type": "text", "text": note})
        return HumanMessage(content=blocks)
    return HumanMessage(content=f"{message}\n\n{note}".strip() if note else message)


# The node langchain's `create_agent` runs tool calls in — see `_speaks_for_the_lead`.
_TOOL_NODE = "tools"


@functools.cache
def _lc_internal_call_marker() -> tuple[str, str] | None:
    """langchain's (key, token) marking a middleware-INTERNAL model call, or None on a
    langchain that predates it. The token is process-local, so user metadata cannot forge
    it; ``lc_source`` (below) is the older, purpose-naming marker."""
    try:
        from langchain.agents.middleware.internal_call_transformer import (
            INTERNAL_CALL_METADATA_KEY,
            internal_call_metadata,
        )
    except ImportError:
        return None
    return INTERNAL_CALL_METADATA_KEY, internal_call_metadata()[INTERNAL_CALL_METADATA_KEY]


def _speaks_for_the_lead(metadata: dict) -> bool:
    """Whether a chat-model event with this metadata is the LEAD answering, so its tokens
    belong in the turn's answer text. (A subagent's are ruled out before this: they carry
    ``parent_task_id``.)

    Not a call made anywhere under the lead's TOOL node. The checkpoint namespace's first
    segment names the node of the lead graph a run belongs to, however deep it nests: the
    tool body itself, a graph a tool runs (``sdk.run_subagent`` — a workflow step — is
    ``tools:<id>|model:<id>``, its own node reads "model"), or work a tool detached into
    a copy of its context, which keeps reporting into this stream while the lead answers.

    And not a middleware's own internal call — the compaction summary, tool selection —
    which langchain marks (``internal_call_metadata()``; ``lc_source`` names its purpose).

    Deliberately an exclusion, not "only the model node": a graph whose answering node is
    named differently still streams its answer, where an inclusion would silence it."""
    ns = str(metadata.get("langgraph_checkpoint_ns") or metadata.get("checkpoint_ns") or "")
    if ns.split("|", 1)[0].split(":", 1)[0] == _TOOL_NODE:
        return False
    marker = _lc_internal_call_marker()
    if marker is not None and metadata.get(marker[0]) == marker[1]:
        return False
    return not metadata.get("lc_source")


def _paragraph_break(before: str, after: str) -> str:
    """The newlines to put between ``before`` and ``after`` so ``after`` opens a new
    paragraph: one blank line, counting any newlines either side already carries."""
    have = (len(before) - len(before.rstrip("\n"))) + (len(after) - len(after.lstrip("\n")))
    return "\n" * max(0, 2 - have)


async def _run_turn_stream(
    message: str,
    session_id: str,
    config: dict,
    *,
    resume_value=None,
    images=None,
    model=None,
    reasoning_effort=None,
    incognito=False,
    subagent_fence=None,
):
    """Run one graph turn over ``astream_events``.

    Yields the same ``(kind, payload)`` status/usage frames the A2A handler
    consumes, then a final ``("__raw__", accumulated_raw)`` sentinel the caller
    intercepts to get the turn's raw model text. Factored out so the initial
    turn, the dropped-scratch kicker retry, and goal-mode continuations all
    share one event loop instead of copy-pasting it.

    When ``resume_value`` is given, the turn resumes a graph paused at an
    ``ask_human`` interrupt (LangGraph HITL) by feeding ``Command(resume=…)``
    instead of a fresh user message. If the turn pauses (the agent called
    ``ask_human``), yields a terminal ``("input_required", {"question": …})``
    frame instead of ``__raw__`` so the A2A layer can park the task (ADR 0003).
    """
    from langgraph.types import Command

    human = _vision_human_message(message, images, session_id=session_id, incognito=incognito)
    background_messages, background_replies = (
        ([], []) if resume_value is not None else _drain_background(session_id)
    )
    for reply in background_replies:
        # Replies arrive as themselves before the lead reads the same authored
        # messages and streams its synthesis. Store ordering is completion ordering.
        yield ("room_reply", reply)

    graph_input = (
        Command(resume=await _resume_payload(config, resume_value))
        if resume_value is not None
        # Prepend any completed background-job notifications (ADR 0050) so the model
        # learns of detached work that finished since this session last ran a turn.
        else {
            "messages": background_messages + [human],
            "session_id": session_id,
            # Incognito (ADR 0069 D3b): always stamped explicitly — the channel
            # persists in the checkpointer, so an omitted key would silently
            # inherit a previous turn's value instead of the caller's intent.
            "incognito": bool(incognito),
            # Per-tab model + reasoning-effort override (ModelOverrideMiddleware reads
            # both); omit each key when unset so the configured default applies.
            **({"model": model} if model else {}),
            **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
            # Per-subagent tool fence for a detached background run (#1639) —
            # SubagentFenceMiddleware blocks tool calls outside it. Omitted (not
            # empty) when unset so ordinary turns carry no fence channel.
            **({"subagent_fence": [str(t) for t in subagent_fence]} if subagent_fence else {}),
        }
    )
    from observability import metrics
    from observability import pricing

    accumulated_raw = ""  # the answer text so far (the model's content; no protocol tags)
    # The model call the answer's latest text came from. Each lead model call is its own
    # message: "I'll check the time first." → tool → "It is noon." must not be glued into
    # "first.It is". So text arriving from a DIFFERENT call than the last text opens a
    # paragraph — keyed on the text's own run, never on a model merely STARTING, because
    # work a tool detached keeps reporting into this stream mid-answer (see below). The
    # break rides the streamed delta itself, not just this accumulator, so the live
    # stream, the executor's accumulation and the canonical `done` text stay ONE string.
    # (The console keeps a turn's text-to-tool interleaving only while they agree; #3210
    # separated only the executor's copy, which the `done` text overrode on this path.)
    _answer_run: object = None
    _llm_started: dict[str, float] = {}  # run_id → monotonic start (per-call latency)
    _tool_started: dict[str, float] = {}  # run_id → monotonic start (per-call latency, #2697)
    _delegate_targets: dict[str, str] = {}  # run_id → delegate name, for delegate_to → room bubble (#3042)
    _bg_delegations: dict[str, dict] = {}  # run_id → a BACKGROUND delegate_to's ask, emitted once it has a job id
    announced_tools: set[str] = set()  # tool_call ids already surfaced as a start frame
    async for event in STATE.graph.astream_events(
        graph_input,
        config=config,
        version="v2",
    ):
        kind = event.get("event", "")
        name = event.get("name", "")
        # A subagent's events carry the delegating `task`/`task_batch` tool-call id (set
        # as run metadata in graph.agent._run_subagent), so the console can nest the
        # subagent's own tool cards under the delegation card BY ID — not by frame order
        # (the delegation runs detached, so the task's on_tool_end races ahead of these).
        parent_tool_id = (event.get("metadata") or {}).get("parent_task_id")
        # Skills are no longer auto-retrieved per turn (ADR 0060 — progressive
        # disclosure); the model loads one on demand via the `load_skill` tool,
        # which surfaces as an ordinary tool card. No `skills_loaded` event to forward.
        if kind == "on_chat_model_start":
            # Stamp the per-call start so on_chat_model_end can measure latency.
            rid = event.get("run_id")
            if rid:
                _llm_started[rid] = time.monotonic()
        elif kind == "on_tool_start":
            # No frame here: the tool card is surfaced earlier — on the model's first
            # streamed tool-call token (on_chat_model_stream) and finalized with full
            # args on on_chat_model_end, both keyed by the tool_call id so on_tool_end
            # closes the same card. Execution-start carries only a run_id (no
            # tool_call id to correlate), so it would just make a duplicate card.
            # It IS the right place to stamp EXECUTION latency though (#2697) — unlike
            # the card-announce timing above, on_tool_start/on_tool_end bracket exactly
            # how long the tool took to run, mirroring _llm_started's run_id-keyed idiom.
            rid = event.get("run_id")
            if rid:
                _tool_started[rid] = time.monotonic()
            # A foreground `delegate_to` is the lead addressing a participant — the same
            # act as an operator `@`. Stash its target now (the args are on the start
            # event, not the end) so on_tool_end can render the reply as an AUTHORED room
            # bubble instead of a tool card (#3042). Correlated by run_id, the same key
            # the latency timing above uses.
            if name == "delegate_to" and rid:
                _tgt = (event.get("data") or {}).get("input") or {}
                _tgt = _tgt if isinstance(_tgt, dict) else {}
                _target = str(_tgt.get("target") or "").strip()
                _q = str(_tgt.get("query") or "").strip()
                _summary = _delegation_summary(_tgt.get("summary"), _q)
                if _target and _tgt.get("background") is True:
                    # A BACKGROUND delegation returns a receipt, not the delegate's answer —
                    # that arrives later through the background drain (#3051). So no reply
                    # frame at on_tool_end (the receipt is instructions to the MODEL; shown
                    # as the delegate's words it read as the delegate's thought process),
                    # and the ask waits for on_tool_end too, to carry the job id the
                    # console tracks the delegation's status by.
                    _bg_delegations[rid] = {"id": rid, "target": _target, "query": _q, "summary": _summary}
                elif _target:
                    _delegate_targets[rid] = _target
                    # Surface the lead's OUTGOING ask, so the operator sees what was
                    # delegated, not just the reply (#3042) — the `lead → proto` half of the
                    # exchange. `addressed_to` + no `author` = the lead speaking to a
                    # participant; the reply below is `author`-stamped as the participant.
                    # The console shows `summary` and keeps the full query behind a
                    # disclosure: the prompt is written for the delegate, not the operator.
                    if _q:
                        yield (
                            "room_reply",
                            # `id` is this delegation's run — two identical asks (same target,
                            # same words) are still two rows, not one deduped away.
                            {"id": rid, "addressed_to": _target, "text": _q, "summary": _summary, "ok": True},
                        )
        elif kind == "on_tool_end":
            output = event.get("data", {}).get("output", "")
            rid = event.get("run_id")
            # `delegate_to` renders as the participant's own chat bubble, not a tool card:
            # the collaboration the lead moderates then reads as a conversation (proto,
            # reviewer) rather than machinery under one reply (#3042). REPLACES the card —
            # this branch emits a room_reply and continues, so no tool_end frame follows.
            # Foreground only: a background delegate_to answers via the background manager —
            # its on_tool_end is the receipt, handled just above.
            _bg = _bg_delegations.pop(rid, None) if rid else None
            if _bg:
                # A background delegate_to's receipt: surface the ASK — once, with the job id
                # to track and the summary — and nothing else. No tool card (#3042) and no
                # reply frame: the delegate's answer arrives on its own through the drain.
                _receipt = _coerce_room_text(output)
                _job = _BG_JOB_ID.search(_receipt)
                _failed = getattr(output, "status", None) == "error" or bool(_BG_DISPATCH_REFUSED.match(_receipt))
                if _job or _failed:
                    yield (
                        "room_reply",
                        {
                            "id": _bg["id"],
                            "addressed_to": _bg["target"],
                            "text": _bg["query"],
                            "summary": _bg["summary"],
                            "background": True,
                            "ok": not _failed,
                            **({"job_id": _job.group(1)} if _job else {}),
                            **({"error": _receipt} if _failed else {}),
                        },
                    )
                    continue
                # No job handle and no error: no BackgroundManager was wired, so the tool
                # fell back to an inline dispatch and `output` IS the delegate's reply. Render
                # it as the foreground exchange it turned into.
                yield (
                    "room_reply",
                    {
                        "id": _bg["id"],
                        "addressed_to": _bg["target"],
                        "text": _bg["query"],
                        "summary": _bg["summary"],
                        "ok": True,
                    },
                )
                yield (
                    "room_reply",
                    {
                        "author": _bg["target"],
                        "from": "assistant",
                        "text": _receipt,
                        "ok": True,
                        "catchup": 0,
                        "truncated": False,
                    },
                )
                continue
            _dtgt = _delegate_targets.pop(rid, None) if rid else None
            if _dtgt:
                _dtext = _coerce_room_text(output)
                _derror = getattr(output, "status", None) == "error"
                yield (
                    "room_reply",
                    {
                        "author": _dtgt,
                        "from": "assistant",  # the LEAD addressed this participant
                        "text": _dtext,
                        "ok": not _derror,
                        "catchup": 0,
                        "truncated": False,
                    },
                )
                continue
            tool_duration_ms = (
                int(max(0.0, time.monotonic() - _tool_started.pop(rid, time.monotonic())) * 1000) if rid else 0
            )
            # Close the card keyed by the tool_call id (the ToolMessage carries it);
            # fall back to run_id/name for non-tool-message producers. A ToolMessage
            # the ToolNode stamped status="error" (a raised tool — a declined
            # run_command, an execution error, an enforcement block) closes the card
            # as a failure (X) instead of a green "done".
            coerced = _coerce_tool_output(output)
            # A show_component result (ADR 0051) carries a sentinel-wrapped payload — lift it
            # into a `component` frame (→ a component-v1 DataPart) and strip it from the card so
            # the user sees the rendered widget, not raw wire JSON. Extract from the FULL tool
            # content, NOT the truncated card preview `coerced`: _coerce_tool_output caps at
            # _TOOL_PREVIEW_CHARS for the SSE frame, which cuts a large component's JSON tail and
            # breaks extraction (#1323 — a rich timeline rendered nothing).
            full = getattr(output, "content", output)
            if isinstance(full, str):
                from graph.components import extract_component, strip_component

                comp = extract_component(full)
                if comp is not None:
                    yield ("component", comp)
                    coerced = str(strip_component(full))[:_TOOL_PREVIEW_CHARS]  # card = human prefix
            yield (
                "tool_end",
                {
                    "id": getattr(output, "tool_call_id", None) or event.get("run_id") or name,
                    "name": name,
                    "output": coerced,
                    # True pre-truncation size — the preview above is capped, so the
                    # console's context-cost estimate must come from this (#2775).
                    "output_chars": _tool_output_chars(output),
                    "error": getattr(output, "status", None) == "error",
                    "duration_ms": tool_duration_ms,  # #2697 — execution time, on_tool_start→on_tool_end
                    **({"parentId": parent_tool_id} if parent_tool_id else {}),
                },
            )
        elif kind == "on_chat_model_stream":
            chunk = event.get("data", {}).get("chunk")
            if chunk is None:
                continue
            # Surface the tool card the moment the model streams a tool *name* —
            # before the call is fully formed or executed — so the UI shows
            # "<tool> · running" instead of a bare loading wheel. Keyed by the
            # tool_call id; on_chat_model_end fills the args, on_tool_end closes it.
            for tcc in getattr(chunk, "tool_call_chunks", None) or []:
                tcid, tcname = tcc.get("id"), tcc.get("name")
                # A `delegate_to` gets NO tool card — it renders as an authored room
                # bubble at on_tool_end instead (#3042). Marked announced so neither this
                # pass nor the on_chat_model_end finalize re-cards it.
                if tcid and tcname == "delegate_to":
                    announced_tools.add(tcid)
                    continue
                if tcid and tcname and tcid not in announced_tools:
                    announced_tools.add(tcid)
                    yield (
                        "tool_start",
                        {"id": tcid, "name": tcname, "input": "", **({"parentId": parent_tool_id} if parent_tool_id else {})},
                    )
            # A subagent's answer + reasoning belong to ITS delegation, not the lead's
            # turn: `_run_subagent` captures the subagent's final message and hands it back
            # as the `task`/`task_batch` tool result (which renders as the delegation card's
            # output). But the subagent's LLM events still surface on THIS stream because
            # LangChain propagates the parent run's callbacks into the nested `ainvoke`,
            # tagging them with `parent_task_id` (set in graph.agent._run_subagent).
            # Forwarding those content/reasoning chunks streams the subagent's internals
            # into the lead answer — polluting `accumulated_raw` and, under `task_batch`,
            # interleaving every concurrent subagent's tokens character-by-character (the
            # garbled-output bug). Suppress them here: the subagent's tool cards above still
            # nest by id, and the `on_chat_model_end` cost accounting below is untouched
            # (subagent tokens still bill). Only the lead's own tokens reach the answer.
            if parent_tool_id:
                continue
            # Nor does any other model call that is not the lead answering: one made under
            # a tool — its body, a graph it runs (a workflow step), work it detached (a
            # background ingest's describe/enrich, a plugin's spawn_work) — or a
            # middleware's own call (the compaction summary). Those tokens used to land in
            # the middle of the answer ("I started the IMAGE-DESCRIPTIONingest…") and in
            # the stored text. Billing below is untouched.
            if not _speaks_for_the_lead(event.get("metadata") or {}):
                continue
            # Native reasoning: the model's REAL thinking, streamed on its own channel.
            # `_ReasoningChatOpenAI` lifts the gateway's `reasoning_content` into
            # additional_kwargs; reasoning chunks carry NO `content`, so this is checked
            # independently of the answer below. Rendered live as a collapsible "thinking"
            # view — it never enters the answer text (so it can't leak to storage either).
            native_reasoning = (getattr(chunk, "additional_kwargs", None) or {}).get("reasoning_content")
            if native_reasoning:
                yield ("reasoning", native_reasoning if isinstance(native_reasoning, str) else str(native_reasoning))
            # The answer is the model's content, streamed directly — no <scratch_pad>/<output>
            # protocol. (extract_output at the terminal still strips any stray legacy tag.)
            if hasattr(chunk, "content") and chunk.content:
                # Content is a plain string (gateway / chat-completions) or LangChain
                # content blocks (the Responses API — the openai-codex provider, ADR
                # 0097). `.text` concatenates the text blocks (skipping reasoning items)
                # and returns a string unchanged, so the answer never renders as a raw
                # "[{'type': 'text', ...}]" list. A reasoning-only chunk yields "".
                text = chunk.content if isinstance(chunk.content, str) else chunk.text
                if text:
                    # Delta-profile diagnostic (#2993). What this log showed: the LiteLLM
                    # gateway path yields ~1-4 char per-token deltas (so the executor's
                    # _FLUSH_CHARS=24 / _FLUSH_INTERVAL_S=0.1 batching does the smoothing),
                    # while the anthropic-oauth path (the Anthropic SDK via
                    # langchain-anthropic) yields multi-word bursts — ~40-60 chars, 8-9
                    # words per AIMessageChunk. The SDK coalesces stream events UPSTREAM of
                    # this loop, so there is nothing finer for the executor to flush and
                    # each burst reaches the console as one artifact-update frame. Fixed
                    # client-side: the console paces rendering through a reveal queue
                    # (apps/web/src/chat/revealQueue.ts). The server flush behavior is
                    # correct and unchanged (tests/test_a2a_flush_granularity.py).
                    if log.isEnabledFor(logging.DEBUG):
                        log.debug(
                            "[stream-delta] t=%.3f chars=%d words=%d preview=%r",
                            time.monotonic(),
                            len(text),
                            len(text.split()),
                            text[:40],
                        )
                    run = event.get("run_id")
                    if run != _answer_run and accumulated_raw.strip():
                        text = _paragraph_break(accumulated_raw, text) + text
                    _answer_run = run
                    accumulated_raw += text
                    yield ("text", text)
        elif kind == "on_chat_model_end":
            output = event.get("data", {}).get("output")
            # Finalize each tool card with its full args, keyed by the tool_call id.
            # `announced_tools` is scoped to THIS turn: this pass also surfaces a card
            # for any tool the stream path didn't announce (e.g. a non-streaming model)
            # without re-emitting an early start already sent earlier this turn.
            for tc in getattr(output, "tool_calls", None) or []:
                tcid = tc.get("id")
                if tcid and tc.get("name") == "delegate_to":
                    announced_tools.add(tcid)  # room bubble, not a card (#3042)
                    continue
                if tcid:
                    announced_tools.add(tcid)
                    yield (
                        "tool_start",
                        {
                            "id": tcid,
                            "name": tc.get("name", ""),
                            "input": _coerce_tool_value(tc.get("args", "")),
                            **({"parentId": parent_tool_id} if parent_tool_id else {}),
                        },
                    )
            usage = getattr(output, "usage_metadata", None) if output else None
            rid = event.get("run_id")
            latency_s = max(0.0, time.monotonic() - _llm_started.pop(rid, time.monotonic())) if rid else 0.0
            model = (
                (event.get("metadata") or {}).get("ls_model_name")
                or getattr(output, "response_metadata", {}).get("model_name", "")
                or "model"
            )
            if usage:
                # Prompt-cache token details (best-effort — OpenAI-compat exposes
                # cached reads via prompt_tokens_details; cache_creation is
                # Anthropic-specific and may not round-trip every gateway).
                details = usage.get("input_token_details") or {}
                cache_read = int(details.get("cache_read", 0) or 0)
                cache_creation = int(details.get("cache_creation", 0) or 0)
                usage_out = {
                    "input_tokens": int(usage.get("input_tokens", 0) or 0),
                    "output_tokens": int(usage.get("output_tokens", 0) or 0),
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_creation,
                }
                cost = pricing.cost_usd(model, usage_out)
                finish_reason = getattr(output, "response_metadata", {}).get("finish_reason", "") or "stop"
                # Wire the per-call Prometheus seam (no-op when unconfigured);
                # previously record_llm_call was defined but never called. The
                # per-call Langfuse generation span comes from the LiteLLM
                # gateway callback — we deliberately don't add a manual shim
                # that would bypass trace_session's nesting (see tracing.py).
                try:
                    metrics.record_llm_call(
                        model,
                        finish_reason,
                        latency_s,
                        tokens_input=usage_out["input_tokens"],
                        tokens_output=usage_out["output_tokens"],
                        cache_read=cache_read,
                        cache_creation=cache_creation,
                        cost_usd=cost,
                    )
                except Exception:  # noqa: BLE001 — telemetry must never break a turn
                    pass
                # Carry cache fields + cost + the ACTUAL model to the A2A handler
                # for the cost-v1 artifact (accumulated across the turn's calls).
                # The model name proves routing per turn — incl. aux/fallback
                # models — vs. the statically-configured lead (ADR 0006 Slice 4b).
                # A subagent's model call (parent_tool_id set) is NOT yielded here:
                # its usage reaches the accumulator via the `task`/`task_batch`
                # custom usage events instead (#2872) — collected from the
                # sub-graph's final state, which also covers delegation paths whose
                # callback-propagated end events never reach this loop. Yielding
                # here too would double-bill the calls that DO bubble up. The
                # per-call Prometheus record above still counts them.
                if not parent_tool_id:
                    yield ("usage", {**usage_out, "cost_usd": cost, "model": model})
        elif kind == "on_custom_event" and name == "usage":
            # A delegation's model usage. Two producers dispatch into this lane:
            # the `task`/`task_batch` tool body after its sub-graph settles
            # (#2872), and the delegates a2a adapter with an A2A peer's OWN
            # cost-v1 telemetry read off the terminal artifact (#3016). Same
            # shape as the on_chat_model_end frame above plus a `subagent_type`
            # or `peer` tag (the executor keeps tagged rows out of the lead
            # thread's context-window fill); forwarded verbatim so the turn bills
            # delegated work.
            data = event.get("data")
            if isinstance(data, dict):
                yield ("usage", dict(data))
        elif kind == "on_custom_event" and name == "steer_consumed":
            # SteeringMiddleware emits this immediately after draining queued
            # operator input and before the next model call. Preserve that exact
            # boundary on the wire so the console can split the live assistant
            # message instead of floating the steer above the whole turn (#2959).
            data = event.get("data")
            items = data.get("items") if isinstance(data, dict) else None
            if isinstance(items, list):
                clean = [
                    {"id": str(item.get("id") or ""), "text": str(item.get("text") or "")}
                    for item in items
                    if isinstance(item, dict) and item.get("id") and item.get("text")
                ]
                if clean:
                    yield ("steer_consumed", {"items": clean})

    # HITL pause (ADR 0003): the agent called ask_human → LangGraph interrupt().
    # The graph is checkpointed at the interrupt; surface the question so the A2A
    # layer parks the task as input-required. Resume later with resume_value.
    interrupt_val = await _pending_interrupt_value(config)
    if interrupt_val is not None:
        yield ("input_required", _interrupt_payload(interrupt_val))
        return

    yield ("__raw__", accumulated_raw)


# Slash-command parsing + execution moved to server/chat_commands.py — it was the one
# concern in this module that isn't the turn loop. Re-imported (not just referenced) so
# `server.<symbol>` keeps resolving through the ADR 0023 re-export block in
# server/__init__.py, which the test suite imports from (tests/test_skill_slash.py).
from server.chat_commands import (  # noqa: E402,F401 — re-export; see above
    _parse_skill_command,
    _parse_slash_command,
    _parse_subagent_command,
    _parse_workflow_command,
    _parse_workflow_inputs,
    _run_parsed_subagent,
    _run_parsed_workflow,
    _skill_directive,
)

# The turn loop's own uses of the shared slash resolver — the lifecycle-command check
# and the plugin chat-command dispatch, neither of which is workflow/subagent/skill
# parsing. chat_commands.py imports the same neutral module for its own needs.
from graph.slash_commands import (  # noqa: E402
    PluginFormRequest as _PluginFormRequest,
    run_plugin_chat_command as _run_plugin_chat_command,
    slash_kind as _slash_kind,
)

# What counts as a slash-command TOKEN — a letter then word chars/hyphens, the whole
# first whitespace-separated word (mirrors the console's `slashCommandName` regex).
# `/home/user/file.txt` fails the fullmatch (its token contains `/` and `.`), so a
# path or prose with a `/` is never mistaken for a command.
_SLASH_TOKEN_RE = re.compile(r"[A-Za-z][\w-]*")


def _unknown_slash_command_reply(message: str) -> str | None:
    """The short-circuit reply for a message that LOOKS like a slash command but
    resolves to no registered one (#2893), else ``None`` (fall through to the normal
    turn). Runs LAST in the dispatch chain, so every registered kind (goal /
    lifecycle / plugin command / workflow / subagent / skill) keeps winning — only a
    genuinely unknown ``/foobar`` is caught instead of silently becoming a plain
    agent turn on the raw command text."""
    name, _rest = _parse_slash_command(message)
    if not name or _SLASH_TOKEN_RE.fullmatch(name) is None:
        return None
    if _slash_kind(name) is not None:
        return None
    return f"Unknown command /{name}. Type / to see available commands."


# The @-delegate room exchange (``_at_delegate_exchange`` and its parse/note helpers)
# moved to server/chat_rooms.py (#3838) and is re-exported at the top of this module.


# Per-thread_id locks (WeakValueDictionary so a lock is GC'd once no turn holds it,
# bounding memory). See _thread_lock.
_THREAD_LOCKS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def _thread_lock(thread_id: str) -> asyncio.Lock:
    """Per-thread_id async lock — serializes turns on the SAME checkpointer thread so
    two concurrent A2A message/send on one context_id can't lost-update each other's
    history. Auto-evicted once no turn references the lock."""
    lock = _THREAD_LOCKS.get(thread_id)
    if lock is None:
        lock = asyncio.Lock()
        _THREAD_LOCKS[thread_id] = lock
    return lock


# Origins (ADR 0022) whose turns run with NO operator watching the chat: a HITL pause
# (ask_human / request_user_input) on one of these would park the task in input-required
# FOREVER — and that state is deliberately exempt from the TTL sweep, so it never settles.
# Live operator turns carry an empty origin (they keep parking — a human is watching);
# inbound `a2a` calls are excluded too, because the remote caller can itself resume the
# input-required task. For everything here, we auto-answer the interrupt instead of parking.
# "background-resume" is the ADR 0070 push-resume nudge — server-fired like the rest
# (the manager discards the A2A response). It stays in this set so an UNATTENDED nudge
# still auto-answers, but it is no longer conclusive on its own (#3110): when a live
# operator is attending the origin session (see the attendance registry below), the
# manager stamps the nudge ``attended`` and the report-delivery turn becomes eligible to
# park for a human, exactly like an ordinary operator turn.
_AUTONOMOUS_ORIGINS = frozenset(
    {"scheduler", "watch", "inbox", "webhook", "background", "background-resume", "delegate-result"}
)

# The immutable provenance of a result-delivery nudge (kept in _AUTONOMOUS_ORIGINS above)
# is distinct from whether a human is *currently* attending its origin session (#3110).
# These are the origins whose autonomy is conditional on live attendance rather than fixed.
_ATTENDANCE_CONDITIONAL_ORIGINS = frozenset({"background-resume", "delegate-result"})

# ── session attendance (SSE presence) — #3110 ────────────────────────────────
# Which origin chat sessions currently have a LIVE operator SSE connection (the console
# opens GET /api/chat/attend while a session is on screen; see ``attendance_stream``).
# Refcounted, because N tabs / a reconnect mid-drop can attend one session at once, and
# an unbalanced release must not evict a session another connection still holds. Bounded
# so a leaked registration can never grow it without limit. This is a live, in-process
# snapshot the manager reads at push-resume time to decide attended vs. unattended — it
# is NOT durable and, by design, fails CLOSED (an unknown/blank/over-cap session reads
# unattended), so ambiguity can never let a server-fired turn park with nobody to answer.
_ATTENDED_SESSIONS: dict[str, int] = {}
_ATTENDED_SESSIONS_MAX = 1024


def mark_session_attended(session_id: str) -> bool:
    """Register one live operator SSE connection to ``session_id`` (refcount++). Returns
    whether the session is now recorded attended. Bounded + fail-closed: a blank id, or a
    NEW session once the registry is at its cap, is a no-op (that session reads unattended)
    — a presence leak degrades to the pre-#3110 auto-answer behavior, never to unbounded
    growth or a spurious park."""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    if sid not in _ATTENDED_SESSIONS and len(_ATTENDED_SESSIONS) >= _ATTENDED_SESSIONS_MAX:
        log.warning("[attendance] registry at cap (%d) — dropping presence for %s", _ATTENDED_SESSIONS_MAX, sid)
        return False
    _ATTENDED_SESSIONS[sid] = _ATTENDED_SESSIONS.get(sid, 0) + 1
    return True


def release_session_attended(session_id: str) -> None:
    """Drop one live SSE connection to ``session_id`` (refcount--); the entry is removed at
    zero. Idempotent and never raises — safe to call from an SSE teardown ``finally`` even
    for a session that was never (or is no longer) registered."""
    sid = str(session_id or "").strip()
    if not sid:
        return
    n = _ATTENDED_SESSIONS.get(sid, 0) - 1
    if n > 0:
        _ATTENDED_SESSIONS[sid] = n
    else:
        _ATTENDED_SESSIONS.pop(sid, None)


def is_session_attended(session_id: str) -> bool:
    """Whether a live operator is connected to ``session_id`` right now. Fails CLOSED: a
    blank id or any lookup error reads as unattended (#3110 — an ambiguous attendance
    signal must never make an otherwise-autonomous turn eligible to park indefinitely)."""
    try:
        sid = str(session_id or "").strip()
        return bool(sid) and _ATTENDED_SESSIONS.get(sid, 0) > 0
    except Exception:  # noqa: BLE001 — ambiguity fails closed to unattended
        return False


# ── server-turn control plane (#3092) ───────────────────────────────────────

_CONTROL_ORIGINS = frozenset({"background-resume", "scheduler", "watch", "inbox", "delegate-result"})


@dataclass
class _LiveServerTurn:
    session_id: str
    task_id: str
    origin: str
    trigger: str = ""
    controllable: bool = False
    accepted_ids: set[str] = field(default_factory=set)


_LIVE_SERVER_TURNS: dict[str, _LiveServerTurn] = {}


def _server_turn_key(session_id: str, task_id: str) -> str:
    return f"{session_id}\0{task_id}"


def _control_origin(origin: object) -> str:
    return str(origin or "").strip().lower()


def server_turn_control_payload(
    session_id: str,
    task_id: str,
    *,
    origin: object = "",
    trigger: object = "",
    attended: object = None,
) -> dict[str, Any] | None:
    """Session-scoped control descriptor for a server-originated chat turn.

    The payload is deliberately small and durable-task keyed: the UI can address the
    in-flight turn by ``task_id``, but the server will only honor it for the SAME
    ``session_id`` and while that exact task remains live in this registry.
    """
    sid = str(session_id or "").strip()
    tid = str(task_id or "").strip()
    org = _control_origin(origin)
    if not sid or not tid or org not in _CONTROL_ORIGINS:
        return None
    is_attended = _truthy(attended) if attended is not None else is_session_attended(sid)
    controllable = bool(is_attended)
    return {
        "session_id": sid,
        "task_id": tid,
        "origin": org,
        "trigger": str(trigger or ""),
        "controllable": controllable,
        "operator_controllable": controllable,
    }


def register_live_server_turn(
    session_id: str,
    task_id: str,
    *,
    origin: object = "",
    trigger: object = "",
    attended: object = None,
) -> dict[str, Any] | None:
    """Record an attended-capable server-originated turn as addressable by task id."""
    payload = server_turn_control_payload(
        session_id,
        task_id,
        origin=origin,
        trigger=trigger,
        attended=attended,
    )
    if payload is None:
        return None
    key = _server_turn_key(payload["session_id"], payload["task_id"])
    _LIVE_SERVER_TURNS[key] = _LiveServerTurn(
        session_id=payload["session_id"],
        task_id=payload["task_id"],
        origin=payload["origin"],
        trigger=payload["trigger"],
        controllable=bool(payload["controllable"]),
    )
    return payload


def finish_live_server_turn(session_id: str, task_id: str) -> None:
    """Forget a live server turn once it parks or reaches a terminal state."""
    sid = str(session_id or "").strip()
    tid = str(task_id or "").strip()
    if sid and tid:
        _LIVE_SERVER_TURNS.pop(_server_turn_key(sid, tid), None)


def live_server_turn_control(session_id: str, task_id: str) -> dict[str, Any] | None:
    """The recorded control payload for this exact live session/task, if any."""
    sid = str(session_id or "").strip()
    tid = str(task_id or "").strip()
    turn = _LIVE_SERVER_TURNS.get(_server_turn_key(sid, tid)) if sid and tid else None
    if turn is None:
        return None
    currently_controllable = bool(turn.controllable and is_session_attended(turn.session_id))
    return {
        "session_id": turn.session_id,
        "task_id": turn.task_id,
        "origin": turn.origin,
        "trigger": turn.trigger,
        "controllable": currently_controllable,
        "operator_controllable": currently_controllable,
    }


def submit_server_turn_interjection(
    session_id: str,
    task_id: str,
    text: str,
    *,
    msg_id: str | None = None,
) -> dict[str, Any]:
    """Queue an operator interjection for a live, attended server-originated turn.

    This intentionally does not acquire the per-thread graph lock or start a graph
    turn. It only enqueues into the same steering queue that an already-running turn
    drains at the model-call boundary. Stale, terminal, wrong-session, unattended, and
    duplicate-id submissions are rejected without touching another session's queue.
    """
    sid = str(session_id or "").strip()
    tid = str(task_id or "").strip()
    body = str(text or "").strip()
    if not body:
        return {"ok": False, "reason": "empty", "pending": 0}
    turn = _LIVE_SERVER_TURNS.get(_server_turn_key(sid, tid)) if sid and tid else None
    if turn is None:
        return {"ok": False, "reason": "not_live", "pending": 0}
    if not turn.controllable or not is_session_attended(sid):
        return {"ok": False, "reason": "uncontrollable", "pending": 0}
    mid = str(msg_id or "").strip() or f"server-steer:{tid}:{len(turn.accepted_ids) + 1}"
    if mid in turn.accepted_ids:
        from graph import steering

        return {"ok": False, "reason": "duplicate", "id": mid, "pending": steering.pending(sid)}
    from graph import steering

    queued = steering.enqueue(sid, body, msg_id=mid)
    if queued is None:
        return {"ok": False, "reason": "empty", "pending": steering.pending(sid)}
    turn.accepted_ids.add(mid)
    return {"ok": True, "id": queued, "pending": steering.pending(sid)}


async def attendance_stream(session_id: str, *, keepalive_s: float = 15.0, is_disconnected=None):
    """SSE body for ``GET /api/chat/attend`` (#3110). Marks ``session_id`` attended for the
    whole life of the connection and RELEASES it in a ``finally`` — so a tab close, a route
    change, or a dropped socket always cleans up and presence fails back to unattended.

    Yields a ``: attending`` comment up front (fires the client's ``onopen``) then periodic
    ``: keepalive`` comments to hold the connection open through idle stretches, mirroring
    ``operator_api.routes._sse_event_stream``. ``is_disconnected`` (the request's disconnect
    probe) lets the loop exit promptly; when omitted the generator relies on being cancelled
    on client disconnect, which still runs the ``finally``."""
    sid = str(session_id or "").strip()
    attended = mark_session_attended(sid)
    try:
        yield ": attending\n\n"
        while True:
            if is_disconnected is not None:
                try:
                    if await is_disconnected():
                        break
                except Exception:  # noqa: BLE001 — a probe failure ends the stream (cleanup in finally)
                    break
            await asyncio.sleep(keepalive_s)
            yield ": keepalive\n\n"
    finally:
        if attended:
            release_session_attended(sid)

# What we resume an autonomous turn's HITL interrupt with, so the agent stops waiting and
# finishes the turn instead of deadlocking. Bounded by the cap below so a model that keeps
# re-asking can't auto-answer in an infinite loop; past the cap we force the turn to complete
# (clearing the stray interrupt) rather than parking — an autonomous turn must never park.
_AUTONOMOUS_HITL_SENTINEL = (
    "[no interactive operator available] This turn is running autonomously "
    "(scheduled / inbox / background), so no human can answer right now. Do not wait for "
    "input — proceed using your best judgment and explicitly state any assumption you made."
)
_MAX_AUTONOMOUS_AUTOANSWERS = 3


def _truthy(value) -> bool:
    """Coerce a metadata value to bool. JSON-RPC callers may send the flag as a real bool,
    a string ("true"/"1"), or an int — bare bool("false") is a footgun, so treat strings
    by their content."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def is_autonomous_origin(origin: object) -> bool:
    """Whether ``origin`` names a SERVER-FIRED turn — one nobody is watching or holding a
    stream for (see ``_AUTONOMOUS_ORIGINS``).

    Public because two decisions now hinge on the same set and must not drift: this
    module's HITL auto-answer (an unattended turn must not park for a human), and
    ``server.a2a``'s live-progress republish (#2361 — a turn the browser is streaming
    itself must NOT also come over the bus, or every tool card renders twice)."""
    return str(origin or "").strip().lower() in _AUTONOMOUS_ORIGINS


# ADR 0115 D6 — origins whose turns an operator is actively watching. These run under the
# model in-flight limiter's `interactive` class (graph/llm_limiter.py), so they (and the
# subagent tasks they spawn) jump the queue ahead of `bulk` fan-outs and hold the reserved
# slot on a saturated lane. The console's own turns carry `api-chat` (the non-streaming
# `/api/chat` route) or an empty origin (the streaming A2A path — a live operator is holding
# the stream; see the empty-origin note on `_AUTONOMOUS_ORIGINS` above). Everything else stays
# `default` (tagging is best-effort — the ADR tags nothing else): inbound `a2a` (the REMOTE
# caller is watching, not us), the server-fired autonomous origins, and the programmatic
# `v1` / `plugin` API surfaces.
_INTERACTIVE_ORIGINS = frozenset({"", "local", "api-chat", "console"})


def is_interactive_origin(origin: object) -> bool:
    """Whether ``origin`` names an operator-watched chat/console turn (ADR 0115 D6 —
    ``interactive`` on the model in-flight limiter). See ``_INTERACTIVE_ORIGINS``."""
    return str(origin or "").strip().lower() in _INTERACTIVE_ORIGINS


@contextlib.contextmanager
def _interactive_turn_priority(origin: object):
    """Run an operator-watched turn under the ADR 0115 D6 ``interactive`` model-limiter class,
    so it and the subagent tasks it spawns queue ahead of ``bulk`` fan-outs on a saturated
    lane. A no-op for every other origin, which stays ``default`` — A2A, background and
    scheduled turns are deliberately left untagged (best-effort tagging; untagged is
    ``default``).

    Mirrors ``goal_turn``: the ContextVar reset can raise if this scope is torn down in a
    different context than it was entered (an SSE consumer's early ``GeneratorExit`` closing
    the streaming generator), and the var resets on context exit regardless, so the raise is
    swallowed."""
    if not is_interactive_origin(origin):
        yield
        return
    from graph.llm_limiter import INTERACTIVE, reset_priority, set_priority

    token = set_priority(INTERACTIVE)
    try:
        yield
    finally:
        try:
            reset_priority(token)
        except ValueError:
            pass


def _background_resume_attended(request_metadata: dict | None) -> bool:
    """Whether a ``background-resume`` nudge was stamped ATTENDED at push-resume time — a
    live operator was connected to the origin session when the manager fired it (#3110).

    Read from the request metadata (the manager snapshots the SSE-boundary presence once,
    at resume time, so singleton and coalesced-batch nudges make the SAME decision for a
    given origin session and the turn honors it deterministically instead of re-racing a
    flapping signal). Fail-closed: anything but an explicit truthy ``attended`` reads as
    unattended, so an unstamped nudge keeps the pre-#3110 autonomous behavior."""
    return _truthy((request_metadata or {}).get("attended"))


def _is_autonomous(request_metadata: dict | None) -> bool:
    """Whether this turn runs with no operator watching (see _AUTONOMOUS_ORIGINS).

    Headless-first (#1911): a fleet-to-fleet A2A caller with no human in the loop can also
    DECLARE the turn unattended by setting ``unattended: true`` in the request metadata,
    which takes the same no-deadlock path as the internal autonomous origins. Undeclared
    plain A2A stays operator-attended (an approval interrupt still parks for a human).

    Attended background-resume (#3110): a ``background-resume`` nudge is autonomous ONLY
    while its origin session has no live operator connection. When the manager stamped it
    ``attended`` (an operator was on the wire at resume time), the report-delivery turn is
    NOT autonomous — it may park for ``ask_human`` / ``request_user_input`` so the human
    can answer, exactly like an ordinary operator turn. Every other autonomous origin
    (scheduler / watch / inbox / webhook / detached background) is unconditional, and an
    explicit ``unattended: true`` always wins."""
    md = request_metadata or {}
    if _truthy(md.get("unattended")):
        return True
    origin = md.get("origin")
    if (
        str(origin or "").strip().lower() in _ATTENDANCE_CONDITIONAL_ORIGINS
        and _background_resume_attended(md)
    ):
        return False
    return is_autonomous_origin(origin)


def _awaiting_self_resume(session_id: str) -> bool:
    """Async handoff (ADR 0079): True when the agent has queued a trigger that will resume THIS
    session later — an active watch whose reaction fires here, or a pending scheduled/wait job
    targeting this context. The goal drive loop PAUSES (leaves the goal active, ends the turn)
    when one exists instead of spinning its continuation loop to the iteration cap; the trigger's
    eventual fire re-enters the session and — the goal still being active — resumes the drive.
    The one-shot that fired THIS turn is not a future resume (``_is_spent_firing_job``): it is
    deleted as soon as the turn returns, so pausing on it strands the goal.
    Best-effort: any read failure just means "not awaiting" (fall through to the normal loop)."""
    if not session_id:
        return False
    try:
        wc = STATE.watch_controller
        if wc is not None and any(
            getattr(w, "status", "") == "active" and getattr(w, "run_session", "") == session_id
            for w in wc.list_watches()
        ):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        sched = STATE.scheduler
        if sched is not None and any(
            getattr(j, "context_id", None) == session_id and not _is_spent_firing_job(j) for j in sched.list_jobs()
        ):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _is_spent_firing_job(job) -> bool:
    """Is ``job`` the one-shot that started the turn now running, and so about to be deleted?

    The scheduler deletes a fired one-shot only after the turn it started returns, so during
    that turn it is still listed. A goal session woken by its own ``wait:<session>`` or
    ``watch-<id>`` job used to read that row as a queued resume, pause the drive, and then
    lose the row: nothing ever resumed the goal. Matched on the turn's ``scheduler_job_id``
    (the same ``_is_firing_now`` check the ``<working_state>`` FIRING NOW tag uses), plus:

    - a cron job rolls forward when it is claimed, so it WILL fire here again: still pending;
    - a ``wait`` in this turn re-adds the SAME ``wait:<session>`` id with a future fire time
      (#2751). That row is a new resume, not the one being fired, so a ``next_fire`` still in
      the future counts as pending. An unreadable ``next_fire`` counts as pending too."""
    from datetime import UTC, datetime

    from graph.projection import _is_firing_now
    from scheduler.interface import is_cron, parse_iso_to_utc

    if not _is_firing_now(job) or is_cron(getattr(job, "schedule", "") or ""):
        return False
    try:
        return parse_iso_to_utc(job.next_fire) <= datetime.now(UTC)
    except (TypeError, ValueError):
        return False


def _is_hitl_resume(request_metadata: dict | None) -> bool:
    """Whether this message IS the operator's answer to the pending HITL pause.

    The console stamps ``hitl_resume`` on the message metadata when it submits or
    dismisses a form / question / approval card — that message must resume the parked
    interrupt (``Command(resume=…)``), not run a fresh graph turn. A2A callers that
    resume properly (message/send on the parked taskId) never need this marker — the
    executor already flips ``resume`` for them."""
    return bool((request_metadata or {}).get("hitl_resume"))


# _hold_if_hitl_pending's "this message answers the pending interrupt" signal — a
# sentinel (not a string) so it can never collide with an interrupt VALUE.
_HITL_RESUME = object()


async def _hold_if_hitl_pending(message: str, session_id: str, config: dict, *, request_metadata: dict | None):
    """The HITL hold (#1560): decide what a FRESH message may do while this thread is
    parked at a ``request_user_input`` / ``ask_human`` / approval ``interrupt()``.

    LangGraph treats fresh input on an interrupted thread as "abandon the interrupt and
    continue" — the un-answered tool_call is left dangling (later stripped by
    ToolCallRepairMiddleware) and the model sees the new message BEFORE (and instead of)
    the form answer, while the parked task can never resolve. So while a HITL interrupt
    is pending:

    - the operator's actual answer (``hitl_resume`` metadata) → resume the graph
      properly (returns the ``_HITL_RESUME`` sentinel);
    - any other operator message → HOLD it: park it in the per-session steering queue
      (returns the interrupt payload). It stays queued while the form is open — the
      queue only drains at a model call, and the parked graph makes none — and folds in
      via ``SteeringMiddleware`` at the FIRST model call after the form resolves
      (submitted OR dismissed), i.e. immediately after the form response, in arrival
      order. A dismissal is also a resume, so held messages can never deadlock; the
      pending-form state itself lives in the durable LangGraph checkpoint (re-read
      here every time), so a restart can't strand the hold.

    Autonomous turns are exempt (they must never park — unchanged clobber semantics),
    and with no pending interrupt this returns ``None`` and the turn is untouched.
    Callers must hold the per-thread lock (the check must not race a parking turn)."""
    if _is_autonomous(request_metadata):
        return None
    pending_val = await _pending_interrupt_value(config)
    if pending_val is None:
        return None
    if _is_hitl_resume(request_metadata):
        return _HITL_RESUME
    from graph import steering

    steering.enqueue(session_id, message)
    log.info("[hitl] holding operator message for session %s — form pending", session_id)
    return pending_val


async def _run_native_turn(message, session_id, config, *, request_metadata=None, resume=False, images=None):
    """One native LangGraph turn (the non-ACP path): run the graph, the dropped-turn
    kicker retry, and goal-mode continuations, then yield the terminal done frame. Extracted from _chat_langgraph_stream so the A2A handler can hold a per-thread
    lock around the whole turn without a deep in-line reindent."""
    from graph.goals.goal_turn import goal_turn

    # Per-tab model + reasoning-effort override (the console puts the tab's chosen model +
    # the /effort level in the A2A request metadata). Threaded into every turn this stream
    # runs — initial, kicker, goal continuation. Unset → the configured default.
    _model = ((request_metadata or {}).get("model") or "").strip() or None
    _effort = ((request_metadata or {}).get("reasoning_effort") or "").strip() or None
    # Incognito thread (ADR 0069 D3b): the console/A2A caller sets metadata
    # `incognito: true` per message — no session persistence, no memory injection.
    _incognito = bool((request_metadata or {}).get("incognito"))
    # Detached background runs of a registry subagent carry the resolved tool
    # allowlist in the fire metadata (#1639) — stamped onto the turn's state below.
    _fence = (request_metadata or {}).get("subagent_fence") or None
    if _fence is not None and not isinstance(_fence, (list, tuple)):
        _fence = None
    # When a goal is already active, the whole turn is goal-driven (suppress cross-session
    # prior_sessions on the initial turn + kicker, matching the continuation turns).
    _goal_state = STATE.goal_controller.active_goal(session_id) if STATE.goal_controller is not None else None
    goal_active = _goal_state is not None
    # Kickoff injection (#1910): on the FIRST goal-driven turn (iteration 0, not a HITL resume)
    # rewrite the message to carry the goal condition, so the agent begins on the goal instead
    # of asking "what goal?" — the raw user text is folded into the kickoff prompt. Re-invoke
    # iterations already get the goal via the continuation prompt, so gate on iteration 0.
    if goal_active and not resume and _goal_state.iteration == 0:
        message = STATE.goal_controller.kickoff_prompt(_goal_state, user_message=message)

    # One graph turn (model tokens accumulated silently; A2A consumers get progress from
    # tool_start/tool_end). Final text is extracted once via extract_output().
    accumulated_raw = ""
    paused = False
    last_tool_out = ""  # streaming equivalent of _last_tool_text — the empty-turn fallback answer
    # An autonomous turn (no operator watching) must never deadlock on a HITL pause: when one
    # of these turns hits input_required we resume the graph with a "no operator" sentinel and
    # run another pass, up to a cap, instead of parking the task forever (see _AUTONOMOUS_*).
    # A goal-driven turn is autonomous BY DEFINITION (#1911): a goal is an explicit opt-in to
    # self-drive, so it must take the no-deadlock path and never park on a HITL interrupt even
    # over plain (undeclared) A2A — otherwise the first "what goal?" ask parks the task and the
    # drive loop below never runs (the #1910 deadlock). Non-goal turns are unchanged.
    _autonomous = _is_autonomous(request_metadata) or goal_active
    _resume_value = (message if resume else None)
    _auto_answers = 0
    with goal_turn(goal_active):
        while True:
            _autoanswer_pending = False
            _autonomous_giveup = False
            async for kind, payload in _run_turn_stream(
                message,
                session_id,
                config,
                resume_value=_resume_value,
                images=images,
                model=_model,
                reasoning_effort=_effort,
                incognito=_incognito,
                subagent_fence=_fence,
            ):
                if kind == "__raw__":
                    accumulated_raw = payload
                elif kind == "input_required":
                    if not _autonomous:
                        # Operator/a2a turn: surface it and park the turn; the A2A runner sets
                        # the task input-required and the caller resumes via message/send on the
                        # same taskId. (A human — local or at the remote a2a caller — can answer.)
                        yield (kind, payload)
                        paused = True
                    elif _auto_answers < _MAX_AUTONOMOUS_AUTOANSWERS:
                        # No human can answer — auto-answer this interrupt and re-run the turn so
                        # it completes, rather than parking an (un-sweepable) input-required task.
                        # The graph is checkpointed at the interrupt; the resume below feeds the
                        # sentinel as ask_human / request_user_input's return value.
                        _autoanswer_pending = True
                    else:
                        # Still asking after the auto-answer budget is spent: an autonomous turn
                        # must NEVER park, so give up on the pause and force the turn to a
                        # terminal state. The stray interrupt is cleared after the loop.
                        _autonomous_giveup = True
                else:
                    if kind == "tool_end" and isinstance(payload, dict) and payload.get("output"):
                        last_tool_out = str(payload["output"])
                    yield (kind, payload)
            if _autoanswer_pending:
                # Resume past the interrupt with the no-operator sentinel and run another pass;
                # images belong only to the first (fresh) pass, so drop them on resume.
                _auto_answers += 1
                _resume_value = _AUTONOMOUS_HITL_SENTINEL
                images = None
                continue
            if _autonomous_giveup:
                # Discard the un-answered interrupt so the checkpoint isn't left dangling, then
                # fall through to the normal completion path below (extract_output → done).
                await _clear_pending_interrupt(config)
            break

    # A paused turn produced no final answer — don't run the dropped-scratch kicker or
    # goal verification; the task is parked.
    if paused:
        return

    final_text = extract_output(accumulated_raw)

    # Goal mode: when an active goal exists for this session, verify the outcome after the
    # agent stops; if not met, re-invoke on the same thread with a continuation prompt until
    # the verifier passes, the iteration budget is spent, or it's flagged unachievable.
    if STATE.goal_controller is not None and STATE.goal_controller.active_goal(session_id):
        guard, hard_cap = 0, STATE.graph_config.goal_max_iterations + 2
        note = ""
        while guard < hard_cap:
            guard += 1
            decision = await STATE.goal_controller.evaluate(session_id, last_text=final_text)
            if decision is None:
                break
            note = decision.note
            yield ("tool_start", f"🎯 {decision.note}")
            if decision.action == "done":
                break
            if _awaiting_self_resume(session_id):
                # The agent handed off to a watch/schedule that resumes this session — pause the
                # drive (goal stays active) rather than spinning; the trigger's fire continues it.
                note = "⏸ goal paused — handed off to a watch/schedule; will resume when it fires."
                yield ("tool_start", f"🎯 {note}")
                break
            # Fresh-context goals get a scoped per-iteration thread; same-session reuse
            # `config`. Shared helper keeps the streaming + non-streaming loops in lockstep.
            cont_config = _goal_continuation_config(config, decision.state)

            cont_raw = ""
            with goal_turn():
                async for kind, payload in _run_turn_stream(
                    decision.message, session_id, cont_config, model=_model, reasoning_effort=_effort, incognito=_incognito
                ):
                    if kind == "__raw__":
                        cont_raw = payload
                    else:
                        yield (kind, payload)
            cont_text = extract_output(cont_raw)
            if cont_text:
                final_text = cont_text
        # Append the terminal goal outcome to the answer so the A2A terminal artifact
        # carries it, matching the non-streaming path (the 🎯 status frames above are
        # transient and can coalesce).
        if note:
            final_text = f"{final_text}\n\n---\n{note}"

    # Never end the stream on a silent empty answer (a native-reasoning model that emitted
    # only reasoning, or an otherwise empty turn): surface the last tool result or a
    # placeholder, matching the non-streaming path's _last_tool_text-or-placeholder.
    if not final_text:
        final_text = last_tool_out or "_(The agent ended the turn without a textual reply.)_"

    yield ("done", final_text)


# ── Idle beacon (#1720) ──────────────────────────────────────────────────────
# The plugin auto-update loop honors a plugin's ``when: idle`` policy by checking
# BOTH signals below: the count of chat turns currently in flight (so a turn that
# runs longer than the idle window is never mistaken for idle — a start-only
# timestamp had that bug) AND a monotonic timestamp of the last turn boundary (so
# we also wait out a quiet cooldown after the last turn ends). A hot-reload
# rebuilds tools/routers — safe between turns, disruptive during one. Both entry
# points (streaming = A2A + console-stream, non-streaming = console + OpenAI-compat)
# bracket their turn with ``_turn_started()`` / ``_turn_ended()`` via a thin
# wrapper so the count is always balanced, even on early generator close or error.
_ACTIVE_TURNS: int = 0
_LAST_TURN_MONOTONIC: float = 0.0


def _turn_started(session_id: str = "") -> None:
    global _ACTIVE_TURNS, _LAST_TURN_MONOTONIC
    _ACTIVE_TURNS += 1
    _LAST_TURN_MONOTONIC = time.monotonic()
    # Per-session half of the same bracket: the busy signal `GET /api/chat/sessions/{id}`
    # reports as `active` (the Zed shim waits on it instead of interleaving two turns).
    _turn_activity.begin(session_id)


def _turn_ended(session_id: str = "") -> None:
    global _ACTIVE_TURNS, _LAST_TURN_MONOTONIC
    _ACTIVE_TURNS = max(0, _ACTIVE_TURNS - 1)
    _LAST_TURN_MONOTONIC = time.monotonic()
    _turn_activity.end(session_id)


def active_turns() -> int:
    """Chat turns currently in flight (0 ⇒ nothing running)."""
    return _ACTIVE_TURNS


def seconds_since_last_turn() -> float:
    """Seconds since the last chat turn boundary (start or end), or ``inf`` if none
    yet this process. Paired with ``active_turns()`` for the auto-update idle gate."""
    if _LAST_TURN_MONOTONIC <= 0:
        return float("inf")
    return max(0.0, time.monotonic() - _LAST_TURN_MONOTONIC)


def _lifecycle_command_reply(message: str) -> str | None:
    """If ``message`` is the core ``/lifecycle`` command (ADR 0074), return its read-only
    listing — the three lifecycle events plus the currently-configured config reactions and
    registered plugin hooks. Else ``None`` (fall through). Reserved like ``/goal``, so no
    plugin/workflow/skill can shadow it; listing only (the config file is the source of
    truth for v1 — no runtime mutation)."""
    name, _rest = _parse_slash_command(message)
    if not name or _slash_kind(name) != "lifecycle":
        return None
    from graph.lifecycle import describe

    return describe()


def _note_agent_active(session_id: str) -> None:
    """Emit the ``agent.active`` lifecycle event (ADR 0074) when the agent goes idle →
    active — the FIRST turn since boot, or the first turn after an idle gap (debounced by
    ``graph.lifecycle.should_emit_active`` so a busy session doesn't broadcast every turn).

    Fire-and-forget on the running loop; best-effort so it can never break a turn.
    ``STATE.last_activity_ts`` is updated every turn regardless of whether we emit."""
    from graph.lifecycle import fire, should_emit_active

    now = time.time()
    last = STATE.last_activity_ts
    STATE.last_activity_ts = now
    emit, idle_seconds, previous_state = should_emit_active(now, last)
    if not emit:
        return
    payload = {
        "ts": now,
        "session_id": session_id,
        "idle_seconds": idle_seconds,
        "previous_state": previous_state,
    }
    try:
        asyncio.create_task(fire("agent_active", payload))
    except Exception:  # noqa: BLE001 — a lifecycle emit must never break a turn
        log.debug("[lifecycle] agent.active emit failed", exc_info=True)


async def _chat_langgraph_stream(
    message: str,
    session_id: str,
    *,
    caller_trace: dict | None = None,
    resume: bool = False,
    request_metadata: dict | None = None,
    images: list[tuple[str, str]] | None = None,
):
    """Idle-beacon wrapper (#1720): mark a turn in flight for the whole generator
    lifetime — including early ``aclose()`` and errors — then delegate. Keeps the
    public name/signature so every caller (A2A executor, console) is unchanged."""
    _turn_started(session_id)
    _note_agent_active(session_id)  # ADR 0074 — idle→active lifecycle event (debounced)
    try:
        async for _ev in _chat_langgraph_stream_impl(
            message,
            session_id,
            caller_trace=caller_trace,
            resume=resume,
            request_metadata=request_metadata,
            images=images,
        ):
            _trace_terminal_output(_ev)
            yield _ev
    finally:
        _turn_ended(session_id)


def _trace_terminal_output(ev: tuple) -> None:
    """Write a terminal frame onto the turn's Langfuse trace as its output.

    The model middleware records the answer only from a final tool-call-free reply, and
    most ways a turn can end never produce one: an ``@delegate`` exchange or a slash
    command skips the graph entirely, a turn parked on ``ask_human`` or an approval ends
    on the tool call that asked, and an error ends it with no reply. Each left a trace
    with input and no output. The terminal frame is what the caller actually received,
    so it is the output on every path. This runs while the impl generator is suspended
    inside its ``trace_session`` scope (a generator runs in its consumer's context), so
    the session span is still current here.
    """
    try:
        kind, payload = ev[0], ev[1]
    except (TypeError, IndexError):
        return
    if kind == "done":
        text = payload if isinstance(payload, str) else str(getattr(payload, "text", "") or "")
        # The caller got nothing (a turn that parked on `wait`, or ended on a tool call):
        # say so, rather than leave the trace looking like its output was lost.
        text = text or "(the turn ended without reply text)"
    elif kind == "input_required" and isinstance(payload, dict):
        text = str(payload.get("question") or payload.get("title") or "")
        text = f"[input required] {text}".rstrip()
    elif kind == "error":
        text = f"[error] {payload}"
    else:
        return
    _set_trace_output(text)


def _trace_reply_output(reply: Any) -> None:
    """The non-streaming driver's counterpart of ``_trace_terminal_output``: record the
    reply it returns (the last assistant message's content) as the trace output. Its
    ``@delegate`` and slash-command short-circuits, HITL parks and error bubbles return
    without a tool-call-free model reply, so without this their ``chat`` traces had
    input and no output (#3695). Called inside the ``trace_session`` scope.
    """
    if not isinstance(reply, list):
        return
    for msg in reversed(reply):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = msg.get("content")
            _set_trace_output(content if isinstance(content, str) else str(content or ""))
            return


def _set_trace_output(text: str) -> None:
    """Record ``text`` as the active turn's trace output: redacted, then capped."""
    if not text:
        return
    from observability import tracing

    try:
        # Redact the WHOLE text, then cap: a secret cut in half no longer matches its
        # pattern, and an exact-match secret (a manager-sourced PEM key, say) can be longer
        # than any fixed headroom. Once per turn, so the full pass is affordable.
        tracing.set_session_output(_redact(text)[: tracing.MAX_IO_CHARS])
    except Exception:  # noqa: BLE001 — tracing never alters the turn
        log.debug("[tracing] turn output not recorded", exc_info=True)


# ── Shared pre-turn dispatch + failure handling (#3805) ─────────────────────
# Both turn drivers — the streaming ``_chat_langgraph_stream_impl`` (A2A / console)
# and the non-streaming ``_chat_langgraph_impl`` (``chat()``: OpenAI-compat /v1,
# /api/chat, plugin surfaces) — used to carry their own copy of this chain and of
# the error handling. The copies drifted: the non-streaming one never learned
# `/subagent` or the context-overflow compact-and-retry. ONE chain now, and one
# failure classifier; each driver only decides how to SHAPE what it yields.


@dataclass
class _PreTurn:
    """Mutable outcome of :func:`_pre_turn_dispatch` (an async generator can't
    return a value, so the caller reads this after draining it).

    ``message`` — the text the turn should run on (a `/skill` rewrites it).
    ``handled`` — a short-circuit answered the turn; its terminal frame
    (``done`` / ``input_required``) was the last one yielded.
    ``acp`` — not handled, and the configured runtime is ACP (ADR 0033): the
    driver runs its own ACP shape instead of the native loop.
    ``fenced`` — the turn carries a ``tool_fence`` (#2972): no short-circuit runs.
    """

    message: str
    handled: bool = False
    acp: bool = False
    fenced: bool = False


async def _pre_turn_dispatch(pre: _PreTurn, session_id: str, request_metadata: dict | None):
    """The pre-turn dispatch chain, in its one canonical order: @-mention → /goal →
    /lifecycle → plugin command → workflow → subagent → skill (rewrite, falls
    through) → unknown /command → ACP switch.

    Yields the same ``(kind, payload)`` frames the streaming driver emits (work
    cards, room replies, then a terminal ``done`` / ``input_required``); the
    non-streaming driver drains it and keeps only the terminal frame. Exceptions
    propagate to the driver's turn-level handler.
    """
    message = pre.message
    # A FENCED turn (#2972 — an untrusted party's message relayed by a plugin
    # surface) runs none of the short-circuits below: each one does work outside
    # the lead turn — a subagent (`/self-improve` can edit the SOUL), a workflow, a
    # plugin command, a delegate exchange, a goal change — where the fence, which
    # only SubagentFenceMiddleware enforces on the lead turn, can't reach it. The
    # text goes to the fenced lead turn verbatim instead.
    if pre.fenced:
        from runtime.acp_runtime import is_acp_runtime

        pre.acp = bool(is_acp_runtime(STATE.graph_config))
        return
    # STEP 0 — @-delegate dispatch (S1): a message opening with `@<delegate>`
    # routes straight to that delegate, short-circuiting the LLM turn. Checked
    # BEFORE goal control (and every slash-command below) so an @-mention is
    # never swallowed by an active goal; a no-op when the delegates plugin isn't
    # loaded (no registry on STATE ⇒ `@` is ordinary text). See _at_delegate_reply.
    # The exchange WRITES this session's checkpointer thread, so it takes the
    # same per-thread lock every other writer takes (see the turn lock below,
    # and compact/rewind). Without it a mention landing while a goal
    # continuation or a scheduled fire writes the same thread lost-updates the
    # transcript — the exact corruption that lock exists to prevent.
    # A direct address deliberately skips the graph, so there are no model or
    # tool events to reassure the operator while a slow delegate works (#3052).
    # Open one ordinary work card before entering the (potentially queued)
    # exchange. The console's existing elapsed timer then keeps ticking even
    # when the adapter has no native progress stream. Unknown / bare mentions
    # answer synchronously and do not need a card.
    _addressed = _parse_at_delegates(message)
    _mention_tool: dict | None = None
    if _addressed is not None and _addressed[1]:
        _mention_names = " ".join(f"@{name}" for name in _addressed[0])
        _mention_tool = {
            "id": f"mention:{','.join(_addressed[0])}",
            "name": _mention_names,
            "input": _addressed[1],
        }
        yield ("tool_start", _mention_tool)

    try:
        async with _thread_lock(_resolve_thread_id(request_metadata, session_id)):
            _at_reply, _at_outcome = await _chat_rooms._at_delegate_exchange(
                message, session_id, request_metadata
            )
    except Exception as exc:
        # Most adapter failures are ordinary room outcomes, but an unexpected
        # exchange failure still flows to the turn-level error handler below.
        # Settle the card first so the console cannot strand it as running.
        if _mention_tool is not None:
            yield (
                "tool_end",
                {
                    "id": _mention_tool["id"],
                    "name": _mention_tool["name"],
                    "output": str(exc) or type(exc).__name__,
                    "error": True,
                },
            )
        raise
    if _mention_tool is not None:
        _failed = sum(not bool(item.get("ok")) for item in (_at_outcome or []))
        # Distinct participants, not dispatches: over three rounds two delegates
        # produce six outcomes, and "6 replied over 3 rounds" describes a room of
        # six people that does not exist.
        _answered = len(
            {
                str(item.get("author") or "")
                for item in (_at_outcome or [])
                if item.get("ok") and not item.get("silent")
            }
        )
        _rounds = max((int(item.get("round") or 1) for item in (_at_outcome or [])), default=1)
        if _at_outcome:
            _status = f"{_answered} replied"
            if _failed:
                _status += f", {_failed} failed"
            if _rounds > 1:
                # A multi-round room is several passes over the same cast; the
                # card is the only place the operator learns it took more than
                # one, since a settle is deliberately quiet in the reply text.
                _status += f" over {_rounds} rounds"
        else:
            # Every stopped local target can fall through to the lead's normal
            # consent/start path (#3126); the addressed wait itself still ended.
            _status = "Handed to lead" if _at_reply is None else "Finished"
        yield (
            "tool_end",
            {
                "id": _mention_tool["id"],
                "name": _mention_tool["name"],
                "output": _status,
                "error": bool(_failed and not _answered),
            },
        )
    if _at_reply is not None:
        for _exchange in _at_outcome or []:
            if _exchange.get("silent"):
                continue  # a `pass` is not a message — no thread record, no frame
            # One authorship frame per exchange: the answer is that participant's
            # own words, not the lead agent's, and a multi-mention turn is several
            # participants answering. `text` rides along so a console that renders
            # per-exchange messages has the words with the byline; consumers that
            # don't know this kind ignore it (the executor's if/elif has no else)
            # and still get the whole answer on the `done` frame.
            #
            # `in_answer` says that the `done` text restates THIS reply, so a
            # console rendering the bubble must not render the answer too
            # (#3449). Set only where the composer claimed it — see the tail of
            # `_at_delegate_exchange` for what disqualifies a turn — and omitted
            # rather than sent false, so the key's presence is the claim and
            # every other `room_reply` producer (a `delegate_to` exchange, a
            # drained background reply) stays untouched: those replies are NOT
            # in the lead's answer, which is its own synthesis.
            yield (
                "room_reply",
                {
                    "author": _exchange.get("author") or "",
                    "from": "operator",
                    "text": str(_exchange.get("reply") or ""),
                    "ok": bool(_exchange.get("ok")),
                    "catchup": int(_exchange.get("catchup") or 0),
                    "truncated": bool(_exchange.get("truncated")),
                    **({"in_answer": True} if _exchange.get("in_answer") else {}),
                },
            )
        # The part of the answer NO participant's bubble carries (#3449) — a
        # failed address's line, an empty reply's stand-in, the room's own bound
        # notes. Its own frame, so a console that renders the bubbles can render
        # the whole answer exactly once instead of either doubling the replies or
        # dropping this. Last, because it is a footnote on what was just said, and
        # only when the composer claimed something (see `_at_delegate_exchange`:
        # with nothing claimed the console lands the answer whole, and this frame
        # would be the duplicate).
        _room_note_text = next(
            (str(o.get("room_note") or "") for o in (_at_outcome or []) if o.get("room_note")), ""
        )
        if _room_note_text:
            yield ("room_reply", {"note": True, "from": "room", "text": _room_note_text, "ok": True})
        pre.handled = True
        yield ("done", _at_reply)
        return

    # Goal control messages (/goal ...) short-circuit the turn: set /
    # status / clear a goal and return the reply without running the graph.
    if STATE.goal_controller is not None:
        reply = await STATE.goal_controller.parse_control(message, session_id, trusted=False)
        if reply is not None:
            gs = STATE.goal_controller.active_goal(session_id)
            if STATE.goal_controller.is_set_ack(reply) and gs is not None:
                # /goal SET kicks the drive immediately (#1910): surface the ack as a
                # status frame, then fall through into a goal-driven turn instead of
                # short-circuiting here and waiting for a separate inbound message. The
                # goal condition is injected at the kickoff below (iteration 0), which
                # also covers a plain message arriving on an already-active goal.
                yield ("tool_start", f"🎯 {reply}")
            else:
                pre.handled = True
                yield ("done", reply)
                return

    # Core /lifecycle command (ADR 0074) — read-only listing of the lifecycle
    # events + configured reactions + registered hooks. Reserved like /goal.
    lc_reply = _lifecycle_command_reply(message)
    if lc_reply is not None:
        pre.handled = True
        yield ("done", lc_reply)
        return

    # Plugin-registered chat control command (/<name> …) short-circuits the
    # turn with the plugin's reply — user-only, like /goal (e.g. the github
    # plugin's /issue). No plugin claims a token by default ⇒ falls through.
    name, rest = _parse_slash_command(message)
    if name:
        cmd_reply = await _run_plugin_chat_command(name, rest, session_id)
        if isinstance(cmd_reply, _PluginFormRequest):
            # A plugin form rides the SAME input_required frame the agent HITL
            # uses (ADR 0045 — one canonical wire), tagged with a callback id so
            # the console routes the answers to the plugin's on_submit instead of
            # resuming a graph interrupt (there is none) — #1701 Slice 2.
            pre.handled = True
            yield ("input_required", {**cmd_reply.form, "plugin_callback_id": cmd_reply.callback_id})
            return
        if cmd_reply is not None:
            pre.handled = True
            yield ("done", cmd_reply)
            return

    # Workflow slash command (/<workflow-name> …) short-circuits the turn:
    # run the recipe and return its output. Each step renders its own
    # tool card (gather → angles → brief) so a multi-step workflow shows
    # live progress instead of one opaque card that looks hung.
    parsed = _parse_workflow_command(message)
    if parsed is not None:
        wf_name, wf_inputs = parsed
        _WF_DONE = object()
        step_q: asyncio.Queue = asyncio.Queue()

        async def _on_step(event: dict) -> None:
            await step_q.put(event)

        async def _runner() -> str:
            try:
                return await _run_parsed_workflow(wf_name, wf_inputs, on_step=_on_step)
            finally:
                await step_q.put(_WF_DONE)

        runner = asyncio.create_task(_runner())
        # An umbrella card for the whole workflow, then one per step.
        yield (
            "tool_start",
            {
                "id": f"workflow:{wf_name}",
                "name": f"workflow:{wf_name}",
                "input": _coerce_tool_value(wf_inputs),
            },
        )
        while True:
            event = await step_q.get()
            if event is _WF_DONE:
                break
            sid = event.get("step_id", "")
            step_tool_id = f"workflow:{wf_name}:{sid}"
            label = f"{wf_name} · {sid}"
            if event.get("phase") == "start":
                yield ("tool_start", {"id": step_tool_id, "name": label, "input": event.get("subagent", "")})
            else:
                yield (
                    "tool_end",
                    {
                        "id": step_tool_id,
                        "name": label,
                        "output": extract_output(event.get("output", "")) or event.get("output", ""),
                    },
                )
        wf_out = await runner
        yield ("tool_end", {"id": f"workflow:{wf_name}", "name": f"workflow:{wf_name}", "output": wf_out[:300]})
        pre.handled = True
        yield ("done", wf_out)
        return

    # Subagent slash command (/<subagent> <prompt>) short-circuits the
    # turn: run the one worker and return its output (ADR 0020 — run from
    # chat). Renders a single tool card. A workflow of the same name wins.
    parsed_sub = _parse_subagent_command(message)
    if parsed_sub is not None:
        sub_type, sub_prompt = parsed_sub
        if not sub_prompt:
            pre.handled = True
            yield ("done", f"Usage: `/{sub_type} <prompt>` — describe the task for the {sub_type} subagent.")
            return
        sub_tool_id = f"subagent:{sub_type}"
        yield ("tool_start", {"id": sub_tool_id, "name": sub_tool_id, "input": sub_prompt})
        sub_out = await _run_parsed_subagent(sub_type, sub_prompt, session_id=session_id)
        yield ("tool_end", {"id": sub_tool_id, "name": sub_tool_id, "output": sub_out[:300]})
        pre.handled = True
        yield ("done", sub_out)
        return

    # User-facing skill slash command (/<skill> [args]) — does NOT
    # short-circuit: rewrite the message to inject the skill's procedure
    # as a directive, then fall through to the normal lead-agent turn so
    # every streaming / HITL / goal invariant holds (ADR 0052).
    parsed_skill = _parse_skill_command(message)
    if parsed_skill is not None:
        message = _skill_directive(*parsed_skill)

    # Unknown /command (#2893) — the message looks like a slash command but
    # matched nothing above: short-circuit with a hint instead of handing the
    # raw `/foobar` text to the agent turn. Non-command uses of `/` (paths,
    # prose) fall through — see _unknown_slash_command_reply.
    unknown_reply = _unknown_slash_command_reply(message)
    if unknown_reply is not None:
        pre.handled = True
        yield ("done", unknown_reply)
        return

    pre.message = message

    # ACP runtime (ADR 0033 slice 4) — when `agent_runtime: acp:<agent>`, an
    # external coding agent (proto/codex/claude/…) drives the turn over ACP
    # instead of the native LangGraph loop. The decision is shared; each driver
    # runs it in its own shape (both through `_acp_drive_turn`).
    from runtime.acp_runtime import is_acp_runtime

    pre.acp = bool(is_acp_runtime(STATE.graph_config))


def _short_circuit_reply(frame: tuple | None) -> list[dict[str, Any]]:
    """The non-streaming shape of a pre-turn short-circuit's terminal frame."""
    kind, payload = frame if frame is not None else ("done", "")
    if kind == "input_required":
        # Non-streaming callers (e.g. the OpenAI-compat /v1 path) can't render a
        # plugin form — degrade to a text note pointing at the console (#1701 S2).
        _title = (payload or {}).get("title") or "This command"
        return [{"role": "assistant", "content": f"**{_title}** needs a form — open it in the protoAgent console."}]
    return [{"role": "assistant", "content": payload}]


# The retry prompt + status line for the context-overflow recovery (#2783, ADR 0101 D4).
_OVERFLOW_RETRY_PROMPT = (
    "(The previous request overflowed the context window and the earlier "
    "history was compacted to a summary. Continue exactly where you left off.)"
)
_OVERFLOW_NOTICE = (
    "🧹 The request overflowed the model's context window — older history was force-compacted; retrying once."
)
_PROVIDER_CLOSED_MSG = "The model provider closed the stream (possibly rate-limited). Please retry."


def _is_provider_stream_drop(exc: BaseException) -> bool:
    """The provider dropped the stream and the per-call reconnects
    (graph.llm._astream) were exhausted, or content had already streamed — a clean
    terminal outcome (most likely rate limiting), not a code bug (#1728)."""
    from graph.llm import RETRYABLE_STREAM_ERRORS

    return isinstance(exc, RETRYABLE_STREAM_ERRORS)


async def _overflow_compacted(exc: BaseException, thread_id: str | None, session_id: str) -> bool:
    """True when ``exc`` is a context-window overflow from a NATIVE turn on
    ``thread_id`` and a forced compaction actually shrank that thread — i.e. the
    caller should retry the turn once (#2783, ADR 0101 D4).

    Before this, nothing caught the overflow class: the raw error surfaced,
    ModelFallback re-sent the same oversized prompt elsewhere, and the next turn hit
    the same wall. ``thread_id`` is None when the failure came from before the native
    turn (a pre-turn short-circuit, or the ACP runtime) — compacting there would
    rewrite a thread the failing request never used, so nothing is retried. The
    turn's lock has already unwound, so the compact and the retry re-acquire it.
    """
    if thread_id is None or _is_provider_stream_drop(exc):
        return False
    from graph.llm import is_context_overflow_error

    return bool(is_context_overflow_error(exc)) and await _force_compact_for_overflow(thread_id, session_id)


async def _force_compact_for_overflow(thread_id: str, session_id: str) -> bool:
    """Emergency thread shrink after a context-window overflow (#2783, ADR 0101 D4).

    Runs ``compact_thread`` in safety-valve mode (``force=True`` — archive
    best-effort, stub summary on summarizer failure; see compaction_op) under
    the per-thread lock. Returns whether the thread actually shrank — a refusal
    (e.g. the thread is already tiny, so overflow must have another cause)
    means retrying would hit the same wall, and the caller surfaces the
    original error instead.
    """
    if STATE.graph is None or STATE.checkpointer is None:
        return False
    try:
        from graph.compaction_op import compact_thread

        async with _thread_lock(thread_id):
            result = await compact_thread(
                STATE.graph,
                STATE.checkpointer,
                STATE.knowledge_store,
                STATE.graph_config,
                thread_id,
                session_id,
                force=True,
                # Tighter than the configured keep: the window is ALREADY blown, so
                # the retry needs real headroom, not a gentle trim.
                keep_recent=min(10, int(getattr(STATE.graph_config, "compaction_keep_messages", 20) or 20)),
            )
        # `too_short` is a benign no-op (refused=False, removed=0) — but for THIS
        # caller a thread that didn't shrink means the retry hits the same wall,
        # so recovery requires actual removal, not merely non-refusal.
        ok = not result.get("refused") and int(result.get("removed") or 0) > 0
        if ok:
            log.warning(
                "[a2a-stream] overflow recovery compacted thread %s: removed %s message(s), archived=%s",
                thread_id,
                result.get("removed"),
                result.get("archived"),
            )
            try:
                from observability import metrics

                metrics.record_overflow_recovery()
            except Exception:  # noqa: BLE001 — telemetry must never break recovery
                pass
        else:
            log.warning(
                "[a2a-stream] overflow recovery could not shrink thread %s (%s) — surfacing the original error",
                thread_id,
                result.get("reason"),
            )
        return ok
    except Exception:  # noqa: BLE001 — recovery must never mask the original error
        log.exception("[a2a-stream] overflow recovery itself failed for thread %s", thread_id)
        return False


async def _fail_turn(exc: BaseException, session_id: str, *, tag: str) -> str:
    """Log, record (#2593) and describe a failed turn — ONE classifier for both drivers,
    so the two surfaces leave the SAME transcript and the same log shape. Returns the
    user-facing message (the streaming driver's ``error`` payload; the non-streaming
    driver wraps it as ``**Error:** …``)."""
    if _is_provider_stream_drop(exc):
        log.warning(
            "[%s] provider closed the stream for session=%s (%s: %s) — possible rate limit; failing the turn cleanly",
            tag,
            session_id,
            type(exc).__name__,
            exc,
        )
        msg = _PROVIDER_CLOSED_MSG
    else:
        log.error("[%s] unhandled exception for session=%s: %s", tag, session_id, exc, exc_info=exc)
        msg = str(exc)
    await record_failed_turn(session_id, f"**Error:** {msg}")
    return msg


async def _chat_langgraph_stream_impl(
    message: str,
    session_id: str,
    *,
    caller_trace: dict | None = None,
    resume: bool = False,
    request_metadata: dict | None = None,
    images: list[tuple[str, str]] | None = None,
):
    """Async generator — yields (event_type, payload) tuples from the
    LangGraph run. Consumed by ``executor.ProtoAgentExecutor`` to
    drive the SDK task lifecycle + SSE streaming.

    Event contract (matches what the A2A handler expects):

    - ``tool_start`` / ``tool_end`` — status frames w/ tool name + preview
    - ``usage`` — per-LLM-call token usage for the cost-v1 DataPart
    - ``done`` — terminal; payload is the final user-facing text
    - ``error`` — terminal; payload is the error string

    ``caller_trace`` is the ``a2a.trace`` metadata from the incoming
    A2A message. When present, Langfuse stamps ``caller_trace_id`` +
    ``caller_span_id`` so operators can cross-reference this trace to
    the dispatching agent's trace in the same project.

    ``request_metadata`` is the merged A2A request metadata; it's handed to
    the pluggable ``thread_id`` resolver (#571) so a fork can scope memory off
    it (e.g. per-project working memory) without editing this file.
    """
    from observability import tracing

    from graph.middleware.request_context import request_metadata_scope

    from graph.config_io import soul_revision

    # Incognito (ADR 0069 D3b): the trace keeps its structure, usage and cost, but no
    # content — not the preview, the input, or any generation's messages.
    _trace_incognito = bool((request_metadata or {}).get("incognito"))
    trace_meta: dict = {"soul_rev": soul_revision()}
    if not _trace_incognito:
        trace_meta["message_preview"] = _redact(message[:100])
    if caller_trace:
        if caller_trace.get("traceId"):
            trace_meta["caller_trace_id"] = caller_trace["traceId"]
        if caller_trace.get("spanId"):
            trace_meta["caller_span_id"] = caller_trace["spanId"]

    if STATE.graph is None:
        auth_err = getattr(STATE, "graph_auth_error", None)
        if auth_err:
            # Signed-out (#2458), not setup-pending — say so instead of pointing at a
            # wizard the user already finished.
            yield ("error", auth_err.get("message") or "provider disconnected — reconnect from the console")
        else:
            yield ("error", "setup required — finish the setup wizard before calling A2A endpoints")
        return

    async with (
        tracing.trace_session(
            session_id=session_id,
            name="a2a-stream",
            metadata=trace_meta,
            input=_redact(message),
            incognito=_trace_incognito,
        ),
        request_metadata_scope(request_metadata),
    ):
        # Set only once the NATIVE turn is about to run: the overflow recovery in the
        # handler below compacts + retries that thread, and must not fire for a failure
        # from the pre-turn chain or the ACP runtime (which never used it).
        _tid: str | None = None
        try:
            # The pre-turn dispatch chain (@-mention, /goal, /lifecycle, plugin command,
            # workflow, subagent, skill, unknown /command, ACP switch) is SHARED with
            # the non-streaming driver (#3805) — see _pre_turn_dispatch. Its frames
            # (work cards, room replies, the terminal `done`) stream straight through.
            pre = _PreTurn(message)
            async with contextlib.aclosing(_pre_turn_dispatch(pre, session_id, request_metadata)) as _pre_frames:
                async for frame in _pre_frames:
                    yield frame
            if pre.handled:
                return
            message = pre.message

            if pre.acp:
                _acp_tid = _resolve_thread_id(request_metadata, session_id)
                # Hold the runtime "in-flight" for the whole turn so a concurrent turn's
                # eviction can't close it mid-stream (a long ACP coding turn can outlast the
                # idle TTL); registry mutation is serialized by _ACP_LOCK. See _acp_acquire.
                rt = await _chat_acp._acp_acquire(_acp_tid)
                try:
                    # One prompt at a time per ACP session — AcpClient forbids concurrent
                    # prompts on an instance (a session is a single conversation), and the
                    # refcount above only guards eviction. Same per-thread lock the native
                    # turns hold, so compact/rewind on this thread are excluded too.
                    async with _thread_lock(_acp_tid):
                        async for frame in _chat_acp._acp_drive_turn(rt, message):
                            yield frame
                finally:
                    await _chat_acp._acp_release(_acp_tid)
                return

            # thread_id keys this session's history in the checkpointer (bound
            # at compile time in create_agent_graph). The prefix isolates A2A
            # sessions from the non-streaming chat in the shared MemorySaver. Derivation is
            # a pluggable seam (#571): a fork registers a resolver to scope memory
            # off request metadata (e.g. per-project) without editing this file.
            _tid = _resolve_thread_id(request_metadata, session_id)
            config = {
                "configurable": {"thread_id": _tid},
                "recursion_limit": getattr(STATE.graph_config, "max_iterations", 200),
            }

            # Serialize turns on the SAME thread_id: two near-simultaneous A2A
            # message/send on one context_id would otherwise run concurrent graph turns
            # against the shared checkpointer thread → lost-update history corruption. A
            # per-thread async lock runs them one-at-a-time (mirrors the console steering
            # queue; different contexts never block each other). The turn body lives in
            # _run_native_turn so the lock wraps it without a deep in-line reindent.
            async with _thread_lock(_tid):
                # HITL hold (#1560): while this thread is parked at a form/question/
                # approval interrupt, a fresh operator message is HELD in the steering
                # queue (it folds in right after the form response) and the turn re-parks
                # on the same payload; the marked form answer converts to a real resume.
                # No pending interrupt ⇒ hold is None and nothing changes.
                if not resume:
                    hold = await _hold_if_hitl_pending(
                        message, session_id, config, request_metadata=request_metadata
                    )
                    if hold is _HITL_RESUME:
                        resume = True
                    elif hold is not None:
                        yield ("input_required", _interrupt_payload(hold))
                        return
                # ADR 0115 D6 (#3760): a live operator turn (empty origin) runs under
                # `interactive`; inbound `a2a` and the server-fired autonomous origins stay
                # `default`. The class is set for the whole native turn — both loops inside
                # _run_native_turn — so its subagent tasks inherit it.
                with _interactive_turn_priority((request_metadata or {}).get("origin")):
                    async for frame in _run_native_turn(
                        message, session_id, config, request_metadata=request_metadata, resume=resume, images=images
                    ):
                        yield frame

        except GeneratorExit:
            # Expected: A2A consumers break out of the SSE loop after
            # capturing the initial task event,
            # then hand off to TaskTracker for polling. Re-raise so Python
            # finalizes the generator cleanly; the OTel cross-context detach
            # noise this used to emit is silenced at the logger level in
            # tracing.py.
            raise
        except Exception as e:
            # Context overflow (#2783, ADR 0101 D4): force-compact the thread once and
            # retry a single time. A second failure falls through to the honest error
            # below, but the thread is smaller, so the NEXT turn no longer inherits the
            # wall. Same classifier as the non-streaming driver (#3805).
            if await _overflow_compacted(e, _tid, session_id):
                yield ("tool_start", _OVERFLOW_NOTICE)
                try:
                    async with _thread_lock(_tid):
                        # Same class as the initial turn (ADR 0115 D6) — the retry is the
                        # same operator/A2A turn, just after a force-compact.
                        with _interactive_turn_priority((request_metadata or {}).get("origin")):
                            async for frame in _run_native_turn(
                                _OVERFLOW_RETRY_PROMPT,
                                session_id,
                                config,
                                request_metadata=request_metadata,
                                resume=False,
                                images=None,
                            ):
                                yield frame
                    return
                except Exception as retry_exc:  # noqa: BLE001 — second failure surfaces honestly
                    log.exception("[a2a-stream] overflow retry failed for session=%s: %s", session_id, retry_exc)
                    e = retry_exc
            # Same record as the non-streaming path: the two surfaces must leave the
            # SAME transcript, or an exported thread depends on which one ran (#2593).
            yield ("error", await _fail_turn(e, session_id, tag="a2a-stream"))
        finally:
            tracing.flush()


# OpenAI-shaped `error.type` per upstream HTTP status. Anything unmapped — including
# 5xx and "no status at all" (a bug in our own code) — is a server_error.
_ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "authentication_error",
    404: "invalid_request_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
}


def _upstream_status(exc: BaseException) -> int | None:
    """The HTTP status an upstream provider returned, if the exception carries one.

    Covers the openai SDK (``status_code``), older/alternate clients (``http_status``),
    and anything wrapping an httpx/requests response.
    """
    for attr in ("status_code", "http_status"):
        code = getattr(exc, attr, None)
        if isinstance(code, int) and 400 <= code < 600:
            return code
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return code if isinstance(code, int) and 400 <= code < 600 else None


async def record_failed_turn(session_id: str, text: str) -> bool:
    """Append a failed turn's error to its checkpointed thread. Returns True if recorded.

    A turn that raises used to leave the checkpoint holding only the user's message: the
    graph saved the human turn, the failure was returned to the caller, and nothing wrote
    it back. An export of that session read ``message_count: 1`` — the operator's
    instruction present, no answer, no record that one was even attempted — and the NEXT
    turn on the same thread had no idea the earlier one never ran, so an instruction could
    evaporate silently and only resurface hours later when the session was reused (#2593).

    Recording it makes the transcript honest and gives the following turn the context. The
    message is a normal ``AIMessage`` so history, ``/export`` and the model all see it,
    tagged in ``additional_kwargs`` so a surface that wants to render it as an error entry
    rather than an answer can tell the difference.

    Best-effort by construction: this runs ON the failure path, so it must never raise and
    mask the error it exists to describe.
    """
    graph = STATE.graph
    if graph is None or not text.strip():
        return False
    try:
        from langchain_core.messages import AIMessage  # lazy, like every other use here

        await graph.aupdate_state(
            {"configurable": {"thread_id": _resolve_thread_id(None, session_id)}},
            {"messages": [AIMessage(content=text, additional_kwargs={"protoagent_turn_failed": True})]},
        )
        return True
    except Exception:  # noqa: BLE001 — never let bookkeeping bury the real failure
        log.warning("[chat] could not record the failed turn for session=%s", session_id, exc_info=True)
        return False


def turn_error(exc: BaseException, message: str | None = None) -> dict[str, Any]:
    """Machine-readable companion to the ``**Error:** …`` bubble a failed turn returns.

    A turn that raises is reported as assistant *content*, which is right for a chat UI
    and wrong for ``/v1/chat/completions``: that endpoint answered HTTP 200 with
    ``finish_reason: "stop"`` and an upstream 401 as the assistant's "answer", so every
    OpenAI SDK client read a hard auth failure as a successful completion (#2578).

    Carrying the failure structurally lets each surface decide. The console keeps
    rendering the bubble (it ignores the extra key, like ``usage``); ``/v1`` maps this to
    a real HTTP error. The content string is unchanged, so nothing that reads it moves.
    """
    return {
        "message": message or str(exc),
        "type": _ERROR_TYPE_BY_STATUS.get(_upstream_status(exc), "server_error"),
        "upstream_status": _upstream_status(exc),
        "exception": type(exc).__name__,
    }


async def _chat_langgraph(
    message: str,
    session_id: str,
    *,
    model: str | None = None,
    incognito: bool = False,
    hitl_resume: bool = False,
    images: list[tuple[str, str]] | None = None,
    tool_fence: list[str] | None = None,
    origin: str = "local",
) -> list[dict[str, Any]]:
    """Idle-beacon wrapper (#1720): mark a turn in flight for the call's lifetime,
    then delegate. Keeps the public name/signature for every caller.

    Also the telemetry seam for this driver (#3000). The non-streaming path used
    to record nothing at all — no store row, no Prometheus sample — so every turn
    from ``/v1/chat/completions``, ``/api/chat``, and the plugin ``HOST.invoke()``
    seam was invisible to the cost, latency, and success-rate surfaces that claim
    to describe the agent. The row is written HERE rather than inside the impl
    because the impl has a dozen return points; the wrapper has exactly one exit.
    """
    _turn_started(session_id)
    _note_agent_active(session_id)  # ADR 0074 — idle→active lifecycle event (debounced)
    started = time.monotonic()
    sink: dict[str, Any] = {}
    result: list[dict[str, Any]] = []
    state = "failed"
    try:
        # ADR 0115 D6 (#3760): a console / operator turn runs under `interactive`; the OpenAI-
        # compat (`v1`) and plugin surfaces stay `default`. Scoped to the turn here (the impl
        # has a dozen return points, this wrapper has one) so the subagent tasks it spawns
        # inherit the class and it resets when the awaited turn returns.
        with _interactive_turn_priority(origin):
            result = await _chat_langgraph_impl(
                message,
                session_id,
                model=model,
                incognito=incognito,
                hitl_resume=hitl_resume,
                images=images,
                tool_fence=tool_fence,
                _telemetry_sink=sink,
            )
        # The impl catches its own exceptions and reports them as an assistant
        # bubble carrying a structured `error` (server.chat.turn_error), so the
        # error key — not an exception — is what distinguishes a failed turn.
        state = "failed" if any(isinstance(m, dict) and m.get("error") for m in result) else "completed"
        return result
    finally:
        _turn_ended(session_id)
        _turn_telemetry.record_local_turn(sink, session_id=session_id, origin=origin, state=state, started=started)


async def _chat_langgraph_impl(
    message: str,
    session_id: str,
    *,
    model: str | None = None,
    incognito: bool = False,
    hitl_resume: bool = False,
    images: list[tuple[str, str]] | None = None,
    tool_fence: list[str] | None = None,
    _telemetry_sink: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Non-streaming LangGraph entry — used by the console + OpenAI-compat.

    ``_telemetry_sink`` (private, set by the ``_chat_langgraph`` wrapper) receives
    this turn's usage callback so the wrapper can write the telemetry row from its
    single exit point rather than at each of this function's many returns (#3000).
    """
    from observability import tracing
    from langchain_core.messages import HumanMessage, AIMessage

    from graph.goals.goal_turn import goal_turn

    # Per-turn model override (ModelOverrideMiddleware reads state["model"]).
    # Incognito is stamped explicitly every turn (the channel persists in the
    # checkpointer — an omitted key would inherit the previous turn's value).
    _state_extra = {"model": model} if (model or "").strip() else {}
    _state_extra["incognito"] = bool(incognito)
    # The tool fence (#2972) is stamped every turn for the same reason: a fenced
    # turn on a session must not leave the NEXT (unfenced) turn on that session
    # fenced — and an unfenced turn must not inherit a fence. Empty list = no
    # fence (SubagentFenceMiddleware treats falsy as "untouched").
    _state_extra["subagent_fence"] = [str(t) for t in tool_fence] if tool_fence else []

    from graph.config_io import soul_revision

    def _traced(reply):
        # Every return inside the trace scope goes through here, so the trace's output
        # is what the caller got on every path — not only a final model reply (#3695).
        _trace_reply_output(reply)
        return reply

    async with tracing.trace_session(
        session_id=session_id,
        name="chat",
        metadata={"soul_rev": soul_revision(), **({} if incognito else {"message_preview": _redact(message[:100])})},
        input=_redact(message),
        incognito=bool(incognito),
    ):
        # Set only once the NATIVE turn is about to run — the overflow recovery below
        # compacts + retries that thread (same contract as the streaming driver, #3805).
        native_tid: str | None = None
        try:
            # The pre-turn dispatch chain is SHARED with the streaming driver (#3805) —
            # see _pre_turn_dispatch. This surface can't render the intermediate frames
            # (work cards, room replies, a /goal SET ack — that one is folded into the
            # turn's terminal goal note), so only the terminal frame becomes the reply.
            # No request_metadata on this driver — the thread resolves from the session
            # id alone, as it does everywhere else in this function.
            pre = _PreTurn(message, fenced=bool(tool_fence))
            last_frame: tuple | None = None
            async with contextlib.aclosing(_pre_turn_dispatch(pre, session_id, None)) as _pre_frames:
                async for frame in _pre_frames:
                    last_frame = frame
            if pre.handled:
                return _traced(_short_circuit_reply(last_frame))
            message = pre.message

            # Non-native runtime (ADR 0033) — the switch itself is shared (see
            # _pre_turn_dispatch). Without it, an acp:* config silently ran this surface
            # (OpenAI-compat /v1, the desktop /api/chat fallback, internal self-prompts)
            # on the native loop — which a gateway-less ACP-only setup (e.g. the Hermes
            # preset) can't serve at all.
            if pre.acp:
                return _traced(await _chat_acp._acp_turn_collected(session_id, message))

            # Same thread-id resolution as the streaming path (ADR 0069 D4): the
            # non-streaming turns used to key `chat:{session_id}` apart from the
            # streaming `a2a:{session_id}` ones, so the SAME session reached via
            # both APIs split into two histories. Old `chat:*` checkpoints orphan
            # once on upgrade — non-streaming chat is short-lived, so harmless.
            # Aggregate token usage across every model call this turn — the initial invoke,
            # each goal continuation, and any nested subagents (LangChain propagates config
            # callbacks into nested ainvokes). Read back at the end and attached to the
            # returned assistant dict; /api/chat ignores the extra key, the /v1 OpenAI-compat
            # handler reads it for `usage` (ADR 0075 D4). Mirrors the streaming path's
            # per-call usage accounting, which sums `on_chat_model_end` events for the turn.
            usage_cb = _turn_telemetry.make_usage_callback()
            if _telemetry_sink is not None:
                # Handed over as soon as it exists, not at the end: the wrapper reads
                # it from a `finally`, so a turn that raises still bills what it spent
                # before it died.
                _telemetry_sink["usage_cb"] = usage_cb
            config = {
                "configurable": {"thread_id": _resolve_thread_id(None, session_id)},
                "callbacks": [usage_cb],
                "recursion_limit": getattr(STATE.graph_config, "max_iterations", 200),
            }

            def _last_ai(result) -> str:
                # Bounded to THIS turn (#2300). An unbounded reverse scan over the
                # accumulated conversation returns the PREVIOUS turn's answer whenever
                # this one produced no assistant message — which is exactly what a turn
                # whose stream dies after its first chunk looks like.
                for msg in reversed(this_turn_messages(result)):
                    if isinstance(msg, AIMessage) and msg.content:
                        # `.text` flattens Responses-API content blocks (openai-codex,
                        # ADR 0097) to a string; a plain string passes through unchanged.
                        return msg.content if isinstance(msg.content, str) else msg.text
                return ""

            async def _native_turn(
                turn_message: str,
                turn_images: list[tuple[str, str]] | None,
                *,
                overflow_retry: bool = False,
            ) -> list[dict[str, Any]]:
                """One native turn on this session's thread, as the reply list. Run once
                for the operator's message and, after a context-overflow compaction, once
                more for the recovery prompt (#3805)."""
                # When a goal is already active, the whole turn is goal-driven —
                # suppress cross-session prior_sessions on the initial turn too.
                _goal_state = (
                    STATE.goal_controller.active_goal(session_id) if STATE.goal_controller is not None else None
                )
                goal_active = _goal_state is not None
                # Sharing the streaming thread means sharing its serialization contract:
                # every other writer to `a2a:{sid}` (the streaming turn driver,
                # compact_session, rewind_session) holds the per-thread lock — an
                # unlocked graph turn here could lost-update a concurrent one (e.g. the
                # desktop /api/chat fallback racing a console /compact on the same tab).
                async with _thread_lock(config["configurable"]["thread_id"]):
                    # HITL hold (#1560) — same contract as the streaming path: while the
                    # thread is parked at a form/question/approval interrupt, hold a fresh
                    # operator message (it folds in right after the form response) and echo
                    # the pending ask; the marked answer resumes the graph properly.
                    # The overflow retry skips it, as the streaming retry does: its message
                    # is the recovery prompt, not an operator message to hold.
                    hold = (
                        None
                        if overflow_retry
                        else await _hold_if_hitl_pending(
                            turn_message,
                            session_id,
                            config,
                            request_metadata=({"hitl_resume": True} if hitl_resume else None),
                        )
                    )
                    if hold is not None and hold is not _HITL_RESUME:
                        payload = _interrupt_payload(hold)
                        question = (
                            payload.get("question") or payload.get("title") or "The agent needs input to continue."
                        )
                        return [
                            {
                                "role": "assistant",
                                "content": (
                                    f"🙋 **Input needed first:** {question}\n\n"
                                    "_(Your message is queued — the agent gets it right after you answer.)_"
                                ),
                            }
                        ]
                    if hold is _HITL_RESUME:
                        from langgraph.types import Command

                        graph_input = Command(resume=await _resume_payload(config, turn_message))
                    else:
                        _msg = turn_message
                        # Kickoff injection (#1910), same as the streaming path: the first
                        # goal-driven turn (iteration 0) carries the goal condition so the agent
                        # begins on the goal instead of asking "what goal?".
                        if goal_active and _goal_state.iteration == 0:
                            _msg = STATE.goal_controller.kickoff_prompt(_goal_state, user_message=turn_message)
                        graph_input = {
                            # Vision parts ride the user message when the model supports
                            # them (#1943) — same gating as the streaming path.
                            "messages": [
                                _vision_human_message(_msg, turn_images, session_id=session_id, incognito=incognito)
                            ],
                            "session_id": session_id,
                            **_state_extra,
                        }
                    with goal_turn(goal_active):
                        result = await STATE.graph.ainvoke(graph_input, config=config)
                        # Headless-first parity (#1911): a goal-driven turn is autonomous, so if it
                        # parks on a HITL interrupt there's no operator here to answer — resume with
                        # the no-operator sentinel and re-run (bounded) instead of echoing the ask
                        # and stalling the drive. Non-goal turns are untouched (they still echo).
                        if goal_active:
                            from langgraph.types import Command

                            _auto = 0
                            while _auto < _MAX_AUTONOMOUS_AUTOANSWERS:
                                if await _pending_interrupt_value(config) is None:
                                    break
                                _auto += 1
                                result = await STATE.graph.ainvoke(
                                    Command(resume=_AUTONOMOUS_HITL_SENTINEL), config=config
                                )
                            if await _pending_interrupt_value(config) is not None:
                                # Budget spent, still parked → clear the dangling interrupt so the
                                # checkpoint isn't stranded; the drive loop below continues on the text.
                                await _clear_pending_interrupt(config)
                raw = _last_ai(result)
                response = extract_output(raw)

                # Robustness parity with the streaming path (bd-2qy): a turn can end
                # with no assistant text — at an ask_human interrupt, after a `wait`
                # yield, or on a scratch-only turn. Returning "" gives /api/chat +
                # OpenAI-compat callers a silent empty 200; surface something useful.
                if not response:
                    interrupt_val = await _pending_interrupt_value(config)
                    if interrupt_val is not None:
                        # ask_human / HITL — the graph paused for input. There's no
                        # task to park on this non-streaming surface, so echo the
                        # prompt; the caller answers with a follow-up message, which
                        # continues the thread (the checkpointer kept the history).
                        payload = _interrupt_payload(interrupt_val)
                        question = (
                            payload.get("question") or payload.get("title") or "The agent needs input to continue."
                        )
                        return [
                            {
                                "role": "assistant",
                                "content": f"🙋 **Input needed:** {question}",
                                "usage": _turn_telemetry.sum_usage(usage_cb.usage_metadata),
                            }
                        ]

                # Still nothing (e.g. a `wait` yield, or a tool-only turn): fall back
                # to the last tool result so the caller gets a signal, not a blank.
                # Both lookups are scoped to THIS turn, so reaching the final string means
                # the turn genuinely produced nothing — most likely it died mid-stream. Say
                # that plainly: the whole point of #2300 is that a caller must be able to
                # tell "no answer" from "an answer", and the previous wording read like a
                # deliberate quiet turn rather than a failure worth retrying.
                if not response:
                    response = _last_tool_text(result) or (
                        "**Error:** the turn produced no reply — it may have stalled or been "
                        "interrupted. Nothing was returned for this request; retry it. "
                        "(This is not the previous turn's answer.)"
                    )

                # Goal mode: verify after the agent stops; re-invoke with a
                # continuation prompt until met / exhausted / unachievable.
                if STATE.goal_controller is not None and STATE.goal_controller.active_goal(session_id):
                    guard, hard_cap = 0, STATE.graph_config.goal_max_iterations + 2
                    note = ""
                    while guard < hard_cap:
                        guard += 1
                        decision = await STATE.goal_controller.evaluate(session_id, last_text=response)
                        if decision is None:
                            break
                        note = decision.note
                        if decision.action == "done":
                            break
                        if _awaiting_self_resume(session_id):
                            # Async handoff (ADR 0079) — mirror the streaming path: the agent queued a
                            # watch/schedule that resumes this session, so pause the drive instead of
                            # spinning; the trigger's fire continues the goal.
                            note = "⏸ goal paused — handed off to a watch/schedule; will resume when it fires."
                            break
                        # Fresh-context goals get a scoped per-iteration thread; same-session
                        # reuse `config`. Same shared helper as the streaming path (no drift).
                        cont_config = _goal_continuation_config(config, decision.state)

                        # Lock the BASE thread (mirrors the streaming driver, which holds it
                        # across the whole goal loop): same-session iterations write `config`'s
                        # thread directly; fresh-context ones still exclude compact/rewind/
                        # streaming turns keyed on the base id.
                        async with _thread_lock(config["configurable"]["thread_id"]):
                            with goal_turn():
                                result = await STATE.graph.ainvoke(
                                    {
                                        "messages": [HumanMessage(content=decision.message)],
                                        "session_id": session_id,
                                        **_state_extra,
                                    },
                                    # Fresh-context iterations get a scoped config without the
                                    # turn's callbacks — re-attach usage_cb so their tokens count.
                                    config={**cont_config, "callbacks": [usage_cb]},
                                )
                        nxt = extract_output(_last_ai(result))
                        if nxt:
                            response = nxt
                    if note:
                        response = f"{response}\n\n---\n{note}"

                return [{"role": "assistant", "content": response, "usage": _turn_telemetry.sum_usage(usage_cb.usage_metadata)}]

            native_tid = config["configurable"]["thread_id"]
            return _traced(await _native_turn(message, images))
        except Exception as e:
            # Context overflow (#2783, ADR 0101 D4) — the recovery the streaming driver
            # always had and this one lacked (#3805): force-compact the thread once and
            # retry a single time; a second failure surfaces honestly below.
            if await _overflow_compacted(e, native_tid, session_id):
                try:
                    return _traced(await _native_turn(_OVERFLOW_RETRY_PROMPT, None, overflow_retry=True))
                except Exception as retry_exc:  # noqa: BLE001 — second failure surfaces honestly
                    log.exception("[chat] overflow retry failed for session=%s: %s", session_id, retry_exc)
                    e = retry_exc
            msg = await _fail_turn(e, session_id, tag="chat")
            return _traced([{"role": "assistant", "content": f"**Error:** {msg}", "error": turn_error(e, msg)}])
        finally:
            tracing.flush()
