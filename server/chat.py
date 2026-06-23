"""Chat backend — the LangGraph turn loop behind every entry point.

Extracted from ``server/__init__.py`` (ADR 0023, phase 2). This module owns the
non-streaming ``chat`` (the console + OpenAI-compat) and streaming
``_chat_langgraph_stream`` (the A2A handler) turn drivers, the shared
``_run_turn_stream`` event loop, tool-preview/interrupt shaping, and slash-command
parsing + execution for workflows and subagents.

It depends only on neutral modules (``runtime.state``, ``graph.output_format``)
plus function-local imports — nothing from ``server/__init__``, so there is no
import cycle. ``server/__init__.py`` re-exports every public name so
``server.<symbol>`` keeps resolving for the OpenAI-compat / A2A wiring in
``_main`` and for the test suite.
"""

import asyncio
import json
import logging
import time
from typing import Any

from graph.output_format import (
    DROPPED_SCRATCH_KICKER,
    StreamingOutputView,
    StreamingReasoningView,
    extract_confidence,
    extract_output,
    is_dropped_scratch_turn,
)
from runtime.state import STATE

log = logging.getLogger("protoagent.server")


_BG_RESULT_CAP = 6000  # chars of a background result injected into the spawning turn


def _drain_background_messages(session_id: str) -> list:
    """Pull completed background jobs for this session (ADR 0050) and render them as
    ``<task-notification>`` messages to prepend to the turn's input.

    Drains exactly-once (the store flips ``notified`` atomically), so a completion is
    announced to the model on the spawning session's next turn and never again. Returns
    ``[]`` when the manager is absent or nothing is pending — a no-op on a normal turn.
    """
    mgr = getattr(STATE, "background_mgr", None)
    if mgr is None or not session_id:
        return []
    try:
        jobs = mgr.store.drain_pending(session_id)
    except Exception:  # noqa: BLE001 — never break a turn over the drain
        log.exception("[background] drain failed for session %s", session_id)
        return []
    if not jobs:
        return []
    from langchain_core.messages import HumanMessage

    msgs = []
    for j in jobs:
        result = j.result or ""
        if len(result) > _BG_RESULT_CAP:
            result = result[:_BG_RESULT_CAP] + f"\n\n…[truncated to {_BG_RESULT_CAP} chars]"
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
    return msgs


def _resolve_thread_id(request_metadata: dict | None, session_id: str) -> str:
    """Resolve the checkpointer ``thread_id`` for this turn (#571).

    Template default keys A2A sessions by conversation id (``a2a:<session_id>``),
    prefixed to isolate them from the non-streaming chat in the shared checkpointer. A fork
    can register a resolver ``(request_metadata, session_id) -> str`` via a plugin
    (``register_thread_id_resolver``) to scope memory off request metadata — e.g.
    per-project working memory — with ZERO edits to this file. Falls back to the
    default when no resolver is registered or a custom one errors / returns falsy.
    """
    resolver = getattr(STATE, "thread_id_resolver", None)
    if resolver is not None:
        try:
            tid = resolver(request_metadata or {}, session_id)
            if tid:
                return str(tid)
            log.warning("[thread_id] resolver returned falsy; using default")
        except Exception:
            log.exception("[thread_id] custom resolver failed; using default")
    return f"a2a:{session_id}"


# One ACP runtime per thread (the ACP session is stateful — the coding agent holds
# history, so we reuse it across turns; ADR 0033 slice 4).
_ACP_RUNTIMES: dict[str, Any] = {}
_ACP_RUNTIME_ACCESS: dict[str, float] = {}  # thread_id → time.monotonic() last access
_ACP_IDLE_TTL_S = 1800  # 30 min idle before eviction
_ACP_MAX_RUNTIMES = 100  # hard cap — evict LRU when exceeded


async def _evict_acp_runtimes(now: float) -> None:
    """Sweep idle ACP runtimes and enforce the hard cap."""
    # Phase 1 — evict entries older than the idle TTL.
    for tid in list(_ACP_RUNTIMES):
        last = _ACP_RUNTIME_ACCESS.get(tid, 0)
        if now - last >= _ACP_IDLE_TTL_S:
            rt = _ACP_RUNTIMES.pop(tid)
            _ACP_RUNTIME_ACCESS.pop(tid, None)
            await rt.close()
            agent = getattr(rt, "agent", "?")
            log.info("[acp-runtime] evicted idle runtime for thread=%s agent=%s", tid, agent)

    # Phase 2 — if still over cap, evict LRU entries until at or below _ACP_MAX_RUNTIMES.
    while len(_ACP_RUNTIMES) > _ACP_MAX_RUNTIMES:
        lru_tid = min(_ACP_RUNTIME_ACCESS, key=lambda k: _ACP_RUNTIME_ACCESS[k])
        rt = _ACP_RUNTIMES.pop(lru_tid)
        _ACP_RUNTIME_ACCESS.pop(lru_tid, None)
        await rt.close()
        agent = getattr(rt, "agent", "?")
        log.info("[acp-runtime] evicted LRU runtime for thread=%s agent=%s", lru_tid, agent)


async def _get_acp_runtime(thread_id: str):
    now = time.monotonic()
    await _evict_acp_runtimes(now)

    rt = _ACP_RUNTIMES.get(thread_id)
    _ACP_RUNTIME_ACCESS[thread_id] = now  # bump on every call (hit or miss)
    if rt is None:
        from runtime.acp_runtime import AcpRuntime

        rt = AcpRuntime(STATE.graph_config)
        _ACP_RUNTIMES[thread_id] = rt
    return rt


def _setup_required_message() -> list[dict[str, Any]]:
    """Returned by chat endpoints when the wizard hasn't been run.

    The console hides the chat pane until setup completes, but the
    HTTP /api/chat, OpenAI-compat, and A2A endpoints don't know the
    UI state — so they emit a plain-text "finish setup first"
    message instead of 500ing on ``STATE.graph is None``.
    """
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


async def chat(message: str, session_id: str, *, model: str | None = None) -> list[dict[str, Any]]:
    """Route a user message through LangGraph and return the final assistant
    response as a list of ``{"role": "assistant", "content": ...}`` dicts.

    This is the non-streaming entry point used by the console + the OpenAI-compat
    endpoint. The A2A handler uses ``_chat_langgraph_stream`` instead to
    capture tool events and emit the cost-v1 DataPart on the terminal
    artifact. ``model`` overrides the lead model for this turn (per-tab / per
    OpenAI request); unset → the configured default.
    """
    if STATE.graph is None:
        return _setup_required_message()
    return await _chat_langgraph(message, session_id, model=model)


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


def _coerce_tool_output(value) -> str:
    """Unwrap a tool result to its payload.

    ``on_tool_end`` hands back the LangChain ``ToolMessage``, whose ``str()``
    leaks ``name=``/``tool_call_id=`` noise — the card wants the actual
    ``.content``. Falls back to the raw value for plain returns.
    """
    return _coerce_tool_value(getattr(value, "content", value))


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


async def _pending_interrupt_value(config: dict):
    """Return the value of a pending LangGraph interrupt (ask_human / HITL) for
    this thread, or ``None``. Both chat paths read the same snapshot to detect a
    turn that paused for human input instead of producing a final answer."""
    try:
        snapshot = await STATE.graph.aget_state(config)
    except Exception:
        return None
    pending = list(getattr(snapshot, "interrupts", None) or [])
    if not pending:
        for t in getattr(snapshot, "tasks", ()) or ():
            pending.extend(getattr(t, "interrupts", ()) or ())
    if not pending:
        return None
    return getattr(pending[0], "value", pending[0])


def _last_tool_text(result) -> str:
    """The last tool result's text in a turn — the fallback when a turn produced
    no assistant text (e.g. a ``wait`` yield, whose 'Yielding…' confirmation is a
    ToolMessage, not an AIMessage)."""
    from langchain_core.messages import ToolMessage

    for msg in reversed((result or {}).get("messages", [])):
        if isinstance(msg, ToolMessage) and msg.content:
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


async def _run_turn_stream(
    message: str, session_id: str, config: dict, *, resume_value=None, images=None, model=None, reasoning_effort=None
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
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    # Native vision (ADR 0021): when the model is vision-capable and the turn
    # carried image parts, the user message is a multimodal content list (text +
    # image_url blocks) the model sees directly — not piped through extraction.
    if images and getattr(STATE.graph_config, "model_vision", False):
        blocks: list[dict] = [{"type": "text", "text": message}] if message else []
        blocks += [{"type": "image_url", "image_url": {"url": uri}} for _mt, uri in images]
        human = HumanMessage(content=blocks)
    else:
        human = HumanMessage(content=message)

    graph_input = (
        Command(resume=resume_value)
        if resume_value is not None
        # Prepend any completed background-job notifications (ADR 0050) so the model
        # learns of detached work that finished since this session last ran a turn.
        else {
            "messages": _drain_background_messages(session_id) + [human],
            "session_id": session_id,
            # Per-tab model + reasoning-effort override (ModelOverrideMiddleware reads
            # both); omit each key when unset so the configured default applies.
            **({"model": model} if model else {}),
            **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
        }
    )
    from observability import metrics
    from observability import pricing

    accumulated_raw = ""
    streamed_len = 0  # chars of visible <output> already emitted as text frames
    reasoned_len = 0  # chars of scratch_pad reasoning already emitted (live thinking)
    # Incremental views: same visible-so-far as stream_visible_output/_reasoning, but
    # they scan only the new tail instead of re-running regexes over the whole
    # accumulated text every chunk — turning the per-turn O(N²) rescan into ~O(N) (#1310).
    out_view = StreamingOutputView()
    reason_view = StreamingReasoningView()
    _llm_started: dict[str, float] = {}  # run_id → monotonic start (per-call latency)
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
            pass
        elif kind == "on_tool_end":
            output = event.get("data", {}).get("output", "")
            # Close the card keyed by the tool_call id (the ToolMessage carries it);
            # fall back to run_id/name for non-tool-message producers. A ToolMessage
            # the ToolNode stamped status="error" (a raised tool — a declined
            # run_command, an execution error, an enforcement block) closes the card
            # as a failure (X) instead of a green "done".
            coerced = _coerce_tool_output(output)
            # A show_component result (ADR 0051) carries a sentinel-wrapped payload — lift
            # it into a `component` frame (→ a component-v1 DataPart) and strip it from the
            # card so the user sees the rendered widget, not raw wire JSON.
            if isinstance(coerced, str):
                from graph.components import extract_component, strip_component

                comp = extract_component(coerced)
                if comp is not None:
                    yield ("component", comp)
                    coerced = strip_component(coerced)
            yield (
                "tool_end",
                {
                    "id": getattr(output, "tool_call_id", None) or event.get("run_id") or name,
                    "name": name,
                    "output": coerced,
                    "error": getattr(output, "status", None) == "error",
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
                if tcid and tcname and tcid not in announced_tools:
                    announced_tools.add(tcid)
                    yield (
                        "tool_start",
                        {"id": tcid, "name": tcname, "input": "", **({"parentId": parent_tool_id} if parent_tool_id else {})},
                    )
            if hasattr(chunk, "content") and chunk.content:
                accumulated_raw += chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                # Stream only the user-facing <output> region, token by token —
                # never the scratch_pad. The terminal artifact (extract_output)
                # reconciles any partial tail held back here.
                visible = out_view.update(accumulated_raw)
                if len(visible) > streamed_len:
                    yield ("text", visible[streamed_len:])
                    streamed_len = len(visible)
                # Stream the scratch_pad reasoning on its own channel — a collapsible
                # "thinking" view in the console (never folded into the answer text).
                reasoning = reason_view.update(accumulated_raw)
                if len(reasoning) > reasoned_len:
                    yield ("reasoning", reasoning[reasoned_len:])
                    reasoned_len = len(reasoning)
        elif kind == "on_chat_model_end":
            output = event.get("data", {}).get("output")
            # Finalize each tool card with its full args, keyed by the tool_call id.
            # `announced_tools` is scoped to THIS turn: this pass also surfaces a card
            # for any tool the stream path didn't announce (e.g. a non-streaming model)
            # without re-emitting an early start already sent earlier this turn.
            for tc in getattr(output, "tool_calls", None) or []:
                tcid = tc.get("id")
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
                yield ("usage", {**usage_out, "cost_usd": cost, "model": model})

    # HITL pause (ADR 0003): the agent called ask_human → LangGraph interrupt().
    # The graph is checkpointed at the interrupt; surface the question so the A2A
    # layer parks the task as input-required. Resume later with resume_value.
    interrupt_val = await _pending_interrupt_value(config)
    if interrupt_val is not None:
        yield ("input_required", _interrupt_payload(interrupt_val))
        return

    yield ("__raw__", accumulated_raw)


# --- Workflow slash commands (ADR 0002) --------------------------------------
# A chat message like ``/research-and-brief quantum computing`` runs the named
# workflow instead of a normal model turn — the slash-command analogue of the
# run_workflow tool. Free text maps to the first unset (required) input; explicit
# ``key=value`` tokens set named inputs. Short-circuits the turn like /goal does.


def _parse_slash_command(message: str) -> tuple[str, str]:
    """Split ``/name rest`` → (name, rest). Returns ("", "") if not a slash msg."""
    s = (message or "").strip()
    if not s.startswith("/"):
        return "", ""
    parts = s[1:].split(None, 1)
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def _parse_workflow_inputs(recipe: dict, rest: str) -> dict:
    """Map a slash-command argument string to a workflow's named inputs.

    ``key=value`` tokens (quotes respected) set inputs explicitly; any leftover
    free text is assigned to the first not-yet-set input, preferring required
    ones — so ``/research-and-brief quantum computing`` fills ``topic``.
    """
    import shlex

    try:
        tokens = shlex.split(rest)
    except ValueError:
        tokens = rest.split()
    inputs: dict = {}
    leftover: list[str] = []
    for tok in tokens:
        if "=" in tok and tok.split("=", 1)[0].isidentifier():
            key, val = tok.split("=", 1)
            inputs[key] = val
        else:
            leftover.append(tok)
    if leftover:
        declared = recipe.get("inputs", []) or []
        target = next((i["name"] for i in declared if i["name"] not in inputs and i.get("required")), None)
        if target is None:
            target = next((i["name"] for i in declared if i["name"] not in inputs), None)
        if target:
            inputs[target] = " ".join(leftover)
    return inputs


def _parse_workflow_command(message: str):
    """Return (name, inputs) if ``message`` is ``/<known-workflow> …``, else None."""
    name, rest = _parse_slash_command(message)
    if not name or _slash_kind(name) != "workflow":
        return None
    recipe = STATE.workflow_registry.get(name)
    if recipe is None:  # defensive — _slash_kind already confirmed it
        return None
    return name, _parse_workflow_inputs(recipe, rest)


async def _run_parsed_workflow(name: str, inputs: dict, *, on_step=None) -> str:
    """Run a workflow command and format its output as the assistant reply.

    ``on_step`` is forwarded to the workflows plugin's runner (``STATE.workflow_run``,
    set when the plugin is enabled) so the caller can stream per-step progress (the
    chat path renders a tool card per step)."""
    if STATE.workflow_run is None:
        return "⚠️ workflows are not enabled"
    try:
        result = await STATE.workflow_run(name, inputs, on_step=on_step)
    except ValueError as exc:
        return f"⚠️ {exc}"
    raw = result.get("output") or ""
    # Strip subagent scratch_pad/output tags so the chat shows clean text,
    # matching how a normal turn is rendered.
    out = extract_output(raw) or raw or "(workflow produced no output)"
    failed = result.get("failed") or []
    if failed:
        out += f"\n\n_(failed steps: {', '.join(failed)})_"
    return out


# --- Subagent slash commands (ADR 0020) --------------------------------------
# A chat message like ``/researcher find me X`` runs the named subagent instead
# of a normal model turn — the slash-command analogue of the ``task`` tool, so
# "run a worker" is a composer gesture, not a separate surface. Free text after
# the name is the subagent's prompt. A workflow of the same name wins (the turn
# dispatch checks workflows first). Short-circuits the turn like /goal does.


def _parse_subagent_command(message: str):
    """Return ``(subagent_type, prompt)`` if ``message`` is ``/<known-subagent>
    …`` (and not a workflow of the same name), else ``None``. Precedence is
    decided once by ``_slash_kind`` (a workflow of the same name wins)."""
    name, rest = _parse_slash_command(message)
    if not name or _slash_kind(name) != "subagent":
        return None
    return name, rest.strip()


async def _run_parsed_subagent(subagent_type: str, prompt: str) -> str:
    """Run one subagent from a chat slash command, formatted as the reply."""
    from graph.agent import run_manual_subagent

    try:
        raw = await run_manual_subagent(
            STATE.graph_config,
            knowledge_store=STATE.knowledge_store,
            scheduler=STATE.scheduler,
            description=f"/{subagent_type} chat command",
            prompt=prompt,
            subagent_type=subagent_type,
        )
    except ValueError as exc:
        return f"⚠️ {exc}"
    # Strip the worker's scratch_pad/output tags so chat shows clean text.
    return extract_output(raw) or raw or "(subagent produced no output)"


# --- User-facing skill slash commands (ADR 0052) -----------------------------
# A chat message like ``/triage <args>`` runs a user-facing skill. Unlike a
# workflow/subagent command, it does NOT spawn a worker or short-circuit the
# turn — it REWRITES the message to inject the skill's procedure as a directive
# and falls through to the normal lead-agent turn, so every streaming / HITL /
# goal / tool invariant holds unchanged. Workflows and subagents of the same
# token win (dispatch checks them first).


# Slash-command precedence + the palette resolver live in graph/slash_commands.py
# — a neutral module both the dispatcher (here) and the console palette import, so
# the two can't drift (operator_api may not import server, so it can't live here).
from graph.slash_commands import (  # noqa: E402
    find_user_facing_skill as _find_user_facing_skill,
    slash_kind as _slash_kind,
)


def _parse_skill_command(message: str):
    """Return ``(skill_dict, args)`` if ``message`` is ``/<user-facing-skill> …``
    (and not ``/goal`` or a workflow/subagent of the same token), else ``None``.
    Precedence is decided once by ``_slash_kind``."""
    name, rest = _parse_slash_command(message)
    if not name or _slash_kind(name) != "skill":
        return None
    skill = _find_user_facing_skill(name)
    if skill is None:  # defensive — _slash_kind already confirmed it
        return None
    return skill, rest.strip()


def _skill_directive(skill: dict, args: str) -> str:
    """Compose the lead-agent directive injecting a user-facing skill's procedure
    into the turn (ADR 0052). The agent runs the procedure with its full toolset."""
    name = skill.get("name") or "skill"
    procedure = (skill.get("prompt_template") or "").strip()
    directive = f"[Running the '{name}' skill]\n\nFollow this procedure:\n\n{procedure}\n"
    if args:
        directive += f"\nInput: {args}\n"
    return directive


async def _chat_langgraph_stream(
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

    from graph.goals.goal_turn import goal_turn
    from graph.middleware.request_context import request_metadata_scope

    trace_meta: dict = {"message_preview": message[:100]}
    if caller_trace:
        if caller_trace.get("traceId"):
            trace_meta["caller_trace_id"] = caller_trace["traceId"]
        if caller_trace.get("spanId"):
            trace_meta["caller_span_id"] = caller_trace["spanId"]

    if STATE.graph is None:
        yield ("error", "setup required — finish the setup wizard before calling A2A endpoints")
        return

    async with (
        tracing.trace_session(
            session_id=session_id,
            name="a2a-stream",
            metadata=trace_meta,
        ),
        request_metadata_scope(request_metadata),
    ):
        try:
            # Goal control messages (/goal ...) short-circuit the turn: set /
            # status / clear a goal and return the reply without running the graph.
            if STATE.goal_controller is not None:
                reply = await STATE.goal_controller.parse_control(message, session_id)
                if reply is not None:
                    yield ("done", reply)
                    return

            # Issue control command (/issue ...) short-circuits the turn: file a
            # GitHub issue. User-only by design — it is NOT an agent tool, so the
            # model can't create issues autonomously (mirrors /goal).
            from tools.gh_issue import effective_default_repo, parse_issue_control

            issue_reply = await parse_issue_control(
                message,
                default_repo=effective_default_repo(
                    getattr(STATE.graph_config, "github_default_repo", ""),
                    getattr(STATE.graph_config, "github_repos", []),
                ),
            )
            if issue_reply is not None:
                yield ("done", issue_reply)
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
                yield ("done", wf_out)
                return

            # Subagent slash command (/<subagent> <prompt>) short-circuits the
            # turn: run the one worker and return its output (ADR 0020 — run from
            # chat). Renders a single tool card. A workflow of the same name wins.
            parsed_sub = _parse_subagent_command(message)
            if parsed_sub is not None:
                sub_type, sub_prompt = parsed_sub
                if not sub_prompt:
                    yield ("done", f"Usage: `/{sub_type} <prompt>` — describe the task for the {sub_type} subagent.")
                    return
                sub_tool_id = f"subagent:{sub_type}"
                yield ("tool_start", {"id": sub_tool_id, "name": sub_tool_id, "input": sub_prompt})
                sub_out = await _run_parsed_subagent(sub_type, sub_prompt)
                yield ("tool_end", {"id": sub_tool_id, "name": sub_tool_id, "output": sub_out[:300]})
                yield ("done", sub_out)
                return

            # User-facing skill slash command (/<skill> [args]) — does NOT
            # short-circuit: rewrite the message to inject the skill's procedure
            # as a directive, then fall through to the normal lead-agent turn so
            # every streaming / HITL / goal invariant holds (ADR 0052).
            parsed_skill = _parse_skill_command(message)
            if parsed_skill is not None:
                message = _skill_directive(*parsed_skill)

            # ACP runtime (ADR 0033 slice 4) — when `agent_runtime: acp:<agent>`, an
            # external coding agent (proto/codex/claude/…) drives the turn over ACP
            # instead of the native LangGraph loop. One stateful ACP session per thread.
            from runtime.acp_runtime import is_acp_runtime

            if is_acp_runtime(STATE.graph_config):
                rt = await _get_acp_runtime(_resolve_thread_id(request_metadata, session_id))
                # Bridge the agent's reader-loop callbacks (answer-text deltas + tool events)
                # into the same text / tool_start / tool_end frames the native runtime yields,
                # in arrival order → live streaming + tool cards.
                _ACP_DONE = object()
                frame_q: asyncio.Queue = asyncio.Queue()

                async def _on_text(delta: str) -> None:
                    await frame_q.put(("text", delta))

                async def _on_tool(ev: dict) -> None:
                    if ev.get("phase") == "start":
                        await frame_q.put(
                            (
                                "tool_start",
                                {"id": ev.get("id", ""), "name": ev.get("name", "tool"), "input": ev.get("input", "")},
                            )
                        )
                    elif ev.get("phase") == "end":
                        await frame_q.put(
                            (
                                "tool_end",
                                {
                                    "id": ev.get("id", ""),
                                    "name": ev.get("name", "tool"),
                                    "output": ev.get("output", ""),
                                },
                            )
                        )

                async def _drive():
                    try:
                        return await rt.run_turn(message, text_callback=_on_text, tool_callback=_on_tool)
                    finally:
                        await frame_q.put(_ACP_DONE)

                driver = asyncio.create_task(_drive())
                while True:
                    frame = await frame_q.get()
                    if frame is _ACP_DONE:
                        break
                    yield frame  # (kind, payload) — already normalized
                try:
                    answer = await driver
                except Exception as exc:  # noqa: BLE001 — surface as a turn error, don't 500
                    log.exception("[acp-runtime] turn failed")
                    yield ("error", f"ACP runtime ({rt.agent}) failed: {exc}")
                    return
                # Attribute the turn to the ACP agent in telemetry — else it defaults to the
                # gateway model (`protolabs/reasoning`), which never ran. Gateway tokens/cost are
                # 0: the external agent's own subscription meters its usage, not us. (The model
                # label `acp:<agent>` is the honest signal that this turn wasn't gateway-metered.)
                yield (
                    "usage",
                    {
                        "model": f"acp:{rt.agent}",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cost_usd": 0.0,
                    },
                )
                # The answer already streamed as text deltas; `done` finalizes (executor appends
                # only meta when text was streamed, so no duplication).
                yield ("done", answer)
                return

            # thread_id keys this session's history in the checkpointer (bound
            # at compile time in create_agent_graph). The prefix isolates A2A
            # sessions from the non-streaming chat in the shared MemorySaver. Derivation is
            # a pluggable seam (#571): a fork registers a resolver to scope memory
            # off request metadata (e.g. per-project) without editing this file.
            config = {
                "configurable": {"thread_id": _resolve_thread_id(request_metadata, session_id)},
                "recursion_limit": 200,
            }

            # Per-tab model + reasoning-effort override (the console puts the tab's chosen
            # model + the /effort level in the A2A request metadata). Threaded into every
            # turn this stream runs — initial, kicker, goal continuation — so the whole
            # conversation stays on the tab's model/effort. Unset → the configured default.
            _model = ((request_metadata or {}).get("model") or "").strip() or None
            _effort = ((request_metadata or {}).get("reasoning_effort") or "").strip() or None

            # When a goal is already active, the whole turn is goal-driven —
            # suppress cross-session prior_sessions on the initial turn (and the
            # kicker retry below), matching the continuation turns.
            goal_active = (
                STATE.goal_controller is not None and STATE.goal_controller.active_goal(session_id) is not None
            )

            # One graph turn (model tokens accumulated silently; A2A consumers
            # get progress from tool_start/tool_end). Final text is extracted
            # once via extract_output().
            accumulated_raw = ""
            paused = False
            with goal_turn(goal_active):
                async for kind, payload in _run_turn_stream(
                    message,
                    session_id,
                    config,
                    resume_value=(message if resume else None),
                    images=images,
                    model=_model,
                    reasoning_effort=_effort,
                ):
                    if kind == "__raw__":
                        accumulated_raw = payload
                    elif kind == "input_required":
                        # Agent paused for human input — surface it and park the
                        # turn; the A2A runner sets the task input-required and the
                        # caller resumes via message/send on the same taskId.
                        yield (kind, payload)
                        paused = True
                    else:
                        yield (kind, payload)

            # A paused turn produced no final answer — don't run the
            # dropped-scratch kicker or goal verification; the task is parked.
            if paused:
                return

            final_text = extract_output(accumulated_raw)
            final_raw = accumulated_raw

            # Dropped-turn recovery: the model emitted only <scratch_pad>/<think>
            # — no <output>, no tool call — so extract_output is empty and the
            # turn would silently drop. Re-prompt once on the same thread with a
            # kicker (history is preserved by the checkpointer). Capped at 1 retry.
            if not final_text and is_dropped_scratch_turn(accumulated_raw):
                log.warning(
                    "[chat-stream] dropped scratch-only turn (session=%s) — kicker retry",
                    session_id,
                )
                yield ("tool_start", "↻ retry: prior turn dropped scratch-only")
                retry_raw = ""
                with goal_turn(goal_active):
                    async for kind, payload in _run_turn_stream(
                        DROPPED_SCRATCH_KICKER, session_id, config, model=_model, reasoning_effort=_effort
                    ):
                        if kind == "__raw__":
                            retry_raw = payload
                        else:
                            yield (kind, payload)
                recovered = extract_output(retry_raw)
                if recovered:
                    final_text, final_raw = recovered, retry_raw
                    log.info("[chat-stream] kicker recovered the turn (session=%s)", session_id)
                else:
                    log.warning(
                        "[chat-stream] kicker retry also empty (session=%s) — falling back",
                        session_id,
                    )

            # Goal mode: when an active goal exists for this session, verify the
            # outcome after the agent stops; if not met, re-invoke on the same
            # thread with a continuation prompt until the verifier passes, the
            # iteration budget is spent, or it's flagged unachievable.
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
                    # For fresh-context goals, create a scoped thread so the checkpointer
                    # starts clean — no accumulated transcript from prior iterations.
                    goal_state = decision.state
                    if goal_state and goal_state.fresh_context:
                        base_tid = _resolve_thread_id(request_metadata, session_id)
                        cont_config = {
                            "configurable": {"thread_id": f"{base_tid}:goal-iter-{goal_state.iteration}"},
                            "recursion_limit": 200,
                        }
                    else:
                        cont_config = config  # same-session (existing behavior)

                    cont_raw = ""
                    with goal_turn():
                        async for kind, payload in _run_turn_stream(
                            decision.message, session_id, cont_config, model=_model, reasoning_effort=_effort
                        ):
                            if kind == "__raw__":
                                cont_raw = payload
                            else:
                                yield (kind, payload)
                    cont_text = extract_output(cont_raw)
                    if cont_text:
                        final_text, final_raw = cont_text, cont_raw
                # Append the terminal goal outcome to the answer so the A2A
                # terminal artifact carries it, matching the non-streaming path
                # (the 🎯 status frames above are transient and can coalesce).
                if note:
                    final_text = f"{final_text}\n\n---\n{note}"

            # Self-reported confidence (from whichever pass produced the answer),
            # yielded before "done" so the A2A handler records it on the
            # terminal artifact's confidence-v1 DataPart.
            confidence, explanation = extract_confidence(final_raw)
            if confidence is not None:
                yield ("confidence", {"confidence": confidence, "explanation": explanation})

            yield ("done", final_text)

        except GeneratorExit:
            # Expected: A2A consumers break out of the SSE loop after
            # capturing the initial task event,
            # then hand off to TaskTracker for polling. Re-raise so Python
            # finalizes the generator cleanly; the OTel cross-context detach
            # noise this used to emit is silenced at the logger level in
            # tracing.py.
            raise
        except Exception as e:
            log.exception(
                "[a2a-stream] unhandled exception for session=%s: %s",
                session_id,
                e,
            )
            yield ("error", str(e))
        finally:
            tracing.flush()


async def _chat_langgraph(message: str, session_id: str, *, model: str | None = None) -> list[dict[str, Any]]:
    """Non-streaming LangGraph entry — used by the console + OpenAI-compat."""
    from observability import tracing
    from langchain_core.messages import HumanMessage, AIMessage

    from graph.goals.goal_turn import goal_turn

    # Per-turn model override (ModelOverrideMiddleware reads state["model"]).
    _model_extra = {"model": model} if (model or "").strip() else {}

    async with tracing.trace_session(
        session_id=session_id,
        name="chat",
        metadata={"message_preview": message[:100]},
    ):
        try:
            # Goal control messages short-circuit (set / status / clear).
            if STATE.goal_controller is not None:
                reply = await STATE.goal_controller.parse_control(message, session_id)
                if reply is not None:
                    return [{"role": "assistant", "content": reply}]

            # Issue control command (/issue ...) short-circuits — file a GitHub
            # issue (user-only; never an agent tool). See the streaming path.
            from tools.gh_issue import effective_default_repo, parse_issue_control

            issue_reply = await parse_issue_control(
                message,
                default_repo=effective_default_repo(
                    getattr(STATE.graph_config, "github_default_repo", ""),
                    getattr(STATE.graph_config, "github_repos", []),
                ),
            )
            if issue_reply is not None:
                return [{"role": "assistant", "content": issue_reply}]

            # Workflow slash command (/<workflow-name> …) short-circuits the turn.
            parsed = _parse_workflow_command(message)
            if parsed is not None:
                return [{"role": "assistant", "content": await _run_parsed_workflow(*parsed)}]

            # User-facing skill slash command (/<skill> [args], ADR 0052) — rewrite
            # the message to inject the skill's procedure and fall through to the
            # normal turn (does not short-circuit). Workflows of the same token win.
            parsed_skill = _parse_skill_command(message)
            if parsed_skill is not None:
                message = _skill_directive(*parsed_skill)

            # `chat:` namespaces non-streaming sessions in the shared checkpointer,
            # apart from the A2A `a2a:` ones (was `gradio:` — renamed when the Gradio
            # UI was removed; non-streaming chat is short-lived so the one-time
            # re-key on upgrade is harmless).
            config = {"configurable": {"thread_id": f"chat:{session_id}"}}

            def _last_ai(result) -> str:
                for msg in reversed(result.get("messages", [])):
                    if isinstance(msg, AIMessage) and msg.content:
                        return msg.content if isinstance(msg.content, str) else str(msg.content)
                return ""

            # When a goal is already active, the whole turn is goal-driven —
            # suppress cross-session prior_sessions on the initial turn too.
            goal_active = (
                STATE.goal_controller is not None and STATE.goal_controller.active_goal(session_id) is not None
            )
            with goal_turn(goal_active):
                result = await STATE.graph.ainvoke(
                    {"messages": [HumanMessage(content=message)], "session_id": session_id, **_model_extra},
                    config=config,
                )
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
                    question = payload.get("question") or payload.get("title") or "The agent needs input to continue."
                    return [{"role": "assistant", "content": f"🙋 **Input needed:** {question}"}]

                # Dropped scratch-only turn (no <output>, no tool call): re-prompt
                # once with the kicker, matching _chat_langgraph_stream.
                if is_dropped_scratch_turn(raw):
                    log.warning("[chat] dropped scratch-only turn (session=%s) — kicker retry", session_id)
                    with goal_turn(goal_active):
                        result = await STATE.graph.ainvoke(
                            {
                                "messages": [HumanMessage(content=DROPPED_SCRATCH_KICKER)],
                                "session_id": session_id,
                                **_model_extra,
                            },
                            config=config,
                        )
                    response = extract_output(_last_ai(result))

            # Still nothing (e.g. a `wait` yield, or a tool-only turn): fall back
            # to the last tool result so the caller gets a signal, not a blank.
            if not response:
                response = _last_tool_text(result) or "_(The agent ended the turn without a textual reply.)_"

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
                    # For fresh-context goals, create a scoped thread so the checkpointer
                    # starts clean — no accumulated transcript from prior iterations.
                    goal_state = decision.state
                    if goal_state and goal_state.fresh_context:
                        cont_config = {
                            "configurable": {"thread_id": f"chat:{session_id}:goal-iter-{goal_state.iteration}"},
                        }
                    else:
                        cont_config = config

                    with goal_turn():
                        result = await STATE.graph.ainvoke(
                            {
                                "messages": [HumanMessage(content=decision.message)],
                                "session_id": session_id,
                                **_model_extra,
                            },
                            config=cont_config,
                        )
                    nxt = extract_output(_last_ai(result))
                    if nxt:
                        response = nxt
                if note:
                    response = f"{response}\n\n---\n{note}"

            return [{"role": "assistant", "content": response}]
        except Exception as e:
            log.exception(
                "[chat] unhandled exception for session=%s: %s",
                session_id,
                e,
            )
            return [{"role": "assistant", "content": f"**Error:** {e}"}]
        finally:
            tracing.flush()
