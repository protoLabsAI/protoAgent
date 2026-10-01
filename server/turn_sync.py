"""The non-streaming turn driver — extracted from ``server/chat.py`` (#3917, epic #3804).

``_chat_langgraph_impl`` is the driver behind ``chat()`` (OpenAI-compat ``/v1``,
``/api/chat``, plugin ``HOST.invoke()``): the shared pre-turn dispatch, the ACP switch,
one native graph turn (``_native_turn``), the goal drive, and the context-overflow
compact-and-retry. ``_native_turn`` and ``_last_ai`` used to be closures inside it; they
are module-level here, with every value they closed over passed explicitly. The helper
only it used moved with it: ``_trace_reply_output``. Symmetric with
``server/turn_stream.py`` (#3874) for the streaming loop.

The telemetry / idle-beacon wrapper ``_chat_langgraph`` stays in ``server.chat`` (``chat()``
calls it by bare name and many tests patch it there) and **reaches the impl through this
module at call time** (``_turn_sync._chat_langgraph_impl``), so a test's patch here is
what runs.

**Collaborators that stay in ``server.chat``** — ``this_turn_messages``,
``_last_tool_text``, the HITL interrupt/resume readers (``_interrupt_payload`` /
``_resume_payload`` / ``_pending_interrupt_value``), ``_vision_human_message``, the
failure handling (``_overflow_compacted`` / ``_fail_turn`` / ``turn_error`` /
``_OVERFLOW_RETRY_PROMPT``) and ``_set_trace_output`` — are read through ``server.chat``
at CALL time (``_chat().<name>``), never bound at import: a patch on ``server.chat`` still
lands, and ``import server.turn_sync`` has no import-time edge back into it. Turn control,
dispatch, ACP, goal loop, telemetry and the fence update are reached through their owning
modules (``_turn_control.<name>`` etc.), as they were in ``server.chat``.

``server.chat`` re-exports ``_chat_langgraph_impl`` and ``_trace_reply_output`` so
``server.chat.<name>`` keeps resolving. Patch these names HERE, not on ``server.chat``: a
re-export is a copy of the binding, so a ``setattr`` there intercepts nothing
(``tests/test_turn_sync_seam.py`` guards it).
"""

import contextlib
import importlib
import logging
from typing import Any

from graph.middleware.redaction import redact as _redact
from graph.output_format import extract_output
from runtime.state import STATE
from server import chat_acp as _chat_acp
from server import chat_dispatch as _chat_dispatch
from server import goal_loop as _goal_loop
from server import turn_control as _turn_control
from server import turn_stream as _turn_stream
from server import turn_telemetry as _turn_telemetry

# Same logger as server.chat, so the moved log lines keep their channel.
log = logging.getLogger("protoagent.server")


def _chat():
    """``server.chat`` the MODULE, resolved at call time — by path, because ``server``
    re-exports the ``chat`` FUNCTION under the submodule's name."""
    return importlib.import_module("server.chat")


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
            _chat()._set_trace_output(content if isinstance(content, str) else str(content or ""))
            return


def _last_ai(result) -> str:
    """The last assistant text of THIS turn (was a closure in ``_chat_langgraph_impl``)."""
    from langchain_core.messages import AIMessage

    # Bounded to THIS turn (#2300). An unbounded reverse scan over the
    # accumulated conversation returns the PREVIOUS turn's answer whenever
    # this one produced no assistant message — which is exactly what a turn
    # whose stream dies after its first chunk looks like.
    for msg in reversed(_chat().this_turn_messages(result)):
        if isinstance(msg, AIMessage) and msg.content:
            # `.text` flattens Responses-API content blocks (openai-codex,
            # ADR 0097) to a string; a plain string passes through unchanged.
            return msg.content if isinstance(msg.content, str) else msg.text
    return ""


def _input_needed(interrupt_val) -> str:
    """The reply line for a turn parked at a HITL interrupt: this surface has no task to
    park on, so it echoes the ask; the caller answers with a follow-up message."""
    payload = _chat()._interrupt_payload(interrupt_val)
    question = payload.get("question") or payload.get("title") or "The agent needs input to continue."
    return f"🙋 **Input needed:** {question}"


async def _native_turn(
    turn_message: str,
    turn_images: list[tuple[str, str]] | None,
    *,
    session_id: str,
    config: dict,
    usage_cb: Any,
    state_extra: dict[str, Any],
    tool_fence: list[str] | None,
    hitl_resume: bool,
    incognito: bool,
    overflow_retry: bool = False,
    origin: str = "local",
    telemetry_sink: dict[str, Any] | None = None,
    model_notice: str = "",
) -> list[dict[str, Any]]:
    """One native turn on this session's thread, as the reply list. Run once
    for the operator's message and, after a context-overflow compaction, once
    more for the recovery prompt (#3805).

    Was a closure inside ``_chat_langgraph_impl`` (#3917); everything it closed over is
    now an explicit keyword: ``session_id`` / ``config`` / ``usage_cb`` /
    ``state_extra`` (the impl's ``_state_extra``) / ``tool_fence`` / ``hitl_resume`` /
    ``incognito``. ``_last_ai`` is module-level; ``goal_turn`` and ``HumanMessage`` are
    imported here (the impl imported them at its top).

    ``origin`` is the surface ``chat()`` was called from (#3891 F2). It is this driver's
    request metadata for the two decisions the streaming driver makes from its own: an
    autonomous origin (``server.turn_control._is_autonomous``) auto-answers a HITL pause
    and is exempt from the hold, exactly as a streaming turn from that origin is; the
    operator surfaces (``local`` / ``api-chat`` / ``v1`` / ``plugin``) stay attended.

    ``telemetry_sink`` is the wrapper's telemetry sink: a turn that ends parked at a HITL
    ask (or held behind one) stamps ``state = "input_required"`` on it, so its row says
    what the A2A surface's does for the same park instead of ``completed`` (#3945).
    """

    def _parked() -> None:
        if telemetry_sink is not None:
            telemetry_sink["state"] = "input_required"

    from langchain_core.messages import HumanMessage

    from graph.goals.goal_turn import goal_turn

    # When a goal is already active, the whole turn is goal-driven —
    # suppress cross-session prior_sessions on the initial turn too.
    _goal_state = _goal_loop.active_goal(session_id)
    goal_active = _goal_state is not None
    if goal_active:
        # A goal-driven turn also runs under the fence of the turn that SET the goal.
        tool_fence = _goal_loop.goal_fenced(_goal_state, tool_fence)
        state_extra = {**state_extra, "subagent_fence": tool_fence}
    # The streaming driver's request metadata, as far as this surface has it (#3891 F2):
    # the origin (autonomy) and the operator's HITL-answer marker.
    turn_metadata: dict[str, Any] = {"origin": origin}
    if hitl_resume:
        turn_metadata["hitl_resume"] = True
    # Sharing the streaming thread means sharing its serialization contract:
    # every other writer to `a2a:{sid}` (the streaming turn driver,
    # compact_session, rewind_session) holds the per-thread lock — an
    # unlocked graph turn here could lost-update a concurrent one (e.g. the
    # desktop /api/chat fallback racing a console /compact on the same tab).
    auto = _goal_loop.HitlAutoAnswer(_goal_loop.is_autonomous_turn(turn_metadata, goal_active=goal_active))
    async with _turn_control._thread_lock(config["configurable"]["thread_id"]):
        # HITL hold (#1560) — same contract as the streaming path: while the
        # thread is parked at a form/question/approval interrupt, hold a fresh
        # operator message (it folds in right after the form response) and echo
        # the pending ask; the marked answer resumes the graph properly.
        # The overflow retry skips it, as the streaming retry does: its message
        # is the recovery prompt, not an operator message to hold.
        hold = (
            None
            if overflow_retry
            else await _turn_control._hold_if_hitl_pending(
                turn_message,
                session_id,
                config,
                request_metadata=turn_metadata,
                fence=tool_fence,
            )
        )
        if hold is not None and hold is not _turn_control._HITL_RESUME:
            _parked()
            payload = _chat()._interrupt_payload(hold)
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
        if hold is _turn_control._HITL_RESUME:
            from langgraph.types import Command

            # A resume carries no fresh input, so the fence rides the
            # Command's state update: a fenced caller's answer never resumes
            # a pass with a wider toolset — it runs under the INTERSECTION of
            # its fence and the parked turn's (narrowest wins). Unfenced, the
            # parked turn keeps its own (a resume continues that turn; it does
            # not clear it).
            graph_input = Command(
                resume=await _chat()._resume_payload(config, turn_message),
                **(await _turn_stream._resume_fence_update(config, tool_fence)),
            )
        else:
            # Kickoff injection (#1910) — shared with the streaming driver
            # (server/goal_loop.py); this branch is never a HITL resume, and the
            # overflow retry's recovery prompt is never wrapped (#3891 F1).
            _msg = _goal_loop.kickoff_message(_goal_state, turn_message, resume=False, overflow_retry=overflow_retry)
            graph_input = {
                # Vision parts ride the user message when the model supports
                # them (#1943) — same gating as the streaming path.
                "messages": [
                    _chat()._vision_human_message(_msg, turn_images, session_id=session_id, incognito=incognito)
                ],
                "session_id": session_id,
                **state_extra,
            }
        with goal_turn(goal_active) as goal_pass:
            result = await STATE.graph.ainvoke(graph_input, config=config)
            # Headless-first parity (#1911), the shared policy (server/goal_loop.py): a
            # goal-driven turn, or one from an autonomous origin (#3891 F2), has no
            # operator here to answer a HITL park — resume (keyed by interrupt id, #3872)
            # with the no-operator sentinel and re-run, bounded, then clear. Attended
            # turns are untouched (they still echo the ask below).
            result = await auto.settle(
                config, result, lambda cmd: STATE.graph.ainvoke(cmd, config=config)
            )
    raw = _last_ai(result)
    response = extract_output(raw)

    # A turn parked at a HITL interrupt (ask_human / a form / an approval) — checked
    # whether or not the turn also produced text (#3931). There's no task to park on
    # this non-streaming surface, so echo the ask after any text the turn wrote; the
    # caller answers with a follow-up message, which resumes the thread (the
    # checkpointer kept the history). It used to be checked only for an EMPTY reply: a
    # turn that wrote "Let me check…" and then asked returned the text alone, the
    # question never reached the caller, and a goal set during that turn was then
    # driven (below) into a thread still waiting for its answer. Returning here is the
    # streaming driver's `turn["paused"]` stop: no goal drive past a parked interrupt.
    # An autonomous turn never parks — ``auto.settle`` above answered or cleared its
    # asks — so, as for a continuation, only an attended turn is checked.
    interrupt_val = None if auto.autonomous else await _chat()._pending_interrupt_value(config)
    if interrupt_val is not None:
        _parked()
        return [
            {
                "role": "assistant",
                "content": (f"{response}\n\n" if response else "") + _input_needed(interrupt_val),
                "usage": _turn_telemetry.sum_usage(usage_cb.usage_metadata),
            }
        ]

    # Robustness parity with the streaming path (bd-2qy): a turn can end
    # with no assistant text — after a `wait` yield, or on a scratch-only turn.
    # Returning "" gives /api/chat + OpenAI-compat callers a silent empty 200;
    # surface something useful: fall back to the last tool result so the caller gets a signal, not a blank.
    # Both lookups are scoped to THIS turn, so reaching the final string means
    # the turn genuinely produced nothing — most likely it died mid-stream. Say
    # that plainly: the whole point of #2300 is that a caller must be able to
    # tell "no answer" from "an answer", and the previous wording read like a
    # deliberate quiet turn rather than a failure worth retrying.
    no_reply = ""
    if not response:
        response = _chat()._last_tool_text(result)
    if not response:
        no_reply = response = (
            "**Error:** the turn produced no reply — it may have stalled or been "
            "interrupted. Nothing was returned for this request; retry it. "
            "(This is not the previous turn's answer.)"
        )

    # Goal mode (shared drive, server/goal_loop.py): verify after the agent
    # stops; run each continuation it asks for. No status surface here — the
    # verifier notes are skipped and only the terminal note reaches the reply.
    drive = _goal_loop.GoalDrive(session_id, config, response)
    drive.notice = model_notice  # the goal's model fell back to the default (#3957)
    drive.last_pass = goal_pass  # a round-capped pass pauses the drive (#3957)
    # This driver locks the thread per pass, not across the drive: the pause note's
    # checkpoint write takes the lock itself, like any other writer to the thread.
    drive.note_lock = lambda: _turn_control._thread_lock(config["configurable"]["thread_id"])
    # The fence each continuation runs under: the turn's own, narrowed by any fenced
    # message an earlier pass folded in (steering, #2972) — refreshed from the previous
    # pass's checkpoint, never the turn's original (wider) one. Same as the streaming driver.
    cont_fence = list(state_extra.get("subagent_fence") or [])
    last_config = config
    async with contextlib.aclosing(drive.steps()) as _goal_steps:
        async for step in _goal_steps:
            if isinstance(step, _goal_loop.GoalNote):
                continue
            # ...and a continuation always drives the (possibly just-set) goal: its fence too.
            cont_fence = _goal_loop.goal_fenced(
                _goal_loop.active_goal(session_id), await _turn_stream._carried_fence(last_config, cont_fence)
            )
            # Fresh-context iterations get a scoped config without the turn's
            # callbacks — re-attach usage_cb so their tokens count.
            cont_config = {**step.config, "callbacks": [usage_cb]}
            last_config = cont_config
            # Lock the BASE thread (mirrors the streaming driver, which holds it
            # across the whole goal loop): same-session iterations write `config`'s
            # thread directly; fresh-context ones still exclude compact/rewind/
            # streaming turns keyed on the base id.
            async with _turn_control._thread_lock(config["configurable"]["thread_id"]):
                with goal_turn() as step.goal_pass:
                    result = await STATE.graph.ainvoke(
                        {
                            "messages": [HumanMessage(content=step.message)],
                            "session_id": session_id,
                            **state_extra,
                            "subagent_fence": cont_fence,
                        },
                        config=cont_config,
                    )
                    # An interrupt INSIDE a continuation gets the initial turn's handling
                    # (#3891 F4) — it used to be dropped: the same policy and budget
                    # auto-answer it when the turn is autonomous...
                    result = await auto.settle(
                        cont_config, result, lambda cmd: STATE.graph.ainvoke(cmd, config=cont_config)
                    )
                    # ...and an attended turn stops the drive and surfaces the ask, as the
                    # streaming driver parks on it.
                    interrupt_val = None if auto.autonomous else await _chat()._pending_interrupt_value(cont_config)
            step.text = extract_output(_last_ai(result))
            if interrupt_val is not None:
                _parked()
                return [
                    {
                        "role": "assistant",
                        "content": (f"{step.text}\n\n" if step.text else "") + _input_needed(interrupt_val),
                        "usage": _turn_telemetry.sum_usage(usage_cb.usage_metadata),
                    }
                ]
    response = drive.text

    reply = {"role": "assistant", "content": response, "usage": _turn_telemetry.sum_usage(usage_cb.usage_metadata)}
    # A turn that produced nothing is a FAILED turn, not an answer that happens to
    # start with "**Error:**" (#3873): carry the structured `error` like every other
    # failure return, so telemetry counts it failed and /v1 answers an error status.
    # Unless a goal continuation replaced the text with a real reply.
    if no_reply and response.startswith(no_reply):
        reply["error"] = _chat().turn_error(None, "the turn produced no reply — it may have stalled or been interrupted; retry it")
    return [reply]


async def _chat_langgraph_impl(
    message: str,
    session_id: str,
    *,
    model: str | None = None,
    incognito: bool = False,
    hitl_resume: bool = False,
    images: list[tuple[str, str]] | None = None,
    tool_fence: list[str] | None = None,
    origin: str = "local",
    _telemetry_sink: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Non-streaming LangGraph entry — used by the console + OpenAI-compat.

    ``_telemetry_sink`` (private, set by the ``_chat_langgraph`` wrapper) receives
    this turn's usage callback so the wrapper can write the telemetry row from its
    single exit point rather than at each of this function's many returns (#3000).
    ``origin`` (the ``chat()`` surface) decides whether the turn is autonomous (#3891 F2).
    """
    from observability import tracing

    # The turn's EFFECTIVE model pick (#3957): the request's own, else an active goal's —
    # test-built, falling back to the default with a notice when it can't be. The lead
    # graph's stamp, the pre-turn chain and `turn_model_scope` all use it.
    requested_model = (model or "").strip()
    model, model_notice = await _goal_loop.resolve_turn_model(session_id, requested_model)
    # Per-turn model override (ModelOverrideMiddleware reads state["model"]).
    # Incognito is stamped explicitly every turn (the channel persists in the
    # checkpointer — an omitted key would inherit the previous turn's value).
    # Stamped EVERY turn, like incognito below (#3957): `model` is a checkpointed channel,
    # so omitting it on a no-pick turn inherited the previous turn's pick — and a pick that
    # can no longer be built then failed every later turn on the chat, with no way back
    # ("Default" sends nothing). "" = the configured default.
    _state_extra: dict[str, Any] = {"model": (model or "").strip()}
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

    from graph.subagent_model import inherited_pick_scope, turn_model_scope

    # The turn's model override is bound for the whole turn (#3955): the pre-turn chain's
    # `/<workflow>` steps and any plugin tool reaching `graph.sdk.run_subagent` /
    # `spawn_background` read it there — they never see `state["model"]`.
    async with (
        tracing.trace_session(
            session_id=session_id,
            name="chat",
            metadata={"soul_rev": soul_revision(), **({} if incognito else {"message_preview": _redact(message[:100])})},
            input=_redact(message),
            incognito=bool(incognito),
        ),
        turn_model_scope(model),
        # An INHERITED pick (the goal's, not this request's) falls back to the default if
        # its provider rejects it mid-turn, instead of failing every re-drive (#3957).
        inherited_pick_scope(model if model != requested_model else ""),
    ):
        if _telemetry_sink is not None:
            # The trace id, read HERE while the scope is open (#3945): the wrapper writes
            # the telemetry row after this `async with` has exited and reset the
            # contextvar, so reading it there always gave "" — every /v1 and /api/chat
            # row lost its link to its Langfuse trace. Same capture-during-the-turn rule
            # as the A2A executor's `_capture_trace_id`.
            _telemetry_sink["trace_id"] = tracing.current_trace_id() or ""
            # The model the caller asked for (#3957): what the row names when no model call
            # reported one — a failed turn, a short-circuit — instead of the configured
            # default, which is what ran only when nothing was requested.
            _telemetry_sink["requested_model"] = requested_model
        # Set only once the NATIVE turn is about to run — the overflow recovery below
        # compacts + retries that thread (same contract as the streaming driver, #3805).
        native_tid: str | None = None
        try:
            # The pre-turn dispatch chain is SHARED with the streaming driver (#3805) —
            # see _pre_turn_dispatch. This surface can't render the intermediate frames
            # (work cards, room replies, a /goal SET ack — that one is folded into the
            # turn's terminal goal note), so only the terminal frame becomes the reply.
            # No request_metadata on this driver — the thread resolves from the session
            # id alone, as it does everywhere else in this function. The model override
            # rides `turn_model` instead, so a `/<subagent>` or `/<workflow>` run follows
            # it here as it does on the streaming driver (#3955).
            pre = _chat_dispatch._PreTurn(
                message, fenced=bool(tool_fence), fence=list(tool_fence or []), turn_model=(model or "").strip()
            )
            last_frame: tuple | None = None
            delegated_usage: list[dict] = []
            try:
                async with contextlib.aclosing(
                    _chat_dispatch._pre_turn_dispatch(pre, session_id, None)
                ) as _pre_frames:
                    async for frame in _pre_frames:
                        if frame and frame[0] == "usage":
                            # A `/<subagent>` / `/<workflow>` run's model calls (#3957) — this
                            # surface renders no frames, but its telemetry row bills them.
                            delegated_usage.append(frame[1])
                            continue
                        last_frame = frame
            finally:
                # Handed over even when the dispatch raised: a failed slash run's row
                # still bills what it spent before it failed.
                if _telemetry_sink is not None and delegated_usage:
                    _telemetry_sink["delegated_usage"] = delegated_usage
            if pre.handled:
                if _telemetry_sink is not None:
                    # A short-circuit (slash command, @-address, /goal control…) is a turn
                    # the A2A surface records a row for — `completed`, or `input_required`
                    # for a plugin form — so this surface does too (#3945).
                    _telemetry_sink["short_circuit"] = True
                    if last_frame is not None and last_frame[0] == "input_required":
                        _telemetry_sink["state"] = "input_required"
                    elif last_frame is not None and last_frame[0] == "error":
                        # A short-circuit that ended FAILED — a `/<workflow>` with a failed
                        # step (#3957) — is a failed row here too, as on the A2A surface.
                        _telemetry_sink["state"] = "failed"
                return _traced(_chat_dispatch._short_circuit_reply(last_frame))
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
                "configurable": {"thread_id": _turn_control._resolve_thread_id(None, session_id)},
                "callbacks": [usage_cb],
                "recursion_limit": getattr(STATE.graph_config, "max_iterations", 200),
            }

            native_tid = config["configurable"]["thread_id"]
            return _traced(
                await _native_turn(
                    message,
                    images,
                    session_id=session_id,
                    config=config,
                    usage_cb=usage_cb,
                    state_extra=_state_extra,
                    tool_fence=tool_fence,
                    hitl_resume=hitl_resume,
                    incognito=incognito,
                    origin=origin,
                    telemetry_sink=_telemetry_sink,
                    model_notice=model_notice,
                )
            )
        except Exception as e:
            # Context overflow (#2783, ADR 0101 D4) — the recovery the streaming driver
            # always had and this one lacked (#3805): force-compact the thread once and
            # retry a single time; a second failure surfaces honestly below.
            if await _chat()._overflow_compacted(e, native_tid, session_id):
                try:
                    # The retry is the same turn: it keeps the fence the failed pass ran
                    # under, narrowed by anything it folded in — never a wider one.
                    retry_fence = await _turn_stream._carried_fence(config, tool_fence)
                    return _traced(
                        await _native_turn(
                            _chat()._OVERFLOW_RETRY_PROMPT,
                            None,
                            session_id=session_id,
                            config=config,
                            usage_cb=usage_cb,
                            state_extra={**_state_extra, "subagent_fence": retry_fence},
                            tool_fence=retry_fence,
                            hitl_resume=hitl_resume,
                            incognito=incognito,
                            overflow_retry=True,
                            origin=origin,
                            telemetry_sink=_telemetry_sink,
                            model_notice=model_notice,
                        )
                    )
                except Exception as retry_exc:  # noqa: BLE001 — second failure surfaces honestly
                    log.exception("[chat] overflow retry failed for session=%s: %s", session_id, retry_exc)
                    e = retry_exc
            msg = await _chat()._fail_turn(e, session_id, tag="chat", thread_id=native_tid)
            return _traced([{"role": "assistant", "content": f"**Error:** {msg}", "error": _chat().turn_error(e, msg)}])
        finally:
            tracing.flush()
