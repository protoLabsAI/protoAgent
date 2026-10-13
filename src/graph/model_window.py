"""Resolve a model's input context window from the LiteLLM gateway (#1378).

LangChain's ``SummarizationMiddleware`` needs ``model.profile["max_input_tokens"]`` to turn a
``fraction:`` / ``tokens:`` compaction trigger into an absolute token threshold; a bare gateway
alias has no built-in profile, so without this it falls back to a message-count trigger and the
chat context meter (#1372) has no ``/ window`` denominator.

The LiteLLM proxy already knows each model's window — declared ``model_info.max_input_tokens``
for self-hosted models, derived from its registry for recognized ones — and serves it at
``/v1/model/group/info``. We fetch that map ONCE per gateway base (best-effort, short timeout)
and cache it; ``create_llm`` sets the profile from it, and the cost/context emitter reads it for
the meter denominator. A gateway that's down or omits the model just leaves the window unknown —
exactly today's behavior (message-count fallback, size-only meter).
"""

from __future__ import annotations

import hashlib
import logging

from graph.config import LangGraphConfig, resolve_model_route

log = logging.getLogger(__name__)

# api_base -> {model_name: max_input_tokens}. Attempted bases are recorded so a miss/outage
# doesn't refetch on every turn (the cost emitter calls this per turn).
_WINDOWS: dict[str, dict[str, int]] = {}
_ATTEMPTED: set[str] = set()

_GATEWAY_UA = "protoAgent/0.1 (+https://github.com/protoLabsAI/protoAgent)"


def _window_from_entry(entry: dict) -> tuple[str | None, int | None]:
    """(model name, max_input_tokens) from one LiteLLM info row, across both shapes:
    ``/model/info`` nests it under ``model_info.max_input_tokens`` (per deployment); the
    grouped ``/model/group/info`` view puts ``max_input_tokens`` top-level on ``model_group``."""
    name = entry.get("model_name") or entry.get("model_group")
    info = entry.get("model_info") if isinstance(entry.get("model_info"), dict) else {}
    win = info.get("max_input_tokens")
    if win is None:
        win = entry.get("max_input_tokens")
    return (name if isinstance(name, str) else None, win if isinstance(win, int) and win > 0 else None)


def _fetch_window_map(api_base: str, api_key: str) -> dict[str, int]:
    """GET the LiteLLM proxy's model metadata → ``{model_name: max_input_tokens}``.

    Tries the per-deployment ``/model/info`` first (what our gateway exposes — window nested
    under ``model_info``), then the grouped ``/model/group/info`` (top-level window), each in
    its ``/v1``-prefixed and un-prefixed form (proxies differ). Bounded: a connection error /
    timeout stops the probe (the host is unreachable), so worst case is one timeout.
    """
    import httpx

    root = (api_base or "").rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    headers = {"User-Agent": _GATEWAY_UA}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    out: dict[str, int] = {}
    for path in ("/v1/model/info", "/model/info", "/v1/model/group/info", "/model/group/info"):
        try:
            resp = httpx.get(f"{root}{path}", headers=headers, timeout=2.5)
        except Exception:  # noqa: BLE001 — host unreachable/timeout: stop probing
            return out
        if resp.status_code != 200:
            continue  # wrong shape/path for this proxy — try the next
        try:
            data = resp.json().get("data") or []
        except Exception:  # noqa: BLE001 — non-JSON body
            continue
        for entry in data:
            if not isinstance(entry, dict):
                continue
            name, win = _window_from_entry(entry)
            if name and win:
                out[name] = win
        if out:
            return out
    return out


def context_window_for(config: LangGraphConfig, model_name: str | None = None) -> int | None:
    """The input context window (``max_input_tokens``) for a model on the gateway, or ``None``.

    Fetched once per gateway base and cached, so it's safe to call per turn. Returns ``None``
    when the gateway is unreachable or doesn't report the model — callers degrade gracefully
    (no profile → message-count compaction; size-only meter)."""
    # The default route's endpoint AND key — the ones the gateway client itself uses.
    # Reading only `model.api_key` sent this lookup KEYLESS for every deployment that
    # supplies the gateway key through the environment (a fleet agent's stack env or its
    # secrets manager, where `model.api_key` is blank by design): a 401, an unknown window
    # for the life of the process, and everything sized off it quietly on its fallback
    # (#3502).
    return _window_on_route(resolve_model_route(config), (model_name or config.model_name or "").strip())


def context_window_for_slot(config: LangGraphConfig, model_name: str | None) -> int | None:
    """``context_window_for`` for a SLOT value, which may be provider-qualified (#3576).

    A subagent's model can name its own route — ``<provider>:<model>`` (ADR 0106) — and
    that provider's gateway is the one that knows the window; the default route does not
    list the model at all, so the plain lookup returned None and everything sized off it
    fell back. The prefix is honoured only when it names a REGISTERED provider, exactly as
    dispatch honours it; an unqualified value is the plain lookup.
    """
    from graph.llm import split_slot_target  # lazy — llm imports this module

    prefix, bare = split_slot_target(model_name, config)
    if not prefix:
        return context_window_for(config, bare or None)
    provider = next((p for p in (getattr(config, "providers", None) or []) if p.id == prefix), None)
    if provider is None:
        return context_window_for(config, bare or None)
    return _window_on_route(resolve_model_route(config, provider), bare)


def _window_on_route(route, model: str) -> int | None:
    base = (route.base_url or "").rstrip("/")
    if not base:
        return None
    # Cached per AUTHENTICATED route, not per base: two providers on one gateway with
    # different keys can see different model lists, and keying on the base alone let the
    # first key's answer stand in for the second's (review on #3577). A digest, so the
    # key never sits in a dict key.
    key = f"{base}#{hashlib.sha256((route.api_key or '').encode()).hexdigest()[:12]}"
    if key not in _ATTEMPTED:
        _ATTEMPTED.add(key)
        try:
            _WINDOWS[key] = _fetch_window_map(base, route.api_key)
        except Exception:  # noqa: BLE001 — never let model-info break model creation / a turn
            _WINDOWS[key] = {}
            log.debug("[model-window] fetch failed for %s", base, exc_info=True)
    return _WINDOWS.get(key, {}).get(model)


def context_window_for_turn(config: LangGraphConfig, state) -> int | None:
    """The window for THIS turn's model, not the configured default.

    The console lets each chat tab pick its own model; the choice rides the
    ``model`` state channel (``graph/state.py``) and ``ModelOverrideMiddleware``
    swaps the LLM per turn. Anything sized off the window — the projected-context
    budget and skill-index cap (ADR 0108 D6), the tool-result pruning threshold —
    has to follow that override or a tab on a small model gets a large model's
    allowance. Unset/blank ⇒ the configured default, i.e. exactly today's number.
    """
    try:
        model = (state or {}).get("model") or ""
    except AttributeError:  # not a mapping (a runtime with its own state object)
        model = ""
    return context_window_for(config, str(model).strip() or None)


def reset_window_cache() -> None:
    """Drop the cached windows — call after a config change (gateway/key) or in tests."""
    _WINDOWS.clear()
    _ATTEMPTED.clear()
