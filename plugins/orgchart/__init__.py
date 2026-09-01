"""orgChart — a live diagram of the agent fleet's delegation graph.

Nodes are agents; a directed edge A→B means "A can delegate to B" (dashed = "A
supervises B", for hub-registered fleet members). The graph is assembled server-side
by :mod:`.topology`: the EFFECTIVE delegate roster (agent ∪ fleet-shared, ADR 0105)
seeds a BFS that fetches each token-held peer's public card + ``/api/delegates``,
reusing the delegates plugin's cached health snapshot instead of re-probing. All
network reads are TTL-cached and the snapshot serves stale-while-revalidate, so the
view paints instantly after the first crawl. Tokens are resolved server-side and
never reach the browser.

Two routers (the view contract): the PAGE on the PUBLIC ``/plugins/orgchart`` prefix
(an iframe src can't carry a bearer), the DATA on the GATED ``/api/plugins/orgchart``.
"""

from __future__ import annotations

import logging


def register(registry) -> None:
    try:
        from . import topology
        from .view import build_data_router, build_view_router

        topology.reset()  # hot-reload: don't serve a snapshot crawled under the old code/config
        registry.register_router(build_view_router(), prefix="/plugins/orgchart")
        registry.register_router(build_data_router(registry.live_config), prefix="/api/plugins/orgchart")
    except Exception:  # noqa: BLE001 — a view wiring error must not break agent boot
        logging.getLogger(__name__).exception("[orgchart] view registration failed")
