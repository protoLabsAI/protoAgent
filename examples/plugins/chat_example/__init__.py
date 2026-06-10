"""Example chat-slot plugin (ADR 0045).

The smallest real replacement for the console's chat panel: the manifest's view
declares ``slot: "chat"``, so the page served here renders under the core Chat rail
id — kept mounted for the app's lifetime — instead of adding its own icon.

The page (``panel.html``) is plain HTML/JS and demonstrates the contract pieces a
chat panel needs:

- the ``protoagent:init`` postMessage handshake (bearer + theme; never in URLs) and
  live ``protoagent:theme`` re-themes (ADR 0038),
- slug-aware routing — derive the API base from the iframe's own path so the panel
  talks to the agent its *window* is focused on (ADR 0042),
- a turn via the documented **non-streaming** ``POST /api/chat`` fallback.

It deliberately stops there: a production panel should drive the streaming A2A
contract (``SendStreamingMessage`` + ``tasks/get`` reconciliation) and honor the
ADR 0045 conformance checklist. This is the scaffold you grow that from.

This is a copy-me example, not a bundled plugin — the loader only discovers
directories under ``plugins/``. To try it::

    cp -r examples/plugins/chat_example plugins/

then ``plugins: { enabled: [chat_example] }`` and reload — Chat becomes this
panel. Disable (or delete) the copy and the built-in chat is back.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("protoagent.plugins.chat_example")

_PANEL = Path(__file__).parent / "panel.html"


def _build_router():
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get("/panel")
    async def _panel():
        # Read per request — edit panel.html and refresh, no restart (it's an example).
        return HTMLResponse(_PANEL.read_text(encoding="utf-8"))

    return router


def register(registry):
    # Served OUTSIDE /api/ on purpose: the iframe's page load can't carry a bearer
    # header, so the page itself is public chrome — the token arrives via the
    # protoagent:init postMessage and is used for the page's own API calls.
    registry.register_router(_build_router(), prefix="/plugins/chat_example")
