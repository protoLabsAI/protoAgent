"""ModelOverrideMiddleware — per-turn model + reasoning-effort selection (per chat tab).

The graph is compiled once with a single lead model, but the console lets each
chat tab pick its own model AND its reasoning effort (the /effort command). Both
ride on the turn as ``state["model"]`` / ``state["reasoning_effort"]`` (stamped by
the chat layer from the request metadata); this middleware reads them at the
``wrap_model_call`` boundary and swaps ``request.model`` to a client built via
``create_llm(config, model_name=…, reasoning_effort=…)`` and cached per
(model, effort). Unset → the configured default, unchanged.

Added OUTERMOST among the wrap_model_call middleware so the actual (overridden)
model is what PromptCacheMiddleware sees when it decides caching.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware

log = logging.getLogger(__name__)


class ModelOverrideError(ValueError):
    """A per-turn model override (``state["model"]`` — a console tab's pick, a ``/v1``
    request's ``model``, an A2A ``metadata.model``) that could not be built (#3957).

    A ``ValueError`` because it is the caller's input that is wrong: an unknown provider
    prefix, a connection with no credentials, a model id the provider rejects. Carries
    ``model`` so a surface can name the bad value without parsing the message."""

    def __init__(self, model: str, cause: BaseException | None = None, *, config=None):
        self.model = model
        reason = _unknown_prefix_reason(model, config) or (str(cause) if cause is not None else "it could not be built")
        super().__init__(
            f"model {model!r} is not available: {reason}. Pick a model this agent can run: "
            "`<connection>:<model>` naming a known connection, or a model id the default "
            "connection serves."
        )


def _unknown_prefix_reason(model: str, config) -> str:
    """``"'x' is not a registered connection …"`` when ``model`` carries a ``<prefix>:``
    that names no registered connection, else ``""``. The underlying build error for such a
    pick is whatever the DEFAULT lane trips over (the gateway's "Missing credentials", a
    native provider's model-id check), which describes the wrong problem; the prefix is the
    thing the caller got wrong. Best-effort — never raises."""
    try:
        from graph.llm import split_slot_target

        prefix, sep, _rest = (model or "").partition(":")
        if not sep or prefix.strip().lower() == "acp" or "/" in prefix:
            return ""
        claimed, _ = split_slot_target(model, config)
        if claimed:
            return ""
        from graph.llm import _LEGACY_SLOT_PROVIDERS

        # The same set `split_slot_target` claims: the registry plus the legacy floor,
        # unless the operator declared an empty registry on purpose.
        known = set(config.provider_ids()) if config is not None and hasattr(config, "provider_ids") else set()
        if not (config is not None and not config.providers and getattr(config, "providers_declared", False)):
            known.update(_LEGACY_SLOT_PROVIDERS)
        avail = f" (known: {', '.join(sorted(known))})" if known else ""
        return f"{prefix.strip()!r} is not a registered connection{avail}"
    except Exception:  # noqa: BLE001 — a hint, never a second failure
        return ""


def _model_name_of(model) -> str:
    return getattr(model, "model_name", None) or getattr(model, "model", "") or ""


class ModelOverrideMiddleware(AgentMiddleware):
    """Swap the turn's model to the tab-selected model + reasoning effort."""

    def __init__(self, config):
        super().__init__()
        self._config = config
        self._cache: dict[tuple[str, str], object] = {}  # (model_name, effort) → ChatOpenAI

    def _llm_for(self, want: str, effort: str):
        key = (want, effort)
        llm = self._cache.get(key)
        if llm is None:
            from graph.llm import create_llm

            llm = create_llm(
                self._config,
                model_name=want or None,
                reasoning_effort=effort or None,
            )
            self._cache[key] = llm
        return llm

    def _override(self, request):
        state = getattr(request, "state", None) or {}
        want = (state.get("model") or "").strip()
        effort = (state.get("reasoning_effort") or "").strip()
        if not want and not effort:
            return request  # nothing selected — use the compiled default
        cur = _model_name_of(getattr(request, "model", None))
        # With no per-tab model the override still targets the current model — but only
        # when an effort is set (otherwise there's nothing to change). Same model + no
        # effort is a no-op.
        target = want or cur
        if not effort and target == cur:
            return request
        try:
            return request.override(model=self._llm_for(target, effort))
        except Exception as exc:  # noqa: BLE001 — classified below
            if want:
                # An EXPLICIT model pick that can't be built is the caller's error, not
                # something to paper over (#3957). Falling back to the default ran the turn
                # on a model nobody chose — billed to a different account, recorded in
                # telemetry as the default, and answered as though the pick had worked. Fail
                # the turn with a message that names the pick; /v1 maps it to a 400.
                raise ModelOverrideError(want, exc, config=self._config) from exc
            # Effort-only on the current model: the model itself is still the one the
            # operator is on, so degrading to it without the effort stays a soft fallback.
            log.exception("[model-override] could not apply effort %r to %r; using default", effort, target)
            return request

    def wrap_model_call(self, request, handler):
        return handler(self._override(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._override(request))
