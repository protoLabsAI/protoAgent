"""LLM factory for the protoAgent LangGraph runtime.

All models route through the LiteLLM gateway (OpenAI-compatible),
so we use ChatOpenAI for everything.
"""

import logging
import os
from collections.abc import Callable

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from graph.config import LangGraphConfig

log = logging.getLogger(__name__)

# Same allowlisted UA the chat client uses (Cloudflare WAF blocks the SDK default).
_GATEWAY_UA = "protoAgent/0.1 (+https://github.com/protoLabsAI/protoAgent)"


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
        # the cost-v1 DataPart on the terminal artifact.
        "stream_usage": True,
        # Cloudflare's managed WAF blocks the OpenAI SDK's default
        # `OpenAI/Python <ver>` User-Agent (observed 403 "Your request
        # was blocked" against api.proto-labs.ai). Override with the
        # same identifier `tools/lg_tools.py` uses for outbound fetches
        # so every protoAgent egress presents a consistent, allowlisted
        # UA. If you self-host behind a different edge, this is safe to
        # keep.
        "default_headers": {
            "User-Agent": "protoAgent/0.1 (+https://github.com/protoLabsAI/protoAgent)",
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
) -> ChatOpenAI:
    """Create a LangChain ChatModel from config.

    Routes through the LiteLLM gateway which handles provider
    routing (Anthropic, OpenAI, vLLM, etc.) behind a single
    OpenAI-compatible endpoint. Pass ``model_name`` to build an instance
    for a different model on the same gateway (used for compaction /
    fallback models). Pass ``reasoning_effort`` to override the config's
    effort for THIS build (the per-turn /effort chat command).
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

    # ACP-only fallback (ADR 0033): when the runtime is an ACP coding agent AND no gateway
    # key is configured, back protoAgent's auxiliary LLM calls (compaction, goal-eval, fact
    # extraction) with that same ACP agent — so an ACP-only setup needs no OpenAI-compatible
    # endpoint. Tightly guarded: native runtimes, and ACP-with-a-gateway-key, are unchanged.
    try:
        from runtime.acp_runtime import _gateway_configured, is_acp_runtime, make_acp_aux_model

        if is_acp_runtime(config) and not _gateway_configured(config):
            return make_acp_aux_model(config)
    except Exception:  # noqa: BLE001 — never let the ACP path break native model creation
        log.debug("[llm] ACP aux-model resolution skipped", exc_info=True)

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
    local ASR model. Raw httpx (not the OpenAI SDK) to send the allowlisted
    User-Agent the gateway's WAF requires. Returns ``None`` when no transcribe
    model is configured; transport/parse errors propagate to the ingestion
    engine, which maps them to a clean extraction failure."""
    model = (getattr(config, "transcribe_model", "") or "").strip()
    if not model:
        return None
    api_base = (config.api_base or "").rstrip("/")
    api_key = config.api_key or os.environ.get("OPENAI_API_KEY", "")

    def _transcribe(data: bytes, filename: str) -> str:
        import httpx

        headers = {"User-Agent": _GATEWAY_UA}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        with httpx.Client(timeout=_TRANSCRIBE_TIMEOUT_S) as client:
            resp = client.post(
                f"{api_base}/audio/transcriptions",
                headers=headers,
                data={"model": model},
                files={"file": (filename or "audio.mp3", data)},
            )
            resp.raise_for_status()
            return (resp.json().get("text") or "").strip()

    return _transcribe


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
