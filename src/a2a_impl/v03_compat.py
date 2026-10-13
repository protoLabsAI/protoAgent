"""The /a2a JSON-RPC routes, with v0.3 compat errors carrying their real codes (#3929).

Upstream bug (a2a-sdk 1.1.0): the v0.3 compat adapter
(``a2a.compat.v0_3.jsonrpc_adapter.JSONRPC03Adapter``) wraps EVERY exception its request
handler raises in ``InternalError`` (-32603). So a classic ``tasks/get`` for an unknown
task — which the SDK's own handler reports as ``TaskNotFoundError`` — reaches the caller
as "internal error", while the same lookup through the v1 ``GetTask`` method returns the
spec's task-not-found code (-32001). A peer that branches on the code (retry vs. give up)
treats a missing task as a server fault.

The fix is host-side and narrow: wrap the adapter instance's non-streaming step so an
``A2AError`` is rendered through the SDK's OWN v1 mapping (``build_error_response`` +
``JSON_RPC_ERROR_CODE_MAP``) — the exact codes the v1 path returns — and anything else
still falls through to the adapter's existing -32603 handling. Like
:mod:`a2a_impl.registry`, it reaches one private attribute (``_v03_adapter``) because the
dispatcher is built inside ``create_jsonrpc_routes`` with no seam; every access is
guarded, so an SDK that moves it degrades to a logged warning + stock behavior. Re-verify
(ideally delete) when bumping past a2a-sdk 1.1.0.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["create_a2a_jsonrpc_routes", "harden_v03_error_codes"]


def harden_v03_error_codes(routes: list[Any]) -> bool:
    """Make the v0.3 compat adapter behind ``routes`` report ``A2AError`` codes.

    Returns True when an adapter was found and wrapped (idempotent)."""
    try:
        from a2a.server.request_handlers.response_helpers import build_error_response
        from a2a.utils.errors import A2AError
        from starlette.responses import JSONResponse
    except Exception:  # noqa: BLE001 — SDK layout moved; keep stock behavior
        log.warning("[a2a] v0.3 error-code fix unavailable (a2a-sdk layout changed)", exc_info=True)
        return False

    patched = False
    for route in routes or []:
        dispatcher = getattr(getattr(route, "endpoint", None), "__self__", None)
        adapter = getattr(dispatcher, "_v03_adapter", None)
        step = getattr(adapter, "_process_non_streaming_request", None)
        if adapter is None or step is None:
            continue
        if getattr(step, "_protoagent_v03_errors", False):
            patched = True
            continue

        async def _with_real_codes(request_id, request_obj, context, *, _step=step):
            try:
                return await _step(request_id, request_obj, context)
            except A2AError as exc:
                # The same shape (and code) the v1 dispatcher renders for this error.
                return JSONResponse(build_error_response(request_id, exc))

        _with_real_codes._protoagent_v03_errors = True  # type: ignore[attr-defined]
        adapter._process_non_streaming_request = _with_real_codes
        patched = True
    if not patched:
        log.warning("[a2a] no v0.3 compat adapter found on the JSON-RPC routes; error codes stay stock")
    return patched


def create_a2a_jsonrpc_routes(request_handler: Any, rpc_url: str = "/a2a") -> list[Any]:
    """``create_jsonrpc_routes`` with v0.3 compat ON, its error codes fixed, and a send
    whose ``contextId`` / ``taskId`` sits on ``params`` refused (see
    :mod:`a2a_impl.request_guard`).

    The one place production (and the tests that mirror it) builds the JSON-RPC routes."""
    from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes

    from starlette.routing import request_response

    from a2a_impl.request_guard import guard_misplaced_ids

    routes = create_jsonrpc_routes(request_handler, rpc_url=rpc_url, enable_v0_3_compat=True)
    harden_v03_error_codes(routes)  # finds the adapter via endpoint.__self__ — before wrapping
    for route in routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or getattr(endpoint, "_protoagent_misplaced_id_guard", False):
            continue
        # FastAPI re-registers from ``endpoint``; a Starlette mount serves ``app``.
        route.endpoint = guard_misplaced_ids(endpoint)
        route.app = request_response(route.endpoint)
    return routes
