"""Resolve a model build's generation profile (ADR 0128 D2).

Host-free: this module imports only ``graph.config`` (enforced by an import-linter
contract). It sits next to ``resolve_subagent_model`` and merges the operator-defined
``model.profiles`` named settings across the layers ADR 0128 defines —
``per-call > subagent > role > global`` — field by field, handing the result to
``graph.llm.create_llm(profile=...)`` (wired in S2b for every dispatch path).

The *global* layer is the config's own ``model.*`` block, which ``create_llm`` already
reads; a :class:`ResolvedProfile` therefore carries only the *overrides* a named profile
places on top of it. When no layer names a defined profile every field stays ``None``
(``.empty`` is true) and the call behaves exactly as today (D7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields

from graph.config import GenerationProfile, LangGraphConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedProfile:
    """The merged profile overrides for one model build; ``None`` means "inherit global"."""

    max_tokens: int | None = None
    thinking: str | None = None
    reasoning_effort: str | None = None
    thinking_budget: int | None = None
    chat_template_kwargs: dict | None = None
    context_budget: int | None = None

    @property
    def empty(self) -> bool:
        """True when no field is set — the call runs on the global block, exactly as today."""
        return all(getattr(self, f.name) is None for f in fields(self))


def _profile_for(config: LangGraphConfig, name: str | None, *, layer: str) -> GenerationProfile | None:
    """The :class:`GenerationProfile` a layer names, or ``None``.

    A blank name is simply "this layer sets nothing". A name that is not defined in
    ``model.profiles`` resolves to nothing too — it is not an error (D2), but it logs one
    warning so a typo is visible, and never raises.
    """
    clean = name.strip() if isinstance(name, str) else ""
    if not clean:
        return None
    profiles = getattr(config, "model_profiles", None) or {}
    profile = profiles.get(clean)
    if profile is None:
        log.warning(
            "generation profile %r (named by the %s layer) is not defined in model.profiles; ignoring it",
            clean,
            layer,
        )
        return None
    return profile


def resolve_profile(
    config: LangGraphConfig,
    *,
    call: str | None = None,
    subagent: str | None = None,
    role: str | None = None,
) -> ResolvedProfile:
    """Merge the named generation profiles for one model build (ADR 0128 D2).

    Precedence, highest first: ``call`` > ``subagent`` > ``role`` > global. Each argument
    is a profile *name*; one not defined in ``config.model_profiles`` resolves to nothing
    (and logs one warning). Scalar fields take the value from the highest layer that set
    them; ``chat_template_kwargs`` is merged key by key (higher layers win per key). The
    global block is never folded in — a field no layer sets stays ``None`` so
    ``create_llm`` keeps the config's own value.
    """
    # Lowest priority first, so a higher (later) layer overwrites a lower one field by field.
    layers = [
        _profile_for(config, role, layer="role"),
        _profile_for(config, subagent, layer="subagent"),
        _profile_for(config, call, layer="per-call"),
    ]

    merged: dict = {}
    chat_template: dict = {}
    for profile in layers:
        if profile is None:
            continue
        for f in fields(profile):
            value = getattr(profile, f.name)
            if value is None:
                continue
            if f.name == "chat_template_kwargs":
                chat_template.update(value)
            else:
                merged[f.name] = value
    if chat_template:
        merged["chat_template_kwargs"] = chat_template
    return ResolvedProfile(**merged)
