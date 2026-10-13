"""Provider identity carried by each concrete model client.

Provider-specific request shaping must follow the model that will receive one
particular call.  The configured default is not authoritative after a chat model
override or a fallback retry has replaced ``request.model``.
"""

from __future__ import annotations

from typing import Any

PROVIDER_TYPE_ATTR = "_protoagent_provider_type"
PROVIDER_ID_ATTR = "_protoagent_provider_id"


def tag_model_provider(model: Any, provider_type: str, provider_id: str = "") -> Any:
    """Stamp routing identity on a model without wrapping or changing its API.

    LangChain chat models are Pydantic objects, but their runtime instances allow
    private attributes. Native OAuth models fail closed if an incompatible model
    implementation cannot retain the identity: sending an unshaped request would
    either lose the Claude identity or leak a system-role item into Codex.
    """
    try:
        object.__setattr__(model, PROVIDER_TYPE_ATTR, (provider_type or "").strip().lower())
        object.__setattr__(model, PROVIDER_ID_ATTR, (provider_id or "").strip().lower())
    except (AttributeError, TypeError) as exc:
        if (provider_type or "").strip().lower() in {"anthropic-oauth", "openai-codex"}:
            raise TypeError("native provider model cannot retain its required wire-shape identity") from exc
    return model


def model_provider_type(model: Any) -> str:
    """Return the explicit provider type for ``model``, following bindings."""
    seen: set[int] = set()
    current = model
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        provider_type = getattr(current, PROVIDER_TYPE_ATTR, "")
        if provider_type:
            return str(provider_type).strip().lower()
        current = getattr(current, "bound", None)
    return ""
