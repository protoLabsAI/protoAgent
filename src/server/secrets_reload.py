"""Refresh live model clients after external secrets hydration."""

from __future__ import annotations

import asyncio
import logging

from graph.config_io import is_setup_complete
from infra.secrets.hydrate import SourceStatus
from runtime.state import STATE

log = logging.getLogger(__name__)


async def reload_after_secrets(result: SourceStatus | None) -> dict:
    """Use the Settings reload path off-loop, containing and redacting failures."""
    if result is None or not (result.changed or (STATE.graph is None and is_setup_complete())):
        return {}

    from server.agent_init import _reload_langgraph_agent

    try:
        ok, _message = await asyncio.to_thread(_reload_langgraph_agent)
    except Exception:  # noqa: BLE001 — hydration succeeded; never turn Sync into a 500
        ok = False
    if not ok:
        # Arbitrary reload errors may carry credentials or variable names.
        log.warning("[secrets] graph reload failed after hydration")
        return {"graph_reloaded": False, "graph_reload_error": "graph reload failed after secrets hydration"}
    return {"graph_reloaded": True}
