"""Process memory ceiling (#3365).

No OS-level RSS ceiling exists to lean on — Darwin refuses ``setrlimit(RLIMIT_RSS)``
outright, so launchd's resource-limit keys can't express one, and Linux ignores it.
A leak therefore has no backstop below the host running out of memory. These tests
pin the in-process substitute, and in particular that it stays *quiet and harmless*
by default: off unless configured, and warning-only unless exiting is asked for.
"""

from __future__ import annotations

import sys

import pytest

from infra.memory_guard import EXIT_CODE, MemoryCeiling, read_rss_bytes

_MB = 1024 * 1024


def test_disabled_by_default() -> None:
    """0 is off — the repo's convention for a threshold knob."""
    guard = MemoryCeiling(0)
    assert guard.enabled is False
    assert guard.evaluate(500 * _MB) == ("none", "")


def test_under_the_ceiling_is_silent() -> None:
    guard = MemoryCeiling(100)
    assert guard.evaluate(99 * _MB)[0] == "none"


def test_breach_warns_but_does_not_exit_by_default() -> None:
    """THE default-safety property: crossing the ceiling never kills the process
    unless the operator explicitly asked for that."""
    guard = MemoryCeiling(100)
    action, message = guard.evaluate(150 * _MB)
    assert action == "warn"
    assert "150" in message and "100" in message


def test_breach_exits_only_when_opted_in() -> None:
    guard = MemoryCeiling(100, exit_on_breach=True)
    action, message = guard.evaluate(150 * _MB)
    assert action == "exit"
    assert str(EXIT_CODE) in message


def test_breach_is_reported_once_per_episode() -> None:
    """A ceiling crossed at a 60s cadence must not emit a line a minute forever."""
    guard = MemoryCeiling(100)
    assert guard.evaluate(150 * _MB)[0] == "warn"
    assert guard.evaluate(160 * _MB)[0] == "none"
    assert guard.evaluate(900 * _MB)[0] == "none"


def test_recovery_rearms_the_warning() -> None:
    """Dropping back under is a genuine recovery, so a later breach is news again."""
    guard = MemoryCeiling(100)
    assert guard.evaluate(150 * _MB)[0] == "warn"
    assert guard.evaluate(50 * _MB)[0] == "none"  # recovered
    assert guard.evaluate(150 * _MB)[0] == "warn"  # new episode


def test_unreadable_rss_is_not_a_breach() -> None:
    """A platform with no reader must not be mistaken for a process at 0 bytes —
    reading nothing has to mean 'no opinion', never 'under the ceiling' and
    certainly never 'exit'."""
    guard = MemoryCeiling(100, exit_on_breach=True)
    assert guard.evaluate(None) == ("none", "")


@pytest.mark.parametrize("bad", ["", None, "not-a-number", -5])
def test_garbage_ceiling_disables_rather_than_raises(bad) -> None:
    """The value arrives from YAML, so it can be anything. A guard that raises on
    config would take down the loop it exists to protect."""
    assert MemoryCeiling(bad).enabled is False


@pytest.mark.skipif(
    not (sys.platform == "darwin" or sys.platform.startswith("linux")),
    reason="no RSS reader wired for this platform (guard self-disables there)",
)
def test_reads_a_plausible_rss_on_supported_platforms() -> None:
    rss = read_rss_bytes()
    assert rss is not None
    # A live CPython running a test suite is comfortably inside this range; the
    # point is to catch a unit mix-up (KB read as bytes, or pages as bytes).
    assert 5 * _MB < rss < 20_000 * _MB, f"implausible RSS: {rss}"


def test_config_knob_defaults_to_off() -> None:
    """Wiring check: the dataclass default must keep the guard inert, so merely
    upgrading never changes how an existing deployment behaves."""
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig()
    assert cfg.memory_ceiling_mb == 0
    assert cfg.memory_ceiling_exit is False
    assert MemoryCeiling(cfg.memory_ceiling_mb, exit_on_breach=cfg.memory_ceiling_exit).enabled is False


# ── Config values arrive from YAML, so "anything at all" is the input domain ──
# (#3373 review). Both knobs must fail SAFE: the ceiling disables, and the exit
# flag stays off, rather than raising into the loop or arming a process kill.


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_ceiling_disables_rather_than_raising(bad) -> None:
    """int(float('inf')) raises OverflowError — which the guard must absorb."""
    assert MemoryCeiling(bad).enabled is False


@pytest.mark.parametrize("truthy_string", ["true", "True", " TRUE "])
def test_quoted_true_is_honored(truthy_string) -> None:
    """A quoted YAML bool is the one realistic mistake worth accepting."""
    assert MemoryCeiling(100, exit_on_breach=truthy_string).exit_on_breach is True


@pytest.mark.parametrize("falsey", ["false", "False", " false ", "", None, 0])
def test_quoted_false_and_absent_stay_off(falsey) -> None:
    """`bool("false")` is True — which would arm a process kill from a config that
    plainly reads false. This is the whole reason the flag isn't parsed with bool()."""
    assert MemoryCeiling(100, exit_on_breach=falsey).exit_on_breach is False


@pytest.mark.parametrize("junk", ["yes", "1", "on", 1, 2.5, [], {"a": 1}, object()])
def test_unrecognized_exit_values_fail_safe(junk) -> None:
    """Anything not plainly a boolean leaves the process-killing switch OFF."""
    assert MemoryCeiling(100, exit_on_breach=junk).exit_on_breach is False


def test_a_breach_with_a_junk_exit_flag_only_warns() -> None:
    """End to end: the fail-safe parse must actually keep os._exit out of reach."""
    guard = MemoryCeiling(100, exit_on_breach="false")
    assert guard.evaluate(500 * _MB)[0] == "warn"


def test_guard_is_cancelled_before_shutdown_awaits_anything() -> None:
    """The memory guard must be cancelled before the first await in shutdown (#3365).

    It is the only background task that can end the process outright (`os._exit` on
    breach). Every cleanup step in `_scheduler_shutdown` yields to the loop — fleet
    members, mDNS withdrawal, surface stops, scheduler, cache warmer — so a guard
    still running through them can exit mid-teardown and skip the rest. Shutdown is
    also when a long-lived process sits closest to its ceiling, so the race is real.

    Source inspection rather than behavior: the ordering is the invariant, and it
    would otherwise regress silently the next time someone tidies that function
    (this test exists because it already did once). Same approach as
    `tests/test_tracing.py`'s boot-order assertion.
    """
    import inspect
    import re

    import server

    src = inspect.getsource(server._main)
    body = src[src.index("async def _scheduler_shutdown"):]
    body = body[: body.index("\n    @app.on_event") if "\n    @app.on_event" in body else len(body)]

    cancel = body.index("STATE.memory_guard_task.cancel()")
    first_await = re.search(r"\bawait\b", body)
    assert first_await, "shutdown no longer awaits anything — re-check this invariant"
    assert cancel < first_await.start(), (
        "memory_guard_task must be cancelled BEFORE the first await in "
        "_scheduler_shutdown — otherwise a breach mid-teardown os._exit()s and "
        "skips the remaining cleanup."
    )
