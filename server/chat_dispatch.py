"""The shared pre-turn dispatch chain — extracted from ``server/chat.py`` (#3861).

Both turn drivers — the streaming ``_chat_langgraph_stream_impl`` in ``server.chat``
(A2A / console) and the non-streaming ``_chat_langgraph_impl`` in ``server.turn_sync`` (``chat()``:
OpenAI-compat /v1, /api/chat, plugin surfaces) — run ONE chain before the turn
(#3805): @-mention → /goal → /lifecycle → plugin command → workflow → subagent →
skill (rewrite, falls through) → unknown /command → ACP switch. A fenced turn
(#2972) skips every short-circuit (and fails closed on an ACP runtime, #3812). So does
any turn in a session whose ACTIVE goal a fenced turn set (``GoalState.fence``) — that
turn is goal-driven under the goal's fence — except ``/goal`` itself, so the operator
can always check, replace or clear the goal.

**The drivers stay in ``server.chat``** and call ``_PreTurn`` / ``_pre_turn_dispatch``
/ ``_short_circuit_reply`` through this module (``_chat_dispatch.<name>``) at call time.

**Collaborators resolve at CALL time through the module that owns them** — the room
exchange through ``server.chat_rooms``, the thread lock/resolver through
``server.turn_control``, the workflow/subagent/skill parsing + runners through
``server.chat_commands``, the tool-input coercion through ``server.chat`` — never bound
at import, so a test's patch on the owner is what runs, and ``import
server.chat_dispatch`` works standalone with no import-time edge back into
``server.chat``.

``server.chat`` re-exports every name here so ``from server.chat import _PreTurn``
keeps resolving. Patch these names HERE, not on ``server.chat``: a re-export is a copy
of the binding, so a ``setattr`` there intercepts nothing
(``tests/test_chat_dispatch_seam.py`` guards it).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
from dataclasses import dataclass
from typing import Any

from graph import delegation_usage
from graph.output_format import extract_output
from graph.subagent_model import turn_model_scope

# Bound at import, exactly as ``server.chat`` binds them: the plugin chat-command
# dispatch and the shared slash resolver (``graph.slash_commands`` is re-imported on a
# plugin reload, so these follow ``server.chat``'s existing binding semantics).
from graph.slash_commands import (
    PluginFormRequest as _PluginFormRequest,
    run_plugin_chat_command as _run_plugin_chat_command,
    slash_kind as _slash_kind,
)
from runtime.state import STATE
from server import chat_commands as _chat_commands
from server import chat_rooms as _chat_rooms
from server import turn_control as _turn_control

# Same logger as server.chat, so the moved log lines keep their channel.
log = logging.getLogger("protoagent.server")


# How long an abandoned workflow runner gets to settle after its cancel (#3933) — the
# same bound, for the same reason, as ``server.chat_acp._ACP_CANCEL_SETTLE_S``: the
# caller's own cleanup (and the next turn behind it) waits on it.
_WORKFLOW_CANCEL_SETTLE_S = 10.0


async def _stop_abandoned_workflow(runner: asyncio.Task, wf_name: str) -> None:
    """Cancel an abandoned ``/workflow`` runner and wait (bounded) for it to stop, so the
    dispatch generator's close returns only once the workflow has actually ended. Never
    raises for the runner's own outcome; a cancel of the CALLER while waiting propagates.
    Mirrors ``server.chat_acp._stop_abandoned_driver`` (#3837)."""
    runner.cancel()
    done, _ = await asyncio.wait({runner}, timeout=_WORKFLOW_CANCEL_SETTLE_S)
    if not done:
        log.warning(
            "[workflow] abandoned /%s run did not stop within %gs of cancel — releasing anyway",
            wf_name,
            _WORKFLOW_CANCEL_SETTLE_S,
        )
        # Nobody awaits it now: retrieve its eventual outcome so a late failure doesn't
        # surface only as asyncio's "Task exception was never retrieved" at GC.
        runner.add_done_callback(lambda t: t.cancelled() or t.exception())
        return
    if runner.cancelled():
        log.info("[workflow] abandoned /%s run cancelled (its turn ended early)", wf_name)
    elif runner.exception() is not None:
        log.debug("[workflow] abandoned /%s run ended with: %r", wf_name, runner.exception())


def _chat():
    """``server.chat`` the MODULE, resolved at call time — by path, because ``server``
    re-exports the ``chat`` FUNCTION under the submodule's name."""
    return importlib.import_module("server.chat")


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
    name, _rest = _chat_commands._parse_slash_command(message)
    if not name or _SLASH_TOKEN_RE.fullmatch(name) is None:
        return None
    if _slash_kind(name) is not None:
        return None
    return f"Unknown command /{name}. Type / to see available commands."


# The reply to `/goal …` when goal mode is off (#3929). Names the config key
# (`goal.enabled`, GraphConfig.goal_enabled) since it isn't a visible Settings field.
GOAL_MODE_DISABLED_REPLY = (
    "Goal mode is off for this agent, so `/goal` does nothing here. To enable it, set "
    "`goal.enabled: true` in the agent's `langgraph-config.yaml` and restart the agent."
)


def _goal_disabled_reply(message: str) -> str | None:
    """:data:`GOAL_MODE_DISABLED_REPLY` if ``message`` is a ``/goal`` command, else ``None``.

    Only consulted when ``STATE.goal_controller`` is ``None`` (goal mode disabled). Uses
    the same ``_slash_kind`` resolution that reserves ``goal`` from the unknown-command
    catch, so the two can't disagree about what counts as ``/goal``."""
    name, _rest = _chat_commands._parse_slash_command(message)
    if not name or _slash_kind(name) != "goal":
        return None
    return GOAL_MODE_DISABLED_REPLY


def _lifecycle_command_reply(message: str) -> str | None:
    """If ``message`` is the core ``/lifecycle`` command (ADR 0074), return its read-only
    listing — the three lifecycle events plus the currently-configured config reactions and
    registered plugin hooks. Else ``None`` (fall through). Reserved like ``/goal``, so no
    plugin/workflow/skill can shadow it; listing only (the config file is the source of
    truth for v1 — no runtime mutation)."""
    name, _rest = _chat_commands._parse_slash_command(message)
    if not name or _slash_kind(name) != "lifecycle":
        return None
    from graph.lifecycle import describe

    return describe()


# The answer to a fenced turn (#2972) on an ACP runtime: the fence can't be enforced there,
# so the turn is refused rather than run with the external agent's full toolset.
_FENCED_ACP_REFUSAL = (
    "I can't act on this message here: it came through a restricted channel, and this agent "
    "runs on an external coding runtime that can't enforce that channel's tool limits. "
    "Ask the operator to handle it directly."
)

# The answer to a turn in a session whose active goal carries a tool fence, on an ACP
# runtime: every turn there drives that goal, and the fence can't be enforced on the
# external runtime. `/goal clear` (never refused) returns the session to normal.
_GOAL_FENCED_ACP_REFUSAL = (
    "I can't run this here: this session's active goal runs with a restricted tool scope, "
    "and this agent runs on an external coding runtime that can't enforce it. Clear the "
    "goal with `/goal clear` to continue in this session."
)

# The status line a turn in such a session gets when its text looked like a command
# (a `/command` or an `@`-mention): it isn't run — the text goes to the goal-driven turn.
_GOAL_FENCED_COMMANDS_PAUSED = (
    "⏸ Commands are paused while this goal runs with a restricted tool scope — "
    "`/goal clear` to resume."
)


def _looks_like_command(message: str) -> bool:
    """A `/command` (the same token shape the unknown-command hint uses) or a leading
    `@`-mention — the text a short-circuit would have claimed."""
    name, _rest = _chat_commands._parse_slash_command(message)
    if name and _SLASH_TOKEN_RE.fullmatch(name) is not None:
        return True
    return (message or "").lstrip().startswith("@")


def _active_goal_fence(session_id: str) -> list[str]:
    """The tool fence of the session's ACTIVE goal — the fence of the turn that set it
    (``GoalState.fence``). ``[]`` when there's no active goal or it was set unfenced."""
    from server import goal_loop as _goal_loop

    return _goal_loop.goal_fenced(_goal_loop.active_goal(session_id), [])


async def _goal_control(pre: _PreTurn, message: str, session_id: str):
    """The ``/goal`` step of the chain: status / clear / set short-circuit the turn
    (``pre.handled``); a successful SET yields its ack and falls through into the
    goal-driven turn it kicks off (#1910)."""
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
    elif (_goal_off := _goal_disabled_reply(message)) is not None:
        # Goal mode off (#3929): `/goal` is still reserved (the unknown-slash catch exempts
        # it), so without this it fell through to the model, which invented an answer
        # ("Goals cleared."). Answer deterministically instead, on both drivers.
        pre.handled = True
        yield ("done", _goal_off)


@dataclass
class _PreTurn:
    """Mutable outcome of :func:`_pre_turn_dispatch` (an async generator can't
    return a value, so the caller reads this after draining it).

    ``message`` — the text the turn should run on (a `/skill` rewrites it).
    ``handled`` — a short-circuit answered the turn; its terminal frame
    (``done`` / ``input_required``) was the last one yielded.
    ``acp`` — not handled, and the configured runtime is ACP (ADR 0033): the
    driver runs its own ACP shape instead of the native loop.
    ``fenced`` — the turn runs under a tool fence: the caller's own ``tool_fence``
    (#2972), or — set by the chain itself — the fence of the session's active goal. No
    short-circuit runs (a goal-fenced turn still runs ``/goal``).
    ``fence`` — that fence (for logging).
    ``acp_exempt`` — a fenced turn the ACP refusal does NOT apply to: only the background
    manager's own detached subagent job (#1639), proven by its single-use fire token
    (``background/fire_auth.py``). It still skips every short-circuit. Default: refused.
    ``turn_model`` — the turn's model override, for a driver that has it outside
    ``request_metadata`` (the non-streaming ``/api/chat`` + ``/v1`` driver, which passes
    no metadata). Blank → ``request_metadata["model"]``. A ``/<subagent>`` or
    ``/<workflow>`` short-circuit runs under it (#3955).
    """

    message: str
    handled: bool = False
    acp: bool = False
    fenced: bool = False
    fence: list[str] | None = None
    acp_exempt: bool = False
    turn_model: str = ""


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
    # The turn's model override (the console tab's pick / the /v1 `model`), from whichever
    # form the driver has it in (#3955): the non-streaming driver passes no metadata.
    turn_model = (pre.turn_model or "").strip() or str((request_metadata or {}).get("model") or "").strip()
    # A FENCED turn (#2972 — an untrusted party's message relayed by a plugin
    # surface) runs none of the short-circuits below: each one does work outside
    # the lead turn — a subagent (`/self-improve` can edit the SOUL), a workflow, a
    # plugin command, a delegate exchange, a goal change — where the fence, which
    # only SubagentFenceMiddleware enforces on the lead turn, can't reach it. The
    # text goes to the fenced lead turn verbatim instead.
    if pre.fenced:
        from runtime.acp_runtime import is_acp_runtime

        # FAIL CLOSED on an external (ACP) runtime. The fence is a list of THIS agent's
        # tool names, enforced by SubagentFenceMiddleware on the NATIVE lead turn; an ACP
        # agent (claude-code, codex, …) runs its own toolset, which that list can't
        # describe or restrict — so running the untrusted text there would hand it the
        # external agent's full tools. Refuse the turn with a clear answer instead.
        if is_acp_runtime(STATE.graph_config) and pre.acp_exempt:
            # The background manager's own detached job keeps running on the ACP runtime
            # (it did before fenced streaming turns were refused) — nothing else does.
            log.info(
                "[chat] fenced background job on session %s runs on the ACP runtime (tool_fence=%s)",
                session_id,
                list(pre.fence or []),
            )
            pre.acp = True
            return
        if is_acp_runtime(STATE.graph_config):
            log.warning(
                "[chat] refused a fenced turn (tool_fence=%s) on session %s: this agent runs on an "
                "ACP runtime, which can't enforce the fence",
                list(pre.fence or []),
                session_id,
            )
            yield ("done", _FENCED_ACP_REFUSAL)
            pre.handled = True
        return
    # A turn in a session whose ACTIVE goal a fenced turn set is goal-driven under that
    # goal's fence (server/goal_loop.goal_fenced) — so it is gated exactly as a fenced
    # caller is: no short-circuit (each works outside the fenced lead turn), refused on an
    # ACP runtime (which can't enforce the fence). The one exception is `/goal` itself,
    # run first: the operator must always be able to check, replace or clear the goal.
    # A caller-fenced turn never gets here, so it still can't change the goal.
    _goal_control_ran = False
    if _active_goal_fence(session_id):
        async for frame in _goal_control(pre, message, session_id):
            yield frame
        if pre.handled:
            return
        _goal_control_ran = True
        # Re-read: a `/goal <new>` SET just replaced the goal with the operator's own.
        _goal_fence = _active_goal_fence(session_id)
        if _goal_fence:
            from runtime.acp_runtime import is_acp_runtime

            pre.fenced = True
            pre.fence = _goal_fence
            if not is_acp_runtime(STATE.graph_config) and _looks_like_command(message):
                # Say so, rather than let the command silently become goal text.
                yield ("tool_start", _GOAL_FENCED_COMMANDS_PAUSED)
            if is_acp_runtime(STATE.graph_config):
                log.warning(
                    "[chat] refused a turn on session %s: its active goal is fenced (tool_fence=%s) "
                    "and this agent runs on an ACP runtime, which can't enforce the fence",
                    session_id,
                    _goal_fence,
                )
                yield ("done", _GOAL_FENCED_ACP_REFUSAL)
                pre.handled = True
            return
    # STEP 0 — @-delegate dispatch (S1): a message opening with `@<delegate>`
    # routes straight to that delegate, short-circuiting the LLM turn. Checked
    # BEFORE goal control (and every slash-command below) so an @-mention is
    # never swallowed by an active goal (one with a tool fence gates it above); a no-op when the delegates plugin isn't
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
    _addressed = _chat_rooms._parse_at_delegates(message)
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
        async with _turn_control._thread_lock(_turn_control._resolve_thread_id(request_metadata, session_id)):
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

    # Goal control (/goal status / clear / set) — see _goal_control. Already run above
    # when the session's goal was fenced.
    if not _goal_control_ran:
        async for frame in _goal_control(pre, message, session_id):
            yield frame
        if pre.handled:
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
    name, rest = _chat_commands._parse_slash_command(message)
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
    parsed = _chat_commands._parse_workflow_command(message)
    if parsed is not None:
        wf_name, wf_inputs = parsed
        _WF_DONE = object()
        step_q: asyncio.Queue = asyncio.Queue()

        async def _on_step(event: dict) -> None:
            await step_q.put(event)

        async def _runner() -> str:
            # Each step runs through `graph.sdk.run_subagent`, which reads the turn's model
            # override from this scope — steps follow the turn's model like a `/<subagent>`
            # run does, under the same pin > override > aux > main precedence (#3955).
            # Bound inside the task: it runs in its own copied context.
            try:
                with turn_model_scope(turn_model):
                    return await _chat_commands._run_parsed_workflow(wf_name, wf_inputs, on_step=_on_step)
            finally:
                await step_q.put(_WF_DONE)

        # Bound BEFORE the runner task is created, so its copied context carries the
        # collector: each step's subagent usage lands here (#3957).
        with delegation_usage.collect() as wf_usage:
            runner = asyncio.create_task(_runner())
        finished = False
        try:
            # An umbrella card for the whole workflow, then one per step.
            yield (
                "tool_start",
                {
                    "id": f"workflow:{wf_name}",
                    "name": f"workflow:{wf_name}",
                    "input": _chat()._coerce_tool_value(wf_inputs),
                },
            )
            while True:
                event = await step_q.get()
                if event is _WF_DONE:
                    finished = True
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
        finally:
            # Closed early (#3933) — GeneratorExit at a `yield` or CancelledError at the
            # `get()`. The runner is part of THIS turn, so it ends with the turn: cancel
            # it, never leave it running detached. That matches every other kind of turn
            # here — this generator is closed only when its driver's OWN turn is ending
            # (an A2A CancelTask, the stall guard, the driver itself being closed), and
            # that same close stops a native turn's graph run and an ACP turn
            # (`_stop_abandoned_driver`, #3837). A client merely dropping its SSE does
            # NOT close it: the a2a SDK keeps the producer (`ProtoAgentExecutor.execute`)
            # consuming the stream in the background, so a workflow outlives a
            # disconnect exactly as a native turn does. What must not survive is a
            # runner whose turn is over and whose frames nobody will ever read.
            if not finished:
                await _stop_abandoned_workflow(runner, wf_name)
        try:
            wf_out = await runner
        except Exception:
            # A failed workflow still spent what its finished steps spent (#3957): bill it
            # before the failure propagates. Ordinary exceptions only — a cancellation or
            # a generator close never yields from cleanup.
            for row in _usage_frames(wf_usage):
                yield row
            raise
        yield ("tool_end", {"id": f"workflow:{wf_name}", "name": f"workflow:{wf_name}", "output": wf_out[:300]})
        for row in _usage_frames(wf_usage):
            yield row
        pre.handled = True
        yield ("done", wf_out)
        return

    # Subagent slash command (/<subagent> <prompt>) short-circuits the
    # turn: run the one worker and return its output (ADR 0020 — run from
    # chat). Renders a single tool card. A workflow of the same name wins.
    parsed_sub = _chat_commands._parse_subagent_command(message)
    if parsed_sub is not None:
        sub_type, sub_prompt = parsed_sub
        if not sub_prompt:
            pre.handled = True
            yield ("done", f"Usage: `/{sub_type} <prompt>` — describe the task for the {sub_type} subagent.")
            return
        sub_tool_id = f"subagent:{sub_type}"
        yield ("tool_start", {"id": sub_tool_id, "name": sub_tool_id, "input": sub_prompt})
        # The turn's model override reaches the slash run under the one subagent
        # precedence: its own pin wins over it (#3944) — on every driver (#3955).
        with delegation_usage.collect() as sub_usage:
            try:
                sub_out = await _chat_commands._run_parsed_subagent(
                    sub_type,
                    sub_prompt,
                    session_id=session_id,
                    turn_model=turn_model,
                )
            except Exception:
                # Bill what the run spent before it failed (#3957); see the workflow branch.
                for row in _usage_frames(sub_usage):
                    yield row
                raise
        yield ("tool_end", {"id": sub_tool_id, "name": sub_tool_id, "output": sub_out[:300]})
        for row in _usage_frames(sub_usage):
            yield row
        pre.handled = True
        yield ("done", sub_out)
        return

    # User-facing skill slash command (/<skill> [args]) — does NOT
    # short-circuit: rewrite the message to inject the skill's procedure
    # as a directive, then fall through to the normal lead-agent turn so
    # every streaming / HITL / goal invariant holds (ADR 0052).
    parsed_skill = _chat_commands._parse_skill_command(message)
    if parsed_skill is not None:
        message = _chat_commands._skill_directive(*parsed_skill)

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


def _usage_frames(rows: list[dict]) -> list[tuple[str, dict]]:
    """The short-circuit run's delegated model calls as ``("usage", row)`` frames (#3957).

    One frame per model call, in the shape the turn drivers already account: the
    streaming executor sums them exactly as it sums a ``task`` delegation's custom usage
    events (each row carries ``subagent_type``, which keeps it out of the LEAD thread's
    context-window fill), and the non-streaming driver folds them into its telemetry row.
    Without them a `/<subagent>` or `/<workflow>` turn recorded 0 calls and 0 tokens on
    the configured default model."""
    return [("usage", dict(r)) for r in rows or [] if isinstance(r, dict)]


def _short_circuit_reply(frame: tuple | None) -> list[dict[str, Any]]:
    """The non-streaming shape of a pre-turn short-circuit's terminal frame."""
    kind, payload = frame if frame is not None else ("done", "")
    if kind == "input_required":
        # Non-streaming callers (e.g. the OpenAI-compat /v1 path) can't render a
        # plugin form — degrade to a text note pointing at the console (#1701 S2).
        _title = (payload or {}).get("title") or "This command"
        return [{"role": "assistant", "content": f"**{_title}** needs a form — open it in the protoAgent console."}]
    return [{"role": "assistant", "content": payload}]
