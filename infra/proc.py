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
- **Track what you own** (#3428): :func:`track_tree` a ``group_kwargs()`` tree
  once it's spawned, :func:`untrack_tree` it once it's reaped. Whatever is still
  tracked when this process starts to exit is torn down by the process, not by
  the owner's cleanup path — which an exit may never reach.

POSIX primitives are process groups (``setsid``/``killpg``); Windows uses
``taskkill /T`` — the built-in tree walker — with every wait BOUNDED so a
stalled taskkill can never extend a caller's own timeout (#2413 review).
Windows Job Objects would be the airtight upgrade (a tree that can't detach);
taskkill is deliberate for now — no pywin32 dependency, and the frozen sidecar
stays stdlib-only. Revisit in ADR 0098 if tree-escape shows up in practice.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import os
import signal
import subprocess
import threading
import time
from typing import Any

# The #1679 liveness probe (Windows: OpenProcess + STILL_ACTIVE — never
# os.kill(pid, 0), the #1678 sidecar-suicide class). One probe, one home;
# proc re-exports it because every tree-teardown caller needs it next to
# the kill primitives.
from infra.paths import atomic_write, pid_alive

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


def signal_tree(pid: int, *, force: bool = True) -> None:
    """Synchronous, NON-BLOCKING tree signal — the cancel/teardown-path
    primitive, safe where awaiting is impossible (a ``CancelledError`` handler)
    and blocking the event loop is not acceptable.

    POSIX: ``killpg(SIGKILL|SIGTERM)`` with a direct-PID fallback — a fast
    syscall. Windows: fire-and-forget ``taskkill /T [/F]`` (no wait — the
    caller observes the effect through its own ``proc.wait()`` timeout), with a
    direct root ``TerminateProcess`` only when taskkill couldn't spawn (killing
    the root first would break taskkill's tree walk). A graceful (non-``/F``)
    taskkill often cannot stop console-less children — fine: callers escalate
    to ``force=True``, exactly like the POSIX TERM→KILL ladder. Never raises.
    """
    if _WINDOWS:
        argv = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
        spawned = False
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            spawned = True
        except (FileNotFoundError, OSError):
            pass
        if force and not spawned:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, sig)


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


# ── owned-tree registry (#3428) ──────────────────────────────────────────────
#
# A tree anchored with group_kwargs() lives in its OWN process group, so signalling
# this process's group never reaches it. Its only teardown is the owner's cleanup
# path (the shell tool's timeout kill, the ACP client's close(), a lifespan-shutdown
# pool teardown) — and an exit can pre-empt every one of those:
#
#   * the hub SIGKILLs a member 3s after SIGTERM, while the member's uvicorn drain
#     alone may take 5s, so its lifespan teardown never starts;
#   * on the desktop, the Tauri shell SIGKILLs the sidecar and the parent-death
#     watchdog `os._exit(0)`s — no lifespan shutdown, no atexit, in the hub AND in
#     every member (they inherit PROTOAGENT_PARENT_PID).
#
# Each left the trees running at ppid=1. So the process keeps its own list of the
# trees it owns and tears them down when the exit STARTS, not at the end of a
# teardown that may never run.

#: root pid -> the process group it leads (POSIX), or the pid itself (Windows).
_TRACKED: dict[int, int] = {}
# Reentrant: the signal-receipt path runs on the main thread, BETWEEN bytecodes, and
# can land while the main thread is itself inside track_tree(). A plain Lock would
# deadlock the process on the way out.
_TRACKED_LOCK = threading.RLock()
_escalation: threading.Timer | None = None
_atexit_armed = False

#: How long a SIGTERMed tree gets before the SIGKILL, on the signal-receipt path.
#: Deliberately inside the hub's 3s straggler window (`supervisor.shutdown_all`), so
#: a tree that ignores SIGTERM is dead before its member can be SIGKILLed.
TEARDOWN_GRACE = 1.5


def track_tree(pid: int) -> None:
    """Record ``pid`` — the root of a tree spawned with :func:`group_kwargs` — as a
    tree this process OWNS, so an exit can take it down (#3428). Never raises.

    Refuses a pid that shares this process's group: a tree that isn't anchored in
    its own group can't be killed by group without killing us, and the registry's
    whole reason to exist is signalling other groups.
    """
    global _atexit_armed
    if pid <= 0 or pid == os.getpid():
        return
    if _WINDOWS:
        # No groups to compare: `taskkill /T` walks from the root, so the guard is
        # simply "not us" — tracking our own pid would have the sweep kill this
        # process's whole tree, the server included.
        key = pid
    else:
        try:
            key = os.getpgid(pid)
        except OSError:
            return  # already gone — nothing to own
        if key <= 1 or key == os.getpgrp():
            return
    with _TRACKED_LOCK:
        _prune_dead_locked()
        _TRACKED[pid] = key
        _persist_locked()
        if not _atexit_armed:
            # Armed on first use, so a process that never owns a tree gets no hook.
            # Covers an ordinary interpreter exit; `os._exit` bypasses atexit, which
            # is why the watchdog calls reap_tracked_trees() itself.
            atexit.register(reap_tracked_trees)
            _atexit_armed = True


def untrack_tree(pid: int) -> None:
    """Forget ``pid`` — call once the owner has reaped the tree itself. Never raises."""
    with _TRACKED_LOCK:
        if _TRACKED.pop(pid, None) is not None:
            _persist_locked()


#: Strong refs to the pending reap-then-forget waits — an unreferenced task can be
#: garbage-collected before it ever runs.
_PENDING_FORGETS: set[asyncio.Task] = set()


def untrack_when_reaped(proc: asyncio.subprocess.Process) -> None:
    """For an owner that stops waiting on a child that is still running — a cancelled
    turn. It stays tracked while its group has anything in it (so an exit still reaches
    it) and is forgotten once the group is gone — not merely once the root is reaped.
    Call from inside the event loop. Never raises.

    Leaving it tracked forever instead is not harmless: once the group is gone its pgid
    is free for reuse, and the exit sweep would then signal whichever unrelated group
    took that id.
    """

    async def _forget_once_reaped() -> None:
        # `Exception`, not BaseException: if THIS wait is cancelled too (the loop is
        # shutting down) the child is still running, so it must stay tracked.
        with contextlib.suppress(Exception):
            await proc.wait()
            _untrack_if_group_gone(proc.pid)

    try:
        task = asyncio.get_running_loop().create_task(_forget_once_reaped())
    except RuntimeError:
        return  # no running loop — leave it tracked; the exit sweep still covers it
    _PENDING_FORGETS.add(task)
    task.add_done_callback(_PENDING_FORGETS.discard)


def _untrack_if_group_gone(pid: int) -> None:
    """Forget ``pid`` only if its whole group is gone. The ROOT being reaped is not
    enough: an abandoned ``sh -c`` often exits while the ``pnpm install`` it started
    runs on in the same group, and that surviving group is exactly the orphan the exit
    sweep exists to reach. It stays tracked; ``_prune_dead_locked`` drops it once the
    group is really gone."""
    with _TRACKED_LOCK:
        key = _TRACKED.get(pid)
        if key is None:
            return
        if _WINDOWS:
            # taskkill /T can't walk from a dead root anyway (see _signal_tracked).
            _TRACKED.pop(pid, None)
            return
        try:
            os.killpg(key, 0)
        except ProcessLookupError:
            _TRACKED.pop(pid, None)
            _persist_locked()
        except (PermissionError, OSError):
            pass


def _prune_dead_locked() -> None:
    """Drop entries whose whole group is gone. Caller holds ``_TRACKED_LOCK``.

    A group that outlived its leader is still alive here (``killpg(pgid, 0)`` reaches
    any member), which is exactly the tree that must stay tracked."""
    for pid, key in list(_TRACKED.items()):
        if _WINDOWS:
            if not pid_alive(pid):
                _TRACKED.pop(pid, None)
            continue
        try:
            os.killpg(key, 0)
        except ProcessLookupError:
            _TRACKED.pop(pid, None)
        except (PermissionError, OSError):
            pass


def tracked_trees() -> list[int]:
    """The root pids currently tracked (diagnostics and tests)."""
    with _TRACKED_LOCK:
        return list(_TRACKED)


def _drop(pid: int) -> None:
    """Forget ``pid`` WITHOUT touching the on-disk record — for the exit-signal path,
    which runs between bytecodes and must not write files. A record left naming a group
    that is gone costs nothing: the sweep skips a group that no longer exists."""
    with _TRACKED_LOCK:
        _TRACKED.pop(pid, None)


def _signal_tracked(*, force: bool, only: set[int] | None = None) -> int:
    """Signal every tracked tree once — or just the roots in ``only`` — without waiting.
    Returns how many were still there to signal; prunes the ones that are gone. Never
    raises."""
    with _TRACKED_LOCK:
        items = [(pid, key) for pid, key in _TRACKED.items() if only is None or pid in only]
    signalled = 0
    for pid, key in items:
        if _WINDOWS:
            # `taskkill /T` walks parent links FROM the root, so once the root has
            # exited its surviving descendants are unreachable here. That is ADR 0098's
            # deliberate "taskkill, not Job Objects — for now": a Job Object is the
            # airtight fix and the trigger to adopt one. (POSIX has no such gap — the
            # group is killed by its recorded pgid, root or no root.)
            if pid_alive(pid):
                signal_tree(pid, force=force)
                signalled += 1
            else:
                _drop(pid)
            continue
        try:
            # By the stored GROUP, not by getpgid(pid): the root is often the first to
            # die (a `sh -c` whose grandchild is the long-running `pnpm install`), and
            # a group outlives its leader. That surviving group is the orphan.
            os.killpg(key, signal.SIGKILL if force else signal.SIGTERM)
            signalled += 1
        except ProcessLookupError:
            _drop(pid)  # the whole group is gone
        except (PermissionError, OSError):
            pass
    return signalled


def begin_tree_teardown(*, grace: float = TEARDOWN_GRACE) -> None:
    """The signal-receipt half: SIGTERM every tracked tree NOW, then SIGKILL what's
    left after ``grace`` on a daemon timer. Non-blocking, idempotent, never raises —
    safe to call from a signal handler.

    Runs before the uvicorn drain, so the trees no longer depend on the lifespan
    teardown finishing — or starting.
    """
    global _escalation
    try:
        if not _signal_tracked(force=False):
            return
        with _TRACKED_LOCK:
            if _escalation is not None:
                return  # a second Ctrl-C re-sends TERM above; one KILL timer is enough
            timer = threading.Timer(grace, _signal_tracked, kwargs={"force": True})
            timer.daemon = True
            _escalation = timer
        timer.start()
    except Exception:  # noqa: BLE001 — a signal handler must never raise into the main thread
        pass


def reap_tracked_trees(*, grace: float = 1.0) -> int:
    """The blocking final sweep: SIGTERM every tracked tree, give them ``grace``
    seconds, SIGKILL whatever remains. Returns how many trees were signalled.

    For the points where the process is about to be gone and may block briefly:
    the end of lifespan shutdown, the parent-death watchdog before ``os._exit``,
    and ``atexit``. Returns at once when nothing is tracked. Never raises.
    """
    try:
        # The trees THIS reap is responsible for. One tracked during the grace below got
        # no SIGTERM, so it must not get the SIGKILL either, nor be forgotten: it stays
        # tracked and recorded, for atexit or — if this process never gets that far —
        # the next process's orphan sweep (#3463).
        with _TRACKED_LOCK:
            termed = set(_TRACKED)
        n = _signal_tracked(force=False, only=termed)
        if n:
            time.sleep(grace)
            _signal_tracked(force=True, only=termed)
        # Settle the on-disk record so a clean exit leaves none behind. A group that has
        # had SIGKILL — which nothing can ignore — is done even if a member still shows
        # as a zombie (a killed root this exit never waited on), so it is forgotten
        # rather than pruned by a liveness probe that would call it alive.
        with _TRACKED_LOCK:
            if n:
                for pid in termed:
                    _TRACKED.pop(pid, None)
            _prune_dead_locked()
            _persist_locked()
        return n
    except Exception:  # noqa: BLE001 — exit-path teardown is best-effort
        return 0


# ── owned-tree records: the SIGKILL / crash half (#3463) ─────────────────────
#
# The registry above lives in memory, so it dies with its owner. An owner that is
# SIGKILLed (the Tauri shell killing the hub sidecar, an OOM kill, `kill -9`) or that
# crashes runs no hook at all — not the signal-receipt teardown, not atexit, not the
# watchdog — and its trees run on at ppid=1. `codex-acp` does not even exit when its
# stdin loses its writer: sixteen of them ran for 36h (~780 MB) before a hand cleanup.
#
# So every owner also mirrors its registry to disk — one record per owning process in
# the machine-shared `.owned-trees/` — and any protoAgent process that boots, or ticks
# its periodic sweep, reaps the groups of owners that are gone. Nothing is killed on a
# guess: an owner counts as gone only when its pid is dead or now belongs to a process
# that started later, and a group is signalled only while it is provably still the
# group that was recorded (`_is_that_group`).
#
# POSIX only. Windows has no start-time probe here to tell a recycled pid from ours, and
# `taskkill /T` cannot reach a tree whose root is gone anyway — the Job Object upgrade
# ADR 0098 defers is the fix there too.

#: ``ps -o lstart=`` reads to the second; a start time within this of the record matches.
_START_TOLERANCE = 2.0
#: When each tracked root was recorded — the reference `_is_that_group` compares against.
_TRACKED_AT: dict[int, float] = {}
_owner_start: float | None = None
_owner_start_read = False


def _records_dir():
    """``<box_root>/.owned-trees`` — shared by every instance on the machine, so a
    sibling (or the next boot) can reap a dead owner's trees. None when unresolvable."""
    try:
        from infra.paths import instance_paths

        return instance_paths().owned_trees_dir
    except Exception:  # noqa: BLE001 — a record is best-effort; never break a spawn
        return None


def _proc_start(pid: int) -> float | None:
    """When ``pid`` started, in epoch seconds — or None if that can't be read (gone, or
    no probe on this platform). ``/proc`` on Linux; ``ps -o lstart=`` elsewhere."""
    if _WINDOWS or pid <= 0:
        return None
    if os.path.isdir("/proc/self"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                raw = f.read()
            ticks = int(raw[raw.rindex(b")") + 2 :].split()[19])  # field 22: starttime
            with open("/proc/stat", "rb") as f:
                btime = next(int(line.split()[1]) for line in f if line.startswith(b"btime"))
            return btime + ticks / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError, StopIteration):
            return None
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return time.mktime(time.strptime(" ".join(out.split()), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def _persist_locked() -> None:
    """Mirror the registry to this process's record — or remove the record once nothing
    is tracked. Caller holds ``_TRACKED_LOCK``. Never raises."""
    global _owner_start, _owner_start_read
    if _WINDOWS:
        return
    try:
        d = _records_dir()
        if d is None:
            return
        path = d / f"{os.getpid()}.json"
        for pid in [p for p in _TRACKED_AT if p not in _TRACKED]:
            _TRACKED_AT.pop(pid, None)
        if not _TRACKED:
            path.unlink(missing_ok=True)
            return
        if not _owner_start_read:
            _owner_start, _owner_start_read = _proc_start(os.getpid()), True
        now = time.time()
        trees = [
            {"root": pid, "pgid": key, "tracked_at": _TRACKED_AT.setdefault(pid, now)} for pid, key in _TRACKED.items()
        ]
        atomic_write(path, json.dumps({"owner": os.getpid(), "owner_start": _owner_start, "trees": trees}))
    except Exception:  # noqa: BLE001 — a record is best-effort; never break a spawn
        pass


def _owner_alive(pid: int, recorded_start: float | None) -> bool:
    """Is the process that wrote a record still running? A live pid that started at a
    different time is a stranger holding a recycled pid — the owner is gone. When the
    start time can't be read either way, lean alive: a missed reap is recoverable, a
    wrong kill is not."""
    if not pid_alive(pid):
        return False
    if recorded_start is None:
        return True
    started = _proc_start(pid)
    return started is None or abs(started - float(recorded_start)) <= _START_TOLERANCE


def _is_that_group(pgid: int, tracked_at: float) -> bool:
    """Is ``pgid`` still the group recorded at ``tracked_at`` — so signalling it cannot
    reach a stranger? Never our own group, never one we may not signal.

    * Gone (no member left): nothing to do.
    * Its leader (pid == pgid) runs: ours started BEFORE it was tracked; a process that
      took the recycled pid, and leads a group of that id, started after.
    * Leaderless but populated — the codex-acp shape, a launcher that died with its
      binary still running: POSIX never reissues a pid that is still a live group's id,
      so this is still the recorded group."""
    if pgid <= 1 or pgid == os.getpgrp():
        return False
    try:
        os.killpg(pgid, 0)
    except OSError:  # ProcessLookupError: gone · PermissionError: not ours to signal
        return False
    if pid_alive(pgid):
        started = _proc_start(pgid)
        return started is not None and started <= tracked_at + _START_TOLERANCE
    return True


def sweep_orphaned_trees(*, grace: float = 1.0) -> int:
    """Reap the trees of owners that died without tearing them down — SIGKILLed,
    crashed, OOM-killed (#3463). Returns how many groups it signalled. Never raises.

    Reads every record in ``.owned-trees/`` except this process's own. A record whose
    owner still runs is left alone; for a dead owner, each group that passes
    ``_is_that_group`` gets SIGTERM, then — re-checked, since the pid could in principle
    be handed on once the group is gone — SIGKILL after ``grace``. The record is
    removed once its groups have been dealt with. Blocking (it shells out to ``ps`` and
    waits ``grace``): call it off the event loop."""
    if _WINDOWS:
        return 0
    try:
        d = _records_dir()
        if d is None or not d.is_dir():
            return 0
        done: list = []
        doomed: list[tuple[int, float]] = []
        for path in d.glob("*.json"):
            try:
                owner = int(path.stem)
            except ValueError:
                continue
            if owner == os.getpid():
                continue
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
                trees = rec.get("trees") or []
                if not isinstance(trees, list):
                    raise TypeError("trees is not a list")
                owner_start = rec.get("owner_start")
                if owner_start is not None:
                    owner_start = float(owner_start)
            except (OSError, ValueError, TypeError, AttributeError):
                # Unreadable or not the shape we write (`[]`, a truncated edit): it can
                # guard nothing, so it goes — and it must never stop this sweep, or every
                # later one, from reaching the records behind it.
                done.append(path)
                continue
            if _owner_alive(owner, owner_start):
                continue
            for tree in trees:
                try:
                    pgid, at = int(tree["pgid"]), float(tree["tracked_at"])
                except (KeyError, TypeError, ValueError):
                    continue
                if _is_that_group(pgid, at):
                    doomed.append((pgid, at))
            done.append(path)
        for pgid, _at in doomed:
            with contextlib.suppress(OSError):
                os.killpg(pgid, signal.SIGTERM)
        if doomed:
            time.sleep(grace)
            for pgid, at in doomed:
                if _is_that_group(pgid, at):
                    with contextlib.suppress(OSError):
                        os.killpg(pgid, signal.SIGKILL)
        for path in done:
            with contextlib.suppress(OSError):
                path.unlink()
        return len(doomed)
    except Exception:  # noqa: BLE001 — a sweep is best-effort housekeeping
        return 0


__all__ = [
    "akill_tree",
    "begin_tree_teardown",
    "detached_kwargs",
    "group_kwargs",
    "kill_tree",
    "pid_alive",
    "reap_tracked_trees",
    "signal_tree",
    "sweep_orphaned_trees",
    "terminate_tree",
    "track_tree",
    "tracked_trees",
    "untrack_tree",
    "untrack_when_reaped",
]
