"""Chat backend — the LangGraph turn loop behind every entry point.

Extracted from ``server/__init__.py`` (ADR 0023, phase 2). This module owns the
non-streaming ``chat`` (the console + OpenAI-compat) and streaming
``_chat_langgraph_stream`` (the A2A handler) turn drivers, tool-preview/interrupt
shaping, and slash-command parsing + execution for workflows and subagents.

The out-of-turn session gestures (``/compact``, export, publish, ``/btw``, rewind,
fork) live in ``server/chat_session_ops.py`` and the non-streaming usage/telemetry
helpers in ``server/turn_telemetry.py`` (#3810); the ACP runtime registry + turn
driving live in ``server/chat_acp.py`` (#3828) — the ``pre.acp`` switch is set by
``_pre_turn_dispatch``; the @-delegate room exchange lives in ``server/chat_rooms.py``
(#3838) — its call site (under the per-thread lock) is in ``_pre_turn_dispatch``;
turn control (thread locks + the thread-id resolver, origin classification, session
attendance, the server-turn control plane, the HITL hold and the idle beacon) lives in
``server/turn_control.py`` (#3847); the shared pre-turn dispatch chain
(``_PreTurn`` / ``_pre_turn_dispatch`` / ``_short_circuit_reply``) lives in
``server/chat_dispatch.py`` (#3861); the shared ``_run_turn_stream`` event loop lives in
``server/turn_stream.py`` (#3874); the non-streaming driver (``_chat_langgraph_impl``)
lives in ``server/turn_sync.py`` (#3917) — ``_chat_langgraph`` (its telemetry +
idle-beacon wrapper) stays here. All eight are re-exported below.

It depends only on neutral modules (``runtime.state``, ``graph.output_format``)
plus function-local imports — nothing from ``server/__init__``, so there is no
import cycle. ``server/__init__.py`` re-exports every public name so
``server.<symbol>`` keeps resolving for the OpenAI-compat / A2A wiring in
``_main`` and for the test suite.
"""

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from graph.fence_scope import fence_scope
from graph.middleware.redaction import redact as _redact
from graph.output_format import extract_output
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
# ``_pre_turn_dispatch`` (server/chat_dispatch.py, #3861) calls it through the module
# (``_chat_rooms.<name>``) so a patch there intercepts; these re-exports are COPIES of the
# bindings — patch server.chat_rooms, never these names (tests/test_chat_rooms_seam.py
# guards it).
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

# The shared pre-turn dispatch chain (``_PreTurn`` / ``_pre_turn_dispatch`` /
# ``_short_circuit_reply`` + the dispatch-only helpers) moved to server/chat_dispatch.py
# (#3861). Both drivers below call it through the module (``_chat_dispatch.<name>``) so a
# patch there intercepts; these re-exports are COPIES of the bindings — patch
# server.chat_dispatch, never these names (tests/test_chat_dispatch_seam.py guards it).
from server import chat_dispatch as _chat_dispatch
from server.chat_dispatch import (  # noqa: F401 — re-export
    _FENCED_ACP_REFUSAL,
    _SLASH_TOKEN_RE,
    _lifecycle_command_reply,
    _pre_turn_dispatch,
    _PreTurn,
    _short_circuit_reply,
    _unknown_slash_command_reply,
)

# Turn control (thread locks, the thread-id resolver, origin classification, attendance,
# the server-turn control plane, the HITL hold and the idle beacon) moved to
# server/turn_control.py (#3847). The drivers below reach every turn-control name they
# use (the locks / resolver / HITL hold, the autonomy classifier + auto-answer cap and
# sentinel, the idle beacon, the priority scope, the HITL-resume marker) through the
# module (``_turn_control.<name>``, #3856) so a patch there intercepts; these re-exports
# are kept for external importers but are COPIES of the bindings — patch/mutate
# server.turn_control, never these names. The rebound idle-beacon ints (``_ACTIVE_TURNS`` /
# ``_LAST_TURN_MONOTONIC``) are deliberately NOT re-exported: a copy of a rebound int is
# stale (tests/test_turn_control_seam.py guards all of it).
from server import turn_control as _turn_control
from server.turn_control import (  # noqa: F401 — re-export
    _ATTENDANCE_CONDITIONAL_ORIGINS,
    _ATTENDED_SESSIONS,
    _ATTENDED_SESSIONS_MAX,
    _AUTONOMOUS_HITL_SENTINEL,
    _AUTONOMOUS_ORIGINS,
    _CONTROL_ORIGINS,
    _HITL_RESUME,
    _INTERACTIVE_ORIGINS,
    _LIVE_SERVER_TURNS,
    _MAX_AUTONOMOUS_AUTOANSWERS,
    _THREAD_LOCKS,
    _background_resume_attended,
    _control_origin,
    _hold_if_hitl_pending,
    _interactive_turn_priority,
    _is_autonomous,
    _is_hitl_resume,
    _LiveServerTurn,
    _resolve_thread_id,
    _server_turn_key,
    _thread_lock,
    _truthy,
    _turn_ended,
    _turn_started,
    active_turns,
    attendance_stream,
    finish_live_server_turn,
    is_autonomous_origin,
    is_interactive_origin,
    is_session_attended,
    live_server_turn_control,
    mark_session_attended,
    register_live_server_turn,
    release_session_attended,
    seconds_since_last_turn,
    server_turn_control_payload,
    submit_server_turn_interjection,
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
# The streaming turn event loop (``_run_turn_stream``) and the helpers only it uses moved to
# server/turn_stream.py (#3874). ``_run_native_turn`` below calls it through the module
# (``_turn_stream._run_turn_stream``) so a patch there intercepts; these re-exports are
# COPIES of the bindings — patch server.turn_stream, never these names
# (tests/test_turn_stream_seam.py guards it).
from server import turn_stream as _turn_stream
from server.turn_stream import (  # noqa: F401 — re-export
    _BG_DISPATCH_REFUSED,
    _BG_JOB_ID,
    _TOOL_NODE,
    _delegation_summary,
    _lc_internal_call_marker,
    _paragraph_break,
    _run_turn_stream,
    _speaks_for_the_lead,
)
# The non-streaming turn driver (``_chat_langgraph_impl``, its lifted ``_native_turn``) and
# ``_trace_reply_output`` moved to server/turn_sync.py (#3917). ``_chat_langgraph`` below
# calls it through the module (``_turn_sync._chat_langgraph_impl``) so a patch there
# intercepts; these re-exports are COPIES of the bindings — patch server.turn_sync, never
# these names (tests/test_turn_sync_seam.py guards it).
from server import turn_sync as _turn_sync
from server.turn_sync import (  # noqa: F401 — re-export
    _chat_langgraph_impl,
    _trace_reply_output,
)
# The goal drive loop and the autonomous HITL auto-answer — ONE copy both turn drivers
# use (#3884). Not re-exported: reach it as ``_goal_loop.<name>`` (tests/test_goal_loop_seam.py).
from server import goal_loop as _goal_loop
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

    Known gap (#3931, rare): a goal-driven turn is autonomous and never parks, but a
    fresh-context goal the operator SETS mid-turn leaves that turn attended — so if one of
    its continuations asks (``ask_human`` / a form / an approval), both drivers park it on
    THIS scoped ``…:goal-iter-N`` thread. The operator's answer, though, is routed by the
    session's BASE thread id (the HITL hold / resume read the base thread's pending
    interrupt), so it doesn't resume the parked continuation: it runs as a fresh turn on the
    base thread, and the ``…:goal-iter-N`` interrupt is left stranded (the next iteration
    gets ``…:goal-iter-N+1``). Durable goal state is the plan artifact, so nothing is lost
    beyond that one ask; routing the answer here would need the parked thread id persisted
    per session.
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
    this" is the question those rows exist to answer. It also decides autonomy (#3891
    F2): a server-fired origin (``server.turn_control._AUTONOMOUS_ORIGINS``) auto-answers
    a HITL pause instead of echoing it, as the streaming driver does for that origin.
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


# The background-delegation receipt parsing (``_BG_JOB_ID`` / ``_BG_DISPATCH_REFUSED`` /
# ``_delegation_summary``) moved to server/turn_stream.py (#3874).


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


# The streaming event loop (``_run_turn_stream``) and its lead-speaker filter moved to
# server/turn_stream.py (#3874) and are re-exported at the top of this module.


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

# The lifecycle-command check and the plugin chat-command dispatch (this module's old uses
# of the shared ``graph.slash_commands`` resolver) moved with the pre-turn dispatch to
# server/chat_dispatch.py (#3861).
# ``_SLASH_TOKEN_RE`` / ``_unknown_slash_command_reply`` moved to server/chat_dispatch.py (#3861).


# The @-delegate room exchange (``_at_delegate_exchange`` and its parse/note helpers)
# moved to server/chat_rooms.py (#3838) and is re-exported at the top of this module.


# The per-thread lock registry (``_THREAD_LOCKS`` / ``_thread_lock``) moved to
# server/turn_control.py (#3847).


# Origin classification, session attendance and the server-turn control plane moved to
# server/turn_control.py (#3847).


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


# The HITL hold (``_hold_if_hitl_pending``) moved to server/turn_control.py (#3847).


def _metadata_fence(request_metadata: dict | None) -> list | None:
    """The per-turn tool fence a streaming request carries (``subagent_fence`` metadata —
    a detached background job, #1639, or a relayed peer turn, #2972), else ``None``."""
    fence = (request_metadata or {}).get("subagent_fence") or None
    if fence is not None and not isinstance(fence, (list, tuple)):
        return None
    return fence


async def _run_native_turn(
    message,
    session_id,
    config,
    *,
    request_metadata=None,
    resume=False,
    images=None,
    overflow_retry=False,
    fence=None,
):
    """One native LangGraph turn (the non-ACP path): run the graph, the dropped-turn
    kicker retry, and goal-mode continuations, then yield the terminal done frame. Extracted from _chat_langgraph_stream so the A2A handler can hold a per-thread
    lock around the whole turn without a deep in-line reindent. ``overflow_retry`` marks
    the context-overflow re-run, whose recovery prompt is never goal-kicked-off (#3891 F1).
    ``fence`` (the overflow retry's) replaces the request metadata's fence: the retry
    keeps the fence the failed pass narrowed to (``_turn_stream._carried_fence``)."""
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
    _fence = _metadata_fence(request_metadata) if fence is None else fence
    # The fence every later pass of this turn runs under: the turn's own, narrowed by any
    # fenced message a pass folds in (steering, #2972) — refreshed from the checkpoint
    # before each goal continuation (``_carried_fence``), as the non-streaming driver does.
    # When a goal is already active, the whole turn is goal-driven (suppress cross-session
    # prior_sessions on the initial turn + kicker, matching the continuation turns).
    _goal_state = _goal_loop.active_goal(session_id)
    goal_active = _goal_state is not None
    # A goal-driven turn also runs under the fence of the turn that SET the goal.
    _turn_fence = {"fence": _goal_loop.goal_fenced(_goal_state, _fence)}
    # Kickoff injection (#1910) — shared with the non-streaming driver (server/goal_loop.py).
    message = _goal_loop.kickoff_message(_goal_state, message, resume=resume, overflow_retry=overflow_retry)

    # An autonomous turn (no operator watching, or goal-driven — #1911) must never deadlock
    # on a HITL pause: the shared policy (server/goal_loop.py) answers each input_required
    # with the no-operator sentinel and runs another pass, up to a cap, then gives up. ONE
    # policy (and budget) for the whole turn: the initial pass and every goal continuation.
    _auto = _goal_loop.HitlAutoAnswer(_goal_loop.is_autonomous_turn(request_metadata, goal_active=goal_active))

    async def _drive_passes(pass_message, pass_config, *, resume_value, pass_images, out: dict):
        """Run one graph turn on ``pass_config`` to completion — a pass, then another for
        each interrupt the policy auto-answers — yielding its live frames. Leaves in ``out``:
        ``raw`` (the turn's raw text), ``paused`` (it parked at an interrupt for a human)
        and ``last_tool_out``. The initial turn and each goal continuation both run here, so
        an interrupt inside a continuation gets the same handling as one in the initial
        turn (#3891 F4) — it used to leak a stray ``input_required`` frame mid-drive."""
        # Text streamed by passes that ended at an auto-answered (or given-up) interrupt:
        # those passes yield `input_required` instead of `__raw__`, so without this their
        # text reached the live stream but never the terminal `done` (#3873). Built from the
        # forwarded `text` frames, which are exactly the pass's accumulated raw text — so
        # `done` stays the SAME string the live stream carried.
        _carried = ""
        while True:
            _autoanswer_pending = False
            _autonomous_giveup = False
            _pass_text = ""
            # aclosing (#3877): a bare `async for` leaves the inner generator unclosed when
            # this one is closed early — its cleanup would run at GC, not before aclose() returns.
            async with contextlib.aclosing(
                _turn_stream._run_turn_stream(
                    pass_message,
                    session_id,
                    pass_config,
                    resume_value=resume_value,
                    images=pass_images,
                    model=_model,
                    reasoning_effort=_effort,
                    incognito=_incognito,
                    # Every pass stamps the turn's fence — a continuation too. A
                    # fresh-context goal runs on a new thread with no checkpointed state to
                    # inherit, so the fence is stamped on every pass explicitly, as the
                    # non-streaming driver stamps ``_state_extra`` on its own; ``[]`` on an
                    # unfenced turn, so no pass inherits a fence the thread held before.
                    subagent_fence=_turn_fence["fence"],
                )
            ) as _turn_frames:
                async for kind, payload in _turn_frames:
                    if kind == "__raw__":
                        out["raw"] = (_carried + _pass_text) if _carried else payload
                    elif kind == "text" and _carried:
                        # A resumed pass's text opens a new paragraph after the carried text,
                        # as a new model call's text does within one pass (turn_stream) — on
                        # the live delta itself, so the stream and `done` stay one string.
                        if not _pass_text and _carried.strip():
                            payload = _turn_stream._paragraph_break(_carried, payload) + payload
                        _pass_text += payload
                        yield (kind, payload)
                    elif kind == "input_required":
                        _verdict = _auto.on_interrupt()
                        if _verdict == _goal_loop.PARK:
                            # Operator/a2a turn: surface it and park the turn; the A2A runner sets
                            # the task input-required and the caller resumes via message/send on the
                            # same taskId. (A human — local or at the remote a2a caller — can answer.)
                            yield (kind, payload)
                            out["paused"] = True
                        elif _verdict == _goal_loop.ANSWER:
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
                        if kind == "text":
                            _pass_text += payload
                        if kind == "tool_end" and isinstance(payload, dict) and payload.get("output"):
                            out["last_tool_out"] = str(payload["output"])
                        yield (kind, payload)
            if _autoanswer_pending or _autonomous_giveup:
                # This pass ended at an interrupt, not `__raw__`: keep its text for `done`.
                _carried += _pass_text
            if _autonomous_giveup and _carried:
                out["raw"] = _carried
            if _autoanswer_pending:
                # Resume past the interrupt with the no-operator sentinel and run another pass;
                # images belong only to the first (fresh) pass, so drop them on resume. The
                # turn stream keys the resume by interrupt id (_resume_payload).
                resume_value = _auto.answer()
                pass_images = None
                continue
            if _autonomous_giveup:
                # Discard the un-answered interrupt so the checkpoint isn't left dangling, then
                # fall through to the normal completion path (extract_output → done).
                await _auto.give_up(pass_config)
            break

    # One graph turn (model tokens accumulated silently; A2A consumers get progress from
    # tool_start/tool_end). Final text is extracted once via extract_output().
    turn: dict = {"raw": "", "paused": False, "last_tool_out": ""}
    with goal_turn(goal_active):
        async with contextlib.aclosing(
            _drive_passes(
                message, config, resume_value=(message if resume else None), pass_images=images, out=turn
            )
        ) as _frames:
            async for frame in _frames:
                yield frame

    # A paused turn produced no final answer — don't run the dropped-scratch kicker or
    # goal verification; the task is parked.
    if turn["paused"]:
        return

    final_text = extract_output(turn["raw"])

    # Never end the stream on a silent empty answer (a native-reasoning model that emitted
    # only reasoning, or an otherwise empty turn): surface the last tool result or a
    # placeholder, matching the non-streaming path's _last_tool_text-or-placeholder. Applied
    # BEFORE the goal drive, as the non-streaming driver does (#3891 F5): the verifier
    # judges the answer the caller gets — it used to see "" here — and the terminal goal
    # note is appended after evaluation, so an empty turn under a goal no longer ends as a
    # bare note with the fallback skipped (the note made the text non-empty).
    if not final_text:
        final_text = turn["last_tool_out"] or "_(The agent ended the turn without a textual reply.)_"

    # Goal mode (shared drive, server/goal_loop.py): verify the outcome after the agent
    # stops; while not met, run the continuation it asks for. The 🎯 status frames are this
    # surface's; the terminal note lands on final_text so the A2A terminal artifact carries
    # it (the status frames are transient and can coalesce).
    drive = _goal_loop.GoalDrive(session_id, config, final_text)
    _last_config = config
    async with contextlib.aclosing(drive.steps()) as _goal_steps:
        async for step in _goal_steps:
            if isinstance(step, _goal_loop.GoalNote):
                yield ("tool_start", f"🎯 {step.note}")
                continue
            # Keep any narrowing the previous pass folded in (narrowest wins).
            # ...and a continuation always drives the (possibly just-set) goal: its fence too.
            _turn_fence["fence"] = _goal_loop.goal_fenced(
                _goal_loop.active_goal(session_id),
                await _turn_stream._carried_fence(_last_config, _turn_fence["fence"]),
            )
            _last_config = step.config
            cont: dict = {"raw": "", "paused": False, "last_tool_out": ""}
            with goal_turn():
                async with contextlib.aclosing(
                    _drive_passes(step.message, step.config, resume_value=None, pass_images=None, out=cont)
                ) as _cont_frames:
                    async for frame in _cont_frames:
                        yield frame
            if cont["paused"]:
                # An attended turn's continuation asked the operator (#3891 F4): the drive
                # stops here and the turn parks on that ask — no further verify/continue,
                # no `done` — exactly as an ask in the initial turn parks it.
                return
            step.text = extract_output(cont["raw"])
    final_text = drive.text

    yield ("done", final_text)


# The idle beacon (``_ACTIVE_TURNS`` / ``active_turns`` / ``seconds_since_last_turn``) moved to
# server/turn_control.py (#3847).


# ``_lifecycle_command_reply`` moved to server/chat_dispatch.py (#3861).


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
    _turn_control._turn_started(session_id)
    # ADR 0074 — idle→active lifecycle event (debounced). Emitted inside the triggering
    # turn's fence scope: a configured prompt reaction is a turn the event enqueues, so it
    # records that fence (the reaction task copies this context) and a fenced turn can't
    # wake an unfenced one (graph/fence_scope).
    with fence_scope(_metadata_fence(request_metadata)):
        _note_agent_active(session_id)
    try:
        # aclosing (#3870): a bare `async for` does not close the impl when the consumer
        # closes THIS generator early, so the impl's `finally` (thread-lock release, trace
        # flush) would wait for the loop's async-generator finalizer (GC).
        async with contextlib.aclosing(
            _chat_langgraph_stream_impl(
                message,
                session_id,
                caller_trace=caller_trace,
                resume=resume,
                request_metadata=request_metadata,
                images=images,
            )
        ) as _impl:
            async for _ev in _impl:
                _trace_terminal_output(_ev)
                yield _ev
    finally:
        _turn_control._turn_ended(session_id)


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
# and the non-streaming ``_chat_langgraph_impl`` (server/turn_sync.py; ``chat()``: OpenAI-compat /v1,
# /api/chat, plugin surfaces) — used to carry their own copy of this chain and of
# the error handling. The copies drifted: the non-streaming one never learned
# `/subagent` or the context-overflow compact-and-retry. ONE chain now, and one
# failure classifier; each driver only decides how to SHAPE what it yields.


# ``_PreTurn`` / ``_pre_turn_dispatch`` / ``_short_circuit_reply`` (and ``_FENCED_ACP_REFUSAL``)
# moved to server/chat_dispatch.py (#3861) and are re-exported at the top of this module.


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

        async with _turn_control._thread_lock(thread_id):
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


async def _fail_turn(exc: BaseException, session_id: str, *, tag: str, thread_id: str | None = None) -> str:
    """Log, record (#2593) and describe a failed turn — ONE classifier for both drivers,
    so the two surfaces leave the SAME transcript and the same log shape. Returns the
    user-facing message (the streaming driver's ``error`` payload; the non-streaming
    driver wraps it as ``**Error:** …``).

    ``thread_id`` is the thread the turn ran on, as the driver resolved it — the record
    must land THERE, not on a re-resolution without the turn's request metadata (#3871)."""
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
    await record_failed_turn(session_id, f"**Error:** {msg}", thread_id=thread_id)
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
            # A FENCED turn skips every short-circuit and is refused on an ACP runtime,
            # as on the non-streaming driver (#3812). The one ACP exception is this
            # process's own detached background job (#1639), proven by the single-use
            # token its fire minted (background/fire_auth.py) — redeemed here for EVERY
            # turn that carries one, so a token is dead once its turn has started.
            _fence = _metadata_fence(request_metadata)
            from background import fire_auth

            _own_background_fire = fire_auth.redeem(request_metadata, session_id)
            pre = _chat_dispatch._PreTurn(
                message,
                fenced=bool(_fence),
                fence=list(_fence or []),
                acp_exempt=bool(_fence) and _own_background_fire,
            )
            async with contextlib.aclosing(
                _chat_dispatch._pre_turn_dispatch(pre, session_id, request_metadata)
            ) as _pre_frames:
                async for frame in _pre_frames:
                    yield frame
            if pre.handled:
                return
            message = pre.message

            if pre.acp:
                _acp_tid = _turn_control._resolve_thread_id(request_metadata, session_id)
                # Hold the runtime "in-flight" for the whole turn so a concurrent turn's
                # eviction can't close it mid-stream (a long ACP coding turn can outlast the
                # idle TTL); registry mutation is serialized by _ACP_LOCK. See _acp_acquire.
                rt = await _chat_acp._acp_acquire(_acp_tid)
                try:
                    # One prompt at a time per ACP session — AcpClient forbids concurrent
                    # prompts on an instance (a session is a single conversation), and the
                    # refcount above only guards eviction. Same per-thread lock the native
                    # turns hold, so compact/rewind on this thread are excluded too.
                    async with _turn_control._thread_lock(_acp_tid):
                        # aclosing: `async for` alone does not close the inner generator
                        # when THIS one is abandoned at its yield — it would be finalized
                        # later by GC, after the release below. Closing it here stops the
                        # turn first (#3837).
                        async with contextlib.aclosing(_chat_acp._acp_drive_turn(rt, message)) as _acp_frames:
                            async for frame in _acp_frames:
                                yield frame
                finally:
                    await _chat_acp._acp_release(_acp_tid)
                return

            # thread_id keys this session's history in the checkpointer (bound
            # at compile time in create_agent_graph). The prefix isolates A2A
            # sessions from the non-streaming chat in the shared MemorySaver. Derivation is
            # a pluggable seam (#571): a fork registers a resolver to scope memory
            # off request metadata (e.g. per-project) without editing this file.
            _tid = _turn_control._resolve_thread_id(request_metadata, session_id)
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
            async with _turn_control._thread_lock(_tid):
                # HITL hold (#1560): while this thread is parked at a form/question/
                # approval interrupt, a fresh operator message is HELD in the steering
                # queue (it folds in right after the form response) and the turn re-parks
                # on the same payload; the marked form answer converts to a real resume.
                # No pending interrupt ⇒ hold is None and nothing changes.
                #
                # A message continuing an input-required TASK (A2A §3.4.3) is only a
                # graph resume while the THREAD still has that interrupt pending (#3930).
                # A task orphaned by an answer that landed elsewhere (a fresh task, the
                # /api/chat fallback) is still input-required but its interrupt is gone:
                # `Command(resume=…)` on a thread with nothing pending is a silent no-op
                # in LangGraph, so the operator's text would vanish and the task complete
                # empty. Run it as the fresh message it now is instead.
                if resume and await _pending_interrupt_value(config) is None:
                    log.info(
                        "[a2a-stream] session=%s: resume on a task with no pending interrupt — running it as a fresh message",
                        session_id,
                    )
                    resume = False
                if not resume:
                    hold = await _turn_control._hold_if_hitl_pending(
                        message, session_id, config, request_metadata=request_metadata, fence=_fence
                    )
                    if hold is _turn_control._HITL_RESUME:
                        resume = True
                    elif hold is not None:
                        yield ("input_required", _interrupt_payload(hold))
                        return
                # ADR 0115 D6 (#3760): a live operator turn (empty origin) runs under
                # `interactive`; inbound `a2a` and the server-fired autonomous origins stay
                # `default`. The class is set for the whole native turn — both loops inside
                # _run_native_turn — so its subagent tasks inherit it.
                with _turn_control._interactive_turn_priority((request_metadata or {}).get("origin")):
                    # aclosing (#3877): close the native turn inside this generator's own
                    # close, so its cleanup (goal_turn scope, the inner event loop) runs
                    # before aclose() returns rather than at GC.
                    async with contextlib.aclosing(
                        _run_native_turn(
                            message, session_id, config, request_metadata=request_metadata, resume=resume, images=images
                        )
                    ) as _native_frames:
                        async for frame in _native_frames:
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
                    # The retry is the same turn: it keeps the fence the failed pass ran
                    # under, narrowed by anything it folded in — never a wider one.
                    _retry_fence = await _turn_stream._carried_fence(config, _metadata_fence(request_metadata))
                    async with _turn_control._thread_lock(_tid):
                        # Same class as the initial turn (ADR 0115 D6) — the retry is the
                        # same operator/A2A turn, just after a force-compact.
                        with _turn_control._interactive_turn_priority((request_metadata or {}).get("origin")):
                            async with contextlib.aclosing(
                                _run_native_turn(
                                    _OVERFLOW_RETRY_PROMPT,
                                    session_id,
                                    config,
                                    request_metadata=request_metadata,
                                    resume=False,
                                    images=None,
                                    overflow_retry=True,
                                    fence=_retry_fence,
                                )
                            ) as _retry_frames:
                                async for frame in _retry_frames:
                                    yield frame
                    return
                except Exception as retry_exc:  # noqa: BLE001 — second failure surfaces honestly
                    log.exception("[a2a-stream] overflow retry failed for session=%s: %s", session_id, retry_exc)
                    e = retry_exc
            # Same record as the non-streaming path: the two surfaces must leave the
            # SAME transcript, or an exported thread depends on which one ran (#2593).
            # Record on the thread the turn ran on (#3871): `_tid` once the native turn
            # resolved it, else the same metadata-aware resolution (pre-turn chain / ACP).
            _fail_tid = _tid or _turn_control._resolve_thread_id(request_metadata, session_id)
            yield ("error", await _fail_turn(e, session_id, tag="a2a-stream", thread_id=_fail_tid))
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


def _upstream_status(exc: BaseException | None) -> int | None:
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


async def record_failed_turn(session_id: str, text: str, *, thread_id: str | None = None) -> bool:
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

    ``thread_id`` is the thread the failed turn ran on. Callers that resolved it (both
    turn drivers) must pass it: re-resolving here has no request metadata, so under a
    metadata-aware resolver (ADR 0069 D4) the record landed on a different thread
    (#3871). Omitted, it falls back to the metadata-less default resolution.
    """
    graph = STATE.graph
    if graph is None or not text.strip():
        return False
    try:
        from langchain_core.messages import AIMessage  # lazy, like every other use here

        await graph.aupdate_state(
            {"configurable": {"thread_id": thread_id or _turn_control._resolve_thread_id(None, session_id)}},
            {"messages": [AIMessage(content=text, additional_kwargs={"protoagent_turn_failed": True})]},
        )
        return True
    except Exception:  # noqa: BLE001 — never let bookkeeping bury the real failure
        log.warning("[chat] could not record the failed turn for session=%s", session_id, exc_info=True)
        return False


def turn_error(exc: BaseException | None, message: str | None = None) -> dict[str, Any]:
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
        "message": message or str(exc or "the turn failed"),
        "type": _ERROR_TYPE_BY_STATUS.get(_upstream_status(exc), "server_error"),
        "upstream_status": _upstream_status(exc),
        # None for a failure with no exception behind it (a turn that produced no reply, #3873).
        "exception": type(exc).__name__ if exc is not None else None,
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
    _turn_control._turn_started(session_id)
    with fence_scope(tool_fence):  # the triggering turn's fence — see _chat_langgraph_stream
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
        with _turn_control._interactive_turn_priority(origin):
            result = await _turn_sync._chat_langgraph_impl(
                message,
                session_id,
                model=model,
                incognito=incognito,
                hitl_resume=hitl_resume,
                images=images,
                tool_fence=tool_fence,
                origin=origin,
                _telemetry_sink=sink,
            )
        # The impl catches its own exceptions and reports them as an assistant
        # bubble carrying a structured `error` (server.chat.turn_error), so the
        # error key — not an exception — is what distinguishes a failed turn.
        state = "failed" if any(isinstance(m, dict) and m.get("error") for m in result) else "completed"
        return result
    finally:
        _turn_control._turn_ended(session_id)
        _turn_telemetry.record_local_turn(sink, session_id=session_id, origin=origin, state=state, started=started)
