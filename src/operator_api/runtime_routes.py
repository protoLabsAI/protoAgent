"""Operator server controls — ``POST /api/restart`` (graceful self-restart).

Restarting the process is the only reliable way to apply changes that can't be
hot-mounted (a plugin's view code, a removed route, env/launch-flag changes). This
endpoint does it the safe way: set a flag, trigger the existing graceful-shutdown
path (the #882 Ctrl-C handler — spins down fleet members, withdraws mDNS, stops the
scheduler), and let ``server._main`` re-exec a fresh process AFTER ``uvicorn.run()``
returns (so the port is released first). A hard ``os.execv`` from inside the request
would skip that cleanup and could fail to rebind the port.

Gated by the ``/api/*`` bearer middleware (a2a_impl/auth.py) like every operator
route — only the authenticated operator can restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid

from fastapi import Body, HTTPException, Request

log = logging.getLogger("protoagent.server.restart")
_BOOT_ID = uuid.uuid4().hex

# A when-idle restart only waits for a turn that raced in during the response
# flush (the route 409s unless idle at request time). Bound that wait so it never
# outlives the operator watching for the restart — the console gives up after
# ~120 one-second polls, so the server abandons at or before that and leaves the
# re-exec flag unset, rather than firing a restart the operator was told failed.
_RESTART_IDLE_GRACE_S = 120


def restart_readiness() -> dict:
    """Read existing work registries; never infer idle from an unavailable store."""
    from runtime.state import STATE
    from runtime.turn_activity import active_sessions

    busy = []
    if active_sessions():
        busy.append("a chat turn is running")
    if STATE.background_mgr is not None:
        jobs = STATE.background_mgr.store.list(status="running", limit=500, include_dismissed=True)
        if jobs:
            count = f"{len(jobs)}" if len(jobs) < 500 else "500 or more"
            busy.append(f"{count} background job{'s' if len(jobs) != 1 else ''}")
    supervised = STATE.uvicorn_server is not None or bool(os.environ.get("PROTOAGENT_FLEET_TOKEN"))
    return {
        "idle": not busy, "busy": busy, "supervised": supervised,
        "how_to": "" if supervised else "Stop the server and start it again the way you launched it.",
        "boot_id": _BOOT_ID,
    }


def audit_restart(request: Request, reason: str, *, target: str = "self") -> None:
    from observability.audit import audit_logger

    actor = getattr(request.state, "device_id", None) or "operator"
    reason = " ".join(reason.split())[:1000]
    audit_logger.log(
        session_id="operator", tool="restart", args={"actor": actor, "reason": reason, "target": target},
        result_summary="Restart requested", duration_ms=0, success=True,
    )
    log.info("[restart] actor=%s target=%s reason=%s", actor, target, reason)



def reexec_command(executable: str, argv: list[str], frozen: bool) -> list[str]:
    """The argv to re-exec the server with, preserving the launch flags. A PyInstaller
    frozen build re-runs the binary directly; from source it's ``python -m server``.
    Pure (no side effects) so it's unit-testable; ``server._main`` feeds it the real
    ``sys.executable`` / ``sys.argv`` / ``sys.frozen`` and calls ``os.execv``."""
    rest = list(argv[1:])  # argv[0] is the script/binary; drop it
    if frozen:
        return [executable, *rest]
    return [executable, "-m", "server", *rest]


def request_server_exit() -> bool:
    """Ask the live uvicorn Server to wind down. True if a server was asked.

    The portable half of the restart (#2585). The old path signalled this process —
    ``os.kill(os.getpid(), signal.SIGINT)`` — which works on POSIX and is *fatal* on
    Windows: ``os.kill`` there delivers only ``CTRL_C_EVENT``/``CTRL_BREAK_EVENT`` as
    signals and turns every other value, ``SIGINT`` included, into ``TerminateProcess``.
    So the frozen Windows server was killed outright at exactly the point it was supposed
    to drain: the endpoint answered 202, the process vanished, ``uvicorn.run()`` never
    returned, and the re-exec in ``server._main`` never ran. Nothing restarted, and the
    operator's only recovery was launching the binary by hand.

    Setting ``should_exit`` is what uvicorn's own signal handler does, minus the signal —
    so the drain, the graceful timeout and the re-exec all behave identically everywhere,
    and the path is exercisable off Windows.
    """
    from runtime.state import STATE

    server = getattr(STATE, "uvicorn_server", None)
    if server is None:
        return False
    server.should_exit = True
    return True


def _shutdown_started() -> bool:
    """True once the live uvicorn Server has already been asked to wind down — by a
    Ctrl-C, a hub supervisor stop, or an earlier restart. The when-idle wait watches
    this so it drops a not-yet-armed restart instead of re-launching on a shutdown that
    is already underway (``_main`` only re-execs when ``STATE.restart_requested`` is set,
    and this path leaves it unset)."""
    from runtime.state import STATE

    server = getattr(STATE, "uvicorn_server", None)
    return bool(server is not None and getattr(server, "should_exit", False))


def register_runtime_control_routes(app) -> None:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter()

    @router.get("/api/restart/readiness")
    async def _readiness():
        return restart_readiness()

    @router.post("/api/restart")
    async def _restart(request: Request, body: dict = Body(default={})):
        """Gracefully restart the server. Returns 202 immediately; the process drains
        and re-execs a moment later, and the console reconnects via the boot gate."""
        from runtime.state import STATE

        audit_restart(request, str(body.get("reason", "operator request")))
        when_idle = bool(body.get("when_idle"))
        # Existing callers retain their graceful restart; Settings explicitly requests
        # the idle guard, rechecked here after the operator's confirmation.
        if when_idle:
            readiness = restart_readiness()
            if not readiness["supervised"]:
                raise HTTPException(409, readiness["how_to"])
            if not readiness["idle"]:
                raise HTTPException(409, "; ".join(readiness["busy"]))
            # Don't arm the re-exec yet. STATE.restart_requested is the authoritative
            # "re-exec once uvicorn.run() returns" signal; setting it before the
            # bounded wait below would re-launch the server on an unrelated Ctrl-C or
            # hub supervisor stop during that wait. It is armed only once idle is
            # reconfirmed, just before the graceful exit is triggered.
        else:
            STATE.restart_requested = True  # no wait — commit as the self-restart always has

        async def _drain_then_signal():
            # Let the 202 flush to the client, then ask the server to wind down; _main
            # re-execs once its run() returns (iff STATE.restart_requested is set).
            await asyncio.sleep(0.3)
            if when_idle:
                # A turn may have started while the response flushed. Give it a bounded
                # grace to finish, and bail the instant the process is already winding
                # down for another reason. In both the time-out and the shutdown case
                # we return WITHOUT arming the re-exec, so the server exits cleanly
                # instead of re-launching on a restart the operator never saw through.
                try:
                    for _ in range(_RESTART_IDLE_GRACE_S):
                        if _shutdown_started():
                            log.info("[restart] shutdown already underway; restart abandoned")
                            return
                        if restart_readiness()["idle"]:
                            break
                        await asyncio.sleep(1)
                    else:
                        log.warning("[restart] agent stayed busy; when-idle restart abandoned")
                        return
                except Exception:
                    log.exception("[restart] could not verify idle; restart abandoned")
                    return
                STATE.restart_requested = True  # idle reconfirmed — arm the re-exec now
            if request_server_exit():
                return
            # No live Server reference (an embedding host that runs the app itself).
            # Fall back to the signal — which is what this used to do unconditionally,
            # and why Windows never restarted (see request_server_exit).
            try:
                os.kill(os.getpid(), signal.SIGINT)
            except Exception:  # noqa: BLE001 — last resort if signalling fails
                log.exception("[restart] failed to signal graceful shutdown")

        asyncio.create_task(_drain_then_signal())
        log.info("[restart] operator requested a restart — draining then re-exec")
        return JSONResponse({"ok": True, "restarting": True}, status_code=202)

    app.include_router(router)
