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

import logging

from graph.config import LangGraphConfig

log = logging.getLogger(__name__)

# api_base -> {model_name: max_input_tokens}. Attempted bases are recorded so a miss/outage
# doesn't refetch on every turn (the cost emitter calls this per turn).
_WINDOWS: dict[str, dict[str, int]] = {}
_ATTEMPTED: set[str] = set()

_GATEWAY_UA = "protoAgent/0.1 (+https://github.com/protoLabsAI/protoAgent)"


def _fetch_window_map(api_base: str, api_key: str) -> dict[str, int]:
    """GET the LiteLLM proxy's per-group context windows → ``{model_group: max_input_tokens}``.

    Bounded: a connection error / timeout on the first path returns empty (don't hammer the
    second). ``/v1/model/group/info`` is the grouped view (top-level ``max_input_tokens`` per
    ``model_group``); ``/model/group/info`` is the un-versioned alias — proxies differ on prefix.
    """
    import httpx

    root = (api_base or "").rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    headers = {"User-Agent": _GATEWAY_UA}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    out: dict[str, int] = {}
    for i, url in enumerate((f"{root}/v1/model/group/info", f"{root}/model/group/info")):
        try:
            resp = httpx.get(url, headers=headers, timeout=2.5)
        except Exception:  # noqa: BLE001 — host unreachable/timeout: don't try the second path
            return out
        if resp.status_code != 200:
            # 404 on the versioned path → try the un-versioned one; any other status → give up.
            if i == 0 and resp.status_code == 404:
                continue
            return out
        try:
            data = resp.json().get("data") or []
        except Exception:  # noqa: BLE001 — non-JSON body
            return out
        for entry in data:
            if not isinstance(entry, dict):
                continue
            name = entry.get("model_group") or entry.get("model_name")
            win = entry.get("max_input_tokens")
            if isinstance(name, str) and isinstance(win, int) and win > 0:
                out[name] = win
        return out
    return out


def context_window_for(config: LangGraphConfig, model_name: str | None = None) -> int | None:
    """The input context window (``max_input_tokens``) for a model on the gateway, or ``None``.

    Fetched once per gateway base and cached, so it's safe to call per turn. Returns ``None``
    when the gateway is unreachable or doesn't report the model — callers degrade gracefully
    (no profile → message-count compaction; size-only meter)."""
    base = (config.api_base or "").rstrip("/")
    if not base:
        return None
    if base not in _ATTEMPTED:
        _ATTEMPTED.add(base)
        try:
            _WINDOWS[base] = _fetch_window_map(base, config.api_key or "")
        except Exception:  # noqa: BLE001 — never let model-info break model creation / a turn
            _WINDOWS[base] = {}
            log.debug("[model-window] fetch failed for %s", base, exc_info=True)
    model = (model_name or config.model_name or "").strip()
    return _WINDOWS.get(base, {}).get(model)


def reset_window_cache() -> None:
    """Drop the cached windows — call after a config change (gateway/key) or in tests."""
    _WINDOWS.clear()
    _ATTEMPTED.clear()
