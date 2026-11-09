"""The one hardened way to run the `agent-browser` CLI — shared by the tools and the plugin client.

Extracted from ``tools.py`` so a second caller (``client.BrowserClient``, the browser client
other plugins get through the ``agent_browser.client`` service) runs every command through the
SAME protections instead of a bare ``subprocess.run``:

* **an aggregate stdout+stderr byte cap** — the pipes are drained incrementally and the child is
  killed the moment the total crosses it, so untrusted page output can't pile up in memory;
* **process-group kill** on timeout or overflow — the CLI runs in its own session, so one
  ``killpg`` reaches a grandchild that would otherwise hold the pipes open;
* **bounded joins** — a descendant that left the group can't pin the calling thread;
* **stdin feeding** on its own thread, so a large script can't deadlock against the drains.

It is mechanism only: no config, no banner, no message wording. Callers turn a :class:`RunResult`
into their own contract (the tools into ``Error: …`` strings, the client into typed results).
Dependency-free (no langchain), so the client imports it without dragging in the tool stack.

Also here: the per-session **guard** both callers take, so a plugin client and the browser tools
that drive the SAME session can't interleave (see :func:`guard`).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("protoagent.plugins.agent_browser")

READ_CHUNK = 65_536
# How long to wait for the pipe-draining threads once the CLI has exited (or been killed).
# Normally they finish at once; see run() for the case where they can't.
JOIN_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class RunResult:
    """What one CLI run did. Exactly one of the failure fields explains a non-success."""

    out: bytes = b""
    err: bytes = b""
    returncode: int | None = None
    timed_out: bool = False
    overflow: bool = False  # the aggregate output crossed the cap; ``out`` holds up to it
    launch_error: OSError | None = None  # Popen itself failed (no binary / not startable)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return (self.launch_error is None and not self.timed_out and not self.overflow
                and self.returncode == 0)

    def stdout(self) -> str:
        return self.out.decode("utf-8", "replace").strip()

    def stderr(self) -> str:
        return self.err.decode("utf-8", "replace").strip()


def kill_tree(proc) -> None:
    """Kill the CLI AND everything it started. It runs in its own session
    (``start_new_session=True``), so its pid is its process-group id and one ``killpg``
    reaches a grandchild that would otherwise keep our pipes open — the case that left the
    drain threads, and the ``asyncio.to_thread`` worker joined on them, blocked forever."""
    try:
        proc.kill()
    except OSError:
        pass
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and hasattr(os, "killpg"):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:  # group already gone (ProcessLookupError) or not ours
            pass


def run(argv: list[str], *, env: dict, timeout: float, max_bytes: int,
        stdin: str | None = None) -> RunResult:
    """Run ``argv`` (``[exe, *args]``) once under the protections in the module doc.

    Uses Popen with one drain thread per pipe (concurrent, so a full stderr can't deadlock
    stdout). On overflow the child is killed and reading stops AT the cap (memory stays
    bounded). On timeout the child's whole group is killed. Either way it is reaped — no
    zombies. ``stdin``, when given, is written by a feeder thread (``eval --stdin``: a script in
    argv overruns Windows' 32,767-char command line, #3689)."""
    started = time.monotonic()
    try:
        # Its own session: its pid becomes a process-group id, so a timeout can kill
        # everything it started (see kill_tree). Ignored on Windows.
        proc = subprocess.Popen(argv,
                                stdin=subprocess.PIPE if stdin is not None else None,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, env=env)
    except OSError as e:
        return RunResult(launch_error=e, seconds=time.monotonic() - started)

    out_buf, err_buf = bytearray(), bytearray()
    total = 0
    overflow = False
    lock = threading.Lock()

    def _drain(pipe, buf: bytearray) -> None:
        nonlocal total, overflow
        hit = False
        try:
            # read1, not read: BufferedReader.read(n) blocks until it has n bytes or EOF,
            # so when a descendant holds the pipe open (no EOF, ever) the CLI's last
            # output sat in the reader's buffer and a bounded join returned NOTHING.
            # read1 hands over whatever has arrived.
            read = getattr(pipe, "read1", pipe.read)
            for block in iter(lambda: read(READ_CHUNK), b""):
                with lock:
                    if overflow:
                        break
                    room = max_bytes - total
                    total += len(block)
                    if total > max_bytes:  # aggregate crossed the cap → stop + kill
                        buf.extend(block[:max(room, 0)])  # keep up to the cap, never past it
                        overflow = True
                        hit = True
                        break
                    buf.extend(block)
        except (OSError, ValueError):
            pass  # pipe closed under us (e.g. after kill) — nothing more to read
        finally:
            try:
                pipe.close()
            except Exception:
                pass
        if hit:
            kill_tree(proc)  # unblock the sibling reader and let wait() return

    drains = [threading.Thread(target=_drain, args=(proc.stdout, out_buf), daemon=True),
              threading.Thread(target=_drain, args=(proc.stderr, err_buf), daemon=True)]
    for t in drains:
        t.start()

    feeder = None
    if stdin is not None:
        def _feed() -> None:
            # The drains are already reading stdout/stderr, so a child that streams
            # output while it consumes a large script can't wedge this write.
            try:
                proc.stdin.write(stdin.encode("utf-8"))
            except (OSError, ValueError):
                pass  # child closed its end / exited early — its exit code carries the story
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
        feeder = threading.Thread(target=_feed, daemon=True)
        feeder.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)  # terminate the CLI and anything it started …
        try:
            proc.wait(timeout=5)  # … and reap, so we never leave a zombie
        except subprocess.TimeoutExpired:
            pass
    # BOUNDED joins. The kill above takes the CLI's whole process group, which is what
    # closes the pipes in practice. A descendant that left the group (its own setsid) can
    # still hold them; the drains then stay blocked in `pipe.read()` — daemon threads,
    # they end when it does — but THIS call returns instead of pinning an
    # asyncio.to_thread worker for the life of the process (the reviewer's reproducer: a
    # CLI that backgrounds `sleep 300`). What the CLI wrote before exiting was already
    # read, so the snapshot below is its full output.
    for t in drains:
        t.join(timeout=JOIN_TIMEOUT_S)
    if feeder is not None:
        feeder.join(timeout=JOIN_TIMEOUT_S)  # the write is done once the child exits
    if any(t.is_alive() for t in drains):
        log.warning("[agent_browser] `agent-browser %s` exited but something it started "
                    "kept its output open; returning what it wrote", " ".join(argv[1:3]))
    with lock:  # a still-running drain may append: take a consistent snapshot
        out_bytes, err_bytes = bytes(out_buf), bytes(err_buf)
    return RunResult(out=out_bytes, err=err_bytes, returncode=proc.returncode,
                     timed_out=timed_out, overflow=overflow, seconds=time.monotonic() - started)


# ── one guard per agent-browser session, across threads and event loops ──────────────────
# The tools serialise their own parallel calls with a per-loop asyncio.Lock (tools.py). A plugin
# client runs in a worker thread (a sync tool) or on another loop, which an asyncio.Lock can't
# see — so when a client drives the SAME session as the tools, both also take this process-wide
# guard, keyed by session name: a client sequence and a browser_fill can't interleave keystrokes.
# Different sessions never contend. Owner-reentrant: a client's own multi-step sequence (open →
# wall probe) re-enters it; another owner waits.


class SessionGuard:
    """A lock with a reentrant owner token. ``owner`` is any hashable: the client passes its
    thread id, a tool call a fresh object (a tool never re-enters)."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._owner: object = None
        self._depth = 0

    def try_acquire(self, owner: object) -> bool:
        with self._cond:
            if self._depth and self._owner != owner:
                return False
            self._owner, self._depth = owner, self._depth + 1
            return True

    def acquire(self, owner: object, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._depth and self._owner != owner:
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._cond.wait(left)
            self._owner, self._depth = owner, self._depth + 1
            return True

    def release(self, owner: object) -> None:
        with self._cond:
            if not self._depth or self._owner != owner:
                return  # not held by this owner — never release someone else's hold
            self._depth -= 1
            if not self._depth:
                self._owner = None
                self._cond.notify_all()

    @property
    def held(self) -> bool:
        with self._cond:
            return bool(self._depth)


_GUARDS: dict[str, SessionGuard] = {}
_GUARDS_LOCK = threading.Lock()


def guard(session: str) -> SessionGuard:
    """The process-wide guard for agent-browser session ``session`` (created on first use)."""
    with _GUARDS_LOCK:
        g = _GUARDS.get(session)
        if g is None:
            g = _GUARDS[session] = SessionGuard()
        return g
