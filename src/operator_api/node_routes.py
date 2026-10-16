"""Managed Node runtime — status + one-click provisioning (ADR 0085).

    GET  /api/runtime/node          → Node status + any in-flight install progress
    POST /api/runtime/node/install  → provision the managed Node in the background (202)

The console renders a status card from the GET and drives the POST, polling the GET
for live progress (the install downloads a Node tarball — seconds, not instant).

Gated by the ``/api/*`` bearer middleware (a2a_impl/auth.py) like every operator route
— only the authenticated operator can provision a runtime. ``operator_api`` may import
``runtime``/``infra`` but never ``server`` (import-linter); the install runs in a worker
thread so the event loop stays free.
"""

from __future__ import annotations

import asyncio
import logging
import threading

log = logging.getLogger("protoagent.server.node")

# Single in-flight install, tracked in memory (a second POST while one runs returns the
# live state rather than starting a second download). Reset on process restart — a
# partial install leaves nothing usable (the swap into `current/` is the last step), so
# the next GET reports reality either way.
_install: dict = {"state": "idle", "pct": 0, "message": "", "error": None}
_lock = threading.Lock()
_tasks: set[asyncio.Task] = set()  # strong refs to in-flight install tasks


def _payload() -> dict:
    from runtime.node_install import node_status

    return {"node": node_status(), "install": dict(_install)}


def _refresh_runtime_gaps() -> None:
    """Re-check the plugins whose ``requires_runtimes`` gap is up (ADR 0085 amendment), so
    their "needs Node.js" banner clears the moment the managed install lands — no reload.
    Runs BEFORE the install state flips to ``done``, so a console that refetches runtime
    status on ``done`` already sees the cleared gap. Best-effort: a failure here must not
    turn a successful install into an error."""
    try:
        from graph.plugins.loader import refresh_runtime_gaps

        cleared = refresh_runtime_gaps()
        if cleared:
            log.info("[node] runtime requirement now met for: %s", ", ".join(cleared))
    except Exception:  # noqa: BLE001 — the install succeeded; a stale banner self-heals on reload
        log.warning("[node] couldn't re-check plugin runtime requirements", exc_info=True)


def _run_install(force: bool) -> None:
    from runtime.node_install import NodeRuntimeError, install_managed_node

    def _progress(done: int, total: int) -> None:
        pct = int(done * 100 / total) if total else 0
        _install["pct"] = pct
        _install["message"] = f"downloading… {pct}%" if total else f"downloading… {done // (1 << 20)} MiB"

    try:
        install_managed_node(force=force, on_progress=_progress)
        _refresh_runtime_gaps()
        _install.update(state="done", pct=100, message="installed", error=None)
        log.info("[node] managed Node provisioned via console")
    except NodeRuntimeError as exc:
        _install.update(state="error", message="install failed", error=str(exc))
        log.warning("[node] install failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 — a worker-thread crash must land as state, not a lost task
        _install.update(state="error", message="install failed", error=str(exc))
        log.exception("[node] unexpected install failure")


def register_node_routes(app) -> None:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter()

    @router.get("/api/runtime/node")
    async def _status():  # noqa: ANN202
        # node_status() probes the filesystem and may spawn `node --version` — off the loop.
        return JSONResponse(await asyncio.to_thread(_payload))

    @router.post("/api/runtime/node/install")
    async def _install_ep(force: bool = False):  # noqa: ANN202, FBT001, FBT002
        from runtime.node_install import is_supported

        if not is_supported():
            return JSONResponse(
                {
                    "ok": False,
                    "error": "no managed Node build for this platform/architecture",
                    **(await asyncio.to_thread(_payload)),
                },
                status_code=400,
            )
        with _lock:
            already_running = _install["state"] == "running"
            if not already_running:
                _install.update(state="running", pct=0, message="starting…", error=None)
        if not already_running:
            # Blocking download+extract off the event loop; progress lands in `_install`.
            # Keep a strong reference: the loop holds tasks only weakly, so a bare
            # create_task can be garbage-collected mid-install.
            task = asyncio.create_task(asyncio.to_thread(_run_install, force))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
        # A second POST while one runs reports the live state rather than starting another download.
        return JSONResponse({"ok": True, **(await asyncio.to_thread(_payload))}, status_code=202)

    app.include_router(router)
