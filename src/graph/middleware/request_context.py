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
    "protoagent_request_metadata",
    default={},
)


# Whether the caller driving this turn may approve (or pre-approve) a gated tool call.
# Bound by the server from the AUTHENTICATED credential's tier — never from the metadata
# above, which carries caller-supplied keys. Default True: a turn nothing bound this for
# is in-process (a test, the SDK, a local run), not a remote caller.
_may_approve_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "protoagent_caller_may_approve",
    default=True,
)


def current_request_metadata() -> dict:
    """The merged A2A request metadata for the in-flight turn (``{}`` when none)."""
    return _request_metadata_ctx.get()


def caller_may_approve() -> bool:
    """Whether the in-flight turn's caller may answer an approval card or ask to skip one.

    False for a federation-tier caller (ADR 0066: no host code execution) — the server
    binds it from the credential it authenticated, so nothing in the request can set it."""
    return _may_approve_ctx.get()


class request_metadata_scope:
    """Bind ``metadata`` as the current request metadata for the enclosed turn.

    Works as both a sync (`with`) and async (`async with`) context manager — the
    A2A stream enters it via `async with` alongside `trace_session`, while sync
    callers/tests use plain `with`. set/reset are sync; the contextvar propagates
    into awaited graph invokes on the same task.
    """

    def __init__(self, metadata: dict | None, *, may_approve: bool = True):
        self._metadata = dict(metadata) if metadata else {}
        self._may_approve = bool(may_approve)
        if not self._may_approve:
            # The per-turn approval skip is the operator's toggle; a caller that can't
            # approve can't ask for it either. Dropped here so every reader of the metadata
            # (plugin middleware included) sees the same thing the gate does.
            self._metadata.pop("bypass_permissions", None)
        self._token: contextvars.Token | None = None
        self._approve_token: contextvars.Token | None = None

    def __enter__(self):
        self._token = _request_metadata_ctx.set(self._metadata)
        # Narrow-only: a turn started inside one that can't approve can't approve either.
        self._approve_token = _may_approve_ctx.set(self._may_approve and _may_approve_ctx.get())
        return self

    def __exit__(self, *_exc):
        if self._approve_token is not None:
            try:
                _may_approve_ctx.reset(self._approve_token)
            except ValueError:  # reset from a different Context — same as below
                _may_approve_ctx.set(True)
            self._approve_token = None
        if self._token is not None:
            try:
                _request_metadata_ctx.reset(self._token)
            except ValueError:
                # The token was created in a different Context — happens when the body
                # awaited across context boundaries (e.g. the ACP runtime's reader-loop
                # tasks). Resetting is best-effort; fall back to clearing the value.
                _request_metadata_ctx.set({})
            self._token = None
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exc):
        return self.__exit__(*exc)


class approval_authority_scope:
    """Bind ONLY the approval authority for the enclosed turn, leaving the request metadata
    as it is — for a driver that carries no metadata of its own (the non-streaming one),
    which may also run nested inside a turn whose metadata must stay visible. Narrow-only,
    like ``request_metadata_scope``."""

    def __init__(self, may_approve: bool):
        self._may_approve = bool(may_approve)
        self._token: contextvars.Token | None = None

    def __enter__(self):
        self._token = _may_approve_ctx.set(self._may_approve and _may_approve_ctx.get())
        return self

    def __exit__(self, *_exc):
        if self._token is not None:
            try:
                _may_approve_ctx.reset(self._token)
            except ValueError:  # reset from a different Context
                _may_approve_ctx.set(True)
            self._token = None
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exc):
        return self.__exit__(*exc)
