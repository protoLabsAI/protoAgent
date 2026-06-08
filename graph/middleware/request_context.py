"""Per-turn request metadata, exposed to middleware via a contextvar (ADR 0032).

The A2A request's merged metadata (project scope, origin, caller-supplied keys) is
request-scoped, not part of the agent state. Middleware — including plugin-contributed
ones registered via ``register_middleware`` — read it through
``current_request_metadata()``; the chat/executor layer binds it for the duration of a
turn via ``request_metadata_scope()``. Mirrors ``tracing.current_session_id``.

This is what lets a plugin middleware replicate a per-request directive (e.g. roxy's
project-scope banner) without editing the core executor.
"""

from __future__ import annotations

import contextvars

_request_metadata_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "protoagent_request_metadata", default={},
)


def current_request_metadata() -> dict:
    """The merged A2A request metadata for the in-flight turn (``{}`` when none)."""
    return _request_metadata_ctx.get()


class request_metadata_scope:
    """Bind ``metadata`` as the current request metadata for the enclosed turn.

    Works as both a sync (`with`) and async (`async with`) context manager — the
    A2A stream enters it via `async with` alongside `trace_session`, while sync
    callers/tests use plain `with`. set/reset are sync; the contextvar propagates
    into awaited graph invokes on the same task.
    """

    def __init__(self, metadata: dict | None):
        self._metadata = dict(metadata) if metadata else {}
        self._token: contextvars.Token | None = None

    def __enter__(self):
        self._token = _request_metadata_ctx.set(self._metadata)
        return self

    def __exit__(self, *_exc):
        if self._token is not None:
            _request_metadata_ctx.reset(self._token)
            self._token = None
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exc):
        return self.__exit__(*exc)
