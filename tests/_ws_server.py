"""A real WebSocket server on a free port, in a background thread.

``stop()`` closes the server and its connections on the server's own loop, joins the
thread and closes the loop. Calling ``loop.stop()`` instead leaves the connection tasks
pending on a dead loop. Garbage collection then finalizes them inside whichever test is
running, where they cancelled an unrelated ``asyncio.run`` in CI.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from types import SimpleNamespace


def start_ws_server(handler: Callable, **serve_kwargs) -> SimpleNamespace:
    """Serve ``handler``. Returns a namespace with ``port``, ``loop`` and ``stop()``."""
    import websockets

    ready = threading.Event()
    state = SimpleNamespace(port=0, loop=asyncio.new_event_loop(), stop=None)
    shutdown: dict = {}

    async def _main():
        shutdown["event"] = asyncio.Event()
        server = await websockets.serve(handler, "127.0.0.1", 0, **serve_kwargs)
        state.port = server.sockets[0].getsockname()[1]
        ready.set()
        await shutdown["event"].wait()
        server.close()
        await server.wait_closed()

    def _run():
        asyncio.set_event_loop(state.loop)
        state.loop.run_until_complete(_main())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert ready.wait(5), "ws server didn't start"

    def stop() -> None:
        if state.loop.is_closed():
            return
        state.loop.call_soon_threadsafe(shutdown["event"].set)
        thread.join(10)
        assert not thread.is_alive(), "ws server didn't stop"
        state.loop.close()

    state.stop = stop
    return state
