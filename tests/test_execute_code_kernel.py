"""Session kernels for execute_code (ADR 0134 S1a).

Real child processes (like tests/test_execute_code.py): these exercise the
long-lived kernel, its code-frame driver loop and the per-call tool bridge end
to end. The timeout test blocks the child with ``time.sleep(99)`` — a
never-returns stub the test kills, NOT a real wait — so nothing here sleeps.
"""

import pytest
import pytest_asyncio
from langchain_core.tools import tool

from infra import paths
from plugins.execute_code import kernel
from plugins.execute_code.engine import build_execute_code_tool

pytestmark = pytest.mark.platform_sensitive


def _pid_alive(pid: int | None) -> bool:
    """Cross-platform liveness via ``infra.paths.pid_alive`` (#1678). A raw
    ``os.kill(pid, 0)`` is NOT a probe on Windows — signal 0 is ``CTRL_C_EVENT``,
    which would *terminate* a live kernel and raise ``OSError`` for a dead pid."""
    return pid is not None and paths.pid_alive(pid)


@pytest_asyncio.fixture(autouse=True)
async def _no_leaked_kernels():
    """Every test leaves no kernel process alive (ADR 0134 S1a acceptance): the
    pool is empty and every pid it held is gone after teardown."""
    yield
    pids = [k.pid for k in kernel._pool.kernels.values()]
    await kernel.stop_all()
    assert kernel._pool.kernels == {}
    for pid in pids:
        assert not _pid_alive(pid), f"kernel pid {pid} survived teardown"


async def _run(code, session_id, *, tool_map=None, timeout=30.0, truncate=6000, fresh=False):
    return await kernel.run_in_kernel(
        code,
        tool_map or {},
        timeout=timeout,
        truncate=truncate,
        session_id=session_id,
        fresh=fresh,
    )


@tool
async def echo_tool(text: str) -> str:
    """Echo back the given text, uppercased."""
    return text.upper()


@pytest.mark.asyncio
async def test_variable_carries_across_calls_in_a_session():
    out1 = await _run("x = 41", "s1")
    assert "new kernel: no earlier state" in out1  # first call in a fresh kernel
    out2 = await _run("print(x + 1)", "s1")
    assert "42" in out2
    assert "new kernel" not in out2  # the kernel was reused, not restarted


@pytest.mark.asyncio
async def test_two_sessions_do_not_share_variables():
    await _run("y = 'from_a'", "sa")
    isolated = await _run("try:\n    print(y)\nexcept NameError:\n    print('isolated')", "sb")
    assert "isolated" in isolated
    # session sa still has its own y
    still_a = await _run("print(y)", "sa")
    assert "from_a" in still_a


@pytest.mark.asyncio
async def test_empty_session_id_takes_one_shot_path():
    ec = build_execute_code_tool([], session_state=True)
    await ec.ainvoke({"code": "z = 7", "state": {"session_id": ""}})
    # No kernel is keyed by "" — the empty session degrades to the one-shot path…
    assert kernel._pool.kernels == {}
    # …so the variable does NOT persist to the next call.
    out = await ec.ainvoke(
        {"code": "try:\n    print(z)\nexcept NameError:\n    print('no-persist')", "state": {"session_id": ""}}
    )
    assert "no-persist" in out
    assert kernel._pool.kernels == {}


@pytest.mark.asyncio
async def test_fresh_true_clears_state():
    await _run("w = 5", "sf")
    assert "5" in await _run("print(w)", "sf")
    out = await _run("try:\n    print(w)\nexcept NameError:\n    print('cleared')", "sf", fresh=True)
    assert "cleared" in out
    assert "new kernel: no earlier state" in out  # fresh restarts the kernel


@pytest.mark.asyncio
async def test_timeout_kills_kernel_then_next_call_is_a_new_kernel():
    await _run("keep = 123", "st")
    # time.sleep(99) is a never-returns stub the timeout kills — not a real wait.
    timed_out = await _run("import time\ntime.sleep(99)", "st", timeout=0.5)
    assert "timed out" in timed_out
    assert "kernel restarted: earlier variables are gone" in timed_out
    # The kernel was killed; the next call starts a fresh one and the earlier var is gone.
    nxt = await _run("try:\n    print(keep)\nexcept NameError:\n    print('gone')", "st")
    assert "new kernel: no earlier state" in nxt
    assert "gone" in nxt


@pytest.mark.asyncio
async def test_result_does_not_leak_into_the_next_call():
    out1 = await _run("result = {'first': 1}", "sr")
    assert "first" in out1  # call 1 returns its own result
    out2 = await _run("print('second call')", "sr")
    assert "second call" in out2
    assert "--- result ---" not in out2  # call 1's result is cleared before call 2
    assert "first" not in out2


@pytest.mark.asyncio
async def test_bridged_tool_call_works_in_kernel():
    out = await _run("print(tools.echo_tool(text='hi'))", "sb1", tool_map={"echo_tool": echo_tool})
    assert "HI" in out


@pytest.mark.asyncio
async def test_concurrent_bridged_calls_in_kernel_never_cross():
    # 16 concurrent bridged calls over the kernel's single bridge; each must get
    # its own distinct reply (#5418), in order, with none crossing.
    code = (
        "from concurrent.futures import ThreadPoolExecutor\n"
        "with ThreadPoolExecutor(8) as pool:\n"
        "    vals = list(pool.map(lambda n: tools.echo_tool(text=chr(97 + n)), range(16)))\n"
        "print(''.join(vals))\n"
    )
    out = await _run(code, "sb2", tool_map={"echo_tool": echo_tool})
    assert "ABCDEFGHIJKLMNOP" in out


@pytest.mark.asyncio
async def test_stop_session_leaves_no_process():
    await _run("v = 1", "sx")
    pid = kernel._pool.kernels["sx"].pid
    assert _pid_alive(pid)
    await kernel.stop_session("sx")
    assert "sx" not in kernel._pool.kernels
    assert not _pid_alive(pid)
