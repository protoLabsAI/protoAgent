"""Chrome for Testing, installed by the CLI's own ``agent-browser install`` — on the
operator's click only.

The CLI drives a Chrome; on a fresh host there's none it can find, and ``doctor`` says so
(``chrome.installed: fail``). The fix is the CLI's own ``install`` subcommand, which
downloads Chrome for Testing (~150 MB, from Google's Chrome-for-Testing endpoints) into
``~/.agent-browser/browsers``. That's an operator decision — a large download into their
home directory — so it runs ONLY from the Chrome setup gap's "Install Chrome" button (the
``install-chrome`` step), never silently from a tool call.

It runs the SAME CLI the tools use (``preflight.resolve_binary`` — the downloaded one when
that's what resolves), on a daemon thread with a generous bound, and reports through the
gap: "installing…" while it runs (no button, so no second click), cleared by the re-probe
once Chrome is there, or the CLI's own error with a Retry button when it isn't. Linux ARM64
is refused up front: Chrome for Testing publishes no build for it (the CLI itself exits 1
saying so), so the gap points at the distro's Chromium instead of offering a button that
can only fail.

State lives in a process-stable ``sys.modules`` slot, like ``cli_fetch``'s.
"""

from __future__ import annotations

import logging
import platform as _platform
import re
import subprocess
import sys
import threading
import time
import types

log = logging.getLogger("protoagent.plugins.agent_browser")

INSTALL_TIMEOUT_S = 20 * 60
MAX_ERROR_CHARS = 400
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
    """One install. ``on_done`` (the banner refresh) runs before the slot reads idle again."""
    try:
        _install(exe, timeout)
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
        # UTF-8 with replacement, never the locale codec: the CLI prints ✓/✗, which a cp1252
        # decode (Windows) would turn into a UnicodeDecodeError instead of an install result.
        p = subprocess.run([exe, "install"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        _finish(state="failed", error=f"`agent-browser install` didn't finish within {int(timeout // 60)} min")
    except OSError as e:
        _finish(state="failed", error=f"couldn't start {exe}: {e.strerror or e}")
    except Exception as e:  # noqa: BLE001 — a thread must not die silently with the state stuck
        _finish(state="failed", error=f"{type(e).__name__}: {e}")
    else:
        if p.returncode == 0:
            _finish(state="done", error="", output=_tail(p.stdout))
            log.info("[agent_browser] Chrome for Testing installed: %s", _tail(p.stdout))
        else:
            _finish(state="failed", error=_tail(p.stderr) or _tail(p.stdout) or f"exit {p.returncode}",
                    output=_tail(f"{p.stdout}\n{p.stderr}"))
            log.warning("[agent_browser] `agent-browser install` failed (exit %s): %s", p.returncode,
                        _tail(p.stderr) or _tail(p.stdout))
