"""Chrome for Testing, installed by the CLI's own ``agent-browser install`` — on the
operator's click only.

The CLI drives a Chrome; on a fresh host there's none it can find, and ``doctor`` says so
(``chrome.installed: fail``). The fix is the CLI's own ``install`` subcommand, which
downloads Chrome for Testing (~150 MB, from Google's Chrome-for-Testing endpoints) into
``~/.agent-browser/browsers``. That's an operator decision — a large download into their
home directory — so it runs ONLY from the Chrome setup gap's "Install Chrome" button (the
``install-chrome`` step), never silently from a tool call.

It runs the SAME CLI the tools use (``preflight.resolve_binary`` — the downloaded one when
that's what resolves), on a daemon thread, and reports through the gap: "installing…" while
it runs (no button, so no second click), cleared by the re-probe once Chrome is there, or the
CLI's own error with a Retry button when it isn't. Linux ARM64 is refused up front: Chrome for
Testing publishes no build for it (the CLI itself exits 1 saying so), so the gap points at the
distro's Chromium instead of offering a button that can only fail.

**The bound is real.** ``subprocess.run(timeout=)`` isn't one: on timeout it kills only the
direct child and then ``communicate()``s with NO timeout, so anything the CLI started that
still holds its pipes would pin this thread — and the state on "installing" — forever. So:
the same shape as the browser tools (``tools._run``) — ``Popen`` as the root of its own process
group (``infra.proc.group_kwargs``), a drain thread per pipe, and on timeout the WHOLE tree
killed (``infra.proc.kill_tree``: ``killpg`` on POSIX, ``taskkill /T`` on Windows) with the
drain joins bounded, so a descendant that escaped the group can't hold the result hostage.

State lives in a process-stable ``sys.modules`` slot, like ``cli_fetch``'s.
"""

from __future__ import annotations

import contextlib
import logging
import platform as _platform
import re
import subprocess
import sys
import threading
import time
import types

from infra.proc import group_kwargs, kill_tree

log = logging.getLogger("protoagent.plugins.agent_browser")

INSTALL_TIMEOUT_S = 20 * 60
MAX_ERROR_CHARS = 400
# Output kept per pipe: the TAIL (the error is at the end); progress lines ahead of it can go.
MAX_OUTPUT_BYTES = 256 * 1024
# How long to wait for the pipe drains once the CLI has exited or been killed. Normally they
# finish at once; a descendant that left the process group can hold the pipes open.
_JOIN_TIMEOUT_S = 5.0
# The CLI colours its status lines (✓ / ✗ indicators); strip that before it reaches a banner.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SLOT_NAME = "agent_browser.chrome_install::state"


def supported(system: str | None = None, machine: str | None = None) -> bool:
    """False on Linux ARM64, where Chrome for Testing has no build (``agent-browser
    install`` exits 1 there)."""
    s = (system or _platform.system()).strip().lower()
    m = (machine or _platform.machine()).strip().lower()
    return not (s == "linux" and m in ("aarch64", "arm64"))


def _fresh() -> dict:
    return {"state": "idle", "error": "", "output": "", "started": 0.0, "finished": 0.0}


def _slot():
    holder = sys.modules.get(_SLOT_NAME)
    if holder is None:
        holder = types.ModuleType(_SLOT_NAME)
        holder.__doc__ = "Process-stable holder for agent_browser's Chrome install state — data, not code."
        holder.state = _fresh()
        holder.lock = threading.Lock()
        holder.idle = threading.Event()  # set ⇔ no install in flight
        holder.idle.set()
        holder = sys.modules.setdefault(_SLOT_NAME, holder)
    return holder


def state() -> dict:
    """A copy of ``{state, error, output, started, finished}`` — ``state`` ∈ idle /
    installing / done / failed."""
    holder = _slot()
    with holder.lock:
        return dict(holder.state)


def reset_state() -> None:
    """Tests only."""
    holder = _slot()
    with holder.lock:
        holder.state = _fresh()
        holder.idle.set()


def _finish(**fields) -> None:
    holder = _slot()
    with holder.lock:
        holder.state.update(finished=time.time(), **fields)


def _tail(text: str) -> str:
    """The last few non-blank lines, colour codes stripped, bounded — enough to say WHY."""
    lines = [ln.strip() for ln in _ANSI.sub("", text or "").splitlines() if ln.strip()]
    return " ".join(lines[-3:])[-MAX_ERROR_CHARS:]


def _human(seconds: float) -> str:
    return f"{seconds / 60:g} min" if seconds >= 60 else f"{seconds:g}s"


def start(exe: str, *, on_done=None, background: bool = True, timeout: float = INSTALL_TIMEOUT_S) -> dict:
    """Run ``<exe> install`` — one at a time: while one runs, this returns its state rather
    than starting a second. ``on_done`` runs after it finishes (the plugin passes its gap
    refresh). Returns ``state()``."""
    holder = _slot()
    with holder.lock:
        if holder.state["state"] == "installing":
            return dict(holder.state)
        holder.state.update(state="installing", error="", output="", started=time.time(), finished=0.0)
        holder.idle.clear()
    if background:
        threading.Thread(target=_run, args=(exe, timeout, on_done), name="agent-browser-chrome-install",
                         daemon=True).start()
    else:
        _run(exe, timeout, on_done)
    return state()


def _run(exe: str, timeout: float, on_done) -> None:
    """One install. ``on_done`` (the banner refresh) runs before the slot reads idle again,
    and the state always leaves "installing" — whatever ``_install`` does."""
    try:
        try:
            _install(exe, timeout)
        except Exception as e:  # noqa: BLE001 — the state must never stay stuck on "installing"
            _finish(state="failed", error=f"{type(e).__name__}: {e}")
            log.exception("[agent_browser] the Chrome install failed unexpectedly")
        if callable(on_done):
            try:
                on_done()
            except Exception:  # noqa: BLE001
                log.exception("[agent_browser] refreshing the setup gap after the Chrome install failed")
    finally:
        _slot().idle.set()


def _install(exe: str, timeout: float) -> None:
    log.info("[agent_browser] installing Chrome for Testing: %s install", exe)
    try:
        proc = subprocess.Popen([exe, "install"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, **group_kwargs())
    except OSError as e:
        _finish(state="failed", error=f"couldn't start {exe}: {e.strerror or e}")
        return

    out_buf, err_buf = bytearray(), bytearray()
    lock = threading.Lock()

    def _drain(pipe, buf: bytearray) -> None:
        try:
            read = getattr(pipe, "read1", pipe.read)
            for block in iter(lambda: read(65536), b""):
                with lock:
                    buf.extend(block)
                    if len(buf) > MAX_OUTPUT_BYTES:
                        del buf[: len(buf) - MAX_OUTPUT_BYTES]
        except (OSError, ValueError):
            pass  # the pipe closed under us (after a kill) — nothing more to read
        finally:
            with contextlib.suppress(Exception):
                pipe.close()

    drains = [threading.Thread(target=_drain, args=(proc.stdout, out_buf), daemon=True),
              threading.Thread(target=_drain, args=(proc.stderr, err_buf), daemon=True)]
    for t in drains:
        t.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if isinstance(proc.pid, int):
            kill_tree(proc.pid)  # the CLI AND everything it started (its own process group)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)  # reap — never leave a zombie
    for t in drains:
        t.join(timeout=_JOIN_TIMEOUT_S)  # BOUNDED: an escaped descendant can't pin this thread
    if any(t.is_alive() for t in drains):
        log.warning("[agent_browser] `agent-browser install` ended but something it started kept its "
                    "output open; reporting what it wrote")
    with lock:  # a still-running drain may append: take a consistent snapshot
        stdout = bytes(out_buf).decode("utf-8", "replace")
        stderr = bytes(err_buf).decode("utf-8", "replace")

    if timed_out:
        _finish(state="failed", error=f"`agent-browser install` didn't finish within {_human(timeout)}, so it "
                                      f"was stopped", output=_tail(f"{stdout}\n{stderr}"))
        log.warning("[agent_browser] `agent-browser install` timed out after %s — killed its process tree",
                    _human(timeout))
    elif proc.returncode == 0:
        _finish(state="done", error="", output=_tail(stdout))
        log.info("[agent_browser] Chrome for Testing installed: %s", _tail(stdout))
    else:
        _finish(state="failed", error=_tail(stderr) or _tail(stdout) or f"exit {proc.returncode}",
                output=_tail(f"{stdout}\n{stderr}"))
        log.warning("[agent_browser] `agent-browser install` failed (exit %s): %s", proc.returncode,
                    _tail(stderr) or _tail(stdout))
