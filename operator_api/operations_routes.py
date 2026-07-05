"""Operations catalog route (ADR 0075 D2) — ``GET /api/operations``.

Enumerates every op on the shared ``ops/`` layer (name, whether it mutates state, a one-line
summary) — "one operation, three projections" made introspectable, and the source the CLI
help + the safe-operator MCP profile derive from rather than hand-maintaining. Read-only,
behind the standard operator-API auth gate; derived from ``ops.registry()`` via ``load_all``
so it reflects every op module, not just the ones some surface happened to import.
"""

from __future__ import annotations


def register_operations_routes(app) -> None:
    """Register ``GET /api/operations``."""

    @app.get("/api/operations")
    async def _api_operations():
        from ops import load_all

        specs = load_all()
        return {
            "operations": [
                {"name": s.name, "mutates": s.mutates, "summary": s.summary}
                for s in sorted(specs.values(), key=lambda s: s.name)
            ]
        }
