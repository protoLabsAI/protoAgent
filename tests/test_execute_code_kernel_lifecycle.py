"""Session-kernel limits and teardown (ADR 0134 D2, S1b).

Real child processes (like tests/test_execute_code_kernel.py from S1a): these
exercise the idle TTL, the per-instance cap and the chat-teardown wiring end to
end. Idle expiry is driven by a FAKE monotonic clock the test advances — never a
real wait — so nothing here sleeps (a real sleep in a test fails check_test_policy).
Every test leaves no kernel process alive.
"""

import pytest
import pytest_asyncio

from infra import paths
from plugins.execute_code import _on_session_event, kernel, register

pytestmark = pytest.mark.platform_sensitive


def _pid_alive(pid: int | None) -> bool:
    """Cross-platform liveness via ``infra.paths.pid_alive`` (#1678), as in S1a."""
    return pid is not None and paths.pid_alive(pid)


@pytest_asyncio.fixture(autouse=True)
async def _bounded_pool():
    """Save the pool's limits/clock, restore them after, and leave no kernel process
    alive (ADR 0134 S1b acceptance): stop the periodic sweeper AND every kernel."""
    pool = kernel._pool
    saved = (pool.idle_ttl, pool.max_kernels, pool._clock)
    yield
    pids = [k.pid for k in pool.kernels.values()]
    await kernel.shutdown()  # stop the periodic sweeper and every kernel
    pool.idle_ttl, pool.max_kernels, pool._clock = saved
    assert pool.kernels == {}
    for pid in pids:
        assert not _pid_alive(pid), f"kernel pid {pid} survived teardown"


async def _run(code, session_id, *, timeout=30.0):
    return await kernel.run_in_kernel(code, {}, timeout=timeout, truncate=6000, session_id=session_id)


@pytest.mark.asyncio
async def test_idle_kernel_stops_after_ttl_then_next_call_is_a_new_kernel():
    pool = kernel._pool
    now = [0.0]
    pool._clock = lambda: now[0]  # a fake monotonic clock: advance it instead of waiting
    pool.idle_ttl = 5.0  # a short TTL; the clock (not a real sleep) drives expiry

    await _run("saved = 'before'", "sidle")
    old_pid = pool.kernels["sidle"].pid
    assert _pid_alive(old_pid)

    now[0] = 100.0  # the kernel is now idle well past the 5s TTL
    await pool.sweep_idle()  # the periodic backstop reclaims it — no wait, no next call
    assert "sidle" not in pool.kernels
    assert not _pid_alive(old_pid)

    out = await _run("print('after')", "sidle")  # the next call starts a fresh kernel
    assert "new kernel: no earlier state" in out
    assert "after" in out
    gone = await _run("try:\n    print(saved)\nexcept NameError:\n    print('state-gone')", "sidle")
    assert "state-gone" in gone  # the stopped kernel's earlier state did not carry over


@pytest.mark.asyncio
async def test_lazy_sweep_on_next_call_restarts_the_idle_session():
    pool = kernel._pool
    now = [0.0]
    pool._clock = lambda: now[0]
    pool.idle_ttl = 5.0

    await _run("kept = 1", "slazy")
    old_pid = pool.kernels["slazy"].pid
    now[0] = 50.0  # idle past the TTL
    # No explicit sweep: get_or_start's lazy sweep stops the idle kernel and starts anew.
    out = await _run("print('fresh')", "slazy")
    assert "new kernel: no earlier state" in out
    assert not _pid_alive(old_pid)
    assert pool.kernels["slazy"].pid != old_pid


@pytest.mark.asyncio
async def test_cap_evicts_least_recently_used_and_leaves_the_other_running():
    pool = kernel._pool
    pool.max_kernels = 2
    pool.idle_ttl = 10_000.0  # keep the idle sweep out of this test

    await _run("a = 1", "cap_a")
    await _run("b = 2", "cap_b")
    a_pid = pool.kernels["cap_a"].pid
    b_pid = pool.kernels["cap_b"].pid
    assert _pid_alive(a_pid) and _pid_alive(b_pid)

    # cap_a is the least-recently-used (started first, untouched since); starting a
    # third session's kernel at the cap stops it and leaves cap_b running.
    out = await _run("c = 3", "cap_c")
    assert "new kernel: no earlier state" in out
    assert set(pool.kernels) == {"cap_b", "cap_c"}
    assert not _pid_alive(a_pid)  # the LRU kernel was stopped
    assert _pid_alive(b_pid)  # the other kernel kept running
    assert _pid_alive(pool.kernels["cap_c"].pid)


@pytest.mark.asyncio
async def test_session_end_events_stop_only_that_sessions_kernel():
    pool = kernel._pool
    pool.idle_ttl = 10_000.0

    await _run("x = 1", "end_del")
    await _run("y = 2", "end_close")
    del_pid = pool.kernels["end_del"].pid
    close_pid = pool.kernels["end_close"].pid

    # session.changed with the deleted status (chat deleted or cleared) stops only end_del.
    await _on_session_event({"event": "session.changed", "data": {"session_id": "end_del", "status": "deleted"}})
    assert "end_del" not in pool.kernels
    assert not _pid_alive(del_pid)
    assert "end_close" in pool.kernels and _pid_alive(close_pid)

    # session.closed (tab closed) stops only end_close.
    await _on_session_event({"event": "session.closed", "data": {"session_id": "end_close"}})
    assert "end_close" not in pool.kernels
    assert not _pid_alive(close_pid)


@pytest.mark.asyncio
async def test_active_session_changed_does_not_stop_the_kernel():
    pool = kernel._pool
    pool.idle_ttl = 10_000.0

    await _run("z = 1", "active")
    pid = pool.kernels["active"].pid
    # A running/idle transition is active use — the chat is still open, so the kernel
    # (and its state) must survive it. Only the deleted status tears down.
    for status in ("running", "idle", "paused"):
        await _on_session_event({"event": "session.changed", "data": {"session_id": "active", "status": status}})
        assert "active" in pool.kernels and _pid_alive(pid)


@pytest.mark.asyncio
async def test_register_subscribes_teardown_to_both_topics_and_a_surface_stop():
    subs: list[tuple[str, object]] = []
    surfaces: list[dict] = []

    class _FakeRegistry:
        config: dict = {}

        def live_config(self) -> dict:
            return {}

        def register_late_tool_factory(self, factory) -> None:
            pass

        def register_surface(self, start, stop=None, name=None, reload=None) -> None:
            surfaces.append({"start": start, "stop": stop, "name": name})

        def on(self, topic, handler):
            subs.append((topic, handler))
            return None

    register(_FakeRegistry())
    assert {t for t, _ in subs} == {"session.changed", "session.closed"}
    assert all(h is _on_session_event for _, h in subs)
    assert surfaces and callable(surfaces[0]["stop"])  # the unload/reload stop is wired


@pytest.mark.asyncio
async def test_shutdown_stops_every_kernel_and_the_sweeper():
    pool = kernel._pool
    pool.idle_ttl = 10_000.0
    kernel.start_sweeper()  # the surface's periodic backstop is running

    await _run("p = 1", "shut_a")
    await _run("q = 2", "shut_b")
    pids = [pool.kernels["shut_a"].pid, pool.kernels["shut_b"].pid]
    assert all(_pid_alive(pid) for pid in pids)

    await kernel.shutdown()  # what the plugin's surface stop runs on unload/reload/shutdown
    assert pool.kernels == {}
    assert pool._sweeper is None  # the periodic sweeper was cancelled too
    for pid in pids:
        assert not _pid_alive(pid)
