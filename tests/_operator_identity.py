"""Stamp a verified human-operator identity on an isolated route-harness app.

Decision answer/cancel (#5678) require the ``principal_kind`` the auth middleware stamps;
route tests that mount ``register_decision_routes`` without the real middleware use this to
stand in for an authenticated operator console."""


def as_operator(app, principal_kind: str = "shared-operator-bearer"):
    """Add a middleware that marks every request as ``principal_kind``; returns ``app``."""

    @app.middleware("http")
    async def operator_identity(request, call_next):
        request.state.principal_kind = principal_kind
        return await call_next(request)

    return app
