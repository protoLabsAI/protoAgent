"""Shared async ``gh`` CLI runner for the GitHub read tools.

A thin wrapper around the GitHub CLI: timeout + kill, missing-binary
detection, and token injection. The container ships ``gh`` (see the
Dockerfile); locally it's whatever is on PATH.

Auth: if ``GITHUB_TOKEN`` (or ``GH_TOKEN``) is set it's injected into the
subprocess env; otherwise ``gh`` uses its own ambient auth (``gh auth
login``). No token is required for public-repo reads at low volume.

Adapted from the protoLabs fleet (quinn ``tools/gh_cli.py``), with the
GitHub-App token plumbing dropped — core uses a plain token.
"""

from __future__ import annotations

import asyncio
import os

_COMMAND_TIMEOUT = 30


def _resolve_token() -> str | None:
    """Pick a GitHub token from the env, or None to use gh's ambient auth."""
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None


async def run_gh(args: list[str], timeout: int = _COMMAND_TIMEOUT) -> tuple[int, str, str]:
    """Run a ``gh`` command, returning ``(returncode, stdout, stderr)``.

    Times out (killing the process), and reports a clean error when ``gh``
    isn't installed instead of raising.
    """
    env = os.environ.copy()
    token = _resolve_token()
    if token:
        env["GITHUB_TOKEN"] = token

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode or 0,
            stdout.decode(errors="replace").strip(),
            stderr.decode(errors="replace").strip(),
        )
    except asyncio.TimeoutError:
        if proc is not None:
            proc.kill()
        return 1, "", f"gh command timed out after {timeout}s"
    except FileNotFoundError:
        return 1, "", "gh CLI is not installed or not on PATH."


def check_gh_error(returncode: int, stderr: str) -> str | None:
    """Return a formatted ``Error: ...`` string if the command failed, else None."""
    if returncode != 0:
        return f"Error (gh exit {returncode}): {stderr[:500]}"
    return None
