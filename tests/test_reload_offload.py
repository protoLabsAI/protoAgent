"""Regression for #497 — the config reload is offloaded off the event loop so
its heavy graph compile no longer freezes the server. The follow-up scheduler /
Discord restart still has to run *on* the loop; from the worker thread the old
code hit ``get_running_loop()``'s ``RuntimeError`` branch and silently dropped
it (killing the scheduler/briefing). ``_run_on_server_loop`` must instead
marshal the coroutine onto the captured ``_main_loop``.
"""

import asyncio

import pytest


@pytest.mark.asyncio
async def test_run_on_server_loop_from_worker_thread_runs_on_main_loop():
    """Scheduled from a worker thread (no running loop) → runs on _main_loop."""
    import server

    saved = server.STATE.main_loop
    server.STATE.main_loop = asyncio.get_running_loop()
    try:
        ran = asyncio.Event()
        ran_on = {}

        async def _work():
            ran_on["loop"] = asyncio.get_running_loop()
            ran.set()

        def _from_worker_thread():
            # No running loop here — the offloaded-reload case. Must NOT drop it.
            server._run_on_server_loop(_work, "test")

        await asyncio.to_thread(_from_worker_thread)
        await asyncio.wait_for(ran.wait(), timeout=3)
        assert ran_on["loop"] is server.STATE.main_loop
    finally:
        server.STATE.main_loop = saved


@pytest.mark.asyncio
async def test_run_on_server_loop_on_loop_runs_directly():
    """Called on the loop (a direct, non-offloaded reload) → still runs."""
    import server

    ran = asyncio.Event()

    async def _work():
        ran.set()

    server._run_on_server_loop(_work, "test")
    await asyncio.wait_for(ran.wait(), timeout=3)
    assert ran.is_set()
