"""Host services a plugin surface/route can reach (ADR 0018+).

A plugin's *tools* run inside the graph, but a *surface* (an ingress gateway like
Discord) needs to call the agent and the event bus — host services it can't
construct itself. The server **populates** these once, before the startup hook
fires; a plugin reads them at surface-start time, via ``registry.host`` (the same
singleton) or ``from graph.plugins.host import HOST``.

Each is optional (``None`` until the server wires it / in a non-server context),
so a plugin guards: ``if registry.host.invoke: ...``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


@dataclass
class PluginHost:
    # async (prompt, session_id) -> str — invoke the agent as a chat surface
    # (one conversation per session_id, the LangGraph thread key).
    invoke: Optional[Callable[[str, str], Awaitable[str]]] = None
    # (event: str, data: dict) -> None — publish to the server→client event bus.
    publish: Optional[Callable[[str, dict], Any]] = None
    # () -> subscription — subscribe to the event bus (e.g. return-address delivery).
    subscribe: Optional[Callable[[], Any]] = None


# Process-lifetime singleton. The server fills it in; plugins read it.
HOST = PluginHost()
