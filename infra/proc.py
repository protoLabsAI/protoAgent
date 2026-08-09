"""Cross-platform process-TREE lifecycle — the one place that knows how to
anchor a child's tree at spawn and how to take the whole tree down (ADR 0098).

Four subsystems spawn children that spawn children — the agent shell tool, ACP
delegates, fleet members, and the ``protoagent up`` CLI — and each had its own
POSIX-only copy of the same two moves: *anchor* the child in its own process
group at spawn, and *kill by group* on stop/timeout so grandchildren can't
survive as orphans. None of those moves exist on Windows (`os.killpg`,
``SIGKILL``, ``setsid()``), which is how the Windows release shipped with
timed-out commands leaking children (#2412).

The contract:

- **Anchor at spawn** with :func:`group_kwargs` (a tree you own and will kill —
  shell commands, ACP delegates) or :func:`detached_kwargs` (a tree that must
  SURVIVE you — fleet members, ``protoagent up`` servers).
- **Kill by tree**, never by PID alone: :func:`kill_tree` (sync),
  :func:`akill_tree` (asyncio children), :func:`terminate_tree` (graceful
  term → wait → hard-kill escalation).

POSIX primitives are process groups (``setsid``/``killpg``); Windows uses
``taskkill /T`` — the built-in tree walker — with every wait BOUNDED so a
stalled taskkill can never extend a caller's own timeout (#2413 review).
Windows Job Objects would be the airtight upgrade (a tree that can't detach);
taskkill is deliberate for now — no pywin32 dependency, and the frozen sidecar
stays stdlib-only. Revisit in ADR 0098 if tree-escape shows up in practice.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from typing import Any

# The #1679 liveness probe (Windows: OpenProcess + STILL_ACTIVE — never
# os.kill(pid, 0), the #1678 sidecar-suicide class). One probe, one home;
# proc re-exports it because every tree-teardown caller needs it next to
# the kill primitives.
from infra.paths import pid_alive

#: Bound every taskkill wait (seconds) — a stalled tree-kill must not extend the
#: caller's own timeout budget; the immediate-kill fallback still runs after it.
_TASKKILL_WAIT = 5.0

_WINDOWS = os.name == "nt"


# ── spawn anchoring ──────────────────────────────────────────────────────────


def group_kwargs() -> dict[str, Any]:
    """``Popen``/``create_subprocess_*`` kwargs anchoring the child as the root
    of its own process tree — for children you own and will kill as a tree
    (shell commands, ACP delegates).

    POSIX: ``start_new_session=True`` (setsid ⇒ its own process group, so
    ``killpg`` reaches every descendant). Windows: ``CREATE_NEW_PROCESS_GROUP``
    (``start_new_session`` is silently ignored there); the tree walk itself is
    ``taskkill /T``, which follows parent links regardless of group.
    """
    if _WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def detached_kwargs() -> dict[str, Any]:
    """``Popen`` kwargs for a child that must SURVIVE this process — fleet
    members and ``protoagent up`` servers, which outlive the CLI that spawned
    them and are stopped later by PID (:func:`terminate_tree`).

    POSIX: same ``setsid()`` detach as before (a new session detaches from the
    controlling terminal). Windows: ``CREATE_NEW_PROCESS_GROUP`` so a Ctrl-C /
    console event aimed at the CLI can't propagate into the server, plus
    ``CREATE_NO_WINDOW`` so the detached server doesn't flash a console.
    """
    if _WINDOWS:
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        }
    return {"start_new_session": True}


# ── tree teardown ────────────────────────────────────────────────────────────


def _taskkill(pid: int, *, force: bool) -> None:
    """Run ``taskkill /T`` against ``pid``, wait bounded, never raise."""
    argv = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
    try:
        subprocess.run(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_TASKKILL_WAIT,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        # Constrained environment without taskkill, or a stalled walk — the
        # caller's immediate-kill fallback still runs.
        pass


def kill_tree(pid: int, *, force: bool = True) -> None:
    """Synchronously take down ``pid`` and every descendant, best-effort.

    POSIX: ``killpg(getpgid(pid), SIGKILL|SIGTERM)`` with a direct-PID fallback
    (the child may not lead its own group). Windows: ``taskkill /T [/F]`` with a
    bounded wait, then a direct ``TerminateProcess`` via ``os.kill`` fallback.
    Never raises.
    """
    if _WINDOWS:
        _taskkill(pid, force=force)
        if force and pid_alive(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(os.getpgid(pid), sig)
        return
    except (ProcessLookupError, PermissionError):
        pass
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, sig)


async def akill_tree(proc: asyncio.subprocess.Process) -> None:
    """Async tree-kill for an asyncio child spawned with :func:`group_kwargs`.

    The #2416 shell-tool logic, extracted: Windows walks the tree with
    ``taskkill /T /F`` (wait bounded at ``_TASKKILL_WAIT``, a stalled killer is
    reaped — #2413 review), POSIX ``killpg``s the group; both fall through to
    killing the immediate child. Never raises.
    """
    if _WINDOWS:
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(killer.communicate(), timeout=_TASKKILL_WAIT)
            except asyncio.TimeoutError:
                # A stalled taskkill must not extend the caller's own timeout —
                # reap it and fall through to the immediate kill.
                with contextlib.suppress(ProcessLookupError):
                    killer.kill()
                await killer.wait()
        except (FileNotFoundError, OSError):
            # taskkill unavailable in a constrained Windows environment.
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass

    try:
        proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


def terminate_tree(pid: int, *, grace: float = 5.0, poll: float = 0.1) -> bool:
    """Graceful tree stop: ask nicely, wait up to ``grace``, then hard-kill.

    POSIX: ``SIGTERM`` the group → poll → ``SIGKILL`` the group. Windows:
    ``taskkill /T`` (WM_CLOSE-style) → poll → ``taskkill /T /F``. Returns True
    when the root PID is gone. This is the stop-side primitive for detached
    children (fleet members, ``protoagent down``).

    When the caller is the DIRECT PARENT of ``pid``, the dead child lingers as
    a zombie that ``pid_alive`` reads as alive — so each poll best-effort reaps
    it (``waitpid(WNOHANG)``; a not-our-child pid just raises and is ignored).
    """
    import time

    def _gone() -> bool:
        if not _WINDOWS:
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(pid, os.WNOHANG)  # reap if it's our zombie child
        return not pid_alive(pid)

    kill_tree(pid, force=False)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if _gone():
            return True
        time.sleep(poll)
    kill_tree(pid, force=True)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if _gone():
            return True
        time.sleep(poll)
    return _gone()


__all__ = [
    "akill_tree",
    "detached_kwargs",
    "group_kwargs",
    "kill_tree",
    "pid_alive",
    "terminate_tree",
]
