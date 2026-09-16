"""LLM factory for the protoAgent LangGraph runtime.

All models route through the LiteLLM gateway (OpenAI-compatible),
so we use ChatOpenAI for everything.
"""

import asyncio
import contextvars
import hashlib
import json
import logging
import math
import os
import re
import time
from collections.abc import AsyncIterator, Callable

import httpcore
import httpx
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import Field

from graph.config import PROVIDER_TYPE_OPENAI_COMPAT, LangGraphConfig, Provider, resolve_model_route
from graph.providers.identity import tag_model_provider

log = logging.getLogger(__name__)

# Same allowlisted UA the chat client uses (Cloudflare WAF blocks the SDK default).
_GATEWAY_UA = "protoAgent/0.1 (+https://github.com/protoLabsAI/protoAgent)"

# A provider stream can drop mid-read — an `httpcore.ReadError` / `httpx.TransportError`
# (or a read timeout) surfaces while iterating the SSE body. `ChatOpenAI(max_retries=…)`
# retries the request *start*, never a mid-body read, so a rate-limited or flaky gateway
# that terminates the response kills the whole turn (#1728). These are the read/transport
# failures we treat as retryable when the stream produced NOTHING yet.
# langchain_openai's stall guard: the socket is alive but the provider stopped producing
# for `stream_chunk_timeout` (120s default). It is NOT an httpx/httpcore error — it
# subclasses TimeoutError/OSError — so it fell through this tuple entirely and no stall was
# ever retried, which is why a stalled turn was lost outright with no partial and no retry
# (#2305). Imported defensively: an older langchain_openai has no such class, and a missing
# optional symbol must not break llm construction.
try:  # pragma: no cover - trivial import guard
    from langchain_openai import StreamChunkTimeoutError as _StreamChunkTimeoutError

    _STALL_ERRORS: tuple[type[BaseException], ...] = (_StreamChunkTimeoutError,)
except ImportError:  # pragma: no cover
    _STALL_ERRORS = ()

RETRYABLE_STREAM_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TransportError,  # httpx.ReadError / ConnectError / ReadTimeout / …
    httpcore.NetworkError,  # httpcore.ReadError / WriteError / ConnectError (raw, unwrapped)
    httpcore.TimeoutException,  # httpcore.ReadTimeout / ConnectTimeout
    *_STALL_ERRORS,  # provider went silent mid-stream (#2305)
)

# Provider phrasings for "the prompt no longer fits the context window" (#2783,
# ADR 0101 D4) — matched as lowercase substrings against the error text.
# Deliberately conservative: a false positive here triggers a destructive
# force-compaction, a false negative just keeps today's behavior (raw error).
_CONTEXT_OVERFLOW_MARKERS: tuple[str, ...] = (
    "context_length_exceeded",  # OpenAI / LiteLLM error code
    "maximum context length",  # OpenAI message text
    "prompt is too long",  # Anthropic messages API
    "exceed context limit",  # Anthropic: "input length and max_tokens exceed context limit"
    "input is too long for requested model",  # Bedrock Anthropic
)


def is_context_overflow_error(exc: BaseException) -> bool:
    """Whether ``exc`` is a provider context-window overflow (#2783).

    Nothing caught this class anywhere before: the raw error surfaced,
    ``ModelFallbackMiddleware`` re-sent the same oversized prompt elsewhere,
    and the NEXT turn on the thread hit the same wall. Callers use this to
    force-compact once and retry, turning a dead end into a recovered event.
    """
    text = str(exc).lower()
    return any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS)


def _chunk_has_content(item: object) -> bool:
    """Did this streamed chunk carry anything a consumer has already SEEN?

    The reconnect rule below is about not duplicating user-visible output. The first
    chunk of an OpenAI stream is the *role* delta — ``{"role": "assistant"}`` with empty
    content — so a stall right after it (the exact ``chunks_received=1`` signature in
    #2305) has shown the user nothing and is safe to replay.

    Deliberately CONSERVATIVE: anything we can't read is treated as content, so an
    unfamiliar chunk shape costs a retry we could have had rather than risking a
    duplicated answer.
    """
    for obj in (getattr(item, "message", None), item):
        if obj is None:
            continue
        content = getattr(obj, "content", None)
        if content:
            return True
        # Reasoning rides its own channel and IS rendered, so it counts as seen.
        extra = getattr(obj, "additional_kwargs", None)
        if isinstance(extra, dict) and extra.get("reasoning_content"):
            return True
        if content == "" or content == []:
            return False  # a shape we understand, and it's empty
    return True  # unknown shape — assume it was seen


_STREAM_RETRY_BACKOFF_S = 0.5


async def _stream_with_reconnect(
    make_stream: Callable[[], AsyncIterator],
    *,
    max_retries: int,
    backoff: float = _STREAM_RETRY_BACKOFF_S,
    sleep: Callable = asyncio.sleep,
) -> AsyncIterator:
    """Yield from a model stream, restarting it on a transport/read error or a provider
    stall that occurs **before any CONTENT has been emitted**.

    Retrying is safe exactly while nothing user-visible has streamed: each content chunk
    fires its ``on_llm_new_token`` callback as it's yielded, so replaying after one would
    duplicate it — there we re-raise. This reconnects the model call only; it never
    replays tools or restarts the turn.

    The bar is CONTENT, not items (#2305). The first chunk of an OpenAI stream is the role
    delta with empty content, so a stall one chunk in — ``chunks_received=1``, the exact
    signature in #2305 — had shown the user nothing yet still counted as "emitted" and
    killed the turn. ``_chunk_has_content`` draws the line where duplication actually
    starts, and errs toward not retrying when a chunk's shape is unfamiliar.

    A provider closing the stream at the top (the rate-limit case in #1728) emits nothing,
    so it still reconnects cleanly.
    """
    attempts = max(max_retries, 0) + 1
    delay = backoff
    for attempt in range(attempts):
        emitted_content = False
        try:
            async for item in make_stream():
                emitted_content = emitted_content or _chunk_has_content(item)
                yield item
            return
        except RETRYABLE_STREAM_ERRORS as exc:
            if emitted_content or attempt == attempts - 1:
                raise
            # Two different failures reach here and the distinction matters when reading
            # logs: a transport drop (provider CLOSED the stream — often a rate limit) vs
            # a stall (socket still open, provider went SILENT — #2305).
            cause = (
                "provider went silent mid-stream"
                if _STALL_ERRORS and isinstance(exc, _STALL_ERRORS)
                else "provider closed stream; possible rate limit"
            )
            log.warning(
                "model stream dropped before any content (%s: %s) — reconnecting, attempt %d/%d in %.1fs (%s)",
                type(exc).__name__,
                str(exc)[:200],
                attempt + 1,
                attempts - 1,
                delay,
                cause,
            )
            await sleep(delay)
            delay *= 2


def _gateway_wire_default() -> bool | None:
    """The `use_responses_api` default for the OpenAI-compatible gateway client.

    ``False`` pins `/v1/chat/completions`. ``None`` hands the choice back to
    langchain-openai's own per-call inference, which is what the escape hatch buys — not
    "always Responses", since the operator wanting it has a mix of models. Read per
    instantiation rather than at import so a test can flip it."""
    if os.environ.get("PROTOAGENT_GATEWAY_RESPONSES_API", "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    return False


# ── Output budget sized to fit the window (#3502) ─────────────────────────────────────
# `model.max_tokens` is a flat output reservation, and providers like vLLM enforce
# prompt + max_tokens <= window. A prompt within that reservation of the window was
# rejected outright ("...maximum context length is 262144 tokens. However, you requested
# 32768 output tokens...") even when a smaller output budget would have fit, and a subagent
# lane, which runs no pruning or compaction, had no other way out.
#
# WHICH window: the one the provider enforces, learned from its own overflow error. The
# gateway's advertised ``max_input_tokens`` can't serve. It may be an input-only cap with a
# separate output cap (gpt-5: 272k in + 128k out), or an operator's declaration rather than
# the backend's limit (protolabs/smart advertises 196,608 in front of a 262,144-token vLLM),
# and sizing off either would cut the budget of calls that succeed today. A message saying
# prompt + output exceeded W states the shared limit exactly. So nothing changes until a
# model overflows once in this process: that call is retried once with the budget that
# fits, and later calls on the model are sized before they're sent.
#
# The prompt is MEASURED, not guessed: chars/4 undercounts code by ~20%, more than the whole
# margin that matters here. Each finished call records its real ``input_tokens`` per
# character of request body, per conversation, and later requests are sized off that.
# "endpoint|model" -> (the shared window its provider enforces, when it was learned). Keyed by
# endpoint too, so two connections serving one model id with different windows never size
# each other; expired after an hour, so an operator raising the window isn't held to the old one.
_LEARNED_WINDOWS: dict[str, tuple[int, float]] = {}
_LEARNED_WINDOW_TTL_S = 3_600.0
_CALIBRATIONS: dict[str, tuple[float, int]] = {}  # key -> (tokens per char, chars measured)
_MAX_CALIBRATIONS = 256
# Calls this small are dominated by chat-template and tool-schema overhead that the request
# body doesn't show, which would inflate the ratio — and they're never the ones that overflow.
_MIN_CALIBRATION_TOKENS = 8_000
# Below this much room a lowered budget can't do useful work (a thinking model spends part
# of it reasoning), so the request is left alone and the overflow reaches the
# force-compact-and-retry path (server/chat.py, #2783), the better recovery there.
_MIN_OUTPUT_TOKENS = 8_192
_BUDGET_MARGIN_FRACTION = 0.02  # tokenization drift between the measured call and this one
_BUDGET_MARGIN_TOKENS = 1_024
# Provider phrasings that state a SHARED prompt + output limit: vLLM's, and OpenAI's classic
# one. Anything else — an input-only cap, "prompt is too long" — teaches nothing.
_SHARED_WINDOW_RES = (
    re.compile(r"maximum context length is (\d+) tokens\. However, you requested \d+ output tokens"),
    re.compile(
        r"maximum context length is (\d+) tokens\. However, you requested \d+ tokens "
        r"\(\d+ in the messages, \d+ in the completion\)"
    ),
)
# (calibration key, request chars, budget sent) for the request this context just built —
# paired with that call's usage when its stream ends. A ContextVar, so concurrent calls never cross.
_REQUEST_MEASURE: contextvars.ContextVar[tuple[str, int, int] | None] = contextvars.ContextVar(
    "_request_measure", default=None
)


# Content parts that aren't text. Their tokens bear no relation to their (base64) size.
_MEDIA_PART_TYPES = frozenset({"image_url", "input_image", "input_audio", "file"})


def _is_media_part(part: object) -> bool:
    return isinstance(part, dict) and part.get("type") in _MEDIA_PART_TYPES


def _has_media(payload: dict) -> bool:
    return any(
        isinstance(m, dict) and isinstance(m.get("content"), list) and any(map(_is_media_part, m["content"]))
        for m in payload.get("messages") or []
    )


def _request_chars(payload: dict) -> int:
    """Characters in the TEXT of a chat-completions body — what becomes prompt tokens in
    proportion to its length. Media parts are left out: an attached image's base64 would
    inflate the estimate and lower the budget of a call that fits."""
    messages = []
    for m in payload.get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list) and any(map(_is_media_part, content)):
            m = {**m, "content": [part for part in content if not _is_media_part(part)]}
        messages.append(m)
    return len(json.dumps(messages, ensure_ascii=False)) + len(json.dumps(payload.get("tools") or [], ensure_ascii=False))


def _opening(message: object, limit: int = 512) -> str:
    content = message.get("content", "") if isinstance(message, dict) else ""
    return (content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))[:limit]


def _calibration_key(model: str, payload: dict) -> str:
    """Model + the openings of the system prompt AND the first non-system message.

    One key per CONVERSATION, not per agent. Parallel lanes of one subagent type share a
    system prompt but not a task, and two chats with one agent share a system prompt but
    not a first turn — and their content mix, so their tokens per char, differs (JSON tool
    output runs ~0.35, code ~0.22). A sibling's ratio would size this request wrong. When
    compaction rewrites the head, the conversation simply starts a fresh key. The task is
    hashed to 8 KB, not 512 chars: templated lanes share long preambles before the part
    that tells them apart."""
    messages = [m for m in payload.get("messages") or [] if isinstance(m, dict)]
    system = next((m for m in messages if m.get("role") in ("system", "developer")), None)
    first = next((m for m in messages if m.get("role") not in ("system", "developer")), None)
    head = f"{_opening(system)}\x00{_opening(first, 8_192)}"
    return f"{model}|{hashlib.sha1(head.encode()).hexdigest()[:16]}"


def _learn_window(exc: BaseException, model: str) -> int | None:
    """The shared window a provider's overflow error states (remembered for ``model``), or None."""
    parts = (exc, getattr(exc, "body", None), exc.__cause__, exc.__context__)
    text = " ".join(str(part) for part in parts if part)
    for pattern in _SHARED_WINDOW_RES:
        match = pattern.search(text)
        if match:
            window = int(match.group(1))
            if model and window > 0:
                _LEARNED_WINDOWS[model] = (window, time.monotonic())
            return window
    return None


def _learned_window(model: str) -> int | None:
    """The shared window learned for ``model`` ("endpoint|model"), unless it has expired."""
    learned = _LEARNED_WINDOWS.get(model)
    if not learned:
        return None
    window, at = learned
    if time.monotonic() - at > _LEARNED_WINDOW_TTL_S:
        _LEARNED_WINDOWS.pop(model, None)
        return None
    return window


def _fitted_budget(cal_key: str, chars: int, requested: int, window: int | None) -> int | None:
    """The output budget that fits ``window`` beside this request, or None to send it as is.

    None unless the window is known, this conversation has a measurement from a request at
    least half this size, and the estimated prompt leaves at least ``_MIN_OUTPUT_TOKENS``
    but less than ``requested``."""
    if not window or requested <= _MIN_OUTPUT_TOKENS:
        return None
    calibration = _CALIBRATIONS.get(cal_key)
    if not calibration:
        return None
    ratio, measured_chars = calibration
    # A ratio measured on a much smaller request carries chat-template and tool-schema
    # overhead the body doesn't show (~8% near the calibration floor), so it overstates a
    # big prompt. A conversation that nears the window grew into it a step at a time, so
    # its previous call is comparable; a sudden jump is left alone.
    if measured_chars * 2 < chars:
        return None
    prompt = math.ceil(chars * ratio * (1 + _BUDGET_MARGIN_FRACTION)) + _BUDGET_MARGIN_TOKENS
    room = window - prompt
    if room >= requested or room < _MIN_OUTPUT_TOKENS:
        return None
    return room


def _fit_output_budget(payload: dict, model: str) -> None:
    """Measure this request for calibration and, once ``model``'s shared window is learned,
    lower its output budget in place when the prompt won't fit beside it (#3502)."""
    _REQUEST_MEASURE.set(None)
    if not isinstance(payload.get("messages"), list):
        return
    key = "max_completion_tokens" if "max_completion_tokens" in payload else "max_tokens"
    requested = payload.get(key)
    if not isinstance(requested, int) or requested <= _MIN_OUTPUT_TOKENS:
        return
    chars = _request_chars(payload)
    cal_key = _calibration_key(model, payload)
    window = _learned_window(model)
    room = _fitted_budget(cal_key, chars, requested, window)
    if room is not None:
        payload[key] = room
        log.info(
            "[llm] output budget %d -> %d so this prompt fits %s's %d-token window (#3502)",
            requested,
            room,
            model,
            window,
        )
    # A call carrying media doesn't calibrate: its reported tokens include the images,
    # which the char count leaves out, so its ratio would overstate every later prompt.
    _REQUEST_MEASURE.set(None if _has_media(payload) else (cal_key, chars, payload[key]))


def _record_calibration(measure: tuple[str, int, int] | None, input_tokens: object) -> None:
    """Store a finished call's real tokens-per-char for its conversation."""
    if not measure or not isinstance(input_tokens, int) or input_tokens < _MIN_CALIBRATION_TOKENS:
        return
    cal_key, chars, _sent = measure
    if chars <= 0:
        return
    _CALIBRATIONS.pop(cal_key, None)  # re-insert last, so the cap evicts the stalest
    _CALIBRATIONS[cal_key] = (input_tokens / chars, chars)
    while len(_CALIBRATIONS) > _MAX_CALIBRATIONS:
        _CALIBRATIONS.pop(next(iter(_CALIBRATIONS)), None)


class _ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI that surfaces the gateway's NATIVE reasoning stream.

    The base class deliberately drops the non-OpenAI ``reasoning_content`` delta field
    ("Use a provider-specific subclass" — its own docstring); DeepSeek and most reasoning
    models routed through our LiteLLM gateway emit it token-by-token. We lift it into the
    message's ``additional_kwargs`` so the chat stream can render the model's REAL thinking
    in real time, instead of a prompted ``<scratch_pad>`` narration.
    """

    # This client talks to OpenAI-COMPATIBLE endpoints — our LiteLLM gateway, a local
    # vLLM, LM Studio, Ollama — and `/v1/chat/completions` is the only wire all of them
    # are guaranteed to speak. `/v1/responses` is an OpenAI-specific surface most of them
    # do not implement at all.
    #
    # Left as `None`, langchain-openai decides per call, and it decides from the MODEL
    # NAME: `_model_prefers_responses_api()` returns True for a handful of prefixes and
    # for any name merely CONTAINING "codex". That clause is already in the locked 1.6.0,
    # so a `protolabs/codex` gateway slot was misrouted on the shipped version; 1.6.1
    # only added `gpt-5.6-sol` to the prefixes, which is what the canary caught (#3392).
    # Gateway aliases are names WE
    # choose (`protolabs/codex`, `gateway:codex`) and say nothing about the wire the
    # endpoint behind them speaks, so that inference reads a property of the model off a
    # string that describes our routing — and silently re-points the request at an
    # endpoint that 404s. It also empties `payload["messages"]`, which quietly disarms the
    # `reasoning_content` round-trip below (#2642) down to a log line.
    #
    # So the wire is pinned here rather than inferred. The native ChatGPT/Codex backend
    # genuinely does speak Responses and opts IN explicitly — `graph.providers.openai_codex`
    # passes `use_responses_api=True`, and an explicit constructor kwarg still wins over
    # this default. Set PROTOAGENT_GATEWAY_RESPONSES_API=1 to hand the decision back to
    # langchain (an openai-compat connection pointed straight at api.openai.com with a
    # Responses-only model is the case that wants it).
    use_responses_api: bool | None = Field(default_factory=_gateway_wire_default)

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        gen = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        if gen is not None:
            choices = chunk.get("choices") or []
            reasoning = (choices[0].get("delta") or {}).get("reasoning_content") if choices else None
            if reasoning:
                gen.message.additional_kwargs["reasoning_content"] = reasoning
        return gen

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        """Round-trip ``reasoning_content`` back out on the NEXT turn (#2642).

        The base class's outbound message-dict builder drops it — same "use a
        provider-specific subclass" non-preservation the docstring on
        ``_convert_chunk_to_generation_chunk`` above already routes around for the
        inbound side. Once any tool call has occurred, DeepSeek 400s a later turn
        that's missing ``reasoning_content`` on an assistant message — even an
        empty string satisfies it, but an ABSENT key doesn't, so every assistant
        message gets the key once thinking is on, not just the ones that happen to
        carry a captured value (a tool-only turn else re-400s on the very case this
        fix was written for). Gated on ``extra_body.thinking`` (#1113) — a no-op for
        every model that doesn't set it, so this never touches Claude/GPT/ungated
        gateway slots."""
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if (self.extra_body or {}).get("thinking", {}).get("type") == "enabled":
            messages = self._convert_input(input_).to_messages()
            payload_messages = payload.get("messages") or []
            if len(messages) != len(payload_messages):
                # Base-class conversion is 1:1 today (verified against a multi-turn
                # tool-call transcript) — if a future langchain_openai starts
                # merging/splitting messages this silently degrades back to the bug
                # this fix exists for, so surface it instead of guessing an alignment.
                log.warning(
                    "[llm] reasoning_content round-trip skipped: input/payload message "
                    "count mismatch (%d vs %d) — a later turn may 400 (#2642)",
                    len(messages),
                    len(payload_messages),
                )
            else:
                for msg, msg_dict in zip(messages, payload_messages, strict=True):
                    if msg_dict.get("role") == "assistant":
                        extra = getattr(msg, "additional_kwargs", None) or {}
                        msg_dict["reasoning_content"] = extra.get("reasoning_content") or ""
        try:
            _fit_output_budget(payload, self._window_key())
        except Exception:  # noqa: BLE001 — sizing must never break a request; send it as configured
            log.debug("[llm] output-budget sizing skipped", exc_info=True)
        return payload

    async def _astream(self, *args, **kwargs):
        """Reconnect a provider stream that drops before emitting any content (#1728).

        Transparent pass-through on the happy path; on a mid-read transport error with
        zero chunks yielded it reconnects (within the model's ``max_retries`` budget)
        instead of letting the error kill the turn — the failure mode a rate-limited
        gateway produces. Once a chunk has streamed, the error propagates unchanged.

        A context overflow that states the provider's shared window is retried ONCE with
        the output budget that fits, when this conversation's measurements can size it
        (#3502); otherwise the error propagates exactly as before."""
        state: dict = {}
        try:
            async for chunk in self._stream_measured(args, kwargs, state):
                yield chunk
            return
        except Exception as exc:
            retry = None if state.get("streamed") else self._overflow_retry_budget(exc)
            if retry is None:
                raise
            log.warning(
                "[llm] %s overflowed its %d-token window; retrying once with max_tokens=%d (#3502)",
                self.model_name,
                _learned_window(self._window_key()) or 0,
                retry,
            )
        async for chunk in self._stream_measured(args, {**kwargs, "max_tokens": retry}, {}):
            yield chunk

    async def _stream_measured(self, args, kwargs, state: dict):
        """One provider stream (with #1728 reconnects) whose usage calibrates the output
        budget of this conversation's later requests (#3502)."""
        input_tokens = None
        measure = None
        async for chunk in _stream_with_reconnect(
            lambda: super(_ReasoningChatOpenAI, self)._astream(*args, **kwargs),
            max_retries=self.max_retries or 0,
        ):
            if not state.get("streamed"):
                # Read on the FIRST chunk: this request's body is built by then, and a model
                # streamed on the same task while we yield can't overwrite what we pair with.
                measure, state["streamed"] = _REQUEST_MEASURE.get(), True
            usage = getattr(getattr(chunk, "message", None), "usage_metadata", None)
            if usage and usage.get("input_tokens"):
                input_tokens = usage["input_tokens"]
            yield chunk
        try:
            _record_calibration(measure, input_tokens)
        except Exception:  # noqa: BLE001 — bookkeeping must never fail a finished call
            log.debug("[llm] output-budget calibration skipped", exc_info=True)

    def _window_key(self) -> str:
        """Where a learned window applies: this endpoint + model (#3502)."""
        return f"{self.openai_api_base or ''}|{self.model_name or ''}"

    def _overflow_retry_budget(self, exc: BaseException) -> int | None:
        """The budget to retry an overflowed request with, or None to let the error stand."""
        try:
            if not is_context_overflow_error(exc):
                return None
            window = _learn_window(exc, self._window_key())
            measure = _REQUEST_MEASURE.get()  # the failed request's own measurement
            if not window or not measure:
                return None
            cal_key, chars, sent = measure
            return _fitted_budget(cal_key, chars, sent, window)
        except Exception:  # noqa: BLE001 — never mask the provider's error with ours
            log.debug("[llm] overflow retry sizing skipped", exc_info=True)
            return None


def _gateway_configured(config: LangGraphConfig, provider: "Provider | None" = None) -> bool:
    """Is there a usable OpenAI-compatible gateway key (config or env)?

    Defined here rather than borrowed from ``runtime.acp_runtime`` — the question is
    "can the gateway path build?", which belongs to this module, and the ACP runtime is
    deprecated (#2548)."""
    if provider is not None:
        # A registered connection is usable when it has somewhere to talk to. An ENDPOINT
        # is the requirement — a key is optional (local endpoints want none) — and neither
        # is ever borrowed from another connection or from the legacy fields. Accepting a
        # key alone would let the build fall through to the legacy endpoint.
        return bool(resolve_model_route(config, provider).base_url)
    return bool(resolve_model_route(config).api_key)


def _build_gateway_llm(
    config: LangGraphConfig,
    model_name: str | None,
    reasoning_effort: str | None,
    provider: "Provider | None" = None,
) -> BaseChatModel:
    """The gateway client for ``model_name`` (or the config's model when blank).

    Extracted so every path that means "route this through the gateway" shares one
    builder: the default, a `/`-shorthand slot under a native provider, and an explicit
    ``gateway:<alias>`` slot."""
    # A registered openai-compat connection supplies its OWN endpoint and key. This is
    # what makes several gateways possible: `prod-gateway:` and `local-vllm:` build the
    # same client class against different connections, rather than every gateway call
    # inheriting the single `model.api_base`/`api_key` pair.
    kwargs = _build_llm_kwargs(config, provider)
    if provider is not None:
        # A registered connection is resolved STRICTLY from its own fields
        # (`resolve_model_route`), so the kwargs never start from the legacy pair. When
        # they did, merely skipping a blank key left the previous connection's credential
        # in place and sent it to THIS endpoint — a local vLLM receiving the production
        # gateway's key — and falling back to the legacy `model.api_base` sent THIS
        # connection's key to the legacy gateway's endpoint, the same coupling mirrored.
        # The migrated `gateway` entry carries the legacy base/key (env included), so
        # nothing that worked before loses its credential here. A connection with no
        # endpoint has nowhere to talk to and is rejected by `_gateway_configured` above.
        #
        # langchain requires SOMETHING; a keyless endpoint (a local vLLM, Ollama) is
        # normal, so a placeholder goes on the wire rather than another connection's key.
        kwargs["api_key"] = kwargs["api_key"] or "not-needed"
    if model_name:
        kwargs["model"] = model_name
    # Per-turn reasoning-effort override (the /effort chat command). When the turn carries
    # an explicit effort it wins over the config default for THIS build; the middleware
    # caches per (model, effort) so the rebuild is paid once.
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    # Context window (#1378): seed the model profile with the gateway's reported
    # max_input_tokens so SummarizationMiddleware can resolve fraction:/tokens: compaction
    # (instead of falling back to a message count) and the chat context meter (#1372) gets a
    # real denominator. Best-effort + cached — an unknown window just omits the profile.
    try:
        from graph.model_window import context_window_for

        win = context_window_for(config, kwargs.get("model"))
        if win:
            kwargs.setdefault("profile", {"max_input_tokens": win})
    except Exception:  # noqa: BLE001 — model-info must never break model creation
        log.debug("[llm] context-window resolution skipped", exc_info=True)
    return _ReasoningChatOpenAI(**kwargs)


# Slot names may name their own provider: `<provider>:<model>` routes THIS call there
# regardless of the main `model.provider`. Extends the `acp:<agent>` convention that
# already existed to the other three lanes, so an operator with a gateway key, a Claude
# subscription and a ChatGPT subscription can mix all of them across slots.
GATEWAY_SLOT = "gateway"
# The ids a pre-ADR-0106 config implies, and therefore the ones every already-stored
# slot value uses. Kept as the fallback whitelist for a config that has no registry
# (a bare LangGraphConfig in a test, a caller that never loaded YAML), so the grammar
# means the same thing there as it always did.
_LEGACY_SLOT_PROVIDERS = (GATEWAY_SLOT, "anthropic-oauth", "openai-codex")


def split_slot_target(model_name: str | None, config: LangGraphConfig | None = None) -> tuple[str, str]:
    """``"openai-codex:gpt-5.6-sol"`` → ``("openai-codex", "gpt-5.6-sol")``.

    ``("", name)`` when unqualified, which keeps every existing slot value meaning
    exactly what it meant before. A prefix is only claimed when it names a REGISTERED
    provider (ADR 0106) — previously a hardcoded triple — which is what lets an
    operator add `prod-gateway:` or `local-vllm:` without touching this module, while
    `bedrock:anthropic.claude` stays a model name because no such provider is
    registered. Provider ids cannot contain a colon or slash, so the split stays
    unambiguous however many are registered (`acp:` is handled separately, upstream).
    """
    raw = (model_name or "").strip()
    prefix, sep, rest = raw.partition(":")
    if not sep:
        return "", raw
    if config is None or not config.providers:
        # Only the legacy union below can claim a prefix here. Lazy import: resolved at
        # call time, so the guard test can make it fatal (graph.config is already loaded).
        from graph.config import note_legacy_registry_floor

        note_legacy_registry_floor("split_slot_target")
    # Registered ids UNION the legacy three — the second half is a compatibility floor,
    # not redundancy. Every qualified value stored before ADR 0106 names one of those
    # three, the old hardcoded tuple accepted them unconditionally, and a migrated
    # config only contains the lanes its legacy fields happened to imply: the common
    # `model.provider: openai` + gateway shape migrates to `[gateway]` alone. Without
    # the floor a stored `anthropic-oauth:claude-opus-4-6` aux slot would stop being
    # claimed and get sent to the GATEWAY as a bare model id — silently breaking exactly
    # the gateway-lead-plus-native-slots mixing #2574 was built for. Dispatch resolves an
    # unregistered-but-legacy prefix by its own name, so it routes as it always did.
    # This floor retires with the legacy fields themselves (no earlier than v0.152.0).
    known = set(config.provider_ids()) if config is not None else set()
    known.update(_LEGACY_SLOT_PROVIDERS)
    if prefix.strip().lower() not in known:
        return "", raw
    return prefix.strip().lower(), rest.strip()


def _build_llm_kwargs(config: LangGraphConfig, connection: Provider | None = None) -> dict:
    """Assemble the ChatOpenAI kwargs from config (extracted for testing).

    Endpoint and key come from ``resolve_model_route``: ``connection``'s own when given,
    otherwise the default route."""
    route = resolve_model_route(config, connection)

    kwargs: dict = {
        "base_url": route.base_url,
        "api_key": route.api_key,
        "model": config.model_name,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        # Bound a hung/slow gateway: a per-call timeout + transient-retry cap so a
        # turn fails cleanly instead of hanging the A2A task / SSE stream forever.
        "timeout": config.request_timeout,
        "max_retries": config.llm_max_retries,
        # Stream tokens. The graph runs model nodes via ``ainvoke``; without
        # this, ``astream_events(v2)`` only emits ``on_chat_model_end`` (the whole
        # message at once), so the A2A/console answer lands in one frame at turn
        # end. With streaming on, ``ainvoke`` uses the streaming API under the
        # hood and ``on_chat_model_stream`` fires per token — which the chat
        # driver turns into ``("text", delta)`` events and the executor forwards
        # as incremental artifact-update frames (live token-by-token answers).
        "streaming": True,
        # Forces token-usage info onto the final streaming chunk so
        # `astream_events(v2)` populates `output.usage_metadata` on
        # `on_chat_model_end`. Without this, streaming chunks arrive as
        # AIMessageChunks with usage_metadata=None and we can't emit
        # the cost-v1 extension metadata on the terminal artifact.
        "stream_usage": True,
        # Cloudflare's managed WAF blocks the OpenAI SDK's default
        # `OpenAI/Python <ver>` User-Agent (observed 403 "Your request
        # was blocked" against api.proto-labs.ai). Override with the
        # same identifier `tools/lg_tools.py` uses for outbound fetches
        # so every protoAgent egress presents a consistent, allowlisted
        # UA. If you self-host behind a different edge, this is safe to
        # keep.
        "default_headers": {
            "User-Agent": _GATEWAY_UA,
        },
    }

    # Optional sampling params — only sent when set, so the gateway / model
    # card defaults win otherwise. top_p + presence_penalty are standard
    # OpenAI fields; top_k, repetition_penalty, and chat_template_kwargs ride
    # `extra_body` for vLLM-compatible gateways (not in OpenAI's schema).
    if config.top_p is not None:
        kwargs["top_p"] = config.top_p
    if config.presence_penalty is not None:
        kwargs["presence_penalty"] = config.presence_penalty
    # Reasoning effort (#1113) — a native ChatOpenAI param, so it goes top-level
    # (not extra_body). Only sent when set, so the model card default wins otherwise.
    if config.reasoning_effort:
        kwargs["reasoning_effort"] = config.reasoning_effort

    extra_body: dict = {}
    if config.top_k is not None and config.top_k >= 0:
        extra_body["top_k"] = config.top_k
    if config.repetition_penalty is not None:
        extra_body["repetition_penalty"] = config.repetition_penalty
    if config.chat_template_kwargs:
        extra_body["chat_template_kwargs"] = dict(config.chat_template_kwargs)
    # Thinking mode (#1113) — DeepSeek's spelling rides extra_body like
    # chat_template_kwargs; "" means inherit (omit it entirely).
    if config.thinking in ("enabled", "disabled"):
        extra_body["thinking"] = {"type": config.thinking}
    if extra_body:
        kwargs["extra_body"] = extra_body

    return kwargs


def create_llm(
    config: LangGraphConfig, *, model_name: str | None = None, reasoning_effort: str | None = None
) -> BaseChatModel:
    """Create a LangChain ChatModel from config.

    Default: routes through the LiteLLM gateway which handles provider routing
    (Anthropic, OpenAI, vLLM, etc.) behind a single OpenAI-compatible endpoint.
    When ``model.provider`` is a native OAuth-subscription provider
    (``anthropic-oauth`` / ``openai-codex``, ADR 0097) this returns a Claude/OpenAI
    client authenticated by a coding-agent OAuth token instead — same native pipeline,
    no gateway. Pass ``model_name`` to build an instance for a different model (used
    for compaction / fallback / subagent slots). Pass ``reasoning_effort`` to override
    the config's effort for THIS build (the per-turn /effort chat command).
    """
    # Explicit per-slot ACP override: an `acp:<agent>` model name (e.g. `aux_model: acp:claude`,
    # `goal.eval_model: acp:claude`, `compaction.model: acp:claude`, or a subagent's model) routes
    # THIS call through that ACP agent — regardless of the main runtime or whether a gateway is
    # configured. Lets a strong coding agent (Claude Code/Opus) back the auxiliary slots while the
    # main brain stays on the gateway. Falls back to the gateway model if the ACP path can't build.
    if model_name and model_name.strip().startswith("acp:"):
        try:
            from runtime.acp_runtime import make_acp_aux_model

            return tag_model_provider(
                make_acp_aux_model(config, agent=model_name.split(":", 1)[1].strip() or None),
                "acp",
                "acp",
            )
        except Exception:  # noqa: BLE001 — degrade to the gateway model rather than break the call
            log.warning("[llm] ACP override %r unavailable; using the main model", model_name, exc_info=True)
            model_name = None

    # Explicit per-slot PROVIDER override: `gateway:protolabs/coder`,
    # `anthropic-oauth:claude-sonnet-5`, `openai-codex:gpt-5.6-sol`. Same shape as `acp:`
    # above, extended to the other three lanes — so an operator holding a gateway key, a
    # Claude subscription and a ChatGPT subscription can mix all of them across slots
    # instead of every slot inheriting `model.provider`. The qualified form wins over
    # every heuristic below, and says out loud which account pays for the call.
    slot_provider, slot_model = split_slot_target(model_name, config)
    if not slot_provider and not model_name:
        # The PRIMARY model names its own connection too (ADR 0106):
        # `model.name: prod-gateway:protolabs/reasoning`. Before the registry the lead
        # model belonged to `model.provider` by definition, so a qualified value there
        # was a misconfiguration and this path never looked. Now it is the normal way to
        # say which connection runs the main brain, and it has to win over the
        # lead-provider dispatch below — otherwise the qualified string is handed to the
        # legacy provider whole and rejected as "not an OpenAI model id".
        slot_provider, slot_model = split_slot_target(getattr(config, "model_name", ""), config)
        if slot_provider:
            # Report the value we actually resolved, not the (absent) argument — an
            # error reading "slot model None names the 'prod-gateway' connection" tells
            # the operator nothing about which setting to go and fix.
            model_name = getattr(config, "model_name", "")
    if slot_provider:
        # Dispatch on the registered connection's TYPE (ADR 0106). An id no longer
        # implies a kind — `prod-gateway` and `local-vllm` are both openai-compat — so
        # the registry is what says how to build, and an unregistered prefix never
        # reaches here because `split_slot_target` declined to claim it.
        entry = config.provider_by_id(slot_provider)
        ptype = entry.type if entry is not None else slot_provider
        if ptype == PROVIDER_TYPE_OPENAI_COMPAT or slot_provider == GATEWAY_SLOT:
            if not _gateway_configured(config, entry):
                raise RuntimeError(
                    f"slot model {model_name!r} names the {slot_provider!r} connection, but that "
                    "connection has no base URL or API key of its own. A connection is resolved "
                    "strictly from its own fields — `model.api_key` and OPENAI_API_KEY are NOT "
                    "consulted for it, so that one connection's credential can never be sent to "
                    "another's endpoint. Set them in Settings ▸ Model ▸ Connections."
                )
            return tag_model_provider(
                _build_gateway_llm(config, slot_model or None, reasoning_effort, provider=entry),
                PROVIDER_TYPE_OPENAI_COMPAT,
                slot_provider,
            )
        from graph.providers import build_native_oauth_llm

        return tag_model_provider(
            build_native_oauth_llm(
                ptype, config, model_name=slot_model or None, reasoning_effort=reasoning_effort
            ),
            ptype,
            slot_provider,
        )

    # ACP-only fallback (ADR 0033): when the runtime is an ACP coding agent AND no gateway
    # key is configured, back protoAgent's auxiliary LLM calls (compaction, goal-eval, fact
    # extraction) with that same ACP agent — so an ACP-only setup needs no OpenAI-compatible
    # endpoint. Tightly guarded: native runtimes, and ACP-with-a-gateway-key, are unchanged.
    try:
        # Uses THIS module's `_gateway_configured`, not acp_runtime's identical private
        # copy — importing that one bound it as a function-scoped local, which shadowed
        # the module-level helper for the whole of create_llm (ruff F823).
        from runtime.acp_runtime import is_acp_runtime, make_acp_aux_model

        if is_acp_runtime(config) and not _gateway_configured(config):
            return tag_model_provider(make_acp_aux_model(config), "acp", "acp")
    except Exception:  # noqa: BLE001 — never let the ACP path break native model creation
        log.debug("[llm] ACP aux-model resolution skipped", exc_info=True)

    # Native OAuth-subscription providers (ADR 0097): authenticate THIS call with a
    # coding-agent OAuth token straight through the native pipeline — no gateway, no
    # ACP. Gated on model.provider so the default gateway path below is unchanged. The
    # branch that isn't taken imports nothing (builders are lazy). A per-slot `acp:`
    # override above still wins.
    from graph.providers import build_native_oauth_llm, is_native_oauth_provider

    if is_native_oauth_provider(getattr(config, "model_provider", "")):
        # ...and so does a per-slot GATEWAY alias (#2550). Selecting a subscription
        # provider used to apply it to every slot that inherits — aux_model,
        # compaction.model, goal.eval_model, each subagent — and a gateway alias in one
        # of those RAISED rather than routing (the native builders reject any name
        # containing "/"). So a subscription-backed agent had exactly one lane and no
        # degrade path: LiteLLM's fallback chain can't see a request that bypasses the
        # gateway, and protoAgent has no app-side failover by design.
        #
        # A "/" is the discriminator because it already is one: gateway aliases are
        # namespaced (`protolabs/coder`), native model ids are not (`claude-opus-4-6`).
        # Same shape as the `acp:` prefix above — a namespaced slot name routes
        # elsewhere. This is the mixed native-main / gateway-aux case ADR 0097 listed as
        # a follow-up; declarative failover for the MAIN slot is still open (#2550).
        if model_name and "/" in model_name and _gateway_configured(config):
            log.info(
                "[llm] slot model %r is a gateway alias — routing it through the gateway "
                "rather than the native %s provider",
                model_name,
                config.model_provider,
            )
        else:
            if model_name and "/" in model_name:
                # Falling through would build a client against an empty gateway. Let the
                # native builder raise its own clear error instead, but say why here —
                # "alias ignored" is otherwise invisible. Config load reconciles the
                # aux/subagent/fallback slots to a coherent id ahead of this
                # (graph.config._reconcile_slot_providers), so reaching here with a
                # '/'-alias means either the LEAD pair itself is incoherent or this is a
                # direct runtime slot that bypassed that pass.
                log.warning(
                    "[llm] slot model %r looks like a gateway alias but no gateway key is "
                    "configured; it cannot override the native %s provider (config-load "
                    "coherence validation should have reconciled or flagged this slot)",
                    model_name,
                    config.model_provider,
                )
            return tag_model_provider(
                build_native_oauth_llm(
                    config.model_provider, config, model_name=model_name, reasoning_effort=reasoning_effort
                ),
                config.model_provider,
                config.model_provider,
            )

    return tag_model_provider(
        _build_gateway_llm(config, model_name, reasoning_effort),
        PROVIDER_TYPE_OPENAI_COMPAT,
        GATEWAY_SLOT,
    )


# Embedding calls run INSIDE the turn (recall precedes every model call), so they get
# a short dedicated timeout — never the chat request_timeout, and never the OpenAI
# SDK defaults (600s + 2 retries), which let one hung gateway route freeze every chat
# turn for minutes while the knowledge breaker couldn't trip (#1681: a Cloudflare-524
# embedding outage read as "chat is broken"). No client retries: the gateway owns
# retries/fallbacks; app-side retries only multiply the hang.
_EMBED_TIMEOUT_S = 8.0


def _build_embeddings(config: LangGraphConfig) -> "OpenAIEmbeddings | None":
    """The shared OpenAIEmbeddings client for ``knowledge.embed_model`` against
    the gateway (ADR 0021), or None when no embed model is configured."""
    model = (getattr(config, "embed_model", "") or "").strip()
    if not model:
        return None
    route = resolve_model_route(config)
    return OpenAIEmbeddings(
        base_url=route.base_url,
        api_key=route.api_key,
        model=model,
        default_headers={"User-Agent": _GATEWAY_UA},
        request_timeout=_EMBED_TIMEOUT_S,
        max_retries=0,
        # Send the raw string, not client-side-tokenized int arrays. Langchain's
        # default tokenizes with tiktoken and posts `input` as arrays of token
        # ids, which a LiteLLM/vLLM-style gateway rejects with 422 ("input should
        # be a valid string"). Off = the gateway tokenizes — the portable choice.
        check_embedding_ctx_length=False,
    )


def create_embed_fn(config: LangGraphConfig) -> Callable[[str], list[float]] | None:
    """Build a sync ``text -> vector`` function against the same gateway, or None.

    Used for query embedding and per-chunk fallback. Returns ``None`` when no
    embed model is configured — callers fall back to FTS5. Runtime embedding
    outages are handled by the ``HybridKnowledgeStore`` circuit breaker.
    """
    emb = _build_embeddings(config)
    return emb.embed_query if emb is not None else None


def create_embed_batch_fn(
    config: LangGraphConfig,
) -> Callable[[list[str]], list[list[float]]] | None:
    """Build a sync ``texts -> vectors`` function (one gateway request for the
    whole list), or None. Used by ``add_document`` to embed all of a document's
    chunks in a single round-trip instead of N serial calls (ADR 0021)."""
    emb = _build_embeddings(config)
    return emb.embed_documents if emb is not None else None


# ── direct gateway endpoint calls (the shared HTTP client, #1931) ─────────────
#
# Core AND plugins sometimes need a non-chat, OpenAI-compatible gateway endpoint
# the LangChain clients don't cover — /audio/transcriptions here, /images/* for
# an image plugin. Every such call must reproduce three load-bearing details, so
# they live in ONE factory instead of being re-derived per caller:
#
#   1. the default route's endpoint + key (bearer auth), from ``resolve_model_route``;
#   2. the allowlisted User-Agent — the gateway's Cloudflare WAF 403s default
#      SDK UAs (see the ``default_headers`` note in ``_build_llm_kwargs``);
#   3. the egress-trust property (ADR 0008): the ``api_base`` host is
#      auto-allowlisted by ``security/egress.py`` and the OpenShell network
#      policy, while a provider *backend* host is deny-by-default (a private IP
#      is denied outright) — so callers go through the gateway, never around it.

# Default timeout for a direct gateway call — generous enough for a slow
# generation endpoint, bounded so a hung gateway can't wedge the caller.
_GATEWAY_CLIENT_TIMEOUT_S = 120.0


def _gateway_client_kwargs(config: LangGraphConfig, *, timeout: float) -> dict:
    """The shared httpx client kwargs (base_url + bearer + allowlisted UA + timeout)."""
    route = resolve_model_route(config)
    headers = {"User-Agent": _GATEWAY_UA}
    if route.api_key:
        headers["Authorization"] = f"Bearer {route.api_key}"
    return {"base_url": (route.base_url or "").rstrip("/"), "headers": headers, "timeout": timeout}


def gateway_client(
    config: LangGraphConfig, *, timeout: float = _GATEWAY_CLIENT_TIMEOUT_S, **httpx_kwargs
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` pre-configured for the model gateway (#1931).

    Request relative paths (``await client.post("/images/generations", json=…)``) —
    they resolve under the configured ``api_base`` with bearer auth and the
    allowlisted User-Agent already set, zero hand-set headers. Use it per call
    (``async with``); ``**httpx_kwargs`` forwards to the client constructor
    (e.g. a ``transport`` in tests). Plugins reach this via
    ``graph.sdk.gateway_client()`` (live config resolved for them)."""
    return httpx.AsyncClient(**_gateway_client_kwargs(config, timeout=timeout), **httpx_kwargs)


def gateway_sync_client(
    config: LangGraphConfig, *, timeout: float = _GATEWAY_CLIENT_TIMEOUT_S, **httpx_kwargs
) -> httpx.Client:
    """The sync twin of :func:`gateway_client` — for worker-thread callers like
    the ingestion pipeline (``create_transcribe_fn``)."""
    return httpx.Client(**_gateway_client_kwargs(config, timeout=timeout), **httpx_kwargs)


# Transcription timeout — STT of a long clip is slow (cold model load + minutes
# of audio); generous but bounded so a hung gateway can't wedge an ingest thread.
_TRANSCRIBE_TIMEOUT_S = 600.0


def create_transcribe_fn(
    config: LangGraphConfig,
) -> Callable[[bytes, str], str] | None:
    """Build a sync ``(audio_bytes, filename) -> transcript`` function, or None.

    Posts to the gateway's OpenAI-compatible ``/audio/transcriptions`` endpoint
    (ADR 0021) using ``knowledge.transcribe_model`` (e.g. ``whisper-1``) — so
    audio/video ingestion reuses the same gateway + key as chat/embeddings, no
    local ASR model. Goes through the shared :func:`gateway_sync_client` (not the
    OpenAI SDK) to send the allowlisted User-Agent the gateway's WAF requires.
    Returns ``None`` when no transcribe model is configured; transport/parse
    errors propagate to the ingestion engine, which maps them to a clean
    extraction failure."""
    model = (getattr(config, "transcribe_model", "") or "").strip()
    if not model:
        return None

    def _transcribe(data: bytes, filename: str) -> str:
        with gateway_sync_client(config, timeout=_TRANSCRIBE_TIMEOUT_S) as client:
            resp = client.post(
                "/audio/transcriptions",
                data={"model": model},
                files={"file": (filename or "audio.mp3", data)},
            )
            resp.raise_for_status()
            return (resp.json().get("text") or "").strip()

    return _transcribe


_DESCRIBE_IMAGE_PROMPT = (
    "Describe this image in detail for a text-only model that cannot see it. Transcribe ALL "
    "visible text verbatim (UI labels, code, error messages, captions), then describe the "
    "layout and salient visual content. Be thorough and literal — this description is the "
    "only way the downstream model can understand the image. No preamble."
)


def create_describe_image_fn(
    config: LangGraphConfig,
) -> Callable[[bytes, str, str], str] | None:
    """Build a sync ``(image_bytes, mime, filename) -> description`` function, or None (#1381).

    Lets a TEXT-only chat model "see" an attached image: the bytes are sent to the configured
    vision model (``knowledge.image_describe_model``) as an OpenAI ``image_url`` block, and its
    description + transcribed text is inlined as context by the attachment pipeline — the same
    shape as ``create_transcribe_fn`` does for audio. Returns ``None`` when no describe model is
    configured (images then stay unsupported on non-vision models, with a clear error)."""
    model = (getattr(config, "image_describe_model", "") or "").strip()
    if not model:
        return None
    llm = create_llm(config, model_name=model)

    def _describe(data: bytes, mime: str, filename: str) -> str:
        import base64

        from langchain_core.messages import HumanMessage

        from graph.output_format import extract_output

        uri = f"data:{mime or 'image/png'};base64,{base64.b64encode(data).decode()}"
        resp = llm.invoke(
            [
                HumanMessage(
                    content=[
                        {"type": "text", "text": _DESCRIBE_IMAGE_PROMPT},
                        {"type": "image_url", "image_url": {"url": uri}},
                    ]
                )
            ]
        )
        return extract_output(str(resp.content)).strip()

    return _describe


# Contextual Retrieval (Anthropic) — situate each chunk in its source document
# before embedding/indexing, so a chunk's vector + FTS terms carry doc-level
# context they'd otherwise lack. Improves both semantic and keyword recall.
_CONTEXT_PROMPT = (
    "<document>\n{doc}\n</document>\n\n"
    "Here is a chunk taken from the document above:\n<chunk>\n{chunk}\n</chunk>\n\n"
    "Give a short, succinct context (one sentence, no preamble) that situates this "
    "chunk within the overall document, to improve search retrieval of the chunk. "
    "Answer with ONLY the context sentence."
)


def create_context_fn(
    config: LangGraphConfig,
) -> Callable[[str, str], str] | None:
    """Build a sync ``(document, chunk) -> context sentence`` function, or None.

    Contextual Retrieval (ADR 0021): at ingest, ``add_document`` prepends this
    one-sentence context to each chunk before storing, so the chunk's embedding
    AND its FTS terms carry document-level context. Uses the cheap aux model
    (``routing.aux_model``, else the main model) — classification-grade work.
    The source document is capped at ``knowledge_context_max_doc_chars`` to bound
    the prompt. Sync (mirrors ``create_embed_fn``) so the store stays sync;
    callers run it off the event loop. Errors propagate to the store, which
    degrades to the raw chunk — enrichment never blocks ingest."""
    from graph.agent import _resolve_aux_model

    cap = max(1, int(getattr(config, "knowledge_context_max_doc_chars", 12000)))
    llm = create_llm(config, model_name=_resolve_aux_model(config, ""))

    def _context(document: str, chunk: str) -> str:
        from langchain_core.messages import HumanMessage

        from graph.output_format import extract_output

        prompt = _CONTEXT_PROMPT.format(doc=(document or "")[:cap], chunk=chunk)
        resp = llm.invoke([HumanMessage(content=prompt)])
        return extract_output(str(resp.content)).strip()

    return _context
