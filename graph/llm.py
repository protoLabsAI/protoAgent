"""LLM factory for the protoAgent LangGraph runtime.

All models route through the LiteLLM gateway (OpenAI-compatible),
so we use ChatOpenAI for everything.
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable

import httpcore
import httpx
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from graph.config import LangGraphConfig

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


class _ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI that surfaces the gateway's NATIVE reasoning stream.

    The base class deliberately drops the non-OpenAI ``reasoning_content`` delta field
    ("Use a provider-specific subclass" — its own docstring); DeepSeek and most reasoning
    models routed through our LiteLLM gateway emit it token-by-token. We lift it into the
    message's ``additional_kwargs`` so the chat stream can render the model's REAL thinking
    in real time, instead of a prompted ``<scratch_pad>`` narration.
    """

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
        return payload

    async def _astream(self, *args, **kwargs):
        """Reconnect a provider stream that drops before emitting any content (#1728).

        Transparent pass-through on the happy path; on a mid-read transport error with
        zero chunks yielded it reconnects (within the model's ``max_retries`` budget)
        instead of letting the error kill the turn — the failure mode a rate-limited
        gateway produces. Once a chunk has streamed, the error propagates unchanged."""
        async for chunk in _stream_with_reconnect(
            lambda: super(_ReasoningChatOpenAI, self)._astream(*args, **kwargs),
            max_retries=self.max_retries or 0,
        ):
            yield chunk


def _gateway_configured(config: LangGraphConfig) -> bool:
    """Is there a usable OpenAI-compatible gateway key (config or env)?

    Defined here rather than borrowed from ``runtime.acp_runtime`` — the question is
    "can the gateway path build?", which belongs to this module, and the ACP runtime is
    deprecated (#2548)."""
    key = (getattr(config, "api_key", "") or "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
    return bool(key)


def _build_gateway_llm(config: LangGraphConfig, model_name: str | None, reasoning_effort: str | None) -> BaseChatModel:
    """The gateway client for ``model_name`` (or the config's model when blank).

    Extracted so every path that means "route this through the gateway" shares one
    builder: the default, a `/`-shorthand slot under a native provider, and an explicit
    ``gateway:<alias>`` slot."""
    kwargs = _build_llm_kwargs(config)
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
_SLOT_PROVIDERS = (GATEWAY_SLOT, "anthropic-oauth", "openai-codex")


def split_slot_target(model_name: str | None) -> tuple[str, str]:
    """``"openai-codex:gpt-5.6-sol"`` → ``("openai-codex", "gpt-5.6-sol")``.

    ``("", name)`` when unqualified, which keeps every existing slot value meaning
    exactly what it meant before. Unambiguous by construction: no gateway alias or
    native model id contains a colon (`acp:` is handled separately, upstream)."""
    raw = (model_name or "").strip()
    prefix, sep, rest = raw.partition(":")
    if not sep or prefix.strip().lower() not in _SLOT_PROVIDERS:
        return "", raw
    return prefix.strip().lower(), rest.strip()


def _build_llm_kwargs(config: LangGraphConfig) -> dict:
    """Assemble the ChatOpenAI kwargs from config (extracted for testing)."""
    api_key = config.api_key or os.environ.get("OPENAI_API_KEY", "")

    kwargs: dict = {
        "base_url": config.api_base,
        "api_key": api_key,
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

            return make_acp_aux_model(config, agent=model_name.split(":", 1)[1].strip() or None)
        except Exception:  # noqa: BLE001 — degrade to the gateway model rather than break the call
            log.warning("[llm] ACP override %r unavailable; using the main model", model_name, exc_info=True)
            model_name = None

    # Explicit per-slot PROVIDER override: `gateway:protolabs/coder`,
    # `anthropic-oauth:claude-sonnet-5`, `openai-codex:gpt-5.6-sol`. Same shape as `acp:`
    # above, extended to the other three lanes — so an operator holding a gateway key, a
    # Claude subscription and a ChatGPT subscription can mix all of them across slots
    # instead of every slot inheriting `model.provider`. The qualified form wins over
    # every heuristic below, and says out loud which account pays for the call.
    slot_provider, slot_model = split_slot_target(model_name)
    if slot_provider:
        if slot_provider == GATEWAY_SLOT:
            if not _gateway_configured(config):
                raise RuntimeError(
                    f"slot model {model_name!r} asks for the gateway, but no gateway key is "
                    "configured (model.api_key / OPENAI_API_KEY)."
                )
            return _build_gateway_llm(config, slot_model or None, reasoning_effort)
        from graph.providers import build_native_oauth_llm

        return build_native_oauth_llm(
            slot_provider, config, model_name=slot_model or None, reasoning_effort=reasoning_effort
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
            return make_acp_aux_model(config)
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
                # "alias ignored" is otherwise invisible.
                log.warning(
                    "[llm] slot model %r looks like a gateway alias but no gateway key is "
                    "configured; it cannot override the native %s provider",
                    model_name,
                    config.model_provider,
                )
            return build_native_oauth_llm(
                config.model_provider, config, model_name=model_name, reasoning_effort=reasoning_effort
            )

    return _build_gateway_llm(config, model_name, reasoning_effort)


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
    api_key = config.api_key or os.environ.get("OPENAI_API_KEY", "")
    return OpenAIEmbeddings(
        base_url=config.api_base,
        api_key=api_key,
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
#   1. the configured ``api_base`` + ``api_key`` (bearer auth);
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
    api_key = config.api_key or os.environ.get("OPENAI_API_KEY", "")
    headers = {"User-Agent": _GATEWAY_UA}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return {"base_url": (config.api_base or "").rstrip("/"), "headers": headers, "timeout": timeout}


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
