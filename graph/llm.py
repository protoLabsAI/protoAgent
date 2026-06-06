"""LLM factory for the protoAgent LangGraph runtime.

All models route through the LiteLLM gateway (OpenAI-compatible),
so we use ChatOpenAI for everything.
"""

import os
from collections.abc import Callable

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from graph.config import LangGraphConfig

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
