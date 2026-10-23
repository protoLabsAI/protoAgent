"""Quota-aware model failover helpers (ADR 0122 D2/D4/D8).

Core's model fallback (:class:`graph.agent.ObservableModelFallbackMiddleware`) and the
per-turn :class:`graph.middleware.model_override.ModelOverrideMiddleware` use these to make
subscription-aware routing decisions WITHOUT reaching through the board — core reads its OWN
usage store (D8). Three tolerant reads:

- :func:`account_for_model` / :func:`resolve_account` — which native-OAuth account
  (``anthropic-oauth`` / ``openai-codex``) a model burns, via the configured provider
  registry; a gateway / ``openai-compat`` / ``acp:`` / unresolvable route is ``None``.
- :func:`quota_429` — whether a model-call exception is a *subscription-quota* 429 (as
  opposed to a transient infra throttle), reusing the status extraction the model-override
  classifier already uses.
- :func:`is_blocked_for_model` — whether the usage store's CACHED snapshot (no forced poll)
  shows a ``blocked`` window applying to a model on an account (D2).

Everything degrades, never raises: an unresolvable account, an absent/stale/unknown
snapshot, or anything that throws reads as "unknown", so an unknown route fails over exactly
as it did before (D4).
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# Body / message / header text that marks a 429 as subscription-quota exhaustion rather than
# an infra throttle blip. Mirrors the quota markers in
# ``graph.upstream_errors.rate_limit_details`` and adds the generic rate-limit wording a
# provider may use for the same thing. Matched case-insensitively.
_QUOTA_MARKERS: tuple[str, ...] = (
    "quota",
    "usage limit",
    "usage_limit",
    "weekly limit",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "insufficient_quota",
    "too many requests",
)


def _name_of(model: Any) -> str:
    """A model's name from a name string or a built chat-model instance (gateway clients
    expose ``model_name``; native clients ``model``)."""
    if isinstance(model, str):
        return model.strip()
    return (getattr(model, "model_name", None) or getattr(model, "model", None) or "").strip()


def resolve_account(model: Any, config: Any) -> tuple[str | None, str]:
    """``(native-OAuth provider id | None, bare model id)`` for a model name or instance.

    The provider id is one of the native-OAuth accounts ADR 0122 covers
    (``anthropic-oauth`` / ``openai-codex``) when the model routes to a subscription —
    resolved from an explicit ``<provider>:<model>`` qualifier (via
    :func:`graph.llm.resolve_slot_target`), else from the config's default
    ``model.provider``. Everything else — the gateway, an ``openai-compat`` connection, an
    ``acp:`` coder, an unresolvable name, no config — is ``None``, an unknown account (D4).

    The bare model id is returned regardless (the window-scope check in
    :func:`is_blocked_for_model` needs it even on an unknown account). Never raises.

    Resolving from a *name string* (the configured slot value) is deliberate: a built
    instance only exposes its bare model id with the provider prefix already stripped, so an
    instance alone cannot tell an ``anthropic-oauth:`` slot from a gateway one.
    """
    name = _name_of(model)
    if not name:
        return None, ""
    try:
        from graph.llm import resolve_slot_target
        from graph.providers import is_native_oauth_provider

        slot, _entry, ptype, bare = resolve_slot_target(name, config)
        if slot:
            provider = ptype.strip().lower() if isinstance(ptype, str) else ""
            return (provider if is_native_oauth_provider(provider) else None), (bare or name)
        default = (getattr(config, "model_provider", "") or "").strip().lower()
        return (default if is_native_oauth_provider(default) else None), name
    except Exception:  # noqa: BLE001 — an unresolvable route is unknown, never a raise (D4)
        log.debug("[quota-routing] account resolution failed for %r", name, exc_info=True)
        return None, name


def account_for_model(model: Any, config: Any) -> str | None:
    """The native-OAuth provider id ``model`` burns, or ``None`` for an unknown route."""
    return resolve_account(model, config)[0]


def quota_429(exc: BaseException | None, *, native_oauth: bool = False) -> bool:
    """Whether ``exc`` is a *subscription-quota* 429 (not a transient infra throttle).

    A quota 429 is a 429 (status extracted exactly as
    :func:`graph.middleware.model_override.is_transient_build_failure` does, via
    ``upstream_status_in_chain``) that EITHER came from a native-OAuth provider
    (``native_oauth`` — a subscription 429 is always quota) OR whose body / message / response
    headers name a usage/rate limit. A plain gateway "slow down" 429 with no such marker is
    not a quota 429, so it keeps today's failover behaviour. Never raises.
    """
    from graph.upstream_errors import upstream_status_in_chain

    try:
        if upstream_status_in_chain(exc) != 429:
            return False
        if native_oauth:
            return True
        return _names_usage_limit(exc)
    except Exception:  # noqa: BLE001 — a classification read, never a second failure
        return False


def _names_usage_limit(exc: BaseException | None) -> bool:
    """Scan the 429 link's message, body and response-header names for a quota marker.

    Walks the explicit ``__cause__`` chain (bounded) to the exception that carries the 429 —
    a subagent re-raises its provider failure wrapped — and tests that one's text."""
    from graph.upstream_errors import upstream_status

    seen: set[int] = set()
    cur = exc
    while cur is not None and id(cur) not in seen and len(seen) < 16:
        seen.add(id(cur))
        if upstream_status(cur) == 429 and any(marker in _error_text(cur) for marker in _QUOTA_MARKERS):
            return True
        cur = cur.__cause__
    return False


def _error_text(exc: BaseException) -> str:
    """The exception's message, serialized body and response-header names, lower-cased."""
    parts = [str(exc)]
    body = getattr(exc, "body", None)
    if body is not None:
        try:
            parts.append(json.dumps(body, default=str))
        except Exception:  # noqa: BLE001 — a non-serializable body still stringifies
            parts.append(str(body))
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        try:
            parts.extend(str(key) for key in headers.keys())
        except Exception:  # noqa: BLE001 — odd header mappings are simply skipped
            pass
    return " ".join(parts).lower()


def is_blocked_for_model(store: Any, provider_id: str | None, model: str | None) -> bool:
    """Whether the store's CACHED snapshot (no forced poll) shows a ``blocked`` window that
    applies to ``model`` on ``provider_id``.

    An absent provider id, an absent / stale / unknown snapshot, or any read error reads as
    not-blocked — unknown usage fails over exactly as today (D4). Reads via
    :meth:`observability.usage_store.UsageStore.cached`, so the on-demand ≤1/60 s poll
    contract is never forced (ADR 0122 D1)."""
    if not provider_id:
        return False
    try:
        data = store.cached(provider_id)
    except Exception:  # noqa: BLE001 — a failing store read is unknown usage
        return False
    if not isinstance(data, dict) or data.get("stale") or data.get("status") != "ok":
        return False
    return _blocked_window_applies(data.get("windows"), model)


def _blocked_window_applies(windows: Any, model: str | None) -> bool:
    """D2, on the store's window dicts: a ``blocked`` window makes the account unusable for
    ``model`` when it applies — an unscoped window always applies; a model-scoped window only
    to its own model (mirrors :meth:`graph.providers.usage.Window.applies_to`)."""
    for window in windows or []:
        if not isinstance(window, dict) or window.get("status") != "blocked":
            continue
        scope = window.get("scope")
        if scope is None or (model and isinstance(scope, dict) and scope.get("model") == model):
            return True
    return False
