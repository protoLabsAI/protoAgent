"""Build a native Claude client authenticated by a Claude Code OAuth token (ADR 0097).

``ChatAnthropic`` authenticates with an ``x-api-key`` (console API key). A Claude
*subscription* OAuth token instead needs **Bearer** auth plus the identity headers
Anthropic's OAuth infrastructure routes on — the same set Claude Code sends:

- ``Authorization: Bearer <token>`` (not ``x-api-key``)
- ``anthropic-beta: claude-code-20250219,oauth-2025-04-20``
- ``User-Agent: claude-code/<version> (external, cli)``
- a system prompt whose FIRST block is the Claude Code identity line (added by
  :class:`graph.middleware.claude_code_identity.ClaudeCodeIdentityMiddleware`, not here)

We get Bearer auth by subclassing ``ChatAnthropic`` and swapping ``api_key`` for the
SDK's ``auth_token`` in the one place the client params are assembled. Everything else
— streaming, tool binding, native reasoning, ``cache_control`` — is stock ChatAnthropic,
so the graph and middleware treat it like any other model.
"""

from __future__ import annotations

import logging
import subprocess
import time
from functools import cached_property
from typing import TYPE_CHECKING, Any

from graph.providers.oauth import resolve_anthropic_oauth

if TYPE_CHECKING:
    from graph.config import LangGraphConfig

log = logging.getLogger("protoagent.providers.anthropic_oauth")

# Beta headers subscription/OAuth traffic requires (matches Claude Code / OpenCode).
_OAUTH_BETAS = "claude-code-20250219,oauth-2025-04-20"
# Anthropic rejects OAuth requests whose spoofed client version is too far behind the
# real release, so we detect the installed Claude Code version and only fall back to a
# recent-enough constant when it can't be found.
_CLAUDE_CODE_VERSION_FALLBACK = "2.1.74"
# The first system block OAuth traffic must carry — see ClaudeCodeIdentityMiddleware.
CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."

_version_cache: str | None = None

# How long a resolved OAuth token is reused before the credential store is re-read.
# Resolution can shell out to the macOS Keychain (`security find-generic-password`), so
# re-resolving on every model call would add a subprocess spawn to each one — a single turn
# makes tens. A short TTL keeps that off the hot path while bounding staleness to seconds
# instead of the process lifetime it used to be (#2582).
_TOKEN_TTL_S = 30.0
_token_cache: tuple[str, float] | None = None


def current_oauth_token(*, force: bool = False) -> str:
    """The Claude OAuth access token, re-resolved at most every :data:`_TOKEN_TTL_S`.

    ``force=True`` bypasses the cache — for the case where the cached value is precisely
    the one that just failed.
    """
    global _token_cache
    now = time.monotonic()
    if not force and _token_cache is not None and now - _token_cache[1] < _TOKEN_TTL_S:
        return _token_cache[0]
    token = (resolve_anthropic_oauth().access_token or "").strip()
    _token_cache = (token, now)
    return token


def _reset_token_cache() -> None:
    """Drop the cached token (tests, and an explicit sign-in/disconnect)."""
    global _token_cache
    _token_cache = None


def _claude_code_version() -> str:
    global _version_cache
    if _version_cache is not None:
        return _version_cache
    for cmd in ("claude", "claude-code"):
        try:
            out = subprocess.run(
                [cmd, "--version"], capture_output=True, text=True, timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            token = out.stdout.strip().split()[0]
            if token and token[0].isdigit():
                _version_cache = token
                return token
    _version_cache = _CLAUDE_CODE_VERSION_FALLBACK
    return _version_cache


def oauth_default_headers() -> dict[str, str]:
    """The identity + beta headers every OAuth request must carry."""
    return {
        "anthropic-beta": _OAUTH_BETAS,
        "User-Agent": f"claude-code/{_claude_code_version()} (external, cli)",
    }


try:
    from langchain_anthropic import ChatAnthropic

    class _OAuthChatAnthropic(ChatAnthropic):
        """``ChatAnthropic`` that authenticates with a Bearer OAuth token.

        Overrides the single ``_client_params`` assembly point to drop ``api_key``
        (which would send ``x-api-key``) and pass the SDK's ``auth_token`` (which
        sends ``Authorization: Bearer``). If a future ``langchain-anthropic`` renames
        or restructures ``_client_params``, ``test_anthropic_oauth`` fails loudly.
        """

        oauth_token: str = ""

        @cached_property
        def _client_params(self) -> dict[str, Any]:
            params = dict(ChatAnthropic._client_params.func(self))  # type: ignore[attr-defined]
            params.pop("api_key", None)
            params["auth_token"] = self.oauth_token
            return params

        def _refresh_oauth_token(self) -> None:
            """Push the CURRENT credential into the live SDK clients, before each request.

            The token used to be resolved once, at graph build, and frozen into the client
            for the life of the process — ``_client_params`` is a ``cached_property``, so
            even reassigning ``oauth_token`` wouldn't have moved it. Anything that rotates
            the shared Claude credential then broke the agent permanently, and this setup
            rotates it *by doing its job*: a board agent dispatching its own Claude Code
            coders shares their keychain login, and each of their runs can refresh it,
            invalidating the access token this process is still presenting. Every call then
            401s as "revoked" while ``/api/config/oauth-status`` — which reads the store
            live — kept reporting a healthy sign-in. The introspection surface and the
            running graph disagreeing is what made it so hard to diagnose (#2582).

            The SDK's ``auth_headers`` is a live property over ``client.auth_token``, so
            updating that attribute is enough: no client rebuild, no reconnect, no dropped
            connection pool.
            """
            try:
                token = current_oauth_token()
            except Exception:  # noqa: BLE001 — a transient store read must not kill a live turn
                log.warning(
                    "[anthropic-oauth] could not re-resolve the access token — keeping the current one",
                    exc_info=True,
                )
                return
            if not token or token == self.oauth_token:
                return
            self.oauth_token = token
            for client in (getattr(self, "_client", None), getattr(self, "_async_client", None)):
                if client is not None:
                    client.auth_token = token
            log.info("[anthropic-oauth] the access token rotated — refreshed the live client")

        # The four request entry points. Overridden explicitly rather than hooked deeper so
        # that a langchain-anthropic rename fails loudly in test_anthropic_oauth, the same
        # contract `_client_params` above relies on.
        def _generate(self, *args: Any, **kwargs: Any) -> Any:
            self._refresh_oauth_token()
            return super()._generate(*args, **kwargs)

        def _stream(self, *args: Any, **kwargs: Any) -> Any:
            self._refresh_oauth_token()
            yield from super()._stream(*args, **kwargs)

        async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
            self._refresh_oauth_token()
            return await super()._agenerate(*args, **kwargs)

        async def _astream(self, *args: Any, **kwargs: Any) -> Any:
            self._refresh_oauth_token()
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk

    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001 — surfaced with an actionable message at build time
    _OAuthChatAnthropic = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


def build_anthropic_oauth_llm(
    config: "LangGraphConfig",
    *,
    model_name: str | None = None,
    reasoning_effort: str | None = None,
) -> Any:
    """Build a Bearer-authenticated ``ChatAnthropic`` for ``model.provider: anthropic-oauth``.

    ``model_name`` overrides ``config.model_name`` (used for aux/subagent slots).
    Raises ``OAuthCredentialError`` when no Claude Code token is available, and a
    clear ``RuntimeError`` if ``langchain-anthropic`` isn't installed.
    """
    if _OAuthChatAnthropic is None:
        raise RuntimeError(
            "model.provider is 'anthropic-oauth' but langchain-anthropic is not "
            f"importable ({_IMPORT_ERROR}). Install it: `uv sync` / `pip install langchain-anthropic`."
        )

    creds = resolve_anthropic_oauth()  # raises OAuthCredentialError if none
    name = (model_name or config.model_name or "").strip()
    if not name or "/" in name:
        # A gateway alias like "protolabs/reasoning" is meaningless to Anthropic —
        # anthropic-oauth needs a real Claude model id.
        raise RuntimeError(
            f"model.provider is 'anthropic-oauth' but model.name={name!r} is not a Claude "
            "model id (e.g. 'claude-sonnet-4-5', 'claude-opus-4-1').\n"
            "A '/' in the name means a gateway alias, which usually means the two halves "
            "came from different config layers: set model.provider and model.name TOGETHER "
            "on this agent (they are one decision), or clear both so it inherits a "
            "coherent pair from the host."
        )

    token = (creds.access_token or "").strip()
    if not token:
        # resolve_anthropic_oauth raises when there's no credential, so this only guards
        # a malformed store — but an empty token would reach Anthropic as "no api key
        # passed in" (a confusing 401), so fail clearly instead.
        raise RuntimeError("anthropic-oauth resolved an empty access token — sign in again.")

    kwargs: dict[str, Any] = {
        "model": name,
        "oauth_token": token,
        # ChatAnthropic requires *some* api_key value even though we override it away;
        # a sentinel makes an accidental x-api-key leak obvious in a capture.
        "api_key": "oauth-via-auth-token",
        "max_tokens": config.max_tokens,
        # NOTE: we do NOT send `temperature`. The current Claude models (the 5 family and
        # newer) reject it ("`temperature` is deprecated for this model"), and it's not a
        # knob worth breaking every turn over — omit it and let the model default.
        "timeout": config.request_timeout,
        "max_retries": config.llm_max_retries,
        "streaming": True,
        "stream_usage": True,
        "default_headers": oauth_default_headers(),
    }
    effort = reasoning_effort if reasoning_effort is not None else config.reasoning_effort
    if config.thinking == "enabled" or effort:
        # Extended thinking. Budget scales with the requested effort.
        budget = {"low": 4096, "medium": 8192, "high": 16384, "max": 24576}.get(effort or "medium", 8192)
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}

    return _OAuthChatAnthropic(**kwargs)
