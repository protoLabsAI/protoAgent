"""The streaming turn event loop — extracted from ``server/chat.py`` (#3874, epic #3804).

``_run_turn_stream`` runs one graph turn over ``astream_events`` and yields the
``(kind, payload)`` frames both chat paths consume (tool cards, room bubbles, text,
reasoning, usage, steer boundaries, then ``__raw__`` or ``input_required``). The helpers
only it uses moved with it: the background-delegation receipt parsing
(``_BG_JOB_ID`` / ``_BG_DISPATCH_REFUSED`` / ``_delegation_summary``), the lead-speaker
filter (``_TOOL_NODE`` / ``_lc_internal_call_marker`` / ``_speaks_for_the_lead``) and
``_paragraph_break``.

**Callers resolve it through this module at call time** (``_turn_stream._run_turn_stream``
in ``server.chat._run_native_turn``), so a test's patch here is what runs.

**Collaborators that stay in ``server.chat``** — the vision message, the background
drain, the HITL resume/interrupt readers and the tool payload shaping
(``_coerce_tool_value`` / ``_coerce_tool_output`` / ``_coerce_room_text`` /
``_tool_output_chars`` / ``_TOOL_PREVIEW_CHARS``) — are read through ``server.chat`` at
CALL time (``_chat().<name>``), never bound at import: a patch on ``server.chat`` still
lands, and ``import server.turn_stream`` has no import-time edge back into it. The loop
reads ``time`` from THIS module's globals.

``server.chat`` re-exports every name here so ``from server.chat import _run_turn_stream``
keeps resolving. Patch these names HERE, not on ``server.chat``: a re-export is a copy of
the binding, so a ``setattr`` there intercepts nothing (``tests/test_turn_stream_seam.py``
guards it).
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import importlib
import logging
import re
import time
from collections.abc import Callable, Iterator
from typing import Any

from runtime.state import STATE

# Same logger as server.chat, so the moved log lines keep their channel.
log = logging.getLogger("protoagent.server")


def _chat():
    """``server.chat`` the MODULE, resolved at call time — by path, because ``server``
    re-exports the ``chat`` FUNCTION under the submodule's name."""
    return importlib.import_module("server.chat")


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


# ── The event loop, decomposed (#3880) ─────────────────────────────────────────
#
# `_run_turn_stream` is a thin dispatch: `_TurnStreamState` holds the accumulators the
# old single if/elif shared, and each `astream_events` kind the loop acts on has ONE
# handler below. Every handler is a plain (SYNCHRONOUS) generator of `(kind, payload)`
# frames, deliberately:
#
# - none of them awaits, so a sync generator adds no event-loop suspension point — the
#   loop's only awaits stay the `astream_events` pull and the post-loop HITL read, so
#   cancellation lands exactly where it did;
# - it is lazy, so each frame reaches the consumer at the same point in the handler's
#   work as the inline `yield` did: state written before a frame is written before the
#   consumer sees it, and work after a frame (the model-end latency read + metrics
#   record after its tool_start cards) runs only once the consumer asks for more — and
#   never if it closes the stream there. Returning a list would run all of it first.
# - the loop drives each one under `contextlib.closing`, so a consumer's aclose() /
#   athrow() at a frame closes the handler at that frame deterministically.
#
# `return` in a handler is the old branch's `continue`.


@dataclasses.dataclass
class _TurnStreamState:
    """The mutable accumulators one ``_run_turn_stream`` call shares across events."""

    metrics: Any  # observability.metrics, imported where the loop always imported it
    pricing: Any  # observability.pricing
    accumulated_raw: str = ""  # the answer text so far (the model's content; no protocol tags)
    # The model call the answer's latest text came from. Each lead model call is its own
    # message: "I'll check the time first." → tool → "It is noon." must not be glued into
    # "first.It is". So text arriving from a DIFFERENT call than the last text opens a
    # paragraph — keyed on the text's own run, never on a model merely STARTING, because
    # work a tool detached keeps reporting into this stream mid-answer (see below). The
    # break rides the streamed delta itself, not just this accumulator, so the live
    # stream, the executor's accumulation and the canonical `done` text stay ONE string.
    # (The console keeps a turn's text-to-tool interleaving only while they agree; #3210
    # separated only the executor's copy, which the `done` text overrode on this path.)
    answer_run: object = None
    # run_id → monotonic start (per-call latency)
    llm_started: dict[str, float] = dataclasses.field(default_factory=dict)
    # run_id → monotonic start (per-call latency, #2697)
    tool_started: dict[str, float] = dataclasses.field(default_factory=dict)
    # run_id → delegate name, for delegate_to → room bubble (#3042)
    delegate_targets: dict[str, str] = dataclasses.field(default_factory=dict)
    # run_id → a BACKGROUND delegate_to's ask, emitted once it has a job id
    bg_delegations: dict[str, dict] = dataclasses.field(default_factory=dict)
    # tool_call ids already surfaced as a start frame
    announced_tools: set[str] = dataclasses.field(default_factory=set)


_Frames = Iterator[tuple[str, Any]]


def _on_chat_model_start(st: _TurnStreamState, event: dict, name: str, parent_tool_id) -> _Frames:
    # Stamp the per-call start so on_chat_model_end can measure latency.
    rid = event.get("run_id")
    if rid:
        st.llm_started[rid] = time.monotonic()
    yield from ()


def _on_tool_start(st: _TurnStreamState, event: dict, name: str, parent_tool_id) -> _Frames:
    # No frame here: the tool card is surfaced earlier — on the model's first
    # streamed tool-call token (on_chat_model_stream) and finalized with full
    # args on on_chat_model_end, both keyed by the tool_call id so on_tool_end
    # closes the same card. Execution-start carries only a run_id (no
    # tool_call id to correlate), so it would just make a duplicate card.
    # It IS the right place to stamp EXECUTION latency though (#2697) — unlike
    # the card-announce timing above, on_tool_start/on_tool_end bracket exactly
    # how long the tool took to run, mirroring llm_started's run_id-keyed idiom.
    rid = event.get("run_id")
    if rid:
        st.tool_started[rid] = time.monotonic()
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
            st.bg_delegations[rid] = {"id": rid, "target": _target, "query": _q, "summary": _summary}
        elif _target:
            st.delegate_targets[rid] = _target
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


def _on_tool_end(st: _TurnStreamState, event: dict, name: str, parent_tool_id) -> _Frames:
    output = event.get("data", {}).get("output", "")
    rid = event.get("run_id")
    # Consume the latency stamp up front: the delegate_to branches below return before
    # the tool card, and a stamp they left behind would leak into a reused run_id (#3883).
    started_at = st.tool_started.pop(rid, None) if rid else None
    # `delegate_to` renders as the participant's own chat bubble, not a tool card:
    # the collaboration the lead moderates then reads as a conversation (proto,
    # reviewer) rather than machinery under one reply (#3042). REPLACES the card —
    # this branch emits a room_reply and returns, so no tool_end frame follows.
    # Foreground only: a background delegate_to answers via the background manager —
    # its on_tool_end is the receipt, handled just below.
    _bg = st.bg_delegations.pop(rid, None) if rid else None
    if _bg:
        # A background delegate_to's receipt: surface the ASK — once, with the job id
        # to track and the summary — and nothing else. No tool card (#3042) and no
        # reply frame: the delegate's answer arrives on its own through the drain.
        _receipt = _chat()._coerce_room_text(output)
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
            return
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
        return
    _dtgt = st.delegate_targets.pop(rid, None) if rid else None
    if _dtgt:
        _dtext = _chat()._coerce_room_text(output)
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
        return
    tool_duration_ms = (
        int(max(0.0, time.monotonic() - (time.monotonic() if started_at is None else started_at)) * 1000) if rid else 0
    )
    # Close the card keyed by the tool_call id (the ToolMessage carries it);
    # fall back to run_id/name for non-tool-message producers. A ToolMessage
    # the ToolNode stamped status="error" (a raised tool — a declined
    # run_command, an execution error, an enforcement block) closes the card
    # as a failure (X) instead of a green "done".
    coerced = _chat()._coerce_tool_output(output)
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
            coerced = str(strip_component(full))[:_chat()._TOOL_PREVIEW_CHARS]  # card = human prefix
    yield (
        "tool_end",
        {
            "id": getattr(output, "tool_call_id", None) or event.get("run_id") or name,
            "name": name,
            "output": coerced,
            # True pre-truncation size — the preview above is capped, so the
            # console's context-cost estimate must come from this (#2775).
            "output_chars": _chat()._tool_output_chars(output),
            "error": getattr(output, "status", None) == "error",
            "duration_ms": tool_duration_ms,  # #2697 — execution time, on_tool_start→on_tool_end
            **({"parentId": parent_tool_id} if parent_tool_id else {}),
        },
    )


def _on_chat_model_stream(st: _TurnStreamState, event: dict, name: str, parent_tool_id) -> _Frames:
    chunk = event.get("data", {}).get("chunk")
    if chunk is None:
        return
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
            st.announced_tools.add(tcid)
            continue
        if tcid and tcname and tcid not in st.announced_tools:
            st.announced_tools.add(tcid)
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
    # nest by id, and the `on_chat_model_end` cost accounting is untouched
    # (subagent tokens still bill). Only the lead's own tokens reach the answer.
    if parent_tool_id:
        return
    # Nor does any other model call that is not the lead answering: one made under
    # a tool — its body, a graph it runs (a workflow step), work it detached (a
    # background ingest's describe/enrich, a plugin's spawn_work) — or a
    # middleware's own call (the compaction summary). Those tokens used to land in
    # the middle of the answer ("I started the IMAGE-DESCRIPTIONingest…") and in
    # the stored text. Billing is untouched.
    if not _speaks_for_the_lead(event.get("metadata") or {}):
        return
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
            if run != st.answer_run and st.accumulated_raw.strip():
                text = _paragraph_break(st.accumulated_raw, text) + text
            st.answer_run = run
            st.accumulated_raw += text
            yield ("text", text)


def _on_chat_model_end(st: _TurnStreamState, event: dict, name: str, parent_tool_id) -> _Frames:
    output = event.get("data", {}).get("output")
    # Finalize each tool card with its full args, keyed by the tool_call id.
    # `announced_tools` is scoped to THIS turn: this pass also surfaces a card
    # for any tool the stream path didn't announce (e.g. a non-streaming model)
    # without re-emitting an early start already sent earlier this turn.
    for tc in getattr(output, "tool_calls", None) or []:
        tcid = tc.get("id")
        if tcid and tc.get("name") == "delegate_to":
            st.announced_tools.add(tcid)  # room bubble, not a card (#3042)
            continue
        if tcid:
            st.announced_tools.add(tcid)
            yield (
                "tool_start",
                {
                    "id": tcid,
                    "name": tc.get("name", ""),
                    "input": _chat()._coerce_tool_value(tc.get("args", "")),
                    **({"parentId": parent_tool_id} if parent_tool_id else {}),
                },
            )
    usage = getattr(output, "usage_metadata", None) if output else None
    rid = event.get("run_id")
    latency_s = max(0.0, time.monotonic() - st.llm_started.pop(rid, time.monotonic())) if rid else 0.0
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
        cost = st.pricing.cost_usd(model, usage_out)
        finish_reason = getattr(output, "response_metadata", {}).get("finish_reason", "") or "stop"
        # Wire the per-call Prometheus seam (no-op when unconfigured);
        # previously record_llm_call was defined but never called. The
        # per-call Langfuse generation span comes from the LiteLLM
        # gateway callback — we deliberately don't add a manual shim
        # that would bypass trace_session's nesting (see tracing.py).
        try:
            st.metrics.record_llm_call(
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


def _on_custom_usage(st: _TurnStreamState, event: dict, name: str, parent_tool_id) -> _Frames:
    # A delegation's model usage. Two producers dispatch into this lane:
    # the `task`/`task_batch` tool body after its sub-graph settles
    # (#2872), and the delegates a2a adapter with an A2A peer's OWN
    # cost-v1 telemetry read off the terminal artifact (#3016). Same
    # shape as the on_chat_model_end frame plus a `subagent_type`
    # or `peer` tag (the executor keeps tagged rows out of the lead
    # thread's context-window fill); forwarded verbatim so the turn bills
    # delegated work.
    data = event.get("data")
    if isinstance(data, dict):
        yield ("usage", dict(data))


def _on_custom_steer_consumed(st: _TurnStreamState, event: dict, name: str, parent_tool_id) -> _Frames:
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


# `astream_events` kind → handler. Kinds not listed produce nothing (skills are no
# longer auto-retrieved per turn — ADR 0060 progressive disclosure; the model loads one
# on demand via the `load_skill` tool, an ordinary tool card — so there is no
# `skills_loaded` event to forward).
_EVENT_HANDLERS: dict[str, Callable[..., _Frames]] = {
    "on_chat_model_start": _on_chat_model_start,
    "on_tool_start": _on_tool_start,
    "on_tool_end": _on_tool_end,
    "on_chat_model_stream": _on_chat_model_stream,
    "on_chat_model_end": _on_chat_model_end,
}
# `on_custom_event` name → handler; other custom events produce nothing.
_CUSTOM_EVENT_HANDLERS: dict[str, Callable[..., _Frames]] = {
    "usage": _on_custom_usage,
    "steer_consumed": _on_custom_steer_consumed,
}


def _handler_for(kind: str, name: str) -> Callable[..., _Frames] | None:
    """The handler for one ``astream_events`` event, or None to skip it."""
    if kind == "on_custom_event":
        return _CUSTOM_EVENT_HANDLERS.get(name)
    return _EVENT_HANDLERS.get(kind)


def _fence_update(fence) -> dict:
    """``Command`` kwargs that stamp a per-turn tool fence (#1639/#2972) on a RESUME pass.

    A fresh pass stamps ``subagent_fence`` in its input dict; a ``Command(resume=…)`` has no
    input dict, so the fence rides the command's state update instead — every pass of a
    fenced turn carries it. Unfenced → no update: a resume continues the parked turn, which
    keeps the fence it was stamped with (a resume never clears one)."""
    if not fence:
        return {}
    return {"update": {"subagent_fence": [str(t) for t in fence]}}


async def _resume_fence_update(config: dict, fence) -> dict:
    """``Command`` kwargs for the fence of a RESUME pass: narrowest wins.

    A resume continues a PARKED turn whose checkpoint may already hold a fence. A fenced
    resumer runs the pass under the INTERSECTION of its fence and the parked one
    (``graph.middleware.subagent_fence.intersect_fences``) — never the resumer's alone,
    which could widen a parked fenced turn (parked ``[ask_human]`` resumed with
    ``[ask_human, current_time]`` must not run ``current_time``). Unfenced → no update:
    the parked turn keeps its own fence. An unreadable checkpoint fails CLOSED (deny-all
    for the rest of the pass; the parked call itself still completes — see
    ``is_resumed_parked_call``)."""
    if not fence:
        return {}
    from graph.middleware.subagent_fence import FENCE_DENY_ALL, intersect_fences

    try:
        snapshot = await STATE.graph.aget_state(config)
        values = getattr(snapshot, "values", None) or {}
        parked = values.get("subagent_fence") if isinstance(values, dict) else None
    except Exception:  # noqa: BLE001 — can't see the parked fence: fail closed
        log.warning("[fence] could not read the parked turn's fence; resuming with no tools", exc_info=True)
        return _fence_update([FENCE_DENY_ALL])
    return _fence_update(intersect_fences(parked, fence))


async def _carried_fence(config: dict, fence) -> list[str]:
    """The fence the NEXT fresh pass of this turn runs under, after a pass on ``config``.

    Every fresh pass stamps its fence explicitly (an unfenced one stamps ``[]``), so a
    later pass of the SAME turn — a goal continuation, the context-overflow retry — must
    not re-stamp the turn's ORIGINAL fence: a fenced message folded into the earlier
    pass (steering, #2972) narrowed that pass's fence on the checkpoint, and the rest of
    the turn keeps the narrowing (narrowest wins — ``intersect_fences``). ``[]`` = no
    fence. An unreadable checkpoint fails CLOSED (deny-all), as a fenced resume does."""
    from graph.middleware.subagent_fence import FENCE_DENY_ALL, intersect_fences

    try:
        snapshot = await STATE.graph.aget_state(config)
        values = getattr(snapshot, "values", None) or {}
        current = values.get("subagent_fence") if isinstance(values, dict) else None
    except Exception:  # noqa: BLE001 — can't see the narrowed fence: fail closed
        log.warning("[fence] could not read the pass's fence; the rest of the turn runs with no tools", exc_info=True)
        return [FENCE_DENY_ALL]
    return intersect_fences(current, fence)


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

    human = _chat()._vision_human_message(message, images, session_id=session_id, incognito=incognito)
    background_messages, background_replies = (
        ([], []) if resume_value is not None else _chat()._drain_background(session_id)
    )
    for reply in background_replies:
        # Replies arrive as themselves before the lead reads the same authored
        # messages and streams its synthesis. Store ordering is completion ordering.
        yield ("room_reply", reply)

    graph_input = (
        Command(
            resume=await _chat()._resume_payload(config, resume_value),
            **(await _resume_fence_update(config, subagent_fence)),
        )
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
            # Per-turn tool fence (#1639/#2972) — SubagentFenceMiddleware blocks tool
            # calls outside it. Always stamped, like incognito: the channel persists in
            # the checkpointer, so an omitted key would inherit whatever fence the
            # thread last held (a fenced turn, or a fenced message folded into one) and
            # leave an ordinary operator turn fenced. ``[]`` = no fence. A server-fired
            # turn that must keep its origin's fence carries it in its metadata.
            "subagent_fence": [str(t) for t in (subagent_fence or [])],
        }
    )
    from observability import metrics
    from observability import pricing

    st = _TurnStreamState(metrics=metrics, pricing=pricing)
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
        handler = _handler_for(kind, name)
        if handler is None:
            continue
        # A sync generator, re-yielded frame by frame (see the section comment above
        # `_TurnStreamState`): no extra await, same frame timing, closed here.
        with contextlib.closing(handler(st, event, name, parent_tool_id)) as frames:
            for frame in frames:
                yield frame

    # HITL pause (ADR 0003): the agent called ask_human → LangGraph interrupt().
    # The graph is checkpointed at the interrupt; surface the question so the A2A
    # layer parks the task as input-required. Resume later with resume_value.
    interrupt_val = await _chat()._pending_interrupt_value(config)
    if interrupt_val is not None:
        yield ("input_required", _chat()._interrupt_payload(interrupt_val))
        return

    yield ("__raw__", st.accumulated_raw)
