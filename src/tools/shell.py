"""Async subprocess helper for shell/CLI-backed tools.

A small, neutral foundation for any tool that shells out — the generic core of
the protoLabs fleet's ``BaseShellTool`` (pwnDeck), stripped of its
offensive-security specifics. Complements ``tools/gh_cli.py`` (which is
``gh``-specific). Handles the things every shell tool gets wrong:

- timeout + process kill,
- missing binary → a structured ``error`` (never a raised ``FileNotFoundError``),
- env merge over the current environment,
- captured stdout/stderr (text), optional stdin and cwd.

    res = await run_command(["git", "rev-parse", "HEAD"])
    return res.stdout if res.ok else f"Error: {res.error or res.stderr}"
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from infra.proc import akill_tree, child_env, group_kwargs, track_tree, untrack_tree, untrack_when_reaped


_WINDOWS = os.name == "nt"
# asyncio's own default StreamReader limit (``asyncio.streams._DEFAULT_LIMIT``).
_STREAM_LIMIT = 2**16
# Once the top-level process has exited, how long to keep collecting output that is already
# buffered before giving up on the readers (#5207). A POSIX child started with ``&`` inherits
# the captured stdout/stderr and holds them open after the shell returns; this bound is what
# stops a read-to-EOF from blocking on that still-running child. Only the backgrounded case
# ever reaches it — a foreground command's readers finish on EOF well before it.
_DRAIN_GRACE = 0.5
# How often to poll for the process's own exit (#5207) — see :func:`_await_process_exit`.
_EXIT_POLL = 0.02


def cmd_command_line(command: str, comspec: str | None = None) -> str:
    """The Windows command line that runs ``command`` under ``cmd.exe`` VERBATIM (#3802).

    ``cmd /s /c "<command>"`` strips exactly the outer quote pair and parses the rest as
    typed, so ``"C:\\Program Files\\Tool\\tool.exe" --version`` reaches cmd intact. Handing
    cmd an argv list instead lets Python's ``subprocess.list2cmdline`` rewrite every
    embedded ``"`` as ``\\"`` — an MSVCRT escape cmd.exe doesn't speak, so it saw a
    program literally named ``\\"C:\\...\\"``. ``/d`` skips AutoRun, as before.

    Pure string building (no platform check) so it is unit-testable everywhere; run the
    result with :func:`run_command`, which passes a ``str`` to ``CreateProcess`` as is.
    """
    if comspec is None:
        comspec = os.environ.get("COMSPEC", "cmd.exe")
    # Quote the interpreter itself only if it needs it (a COMSPEC under a spaced path).
    return f'{subprocess.list2cmdline([comspec])} /d /s /c "{command}"'


def _program_of(command_line: str) -> str:
    """The program token of a Windows command line — for error messages only."""
    if command_line.startswith('"'):
        return command_line[1:].split('"', 1)[0]
    return command_line.split(" ", 1)[0]


async def _spawn_command_line(command_line: str, **kwds) -> asyncio.subprocess.Process:
    """``asyncio.create_subprocess_exec`` for a pre-built Windows command line.

    The public API can't express this: ``loop.subprocess_exec`` always hands Popen the
    tuple ``(program, *args)``, which Windows Popen joins with ``list2cmdline``;
    ``loop.subprocess_shell`` wraps the string in its own ``ComSpec /c "…"``. Popen given a
    ``str`` passes it to ``CreateProcess`` unchanged, so this is ``create_subprocess_exec``
    with the one difference that ``args`` is that string — same transport, same
    ``Process`` (so ``communicate``, timeouts and the tree-kill all behave identically).
    """
    loop = asyncio.get_running_loop()
    protocol = asyncio.subprocess.SubprocessStreamProtocol(limit=_STREAM_LIMIT, loop=loop)
    transport = await loop._make_subprocess_transport(  # noqa: SLF001 — see docstring
        protocol, command_line, False, kwds.pop("stdin"), kwds.pop("stdout"), kwds.pop("stderr"), 0, **kwds
    )
    return asyncio.subprocess.Process(transport, protocol, loop)


@dataclass
class ShellResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None  # set when the command couldn't run at all

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.returncode == 0


class _Capture:
    """A byte sink a reader appends to, switchable to discard mode. While a command runs we
    keep everything it writes (no cap — same as ``communicate``); once we've harvested a
    backgrounded command's output, :meth:`stop` frees it and makes further reads no-ops, so the
    detached drain that keeps the child's pipe flowing doesn't buffer its output forever."""

    __slots__ = ("_chunks", "_keep")

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._keep = True

    def feed(self, chunk: bytes) -> None:
        if self._keep:
            self._chunks.append(chunk)

    def bytes(self) -> bytes:
        return b"".join(self._chunks)

    def stop(self) -> None:
        self._keep = False
        self._chunks = []


async def _pump(stream: asyncio.StreamReader | None, sink: _Capture) -> None:
    """Read ``stream`` into ``sink`` until EOF — or until cancelled, keeping what it read."""
    if stream is None:
        return
    while True:
        chunk = await stream.read(_STREAM_LIMIT)
        if not chunk:
            break
        sink.feed(chunk)


async def _await_process_exit(proc: asyncio.subprocess.Process) -> None:
    """Return once the process itself has exited — NOT once its pipes close (#5207).

    ``proc.wait()``/``proc.communicate()`` resolve only when every captured pipe has reached
    EOF (``BaseSubprocessTransport`` gates its exit waiters on ``connection_lost`` for all
    pipes), so a child backgrounded with ``&`` that inherits stdout/stderr holds them open and
    keeps both blocked. ``proc.returncode``, by contrast, is set by the child watcher the
    moment the process is reaped, independent of the pipes — so poll it. The caller's
    ``wait_for`` bounds this, so an unbounded poll can't outlive the command's timeout.
    """
    while proc.returncode is None:
        await asyncio.sleep(_EXIT_POLL)


async def _communicate_until_exit(proc: asyncio.subprocess.Process, stdin: str | None) -> tuple[bytes, bytes]:
    """Like ``proc.communicate`` but returns when the TOP-LEVEL process exits, not when the
    captured pipes reach EOF (#5207, POSIX).

    A command that backgrounds a child with ``&`` leaves that child holding the inherited
    stdout/stderr open after the shell itself has returned, so ``communicate`` — which waits
    for EOF — would block until every background child exits. Instead drain both streams
    concurrently (so a chatty foreground command can't deadlock on a full pipe), wait for the
    process to exit, then collect whatever it already wrote with a short bound and stop. The
    background child is left running — its pipes kept drained-and-discarded by a detached task
    so it neither blocks on a full pipe nor dies with ``SIGPIPE`` when we stop capturing. The
    caller's ``wait_for`` still bounds the whole call, and cancelling it tears everything down.
    """
    out = _Capture()
    err = _Capture()
    readers = [
        asyncio.ensure_future(_pump(proc.stdout, out)),
        asyncio.ensure_future(_pump(proc.stderr, err)),
    ]
    detached = False
    try:
        if stdin is not None and proc.stdin is not None:
            proc.stdin.write(stdin.encode())
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                await proc.stdin.drain()
            proc.stdin.close()
        await _await_process_exit(proc)
        # The process has exited; its own output is in the pipe buffer now. Collect it with a
        # short bound rather than waiting for an EOF a backgrounded child may withhold.
        await asyncio.wait(readers, timeout=_DRAIN_GRACE)
        captured = out.bytes(), err.bytes()
        if not all(r.done() for r in readers):
            # A backgrounded child still holds the pipes open. Keep draining them (discarding,
            # via stop()) in a detached task so the child stays alive and unblocked; it ends
            # when the child finally closes the pipes.
            out.stop()
            err.stop()
            _spawn_background_drain(proc, readers)
            detached = True
        return captured
    finally:
        if not detached:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)


#: Strong refs to detached background drains — a task (and the process it keeps referenced) that
#: is otherwise unreferenced can be garbage-collected, which would close the inherited pipes and
#: ``SIGPIPE`` the still-running child. Dropped once the child closes the pipes on its own.
_BACKGROUND_DRAINS: set[asyncio.Task] = set()


def _spawn_background_drain(proc: asyncio.subprocess.Process, readers: list[asyncio.Task]) -> None:
    """Keep ``proc``'s inherited pipes drained (output already switched to discard) until its
    backgrounded child closes them, so the child keeps running — neither blocked on a full pipe
    nor killed by a ``SIGPIPE`` from a closed read end. Holds a strong ref to the drain so it
    isn't GC'd (which would close the pipes)."""

    async def _drain() -> None:
        with contextlib.suppress(Exception):
            await asyncio.gather(*readers)
        with contextlib.suppress(Exception):
            await proc.wait()  # keeps proc referenced until the pipes are truly gone

    try:
        task = asyncio.ensure_future(_drain())
    except RuntimeError:
        # No running loop (shouldn't happen inside run_command): fall back to dropping the pipes.
        for reader in readers:
            reader.cancel()
        return
    _BACKGROUND_DRAINS.add(task)
    task.add_done_callback(_BACKGROUND_DRAINS.discard)


async def run_command(
    argv: Sequence[str] | str,
    *,
    timeout: float = 30.0,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    base_env: dict[str, str] | None = None,
) -> ShellResult:
    """Run ``argv`` as an async subprocess, returning a ``ShellResult``.

    Never raises for the common failure modes: a missing binary or a timeout
    come back as ``error`` / ``timed_out`` so callers can return a clean tool
    string. ``env`` is merged over the current environment — the frozen-bundle-scrubbed
    one (:func:`infra.proc.child_env`), since a command can start something that
    outlives this server (``zed .``, a daemon). ``base_env`` REPLACES that base (a caller
    that already scrubbed it — the agent-facing ``run_command``, see
    :func:`infra.proc.scrub_agent_env`); ``env`` still merges on top.

    ``argv`` as a ``str`` is a complete Windows command line (see :func:`cmd_command_line`),
    passed to ``CreateProcess`` verbatim instead of being re-quoted from a list. Windows-only.

    On POSIX the call returns once the top-level process exits, NOT once the captured pipes
    reach EOF (#5207): a child backgrounded with ``&`` inherits stdout/stderr and keeps them
    open, and waiting for EOF would block until it too exits. Whatever the command itself
    wrote is captured; the background child is left running on the inherited pipes.
    """
    raw = isinstance(argv, str)
    binary = (_program_of(argv) if raw else (argv[0] if argv else "?")) or "?"
    if raw and not _WINDOWS:
        return ShellResult(1, "", "", error="a raw command line is Windows-only — pass an argv list.")
    merged_env = dict(base_env) if base_env is not None else child_env()
    if env is not None:
        merged_env.update(env)

    proc = None
    try:
        spawn_kwargs = dict(
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            cwd=cwd,
            # Anchor the child as its own tree root so a timeout can kill the
            # whole tree — the launched command may create children of its own.
            **group_kwargs(),
        )
        if raw:
            proc = await _spawn_command_line(argv, **spawn_kwargs)
        else:
            proc = await asyncio.create_subprocess_exec(*argv, **spawn_kwargs)
    except FileNotFoundError:
        return ShellResult(1, "", "", error=f"{binary!r} is not installed or not on PATH.")
    except OSError as exc:
        return ShellResult(1, "", "", error=f"failed to launch {binary!r}: {exc}")

    # Owned until reaped (#3428): if this process starts to exit mid-command, the
    # tree is torn down with it rather than left running at ppid=1.
    track_tree(proc.pid)
    try:
        # POSIX: return when the shell exits, not when its (possibly inherited) pipes hit EOF
        # (#5207). Windows can't orphan the captured pipes to a `&`-backgrounded child the way
        # POSIX does, so it keeps the simple read-to-EOF path untouched.
        read = (
            proc.communicate(stdin.encode() if stdin is not None else None)
            if _WINDOWS
            else _communicate_until_exit(proc, stdin)
        )
        out, err = await asyncio.wait_for(read, timeout=timeout)
    except asyncio.TimeoutError:
        # Kill the whole process tree, not just the immediate child: a shell command can
        # spawn children (pipes, &, $(…)) that would otherwise be orphaned on timeout.
        await akill_tree(proc)
        try:
            await proc.communicate()
        except Exception:  # noqa: BLE001 — process already killed; draining is best-effort
            pass
        return ShellResult(1, "", "", timed_out=True, error=f"timed out after {timeout:g}s")
    finally:
        # Only once it's actually reaped. A cancelled turn leaves the command running:
        # it stays tracked while it runs, so the process's exit still reaches it, and is
        # forgotten when it finishes on its own.
        if proc.returncode is not None:
            untrack_tree(proc.pid)
        else:
            untrack_when_reaped(proc)

    return ShellResult(
        returncode=proc.returncode or 0,
        stdout=out.decode(errors="replace").strip(),
        stderr=err.decode(errors="replace").strip(),
    )
