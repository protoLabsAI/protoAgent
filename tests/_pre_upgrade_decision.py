"""A stand-in for ``request_decision`` as it was before #5678 slice 10: it paused the turn.

Today's tool writes a no-pause ledger record and never calls ``interrupt()``. A ``kind:
"decision"`` pause still reaches the turn drivers from two places: the unattended policy's
ask tier, and a checkpoint parked by the old tool before the upgrade (its record keeps an
interrupt id and must stay answerable). Tests of that pause machinery park a real checkpoint
with this helper instead of the real tool.
"""

from __future__ import annotations

from typing import Any


def pre_upgrade_request_decision(args: dict[str, Any]) -> str:
    """Pause on the payload the old tool raised and return the resume value as a string."""
    from langgraph.types import interrupt

    payload = {"kind": "decision", "question": "", "options": [], "context": "",
               "expires_in_minutes": None, "default_option": None, "default_action": None,
               "requires_pick": False, **args}
    answer = interrupt(payload)
    return answer if isinstance(answer, str) else str(answer)


class _PreUpgradeTool:
    """``.invoke(args)`` like the tool object, so a graph node can call it the same way."""

    name = "request_decision"

    @staticmethod
    def invoke(args: dict[str, Any]) -> str:
        return pre_upgrade_request_decision(args)


PRE_UPGRADE_REQUEST_DECISION = _PreUpgradeTool()
