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

    extra_body: dict = {}
    if config.top_k is not None and config.top_k >= 0:
        extra_body["top_k"] = config.top_k
    if config.repetition_penalty is not None:
        extra_body["repetition_penalty"] = config.repetition_penalty
    if config.chat_template_kwargs:
        extra_body["chat_template_kwargs"] = dict(config.chat_template_kwargs)
    if extra_body:
        kwargs["extra_body"] = extra_body

    return kwargs


def create_llm(config: LangGraphConfig, *, model_name: str | None = None) -> ChatOpenAI:
    """Create a LangChain ChatModel from config.

    Routes through the LiteLLM gateway which handles provider
    routing (Anthropic, OpenAI, vLLM, etc.) behind a single
    OpenAI-compatible endpoint. Pass ``model_name`` to build an instance
    for a different model on the same gateway (used for compaction /
    fallback models).
    """
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
    return ChatOpenAI(**kwargs)


def create_embed_fn(config: LangGraphConfig) -> Callable[[str], list[float]] | None:
    """Build a sync ``text -> vector`` function against the same gateway, or None.

    Routes ``knowledge.embed_model`` through the OpenAI-compatible LiteLLM
    gateway (ADR 0021), so semantic search reuses the model infra we already
    have. Returns ``None`` when no embed model is configured — callers fall back
    to FTS5. Runtime embedding outages are handled by the
    ``HybridKnowledgeStore`` circuit breaker, not here.
    """
    model = (getattr(config, "embed_model", "") or "").strip()
    if not model:
        return None
    api_key = config.api_key or os.environ.get("OPENAI_API_KEY", "")
    embeddings = OpenAIEmbeddings(
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
    return embeddings.embed_query


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
