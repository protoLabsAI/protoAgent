"""Persistent MCP session pool — one long-lived session per server, shared across calls.

Why: ``MultiServerMCPClient`` is stateless — every tool INVOCATION opens a fresh
MCP session, which for a stdio server means spawning a fresh subprocess per call
(measured ~1s of pure overhead per call for ``npx``-launched servers). An agent
turn making 5–15 MCP calls pays that 5–15 times over. This pool keeps ONE
long-lived session per configured server, lazily opened on first use and reused
across calls. This also matches how other MCP hosts (Claude Desktop, Cursor)
drive servers — one session for the app's lifetime — so servers keep whatever
in-memory state they expect to keep.

Design constraints (verified against the pinned ``mcp`` / adapter sources):

* **Task affinity** — ``ClientSession`` and the transport contexts are anyio
  task groups: they must be entered and exited by the SAME asyncio task. Each
  server session therefore lives inside a dedicated *owner task* that opens the
  context, parks on a stop event, and exits the context on shutdown.
* **Loop affinity** — protoAgent invokes tools from whatever event loop happens
  to be current (the server loop, ``_run_blocking`` throwaway loops, sync
  wrappers). A session cannot hop loops, so the pool runs ONE daemon thread
  with its own event loop and bridges every call onto it with
  ``run_coroutine_threadsafe``. That keeps the discovered tools loop-agnostic,
  exactly like the stateless per-call design they replace.
* **Concurrency** — the MCP SDK multiplexes concurrent in-flight requests over
  one session (responses are matched by JSON-RPC request id, see
  ``BaseSession._response_streams``), so parallel calls are protocol-safe. We
  still serialize per server with an asyncio lock: most community stdio servers
  process requests sequentially anyway, and single-flight sessions make the
  reconnect path race-free (a broken pipe cannot trigger competing respawns).
  Serialized-but-pooled is still orders of magnitude faster than parallel
  spawns; lifting the lock later would be a pure concurrency change.
* **Reconnect** — a dead subprocess surfaces as ``McpError(CONNECTION_CLOSED)``
  on the in-flight call (the SDK's receive loop flushes pending requests with
  that error on EOF — so a dying server fails FAST, it does not hang) or as a
  transport/stream error on the next send. Either way the pool tears the
  session down and retries ONCE on a fresh session; a second failure
  propagates, and ``PooledServerSession`` degrades it to a ``ToolException`` so
  the model sees a recoverable tool-error string instead of a dead turn. Note
  the retried request may have partially executed on a server that died
  mid-call — the same at-most-twice semantics every retrying MCP host has.
* **Shutdown** — ``close()`` is sync, idempotent, and callable from any thread
  (config reload swaps pools; ``atexit`` covers process exit). In-flight calls
  fail with a tool error, never hang.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
from typing import Any

log = logging.getLogger("protoagent.mcp")

# Owner tasks get this long to exit their session contexts before being cancelled.
_SLOT_CLOSE_TIMEOUT = 5.0


def _session_is_dead(exc: BaseException) -> bool:
    """Whether ``exc`` means the SESSION is broken (vs a live server saying no).

    A killed/dead server surfaces as ``McpError`` with code ``CONNECTION_CLOSED``
    on the in-flight call, or as a transport/stream error (``BrokenResourceError``,
    ``ClosedResourceError``, ...) on the next send. Any other ``McpError`` is a
    protocol-level answer from a healthy server (unknown method, request timeout,
    ...) — reconnecting would not help, so it is re-raised without a retry.
    """
    from mcp.shared.exceptions import McpError
    from mcp.types import CONNECTION_CLOSED

    if isinstance(exc, McpError):
        error = getattr(exc, "error", None)
        return error is not None and error.code == CONNECTION_CLOSED
    return True


class _Slot:
    """Per-server pool state. Mutated only on the pool loop (single-threaded)."""

    __slots__ = ("connection", "lock", "name", "session", "stop", "task")

    def __init__(self, name: str, connection: dict) -> None:
        self.name = name
        self.connection = connection
        # Binds to the pool loop on first use (asyncio.Lock is loop-lazy on 3.10+).
        self.lock = asyncio.Lock()
        self.session: Any = None  # live ClientSession while open
        self.stop: asyncio.Event | None = None  # tells the owner task to exit
        self.task: asyncio.Task | None = None  # the owner task itself


class MCPSessionPool:
    """One long-lived MCP session per server, reused across tool calls.

    Create per ``build_mcp_tools`` run; hand out ``server_session`` proxies for
    the adapter to bind tools to; ``close()`` when the config is rebuilt.
    """

    def __init__(self, *, connect_timeout: float = 20.0) -> None:
        self._connect_timeout = connect_timeout
        self._slots: dict[str, _Slot] = {}
        self._state = threading.Lock()  # guards _slots/_loop/_thread/_closed
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        atexit.register(self.close)  # unregistered again in close()

    # ── public surface ───────────────────────────────────────────────────────

    def server_session(self, server_name: str, connection: dict) -> "PooledServerSession":
        """A loop-agnostic session proxy for ``server_name`` (no I/O yet —
        the real session opens lazily on the first round-trip)."""
        with self._state:
            if self._closed:
                raise RuntimeError("MCP session pool is closed")
            slot = self._slots.get(server_name)
            if slot is None:
                slot = _Slot(server_name, dict(connection))
                self._slots[server_name] = slot
        return PooledServerSession(self, slot)

    def close(self, *, timeout: float = 10.0) -> None:
        """Shut down every session and the pool loop. Idempotent, thread-safe.

        Called on config reload (the old pool is swapped out) and at process
        exit. An in-flight call sees its pool-side task cancelled and degrades
        to a tool error on the caller side — it never hangs.
        """
        with self._state:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
            slots = list(self._slots.values())
            self._slots.clear()
        atexit.unregister(self.close)
        if loop is None:  # never started — nothing to unwind
            return

        async def _shutdown() -> None:
            for slot in slots:
                try:
                    async with slot.lock:
                        await self._close_slot_locked(slot)
                except Exception:  # noqa: BLE001 — close the rest regardless
                    log.warning("[mcp] error closing session for %r", slot.name, exc_info=True)

        if thread is not None and thread is threading.current_thread():
            # Defensive: close() from the pool thread itself can't block on it.
            loop.create_task(_shutdown())
            loop.call_soon(loop.stop)
            return
        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), loop).result(timeout)
        except Exception:  # noqa: BLE001 — a stuck session must not wedge shutdown
            log.warning("[mcp] pool shutdown incomplete — cancelling remaining sessions")

        def _finalize() -> None:
            # Cancel stragglers (e.g. a call still holding a slot lock), then stop
            # once the cancellations have had a cycle to unwind.
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.call_soon(loop.stop)

        loop.call_soon_threadsafe(_finalize)
        if thread is not None:
            thread.join(timeout=5.0)
            if thread.is_alive():  # pragma: no cover — pathological hang
                log.warning("[mcp] pool thread did not stop; leaving loop open")
                return
        loop.close()

    async def roundtrip(self, slot: _Slot, method: str, /, *args: Any, **kwargs: Any) -> Any:
        """Run ``session.<method>(*args, **kwargs)`` on the pool loop, from any loop."""
        loop = self._ensure_loop()  # before building the coroutine — may raise (closed)
        future = asyncio.run_coroutine_threadsafe(self._call(slot, method, args, kwargs), loop)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            if future.cancelled():
                # Pool-side cancellation (shutdown/reload swap), NOT the caller's
                # own cancel — degrade instead of cancelling the whole turn.
                raise RuntimeError(
                    f"MCP session pool closed while calling server {slot.name!r}"
                ) from None
            future.cancel()  # caller cancelled — propagate to the pool side
            raise

    # ── pool-loop internals (everything below runs on the pool loop) ─────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._state:
            if self._closed:
                raise RuntimeError("MCP session pool is closed")
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever, name="mcp-session-pool", daemon=True
                )
                self._thread.start()
            return self._loop

    async def _call(self, slot: _Slot, method: str, args: tuple, kwargs: dict) -> Any:
        async with slot.lock:
            fresh = slot.session is None
            session = slot.session if not fresh else await self._open_locked(slot)
            try:
                return await getattr(session, method)(*args, **kwargs)
            except Exception as exc:
                if not _session_is_dead(exc):
                    raise  # protocol-level error from a live server — no reconnect
                await self._close_slot_locked(slot)
                if fresh:
                    raise  # a brand-new session already failed — don't loop
                log.warning(
                    "[mcp] %s: session died mid-call (%s) — reconnecting once", slot.name, exc
                )
                session = await self._open_locked(slot)
                try:
                    return await getattr(session, method)(*args, **kwargs)
                except Exception as exc2:
                    if _session_is_dead(exc2):
                        await self._close_slot_locked(slot)
                    raise

    async def _open_locked(self, slot: _Slot) -> Any:
        """Open ``slot``'s session (slot lock held) inside a dedicated owner task,
        so the anyio task-group contexts are entered/exited by one task."""
        from langchain_mcp_adapters.sessions import create_session

        loop = asyncio.get_running_loop()
        ready: asyncio.Future = loop.create_future()
        stop = asyncio.Event()

        async def owner() -> None:
            try:
                async with create_session(slot.connection) as session:
                    await session.initialize()
                    if not ready.done():
                        ready.set_result(session)
                    await stop.wait()
            except BaseException as exc:  # noqa: BLE001 — surfaced via `ready` or logged
                if not ready.done():
                    ready.set_exception(exc)
                    return
                log.debug("[mcp] %s: session owner exited: %r", slot.name, exc)
            finally:
                if slot.stop is stop:  # still the current generation → mark closed
                    slot.session = slot.task = slot.stop = None

        task = loop.create_task(owner(), name=f"mcp-session-{slot.name}")
        try:
            session = await asyncio.wait_for(asyncio.shield(ready), self._connect_timeout)
        except BaseException:
            stop.set()
            task.cancel()
            # A late `ready` exception (owner failing after our timeout) must be
            # retrieved or asyncio logs "exception was never retrieved".
            ready.add_done_callback(
                lambda f: f.exception() if not f.cancelled() else None
            )
            raise
        slot.session, slot.stop, slot.task = session, stop, task
        log.info("[mcp] %s: persistent session opened", slot.name)
        return session

    async def _close_slot_locked(self, slot: _Slot) -> None:
        """Tear down ``slot``'s session (slot lock held). Never raises."""
        task, stop = slot.task, slot.stop
        slot.session = slot.stop = slot.task = None
        if stop is not None:
            stop.set()
        if task is not None and not task.done():
            # asyncio.wait (not wait_for/await) so a cancelled owner can't
            # propagate CancelledError into the closer.
            done, _ = await asyncio.wait({task}, timeout=_SLOT_CLOSE_TIMEOUT)
            if not done:
                task.cancel()
                await asyncio.wait({task}, timeout=_SLOT_CLOSE_TIMEOUT)


class PooledServerSession:
    """Duck-typed stand-in for ``mcp.ClientSession``, backed by the pool.

    Implements exactly the surface ``langchain_mcp_adapters`` touches when
    handed a session — ``list_tools`` (discovery, with pagination cursor) and
    ``call_tool`` (invocation) — and unlike a raw ``ClientSession`` it is safe
    to use from any thread or event loop.
    """

    def __init__(self, pool: MCPSessionPool, slot: _Slot) -> None:
        self._pool = pool
        self._slot = slot

    @property
    def server_name(self) -> str:
        return self._slot.name

    async def list_tools(self, cursor: str | None = None) -> Any:
        return await self._pool.roundtrip(self._slot, "list_tools", cursor=cursor)

    async def call_tool(self, name: str, arguments: dict | None = None, **kwargs: Any) -> Any:
        from langchain_core.tools import ToolException
        from mcp.shared.exceptions import McpError

        try:
            return await self._pool.roundtrip(self._slot, "call_tool", name, arguments, **kwargs)
        except McpError as exc:
            if _session_is_dead(exc):
                # Server died mid-call AND the one reconnect retry died too.
                raise ToolException(
                    f"MCP server '{self._slot.name}' died during the call "
                    f"(one reconnect was attempted): {exc}"
                ) from exc
            raise  # live-server protocol error — same semantics as per-call sessions
        except ToolException:
            raise
        except Exception as exc:  # noqa: BLE001 — degrade, don't kill the turn
            # Transport gone and reconnect failed (server won't start, pool closed
            # by a config reload, ...). A ToolException is caught by the tool's
            # handle_tool_error and returned to the model as a recoverable
            # tool-error string — a dead engine must not cost the whole turn.
            raise ToolException(
                f"MCP server '{self._slot.name}' is unreachable (reconnect failed): {exc}"
            ) from exc
