"""Process memory ceiling (#3365).

There is no OS-level RSS ceiling to lean on. Darwin refuses
``setrlimit(RLIMIT_RSS)`` outright — so launchd's ``SoftResourceLimits`` /
``HardResourceLimits`` keys, which are a thin wrapper over ``setrlimit``, cannot
express one either — and Linux has ignored ``RLIMIT_RSS`` since 2.4. A runaway
process therefore has no backstop below *the host runs out of memory*, which is
exactly how three leaking instances saturated a 32 GB machine's memory
compressor and left it unresponsive for the better part of a day.

So the ceiling lives in-process. This samples RSS on a cadence and says so —
loudly, and once per episode — when the process crosses the configured ceiling.

**Exiting is opt-in.** A server that kills itself mid-turn drops that turn, and
whether that trade is worth making depends on what's supervising the process.
That belongs to the operator, not to a default every deployment inherits. The
ceiling defaults to off; enabling it warns; exiting takes a second, explicit
knob.

Decisions live in :class:`MemoryCeiling`, which never logs and never exits — it
is a pure function of (rss, config) so the interesting cases are testable
without spawning anything. The caller does the shouting and the dying.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

# EX_TEMPFAIL. "Transient — restart me", which is what a supervisor should do with
# a process that hit a memory ceiling; distinguishable from a config/crash exit.
EXIT_CODE = 75

_MB = 1024 * 1024

# Warn once when the reading mechanism isn't available on this platform, rather
# than every cadence forever.
_UNAVAILABLE_LOGGED = False


def read_rss_bytes() -> int | None:
    """Current resident set size in bytes, or ``None`` where we can't read it.

    Deliberately *current* RSS rather than ``resource.getrusage().ru_maxrss``:
    that is a high-water mark, so a process that spiked once and recovered would
    stay tripped forever. The cadence is minutes, so the macOS subprocess is not
    a cost worth optimising away.
    """
    global _UNAVAILABLE_LOGGED
    try:
        if sys.platform.startswith("linux"):
            # statm field 2 is resident pages.
            with open("/proc/self/statm", encoding="ascii") as fh:
                resident_pages = int(fh.read().split()[1])
            return resident_pages * os.sysconf("SC_PAGE_SIZE")
        if sys.platform == "darwin":
            out = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(os.getpid())],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            return int(out) * 1024 if out else None
    except Exception:  # noqa: BLE001 — a guard that can't read memory must not raise into the loop
        log.debug("[memory] RSS read failed", exc_info=True)
        return None
    # Windows and anything else: no reader wired. Say so once, then stay quiet —
    # the guard disables itself rather than pretending to protect the process.
    if not _UNAVAILABLE_LOGGED:
        _UNAVAILABLE_LOGGED = True
        log.warning("[memory] no RSS reader for platform %r — memory ceiling inactive", sys.platform)
    return None


def _parse_exit_flag(value: object) -> bool:
    """Strictly: does the operator want this process to exit on breach?

    ``bool()`` is the wrong tool here. This value comes from YAML, where quoting
    is easy to get wrong, and ``bool("false")`` is ``True`` — so a config that
    plainly reads ``memory_ceiling_exit: "false"`` would start killing the
    process. For a knob whose whole job is to end the process, an unrecognized
    value has to fail safe rather than fail loud-and-armed.

    Real booleans win. The one realistic mistake — a quoted ``"true"`` /
    ``"false"`` — is honored. Everything else is off.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"true", "false"}:
            return token == "true"
    if value not in (None, "", False):
        log.warning(
            "[memory] ignoring runtime.memory_ceiling_exit=%r (not a boolean) — leaving exit-on-breach OFF",
            value,
        )
    return False


class MemoryCeiling:
    """Decides what a given RSS reading means. Never logs, never exits.

    Breach is latched, so crossing the ceiling warns once rather than once per
    sample. Dropping back under clears the latch, so a genuine second episode is
    reported again.
    """

    def __init__(self, ceiling_mb: int | float | None, *, exit_on_breach: object = False) -> None:
        try:
            mb = int(ceiling_mb or 0)
        except (TypeError, ValueError, OverflowError):
            # OverflowError is float("inf"), which int() refuses. Both values arrive
            # straight from YAML, so "anything at all" is the real input domain.
            mb = 0
        self.ceiling_bytes = max(0, mb) * _MB
        self.exit_on_breach = _parse_exit_flag(exit_on_breach)
        self._breached = False

    @property
    def enabled(self) -> bool:
        """0 (the default) disables the ceiling — the repo's convention for a
        threshold knob, cf. ``telemetry_retention_days``."""
        return self.ceiling_bytes > 0

    def evaluate(self, rss_bytes: int | None) -> tuple[str, str]:
        """``(action, message)`` where action is ``"none"``, ``"warn"`` or ``"exit"``.

        ``"exit"`` is only ever returned when the operator asked for it; otherwise
        a breach is ``"warn"`` no matter how far over the ceiling it is.
        """
        if not self.enabled or rss_bytes is None:
            return "none", ""
        if rss_bytes < self.ceiling_bytes:
            self._breached = False  # recovered — a later breach is a new episode
            return "none", ""
        if self._breached:
            return "none", ""  # already reported this episode
        self._breached = True
        msg = (
            f"[memory] RSS {rss_bytes / _MB:.0f} MB crossed the configured ceiling "
            f"of {self.ceiling_bytes / _MB:.0f} MB"
        )
        if self.exit_on_breach:
            return "exit", f"{msg} — exiting {EXIT_CODE} so the supervisor restarts this process"
        return "warn", (
            f"{msg}. Not exiting (runtime.memory_ceiling_exit is off). If this keeps climbing, "
            "the process is leaking — capture a tracemalloc snapshot before restarting it."
        )
