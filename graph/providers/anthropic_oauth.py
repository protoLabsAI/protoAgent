"""Build a native Claude client authenticated by a Claude Code OAuth token (ADR 0097).

``ChatAnthropic`` authenticates with an ``x-api-key`` (console API key). A Claude
*subscription* OAuth token instead needs **Bearer** auth plus the identity headers
Anthropic's OAuth infrastructure routes on — the same set Claude Code sends:

- ``Authorization: Bearer <token>`` (not ``x-api-key``)
- ``anthropic-beta: claude-code-20250219,oauth-2025-04-20``
- ``User-Agent: claude-code/<version> (external, cli)``
- a system prompt whose FIRST block is exactly the Claude Code identity line —
  enforced on every request body by ``_OAuthChatAnthropic._get_request_payload``
  (:func:`shape_oauth_system`), so direct model calls that skip the middleware stack
  (summarization, titles, distill, probes) are covered too;
  :class:`graph.middleware.claude_code_identity.ClaudeCodeIdentityMiddleware` applies
  the same shape earlier for the agent's main calls (visible to prompt capture)

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


def _split_leading_prefix(text: str) -> str | None:
    """The remainder of ``text`` after a leading identity line, or None if absent.

    Handles the merged ``"{prefix}\\n\\n{rest}"`` shape so an already-"prefixed"
    prompt is REPAIRED into the exact-block shape rather than skipped as done. The
    line only counts as a prefix when followed by whitespace or the end of the text
    (``"{prefix}ai ..."`` is some other sentence, not the identity line), and the
    remainder is stripped — "" means nothing is left.
    """
    stripped = text.lstrip()
    if not stripped.startswith(CLAUDE_CODE_SYSTEM_PREFIX):
        return None
    rest = stripped[len(CLAUDE_CODE_SYSTEM_PREFIX) :]
    if rest and not rest[0].isspace():
        return None
    return rest.strip()


def _is_blank_block(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type", "text") == "text" and not str(block.get("text", "")).strip()


def shape_oauth_system(system: Any) -> list[Any]:
    """Return ``system`` (Anthropic wire shape) with the identity line as its exact first block.

    The single source of the OAuth system-prompt shape (ADR 0097, #2763): the FIRST
    block must be byte-exactly :data:`CLAUDE_CODE_SYSTEM_PREFIX`, on its own, with no
    extra keys — anything else (no system at all, a string, a merged first block) is
    refused with a fake 429. Idempotent, never stacks the line, never emits a blank
    text block, and keeps a merged block's other keys (e.g. ``cache_control``) on the
    REMAINDER, not the identity line — or, when nothing remains, moves its
    ``cache_control`` to the last non-blank block so the breakpoint isn't lost.
    """
    prefix_block = {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}
    if system is None or system == [] or (isinstance(system, str) and not system.strip()):
        return [prefix_block]
    if isinstance(system, str):
        rest = _split_leading_prefix(system)
        body = system if rest is None else rest
        return [prefix_block] + ([{"type": "text", "text": body}] if body else [])
    if isinstance(system, list):
        first = system[0]
        if first == prefix_block:
            return list(system)
        rest = _split_leading_prefix(str(first.get("text", ""))) if isinstance(first, dict) and first.get("type", "text") == "text" else None
        if rest is None:
            return [prefix_block, *system]
        if rest:
            return [prefix_block, {**first, "text": rest}, *system[1:]]
        # Nothing left of the first block (it was the line, maybe with whitespace or
        # extra keys): drop it, carrying a cache breakpoint to the last real block.
        tail = [dict(b) if isinstance(b, dict) else b for b in system[1:]]
        cache = first.get("cache_control")
        if cache is not None:
            for block in reversed(tail):
                if isinstance(block, dict) and not _is_blank_block(block):
                    block.setdefault("cache_control", cache)
                    break
        return [prefix_block, *tail]
    return [prefix_block]


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

        def _get_request_payload(self, *args: Any, **kwargs: Any) -> dict:
            """Every request this client sends carries the exact identity first block.

            Shaping lives HERE — the one place every Messages call (``invoke``/``stream``,
            sync and async) assembles its body — not only in ``ClaudeCodeIdentityMiddleware``,
            which sees just the agent's main model calls. Anything that calls the model
            directly bypassed it: langchain's ``SummarizationMiddleware`` invokes it with a
            bare string (no system at all), so with ``compaction.trigger`` set the compaction
            call got the OAuth enforcement's fake 429 and killed the turn. Titles, memory
            distill, judges and probes are the same shape; doing it at the client means a
            new call site can't regress. If langchain-anthropic renames this hook,
            ``tests/test_oauth_identity_every_call.py`` fails on the wire body.
            """
            payload = super()._get_request_payload(*args, **kwargs)
            payload["system"] = shape_oauth_system(payload.get("system"))
            # Every inline image inside Anthropic's limits (a 400 on one oversized image in
            # the checkpointed history otherwise poisons the session for good). Builds new
            # containers, so the stored history is untouched; never breaks a request.
            try:
                from graph.image_limits import clamp_request_images

                payload = clamp_request_images(payload)
            except Exception:  # noqa: BLE001 — clamping must never be what fails a turn
                log.warning("[anthropic-oauth] image clamping skipped", exc_info=True)
            return payload

        def _lane_key(self) -> str:
            """The ADR 0115 in-flight lane for this client: ``anthropic-oauth|<model>`` (D1,
            #3760) — the anthropic-oauth path talks to Anthropic directly, not the gateway,
            so its lane is keyed by the provider and model, not a base URL."""
            return f"anthropic-oauth|{self.model}"

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
            # Non-streaming generation holds one in-flight slot around the call (ADR 0115
            # D3/D6, #3760). `_held_lane_slot` marks the lane held for the duration, so if
            # `super()._agenerate` hands off to `self._astream` (which also wraps this lane)
            # it reuses THIS slot instead of asking for a second one and deadlocking a
            # `max_inflight: 1` lane. `max_inflight` 0 (the default) keeps it a pass-through.
            from graph.image_limits import aprewarm
            from graph.llm import _held_lane_slot

            await aprewarm(args[0] if args else kwargs.get("messages"))  # off-loop image work
            async with _held_lane_slot(self._lane_key()):
                return await super()._agenerate(*args, **kwargs)

        async def _astream(self, *args: Any, **kwargs: Any) -> Any:
            self._refresh_oauth_token()
            # Enforce request_timeout on the stream ITSELF (#3699). ChatAnthropic's
            # `max_retries` retries the request START, and an httpx read timeout only
            # bounds a single socket read — neither bounds an SSE stream that stays open
            # and silent, which is how a 262K-token lead-agent call hung >17 min under
            # `request_timeout: 120`. The shared guard raises a retryable StreamStallTimeout
            # on the time-to-first-token / inter-chunk idle deadline; a stall before any
            # content reconnects within `max_retries`, then the turn fails with a clear
            # error naming this provider/model. `lane` acquires one ADR 0115 in-flight slot
            # per attempt, outside the guard so wait time never counts toward request_timeout
            # (D4). Imported lazily to keep this module's import off the default gateway path
            # (and clear of any import cycle with graph.llm).
            from graph.image_limits import aprewarm
            from graph.llm import _guarded_reconnecting_stream, _stream_timeout_s

            await aprewarm(args[0] if args else kwargs.get("messages"))  # off-loop image work

            async for chunk in _guarded_reconnecting_stream(
                lambda: super(_OAuthChatAnthropic, self)._astream(*args, **kwargs),
                timeout=_stream_timeout_s(self.default_request_timeout),
                max_retries=self.max_retries or 0,
                label=f"anthropic-oauth model {self.model!r}",
                lane=self._lane_key(),
            ):
                yield chunk

    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001 — surfaced with an actionable message at build time
    _OAuthChatAnthropic = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


def resolve_claude_model_name(config: "LangGraphConfig", model_name: str | None = None) -> str:
    """The concrete Claude model id this native-OAuth call will build against.

    ``model_name`` (a per-slot override, e.g. an aux/subagent tier) wins; an empty/None
    override INHERITS the lead ``config.model_name`` — the coherent pair travels together
    (ADR 0097) — never the ``protolabs/reasoning`` gateway-alias dataclass default via
    some other layer. Raises a clear ``RuntimeError`` when the resolved id is empty or a
    '/'-bearing gateway alias (meaningless to Anthropic).

    Config load reconciles the aux/subagent/fallback slots ahead of this
    (``graph.config._reconcile_slot_providers``), so a raise here now means the LEAD pair
    itself is incoherent — surfaced at load, not discovered here on a live delegation.
    """
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
    return name


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
    name = resolve_claude_model_name(config, model_name)

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
        #
        # ChatAnthropic's timeout field is `default_request_timeout` (`timeout` is only its
        # alias); pass the canonical name so the configured request_timeout unambiguously
        # reaches the client and the streaming guard in `_astream` can read it back (#3699).
        "default_request_timeout": config.request_timeout,
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
