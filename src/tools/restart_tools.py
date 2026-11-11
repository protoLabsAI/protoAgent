"""Consent-gated restart with a server-owned scheduler injected by the lead builder."""

from collections.abc import Callable
import re

from langchain_core.tools import tool
from langgraph.types import interrupt

from tools.fs_tools import _approved


DisabledToolGroups = tuple[tuple[str, tuple[str, ...]], ...]


def disabled_tool_groups(dropped, disabled, hidden=()) -> DisabledToolGroups:
    """Describe only loaded, publicly disabled tools from the builder's catalog."""
    # Match the denylist's normalization, including hidden winning on overlap.
    public = {str(name).strip() for name in (disabled or ())} - {
        str(name).strip() for name in (hidden or ())
    }
    groups: dict[str, set[str]] = {}
    for item in dropped:
        if item.name not in public:
            continue
        metadata = getattr(item, "metadata", None) or {}
        category = metadata.get("category")
        # Tool metadata is supplied by the tool factory, never inferred from prose.
        group = category.strip() if isinstance(category, str) and category.strip() else item.name.split("_", 1)[0]
        groups.setdefault(group, set()).add(item.name)
    return tuple((group, tuple(sorted(names))) for group, names in sorted(groups.items()))


def disabled_tools_explanation(groups: DisabledToolGroups) -> str:
    if not groups:
        return ""
    rows = "\n".join(f"- {group}: {', '.join(names)}" for group, names in groups)
    return (
        "These loaded tools are excluded by tools.disabled in the current configuration:\n"
        f"{rows}\nRestart cannot enable them. Ask the operator to enable them in Settings > Tools. "
        "Do not retry self_restart to load these capabilities."
    )


def disabled_restart_reason(reason: str, groups: DisabledToolGroups) -> str | None:
    """Recognize explicit load/enable requests, not arbitrary mentions in prose.

    Full matching deliberately leaves ambiguous or compound reasons to consent.
    """
    for group, names in groups:
        for target in (group, *names):
            pattern = (
                r"\s*(?:please\s+)?(?:restart\s+(?:(?:the\s+)?agent\s+)?(?:to|and)\s+)?"
                r"(?:load|enable|activate|make available)\s+(?:the\s+)?[`\"']?"
                + re.escape(target)
                + r"[`\"']?(?:\s+(?:tools?|capability|integration|plugin))?\s*[.!]?\s*"
            )
            if re.fullmatch(pattern, reason, re.IGNORECASE):
                return "Not restarted: " + disabled_tools_explanation(((group, names),))
    return None


def build_restart_tools(
    scheduler: Callable[[str], dict] | None,
    preflight: Callable[[str], str | None] | None = None,
):
    """Bind self_restart only when a restart scheduler is available."""
    if scheduler is None:
        return []

    @tool
    async def self_restart(reason: str) -> str:
        """Ask the operator to approve restarting this agent, for example after a plugin update.

        After scheduling a restart, end your turn so the restart can proceed.
        """
        if preflight is not None:
            refusal = preflight(reason)
            if refusal:
                return refusal
        decision = interrupt(
            {
                "kind": "approval",
                "title": "Approve agent restart?",
                "detail": reason,
                "session_allow": False,
            }
        )
        if not _approved(decision):
            return "Not restarted: explicit operator approval is required. Do not retry without operator instruction."
        result = scheduler(reason)
        if result.get("scheduled") is not True:
            return result.get("reason", "Not restarted: restart was not scheduled.")
        return "Restart scheduled: it happens once this turn ends. End your turn now."

    return [self_restart]
