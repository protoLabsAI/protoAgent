"""Native OAuth-subscription providers (ADR 0097).

protoAgent's default pipeline routes every model call through an OpenAI-compatible
LiteLLM gateway (``model.provider`` is just a label there). These modules add two
*native* providers that authenticate the same pipeline with a coding-agent OAuth
subscription instead of a gateway API key — no gateway, no ACP:

- ``anthropic-oauth`` — Claude (Pro/Max) via a Claude Code OAuth token.
- ``openai-codex``    — ChatGPT/Codex via a Codex OAuth token (Responses API).

The single seam is :func:`graph.llm.create_llm`, which dispatches on
``config.model_provider`` and returns a ``BaseChatModel``; the graph, middleware,
subagents, and compaction are all model-agnostic, so nothing else changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from graph.config import LangGraphConfig

# The ``model.provider`` values that authenticate the native pipeline with a
# coding-agent OAuth subscription instead of the gateway. Everything else is the
# gateway path, unchanged.
NATIVE_OAUTH_PROVIDERS = frozenset({"anthropic-oauth", "openai-codex"})


def is_native_oauth_provider(provider: str | None) -> bool:
    return (provider or "").strip().lower() in NATIVE_OAUTH_PROVIDERS


def build_native_oauth_llm(
    provider: str,
    config: "LangGraphConfig",
    *,
    model_name: str | None = None,
    reasoning_effort: str | None = None,
) -> Any:
    """Route to the right OAuth client builder for a native provider.

    Builders are imported lazily so the default gateway path never imports
    ``langchain-anthropic`` or touches a credential file.
    """
    provider = provider.strip().lower()
    if provider == "anthropic-oauth":
        from graph.providers.anthropic_oauth import build_anthropic_oauth_llm

        return build_anthropic_oauth_llm(
            config, model_name=model_name, reasoning_effort=reasoning_effort
        )
    if provider == "openai-codex":
        from graph.providers.openai_codex import build_codex_llm

        return build_codex_llm(config, model_name=model_name, reasoning_effort=reasoning_effort)
    raise ValueError(f"not a native OAuth provider: {provider!r}")
