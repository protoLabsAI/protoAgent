"""protoAgent's A2A 1.0 AgentExecutor — drives LangGraph through ``a2a-sdk``.

Replaces the hand-rolled ``a2a_handler.py``. ``a2a-sdk`` owns every piece of
protocol mechanics (JSON-RPC dispatch, SSE streaming, the task lifecycle, push
delivery, the in-memory task store). This module is the bridge: it adapts
protoAgent's existing ``_chat_langgraph_stream`` event generator
(``(event_type, payload)`` tuples) onto the SDK's ``EventQueue`` via
``TaskUpdater``, and emits the four protoLabs extensions through
``protolabs_a2a``.

The producer-event contract (unchanged from the hand-rolled handler) is::

    text            accumulated answer text (streamed)
    tool_start      a tool began      (dict {id,name,input} | str)
    tool_end        a tool finished   (dict {id,name,output} | str)
    delta           a worldstate-delta {domain,path,op,value}
    usage           per-LLM-call token usage {input_tokens,output_tokens,...}
    confidence      self-reported {confidence, explanation?}
    input_required  HITL pause {question}
    done            terminal; payload is the final text
    error           terminal; payload is the error string

On terminal completion the accumulated text + the cost / confidence /
worldstate-delta extension DataParts are published as a single artifact. Tool
events are surfaced as tool-call-v1 DataParts on the working status frames.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus
from google.protobuf import json_format, struct_pb2

import protolabs_a2a as pa

logger = logging.getLogger(__name__)

# protoAgent-LOCAL extension (not one of the four fleet extensions in
# ``protolabs_a2a``): the HITL form/approval payload surfaced on an
# ``input-required`` frame so the operator console can render a JSON-schema form
# or an Approve/Deny card (the agent's ``request_user_input`` / ``run_command``
# approval). The console matches on this MIME; the hub doesn't need it. Built
# with the same ``data_part`` wire primitive as the fleet extensions, so it
# rides the 1.0 envelope identically — it's just not on the shared card.
HITL_MIME = "application/vnd.protolabs.hitl-v1+json"
# Streamed scratch_pad reasoning ("thinking") — carried on WORKING status frames as
# a DataPart, distinct from the answer artifact, so the console can show a
# collapsible reasoning view. Plain consumers ignore it.
REASONING_MIME = "application/vnd.protolabs.reasoning-v1+json"

# A renderable UI component (ADR 0051 Slice 2) — a typed, data-only widget the console
# renders inline ({component, props}). Same DataPart contract as the HITL/tool-call parts.
from graph.components import COMPONENT_MIME  # noqa: E402


@dataclass
class TurnOutcome:
    """Everything a host needs at the end of an A2A turn (ADR 0003 / 0006).

    Passed to the registered terminal hook so the host can (a) surface the
    visible answer on the Activity thread and (b) record a per-turn telemetry
    row — without the executor itself depending on either subsystem.
    """

    task_id: str
    context_id: str
    state: str  # "completed" | "failed"
    text: str
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    duration_ms: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    models: list[str] = field(default_factory=list)
    # Provenance (ADR 0022) — what triggered this turn, from the inbound A2A
    # message metadata. ``origin`` ∈ scheduler|inbox|webhook|a2a|"" (empty = a
    # live/operator turn); ``trigger`` is a human label (job id / inbox source);
    # ``priority`` is the inbox tier when applicable.
    origin: str = ""
    trigger: str = ""
    priority: str = ""
    # The triggering input text (the scheduled prompt / inbound message / webhook body),
    # truncated — so the Activity feed can show the response as an explicit reply to it (#1375).
    stimulus: str = ""


# A terminal hook the host can register (ADR 0003 / 0006): invoked with a
# ``TurnOutcome`` when a turn reaches a terminal state, so the host can surface
# the answer on the Activity thread and record telemetry. No-op when unset.
_ON_TERMINAL: list[Callable[[TurnOutcome], None] | None] = [None]


def set_terminal_hook(hook: Callable[[TurnOutcome], None] | None) -> None:
    """Register (or clear) the terminal hook fired on task completion."""
    _ON_TERMINAL[0] = hook


def _notify_terminal(outcome: TurnOutcome) -> None:
    cb = _ON_TERMINAL[0]
    if cb is None:
        return
    try:
        cb(outcome)
    except Exception:  # noqa: BLE001 — best-effort, never breaks the turn
        logger.exception("[a2a] terminal hook failed for context %s", outcome.context_id)


# A progress hook (ADR 0051) the host can register to observe a turn's realtime
# frames — fired at turn start (``turn_started``, which carries the task_id) and on
# each tool start/end with ``(context_id, task_id, frame)``. No-op when unset, so live
# turns (which already stream over their own SSE) pay nothing; the background-subagents
# publisher uses it to surface a detached turn's progress on the event bus.
_ON_PROGRESS: list[Callable[[str, str, dict], None] | None] = [None]


def set_progress_hook(hook: Callable[[str, str, dict], None] | None) -> None:
    """Register (or clear) the per-frame progress hook (ADR 0051)."""
    _ON_PROGRESS[0] = hook


def _notify_progress(context_id: str, task_id: str, frame: dict) -> None:
    cb = _ON_PROGRESS[0]
    if cb is None:
        return
    try:
        cb(context_id or "", task_id or "", frame)
    except Exception:  # noqa: BLE001 — best-effort, never breaks the turn
        logger.exception("[a2a] progress hook failed for context %s", context_id)


def _text_part(text: str) -> Part:
    return Part(text=text)


def _data_part_proto(payload: Any, mime_type: str) -> Part:
    """A proto ``Part`` carrying ``payload`` under ``mime_type``.

    ``a2a-sdk`` serializes this to the A2A 1.0 wire shape
    ``{"data": …, "metadata": {"mimeType": …}, "mediaType": "application/json"}``.
    The payload values, MIME, and extension URI match the protoLabs contract
    that ``protolabs_a2a`` documents.
    """
    part = Part()
    value = struct_pb2.Value()
    json_format.ParseDict(payload, value.struct_value)
    part.data.CopyFrom(value)
    part.metadata.update({pa.MIME_KEY: mime_type})
    part.media_type = pa.DATA_MEDIA_TYPE
    return part


def _ext_data_part(emit_dict: dict[str, Any]) -> Part:
    """Convert a ``protolabs_a2a.emit_*`` contract dict into a proto ``Part``."""
    mime = emit_dict["metadata"][pa.MIME_KEY]
    payload = emit_dict["content"]["value"]
    return _data_part_proto(payload, mime)


def _hitl_prompt(payload: Any) -> str:
    """A human-readable prompt for an ``input-required`` pause, for consumers
    that don't parse the hitl-v1 DataPart. Forms/approvals fall back to their
    title; a plain ask uses its question."""
    if isinstance(payload, dict):
        return str(payload.get("question") or payload.get("title") or "Input required.")
    return str(payload) if payload is not None else "Input required."


class ProtoAgentExecutor(AgentExecutor):
    """Bridges protoAgent's LangGraph stream onto the A2A event queue.

    A single ``execute`` call runs one turn end-to-end (or to a HITL pause).
    ``cancel`` genuinely stops the turn: the a2a-sdk cancels the producer task,
    injecting ``CancelledError`` into this coroutine — which the execute loop catches
    to record a ``canceled`` outcome before re-raising, and the LangGraph stream
    unwinds. (A live ``SubscribeToTask`` re-attach also exists in this SDK; polling
    ``GetTask`` is only the *cross-restart* ceiling.)
    """

    def __init__(
        self,
        stream_fn_factory: Callable[..., AsyncGenerator[tuple[str, Any], None]],
        structured_finalizer: Callable[[str, str], Any] | None = None,
        context_meta_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        # ``stream_fn_factory(text, context_id, *, resume, caller_trace,
        # request_metadata)`` → async generator of (event_type, payload). This is
        # protoAgent's ``_chat_langgraph_stream``; ``request_metadata`` is the
        # merged A2A request metadata, passed through so the backend's thread_id
        # resolver (#571) can scope memory off it.
        self._stream_factory = stream_fn_factory
        # ``structured_finalizer(skill_id, final_text)`` → an emit DataPart dict
        # or None (#476). Injected by server.py so the executor stays decoupled
        # from the skill registry (no circular import).
        self._structured_finalizer = structured_finalizer
        # ``context_meta_provider()`` → the static compaction context for the context-v1
        # DataPart (#1372): {enabled, trigger, compactionAtTokens?}. Injected by server.py
        # (it reads STATE.graph_config) so the executor needn't import the config (layering).
        self._context_meta_provider = context_meta_provider

    async def _append_structured(self, parts: list[Part], context: RequestContext, final_text: str) -> list[Part]:
        """If the turn targets a structured skill (``skillHint`` + a declared
        schema), append the schema-enforced result as a DataPart (#476). No-op
        otherwise; a finalizer error degrades to the text-only parts."""
        if self._structured_finalizer is None or not final_text:
            return parts
        skill = _extract_skill_hint(context)
        if not skill:
            return parts
        try:
            emit = await self._structured_finalizer(skill, final_text)
        except Exception:  # noqa: BLE001
            logger.exception("[structured] finalizer raised for skill %s", skill)
            return parts
        return [*parts, _ext_data_part(emit)] if emit else parts

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)

        # A resumed task (HITL) already exists in input-required; a fresh task
        # must be enqueued as a Task object first (the framework requires the
        # initial Task before any TaskStatusUpdateEvent), then transitioned to
        # working.
        resume = bool(context.current_task and _is_input_required(context.current_task))
        if not resume:
            await event_queue.enqueue_event(
                Task(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                )
            )
        await updater.start_work()
        # Realtime: announce the turn (carries the task_id — the handle a host needs to
        # control or re-attach to this turn) before any work runs (ADR 0051).
        _notify_progress(context.context_id, context.task_id, {"phase": "turn_started"})

        text = context.get_user_input()
        images = _extract_image_parts(context)
        caller_trace = _extract_caller_trace(context)

        # Provenance for the Activity feed (ADR 0022): what triggered this turn.
        _md = _request_metadata(context)
        _origin = str(_md.get("origin", "") or "")
        _priority = str(_md.get("priority", "") or "")
        _trigger = str(_md.get("trigger") or _md.get("scheduler_job_id") or _md.get("inbox_source") or "")
        # The stimulus = this turn's input text, kept as a truncated preview so the Activity
        # feed can show the response as an explicit reply to what triggered it (#1375).
        _stimulus = (text or "").strip()
        if len(_stimulus) > 600:
            _stimulus = _stimulus[:600] + "…"

        started = time.monotonic()
        accumulated = ""
        deltas: list[dict] = []
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        cost_usd = 0.0
        had_usage = False
        # Peak prompt size this turn = the largest single model call's input_tokens (the
        # model's own count of all prompt tokens, incl. cache reads). Unlike the summed
        # usage above, this is the live context-window FILL, not per-turn spend (#1372).
        context_tokens = 0
        confidence: float | None = None
        confidence_expl: str | None = None
        llm_calls = 0
        tool_calls = 0
        models: list[str] = []

        # Live answer streaming: forward each text delta as an incremental
        # artifact-update (append) frame so the console fills the bubble as the
        # model writes, instead of the whole answer landing at turn end. Batched
        # by a small char threshold to avoid a frame per token. The terminal
        # emission then REPLACES this artifact (append=False) with the canonical
        # final text + the cost/confidence DataParts — so the durable task and any
        # re-fetch carry the answer exactly once (and a kicker/goal retry that
        # changed the text still finalizes correctly).
        answer_aid = f"{context.task_id or 'turn'}-answer"
        _text_buf = ""
        _answer_started = False  # first chunk creates the artifact (append=False); rest append
        _FLUSH_CHARS = 24

        async def _flush_text() -> None:
            nonlocal _text_buf, _answer_started
            if not _text_buf:
                return
            await updater.add_artifact(
                [_text_part(_text_buf)],
                artifact_id=answer_aid,
                append=_answer_started,
                last_chunk=False,
            )
            _answer_started = True
            _text_buf = ""

        async def _finalize(final_text: str) -> None:
            """Close the answer artifact + emit the cost/confidence DataParts. If
            the text was streamed (delta frames), append ONLY the meta parts so
            concat-based consumers don't double the answer; otherwise emit the full
            text once (the non-streaming path: workflow/subagent short-circuits)."""
            # text="" yields a dataparts-only list (the text part is conditional).
            body = "" if _answer_started else final_text
            # Compaction context (#1372): the live prompt size + the configured trigger /
            # token threshold, merged into one context-v1 DataPart. Provider failures
            # degrade to "size only" — never break the turn's finalization.
            context_meta: dict[str, Any] | None = None
            if context_tokens > 0:
                meta: dict[str, Any] = {}
                if self._context_meta_provider is not None:
                    try:
                        meta = self._context_meta_provider() or {}
                    except Exception:  # noqa: BLE001 — telemetry must never break a turn
                        meta = {}
                context_meta = {"contextTokens": context_tokens, **meta}
            parts = _terminal_parts(
                body,
                deltas,
                usage if had_usage else None,
                cost_usd,
                confidence,
                confidence_expl,
                context_meta,
                success=True,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            parts = await self._append_structured(parts, context, final_text)
            if parts:
                await updater.add_artifact(
                    parts,
                    artifact_id=answer_aid,
                    append=_answer_started,
                    last_chunk=True,
                )

        def _outcome(state: str, final_text: str) -> TurnOutcome:
            return TurnOutcome(
                task_id=context.task_id,
                context_id=context.context_id,
                state=state,
                text=final_text,
                usage=dict(usage),
                cost_usd=round(cost_usd, 6),
                duration_ms=int((time.monotonic() - started) * 1000),
                llm_calls=llm_calls,
                tool_calls=tool_calls,
                models=list(models),
                origin=_origin,
                trigger=_trigger,
                priority=_priority,
                stimulus=_stimulus,
            )

        try:
            async for event_type, payload in self._stream_factory(
                text,
                context.context_id,
                resume=resume,
                caller_trace=caller_trace,
                request_metadata=_md,
                images=images,
            ):
                if event_type == "text":
                    accumulated += payload
                    _text_buf += payload
                    if len(_text_buf) >= _FLUSH_CHARS:
                        await _flush_text()

                elif event_type in ("tool_start", "tool_end"):
                    # Flush any buffered preamble text BEFORE the tool frame so the
                    # console renders pre-tool text above the tool card (ordering fix)
                    # rather than after it — the client builds its ordered render blocks
                    # in frame-arrival order, so text must reach it before the tool.
                    await _flush_text()
                    if event_type == "tool_start":
                        tool_calls += 1
                    part = _tool_call_part(event_type, payload)
                    if part is not None:
                        await updater.update_status(
                            TaskState.TASK_STATE_WORKING,
                            message=updater.new_agent_message([part]),
                        )
                    # Realtime tap (ADR 0051) — surface the tool frame to any host hook.
                    if isinstance(payload, dict):
                        _notify_progress(context.context_id, context.task_id, {"phase": event_type, **payload})

                elif event_type == "component":
                    # A renderable UI component (ADR 0051 Slice 2) — emit it as a
                    # component-v1 DataPart on a working frame so the console renders it
                    # inline immediately.
                    if isinstance(payload, dict):
                        await updater.update_status(
                            TaskState.TASK_STATE_WORKING,
                            message=updater.new_agent_message([_data_part_proto(payload, COMPONENT_MIME)]),
                        )

                elif event_type == "reasoning":
                    # Live "thinking" — a reasoning DataPart on a WORKING frame,
                    # separate from the answer artifact (plain consumers ignore it).
                    if payload:
                        await updater.update_status(
                            TaskState.TASK_STATE_WORKING,
                            message=updater.new_agent_message(
                                [_data_part_proto({"text": str(payload)}, REASONING_MIME)]
                            ),
                        )

                elif event_type == "delta":
                    if isinstance(payload, dict):
                        deltas.append(payload)

                elif event_type == "usage":
                    if isinstance(payload, dict):
                        had_usage = True
                        llm_calls += 1
                        context_tokens = max(context_tokens, int(payload.get("input_tokens", 0) or 0))
                        usage["input_tokens"] += int(payload.get("input_tokens", 0) or 0)
                        usage["output_tokens"] += int(payload.get("output_tokens", 0) or 0)
                        usage["cache_read_input_tokens"] += int(payload.get("cache_read_input_tokens", 0) or 0)
                        usage["cache_creation_input_tokens"] += int(payload.get("cache_creation_input_tokens", 0) or 0)
                        cost_usd += float(payload.get("cost_usd", 0.0) or 0.0)
                        model = payload.get("model", "")
                        if model and model not in models:
                            models.append(model)

                elif event_type == "confidence":
                    if isinstance(payload, dict) and payload.get("confidence") is not None:
                        confidence = max(0.0, min(1.0, float(payload["confidence"])))
                        expl = payload.get("explanation")
                        confidence_expl = expl.strip() if isinstance(expl, str) and expl.strip() else None

                elif event_type == "input_required":
                    await _flush_text()  # persist any answer text streamed before the pause
                    # Human-readable prompt for plain consumers; the full
                    # form/approval payload rides a protoAgent-local hitl-v1
                    # DataPart so the console renders the form / approval card.
                    parts = [_text_part(_hitl_prompt(payload))]
                    if isinstance(payload, dict):
                        parts.append(_data_part_proto(payload, HITL_MIME))
                    await updater.requires_input(message=updater.new_agent_message(parts))
                    return  # parked — the caller resumes via message/send on this task

                elif event_type == "done":
                    await _flush_text()
                    final_text = payload or accumulated
                    await _finalize(final_text)
                    await updater.complete()
                    _notify_terminal(_outcome("completed", final_text))
                    return

                elif event_type == "error":
                    await updater.failed(message=updater.new_agent_message([_text_part(str(payload))]))
                    _notify_terminal(_outcome("failed", accumulated))
                    return

            # Stream ended without an explicit terminal event — treat the
            # accumulated text as the answer.
            await _flush_text()
            await _finalize(accumulated)
            await updater.complete()
            _notify_terminal(_outcome("completed", accumulated))

        except asyncio.CancelledError:
            # A real CancelTask (ADR 0051): the SDK cancels the producer task, injecting
            # CancelledError here. Record a terminal outcome so the turn isn't an
            # observability hole and a canceled background job settles — then re-raise so
            # the SDK's cancel flow (the `canceled` frame) completes normally.
            _notify_terminal(_outcome("canceled", accumulated))
            raise

        except Exception as exc:  # noqa: BLE001 — surface to the task, fail loud
            logger.exception("[a2a] execute crashed for task %s", context.task_id)
            await updater.failed(message=updater.new_agent_message([_text_part(str(exc))]))
            _notify_terminal(_outcome("failed", accumulated))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _is_input_required(task: Any) -> bool:
    try:
        return task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    except AttributeError:
        return False


def _request_metadata(context: RequestContext) -> dict:
    """Merged A2A request metadata: message-level (fallback) overlaid by
    request-level (preferred). The a2a-sdk surfaces ``SendMessageRequest``-level
    metadata on ``context.metadata`` (a dict) — that's where clients (e.g. the
    hub) put routing keys like ``skillHint`` + ``a2a.trace``, so request-level
    must win. Reading only ``context.message.metadata`` silently misses all of
    it (the latent bug found via jon's reference)."""
    merged: dict = {}
    msg = getattr(context, "message", None)
    if msg is not None and getattr(msg, "metadata", None):
        try:
            merged.update(json_format.MessageToDict(msg.metadata))
        except Exception:  # noqa: BLE001
            pass
    req = getattr(context, "metadata", None)
    if isinstance(req, dict):
        merged.update(req)
    elif req is not None:
        try:
            merged.update(json_format.MessageToDict(req))
        except Exception:  # noqa: BLE001
            pass
    return merged


def _extract_caller_trace(context: RequestContext) -> dict:
    """The ``a2a.trace`` metadata (Langfuse cross-trace propagation), or {}."""
    trace = _request_metadata(context).get("a2a.trace")
    return trace if isinstance(trace, dict) else {}


def _extract_image_parts(context: RequestContext) -> list[tuple[str, str]]:
    """Vision image parts off the inbound message → ``[(media_type, data_uri)]``.

    A part with an ``image/*`` media_type carries either inline bytes (proto
    ``raw``, base64-encoded into a ``data:`` URI) or a ``url`` (already a
    ``data:``/``http`` URL). Empty when there are none — so non-vision turns and
    text-only consumers pay nothing. The turn handler only forwards these to the
    model when the active model is vision-capable."""
    import base64

    out: list[tuple[str, str]] = []
    msg = getattr(context, "message", None)
    for p in getattr(msg, "parts", None) or []:
        mt = (getattr(p, "media_type", "") or "").lower()
        if not mt.startswith("image/"):
            continue
        raw = getattr(p, "raw", b"") or b""
        if raw:
            out.append((mt, f"data:{mt};base64,{base64.b64encode(bytes(raw)).decode()}"))
        elif getattr(p, "url", ""):
            out.append((mt, p.url))
    return out


def _extract_skill_hint(context: RequestContext) -> str:
    """The ``skillHint`` the caller set to invoke a specific skill — the
    structured-finalizer dispatch (A2A has no skill field on the message).
    '' when absent."""
    hint = _request_metadata(context).get("skillHint")
    return hint if isinstance(hint, str) else ""


def _tool_call_part(event_type: str, payload: Any) -> Part | None:
    """Build a tool-call-v1 DataPart from a tool_start/tool_end event.

    Structured dict payloads ({id,name,input|output}) become a typed
    tool-call-v1 part; a plain-string payload (legacy producers) becomes a
    plain text status part so text-only consumers still see progress.
    """
    if isinstance(payload, dict):
        errored = event_type == "tool_end" and bool(payload.get("error"))
        # A failed tool end carries phase="failed" (+ the error text) so the card
        # renders the X glyph instead of the green "done".
        phase = "started" if event_type == "tool_start" else ("failed" if errored else "completed")
        kwargs: dict[str, Any] = {}
        if event_type == "tool_start" and payload.get("input") is not None:
            kwargs["args"] = payload.get("input")
        if event_type == "tool_end" and payload.get("output") is not None:
            kwargs["result"] = payload.get("output")
        if errored:
            kwargs["error"] = str(payload.get("output") or "tool failed")
        emit = pa.emit_tool_call(
            str(payload.get("id", "")),
            str(payload.get("name", "")),
            phase,
            **kwargs,
        )
        # A subagent's tool frame carries its parent delegation's tool-call id so the
        # console nests it under the `task` card by id (the contract dict has no field
        # for it, so ride it as an extra payload key the console reads).
        if payload.get("parentId"):
            emit["content"]["value"]["parentToolCallId"] = str(payload["parentId"])
        return _ext_data_part(emit)
    if payload:
        return _text_part(str(payload))
    return None


_CONTEXT_MIME = "application/vnd.protolabs.context-v1+json"


def _terminal_parts(
    text: str,
    deltas: list[dict],
    usage: dict | None,
    cost_usd: float,
    confidence: float | None,
    confidence_expl: str | None,
    context: dict | None = None,
    *,
    success: bool,
    duration_ms: int | None = None,
) -> list[Part]:
    """Assemble the terminal artifact's parts: text first, then the cost /
    confidence / worldstate-delta / context extension DataParts that have content.

    Mirrors the hand-rolled handler's ``_terminal_artifact_parts`` ordering
    (text → worldstate → cost → confidence) so consumers reading parts in
    order are unchanged; the context-v1 part trails as a pure append.
    """
    parts: list[Part] = []
    if text:
        parts.append(_text_part(text))
    if deltas:
        parts.append(_ext_data_part(pa.emit_worldstate_delta(deltas)))
    if usage and (usage.get("input_tokens", 0) or usage.get("output_tokens", 0)):
        parts.append(
            _ext_data_part(
                pa.emit_cost(
                    usage,
                    duration_ms=duration_ms,
                    cost_usd=round(cost_usd, 6) if cost_usd > 0 else None,
                    success=success,
                )
            )
        )
    if confidence is not None:
        parts.append(
            _ext_data_part(
                pa.emit_confidence(
                    confidence,
                    explanation=confidence_expl,
                    success=success,
                )
            )
        )
    # Compaction context-window readout (#1372) — a template extension (no SDK helper),
    # built with the generic DataPart packer. Only when we actually measured a prompt.
    if context and context.get("contextTokens"):
        parts.append(_ext_data_part(pa.data_part(context, _CONTEXT_MIME)))
    return parts
