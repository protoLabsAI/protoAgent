"""Which model a subagent run uses — ONE precedence for every dispatch path (#3944).

1. The subagent's own pinned model (``subagents.<name>.model``, applied onto
   ``SUBAGENT_REGISTRY`` at config load) — a deliberate per-role choice wins.
2. The turn's model override (the per-tab / per-request ``metadata.model`` the lead
   turn carries as ``state["model"]``) — a delegation follows the model the operator
   picked for the turn that spawned it.
3. The default: ``routing.aux_model`` (the fast helper alias), else the main model.

Used by in-graph ``task`` / ``task_batch`` delegations, ``/<subagent>`` slash runs,
and background jobs. A background job runs the full lead graph as a detached turn,
so it only needs levels 1-2 carried in its fire metadata — with neither, the fire
carries no model and the turn runs on the configured default, exactly as before.
"""

from __future__ import annotations

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
