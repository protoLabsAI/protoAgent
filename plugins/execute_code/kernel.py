"""Session kernels for execute_code (ADR 0134 D1 + the D2 timeout rule, S1a).

With ``execute_code.session_state: true`` the first call in a session starts a
long-lived child — the *kernel* — keyed by ``session_id``, and later calls run
their code in the kernel's existing globals, so variables, imports and functions
carry over. The model can fetch a large tool result once, keep it in a variable,
and filter it again in a later call without re-fetching or printing it into
context — the reason the bridge exists.

A kernel is one child spawned exactly like the one-shot runner (same interpreter
resolution, scrubbed env, ``group_kwargs`` + ``track_tree`` lifecycle, ADR 0098),
gated by a per-kernel token (the ADR 0089 discipline applied to the kernel's
lifetime). It runs a tiny driver loop that receives *code frames* over its socket
and ``exec``s each in one persistent globals dict holding the ``tools`` proxy.
stdout/stderr are captured per call; the bridge's concurrent calls (#5418) and
the ``result`` structured return (#5420) work unchanged, and ``result`` is cleared
before each call so one run's value never leaks into the next.

Policy binds to the *call*, not the kernel (ADR 0134 D3): every bridged call is
checked against the session_id, fence and enforcement gate of the call running
now — the same ``_invoke_bridged`` path the one-shot service uses. A call that
times out kills the kernel (state is lost) and says so in plain words; the first
call in a fresh kernel tells the model it has no earlier state.

The pool API (``get_or_start``/``stop``/``stop_all``) is deliberately small; S1b
extends it with the idle TTL, the per-instance kernel cap and chat teardown.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets as _secrets
import tempfile
import time

from infra.proc import akill_tree, group_kwargs, track_tree, untrack_tree, untrack_when_reaped

from .engine import (
    _MAX_PARALLEL_CALLS,
    _NO_RUNTIME_MESSAGE,
    _invoke_bridged,
    _resolve_child_interpreter,
    _truncate_or_spill,
)

log = logging.getLogger(__name__)

#: How long the parent waits for a freshly spawned kernel to connect back and
#: authenticate before giving up. A healthy child connects in milliseconds; the
#: generous bound only covers a cold interpreter on a loaded CI box.
_KERNEL_START_TIMEOUT = 30.0

#: D2 limits (ADR 0134), overridden by the plugin's ``kernel_idle_ttl`` /
#: ``max_kernels`` config. A kernel unused for ``idle_ttl`` seconds is stopped;
#: at ``max_kernels`` live kernels a new one evicts the least-recently-used first.
_DEFAULT_IDLE_TTL = 600.0
_DEFAULT_MAX_KERNELS = 4

#: The periodic idle-sweep backstop runs at most this often. The per-call lazy sweep
#: handles the common case (a session that keeps calling); this only catches a chat
#: that goes idle and never calls again, so a coarse cadence is enough.
_SWEEP_INTERVAL_CAP = 60.0


# The driver program the kernel child runs. Stdlib-only (like the one-shot
# prelude) and portable to Windows by using a loopback socket, not fd inheritance.
# Internal names live in the driver's own module namespace, kept OUT of the exec
# globals (``_G``) so user code can neither see nor clobber the bridge machinery.
#
# Protocol (line-delimited JSON, both directions):
#   parent -> kernel : {"type":"exec","cid":N,"code":...}  and
#                      {"type":"tool_reply","id":M,"ok":...,"result"/"error":...}
#   kernel -> parent : {"type":"tool_call","id":M,"tool":...,"args":...}  and
#                      {"type":"exec_result","cid":N,"status":...,"stdout":...,
#                       "stderr":...,"error":...,"result":...}
# Each exec runs in its own worker thread so the main thread can keep reading and
# routing tool replies while the code's bridged calls are in flight (#5418).
_KERNEL_DRIVER = r'''
import os, sys, json, socket, threading, io, traceback
from contextlib import redirect_stdout, redirect_stderr

_sock = socket.create_connection(("127.0.0.1", int(os.environ["EC_PORT"])))
_REQ = _sock.makefile("w")   # kernel -> parent
_RESP = _sock.makefile("r")  # parent -> kernel
_REQ.write(os.environ["EC_TOKEN"] + "\n")  # authenticate this kernel's connection
_REQ.flush()

_write_lock = threading.Lock()
_tool_lock = threading.Lock()
_tool_seq = 0
_tool_pending = {}

def _send(_obj):
    _data = json.dumps(_obj) + "\n"
    with _write_lock:
        _REQ.write(_data)
        _REQ.flush()

def _tool_call(_name, **kwargs):
    global _tool_seq
    with _tool_lock:
        _tool_seq += 1
        _rid = _tool_seq
        _waiter = [threading.Event(), None]
        _tool_pending[_rid] = _waiter
    _send({"type": "tool_call", "id": _rid, "tool": _name, "args": kwargs})
    _waiter[0].wait()
    _resp = _waiter[1]
    if not _resp.get("ok"):
        raise RuntimeError(_resp.get("error") or ("tool '%s' failed" % _name))
    return _resp.get("result")

class _ToolProxies:
    """Attribute access returns a callable that RPCs the named tool."""
    def __getattr__(self, _name):
        def _proxy(**kwargs):
            return _tool_call(_name, **kwargs)
        _proxy.__name__ = _name
        return _proxy
    def __call__(self, _name, **kwargs):  # tools("name", **kw) also works
        return _tool_call(_name, **kwargs)

# The one persistent namespace: variables, imports and functions carry over here.
_G = {"__name__": "__main__", "tools": _ToolProxies()}

def _run_exec(_cid, _code):
    _G.pop("result", None)  # one run's result never leaks into the next (#5420)
    _out, _err = io.StringIO(), io.StringIO()
    _status, _error, _want_result = "ok", "", True
    with redirect_stdout(_out), redirect_stderr(_err):
        try:
            exec(compile(_code, "<execute_code>", "exec"), _G)
        except SystemExit as _se:
            _want_result = False  # sys.exit() skips the result epilogue, as one-shot
            if _se.code not in (None, 0):
                _status, _error = "error", "SystemExit: %r" % (_se.code,)
        except BaseException:
            _want_result = False
            _status, _error = "error", traceback.format_exc()
    _result_text = None
    if _status == "ok" and _want_result and "result" in _G:
        try:
            _result_text = json.dumps(_G["result"], default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            _result_text = json.dumps(str(_G["result"]), ensure_ascii=False)
    _send({
        "type": "exec_result", "cid": _cid, "status": _status,
        "stdout": _out.getvalue(), "stderr": _err.getvalue(),
        "error": _error, "result": _result_text,
    })

def _route(_line):
    _msg = json.loads(_line)
    _type = _msg.get("type")
    if _type == "tool_reply":
        with _tool_lock:
            _waiter = _tool_pending.pop(_msg.get("id"), None)
        if _waiter is not None:
            _waiter[1] = _msg
            _waiter[0].set()
    elif _type == "exec":
        threading.Thread(target=_run_exec, args=(_msg.get("cid"), _msg.get("code")), daemon=True).start()

def _driver():
    while True:
        _line = _RESP.readline()
        if not _line:
            break
        try:
            _route(_line)
        except Exception:
            pass  # a malformed control frame must not kill the kernel

_driver()
'''


class _KernelDied(Exception):
    """The kernel process exited or its connection was lost mid-call."""


class _KernelTimeout(Exception):
    """A call did not finish within its per-call timeout."""


def _write_driver_file() -> str:
    fd, path = tempfile.mkstemp(prefix="ec_kernel_", suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write(_KERNEL_DRIVER)
    return path


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    """Read one newline-terminated frame from ``reader`` with NO size ceiling.

    An ``exec_result`` frame carries the call's WHOLE stdout as a single JSON
    line, so a script that prints more than asyncio's default 64 KiB
    ``StreamReader`` limit would make ``StreamReader.readline()`` raise
    ``ValueError`` — and that escapes the read loop, kills the kernel and wipes
    the session's state even though the exec succeeded. Reassemble the line from
    the ``LimitOverrunError`` chunks instead, so a kernel reply is as unbounded as
    the one-shot path's ``proc.communicate()``. Returns ``b""`` at a clean EOF.
    """
    chunks: list[bytes] = []
    while True:
        try:
            chunk = await reader.readuntil(b"\n")
        except asyncio.LimitOverrunError as exc:
            # The newline isn't within the limit yet: drain exactly the bytes
            # scanned so far (already buffered, so this returns at once) and keep
            # reading for the separator.
            chunks.append(await reader.readexactly(exc.consumed))
            continue
        except asyncio.IncompleteReadError as exc:
            # EOF before a newline — return whatever remained (b"" at a clean EOF).
            chunks.append(exc.partial)
            return b"".join(chunks)
        chunks.append(chunk)
        return b"".join(chunks)


def _format_kernel_output(msg: dict) -> str:
    """Turn an ``exec_result`` frame into the same shaped text the one-shot path
    returns: stdout, an optional ``--- result ---`` block, or an error report."""
    out = (msg.get("stdout") or "").strip()
    if msg.get("status") == "error":
        detail = (msg.get("error") or "").strip() or "(no error output)"
        body = f"Error: script raised an exception.\n{detail}"
        if out:
            body += f"\n\n--- stdout before failure ---\n{out}"
        return body
    result_text = msg.get("result")
    if result_text is not None:
        out = f"{out}\n--- result ---\n{result_text}" if out else result_text
    if not out:
        out = "(script produced no stdout)"
    return out


class Kernel:
    """One long-lived child running the driver loop, plus its parent-side bridge.

    Calls are serialised through ``_call_lock`` (one exec outstanding at a time);
    each exec's bridged tool calls fan out concurrently up to ``_MAX_PARALLEL_CALLS``,
    exactly as a one-shot run does.
    """

    def __init__(self, session_id: str, interpreter: str):
        self.session_id = session_id
        self._interpreter = interpreter
        self._token = _secrets.token_hex(16)
        self._proc: asyncio.subprocess.Process | None = None
        self._server: asyncio.AbstractServer | None = None
        self._port: int | None = None
        self._conn: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self._authed: asyncio.Future | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: list[str] = []
        self._driver_path: str | None = None
        self._write_lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(_MAX_PARALLEL_CALLS)
        self._pending: set[asyncio.Task] = set()
        self._exec_future: asyncio.Future | None = None
        self._exec_cid = 0
        # The context of the call running now (ADR 0134 D3) — bridged calls bind to it.
        self._ctx: dict = {"tool_map": {}, "session_id": "", "gate": None, "fence": None}
        self._alive = False
        self._closed = False
        #: Monotonic stamp (the pool's clock) of this kernel's last use — set when the
        #: pool hands it out and when a call finishes, read by the idle TTL / LRU sweeps.
        self.last_used: float = 0.0

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def alive(self) -> bool:
        return self._alive and self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._authed = loop.create_future()
        self._server = await asyncio.start_server(self._on_client, "127.0.0.1", 0)
        self._port = self._server.sockets[0].getsockname()[1]
        self._driver_path = _write_driver_file()
        # Scrubbed env — no gateway keys / auth tokens reach the kernel, same as the
        # one-shot child. On Windows a fresh python.exe still needs a few OS vars.
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
            "EC_PORT": str(self._port),
            "EC_TOKEN": self._token,
        }
        if os.name == "nt":
            for key in ("SystemRoot", "TEMP", "TMP", "COMSPEC", "PATHEXT"):
                if key in os.environ:
                    env[key] = os.environ[key]
        self._proc = await asyncio.create_subprocess_exec(
            self._interpreter,
            "-u",
            self._driver_path,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,  # per-exec stdout is captured in-child and framed
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **group_kwargs(),  # ADR 0098: anchor the tree so teardown reaps grandchildren too
        )
        track_tree(self._proc.pid)  # owned until reaped (#3428)
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())
        try:
            reader, writer = await asyncio.wait_for(self._authed, timeout=_KERNEL_START_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — any failed start is a dead kernel
            tail = "; ".join(self._stderr_tail[-5:])
            await self.close()
            raise _KernelDied(f"kernel failed to start: {exc}{(' — ' + tail) if tail else ''}") from exc
        self._conn = (reader, writer)
        self._alive = True
        self._reader_task = asyncio.ensure_future(self._read_loop())

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await reader.readline()
        except Exception:  # noqa: BLE001 — a failed handshake just drops the socket
            first = b""
        # Wrong token, or a second connection after we're already serving one → refuse.
        if self._authed is None or self._authed.done() or first.decode(errors="replace").strip() != self._token:
            with contextlib.suppress(Exception):
                writer.close()
            return
        self._authed.set_result((reader, writer))

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        with contextlib.suppress(Exception):
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    del self._stderr_tail[:-50]  # keep only the last 50 lines
                    log.debug("[execute_code kernel %s] %s", self.session_id, text)

    async def _read_loop(self) -> None:
        reader = self._conn[0]  # type: ignore[index]
        try:
            while True:
                # Unbounded: an exec_result frame carries the full stdout, which can
                # exceed asyncio's 64 KiB readline limit — reading it whole keeps a
                # large-output exec from killing the kernel (parity with communicate()).
                line = await _read_frame(reader)
                if not line:
                    break
                try:
                    msg = json.loads(line.decode())
                except Exception:  # noqa: BLE001 — a malformed frame is ignored, not fatal
                    continue
                kind = msg.get("type")
                if kind == "exec_result":
                    fut = self._exec_future
                    if fut is not None and not fut.done() and msg.get("cid") == self._exec_cid:
                        fut.set_result(msg)
                elif kind == "tool_call":
                    task = asyncio.create_task(self._dispatch_tool(msg))
                    self._pending.add(task)
                    task.add_done_callback(self._pending.discard)
        except asyncio.CancelledError:
            raise
        finally:
            self._alive = False
            fut = self._exec_future
            if fut is not None and not fut.done():
                fut.set_exception(_KernelDied("kernel exited before responding"))

    async def _dispatch_tool(self, msg: dict) -> None:
        async with self._semaphore:
            rid, name, args = msg.get("id"), msg.get("tool"), msg.get("args") or {}
            ctx = self._ctx
            ok, payload = await _invoke_bridged(
                name, args, ctx["tool_map"], session_id=ctx["session_id"], gate=ctx["gate"], fence=ctx["fence"]
            )
            reply = {"type": "tool_reply", "id": rid, "ok": ok, ("result" if ok else "error"): payload}
            with contextlib.suppress(Exception):
                await self._send(reply)

    async def _send(self, obj: dict) -> None:
        conn = self._conn
        if conn is None:
            raise _KernelDied("kernel connection is closed")
        data = (json.dumps(obj) + "\n").encode()
        async with self._write_lock:
            try:
                conn[1].write(data)
                await conn[1].drain()
            except Exception as exc:  # noqa: BLE001 — a lost pipe is a dead kernel
                raise _KernelDied(f"kernel connection lost: {exc}") from exc

    async def run(self, code: str, *, tool_map: dict, timeout: float, session_id: str, gate, fence) -> str:
        """Run ``code`` in the kernel's persistent globals and return the body text
        (pre-truncation). Raises ``_KernelTimeout`` if the call overruns ``timeout``
        and ``_KernelDied`` if the kernel is gone."""
        async with self._call_lock:
            if not self.alive:
                raise _KernelDied("kernel is not running")
            # Bind this call's policy context (ADR 0134 D3) before any bridged call can arrive.
            self._ctx = {"tool_map": tool_map, "session_id": session_id, "gate": gate, "fence": fence}
            loop = asyncio.get_running_loop()
            self._exec_cid += 1
            cid = self._exec_cid
            fut = loop.create_future()
            self._exec_future = fut
            try:
                await self._send({"type": "exec", "cid": cid, "code": code})
                msg = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise _KernelTimeout() from exc
            finally:
                self._exec_future = None
            return _format_kernel_output(msg)

    async def close(self) -> None:
        """Kill the kernel's whole tree, reap it, and release every parent-side
        resource. Idempotent; leaves no process and no tracked tree behind. Every
        wait is bounded so a wedged child or socket can't stall teardown."""
        if self._closed:
            return
        self._closed = True
        self._alive = False
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                await akill_tree(proc)  # ADR 0098: the whole tree, not just the direct child
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5.0)  # reap so the pid is gone, not a zombie
        for task in (self._reader_task, self._stderr_task, *list(self._pending)):
            if task is not None:
                task.cancel()
        for task in (self._reader_task, self._stderr_task, *list(self._pending)):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._pending.clear()
        fut = self._exec_future
        if fut is not None and not fut.done():
            fut.set_exception(_KernelDied("kernel stopped"))
        if proc is not None:
            if proc.returncode is not None:
                untrack_tree(proc.pid)
            else:
                with contextlib.suppress(Exception):
                    untrack_when_reaped(proc)
        # Close the accepted connection BEFORE the server: in Python 3.12
        # Server.wait_closed() blocks until every active transport is gone, so an
        # open connection writer would wedge it (the one-shot path closes first too).
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn[1].close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._conn[1].wait_closed(), timeout=2.0)
            self._conn = None
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            self._server = None
        if self._driver_path:
            with contextlib.suppress(OSError):
                os.unlink(self._driver_path)
            self._driver_path = None


class KernelPool:
    """Session_id -> live kernel, bounded by the ADR 0134 D2 limits: a kernel unused
    for ``idle_ttl`` is swept, and a new kernel over ``max_kernels`` evicts the
    least-recently-used one first. Teardown on chat delete/clear is wired by the
    plugin (``__init__.py``), which calls :meth:`stop` on the bus event."""

    def __init__(self):
        self.kernels: dict[str, Kernel] = {}
        self.idle_ttl: float = _DEFAULT_IDLE_TTL
        self.max_kernels: int = _DEFAULT_MAX_KERNELS
        # The monotonic clock the TTL/LRU stamps are measured on. An attribute (not a
        # bare time.monotonic call) so a test can drive idle expiry with a fake clock
        # instead of a real sleep, which the test-policy gate bans (check_test_policy).
        self._clock = time.monotonic
        # The periodic idle-sweep backstop task (the plugin's background surface starts
        # it; stop_sweeper cancels it). Bound per running loop like the lock below.
        self._sweeper: asyncio.Task | None = None
        # Created lazily and rebound per running loop: the pool is a module-level
        # singleton that outlives any single test's event loop, and an asyncio.Lock
        # bound to a dead loop would raise when awaited in the next one.
        self._lock: asyncio.Lock | None = None
        self._lock_loop = None

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def configure(self, *, idle_ttl: float, max_kernels: int) -> None:
        """Apply the operator's kernel limits (ADR 0134 D2). Called at each graph
        build from live config, so an edited limit takes effect on reload."""
        self.idle_ttl = float(idle_ttl)
        self.max_kernels = int(max_kernels)

    def _idle_sids(self, now: float) -> list[str]:
        """Session ids whose kernel has been unused for at least ``idle_ttl`` (snapshot
        of the keys so a caller can pop while iterating)."""
        return [sid for sid, k in list(self.kernels.items()) if now - k.last_used >= self.idle_ttl]

    async def get_or_start(self, session_id: str, *, interpreter: str) -> tuple[Kernel, bool]:
        """Return the session's kernel, starting one if none is alive. The bool is
        True when this call started a fresh kernel (so the caller can say so).

        Before deciding, a lazy sweep stops every kernel idle past ``idle_ttl`` (so the
        next call in a long-idle session reports ``no earlier state``), and starting a
        new kernel at the ``max_kernels`` cap first stops the least-recently-used one."""
        async with self._get_lock():
            now = self._clock()
            # Lazy idle sweep (ADR 0134 D2): close kernels unused past the TTL — their
            # state is gone, so the next call in that session starts fresh.
            for sid in self._idle_sids(now):
                await self.kernels.pop(sid).close()
            kernel = self.kernels.get(session_id)
            if kernel is not None and kernel.alive:
                kernel.last_used = now
                return kernel, False
            if kernel is not None:  # a dead kernel lingering in the map — clear it
                self.kernels.pop(session_id, None)
                await kernel.close()
            # Cap (ADR 0134 D2): at the per-instance limit, stop the least-recently-used
            # kernel before starting a new one so the live count never exceeds the cap.
            while self.kernels and len(self.kernels) >= self.max_kernels:
                lru = min(self.kernels, key=lambda s: self.kernels[s].last_used)
                await self.kernels.pop(lru).close()
            kernel = Kernel(session_id, interpreter)
            await kernel.start()
            kernel.last_used = self._clock()
            self.kernels[session_id] = kernel
            return kernel, True

    async def sweep_idle(self) -> None:
        """Stop every kernel unused past ``idle_ttl`` (the periodic backstop path; the
        same rule :meth:`get_or_start` applies lazily). Closes outside the lock."""
        now = self._clock()
        async with self._get_lock():
            victims = [self.kernels.pop(sid) for sid in self._idle_sids(now)]
        for kernel in victims:
            await kernel.close()

    async def _sweep_loop(self) -> None:
        """Periodic idle sweep so a chat that goes idle and never calls again still
        has its kernel reclaimed without waiting for the next (never-coming) call."""
        try:
            while True:
                await asyncio.sleep(max(1.0, min(self.idle_ttl, _SWEEP_INTERVAL_CAP)))
                try:
                    await self.sweep_idle()
                except Exception:  # noqa: BLE001 — a bad sweep must not kill the backstop
                    log.exception("[execute_code] periodic kernel sweep failed")
        except asyncio.CancelledError:
            raise

    def start_sweeper(self) -> None:
        """Start the periodic backstop (idempotent — a live sweeper is left running)."""
        if self._sweeper is not None and not self._sweeper.done():
            return
        self._sweeper = asyncio.ensure_future(self._sweep_loop())

    async def stop_sweeper(self) -> None:
        """Cancel the periodic backstop and await its exit (no-op if not running)."""
        task = self._sweeper
        self._sweeper = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def stop(self, session_id: str) -> None:
        async with self._get_lock():
            kernel = self.kernels.pop(session_id, None)
        if kernel is not None:
            await kernel.close()

    async def stop_all(self) -> None:
        async with self._get_lock():
            kernels = list(self.kernels.values())
            self.kernels.clear()
        for kernel in kernels:
            await kernel.close()


#: The process-wide pool. S1b wires its TTL/cap/teardown; S1a's engine dispatches here.
_pool = KernelPool()


async def run_in_kernel(
    code: str,
    tool_map: dict,
    *,
    timeout: float,
    truncate: int,
    session_id: str,
    gate=None,
    fence=None,
    fresh: bool = False,
) -> str:
    """Run ``code`` in ``session_id``'s kernel, starting it on first use.

    ``fresh`` restarts the session's kernel before running (a clean slate). A call
    that times out or finds the kernel gone kills it and reports the loss in plain
    words; the first call in a new kernel is prefixed ``new kernel: no earlier state``.
    Bridged calls bind to THIS call's ``session_id``/``gate``/``fence`` (ADR 0134 D3).
    """
    interpreter = _resolve_child_interpreter()
    if interpreter is None:
        return _NO_RUNTIME_MESSAGE
    if fresh:
        await _pool.stop(session_id)
    try:
        kernel, is_new = await _pool.get_or_start(session_id, interpreter=interpreter)
    except _KernelDied as exc:
        return f"Error: execute_code could not start a session kernel: {exc}"
    notice = "new kernel: no earlier state" if is_new else ""
    try:
        body = await kernel.run(code, tool_map=tool_map, timeout=timeout, session_id=session_id, gate=gate, fence=fence)
    except _KernelTimeout:
        await _pool.stop(session_id)
        return (
            f"Error: execute_code timed out after {timeout}s (process killed). "
            "kernel restarted: earlier variables are gone"
        )
    except _KernelDied as exc:
        await _pool.stop(session_id)
        return f"Error: execute_code kernel exited unexpectedly ({exc}). kernel restarted: earlier variables are gone"
    kernel.last_used = _pool._clock()  # reset the idle timer to this call's end
    body = _truncate_or_spill(body, truncate, session_id=session_id)
    return f"{notice}\n{body}" if notice else body


def configure(*, idle_ttl: float, max_kernels: int) -> None:
    """Apply the operator's kernel limits to the process-wide pool (ADR 0134 D2).
    The plugin's late-tool factory calls this from live config on each graph build."""
    _pool.configure(idle_ttl=idle_ttl, max_kernels=max_kernels)


def start_sweeper() -> None:
    """Start the periodic idle-sweep backstop. The plugin's background surface start
    runs this once the server loop is up; :func:`shutdown` stops it."""
    _pool.start_sweeper()


async def stop_session(session_id: str) -> None:
    """Stop and forget the kernel for ``session_id`` (no-op if none). S1b calls this
    on chat delete/clear/tab close; tests call it (or :func:`stop_all`) to leave no process."""
    await _pool.stop(session_id)


async def stop_all() -> None:
    """Stop every kernel. For server shutdown and test teardown."""
    await _pool.stop_all()


async def shutdown() -> None:
    """Stop the periodic sweeper and every kernel — the plugin's surface ``stop`` runs
    this on plugin unload/reload and server shutdown, leaving no long-lived process."""
    await _pool.stop_sweeper()
    await _pool.stop_all()
