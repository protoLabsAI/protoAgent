"""Build a native OpenAI client backed by a ChatGPT/Codex OAuth subscription (ADR 0097).

The ChatGPT backend (``https://chatgpt.com/backend-api/codex``) does not serve the
chat-completions API — it serves the **Responses API** with subscription-specific rules:

- Bearer auth with the Codex OAuth access token (resolved in :mod:`graph.providers.oauth`)
- ``ChatGPT-Account-Id`` header + the ``codex_cli_rs`` originator the backend validates
- ``store=false`` is mandatory; encrypted reasoning is threaded back via
  ``include=["reasoning.encrypted_content"]``

``ChatOpenAI`` (langchain-openai ≥ 1.0) implements the Responses API and exposes
``store`` / ``include`` / ``reasoning`` as first-class fields, so we configure it rather
than hand-rolling the transport. The one part that must be proven live is multi-turn
encrypted-reasoning replay across the LangGraph checkpointer — see ADR 0097's open items.

Using ChatGPT-subscription OAuth from a third-party app is a grayer ToS area than the
Claude path; this provider is opt-in via ``model.provider: openai-codex``.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from graph.providers.oauth import resolve_codex_oauth

if TYPE_CHECKING:
    from graph.config import LangGraphConfig

log = logging.getLogger("protoagent.providers.openai_codex")

# The originator the Codex backend validates, plus the Responses beta flag.
_CODEX_ORIGINATOR = "codex_cli_rs"
_CODEX_USER_AGENT = "protoAgent-codex/0.1 (+https://github.com/protoLabsAI/protoAgent)"


def build_codex_llm(
    config: "LangGraphConfig",
    *,
    model_name: str | None = None,
    reasoning_effort: str | None = None,
) -> Any:
    """Build a ``ChatOpenAI`` (Responses API) for ``model.provider: openai-codex``.

    Resolves + refreshes the Codex OAuth token, points the client at the ChatGPT
    backend, and sets the subscription-required headers and ``store``/``include``
    flags. ``model_name`` overrides ``config.model_name`` for aux/subagent slots.
    """
    # Local import — avoids a cycle at module load (the client subclasses graph.llm's).
    from graph.providers.codex_client import build_codex_client, issuer_fingerprint

    creds = resolve_codex_oauth()  # raises OAuthCredentialError if none

    name = (model_name or config.model_name or "").strip()
    if not name or "/" in name:
        raise RuntimeError(
            f"model.provider is 'openai-codex' but model.name={name!r} is not an OpenAI "
            "model id (e.g. 'gpt-5-codex', 'gpt-5', 'o4-mini')."
        )

    headers = {
        "OpenAI-Beta": "responses=experimental",
        "originator": _CODEX_ORIGINATOR,
        "User-Agent": _CODEX_USER_AGENT,
    }
    if creds.account_id:
        headers["ChatGPT-Account-Id"] = creds.account_id
    else:
        log.warning(
            "[openai-codex] no ChatGPT account id resolved from the token — the backend "
            "may reject requests; re-run `codex` to refresh credentials."
        )

    kwargs: dict[str, Any] = {
        "model": name,
        "base_url": creds.base_url,
        "api_key": creds.access_token,
        "use_responses_api": True,
        # Content blocks, not the legacy "v0" string. This is the follow-up the v0
        # pin was holding open: "v0" collapses a turn's reasoning into ONE
        # additional_kwargs slot (later items overwrite the first, and streamed
        # fragments of two different items merge into each other), so it cannot
        # carry per-item encrypted blobs. "responses/v1" keeps each reasoning item
        # as its own block, in order, and langchain replays them that way — which
        # is what makes cross-turn encrypted-reasoning continuity possible at all.
        # The rendering half of the pin was already paid off: every answer site
        # reads `AIMessage.text`, which yields text blocks only.
        # PROTOAGENT_CODEX_OUTPUT_VERSION is the escape hatch back to "v0" (no
        # replay, but no block-shaped content either) if a surface turns out to
        # still assume the string shape.
        "output_version": os.environ.get("PROTOAGENT_CODEX_OUTPUT_VERSION", "").strip() or "responses/v1",
        # The ChatGPT backend mandates store=false, so a replayed reasoning item must
        # carry its own blob — hence `include`. langchain-openai's streaming path
        # drops the event that carries it; `codex_client` re-surfaces it and stamps
        # the issuer, so this now asks for a blob that is actually read, kept, and
        # replayed only back to the endpoint that minted it.
        "store": False,
        "include": ["reasoning.encrypted_content"],
        # The Codex backend manages its own output truncation and rejects
        # max_output_tokens (which langchain derives from max_tokens), so we omit it.
        "timeout": config.request_timeout,
        "max_retries": config.llm_max_retries,
        "streaming": True,
        "stream_usage": True,
        "default_headers": headers,
    }
    effort = reasoning_effort if reasoning_effort is not None else config.reasoning_effort
    if effort:
        kwargs["reasoning"] = {"effort": effort, "summary": "auto"}
    if config.top_p is not None:
        kwargs["top_p"] = config.top_p

    # The blob is sealed to endpoint+account: stamp WHICH one, so a later turn that
    # lands on another connection drops those items instead of 400-ing on them.
    return build_codex_client(
        issuer=issuer_fingerprint(creds.base_url, creds.account_id or ""), **kwargs
    )
