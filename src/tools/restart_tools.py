"""Consent-gated restart with a server-owned scheduler injected by the lead builder."""

from collections.abc import Callable

from langchain_core.tools import tool
from langgraph.types import interrupt

from tools.fs_tools import _approved


def build_restart_tools(scheduler: Callable[[str], dict] | None):
    """Bind self_restart only when a restart scheduler is available."""
    if scheduler is None:
        return []

    @tool
    async def self_restart(reason: str) -> str:
        """Ask the operator to approve restarting this agent, for example after a plugin update.

        After scheduling a restart, end your turn so the restart can proceed.
        """
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
