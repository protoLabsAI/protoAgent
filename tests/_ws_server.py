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

    # A peer that's already gone never answers the close frame. The default is 10s.
    serve_kwargs.setdefault("close_timeout", 0.5)
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
        try:
            await asyncio.wait_for(server.wait_closed(), 1)
        except TimeoutError:
            pass  # wait_closed can hang on a peer that's gone; the cancel below ends it
        pending = asyncio.all_tasks() - {asyncio.current_task()}
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def _run():
        asyncio.set_event_loop(state.loop)
        state.loop.run_until_complete(_main())
        state.loop.run_until_complete(state.loop.shutdown_asyncgens())

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
