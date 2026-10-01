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


_HINT_MAX_CHARS = 400


def _fix_it_hint(cause: BaseException | None) -> str:
    """The cause's own message, redacted and capped — the "Run `codex login`" / "Sign in
    again" text a credential error carries. Shown only on trusted (operator) surfaces;
    never on ``/v1`` or to a federation-tier A2A caller."""
    if cause is None:
        return ""
    try:
        from graph.middleware.redaction import redact

        return redact(str(cause)).strip()[:_HINT_MAX_CHARS]
    except Exception:  # noqa: BLE001 — a hint, never a second failure
        return ""


class ModelOverrideError(ValueError):
    """A per-turn model pick (``state["model"]`` — a console tab's pick, a ``/v1``
    request's ``model``, an A2A ``metadata.model``) this agent cannot use (#3957), for a
    reason that will not go away by retrying: an unknown connection prefix, a model id the
    connection's builder rejects, a connection with no endpoint/key, a sign-in that needs
    redoing. ``/v1`` answers it 400.

    ``str()`` is deliberately short — it reaches remote callers (``/v1``, A2A) — and names
    neither the build error nor the agent's connection list. ``hint`` carries the cause's
    own fix-it text for trusted surfaces only."""

    def __init__(self, model: str, prefix: str = "", *, hint: str = ""):
        self.model = model
        self.hint = hint
        what = f"{prefix!r} is not a known connection" if prefix else "this agent cannot use it"
        super().__init__(f"model {model!r} is not available: {what}.")


class ModelUnavailableError(RuntimeError):
    """A per-turn model pick whose client could not be built because of something
    TRANSIENT (#3957) — a network error or timeout reaching the provider or its token
    endpoint, a 5xx/429 from it. Not the caller's fault and retryable: ``/v1`` answers 503
    with ``Retry-After``. Same short-``str()`` / trusted-``hint`` rule as
    :class:`ModelOverrideError`."""

    RETRY_AFTER_S = 30

    def __init__(self, model: str, *, hint: str = ""):
        self.model = model
        self.hint = hint
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


def is_transient_build_failure(cause: BaseException | None) -> bool:
    """Would building this model plausibly succeed if retried shortly? Only for a network
    error / timeout (on the explicit ``__cause__`` chain), a 429 or 5xx from the provider or
    its token endpoint, or an OAuth credential error flagged as not needing a re-login (the
    refresh could not reach the provider). Everything else — an unknown model id, a
    connection with no endpoint/key, a sign-in that must be redone — is permanent."""
    from graph.upstream_errors import upstream_status_in_chain, upstream_unreachable

    seen: set[int] = set()
    exc = cause
    while exc is not None and id(exc) not in seen and len(seen) < 16:
        if isinstance(exc, TimeoutError):
            return True
        if type(exc).__name__ == "OAuthCredentialError" and getattr(exc, "relogin", True) is False:
            return True
        seen.add(id(exc))
        exc = exc.__cause__
    if upstream_unreachable(cause):
        return True
    status = upstream_status_in_chain(cause)
    return status is not None and (status == 429 or status >= 500)


def override_error(model: str, cause: BaseException, config) -> Exception:
    """The error a failed EXPLICIT pick fails its turn with (#3957): a retryable
    :class:`ModelUnavailableError` only for a transient cause, else the caller's
    :class:`ModelOverrideError`. The cause is logged here in full."""
    prefix = _unknown_prefix(model, config)
    transient = not prefix and is_transient_build_failure(cause)
    log.warning(
        "[model-override] could not build %r (%s): %s",
        model,
        "unknown connection" if prefix else ("transient" if transient else "unusable"),
        cause,
    )
    hint = _fix_it_hint(cause) if not prefix else ""
    if transient:
        return ModelUnavailableError(model, hint=hint)
    return ModelOverrideError(model, prefix, hint=hint)


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
                # the turn with a message that names the pick (/v1: 400, or 503 when the
                # cause is transient).
                raise override_error(want, exc, self._config) from exc
            # Effort-only on the current model: the model itself is still the one the
            # operator is on, so degrading to it without the effort stays a soft fallback.
            log.exception("[model-override] could not apply effort %r to %r; using default", effort, target)
            return request

    def wrap_model_call(self, request, handler):
        return handler(self._override(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._override(request))
