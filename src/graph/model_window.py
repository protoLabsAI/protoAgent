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


def context_window_for(config: LangGraphConfig, model_name: str | None = None, connection=None) -> int | None:
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
    #
    # ``connection`` (a registered ``Provider``) asks THAT connection's gateway instead — the
    # one a connection-built chat client actually talks to. Without it a wizard-written
    # config (one declared connection, legacy ``model.api_base`` left on the App default)
    # probed a host that does not exist and ran with no known window.
    model = (model_name or config.model_name or "").strip()
    win = _window_on_route(resolve_model_route(config, connection), model)
    if win:
        return win
    ptype = getattr(connection, "type", "") if connection is not None else ""
    return fallback_window(config, model, ptype)


# ── when the gateway doesn't know: the model's own profile, then a known family ────────
#
# The gateway lookup alone left the window unknown for every native-subscription model
# (``anthropic-oauth`` / ``openai-codex`` don't go through a gateway at all) and for any
# alias the gateway doesn't describe — so the pruner sized itself off a fixed 80k guess,
# the meter had no denominator, and compaction relied on whatever profile the client
# library happened to ship (brandLaunch on codex ``gpt-6.1-sol``, 10-2026).

#: The ChatGPT-subscription (Codex) backend enforces a smaller window than the public
#: API's model card — the Codex CLI itself budgets 272k for its models. Conservative on
#: purpose: an underestimate only compacts/prunes early, an overestimate overflows.
CODEX_SUBSCRIPTION_WINDOW = 272_000
#: Conservative windows by family, for a model neither the gateway nor a client-library
#: profile describes. Longest prefix wins.
KNOWN_FAMILY_WINDOWS: tuple[tuple[str, int], ...] = (
    ("gpt-", 272_000),
    ("o3", 200_000),
    ("o4", 200_000),
    ("claude-", 200_000),
)


def _library_profile_window(model: str) -> int | None:
    """``max_input_tokens`` from langchain-anthropic / langchain-openai's bundled model
    profiles — what the client would report as ``profile`` for this exact model id."""
    for mod in ("langchain_anthropic.data._profiles", "langchain_openai.data._profiles"):
        try:
            import importlib

            profiles = getattr(importlib.import_module(mod), "_PROFILES", None) or {}
        except Exception:  # noqa: BLE001 — optional dependency / private table moved
            continue
        win = (profiles.get(model) or {}).get("max_input_tokens")
        if isinstance(win, int) and win > 0:
            return win
    return None


def fallback_window(config, model: str, provider_type: str = "") -> int | None:
    """The window for ``model`` when the gateway doesn't report one.

    ``provider_type`` is the lane the model runs on (a connection's ``type``); blank means
    "work it out": a provider-qualified value names its lane, an unqualified one runs on
    ``model.provider``. Order: the Codex subscription window for the codex lane; the
    model's own langchain profile (a gateway alias like ``anthropic/claude-opus-5-5`` is
    tried bare too); a conservative family window; else None.
    """
    model = (model or "").strip()
    if not model:
        return None
    if not provider_type:
        try:
            from graph.llm import resolve_slot_target  # lazy — llm imports this module

            prefix, _entry, ptype, bare = resolve_slot_target(model, config)
            if prefix:
                provider_type, model = ptype, bare or model
        except Exception:  # noqa: BLE001 — a probe never breaks a turn
            pass
        provider_type = provider_type or str(getattr(config, "model_provider", "") or "")
    if provider_type.strip().lower() == "openai-codex":
        return CODEX_SUBSCRIPTION_WINDOW
    bare = model.rsplit("/", 1)[-1]
    for name in dict.fromkeys((model, bare)):
        win = _library_profile_window(name)
        if win:
            return win
    best = max((p for p in KNOWN_FAMILY_WINDOWS if bare.lower().startswith(p[0])), key=lambda p: len(p[0]), default=None)
    return best[1] if best else None


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
    return _window_on_route(resolve_model_route(config, provider), bare) or fallback_window(config, bare, provider.type)


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
