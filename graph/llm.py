"""LLM factory for the protoAgent LangGraph runtime.

All models route through the LiteLLM gateway (OpenAI-compatible),
so we use ChatOpenAI for everything.
"""

import os

from langchain_openai import ChatOpenAI

from graph.config import LangGraphConfig


def _build_llm_kwargs(config: LangGraphConfig) -> dict:
    """Assemble the ChatOpenAI kwargs from config (extracted for testing)."""
    api_key = config.api_key or os.environ.get("OPENAI_API_KEY", "")

    kwargs: dict = {
        "base_url": config.api_base,
        "api_key": api_key,
        "model": config.model_name,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
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
