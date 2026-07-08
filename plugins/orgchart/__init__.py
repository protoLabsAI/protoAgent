"""orgChart — a live diagram of the agent fleet's delegation graph.

Nodes are agents; a directed edge A→B means "A can delegate to B". The graph is
assembled by a server-side crawl (see ``view.build_data_router``): read this agent's
own delegates from config, then for each A2A peer we hold a token for, fetch that
peer's public card + health + its ``/api/delegates`` to discover its edges. Peers we
can't authenticate to appear as leaf nodes (their identity from the public card, but
their outbound edges unknown). Tokens are resolved from ``os.environ`` server-side and
never reach the browser.

Two routers (the view contract): the PAGE on the PUBLIC ``/plugins/orgchart`` prefix
(an iframe src can't carry a bearer), the DATA on the GATED ``/api/plugins/orgchart``.
"""

from __future__ import annotations

import logging


def register(registry) -> None:
    try:
        from .view import build_data_router, build_view_router

        registry.register_router(build_view_router(), prefix="/plugins/orgchart")
        registry.register_router(build_data_router(), prefix="/api/plugins/orgchart")
    except Exception:  # noqa: BLE001 — a view wiring error must not break agent boot
        logging.getLogger(__name__).exception("[orgchart] view registration failed")
