"""Refuse a send whose ``contextId`` / ``taskId`` sits on ``params`` instead of the message.

Since a2a-sdk 1.2 the JSON-RPC dispatcher ignores unrecognized request fields for forward
compatibility (a2aproject/a2a-python#1273). That is right for a field a newer client adds,
but ``contextId`` / ``taskId`` are not new: they are core fields in the WRONG place
(``SendMessageRequest`` carries them only inside ``message``). Ignored, a params-level
``contextId`` silently starts a FRESH session: the agent "forgets" the conversation and a
HITL answer lands nowhere, with no error anywhere to explain it. 1.1 rejected the request
with -32602; this restores that refusal for exactly these keys, with a message that names
the fix. Every other unknown field still passes through to the SDK untouched.

The guard wraps the route's endpoint rather than reaching into the SDK's dispatcher. Both
mounts use it: ``add_a2a_routes_to_fastapi`` re-registers the route from ``endpoint``, and
a plain Starlette mount serves ``route.app``, which is rebuilt from the wrapped endpoint.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["guard_misplaced_ids", "misplaced_id_keys"]

# Both vocabularies served on /a2a: v1 (a2a-sdk 1.x) and the v0.3 compat adapter.
_SEND_METHODS = frozenset({"SendMessage", "SendStreamingMessage", "message/send", "message/stream"})
# The keys that belong inside ``params.message``, in either spelling.
_MESSAGE_KEYS = ("contextId", "context_id", "taskId", "task_id")

INVALID_PARAMS = -32602


def misplaced_id_keys(payload: Any) -> list[str]:
    """The message-level id keys a send request carries on ``params`` ([] when fine)."""
    if not isinstance(payload, dict) or payload.get("method") not in _SEND_METHODS:
        return []
    params = payload.get("params")
    if not isinstance(params, dict):
        return []
    return [k for k in _MESSAGE_KEYS if k in params]


def _refusal(request_id: Any, keys: list[str]) -> bytes:
    names = ", ".join(keys)
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": INVALID_PARAMS,
                "message": (
                    f"Invalid params: {names} belongs on params.message, not on params. "
                    "A params-level value is ignored, so the turn would run in a fresh context."
                ),
            },
        }
    ).encode()


def guard_misplaced_ids(endpoint: Any) -> Any:
    """Wrap a JSON-RPC route's ``endpoint`` (``async (Request) -> Response``) with the
    misplaced-id refusal. The body is read once here; Starlette caches it on the request,
    so the stock endpoint reads the identical bytes."""
    from starlette.requests import Request
    from starlette.responses import Response

    async def guarded(request: Request) -> Response:
        if request.method == "POST":
            body = await request.body()
            try:
                payload = json.loads(body) if body else None
            except ValueError:
                payload = None  # the SDK owns parse errors (-32700)
            keys = misplaced_id_keys(payload)
            if keys:
                log.info("[a2a] refused %s with %s on params (belongs on params.message)", payload.get("method"), keys)
                # JSON-RPC errors ride a 200, as the SDK's own do.
                return Response(_refusal(payload.get("id"), keys), media_type="application/json")
        return await endpoint(request)

    guarded._protoagent_misplaced_id_guard = True  # type: ignore[attr-defined]
    guarded.__wrapped__ = endpoint  # type: ignore[attr-defined]
    return guarded
