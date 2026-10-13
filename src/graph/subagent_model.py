"""Which model a subagent run uses — ONE precedence for every dispatch path (#3944).

1. The subagent's own pinned model (``subagents.<name>.model``, applied onto
   ``SUBAGENT_REGISTRY`` at config load) — a deliberate per-role choice wins.
2. The turn's model override (the per-tab / per-request ``metadata.model`` the lead
   turn carries as ``state["model"]``) — a delegation follows the model the operator
   picked for the turn that spawned it.
3. The default: ``routing.aux_model`` (the fast helper alias), else the main model.

Used by in-graph ``task`` / ``task_batch`` delegations, ``/<subagent>`` slash runs,
workflow steps (``graph.sdk.run_subagent``), and background jobs. A background job runs the full lead graph as a detached turn,
so it only needs levels 1-2 carried in its fire metadata — with neither, the fire
carries no model and the turn runs on the configured default, exactly as before.
"""

from __future__ import annotations

import contextvars
from typing import Any


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def pinned_subagent_model(subagent_type: str) -> str:
    """The subagent's own configured model, or ``""`` when it has none (or isn't
    a registry subagent)."""
    try:
        from graph.subagents.config import SUBAGENT_REGISTRY

        return _clean(getattr(SUBAGENT_REGISTRY.get(subagent_type), "model", ""))
    except Exception:  # noqa: BLE001 — a lookup failure means "no pin", never a failed run
        return ""


# The in-flight turn's model override, for dispatch paths that run OUTSIDE the lead
# graph's state (#3955): a workflow step (``graph.sdk.run_subagent``, reached from the
# ``/<workflow>`` slash run or a plugin tool) and ``graph.sdk.spawn_background`` never
# see ``state["model"]``. Both chat drivers bind it for the whole turn — pre-turn
# dispatch and the graph run — so those paths resolve the same precedence as ``task``.
_turn_model_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("protoagent_turn_model", default="")


def current_turn_model() -> str:
    """The in-flight turn's model override (``""`` outside a turn or with none)."""
    return _turn_model_ctx.get()


class turn_model_scope:
    """Bind ``model`` as the current turn's model override for the enclosed block.

    Sync (``with``) and async (``async with``) — same shape as
    ``graph.middleware.request_context.request_metadata_scope``, including its
    best-effort reset when the body crossed a context boundary."""

    def __init__(self, model: Any):
        self._model = _clean(model)
        self._token: contextvars.Token | None = None

    def __enter__(self):
        self._token = _turn_model_ctx.set(self._model)
        return self

    def __exit__(self, *_exc):
        if self._token is not None:
            try:
                _turn_model_ctx.reset(self._token)
            except ValueError:
                _turn_model_ctx.set("")
            self._token = None
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exc):
        return self.__exit__(*exc)


def turn_model_from(state: Any) -> str:
    """The turn's model override from injected graph state (``""`` when unset)."""
    return _clean(state.get("model")) if isinstance(state, dict) else ""


def resolve_subagent_model(
    config: Any,
    subagent_type: str,
    turn_model: str = "",
    *,
    include_default: bool = True,
    pinned: str | None = None,
) -> str | None:
    """Pinned model > turn override > ``routing.aux_model`` > ``None`` (the main model).

    ``include_default=False`` stops after the turn override — the background fire's
    form: with neither a pin nor an override its turn keeps the configured default.
    ``pinned`` supplies the pin directly when the caller already holds the subagent's
    config entry (``None`` → read it from the registry)."""
    pin = _clean(pinned) if pinned is not None else pinned_subagent_model(subagent_type)
    for candidate in (pin, _clean(turn_model)):
        if candidate:
            return candidate
    if include_default:
        aux = _clean(getattr(config, "aux_model", ""))
        if aux:
            return aux
    return None


# ── an INHERITED pick (#3957 review) ────────────────────────────────────────────────
# A turn's pick is either carried by the request (it may hard-fail the turn) or
# inherited from the goal the turn drives (``GoalState.model``) — and an inherited pick
# must never lock the goal into failing. The drivers bind this marker for a turn whose
# pick is inherited; the model middleware falls back to the default when the provider
# rejects it at call time and flags the marker, so the driver can say so.


class InheritedPick:
    """The turn's inherited model pick; ``fell_back`` is set once a call on it was
    rejected and the turn moved to the default model. Mutable on purpose: LangGraph runs
    nodes in a COPY of the invoking context — the copy holds this same object."""

    def __init__(self, model: str):
        self.model = _clean(model)
        self.fell_back = False


_inherited_ctx: contextvars.ContextVar[InheritedPick | None] = contextvars.ContextVar(
    "protoagent_inherited_pick", default=None
)


def current_inherited_pick() -> InheritedPick | None:
    return _inherited_ctx.get()


class inherited_pick_scope:
    """Bind ``model`` as the turn's INHERITED pick (``""`` binds nothing). Sync and async,
    like :class:`turn_model_scope`; yields the marker (or ``None``)."""

    def __init__(self, model: Any):
        self._pick = InheritedPick(model) if _clean(model) else None
        self._token: contextvars.Token | None = None

    def __enter__(self):
        if self._pick is not None:
            self._token = _inherited_ctx.set(self._pick)
        return self._pick

    def __exit__(self, *_exc):
        if self._token is not None:
            try:
                _inherited_ctx.reset(self._token)
            except ValueError:
                _inherited_ctx.set(None)
            self._token = None
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exc):
        return self.__exit__(*exc)
