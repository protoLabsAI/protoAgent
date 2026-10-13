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
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from infra.proc import akill_tree, child_env, group_kwargs, track_tree, untrack_tree, untrack_when_reaped


_WINDOWS = os.name == "nt"
# asyncio's own default StreamReader limit (``asyncio.streams._DEFAULT_LIMIT``).
_STREAM_LIMIT = 2**16


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
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None),
            timeout=timeout,
        )
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
