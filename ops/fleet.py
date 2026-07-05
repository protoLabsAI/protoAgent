"""Fleet ops (ADR 0075 D2) — start / stop / status the fleet member agents.

Thin ops over ``graph.fleet.supervisor`` — already the neutral shared core the fleet CLI
(`protoagent fleet …`) and the ``/api/fleet`` routes both project. They exist so fleet
lifecycle is in the op registry too: enumerated by ``GET /api/operations``, callable over
the operator MCP, each with the right read/write metadata (``up``/``down`` mutate; ``status``
reads). The supervisor's calls are blocking (subprocess + file state), so run off the loop.
"""

from __future__ import annotations

import asyncio

from ops import op


@op(name="fleet.up", mutates=True, summary="Start fleet member agents (named, or all workspaces) as background processes.")
async def up(names: list[str] | None = None) -> list[dict]:
    from graph.fleet import supervisor

    return await asyncio.to_thread(supervisor.up, names)


@op(name="fleet.down", mutates=True, summary="Stop fleet member agents (named, or all running).")
async def down(names: list[str] | None = None) -> list[dict]:
    from graph.fleet import supervisor

    return await asyncio.to_thread(supervisor.down, names)


@op(name="fleet.status", mutates=False, summary="List fleet members (host + workspaces + remotes) with live status.")
async def status() -> list[dict]:
    from graph.fleet import supervisor

    return await asyncio.to_thread(supervisor.status)
