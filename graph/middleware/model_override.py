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
    request's ``model``, an A2A ``metadata.model``) that names nothing this agent can run
    (#3957): its ``<prefix>:`` is not a known connection.

    A ``ValueError`` because it is the caller's input that is wrong — ``/v1`` answers it
    400. The message is deliberately short: it reaches remote callers (``/v1``, A2A), so it
    names the pick and the prefix but neither the build error nor the agent's connection
    list; those go to the server log."""

    def __init__(self, model: str, prefix: str = ""):
        self.model = model
        what = f"{prefix!r} is not a known connection" if prefix else "it is not a known model"
        super().__init__(f"model {model!r} is not available: {what}.")


class ModelUnavailableError(RuntimeError):
    """A per-turn model override that names a KNOWN connection but whose client could not
    be built right now (#3957) — a sign-in token that failed to refresh, a credential file
    that is missing, a network error during the refresh. Not the caller's fault and often
    transient, so it is not a 400: ``/v1`` answers 503. Same short-message rule as
    :class:`ModelOverrideError`; the cause is logged, never echoed."""

    def __init__(self, model: str):
        self.model = model
        super().__init__(f"model {model!r} could not be loaded right now; try again shortly.")


def _unknown_prefix(model: str, config) -> str:
    """The ``<prefix>`` of ``model`` when it names no known connection, else ``""``.

    The build error for such a pick is whatever the DEFAULT lane trips over (the gateway's
    "Missing credentials", say), which describes the wrong problem — the prefix is what the
    caller got wrong. ``acp:`` is handled by ``create_llm`` itself; a ``/`` before the colon
    makes it a gateway alias, not a prefix. Best-effort — never raises."""
    try:
        from graph.llm import split_slot_target

        prefix, sep, _rest = (model or "").partition(":")
        if not sep or prefix.strip().lower() == "acp" or "/" in prefix:
            return ""
        claimed, _ = split_slot_target(model, config)
        return "" if claimed else prefix.strip()
    except Exception:  # noqa: BLE001 — a classification hint, never a second failure
        return ""


def override_error(model: str, cause: BaseException, config) -> Exception:
    """The error a failed explicit pick fails its turn with (#3957): the caller's
    :class:`ModelOverrideError` for an unknown connection prefix, else a
    :class:`ModelUnavailableError`. The cause is logged here, with the detail the
    short messages leave out."""
    prefix = _unknown_prefix(model, config)
    log.warning("[model-override] could not build %r (%s): %s", model, "unknown connection" if prefix else "unavailable", cause)
    return ModelOverrideError(model, prefix) if prefix else ModelUnavailableError(model)


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
                # the turn with a message that names the pick (/v1: 400 for an unknown
                # connection, 503 for one that could not be built right now).
                raise override_error(want, exc, self._config) from exc
            # Effort-only on the current model: the model itself is still the one the
            # operator is on, so degrading to it without the effort stays a soft fallback.
            log.exception("[model-override] could not apply effort %r to %r; using default", effort, target)
            return request

    def wrap_model_call(self, request, handler):
        return handler(self._override(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._override(request))
