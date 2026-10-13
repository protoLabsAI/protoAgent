"""Managed Python runtime — status + one-click provisioning (ADR 0094).

    GET  /api/runtime/python          → runtime status + any in-flight install progress
    POST /api/runtime/python/install  → provision the managed CPython in the background (202)

The console renders a status card from the GET (Settings ▸ Tools, beside the
execute_code toggle — right where the gap bites) and drives the POST, polling the
GET for live progress. The install has two phases the message narrates: the
hash-verified interpreter download, then pip-installing the document baseline
into the runtime's own site-packages.

Gated by the ``/api/*`` bearer middleware (a2a_impl/auth.py) like every operator
route — only the authenticated operator can provision a runtime (the consent
click, per ADR 0071). ``operator_api`` may import ``runtime``/``infra`` but never
``server`` (import-linter); the install runs in a worker thread so the event loop
stays free.
"""

from __future__ import annotations

import asyncio
import logging
import threading

log = logging.getLogger("protoagent.server.python")

# Single in-flight install, tracked in memory (a second POST while one runs returns the
# live state rather than starting a second download). Reset on process restart — a
# partial install leaves nothing usable (the swap into `current/` precedes the deps
# phase, and status reports baseline state independently), so the next GET reports
# reality either way.
_install: dict = {"state": "idle", "pct": 0, "message": "", "error": None}
_lock = threading.Lock()


def _payload() -> dict:
    from runtime.python_install import python_status

    return {"python": python_status(), "install": dict(_install)}


def _run_install(force: bool) -> None:
    from runtime.python_install import PythonRuntimeError, install_managed_python

    def _progress(done: int, total: int) -> None:
        pct = int(done * 100 / total) if total else 0
        _install["pct"] = pct
        _install["message"] = f"downloading… {pct}%" if total else f"downloading… {done // (1 << 20)} MiB"

    def _phase(name: str) -> None:
        if name == "deps":
            _install["pct"] = 100
            _install["message"] = "installing document libraries…"

    try:
        install_managed_python(force=force, on_progress=_progress, on_phase=_phase)
        _install.update(state="done", pct=100, message="installed", error=None)
        log.info("[python] managed CPython provisioned via console")
    except PythonRuntimeError as exc:
        _install.update(state="error", message="install failed", error=str(exc))
        log.warning("[python] install failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 — a worker-thread crash must land as state, not a lost task
        _install.update(state="error", message="install failed", error=str(exc))
        log.exception("[python] unexpected install failure")


def register_python_routes(app) -> None:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter()

    @router.get("/api/runtime/python")
    async def _status():  # noqa: ANN202
        return JSONResponse(_payload())

    @router.post("/api/runtime/python/install")
    async def _install_ep(force: bool = False):  # noqa: ANN202, FBT001, FBT002
        from runtime.python_install import is_supported

        if not is_supported():
            return JSONResponse(
                {"ok": False, "error": "no managed Python build for this platform/architecture", **_payload()},
                status_code=400,
            )
        with _lock:
            if _install["state"] == "running":
                return JSONResponse({"ok": True, **_payload()}, status_code=202)  # already in flight
            _install.update(state="running", pct=0, message="starting…", error=None)
        # Blocking download+extract+pip off the event loop; progress lands in `_install`.
        asyncio.create_task(asyncio.to_thread(_run_install, force))
        return JSONResponse({"ok": True, **_payload()}, status_code=202)

    app.include_router(router)
