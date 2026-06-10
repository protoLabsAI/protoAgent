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
from dataclasses import dataclass


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
    argv: list[str],
    *,
    timeout: float = 30.0,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> ShellResult:
    """Run ``argv`` as an async subprocess, returning a ``ShellResult``.

    Never raises for the common failure modes: a missing binary or a timeout
    come back as ``error`` / ``timed_out`` so callers can return a clean tool
    string. ``env`` is merged over the current environment.
    """
    merged_env = None
    if env is not None:
        merged_env = os.environ.copy()
        merged_env.update(env)

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            cwd=cwd,
        )
    except FileNotFoundError:
        binary = argv[0] if argv else "?"
        return ShellResult(1, "", "", error=f"{binary!r} is not installed or not on PATH.")
    except OSError as exc:
        return ShellResult(1, "", "", error=f"failed to launch {argv[0]!r}: {exc}")

    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.communicate()
        except Exception:  # noqa: BLE001 — process already killed; draining is best-effort
            pass
        return ShellResult(1, "", "", timed_out=True, error=f"timed out after {timeout:g}s")

    return ShellResult(
        returncode=proc.returncode or 0,
        stdout=out.decode(errors="replace").strip(),
        stderr=err.decode(errors="replace").strip(),
    )
