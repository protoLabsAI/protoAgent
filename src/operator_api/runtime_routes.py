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

log = logging.getLogger("protoagent.server.restart")


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


def register_runtime_control_routes(app) -> None:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter()

    @router.post("/api/restart")
    async def _restart():
        """Gracefully restart the server. Returns 202 immediately; the process drains
        and re-execs a moment later, and the console reconnects via the boot gate."""
        from runtime.state import STATE

        STATE.restart_requested = True

        async def _drain_then_signal():
            # Let the 202 flush to the client, then ask the server to wind down; _main
            # re-execs once its run() returns.
            await asyncio.sleep(0.3)
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
